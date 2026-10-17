#!/usr/bin/env python3
"""SAFETY-LINT-MASTER-PARITY-DIFF-1: tests for tools/repo_hygiene/safety_lint_parity.py (the master-vs-head safety-lint runner).

Cost: the hook-only tests run in process (zero pwsh). Exactly ONE test (the base == head full-corpus pin) runs the playback lint, and
it spawns two pwsh processes (master's lint, the head's; `pwsh_spawns` is asserted). Base is "HEAD" throughout, so the tests do not
depend on a fork/master ref being fetched; the real-tree run against fork/master is the CLI in the PR body.
HeadVersusBaselineGateTests is the gate itself; it runs (two more pwsh) only when a lint differs from the merge-base's, and skips otherwise,
so a PR that touches neither lint pays nothing for it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import safety_lint_parity as slp  # noqa: E402

REPO_ROOT = HERE.parents[1]
PWSH = shutil.which("pwsh")

# Where the gate takes the baseline from: the first ref that resolves, as LintDifferentialPinTests does.
BASELINE_REFS = ("fork/master", "origin/master", "master")
BOARD, VENUE = "C:/review/board", "C:/review/worktree"
# A plain delete of a protected run dir: master's NA-2 refuses it, and a head that allows everything loosens it.
DENIED = {"id": "t-protected-rd", "lint": "hook", "tool_name": "PowerShell", "board_root": BOARD, "cwd": VENUE,
          "command": "cmd /c rd /s /q " + BOARD + "/.claude-state/fleet-runs/lane-A-r1", "why": "synthetic: a delete of a protected run dir"}

# r5 (CoS I2): an allow-list entry is bound to the row's text hash; for a hook row that text is its command.
DENIED_HASH = slp.short_hash(DENIED["command"])

ALWAYS_ALLOW_HOOK ="def decide(payload, project_dir):\n    return 0, ''\n\n\ndef main():\n    return 0\n"
ALWAYS_THROW_HOOK = "def decide(payload, project_dir):\n    raise RuntimeError('synthetic rule crash')\n\n\ndef main():\n    return 0\n"

# Pinned floors. A shrinking corpus is a red test; raise these when rows are added, never lower them.
MIN_STATIC_ROWS = 410      # the JSON corpus: 205 kept static rows + the 180-row #455 sweep when this card landed (385), + the 25 r5 CoS #465 B5/B6 rows (410)
MIN_HOOK_ROWS = 247        # hook share of the JSON corpus: 67 static #455 rows + the 180-row sweep (247 when this card landed)
MIN_PLAYBACK_ROWS = 163    # playback share: the #465 RULE4 rows master refuses + the authored spaced-clip rows (138 when this card landed), + 25 r5 B5/B6 rows (163)
MIN_DERIVED_HOOK_ROWS = 200  # DENY rows of test_mlv_never_authorized.CASES (203 when this card landed)

# r4 EXACT pins (sol r1 HARDENING: the floor of 200 let three existing DENY rows vanish with the count pin still green, and the
# derived playback rows had no pin at all). The derived rows are the whole of the head's own deny suites, so any change in their
# number is a corpus change that a reviewer should see: adding a deny row to either suite means raising the matching number here in
# the same commit; DELETING one fails this test. The floors above stay as the coarse lower bound.
EXACT_DERIVED_HOOK_ROWS = 203      # DENY rows of test_mlv_never_authorized.CASES
EXACT_DERIVED_PLAYBACK_ROWS = 211  # must-refuse sentences of test_hub_vm_no_local_playback.py, deduplicated by (name, text)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class ParityRunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="parity-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.allow = _write(self.tmp / "allow.json", "[]")

    def corpus(self, *rows, name="corpus.json") -> Path:
        return _write(self.tmp / name, json.dumps({"schema": slp.CORPUS_SCHEMA, "rows": list(rows)}))

    def head_hook(self, source: str) -> Path:
        return _write(self.tmp / "head_hook.py", source)

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = slp.main(["--base", "HEAD", "--lint", "hook", "--no-derived", *args])
        return rc, out.getvalue()


class RedGreenTests(ParityRunnerCase):
    def test_a_head_that_allows_a_master_denied_row_exits_1_and_names_the_row(self):
        rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow), "--head-hook", str(self.head_hook(ALWAYS_ALLOW_HOOK)))
        self.assertEqual(rc, slp.EXIT_LOOSENED, out)
        self.assertIn("LOOSENED t-protected-rd", out)
        self.assertIn("master=DENY head=ALLOW", out)
        self.assertIn("cmd /c rd /s /q", out)  # the line carries the command

    def test_the_real_head_equal_to_master_exits_0(self):
        rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow))
        self.assertEqual(rc, slp.EXIT_OK, out)
        self.assertNotIn("LOOSENED t-protected-rd", out)
        self.assertIn("rows=1 same=1 tightened=0 loosened=0", out)

    def test_the_control_row_really_is_denied_by_the_baseline(self):
        report = slp.run("HEAD", ("hook",), self.corpus(DENIED), self.allow, derived=False)
        self.assertEqual(report["results"][0]["master"], slp.DENY, report["results"][0])

    def test_a_head_that_denies_what_master_allows_is_tightened_not_loosened(self):
        allowed = dict(DENIED, id="t-harmless-ls", command="ls -la")
        deny_all = "def decide(payload, project_dir):\n    return 2, 'synthetic deny'\n\n\ndef main():\n    return 2\n"
        rc, out = self.cli("--corpus", str(self.corpus(allowed)), "--allow", str(self.allow), "--head-hook", str(self.head_hook(deny_all)))
        self.assertEqual(rc, slp.EXIT_OK, out)
        self.assertIn("tightened=1 loosened=0", out)


class UndecidedTests(ParityRunnerCase):
    def test_a_lint_that_throws_exits_2_not_0(self):
        rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow), "--head-hook", str(self.head_hook(ALWAYS_THROW_HOOK)))
        self.assertEqual(rc, slp.EXIT_UNDECIDED, out)
        self.assertIn("ERROR t-protected-rd", out)
        self.assertIn("synthetic rule crash", out)

    def test_an_unparseable_row_exits_2(self):
        for bad in ({"id": "t-no-command", "lint": "hook", "why": "x"}, {"id": "t-bad-lint", "lint": "nope", "command": "x", "why": "x"},
                    {"lint": "hook", "command": "x", "why": "no id"}):
            with self.subTest(row=bad):
                rc, out = self.cli("--corpus", str(self.corpus(bad)), "--allow", str(self.allow))
                self.assertEqual(rc, slp.EXIT_UNDECIDED, out)
                self.assertIn("CANNOT-DECIDE", out)

    def test_a_corpus_that_is_not_json_exits_2(self):
        rc, out = self.cli("--corpus", str(_write(self.tmp / "bad.json", "{not json")), "--allow", str(self.allow))
        self.assertEqual(rc, slp.EXIT_UNDECIDED, out)

    def test_a_base_ref_git_cannot_resolve_exits_2(self):
        rc, out = self.cli("--base", "refs/heads/no-such-ref-for-parity", "--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow))
        self.assertEqual(rc, slp.EXIT_UNDECIDED, out)

    def test_a_loosened_row_outranks_an_error_in_the_exit_code(self):
        errored = dict(DENIED, id="t-errors", command="ls")
        mixed = "def decide(payload, project_dir):\n    if 'ls' == payload['tool_input']['command']:\n        raise RuntimeError('boom')\n    return 0, ''\n\n\ndef main():\n    return 0\n"
        rc, out = self.cli("--corpus", str(self.corpus(DENIED, errored)), "--allow", str(self.allow), "--head-hook", str(self.head_hook(mixed)))
        self.assertEqual(rc, slp.EXIT_LOOSENED, out)
        self.assertIn("ERROR t-errors", out)


class AllowListTests(ParityRunnerCase):
    def allow_with(self, *entries) -> Path:
        return _write(self.tmp / "allow-with.json", json.dumps(list(entries)))

    def test_an_allow_listed_row_is_reported_but_does_not_fail(self):
        entry = {"id": "t-protected-rd", "reason": "synthetic intended loosening", "pr": 999, "hash": DENIED_HASH}
        rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow_with(entry)), "--head-hook", str(self.head_hook(ALWAYS_ALLOW_HOOK)))
        self.assertEqual(rc, slp.EXIT_OK, out)
        self.assertIn("ALLOWED-LOOSENED t-protected-rd", out)
        self.assertIn("pr=999", out)
        self.assertIn("synthetic intended loosening", out)
        self.assertIn("unlisted=0, allowed=1", out)

    def test_an_allow_entry_for_another_row_does_not_excuse_this_one(self):
        entry = {"id": "t-some-other-row", "reason": "unrelated", "pr": 1, "hash": DENIED_HASH}
        rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow_with(entry)), "--head-hook", str(self.head_hook(ALWAYS_ALLOW_HOOK)))
        self.assertEqual(rc, slp.EXIT_LOOSENED, out)
        self.assertIn("note: allow-list entry t-some-other-row matches no LOOSENED row", out)

    def test_an_allow_entry_without_a_reason_or_pr_exits_2(self):
        h = DENIED_HASH  # r5: each entry carries a valid hash, so it is the missing reason / pr that makes it malformed
        for entry in ({"id": "t-protected-rd", "pr": 5, "hash": h}, {"id": "t-protected-rd", "reason": "r", "hash": h},
                      {"id": "t-protected-rd", "reason": "r", "pr": 0, "hash": h}):
            with self.subTest(entry=entry):
                rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow_with(entry)))
                self.assertEqual(rc, slp.EXIT_UNDECIDED, out)

    def test_an_allow_entry_without_a_valid_hash_exits_2(self):
        # r5 (CoS I2): an entry with no hash would be keyed by id alone, which is the hole this closes
        base = {"id": "t-protected-rd", "reason": "r", "pr": 5}
        for label, entry in {"missing": base, "empty": dict(base, hash=""), "not-hex": dict(base, hash="not-a-hash!"),
                             "wrong-type": dict(base, hash=12345), "too-short": dict(base, hash="abc")}.items():
            with self.subTest(hash=label):
                rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow_with(entry)))
                self.assertEqual(rc, slp.EXIT_UNDECIDED, out)
                self.assertIn("hash", out)

    def test_I2_the_same_id_with_edited_text_is_not_excused_by_the_old_entry(self):
        entry = {"id": "t-protected-rd", "reason": "synthetic intended loosening", "pr": 999, "hash": DENIED_HASH}
        edited = dict(DENIED, command=DENIED["command"] + "-r2")  # same id, the command text changed
        self.assertNotEqual(slp.short_hash(edited["command"]), DENIED_HASH)
        rc, out = self.cli("--corpus", str(self.corpus(edited)), "--allow", str(self.allow_with(entry)), "--head-hook", str(self.head_hook(ALWAYS_ALLOW_HOOK)))
        self.assertEqual(rc, slp.EXIT_LOOSENED, out)
        self.assertIn("LOOSENED t-protected-rd", out)
        self.assertIn(f"hash={slp.short_hash(edited['command'])}", out)
        self.assertIn("treated as unlisted", out)
        self.assertNotIn("ALLOWED-LOOSENED", out)
        self.assertIn("unlisted=1, allowed=0", out)

    def test_I2_the_unedited_row_is_still_excused_and_its_hash_is_printed_for_the_entry_author(self):
        entry = {"id": "t-protected-rd", "reason": "synthetic intended loosening", "pr": 999, "hash": DENIED_HASH}
        rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow_with(entry)), "--head-hook", str(self.head_hook(ALWAYS_ALLOW_HOOK)))
        self.assertEqual(rc, slp.EXIT_OK, out)
        self.assertIn("unlisted=0, allowed=1", out)
        rc, out = self.cli("--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow), "--head-hook", str(self.head_hook(ALWAYS_ALLOW_HOOK)))
        self.assertIn(f"LOOSENED t-protected-rd [hook] hash={DENIED_HASH}", out)

    def test_the_tracked_allow_list_starts_empty(self):
        self.assertEqual(slp.load_allow(slp.DEFAULT_ALLOW), {}, "an entry belongs here only with a reason and a PR number, added in review")


class CorpusPinTests(unittest.TestCase):
    def test_the_tracked_corpus_cannot_silently_shrink(self):
        rows = slp.load_corpus(slp.DEFAULT_CORPUS)
        hook = [r for r in rows if r["lint"] == "hook"]
        playback = [r for r in rows if r["lint"] == "playback"]
        self.assertGreaterEqual(len(rows), MIN_STATIC_ROWS)
        self.assertGreaterEqual(len(hook), MIN_HOOK_ROWS)
        self.assertGreaterEqual(len(playback), MIN_PLAYBACK_ROWS)

    def test_an_fstring_row_and_a_spaced_clip_row_are_present_by_id(self):
        by_id = {r["id"]: r for r in slp.load_corpus(slp.DEFAULT_CORPUS)}
        self.assertIn("b455-fstring-01", by_id)
        self.assertEqual(by_id["b455-fstring-01"]["lint"], "hook")
        self.assertIn("#455", by_id["b455-fstring-01"]["why"])
        self.assertIn("authored-spaced-clip-01", by_id)
        spaced = by_id["authored-spaced-clip-01"]
        self.assertEqual(spaced["lint"], "playback")
        self.assertRegex(spaced["text"], r'--clip "[^"]* [^"]*"', "a quoted clip path that contains a space")

    def test_every_row_names_its_source(self):
        for row in slp.load_corpus(slp.DEFAULT_CORPUS):
            self.assertTrue(row["why"].strip(), row["id"])

    def test_the_sweep_generator_expands_to_180_rows(self):
        sweep = [r for r in slp.load_corpus(slp.DEFAULT_CORPUS) if r["id"].startswith("b455-sweep-")]
        self.assertEqual(len(sweep), 180)  # 4 verbs x 9 tails x 5 wrappers, all distinct

    def test_the_derived_hook_rows_cannot_silently_shrink(self):
        self.assertGreaterEqual(len(slp.derived_hook_cases(REPO_ROOT)), MIN_DERIVED_HOOK_ROWS)


COS_465_PROBE_IDS = [f"b465-b5-{n:02d}" for n in range(1, 18)] + [f"b465-b6-{n:02d}" for n in range(1, 9)]


class Cos465ProbeRowsTests(ParityRunnerCase):
    """r5 (CoS I1): the #465 B5 (comment 6230178238) and B6 (comment 6230713095) probe rows that master refuses are in the corpus."""

    def test_the_25_probe_rows_are_present_playback_rows_that_cite_their_comment(self):
        by_id = {r["id"]: r for r in slp.load_corpus(slp.DEFAULT_CORPUS)}
        for row_id in COS_465_PROBE_IDS:
            with self.subTest(row=row_id):
                self.assertIn(row_id, by_id)
                self.assertEqual(by_id[row_id]["lint"], "playback")
                self.assertIn("6230178238" if "-b5-" in row_id else "6230713095", by_id[row_id]["why"])
        self.assertEqual(len(COS_465_PROBE_IDS), 25)

    @unittest.skipUnless(PWSH, "pwsh is required to run the playback probe")
    def test_master_refuses_every_probe_row(self):
        sha = _baseline_sha()
        if sha is None:
            self.skipTest("no baseline ref resolves (fork/master, origin/master, master)")
        rows = [{k: v for k, v in r.items() if k != "source"} for r in slp.load_corpus(slp.DEFAULT_CORPUS) if r["id"] in COS_465_PROBE_IDS]
        self.assertEqual(len(rows), len(COS_465_PROBE_IDS))
        report = slp.run(sha, ("playback",), self.corpus(*rows), self.allow, derived=False)
        self.assertEqual([r["id"] for r in report["results"] if r.get("master") != slp.DENY], [], "master's lint must DENY every probe row")


