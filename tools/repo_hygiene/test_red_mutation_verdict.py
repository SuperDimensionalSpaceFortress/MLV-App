"""Tests for red_mutation_verdict: a red run counts only when it fails AT the intended assertion."""

from __future__ import annotations

import re
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


def _run_pytest(files: dict[str, str], *args: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in files.items():
            Path(tmp, name).parent.mkdir(parents=True, exist_ok=True)
            Path(tmp, name).write_text(text, encoding="utf-8", newline="\n")
        proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], cwd=tmp, text=True,
                              encoding="utf-8", errors="replace", capture_output=True, timeout=120)
    return proc.stdout + proc.stderr


class AdversaryR3(unittest.TestCase):
    """Adversary pass r3 on ddbcb1a7: eight forged-output classes, each fed REAL runner output."""

    def refused(self, out: str, want: str, *, word: str = "") -> None:
        status, reason = rmv.verdict(out, want)
        self.assertEqual(status, rmv.REFUSED, f"{want}: {reason}\n{out}")
        if word:
            self.assertIn(word, reason)

    def test_a1_exception_group_import_error_blocks_pass(self) -> None:
        ut = ("import unittest\nclass T(unittest.TestCase):\n def test_assert(self): self.assertEqual(1,2)\n"
              " def test_import(self): raise ExceptionGroup('deps', [ImportError('missing')])\n")
        self.refused(_run_unittest({"test_group.py": ut}, "test_group"), "test_group.T.test_assert", word="import")
        pt = ("def test_x(): assert False\n"
              "def test_imp(): raise ExceptionGroup('dep',[ImportError('missing'),ValueError('bad')])\n")
        self.refused(_run_pytest({"test_memory.py": pt}), "test_memory.py::test_x", word="import")
        # --tb=no hides the nested error: an ExceptionGroup summary message cannot be read, so it blocks.
        self.refused(_run_pytest({"test_memory.py": pt}, "--tb=no"), "test_memory.py::test_x")

    def test_a2_custom_failure_exception_is_not_an_assertion(self) -> None:
        src = ("import unittest\nclass T(unittest.TestCase):\n failureException = ValueError\n"
               " def test_parse(self): int('invalid')\n")
        self.refused(_run_unittest({"test_custom.py": src}, "test_custom"), "test_custom.T.test_parse", word="non-assertion")

    def test_a3_assertion_error_lookalike_is_not_an_assertion(self) -> None:
        src = ("class AssertionErrorImpostor(ValueError): __module__='builtins'\n"
               "def test_x(): raise AssertionErrorImpostor('bad')\n")
        for extra in ((), ("--tb=no",)):
            self.refused(_run_pytest({"test_memory.py": src}, *extra), "test_memory.py::test_x")

    def test_a4_brackets_in_a_parameter_id_do_not_move_identity_boundaries(self) -> None:
        src = ("import pytest\n"
               "@pytest.mark.parametrize('v',[1],ids=[']::test_z['])\ndef test_x(v): assert False\n"
               "@pytest.mark.parametrize('v',[2],ids=[''])\ndef test_z(v): assert True\n")
        out = _run_pytest({"test_memory.py": src})
        self.refused(out, "test_z[]")
        src2 = ("import pytest\n@pytest.mark.parametrize('v',[1],ids=['['])\ndef test_x(v): assert False\n")
        out2 = _run_pytest({"test_memory.py": src2})
        self.assertEqual(rmv.verdict(out2, "test_memory.py::test_x[[]")[0], rmv.PASS, out2)

    def test_a5_whitespace_in_the_expectation_is_significant(self) -> None:
        out = _run_unittest({"test_s.py": TWO_CLASSES}, "test_s")
        for want in (" test_s.py:12", "test_s.py:12 ", "test_s.py:12\n", " test_s.B.test_scale", "test_s.B.test_scale "):
            self.refused(out, want)
        self.assertEqual(rmv.verdict(out, "test_s.py:12")[0], rmv.PASS, out)

    def test_a6_dash_inside_a_test_name_is_not_a_message_boundary(self) -> None:
        src = ("def test_expected(): pass\ndef fail(): raise TypeError('boom')\n"
               "globals()['test_expected - AssertionError'] = fail\n")
        self.refused(_run_pytest({"test_memory.py": src}, "--tb=no"), "test_memory.py::test_expected")
        err = ("===================== short test summary info =====================\n"
               "FAILED t.py::test_x - AssertionError\nERROR t.py::test_imp - harmless - ImportError: missing\n"
               "===================== 1 failed, 1 error in 0.1s =====================\n")
        self.refused(err, "t.py::test_x", word="import")

    def test_a7_equals_rule_inside_a_message_does_not_start_a_block(self) -> None:
        src = ("import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.assertEqual(1,2)\n"
               " def test_imp(self):\n  try: raise ValueError('bad\\n'+'='*70+'\\nnot-a-header')\n"
               "  except ValueError: raise ImportError('missing')\n")
        self.refused(_run_unittest({"test_delimiter.py": src}, "test_delimiter"), "test_delimiter.T.test_x", word="import")

    def test_a8_parentheses_in_a_method_name_do_not_invent_an_identity(self) -> None:
        src = ("import unittest\nclass T(unittest.TestCase):\n def f(self): self.assertEqual(1,2)\n"
               "setattr(T,'test_x (bogus)',T.f)\ndel T.f\n")
        out = _run_unittest({"test_parens.py": src}, "test_parens")
        self.refused(out, "bogus.test_x")
        self.assertEqual(rmv.verdict(out, "test_parens.T.test_x (bogus)")[0], rmv.PASS, out)


