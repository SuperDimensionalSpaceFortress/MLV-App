#!/usr/bin/env python3
"""Fail-closed validation and full-frame comparison for MLV-App output A/B evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import subprocess
import sys
import zlib
import os
from pathlib import Path, PureWindowsPath
from typing import Any


SCHEMA = "mlvapp.output-budget-spec.v1"
REPORT_SCHEMA = "mlvapp.output-budget-report.v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")
STAMP = re.compile(rb"MLVAPP_BUILDSTAMP_v1\|sha=([0-9a-f]{40})\|dirty=([01])")


class ContractError(ValueError):
    pass


class FileBindingError(FileNotFoundError):
    """A pinned file is absent or differs from its pin; ``kind`` is MISSING, LENGTH_MISMATCH or HASH_MISMATCH."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _is_absolute_path(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def resolve_receipt_path(spec: dict[str, Any], repo_root: Path) -> Path:
    """The pinned receipt: an absolute path as written, or a repo-relative path resolved against ``repo_root``."""
    raw = spec["receipt"]["path"]
    return Path(raw) if _is_absolute_path(raw) else (repo_root / raw).resolve()


def _pairs(pairs: list[tuple[str, Any]], where: str = "object") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate key {key!r} in {where}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        raw = path.read_bytes().decode("utf-8", errors="strict")
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ContractError(f"non-finite JSON value {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON {path}: {exc}") from exc


def exact_keys(value: Any, required: set[str], where: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"{where} must be an object")
    actual = set(value)
    if actual != required:
        raise ContractError(f"{where} keys differ: missing={sorted(required-actual)} extra={sorted(actual-required)}")
    return value


def is_int(value: Any) -> bool:
    return type(value) is int


def is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def require_hash(value: Any, where: str, length: int = 64) -> str:
    pattern = HEX64 if length == 64 else HEX40
    if type(value) is not str or not pattern.fullmatch(value):
        raise ContractError(f"{where} must be {length} hexadecimal characters")
    return value.upper() if length == 64 else value.lower()


