"""CPU-SCOPE-LIVE-CRASH-FIX-1 G1: the fixture's failed controls and setup steps are INVALID/3, never a verdict.

Sol r6 B1/B2 escaped two approvals: a missing negative control gave GREEN/0, and a failed key or bookkeeping
allocation gave usage exit 4. This file keeps both fixed. It drives the test-only seam MLV_TLS_FIXTURE_FAULT=<site>
in tests/console/tls_exit_order_fixture.cpp.

Two parts:
  FixtureSourceTests  run everywhere (no exe needed). Every seam site in the fixture is in SITES, and each maps to an
                      INVALID path: a named why= on an INVALID record, and every report reads the setup failure before
                      it can print a verdict.
  FixtureExeTests     run the built fixture. MLV_TLS_FIXTURE_EXE names it; otherwise the in-source qmake output
                      tests/console/tls_exit_order_fixture.exe is used if it exists. With neither, every test SKIPs and
                      names why: it never passes without running. A path in MLV_TLS_FIXTURE_EXE that does not exist fails.
                      MLV_TLS_FIXTURE_DLL_PATH (os.pathsep list) is put first on PATH for the run; without it the pinned
                      toolchain bin dirs are used when present.

Run: py -3 -m unittest discover -s tests/console -p test_tls_exit_order_fixture_faults.py -v
Hosted CI does not build the fixture yet (TLS-EXIT-ORDER-FIXTURE-CI-1).
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SOURCE = ROOT / "tests" / "console" / "tls_exit_order_fixture.cpp"
CONVENTIONAL_EXE = ROOT / "tests" / "console" / "tls_exit_order_fixture.exe"
EXE_ENV = "MLV_TLS_FIXTURE_EXE"
DLL_PATH_ENV = "MLV_TLS_FIXTURE_DLL_PATH"
FAULT_ENV = "MLV_TLS_FIXTURE_FAULT"
PINNED_TOOLCHAIN_BINS = (r"C:\Qt\6.10.2\mingw_64\bin", r"C:\Qt\Tools\mingw1310_64\bin")
STATUS_DLL_NOT_FOUND = 0xC0000135
RUN_TIMEOUT_S = 120

# The seam table: site -> the why= the INVALID record must name.
SITES = {
    "neg_alloc": "negative_control_alloc_failed",
    "pos_alloc": "positive_control_alloc_failed",
    "search_alloc": "search_alloc_failed",
    "warm_alloc": "warm_alloc_failed",
    "key_create": "key_create_failed",
    "kept_alloc": "kept_alloc_failed",
    "set_key": "set_key_failed",
}

# (site, mode, kind): each must be INVALID/3 with SITES[site]. search_alloc is separate (see its test).
FAULT_CASES = (
    ("neg_alloc", "helper", "stdthread"),
    ("neg_alloc", "helper", "qthread"),
    ("neg_alloc", "helper", "omp"),
    ("neg_alloc", "helper", "main"),
    ("neg_alloc", "reuse", "stdthread"),
    ("neg_alloc", "order", "stdthread"),
    ("pos_alloc", "reuse", "stdthread"),
    ("pos_alloc", "helper", "stdthread"),
    ("pos_alloc", "order", "stdthread"),
    ("key_create", "order", "stdthread"),
    ("key_create", "helper", "stdthread"),
    ("kept_alloc", "reuse", "stdthread"),
    ("kept_alloc", "order", "stdthread"),
    ("warm_alloc", "reuse", "stdthread"),
    ("set_key", "order", "stdthread"),
)
RUNS_PER_FAULT_CASE = 2

# Sol r8 B1: crash mode, every thread kind, either control missing. Each must be INVALID/3 with SITES[site].
CRASH_CONTROL_CASES = tuple((site, "crash", kind) for site in ("neg_alloc", "pos_alloc") for kind in ("stdthread", "qthread", "omp"))
STATUS_HEAP_CORRUPTION = 0xC0000374

# The r6 B1 escape: with the negative control missing, these modes printed GREEN (helper) or RED (reuse) and exit 0.
MISSING_NEGATIVE_CONTROL_MODES = (
    ("helper", "stdthread"),
    ("helper", "qthread"),
    ("helper", "omp"),
    ("helper", "main"),
    ("reuse", "stdthread"),
)

# The seam fails a search allocation only once the positive control is back and the base is not: when the search
# finds the base first, it completes and never reaches the seam. So each run is INVALID or a complete search. About
# half the runs reached the seam on the pinned toolchain (r8: 6 of 10), so the test keeps going past SEARCH_ALLOC_RUNS
# until the seam has fired once, up to SEARCH_ALLOC_MAX_RUNS (0.5**48 odds of a spurious miss). A toolchain where the base
# always comes back first never reaches the seam; that fails on purpose, since the INVALID path is then unproven.
SEARCH_ALLOC_RUNS = 12
SEARCH_ALLOC_MAX_RUNS = 48

USAGE_CASES = (
    ((), None),
    (("order", "bogus"), None),
    (("bogus", "stdthread"), None),
    (("bogus", "stdthread"), "key_create"),
)

VERDICT_WORDS = ("RED", "GREEN", "NO_KEY_DTOR", "NO_FAST_FAIL", "REPORT", "CONTROL_OK")


def fixture_text():
    return FIXTURE_SOURCE.read_text(encoding="utf-8")


def region(text, start_pattern, end_pattern):
    start = re.search(start_pattern, text)
    if not start:
        raise AssertionError("anchor not found in the fixture (update this test): " + start_pattern)
    end = re.compile(end_pattern).search(text, start.end())
    if not end:
        raise AssertionError("end anchor not found in the fixture (update this test): " + end_pattern)
    return text[start.start():end.start()]


def print_result_calls(body):
    """Each printResult(...) call up to its statement end, with its offset."""
    return [(m.start(), body[m.start():body.index(";", m.start())]) for m in re.finditer(r"\bprintResult\(", body)]


def is_verdict_call(call):
    return any('"' + word + '"' in call for word in VERDICT_WORDS)


class FixtureSourceTests(unittest.TestCase):
    """Runs everywhere: the seam table and the source agree, and every site reaches INVALID/3."""

    @classmethod
    def setUpClass(cls):
        cls.text = fixture_text()

    def test_every_seam_site_in_the_fixture_is_in_the_table(self):
        in_code = set(re.findall(r'\bfaultAt\("([a-z_]+)"\)', self.text))
        in_code |= set(re.findall(r'\bblockMalloc\("([a-z_]+)"\)', self.text))
        self.assertEqual(sorted(SITES), sorted(in_code), "seam sites in the fixture and SITES differ")

    def test_the_seam_comment_lists_every_site(self):
        comment = region(self.text, r"// Test-only fault seam\.", r"\nconst char \* g_fault")
        listed = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", comment.split("Sites:", 1)[1]))
        self.assertEqual(sorted(SITES), sorted(listed))

    def test_each_site_names_its_why_on_an_invalid_path(self):
        for site, why in SITES.items():
            with self.subTest(site=site):
                noted = 'noteSetupFailure("' + why + '")' in self.text
                printed = re.search(r'"INVALID",\s*"why=' + re.escape(why) + r'[ "]', self.text)
                self.assertTrue(noted or printed, site + " has no INVALID path naming why=" + why)

    def test_invalid_is_exit_3(self):
        self.assertRegex(self.text, r'std::strcmp\(verdict, "INVALID"\) == 0 \? 3 : 0')
        # set_key prints its own record and exits, so its exit code is spelled out.
        set_key = region(self.text, r"\nvoid setKeyK\(\)", r"\n}\n")
        self.assertIn('"why=set_key_failed"', set_key)
        self.assertIn("std::_Exit(3);", set_key)

    def test_every_report_reads_the_setup_failure_before_any_verdict(self):
        regions = {
            "reportOrder": region(self.text, r"\nint reportOrder\(", r"\n}\n"),
            "reportReuse": region(self.text, r"\nint reportReuse\(", r"\n}\n"),
            "runHelper main": region(self.text, r'if \(kind == "main"\)', r"// A worker still running"),
            "runHelper worker": region(self.text, r"// A worker still running after the wait may still write g_search, so nothing",
                                       r"\n}\n"),
            "crash": region(self.text, r'if \(mode == "crash"\)', r'if \(mode == "setspecific_fail"\)'),
        }
        for name, body in regions.items():
            with self.subTest(report=name):
                verdicts = [offset for offset, call in print_result_calls(body) if is_verdict_call(call)]
                self.assertTrue(verdicts, name + ": no verdict call found (update this test)")
                check = body.find("g_setup_failure.load()")
                self.assertNotEqual(-1, check, name + " never reads g_setup_failure")
                self.assertLess(check, verdicts[0], name + " can print a verdict before it reads g_setup_failure")

    def test_no_destructive_adversary_after_a_recorded_setup_failure(self):
        """Sol r8 B1, by source: the key destructor's search and overwrite, and crash mode's late Probe teardown, are
        each gated on g_setup_failure, so a failed control can only end as an INVALID record."""
        key = region(self.text, r"\nvoid keyDestructor\(void \*\)", r"\n}\n")
        search = key.find("searchForBase(")
        self.assertNotEqual(-1, search, "keyDestructor no longer runs the search (update this test)")
        gate = key.find("!g_setup_failure.load()")
        self.assertNotEqual(-1, gate, "keyDestructor runs its adversary without reading g_setup_failure")
        self.assertLess(gate, search, "keyDestructor searches before it reads g_setup_failure")
        write = key.find("writeDumpBytes(")
        self.assertNotEqual(-1, write, "keyDestructor no longer writes the dump bytes (update this test)")
        self.assertRegex(key[search:write + len("writeDumpBytes(")], r"if \(block && !g_setup_failure\.load\(\)\) writeDumpBytes\(",
                         "the dump write is not gated on a failure the search itself recorded")
        probe = region(self.text, r"\n__attribute__\(\(noinline\)\) Probe::~Probe\(\)", r"\n}\n")
        crash_gate = re.search(r"if \(g_mode == Mode::Crash\)\s*\{\s*if \(const char \* const why = g_setup_failure\.load\(\)\)"
                               r"\s*\{\s*printResult\(\"crash\", g_kind_arg, \"INVALID\", std::string\(\"why=\"\) \+ why\);"
                               r"\s*TerminateProcess\(GetCurrentProcess\(\), 3\);", probe)
        self.assertIsNotNone(crash_gate, "crash-mode ~Probe does not stop with INVALID/3 before its member teardown")

    def test_setup_steps_print_invalid_not_usage(self):
        setup = region(self.text, r"// From here a failure is the fixture's own setup", r'if \(mode == "order"\)')
        for why in ("key_create_failed", "kept_alloc_failed", "warm_alloc_failed"):
            with self.subTest(why=why):
                self.assertRegex(setup, r'return printResult\(mode\.c_str\(\), kind, "INVALID", "why=' + why)
        self.assertNotIn("return 4;", setup)

    def test_usage_errors_are_checked_before_setup(self):
        usage_end = self.text.index("g_fault = std::getenv")
        self.assertIn('"unknown mode %s\\n"', self.text[:usage_end])
        self.assertIn('"unknown kind %s for mode %s\\n"', self.text[:usage_end])


def resolve_exe():
    """(path, None) or (None, skip reason). A configured path that is missing is a failure, not a skip."""
    configured = os.environ.get(EXE_ENV, "").strip()
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise AssertionError(EXE_ENV + " names a file that does not exist: " + configured)
        return path, None
    if CONVENTIONAL_EXE.is_file():
        return CONVENTIONAL_EXE, None
    return None, ("SKIP no tls_exit_order_fixture exe: set " + EXE_ENV + " or build tests/console/tls_exit_order_fixture.pro "
                  "in-source (hosted CI wiring: TLS-EXIT-ORDER-FIXTURE-CI-1)")


def parse_record(stdout):
    lines = [line for line in stdout.splitlines() if line.startswith("RESULT ")]
    if len(lines) != 1:
        return None, len(lines)
    fields = dict(token.split("=", 1) for token in lines[0].split()[1:] if "=" in token)
    return fields, 1


class FixtureExeTests(unittest.TestCase):
    """Runs the fixture under the seam. SKIPs, with the reason, only when no exe is configured or found."""

    @classmethod
    def setUpClass(cls):
        if sys.platform != "win32":
            raise unittest.SkipTest("SKIP the fixture is a Windows/MinGW console binary; this host is " + sys.platform)
        cls.exe, reason = resolve_exe()
        if cls.exe is None:
            raise unittest.SkipTest(reason)
        dll_dirs = [d for d in os.environ.get(DLL_PATH_ENV, "").split(os.pathsep) if d]
        if not dll_dirs:
            dll_dirs = [d for d in PINNED_TOOLCHAIN_BINS if Path(d).is_dir()]
        cls.base_env = dict(os.environ)
        cls.base_env.pop(FAULT_ENV, None)
        cls.base_env["PATH"] = os.pathsep.join(dll_dirs + [cls.base_env.get("PATH", "")])

    def run_fixture(self, args, fault=None):
        env = dict(self.base_env)
        if fault:
            env[FAULT_ENV] = fault
        proc = subprocess.run([str(self.exe), *args], env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
        code = proc.returncode & 0xFFFFFFFF
        self.assertNotEqual(STATUS_DLL_NOT_FOUND, code, "the fixture could not load its DLLs; set " + DLL_PATH_ENV)
        record, count = parse_record(proc.stdout)
        label = "%s %s exit=0x%08X stdout=%r stderr=%r" % ("FAULT=" + fault if fault else "", " ".join(args), code,
                                                          proc.stdout.strip(), proc.stderr.strip())
        return code, record, count, label

    # --- the seam is off by default, so a fixture that always says INVALID/3 cannot pass the fault tests ---------------
    def test_seam_off_reaches_a_verdict(self):
        for args in (("order", "stdthread"), ("reuse", "stdthread"), ("helper", "stdthread")):
            with self.subTest(args=args):
                code, record, count, label = self.run_fixture(args)
                self.assertEqual(1, count, label)
                self.assertEqual(0, code, label)
                self.assertIn(record.get("verdict"), ("RED", "GREEN"), label)

    # --- G1 (a): every fault site is INVALID with its named why= and exit 3 -------------------------------------------
    def test_fault_sites_are_invalid_3_with_the_named_why(self):
        for site, mode, kind in FAULT_CASES:
            for run in range(1, RUNS_PER_FAULT_CASE + 1):
                with self.subTest(site=site, mode=mode, kind=kind, run=run):
                    code, record, count, label = self.run_fixture((mode, kind), site)
                    self.assertEqual(1, count, "exactly one RESULT record: " + label)
                    self.assertEqual("INVALID", record.get("verdict"), label)
                    self.assertEqual(SITES[site], record.get("why"), label)
                    self.assertEqual(3, code, label)

    def test_search_alloc_is_invalid_3_or_a_complete_search_never_green(self):
        invalid = 0
        runs = 0
        while runs < SEARCH_ALLOC_MAX_RUNS and (runs < SEARCH_ALLOC_RUNS or invalid == 0):
            runs += 1
            with self.subTest(run=runs):
                code, record, count, label = self.run_fixture(("reuse", "stdthread"), "search_alloc")
                self.assertEqual(1, count, label)
                self.assertNotEqual("GREEN", record.get("verdict"), "a cut-short search read as GREEN: " + label)
                if record.get("verdict") == "INVALID":
                    invalid += 1
                    self.assertEqual(SITES["search_alloc"], record.get("why"), label)
                    self.assertEqual(3, code, label)
                else:
                    # The base came back before the positive control, so the seam was never reached.
                    self.assertEqual(("1", "1"), (record.get("base_found"), record.get("pos_found")), label)
                    self.assertEqual(0, code, label)
        self.assertGreater(invalid, 0, "the search_alloc seam never fired in %d runs: its INVALID path is unproven" % runs)

    def test_usage_errors_stay_4(self):
        for args, fault in USAGE_CASES:
            with self.subTest(args=args, fault=fault):
                code, _record, count, label = self.run_fixture(args, fault)
                self.assertEqual(4, code, label)
                self.assertEqual(0, count, "a usage error prints no RESULT record: " + label)

    # --- G1 (b): adversarial. The r6 B1 escape was GREEN/0 (helper) and RED/0 (reuse) on a missing negative control ----
    def test_missing_negative_control_is_never_a_verdict_with_exit_0(self):
        for mode, kind in MISSING_NEGATIVE_CONTROL_MODES:
            with self.subTest(mode=mode, kind=kind):
                code, record, count, label = self.run_fixture((mode, kind), "neg_alloc")
                self.assertEqual(1, count, "a missing negative control must still say why: " + label)
                self.assertNotEqual("GREEN", record.get("verdict"), "GREEN on a missing negative control: " + label)
                self.assertNotIn(record.get("verdict"), VERDICT_WORDS, "a verdict on a missing negative control: " + label)
                self.assertNotEqual(0, code, "exit 0 on a missing negative control: " + label)

    # --- Sol r8 B1: crash mode with a failed control. Before r9 the key destructor's adversary still ran and overwrote the
    # freed Probe block, so the late teardown fast-failed (0xC0000374) before any RESULT. It must be INVALID/3, named. ----
    def test_crash_mode_failed_control_is_invalid_3_with_the_named_why(self):
        for site, mode, kind in CRASH_CONTROL_CASES:
            for run in range(1, RUNS_PER_FAULT_CASE + 1):
                with self.subTest(site=site, mode=mode, kind=kind, run=run):
                    code, record, count, label = self.run_fixture((mode, kind), site)
                    self.assertNotEqual(STATUS_HEAP_CORRUPTION, code, "heap fast-fail after a recorded control failure: " + label)
                    self.assertEqual(1, count, "exactly one RESULT record: " + label)
                    self.assertEqual("INVALID", record.get("verdict"), label)
                    self.assertEqual(SITES[site], record.get("why"), label)
                    self.assertEqual(3, code, label)


if __name__ == "__main__":
    unittest.main()
