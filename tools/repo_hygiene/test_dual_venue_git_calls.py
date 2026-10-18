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
    partial bytes), never git's own exit code;
  * (r3, CoS N3) a git that cannot be started is a typed ``DV_GIT_START_FAILED`` refusal and leaks no job object;
  * (r3, CoS N4) the straggler message says "(killed)" only when the kill is confirmed, "(kill not confirmed)" otherwise.
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


def _in_module(case: unittest.TestCase, body: str, env_extra: dict[str, str] | None = None) -> str:
    """Run ``body`` inside the module scope (Invoke-DvGit, its script-scope seams, counters and cache are module-private). Returns stdout."""
    script = _import(f"& (Get-Module DualVenueRunner) {{\n{body}\n}}\n")
    proc = subprocess.run([PWSH, *PWSH_ARGS, "-Command", script], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                          env=dict(os.environ, **(env_extra or {})))
    case.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
    return proc.stdout


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

    def test_the_blob_cache_keeps_repos_that_differ_only_by_case_apart(self) -> None:
        # HARDENING-2 (3), sol on #472: the default PowerShell hashtable compares keys case-insensitively, so /tmp/Repo and /tmp/repo (two repos
        # on a case-sensitive filesystem) shared one entry. A blob id names its bytes, so no wrong measurement was shown; the cache is still
        # ordinal so that it can never be wrong about which repository a hit belongs to.
        blob = "a" * 40
        out = _in_module(self,
                         f"$c = $script:LegBlobCache\n$c['/tmp/Repo' + [char]0 + '{blob}'] = 1\n"
                         f"Write-Output ('HIT ' + $c.ContainsKey('/tmp/repo' + [char]0 + '{blob}'))\n")
        self.assertIn("HIT False", out, "repo paths differing only by case must be separate cache entries")

    def test_the_blob_cache_normalises_the_repo_root_spelling(self) -> None:
        # HARDENING-2 (3), fable on #472: 'C:\r' and 'C:\r\' (or r/../r) are one repo and must be one cache entry per blob, not one per spelling.
        repo, head = self.make_repo()
        sep = os.sep
        spellings = [str(repo), str(repo) + sep, str(repo.parent) + sep + "." + sep + repo.name]
        lookups = "".join(
            f"$r = Find-DvCommittedLegSpec -RepoRoot '{spelling}' -Commit '{head}' -LegSpecSha256 $sha\n" for spelling in spellings)
        out = _in_module(self,
                         f"$f = Get-ChildItem -LiteralPath (Join-Path '{repo}' '{LEGS_REL}') -Filter leg-{LEG_COUNT - 1:02d}.json\n"
                         f"$sha = Get-DvLegSpecSha256 ([IO.File]::ReadAllBytes($f.FullName))\n{lookups}"
                         "Write-Output ('CACHED ' + $script:LegBlobCache.Count)\n")
        match = re.search(r"CACHED (\d+)", out)
        self.assertIsNotNone(match, out)
        # The last leg is found after reading every blob before it, so the cache holds LEG_COUNT entries for ONE spelling of the repo.
        self.assertEqual(int(match.group(1)), LEG_COUNT, f"three spellings of one repo made {match.group(1)} cache entries, expected {LEG_COUNT}")


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
        # CoS N4: "(killed)" is claimed because the call's pipes closed after the kill; "(kill not confirmed)" would mean they did not.
        self.assertIn("(killed)", done.stdout)
        self.assertNotIn("kill not confirmed", done.stdout)
        self.assertNotIn("partial", done.stdout.split("ERR ", 1)[0], "no partial bytes may be returned")
        self.assertLess(time.monotonic() - started, 30, "the drain bound is the 5 s git bound; the call must not wait out the 60 s child")
        deadline = time.monotonic() + 10
        survivors = _sleepers() - before
        while survivors and time.monotonic() < deadline:
            time.sleep(0.5)
            survivors = _sleepers() - before
        self.assertEqual(survivors, set(), "the sleeper git started must be killed, not left running")

    def in_module(self, body: str, env_extra: dict[str, str] | None = None) -> str:
        return _in_module(self, body, env_extra)

    @unittest.skipUnless(sys.platform == "win32", "job objects exist on Windows only (on Linux and macOS opened = closed = 0)")
    def test_a_successful_git_call_opens_and_closes_its_job_object(self) -> None:
        # HARDENING-2 (1), fable on #472: the N3 test above only covers a failed Start (opened = closed = 0). The CloseHandle in the finally of a
        # call that DID start is pinned here: a module without it leaves opened > closed.
        repo = self.make_repo()
        out = self.in_module(
            "for ($i = 0; $i -lt 3; $i++) { $r = Invoke-DvGit -RepoRoot '" + str(repo) + "' -GitArgs @('rev-parse', '--is-inside-work-tree'); Write-Output ('EXIT ' + $r.exitCode) }\n"
            "Write-Output ('JOBS opened=' + $script:DvJobsOpened + ' closed=' + $script:DvJobsClosed)\n")
        self.assertEqual(out.count("EXIT 0"), 3, out)
        match = re.search(r"JOBS opened=(\d+) closed=(\d+)", out)
        self.assertIsNotNone(match, out)
        self.assertEqual(match.group(1), "3", f"each started git call opens one job object: {out}")
        self.assertEqual(match.group(1), match.group(2), "every job object a started call opened must be closed")

    def test_a_faulted_pipe_read_is_a_typed_refusal_not_an_exception(self) -> None:
        # HARDENING-2 (2), fable on #472: the drain WaitAll was outside any try, so a faulted output read (here: a sink that cannot hold git's
        # output, a non-expandable one-byte MemoryStream) escaped Invoke-DvGit as an AggregateException. Now: exit -1, no bytes, DV_GIT_PIPE_FAULT.
        repo = self.make_repo()
        out = self.in_module(
            "$script:DvGitNewSink = { [IO.MemoryStream]::new([byte[]]::new(1)) }\n"
            f"try {{ $r = Invoke-DvGit -RepoRoot '{repo}' -GitArgs @('rev-parse', '--is-inside-work-tree'); Write-Output ('EXIT ' + $r.exitCode); Write-Output ('BYTES ' + $r.bytes.Length); Write-Output ('ERR ' + $r.stderr) }}\n"
            "catch { Write-Output ('THREW ' + $_.Exception.GetType().Name) }\n"
            "Write-Output ('JOBS opened=' + $script:DvJobsOpened + ' closed=' + $script:DvJobsClosed)\n")
        self.assertNotIn("THREW", out, "a faulted pipe read must be a typed refusal, not an exception")
        self.assertIn("EXIT -1", out)
        self.assertIn("BYTES 0", out)
        self.assertIn("ERR DV_GIT_PIPE_FAULT", out)
        match = re.search(r"JOBS opened=(\d+) closed=(\d+)", out)
        self.assertIsNotNone(match, out)
        self.assertEqual(match.group(1), match.group(2), "the fault path must still close its job object")

    def test_the_timeout_override_accepts_ascii_digits_only(self) -> None:
        # HARDENING-2 (5), fable on #472: .NET's \d matches any Unicode decimal digit, and the [int] cast of Arabic-Indic digits throws. The
        # override is [0-9]{1,3}; anything else is the 120 s default.
        out = self.in_module(
            "$arabic = ([string][char]0x0663) + ([string][char]0x0660)\n"
            "foreach ($v in @($arabic, '30', '601', '0', 'off')) {\n"
            "  $env:DV_GIT_TIMEOUT_SECONDS = $v\n"
            "  try { Write-Output ('MS ' + (Get-DvGitTimeoutMilliseconds)) } catch { Write-Output ('THREW ' + $_.Exception.GetType().Name) }\n"
            "}\n")
        self.assertNotIn("THREW", out, "a non-ASCII digit override must fall to the default, never throw")
        self.assertEqual([line for line in out.splitlines() if line.startswith("MS ")], ["MS 120000", "MS 30000", "MS 120000", "MS 120000", "MS 120000"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "the escape needs /proc/<pid>/environ tag discovery (Linux) and POSIX env/sleep")
    def test_a_straggler_that_scrubs_its_environment_is_refused_with_kill_not_confirmed(self) -> None:
        # CI-DUALVENUE-GIT-KILL-NOT-CONFIRMED-ROW-1 (CoS #472 N5): `env -i` drops the per-call tag, so Stop-DvGitCall cannot find the child by
        # /proc/<pid>/environ and its parent (sh) is gone, so Kill($true) misses it too: the pipes stay open and the kill is NOT confirmed. The
        # refusal is the same typed exit -1 with no bytes; only the wording differs. Then the test kills the survivor itself.
        before = _sleepers("61")
        self.addCleanup(_kill_new_sleepers, before, "61")
        done = self.invoke_dv_git(self.make_repo(), "'-c', 'alias.dvscrub=!echo partial; env -i /bin/sleep 61 & exit 0', 'dvscrub'", {"DV_GIT_TIMEOUT_SECONDS": "5"}, timeout=60)
        self.assertIn("EXIT -1", done.stdout)
        self.assertIn("DV_GIT_PIPE_STRAGGLER", done.stdout)
        self.assertIn("(kill not confirmed)", done.stdout)
        self.assertNotIn("(killed)", done.stdout)
        self.assertNotIn("partial", done.stdout.split("ERR ", 1)[0], "no partial bytes may be returned")
        self.assertEqual(done.stdout.split("OUT ", 1)[1].split("\n", 1)[0].strip(), "", "no bytes at all")
        self.assertTrue(_sleepers("61") - before, "the scrubbed child must have escaped the kill: that is what makes the kill 'not confirmed'")

    def test_a_git_that_cannot_be_started_fails_typed_and_leaks_no_job_handle(self) -> None:
        # CoS N3 (PR #472, comment 6228350481): r2 created the job object BEFORE Process.Start and ran Start outside the try, so a Start that
        # threw leaked the job handle (Windows) and escaped as an exception. Now: typed exit -1, and every job opened is closed. On Linux and
        # macOS no job object exists (opened = closed = 0), so there the test pins the typed failure only.
        repo = self.make_repo()
        out = self.in_module(
            "$script:DvGitExecutable = 'dv-no-such-git-binary'\n"
            "for ($i = 0; $i -lt 3; $i++) {\n"
            f"  try {{ $r = Invoke-DvGit -RepoRoot '{repo}' -GitArgs @('rev-parse', 'HEAD'); Write-Output ('EXIT ' + $r.exitCode); Write-Output ('BYTES ' + $r.bytes.Length); Write-Output ('ERR ' + $r.stderr) }}\n"
            "  catch { Write-Output ('THREW ' + $_.Exception.Message) }\n"
            "}\n"
            "Write-Output ('JOBS opened=' + $script:DvJobsOpened + ' closed=' + $script:DvJobsClosed)\n")
        self.assertNotIn("THREW", out, "a git that cannot be started must be a typed refusal, not an exception")
        self.assertEqual(out.count("EXIT -1"), 3, out)
        self.assertEqual(out.count("BYTES 0"), 3, out)
        self.assertEqual(out.count("ERR DV_GIT_START_FAILED"), 3, out)
        match = re.search(r"JOBS opened=(\d+) closed=(\d+)", out)
        self.assertIsNotNone(match, out)
        self.assertEqual(match.group(1), match.group(2), "every job object opened must be closed (a Start that throws must not leak one)")

    def test_the_straggler_message_claims_a_kill_only_when_it_is_confirmed(self) -> None:
        # CoS N4: macOS reaches only the direct child, so the message must not say "(killed)" unless the kill was confirmed. The refusal is the
        # same typed exit -1 either way; only the wording differs.
        out = self.in_module(
            "$a = Get-DvStragglerMessage -GitExit 0 -Seconds 5 -KillConfirmed $true -GitArgs @('log', '-1')\n"
            "$b = Get-DvStragglerMessage -GitExit 0 -Seconds 5 -KillConfirmed $false -GitArgs @('log', '-1')\n"
            "Write-Output ('CONFIRMED ' + $a)\nWrite-Output ('UNCONFIRMED ' + $b)\n")
        confirmed = next(line for line in out.splitlines() if line.startswith("CONFIRMED "))
        unconfirmed = next(line for line in out.splitlines() if line.startswith("UNCONFIRMED "))
        for line in (confirmed, unconfirmed):
            self.assertIn("DV_GIT_PIPE_STRAGGLER", line)
            self.assertIn("git exited 0 but a process it started still held its output open after 5 s", line)
            self.assertTrue(line.endswith("git log -1"), line)
        self.assertIn("(killed)", confirmed)
        self.assertNotIn("kill not confirmed", confirmed)
        self.assertIn("(kill not confirmed)", unconfirmed)
        self.assertNotIn("(killed)", unconfirmed)


def _sleeper_pids_from_ps(ps_output: str, seconds: str = "60") -> set[int]:
    """Parse `ps -axo pid=,args=` (macOS has no /proc): the pids whose argv is `sleep <seconds>`."""
    found: set[int] = set()
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and os.path.basename(parts[1]) in ("sleep", "sleep.exe") and parts[2] == seconds:
            found.add(int(parts[0]))
    return found


def _sleepers(seconds: str = "60") -> set[int]:
    """Pids of the processes running `sleep <seconds>` (the straggler of the aliases above). Compared as a before/after set: other work on the
    host may run its own sleeps, and only a NEW one is this test's. HARDENING-2 (4): Windows asks CIM, Linux reads /proc, anything else with
    no /proc (macOS) asks ps."""
    found: set[int] = set()
    if sys.platform == "win32":
        pattern = re.compile(r'sleep(\.exe)?"?\s+' + re.escape(seconds) + r'\s*$')
        query = "Get-CimInstance Win32_Process -Filter \"Name='sleep.exe'\" | ForEach-Object { '' + $_.ProcessId + [char]9 + $_.CommandLine }"
        out = subprocess.run([PWSH, *PWSH_ARGS, "-Command", query], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        for line in out.splitlines():
            pid, _, cmd = line.partition("\t")
            if pid.strip().isdigit() and pattern.search(cmd.strip()):
                found.add(int(pid))
        return found
    if not Path("/proc").is_dir():
        return _sleeper_pids_from_ps(subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout, seconds)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 2 and os.path.basename(argv[0]) in (b"sleep", b"sleep.exe") and argv[1] == seconds.encode():
            found.add(int(entry.name))
    return found


def _kill_new_sleepers(before: set[int], seconds: str = "60") -> None:
    """Cleanup only (the assertion above is the check): a RED run must not leave its sleeper behind."""
    for pid in _sleepers(seconds) - before:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


class SleeperDiscoveryTests(unittest.TestCase):
    """HARDENING-2 (4): process discovery must not assume /proc on every non-Windows platform (macOS has none)."""

    def test_the_ps_form_finds_exactly_the_sleep_of_the_requested_duration(self) -> None:
        listing = ("  101 /bin/sleep 60\n  102 sleep 61\n  103 /usr/bin/sleep 600\n  104 /usr/bin/python3 sleep 60\n  105 sleep\n  x 106 sleep 60\n"
                   " 2107 -bash\n")
        self.assertEqual(_sleeper_pids_from_ps(listing), {101})
        self.assertEqual(_sleeper_pids_from_ps(listing, "61"), {102})

    @unittest.skipIf(sys.platform == "win32" or Path("/proc").is_dir(), "the ps path is for platforms without /proc (macOS)")
    def test_discovery_runs_where_there_is_no_proc(self) -> None:
        self.assertIsInstance(_sleepers(), set)   # on macOS this used to raise FileNotFoundError at Path('/proc').iterdir()


if __name__ == "__main__":
    unittest.main()
