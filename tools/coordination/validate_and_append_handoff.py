#!/usr/bin/env python3
"""Validate and append one exact-range CODEX handoff to all coordination ledgers.

The command is deliberately strict: the two Git refs must resolve to commits,
the feature must descend from the start, and all three ledgers are verified
after the append. A failed multi-file append restores the original bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False, creationflags=_NO_WINDOW
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def resolve_commit(repo: Path, value: str, label: str) -> str:
    resolved = run_git(repo, "rev-parse", "--verify", f"{value}^{{commit}}")
    if not SHA_RE.fullmatch(resolved):
        raise RuntimeError(f"{label} did not resolve to a canonical full-40 commit: {value}")
    return resolved


def validate_range(repo: Path, start: str, feature: str) -> tuple[str, str, str]:
    start_sha = resolve_commit(repo, start, "start")
    feature_sha = resolve_commit(repo, feature, "feature")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", start_sha, feature_sha],
        cwd=repo,
        capture_output=True,
        check=False, creationflags=_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"feature {feature_sha} is not descended from start {start_sha}")
    return start_sha, feature_sha, f"{start_sha}..{feature_sha}"



def coordination_timestamp(supplied: "str | None") -> str:
    """Return the entry timestamp, rejecting a caller value that is not a real instant.

    The ledger heading grammar is `### [<timestamp>] ACTOR - KIND (free text)`, and the
    closeout content-review gate BLOCKS on a heading it cannot parse
    (`content_approval_unparsable_heading`). This appender previously used --timestamp
    verbatim, so `--timestamp NOT-A-CLOCK` produced a heading that could stop finalize --
    a denial of service on the gate, authored by our own tool.

    Validate only; the caller's value is passed through unchanged when it parses, because
    --timestamp exists for replay and fixtures and silently rewriting it would surprise
    those callers.
    """
    if not supplied:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        datetime.fromisoformat(supplied)
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"--timestamp must be an ISO-8601 instant, got {supplied!r}: {exc}"
        )
    return supplied


def build_entry(args: argparse.Namespace, range_token: str) -> str:
    timestamp = coordination_timestamp(args.timestamp)
    fields = [
        f"### [{timestamp}] CODEX - HANDOFF ({args.summary})",
        f"Range: {range_token}",
        "Verdict: HANDOFF",
        f"WorkBlock: {args.work_block}",
        f"Changes: {args.changes}",
        f"Validation: {args.validation}",
        f"Proof boundary: {args.proof_boundary}",
        f"Request: {args.request}",
        f"Reviewer: {args.reviewer}",
        "",
    ]
    return "\n".join(fields)


def verify_tail(path: Path, entry: str, range_token: str, work_block: str, feature_sha: str) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.endswith(entry):
        raise RuntimeError(f"post-append invariant failed: {path} does not end with the handoff block")
    tail = text[-max(len(entry) + 256, 4096) :]
    for token in (range_token, work_block, feature_sha):
        if token not in tail:
            raise RuntimeError(f"post-append invariant failed: {path} tail lacks {token}")


def append_transactionally(paths: list[Path], entry: str, range_token: str, work_block: str, feature_sha: str) -> None:
    originals: dict[Path, bytes] = {}
    try:
        for path in paths:
            if not path.is_file():
                raise RuntimeError(f"ledger missing: {path}")
            original = path.read_bytes()
            if range_token in original.decode("utf-8", errors="replace") and work_block in original.decode("utf-8", errors="replace"):
                raise RuntimeError(f"duplicate handoff identity already present: {path}")
            originals[path] = original

        for path, original in originals.items():
            separator = b"" if not original or original.endswith((b"\n", b"\r")) else b"\n"
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(original)
                handle.write(separator)
                handle.write(entry.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)

        for path in paths:
            verify_tail(path, entry, range_token, work_block, feature_sha)
    except Exception:
        for path, original in originals.items():
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.rollback.", delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--start", required=True, help="start commit/ref")
    parser.add_argument("--feature", required=True, help="feature commit/ref")
    parser.add_argument("--work-block", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--changes", required=True)
    parser.add_argument("--validation", required=True)
    parser.add_argument("--proof-boundary", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--reviewer", default="Claude")
    parser.add_argument("--timestamp")
    parser.add_argument("--ledger", action="append", type=Path, dest="ledgers")
    parser.add_argument("--codex-ledger", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo_root.resolve()
    start_sha, feature_sha, range_token = validate_range(repo, args.start, args.feature)
    args.ledgers = args.ledgers or [
        repo / ".claude-state/coordination/gpu-lane-impl-review-sync.md",
        repo / ".claude-state/coordination/dual-lane/claude.md",
    ]
    paths = [path if path.is_absolute() else repo / path for path in args.ledgers]
    if args.codex_ledger:
        paths.append(args.codex_ledger if args.codex_ledger.is_absolute() else repo / args.codex_ledger)
    entry = build_entry(args, range_token)
    if args.dry_run:
        print(entry, end="")
        return 0
    append_transactionally(paths, entry, range_token, args.work_block, feature_sha)
    digest = hashlib.sha256(entry.encode("utf-8")).hexdigest()
    print(f"HANDOFF_APPENDED range={range_token} workBlock={args.work_block} entrySha256={digest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"HANDOFF_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