def validate_spec(spec: Any) -> dict[str, Any]:
    root = exact_keys(spec, {"schema", "shippingDefaults", "baseline", "receipt", "profiles", "clips", "budgets", "legacyPolicyAllowlist"}, "spec")
    if root["schema"] != SCHEMA:
        raise ContractError(f"unsupported schema {root['schema']!r}")
    shipping = exact_keys(root["shippingDefaults"], {"path", "sha256"}, "shippingDefaults")
    baseline = exact_keys(root["baseline"], {"status", "commit", "executableSha256", "artifact"}, "baseline")
    if baseline["status"] == "reviewed_instrumented_known_good":
        require_hash(baseline["commit"], "baseline.commit", 40)
        require_hash(baseline["executableSha256"], "baseline.executableSha256")
        if type(baseline["artifact"]) is not str or not baseline["artifact"]:
            raise ContractError("baseline.artifact must identify the reviewed frozen artifact")
    elif baseline["status"] == "pending_instrumented_known_good_bridge":
        if any(baseline[name] is not None for name in ("commit", "executableSha256", "artifact")):
            raise ContractError("pending baseline identity fields must remain null")
    else:
        raise ContractError("baseline.status is unsupported")
    receipt = exact_keys(root["receipt"], {"path", "length", "sha256"}, "receipt")
    for obj, where in ((shipping, "shippingDefaults"), (receipt, "receipt")):
        if type(obj["path"]) is not str or not obj["path"]:
            raise ContractError(f"{where}.path must be a nonempty string")
        require_hash(obj["sha256"], f"{where}.sha256")
    if not is_int(receipt["length"]) or receipt["length"] < 0:
        raise ContractError("receipt.length must be a nonnegative integer")
    if not _is_absolute_path(receipt["path"]) and ".." in receipt["path"].replace("\\", "/").split("/"):
        raise ContractError("a repo-relative receipt.path must not leave the repository")
    if type(root["profiles"]) is not list or not root["profiles"]:
        raise ContractError("profiles must be a nonempty array")
    profile_ids: set[str] = set()
    for index, profile_value in enumerate(root["profiles"]):
        profile = exact_keys(profile_value, {"id", "disableLookAssist", "playbackProcessing", "qualityMode", "expectedQualityMode", "scaleFactor", "expectedScaleRequest", "selectionAuthority"}, f"profiles[{index}]")
        if type(profile["id"]) is not str or not profile["id"] or profile["id"] in profile_ids:
            raise ContractError(f"profiles[{index}].id must be unique")
        profile_ids.add(profile["id"])
        if type(profile["disableLookAssist"]) is not bool:
            raise ContractError(f"profiles[{index}].disableLookAssist must be boolean")
        # The value is the runner's -PlaybackProcessing and the app's playback_processing_selected label, which are
        # "subset" (the smoke lane's fast processing, no receipt) or "receipt". "none" is neither, so it can bind to
        # neither the runner nor the evidence.
        if profile["playbackProcessing"] not in ("receipt", "subset"):
            raise ContractError(f"profiles[{index}].playbackProcessing is unsupported")
        if profile["qualityMode"] != "1" or profile["expectedQualityMode"] != 1 or profile["scaleFactor"] != "" or profile["expectedScaleRequest"] != 4 or profile["selectionAuthority"] != "shipping-default-controlled":
            raise ContractError(f"profiles[{index}] must use the pinned High Quality shipping selection and derived scale")
    if type(root["clips"]) is not list or not root["clips"]:
        raise ContractError("clips must be a nonempty array")
    clip_ids: set[str] = set()
    for index, clip_value in enumerate(root["clips"]):
        allowed = {"id", "required", "path", "length", "sha256", "parts", "startFrame", "aspect", "evidenceStatus"}
        if type(clip_value) is not dict or not set(clip_value).issubset(allowed) or not {"id", "required", "path", "length", "sha256", "parts", "startFrame", "aspect"}.issubset(clip_value):
            raise ContractError(f"clips[{index}] has missing or unknown keys")
        clip = clip_value
        if type(clip["id"]) is not str or not clip["id"] or clip["id"] in clip_ids:
            raise ContractError(f"clips[{index}].id must be unique")
        clip_ids.add(clip["id"])
        if type(clip["required"]) is not bool or type(clip["path"]) is not str:
            raise ContractError(f"clips[{index}] required/path type mismatch")
        unavailable = clip.get("evidenceStatus") == "required_corpus_not_acquired"
        if unavailable:
            if not clip["required"] or clip["length"] is not None or clip["sha256"] is not None or clip["parts"] != []:
                raise ContractError(f"clips[{index}] unavailable evidence must be required with null length/hash")
        else:
            if not is_int(clip["length"]) or clip["length"] <= 0:
                raise ContractError(f"clips[{index}].length must be a positive integer")
            require_hash(clip["sha256"], f"clips[{index}].sha256")
            if type(clip["parts"]) is not list or not clip["parts"]:
                raise ContractError(f"clips[{index}].parts must bind the complete ordered multipart clip")
            for part_index, part_value in enumerate(clip["parts"]):
                part = exact_keys(part_value, {"path", "length", "sha256"}, f"clips[{index}].parts[{part_index}]")
                if type(part["path"]) is not str or not part["path"] or not is_int(part["length"]) or part["length"] <= 0:
                    raise ContractError(f"clips[{index}].parts[{part_index}] path/length is invalid")
                require_hash(part["sha256"], f"clips[{index}].parts[{part_index}].sha256")
            first = clip["parts"][0]
            if first["path"] != clip["path"] or first["length"] != clip["length"] or first["sha256"].upper() != clip["sha256"].upper():
                raise ContractError(f"clips[{index}] primary path/length/hash must equal parts[0]")
        if not is_int(clip["startFrame"]) or clip["startFrame"] < 0:
            raise ContractError(f"clips[{index}].startFrame must be a nonnegative integer")
        aspect = exact_keys(clip["aspect"], {"mode", "stretchX", "stretchY", "hStretchIndex", "vStretchIndex"}, f"clips[{index}].aspect")
        if aspect["mode"] != "presented-playback-stretch" or not all(is_number(aspect[k]) for k in ("stretchX", "stretchY")) or not all(is_int(aspect[k]) for k in ("hStretchIndex", "vStretchIndex")):
            raise ContractError(f"clips[{index}].aspect is invalid")
    budgets = exact_keys(root["budgets"], {"values", "pixels", "cadence"}, "budgets")
    value_keys = {"maxAbsLumaP05Delta", "maxAbsLumaP50Delta", "maxAbsLumaP95Delta", "maxAbsLumaMeanDelta", "maxAbsVisibleGreenAxisDelta", "maxAbsGreenArtifactRatioDelta"}
    pixel_keys = {"maxMeanAbsRgb", "maxP95AbsRgb", "maxAbsRgb", "maxChangedPixelRatioGt0", "maxChangedPixelRatioGt2", "maxChangedPixelRatioGt8", "maxRmseRgb"}
    for section, keys in (("values", value_keys), ("pixels", pixel_keys)):
        item = exact_keys(budgets[section], keys, f"budgets.{section}")
        if not all(is_number(v) and float(v) >= 0 for v in item.values()):
            raise ContractError(f"budgets.{section} values must be finite and nonnegative")
    cadence = exact_keys(budgets["cadence"], {"repeats", "seconds", "maxP99DeltaMs", "maxHitchFractionDelta", "authority"}, "budgets.cadence")
    if not is_int(cadence["repeats"]) or cadence["repeats"] < 1 or not is_int(cadence["seconds"]) or cadence["seconds"] < 1 or cadence["authority"] != "advisory_only":
        raise ContractError("cadence repetitions/seconds/authority are invalid")
    if not is_number(cadence["maxP99DeltaMs"]) or not is_number(cadence["maxHitchFractionDelta"]):
        raise ContractError("cadence thresholds must be finite numbers")
    if type(root["legacyPolicyAllowlist"]) is not list or any(type(v) is not str or not HEX40.fullmatch(v) for v in root["legacyPolicyAllowlist"]):
        raise ContractError("legacyPolicyAllowlist must contain only full lowercase commits")
    return root


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def validate_file(path: Path, length: int, digest: str, where: str) -> None:
    if not path.is_file():
        raise FileBindingError("MISSING", f"{where} missing: {path}")
    actual_length = path.stat().st_size
    if actual_length != length:
        raise FileBindingError("LENGTH_MISMATCH", f"{where} length mismatch: expected {length}, got {actual_length}")
    actual_hash = sha256_file(path)
    if actual_hash != digest.upper():
        raise FileBindingError("HASH_MISMATCH", f"{where} hash mismatch: expected {digest.upper()}, got {actual_hash}")


