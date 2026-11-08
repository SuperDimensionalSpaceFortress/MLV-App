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
        proc = subprocess.run([sys.executable, "-m", "unittest", target], cwd=tmp, capture_output=True, timeout=120,
                              env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    return _raw_text(proc)


def _primary(output: str, expected: str) -> tuple[str, str]:
    """(status, PRIMARY reason). A test that checks WHY a run was refused reads this, never verdict()'s joined text:
    a word found only in an ``; also:`` diagnostic would pass while another refusal decided the case (r10, Dot R8-T1)."""
    v = rmv.classify(output, expected)
    return v.status, v.reason


def _raw_text(proc: subprocess.CompletedProcess) -> str:
    """stdout + stderr decoded WITHOUT newline translation (r9 adversary A1 sibling): the text a saved file of the run
    holds, so verdict() frames the runner's own line ends (CRLF on Windows). Strict UTF-8 (adversary pass 2, A2
    sibling): a lossy decode would hand verdict() a replacement character the run never printed."""
    return (proc.stdout + proc.stderr).decode("utf-8")


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


def _run_pytest(files: dict[str, str], *args: str, columns: int = 80, ci: bool = False) -> str:
    """Run a REAL pytest with its width and CI mode pinned (r6): pytest cuts a short-summary message to the terminal
    width (COLUMNS) unless CI / BUILD_NUMBER is set, so the runner's own environment must never choose the text."""
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("CI", "BUILD_NUMBER", "COLUMNS")}
    env["COLUMNS"] = str(columns)
    env["PYTHONIOENCODING"] = "utf-8"  # the child prints non-ASCII ids; a cp1252 console would replace them
    if ci:
        env["CI"] = "true"
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in files.items():
            Path(tmp, name).parent.mkdir(parents=True, exist_ok=True)
            Path(tmp, name).write_text(text, encoding="utf-8", newline="\n")
        proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], cwd=tmp,
                              capture_output=True, timeout=120, env=env)
    return _raw_text(proc)


class AdversaryR3(unittest.TestCase):
    """Adversary pass r3 on ddbcb1a7: eight forged-output classes, each fed REAL runner output."""

    def refused(self, out: str, want: str, *, word: str = "") -> None:
        status, reason = _primary(out, want)
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
        # r10 R8-T1: the chain is refused first (its primary reason); the import error is only its diagnostic
        self.refused(_run_unittest({"test_delimiter.py": src}, "test_delimiter"), "test_delimiter.T.test_x", word="chained")
        # the same rule inside an unchained import error's own message: the block is still read, the import error found
        plain = ("import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.assertEqual(1,2)\n"
                 " def test_imp(self): raise ImportError('missing\\n'+'='*70+'\\nnot-a-header')\n")
        self.refused(_run_unittest({"test_delimiter.py": plain}, "test_delimiter"), "test_delimiter.T.test_x", word="import")

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
        # r13 HUB RULING: a `::` inside the trailing brackets reads the same as a class `test_x[a` holding method `b/c]`
        # (see SummarySplitR11.test_bracket_before_last_segment_is_could_not_check), so it is COULD-NOT-CHECK, never PASS
        v = rmv.classify(out, "test_memory.py::test_x[a::b/c]")
        self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-bracket-before-last-segment"), out)
        self.refused(out, "c]")
        src = "import pytest\n@pytest.mark.parametrize('x',[1],ids=['a/b.c'])\ndef test_x(x): assert False\n"
        out = _run_pytest({"test_memory.py": src})  # a `/` or `.` after the last `::` stays inside the parameter id
        self.assertEqual(rmv.verdict(out, "test_memory.py::test_x[a/b.c]")[0], rmv.PASS, out)
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
        status, reason = _primary(out, want)
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
            proc = subprocess.run([sys.executable, "-m", "unittest", "-b", "test_buf"], cwd=tmp,
                                  capture_output=True, timeout=120)
        self.assertIn(b"Stdout:", proc.stderr)
        self.cnc(_raw_text(proc), "test_buf.T.test_x", "captured")

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

    def test_pass4_a1_number_spelled_beyond_a_float_is_refused_at_every_probe_position(self) -> None:
        for bug, fix, lo, hi in (("1e400", "1", "1", "1"), ("0", "1e400", "1", "1"), ("0", "1", "1e400", "1e400"),
                                 ("0", "1", "1", "1e400"), ("0", "1", "-1e400", "1")):
            self.assertEqual(rmv.main(["probe", "--bug", bug, "--fix", fix, "--window", lo, hi]), 1, (bug, fix, lo, hi))
        ok, why = rmv.probe_discriminates(0, float("inf"), (1, 2))
        self.assertFalse(ok)
        self.assertIn("COULD-NOT-CHECK", why)
        # a spelled-out `inf` window bound is a real, unbounded window and still works
        self.assertEqual(rmv.main(["probe", "--bug", "0", "--fix", "5", "--window", "5", "inf"]), 0)

    def test_pass4_a2_unreadable_failure_header_is_refused_not_dropped(self) -> None:
        # A test name holding a newline breaks the header the parser reads; its ImportError must not vanish.
        ut = ("import unittest\nclass T(unittest.TestCase):\n def test_assert(self): self.assertEqual(1, 2)\n"
              " def f(self): raise ImportError('missing')\nsetattr(T, 'test_import\\nsuffix', T.f)\ndel T.f\n")
        out = _run_unittest({"test_s.py": ut}, "test_s")
        self.assertIn("failures=1, errors=1", out)
        self.cnc(out, "test_s.T.test_assert", "failure blocks")
        pt = ("def fail(): raise ImportError('missing')\nglobals()['test_import\\nsuffix'] = fail\ndel fail\n"
              "def test_assert(): assert False\n")
        # with a summary the unreadable line is named at once; with -rN only the runner's count can give it away
        self.cnc(_run_pytest({"test_memory.py": pt}), "test_memory.py::test_assert", "short-summary line")
        self.cnc(_run_pytest({"test_memory.py": pt}, "--tb=no"), "test_memory.py::test_assert", "short-summary line")
        self.cnc(_run_pytest({"test_memory.py": pt}, "-rN"), "test_memory.py:4", "sections")
        plain = _run_pytest({"test_memory.py": "def test_assert(): assert False\n"})
        self.assertEqual(rmv.verdict(plain, "test_memory.py::test_assert")[0], rmv.PASS, plain)

    def _two_failures(self, name: str, **env) -> str:
        src = ("def test_assert(): assert False\ndef fail(): raise ImportError('missing')\n"
               f"globals()['{name}'] = fail\ndel fail\n")
        out = _run_pytest({"test_memory.py": src}, "--tb=no", **env)
        self.assertIn("2 failed", out)
        return out

    def test_pass7_a1_multi_line_test_id_with_a_summary_word_continuation_is_refused(self) -> None:
        for word in ("PASSED", "SKIPPED", "XFAIL", "XPASS"):
            self.cnc(self._two_failures(f"test_import\\n{word}"), "test_memory.py::test_assert", "short-summary line")

    def test_pass7_a2_over_long_node_id_without_a_message_is_refused(self) -> None:
        # r6: the same run prints a DIFFERENT line at width 80 (message dropped), at width 200 (full line) and on a
        # CI runner (never cut). The verdict must be the same typed refusal for all of them.
        for env in ({"columns": 80}, {"columns": 200}, {"columns": 80, "ci": True}, {"columns": 200, "ci": True}):
            out = self._two_failures("test_import_' + 'x' * 90 + '", **env)
            self.assertEqual("- ImportError: missing" in out, env["columns"] == 200 or bool(env.get("ci")), out)
            self.cnc(out, "test_memory.py::test_assert", "short-summary line")

    def test_r6_a_line_of_exactly_the_limit_is_still_read_and_one_longer_is_not(self) -> None:
        """The boundary of the width rule, on hand-built lines: `FAILED <node>` of 74 characters keeps its message."""
        bar = "=== 1 failed in 0.1s ==="
        def out(node: str) -> str:
            return f"=== short test summary info ===\nFAILED {node} - AssertionError: 1 != 2\n{bar}\n"
        base = "test_memory.py::test_"
        at = base + "a" * (74 - len("FAILED ") - len(base))
        self.assertEqual(len("FAILED " + at), 74)
        self.assertEqual(rmv.verdict(out(at), at)[0], rmv.PASS, out(at))
        self.cnc(out(at + "a"), at + "a", "too long to rule out a width-cut message")

    def test_r6_pass10_a1_wide_characters_are_judged_by_display_width_not_code_points(self) -> None:
        """pytest measures the summary line in terminal cells: 35 CJK characters are 70 cells but 35 code points."""
        node = "t.py::test_" + "\u754c" * 35
        conftest = ("def pytest_collection_modifyitems(items):\n    for item in items:\n"
                    f"        item._nodeid = {node!r}\n")
        for columns in (80, 200):
            out = _run_pytest({"conftest.py": conftest, "test_t.py": "def test_x(): assert False\n"}, "--tb=no",
                              columns=columns)
            self.assertIn("1 failed", out)
            self.cnc(out, node, "short-summary line")

    def test_r6_pass10_a3_a_rule_line_inside_a_multi_line_id_cannot_hide_the_real_summary(self) -> None:
        conftest = ("def pytest_collection_modifyitems(items):\n    for item in items:\n"
                    "        item._nodeid = 'fake.py::test_expected - AssertionError\\n=== hidden ===\\nreal.py::' + item.name\n")
        out = _run_pytest({"conftest.py": conftest, "test_real.py": "def test_other(): raise TypeError('boom')\n"},
                          "--tb=no", columns=300)
        self.assertIn("=== hidden ===", out)
        self.cnc(out, "fake.py::test_expected", "short-summary line")

    def test_r6_pass10_a2_a_forged_continuation_is_refused_even_when_a_genuine_outcome_matches_the_count(self) -> None:
        # The adversary's variant: one genuine passing/skipped item so that the forged line's count agrees, and -rf so
        # the genuine outcome prints no summary line of its own. Shape of a real line is what tells them apart.
        for word, extra in (("PASSED", "def test_ok(): pass\n"),
                            ("SKIPPED [1]", "import pytest\n@pytest.mark.skip(reason='r')\ndef test_ok(): pass\n")):
            conftest = ("def pytest_collection_modifyitems(items):\n    for item in items:\n"
                        "        if item.name == 'test_other':\n"
                        f"            item._nodeid = 'fake.py::test_expected - AssertionError\\n{word} bad/test_real.py::test_other'\n")
            out = _run_pytest({"conftest.py": conftest,
                               "test_real.py": "def test_other(): raise TypeError('boom')\n" + extra},
                              "--tb=no", "-rf", columns=300)
            self.assertIn(f"{word} bad/test_real.py::test_other - TypeError: boom", out)
            self.cnc(out, "fake.py::test_expected", "short-summary line")

    def test_pass7_a3_dash_in_a_directory_name_does_not_invent_a_node(self) -> None:
        files = {"test_control.py": "def test_expected(): pass\n",
                 "test_expected - assert False/test_memory.py": "def test_other(): raise TypeError('boom')\n"}
        out = _run_pytest(files)
        self.assertIn("1 failed, 1 passed", out)
        self.cnc(out, "test_expected", "short-summary line")

    def test_r5_class_rule_every_short_summary_line_splits_exactly_and_the_count_agrees(self) -> None:
        """HUB RULING r5: ANY pytest run in which a FAILED/ERROR short-summary line does not split into exactly
        `<file>::<name> - <message>` (non-empty message), or whose number of such lines differs from the runner's own
        failed+errored count, is COULD-NOT-CHECK. Hand-built outputs of both kinds, then runs that still pass."""
        ok = "FAILED test_s.py::T::test_a - AssertionError: 1 != 2\n"
        top = "=== short test summary info ===\n"
        for lines, bar in (
                ("FAILED test_s.py::T::test_a\n", "=== 1 failed in 0.1s ==="),  # no message
                ("FAILED test_s.py::T::test_a - \n", "=== 1 failed in 0.1s ==="),  # empty message
                ("FAILED test_s - AssertionError\n", "=== 1 failed in 0.1s ==="),  # no file::name
                (ok + "ERROR test_s.py::T::test_b\n", "=== 1 failed, 1 error in 0.1s ==="),  # error without message
                (ok, "=== 2 failed in 0.1s ==="),  # fewer lines than the runner counts
                (ok + ok.replace("test_a", "test_b"), "=== 1 failed in 0.1s ===")):  # more lines than it counts
            self.cnc(top + lines + bar + "\n", "test_s.py::T::test_a", "short-summary line")
        cut = top + "FAILED test_s.py::T::test_a - Assertio...\n=== 1 failed in 0.1s ===\n"  # type cut off
        self.assertEqual(rmv.verdict(cut, "test_s.py::T::test_a")[0], rmv.REFUSED)
        self.assertIn("COULD-NOT-CHECK", _primary(cut, "test_s.py::T::test_a")[1])
        green = top + ok + "=== 1 failed in 0.1s ===\n"
        self.assertEqual(rmv.verdict(green, "test_s.py::T::test_a")[0], rmv.PASS, green)
        plain = _run_pytest({"test_memory.py": "def test_assert(): assert False\n"}, "--tb=no")
        self.assertEqual(rmv.verdict(plain, "test_memory.py::test_assert")[0], rmv.PASS, plain)

    def test_r5_pass8_a1_directory_name_with_a_node_shaped_prefix_is_refused(self) -> None:
        # A directory may hold `::` and ` - ` (POSIX; Windows cannot create it), so the real runner is made to print
        # that node id through conftest: the summary line is pytest's own formatting of a forged-looking id.
        conftest = ("def pytest_collection_modifyitems(items):\n    for item in items:\n"
                    "        item._nodeid = 'fake.py::test_expected - assert False/test_bad.py::' + item.name\n")
        out = _run_pytest({"conftest.py": conftest, "test_bad.py": "def test_other(): raise TypeError('boom')\n"},
                          "--tb=no", columns=300)
        self.assertIn("FAILED fake.py::test_expected - assert False/test_bad.py::test_other - TypeError: boom", out)
        self.cnc(out, "fake.py::test_expected", "short-summary line")
        self.cnc(out, "test_other", "short-summary line")

    def test_r5_pass8_a2_a_stated_zero_count_still_has_to_agree_with_what_was_read(self) -> None:
        pt = "=== short test summary info ===\nFAILED t.py::test_x - AssertionError\n=== 0 failed in 0.1s ===\n"
        self.cnc(pt, "t.py::test_x", "short-summary line")
        ut = _run_unittest({"test_s.py": TWO_CLASSES}, "test_s")
        self.assertEqual(rmv.verdict(ut, "test_s.py:12")[0], rmv.PASS, ut)
        zero = re.sub(r"FAILED \(failures=\d+\)", "FAILED (failures=0)", ut)
        self.assertNotEqual(zero, ut)
        self.cnc(zero, "test_s.py:12", "failure blocks")
        sections = _run_pytest({"test_memory.py": "def test_assert(): assert False\n"}).replace("1 failed", "0 failed")
        self.cnc(sections, "test_memory.py::test_assert", "counts 0 failed or errored")

    def test_r5_pass9_a1_summary_word_continuation_of_a_multi_line_id_is_refused_by_the_count(self) -> None:
        # The first physical line is a complete, valid FAILED line; the continuation starts with a summary word and
        # carries the real exception. Only the runner's own passed/skipped/xfailed/xpassed count tells them apart.
        for word in ("PASSED", "SKIPPED", "XFAIL", "XPASS"):
            conftest = ("def pytest_collection_modifyitems(items):\n    for item in items:\n"
                        f"        item._nodeid = 'fake.py::test_expected - AssertionError\\n{word} bad/test_real.py::test_other'\n")
            out = _run_pytest({"conftest.py": conftest, "test_real.py": "def test_other(): raise TypeError('boom')\n"},
                              "--tb=no", columns=300)
            self.assertIn(f"{word} bad/test_real.py::test_other - TypeError: boom", out)
            self.cnc(out, "fake.py::test_expected", "short-summary line")

    def test_r5_pass9_a1_real_summary_words_still_reconcile(self) -> None:
        src = ("import pytest\ndef test_assert(): assert False\ndef test_ok(): pass\n"
               "@pytest.mark.skip(reason='r')\ndef test_skip(): pass\n"
               "@pytest.mark.xfail(reason='r')\ndef test_xf(): assert False\n"
               "@pytest.mark.xfail(reason='r')\ndef test_xp(): pass\n")
        out = _run_pytest({"test_memory.py": src}, "-rA", "--tb=no", columns=200)
        for word in ("PASSED ", "SKIPPED ", "XFAIL ", "XPASS "):
            self.assertIn(word, out)
        self.assertEqual(rmv.verdict(out, "test_memory.py::test_assert")[0], rmv.PASS, out)

    def test_r5_pass9_a2_a_number_that_underflows_to_zero_is_refused_not_read_as_zero(self) -> None:
        for bug, fix, lo, hi in (("1", "1e-400", "0", "0"), ("1e-400", "0", "0", "0"), ("1", "0", "1e-400", "1e-400"),
                                 ("1", "0", "-1e-400", "0")):
            self.assertEqual(rmv.main(["probe", "--bug", bug, "--fix", fix, "--window", lo, hi]), 1, (bug, fix, lo, hi))
        ok, why = rmv.probe_discriminates(1, 0, ("1e-400", "1e-400"))
        self.assertFalse(ok)
        self.assertIn("COULD-NOT-CHECK", why)
        self.assertTrue(rmv.probe_discriminates(1, 0, ("0.0", "0e-400"))[0])  # a real zero is still a zero
        self.assertTrue(rmv.probe_discriminates(1, 5e-324, (5e-324, 1e-300))[0])  # a real subnormal is a number

    def test_r5_pass9_a3_an_exception_type_that_may_subclass_importerror_is_refused(self) -> None:
        ut = ("import unittest\nclass MissingDependency(ImportError): pass\nclass T(unittest.TestCase):\n"
              " def test_expected(self): self.assertEqual(1, 2)\n def test_import(self): raise MissingDependency('missing')\n")
        self.cnc(_run_unittest({"test_s.py": ut}, "test_s"), "test_s.T.test_expected", "exception type")
        pt = ("class MissingDependency(ImportError): pass\ndef test_expected(): assert False\n"
              "def test_import(): raise MissingDependency('missing')\n")
        for extra in ((), ("--tb=no",), ("-rN",)):
            out = _run_pytest({"test_memory.py": pt}, *extra)
            self.cnc(out, "test_memory.py::test_expected" if "-rN" not in extra else "test_memory.py:2", "exception type")
        grp = ("class DependencyFailures(ExceptionGroup): pass\ndef test_expected(): assert False\n"
               "def test_import(): raise DependencyFailures('dependencies', [ImportError('missing'), ValueError('bad')])\n")
        self.cnc(_run_pytest({"test_memory.py": grp}, "--tb=no"), "test_memory.py::test_expected", "exception type")
        builtin = ("def test_expected(): assert False\ndef test_other(): raise TypeError('boom')\n")
        out = _run_pytest({"test_memory.py": builtin})
        self.assertEqual(rmv.verdict(out, "test_memory.py::test_expected")[0], rmv.PASS, out)

    def test_the_narrowed_promise_is_documented(self) -> None:
        for word in ("NARROWED PROMISE", "COULD-NOT-CHECK", "chained", "Captured"):
            self.assertIn(word, rmv.__doc__)


