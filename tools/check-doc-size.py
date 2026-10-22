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
    # Frozen lane run evidence (receipts, outputs, and the full repo copies a lane stages).
    # HOST-FILE-CHURN-POOL-GROWTH-1: 75k markdown files / ~614k directory touches per
    # Stop-hook run on the board, 45 findings on files nobody may split. Same reason as
    # profiling above; the live coordination ledgers elsewhere under .claude-state stay hub-tier.
    ".claude-state/fleet-runs/**",
    # HOST-FILE-CHURN-POOL-GROWTH-1 r3 (hub ruling on B1): a report row leaves the board only because
    # a NAMED pattern below says so, never because the walk skipped its tree unnamed. Each entry is
    # one top-level state dir that is not a ledger home; the tracked originals of any doc copied
    # into them stay governed through git. No `.claude-state/**`-wide entry (test-pinned). A new
    # top-level state dir that is on neither this list nor EXTRA_ROOTS shows up as an
    # UNLISTED_ROOT row until it is named in one of the two.
    # -- scratch / evidence copies (held 2,398 report rows in r2, incl. 11 BREACH copies of tracked docs):
    ".claude-state/tmp/**",              # lane scratch incl. full copies of the doctrine bus files
    ".claude-state/validation/**",       # frozen validation copies of tracked docs
    ".claude-state/disk-hygiene/**",     # retention and quarantine copies of whole repo checkouts
    ".claude-state/dual-venue/**",       # dual-venue evidence
    ".claude-state/project-audit/**",    # audit scratch
    ".claude-state/playback-review/**",  # playback review evidence
    ".claude-state/cos-acks-20261015/**",  # dated Chief-of-Staff ack evidence
    ".claude-state/cos-acks-20261016/**",
    ".claude-state/sbp-development/**",  # sbp evidence
    ".claude-state/sbp-deploy/**",
    ".claude-state/crash-analysis/**",
    ".claude-state/analysis/**",
    ".claude-state/ls/**",
    ".claude-state/dist/**",
    ".claude-state/tools/**",            # untracked helper copies, not the tracked tools/ tree
    ".claude-state/gpu-4090-bundle/**",
    ".claude-state/gpu-amaze-dll-bundle/**",
    ".claude-state/repin-20260921/**",
    ".claude-state/repin-20260921b/**",
    ".claude-state/repin-20260922/**",
    ".claude-state/design/**",
    ".claude-state/disk-containment/**",
    ".claude-state/cpu-triage/**",
    ".claude-state/deliverables/**",
    ".claude-state/docs-audit/**",
    ".claude-state/doctrine/**",
    ".claude-state/jev/**",
    ".claude-state/known-good-artifacts/**",
    ".claude-state/ml-gates/**",
    ".claude-state/perf-leaderboard/**",
    ".claude-state/preservation/**",
    ".claude-state/repo-hygiene/**",
    # -- state dirs that held NO markdown file on the 2026-10-22 board report (logs, builds, captures,
    #    toolchains, ssh/openssh, smoke output). Named so the UNLISTED_ROOT report stays quiet for
    #    them; a ledger home must never be added here, put it in EXTRA_ROOTS.
    ".claude-state/advance-backup-f1-20260708/**",
    ".claude-state/advance-backup-f3-20260706/**",
    ".claude-state/advance-backup-seq209-20260705/**",
    ".claude-state/agent-bridge/**",
    ".claude-state/artifacts/**",
    ".claude-state/audit-scratch/**",
    ".claude-state/auto-look-full-sweep/**",
    ".claude-state/automation/**",
    ".claude-state/bin/**",
    ".claude-state/browser-review/**",
    ".claude-state/build/**",
    ".claude-state/build-console-tests/**",
    ".claude-state/build-logs/**",
    ".claude-state/build-tmp-68f161aea8f3/**",
    ".claude-state/ci-cost/**",
    ".claude-state/ci-debug/**",
    ".claude-state/closeout-log/**",
    ".claude-state/env-backups/**",
    ".claude-state/evidence/**",
    ".claude-state/fleet-runs$D/**",
    ".claude-state/green-regression-capture/**",
    ".claude-state/green-regression-capture-after/**",
    ".claude-state/green-regression-capture-final/**",
    ".claude-state/green-regression-smoke/**",
    ".claude-state/green-regression-smoke-after/**",
    ".claude-state/laptop-bench/**",
    ".claude-state/logs/**",
    ".claude-state/look-assist-smoke/**",
    ".claude-state/look-previews/**",
    ".claude-state/nothrow-ab/**",
    ".claude-state/openssh/**",
    ".claude-state/ops/**",
    ".claude-state/playback-smoke/**",
    ".claude-state/pr84-review-20260907T174014Z/**",
    ".claude-state/pwsh/**",
    ".claude-state/python-libs/**",
    ".claude-state/quarantine/**",
    ".claude-state/recovered/**",
    ".claude-state/review/**",
    ".claude-state/sbp-remediation/**",
    ".claude-state/scale-smoke/**",
    ".claude-state/scale-toggle-smoke/**",
    ".claude-state/scratch/**",
    ".claude-state/scratch-um-watchdog/**",
    ".claude-state/scripts/**",
    ".claude-state/ssh/**",
    ".claude-state/telemetry/**",
    ".claude-state/test-logs/**",
    ".claude-state/toolchains/**",
    ".claude-state/um-one-off/**",
    ".claude-state/venue-sheets/**",
    # Generated artifacts: regenerate rather than edit.
    ".claude-state/commit-message-rewrite/**",
    # Packaged brief corpora: frozen inputs to an external consumer.
    ".claude-state/llm-playback-*/**",
    # Rotation leftovers, reaped by COORDINATION-PRUNE-POLICY, not by this check.
    "**/*.bak",
]