def validate_shipping_alignment(spec: dict[str, Any], shipping_path: Path) -> None:
    shipping = load_json(shipping_path)
    try:
        quality = shipping["playback"]["qualityMode"]["value"]
        scale_override = shipping["playback"]["scaleFactorOverride"]["value"]
        non_dual = shipping["playback"]["derived"]["initialScaleRequest"]["nonDualIso"]
        dual = shipping["playback"]["derived"]["initialScaleRequest"]["dualIso"]
    except (KeyError, TypeError) as exc:
        raise ContractError("shipping-defaults playback projection is incomplete") from exc
    if not is_int(quality) or not is_int(scale_override) or not is_int(non_dual) or not is_int(dual):
        raise ContractError("shipping-defaults playback projection has invalid primitive types")
    for profile in spec["profiles"]:
        if profile["expectedQualityMode"] != quality or profile["qualityMode"] != str(quality):
            raise ContractError(f"profile {profile['id']} quality does not match shipping defaults")
        if scale_override != 0 or profile["scaleFactor"] != "" or profile["expectedScaleRequest"] != non_dual or profile["expectedScaleRequest"] != dual:
            raise ContractError(f"profile {profile['id']} scale does not match the shipping default projection")


def validate_exe(path: Path, commit: str, digest: str, repo_root: Path) -> dict[str, Any]:
    require_hash(commit, "commit", 40)
    require_hash(digest, "exe sha256")
    if not path.is_file():
        raise ContractError(f"executable missing: {path}")
    actual_hash = sha256_file(path)
    if actual_hash != digest.upper():
        raise ContractError(f"executable hash mismatch for {path}")
    matches = STAMP.findall(path.read_bytes())
    if len(matches) != 1:
        raise ContractError(f"executable must contain exactly one build stamp: {path}")
    embedded_commit = matches[0][0].decode("ascii")
    dirty = int(matches[0][1])
    if embedded_commit != commit or dirty != 0:
        raise ContractError(f"executable stamp mismatch/dirty: {path}")
    check = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_root, capture_output=True)
    if check.returncode != 0:
        raise ContractError(f"commit does not resolve to a commit object: {commit}")
    return {"path": str(path.resolve()), "sha256": actual_hash, "commit": commit, "embeddedCommit": embedded_commit, "dirty": dirty}


def check_baseline_artifact(pinned: dict[str, Any], spec_path: Path) -> tuple[str | None, str]:
    """(code, message) for a pinned baseline whose ``artifact`` is not a file matching ``executableSha256``; (None, "") if verified."""
    raw = pinned["artifact"]
    path = Path(raw) if _is_absolute_path(raw) else spec_path.resolve().parent / raw
    try:
        found = path.is_file()
    except OSError:
        found = False
    if not found:
        return "BASELINE_ARTIFACT_UNVERIFIED", f"baseline artifact does not resolve to a file: {raw}"
    try:
        actual = sha256_file(path)
    except OSError as exc:
        return "BASELINE_ARTIFACT_UNVERIFIED", f"baseline artifact unreadable: {path}: {exc}"
    if actual != pinned["executableSha256"].upper():
        return "BASELINE_ARTIFACT_SHA_MISMATCH", f"baseline artifact hash mismatch: expected {pinned['executableSha256'].upper()}, got {actual}"
    return None, ""