def _forged_id_conftest(first_line: str, tail: str, only: str = "test_other") -> str:
    """A conftest that gives the test named `only` a node id of `first_line`, a line break, then `tail` + its name, so
    the REAL runner prints a multi-line id (the shape a forged continuation line takes)."""
    return ("def pytest_collection_modifyitems(items):\n    for item in items:\n"
            f"        if item.name == {only!r}:\n"
            f"            item._nodeid = {first_line + chr(10) + tail!r} + item.name\n")


class AllowlistR7(unittest.TestCase):
    """r7 HUB RULING (structural stop): the short-summary and numeric readers are ALLOWLISTS. PASS needs every input in
    an enumerated readable shape from a run made with -rA; anything else is COULD-NOT-CHECK. One RED->GREEN case per
    finding (Dot A1/A2, r6 OPEN A1-A5), each with a control showing that a readable shape is still judged. Real-runner
    cases run at two terminal widths, because the r6 defect depended on it."""

    WIDTHS = (80, 200)

    def cnc(self, out: str, want: str, word: str) -> None:
        status, reason = _primary(out, want)
        self.assertEqual(status, rmv.REFUSED, f"{want}: {reason}\n{out}")
        self.assertIn("COULD-NOT-CHECK", reason, out)
        self.assertIn(word, reason, out)

    # ---- Dot A1: a forged PASSED count that matches the real omitted pass total -------------------------------
    def test_dot_a1_forged_passed_continuation_with_a_real_omitted_pass_is_refused(self) -> None:
        ok = "def test_ok(): pass\n"
        for columns in self.WIDTHS:
            # the negative: ONE ordinary passing test, DEFAULT reporting (it omits PASSED lines), --tb=no
            conftest = _forged_id_conftest("fake.py::test_expected - AssertionError", "PASSED real/test_real.py::")
            out = _run_pytest({"conftest.py": conftest, "test_real.py": "def test_other(): raise TypeError('boom')\n" + ok},
                              "--tb=no", columns=columns)
            self.assertIn("1 failed, 1 passed", out)
            self.assertNotIn("PASSED real/test_real.py::test_ok", out)  # the real pass has no line of its own
            self.cnc(out, "fake.py::test_expected", "short-summary line")
        # the same text without the ' - ' that the older rule keyed on: the forged line is exactly a real PASSED line
        forged = ("=== short test summary info ===\nFAILED fake.py::test_expected - AssertionError\n"
                  "PASSED real.py::test_ok\n=== 1 failed, 1 passed in 0.1s ===\n")
        self.cnc(forged, "fake.py::test_expected", "short-summary line")
        # default reporting alone: a run that counts a pass but lists none cannot be reconciled
        plain = ("=== short test summary info ===\nFAILED t.py::test_x - AssertionError\n"
                 "=== 1 failed, 1 passed in 0.1s ===\n")
        self.cnc(plain, "t.py::test_x", "-rA")

    def test_dot_a1_control_a_genuine_rA_run_is_still_judged(self) -> None:
        src = "def test_expected(): assert False\ndef test_ok(): pass\n"
        for columns in self.WIDTHS:
            out = _run_pytest({"test_t.py": src}, "-rA", "--tb=no", columns=columns)
            self.assertIn("PASSED test_t.py::test_ok", out)
            self.assertEqual(rmv.verdict(out, "test_t.py::test_expected")[0], rmv.PASS, out)
            plain = _run_pytest({"test_t.py": "def test_expected(): assert False\n"}, "--tb=no", columns=columns)
            self.assertEqual(rmv.verdict(plain, "test_t.py::test_expected")[0], rmv.PASS, plain)  # no pass to hide

    # ---- r6 OPEN A1: XFAIL / XPASS continuation of a multi-line id --------------------------------------------
    def test_r6_open_a1_xfail_and_xpass_continuations_are_out_of_order_and_refused(self) -> None:
        for word, label in (("XFAIL", "xfailed"), ("XPASS", "xpassed")):
            text = ("=== short test summary info ===\nFAILED fake.py::test_expected - AssertionError\n"
                    f"{word} real.py::test_other - TypeError: boom\n=== 1 failed, 1 {label} in 0.1s ===\n")
            self.cnc(text, "fake.py::test_expected", "short-summary line")
            for columns in self.WIDTHS:  # the real runner, with -rf hiding the genuine xfail/xpass
                extra = ("import pytest\n@pytest.mark.xfail(reason='r')\ndef test_g(): "
                         + ("assert False\n" if word == "XFAIL" else "pass\n"))
                conftest = _forged_id_conftest("fake.py::test_expected - AssertionError", f"{word} real/test_real.py::")
                out = _run_pytest({"conftest.py": conftest,
                                   "test_real.py": "def test_other(): raise TypeError('boom')\n" + extra},
                                  "--tb=no", "-rf", columns=columns)
                self.assertIn(f"1 {label}", out)
                self.cnc(out, "fake.py::test_expected", "short-summary line")

    # ---- r6 OPEN A2: a result-shaped rule line inside a multi-line id ------------------------------------------
    def test_r6_open_a2_a_result_shaped_rule_inside_an_id_is_refused(self) -> None:
        text = ("=== short test summary info ===\nFAILED fake.py::test_expected - AssertionError\n"
                "=== 1 failed in 0.1s ===\nreal.py::test_other - TypeError: boom\n=== 1 failed in 0.2s ===\n")
        self.cnc(text, "fake.py::test_expected", "short-summary line")
        for columns in self.WIDTHS:
            conftest = _forged_id_conftest("fake.py::test_expected - AssertionError\n=== 1 failed in 0.1s ===", "real/test_real.py::")
            out = _run_pytest({"conftest.py": conftest, "test_real.py": "def test_other(): raise TypeError('boom')\n"},
                              "--tb=no", columns=columns)
            self.assertIn("=== 1 failed in 0.1s ===", out)
            self.cnc(out, "fake.py::test_expected", "short-summary line")
        genuine = ("=== short test summary info ===\nFAILED t.py::test_x - AssertionError\n=== 1 failed in 0.1s ===\n")
        self.assertEqual(rmv.verdict(genuine, "t.py::test_x")[0], rmv.PASS, genuine)  # control: one final bar is fine

    # ---- r6 OPEN A3: the exception name must be exactly one on the fixed list -----------------------------------
    def test_r6_open_a3_only_exact_builtin_names_are_read(self) -> None:
        def run(msg: str) -> str:
            return ("=== short test summary info ===\nFAILED t.py::test_expected - AssertionError\n"
                    f"FAILED t.py::test_other - {msg}\n=== 2 failed in 0.1s ===\n")
        for msg in ("UserWarning: boom", "builtins.TypeError: boom", "t.AssertionError: boom", "MyError: boom",
                    "TypeError(boom)"):
            self.cnc(run(msg), "t.py::test_expected", "exception type")
        # an ExceptionGroup is not on the list either; the older import-blocking rule answers first
        self.assertEqual(rmv.verdict(run("ExceptionGroup: deps (1 sub-exception)"), "t.py::test_expected")[0], rmv.REFUSED)
        self.assertEqual(rmv.verdict(run("TypeError: boom"), "t.py::test_expected")[0], rmv.PASS)  # control
        self.assertEqual(rmv.verdict(run("KeyError"), "t.py::test_expected")[0], rmv.PASS)  # no message is still a name
        for columns in self.WIDTHS:  # a class that SHADOWS a builtin name prints module-qualified from the real runner
            src = "class AssertionError(ImportError): pass\ndef test_expected(): assert False\n" \
                  "def test_import(): raise AssertionError('missing')\n"
            out = _run_pytest({"test_memory.py": src}, "--tb=no", columns=columns)
            self.assertIn("test_memory.AssertionError: missing", out)
            self.cnc(out, "test_memory.py::test_expected", "exception type")

    # ---- Dot A2 / r6 OPEN A4 / A5: numbers ----------------------------------------------------------------------
    def test_dot_a2_numeric_objects_are_refused_before_any_conversion(self) -> None:
        from decimal import Decimal
        from fractions import Fraction

        class Floaty:  # numpy-like: a scalar object with __float__
            def __init__(self, x: float) -> None:
                self.x = x

            def __float__(self) -> float:
                return self.x

        for bug, fix, window in (
                (1, Decimal("1e-400"), (0, 0)), (1, Fraction(1, 10 ** 400), (0, 0)), (1, Decimal("-1e-400"), (0, 0)),
                (1, 0, (Decimal("1e-400"), Decimal("1e-400"))), (0, 1, (1, Decimal("1e400"))),
                (1, Decimal("5E-324"), (Decimal("5E-324"), 1)),  # survives conversion, still not an allowlisted type
                (Decimal("0.5"), 3, (1, 5)), (Fraction(1, 2), 3, (1, 5)), (Floaty(1.0), 3, (1, 5)), (True, 3, (1, 5))):
            ok, why = rmv.probe_discriminates(bug, fix, window)
            self.assertFalse(ok, (bug, fix, window, why))
            self.assertIn("COULD-NOT-CHECK", why, (bug, fix, window))
        # controls: the allowlisted types are still judged, a real subnormal included
        self.assertTrue(rmv.probe_discriminates(1, 5e-324, (5e-324, 1e-300))[0])
        self.assertTrue(rmv.probe_discriminates(1, "5e-324", (5e-324, 1e-300))[0])
        self.assertTrue(rmv.probe_discriminates(0.05, 3, (1, "5"))[0])
        self.assertTrue(rmv.probe_discriminates(0, 5, (5, "inf"))[0])

    def test_r6_open_a4_non_ascii_digits_and_separators_are_refused(self) -> None:
        for bug, fix, lo, hi in (("1", "١e-400", "0", "0"),  # ARABIC-INDIC DIGIT ONE then e-400: float() reads 0.0
                                 ("1", "１e-400", "0", "0"),  # FULLWIDTH DIGIT ONE
                                 ("1", "1_0", "10", "10"), ("1", "2", "٢", "٢")):
            self.assertEqual(rmv.main(["probe", "--bug", bug, "--fix", fix, "--window", lo, hi]), 1, (bug, fix, lo, hi))
            ok, why = rmv.probe_discriminates(bug, fix, (lo, hi))
            self.assertFalse(ok)
            self.assertIn("COULD-NOT-CHECK", why)
        self.assertEqual(rmv.main(["probe", "--bug", "1", "--fix", "2", "--window", "2", "2"]), 0)  # control: ASCII text

    # ---- r7 adversary pass on f8c71bb8: A1 (unittest identity) and A2 (integer text vs its float spelling) -------------
    def test_r7_adversary_a1_a_unittest_method_name_that_reads_two_ways_is_refused(self) -> None:
        src = ("import unittest\nclass T(unittest.TestCase):\n def f(self): self.assertEqual(1, 2)\n"
               "setattr(T, 'test_x (bogus.test_x)', T.f)\ndel T.f\n")
        out = _run_unittest({"test_s.py": src}, "test_s")
        self.assertIn("FAIL: test_x (bogus.test_x)", out)
        self.cnc(out, "bogus.test_x", "identity")
        self.cnc(out, "test_s.T.test_x (bogus.test_x)", "identity")
        ordinary = _run_unittest({"test_s.py": TWO_CLASSES}, "test_s")  # control: one reading, still judged
        self.assertEqual(rmv.verdict(ordinary, "test_s.py:12")[0], rmv.PASS, ordinary)

    def test_r7_adversary_a2_integer_text_and_its_float_spelling_denote_one_number(self) -> None:
        big = "9007199254740993"  # 2**53 + 1: a float cannot hold it
        for spelling in (big + ".0", big + "e0", "90071992547409930e-1"):
            self.assertEqual(rmv.main(["probe", "--bug", big, "--fix", spelling, "--window", "9007199254740992", "9007199254740992"]),
                             1, spelling)  # same number on both sides, neither inside the window: not a discriminating probe
            self.assertEqual(rmv.main(["probe", "--bug", "0", "--fix", big, "--window", spelling, spelling]), 0, spelling)
            ok, why = rmv.probe_discriminates(big, spelling, (2 ** 53, 2 ** 53))
            self.assertFalse(ok, why)
        for spelling in (big + ".5", "9007199254740993.5e0"):  # neither an int nor a float can hold it exactly
            ok, why = rmv.probe_discriminates(0, spelling, (1, 2))
            self.assertFalse(ok)
            self.assertIn("COULD-NOT-CHECK", why)
        self.assertEqual(rmv.main(["probe", "--bug", "3.0", "--fix", "1e3", "--window", "999", "1001"]), 0)  # control

    # ---- r7 adversary pass on a7e41ffc: A1 (no closing bar), A2 (no summary section), A3 (empty id segments) ---------------
    def test_r7_adversary2_a1_a_short_summary_with_no_final_result_bar_is_refused(self) -> None:
        cut = "=== short test summary info ===\nFAILED t.py::test_x - AssertionError\n"
        self.cnc(cut, "t.py::test_x", "short-summary line")
        done = cut + "=== 1 failed in 0.1s ===\n"
        self.assertEqual(rmv.verdict(done, "t.py::test_x")[0], rmv.PASS, done)  # control: the closing bar makes it readable
        for columns in self.WIDTHS:
            out = _run_pytest({"test_t.py": "def test_x(): assert False\n"}, "--tb=no", columns=columns)
            self.assertEqual(rmv.verdict(out, "test_t.py::test_x")[0], rmv.PASS, out)
            lines = out.rstrip("\n").splitlines()
            self.cnc("\n".join(lines[:-1]) + "\n", "test_t.py::test_x", "short-summary line")  # the real run, bar cut off

    def test_r7_adversary2_a2_a_run_without_a_summary_section_still_has_to_reconcile_its_passes(self) -> None:
        text = "=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n=== 1 failed, 1 passed in 0.1s ===\n"
        self.cnc(text, "t.py:2", "-rA")
        for label in ("skipped", "xfailed", "xpassed"):
            self.cnc(text.replace("1 passed", f"1 {label}"), "t.py:2", "-rA")
        for columns in self.WIDTHS:  # the real runner: -rN prints no summary section at all
            out = _run_pytest({"test_t.py": "def test_x(): assert False\ndef test_ok(): pass\n"}, "-rN", columns=columns)
            self.assertNotIn("short test summary", out)
            self.cnc(out, "test_t.py:1", "-rA")
            alone = _run_pytest({"test_t.py": "def test_x(): assert False\n"}, "-rN", columns=columns)
            self.assertEqual(rmv.verdict(alone, "test_t.py:1")[0], rmv.PASS, alone)  # control: nothing hidden, file:line

    def test_r7_adversary2_a3_an_empty_segment_in_a_passed_xfail_or_skipped_id_is_refused(self) -> None:
        for line, label in (("PASSED t.py::::test_ok", "passed"), ("PASSED t.py::test_ok::", "passed"),
                            ("PASSED a//t.py::test_ok", "passed"), ("XFAIL t.py::::test_ok", "xfailed"),
                            ("XPASS t.py::test_ok::", "xpassed"), ("SKIPPED [1] a//t.py:3: r", "skipped")):
            text = (f"=== short test summary info ===\n{line}\nFAILED t.py::test_x - AssertionError\n"
                    f"=== 1 failed, 1 {label} in 0.1s ===\n")
            self.cnc(text, "t.py::test_x", "short-summary line")
        ok = ("=== short test summary info ===\nPASSED t.py::test_ok\nFAILED t.py::test_x - AssertionError\n"
              "=== 1 failed, 1 passed in 0.1s ===\n")
        self.assertEqual(rmv.verdict(ok, "t.py::test_x")[0], rmv.PASS, ok)  # control

    def test_r6_open_a5_decimal_and_fraction_overflow_and_underflow_are_refused(self) -> None:
        from decimal import Decimal
        from fractions import Fraction
        for args in ((1, Decimal("1e-400"), (0, 0)), (1, Fraction(1, 10 ** 400), (0, 0)), (0, 1, (1, Decimal("1e400")))):
            self.assertFalse(rmv.probe_discriminates(*args)[0], args)