# Git-ignored directories scanned anyway: the hub-tier coordination ledgers a
# tracked-only scan would silently ungovern. HOST-FILE-CHURN-POOL-GROWTH-1 r2: an
# explicit allowlist, never `.claude-state` whole -- that tree also holds lane
# scratch, validation copies of tracked docs and disk-hygiene quarantine of whole
# repo checkouts, and this check runs at every turn end. A tree not listed here is
# enumerated ZERO times, and (r3) a top-level state dir named in neither this list
# nor EXEMPT is reported as an UNLISTED_ROOT WARN row. A new ledger home goes here
# by name, in the same change that creates it; test_check_doc_size_walk pins that
# the list never names the state tree whole.
EXTRA_ROOTS: list[str] = [
    ".claude-state/adjudications",   # hub rulings (BREACH-eligible ledgers)
    ".claude-state/closeout",        # closeout contract + remediation notes
    ".claude-state/continuity",      # resume/handoff designs, bus drafts
    ".claude-state/coordination",    # lane ledgers + dual-lane channel (COORDINATION-PRUNE-POLICY)
    ".claude-state/doctrine-outbox",
    ".claude-state/escalation",
    ".claude-state/handoff",
    ".claude-state/handoff-archive",
    ".claude-state/heartbeat",       # board-snapshot / inflight
    ".claude-state/hub-autopilot",
    ".claude-state/kernel",          # subject-ledger.md
    ".claude-state/planning",
    ".claude-state/project-memory",  # canonical durable memory (docs list in README.md)
]

# Directories whose OWN markdown files (depth 0, no descent) are governed, e.g.
# .claude-state/SESSION-HANDOFF.md and RESUME.md.
EXTRA_DEPTH0_DIRS: list[str] = [
    ".claude-state",
]

WALK_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv"}

