"""Bind captured frames to their capture records by IMAGE BYTES.

A frame copied off the bench without its ``reference-frame-candidate.json`` is a pixel
array with no identity: which build produced it, which clip, under which settings, all
live outside the file. On 2026-09-03 forty-five frames accumulated on the board that way,
and their identity survived only in a chat transcript -- which is exactly what an
unplanned handoff destroys.

The key is the SHA256 of the PNG bytes, deliberately NOT the filename. A filename is
chosen by whoever copied the file and can be wrong; the bytes cannot. Matching on bytes
means a mislabelled copy fails to match rather than silently inheriting the provenance of
whatever its name suggests.

Emits ``mlv-app/frame-provenance.v1``.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

SCHEMA = "mlv-app/frame-provenance.v1"
CANDIDATE_NAME = "reference-frame-candidate.json"
# VENUE-REFERENCE-FRAME-CANDIDATE-INVALID-MARKER-1: the runner job writes this beside a manifest it voided (exit 225) because the in-place
# "invalid" stamp is best effort. Present = not a candidate, whatever the manifest says (it may still parse and still say candidate-unreviewed).
INVALID_MARKER_NAME = "reference-frame-candidate.invalid"
FRAME_NAME = "reference-frame.png"
RESULT_NAME = "result.json"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_stamped_invalid(manifest_path: str) -> bool:
    """True when the manifest itself says the capture was voided (the runner job's in-place stamp: ``invalid`` true and/or status ``invalid``).

    REFERENCE-FRAME-CONSUMER-HONORS-INVALID-STAMP-1: the runner job writes the marker first, but when only the marker publish fails and the
    stamp lands it writes no marker and does not quarantine, so the stamp is the only record. A manifest that cannot be read or parsed is not
    claimed to be stamped (False): that case keeps its prior behaviour.
    """
    try:
        with open(manifest_path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and (doc.get("invalid") is True or doc.get("status") == "invalid")


def _parent_result_capture_failed(capture_dir: str) -> bool:
    """True when the ``result.json`` beside the capture directory says ``status: capture-failed``.

    R18-ADVISORY-TAIL-572-573-574-576-1 (Dot R18 advisory on #573): the runner job lays a capture out as
    ``<jobId>.artifacts/{result.json, capture/}`` and, after a capture-failed/225, voids the candidate with a marker, an in-place stamp and a
    quarantine rename. All three are best effort; if every one fails the manifest keeps the candidate name and unchanged bytes, and only the
    parent result.json still says the capture failed. Reading it needs no change to the result contract (``status`` is already written).

    Residual, deliberately not closed: a result.json that is absent (the job could not publish it, exit 223), unreadable or not an object is
    not claimed to be a failure (False), so that case keeps its prior behaviour and the three voiding layers stay the only guard there.
    """
    try:
        with open(os.path.join(os.path.dirname(capture_dir), RESULT_NAME), encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("status") == "capture-failed"


def index_capture_records(outbox_root: str, skipped: list[dict[str, str]] | None = None) -> dict[str, str]:
    """Map pixel-SHA256 -> candidate manifest path, for every capture under a root.

    A manifest whose sibling frame is missing is skipped: it describes a capture whose
    image is gone, so it can never be the source of a frame we hold. A manifest beside an
    ``INVALID_MARKER_NAME`` file is skipped for the same reason in the other direction: the
    capture was voided, so its pixels are not a candidate. So is a manifest that is itself
    stamped invalid, and a capture whose parent ``result.json`` says ``capture-failed``. When
    ``skipped`` is given, each voided capture is appended to it as ``{"capture": <dir>, "reason":
    "invalid-marker" | "manifest-stamped-invalid" | "result-capture-failed"}``.
    """
    index: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(outbox_root):
        if CANDIDATE_NAME not in filenames:
            continue
        if INVALID_MARKER_NAME in filenames:
            if skipped is not None:
                skipped.append({"capture": dirpath, "reason": "invalid-marker"})
            continue
        if _manifest_stamped_invalid(os.path.join(dirpath, CANDIDATE_NAME)):
            if skipped is not None:
                skipped.append({"capture": dirpath, "reason": "manifest-stamped-invalid"})
            continue
        if _parent_result_capture_failed(dirpath):
            if skipped is not None:
                skipped.append({"capture": dirpath, "reason": "result-capture-failed"})
            continue
        frame = os.path.join(dirpath, FRAME_NAME)
        if not os.path.isfile(frame):
            continue
        try:
            index[sha256_file(frame)] = os.path.join(dirpath, CANDIDATE_NAME)
        except OSError:
            continue
    return index


NESTED_OBJECT_FIELDS = ("executable", "clip")


class _MalformedFieldError(ValueError):
    """A readable manifest object whose nested identity field has the wrong JSON type; its message names the field(s)."""


def _malformed_nested_fields(manifest: dict[str, Any]) -> list[str]:
    """Names of the nested identity fields that are present but not JSON objects (absent / null stay a quiet None sha, as before)."""
    return [k for k in NESTED_OBJECT_FIELDS if manifest.get(k) is not None and not isinstance(manifest[k], dict)]


def _nested_sha(manifest: dict[str, Any], key: str) -> Any:
    nested = manifest.get(key)
    return nested.get("sha256") if isinstance(nested, dict) else None


def _identity(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "commit": manifest.get("commit"),
        "host": manifest.get("host"),
        "capturedUtc": manifest.get("capturedUtc"),
        "exeSha256": _nested_sha(manifest, "executable"),
        "clipSha256": _nested_sha(manifest, "clip"),
        "settings": manifest.get("settings"),
        "appSettings": manifest.get("appSettings"),
    }


def build(frames_dir: str, outbox_root: str) -> dict[str, Any]:
    """Bind every ``*.png`` in ``frames_dir`` to a capture record, or mark it orphaned."""
    skipped: list[dict[str, str]] = []
    index = index_capture_records(outbox_root, skipped)
    records: list[dict[str, Any]] = []
    matched = 0
    for name in sorted(os.listdir(frames_dir)):
        if not name.lower().endswith(".png"):
            continue
        path = os.path.join(frames_dir, name)
        digest = sha256_file(path)
        manifest_path = index.get(digest)
        row: dict[str, Any] = {"frame": name, "sha256": digest}
        if manifest_path is None:
            # Orphaned, not unknown: the hash still identifies the image exactly, so a
            # later run against a fuller outbox can bind it. Say that, rather than
            # implying the frame itself is unusable.
            row["provenance"] = "orphaned - no capture record with these pixels under the searched root"
        else:
            try:
                with open(manifest_path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
                if not isinstance(doc, dict):
                    raise ValueError("manifest is not a JSON object")
                if _malformed_nested_fields(doc):
                    # R18-ADVISORY-TAIL-572-573-574-576-1 (Dot R18 advisory on #576): a truthy scalar here used to raise out of _identity and abort the report.
                    raise _MalformedFieldError("malformed field: " + ", ".join(_malformed_nested_fields(doc)))
            except (OSError, ValueError) as exc:
                # The index accepted this manifest (an unparseable one is indexed on purpose) but it cannot be read now: malformed, not an
                # object, undecodable, or gone since indexing. One bad record must not abort the report for every other frame. The pixels
                # matched a record whose identity we could not read, so the frame is not counted as matched (main() then exits 4, as for an
                # orphan) and keeps the path so the operator can go and look.
                detail = str(exc) if isinstance(exc, _MalformedFieldError) else type(exc).__name__
                row["provenance"] = f"unreadable - a capture record matched these pixels but could not be read ({detail})"
                row["manifest"] = manifest_path
            else:
                matched += 1
                row.update(_identity(doc))
                row["manifest"] = manifest_path
        records.append(row)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "framesDir": frames_dir,
        "outboxRoot": outbox_root,
        "recordsIndexed": len(index),
        "matched": matched,
        "orphaned": len(records) - matched,
        "frames": records,
    }
    if skipped:
        # Only present when a voided capture was passed over, so a root with none renders byte-identically to before.
        result["skipped"] = skipped
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Bind captured frames to capture records by image bytes.")
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--outbox-root", required=True)
    ap.add_argument("--out", help="output JSON path (default: <frames-dir>/frame-provenance.json)")
    args = ap.parse_args(argv)

    result = build(args.frames_dir, args.outbox_root)
    out = args.out or os.path.join(args.frames_dir, "frame-provenance.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(f"{result['matched']} matched, {result['orphaned']} orphaned -> {out}")
    # Orphans are a reportable condition, not a crash: partial provenance is still useful.
    return 0 if result["orphaned"] == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
