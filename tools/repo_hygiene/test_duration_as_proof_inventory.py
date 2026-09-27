"""PROD-TELEMETRY-DURATION-AS-PROOF-4 round 1: make the duration-as-proof inventory mechanical.

Two PRs in a row (#156, #169) were parked because a hand-made inventory of "stage ran"
inferred from elapsed ms > 0 was incomplete -- the producer's own claim that a given file held
no other such site was proven false by a cross-family reviewer both times. This test replaces
the hand inventory with a live, CI-enforced one: tools/repo_hygiene/duration_as_proof_scan.py
re-derives every candidate site (see that module's docstring for the four pattern shapes) from
src/, platform/qt/ and tests/, and this test diffs that live output against the checked-in,
hand-classified tools/repo_hygiene/duration_as_proof_inventory.json.

A site is identified by (path, anchor, occurrence_count) -- anchor is NORMALIZED LINE TEXT
(see duration_as_proof_scan.normalize_anchor), never a line number, and occurrence_count is
how many physical lines in that file currently share that anchor (len of the row's own
"lines" list). So a site that merely moves (another line inserted above it) does not need
reclassification, but ANY change to the line's own text does -- the anchor no longer
matches -- and so does adding a new, otherwise-identical copy of an already-classified
anchor: the count changes, so the (path, anchor, count) key changes too, and the new copy
cannot silently ride in on the old row's classification. Both directions fail the gate:
  - a NEW or CHANGED site, or a NEW occurrence of an existing one
    (test_every_live_site_is_pinned_and_classified)
  - a STALE row the live scan no longer finds at that count (test_no_stale_inventory_rows)

Positive controls (test_matcher_flags_seeded_positive_control /
test_matcher_ignores_seeded_negative_control) pin the scanner's own behavior against two
seeded snippets that are never written to disk as real source, so the gate proves it actually
runs the four patterns rather than passing vacuously.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.repo_hygiene.duration_as_proof_scan import ROOT, normalize_anchor, scan_repo, scan_text

INVENTORY = Path(__file__).resolve().parent / "duration_as_proof_inventory.json"

KNOWN_CLASSES = {
    "division_guard", "display_or_format", "value_fallback_select",
    "not_run_proof_structural", "run_proof",
}
KNOWN_CLOCKS = {"qpc_stage_clock", "omp_get_wtime_direct", "other"}


def _row_key(row: dict) -> tuple:
    # The occurrence count (how many physical lines this anchor is found at) is part of the
    # site's identity: an inventory row pins the anchor AND how many times it currently
    # occurs, so a newly added identical copy of a classified line is a different key and
    # must be classified itself, rather than silently inheriting the original row's verdict.
    return (row["path"], row["anchor"], len(row.get("lines", [])))


class DurationAsProofInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.live_candidates = scan_repo(ROOT)
        cls.live_by_key = {(c.path, c.anchor, len(c.lines)): c for c in cls.live_candidates}
        data = json.loads(INVENTORY.read_text(encoding="utf-8"))
        cls.meta = data["_meta"]
        cls.inventory = data["rows"]
        cls.inventory_by_key = {_row_key(r): r for r in cls.inventory}

    def test_scanner_finds_at_least_one_candidate(self) -> None:
        # A scanner that silently matched nothing would make every other test in this module
        # vacuously pass. src/, platform/qt/ and tests/ are known (from the #169 review) to
        # contain real duration-as-proof sites, so an empty live scan is itself a failure.
        self.assertGreater(len(self.live_candidates), 0)

    def test_inventory_has_no_duplicate_keys(self) -> None:
        keys = [_row_key(r) for r in self.inventory]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(duplicates, [])

    def test_every_inventory_row_carries_a_known_class_and_clock(self) -> None:
        bad = [
            {"key": _row_key(r), "class": r.get("class"), "clock": r.get("clock")}
            for r in self.inventory
            if r.get("class") not in KNOWN_CLASSES or r.get("clock") not in KNOWN_CLOCKS
        ]
        self.assertEqual(bad, [], json.dumps(bad, indent=2))

    def test_every_run_proof_row_names_a_flag_or_is_openly_tracked(self) -> None:
        bad = []
        for r in self.inventory:
            if r.get("class") != "run_proof":
                continue
            has_flag = bool(r.get("flag"))
            is_open = r.get("status") == "open" and bool(r.get("successor"))
            if not (has_flag or is_open):
                bad.append(_row_key(r))
        self.assertEqual(
            bad, [],
            "run_proof row(s) with neither a replacing 'flag' nor an open 'status'+'successor': "
            f"{bad}",
        )

    def test_every_inventory_row_carries_a_nonempty_justification(self) -> None:
        bad = [_row_key(r) for r in self.inventory if not r.get("justification", "").strip()]
        self.assertEqual(bad, [])

    def test_every_live_site_is_pinned_and_classified(self) -> None:
        missing = [
            {"path": c.path, "anchor": c.anchor, "lines": list(c.lines), "triggers": list(c.triggers)}
            for key, c in self.live_by_key.items()
            if key not in self.inventory_by_key
        ]
        self.assertEqual(
            missing, [],
            "New or changed duration-as-proof site(s) with no pinned classification -- add a "
            f"row to {INVENTORY.name} with an honest class/clock/justification (see this test "
            "module's docstring and the inventory file's own _meta block for the legend), or "
            "fix the site so it no longer needs one:\n" + json.dumps(missing, indent=2),
        )

    def test_no_stale_inventory_rows(self) -> None:
        stale = [
            _row_key(r) for r in self.inventory if _row_key(r) not in self.live_by_key
        ]
        self.assertEqual(
            stale, [],
            f"Inventory row(s) no longer found by the live scan -- remove from {INVENTORY.name} "
            "if the site was deleted/rewritten away, or fix the row's anchor if the line's text "
            f"changed:\n{json.dumps(stale, indent=2)}",
        )

    # -- positive controls: prove the scanner itself still recognizes each pattern shape ------

    def test_matcher_flags_seeded_positive_control(self) -> None:
        seeded = "\n".join([
            "void f() {",
            '    if (some_stage_duration_ms > 0.0) { markRan(); }',
            "}",
        ])
        found = scan_text(seeded, source="<seeded-positive>")
        self.assertEqual(len(found), 1, found)
        self.assertIn("ident_compare", found[0].triggers)
        self.assertEqual(
            found[0].anchor,
            normalize_anchor("    if (some_stage_duration_ms > 0.0) { markRan(); }"),
        )

    def test_matcher_flags_seeded_json_key_and_assert_macro_controls(self) -> None:
        seeded = "\n".join([
            'if (sample.value(QStringLiteral("seeded_probe_ms")).toDouble() > 0.0) { return; }',
            "ASSERT_EQ(0.0, getSeededProbeMilliseconds());",
        ])
        found = scan_text(seeded, source="<seeded-json-and-macro>")
        triggers_by_anchor = {c.anchor: set(c.triggers) for c in found}
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("json_ms_key" in t for t in triggers_by_anchor.values()))
        self.assertTrue(any("assert_macro" in t for t in triggers_by_anchor.values()))

    def test_matcher_flags_seeded_json_key_with_default_arg_control(self) -> None:
        # A `.toDouble(<default>)` / `.toInt(<default>)` read (a default-value fallback, not
        # the bare no-arg form) must still be recognized -- this is the shape sol/fable found
        # missing in test_dual_iso_pipeline.cpp's `avg_ms`.toDouble(-1.0) reads.
        seeded = 'if (sample.value(QStringLiteral("seeded_default_ms")).toDouble(-1.0) > 0.0) { return; }'
        found = scan_text(seeded, source="<seeded-json-default-arg>")
        self.assertEqual(len(found), 1, found)
        self.assertIn("json_ms_key", found[0].triggers)

    def test_matcher_flags_seeded_exclusive_getter_control(self) -> None:
        # A bare (non-macro) comparison against a duration-named getter call must be caught
        # on its own -- not only when it happens to be wrapped in an ASSERT_*/EXPECT_* macro,
        # which would let disabling the getter-comparison path hide behind assert_macro
        # coverage instead of being independently proven (sol's "no exclusive positive
        # control" hardening finding).
        seeded = "if (getSeededProbeMilliseconds() > 0.0) { return; }"
        found = scan_text(seeded, source="<seeded-exclusive-getter>")
        self.assertEqual(len(found), 1, found)
        self.assertIn("duration_getter", found[0].triggers)
        self.assertNotIn("assert_macro", found[0].triggers)

    def test_matcher_flags_seeded_near_and_double_eq_macro_controls(self) -> None:
        # ASSERT_NEAR/EXPECT_NEAR (3-arg, tolerance ignored) and ASSERT_DOUBLE_EQ/
        # ASSERT_FLOAT_EQ (2-arg) were previously outside the macro alternation entirely.
        seeded = "\n".join([
            "ASSERT_NEAR(0.0, getSeededNearProbeMs(), 1e-9);",
            "ASSERT_DOUBLE_EQ(0.0, getSeededDoubleEqProbeMs());",
        ])
        found = scan_text(seeded, source="<seeded-near-and-double-eq>")
        self.assertEqual(len(found), 2, found)
        for c in found:
            self.assertIn("assert_macro", c.triggers)

    def test_matcher_ignores_seeded_negative_control(self) -> None:
        # None of these compare a duration-suffixed value to a zero literal: a non-zero
        # comparison, a comparison between two non-zero-literal expressions, a duration-shaped
        # identifier used without any comparison, an unrelated "ms" substring that is not a
        # duration suffix (lowercase, mid-word), a non-zero literal whose text merely ends in
        # the digit 0 (`10.0`, `0.5`), a duration-named call with a real (non-empty) argument,
        # a duration-named getter/JSON read compared against a NON-zero literal, and a bare
        # camelCase `Us`/`Ns` token that is not underscore-delimited.
        seeded = "\n".join([
            "void f() {",
            "    if (some_stage_duration_ms > 5.0) { markRan(); }",
            "    if (stage_a_ms > stage_b_ms) { pickA(); }",
            "    logDuration(some_stage_duration_ms);",
            "    int items = countItems();",
            "    if (10.0 > some_stage_duration_ms) { markRan(); }",
            "    if (some_stage_duration_ms > 0.5) { markRan(); }",
            "    processDuration(some_stage_duration_ms);",
            "    if (getSeededProbeMilliseconds() > 5.0) { return; }",
            "    ASSERT_NEAR(1.0, getSeededNearProbeMs(), 1e-9);",
            "    if (someValueUs > 0) { markRan(); }",
            "}",
        ])
        found = scan_text(seeded, source="<seeded-negative>")
        self.assertEqual(found, [], found)

    def test_seeded_controls_do_not_leak_into_the_real_inventory(self) -> None:
        # The seeded snippets above are synthetic sources (source="<seeded-...>"), never written
        # under src/, platform/qt/ or tests/, so they must never appear as a live repo candidate
        # or a pinned inventory row -- this would only happen if a future edit accidentally wrote
        # the seed strings into a real tracked file.
        seeded_markers = (
            "some_stage_duration_ms", "seeded_probe_ms", "getSeededProbeMilliseconds",
            "seeded_default_ms", "getSeededNearProbeMs", "getSeededDoubleEqProbeMs",
        )
        for c in self.live_candidates:
            for marker in seeded_markers:
                self.assertNotIn(marker, c.anchor, c)
        for row in self.inventory:
            for marker in seeded_markers:
                self.assertNotIn(marker, row["anchor"], row)


if __name__ == "__main__":
    unittest.main()
