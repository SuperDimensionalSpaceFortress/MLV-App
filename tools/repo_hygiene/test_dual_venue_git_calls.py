"""CI-DUALVENUE-EVIDENCE-SHARD2-HANG-1: the dual-venue module's git calls are bounded and cheap.

Hosted Windows CI shard 2/8 hit its 40 minute job cap on two attempts of the same run, with no test id on the last line. It was not a hang:
``Find-DvCommittedLegSpec`` read EVERY blob of the legs directory (``git cat-file -t`` plus ``git cat-file blob``) on every call, and the
LineEndings tests call it once per shipped leg, so the git spawn count grew with the SQUARE of the leg count (about 22 minutes for four
tests once GPU-DEVICE-LOSS-SELF-HEAL-1 added a leg). The same helper had no bound at all: a git child that never returned (an open stdin, a
credential prompt, a pager) would block the whole shard until the job cap killed it.

Pinned here:
  * finding every leg spec costs git spawns LINEAR in the leg count (a blob is read once per process, whatever the number of lookups);
  * ``Invoke-DvGit`` returns a typed ``DV_GIT_TIMEOUT`` failure (exit -1, never an admission) when git does not return within the bound;
  * ``Invoke-DvGit`` closes git's stdin and turns prompts and the pager off;
  * (r2, CoS I1) a child that outlives git holding its output is killed and is a typed ``DV_GIT_PIPE_STRAGGLER`` failure (exit -1, no
    partial bytes), never git's own exit code.
The tests run wherever pwsh does (Windows and the ubuntu shards; CoS N2).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "dual-venue" / "DualVenueRunner.psm1"
LEGS_REL = "tools/profiling/dual-venue/legs"
PWSH = shutil.which("pwsh")
# r1: requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")
# CoS N2: the tests pass on Linux pwsh too, so they also run on the ubuntu shards. The one skip left is the one they genuinely need: no pwsh.
requires_pwsh = unittest.skipIf(PWSH is None, "needs pwsh on PATH (the module under test is PowerShell)")
requires_windows_pwsh = requires_pwsh   # the r1 name, kept so nothing that imports it breaks; it no longer means Windows only
PWSH_ARGS = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
LEG_COUNT = 16


def _import(extra: str) -> str:
    return f"Import-Module '{MODULE}' -Force\n{extra}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


@requires_windows_pwsh
class LegLookupCostTests(unittest.TestCase):
    """The probe is ONE pwsh process (as the runner and every caller are): a blob read for one lookup is not read again for the next."""

    def make_repo(self) -> tuple[Path, str]:
        tmp = tempfile.TemporaryDirectory(prefix="dve-lookup-")
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        legs = repo / "tools" / "profiling" / "dual-venue" / "legs"
        legs.mkdir(parents=True)
        for n in range(LEG_COUNT):
            (legs / f"leg-{n:02d}.json").write_bytes(f'{{"legId": "leg-{n:02d}", "n": {n}}}\n'.encode())
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "unit@example.invalid")
        _git(repo, "config", "user.name", "unit")
        _git(repo, "config", "core.autocrlf", "false")
        _git(repo, "add", "tools")
        _git(repo, "commit", "-q", "-m", "legs")
        return repo, _git(repo, "rev-parse", "HEAD")

    def test_finding_every_leg_spec_costs_git_spawns_linear_in_the_leg_count(self) -> None:
        repo, head = self.make_repo()
        trace = Path(tempfile.mkdtemp(prefix="dve-trace-")) / "git-trace.txt"
        self.addCleanup(shutil.rmtree, trace.parent, True)
        script = _import(
            f"$root = '{repo}'\n"
            f"foreach ($f in Get-ChildItem -LiteralPath (Join-Path $root '{LEGS_REL}') -Filter *.json) {{\n"
            "  $sha = Get-DvLegSpecSha256 ([IO.File]::ReadAllBytes($f.FullName))\n"
            f"  $r = Find-DvCommittedLegSpec -RepoRoot $root -Commit '{head}' -LegSpecSha256 $sha\n"
            "  Write-Output ('LEG ' + $f.Name + ' found=' + $r.ok + ' path=' + $r.relativePath)\n}\n")
        env = dict(os.environ, GIT_TRACE=str(trace))
        proc = subprocess.run([PWSH, *PWSH_ARGS, "-Command", script], capture_output=True, text=True, timeout=300, env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for n in range(LEG_COUNT):
            self.assertIn(f"LEG leg-{n:02d}.json found=True path={LEGS_REL}/leg-{n:02d}.json", proc.stdout)
        spawns = [line for line in trace.read_text(encoding="utf-8", errors="replace").splitlines() if "trace: built-in:" in line]
        # Linear: per lookup one commit check and one listing (2), plus each blob read once (2 each). Quadratic is ~LEG_COUNT * (2 + LEG_COUNT) = 288.
        self.assertLessEqual(len(spawns), 6 * LEG_COUNT,
                             f"{len(spawns)} git spawns to find {LEG_COUNT} legs: every lookup re-reads every blob (quadratic in the leg count)")


@requires_windows_pwsh
class GitCallsAreBoundedTests(unittest.TestCase):
    def make_repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="dve-gitbound-", ignore_cleanup_errors=True)   # a leaked git child may still hold the directory when the bound is missing
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        return repo

    def invoke_dv_git(self, repo: Path, git_args: str, env_extra: dict[str, str] | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
        """Invoke-DvGit is module-private: call it inside the module scope, as the module's own functions do."""
        script = _import(
            f"$r = & (Get-Module DualVenueRunner) {{ Invoke-DvGit -RepoRoot '{repo}' -GitArgs @({git_args}) }}\n"
            "Write-Output ('EXIT ' + $r.exitCode)\nWrite-Output ('OUT ' + [Text.Encoding]::UTF8.GetString($r.bytes).Trim())\nWrite-Output ('ERR ' + $r.stderr)\n")
        env = dict(os.environ, **(env_extra or {}))
        proc = subprocess.Popen([PWSH, *PWSH_ARGS, "-Command", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail(f"Invoke-DvGit did not return within {timeout} s: git blocked (open stdin, prompt, pager or a child that never exits)")
        finally:
            proc.stdin.close()
        out, err = proc.stdout.read(), proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(proc.returncode, 0, out + err)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def test_a_git_call_that_never_returns_fails_typed_within_the_bound(self) -> None:
        started = time.monotonic()
        done = self.invoke_dv_git(self.make_repo(), "'-c', 'alias.dvhang=!sleep 45', 'dvhang'", {"DV_GIT_TIMEOUT_SECONDS": "3"})
        self.assertLess(time.monotonic() - started, 25, "the bound is 3 s; the call must not wait out the 45 s child")
        self.assertIn("EXIT -1", done.stdout)
        self.assertIn("DV_GIT_TIMEOUT", done.stdout)

    def test_a_call_that_returns_in_time_is_unchanged(self) -> None:
        done = self.invoke_dv_git(self.make_repo(), "'rev-parse', '--is-inside-work-tree'", {"DV_GIT_TIMEOUT_SECONDS": "30"})
        self.assertIn("EXIT 0", done.stdout)
        self.assertIn("OUT true", done.stdout)

    def test_git_gets_a_closed_stdin(self) -> None:
        # The parent keeps its own stdin pipe OPEN; a git that inherits it blocks reading it. A closed stdin reads EOF: the empty blob id.
        done = self.invoke_dv_git(self.make_repo(), "'hash-object', '--stdin'")
        self.assertIn("EXIT 0", done.stdout)
        self.assertIn("OUT e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", done.stdout)

    def test_git_runs_with_prompts_and_the_pager_off(self) -> None:
        done = self.invoke_dv_git(self.make_repo(), "'-c', 'alias.dvenv=!echo T=$GIT_TERMINAL_PROMPT P=$GIT_PAGER', 'dvenv'",
                                  {"GIT_TERMINAL_PROMPT": "1", "GIT_PAGER": "more"})
        self.assertIn("OUT T=0 P=cat", done.stdout)

    def test_a_child_that_outlives_git_holding_its_output_is_killed_and_fails_typed(self) -> None:
        # CoS I1 (PR #472, comment 6227363745), reproduced: git exits 0 but a process it started still holds stdout open. On 76999ae4 the call
        # returned git's own exit 0 with the partial bytes ("EXIT 0 OUT [partial]") after two unchecked 10 s waits, and the sleeper lived on.
        before = _sleepers()
        self.addCleanup(_kill_new_sleepers, before)
        started = time.monotonic()
        done = self.invoke_dv_git(self.make_repo(), "'-c', 'alias.dvs=!echo partial; sleep 60 & exit 0', 'dvs'", {"DV_GIT_TIMEOUT_SECONDS": "5"}, timeout=45)
        self.assertIn("EXIT -1", done.stdout, "a straggler holding git's output must fail typed, never return git's exit code with partial bytes")
        self.assertIn("DV_GIT_PIPE_STRAGGLER", done.stdout)
        self.assertNotIn("partial", done.stdout.split("ERR ", 1)[0], "no partial bytes may be returned")
        self.assertLess(time.monotonic() - started, 30, "the drain bound is the 5 s git bound; the call must not wait out the 60 s child")
        deadline = time.monotonic() + 10
        survivors = _sleepers() - before
        while survivors and time.monotonic() < deadline:
            time.sleep(0.5)
            survivors = _sleepers() - before
        self.assertEqual(survivors, set(), "the sleeper git started must be killed, not left running")


_SLEEPER = re.compile(r'sleep(\.exe)?"?\s+60\s*$')


def _sleepers() -> set[int]:
    """Pids of the processes running `sleep 60` (the straggler of the alias above). Compared as a before/after set: other work on the host may
    run its own sleeps, and only a NEW one is this test's."""
    found: set[int] = set()
    if sys.platform == "win32":
        query = "Get-CimInstance Win32_Process -Filter \"Name='sleep.exe'\" | ForEach-Object { '' + $_.ProcessId + [char]9 + $_.CommandLine }"
        out = subprocess.run([PWSH, *PWSH_ARGS, "-Command", query], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        for line in out.splitlines():
            pid, _, cmd = line.partition("\t")
            if pid.strip().isdigit() and _SLEEPER.search(cmd.strip()):
                found.add(int(pid))
        return found
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 2 and os.path.basename(argv[0]) in (b"sleep", b"sleep.exe") and argv[1] == b"60":
            found.add(int(entry.name))
    return found


def _kill_new_sleepers(before: set[int]) -> None:
    """Cleanup only (the assertion above is the check): a RED run must not leave its 60 s sleeper behind."""
    for pid in _sleepers() - before:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