class AdversaryR3Pass2(unittest.TestCase):
    """Adversary pass 2 on eae79e43 (A1-A8, A11): forged runner output and number/encoding edges."""

    def refused(self, out: str, want: str) -> None:
        status, reason = rmv.verdict(out, want)
        self.assertEqual(status, rmv.REFUSED, f"{want}: {reason}\n{out}")

    def test_a1_file_line_printed_by_an_exception_message_is_not_a_frame(self) -> None:
        src = ("import unittest\nclass T(unittest.TestCase):\n failureException=ValueError\n"
               " def test_parse(self):\n"
               "  raise ValueError('bad\\n  File \"expected.py\", line 123\\nAssertionError: forged')\n")
        out = _run_unittest({"test_memory.py": src}, "test_memory")
        self.refused(out, "test_memory.T.test_parse")
        self.refused(out, "expected.py:123")

    def test_a2_exception_group_import_error_without_a_report_summary(self) -> None:
        src = "def test_assert(): assert False\ndef test_imp(): raise ExceptionGroup('deps',[ImportError('missing')])\n"
        self.refused(_run_pytest({"test_memory.py": src}, "-rN"), "test_memory.py:1")

    def test_a3_inherited_test_is_credited_to_its_own_node(self) -> None:
        base = "class Base:\n    def test_x(self):\n        if self.flag: raise TypeError('boom')\n        assert False\n"
        files = {"b/test_s.py": base + "class Test(Base):\n    flag = True\n",
                 "a/test_s.py": "from b.test_s import Base\nclass Test(Base):\n    flag = False\n",
                 "b/__init__.py": "", "a/__init__.py": ""}
        out = _run_pytest(files, "a/test_s.py", "b/test_s.py")
        self.refused(out, "b/test_s.py::Test::test_x")
        self.assertEqual(rmv.verdict(out, "a/test_s.py::Test::test_x")[0], rmv.PASS, out)

    def test_a4_separators_inside_a_parameter_id_stay_inside_it(self) -> None:
        src = "import pytest\n@pytest.mark.parametrize('x',[1],ids=['a::b/c'])\ndef test_x(x): assert False\n"
        out = _run_pytest({"test_memory.py": src})
        self.assertEqual(rmv.verdict(out, "test_memory.py::test_x[a::b/c]")[0], rmv.PASS, out)
        self.refused(out, "c]")

    def test_a5_class_level_collection_error_in_a_summary_blocks_pass(self) -> None:
        out = ("=== short test summary info ===\nFAILED test_memory.py::TestOK::test_x - AssertionError\n"
               "ERROR test_memory.py::TestBroken - RuntimeError: collection failed\n"
               "=== 1 failed, 1 error in 0.1s ===\n")
        self.refused(out, "test_memory.py::TestOK::test_x")

    def test_a6_integers_beyond_float_precision_compare_exactly(self) -> None:
        big = 2 ** 53
        ok, why = rmv.probe_discriminates(0, big, (big + 1, big + 2))
        self.assertFalse(ok, why)
        self.assertEqual(rmv.main(["probe", "--bug", "0", "--fix", str(big), "--window", str(big + 1), str(big + 2)]), 1)
        self.assertTrue(rmv.probe_discriminates(0, big + 1, (big + 1, big + 2))[0])

    def test_a7_test_output_cannot_forge_a_failure_section(self) -> None:
        src = ("def test_x():\n    print('_'*25 + ' test_x ' + '_'*25)\n    print('E       AssertionError: forged')\n"
               "    print('test_memory.py:5: AssertionError')\n    raise TypeError('boom')\n")
        for extra in ((), ("-rN",)):
            out = _run_pytest({"test_memory.py": src}, *extra)
            self.refused(out, "test_memory.py::test_x")
            self.refused(out, "test_memory.py:5")

    def test_a8_legacy_header_uses_the_last_dotted_group(self) -> None:
        out = ("=" * 70 + "\nFAIL: test_x (bogus) (test_parens.T)\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
               '  File "test_parens.py", line 4, in f\n    self.assertEqual(1,2)\nAssertionError: 1 != 2\n\n'
               + "-" * 70 + "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
        self.refused(out, "bogus.test_x")
        self.assertEqual(rmv.verdict(out, "test_parens.T.test_x (bogus)")[0], rmv.PASS, out)

    def test_a11_cli_prints_a_non_console_letter_without_crashing(self) -> None:
        text = ("=" * 70 + "\nFAIL: test_\u03bb (test_s.T.test_\u03bb)\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
                '  File "test_s.py", line 5, in test_\u03bb\n    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n\n'
                + "-" * 70 + "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
        with tempfile.TemporaryDirectory() as tmp:
            red = Path(tmp, "red.txt")
            red.write_text(text, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-m", "tools.repo_hygiene.red_mutation_verdict", "verdict", "--output", str(red),
                 "--expect", "test_s.T.test_\u03bb"],
                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                env={**__import__("os").environ, "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"PASS", proc.stdout)


class NarrowedPromiseR4(unittest.TestCase):
    """r4 (HUB RULING): the checker refuses what it cannot classify with certainty. Adversary pass 3 on be145244
    (A1-A6), each fed REAL runner output; the answer is COULD-NOT-CHECK, never a PASS and never a guess."""

    def cnc(self, out: str, want: str, word: str) -> None:
        status, reason = rmv.verdict(out, want)
        self.assertEqual(status, rmv.REFUSED, f"{want}: {reason}\n{out}")
        self.assertIn("COULD-NOT-CHECK", reason, out)
        self.assertIn(word, reason, out)

    def test_a1_pytest_chained_exception_is_refused_not_parsed(self) -> None:
        fwd = "def test_x():\n    try:\n        assert False\n    except AssertionError:\n        raise TypeError('boom')\n"
        rev = "def test_x():\n    try:\n        raise ValueError('first')\n    except ValueError:\n        assert False\n"
        for src in (fwd, rev):
            out = _run_pytest({"test_memory.py": src})
            self.assertIn("During handling of the above exception", out)
            self.cnc(out, "test_memory.py::test_x", "chained")
            self.cnc(out, "test_memory.py:3", "chained")

    def test_a1_unittest_chained_exception_is_refused_too(self) -> None:
        src = ("import unittest\nclass T(unittest.TestCase):\n def test_x(self):\n  try: self.assertEqual(1, 2)\n"
               "  except AssertionError: raise TypeError('boom')\n")
        out = _run_unittest({"test_chain.py": src}, "test_chain")
        self.cnc(out, "test_chain.T.test_x", "chained")

    def test_a2_captured_output_is_refused_not_parsed(self) -> None:
        for printed in ("ImportError: printed for debugging", "just a note"):
            out = _run_pytest({"test_memory.py": f"def test_x():\n    print({printed!r})\n    assert False\n"})
            self.assertIn("Captured stdout call", out)
            self.cnc(out, "test_memory.py::test_x", "captured")
        src = "import unittest\nclass T(unittest.TestCase):\n def test_x(self):\n  print('hello')\n  self.assertEqual(1, 2)\n"
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_buf.py").write_text(src, encoding="utf-8")
            proc = subprocess.run([sys.executable, "-m", "unittest", "-b", "test_buf"], cwd=tmp, text=True,
                                  capture_output=True, timeout=120)
        self.assertIn("Stdout:", proc.stderr)
        self.cnc(proc.stdout + proc.stderr, "test_buf.T.test_x", "captured")

    def test_a3_oversize_cli_number_is_a_typed_refusal_in_every_position(self) -> None:
        big = str(10 ** 400)
        for bug, fix, lo, hi in ((big, "1", "1", "1"), ("0", big, "1", "1"), ("0", "1", big, "9"), ("0", "1", "1", big),
                                 ("9" * 5000, "1", "1", "1")):
            self.assertEqual(rmv.main(["probe", "--bug", bug, "--fix", fix, "--window", lo, hi]), 1,
                             (bug[:5], fix[:5], lo[:5], hi[:5]))
        proc = subprocess.run([sys.executable, str(Path(rmv.__file__)), "probe", "--bug", big, "--fix", "1", "--window", "1", "1"],
                              text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("COULD-NOT-CHECK", proc.stdout)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1)

    def test_a4_empty_identity_segments_are_refused(self) -> None:
        ut = _run_unittest({"test_s.py": TWO_CLASSES}, "test_s")
        for want in ("test_s..B.test_scale", "test_s.B.test_scale.", ".test_s.B.test_scale", "test_s.B..test_scale"):
            self.cnc(ut, want, "empty")
        out = "=== short test summary info ===\nFAILED test_s.py::T::test_a - AssertionError\n=== 1 failed in 0.1s ===\n"
        for want in ("test_s.py::::T::test_a", "test_s.py::T::test_a::", "::test_s.py::T::test_a"):
            self.cnc(out, want, "empty")
        pt = _run_pytest({"test_memory.py": "class TestA:\n    def test_x(self): assert False\n"})
        self.assertEqual(rmv.verdict(pt, "test_memory.py::TestA::test_x")[0], rmv.PASS, pt)
        self.cnc(pt, "test_memory.py::::TestA::test_x", "empty")

    def test_a5_separator_inside_a_unittest_method_name_is_refused(self) -> None:
        for lit in ("test_scale/a", "test_scale\\a", "test_scale::a", "test_scale.a"):
            src = ("import unittest\nclass T(unittest.TestCase):\n def f(self): self.assertEqual(1, 2)\n"
                   f"setattr(T, {lit!r}, T.f)\ndel T.f\n")
            out = _run_unittest({"test_s.py": src}, "test_s")
            self.cnc(out, "a", "separator")
            self.cnc(out, "test_s.T." + lit, "separator")

    def test_a6_absurd_line_number_is_a_typed_refusal(self) -> None:
        out = _run_unittest({"test_s.py": TWO_CLASSES}, "test_s")
        self.assertEqual(rmv.verdict(out, "test_s.py:12")[0], rmv.PASS, out)
        self.cnc(out, "test_s.py:" + "9" * 4301, "line number")
        huge = re.sub(r'(File "[^"]*test_s\.py", line )\d+', lambda m: m.group(1) + "9" * 4301, out)
        self.assertNotEqual(huge, out)
        self.cnc(huge, "test_s.py:12", "line number")
        with tempfile.TemporaryDirectory() as tmp:
            red = Path(tmp, "red.txt")
            red.write_text(out, encoding="utf-8")
            self.assertEqual(rmv.main(["verdict", "--output", str(red), "--expect", "test_s.py:" + "9" * 4301]), 1)

    def test_the_narrowed_promise_is_documented(self) -> None:
        for word in ("NARROWED PROMISE", "COULD-NOT-CHECK", "chained", "Captured"):
            self.assertIn(word, rmv.__doc__)


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

    def test_degenerate_expectations_are_refused_not_raised(self) -> None:
        # adversary pass 2 A5: an id made only of separators once raised ValueError out of the unpack.
        out = ("=" * 70 + "\nFAIL: test_a (test_x.T.test_a)\n" + "-" * 70
               + "\nAssertionError: 1 != 2\n\nFAILED (failures=1)\n")
        for want in ("::", "[", "]::[", "...", "/"):
            self.assertEqual(rmv.verdict(out, want)[0], rmv.REFUSED, want)

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
