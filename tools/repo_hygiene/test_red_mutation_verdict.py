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
        proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], cwd=tmp, text=True,
                              encoding="utf-8", errors="replace", capture_output=True, timeout=120, env=env)
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
        self.assertIn("COULD-NOT-CHECK", rmv.verdict(cut, "test_s.py::T::test_a")[1])
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
        status, reason = rmv.verdict(out, want)
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

    def test_r6_open_a5_decimal_and_fraction_overflow_and_underflow_are_refused(self) -> None:
        from decimal import Decimal
        from fractions import Fraction
        for args in ((1, Decimal("1e-400"), (0, 0)), (1, Fraction(1, 10 ** 400), (0, 0)), (0, 1, (1, Decimal("1e400")))):
            self.assertFalse(rmv.probe_discriminates(*args)[0], args)


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
