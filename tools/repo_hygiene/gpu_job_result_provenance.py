"""Validate that a GPU-host job result self-binds to the source it built.

Context: tools/profiling/ultra-magnus-agent.ps1 runs opaque .job.ps1 scripts on the
RTX 4090 host and writes a generic outbox\\<jobId>.result.json (jobId, exitCode,
timing, stdout/stderr, process-identity fields). That manifest carried nothing
tying a result to the source it was built from - a result was only ever trusted
by filename adjacency to a submit-time claim it never proved. Byte-identical
tools/gpu source has been observed to produce different DLL hashes across builds,
so a source sha alone would not have been a binding even if one had been added.

The job submitters (tools/profiling/invoke-ultramagnus-cdng-export-evidence.ps1
and tools/profiling/invoke-ultramagnus-p3-evidence.ps1) now write a per-job
provenance sidecar carrying four fields, which the agent echoes into its result:
  rangeHeadSha           - git HEAD of the source staged for this job
  llrawprocBlobId        - git blob id of the CPU reference (llrawproc.c) at that head
  dllSha256              - sha256 of the actually-built GPU backend DLL
  pendingSymbolPresence  - whether the built DLL exports the igpu_recon_ ABI

This module validates that a manifest carrying those fields actually binds: the
range head must be a real, locally-known commit, and the recorded llrawproc.c
blob id must match what that commit's tree actually contains - not just look like
a hash. An absent or malformed field fails closed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol
import os

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


DEFAULT_LLRAWPROC_PATH = "src/mlv/llrawproc/llrawproc.c"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProvenanceValidationError(Exception):
    """Raised when a GPU job result manifest does not self-bind."""


class GitWitness(Protocol):
    def commit_exists(self, commit: str) -> bool: ...

    def blob_id(self, commit: str, relative_path: str) -> str: ...


class SubprocessGitWitness:
    """Answers provenance questions from real, local git history only."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def _run(self, args: list[str], allowed_returncodes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            check=False, creationflags=_NO_WINDOW,
        )
        if result.returncode not in allowed_returncodes:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise ProvenanceValidationError(f"git {' '.join(args)} failed with exit code {result.returncode}: {stderr}")
        return result

    def commit_exists(self, commit: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.repo_root), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            check=False, creationflags=_NO_WINDOW,
        )
        return result.returncode == 0

    def blob_id(self, commit: str, relative_path: str) -> str:
        output = self._run(["rev-parse", f"{commit}:{relative_path}"]).stdout.decode("ascii").strip()
        if not FULL_SHA_RE.fullmatch(output):
            raise ProvenanceValidationError(
                f"git rev-parse {commit}:{relative_path} did not resolve to a blob id"
            )
        return output


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProvenanceValidationError(message)


def _require_full_sha(value: Any, label: str) -> str:
    _require(isinstance(value, str) and FULL_SHA_RE.fullmatch(value) is not None,
             f"{label} must be a lowercase full 40-character hex id")
    return value


def _require_commit(git: GitWitness, value: Any, label: str) -> str:
    commit = _require_full_sha(value, label)
    _require(git.commit_exists(commit), f"{label} commit is absent from local history: {commit}")
    return commit


def validate(
    manifest: Any,
    *,
    git: GitWitness,
    llrawproc_path: str = DEFAULT_LLRAWPROC_PATH,
) -> None:
    """Validate that a GPU job result manifest self-binds to the source it built.

    Raises ProvenanceValidationError naming the first requirement that was not met.
    """
    _require(isinstance(manifest, dict), "GPU job result manifest must be an object")

    # Range-head validation: the claimed head must be a real, locally-known commit,
    # not merely a well-formed hash - a plausible-looking sha for a commit nobody
    # has proves nothing.
    range_head = _require_commit(git, manifest.get("rangeHeadSha"), "rangeHeadSha")

    # Blob-hash binding: the claimed llrawproc.c blob id must match what that
    # commit's tree actually contains. This is what makes the field a proof
    # rather than a second unverified claim sitting next to the first.
    llrawproc_blob_id = _require_full_sha(manifest.get("llrawprocBlobId"), "llrawprocBlobId")
    actual_blob_id = git.blob_id(range_head, llrawproc_path)
    _require(
        llrawproc_blob_id == actual_blob_id,
        f"llrawprocBlobId does not match {llrawproc_path} at rangeHeadSha {range_head}: "
        f"expected {actual_blob_id}, got {llrawproc_blob_id}",
    )

    dll_sha256 = manifest.get("dllSha256")
    _require(isinstance(dll_sha256, str) and SHA256_RE.fullmatch(dll_sha256) is not None,
             "dllSha256 must be a lowercase 64-character SHA-256 digest")

    # Booleans only - not truthy/falsy stand-ins. A job that could not run the
    # export check (dumpbin missing, DLL missing) must say so explicitly by
    # failing this check, never by omitting or nulling the field.
    pending_symbol_presence = manifest.get("pendingSymbolPresence")
    _require(
        isinstance(pending_symbol_presence, bool),
        "pendingSymbolPresence must be a boolean - an inconclusive export check must "
        "still be recorded, never omitted or left null",
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceValidationError(f"manifest is not valid UTF-8 JSON: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Path to a GPU job result manifest (JSON).")
    parser.add_argument("--repo-root", type=Path, default=Path("."), help="Repository root for git history lookups.")
    parser.add_argument("--llrawproc-path", default=DEFAULT_LLRAWPROC_PATH,
                         help="Repo-relative path to the CPU reference source.")
    args = parser.parse_args(argv)

    try:
        manifest = _load_json(args.manifest)
        git = SubprocessGitWitness(args.repo_root.resolve())
        validate(manifest, git=git, llrawproc_path=args.llrawproc_path)
    except ProvenanceValidationError as exc:
        print(f"gpu_job_result_provenance: FAIL: {exc}", file=sys.stderr)
        return 1

    print("gpu_job_result_provenance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
