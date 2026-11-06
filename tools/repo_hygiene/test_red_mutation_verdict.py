"""Tests for red_mutation_verdict: a red run counts only when it fails AT the intended assertion."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from . import red_mutation_verdict as rmv

SUITE = (
    "import unittest\n"
    "\n"
    "def scale(x):\n"
    "    return x * 60\n"
    "\n"
    "class T(unittest.TestCase):\n"
    "    def test_scale(self):\n"
    "        self.assertEqual(scale(2), 120)\n"       # line 8
    "\n"
    "    def test_other(self):\n"
    "        self.assertEqual(1, 2)\n"                 # line 11
)


def _run_unittest(files: dict[str, str], target: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in files.items():
            Path(tmp, name).write_text(text, encoding="utf-8", newline="\n")
        proc = subprocess.run([sys.executable, "-m", "unittest", target], cwd=tmp, text=True,
                              encoding="utf-8", errors="replace", capture_output=True, timeout=120)
    return proc.stdout + proc.stderr


class VerdictOnRealRuns(unittest.TestCase):
    def test_pass_only_at_the_expected_assertion(self) -> None:
        mutated = SUITE.replace("x * 60", "x * 2")
        out = _run_unittest({"test_m.py": mutated}, "test_m")
        self.assertEqual(rmv.verdict(out, "test_m.py:8")[0], rmv.PASS, out)

    def test_red_from_another_assertion_is_refused(self) -> None:
        # test_other fails at line 11 whatever the mutation does; naming line 8 must not pass.
        out = _run_unittest({"test_m.py": SUITE}, "test_m")
        status, reason = rmv.verdict(out, "test_m.py:11")
        self.assertEqual(status, rmv.PASS, reason)
        status, reason = rmv.verdict(out, "test_m.py:8")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("test_m.T.test_other", reason)

    def test_import_error_is_refused_even_when_a_failure_names_the_site(self) -> None:
        out = _run_unittest({"test_m.py": SUITE.replace("x * 60", "x * 2"), "test_bad.py": "import no_such_mod_zz\n"},
                            "discover")
        status, reason = rmv.verdict(out, "test_m.py:8")
        self.assertEqual(status, rmv.REFUSED, out)
        self.assertIn("import", reason)

    def test_green_run_is_refused(self) -> None:
        out = _run_unittest({"test_m.py": SUITE.replace("self.assertEqual(1, 2)", "self.assertEqual(1, 1)")}, "test_m")
        status, reason = rmv.verdict(out, "test_m.py:8")
        self.assertEqual(status, rmv.REFUSED, out)
        self.assertIn("green", reason)

    def test_non_assertion_error_at_the_site_is_refused(self) -> None:
        out = _run_unittest({"test_m.py": SUITE.replace("x * 60", "x // 'a'")}, "test_m")
        status, reason = rmv.verdict(out, "test_m.py:8")
        self.assertEqual(status, rmv.REFUSED, out)
        self.assertIn("non-assertion", reason)


class VerdictOnText(unittest.TestCase):
    def test_red_without_a_named_assertion_is_refused(self) -> None:
        status, reason = rmv.verdict("Ran 3 tests in 0.1s\n\nFAILED (failures=1)\n", "a.py:1")
        self.assertEqual(status, rmv.REFUSED)
        self.assertIn("names no failing assertion", reason)

    def test_missing_expectation_is_refused(self) -> None:
        self.assertEqual(rmv.verdict("Ran 1 test\n\nOK\n", "  ")[0], rmv.REFUSED)

    def test_path_match_is_by_component_suffix(self) -> None:
        block = ("=" * 70 + "\nFAIL: test_a (pkg.test_x.T.test_a)\n" + "-" * 70
                 + '\nTraceback (most recent call last):\n  File "C:\\w\\tools\\pkg\\test_x.py", line 5, in test_a\n'
                 "    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n\n" + "-" * 70 + "\nRan 1 test\n\nFAILED (failures=1)\n")
        self.assertEqual(rmv.verdict(block, "pkg/test_x.py:5")[0], rmv.PASS)
        self.assertEqual(rmv.verdict(block, "other/test_x.py:5")[0], rmv.REFUSED)
        self.assertEqual(rmv.verdict(block, "est_x.py:5")[0], rmv.REFUSED)
        self.assertEqual(rmv.verdict(block, "pkg.test_x.T.test_a")[0], rmv.PASS)


class ProbeDiscriminates(unittest.TestCase):
    def test_pr705_probe_collides_after_rounding(self) -> None:
        # PR #705 B1: probe 2.8 s. The Int32 overload ceils it to 3; the fixed double overload, once
        # the test rounds the reading for display, also reads 3. Window [3, 3] accepts both.
        ok, reason = rmv.probe_discriminates(bug_value=3, fix_value=3, window=(3, 3))
        self.assertFalse(ok, reason)
        self.assertIn("both sit inside", reason)

    def test_probe_that_separates_the_readings_passes(self) -> None:
        # Probe 2.2 s: the bug reads ceil(2.2) = 3, the fix reads 2.2; window [2.0, 2.5] holds only the fix.
        self.assertTrue(rmv.probe_discriminates(3, 2.2, (2.0, 2.5))[0])

    def test_each_failure_shape_is_named(self) -> None:
        self.assertIn("neither", rmv.probe_discriminates(30, 40, (0, 2))[1])
        self.assertIn("only the bug", rmv.probe_discriminates(1, 9, (0, 5))[1])
        self.assertIn("empty window", rmv.probe_discriminates(1, 2, (5, 1))[1])
        self.assertIn("NaN", rmv.probe_discriminates(float("nan"), 2, (0, 5))[1])
        self.assertIn("numbers", rmv.probe_discriminates("a", 2, (0, 5))[1])

    def test_window_bounds_are_inclusive(self) -> None:
        self.assertTrue(rmv.probe_discriminates(3, 2.5, (2.0, 2.5))[0])


if __name__ == "__main__":
    unittest.main()