class VerdictOnText(unittest.TestCase):
    def test_red_without_a_named_assertion_is_refused(self) -> None:
        # r10 R8-T1: the count precheck decides this text (its primary reason); "names no failing assertion" is only
        # the secondary diagnostic, so it is checked there and never as the reason
        got = rmv.classify("Ran 3 tests in 0.1s\n\nFAILED (failures=1)\n", "a.py:1")
        self.assertEqual(got.status, rmv.REFUSED)
        self.assertIn("0 failure blocks were readable", got.reason)
        self.assertTrue(any("names no failing assertion" in a for a in got.also), got)

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


    # ---- r8 (R7-C1 / R7-T1): a PASS needs a recognised terminal result line on EVERY path ------------------------
    MIXED = ("def test_expected(): assert False\n"
             "def test_imp():\n    import no_such_mod_rmv_r8\n")

    def _cut_before_second_section(self, out: str) -> str:
        """A -rN log whose tail was lost: everything from the second FAILURES section on (the import failure, the
        result bar) is gone, so the text still holds a readable assertion failure and nothing that closes the run."""
        marks = [m.start() for m in re.finditer(r"^_{3,} test_\w+ _{3,}\r?$", out, re.M)]  # the run's own CRLF on Windows
        self.assertGreaterEqual(len(marks), 2, out)
        return out[:marks[1]]

    def test_rN_missing_tail_before_import_failure_is_could_not_check(self) -> None:
        for columns in (80, 200):
            full = _run_pytest({"test_t.py": self.MIXED}, "-rN", "--tb=short", columns=columns)
            self.assertRegex(full, r"2 failed", full)
            cut = self._cut_before_second_section(full)
            self.assertNotRegex(cut, r"\d+ (?:failed|passed)", cut)  # no result bar survives the cut
            status, reason = rmv.verdict(cut, "test_t.py:1")
            self.assertEqual(status, rmv.REFUSED, f"{reason}\n{cut}")
            self.assertIn("COULD-NOT-CHECK", reason, cut)
            self.assertIn("result line", reason, cut)

    def test_rN_complete_runs_are_still_judged(self) -> None:
        for columns in (80, 200):
            only = _run_pytest({"test_t.py": "def test_expected(): assert False\n"}, "-rN", "--tb=short", columns=columns)
            self.assertEqual(rmv.verdict(only, "test_t.py:1")[0], rmv.PASS, only)  # complete, assertion only
            mixed = _run_pytest({"test_t.py": self.MIXED}, "-rN", "--tb=short", columns=columns)
            status, reason = rmv.verdict(mixed, "test_t.py:1")  # complete, assertion plus import failure
            self.assertEqual(status, rmv.REFUSED, mixed)
            self.assertIn("collection or import error", reason, mixed)

    def test_unittest_blocks_with_no_result_line_are_could_not_check(self) -> None:
        block = ("=" * 70 + "\nFAIL: test_a (test_x.T.test_a)\n" + "-" * 70
                 + '\nTraceback (most recent call last):\n  File "test_x.py", line 5, in test_a\n'
                 "    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n")
        status, reason = rmv.verdict(block, "test_x.py:5")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("COULD-NOT-CHECK", reason)
        self.assertIn("result line", reason)
        self.assertEqual(rmv.verdict(block + "\nRan 1 test in 0.0s\n\nFAILED (failures=1)\n", "test_x.py:5")[0], rmv.PASS)

    def test_no_parsed_failure_leaves_the_count_check_with_an_empty_answer(self) -> None:
        # R7-C1 as the hub ACK words it: `_count_mismatch` returned "" for a text with no result line, whatever it held.
        cut = "=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n"
        self.assertTrue(rmv.parse_run(cut).failures)
        self.assertNotEqual(rmv.parse_run(cut).mismatch, "")


    # ---- r8 adversary A1: the terminal result is the LAST line of the run, not any result-shaped line --------------
    def test_adversary_a1_a_result_shaped_line_inside_the_run_is_not_the_terminal_result(self) -> None:
        # Sol's input: a real unittest log cut inside its second failure header. `FAILED (failures=1)` sits MID-text (a
        # failing test printed it, or a trailer from another run was pasted in), no `Ran N tests` footer exists, and the
        # complete log holds an ImportError, so a PASS here is wrong.
        rule, dash = "=" * 70, "-" * 70
        cut = ("FF\n" + rule + "\nFAIL: test_a (test_attack.T.test_a)\n" + dash + "\nTraceback (most recent call last):\n"
               '  File "test_attack.py", line 4, in test_a\nAssertionError: bad\nFAILED (failures=1)\n\n'
               + rule + "\nFAIL: test_b (test_attack.T.test_b)\n")
        for want in ("test_attack.T.test_a", "test_attack.py:4"):
            status, reason = rmv.verdict(cut, want)
            self.assertEqual(status, rmv.REFUSED, f"{want}: {reason}")
            self.assertIn("COULD-NOT-CHECK", reason)
            self.assertIn("result line", reason)

    def test_adversary_a1_a_unittest_trailer_without_its_ran_footer_is_not_terminal(self) -> None:
        block = ("=" * 70 + "\nFAIL: test_a (test_x.T.test_a)\n" + "-" * 70
                 + '\nTraceback (most recent call last):\n  File "test_x.py", line 5, in test_a\n'
                 "    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n")
        status, reason = rmv.verdict(block + "\nFAILED (failures=1)\n", "test_x.py:5")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("result line", reason)
        self.assertEqual(rmv.verdict(block + "\nRan 1 test in 0.0s\n\nFAILED (failures=1)\n", "test_x.py:5")[0], rmv.PASS)
        # the trailer must also be the last thing in the run
        status, reason = rmv.verdict(block + "\nRan 1 test in 0.0s\n\nFAILED (failures=1)\nmore text\n", "test_x.py:5")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("result line", reason)

    def test_adversary_a1_a_pytest_bar_inside_the_run_is_not_the_terminal_result(self) -> None:
        cut = ("=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n"
               "=== 1 failed in 0.1s ===\n___ test_y ___\nE import error cut here\n")
        status, reason = rmv.verdict(cut, "t.py:2")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("COULD-NOT-CHECK", reason)
        self.assertIn("result line", reason)
        complete = "=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n=== 1 failed in 0.1s ===\n"
        self.assertEqual(rmv.verdict(complete, "t.py:2")[0], rmv.PASS)

    # ---- r8 adversary A2: a header too long to read is refused at once, not parsed in quadratic time ---------------
    def test_adversary_a2_an_overlong_subtest_header_is_could_not_check_in_bounded_time(self) -> None:
        import time
        rest = "test_x (t.T.test_x) (rows=[" + ", ".join(f"({i}, {i + 1})" for i in range(24000)) + "])"
        block = ("=" * 70 + "\nFAIL: " + rest + "\n" + "-" * 70
                 + '\nTraceback (most recent call last):\n  File "t.py", line 5, in test_x\n'
                 "    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n\nRan 1 test in 0.0s\n\nFAILED (failures=1)\n")
        started = time.perf_counter()
        status, reason = rmv.verdict(block, "t.py:5")
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("COULD-NOT-CHECK", reason)
        self.assertIn("header", reason)
        short = block.replace(rest, "test_x (t.T.test_x) (rows=[(0, 1), (1, 2)])")  # an ordinary subTest header still reads
        self.assertEqual(rmv.verdict(short, "t.py:5")[0], rmv.PASS)


    # ---- r8 adversary pass 2 (8e754e3d) ----------------------------------------------------------------------
    GROUP = ("class MissingDependency(ImportError): pass\n"
             "def test_expected(): assert False\n"
             "def test_import(): raise ExceptionGroup('deps', [MissingDependency('missing')])\n")

    def test_adversary2_a1_a_failure_that_prints_no_exception_line_is_could_not_check(self) -> None:
        # An exception group prints no `E` line, and under -rN there is no summary line either, so the exception type
        # of the second failure is not shown anywhere; its class may subclass ImportError.
        for columns in (80, 200):
            for tb in ("--tb=long", "--tb=short"):
                out = _run_pytest({"test_t.py": self.GROUP}, "-rN", tb, columns=columns)
                self.assertIn("2 failed", out)
                status, reason = rmv.verdict(out, "test_t.py:2")
                self.assertEqual(status, rmv.REFUSED, f"{tb}: {reason}\n{out}")
                self.assertIn("COULD-NOT-CHECK", reason, out)
                self.assertIn("exception line", reason, out)
        ordinary = _run_pytest({"test_t.py": "def test_expected(): assert False\ndef test_other(): raise TypeError('boom')\n"},
                               "-rN", "--tb=short")
        status, reason = rmv.verdict(ordinary, "test_t.py:1")  # an ordinary builtin type is still classified: not an import error
        self.assertEqual(status, rmv.PASS, f"{reason}\n{ordinary}")

    def test_adversary2_a1_the_same_group_in_a_unittest_run_is_could_not_check(self) -> None:
        src = ("import unittest\n" + self.GROUP.split("\n", 1)[0] + "\n"
               "class T(unittest.TestCase):\n"
               "    def test_expected(self):\n        self.assertEqual(1, 2)\n"
               "    def test_import(self):\n        raise ExceptionGroup('deps', [MissingDependency('missing')])\n")
        out = _run_unittest({"test_t.py": src}, "test_t")
        status, reason = rmv.verdict(out, "test_t.py:5")
        self.assertEqual(status, rmv.REFUSED, f"{reason}\n{out}")
        self.assertIn("COULD-NOT-CHECK", reason, out)

    def test_adversary2_a2_an_indented_result_line_is_not_the_terminal_result(self) -> None:
        # The terminal result starts at column 0: an indented ` FAILED (failures=2)` is message text, and with the
        # strict reading the stated count (2) is never compared with the one readable failure.
        unit = ("FF\n" + "=" * 70 + "\nFAIL: test_a (test_attack.A.test_a)\n" + "-" * 70
                + '\nTraceback (most recent call last):\n  File "test_attack.py", line 3, in test_a\nAssertionError: bad\n'
                "Ran 2 tests\n\n FAILED (failures=2)\n")
        status, reason = rmv.verdict(unit, "test_attack.A.test_a")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("result line", reason)
        pytest_text = ("=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n === 2 failed in 0.1s ===\n")
        status, reason = rmv.verdict(pytest_text, "t.py:2")
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertIn("result line", reason)
        flush = unit.replace(" FAILED (failures=2)", "FAILED (failures=1)")  # a real, flush-left trailer is judged
        self.assertEqual(rmv.verdict(flush, "test_attack.A.test_a")[0], rmv.PASS)

    def test_a_long_subtest_header_is_read_in_linear_time(self) -> None:
        # opus r7 RMV-UT-IDENTITIES-SCALING-1: every ' (' x ')' pair was tried with a slice copy each.
        import time
        rest = "test_x (t.T.test_x) (rows=[" + ", ".join(f"({i}, {i + 1})" for i in range(3000)) + "])"
        started = time.perf_counter()
        self.assertEqual(rmv._ut_identities(rest), [("test_x", "t.T.test_x")])
        self.assertLess(time.perf_counter() - started, 2.0)

    # ---- r9 (both r8 keys + r8 adversary A1-A3 on cd8773fc): two STRUCTURES, not three more parse rules -----------
    # Structure 2: one exception account per FAILED occurrence; an ambiguous pairing of sections and occurrences is
    # COULD-NOT-CHECK and names the node ids.
    SUFFIX = ("=== FAILURES ===\n___ test_x ___\nE assert False\na/test_s.py:2: AssertionError\n"
              "___ test_x ___\nE b.a.test_s.MissingDependency: missing\nb/a/test_s.py:3: MissingDependency\n"
              "___ test_expected ___\nE assert False\ntest_ok.py:1: AssertionError\n=== short test summary info ===\n"
              "FAILED a/test_s.py::test_x - assert False\n"
              "FAILED b/a/test_s.py::test_x - b.a.test_s.MissingDependency: missing\n"
              "FAILED test_ok.py::test_expected - assert False\n=== 3 failed in 0.1s ===\n")
    DUPE = ("=== FAILURES ===\n___ test_x ___\nE test_dupe.MissingDependency: missing\ntest_dupe.py:5: MissingDependency\n"
            "___ test_x ___\nE assert False\ntest_dupe.py:6: AssertionError\n=== short test summary info ===\n"
            "FAILED test_dupe.py::test_x - test_dupe.MissingDependency: missing\n"
            "FAILED test_dupe.py::test_x - assert False\n=== 2 failed in 0.1s ===\n")

    def _could_not_check(self, text: str, expected: str, *needles: str) -> str:
        status, reason = _primary(text, expected)
        self.assertEqual(status, rmv.REFUSED, reason)
        self.assertTrue(reason.startswith("COULD-NOT-CHECK"), reason)
        for needle in needles:
            self.assertIn(needle, reason)
        return reason

    def test_r9_a1_suffix_related_files_sharing_a_test_name_are_could_not_check(self) -> None:
        self._could_not_check(self.SUFFIX, "test_ok.py:1", "a/test_s.py::test_x", "b/a/test_s.py::test_x")
        shallow = self.SUFFIX.replace("b/a/test_s.py", "b/test_s.py").replace("a/test_s.py", "test_s.py")
        self._could_not_check(shallow, "test_ok.py:1", "test_s.py::test_x", "b/test_s.py::test_x")

    def test_r9_a1_class_row_suffix_related_files_with_the_same_exception_type(self) -> None:
        same = self.SUFFIX.replace("E b.a.test_s.MissingDependency: missing\nb/a/test_s.py:3: MissingDependency",
                                   "E assert False\nb/a/test_s.py:3: AssertionError").replace(
            "b/a/test_s.py::test_x - b.a.test_s.MissingDependency: missing", "b/a/test_s.py::test_x - assert False")
        self._could_not_check(same, "test_ok.py:1", "a/test_s.py::test_x", "b/a/test_s.py::test_x")

    def test_r9_a1_control_files_that_are_not_suffix_related_still_pair(self) -> None:
        apart = self.SUFFIX.replace("b/a/test_s.py", "b/test_s.py")  # a/test_s.py and b/test_s.py: two files, one each
        reason = self._could_not_check(apart, "test_ok.py:1", "exception type")  # the custom type still blocks
        self.assertNotIn("pairing", reason)
        clean = apart.replace("E b.a.test_s.MissingDependency: missing\nb/test_s.py:3: MissingDependency",
                              "E assert False\nb/test_s.py:3: AssertionError").replace(
            "b/test_s.py::test_x - b.a.test_s.MissingDependency: missing", "b/test_s.py::test_x - assert False")
        self.assertEqual(rmv.verdict(clean, "test_ok.py:1")[0], rmv.PASS)
        self.assertEqual(rmv.verdict(clean, "b/test_s.py:3")[0], rmv.PASS)

    def test_r9_a2_a_repeated_node_id_is_could_not_check(self) -> None:
        self._could_not_check(self.DUPE, "test_dupe.py:6", "test_dupe.py::test_x")
        swapped = self.DUPE.replace("FAILED test_dupe.py::test_x - test_dupe.MissingDependency: missing\n"
                                    "FAILED test_dupe.py::test_x - assert False\n",
                                    "FAILED test_dupe.py::test_x - assert False\n"
                                    "FAILED test_dupe.py::test_x - test_dupe.MissingDependency: missing\n")
        self._could_not_check(swapped, "test_dupe.py:6", "test_dupe.py::test_x")

    def test_r9_a2_class_row_a_repeated_node_id_with_the_same_message(self) -> None:
        same = ("=== FAILURES ===\n___ test_x ___\nE assert False\ntest_dupe.py:5: AssertionError\n"
                "___ test_x ___\nE assert False\ntest_dupe.py:6: AssertionError\n=== short test summary info ===\n"
                "FAILED test_dupe.py::test_x - assert False\nFAILED test_dupe.py::test_x - assert False\n"
                "=== 2 failed in 0.1s ===\n")
        self._could_not_check(same, "test_dupe.py:6", "test_dupe.py::test_x")
        self._could_not_check(same, "test_dupe.py::test_x", "test_dupe.py::test_x")  # by id, too

    # Structure 1: the text is framed ONCE at entry; a line boundary other than LF / CRLF is COULD-NOT-CHECK.
    UT_GREEN = ("=" * 70 + "\nFAIL: test_x (t.T.test_x)\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
                '  File "t.py", line 2, in test_x\nAssertionError: no\n\nRan 1 test in 0.0s\n\nOK\n')
    FOREIGN = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r")

    def test_r9_a3_a_foreign_line_boundary_is_could_not_check(self) -> None:
        zero = self.UT_GREEN.replace("OK\n", "FAILED (failures=0)\n")
        for sep in self.FOREIGN:
            for text in (self.UT_GREEN, zero):
                with self.subTest(sep=f"U+{ord(sep):04X}", tail=text[-8:]):
                    self._could_not_check(text.replace("\n", sep), "t.py:2", f"U+{ord(sep):04X}")

    def test_r9_a3_one_foreign_boundary_anywhere_refuses_the_whole_run(self) -> None:
        red = self.UT_GREEN.replace("OK\n", "FAILED (failures=1)\n")
        self.assertEqual(rmv.verdict(red, "t.py:2")[0], rmv.PASS)
        for sep in self.FOREIGN:
            with self.subTest(sep=f"U+{ord(sep):04X}"):
                self._could_not_check(red.replace("AssertionError: no", "AssertionError: n" + sep + "o"), "t.py:2",
                                      f"U+{ord(sep):04X}")

    def test_r9_a3_lf_and_crlf_controls_keep_their_r8_verdicts(self) -> None:
        red = self.UT_GREEN.replace("OK\n", "FAILED (failures=1)\n")
        zero = self.UT_GREEN.replace("OK\n", "FAILED (failures=0)\n")
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=repr(eol)):
                self.assertEqual(rmv.verdict(red.replace("\n", eol), "t.py:2")[0], rmv.PASS)
                status, reason = rmv.verdict(self.UT_GREEN.replace("\n", eol), "t.py:2")
                self.assertEqual(status, rmv.REFUSED, reason)
                self.assertIn("green", reason)
                status, reason = rmv.verdict(zero.replace("\n", eol), "t.py:2")
                self.assertEqual(status, rmv.REFUSED, reason)
                self.assertIn("COULD-NOT-CHECK", reason)
                self.assertIn("counts 0", reason)
                pt = "=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n=== 1 failed in 0.1s ===\n"
                self.assertEqual(rmv.verdict(pt.replace("\n", eol), "t.py:2")[0], rmv.PASS)

    def test_r9_the_foreign_boundary_set_is_exactly_what_splitlines_splits_on(self) -> None:
        splits = {chr(c) for c in range(0x110000)
                  if not 0xD800 <= c <= 0xDFFF and len(("a" + chr(c) + "b").splitlines()) > 1}
        self.assertEqual(splits - {"\n"}, set(self.FOREIGN))
        self.assertEqual(set(rmv._FOREIGN_BREAKS), set(self.FOREIGN))

    def test_r9_every_reader_uses_the_one_framing(self) -> None:
        # No reader may split or anchor on the raw text again: one helper frames it, and every check reads its lines.
        import tokenize
        with open(rmv.__file__, encoding="utf-8") as fh:  # code tokens only: comments and docstrings may name them
            toks = [(t.type, t.string) for t in tokenize.generate_tokens(fh.readline)
                    if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)]
        names = [s for k, s in toks if k == tokenize.NAME]
        self.assertNotIn("splitlines", names)
        self.assertNotIn("MULTILINE", names)
        self.assertFalse(any(toks[i:i + 3] == [(tokenize.NAME, "re"), (tokenize.OP, "."), (tokenize.NAME, flag)]
                             for i in range(len(toks)) for flag in ("M", "S", "DOTALL")), "no multi-line regex flag")
        splits = [i for i in range(len(toks) - 3) if toks[i][1] == "split" and toks[i + 1][1] == "("
                  and toks[i + 2][0] == tokenize.STRING and toks[i + 2][1] in ('"\\n"', "'\\n'")]
        self.assertEqual(len(splits), 1, "only _frame splits the text into lines")

    # ---- r10 R8-T1 (Dot 6706642324): the primary refusal is a field of its own, never a substring of a composite ----
    def test_r10_primary_reason_is_separate_from_also_diagnostics(self) -> None:
        malformed = ("=== short test summary info ===\nFAILED t.py::test_x - assert False\n"
                     "=== 1 failed, 1 passed in 0.1s ===\n")
        got = rmv.classify(malformed, "other.py::test_x")
        self.assertIsInstance(got, rmv.Verdict)
        self.assertEqual(got.status, rmv.REFUSED)
        self.assertTrue(got.reason.startswith("COULD-NOT-CHECK (short-summary line"), got)
        self.assertNotIn("also", got.reason)
        self.assertNotIn("the run is red but not at", got.reason)
        self.assertEqual(len(got.also), 1, got)
        self.assertTrue(got.also[0].startswith("the run is red but not at other.py::test_x"), got)
        # verdict() keeps its (status, text) shape and its exact text: the primary, then each diagnostic after `; also: `
        self.assertEqual(rmv.verdict(malformed, "other.py::test_x"),
                         (got.status, got.reason + "".join("; also: " + a for a in got.also)))
        # a precheck refusal that the classifier ALSO refuses as unclassifiable: the second one is a diagnostic too
        cut = "Ran 1 test in 0.000s\n\nFAILED (failures=1)\n"
        got = rmv.classify(cut, "t.py:8")
        self.assertTrue(got.reason.startswith("COULD-NOT-CHECK (the runner counts 1"), got)
        self.assertNotIn("names no failing assertion", got.reason)
        self.assertEqual(rmv.verdict(cut, "t.py:8")[1], got.reason + "".join("; also: " + a for a in got.also))
        # a refusal with no precheck problem and a PASS carry no diagnostic
        for text, want in (("Ran 1 test in 0.0s\n\nOK\n", rmv.REFUSED), (self.PT_RED, rmv.PASS)):
            got = rmv.classify(text, "t.py:2")
            self.assertEqual((got.status, got.also), (want, ()), got)
            self.assertEqual(rmv.verdict(text, "t.py:2"), (got.status, got.reason))
        got = rmv.classify(None, "t.py:2")  # type: ignore[arg-type]
        self.assertEqual((got.status, got.also), (rmv.REFUSED, ()), got)
        self.assertIn("internal-error", got.reason)

    # ---- r10 adversary A1 (ee9972d8): a segment's KIND is part of the identity ------------------------------------
    def test_r10_a1_a_class_name_does_not_satisfy_a_file_qualified_expectation(self) -> None:
        def pt(node: str) -> str:
            return f"=== short test summary info ===\nFAILED {node} - assert False\n=== 1 failed in 0.1s ===\n"

        for node, expected in (("test_s.py::TestOther::test_x", "TestOther.py::test_x"),       # the finding's input
                               ("q/dir.py::TestOther::test_x", "dir/TestOther.py::test_x"),    # a file named as a dir
                               ("a.py::test_s::test_x", "a/test_s.py::test_x"),                # a dir named as a file
                               ("pkg/test_s.py::test_x", "pkg.py::test_x")):                   # a dir as the file
            status, reason = _primary(pt(node), expected)
            self.assertEqual(status, rmv.REFUSED, (node, expected, reason))
            self.assertIn("the run is red but not at", reason, (node, expected))
        # controls: an expectation that says no more than the run still matches it
        for expected in ("test_s.py::TestOther::test_x", "TestOther::test_x", "TestOther.test_x", "test_x",
                         "test_s.TestOther.test_x"):
            self.assertEqual(rmv.verdict(pt("test_s.py::TestOther::test_x"), expected)[0], rmv.PASS, expected)
        self.assertEqual(rmv.verdict(pt("q/dir.py::TestOther::test_x"), "q/dir.py::TestOther::test_x")[0], rmv.PASS)

    def test_r10_a1_a_file_qualified_expectation_never_matches_an_untyped_unittest_id(self) -> None:
        # a unittest header prints `module.Class.method`: where the module ends is not in the text, so a FILE-qualified
        # expectation is refused (fail closed), while the dotted forms still match
        ut = ("=" * 70 + "\nFAIL: test_x (test_s.TestOther.test_x)\n" + "-" * 70 + "\n"
              "Traceback (most recent call last):\n  File \"test_s.py\", line 4, in test_x\n"
              "    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n\n" + "-" * 70
              + "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
        self.assertEqual(_primary(ut, "TestOther.py::test_x")[0], rmv.REFUSED)
        for expected in ("test_s.TestOther.test_x", "TestOther.test_x", "test_x", "test_s.py:4"):
            self.assertEqual(rmv.verdict(ut, expected)[0], rmv.PASS, expected)

    def test_r10_a1p2_a_parameter_id_holding_a_message_separator_with_the_message_dropped(self) -> None:
        # adversary pass 2 A1 (07087c00): at width 80 pytest prints the node id alone; its parameter id holds `] - `
        x = "x" * 60
        line = f"FAILED t.py::test_x[] - AssertionError: {x}]"
        out = f"=== short test summary info ===\n{line}\n=== 1 failed in 0.1s ===\n"
        self.assertEqual(_primary(out, "t.py::test_x[]")[0], rmv.REFUSED)
        sec = "=== FAILURES ===\n___ test_x[] - AssertionError: " + x + "] ___\nE TypeError: boom\nt.py:2: TypeError\n"
        self.assertEqual(_primary(sec + out, "t.py::test_x[]")[0], rmv.REFUSED)  # with the TypeError section too
        # control: an ordinary parametrized failure with its message is still judged
        ok = "=== short test summary info ===\nFAILED t.py::test_x[1] - assert False\n=== 1 failed in 0.1s ===\n"
        self.assertEqual(rmv.verdict(ok, "t.py::test_x[1]")[0], rmv.PASS)

    def test_r10_cli_still_prints_the_whole_text(self) -> None:
        malformed = ("=== short test summary info ===\nFAILED t.py::test_x - assert False\n"
                     "=== 1 failed, 1 passed in 0.1s ===\n")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "red.txt")
            out.write_text(malformed, encoding="utf-8", newline="")
            proc = subprocess.run([sys.executable, str(Path(rmv.__file__)), "verdict", "--output", str(out),
                                   "--expect", "other.py::test_x"], text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "REFUSED " + rmv.verdict(malformed, "other.py::test_x")[1])
        self.assertIn("; also: the run is red but not at", proc.stdout)

    def test_r9_the_cli_refuses_a_saved_file_with_a_vertical_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "red.txt")
            out.write_text(self.UT_GREEN.replace("\n", "\x0b"), encoding="utf-8", newline="")
            proc = subprocess.run([sys.executable, str(Path(rmv.__file__)), "verdict", "--output", str(out),
                                   "--expect", "t.py:2"], text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("COULD-NOT-CHECK", proc.stdout)
        self.assertIn("U+000B", proc.stdout)

    # ---- r9 adversary pass 1 (e1bde592) ------------------------------------------------------------------------
    PT_RED = "=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n=== 1 failed in 0.1s ===\n"

    def _cli(self, data: bytes, *output: str) -> tuple[int, str]:
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "red.txt")
            path.write_bytes(data)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = rmv.main(["verdict", "--output", output[0] if output else str(path), "--expect", "t.py:2"])
        return code, buf.getvalue()

    def test_r9_adv_a1_the_cli_does_not_translate_a_lone_cr_before_the_framing(self) -> None:
        # Path.read_text() reads with universal newlines, so a lone CR became LF before _frame could see it.
        code, printed = self._cli(self.PT_RED.replace("\n", "\r").encode("utf-8"))
        self.assertEqual(code, 1, printed)
        self.assertIn("COULD-NOT-CHECK", printed)
        self.assertIn("U+000D", printed)
        for eol in ("\n", "\r\n"):  # controls: LF and CRLF files still read as the run they hold
            with self.subTest(eol=repr(eol)):
                code, printed = self._cli(self.PT_RED.replace("\n", eol).encode("utf-8"))
                self.assertEqual(code, 0, printed)

    def test_r9_adv_a2_an_output_path_with_a_nul_is_the_typed_exit_2(self) -> None:
        code, printed = self._cli(b"", "x\x00y")
        self.assertEqual(code, 2, printed)
        self.assertIn("cannot read", printed)
        self.assertNotIn("Traceback", printed)

    # ---- r9 adversary pass 2 (d0ab2372) ------------------------------------------------------------------------
    UT_BLOCK = ("=" * 70 + "\nFAIL: test_a (t.T.test_a)\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
                '  File "t.py", line 8, in test_a\nAssertionError: bad\n\nRan 1 test in 0.1s\n\n')
    PT_SECTION = "=== FAILURES ===\n___ test_x ___\nE assert False\nt.py:2: AssertionError\n"

    def test_r9_adv2_a1_a_trailer_of_the_other_runner_is_could_not_check(self) -> None:
        # One run has one runner: its terminal result must reconcile EVERY failure that was read, so failures read by
        # one parser under the other runner's trailer cannot be counted at all.
        for n in ("0", "1"):
            with self.subTest(case=f"unittest blocks under a pytest bar of {n}"):
                self._could_not_check(self.UT_BLOCK + f"=== {n} failed in 0.1s ===\n", "t.py:8", "runner")
            with self.subTest(case=f"pytest section under a unittest trailer of {n}"):
                self._could_not_check(self.PT_SECTION + f"Ran 1 test in 0.1s\n\nFAILED (failures={n})\n", "t.py:2", "runner")
        both = self.UT_BLOCK.replace("Ran 1 test in 0.1s\n\n", "") + self.PT_SECTION + "Ran 1 test in 0.1s\n\nFAILED (failures=1)\n"
        self._could_not_check(both, "t.py:8", "runner")
        self.assertEqual(rmv.verdict(self.UT_BLOCK + "FAILED (failures=1)\n", "t.py:8")[0], rmv.PASS)  # controls
        self.assertEqual(rmv.verdict(self.PT_SECTION + "=== 1 failed in 0.1s ===\n", "t.py:2")[0], rmv.PASS)

    def test_r9_adv2_a2_a_lossy_decode_never_invents_a_matching_name(self) -> None:
        raw = (b"F\n" + b"=" * 70 + b"\nFAIL: test_x (test_m.T.test_x)\n" + b"-" * 70
               + b'\nTraceback (most recent call last):\n  File "test_\xe9.py", line 4, in test_x\nAssertionError: 1 != 2\n\n'
               + b"-" * 70 + b"\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")  # cp1252 bytes: not UTF-8
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "red.txt")
            path.write_bytes(raw)
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = rmv.main(["verdict", "--output", str(path), "--expect", "test_\ufffd.py:4"])
        self.assertEqual(code, 2, buf.getvalue())
        self.assertIn("cannot read", buf.getvalue())
        lossy = raw.decode("utf-8", errors="replace")  # a text already decoded with replacement is refused too
        self._could_not_check(lossy, "test_\ufffd.py:4", "U+FFFD")
        self.assertEqual(rmv.verdict(raw.decode("cp1252"), "test_\xe9.py:4")[0], rmv.PASS)  # the true name still reads
        with self.assertRaises(UnicodeDecodeError):  # sibling: the real-run helpers decode strictly
            _raw_text(subprocess.CompletedProcess([], 1, raw, b""))

    def test_r9_adv_a1_sibling_the_real_run_helpers_keep_the_runner_line_ends(self) -> None:
        # The helpers hand verdict() exactly the text a saved file holds, so the real-run tests go through _frame with
        # the runner's own line ends (CRLF on Windows), never through a newline translation.
        out = _run_pytest({"test_t.py": "def test_x():\n    assert False\n"})
        self.assertEqual("\r\n" in out, sys.platform == "win32", out[-200:])
        self.assertEqual(rmv.verdict(out, "test_t.py:2")[0], rmv.PASS, out)


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


    def test_r8_window_must_be_a_list_or_tuple_of_two_numbers(self) -> None:
        # opus r7 RMV-PROBE-WINDOW-PAIR-1: a dict's keys and a two-character string unpacked as the bounds.
        for window in ({"0": 5, "1": 6}, "01", {0, 1}, iter((0, 1))):
            ok, reason = rmv.probe_discriminates(3, 0, window)
            self.assertFalse(ok, (window, reason))
            self.assertIn("COULD-NOT-CHECK", reason)
        self.assertTrue(rmv.probe_discriminates(3, 0, [0, 1])[0])
        self.assertTrue(rmv.probe_discriminates(3, 0, (0, 1))[0])

    def test_r8_pr705_measured_readings_are_the_ones_the_fixtures_use(self) -> None:
        # powershell.exe 5.1.19041.6456, sol r1 (PR comment 6680026607): the Int32 overload reads 2 for the 2.2 s probe and
        # 3 for the 2.8 s probe; the double overload reads the probe itself. Every PR #705 reading in the corpus and in
        # the guard fixtures must be one of these pairs (the guard tests pin the fixtures, this pins the arithmetic).
        for probe, bug, fix, window, want in ((2.2, 2, 2.2, (2.0, 2.5), False), (2.8, 3, 2.8, (2.7, 2.9), True)):
            self.assertEqual(rmv.probe_discriminates(bug, fix, window)[0], want, probe)

    def test_each_failure_shape_is_named(self) -> None:
        self.assertIn("neither", rmv.probe_discriminates(30, 40, (0, 2))[1])
        self.assertIn("only the bug", rmv.probe_discriminates(1, 9, (0, 5))[1])
        self.assertIn("empty window", rmv.probe_discriminates(1, 2, (5, 1))[1])
        self.assertIn("NaN", rmv.probe_discriminates(float("nan"), 2, (0, 5))[1])
        self.assertIn("numbers", rmv.probe_discriminates("a", 2, (0, 5))[1])
        self.assertIn("numbers", rmv.probe_discriminates(10 ** 400, 2, (0, 5))[1])  # adversary A5: OverflowError

    def test_window_bounds_are_inclusive(self) -> None:
        self.assertTrue(rmv.probe_discriminates(3, 2.5, (2.0, 2.5))[0])