def derived_pin_failures(root: Path) -> list[str]:
    """Every way the head's derived row counts differ from the exact pins (empty list == pinned). Takes a root so a scratch copy of
    the two deny suites, with one row deleted, can be judged by the very check the test runs (see the r4 RED proof in the PR body)."""
    failures = []
    hook = len(slp.derived_hook_cases(root))
    playback = len(slp.derived_playback_rows(root)[0])
    if hook != EXACT_DERIVED_HOOK_ROWS:
        failures.append(f"derived hook DENY rows: {hook} != pinned {EXACT_DERIVED_HOOK_ROWS}")
    if playback != EXACT_DERIVED_PLAYBACK_ROWS:
        failures.append(f"derived playback must-refuse rows: {playback} != pinned {EXACT_DERIVED_PLAYBACK_ROWS}")
    return failures


class DerivedCorpusExactPinTests(unittest.TestCase):
    """PARITY-HARDEN-01 (sol r1 HARDENING): a deleted derived deny row, hook or playback, fails a pin; the floor is not the pin."""

    def test_the_derived_rows_match_the_exact_pins(self):
        self.assertEqual(derived_pin_failures(REPO_ROOT), [], "raise the EXACT_* pin in the same commit that ADDS a deny row; deleting one is the failure")

    def _trimmed_loader(self, drop_hook: bool, drop_playback: bool):
        real = slp.load_module

        def load(path, name):
            module = real(path, name)
            if drop_hook and hasattr(module, "CASES"):
                gone = next(c for c in module.CASES if c.get("expect") == "DENY")
                module.CASES = [c for c in module.CASES if c is not gone]
            if drop_playback and hasattr(module, "PromptLintTests"):
                lints = module.PromptLintTests
                victim = sorted(n for n in dir(lints) if n.startswith("CLAUSE_MUST_REFUSE"))[0]
                setattr(lints, victim, tuple(getattr(lints, victim))[1:])
            return module
        return load

    def test_one_deleted_hook_deny_row_fails_the_pin_that_the_floor_would_have_passed(self):
        with mock.patch.object(slp, "load_module", self._trimmed_loader(True, False)):
            self.assertEqual(len(slp.derived_hook_cases(REPO_ROOT)), EXACT_DERIVED_HOOK_ROWS - 1)
            self.assertGreaterEqual(EXACT_DERIVED_HOOK_ROWS - 1, MIN_DERIVED_HOOK_ROWS, "the old floor is blind to this deletion")
            failures = derived_pin_failures(REPO_ROOT)
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("derived hook DENY rows", failures[0])

    def test_one_deleted_playback_must_refuse_row_fails_the_pin(self):
        with mock.patch.object(slp, "load_module", self._trimmed_loader(False, True)):
            self.assertEqual(len(slp.derived_playback_rows(REPO_ROOT)[0]), EXACT_DERIVED_PLAYBACK_ROWS - 1)
            failures = derived_pin_failures(REPO_ROOT)
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("derived playback must-refuse rows", failures[0])


