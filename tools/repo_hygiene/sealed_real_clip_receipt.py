"""Strict structural, semantic, and Git-object checks for sealed real-clip A/B receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import os

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


SCHEMA_NAME = "sealed-real-clip-ab-receipt.schema.json"
COMPARER_PATH = "tools/profiling/compare-release-gui-smoke-ab.ps1"
RUNNER_PATH = "tools/profiling/run-release-gui-smoke.ps1"
NEUTRAL_RECEIPT_PATH = "tests/fixtures/receipts/neutral_look_assist_off_v4.marxml"
REQUIRED_CLIPS = (
    "M16-1327",
    "M16-1347",
    "M17-1207",
    "M15-1320",
    "M16-1210",
    "M16-1243",
    "M02-1344",
)


class SealedReceiptError(ValueError):
    """Raised when a sealed receipt cannot support its declared disposition."""


def _git(repo_root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        check=False, creationflags=_NO_WINDOW,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
        raise SealedReceiptError(f"git {' '.join(args)} failed: {detail.strip()}")
    return completed.stdout


def _tree(repo_root: Path, commit: str) -> str:
    return _git(repo_root, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()


def _blob_at(repo_root: Path, commit: str, path: str) -> tuple[str, bytes]:
    blob = _git(repo_root, "rev-parse", f"{commit}:{path}").decode("ascii").strip()
    return blob, _git(repo_root, "cat-file", "blob", blob)


def _assert_source_binding(
    repo_root: Path,
    *,
    commit: str,
    path: str,
    expected_blob: str,
    expected_sha256: str,
) -> None:
    actual_blob, data = _blob_at(repo_root, commit, path)
    if actual_blob != expected_blob:
        raise SealedReceiptError(
            f"{path} blob mismatch at {commit}: {actual_blob} != {expected_blob}"
        )
    actual_sha256 = hashlib.sha256(data).hexdigest().upper()
    if actual_sha256 != expected_sha256:
        raise SealedReceiptError(
            f"{path} SHA256 mismatch: {actual_sha256} != {expected_sha256}"
        )


def validate_sealed_receipt(
    receipt: dict[str, Any],
    *,
    repo_root: Path,
    require_git_objects: bool = True,
) -> None:
    schema_path = Path(__file__).with_name(SCHEMA_NAME)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(receipt),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise SealedReceiptError(f"schema rejected {location}: {error.message}")

    verifier = receipt["verifier"]
    candidate = receipt["candidate"]
    baseline = receipt["baseline"]
    if (candidate["productHead"], candidate["productTree"]) != (
        verifier["head"],
        verifier["tree"],
    ):
        raise SealedReceiptError("candidate and verifier exact Git subjects differ")
    if candidate["productHead"] == baseline["protectedProductHead"]:
        raise SealedReceiptError("same-head baseline/candidate A/B is forbidden")
    if candidate["productTree"] == baseline["protectedProductTree"]:
        raise SealedReceiptError("same-tree baseline/candidate A/B is forbidden")

    clips = receipt["clips"]
    clip_names = [clip["name"] for clip in clips]
    if len(set(clip_names)) != len(clip_names):
        raise SealedReceiptError("duplicate named clip in sealed receipt")
    required = receipt["coverage"]["requiredNamedClips"]
    if tuple(required) != REQUIRED_CLIPS:
        raise SealedReceiptError("required named-clip corpus is not exact")
    unavailable = receipt["coverage"]["unavailable"]
    if set(clip_names) | set(unavailable) != set(REQUIRED_CLIPS):
        raise SealedReceiptError("available and unavailable clips do not close the corpus")
    if set(clip_names) & set(unavailable):
        raise SealedReceiptError("clip cannot be both passed and unavailable")
    if receipt["coverage"]["availableAndPassed"] != len(clips):
        raise SealedReceiptError("availableAndPassed does not equal the passed clip count")

    for clip in clips:
        if clip["beforeScreenshotSha256"] != clip["afterScreenshotSha256"]:
            raise SealedReceiptError(f"{clip['name']} screenshot hashes differ")
        if clip["meanAbsRgbDelta"] > verifier["maxMeanAbsRgbDelta"]:
            raise SealedReceiptError(f"{clip['name']} mean RGB delta exceeds policy")
        if clip["changedSampleRatio"] > verifier["maxChangedSampleRatio"]:
            raise SealedReceiptError(f"{clip['name']} changed-sample ratio exceeds policy")

    if not require_git_objects:
        return

    if _tree(repo_root, candidate["productHead"]) != candidate["productTree"]:
        raise SealedReceiptError("candidate commit does not resolve to the recorded tree")
    if _tree(repo_root, baseline["protectedProductHead"]) != baseline["protectedProductTree"]:
        raise SealedReceiptError("baseline protected commit does not resolve to the recorded tree")
    if _tree(repo_root, verifier["head"]) != verifier["tree"]:
        raise SealedReceiptError("verifier commit does not resolve to the recorded tree")
    ancestry = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            baseline["protectedProductHead"],
            candidate["productHead"],
        ],
        cwd=repo_root,
        capture_output=True,
        check=False, creationflags=_NO_WINDOW,
    )
    if ancestry.returncode != 0:
        raise SealedReceiptError("protected baseline is not an ancestor of candidate product head")

    _assert_source_binding(
        repo_root,
        commit=verifier["head"],
        path=COMPARER_PATH,
        expected_blob=verifier["comparerGitBlob"],
        expected_sha256=verifier["comparerSha256"],
    )
    _assert_source_binding(
        repo_root,
        commit=verifier["head"],
        path=RUNNER_PATH,
        expected_blob=verifier["runnerGitBlob"],
        expected_sha256=verifier["runnerSha256"],
    )
    _assert_source_binding(
        repo_root,
        commit=verifier["head"],
        path=NEUTRAL_RECEIPT_PATH,
        expected_blob=receipt["renderContract"]["receiptGitBlob"],
        expected_sha256=receipt["renderContract"]["receiptSha256"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    receipt_path = args.receipt
    if not receipt_path.is_absolute():
        receipt_path = repo_root / receipt_path
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    validate_sealed_receipt(receipt, repo_root=repo_root, require_git_objects=True)
    print(
        "SEALED_RECEIPT_PASS "
        f"clips={len(receipt['clips'])} "
        f"candidate={receipt['candidate']['productHead']} "
        "authority=CLOSED"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
