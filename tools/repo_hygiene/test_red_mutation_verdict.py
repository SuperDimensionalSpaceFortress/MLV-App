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

    def test_runtime_import_error_in_another_test_is_refused(self) -> None:
        # test_import dies with ModuleNotFoundError at run time, test_assert fails at line 4: the run is
        # red for a reason that is not (only) the assertion, exactly like a collection error.
        src = ("import unittest\nclass T(unittest.TestCase):\n def test_assert(self):\n  self.assertEqual(1, 2)\n"
               " def test_import(self):\n  import no_such_mod_rmv_attack\n")
        out = _run_unittest({"test_attack.py": src}, "test_attack")
        status, reason = rmv.verdict(out, "test_attack.py:4")
        self.assertEqual(status, rmv.REFUSED, out)
        self.assertIn("import", reason)

    def test_non_assertion_error_at_the_site_is_refused(self) -> None:
        out = _run_unittest({"test_m.py": SUITE.replace("x * 60", "x // 'a'")}, "test_m")
        status, reason = rmv.verdict(out, "test_m.py:8")
        self.assertEqual(status, rmv.REFUSED, out)
        self.assertIn("non-assertion", reason)


TWO_CLASSES = (
    "import unittest\n"
    "\n"
    "def scale(x):\n"
    "    return x * 60\n"
    "\n"
    "class A(unittest.TestCase):\n"
    "    def test_scale(self):\n"
    "        self.assertEqual(scale(2), 120)\n"           # line 8, passes
    "\n"
    "class B(unittest.TestCase):\n"
    "    def test_scale(self):\n"
    "        self.assertEqual(scale(2), 121)\n"           # line 12, fails
)


class QualifiedIdentity(unittest.TestCase):
    """An expected id must match the failure's FULL identity; sharing the last segment is not a match."""

    def setUp(self) -> None:
        self.out = _run_unittest({"test_s.py": TWO_CLASSES}, "test_s")

    def test_the_failing_class_passes(self) -> None:
        self.assertEqual(rmv.verdict(self.out, "test_s.B.test_scale")[0], rmv.PASS, self.out)
        self.assertEqual(rmv.verdict(self.out, "B.test_scale")[0], rmv.PASS, self.out)

    def test_same_method_name_in_another_class_is_refused(self) -> None:
        status, reason = rmv.verdict(self.out, "test_s.A.test_scale")
        self.assertEqual(status, rmv.REFUSED, reason)

    def test_nonexistent_qualified_id_is_refused(self) -> None:
        for want in ("zz.Nope.test_scale", "wrong.Expected.test_scale", "test_s.B.test_scale.extra"):
            status, reason = rmv.verdict(self.out, want)
            self.assertEqual(status, rmv.REFUSED, f"{want}: {reason}")

    def test_file_line_in_another_directory_is_refused(self) -> None:
        self.assertEqual(rmv.verdict(self.out, "test_s.py:12")[0], rmv.PASS, self.out)
        status, reason = rmv.verdict(self.out, "elsewhere/test_s.py:12")
        self.assertEqual(status, rmv.REFUSED, reason)

    def test_bare_name_matching_two_failures_is_ambiguous(self) -> None:
        out = _run_unittest({"test_s.py": TWO_CLASSES.replace("scale(2), 120", "scale(2), 119")}, "test_s")
        status, reason = rmv.verdict(out, "test_scale")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("ambiguous", reason)
        self.assertEqual(rmv.verdict(out, "A.test_scale")[0], rmv.PASS, out)


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

    def test_expectation_longer_than_the_failure_is_never_a_match(self) -> None:
        relative = ("=" * 70 + "\nFAIL: test_a (test_x.T.test_a)\n" + "-" * 70
                    + '\nTraceback (most recent call last):\n  File "test_x.py", line 5, in test_a\n'
                    "    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n\n" + "-" * 70 + "\nRan 1 test\n\nFAILED (failures=1)\n")
        self.assertEqual(rmv.verdict(relative, "test_x.py:5")[0], rmv.PASS)
        self.assertEqual(rmv.verdict(relative, "elsewhere/test_x.py:5")[0], rmv.REFUSED)
        self.assertEqual(rmv.verdict(relative, "T.test_a")[0], rmv.PASS)
        self.assertEqual(rmv.verdict(relative, "other.test_x.T.test_a")[0], rmv.REFUSED)


