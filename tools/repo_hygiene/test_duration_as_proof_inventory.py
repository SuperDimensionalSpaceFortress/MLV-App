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

    # -- DURATION-AS-PROOF-GATE-HARDENING-2: each test is a mutant/repro the PREVIOUS scanner
    # -- mis-handled (verified against fork/master a4f73c11 before the fix) ------------------

    def test_raw_string_body_cannot_fake_a_site(self) -> None:
        # (a) The old stripper had no raw-string state, so the body of an embedded
        # shader/JSON/regex literal was read as code and a site-shaped line inside it was
        # flagged. Blanked bodies must never trigger -- single- and multi-line, with a custom
        # delimiter, and with the u8/L prefixes.
        for seeded in (
            'const char *s = R"(if (elapsed_ms > 0.0) { x(); })";',
            'auto s = R"(\nif (elapsed_ms > 0.0) {}\n)";',
            'auto s = R"xx(if (elapsed_ms > 0.0) {} )" still inside)xx";',
            'auto s = u8R"(ASSERT_EQ(0, elapsed_ms);)";',
            'auto s = LR"(0.0 < elapsed_ms)";',
        ):
            self.assertEqual(scan_text(seeded, source="<raw-fake>"), [], seeded)

    def test_raw_string_with_comment_or_quote_inside_cannot_hide_a_site(self) -> None:
        # (a) Only the quote-then-comment-opener shape (`R"xx(a)" /* )xx"`, `R"tag(a)" /*)tag"`)
        # was actually HIDDEN on fork/master (0 sites; now 1). The other shapes below already
        # flagged once on master (the string ended early but the code after it survived) and
        # are pinned as no-regression, so none of this is claimed as a master blind spot.
        for seeded in (
            'auto s = R"(//)"; if (elapsed_ms > 0.0) {}',
            'auto s = R"(/*)"; if (elapsed_ms > 0.0) {}',
            'auto s = R"(he said " hi)"; if (elapsed_ms > 0.0) {}',
            'auto s = R"xx(a)" /* )xx"; if (elapsed_ms > 0.0) {}',
            'auto s = R"(\n// not a comment\n)";\nif (elapsed_ms > 0.0) {}',
        ):
            found = scan_text(seeded, source="<raw-hide>")
            self.assertEqual(len(found), 1, seeded)
            self.assertIn("ident_compare", found[0].triggers)

    def test_raw_string_prefix_must_not_be_an_identifier_tail(self) -> None:
        # `FOOR"x"` is an identifier followed by a plain string, not a raw string: the code
        # after it must still be scanned.
        found = scan_text('f(FOOR"(", elapsed_ms > 0.0); g(")");', source="<not-raw>")
        self.assertEqual(len(found), 1, found)

    def test_raw_string_literal_data_still_keys_the_anchor(self) -> None:
        # The anchor view keeps raw-string bodies (only the TRIGGER view blanks them): editing
        # a literal's data on a flagged line must still change the anchor.
        a = scan_text('if (elapsed_ms > 0.0) { log(R"(one)"); }', source="<a>")
        b = scan_text('if (elapsed_ms > 0.0) { log(R"(two)"); }', source="<b>")
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertNotEqual(a[0].anchor, b[0].anchor)

    def test_parenthesized_operands_are_flagged(self) -> None:
        # (b) `(elapsed_ms) > 0.0` ended the left operand in `)`, which the operand regex
        # could not match, so a redundantly parenthesized compare slipped through.
        for seeded in (
            "if ((elapsed_ms) > 0.0) { run(); }",
            "if (0.0 < (elapsed_ms)) { run(); }",
            "if ((((x.elapsed_ms))) <= 0) { run(); }",
            "if (0 == ( stage . durationMs )) { run(); }",
            "if (static_cast<double>(elapsed_ms) > 0.0) { run(); }",
            "if (0.0 < static_cast<double>(elapsed_ms)) { run(); }",
            "if (0 < (double)elapsed_ms) { run(); }",
            "if ((getStageMilliseconds()) > 0.0) { run(); }",
            "EXPECT_GT((elapsed), 0);",
            "ASSERT_EQ((0), (stage.elapsed_ms));",
        ):
            found = scan_text(seeded, source="<paren>")
            self.assertEqual(len(found), 1, seeded)

    def test_parenthesized_non_operands_are_not_flagged(self) -> None:
        # The parens of a CALL WITH ARGUMENTS belong to the call: `foo(elapsed_ms) > 0` compares
        # foo's result, and a parenthesized sum is not a bare duration operand.
        seeded = "\n".join([
            "if (foo(elapsed_ms) > 0.0) { a(); }",
            "if (0.0 < foo(elapsed_ms)) { a(); }",
            "if ((elapsed_ms + 1.0) > 0.0) { a(); }",
            "if (0.0 < (elapsed_ms + base)) { a(); }",
            "if ((count) > 0) { a(); }",
        ])
        self.assertEqual(scan_text(seeded, source="<paren-neg>"), [])

    def test_multiline_macro_and_fabs_sites_are_flagged_and_keyed_by_all_lines(self) -> None:
        # (c) sol's finding: test_receipt_applier.cpp splits `ASSERT_TRUE( std::fabs(d)` /
        # `< 0.000001 );` over two physical lines, which a per-line matcher cannot see. The
        # site is the whole statement; it is keyed by its joined lines, reported at its first.
        seeded = "\n".join([
            "void f() {",
            "    ASSERT_TRUE( std::fabs(plan.expectedDurationSeconds)",
            "        < 0.000001 );",
            "    ASSERT_EQ(0.0,",
            "        stage.durationMs);",
            "    ASSERT_NEAR(std::fabs(",
            "        stage_ms), 0.0, 1e-9);",
            "    EXPECT_EQ( 0, elapsedMs ) << foo(1);",
            "}",
        ])
        found = {c.lines: c for c in scan_text(seeded, source="<multiline>")}
        self.assertEqual(sorted(found), [(2,), (4,), (6,), (8,)])
        self.assertIn("fabs_zero", found[(2,)].triggers)
        self.assertIn("assert_macro", found[(4,)].triggers)
        self.assertIn("assert_macro", found[(6,)].triggers)
        self.assertIn("assert_macro", found[(8,)].triggers)  # a trailing `<<` stream must not hide it
        self.assertEqual(
            found[(2,)].anchor,
            normalize_anchor("ASSERT_TRUE( std::fabs(plan.expectedDurationSeconds) < 0.000001 );"),
        )

    def test_multiline_site_anchor_covers_the_continuation_line(self) -> None:
        a = scan_text("ASSERT_EQ(0.0,\n    stage.durationMs);", source="<a>")
        reflowed = scan_text("ASSERT_EQ(0.0,\n\t\t  stage . durationMs );", source="<b>")
        edited = scan_text("ASSERT_EQ(0.0,\n    other.durationMs);", source="<c>")
        self.assertEqual(a[0].anchor, reflowed[0].anchor)  # indentation/alignment is not identity
        self.assertNotEqual(a[0].anchor, edited[0].anchor)  # but the continuation's text is

    def test_fabs_of_a_value_comparison_is_not_a_zero_assert(self) -> None:
        # fabs(d - 10.01) is a value comparison and a >1e-3 literal is not an epsilon; neither
        # is `fabs(non_duration) < eps`. A bare-duration fabs vs an epsilon NAME is a zero assert.
        negatives = "\n".join([
            "ASSERT_TRUE( std::fabs(metadata.durationSeconds - 10.01) < 0.000001 );",
            "ASSERT_TRUE( std::fabs(metadata.durationSeconds) < 5.0 );",
            "ASSERT_TRUE( std::fabs(metadata.offset) < 0.000001 );",
        ])
        self.assertEqual(scan_text(negatives, source="<fabs-neg>"), [])
        positive = scan_text("ASSERT_TRUE( fabs(x.elapsed_ms) < kEpsilon );", source="<fabs-pos>")
        self.assertEqual(len(positive), 1)
        self.assertIn("fabs_zero", positive[0].triggers)
        mirrored = scan_text("ASSERT_TRUE( 1e-9 > std::fabs(elapsedMs) );", source="<fabs-mirror>")
        self.assertEqual(len(mirrored), 1)

    def test_comment_edits_never_rekey_a_site(self) -> None:
        # (d) The anchor used the ORIGINAL line, so editing a trailing comment on an unchanged
        # code line changed the key and forced a spurious reclassification. Comments (line and
        # block) are now stripped from the anchor view; a CODE edit still re-keys.
        base = scan_text("if (elapsed_ms > 0.0) { run(); }", source="<base>")
        self.assertEqual(len(base), 1)
        for variant in (
            "if (elapsed_ms > 0.0) { run(); } // a note",
            "if (elapsed_ms > 0.0) { run(); } // a different note",
            "if (elapsed_ms > 0.0) { run(); } /* block */",
            "if (elapsed_ms > 0.0) /* mid */ { run(); }",
        ):
            found = scan_text(variant, source="<variant>")
            self.assertEqual(len(found), 1, variant)
            self.assertEqual(found[0].anchor, base[0].anchor, variant)
        edited = scan_text("if (elapsed_ms > 0.0) { runOther(); } // a note", source="<edited>")
        self.assertNotEqual(edited[0].anchor, base[0].anchor)
        # A `//` inside a string literal is data, not a comment: still part of the anchor.
        with_url = scan_text('if (elapsed_ms > 0.0) { log("http://x"); }', source="<url>")
        self.assertIn('"http://x"', with_url[0].anchor)

    def test_trailing_At_name_alone_is_never_a_position(self) -> None:
        # (e) round-2 (sol's blocker on 207bcf77): a trailing `At`/`_at` word used to exempt
        # a name unconditionally, which hid REAL measured durations. Each input below flags
        # exactly once on fork/master (a4f73c11 and d3cf6850) and must keep flagging: a name
        # is not evidence of a position (and, since r3n, no same-file proof exempts it either).
        for seeded in (
            # sol's QElapsedTimer-backed repro, one line and split over lines
            "const double elapsedAt = timer.nsecsElapsed() / 1000000.0; if (elapsedAt > 0.0) markRan();",
            "QElapsedTimer timer;\nconst double elapsedAt = timer.nsecsElapsed() / 1e6;\nif (elapsedAt > 0.0) { a(); }",
            # the macro spelling, with and without a visible timer source
            "ASSERT_GT(duration_ms_at, 0);",
            "QElapsedTimer t; t.start(); const double duration_ms_at = t.nsecsElapsed() / 1e6;\nASSERT_GT(duration_ms_at, 0);",
            "EXPECT_EQ(0, durationAt);",
            # a duration-returning getter, and a name never assigned in this file (member, param, extern)
            "if (getElapsedAt() > 0) { markRan(); }",
            "if (getDurationAt() > 0) { a(); }",
            "if (playedMsAt >= 0) { a(); }",
            "void f(double elapsedAt) { if (elapsedAt > 0) { a(); } }",
            # names that only END in a unit word after `At`, or carry `at` mid-name, were never exempt
            "if (elapsedAtStop > 0) { a(); }",
            "if (durationAtEnd > 0) { a(); }",
            "if (durationAtStart > 0) { a(); }",
            "if (atMs > 0) { a(); }",
            "if (elapsed_at_ms > 0) { a(); }",
            "if (at_elapsed > 0) { a(); }",
        ):
            found = scan_text(seeded, source="<at-duration>")
            self.assertEqual(len(found), 1, seeded)
        # the near-zero spelling is covered by the new fabs matcher the same way
        fabs = scan_text("ASSERT_TRUE(std::fabs(elapsedAt) < 1e-6);", source="<at-fabs>")
        self.assertEqual(len(fabs), 1)
        self.assertIn("fabs_zero", fabs[0].triggers)

    def test_no_name_or_position_exemption_for_At_names(self) -> None:
        # (e) r3n, per hub ruling: item (e) is NARROWED to master's behaviour. The r1/r2
        # position exemption (same-file textual proof that an `...At` name holds an indexOf/find
        # result) was escaped in both rounds -- through a macro, an alias, std::tie, a default
        # capture, a same-name declaration in another function, a reference-output helper, a
        # bare argument and `this->`. Every input below flags exactly once on fork/master and
        # must keep flagging: a trailing At/_at name is never exempt, whatever the file shows.
        # (The escape-proof design is a separate card.) This REPLACES the r2 test
        # `test_position_names_ending_in_At_are_exempt_only_with_same_file_proof`, which asserted
        # the exemption itself.
        search = "int elapsedAt = s.indexOf(x);\n"
        for seeded in (
            # the r2 "proven position" fixtures: flagged, same as master
            "const int durationAt = s.indexOf(x);\nif (durationAt > 0) { a(); }",
            "const int duration_at = s.find(x);\nASSERT_GT(duration_at, 0);",
            "int playedMsAt = smokeBody.indexOf(\n    QStringLiteral(\"const qint64 playedMs = playbackClock.elapsed();\"), loopAt);\n"
            "ASSERT_TRUE(playedMsAt >= 0);",
            "const auto preambleMsAt = static_cast<int>(text.find(\"x\"));\nif (0 < preambleMsAt) { a(); }",
            "int stage_ms_at = s.indexOf(a);\nstage_ms_at = s.indexOf(b, stage_ms_at);\nif (stage_ms_at > 0) { a(); }",
            "int elapsedSecondsAt = s.indexOf(x);\nASSERT_TRUE(0 < elapsedSecondsAt);",
            # sol r2 table (9 rows): search-valued name, then a real measured duration
            "void f() { " + search + "STORE(elapsedAt, timer.elapsed()); if (elapsedAt > 0) markRan(); }",
            "void f() { " + search + "auto& sample = elapsedAt; sample = timer.elapsed(); if (elapsedAt > 0) markRan(); }",
            "void f() { " + search + "std::tie(elapsedAt, count) = std::make_tuple(timer.elapsed(), 1); if (elapsedAt > 0) markRan(); }",
            "void f() { " + search + "auto g = [&elapsedAt, &timer]() { elapsedAt = timer.elapsed(); }; g(); if (elapsedAt > 0) markRan(); }",
            "void f() { " + search + "auto g = [&]() { writeElapsed(elapsedAt); }; g(); if (elapsedAt > 0) markRan(); }",
            "void verifyText(const QString& s) { const int elapsedAt = s.indexOf(\"elapsed\"); }\n"
            "void measure() { QElapsedTimer timer; timer.start(); runStage(); const qint64 elapsedAt{timer.elapsed()}; if (elapsedAt > 0) markRan(); }",
            "void f() { int elapsedAt = ready ? s.find(x) : timer.elapsed(); if (elapsedAt > 0) markRan(); }",
            "void verifyText(const QString& s) { int elapsedAt = s.indexOf(x); }\n"
            "void measure() { auto [elapsedAt, n] = measure2(); if (elapsedAt > 0) markRan(); }",
            "void f() { " + search + "fillElapsed(elapsedAt); if (elapsedAt > 0) markRan(); }",
            # fable r2: bare argument, reference binding, this-> write
            "void f() { int durationAt = s.indexOf(x); consume(durationAt); if (durationAt > 0) markRan(); }",
            "void f() { int durationAt = s.indexOf(x); int& d = durationAt; d = timer.elapsed(); if (durationAt > 0) markRan(); }",
            "void T::f() { int durationAt = s.indexOf(x); this->durationAt = timer.elapsed(); if (durationAt > 0) markRan(); }",
            "int durationAt = s.indexOf(x);\nmeasure(durationAt);\nif (durationAt > 0) { a(); }",
            "int durationAt = s.indexOf(x);\nstd::tie(durationAt, ok) = probe();\nif (durationAt > 0) { a(); }",
            # the remaining r2 spoil shapes
            "int durationAt = s.indexOf(x);\nin >> durationAt;\nif (durationAt > 0) { a(); }",
            "void g(double durationAt) {}\nint durationAt = s.indexOf(x);\nif (durationAt > 0) { a(); }",
            "int durationAt = s.indexOf(x);\nif (durationAt() > 0) { a(); }",
            "int durationAt = s.indexOf(x);\nif (probe->durationAt > 0) { a(); }",
            "int durationAt = s.indexOf(x);\nif (Probe::durationAt > 0) { a(); }",
            # earlier spoiled shapes: still flagged, now for the plain reason that nothing exempts
            "const int durationAt = timer.elapsed();\nif (durationAt > 0) { a(); }",
            "int durationAt = s.indexOf(x);\ndurationAt += timer.elapsed();\nif (durationAt > 0) { a(); }",
            "int durationAt = s.indexOf(x);\nfill(&durationAt);\nif (durationAt > 0) { a(); }",
            "int durationAt = s.indexOf(x);\nif (probe.durationAt > 0) { a(); }",
            "int durationAt = s.indexOf(x);\nif (getDurationAt() > 0) { a(); }",
        ):
            found = scan_text(seeded, source="<at-no-exemption>")
            self.assertEqual(len(found), 1, seeded)
        # a name proven by one function and timer-fed in another is flagged at BOTH sites
        mixed = (
            "void a() { int durationAt = s.indexOf(x); if (durationAt > 0) { r(); } }\n"
            "void b() { double durationAt = t.nsecsElapsed() / 1e6; if (durationAt > 0) { r(); } }"
        )
        self.assertEqual(len(scan_text(mixed, source="<mixed>")), 2)
        # fable r2: a this-> write in one member and a local indexOf in another flags both reads
        member = (
            "void P::tick() { this->durationAt = timer.elapsed(); }\n"
            "bool P::ran() const { return durationAt > 0; }\n"
            "void P::parse() { int durationAt = s.indexOf(x); if (durationAt >= 0) {} }"
        )
        self.assertEqual(len(scan_text(member, source="<member>")), 2)

    def test_raw_string_body_with_unicode_line_breaks_keeps_views_aligned(self) -> None:
        # fable's note: blanking a raw body kept only \r/\n, but the anchor view (body verbatim)
        # is split by splitlines(), which also breaks on \v \f \x1c-\x1e \x85    ; the
        # two views then disagreed on line numbers. Both must agree, so the site after the
        # literal is still found, keyed to its own line, with its own anchor.
        for brk in ("\v", "\f", "\x1c", "\x85", " ", " ", "\r\n"):
            seeded = f'auto s = R"({brk}x{brk}y)";\nif (elapsed_ms > 0.0) {{ run(); }}'
            found = scan_text(seeded, source="<raw-brk>")
            self.assertEqual(len(found), 1, repr(brk))
            self.assertIn("elapsed_ms", found[0].anchor, repr(brk))
            self.assertIn("ident_compare", found[0].triggers, repr(brk))

    def test_hardening1_items_already_closed_on_master_stay_closed(self) -> None:
        # HARDENING-1 (fable on #173) was verified already fixed by 4B; pinned here so it
        # cannot regress unnoticed: a bare `ms` identifier, a copied anchor surfacing as an
        # extra occurrence (the (path, anchor, count) key changes), ASSERT_NEAR/DOUBLE_EQ.
        bare = scan_text("if (ms > 0.0) { a(); }", source="<bare-ms>")
        self.assertEqual(len(bare), 1)
        dup = scan_text("if (elapsed_ms > 0.0) { a(); }\nint x;\nif (elapsed_ms > 0.0) { a(); }", source="<dup>")
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0].lines, (1, 3))
        self.assertEqual(len(scan_text("ASSERT_NEAR(0.0, elapsed_ms, 1e-9);", source="<near>")), 1)
        self.assertEqual(len(scan_text("EXPECT_DOUBLE_EQ(0.0, elapsed_ms);", source="<deq>")), 1)

    def test_gui_latency_golden_rows_are_structural_not_open_debt(self) -> None:
        # (f) sol's "five conservative GUI-latency debt rows" are the test_clip_golden.cpp
        # average_latency_ms / play_to_first_frame_ms (x3) / latency_ms assertions. 3b already
        # turned them into `>= 0.0` non-negativity checks whose run-proof is carried by
        # ASSERT_EQ(2, frames.size()) / play_to_first_frame_measured; verified against the
        # source at HARDENING-2 time. Pin: they are five live sites, none of them open debt.
        path = "tests/console/test_clip_golden.cpp"
        anchors = {
            'ASSERT_TRUE ( metadata . value ( QStringLiteral ( "average_latency_ms" ) ) . toDouble ( ) >= 0.0 ) ;': 1,
            'ASSERT_TRUE ( metadata . value ( QStringLiteral ( "play_to_first_frame_ms" ) ) . toDouble ( ) >= 0.0 ) ;': 3,
            'ASSERT_TRUE ( sample . value ( QStringLiteral ( "latency_ms" ) ) . toDouble ( ) >= 0.0 ) ;': 1,
        }
        for anchor, count in anchors.items():
            row = self.inventory_by_key.get((path, anchor, count))
            self.assertIsNotNone(row, (anchor, count))
            self.assertEqual(row["class"], "not_run_proof_structural", row)
            self.assertNotIn("status", row)

    def test_multiline_fabs_zero_asserts_in_receipt_applier_are_inventoried(self) -> None:
        # (c) the live multi-line sites sol named; each is a plan-derived (not measured) value.
        path = "tests/console/test_receipt_applier.cpp"
        fabs_sites = [
            c for c in self.live_candidates if c.path == path and "fabs_zero" in c.triggers
        ]
        self.assertGreaterEqual(len(fabs_sites), 3, fabs_sites)
        for c in fabs_sites:
            row = self.inventory_by_key.get((c.path, c.anchor, len(c.lines)))
            self.assertIsNotNone(row, c)
            self.assertEqual(row["class"], "not_run_proof_structural", row)

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