BROKEN_BASELINES = {
    # master's playback lint defines no finding function (the sol r1 repro: stderr, exit 0, [0])
    "no-finding-function": "# a baseline that defines nothing\n",
    # the baseline dot-sources a file that is not there
    "unreadable-dot-source": ". 'C:/no/such/dir/lane-local-playback-lint-baseline.ps1'\n",
    # a finding function that reports a NON-terminating error and returns no findings
    "nonterminating-write-error": "function Get-LocalPlaybackPromptFindings { param($Text) Write-Error 'synthetic evaluation error'; return @() }\n",
}


class PlaybackProbeErrorTests(ParityRunnerCase):
    """PARITY-ERROR-01 (sol r1 BLOCKER): a playback probe that hit an evaluation error is class `error` (exit 2), never an ALLOW verdict.

    Before r4 the adopted probe left $ErrorActionPreference at Continue and its collector accepted a right-length count list when
    pwsh exited 0, so an unreadable baseline read master=ALLOW, which against a head that DENIES is `tightened` and selects exit 0."""

    def playback_row(self):
        row = next(r for r in slp.load_corpus(slp.DEFAULT_CORPUS) if r["lint"] == "playback")
        return {k: v for k, v in row.items() if k != "source"} | {"id": "t-playback-deny"}

    def cli_with_baseline(self, baseline_source: str):
        real_git = slp.git

        def fake_git(root, *args):
            if len(args) >= 2 and args[0] == "show" and args[1].endswith(":" + slp.LINT_REL):
                return baseline_source
            return real_git(root, *args)

        with mock.patch.object(slp, "git", fake_git):
            return self.cli("--lint", "playback", "--corpus", str(self.corpus(self.playback_row())), "--allow", str(self.allow))

    @unittest.skipUnless(PWSH, "pwsh is required to run the playback probe")
    def test_PARITY_ERROR_01_an_unreadable_or_broken_playback_baseline_exits_2_never_allow(self):
        for label, source in BROKEN_BASELINES.items():
            with self.subTest(baseline=label):
                rc, out = self.cli_with_baseline(source)
                self.assertEqual(rc, slp.EXIT_UNDECIDED, out)
                self.assertIn("ERROR t-playback-deny", out)
                self.assertIn("error=1", out)
                self.assertNotIn("tightened=1", out, "a row that cannot be run must not read as an ALLOW verdict")

    @unittest.skipUnless(PWSH, "pwsh is required to run the playback probe")
    def test_PARITY_ERROR_01_a_missing_baseline_file_is_an_error_row_not_an_allow(self):
        pb = slp.derived_playback_rows(REPO_ROOT)[1]
        rows = [dict(self.playback_row(), source="corpus")]
        stats = {"pwsh_spawns": 0}
        results = slp.run_playback(pb, self.tmp / "no-such-baseline.ps1", REPO_ROOT / slp.LINT_REL, rows, self.tmp, stats)
        self.assertIn("error", results[0], results[0])
        self.assertNotIn("master", results[0])
        self.assertEqual(slp.classify(results[0], {}), "error")

    @unittest.skipUnless(PWSH, "pwsh is required to run the playback probe")
    def test_PARITY_ERROR_01_a_broken_head_lint_is_an_error_row_too(self):
        pb = slp.derived_playback_rows(REPO_ROOT)[1]
        broken = _write(self.tmp / "broken-head.ps1", BROKEN_BASELINES["nonterminating-write-error"])
        rows = [dict(self.playback_row(), source="corpus")]
        results = slp.run_playback(pb, REPO_ROOT / slp.LINT_REL, broken, rows, self.tmp, {"pwsh_spawns": 0})
        self.assertEqual(slp.classify(results[0], {}), "error", results[0])

    def test_PARITY_ERROR_01_the_collector_refuses_stderr_noise_and_bad_shapes(self):
        self.assertEqual(slp.parse_probe_output(0, "[0,2,1]", "", 3), [0, 2, 1])
        for label, args in {"stderr-with-exit-0": (0, "[0]", "Get-LocalPlaybackPromptFindings : not recognized", 1),
                            "nonzero-exit": (1, "[0]", "", 1), "wrong-length": (0, "[0,1]", "", 3),
                            "not-json": (0, "boom", "", 1), "negative-count": (0, "[-1]", "", 1),
                            "non-integer": (0, "[\"x\"]", "", 1)}.items():
            with self.subTest(case=label), self.assertRaises(slp.ParityError):
                slp.parse_probe_output(*args)


