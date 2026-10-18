"""Tests for tools/repo_hygiene/runtime_floor_suites.py and tools/coordination/runtime-floors.json.

RUNTIME-FLOOR-LOCAL-MATRIX-1 (CoS FWO-DEFECT-PREVENT-1 item 5). Two halves:

* the checked-in manifest: valid schema, every declared floor cites a real line that still contains the quoted
  text, every named suite file exists;
* the runner, against throwaway git repos: a suite that passes on pwsh 7 but fails on Windows PowerShell 5.1
  exits 1 when both are declared and present (RED), the same suite without the 7-only operator exits 0
  (GREEN); an absent runtime prints SKIP_ABSENT and is not a pass; an unmapped diff exits 0 "none mapped";
  a row naming a missing suite file exits 2.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
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

from tools.repo_hygiene import runtime_floor_suites as rfs

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / rfs.DEFAULT_MANIFEST
HAVE_5_1 = rfs.detect_runtime("powershell-5.1")["argv"] is not None
HAVE_PWSH = rfs.detect_runtime("pwsh")["argv"] is not None
HAVE_PYTEST = importlib.util.find_spec("pytest") is not None
THIS_PY = "python-%d.%d" % sys.version_info[:2]
EVIDENCE = [{"file": "x", "line": 1, "contains": "x"}]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def row(row_id, globs, runtimes, suites, **extra):
    base = {"id": row_id, "globs": globs, "floor": "declared" if runtimes else "undeclared", "runtimes": runtimes,
            "suites": suites}
    if runtimes:
        base["evidence"] = EVIDENCE
    else:
        base["note"] = "searched; nothing stated"
    base.update(extra)
    return base


def manifest_of(*rows):
    return {"schema": rfs.SCHEMA, "rows": list(rows)}


class FixtureRepo:
    """A throwaway git repo: `base` tag holds base_files, HEAD holds base_files + head_files."""

    def __init__(self, test: unittest.TestCase, manifest: dict, base_files: dict, head_files: dict):
        self.root = Path(tempfile.mkdtemp(prefix="rfs-fixture-"))
        test.addCleanup(shutil.rmtree, str(self.root), True)
        self._git("init", "-q")
        self._write(base_files)
        self._write({"manifest.json": json.dumps(manifest)})
        self._commit("base")
        self._git("tag", "base")
        self._write(head_files)
        self._commit("head")

    def _write(self, files: dict) -> None:
        for rel, text in files.items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")

    def _git(self, *args: str) -> str:
        done = subprocess.run(GIT + list(args), cwd=str(self.root), capture_output=True, text=True)
        if done.returncode != 0:
            raise AssertionError(done.stderr)
        return done.stdout

    def _commit(self, message: str) -> None:
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def run(self, *extra: str, detect=rfs.detect_runtime) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rfs.main(["--worktree", str(self.root), "--base", "base", "--manifest",
                             str(self.root / "manifest.json"), "--timeout", "120", *extra], detect=detect)
        return code, buffer.getvalue()

    def porcelain(self) -> str:
        return self._git("status", "--porcelain", "--ignored")


SEVEN_ONLY = "$a = $null\n$b = $a ?? 'fallback'\nexit 0\n"
PORTABLE = "$a = $null\nif ($null -eq $a) { $b = 'fallback' }\nexit 0\n"


class CheckedInManifestTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_schema_is_valid(self):
        self.assertEqual([], rfs.validate_manifest(self.data))

    def test_every_declared_floor_cites_a_real_line(self):
        self.assertEqual([], rfs.check_evidence(self.data, REPO_ROOT))

    def test_every_named_suite_file_exists(self):
        missing = [f"{r['id']}: {s['path']}" for r in self.data["rows"] for s in r["suites"]
                   if s.get("path") and not (REPO_ROOT / s["path"].split("::", 1)[0]).is_file()]
        self.assertEqual([], missing)

    def test_undeclared_rows_are_listed_not_invented(self):
        undeclared = [r for r in self.data["rows"] if r["floor"] == "undeclared"]
        self.assertTrue(undeclared)
        for r in undeclared:
            self.assertEqual([], r["runtimes"], r["id"])
            self.assertNotIn("evidence", r, r["id"])

    def test_the_two_hosts_that_broke_in_468_are_both_represented(self):
        runtimes = {rt for r in self.data["rows"] for rt in r["runtimes"]}
        self.assertTrue({"powershell-5.1", "pwsh"} <= runtimes)
        self.assertTrue(any(rt.startswith("python-") for rt in runtimes))

    def test_ci_python_pin_matches_the_declared_python(self):
        pin = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
        for r in self.data["rows"]:
            for rt in r["runtimes"]:
                if rt.startswith("python-"):
                    self.assertTrue(pin.startswith(rt.split("-", 1)[1] + "."), f"{r['id']}: {rt} vs .python-version {pin}")


class ValidateManifestTests(unittest.TestCase):
    def test_rejects_each_malformed_shape(self):
        good = row("a", ["x/*.ps1"], ["pwsh"], [{"kind": "ps-parse"}])
        cases = {
            "wrong schema": {"schema": "other", "rows": [good]},
            "no rows": {"schema": rfs.SCHEMA, "rows": []},
            "duplicate id": manifest_of(good, dict(good)),
            "bad runtime": manifest_of(row("a", ["x"], ["powershell-7"], [])),
            "declared without runtimes": manifest_of({**good, "runtimes": []}),
            "declared without evidence": manifest_of({k: v for k, v in good.items() if k != "evidence"}),
            "undeclared with runtimes": manifest_of({**good, "floor": "undeclared"}),
            "unknown suite kind": manifest_of(row("a", ["x"], ["pwsh"], [{"kind": "bash"}])),
            "pytest without path": manifest_of(row("a", ["x"], ["pwsh"], [{"kind": "pytest"}])),
            "ps suite without a ps runtime": manifest_of(row("a", ["x"], [THIS_PY], [{"kind": "ps-parse"}])),
            "min_versions on an undeclared runtime": manifest_of({**good, "min_versions": {"powershell-5.1": "5.1"}}),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertTrue(rfs.validate_manifest(data), name)
        self.assertEqual([], rfs.validate_manifest(manifest_of(good)))


class MatchRowsTests(unittest.TestCase):
    def test_declared_row_claims_the_path_before_an_undeclared_catch_all(self):
        data = manifest_of(
            row("catch-all", ["tools/*.ps1"], [], []),
            row("declared", ["tools/a.ps1"], ["pwsh"], [{"kind": "ps-parse"}]),
        )
        matched = {r["id"]: because for r, because in rfs.match_rows(data, ["tools/a.ps1", "tools/b.ps1"])}
        self.assertEqual({"declared": ["tools/a.ps1"], "catch-all": ["tools/b.ps1"]}, matched)

    def test_a_changed_suite_file_maps_back_to_its_row(self):
        data = manifest_of(row("r", ["tools/a.py"], [THIS_PY], [{"kind": "pytest", "path": "tools/test_a.py::Cls"}]))
        self.assertEqual(1, len(rfs.match_rows(data, ["tools/test_a.py"])))


class RunnerTests(unittest.TestCase):
    def test_unmapped_diff_is_none_mapped_exit_0(self):
        data = manifest_of(row("r", ["tools/a.ps1"], ["pwsh"], [{"kind": "ps-parse"}]))
        repo = FixtureRepo(self, data, {"tools/a.ps1": PORTABLE}, {"docs/readme.md": "hello\n"})
        code, out = repo.run()
        self.assertEqual(0, code, out)
        self.assertIn("none mapped", out)

    def test_missing_suite_file_fails_closed_exit_2(self):
        data = manifest_of(row("r", ["tools/a.py"], [THIS_PY], [{"kind": "pytest", "path": "tools/test_gone.py"}]))
        repo = FixtureRepo(self, data, {"tools/a.py": "x = 1\n"}, {"tools/a.py": "x = 2\n"})
        code, out = repo.run()
        self.assertEqual(2, code, out)
        self.assertIn("suite file that does not exist", out)

    def test_invalid_manifest_and_bad_base_are_exit_2(self):
        data = manifest_of(row("r", ["tools/a.ps1"], ["pwsh"], [{"kind": "ps-parse"}]))
        repo = FixtureRepo(self, data, {"tools/a.ps1": PORTABLE}, {"tools/a.ps1": PORTABLE + "# c\n"})
        (repo.root / "manifest.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(2, repo.run()[0])
        (repo.root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rfs.main(["--worktree", str(repo.root), "--base", "no-such-ref", "--manifest",
                             str(repo.root / "manifest.json")])
        self.assertEqual(2, code, buffer.getvalue())
        self.assertIn("UNKNOWN", buffer.getvalue())

    def test_absent_runtime_prints_skip_absent_and_is_not_a_pass(self):
        data = manifest_of(row("r", ["tools/a.py"], ["python-3.99"], [{"kind": "pytest", "path": "tools/test_a.py"}]))
        repo = FixtureRepo(self, data, {"tools/a.py": "x = 1\n", "tools/test_a.py": "def test_a():\n    assert True\n"},
                           {"tools/a.py": "x = 2\n"})
        code, out = repo.run()
        self.assertEqual(0, code, out)
        self.assertIn("SKIP_ABSENT python-3.99", out)
        self.assertIn("0 task(s) ran and passed", out)
        self.assertNotIn("PASS    pytest", out)
        code, out = repo.run("--strict-absent")
        self.assertEqual(1, code, out)

    def test_runtime_older_than_min_version_is_skip_absent(self):
        data = manifest_of(row("r", ["tools/a.ps1"], ["pwsh"], [{"kind": "ps-parse"}], min_versions={"pwsh": "7.4"}))
        repo = FixtureRepo(self, data, {"tools/a.ps1": PORTABLE}, {"tools/a.ps1": PORTABLE + "# c\n"})
        old = lambda rid: {"id": rid, "argv": ["never-run"], "version": "7.2.0"}
        code, out = repo.run(detect=old)
        self.assertEqual(0, code, out)
        self.assertIn("SKIP_ABSENT pwsh", out)
        self.assertIn("need >=7.4", out)

    def test_slow_suite_is_skipped_unless_asked(self):
        data = manifest_of(row("r", ["tools/a.py"], [THIS_PY], [{"kind": "pytest", "path": "tools/test_a.py", "slow": True}]))
        repo = FixtureRepo(self, data, {"tools/a.py": "x = 1\n", "tools/test_a.py": "def test_a():\n    assert False\n"},
                           {"tools/a.py": "x = 2\n"})
        code, out = repo.run()
        self.assertEqual(0, code, out)
        self.assertIn("SKIP_SLOW", out)

    def test_undeclared_row_is_listed_and_its_pytest_suite_runs_once(self):
        data = manifest_of(row("u", ["tools/a.ps1"], [], [{"kind": "pytest", "path": "tools/test_a.py"}]))
        repo = FixtureRepo(self, data, {"tools/a.ps1": PORTABLE, "tools/test_a.py": "def test_a():\n    assert True\n"},
                           {"tools/a.ps1": PORTABLE + "# c\n"})
        code, out = repo.run("--list")
        self.assertEqual(0, code, out)
        self.assertIn("UNDECLARED_FLOOR u", out)
        self.assertIn("WOULD-RUN pytest tools/test_a.py [default]", out)

    @unittest.skipUnless(HAVE_PYTEST, "pytest not installed")
    def test_pytest_suite_red_and_green_on_the_declared_python(self):
        data = manifest_of(row("r", ["tools/a.py"], [THIS_PY], [{"kind": "pytest", "path": "tools/test_a.py::TestA"}]))
        suite = "class TestA:\n    def test_a(self):\n        from tools import a\n        assert a.X == {want}\n"
        files = {"tools/__init__.py": "", "tools/a.py": "X = 1\n", "tools/test_a.py": suite.format(want=1)}
        repo = FixtureRepo(self, data, files, {"tools/a.py": "X = 1\n# edit\n"})
        code, out = repo.run()
        self.assertEqual(0, code, out)
        self.assertIn("PASS", out)
        self.assertIn(f"[{THIS_PY}]", out)
        (repo.root / "tools/a.py").write_text("X = 2\n", encoding="utf-8")
        repo._commit("break")
        code, out = repo.run()
        self.assertEqual(1, code, out)
        self.assertIn("FAIL", out)

    @unittest.skipUnless(HAVE_PYTEST, "pytest not installed")
    def test_run_leaves_the_worktree_untouched(self):
        data = manifest_of(row("r", ["tools/a.py"], [THIS_PY], [{"kind": "pytest", "path": "tools/test_a.py"}]))
        files = {"tools/__init__.py": "", "tools/a.py": "X = 1\n", "tools/test_a.py": "def test_a():\n    assert True\n"}
        repo = FixtureRepo(self, data, files, {"tools/a.py": "X = 2\n"})
        before = repo.porcelain()
        code, out = repo.run()
        self.assertEqual(0, code, out)
        self.assertEqual(before, repo.porcelain())

    @unittest.skipUnless(HAVE_5_1 and HAVE_PWSH, "needs both powershell.exe 5.1 and pwsh 7")
    def test_script_suite_passes_on_pwsh7_fails_on_5_1_red_then_green(self):
        data = manifest_of(row("r", ["tools/a.ps1"], ["powershell-5.1", "pwsh"], [{"kind": "ps-script", "path": "tools/t.ps1"}]))
        repo = FixtureRepo(self, data, {"tools/a.ps1": PORTABLE, "tools/t.ps1": SEVEN_ONLY}, {"tools/a.ps1": PORTABLE + "# c\n"})
        code, out = repo.run()
        self.assertEqual(1, code, out)
        self.assertRegex(out, r"FAIL\s+ps-script tools/t\.ps1 \[powershell-5\.1\]")
        self.assertRegex(out, r"PASS\s+ps-script tools/t\.ps1 \[pwsh\]")
        (repo.root / "tools/t.ps1").write_text(PORTABLE, encoding="utf-8")
        repo._commit("fix")
        code, out = repo.run()
        self.assertEqual(0, code, out)
        self.assertRegex(out, r"PASS\s+ps-script tools/t\.ps1 \[powershell-5\.1\]")
        self.assertRegex(out, r"PASS\s+ps-script tools/t\.ps1 \[pwsh\]")

    @unittest.skipUnless(HAVE_5_1 and HAVE_PWSH, "needs both powershell.exe 5.1 and pwsh 7")
    def test_parse_suite_catches_a_7_only_operator_in_the_changed_tool(self):
        data = manifest_of(row("r", ["tools/a.ps1"], ["powershell-5.1", "pwsh"], [{"kind": "ps-parse"}]))
        repo = FixtureRepo(self, data, {"tools/a.ps1": PORTABLE}, {"tools/a.ps1": SEVEN_ONLY})
        code, out = repo.run()
        self.assertEqual(1, code, out)
        self.assertRegex(out, r"FAIL\s+ps-parse \(parse\) \[powershell-5\.1\]")
        self.assertIn("PARSE-ERR", out)
        self.assertRegex(out, r"PASS\s+ps-parse \(parse\) \[pwsh\]")
        (repo.root / "tools/a.ps1").write_text(PORTABLE + "# fixed\n", encoding="utf-8")
        repo._commit("fix")
        code, out = repo.run()
        self.assertEqual(0, code, out)

    @unittest.skipUnless(HAVE_5_1 and HAVE_PWSH, "needs both powershell.exe 5.1 and pwsh 7")
    def test_parse_suite_handles_a_quote_and_space_in_the_path(self):
        name = "tools/it's a tool.ps1"
        data = manifest_of(row("r", ["tools/*.ps1"], ["powershell-5.1", "pwsh"], [{"kind": "ps-parse"}]))
        repo = FixtureRepo(self, data, {name: PORTABLE}, {name: PORTABLE + "# c\n"})
        code, out = repo.run()
        self.assertEqual(0, code, out)


class ChangedFilesPathTests(unittest.TestCase):
    """DG-GIT-PATHLIST: git quotes/escapes a special path in --name-only output unless -z is used, and the text
    decode of a non-ASCII name is locale-dependent; the runner must hand back the REAL path so a row can match it."""

    def test_non_ascii_and_spaced_paths_come_back_real_and_map_to_their_row(self):
        committed = "tools/coordination/café tool.ps1"
        uncommitted = "tools/coordination/naïve tool.ps1"
        names = [committed, uncommitted]
        if sys.platform != "win32":
            quoted = 'tools/coordination/say "hi" \\ tool.ps1'
            names.append(quoted)
        data = manifest_of(row("coord", ["tools/coordination/*.ps1"], [], []))
        repo = FixtureRepo(self, data, {uncommitted: PORTABLE}, {name: PORTABLE for name in names if name != uncommitted})
        (repo.root / uncommitted).write_text(PORTABLE + "# edit\n", encoding="utf-8", newline="\n")
        merge_base, changed = rfs.changed_files(repo.root, "base")
        self.assertRegex(merge_base, r"^[0-9a-f]{40}$")
        self.assertEqual(sorted(names), changed)
        matched = rfs.match_rows(data, changed)
        self.assertEqual([("coord", sorted(names))], [(r["id"], because) for r, because in matched])
        plan = rfs.build_plan(matched, repo.root)
        self.assertEqual([sorted(names)], [t["files"] for t in plan if t["kind"] == "row-note"])

    def test_a_backslash_in_a_git_reported_path_is_kept_on_every_os(self):
        """git writes ``/`` as the separator in -z output on every OS, so a ``\\`` in a reported name is a legal
        POSIX filename character; rewriting it names a different, nonexistent file. Reached without creating
        such a file, so the Windows leg covers the defect the ubuntu leg found."""
        real = 'tools/coordination/say "hi" \\ tool.ps1'
        sha = "a" * 40

        def fake_git(worktree, *args, nul=False):
            if args[0] == "merge-base":
                return [sha]
            if args[0] == "diff":
                return [real] if f"{sha}..HEAD" in args else []
            return ["fake-git-dir"]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rfs, "_git", fake_git):
            merge_base, changed = rfs.changed_files(Path(tmp), "base")
        self.assertEqual(sha, merge_base)
        self.assertEqual([real], changed)

    def test_nul_records_split_without_touching_a_backslash(self):
        raw = 'tools/a\\b.ps1\0tools/café tool.ps1\0'.encode("utf-8")
        self.assertEqual(['tools/a\\b.ps1', 'tools/café tool.ps1'], rfs._records(raw, nul=True))
        self.assertEqual(['one', 'two'], rfs._records(b"one\ntwo\n", nul=False))


class FakeProc:
    """Stands in for a Popen child: ``communicate`` pops scripted results (an exception is raised, not returned)."""

    def __init__(self, pid=4242, results=(("ok", None),), returncode=0):
        self.pid, self.returncode, self.results, self.killed = pid, returncode, list(results), 0

    def communicate(self, timeout=None):
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def kill(self):
        self.killed += 1


class RelativeWorktreeTests(unittest.TestCase):
    """CoS B1 (head e5dd4b47): a relative --worktree was kept as given, prefixed onto the PowerShell file
    arguments, AND set as the child cwd, so ``lane/tools/a.ps1`` was looked up under ``lane/lane/tools/a.ps1``."""

    @staticmethod
    def _detect(rid):
        return {"id": rid, "argv": [rid + "-exe"], "version": "9.9.9"}

    @staticmethod
    def _parse_files(argv):
        snippet = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
        listed = re.search(r"foreach \(\$f in @\((.*?)\)\) \{", snippet).group(1)
        return [name.replace("''", "'") for name in re.findall(r"'((?:[^']|'')*)'", listed)]

    def _drive_from_parent(self, suites, runtimes):
        data = manifest_of(row("r", ["tools/a.ps1"], runtimes, suites))
        base = {"tools/a.ps1": PORTABLE, "tools/t.ps1": PORTABLE, "tools/test_a.py": "def test_a():\n    pass\n"}
        repo = FixtureRepo(self, data, base, {"tools/a.ps1": PORTABLE + "# c\n"})
        calls = []

        def fake_spawn(argv, **kwargs):
            calls.append((list(argv), kwargs.get("cwd")))
            return FakeProc()

        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(str(repo.root.parent))
        buffer = io.StringIO()
        with mock.patch.object(rfs, "_spawn", fake_spawn), contextlib.redirect_stdout(buffer):
            code = rfs.main(["--worktree", repo.root.name, "--base", "base", "--manifest",
                             str(repo.root / "manifest.json"), "--timeout", "120"], detect=self._detect)
        self.assertEqual(0, code, buffer.getvalue())
        return repo, calls

    def _assert_resolves(self, repo, cwd, name):
        resolved = Path(os.path.abspath(cwd)) / name
        self.assertTrue(resolved.is_file(), f"{name!r} resolved against child cwd {cwd!r} names {resolved}, not a file")
        self.assertEqual(repo.root.resolve(), Path(os.path.abspath(cwd)).resolve())

    def test_relative_worktree_ps_script_file_argument_names_an_existing_file(self):
        repo, calls = self._drive_from_parent([{"kind": "ps-script", "path": "tools/t.ps1"}], ["pwsh"])
        self.assertEqual(1, len(calls), calls)
        argv, cwd = calls[0]
        self._assert_resolves(repo, cwd, argv[argv.index("-File") + 1])

    def test_relative_worktree_ps_parse_file_arguments_name_existing_files(self):
        repo, calls = self._drive_from_parent([{"kind": "ps-parse"}], ["pwsh"])
        self.assertEqual(1, len(calls), calls)
        argv, cwd = calls[0]
        files = self._parse_files(argv)
        self.assertEqual(1, len(files), files)
        for name in files:
            self._assert_resolves(repo, cwd, name)

    def test_relative_worktree_pytest_path_stays_the_relative_suite_path(self):
        repo, calls = self._drive_from_parent([{"kind": "pytest", "path": "tools/test_a.py"}], [THIS_PY])
        self.assertEqual(1, len(calls), calls)
        argv, cwd = calls[0]
        self.assertIn("tools/test_a.py", argv)
        self._assert_resolves(repo, cwd, "tools/test_a.py")


class TimeoutKillTests(unittest.TestCase):
    """RUNTIME-FLOOR-TASKKILL-TIMEOUT-1: a hung taskkill.exe must not hang the runner, and a POSIX timeout must
    reach grandchildren, so the child starts in its own session and the whole group is killed."""

    TASK = {"kind": "pytest", "suite": "tools/test_a.py", "runtime": None, "row": "r", "argv_prefix": ["py"]}
    EXPIRED = subprocess.TimeoutExpired("suite", 1)

    def _run(self, proc, nt):
        spawned = []

        def fake_spawn(argv, **kwargs):
            spawned.append(kwargs)
            return proc

        with mock.patch.object(rfs, "_spawn", fake_spawn), mock.patch.object(rfs, "_IS_NT", nt):
            return rfs.run_task(self.TASK, Path(tempfile.gettempdir()), 1), spawned[0]

    def test_a_hung_taskkill_is_bounded_and_falls_back_to_killing_the_child(self):
        proc = FakeProc(results=[self.EXPIRED, ("late", None)])
        calls = []

        def hung_taskkill(argv, **kwargs):
            calls.append((argv, kwargs))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        with mock.patch.object(rfs.subprocess, "run", hung_taskkill):
            result, spawn_kwargs = self._run(proc, nt=True)
        self.assertEqual("TIMEOUT", result["status"])
        self.assertEqual(1, len(calls), calls)
        self.assertEqual("taskkill.exe", calls[0][0][0])
        self.assertIsNotNone(calls[0][1].get("timeout"), "taskkill.exe must be run with a timeout")
        self.assertEqual(1, proc.killed)
        self.assertNotIn("start_new_session", spawn_kwargs)

    def test_posix_timeout_kills_the_whole_process_group_of_a_child_in_its_own_session(self):
        proc = FakeProc(pid=777, results=[self.EXPIRED, ("late", None)])
        killpg = mock.Mock()
        with mock.patch.object(rfs.os, "killpg", killpg, create=True):
            result, spawn_kwargs = self._run(proc, nt=False)
        self.assertEqual("TIMEOUT", result["status"])
        self.assertIs(True, spawn_kwargs.get("start_new_session"))
        killpg.assert_called_once_with(777, rfs._KILL_SIGNAL)

    def test_posix_group_kill_falls_back_to_the_child_when_the_group_is_gone(self):
        proc = FakeProc(pid=778, results=[self.EXPIRED, ("late", None)])
        with mock.patch.object(rfs.os, "killpg", mock.Mock(side_effect=ProcessLookupError), create=True):
            result, _ = self._run(proc, nt=False)
        self.assertEqual("TIMEOUT", result["status"])
        self.assertEqual(1, proc.killed)


if __name__ == "__main__":
    unittest.main()