def _ut_text(trailer: str, frame: str = '  File "t.py", line 8, in test_a', ran: str = "Ran 1 test in 0.001s",
             eol: str = "\n") -> str:
    """One readable unittest FAIL block (an assertion at t.py:8 by default), its footer and `trailer`."""
    text = ("=" * 70 + "\nFAIL: test_a (t.T.test_a)\n" + "-" * 70 + "\nTraceback (most recent call last):\n" + frame
            + "\n    self.assertEqual(3, 4)\nAssertionError: 3 != 4\n\n" + "-" * 70 + "\n" + ran + "\n\n" + trailer + "\n")
    return text.replace("\n", eol)


def _pt_text(bar: str, before: str = "", eol: str = "\n") -> str:
    """A pytest short summary with one FAILED line (t.py::test_x, an assertion), `before` lines ahead of it, and `bar`."""
    text = "=== short test summary info ===\n" + before + "FAILED t.py::test_x - assert False\n" + bar + "\n"
    return text.replace("\n", eol)


_PT_SECTION_TEXT = ("=================================== FAILURES ===================================\n"
                    "____________________________________ test_x ____________________________________\n\n"
                    "    def test_x():\n>       assert False\nE       assert False\n\n{loc}\n"
                    "=========================== short test summary info ============================\n"
                    "FAILED t.py::test_x - assert False\n"
                    "============================== 1 failed in 0.12s ===============================\n")