class RootFixTests(unittest.TestCase):
    def test_master_source_runs_with_the_head_files_location(self):
        pretend = HERE / "no-such-dir" / "tools" / "hooks" / "x.py"
        module = slp.load_source_as("WHERE = __file__\n", "parity_test_where", pretend)
        self.assertEqual(module.WHERE, str(pretend))

    def test_a_source_that_does_not_compile_is_an_error_not_a_pass(self):
        with self.assertRaises(slp.ParityError):
            slp.load_source_as("def (:\n", "parity_test_broken", HERE / "x.py")

    def test_base_equal_to_head_over_the_full_corpus_is_zero_tightened_zero_loosened(self):
        """Pins the r3 root fix: without it master's hook, loaded from a temp path, derived a different worktree_root from its own
        __file__ and six NA-7 / NA-10 rows read as 'tightened'. A tree whose lints are edited and uncommitted is skipped: the
        comparison would then be of a real change, not of a copy with itself."""
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", slp.HOOK_REL, slp.LINT_REL],
                               capture_output=True, text=True, timeout=60)
        if dirty.returncode != 0 or dirty.stdout.strip():
            self.skipTest("a lint file is edited and uncommitted; base HEAD is not a copy of it")
        lints = slp.LINTS if PWSH else ("hook",)
        report = slp.run("HEAD", lints)
        self.assertEqual(report["counts"]["tightened"], 0, [r["id"] for r in report["results"] if r["class"] == "tightened"])
        self.assertEqual(report["counts"]["LOOSENED"], 0, [r["id"] for r in report["results"] if r["class"] == "LOOSENED"])
        self.assertEqual(report["counts"]["error"], 0, [r for r in report["results"] if r["class"] == "error"][:3])
        self.assertEqual(report["rc"], slp.EXIT_OK)
        self.assertGreaterEqual(report["rows"], MIN_STATIC_ROWS + MIN_DERIVED_HOOK_ROWS)
        self.assertEqual(report["pwsh_spawns"], 2 if PWSH else 0, "one pwsh per lint, however many rows")


# r5 B1 (CoS review of 778974a4): derived rows came only from the HEAD's own test modules, so a PR that loosens a lint and flips the
# matching test row to ALLOW (while adding one new DENY row, so the exact count pin stays green) left the comparison clean.  These
# fixtures are scratch "head" files, written to a temp dir, that WRAP the real tracked files: each is exec'd with __file__ pointing at
# the real path (so the worktree_root / REPO_ROOT it derives are the real ones) and then changes exactly one thing.  Nothing tracked
# is edited.  LOOSEN_COMMAND is the CoS repro row: master's NA-3 denies it.
LOOSEN_COMMAND = "claude auth logout"
LOOSENED_CASE_NAME = "control claude auth logout"
LOOSENED_ROW_ID = "hook:control-claude-auth-logout"


