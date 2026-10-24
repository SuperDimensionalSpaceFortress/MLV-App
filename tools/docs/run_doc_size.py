#!/usr/bin/env python3
"""Run the fleet's canonical check-doc-size BY PIN against this repo's budget config.

CHECK-DOC-SIZE-SINGLE-SOURCE-1: tools/check-doc-size.py (a project copy of a bus tool) was retired in
favour of the reviewed, pinned bus bytes. This wrapper is the one MLV-specific seam:

  * it runs `python -I factory-pins/pin-runner.py run check-doc-size -- --config
    tools/docs/doc-size-budget.config.json --repo <repo> ...`, so the bytes that execute are the blob
    recorded in factory-pins/factory-pins.json (a bus push changes nothing that runs);
  * it adds the one thing the pinned tool does not have, `--trace FILE`: append a one-line run
    record `<utc> governed=N breaches=B warnings=W` so a hook-driven run leaves evidence
    (docs/22-doc-fragmentation-policy.md, "Stop hook"). Without it "the hook is wired" cannot be told
    from "the hook is inert". Never raises: an observability aid must not fail what it observes.

Every other argument is forwarded unchanged (--all, --json, --warn-only, --path, --headroom, --strict,
--repo, --config). The exit status is the pinned tool's (0 clean, 1 breach, 2 usage), or the runner's
own (2 refused: bytes do not match the pin; 3 unavailable: the pinned blob is in neither the blob cache
nor a bus clone -- see docs/22 "Pinned runs").
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "factory-pins" / "pin-runner.py"
CONFIG = ROOT / "tools" / "docs" / "doc-size-budget.config.json"
TRACE_KEEP_LINES = 200
SUMMARY = re.compile(r"^DOC SIZE: (\d+) governed files \(\d+ gated, \d+ deferred\), (\d+) breach, (\d+) over soft cap", re.M)


def split_trace(argv: list[str]) -> tuple[list[str], str | None]:
    """Remove `--trace FILE` (or `--trace=FILE`) from argv; the pinned tool does not know it."""
    rest: list[str] = []
    trace: str | None = None
    it = iter(argv)
    for arg in it:
        if arg == "--trace":
            trace = next(it, None)
        elif arg.startswith("--trace="):
            trace = arg.split("=", 1)[1]
        else:
            rest.append(arg)
    return rest, trace


def counts(text: str) -> tuple[int, int, int] | None:
    found = SUMMARY.search(text)
    if found:
        return int(found.group(1)), int(found.group(2)), int(found.group(3))
    try:
        data = json.loads(text)
        return len(data["rows"]), int(data["breaches"]), int(data["warnings"])
    except (ValueError, KeyError, TypeError):
        return None


def write_trace(repo: Path, rel: str, found: tuple[int, int, int]) -> None:
    try:
        target = Path(rel)
        if not target.is_absolute():
            target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} governed={found[0]} breaches={found[1]} warnings={found[2]}\n")
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > TRACE_KEEP_LINES:
            target.write_text("".join(lines[-TRACE_KEEP_LINES:]), encoding="utf-8")
    except Exception:
        pass


def main(argv: list[str]) -> int:
    args, trace = split_trace(argv)
    if "--repo" not in args and not any(a.startswith("--repo=") for a in args):
        args += ["--repo", str(ROOT)]
    if "--config" not in args and not any(a.startswith("--config=") for a in args):
        args += ["--config", str(CONFIG)]
    repo = Path(next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--repo"), str(ROOT))).resolve()
    done = subprocess.run([sys.executable, "-X", "utf8", "-I", str(RUNNER), "run", "check-doc-size", "--", *args],
                          cwd=ROOT, capture_output=True)
    sys.stdout.buffer.write(done.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(done.stderr)
    sys.stderr.buffer.flush()
    if trace and done.returncode in (0, 1):
        found = counts(done.stdout.decode("utf-8", errors="replace"))
        if found:
            write_trace(repo, trace, found)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