class RunnerMetadataR11(unittest.TestCase):
    """r11, Dot r9-A1 (bus 7cf8484): a runner count field and a traceback line number are read WHOLE. Each malformed
    value below holds a digit substring EQUAL to the real block/summary count, so a count-mismatch check cannot mask it:
    only parsing the whole field refuses it. The primary reason (classify().code, r11 r10-A2) is asserted, never text."""

    UT_MALFORMED = ("FAILED (failures=1.5)", "FAILED (failures=1.0)", "FAILED (failures=+1)", "FAILED (failures=01)",
                    "FAILED (failures=1e0)", "FAILED (failures=1x)", "FAILED (failures=١)",
                    "FAILED (failures=1, errors=0.0)", "FAILED (failures=1, errors=-0)", "FAILED (failures=1,errors=0)",
                    "FAILED (failures=1, bogus=2)", "FAILED (failures=1, failures=0)", "FAILED (failures=1, skipped=1.5)",
                    "FAILED (failures=1, expected failures=0.5)", "FAILED (failures=1, unexpected successes=x)")
    PT_MALFORMED = ("=== -1 failed in 0.1s ===", "=== +1 failed in 0.1s ===", "=== 0.1 failed in 0.1s ===",
                    "=== 1.1 failed in 0.1s ===", "=== 01 failed in 0.1s ===", "=== ١ failed in 0.1s ===",
                    "=== 1 failed, 0.0 errors in 0.1s ===", "=== 1 failed, -0 errors in 0.1s ===",
                    "=== 1 failed, 1 bogus in 0.1s ===", "=== 1 failed, 0 failed in 0.1s ===",
                    "=== 1 failed in 0.1.2s ===", "=== 1 failed in 0.1s extra ===", "=== 1 failed, in 0.1s ===")
    # every OTHER outcome count, each beside one summary line of its kind (so the extracted substring equals the tally)
    PT_OUTCOME_MALFORMED = (("PASSED t.py::test_p\n", "=== 1 failed, 0.1 passed in 0.1s ==="),
                            ("PASSED t.py::test_p\n", "=== 1 failed, -1 passed in 0.1s ==="),
                            ("SKIPPED [1] t.py:3: why\n", "=== 1 failed, 0.1 skipped in 0.1s ==="),
                            ("XFAIL t.py::test_f\n", "=== 1 failed, 0.1 xfailed in 0.1s ==="),
                            ("XPASS t.py::test_z\n", "=== 1 failed, 0.1 xpassed in 0.1s ==="))

    def _code(self, text: str, expected: str) -> str:
        v = rmv.classify(text, expected)
        self.assertEqual(v.status, rmv.REFUSED, (text, v))
        return v.code

    def test_runner_metadata_requires_complete_integer_fields_unittest(self) -> None:
        self.assertEqual(rmv.classify(_ut_text("FAILED (failures=1)"), "t.py:8").status, rmv.PASS)  # the control
        for trailer in self.UT_MALFORMED:
            with self.subTest(trailer=trailer):
                self.assertEqual(self._code(_ut_text(trailer), "t.py:8"), "cnc-result-line")
        for trailer in ("OK (skipped=1.5)", "OK (failures=1)", "OK (skipped=01)"):  # a green trailer is parsed whole too
            with self.subTest(trailer=trailer):
                self.assertEqual(self._code("Ran 2 tests in 0.001s\n\n" + trailer + "\n", "t.py:8"), "cnc-result-line")
        for ran in ("Ran 1.5 tests in 0.001s", "Ran -1 tests in 0.001s", "Ran 1 test in 0.0.1s"):  # the footer, too
            with self.subTest(ran=ran):
                self.assertEqual(self._code(_ut_text("FAILED (failures=1)", ran=ran), "t.py:8"), "cnc-no-ran-footer")

    def test_runner_metadata_requires_complete_integer_fields_pytest(self) -> None:
        self.assertEqual(rmv.classify(_pt_text("=== 1 failed in 0.1s ==="), "t.py::test_x").status, rmv.PASS)
        for bar in self.PT_MALFORMED:
            with self.subTest(bar=bar):
                self.assertEqual(self._code(_pt_text(bar), "t.py::test_x"), "cnc-result-line")
        for before, bar in self.PT_OUTCOME_MALFORMED:
            with self.subTest(bar=bar):
                self.assertEqual(self._code(_pt_text(bar, before), "t.py::test_x"), "cnc-result-line")

    def test_runner_metadata_requires_complete_integer_fields_controls(self) -> None:
        # genuine integer counts (zero, one, several), decimal elapsed times, every outcome word, LF and CRLF
        for eol in ("\n", "\r\n"):
            for trailer, n_blocks in (("FAILED (failures=1)", 1), ("FAILED (failures=1, skipped=2)", 1),
                                      ("FAILED (failures=1, errors=0)", 1),
                                      ("FAILED (failures=1, skipped=1, expected failures=3, unexpected successes=10)", 1)):
                with self.subTest(trailer=trailer, eol=eol):
                    self.assertEqual(rmv.classify(_ut_text(trailer, eol=eol), "t.py:8").status, rmv.PASS)
            for bar, before in (("=== 1 failed in 0.1s ===", ""), ("=== 1 failed in 0.10s ===", ""),
                                ("=== 1 failed in 75.12s (0:01:15) ===", ""), ("=== 1 failed, 0 errors in 3s ===", ""),
                                ("=== 1 failed, 2 passed, 1 warning in 0.52s ===", "PASSED t.py::test_p\nPASSED t.py::test_q\n"),
                                ("=== 1 failed, 1 skipped, 1 deselected, 12 warnings in 1.51s ===", "SKIPPED [1] t.py:3: why\n"),
                                ("=== 1 failed, 1 xfailed, 1 xpassed in 0.1s ===", "XFAIL t.py::test_f\nXPASS t.py::test_z\n")):
                with self.subTest(bar=bar, eol=eol):
                    self.assertEqual(rmv.classify(_pt_text(bar, before, eol), "t.py::test_x").status, rmv.PASS)
            # a stated count that disagrees is still refused for the COUNT, not for its spelling
            self.assertEqual(rmv.classify(_ut_text("FAILED (failures=2)", eol=eol), "t.py:8").code, "cnc-count")
            self.assertEqual(rmv.classify(_pt_text("=== 2 failed in 0.1s ===", eol=eol), "t.py::test_x").code,
                             "cnc-short-summary")
        for green in ("Ran 2 tests in 0.001s\n\nOK\n", "Ran 2 tests in 0.001s\n\nOK (skipped=1)\n",
                      "Ran 3 tests in 0.001s\n\nOK (skipped=1, expected failures=1)\n",
                      "=== short test summary info ===\nPASSED t.py::test_p\n=== 1 passed in 0.10s ===\n"):
            with self.subTest(green=green):
                self.assertEqual(rmv.classify(green, "t.py:8").code, "final-green")

    def test_unittest_frame_line_requires_complete_integer_token(self) -> None:
        for frame in ('  File "t.py", line 2.5, in test_a', '  File "t.py", line 2x, in test_a',
                      '  File "t.py", line 2,in test_a', '  File "t.py", line 2 , in test_a',
                      '  File "t.py", line ٢, in test_a', '  File "t.py", line +2, in test_a',
                      '  File "t.py", line 2;'):
            with self.subTest(frame=frame):
                self.assertEqual(self._code(_ut_text("FAILED (failures=1)", frame=frame), "t.py:2"), "cnc-frame")
        for eol in ("\n", "\r\n"):  # controls: a real frame, a frame with no function (as a SyntaxError prints it)
            for frame in ('  File "t.py", line 2, in test_a', '  File "t.py", line 2'):
                with self.subTest(frame=frame, eol=eol):
                    self.assertEqual(rmv.classify(_ut_text("FAILED (failures=1)", frame=frame, eol=eol), "t.py:2").status,
                                     rmv.PASS)

    def test_r11_pass2_a1_frame_line_with_two_readings_is_could_not_check(self) -> None:
        # adversary r11 pass 2 A1 (75aecf85): a co_name `test_x "/victim.py", line 2, in target` made the frame of real.py:3
        # read as victim.py:2; the pytest sibling is a directory named `victim.py:2: sub`. Both PASSed at e2d9dc08 too.
        frame = '  File "real.py", line 3, in test_x "/victim.py", line 2, in target'
        for want in ("victim.py:2", "real.py:3"):
            self.assertEqual(self._code(_ut_text("FAILED (failures=1)", frame=frame), want), "cnc-frame")
        pt = ("=== FAILURES ===\n___ test_x ___\nE assert False\nvictim.py:2: sub/real.py:3: AssertionError\n"
              "=== short test summary info ===\nFAILED victim.py:2: sub/real.py::test_x - assert False\n"
              "=== 1 failed in 0.1s ===\n")
        self.assertEqual(self._code(pt, "victim.py:2"), "cnc-frame")
        # controls: one frame per line, and a location line with one `<file>.py:N:` (its rest may name a function)
        self.assertEqual(rmv.classify(_ut_text("FAILED (failures=1)"), "t.py:8").status, rmv.PASS)
        self.assertEqual(rmv.classify(_PT_SECTION_TEXT.format(loc="t.py:2: in helper"), "t.py:2").status, rmv.PASS)

    def test_r11_pass2_a3_file_line_expectation_with_an_empty_segment_is_refused(self) -> None:
        # adversary r11 pass 2 A3 (75aecf85): `a//t.py:2` skipped the empty-segment check and _norm_path dropped the gap
        out = "=== FAILURES ===\n___ test_x ___\nE assert False\na/t.py:2: AssertionError\n=== 1 failed in 0.1s ===\n"
        for want in ("a//t.py:2", "a\\\\t.py:2", "a/./t.py:2x"):
            with self.subTest(want=want):
                self.assertEqual(rmv.classify(out, want).status, rmv.REFUSED)
        for want in ("a//t.py:2", "a\\\\t.py:2"):
            self.assertEqual(rmv.classify(out, want).code, "cnc-expectation")
        self.assertEqual(rmv.classify(out, "a/t.py:2").status, rmv.PASS)  # control
        ut = _ut_text("FAILED (failures=1)", frame='  File "a/t.py", line 8, in test_a')
        self.assertEqual(rmv.classify(ut, "a//t.py:8").code, "cnc-expectation")
        self.assertEqual(rmv.classify(ut, "a/t.py:8").status, rmv.PASS)

    def test_frame_delimiter_count_only_on_frame_lines(self) -> None:
        # r12 (Dot e3f43d7, request 6711763476): the `", line ` count ran on EVERY indented line, so a source line that
        # holds the text was refused as cnc-frame before it was known to be a frame. It runs on frame lines only.
        for src in ("""    self.fail('", line 1, ", line 2')""", """    x = '", line 1' + '", line 2'"""):
            with self.subTest(src=src):
                ut = _ut_text("FAILED (failures=1)").replace("    self.assertEqual(3, 4)", src)
                v = rmv.classify(ut, "t.py:8")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)
        # the r11 pass 2 A1 co_name witness is a FRAME line with two readings: still refused
        frame = '  File "real.py", line 3, in test_x "/victim.py", line 2, in target'
        self.assertEqual(self._code(_ut_text("FAILED (failures=1)", frame=frame), "victim.py:2"), "cnc-frame")

    A3_SOURCE = ('import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(\n'
                 '            """a\nFile "victim.py", line 2\nb""", 1)\n')

    def test_r11_pass3_a3_source_line_is_never_a_frame(self) -> None:
        # adversary r11 pass 3 A3 (071fa1a7): Python prints a frame's source lines four spaces in (3.11+ prints every line
        # of a multi-line call), so the string text `File "victim.py", line 2` printed under the real t.py frame was read
        # as a second frame and victim.py:2 passed. A frame is the two-space `  File "...", line N` line only.
        text = ("=" * 70 + "\nFAIL: test_x (t.T.test_x)\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
                '  File "t.py", line 4, in test_x\n            self.assertEqual(\n            ~~~~~~~~~~~~~~~~^\n'
                '                """a\n                ^^^^\n    File "victim.py", line 2\n'
                "    ^^^^^^^^^^^^^^^^^^^^^^^^\n    b\"\"\", 1)\n    ^^^^^^^^\n"
                "AssertionError: 'a\\nFile \"victim.py\", line 2\\nb' != 1\n\n" + "-" * 70
                + "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
        for eol in ("\n", "\r\n"):
            with self.subTest(eol=eol):
                v = rmv.classify(text.replace("\n", eol), "victim.py:2")
                self.assertEqual((v.status, v.code), (rmv.REFUSED, "not-at"), v)
                self.assertEqual(rmv.classify(text.replace("\n", eol), "t.py:4").status, rmv.PASS)  # the real frame
        # the source line holding a malformed frame shape is source too, never a cnc-frame refusal
        self.assertEqual(rmv.classify(text.replace('    File "victim.py", line 2\n', '    File "v.py", line 2.5,\n'),
                                      "t.py:4").status, rmv.PASS)
        real = _run_unittest({"t.py": self.A3_SOURCE}, "t")  # the real runner's own text
        self.assertEqual(rmv.classify(real, "victim.py:2").status, rmv.REFUSED)
        self.assertEqual(rmv.classify(real, "t.py:4").status, rmv.PASS)

    def test_pytest_location_line_requires_complete_integer_token(self) -> None:
        # the same rule for the pytest `file.py:N:` location line (a sibling of _FRAME)
        self.assertEqual(rmv.classify(_PT_SECTION_TEXT.format(loc="t.py:2: AssertionError"), "t.py:2").status, rmv.PASS)
        for loc in ("t.py:2.5: AssertionError", "t.py:٢: AssertionError", "t.py:2x: AssertionError"):
            with self.subTest(loc=loc):
                self.assertEqual(self._code(_PT_SECTION_TEXT.format(loc=loc), "t.py:2"), "cnc-frame")
        # an expectation is read whole too: a non-ASCII digit is not a line number
        self.assertEqual(rmv.classify(_PT_SECTION_TEXT.format(loc="t.py:2: AssertionError"), "t.py:٢").status,
                         rmv.REFUSED)


class SummarySplitR11(unittest.TestCase):
    """r11, r10 adversary A1 (e2d9dc08): a FAILED line whose node/message boundary is ambiguous is COULD-NOT-CHECK."""

    def _run(self, line: str) -> str:
        return "=== short test summary info ===\n" + line + "\n=== 1 failed in 0.1s ===\n"

    def test_ambiguous_node_message_split_is_could_not_check(self) -> None:
        # The witness: globals()['TestA - AssertionError'] = C, whose ordinary test_x raises TypeError('boom').
        witness = self._run("FAILED t.py::TestA - AssertionError::test_x - TypeError: boom")
        self.assertEqual(rmv.classify(witness, "t.py::TestA").code, "cnc-summary-split")
        self.assertEqual(rmv.classify(witness, "t.py::TestA").status, rmv.REFUSED)
        for line, want in (("FAILED t.py::test_a - AssertionError - TypeError: boom", "t.py::test_a"),
                           ("FAILED t.py::test_a - AssertionError: x - TypeError: boom", "t.py::test_a"),
                           ("FAILED t.py::test_a - AssertionError: x - Boom", "t.py::test_a"),
                           ("FAILED t.py::T - assert x::test_a - TypeError: boom", "t.py::T")):
            with self.subTest(line=line):
                v = rmv.classify(self._run(line), want)
                self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-summary-split"), v)
        # with the failure's own section: still refused (the adversary: a full TypeError section did not prevent it)
        section = ("=================================== FAILURES ===================================\n"
                   "___________________________ TestA - AssertionError.test_x ___________________________\n\n"
                   "E       TypeError: boom\n\nt.py:3: TypeError\n")
        self.assertEqual(rmv.classify(section + witness, "t.py::TestA").status, rmv.REFUSED)

    def test_assertion_head_needs_a_real_delimiter(self) -> None:
        # sibling :195/:489: `AssertionError::` is not an assertion (the `:` is the start of `::`)
        v = rmv.classify(self._run("FAILED t.py::test_a - AssertionError::x"), "t.py::test_a")
        self.assertEqual(v.status, rmv.REFUSED, v)
        ut = _ut_text("FAILED (failures=1)").replace("AssertionError: 3 != 4", "AssertionError:: 3 != 4")
        self.assertEqual(rmv.classify(ut, "t.py:8").status, rmv.REFUSED)

    @staticmethod
    def _with_section(name: str, line: str, e_line: str = "assert False") -> str:
        """The default-traceback run of `line`: pytest prints the failure's own section (its header names the test)."""
        return ("=================================== FAILURES ===================================\n"
                f"_____ {name} _____\n\n    def test():\n>       assert False\nE       {e_line}\n\nt.py:2: AssertionError\n"
                "=========================== short test summary info ============================\n"
                + line + "\n=========================== 1 failed in 0.12s ===========================\n")

    def test_ordinary_assertion_lines_stay_accepted(self) -> None:
        # one ' - ' (the boundary itself): readable from the summary line alone
        for line, want in (("FAILED t.py::test_x - AssertionError: boom", "t.py::test_x"),
                           ("FAILED t.py::test_x - AssertionError", "t.py::test_x"),
                           ("FAILED t.py::test_x[a - b] - assert 1 == 2", "t.py::test_x[a - b]")):
            with self.subTest(line=line):
                self.assertEqual(rmv.classify(self._run(line), want).status, rmv.PASS)
        # a ' - ' inside the message: readable when the failure's own section names exactly one of the readings
        for line in ("FAILED t.py::test_x - assert (1 - 2) == 0", "FAILED t.py::test_x - assert x - 1 == y",
                     "FAILED t.py::test_x - assert x[0] - 1 == 2", "FAILED t.py::test_x - AssertionError: 3 - 4 != 0",
                     "FAILED t.py::test_x - AssertionError: a - b"):
            with self.subTest(line=line):
                self.assertEqual(rmv.classify(self._with_section("test_x", line), "t.py::test_x").status, rmv.PASS)
                self.assertEqual(rmv.classify(self._with_section("test_x", line), "t.py:2").status, rmv.PASS)
                # with --tb=no nothing names the node: two readings, COULD-NOT-CHECK (fail closed)
                self.assertEqual(rmv.classify(self._run(line), "t.py::test_x").code, "cnc-summary-split")

    def test_r11_pass1_a1_any_class_name_is_a_reading(self) -> None:
        # adversary r11 pass 1 A1 (ab16d960): type('Bad-Name', (ImportError,), {'__module__': 't'}) prints `t.Bad-Name`,
        # which no identifier grammar accepts, so the later split was missed and the invented node t.py::TestA passed.
        line = "FAILED t.py::TestA - AssertionError: x::test_x - t.Bad-Name: boom"
        v = rmv.classify(self._run(line), "t.py::TestA")
        self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-summary-split"), v)
        for odd in ("t.a b: boom", "1x: boom", "t.<lambda>: boom", "t.Bad-Name"):
            with self.subTest(odd=odd):
                v = rmv.classify(self._run(f"FAILED t.py::test_a - AssertionError: x - {odd}"), "t.py::test_a")
                self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-summary-split"), v)
        # the failure section names the REAL node, so the split is told and the real exception is judged
        sec = self._with_section("TestA - AssertionError: x.test_x", line, e_line="t.Bad-Name: boom")
        v = rmv.classify(sec, "t.py::TestA")
        self.assertEqual(v.status, rmv.REFUSED, v)
        self.assertEqual(rmv.classify(sec, "t.py:2").code, "cnc-exception-type")

    def test_r11_pass1_a2_assert_head_needs_its_delimiter(self) -> None:
        # adversary r11 pass 1 A2 (ab16d960): `assert\\b` matched `assert.MissingDependency` (a class in a module named
        # assert, subclassing ImportError). Summary-only, full-section and -rN paths all returned PASS.
        summary = self._run("FAILED t.py::test_x - assert.MissingDependency: boom")
        self.assertEqual(rmv.classify(summary, "t.py::test_x").status, rmv.REFUSED)
        section = ("=== FAILURES ===\n___ test_x ___\nE assert.MissingDependency: boom\nt.py:2: MissingDependency\n")
        for tail in ("=== short test summary info ===\nFAILED t.py::test_x - assert.MissingDependency: boom\n"
                     "=== 1 failed in 0.1s ===\n", "=== 1 failed in 0.1s ===\n"):
            with self.subTest(tail=tail):
                self.assertEqual(rmv.classify(section + tail, "t.py:2").status, rmv.REFUSED)
        for head in ("AssertionError:x: boom", "Failed::x", "TypeError:x: boom"):  # the same delimiter, siblings
            with self.subTest(head=head):
                self.assertEqual(rmv.classify(self._run(f"FAILED t.py::test_x - {head}"), "t.py::test_x").status,
                                 rmv.REFUSED)
        for ok in ("assert False", "assert 1 == 2", "AssertionError: boom", "AssertionError", "Failed: no"):  # controls
            with self.subTest(ok=ok):
                self.assertEqual(rmv.classify(self._run(f"FAILED t.py::test_x - {ok}"), "t.py::test_x").status, rmv.PASS)

    # r12 A1-U (Dot e3f43d7, request 6711763476; the same defect as r11 pass 3 A2): a later ' - ' whose left side is a node
    # id but for a non-ASCII or control character was DROPPED, so the invented ASCII prefix was the only reading.
    UNI_LINE = "FAILED t.py::TestA - AssertionError: x::test_{c} - TypeError: boom"
    UNI_SECTION = ("=================================== FAILURES ===================================\n"
                   "_____ TestA - AssertionError: x.test_{c} _____\n\n    def test_{c}(self):\n"
                   ">       raise TypeError('boom')\nE       TypeError: boom\n\nt.py:3: TypeError\n"
                   "=========================== short test summary info ============================\n")

    def test_unicode_later_boundary_is_could_not_check(self) -> None:
        for c in ("é", "​", "\t", "éé"):
            with self.subTest(c=repr(c)):
                # (a) Dot's summary-only witness, (b) the same with its full TypeError section
                for text in (self._run(self.UNI_LINE.format(c=c)),
                             self.UNI_SECTION.format(c=c) + self.UNI_LINE.format(c=c) + "\n=== 1 failed in 0.12s ===\n"):
                    v = rmv.classify(text, "t.py::TestA")
                    self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-short-summary"), v)
                    self.assertEqual(rmv.classify(text, "t.py:3").code, "cnc-short-summary")
                # (c) the ERROR-line variant: refused for the unreadable id, before any collection guess
                err = ("=== short test summary info ===\n" + self.UNI_LINE.format(c=c).replace("FAILED", "ERROR", 1)
                       + "\n=== 1 error in 0.1s ===\n")
                v = rmv.classify(err, "t.py::TestA")
                self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-short-summary"), v)
        # ASCII CONTROLS keep the r11 verdicts: the ASCII twin (summary only, and with the section that names its node)
        twin = self.UNI_LINE.format(c="e")
        v = rmv.classify(self._run(twin), "t.py::TestA")
        self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-summary-split"), v)
        twin_sec = self.UNI_SECTION.format(c="e") + twin + "\n=== 1 failed in 0.12s ===\n"
        self.assertEqual((rmv.classify(twin_sec, "t.py::TestA").status, rmv.classify(twin_sec, "t.py::TestA").code),
                         (rmv.REFUSED, "not-at"))
        self.assertEqual(rmv.classify(twin_sec, "t.py:3").code, "non-assertion")
        v = rmv.classify(self._run("FAILED t.py::test_a - AssertionError: boom"), "t.py::test_a")
        self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)
        # a Unicode MESSAGE after one ASCII node id stays readable (the id is ASCII; only the message is not)
        for msg in ("AssertionError: café", "assert 'é' == 'e'", "AssertionError: é::x"):
            with self.subTest(msg=msg):
                v = rmv.classify(self._run(f"FAILED t.py::test_a - {msg}"), "t.py::test_a")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)

    def test_r11_pass3_a1_section_binds_a_reading_only_in_its_own_file(self) -> None:
        # adversary r11 pass 3 A1 (071fa1a7): u.py's section `TestA.test_target` named t.py's shorter reading, so the
        # invented node t.py::TestA::test_target passed. A section tells a reading only when its frames are in that file.
        out = ("=== FAILURES ===\n___ TestA::test_target - AssertionError: x::part.test_x ___\nE TypeError: boom\n"
               "t.py:2: TypeError\n___ TestA.test_target ___\nE AssertionError\nu.py:3: AssertionError\n"
               "=== short test summary info ===\n"
               "FAILED t.py::TestA::test_target - AssertionError: x::part::test_x - TypeError: boom\n"
               "FAILED u.py::TestA::test_target - AssertionError\n=== 2 failed in 0.04s ===\n")
        v = rmv.classify(out, "t.py::TestA::test_target")
        self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-summary-split"), v)
        # control: a section whose frame is in the reading's own file still tells it; the same name in u.py does not
        one = ("=== FAILURES ===\n___ TestA.test_target ___\nE AssertionError: x - y\nt.py:3: AssertionError\n"
               "=== short test summary info ===\nFAILED t.py::TestA::test_target - AssertionError: x - y\n"
               "=== 1 failed in 0.04s ===\n")
        self.assertEqual(rmv.classify(one, "t.py::TestA::test_target").status, rmv.PASS)
        self.assertEqual(rmv.classify(one.replace("t.py:3:", "u.py:3:"), "t.py::TestA::test_target").code,
                         "cnc-summary-split")

    def test_r12_pass1_a1_a_section_tells_by_its_first_frame_and_one_line_only(self) -> None:
        # adversary r12 pass 1 A1 (d1ff9699): u.py's section also held a helper frame in t.py, and "any frame in the file"
        # let it tell t.py's invented reading. The test's own frame is the FIRST; and an inherited test (its first frame
        # the base class in t.py) is the account of u.py's own FAILED line, so a name another line prints tells nothing.
        head = ("=== FAILURES ===\n___ TestA::test_target - AssertionError: x::part.test_x ___\nE TypeError: boom\n"
                "t.py:2: TypeError\n___ TestA.test_target ___\nE AssertionError\n")
        tail = ("=== short test summary info ===\n"
                "FAILED t.py::TestA::test_target - AssertionError: x::part::test_x - TypeError: boom\n"
                "FAILED u.py::TestA::test_target - AssertionError\n=== 2 failed in 0.04s ===\n")
        for frames in ("u.py:3:\nt.py:8: AssertionError\n", "t.py:8: AssertionError\n"):
            with self.subTest(frames=frames):
                v = rmv.classify(head + frames + tail, "t.py::TestA::test_target")
                self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-summary-split"), v)
        # controls: one line told by its own section stays readable, and so does a second line of another test name
        one = ("=== FAILURES ===\n___ TestA.test_target ___\nE AssertionError: x - y\nt.py:3: AssertionError\n"
               "___ test_b ___\nE assert False\nt.py:9: AssertionError\n=== short test summary info ===\n"
               "FAILED t.py::TestA::test_target - AssertionError: x - y\nFAILED t.py::test_b - assert False\n"
               "=== 2 failed in 0.04s ===\n")
        self.assertEqual(rmv.classify(one, "t.py::TestA::test_target").status, rmv.PASS)
        self.assertEqual(rmv.classify(one, "t.py::test_b").status, rmv.PASS)

    def test_r12_pass2_a1_a_bracket_in_a_class_name_keeps_every_later_reading(self) -> None:
        # adversary r12 pass 2 A1 (c62bbb01): a `[` in a nested class name (`test_target[x] - AssertionError: x`) sent the
        # line down the parameter-id branch, which kept only cuts after a `]` and dropped the real one before TypeError.
        for c in ("x", "é"):
            line = f"FAILED t.py::TestA::test_target[x] - AssertionError: x::test_{c} - TypeError: boom"
            section = ("=== FAILURES ===\n" f"___ TestA.test_target[x] - AssertionError: x.test_{c} ___\n"
                       "E       TypeError: boom\nt.py:3: TypeError\n")
            for text in (f"=== short test summary info ===\nPASSED t.py::TestA::test_target[x]\n{line}\n"
                         "=== 1 failed, 1 passed in 0.1s ===\n",
                         f"=== short test summary info ===\n{line}\n=== 1 failed in 0.1s ===\n",
                         f"{section}=== short test summary info ===\n{line}\n=== 1 failed in 0.1s ===\n"):
                with self.subTest(c=repr(c), text=text[:40]):
                    v = rmv.classify(text, "t.py::TestA::test_target[x]")
                    want_code = "cnc-summary-split" if c.isascii() else "cnc-short-summary"
                    self.assertEqual((v.status, v.code), (rmv.REFUSED, want_code), v)
        # controls: a parameter id holding ' - ', one cut after its `]`, stays readable
        for line, want in (("FAILED t.py::test_x[a - b] - assert 1 == 2", "t.py::test_x[a - b]"),
                           ("FAILED t.py::test_x[p] - AssertionError: boom", "t.py::test_x[p]")):
            with self.subTest(line=line):
                v = rmv.classify(self._run(line), want)
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)
        # r13 HUB RULING: one holding '::' is no longer read (a `[` before the last `::`; `TestA[x.py::test_y]` reads alike)
        v = rmv.classify(self._run("FAILED t.py::T::test_x[a::b] - assert 1 == 2"), "t.py::T::test_x[a::b]")
        self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-bracket-before-last-segment"), v)

    def test_r12_pass1_a2_bare_failed_is_an_assertion_head(self) -> None:
        # adversary r12 pass 1 A2 (d1ff9699, minor): pytest.fail() with no message prints a bare `Failed`
        for msg in ("Failed", "Failed: why"):
            with self.subTest(msg=msg):
                v = rmv.classify(self._run(f"FAILED t.py::test_x - {msg}"), "t.py::test_x")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)
        for msg in ("Failed:x", "Failed::x", "FailedX", "Failed.Name: boom"):  # the delimiter rule still holds
            with self.subTest(msg=msg):
                self.assertEqual(rmv.classify(self._run(f"FAILED t.py::test_x - {msg}"), "t.py::test_x").status,
                                 rmv.REFUSED)

    BRACKET = "cnc-bracket-before-last-segment"

    def _bracket(self, text: str, expected: str) -> None:
        v = rmv.classify(text, expected)
        self.assertEqual((v.status, v.code), (rmv.REFUSED, self.BRACKET), v)

    def test_bracket_before_last_segment_is_could_not_check(self) -> None:
        # r13 HUB RULING (r12 adversary f2b4440f A1-A3, Dot 04ceb61): a class name set through globals() may hold `[`, so
        # the FIRST `[` is not the start of the parameter id. Any id with a `[` before its last `::` (a pytest node), or
        # before its last `.`/`/`/`\` (a dotted id, a path), is COULD-NOT-CHECK, on the expected side and the run side.
        # A2 (Dot's two-file witness): class `TestA[x].py` in t.py is not FILE TestA[x].py
        dot = self._run("FAILED t.py::TestA[x].py::test_x[p] - AssertionError: boom")
        for expected in ("TestA[x].py::test_x[p]", "TestA[x]/TestB.py::test_x[p]", "t.py::TestA[x].py::test_x[p]",
                         "test_x[p]"):  # the last one is a safe expectation: the RUN side refuses
            with self.subTest(a2=expected):
                self._bracket(dot, expected)
        safe = self._run("FAILED t.py::test_x[p] - AssertionError: boom")
        for expected in ("TestA[x].py::test_x[p]", "TestA[x]/TestB.py::test_x[p]", "t.py::TestB[x]::test_x::[p]",
                         "TestA[x.py::test_y]", "t.T[x]..Oops.test_a", "a[x]/t.py:2", "a[x]\\t.py:2"):
            with self.subTest(expected_side=expected):  # an unsafe expectation against a readable run
                self._bracket(safe, expected)
        # A2 sibling, an unbalanced `[` in a file or class name and a `]` in a function name: the same text as a
        # parameter id holding `::`, so a `::` inside the trailing brackets cannot be told from a name boundary either
        self._bracket(self._run("FAILED t.py::TestA[x.py::test_y] - AssertionError: boom"), "TestA[x.py::test_y]")
        # A3: an empty segment after the class bracket, run side (pytest FAILED and each non-failure word, and unittest)
        self._bracket(self._run("FAILED t.py::TestB[x]::test_x::[p] - AssertionError"), "t.py::TestB[x]::test_x::[p]")
        self._bracket(self._run("FAILED t.py::TestB[x]::test_x::[p] - AssertionError"), "test_x[p]")
        for word in ("PASSED", "XFAIL", "XPASS"):
            count = {"PASSED": "1 passed", "XFAIL": "1 xfailed", "XPASS": "1 xpassed"}[word]
            text = (f"=== short test summary info ===\n{word} t.py::TestB[x]::test_x::[p]\n"
                    f"FAILED t.py::test_a - AssertionError\n=== 1 failed, {count} in 0.1s ===\n")
            with self.subTest(word=word):
                self._bracket(text, "t.py::test_a")
        ut = ("F\n======================================================================\nFAIL: test_a (t.T[x]..Oops.test_a)\n"
              "----------------------------------------------------------------------\nTraceback (most recent call last):\n"
              '  File "t.py", line 4, in test_a\nAssertionError: 1 != 2\n\n'
              "----------------------------------------------------------------------\nRan 1 test in 0.001s\n\n"
              "FAILED (failures=1)\n")
        self._bracket(ut, "t.py:4")
        self._bracket(ut.replace("t.T[x]..Oops.test_a", "t.T[x].Oops.test_a"), "t.py:4")  # no empty segment: still `[`
        # a frame in a directory whose name holds `[`: the path is split at its first `[` no more
        frame = ("=== FAILURES ===\n___ test_x ___\nE   AssertionError: boom\na[x]/t.py:2: AssertionError\n"
                 "=== short test summary info ===\nFAILED t.py::test_x - AssertionError: boom\n=== 1 failed in 0.1s ===\n")
        self._bracket(frame, "t.py::test_x")
        self._bracket(self._run("ERROR a[x]/t.py - ValueError: boom"), "t.py::test_x")  # a module id is a path
        # A1: the competing node `u.py::TestA[x]::test_target[p]` (its section prints `TestA[x].test_target[p]`)
        a1 = ("=== FAILURES ===\n___ TestA[x].test_target[p] - AssertionError: x.test_x ___\nE TypeError: boom\n"
              "t.py:2: TypeError\n___ TestA[x].test_target[p] ___\nE AssertionError\nt.py:8: AssertionError\n"
              "=== short test summary info ===\n"
              "FAILED t.py::TestA[x].test_target[p] - AssertionError: x::test_x - TypeError: boom\n"
              "FAILED u.py::TestA[x]::test_target[p] - AssertionError\n=== 2 failed in 0.04s ===\n")
        self._bracket(a1, "t.py::TestA[x].test_target[p]")

    def test_r13_pass1_other_lines_check_every_reading_of_their_id(self) -> None:
        # adversary r13 pass 1 (11a64a80): A1, a SKIPPED location cut at its first `:` (a drive letter) and A2, an
        # XFAIL/XPASS id cut at its first ' - ' (inside its parameter id), each hid the rest of the id from the checks
        def text(line: str, count: str) -> str:
            return (f"=== short test summary info ===\n{line}\nFAILED t.py::test_a - AssertionError\n"
                    f"=== 1 failed, {count} in 0.1s ===\n")
        for line in ("SKIPPED [1] C:\\a[x]\\t.py:3: why", "SKIPPED [1] C:/a[x]/t.py:3: why", "SKIPPED [1] a[x]/t.py:3: why"):
            with self.subTest(line=line):
                self._bracket(text(line, "1 skipped"), "t.py::test_a")
        for line in ("SKIPPED [1] C:\\a\\\\t.py:3: why", "SKIPPED [1] C:/a//t.py:3: why"):  # an empty segment after `C:`
            with self.subTest(line=line):
                v = rmv.classify(text(line, "1 skipped"), "t.py::test_a")
                self.assertEqual(v.status, rmv.REFUSED, v)
        for word, count in (("XFAIL", "1 xfailed"), ("XPASS", "1 xpassed")):
            for node in ("t.py::test_b[p - q::r] - reason", "t.py::TestB[x - y]::test_b - reason"):
                with self.subTest(word=word, node=node):
                    self._bracket(text(f"{word} {node}", count), "t.py::test_a")
            with self.subTest(word=word, empty=True):  # an empty segment after a ' - ' inside a class name
                v = rmv.classify(text(f"{word} t.py::TestB - x::::test_b - reason", count), "t.py::test_a")
                self.assertEqual(v.status, rmv.REFUSED, v)
        # controls: a drive-qualified SKIPPED location and an XFAIL with a reason stay readable
        for line, count in (("SKIPPED [1] C:\\a\\t.py:3: why", "1 skipped"), ("XFAIL t.py::test_b[p] - reason", "1 xfailed"),
                            ("XPASS t.py::test_b - a reason", "1 xpassed")):
            with self.subTest(control=line):
                v = rmv.classify(text(line, count), "t.py::test_a")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)

    def test_bracket_only_in_the_trailing_parameter_id_stays_readable(self) -> None:
        # r13 controls: a `[` after the last separator is the last segment's parameter id, whatever it holds but `::`
        for line, want in (("FAILED t.py::test_x[p] - AssertionError: boom", "t.py::test_x[p]"),
                           ("FAILED t.py::TestA::test_x[a[b]] - AssertionError: boom", "t.py::TestA::test_x[a[b]]"),
                           ("FAILED t.py::test_x[a/b] - AssertionError: boom", "t.py::test_x[a/b]"),
                           ("FAILED t.py::TestA::test_x[1.5] - AssertionError: boom", "t.py::TestA::test_x[1.5]"),
                           ("FAILED d/t.py::TestA::test_x[a.b\\c] - AssertionError: boom", "TestA::test_x[a.b\\c]")):
            with self.subTest(line=line):
                v = rmv.classify(self._run(line), want)
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)
        section = ("=== FAILURES ===\n___ TestA.test_x[1.5] ___\nE   AssertionError: boom\nt.py:3: AssertionError\n"
                   "=== short test summary info ===\nFAILED t.py::TestA::test_x[1.5] - AssertionError: boom\n"
                   "=== 1 failed in 0.1s ===\n")  # a section title is pytest's own heading: a `.` in its param is read
        v = rmv.classify(section, "t.py::TestA::test_x[1.5]")
        self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)

    # r14 HUB RULING (HUB-TICK 20261107T2334Z; r13 adversary fde24fbc A1/A2, Dot 4153241): a line break inside a frame's
    # metadata (a code object's co_name or filename) forges the next line. Out of threat model (hostile test code), but a
    # cheap fail-closed tripwire refuses the shapes it leaves, typed `cnc-traceback-line-break`.
    LINE_BREAK = "cnc-traceback-line-break"

    @staticmethod
    def _saved_ut(src: str, co_name: str | None = None) -> str:
        """The unittest text of module `t` (``import unittest`` then `src`) whose ``T.test_x`` code object is renamed to
        `co_name`: the r13 adversary's ``saved()``, run in this process so the code object can be replaced."""
        import importlib.util
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "t.py")
            path.write_text("import unittest\n" + src, encoding="utf-8", newline="\n")
            spec = importlib.util.spec_from_file_location("t", path)
            mod = importlib.util.module_from_spec(spec)
            saved, sys.modules["t"] = sys.modules.get("t"), mod
            try:
                spec.loader.exec_module(mod)
                if co_name is not None:
                    fn = mod.T.test_x
                    fn.__code__ = fn.__code__.replace(co_name=co_name)
                buf = io.StringIO()
                unittest.TextTestRunner(stream=buf, verbosity=0).run(
                    unittest.defaultTestLoader.loadTestsFromTestCase(mod.T))
            finally:
                if saved is None:
                    sys.modules.pop("t", None)
                else:
                    sys.modules["t"] = saved
        return buf.getvalue()

    def _line_break(self, text: str, expected: str) -> None:
        v = rmv.classify(text, expected)
        self.assertEqual((v.status, v.code), (rmv.REFUSED, self.LINE_BREAK), (v, text))

    @staticmethod
    def _ut_block(body: str) -> str:
        return ("=" * 70 + "\nFAIL: test_x (t.T.test_x)\n" + "-" * 70 + "\nTraceback (most recent call last):\n" + body
                + "\n\n" + "-" * 70 + "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")

    def test_traceback_line_break_is_could_not_check(self) -> None:
        # fde24fbc-A1, exactly as written: failureException=TypeError, co_name `test_x\nAssertionError: forged`. The real
        # TypeError gets a FAIL header and the forged line was read as the exception line: PASS on a TypeError.
        a1 = self._saved_ut('class T(unittest.TestCase):\n    failureException = TypeError\n    def test_x(self):\n'
                            '        raise TypeError("boom")\n', "test_x\nAssertionError: forged")
        for expected in ("t.T.test_x", "t.py:5"):
            with self.subTest(a1=expected):
                self._line_break(a1, expected)
        # fde24fbc-A2, exactly as written: co_name `test_x\n  File "victim.py", line 2, in target` adds a frame that does
        # not exist, and it decides `victim.py:2`
        a2 = self._saved_ut("class T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1,2)\n",
                            'test_x\n  File "victim.py", line 2, in target')
        cases = [("a2", a2, "victim.py:2"),
                 # the compiled-filename sibling: the forged frame comes FIRST, directly followed by the real one
                 ("filename", self._ut_block('  File "t.py", line 4, in test_x\n    exec(code)\n'
                                             '  File "victim.py", line 2, in injected\n'
                                             '  File "real.py", line 1, in <module>\nAssertionError'), "victim.py:2"),
                 # rule (a) part 2: a column-0 line between `Traceback` and a later frame header (it was read as a
                 # non-assertion exception line, so the frames after it were dropped)
                 ("col0-before-frame", self._ut_block('  File "t.py", line 4, in test_x\n    helper()\nforged line\n'
                                                      '  File "victim.py", line 2, in target\n    self.assertEqual(1, 2)\n'
                                                      "AssertionError: 1 != 2"), "victim.py:2"),
                 # rule (a) part 1 with a forged frame after the forged exception line
                 ("two-heads", self._ut_block('  File "t.py", line 5, in test_x\nAssertionError: forged\n'
                                              '  File "t.py", line 9, in helper\n    raise TypeError("boom")\n'
                                              "TypeError: boom"), "t.py:5")]
        for name, text, expected in cases:
            with self.subTest(case=name):
                self._line_break(text, expected)
        # the pytest sibling (:1051), hand-written as pytest's long and short reprs print it (the pytest form below runs the
        # real formatter when pytest is importable): compile('assert False\n', 'victim.py:2: in injected\nreal.py', 'exec')
        long_ = ("=== FAILURES ===\n___ test_x ___\n\n    def test_x():\n>       exec(code)\n\nt.py:4: \n"
                 "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _\n\n>   ???\nE   AssertionError\n\n"
                 "victim.py:2: in injected\nreal.py:1: AssertionError\n=== 1 failed in 0.1s ===\n")
        short = ("=== FAILURES ===\n___ test_x ___\nt.py:4: in test_x\n    exec(code)\nvictim.py:2: in injected\n"
                 "real.py:1: in <module>\n    ???\nE   AssertionError\n=== 1 failed in 0.1s ===\n")
        for name, text in (("long", long_), ("short", short)):
            with self.subTest(pytest=name):
                self._line_break(text, "victim.py:2")

    def test_traceback_line_break_pytest_real_formatter(self) -> None:
        try:
            import pytest
        except ImportError:
            self.skipTest("pytest is not importable; the hand-written equivalent runs in the test above")
        code = compile("assert False\n", "victim.py:2: in injected\nreal.py", "exec")
        for style in ("long", "short"):
            try:
                exec(code, {})
            except AssertionError:
                body = str(pytest.ExceptionInfo.from_current().getrepr(style=style, showlocals=False, tbfilter=False))
            text = "=== FAILURES ===\n___ test_x ___\n" + body + "\n=== 1 failed in 0.1s ===\n"
            with self.subTest(style=style):
                self._line_break(text, "victim.py:2")

    def test_traceback_line_break_controls_keep_their_verdict(self) -> None:
        passing = rmv.PASS, "assertion-failed"
        # an ordinary AssertionError FAIL, and multi-line assertEqual diff messages (list, multi-line string, dict)
        for src, line in (("        self.assertEqual(1, 2)\n", 4), ("        self.assertEqual([1, 2], [1, 3])\n", 4),
                          ('        self.assertEqual("a\\nb\\n", "a\\nc\\n")\n', 4),
                          ('        self.assertEqual({"a": 1, "b": [1, 2]}, {"a": 2, "b": [1, 2]})\n', 4)):
            text = self._saved_ut("class T(unittest.TestCase):\n    def test_x(self):\n" + src)
            for expected in ("t.T.test_x", f"t.py:{line}"):
                with self.subTest(src=src, expected=expected):
                    v = rmv.classify(text, expected)
                    self.assertEqual((v.status, v.code), passing, (v, text))
        # a chained exception keeps its verdict
        chained = self._ut_block('  File "t.py", line 4, in test_x\n    f()\nKeyError: 1\n\n'
                                 "During handling of the above exception, another exception occurred:\n\n"
                                 "Traceback (most recent call last):\n"
                                 '  File "t.py", line 6, in test_x\n    self.fail()\nAssertionError: None')
        self.assertEqual(rmv.classify(chained, "t.py:6").code, "cnc-chained")
        # sourceless frames that do not decide the verdict (exec'd or frozen code prints no source line)
        for body in ('  File "t.py", line 8, in test_a\n    exec(code)\n  File "<string>", line 1, in <module>\n'
                     "AssertionError: 3 != 4",
                     '  File "t.py", line 8, in test_a\n    run()\n  File "<frozen a>", line 1, in x\n'
                     '  File "<frozen b>", line 2, in y\nAssertionError: 3 != 4'):
            with self.subTest(body=body):
                v = rmv.classify(self._ut_block(body), "t.py:8")
                self.assertEqual((v.status, v.code), passing, v)
        # pytest: the long and short reprs of an ordinary two-frame assertion
        long_ = ("=== FAILURES ===\n___ test_x ___\n\n    def test_x():\n>       helper()\n\nt.py:4: \n"
                 "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _\n\n    def helper():\n>       assert False\nE       assert False\n\n"
                 "u.py:5: AssertionError\n=== 1 failed in 0.1s ===\n")
        short = ("=== FAILURES ===\n___ test_x ___\nt.py:4: in test_x\n    helper()\nu.py:5: in helper\n    assert False\n"
                 "E   assert False\n=== 1 failed in 0.1s ===\n")
        for text in (long_, short):
            for expected in ("u.py:5", "t.py:4"):
                with self.subTest(text=text[-60:], expected=expected):
                    v = rmv.classify(text, expected)
                    self.assertEqual((v.status, v.code), passing, v)

    def test_subtest_message_header_keeps_its_identity(self) -> None:
        # r14 adversary 716e6018-A1 (honest output): `with self.subTest("ordinary message")` prints the header
        # `test_x (t.T.test_x) [ordinary message]`. Its `[msg]` suffix was not accepted, so the legacy fallback invented
        # `t.T.test_x.test_x` and `test_x.test_x` PASSed while the failing test was T.test_x.
        rule, dash = "=" * 70, "-" * 70
        witness = (rule + "\nFAIL: test_x (t.T.test_x) [ordinary message]\n" + dash + "\nTraceback (most recent call last):\n"
                   '  File "t.py", line 5, in test_x\n    self.assertEqual(1,2)\nAssertionError: 1 != 2\n\n' + dash
                   + "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
        self.assertNotEqual(rmv.classify(witness, "test_x.test_x").status, rmv.PASS)
        v = rmv.classify(witness, "t.T.test_x")
        self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)
        # real runs: message only, an empty message, message and parameters, parameters only
        for sub in ('"ordinary message"', '""', '"m", k=1', "k=1, j='a (b.c)'", '"see (os.path)"'):
            out = self._saved_ut("class T(unittest.TestCase):\n    def test_x(self):\n"
                                 f"        with self.subTest({sub}):\n            self.assertEqual(1, 2)\n")
            with self.subTest(sub=sub):
                self.assertNotEqual(rmv.classify(out, "test_x.test_x").status, rmv.PASS, out)
                v = rmv.classify(out, "t.T.test_x")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), (v, out))
        # the legacy form (Python < 3.11) with a message: the class group is the one the subTest suffix follows
        legacy = witness.replace("test_x (t.T.test_x) [ordinary message]", "test_x (t.T) [see (os.path)]")
        self.assertNotEqual(rmv.classify(legacy, "os.path.test_x").status, rmv.PASS)
        v = rmv.classify(legacy, "t.T.test_x")
        self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)

    # r15 (Dot R18 B1 6715619691, bus a6a68d2; honest output): CPython <= 3.10 prints a test as `name (module.Class)`.
    # CPython v3.10.0 Lib/unittest/case.py:463-464:
    #     def __str__(self):
    #         return "%s (%s)" % (self._testMethodName, strclass(self.__class__))
    # and :1422-1443 (_SubTest):
    #     def _subDescription(self):
    #         parts = []
    #         if self._message is not _subtest_msg_sentinel:
    #             parts.append("[{}]".format(self._message))
    #         if self.params:
    #             params_desc = ', '.join(
    #                 "{}={!r}".format(k, v)
    #                 for (k, v) in self.params.items())
    #             parts.append("({})".format(params_desc))
    #         return " ".join(parts) or '(<subtest>)'
    #     ...
    #     def __str__(self):
    #         return "{} {}".format(self.test_case, self._subDescription())
    # So module t, class test_x, method test_x prints `test_x (t.test_x)` plus the subTest suffix, and its identity is
    # t.test_x.test_x. A 3.11+ header names module, class AND method, so `t.test_x` (two segments) is never one.
    _LEGACY_STR = ("import unittest\n"
                   "unittest.TestCase.__str__ = lambda self: '%s (%s)' % (self._testMethodName,\n"
                   "    '%s.%s' % (type(self).__module__, type(self).__qualname__))  # 3.10 case.py:463-464\n")

    def _legacy_run(self, cls: str, body: str, *, legacy: bool = True) -> str:
        """REAL unittest output of module `t` with one class `cls` and method `test_x` running `body`, rendered by the
        CPython 3.10 TestCase.__str__ quoted above (``legacy``) or by the host's own (3.11+) one."""
        src = ((self._LEGACY_STR if legacy else "import unittest\n") + f"class {cls}(unittest.TestCase):\n"
               "    def test_x(self):\n" + "".join(f"        {ln}\n" for ln in body.splitlines()))
        return _run_unittest({"t.py": src}, "t")

    COLLISION_FORMS = (
        ('with self.subTest("ordinary message"):\n    self.assertEqual(1, 2)', " [ordinary message]"),
        ("with self.subTest(k=1):\n    self.assertEqual(1, 2)", " (k=1)"),
        ('with self.subTest("m", k=1):\n    self.assertEqual(1, 2)', " [m] (k=1)"),
        ("self.assertEqual(1, 2)", ""),
    )

    def test_legacy_subtest_class_method_collision(self) -> None:
        rule, dash = "=" * 70, "-" * 70
        witness = (rule + "\nFAIL: test_x (t.test_x) [ordinary message]\n" + dash + "\nTraceback (most recent call last):\n"
                   '  File "t.py", line 5, in test_x\n    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n\n' + dash
                   + "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
        texts = [("dot-witness", witness)]
        for body, suffix in self.COLLISION_FORMS:
            out = self._legacy_run("test_x", body)
            self.assertIn(f"FAIL: test_x (t.test_x){suffix}\n", out.replace("\r\n", "\n"))
            texts.append((suffix or "no-suffix", out))
        for label, text in texts:
            with self.subTest(form=label):
                v = rmv.classify(text, "t.test_x")
                self.assertEqual(v.status, rmv.REFUSED, (v, text))
                self.assertEqual(v.code, "not-at", (v, text))
                for right in ("t.test_x.test_x", "test_x.test_x"):
                    v = rmv.classify(text, right)
                    self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), (right, v, text))

    def test_legacy_collision_controls_keep_their_verdict(self) -> None:
        for body, suffix in self.COLLISION_FORMS:
            with self.subTest(suffix=suffix):
                # 3.11+ (the host's own rendering), class T and the colliding class test_x: the true identity PASSes
                modern = self._legacy_run("T", body, legacy=False)
                self.assertIn(f"FAIL: test_x (t.T.test_x){suffix}\n", modern.replace("\r\n", "\n"))
                v = rmv.classify(modern, "t.T.test_x")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), (v, modern))
                self.assertNotEqual(rmv.classify(modern, "t.T.test_x.test_x").status, rmv.PASS)
                same = self._legacy_run("test_x", body, legacy=False)
                self.assertIn(f"FAIL: test_x (t.test_x.test_x){suffix}\n", same.replace("\r\n", "\n"))
                v = rmv.classify(same, "t.test_x.test_x")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), (v, same))
                # <= 3.10 with class T (no collision): the true identity PASSes, the class alone does not
                legacy = self._legacy_run("T", body)
                self.assertIn(f"FAIL: test_x (t.T){suffix}\n", legacy.replace("\r\n", "\n"))
                v = rmv.classify(legacy, "t.T.test_x")
                self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), (v, legacy))
                self.assertNotEqual(rmv.classify(legacy, "t.T").status, rmv.PASS)
        # the identity reader itself: a two-segment group ending in `.name` is the <= 3.10 layout only
        self.assertEqual(rmv._ut_identities("test_x (t.test_x) [m] (k=1)"), [("test_x", "t.test_x.test_x")])
        self.assertEqual(rmv._ut_identities("test_x (t.T.test_x) [m] (k=1)"), [("test_x", "t.T.test_x")])

    def test_long_nonfailure_reason_is_bounded(self) -> None:
        # Dot 4153241 (:845/:859): every prefix of a SKIPPED/XFAIL/XPASS reason was materialized and checked, quadratic in
        # its length. A 200 KB reason must finish in under 2 s, and it never PASSes as anything but the run's own failure.
        import time

        def text(line: str, count: str) -> str:
            return (f"=== short test summary info ===\n{line}\nFAILED t.py::test_a - AssertionError\n"
                    f"=== 1 failed, {count} in 0.1s ===\n")
        size = 200 * 1024
        for line, count in (("SKIPPED [1] t.py:3: " + ("x:1: " * (size // 5)), "1 skipped"),
                            ("XFAIL t.py::test_b - " + ("a - " * (size // 4)), "1 xfailed"),
                            ("XPASS t.py::test_b - " + ("a - " * (size // 4)), "1 xpassed"),
                            ("SKIPPED [1] t.py:3: " + ("x" * size), "1 skipped")):
            with self.subTest(line=line[:30]):
                start = time.perf_counter()
                v = rmv.classify(text(line, count), "t.py::test_a")
                self.assertLess(time.perf_counter() - start, 2.0, v)
                self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-short-summary"), v)
        # the FAILED/ERROR sibling: every ' - ' of the line is a reading, each checked as its own prefix
        for word in ("FAILED", "ERROR"):
            run = (f"=== short test summary info ===\n{word} t.py::test_a - AssertionError: " + ("a - " * (size // 4))
                   + f"\n=== 1 {'failed' if word == 'FAILED' else 'error'} in 0.1s ===\n")
            with self.subTest(word=word):
                start = time.perf_counter()
                v = rmv.classify(run, "t.py::test_a")
                self.assertLess(time.perf_counter() - start, 2.0, v)
                self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-short-summary"), v)
        # r14 adversary e905500a-A1 (honest output): a subTest whose parameter holds 24000 spaces prints a unittest header
        # that `.+?\s*$` backtracked over quadratically (6.9 s) before the header-length guard; both callers (:715, :725)
        out = self._saved_ut("class T(unittest.TestCase):\n    def test_x(self):\n"
                             "        with self.subTest(x=' ' * 24000):\n            self.assertEqual(1, 2)\n")
        self.assertGreater(max(len(ln) for ln in out.splitlines()), 24000)
        start = time.perf_counter()
        v = rmv.classify(out, "t.T.test_x")
        self.assertLess(time.perf_counter() - start, 2.0, v)
        self.assertEqual((v.status, v.code), (rmv.REFUSED, "cnc-identity"), v)
        # control: a short subTest header is still read
        out = self._saved_ut("class T(unittest.TestCase):\n    def test_x(self):\n"
                             "        with self.subTest(x='  a  '):\n            self.assertEqual(1, 2)\n")
        v = rmv.classify(out, "t.py:5")
        self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), (v, out))
        # control: a summary line just under the bound is still read
        v = rmv.classify(text("XFAIL t.py::test_b - " + "r" * 3000, "1 xfailed"), "t.py::test_a")
        self.assertEqual((v.status, v.code), (rmv.PASS, "assertion-failed"), v)


class ReasonCodeR11(unittest.TestCase):
    """r11, r10 adversary A2: every verdict carries a reason CODE the checker sets where it writes the reason."""

    QUOTED = ("=== short test summary info ===\nFAILED t.py::test_x - MissingDependency: boom\n"
              "=== 1 failed in 0.1s ===\n")

    def test_every_verdict_has_a_known_code(self) -> None:
        cases = [(_ut_text("FAILED (failures=1)"), "t.py:8"), (_ut_text("FAILED (failures=1)"), "t.py:9"),
                 (_ut_text("FAILED (failures=1.5)"), "t.py:8"), ("Ran 1 test in 0.001s\n\nOK\n", "t.py:8"),
                 ("nothing here\n", "t.py:8"), (_ut_text("FAILED (failures=1)"), ""), (self.QUOTED, "other"),
                 ("a\rb", "t.py:1"), (7, "t.py:1")]
        for out, want in cases:
            with self.subTest(out=out, want=want):
                v = rmv.classify(out, want)
                self.assertIn(v.code, rmv.REASON_CODES, v)
        # the quoted input never becomes the code: the code is one of the checker's own names
        self.assertEqual(rmv.classify(self.QUOTED, "other").code, "cnc-exception-type")
        self.assertEqual(rmv.classify(_ut_text("FAILED (failures=1)"), "t.py:8").code, "assertion-failed")
        self.assertEqual(rmv.classify(_ut_text("FAILED (failures=1)"), "t.py:9").code, "not-at")

    def test_probe_codes(self) -> None:
        for args, code in (((3, 3, (3, 3)), "probe-both-inside"), ((30, 40, (0, 2)), "probe-neither-inside"),
                           ((1, 9, (0, 5)), "probe-bug-inside"), ((1, 2, (5, 1)), "probe-empty-window"),
                           ((float("nan"), 2, (0, 5)), "probe-nan"), (("a", 2, (0, 5)), "probe-number"),
                           ((float("inf"), 2, (0, 5)), "probe-infinite"), ((3, 2.8, (2.7, 2.9)), "probe-separates")):
            with self.subTest(args=args):
                v = rmv.probe_classify(*args)
                self.assertEqual(v.code, code, v)
                self.assertIn(v.code, rmv.REASON_CODES)
                self.assertEqual(rmv.probe_discriminates(*args), (v.status == rmv.PASS, v.reason))


if __name__ == "__main__":
    unittest.main()