def _wrapped_head_hook_source(loosen_command: str = LOOSEN_COMMAND) -> str:
    """The wrapper head hook: the real hook, except that it ALLOWS exactly `loosen_command` (default: the CoS repro row). r6 parametrized it."""
    real = str(REPO_ROOT / slp.HOOK_REL)
    return "\n".join([
        f"_REAL = {real!r}",
        # __file__ is deliberately NOT reset here: the runner loads master's bytes as if they lived at THIS file's path (the r3 root
        # fix), so both copies must derive the same worktree_root from it, or six NA-7 rows read as phantom 'tightened'.
        "exec(compile(open(_REAL, encoding='utf-8').read(), _REAL, 'exec'), globals())",
        "_real_decide = decide",
        "",
        "",
        "def decide(payload, project_dir):",
        f"    if (payload.get('tool_input') or {{}}).get('command') == {loosen_command!r}:",
        "        return 0, ''",
        "    return _real_decide(payload, project_dir)",
        "",
    ])


def _wrapped_head_hook_tests_source() -> str:
    real = str(REPO_ROOT / slp.HOOK_TEST_REL)
    return "\n".join([
        f"_REAL = {real!r}",
        "__file__ = _REAL",
        "exec(compile(open(_REAL, encoding='utf-8').read(), _REAL, 'exec'), globals())",
        f"_target = next(c for c in CASES if c['name'] == {LOOSENED_CASE_NAME!r})",
        "_donor = next(c for c in CASES if c.get('expect') == 'DENY' and c['name'] != _target['name'] and 'raw' not in c)",
        "# the swap: the loosened row's expectation flips to ALLOW, one NEW deny row (a copy of another DENY row) keeps the count",
        "CASES = [dict(c, expect='ALLOW') if c is _target else c for c in CASES] + [dict(_donor, name='r5 swapped-in deny row')]",
        "",
    ])


class BaselineDerivedRowsTests(ParityRunnerCase):
    """r5 B1: derived rows are the UNION of the head's test modules and the baseline's, by id; a base-only row is judged like any other."""

    def scratch_head(self):
        return (_write(self.tmp / "head_hook.py", _wrapped_head_hook_source()),
                _write(self.tmp / "head_hook_tests.py", _wrapped_head_hook_tests_source()))

    def redirect_head_hook_tests(self, tests: Path):
        real = slp.load_module

        def load(path, name):
            return real(tests if Path(path).name == Path(slp.HOOK_TEST_REL).name else path, name)
        return mock.patch.object(slp, "load_module", load)

    def run_derived(self, tests: Path, *args):
        out = io.StringIO()
        with self.redirect_head_hook_tests(tests), contextlib.redirect_stdout(out):
            rc = slp.main(["--base", "HEAD", "--lint", "hook", "--corpus", str(self.corpus()), "--allow", str(self.allow), *args])
        return rc, out.getvalue()

    def test_B1_a_row_loosened_and_flipped_to_allow_with_the_count_unchanged_is_still_loosened(self):
        hook, tests = self.scratch_head()
        with self.redirect_head_hook_tests(tests):
            head_ids = [c.id for c in slp.derived_hook_cases(REPO_ROOT)]
        self.assertEqual(len(head_ids), EXACT_DERIVED_HOOK_ROWS, "the swap keeps the exact count pin green: this is the CoS repro")
        self.assertNotIn(LOOSENED_ROW_ID, head_ids, "the head's own table no longer expects a deny for the loosened row")
        rc, out = self.run_derived(tests, "--head-hook", str(hook))
        self.assertEqual(rc, slp.EXIT_LOOSENED, out)
        self.assertIn(f"LOOSENED {LOOSENED_ROW_ID}", out)
        self.assertIn("master=DENY head=ALLOW", out)
        self.assertIn("loosened=1", out)

    def test_B1_control_the_same_head_tests_with_the_real_head_hook_are_clean(self):
        _, tests = self.scratch_head()
        rc, out = self.run_derived(tests)
        self.assertEqual(rc, slp.EXIT_OK, out)
        self.assertIn("tightened=0 loosened=0", out, "no phantom rows: base-only rows classify the same as the head on an unchanged hook")
        self.assertNotIn("LOOSENED", out.replace("loosened=", ""))

    def test_B1_the_union_adds_only_rows_the_head_lacks_and_nothing_when_base_equals_head(self):
        head_only = slp.derived_hook_cases(REPO_ROOT)
        union = slp.derived_hook_cases(REPO_ROOT, "HEAD")
        self.assertEqual([c.id for c in union], [c.id for c in head_only], "base == head: the union adds no row")
        _, tests = self.scratch_head()
        with self.redirect_head_hook_tests(tests):
            swapped = slp.derived_hook_cases(REPO_ROOT, "HEAD")
        self.assertEqual(len(swapped), EXACT_DERIVED_HOOK_ROWS + 1, "the swapped-out row comes back from the baseline: head's 203 + 1 base-only")
        self.assertEqual([c.source for c in swapped if c.id == LOOSENED_ROW_ID], ["derived-base"])

    def test_B1_a_baseline_test_module_that_cannot_be_loaded_is_exit_2_never_a_skip(self):
        real_git = slp.git
        for label, outcome in {"does-not-compile": "def (:\n", "git-show-fails": slp.ParityError("git show failed: no such path at base")}.items():
            def fake_git(root, *args, outcome=outcome):
                if len(args) >= 2 and args[0] == "show" and args[1].endswith(":" + slp.HOOK_TEST_REL):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                return real_git(root, *args)
            with self.subTest(baseline=label):
                out = io.StringIO()
                with mock.patch.object(slp, "git", fake_git), contextlib.redirect_stdout(out):
                    rc = slp.main(["--base", "HEAD", "--lint", "hook", "--corpus", str(self.corpus()), "--allow", str(self.allow)])
                self.assertEqual(rc, slp.EXIT_UNDECIDED, out.getvalue())
                self.assertIn("CANNOT-DECIDE", out.getvalue())

    @unittest.skipUnless(PWSH, "pwsh is required to run the playback probe")
    def test_B1_playback_a_sentence_the_head_dropped_from_its_suite_and_now_allows_is_still_loosened(self):
        # the victim: one must-refuse sentence that appears exactly once, so dropping it from the head's suite removes the only row
        rows, pb = slp.derived_playback_rows(REPO_ROOT)
        texts = [r["text"] for r in rows]
        victim_row = next(r for r in rows if r["id"].startswith("playback:CLAUSE_MUST_REFUSE") and texts.count(r["text"]) == 1)
        victim, name = victim_row["text"], victim_row["id"].split(":")[1]
        tests = _write(self.tmp / "head_playback_tests.py", "\n".join([
            f"_REAL = {str(REPO_ROOT / slp.PLAYBACK_TEST_REL)!r}",
            "__file__ = _REAL",
            "exec(compile(open(_REAL, encoding='utf-8').read(), _REAL, 'exec'), globals())",
            f"setattr(PromptLintTests, {name!r}, tuple(t for t in getattr(PromptLintTests, {name!r}) if t != {victim!r}))",
            ""]))
        victim_file = self.tmp / "victim.txt"
        victim_file.write_bytes(victim.encode("utf-8"))
        lint = _write(self.tmp / "head_lint.ps1", "\n".join([
            "$ErrorActionPreference = 'Stop'",
            ". " + pb._psq(REPO_ROOT / slp.LINT_REL),
            "# a ScriptBlock copy of the real function (a FunctionInfo re-resolves by name and would recurse into the override below)",
            "$script:orig = ${function:Get-LocalPlaybackPromptFindings}",
            "$script:victim = [System.IO.File]::ReadAllText(" + pb._psq(victim_file) + ", [System.Text.Encoding]::UTF8)",
            "function Get-LocalPlaybackPromptFindings { param([string]$Text) if ($Text -ceq $script:victim) { return @() }; & $script:orig -Text $Text }",
            ""]))
        real = slp.load_module

        def load(path, mod_name):
            return real(tests if Path(path).name == Path(slp.PLAYBACK_TEST_REL).name else path, mod_name)
        out = io.StringIO()
        with mock.patch.object(slp, "load_module", load), contextlib.redirect_stdout(out):
            rc = slp.main(["--base", "HEAD", "--lint", "playback", "--corpus", str(self.corpus()), "--allow", str(self.allow), "--head-lint", str(lint)])
        self.assertEqual(rc, slp.EXIT_LOOSENED, out.getvalue())
        self.assertIn(f"LOOSENED {victim_row['id']}", out.getvalue())
        self.assertIn("loosened=1", out.getvalue())