def preflight(spec_path: Path, repo_root: Path, baseline: tuple[Path, str, str] | None, candidate: tuple[Path, str, str] | None) -> tuple[dict[str, Any], int]:
    spec = validate_spec(load_json(spec_path))
    problems: list[str] = []
    typed: list[dict[str, Any]] = []

    def block(code: str, message: str, **extra: Any) -> None:
        # ``failures`` keeps the human strings; ``typedFailures`` is the same list with a stable code per entry, so a
        # later run can tell "baseline not pinned" from "clip not acquired" from "receipt unreachable".
        problems.append(message)
        typed.append({"code": code, "detail": message, **extra})

    corpus: list[dict[str, Any]] = []
    shipping_path = (repo_root / spec["shippingDefaults"]["path"]).resolve()
    try:
        validate_file(shipping_path, shipping_path.stat().st_size, spec["shippingDefaults"]["sha256"], "shipping defaults")
    except (OSError, FileNotFoundError) as exc:
        raise ContractError(str(exc)) from exc
    validate_shipping_alignment(spec, shipping_path)
    if spec["baseline"]["status"] != "reviewed_instrumented_known_good":
        block("BASELINE_NOT_PINNED", "reviewed instrumented known-good baseline bridge is not pinned")
    else:
        # "pinned" is not "verified": the artifact must resolve to a file whose bytes are the pinned executableSha256,
        # with or without baseline executables supplied.
        artifact_code, artifact_message = check_baseline_artifact(spec["baseline"], spec_path)
        if artifact_code:
            block(artifact_code, artifact_message)
    unavailable_ids = [clip["id"] for clip in spec["clips"] if clip.get("evidenceStatus") == "required_corpus_not_acquired" and clip["required"]]
    for clip_id in unavailable_ids:
        block("REQUIRED_CLIP_NOT_ACQUIRED", f"required clip has no pinned length/hash: {clip_id}", clipId=clip_id)
    receipt = resolve_receipt_path(spec, repo_root)
    try:
        validate_file(receipt, spec["receipt"]["length"], spec["receipt"]["sha256"], "receipt")
    except FileBindingError as exc:
        block(f"RECEIPT_{exc.kind}", str(exc))
    if problems:
        corpus = [
            {"id": clip["id"], "required": clip["required"], "status": "missing_contract_evidence" if clip["id"] in unavailable_ids else "not_checked_due_to_contract_block"}
            for clip in spec["clips"]
        ]
        report = {
            "schema": REPORT_SCHEMA, "phase": "preflight", "specPath": str(spec_path.resolve()),
            "specSha256": sha256_file(spec_path), "shippingDefaultsSha256": sha256_file(shipping_path),
            "corpus": corpus, "executables": {}, "blockingVerdict": "INDETERMINATE",
            "cadenceVerdict": "INDETERMINATE", "authorizing": False, "failures": problems, "typedFailures": typed,
        }
        return report, 4
    for clip in spec["clips"]:
        status = "available"
        try:
            for part in clip["parts"]:
                validate_file(Path(part["path"]), part["length"], part["sha256"], f"clip {clip['id']} part {part['path']}")
        except FileBindingError as exc:
            status = "missing_or_mismatch"
            if clip["required"]:
                block(f"CLIP_PART_{exc.kind}", str(exc), clipId=clip["id"])
        corpus.append({"id": clip["id"], "required": clip["required"], "status": status})
    executables: dict[str, Any] = {}
    if baseline is not None and candidate is not None:
        executables["baseline"] = validate_exe(*baseline, repo_root)
        executables["candidate"] = validate_exe(*candidate, repo_root)
        if executables["baseline"]["commit"] == executables["candidate"]["commit"] or executables["baseline"]["sha256"] == executables["candidate"]["sha256"]:
            raise ContractError("authorizing A/B requires different baseline and candidate builds")
        pinned = spec["baseline"]
        if pinned["status"] != "reviewed_instrumented_known_good" or executables["baseline"]["commit"] != pinned["commit"] or executables["baseline"]["sha256"] != pinned["executableSha256"].upper():
            raise ContractError("baseline does not match the reviewed instrumented known-good binding")
    report = {
        "schema": REPORT_SCHEMA,
        "phase": "preflight",
        "specPath": str(spec_path.resolve()),
        "specSha256": sha256_file(spec_path),
        "shippingDefaultsSha256": sha256_file(shipping_path),
        "corpus": corpus,
        "executables": executables,
        "blockingVerdict": "INDETERMINATE" if problems else "READY",
        "cadenceVerdict": "INDETERMINATE",
        "authorizing": False,
        "failures": problems,
        "typedFailures": typed,
    }
    return report, 4 if problems else 0


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def read_png_rgb(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ContractError(f"not a PNG: {path}")
    offset, width, height, bit_depth, color_type = 8, 0, 0, 0, 0
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if bit_depth != 8 or color_type not in (2, 6) or compression or filtering or interlace:
                raise ContractError("only noninterlaced 8-bit RGB/RGBA PNG is supported")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    if len(raw) != height * (stride + 1):
        raise ContractError(f"PNG payload length mismatch: {path}")
    rows: list[bytearray] = []
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        source = raw[cursor + 1:cursor + 1 + stride]
        cursor += stride + 1
        prior = rows[-1] if rows else bytearray(stride)
        row = bytearray(stride)
        for index, value in enumerate(source):
            left = row[index - channels] if index >= channels else 0
            up = prior[index]
            upper_left = prior[index - channels] if index >= channels else 0
            if filter_type == 0:
                filtered = value
            elif filter_type == 1:
                filtered = value + left
            elif filter_type == 2:
                filtered = value + up
            elif filter_type == 3:
                filtered = value + ((left + up) // 2)
            elif filter_type == 4:
                filtered = value + _paeth(left, up, upper_left)
            else:
                raise ContractError(f"unknown PNG filter {filter_type}")
            row[index] = filtered & 0xFF
        rows.append(row)
    rgb = bytearray(width * height * 3)
    target = 0
    for row in rows:
        for source in range(0, len(row), channels):
            rgb[target:target + 3] = row[source:source + 3]
            target += 3
    return width, height, bytes(rgb)


def percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(math.floor(fraction * (len(ordered) - 1))))])