# Tier name of the report row for an EXTRA_DEPTH0_DIRS child that is on neither EXTRA_ROOTS nor EXEMPT.
UNLISTED_ROOT_TIER = "UNLISTED_ROOT"

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
        encoding="utf-8",
        errors="surrogateescape",
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
        encoding="utf-8",
        errors="surrogateescape",
    )
    if proc.returncode != 0:
        print("git ls-files failed:", proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(2)
    return [p for p in proc.stdout.split("\0") if p]


_DIR_EXEMPT_CACHE: dict[tuple[str, ...], tuple[frozenset[str], tuple[str, ...]]] = {}


def _dir_exempt_patterns() -> tuple[frozenset[str], tuple[str, ...]]:
    """EXEMPT's directory patterns (`.../**`) split into literal stems and wildcard patterns."""
    key = tuple(EXEMPT)
    hit = _DIR_EXEMPT_CACHE.get(key)
    if hit is None:
        dir_patterns = [p for p in key if p.endswith("/**")]
        literal = [p[:-3] for p in dir_patterns if not any(c in p[:-3] for c in "*?[")]
        wild = tuple(p for p in dir_patterns if any(c in p[:-3] for c in "*?["))
        hit = _DIR_EXEMPT_CACHE[key] = (frozenset(os.path.normcase(s) for s in literal), wild)
    return hit


def dir_is_exempt(rel_dir: str) -> bool:
    """True when every file under this directory is exempt, so the walk need not open it.

    Only EXEMPT patterns ending in `/**` can prove that for a whole directory; a file pattern
    such as `**/*.bak` says nothing about a directory that merely has that name.
    """
    rel_dir = rel_dir.rstrip("/")
    stems, wild = _dir_exempt_patterns()
    parts = rel_dir.split("/")
    # `lit/**` with no wildcard in `lit` exempts a directory iff `lit` is that directory or one of
    # its ancestors: a set lookup per ancestor instead of one fnmatch per EXEMPT entry, which the
    # walk would pay for every directory it visits (r3 added ~100 named entries).
    if any(os.path.normcase("/".join(parts[:i])) in stems for i in range(1, len(parts) + 1)):
        return True
    probe = rel_dir + "/x"
    return any(_match(probe, p) for p in wild)


def untracked_scan(repo: Path) -> tuple[list[str], list[str]]:
    """(markdown paths, unlisted top-level dirs) of the git-ignored state this check governs.

    The depth-0 listing of each EXTRA_DEPTH0_DIRS entry is the only look at a directory that is
    neither allowlisted nor exempt: it is named in the second list and never opened
    (HOST-FILE-CHURN-POOL-GROWTH-1 r3), so a new ledger home surfaces instead of vanishing.
    """
    out: list[str] = []
    unlisted: list[str] = []
    for root in EXTRA_DEPTH0_DIRS:
        base = repo / root
        try:
            with os.scandir(base) as entries:
                for e in entries:
                    rel = (Path(root) / e.name).as_posix()
                    if e.name.endswith(".md") and e.is_file():
                        out.append(rel)
                    elif (
                        e.name not in WALK_SKIP
                        and rel not in EXTRA_ROOTS
                        and not dir_is_exempt(rel)
                        and e.is_dir()
                    ):
                        unlisted.append(rel)
        except OSError:
            continue
    for root in EXTRA_ROOTS:
        base = repo / root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune before descending: classify() would drop these later, but only after every
            # directory in them had been opened (HOST-FILE-CHURN-POOL-GROWTH-1).
            here = Path(dirpath).relative_to(repo)
            dirnames[:] = [
                d for d in dirnames
                if d not in WALK_SKIP and not dir_is_exempt((here / d).as_posix())
            ]
            for name in filenames:
                if name.endswith(".md"):
                    out.append((here / name).as_posix())
    return out, unlisted


def untracked_markdown(repo: Path) -> list[str]:
    return untracked_scan(repo)[0]


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
    untracked, unlisted = untracked_scan(repo)
    for rel in tracked_markdown(repo) + untracked:
        if rel in seen:
            continue
        seen.add(rel)
        row = row_for(repo, rel, upstream)
        if row:
            rows.append(row)
    # One WARN row per state dir nobody has named: size unknown because it is not opened.
    for rel in unlisted:
        rows.append({
            "path": rel,
            "bytes": 0,
            "tier": UNLISTED_ROOT_TIER,
            "soft": 0,
            "hard": 0,
            "state": "WARN",
        })
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
        unlisted = [r for r in warns if r["tier"] == UNLISTED_ROOT_TIER]
        files = [r for r in rows if r["tier"] != UNLISTED_ROOT_TIER]
        governed = sum(r["bytes"] for r in files)
        print(
            f"DOC SIZE: {len(files)} governed files, {governed:,} bytes, "
            f"{len(breaches)} over hard cap, {len(warns) - len(unlisted)} over soft cap, "
            f"{len(unlisted)} unlisted state roots"
        )
        if unlisted:
            print("UNLISTED_ROOT: name each in EXTRA_ROOTS (a ledger home) or EXEMPT (scratch) in")
            print("tools/check-doc-size.py; until then nothing under it is size-checked.")
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