# r6 B2 (CoS review of 9931280c): both unions were keyed by id, and a hook row's id is its case NAME, so editing a row's input in place under
# the same name (or a static corpus row's text under its id) took the master-denied command out of the comparison with every count
# unchanged.  The unions are now by (id, content); a baseline row whose id the head has with different content joins as `<id>@base-<hash>`.
# The scratch "head" files below wrap the real tracked ones and change exactly one thing; nothing tracked is edited.
def _head_hook_tests_source_with(case_name: str, mutation: str) -> str:
    """A scratch head copy of the hook suite whose CASES row named `case_name` is replaced by `mutation`, an expression over `c`."""
    real = str(REPO_ROOT / slp.HOOK_TEST_REL)
    return "\n".join([
        f"_REAL = {real!r}",
        "__file__ = _REAL",
        "exec(compile(open(_REAL, encoding='utf-8').read(), _REAL, 'exec'), globals())",
        f"_target = next(c for c in CASES if c['name'] == {case_name!r})",
        f"CASES = [({mutation}) if c is _target else c for c in CASES]",
        "",
    ])


def _loosened_ids(output: str) -> list[str]:
    """The row ids on the runner's `LOOSENED <id> [<lint>] ...` lines (the summary's `loosened=` counter is lower-case and has no `[`)."""
    return re.findall(r"^LOOSENED (\S+) \[", output, re.MULTILINE)