class CliTypedErrors(unittest.TestCase):
    def test_missing_output_file_exits_2_with_a_message_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(Path(rmv.__file__)), "verdict", "--output", str(Path(tmp, "nope.txt")), "--expect", "a.py:1"],
                text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("cannot read", proc.stderr + proc.stdout)

    def test_unreadable_output_path_is_the_same_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:  # a directory is not a readable output file
            self.assertEqual(rmv.main(["verdict", "--output", tmp, "--expect", "a.py:1"]), 2)

    def test_refused_still_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "green.txt")
            path.write_text("Ran 1 test in 0.0s\n\nOK\n", encoding="utf-8")
            self.assertEqual(rmv.main(["verdict", "--output", str(path), "--expect", "a.py:1"]), 1)


class ProbeDiscriminates(unittest.TestCase):
    def test_pr705_probe_collides_after_rounding(self) -> None:
        # PR #705 B1 as the bus trap records it: probe 2.8 s, and the test rounded the reading for
        # display, so the Int32 overload and the fixed double overload both read 3. Window [3, 3]
        # accepts both.
        ok, reason = rmv.probe_discriminates(bug_value=3, fix_value=3, window=(3, 3))
        self.assertFalse(ok, reason)
        self.assertIn("both sit inside", reason)

    def test_pr705_probe_2_2_cannot_tell_the_overloads_apart(self) -> None:
        # Measured in powershell.exe 5.1.19041.6456 (run dir probe-ps51.out.txt):
        #   [Math]::Min(330, 2.2)   -> Int32 2   (the bug: the Int32 overload)
        #   [Math]::Min(330.0, 2.2) -> Double 2.2 (the fix)
        # Window [2.0, 2.5] holds both, so the assertion passes under the bug and the fix.
        ok, reason = rmv.probe_discriminates(bug_value=2, fix_value=2.2, window=(2.0, 2.5))
        self.assertFalse(ok, reason)
        self.assertIn("both sit inside", reason)

    def test_probe_that_separates_the_readings_passes(self) -> None:
        # Same measurement, probe 2.8 s and the RAW readings (no display rounding):
        #   [Math]::Min(330, 2.8)   -> Int32 3   (2.8 converts to the nearest integer)
        #   [Math]::Min(330.0, 2.8) -> Double 2.8
        # Window [2.7, 2.9]: 2.8 is inside, 3 is outside, so only the fix reading passes.
        self.assertTrue(rmv.probe_discriminates(3, 2.8, (2.7, 2.9))[0])

    def test_each_failure_shape_is_named(self) -> None:
        self.assertIn("neither", rmv.probe_discriminates(30, 40, (0, 2))[1])
        self.assertIn("only the bug", rmv.probe_discriminates(1, 9, (0, 5))[1])
        self.assertIn("empty window", rmv.probe_discriminates(1, 2, (5, 1))[1])
        self.assertIn("NaN", rmv.probe_discriminates(float("nan"), 2, (0, 5))[1])
        self.assertIn("numbers", rmv.probe_discriminates("a", 2, (0, 5))[1])
        self.assertIn("numbers", rmv.probe_discriminates(10 ** 400, 2, (0, 5))[1])  # adversary A5: OverflowError

    def test_window_bounds_are_inclusive(self) -> None:
        self.assertTrue(rmv.probe_discriminates(3, 2.5, (2.0, 2.5))[0])


if __name__ == "__main__":
    unittest.main()
