"""ATTR3-FOOTAGE-STAGE-1 round 7 (item 3: pinned I/O inventory) -- makes the class finite.

Since round 3 every round found a NEW instance of the SAME two recurring defect classes (link
containment at some I/O site; a path escaping through an uncaught exception) because nothing
proved the set of I/O call sites in these four files was finite and fully reviewed -- each round's
fix covered only the specific site a review happened to find.

This test runs tools/repo_hygiene/attr3_footage_io_inventory_scan.ps1 -- an AST-based scan of
every file-system cmdlet call and .NET I/O member call in attr3-footage-stage.ps1,
Attr3FootageStageJob.psm1, Attr3FootagePresenceJob.psm1 and AttrCudaOwnerFootage.psm1, INCLUDING
the two emitted job templates ($template here-strings), parsed as their own scripts too -- and
compares its live output against the checked-in, hand-classified
tools/repo_hygiene/attr3_footage_io_inventory.json. Any row that is NEW, CHANGED (moved line,
changed text) or MISSING from the inventory fails test_every_live_site_is_pinned_and_classified;
any row LEFT in the inventory that the live scan no longer finds fails
test_no_stale_inventory_rows, so the pinning is exact in both directions -- a future edit that
touches an I/O call site in these four files must be re-classified here, not just reviewed.

Disposition legend (also documented in the inventory file's own rows, per-site):
  boundary-root-asserted  the call's target path was proven link-free from a TRUSTED ROOT down to
                           (and including) the call's own target, via Assert-AttrCudaNoLinkBelowRoot
                           or Assert-Attr3NoLinkFromBoundary, checked earlier in the same
                           control-flow path (this function, or a documented caller contract).
  leaf-asserted            the call operates on a specific LEAF path (the fixed-name residue
                           marker) whose own link-safety was established directly for this call --
                           either an Assert-Attr3NoLinkFromBoundary check immediately before it, or
                           (for the marker WRITE) [IO.FileMode]::CreateNew's own exclusive-creation
                           semantics, which refuse outright rather than write through an existing
                           file or reparse point.
  job-private-temp         the call's target is a path this run/job itself created fresh (a
                           GUID-named temp file/directory, or a private per-job hard-link
                           workspace) -- nothing else could have pre-planted a link there before
                           this run's own creation.
  read-only-fixed          the call only ever reads metadata or bytes -- never enumerates a
                           directory for further action, never writes or deletes -- from a path
                           that is either fixed/generator-controlled with no caller influence, or
                           whose worst case under a reparse point discloses identity/attribute
                           metadata alone, never file content or a mutation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "tools" / "repo_hygiene" / "attr3_footage_io_inventory_scan.ps1"
INVENTORY = ROOT / "tools" / "repo_hygiene" / "attr3_footage_io_inventory.json"
PWSH = shutil.which("pwsh")

DISPOSITIONS = {"boundary-root-asserted", "leaf-asserted", "job-private-temp", "read-only-fixed"}


def _row_key(row: dict) -> tuple:
    # NEW or CHANGED (moved line, changed shape at the same line) both fail: the key includes
    # source, line, kind AND name, so a call that merely moved lines -- even by one, even to the
    # same statement reordered -- no longer matches its old inventory entry and must be
    # re-classified, not silently carried forward by fuzzy matching.
    return (row["source"], row["line"], row["kind"], row["name"])


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class Attr3FootageIoInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(SCANNER)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.live_rows = json.loads(proc.stdout)
        self.inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))["rows"]
        self.inventory_by_key = {_row_key(r): r for r in self.inventory}

    def test_scan_itself_parses_every_source_with_no_ast_error(self) -> None:
        parse_errors = [r for r in self.live_rows if r["kind"] in ("parse-error", "template-missing")]
        self.assertEqual(parse_errors, [], json.dumps(parse_errors, indent=2))

    def test_inventory_has_no_duplicate_keys(self) -> None:
        keys = [_row_key(r) for r in self.inventory]
        duplicates = [k for k in keys if keys.count(k) > 1]
        self.assertEqual(duplicates, [], f"duplicate inventory rows: {duplicates}")

    def test_every_inventory_row_carries_a_known_disposition(self) -> None:
        bad = [r for r in self.inventory if r.get("disposition") not in DISPOSITIONS]
        self.assertEqual(bad, [], json.dumps(bad, indent=2))

    def test_every_live_site_is_pinned_and_classified(self) -> None:
        # A NEW site (not in the inventory at all) or a CHANGED one (line/kind/name shifted, so
        # its old key no longer matches) both land here -- this is the test that "makes the class
        # finite": nothing in these four files' I/O surface can change shape without this test
        # demanding an explicit, honest disposition for it.
        missing = [row for row in self.live_rows if _row_key(row) not in self.inventory_by_key]
        self.assertEqual(
            missing, [],
            "New or changed I/O call site(s) with no pinned classification -- add a row to "
            f"{INVENTORY.name} with an honest disposition (see this test module's own docstring "
            "for the legend), or fix the site so it no longer needs one:\n"
            + json.dumps(missing, indent=2),
        )

    def test_no_stale_inventory_rows(self) -> None:
        # A row the live scan no longer finds (the call was removed, or moved/changed shape and
        # was re-added under a new key without removing the old one) would otherwise let the
        # inventory silently drift away from what the code actually does.
        live_keys = {_row_key(row) for row in self.live_rows}
        stale = [row for row in self.inventory if _row_key(row) not in live_keys]
        self.assertEqual(
            stale, [],
            f"Inventory row(s) no longer found by the live scan -- remove from {INVENTORY.name} "
            "if the call site was deleted, or fix the row's source/line/kind/name if it merely "
            "moved:\n" + json.dumps(stale, indent=2),
        )

    def test_every_matched_row_agrees_on_text_and_function(self) -> None:
        # Same key (source/line/kind/name) but the SURROUNDING text or enclosing function differs
        # -- a more surgical edit than a line move (e.g. the same call rewritten in place with a
        # different argument) that the key alone would not catch.
        mismatches = []
        for row in self.live_rows:
            key = _row_key(row)
            pinned = self.inventory_by_key.get(key)
            if pinned is None:
                continue
            if pinned.get("text") != row.get("text") or pinned.get("function") != row.get("function"):
                mismatches.append({"key": key, "live": row, "pinned": pinned})
        self.assertEqual(mismatches, [], json.dumps(mismatches, indent=2))


if __name__ == "__main__":
    unittest.main()
