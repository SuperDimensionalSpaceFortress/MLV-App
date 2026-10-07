#!/usr/bin/env python3
"""Portable documentation size checker.

Ported into MLV-App with the repo-specific parts confined to the CONFIG block
below. The machinery below END CONFIG is deliberately unmodified so findings stay
comparable across projects in the fleet.

Budgets are assigned by ROLE, not by directory, because the cost of a large file
is proportional to how often it is read. An entry point every cold session opens
is expensive at 12 KB; a spec one implementer reads once is not expensive at
30 KB.

Companion check: this script measures SIZE only. Splitting a doc in this repo can
also break closeout, because `tools/repo_hygiene/brokered_closeout.py` and its
test suite assert exact substrings against CLAUDE.md, AGENTS.md and several docs/
files. That is guarded separately by `tools/docs/check_pinned_tokens.py`, which
has no equivalent in the portable machinery. Run both.

Exit status:
  0  no hard-cap breach (soft-cap warnings may still be printed)
  1  at least one hard-cap breach
  2  bad usage, or the git listing failed
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

# =============================== CONFIG =====================================
# Everything below this line to END CONFIG is repo-specific.

# Tier budgets, in bytes.
#   soft: warn, and plan the split.
#   hard: breach; demote the file to an index in place before the turn ends.
#
# Glob note: matching is fnmatch over forward-slash relative paths, so `*`
# crosses `/`. A leading `**/` is stripped and retried, but a bare `docs/*.md`
# still matches `docs/a/b.md`.
TIERS = {
    "entry": {
        "soft": 8_000,
        "hard": 12_000,
        "why": "auto-loaded by an agent on every cold session; every byte taxes every session",
        "globs": [
            # Exactly two files. Keeping this list tiny is what makes the budget
            # mean anything. README.md is NOT here: it is upstream fork ancestry
            # and is exempt below, not entry-tier.
            "CLAUDE.md",
            "AGENTS.md",
        ],
    },
    "hub": {
        # Git-ignored coordination state. NOT a number invented here: 150 KB is
        # the live-lane-ledger budget ratified in
        # .claude-state/coordination/COORDINATION-PRUNE-POLICY.md (BINDING
        # 2026-07-15, acked by fable + codex + claude-review, opus with
        # amendment A1). That policy defines a SINGLE trigger, so soft == hard
        # here on purpose -- inventing a second threshold would be freelancing
        # on a multi-lane ratified policy.
        "soft": 150_000,
        "hard": 150_000,
        "why": "multi-lane coordination ledgers; re-read on every seat rotation (ratified 150 KB)",
        "globs": [".claude-state/**"],
    },
    "ledger": {
        "soft": 30_000,
        "hard": 60_000,
        "why": "append-only; the live head is capped and the tail rotates to a dated archive",
        "globs": [
            ".claude/ANALYSIS_LOG.md",
        ],
    },
    "working": {
        "soft": 25_000,
        "hard": 40_000,
        "why": "read in full by one lane when it picks the item up",
        "globs": [
            "docs/**",
            "tools/**",
            ".claude/**",
            "claude/**",
            "*.md",
        ],
    },
}

# First match wins, so the most specific tier goes first.
TIER_ORDER = ["entry", "hub", "ledger", "working"]

FALLBACK_TIER = "working"

# Exempt by policy, not by oversight. Every entry carries a reason.
EXEMPT = [
    # Archives: the destination of a roll cannot itself be over budget.
    "**/*-archive-*.md",
    "**/*_ARCHIVE*.md",
    "**/archive/**",
    "**/*-superseded-*.md",
    # Hash-bound frozen file: must stay byte-identical across repos, verified by
    # CLOSEOUT-CANONICAL-CONTRACT.sha256. Editing it breaks a cross-repo contract.
    "CLOSEOUT-CANONICAL-CONTRACT.md",
    # Vendored third-party tree; not our source.
    "platform/qt/avir/**",
    # Upstream MLV-App fork ancestry; diverging complicates merges.
    "README.md",
    # Immutable recorded evidence: splitting rewrites the record it exists to preserve.
    # .claude-state/profiling holds packaged dogfood/compare kits that bundle a
    # COPY of a tracked doc; governing the copy would double-count the original.
    ".claude/profiling/**",
    ".claude/analysis/**",
    ".claude-state/profiling/**",
    # Untracked full repo copies (.gitignore:55) -- would multiply every count.
    ".claude/worktrees/**",
    ".claude-state/worktrees/**",
    ".claude-state/closeout/repo-sweep/integration-probes/**",
    # Generated artifacts: regenerate rather than edit.
    ".claude-state/commit-message-rewrite/**",
    # Packaged brief corpora: frozen inputs to an external consumer.
    ".claude-state/llm-playback-*/**",
    # Rotation leftovers, reaped by COORDINATION-PRUNE-POLICY, not by this check.
    "**/*.bak",
]

# Directories scanned even though git does not track them. .claude-state is 58%
# of this repo's markdown mass; a tracked-only scan would report compliance while
# the largest, hottest files sat outside it.
EXTRA_ROOTS: list[str] = [
    ".claude-state",
]

WALK_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv"}

# This fork's markdown ancestry is not cleanly separable by ref, and the handful
# of genuinely-upstream docs are listed in EXEMPT instead. Left off.
UPSTREAM_REF: str | None = None
# ============================= END CONFIG ===================================


def _match(path: str, pattern: str) -> bool:
    """fnmatch, with a leading `**/` also allowed to match at the root."""
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    return False


def upstream_markdown(repo: Path) -> set[str]:
    """Markdown inherited from upstream. Empty set when the ref is unavailable,
    which degrades safely to governing everything."""
    if not UPSTREAM_REF:
        return set()
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "-z", UPSTREAM_REF],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return set()
    return {p for p in proc.stdout.split("\0") if p.endswith(".md")}


def classify(path: str, upstream: frozenset[str] = frozenset()) -> str | None:
    """Return the tier name for a path, or None when the path is exempt."""
    if path in upstream:
        return None
    for pattern in EXEMPT:
        if _match(path, pattern):
            return None
    for tier in TIER_ORDER:
        for pattern in TIERS[tier]["globs"]:
            if _match(path, pattern):
                return tier
    return FALLBACK_TIER


def tracked_markdown(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "*.md"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print("git ls-files failed:", proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(2)
    return [p for p in proc.stdout.split("\0") if p]


def untracked_markdown(repo: Path) -> list[str]:
    out: list[str] = []
    for root in EXTRA_ROOTS:
        base = repo / root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in WALK_SKIP]
            for name in filenames:
                if name.endswith(".md"):
                    rel = (Path(dirpath) / name).relative_to(repo)
                    out.append(rel.as_posix())
    return out


def row_for(repo: Path, rel: str, upstream: frozenset[str]) -> dict | None:
    tier = classify(rel, upstream)
    if tier is None:
        return None
    try:
        size = (repo / rel).stat().st_size
    except OSError:
        return None
    budget = TIERS[tier]
    state = (
        "BREACH" if size > budget["hard"]
        else "WARN" if size > budget["soft"]
        else "ok"
    )
    return {
        "path": rel,
        "bytes": size,
        "tier": tier,
        "soft": budget["soft"],
        "hard": budget["hard"],
        "state": state,
    }


def scan(repo: Path) -> list[dict]:
    upstream = frozenset(upstream_markdown(repo))
    seen: set[str] = set()
    rows: list[dict] = []
    for rel in tracked_markdown(repo) + untracked_markdown(repo):
        if rel in seen:
            continue
        seen.add(rel)
        row = row_for(repo, rel, upstream)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: (-r["bytes"], r["path"]))
    return rows


TRACE_KEEP_LINES = 200


def _write_trace(repo: Path, rel: str, governed: int, breaches: int, warns: int) -> None:
    """Append one run record, then rotate. Never raises: an observability aid must
    not be able to fail the thing it is observing."""
    try:
        from datetime import datetime, timezone

        target = Path(rel)
        if not target.is_absolute():
            target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{stamp} governed={governed} breaches={breaches} warnings={warns}\n")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
        # P3: regenerable machine log -> rotate at cap and drop the excess.
        existing = target.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(existing) > TRACE_KEEP_LINES:
            target.write_text("".join(existing[-TRACE_KEEP_LINES:]), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="repository root (default: cwd)")
    ap.add_argument("--all", action="store_true", help="list every file, not just problems")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--warn-only",
        action="store_true",
        help="always exit 0; use when reporting rather than gating",
    )
    ap.add_argument(
        "--path",
        action="append",
        default=[],
        help="check only these paths (repeatable); useful from an editor or Stop hook",
    )
    ap.add_argument(
        "--trace",
        metavar="FILE",
        help="append a one-line run record. Without this a hook-driven run leaves no "
             "evidence, so 'the hook is wired' cannot be distinguished from 'the hook "
             "is inert' after the fact. Rotates per COORDINATION-PRUNE-POLICY P3: this "
             "is a regenerable machine log, not fidelity.",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()

    if args.path:
        # Explicit paths are checked whether or not they are tracked yet, so a
        # file can be checked in the same turn it is written.
        upstream = frozenset(upstream_markdown(repo))
        rows = []
        for raw in args.path:
            rel = raw.replace("\\", "/")
            rel = rel[2:] if rel.startswith("./") else rel
            if classify(rel, upstream) is None:
                print(f"exempt (not governed): {rel}")
                continue
            row = row_for(repo, rel, upstream)
            if row is None:
                print(f"missing: {rel}", file=sys.stderr)
                continue
            rows.append(row)
        rows.sort(key=lambda r: (-r["bytes"], r["path"]))
    else:
        rows = scan(repo)

    breaches = [r for r in rows if r["state"] == "BREACH"]
    warns = [r for r in rows if r["state"] == "WARN"]

    if args.json:
        print(json.dumps(
            {"rows": rows, "breaches": len(breaches), "warnings": len(warns)},
            indent=2,
        ))
    else:
        shown = rows if args.all else breaches + warns
        if shown:
            print(f"{'bytes':>9}  {'tier':8} {'state':6} path")
            for r in shown:
                print(f"{r['bytes']:>9,}  {r['tier']:8} {r['state']:6} {r['path']}")
            print()
        governed = sum(r["bytes"] for r in rows)
        print(
            f"DOC SIZE: {len(rows)} governed files, {governed:,} bytes, "
            f"{len(breaches)} over hard cap, {len(warns)} over soft cap"
        )
        if breaches:
            print("Over the hard cap -> demote the file to an index IN PLACE and move")
            print("the detail into children. Do NOT rename the parent: every existing")
            print("link and pinned string must keep resolving.")

    if args.trace:
        _write_trace(repo, args.trace, len(rows), len(breaches), len(warns))

    if args.warn_only:
        return 0
    return 1 if breaches else 0


if __name__ == "__main__":
    raise SystemExit(main())