def image_metrics(path: Path) -> tuple[dict[str, Any], bytes]:
    width, height, rgb = read_png_rgb(path)
    pixels = len(rgb) // 3
    channels = [rgb[index::3] for index in range(3)]
    luma = [(54 * rgb[i] + 183 * rgb[i + 1] + 19 * rgb[i + 2]) >> 8 for i in range(0, len(rgb), 3)]
    visible_indices = [i for i, value in enumerate(luma) if value >= 12]
    visible_means = [sum(channel[i] for i in visible_indices) / len(visible_indices) if visible_indices else 0.0 for channel in channels]
    green_axes = [channels[1][i] - ((channels[0][i] + channels[2][i]) * 0.5) for i in visible_indices]
    artifact_count = sum(1 for position, axis in zip(visible_indices, green_axes) if channels[1][position] >= 30 and axis >= 25.0)
    return {
        "path": str(path.resolve()), "sha256": sha256_file(path), "width": width, "height": height,
        "meanRgb": [sum(channel) / pixels for channel in channels],
        "lumaP05": percentile(luma, 0.05), "lumaP50": percentile(luma, 0.50), "lumaP95": percentile(luma, 0.95), "lumaMean": sum(luma) / pixels,
        "visibleGreenAxis": visible_means[1] - ((visible_means[0] + visible_means[2]) * 0.5),
        "greenArtifactRatio": artifact_count / len(visible_indices) if visible_indices else 0.0,
    }, rgb