class BaselineContentUnionTests(ParityRunnerCase):
    """r6 B2, hook half: derived hook rows are unioned with the baseline's by (id, content), content = tool, input, fixture and raw."""

    def redirect_head_hook_tests(self, tests: Path):
        real = slp.load_module

        def load(path, name):
            return real(tests if Path(path).name == Path(slp.HOOK_TEST_REL).name else path, name)
        return mock.patch.object(slp, "load_module", load)

    def test_B2_an_input_edited_in_place_under_the_same_name_is_still_loosened(self):
        hook = _write(self.tmp / "head_hook.py", _wrapped_head_hook_source())
        tests = _write(self.tmp / "head_hook_tests.py", _head_hook_tests_source_with(
            LOOSENED_CASE_NAME, "dict(c, input=dict(c['input'], command=c['input']['command'] + ' --all'))"))
        with self.redirect_head_hook_tests(tests):
            head_ids = [c.id for c in slp.derived_hook_cases(REPO_ROOT)]
        self.assertEqual(len(head_ids), EXACT_DERIVED_HOOK_ROWS, "the count pin stays green: this is the CoS B2 repro")
        self.assertIn(LOOSENED_ROW_ID, head_ids, "same name, same expectation: the head still has a row under the id")
        # r7: independent of where the temp dir lives.  A hosted runner's temp layout can make path-escape rows ({OUTSIDE}, {BOARD}/..) read
        # LOOSENED under the scratch head hook in EVERY run of that layout, so the number is not 1 there.  The CONTROL (same scratch hook, the
        # suite unedited) names those rows; the edit must add exactly one row on top of them and change no other row's class.
        control = io.StringIO()
        with contextlib.redirect_stdout(control):
            slp.main(["--base", "HEAD", "--lint", "hook", "--corpus", str(self.corpus()), "--allow", str(self.allow), "--head-hook", str(hook)])
        control_ids = _loosened_ids(control.getvalue())
        self.assertIn(LOOSENED_ROW_ID, control_ids, "the control has the unedited row, which the scratch head hook allows")
        phantom = sorted(set(control_ids) - {LOOSENED_ROW_ID})
        out = io.StringIO()
        with self.redirect_head_hook_tests(tests), contextlib.redirect_stdout(out):
            rc = slp.main(["--base", "HEAD", "--lint", "hook", "--corpus", str(self.corpus()), "--allow", str(self.allow), "--head-hook", str(hook)])
        self.assertEqual(rc, slp.EXIT_LOOSENED, out.getvalue())
        self.assertRegex(out.getvalue(), rf"LOOSENED {LOOSENED_ROW_ID}@base-[0-9a-f]{{10}} ")
        self.assertIn("master=DENY head=ALLOW", out.getvalue())
        edited_ids = _loosened_ids(out.getvalue())
        self.assertEqual(sorted(i for i in edited_ids if "@base-" not in i), phantom, "no other row changed class relative to the control")
        self.assertEqual(len([i for i in edited_ids if i.startswith(LOOSENED_ROW_ID + "@base-")]), 1)
        self.assertEqual(len(edited_ids), len(phantom) + 1, "exactly the one edited row on top of the control's set")
        self.assertIn(f"loosened={len(phantom) + 1} (", out.getvalue())

    def test_B2_a_fixture_or_raw_swap_under_the_same_name_and_input_joins_as_a_base_row(self):
        tm = slp.load_module(REPO_ROOT / slp.HOOK_TEST_REL, "parity_b2_probe_tests")
        deny = [c for c in tm.CASES if c.get("expect") == "DENY"]
        fixture_case = next(c for c in deny if "raw" not in c and c.get("fixture", "default") != "default")
        raw_case = next(c for c in deny if "raw" in c)
        for label, case, mutation in (("fixture", fixture_case, "dict(c, fixture='default')"), ("raw", raw_case, "dict(c, raw=c['raw'] + ' ')")):
            with self.subTest(swap=label):
                tests = _write(self.tmp / f"head_{label}_tests.py", _head_hook_tests_source_with(case["name"], mutation))
                with self.redirect_head_hook_tests(tests):
                    union = slp.derived_hook_cases(REPO_ROOT, "HEAD")
                extra = [c for c in union if "@base-" in c.id]
                self.assertEqual(len(union), EXACT_DERIVED_HOOK_ROWS + 1)
                self.assertEqual(len(extra), 1, [c.id for c in extra])
                self.assertTrue(extra[0].id.startswith("hook:" + slp.slug(case["name"])), extra[0].id)
                self.assertEqual(extra[0].source, "derived-base")

    def test_B2_the_text_a_derived_hook_row_is_hashed_over_carries_fixture_and_raw(self):
        rows = {c.id: c for c in slp.derived_hook_cases(REPO_ROOT)}
        tm = slp.load_module(REPO_ROOT / slp.HOOK_TEST_REL, "parity_b2_probe_tests")
        fixture_case = next(c for c in tm.CASES if c.get("expect") == "DENY" and "raw" not in c and c.get("fixture", "default") != "default")
        self.assertEqual(json.loads(rows["hook:" + slp.slug(fixture_case["name"])].text)[2], fixture_case["fixture"])
        raw_case = next(c for c in tm.CASES if c.get("expect") == "DENY" and "raw" in c)
        self.assertEqual(json.loads(rows["hook:" + slp.slug(raw_case["name"])].text)[3], raw_case["raw"])

    def test_B2_with_base_equal_to_head_the_content_union_adds_no_row(self):
        head_only, _, _ = slp.collect(("hook",), slp.DEFAULT_CORPUS, True, REPO_ROOT, None)
        union, _, _ = slp.collect(("hook",), slp.DEFAULT_CORPUS, True, REPO_ROOT, "HEAD")
        self.assertEqual([c.id for c in union], [c.id for c in head_only])