def compare_images(baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    baseline, base_rgb = image_metrics(baseline_path)
    candidate, cand_rgb = image_metrics(candidate_path)
    same = (baseline["width"], baseline["height"]) == (candidate["width"], candidate["height"])
    if not same:
        return {"sameDimensions": False, "baseline": baseline, "candidate": candidate, "values": {}, "pixels": {}}
    absolute = [abs(a - b) for a, b in zip(base_rgb, cand_rgb)]
    per_pixel = [max(absolute[i:i + 3]) for i in range(0, len(absolute), 3)]
    return {
        "sameDimensions": True,
        "baseline": baseline,
        "candidate": candidate,
        "values": {
            "absLumaP05Delta": abs(candidate["lumaP05"] - baseline["lumaP05"]),
            "absLumaP50Delta": abs(candidate["lumaP50"] - baseline["lumaP50"]),
            "absLumaP95Delta": abs(candidate["lumaP95"] - baseline["lumaP95"]),
            "absLumaMeanDelta": abs(candidate["lumaMean"] - baseline["lumaMean"]),
            "absVisibleGreenAxisDelta": abs(candidate["visibleGreenAxis"] - baseline["visibleGreenAxis"]),
            "absGreenArtifactRatioDelta": abs(candidate["greenArtifactRatio"] - baseline["greenArtifactRatio"]),
        },
        "pixels": {
            "meanAbsRgb": sum(absolute) / len(absolute), "p95AbsRgb": percentile(absolute, 0.95), "maxAbsRgb": max(absolute),
            "changedPixelRatioGt0": sum(v > 0 for v in per_pixel) / len(per_pixel),
            "changedPixelRatioGt2": sum(v > 2 for v in per_pixel) / len(per_pixel),
            "changedPixelRatioGt8": sum(v > 8 for v in per_pixel) / len(per_pixel),
            "rmseRgb": math.sqrt(sum(v * v for v in absolute) / len(absolute)),
        },
    }


def _smoke_binding(result_path: Path, expected_exe: dict[str, Any], expected_clip: dict[str, Any], expected_profile: dict[str, Any], spec: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
    result = load_json(result_path)
    if type(result) is not dict or result.get("schema") != "mlvapp-gui-smoke-result.v1":
        raise ContractError(f"invalid smoke result schema: {result_path}")
    if result.get("validation", {}).get("ok") is not True:
        raise ContractError(f"smoke validation failed: {result_path}")
    if str(Path(result.get("exePath", "")).resolve()).casefold() != str(Path(expected_exe["path"]).resolve()).casefold():
        raise ContractError(f"smoke executable path mismatch: {result_path}")
    if str(Path(result.get("clipPath", "")).resolve()).casefold() != str(Path(expected_clip["path"]).resolve()).casefold():
        raise ContractError(f"smoke clip path mismatch: {result_path}")
    bindings = result.get("inputBindings")
    if type(bindings) is not dict:
        raise ContractError(f"smoke immutable input bindings are missing: {result_path}")
    exe_binding = bindings.get("executable")
    if type(exe_binding) is not dict or exe_binding.get("stampFound") is not True or exe_binding.get("dirty") != 0 or exe_binding.get("embeddedCommit") != expected_exe["commit"] or exe_binding.get("length") != Path(expected_exe["path"]).stat().st_size or str(exe_binding.get("sha256", "")).upper() != expected_exe["sha256"].upper() or str(Path(exe_binding.get("path", "")).resolve()).casefold() != str(Path(expected_exe["path"]).resolve()).casefold():
        raise ContractError(f"smoke launch-time executable binding mismatch: {result_path}")
    observed_parts = bindings.get("clipParts")
    if type(observed_parts) is not list or len(observed_parts) != len(expected_clip["parts"]):
        raise ContractError(f"smoke multipart clip binding count mismatch: {result_path}")
    for observed, expected in zip(observed_parts, expected_clip["parts"]):
        if type(observed) is not dict or str(Path(observed.get("path", "")).resolve()).casefold() != str(Path(expected["path"]).resolve()).casefold() or observed.get("length") != expected["length"] or str(observed.get("sha256", "")).upper() != expected["sha256"].upper():
            raise ContractError(f"smoke multipart clip binding mismatch: {result_path}")
    receipt_binding = bindings.get("receipt")
    if expected_profile["playbackProcessing"] == "receipt":
        expected_receipt = spec["receipt"]
        if type(receipt_binding) is not dict or str(Path(receipt_binding.get("path", "")).resolve()).casefold() != str(receipt_path.resolve()).casefold() or receipt_binding.get("length") != expected_receipt["length"] or str(receipt_binding.get("sha256", "")).upper() != expected_receipt["sha256"].upper():
            raise ContractError(f"smoke receipt binding mismatch: {result_path}")
    elif receipt_binding is not None:
        raise ContractError(f"WB-locked profile must not carry a receipt binding: {result_path}")
    provenance = result.get("validation", {}).get("screenshotProvenance")
    if type(provenance) is not dict or provenance.get("schema") != "mlvapp-gui-smoke-screenshot-provenance.v2" or provenance.get("validAssociation") is not True or provenance.get("validFresh") is not True:
        raise ContractError(f"smoke screenshot provenance is not fresh: {result_path}")
    if provenance.get("pathSource") != "render_thread" or provenance.get("processed8CacheHit") != 0:
        raise ContractError(f"smoke screenshot is not a fresh render-thread present: {result_path}")
    frame = provenance.get("displayFrame")
    if not is_int(frame):
        raise ContractError(f"smoke display frame missing: {result_path}")
    if provenance.get("requestedStartFrame") != expected_clip["startFrame"] or provenance.get("effectiveStartFrame") != expected_clip["startFrame"]:
        raise ContractError(f"smoke start frame does not match the pinned clip start: {result_path}")
    screenshot = result.get("screenshot", {}).get("path")
    if type(screenshot) is not str or not Path(screenshot).is_file():
        raise ContractError(f"smoke screenshot missing: {result_path}")
    if str(Path(provenance.get("screenshotPath", "")).resolve()).casefold() != str(Path(screenshot).resolve()).casefold():
        raise ContractError(f"screenshot provenance path does not match consumed screenshot: {result_path}")
    recorded_image = result.get("screenshot", {}).get("capture", {}).get("image", {})
    recorded_hash = recorded_image.get("sha256") if type(recorded_image) is dict else None
    if type(recorded_hash) is not str or not HEX64.fullmatch(recorded_hash) or sha256_file(Path(screenshot)) != recorded_hash.upper():
        raise ContractError(f"smoke screenshot hash is missing or mismatched: {result_path}")
    policy = result.get("visualQuality", {}).get("playbackPolicy")
    commit = expected_exe["commit"]
    if type(policy) is not dict or not policy:
        if commit not in spec["legacyPolicyAllowlist"]:
            raise ContractError(f"effective playback policy missing for unallowlisted build: {result_path}")
        raise ContractError(f"legacy policy derivation is not complete for allowlisted build: {result_path}")
    selected_processing = policy.get("playback_processing_selected")
    if selected_processing != expected_profile["playbackProcessing"]:
        raise ContractError(f"effective playback processing does not match profile {expected_profile['id']}: {result_path}")
    visual_state = result.get("visualQuality", {}).get("visualState")
    if type(visual_state) is not dict or visual_state.get("look_assist_enabled") != (0 if expected_profile["disableLookAssist"] else 1):
        raise ContractError(f"effective Look Assist state does not match profile {expected_profile['id']}: {result_path}")
    if visual_state.get("quality_mode") != expected_profile["expectedQualityMode"] or visual_state.get("scale_request") != expected_profile["expectedScaleRequest"]:
        raise ContractError(f"effective quality/scale does not match profile {expected_profile['id']}: {result_path}")
    aspect = result.get("visualQuality", {}).get("aspectEvidence")
    expected_aspect = expected_clip["aspect"]
    if type(aspect) is not dict:
        raise ContractError(f"aspect evidence missing: {result_path}")
    comparisons = (("mode", str), ("hStretchIndex", int), ("vStretchIndex", int))
    for name, expected_type in comparisons:
        actual = aspect.get(name)
        if (expected_type is int and not is_int(actual)) or (expected_type is str and type(actual) is not str) or actual != expected_aspect[name]:
            raise ContractError(f"aspect evidence {name} mismatch: {result_path}")
    for name in ("stretchX", "stretchY"):
        if not is_number(aspect.get(name)) or abs(float(aspect[name]) - float(expected_aspect[name])) > 0.0001:
            raise ContractError(f"aspect evidence {name} mismatch: {result_path}")
    required_pair_fields = ("requestedStartFrame", "effectiveStartFrame", "requestedFrame", "displayFrame", "presentationIndex", "requestSerial", "requestSerialOffset", "screenshotMethod", "screenshotWidth", "screenshotHeight")
    if any(provenance.get(name) is None for name in required_pair_fields) or type(provenance.get("presentedHistory")) is not list or not provenance["presentedHistory"] or type(provenance.get("effectiveState")) is not dict or not provenance["effectiveState"]:
        raise ContractError(f"smoke V2 transaction history/effective state is incomplete: {result_path}")
    for name in ("requestedStartFrame", "effectiveStartFrame", "requestedFrame", "displayFrame", "presentationIndex", "requestSerial", "requestSerialOffset", "screenshotWidth", "screenshotHeight"):
        if not is_int(provenance[name]):
            raise ContractError(f"smoke V2 field {name} must be an integer: {result_path}")
    if type(provenance["screenshotMethod"]) is not str or not provenance["screenshotMethod"]:
        raise ContractError(f"smoke V2 screenshotMethod must be a nonempty string: {result_path}")
    for history_index, history_value in enumerate(provenance["presentedHistory"]):
        history = exact_keys(history_value, {"index", "displayFrame", "serial", "serialOffset", "requestedFrame", "generation"}, f"presentedHistory[{history_index}]")
        if not all(is_int(value) for value in history.values()):
            raise ContractError(f"smoke V2 presented history values must be integers: {result_path}")
    exact_keys(provenance["effectiveState"], {"visualState", "playbackPolicy", "frame", "renderManifest"}, "effectiveState")
    return {"resultPath": str(result_path.resolve()), "frame": frame, "screenshot": screenshot, "policy": policy, "provenance": provenance}


def evaluate(spec: dict[str, Any], evidence: Any, spec_path: Path) -> tuple[dict[str, Any], int]:
    evidence_root = exact_keys(evidence, {"schema", "specSha256", "shippingDefaultsSha256", "baseline", "candidate", "pairs", "cadence"}, "evidence")
    if evidence_root["schema"] != "mlvapp.output-budget-evidence.v1":
        raise ContractError("unsupported evidence schema")
    if evidence_root["specSha256"].upper() != sha256_file(spec_path):
        raise ContractError("evidence spec hash does not match the evaluated spec")
    shipping_path = (spec_path.parents[2] / spec["shippingDefaults"]["path"]).resolve()
    if evidence_root["shippingDefaultsSha256"].upper() != sha256_file(shipping_path):
        raise ContractError("evidence shipping-defaults hash does not match")
    validate_shipping_alignment(spec, shipping_path)
    for side in ("baseline", "candidate"):
        binding = exact_keys(evidence_root[side], {"path", "commit", "sha256"}, f"evidence.{side}")
        require_hash(binding["commit"], f"evidence.{side}.commit", 40)
        require_hash(binding["sha256"], f"evidence.{side}.sha256")
    if evidence_root["baseline"]["commit"] == evidence_root["candidate"]["commit"] or evidence_root["baseline"]["sha256"].upper() == evidence_root["candidate"]["sha256"].upper():
        raise ContractError("evidence baseline and candidate must be different builds")
    pinned = spec["baseline"]
    if pinned["status"] != "reviewed_instrumented_known_good" or evidence_root["baseline"]["commit"] != pinned["commit"] or evidence_root["baseline"]["sha256"].upper() != pinned["executableSha256"].upper():
        raise ContractError("evidence baseline does not match the reviewed instrumented known-good binding")
    repo_root = spec_path.parents[2]
    validate_exe(Path(evidence_root["baseline"]["path"]), evidence_root["baseline"]["commit"], evidence_root["baseline"]["sha256"], repo_root)
    validate_exe(Path(evidence_root["candidate"]["path"]), evidence_root["candidate"]["commit"], evidence_root["candidate"]["sha256"], repo_root)
    receipt_path = resolve_receipt_path(spec, repo_root)
    validate_file(receipt_path, spec["receipt"]["length"], spec["receipt"]["sha256"], "receipt")
    for clip in spec["clips"]:
        if clip.get("evidenceStatus") == "required_corpus_not_acquired":
            raise ContractError(f"required clip has no pinned length/hash: {clip['id']}")
        for part in clip["parts"]:
            validate_file(Path(part["path"]), part["length"], part["sha256"], f"clip {clip['id']} part {part['path']}")
    if type(evidence_root["pairs"]) is not list:
        raise ContractError("evidence.pairs must be an array")
    expected = {(clip["id"], profile["id"]) for clip in spec["clips"] if clip["required"] for profile in spec["profiles"]}
    observed: set[tuple[str, str]] = set()
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    vmap = {
        "absLumaP05Delta": "maxAbsLumaP05Delta", "absLumaP50Delta": "maxAbsLumaP50Delta", "absLumaP95Delta": "maxAbsLumaP95Delta",
        "absLumaMeanDelta": "maxAbsLumaMeanDelta", "absVisibleGreenAxisDelta": "maxAbsVisibleGreenAxisDelta", "absGreenArtifactRatioDelta": "maxAbsGreenArtifactRatioDelta",
    }
    pmap = {
        "meanAbsRgb": "maxMeanAbsRgb", "p95AbsRgb": "maxP95AbsRgb", "maxAbsRgb": "maxAbsRgb", "changedPixelRatioGt0": "maxChangedPixelRatioGt0",
        "changedPixelRatioGt2": "maxChangedPixelRatioGt2", "changedPixelRatioGt8": "maxChangedPixelRatioGt8", "rmseRgb": "maxRmseRgb",
    }
    for index, pair_value in enumerate(evidence_root["pairs"]):
        pair = exact_keys(pair_value, {"clipId", "profileId", "baselineResult", "candidateResult"}, f"pairs[{index}]")
        key = (pair["clipId"], pair["profileId"])
        if key not in expected or key in observed:
            raise ContractError(f"unexpected or duplicate evidence pair {key}")
        observed.add(key)
        clip = next(item for item in spec["clips"] if item["id"] == pair["clipId"])
        profile = next(item for item in spec["profiles"] if item["id"] == pair["profileId"])
        baseline_binding = _smoke_binding(Path(pair["baselineResult"]), evidence_root["baseline"], clip, profile, spec, receipt_path)
        candidate_binding = _smoke_binding(Path(pair["candidateResult"]), evidence_root["candidate"], clip, profile, spec, receipt_path)
        left_provenance = baseline_binding["provenance"]
        right_provenance = candidate_binding["provenance"]
        pair_fields = ("requestedStartFrame", "effectiveStartFrame", "requestedFrame", "displayFrame", "presentationIndex", "requestSerial", "requestSerialOffset", "screenshotMethod", "screenshotWidth", "screenshotHeight")
        for name in pair_fields:
            if left_provenance[name] != right_provenance[name]:
                failures.append(f"{key}: provenance {name} mismatch")
        if left_provenance["presentedHistory"] != right_provenance["presentedHistory"]:
            failures.append(f"{key}: presented history mismatch")
        if left_provenance["effectiveState"] != right_provenance["effectiveState"]:
            failures.append(f"{key}: effective state mismatch")
        comparison = compare_images(Path(baseline_binding["screenshot"]), Path(candidate_binding["screenshot"]))
        for binding, side in ((baseline_binding, "baseline"), (candidate_binding, "candidate")):
            provenance = binding["provenance"]
            image = comparison[side]
            if provenance["screenshotWidth"] != image["width"] or provenance["screenshotHeight"] != image["height"]:
                failures.append(f"{key}: {side} screenshot dimensions disagree with V2 provenance")
        pair_failures: list[str] = []
        if not comparison["sameDimensions"]:
            pair_failures.append("screenshot dimensions differ")
        for metric, budget_name in vmap.items():
            if comparison["sameDimensions"] and comparison["values"][metric] > float(spec["budgets"]["values"][budget_name]):
                pair_failures.append(f"{metric} exceeds {budget_name}")
        for metric, budget_name in pmap.items():
            if comparison["sameDimensions"] and comparison["pixels"][metric] > float(spec["budgets"]["pixels"][budget_name]):
                pair_failures.append(f"{metric} exceeds {budget_name}")
        failures.extend(f"{key}: {item}" for item in pair_failures)
        results.append({"clipId": key[0], "profileId": key[1], "baselineBinding": baseline_binding, "candidateBinding": candidate_binding, "comparison": comparison, "verdict": "PASS" if not pair_failures else "FAIL", "failures": pair_failures})
    missing = sorted(expected - observed)
    failures.extend(f"missing required evidence pair {key}" for key in missing)
    cadence_verdict = "INDETERMINATE"
    if evidence_root["cadence"] is not None:
        raise ContractError("cadence evidence is not yet raw-run bound; it must remain null/INDETERMINATE")
    blocking = "FAIL" if failures else "PASS"
    report = {"schema": REPORT_SCHEMA, "phase": "evaluation", "pairs": results, "blockingVerdict": blocking, "cadenceVerdict": cadence_verdict, "authorizing": blocking == "PASS", "failures": failures}
    return report, 3 if failures else 0


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--spec", type=Path, required=True)
    pre.add_argument("--repo-root", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    for prefix in ("baseline", "candidate"):
        pre.add_argument(f"--{prefix}-exe", type=Path)
        pre.add_argument(f"--{prefix}-commit")
        pre.add_argument(f"--{prefix}-sha256")
    ev = sub.add_parser("evaluate")
    ev.add_argument("--spec", type=Path, required=True)
    ev.add_argument("--evidence", type=Path, required=True)
    ev.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            baseline = (args.baseline_exe, args.baseline_commit, args.baseline_sha256) if args.baseline_exe else None
            candidate = (args.candidate_exe, args.candidate_commit, args.candidate_sha256) if args.candidate_exe else None
            if (baseline is None) != (candidate is None):
                raise ContractError("baseline and candidate executable bindings must be supplied together")
            report, code = preflight(args.spec, args.repo_root.resolve(), baseline, candidate)
        else:
            spec = validate_spec(load_json(args.spec))
            report, code = evaluate(spec, load_json(args.evidence), args.spec.resolve())
        write_report(args.output, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return code
    except (ContractError, OSError, UnicodeError, struct.error, zlib.error, TypeError, OverflowError, ZeroDivisionError) as exc:
        report = {"schema": REPORT_SCHEMA, "blockingVerdict": "INDETERMINATE", "cadenceVerdict": "INDETERMINATE", "authorizing": False, "failures": [str(exc)]}
        write_report(args.output, report)
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