class BaselineCorpusUnionTests(ParityRunnerCase):
    """r6 B2, static-corpus half: `collect` also reads the baseline's safety_lint_parity_corpus.json (`git show`) and unions it with the
    head's by (id, content); a row whose text changed under its id comes back as `<id>@base-<hash>`."""

    def tracked_doc(self) -> dict:
        return json.loads(slp.DEFAULT_CORPUS.read_text(encoding="utf-8"))

    def test_B2_a_hook_corpus_row_whose_text_changed_under_its_id_is_still_loosened(self):
        expanded = [r for r in slp.load_corpus(slp.DEFAULT_CORPUS) if r["lint"] == "hook"]
        commands = [r["command"] for r in expanded]
        # a row whose command no other corpus row carries (b455-literal-01 is also swept by b455-sweep-001: that would mask the hole)
        picked = next(r for r in expanded if commands.count(r["command"]) == 1 and r["id"].startswith("b455-literal-"))
        original, row_id = picked["command"], picked["id"]
        doc = self.tracked_doc()
        next(r for r in doc["rows"] if r["id"] == row_id)["command"] = "ls"
        head_corpus = _write(self.tmp / "head_corpus.json", json.dumps(doc))
        self.assertEqual(len(slp.load_corpus(head_corpus)), len(slp.load_corpus(slp.DEFAULT_CORPUS)), "row count unchanged")
        hook = _write(self.tmp / "head_hook.py", _wrapped_head_hook_source(original))
        # r7: independent of where the temp dir lives (see the hook-half test above): the control is the same scratch hook over the UNEDITED
        # tracked corpus; the edit adds exactly one row on top of whatever that layout already reads as LOOSENED.
        control = io.StringIO()
        with contextlib.redirect_stdout(control):
            slp.main(["--base", "HEAD", "--lint", "hook", "--corpus", str(slp.DEFAULT_CORPUS), "--allow", str(self.allow), "--head-hook", str(hook)])
        control_ids = _loosened_ids(control.getvalue())
        self.assertIn(row_id, control_ids, "the control has the unedited row, which the scratch head hook allows")
        phantom = sorted(set(control_ids) - {row_id})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = slp.main(["--base", "HEAD", "--lint", "hook", "--corpus", str(head_corpus), "--allow", str(self.allow), "--head-hook", str(hook)])
        self.assertEqual(rc, slp.EXIT_LOOSENED, out.getvalue())
        self.assertRegex(out.getvalue(), rf"LOOSENED {row_id}@base-[0-9a-f]{{10}} ")
        self.assertIn("master=DENY head=ALLOW", out.getvalue())
        edited_ids = _loosened_ids(out.getvalue())
        self.assertEqual(sorted(i for i in edited_ids if "@base-" not in i), phantom, "no other row changed class relative to the control")
        self.assertEqual(len([i for i in edited_ids if i.startswith(row_id + "@base-")]), 1)
        self.assertEqual(len(edited_ids), len(phantom) + 1, "exactly the one edited row on top of the control's set")
        self.assertIn(f"loosened={len(phantom) + 1} (", out.getvalue())

    @unittest.skipUnless(PWSH, "pwsh is required to run the playback probe")
    def test_B2_the_8_b465_b6_rows_replaced_by_a_copy_of_b5_01_under_their_ids_are_still_loosened(self):
        rows = {r["id"]: r for r in slp.load_corpus(slp.DEFAULT_CORPUS)}
        b6 = [f"b465-b6-{n:02d}" for n in range(1, 9)]
        victims = [rows[i]["text"] for i in b6]
        doc = self.tracked_doc()
        for row in doc["rows"]:
            if row["id"] in b6:
                row["text"] = rows["b465-b5-01"]["text"]
        head_corpus = _write(self.tmp / "head_corpus.json", json.dumps(doc))
        self.assertEqual(len(slp.load_corpus(head_corpus)), len(rows), "row count unchanged")
        pb = slp.derived_playback_rows(REPO_ROOT)[1]
        victim_file = _write(self.tmp / "victims.json", json.dumps(victims))
        lint = _write(self.tmp / "head_lint.ps1", "\n".join([
            "$ErrorActionPreference = 'Stop'",
            ". " + pb._psq(REPO_ROOT / slp.LINT_REL),
            "$script:orig = ${function:Get-LocalPlaybackPromptFindings}",
            "$script:victims = @(ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText(" + pb._psq(victim_file) + ")))",
            "function Get-LocalPlaybackPromptFindings { param([string]$Text) if ($script:victims -ccontains $Text) { return @() }; & $script:orig -Text $Text }",
            ""]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = slp.main(["--base", "HEAD", "--lint", "playback", "--corpus", str(head_corpus), "--allow", str(self.allow), "--head-lint", str(lint)])
        self.assertEqual(rc, slp.EXIT_LOOSENED, out.getvalue())
        for row_id in b6:
            self.assertRegex(out.getvalue(), rf"LOOSENED {row_id}@base-[0-9a-f]{{10}} ")

    def test_B2_with_base_equal_to_head_the_corpus_union_adds_no_row(self):
        head_only, head_pb, _ = slp.collect(("hook", "playback"), slp.DEFAULT_CORPUS, False, REPO_ROOT, None)
        union, union_pb, _ = slp.collect(("hook", "playback"), slp.DEFAULT_CORPUS, True, REPO_ROOT, "HEAD")
        static_ids = {r["id"] for r in slp.load_corpus(slp.DEFAULT_CORPUS)}
        self.assertEqual({c.id for c in union if c.source.startswith("corpus")}, {c.id for c in head_only if c.source.startswith("corpus")})
        self.assertEqual([r["id"] for r in union_pb if r["id"] in static_ids], [r["id"] for r in head_pb if r["id"] in static_ids])
        self.assertEqual([x for x in [c.id for c in union] + [r["id"] for r in union_pb] if "@base-" in x], [])

    def test_B2_a_baseline_corpus_that_cannot_be_read_or_parsed_is_exit_2_never_a_skip(self):
        real_git = slp.git
        shapes = {"not-json": "{not json", "wrong-schema": json.dumps({"schema": "nope", "rows": []}),
                  "git-show-fails": slp.ParityError("git show failed: corpus unreadable at base")}
        for label, outcome in shapes.items():
            def fake_git(root, *args, outcome=outcome):
                if len(args) >= 2 and args[0] == "show" and args[1].endswith(":" + slp.CORPUS_REL):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                return real_git(root, *args)
            with self.subTest(baseline=label):
                out = io.StringIO()
                with mock.patch.object(slp, "git", fake_git), contextlib.redirect_stdout(out):
                    rc = slp.main(["--base", "HEAD", "--lint", "hook", "--corpus", str(self.corpus(DENIED)), "--allow", str(self.allow)])
                self.assertEqual(rc, slp.EXIT_UNDECIDED, out.getvalue())
                self.assertIn("CANNOT-DECIDE", out.getvalue())
                self.assertIn("baseline corpus", out.getvalue())

    def test_B2_a_base_that_has_no_corpus_file_at_all_is_a_stated_bootstrap_not_an_error(self):
        # fork/master carried no corpus until this card merged: the path being ABSENT at the base is the only silent-free case, and it is reported
        with mock.patch.object(slp, "git_path_exists", lambda root, ref, rel: False):
            report = slp.run("HEAD", ("hook",), self.corpus(DENIED), self.allow, derived=True)
        self.assertEqual(report["rc"], slp.EXIT_OK, report["counts"])
        self.assertTrue(any("no baseline corpus" in n for n in report["notes"]), report["notes"])
        self.assertIn("no baseline corpus", slp.render(report))


def _baseline_sha() -> str | None:
    """The merge-base of HEAD and the first baseline ref that resolves (PARITY_BASELINE_REF overrides), or None.

    The MERGE-BASE, not the ref tip: a branch cut before master tightened a lint must not read as having loosened it."""
    refs = (os.environ["PARITY_BASELINE_REF"],) if os.environ.get("PARITY_BASELINE_REF") else BASELINE_REFS
    for ref in refs:
        done = subprocess.run(["git", "-C", str(REPO_ROOT), "merge-base", ref, "HEAD"], capture_output=True, text=True, timeout=60)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return None


class HeadVersusBaselineGateTests(unittest.TestCase):
    """The gate itself, as a unit test so hosted CI enforces it with no workflow edit: the head's two safety lints must not loosen
    anything the baseline's lints deny over the whole corpus, unless safety_lint_parity_allow.json lists the row with a reason and a PR.

    Skipped when the lints are byte-identical to the baseline's (nothing to compare; also the state right after a merge, where the base
    == head pin above already ran). Like LintDifferentialPinTests, a missing baseline ref FAILS in hosted CI (fetch-depth: 0)."""

    def test_head_does_not_loosen_the_baseline_lints(self):
        sha = _baseline_sha()
        if sha is None:
            message = "no baseline ref resolves (fork/master, origin/master, master); the parity gate needs git history"
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise AssertionError(message + " -- the hygiene jobs check out with fetch-depth: 0, so a missing ref is a broken checkout")
            self.skipTest(message)
        same = subprocess.run(["git", "-C", str(REPO_ROOT), "diff", "--quiet", sha, "--", slp.HOOK_REL, slp.LINT_REL], timeout=60)
        if same.returncode == 0:
            self.skipTest(f"both lints are byte-identical to the baseline's at {sha[:8]}; nothing to compare")
        report = slp.run(sha, slp.LINTS if PWSH else ("hook",))
        loosened = [f"{r['id']}: {r['display']}" for r in report["results"] if r["class"] == "LOOSENED" and not r["allowed"]]
        self.assertEqual(loosened, [], "master DENIES, head ALLOWS; fix the lint, or list the row in safety_lint_parity_allow.json with a reason and a PR")
        self.assertEqual([r["id"] for r in report["results"] if r["class"] == "error"], [], "a row that cannot be run is never a pass")


if __name__ == "__main__":
    unittest.main()
