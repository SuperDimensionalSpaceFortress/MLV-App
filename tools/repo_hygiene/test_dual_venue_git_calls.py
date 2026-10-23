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
  * (r3, CoS N4) the straggler message says "(killed)" only when the kill is confirmed, "(kill not confirmed)" otherwise;
  * (DV-GIT-NATIVE-START-TYPED-FAILURE-1) a failed AssignProcessToJobObject, a failed ResumeThread, a throw building the pipe streams after the
    resume and a failed GetExitCodeProcess are each their own typed refusal (exit -1, no bytes; never a timeout, never a success, never a null
    that lets a second git start), and the child that was started is terminated.
The tests run wherever pwsh does (Windows and the ubuntu shards; CoS N2).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
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


def _is_git_spawn_trace(line: str) -> bool:
    """One GIT_TRACE line that marks one git run. A built-in (`rev-parse`) logs `trace: built-in:`; a shell alias (`alias.x=!cmd`) is not a
    built-in and logs `trace: exec: git-<alias>` instead, so counting only the first form read 0 for every alias run (DV-GIT-NATIVE-START-
    UNASSIGNED-CHILD-CLEANUP-1 U3: the "at most one git" checks of the alias tests were vacuous)."""
    return "trace: built-in:" in line or "trace: exec: git-" in line


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
        token = _own_sleep_token("60")
        self.addCleanup(_kill_sleepers, token)
        started = time.monotonic()
        done = self.invoke_dv_git(self.make_repo(), f"'-c', 'alias.dvs=!echo partial; sleep {token} & exit 0', 'dvs'", {"DV_GIT_TIMEOUT_SECONDS": "5"}, timeout=45)
        self.assertIn("EXIT -1", done.stdout, "a straggler holding git's output must fail typed, never return git's exit code with partial bytes")
        self.assertIn("DV_GIT_PIPE_STRAGGLER", done.stdout)
        # CoS N4: "(killed)" is claimed because the call's pipes closed after the kill; "(kill not confirmed)" would mean they did not.
        self.assertIn("(killed)", done.stdout)
        self.assertNotIn("kill not confirmed", done.stdout)
        self.assertNotIn("partial", done.stdout.split("ERR ", 1)[0], "no partial bytes may be returned")
        self.assertLess(time.monotonic() - started, 30, "the drain bound is the 5 s git bound; the call must not wait out the 60 s child")
        deadline = time.monotonic() + 10
        survivors = _sleepers(token)
        while survivors and time.monotonic() < deadline:
            time.sleep(0.5)
            survivors = _sleepers(token)
        self.assertEqual(survivors, set(), "the sleeper git started must be killed, not left running")

    @unittest.skipUnless(sys.platform == "win32", "job objects exist on Windows only")
    def test_git_is_in_the_job_before_it_can_start_a_child_however_long_the_parent_stalls(self) -> None:
        # CI-FLAKE-DV-GIT-PIPE-STRAGGLER-KILL-1: hosted Windows shard 2/8 failed this file's straggler test three times in 2.5 h with "(kill not
        # confirmed)". Process.Start followed by AssignProcessToJobObject left a window: a child git started before the assignment (`sh -c 'sleep & exit 0'`
        # is born, its parent exits) was never in the job, so TerminateJobObject missed it and Kill($true) had no parent chain to it. Measured on an
        # idle host, a 20-40 ms stall between the two calls was enough. The module now starts git suspended and resumes it once it is in the job, so
        # a stall at that point (the DvStartGate seam, 1.5 s here, 75 times the measured escape) cannot let anything out: the child has not run yet.
        token = _own_sleep_token("62")
        self.addCleanup(_kill_sleepers, token)
        repo = self.make_repo()
        out = self.in_module(
            "$script:GateRan = $false\n"
            "$script:DvStartGate = { Start-Sleep -Milliseconds 1500; $script:GateRan = $true }\n"
            f"$r = Invoke-DvGit -RepoRoot '{repo}' -GitArgs @('-c', 'alias.dvs=!echo partial; sleep {token} & exit 0', 'dvs')\n"
            "Write-Output ('GATE ' + $script:GateRan)\nWrite-Output ('EXIT ' + $r.exitCode)\nWrite-Output ('ERR ' + $r.stderr)\n"
            "Write-Output ('JOBS opened=' + $script:DvJobsOpened + ' closed=' + $script:DvJobsClosed)\n",
            {"DV_GIT_TIMEOUT_SECONDS": "3"})
        self.assertIn("GATE True", out, "the stall seam must have run between the child's creation and its job assignment")
        self.assertIn("EXIT -1", out)
        self.assertIn("DV_GIT_PIPE_STRAGGLER", out)
        self.assertIn("(killed)", out, "the child was in the job from its first instruction, so the kill must be confirmed")
        self.assertNotIn("kill not confirmed", out)
        match = re.search(r"JOBS opened=(\d+) closed=(\d+)", out)
        self.assertIsNotNone(match, out)
        self.assertEqual(match.group(1), match.group(2), "every job object opened must be closed")
        deadline = time.monotonic() + 10
        survivors = _sleepers(token)
        while survivors and time.monotonic() < deadline:
            time.sleep(0.5)
            survivors = _sleepers(token)
        self.assertEqual(survivors, set(), "the sleeper git started must be killed, not left running")

    def test_a_windows_argument_survives_the_suspended_start_byte_for_byte(self) -> None:
        # The suspended start builds git's command line itself (ConvertTo-DvWindowsArg), where Process.Start used ArgumentList: spaces, quotes,
        # backslash runs before a quote, a trailing backslash and shell metacharacters must reach git unchanged.
        # `git rev-parse --sq-quote <arg>` prints the argument git received, single-quoted. The cases travel as a JSON file, so no shell layer
        # between Python and the module can reshape them.
        cases = ["plain", "two words", 'say "hi"', "back\\", 'back\\"quote', "C:\\a b\\", "a&b;c|d", "tab\there", 'x\\\\"y']   # no empty case: Invoke-DvGit's mandatory [string[]] GitArgs refuses an empty element
        repo = self.make_repo()
        cases_file = Path(tempfile.mkdtemp(prefix="dve-args-")) / "cases.json"
        self.addCleanup(shutil.rmtree, cases_file.parent, True)
        cases_file.write_text(json.dumps(cases), encoding="utf-8")
        out = self.in_module(
            f"$cases = @(Get-Content -Raw -LiteralPath '{cases_file}' | ConvertFrom-Json)\n"
            "for ($i = 0; $i -lt $cases.Count; $i++) {\n"
            f"  $r = Invoke-DvGit -RepoRoot '{repo}' -GitArgs @('rev-parse', '--sq-quote', [string]$cases[$i])\n"
            "  Write-Output ('CASE ' + $i + ' ' + $r.exitCode + ' ' + [Convert]::ToBase64String($r.bytes))\n"
            "}\n")
        for index, case in enumerate(cases):
            match = re.search(rf"^CASE {index} (-?\d+) (\S*)$", out, re.MULTILINE)
            self.assertIsNotNone(match, out)
            self.assertEqual(match.group(1), "0", f"case {index} {case!r}: {out}")
            self.assertEqual(base64.b64decode(match.group(2)).decode("utf-8").strip(), "'" + case + "'", f"case {index} {case!r} did not reach git unchanged")

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

    @unittest.skipUnless(sys.platform == "win32", "job objects exist on Windows only")
    def test_the_job_handles_are_really_released_not_just_counted(self) -> None:
        # r2 (opus DV-GIT-JOB-CLOSE-REAL-HANDLE-PIN-1): the opened = closed counters above move in the module's own code, so a build that keeps the
        # counter and drops the CloseHandle call passes them. The process's real handle count does not lie: 30 calls that leak a job handle each
        # grow it by 30 or more; a clean module's growth is the few handles the runtime itself caches.
        repo = self.make_repo()
        out = self.in_module(
            "$self = [Diagnostics.Process]::GetCurrentProcess()\n"
            "$null = Invoke-DvGit -RepoRoot '" + str(repo) + "' -GitArgs @('rev-parse', '--is-inside-work-tree')\n"
            "[GC]::Collect(); [GC]::WaitForPendingFinalizers(); $self.Refresh(); $base = $self.HandleCount\n"
            "for ($i = 0; $i -lt 30; $i++) { $null = Invoke-DvGit -RepoRoot '" + str(repo) + "' -GitArgs @('rev-parse', '--is-inside-work-tree') }\n"
            "[GC]::Collect(); [GC]::WaitForPendingFinalizers(); $self.Refresh()\n"
            "Write-Output ('GROWTH ' + ($self.HandleCount - $base))\n")
        match = re.search(r"GROWTH (-?\d+)", out)
        self.assertIsNotNone(match, out)
        self.assertLess(int(match.group(1)), 15, f"30 git calls grew the process handle count by {match.group(1)}: job handles are leaking")

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

    def test_the_timeout_override_refuses_a_trailing_newline(self) -> None:
        # r2 (opus P-01): .NET's `$` also matches before a final newline, so '30<LF>' was read as 30 s. `\z` is exact: it is the 120 s default.
        out = self.in_module(
            "foreach ($v in @(\"30`n\", \"30`r`n\", '30')) {\n"
            "  $env:DV_GIT_TIMEOUT_SECONDS = $v\n"
            "  Write-Output ('MS ' + (Get-DvGitTimeoutMilliseconds))\n"
            "}\n")
        self.assertEqual([line for line in out.splitlines() if line.startswith("MS ")], ["MS 120000", "MS 120000", "MS 30000"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "the escape needs /proc/<pid>/environ tag discovery (Linux) and POSIX env/sleep")
    def test_a_straggler_that_scrubs_its_environment_is_refused_with_kill_not_confirmed(self) -> None:
        # CI-DUALVENUE-GIT-KILL-NOT-CONFIRMED-ROW-1 (CoS #472 N5): `env -i` drops the per-call tag, so Stop-DvGitCall cannot find the child by
        # /proc/<pid>/environ and its parent (sh) is gone, so Kill($true) misses it too: the pipes stay open and the kill is NOT confirmed. The
        # refusal is the same typed exit -1 with no bytes; only the wording differs. Then the test kills the survivor itself.
        token = _own_sleep_token("61")
        self.addCleanup(_kill_sleepers, token)
        done = self.invoke_dv_git(self.make_repo(), f"'-c', 'alias.dvscrub=!echo partial; env -i /bin/sleep {token} & exit 0', 'dvscrub'", {"DV_GIT_TIMEOUT_SECONDS": "5"}, timeout=60)
        self.assertIn("EXIT -1", done.stdout)
        self.assertIn("DV_GIT_PIPE_STRAGGLER", done.stdout)
        self.assertIn("(kill not confirmed)", done.stdout)
        self.assertNotIn("(killed)", done.stdout)
        self.assertNotIn("partial", done.stdout.split("ERR ", 1)[0], "no partial bytes may be returned")
        self.assertEqual(done.stdout.split("OUT ", 1)[1].split("\n", 1)[0].strip(), "", "no bytes at all")
        self.assertTrue(_sleepers(token), "the scrubbed child must have escaped the kill: that is what makes the kill 'not confirmed'")

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

    # DV-GIT-NATIVE-START-TYPED-FAILURE-1 (CoS Dot on #511, all nonblocking hardening): every native call of the suspended start is checked and a
    # failure after the child exists is a TYPED refusal (exit -1, no bytes), never a null that lets the caller start a second git beside the
    # first, and never a success. The module's seams: $script:DvNativeFault (a native call's name -> the result to read instead of the real
    # one; the real call still runs, so the child exists and the cleanup has something to prove) and $script:DvNewPipeStream (builds one
    # parent-side pipe stream; a test swaps in one that throws).
    def run_native_fault(self, setup: str, git_args: str = "'rev-parse', '--is-inside-work-tree'", env_extra: dict[str, str] | None = None,
                         trace: bool = False, after: str = "") -> tuple[str, list[str]]:
        repo = self.make_repo()
        env = dict(env_extra or {})
        trace_file = None
        if trace:
            trace_dir = Path(tempfile.mkdtemp(prefix="dve-trace-"))
            self.addCleanup(shutil.rmtree, trace_dir, True)
            trace_file = trace_dir / "git-trace.txt"
            env["GIT_TRACE"] = str(trace_file)
        out = self.in_module(
            f"{setup}\n"
            f"try {{ $r = Invoke-DvGit -RepoRoot '{repo}' -GitArgs @({git_args}); Write-Output ('EXIT ' + $r.exitCode); Write-Output ('BYTES ' + $r.bytes.Length); Write-Output ('ERR ' + $r.stderr) }}\n"
            "catch { Write-Output ('THREW ' + $_.Exception.Message) }\n"
            f"{after}\n"
            "Write-Output ('JOBS opened=' + $script:DvJobsOpened + ' closed=' + $script:DvJobsClosed)\n", env)
        spawns = []
        if trace_file is not None and trace_file.exists():
            spawns = [line for line in trace_file.read_text(encoding="utf-8", errors="replace").splitlines() if _is_git_spawn_trace(line)]
        return out, spawns

    def assert_typed_refusal(self, out: str, token: str) -> None:
        self.assertNotIn("THREW", out, "a failed native call must be a typed refusal, not an exception")
        self.assertIn("EXIT -1", out)
        self.assertIn("BYTES 0", out)
        self.assertIn(f"ERR {token}", out)
        self.assertNotIn("DV_GIT_TIMEOUT", out, "a start failure is its own status, never read as a later timeout")
        match = re.search(r"JOBS opened=(\d+) closed=(\d+)", out)
        self.assertIsNotNone(match, out)
        self.assertEqual(match.group(1), match.group(2), "the failure path must still close its job object")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_failed_job_assignment_is_a_typed_refusal_and_no_second_git_starts(self) -> None:
        # I1: AssignProcessToJobObject's return was dropped, so a child outside the job ran on and a timeout could not reach its children.
        out, spawns = self.run_native_fault("$script:DvNativeFault = @{ AssignProcessToJobObject = $false }", trace=True)
        self.assert_typed_refusal(out, "DV_GIT_JOB_ASSIGN_FAILED")
        self.assertEqual(spawns, [], "the suspended child must never run, and the Process.Start fallback must not start a second git")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_failed_resume_is_a_typed_refusal_and_no_second_git_starts(self) -> None:
        # I1: ResumeThread's return was dropped; 0xFFFFFFFF means the thread was never resumed and the call then waited out the whole bound.
        started = time.monotonic()
        out, spawns = self.run_native_fault("$script:DvNativeFault = @{ ResumeThread = [uint32]::MaxValue }", env_extra={"DV_GIT_TIMEOUT_SECONDS": "20"}, trace=True)
        self.assert_typed_refusal(out, "DV_GIT_RESUME_FAILED")
        self.assertLess(time.monotonic() - started, 15, "a failed resume is refused at once, not after the git bound")
        self.assertLessEqual(len(spawns), 1, "the Process.Start fallback must not start a second git beside the first")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_stream_construction_throw_after_resume_is_typed_and_cleans_up_the_started_child(self) -> None:
        # I2: a throw building the pipe streams AFTER the child was resumed returned null, and the caller then started another git beside the
        # running first one (leaked, unwatched, still in the job). Now the started child is killed with its children and the call is exit -1.
        for label, fail_on in (("first", 1), ("second", 2)):
            with self.subTest(stream=label):
                token = _own_sleep_token("63")
                self.addCleanup(_kill_sleepers, token)
                started = time.monotonic()
                out, spawns = self.run_native_fault(
                    "$script:StreamCalls = 0\n"
                    "$script:DvNewPipeStream = { param($h) $script:StreamCalls++\n"
                    f"  if ($script:StreamCalls -eq {fail_on}) {{ throw 'injected stream failure' }}\n"
                    "  [System.IO.Pipes.AnonymousPipeClientStream]::new([System.IO.Pipes.PipeDirection]::In, [Microsoft.Win32.SafeHandles.SafePipeHandle]::new($h, $true)) }",
                    f"'-c', 'alias.dvs=!sleep {token}', 'dvs'", {"DV_GIT_TIMEOUT_SECONDS": "3"}, trace=True)
                self.assert_typed_refusal(out, "DV_GIT_STREAM_FAILED")
                self.assertIn("injected stream failure", out)
                self.assertLess(time.monotonic() - started, 25, "the refusal must not wait out the git bound or the 63 s child")
                self.assertLessEqual(len(spawns), 1, "the Process.Start fallback must not start a second git beside the first")
                deadline = time.monotonic() + 10
                survivors = _sleepers(token)
                while survivors and time.monotonic() < deadline:
                    time.sleep(0.5)
                    survivors = _sleepers(token)
                self.assertEqual(survivors, set(), "the child that was resumed must be terminated with its children, not left running")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_failed_exit_code_read_is_a_typed_refusal_not_a_success(self) -> None:
        # I3: GetExitCodeProcess's return was dropped while the code started at 0, so a failed read looked like git exiting 0.
        out, _ = self.run_native_fault("$script:DvNativeFault = @{ GetExitCodeProcess = $false }")
        self.assert_typed_refusal(out, "DV_GIT_EXITCODE_FAILED")

    # DV-GIT-NATIVE-START-UNASSIGNED-CHILD-CLEANUP-1 (Dot R18 advisory on #607). The seams added for it, all empty in production:
    # $script:DvNativeSkipCall (a native call's name -> the real call is NOT made, only the DvNativeFault result is read; the child is then
    # genuinely outside the job), $script:DvNativeFaultHits (the names a fault was read for: proof the fault fired) and $script:DvCleanupGate
    # (called with the child's pid, process handle and the job as the failure cleanup starts: the child's state at that moment).
    CHILD_PROBE_PS = (
        "Add-Type -Namespace DvT -Name K -MemberDefinition '[System.Runtime.InteropServices.DllImport(\"kernel32.dll\")] "
        "public static extern bool IsProcessInJob(System.IntPtr p, System.IntPtr j, out bool r);'\n"
        "$script:Seen = @{}\n"
        "$script:DvCleanupGate = { param($ChildPid, $Process, $Job)\n"
        "  $r = $true; [void][DvT.K]::IsProcessInJob($Process, $Job, [ref]$r)\n"
        "  $script:Seen = @{ pid = $ChildPid; inJob = $r; alive = (-not [Diagnostics.Process]::GetProcessById($ChildPid).HasExited) } }\n")
    CHILD_REPORT_PS = (
        "Write-Output ('HITS ' + (@($script:DvNativeFaultHits) -join ','))\n"
        "Write-Output ('SEEN pid=' + $script:Seen.pid + ' injob=' + $script:Seen.inJob + ' alive=' + $script:Seen.alive)\n"
        "$gone = $false\n"
        "try { $gone = [Diagnostics.Process]::GetProcessById([int]$script:Seen.pid).HasExited } catch { $gone = $true }\n"
        "Write-Output ('GONE ' + $gone)\n")

    def assert_child_was_unassigned_alive_then_gone(self, out: str) -> None:
        seen = re.search(r"SEEN pid=(\d{2,}) injob=(\w+) alive=(\w+)", out)
        self.assertIsNotNone(seen, f"the cleanup gate never saw a created child: {out}")
        self.assertEqual(seen.group(2), "False", "the child must be OUTSIDE the job when the cleanup starts: that is the case under test")
        self.assertEqual(seen.group(3), "True", "the suspended child must be alive when the cleanup starts")
        self.assertIn("GONE True", out, "the unassigned child must be terminated by the failure cleanup (a job close does not reach it)")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_child_never_assigned_to_the_job_is_terminated_by_the_failure_cleanup(self) -> None:
        # U1: the AssignProcessToJobObject fault used to let the REAL call run first, so the child was in the job when the cleanup ran and a
        # job-based kill could have hidden a cleanup that does not reach it. Here the real call is skipped: the child is genuinely unassigned.
        out, spawns = self.run_native_fault(
            self.CHILD_PROBE_PS + "$script:DvNativeFault = @{ AssignProcessToJobObject = $false }\n$script:DvNativeSkipCall = @{ AssignProcessToJobObject = $true }\n",
            trace=True, after=self.CHILD_REPORT_PS)
        self.assert_typed_refusal(out, "DV_GIT_JOB_ASSIGN_FAILED")
        self.assertIn("HITS AssignProcessToJobObject", out)
        self.assert_child_was_unassigned_alive_then_gone(out)
        self.assertEqual(spawns, [], "the suspended child must never run, and the Process.Start fallback must not start a second git")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_throw_between_creation_and_assignment_terminates_the_unassigned_child(self) -> None:
        # U2: DvStartGate runs between CreateProcessW and the assignment. A throw there is the earliest failure with a child alive.
        out, spawns = self.run_native_fault(
            self.CHILD_PROBE_PS + "$script:DvStartGate = { throw 'injected gate failure' }\n", trace=True, after=self.CHILD_REPORT_PS)
        self.assert_typed_refusal(out, "DV_GIT_START_FAILED")
        self.assertIn("injected gate failure", out)
        self.assert_child_was_unassigned_alive_then_gone(out)
        self.assertEqual(spawns, [], "the suspended child must never run, and the Process.Start fallback must not start a second git")

    def handle_delta(self, setup: str, calls: int = 25) -> str:
        """Run Invoke-DvGit `calls` times after a warm-up and print how many handles this pwsh gained (a leak of even one handle per call shows)."""
        repo = self.make_repo()
        return self.in_module(
            f"{setup}\n"
            "function Get-DvTestHandles { [GC]::Collect(); [GC]::WaitForPendingFinalizers(); [GC]::Collect(); (Get-Process -Id $PID).HandleCount }\n"
            f"$call = {{ $script:Last = Invoke-DvGit -RepoRoot '{repo}' -GitArgs @('rev-parse', '--is-inside-work-tree') }}\n"
            "1..3 | ForEach-Object { & $call }\n"
            "$before = Get-DvTestHandles\n"
            f"1..{calls} | ForEach-Object {{ & $call }}\n"
            "$after = Get-DvTestHandles\n"
            "Write-Output ('DELTA ' + ($after - $before))\n"
            "Write-Output ('LAST ' + $script:Last.exitCode + ' ' + $script:Last.stderr)\n"
            "Write-Output ('HITS ' + (@($script:DvNativeFaultHits | Select-Object -Unique) -join ','))\n"
            "Write-Output ('JOBS opened=' + $script:DvJobsOpened + ' closed=' + $script:DvJobsClosed)\n")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_every_early_return_before_the_job_assignment_releases_its_handles(self) -> None:
        # U2: each step that can fail before the assignment (the second and third rows have created some pipes; the gate row has created the
        # suspended child) must leave no handle behind, call after call. The first three fall back to Process.Start (no child existed), which
        # is git's own result; the gate row is the typed refusal. A leak shows as at least one handle per call, so the bound is half that.
        calls = 25
        cases = (
            ("CreatePipe", "$script:DvNativeFault = @{ CreatePipe = $false }; $script:DvNativeSkipCall = @{ CreatePipe = $true }", "LAST 0 ", "CreatePipe"),
            ("SetHandleInformation", "$script:DvNativeFault = @{ SetHandleInformation = $false }", "LAST 0 ", "SetHandleInformation"),
            ("CreateProcessW", "$script:DvNativeFault = @{ CreateProcessW = $false }; $script:DvNativeSkipCall = @{ CreateProcessW = $true }", "LAST 0 ", "CreateProcessW"),
            ("gate", "$script:DvStartGate = { throw 'injected gate failure' }", "LAST -1 DV_GIT_START_FAILED", ""),
        )
        for label, setup, last, hit in cases:
            with self.subTest(step=label):
                out = self.handle_delta(setup, calls)
                self.assertIn(last, out, out)
                if hit:
                    self.assertIn(f"HITS {hit}", out, "the fault must have fired, or the case proves nothing")
                delta = re.search(r"DELTA (-?\d+)", out)
                self.assertIsNotNone(delta, out)
                self.assertLess(int(delta.group(1)), calls // 2, f"{label}: handles leaked across {calls} calls: {out}")
                jobs = re.search(r"JOBS opened=(\d+) closed=(\d+)", out)
                self.assertIsNotNone(jobs, out)
                self.assertEqual(jobs.group(1), jobs.group(2), "every job object opened must be closed")

    # U3: the alias tests above counted only `trace: built-in:` lines, which an alias never writes (see _is_git_spawn_trace), so their "at most
    # one git" bounds held for any number of gits. These two pin the count through a shell alias: exactly one run on the success path (the
    # control that the counter sees an alias at all) and none when the suspended child is refused before it can run.
    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_shell_alias_through_the_native_start_is_traced_as_exactly_one_git(self) -> None:
        out, spawns = self.run_native_fault("$script:DvNativeFault = @{}", "'-c', 'alias.dvhi=!echo hi', 'dvhi'", trace=True)
        self.assertIn("EXIT 0", out)
        self.assertEqual(len(spawns), 1, f"one alias call is one git run in the trace: {spawns}")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_a_refused_start_through_a_shell_alias_never_runs_the_alias(self) -> None:
        for label, setup in (("assign", "$script:DvNativeFault = @{ AssignProcessToJobObject = $false }"),
                             ("gate", "$script:DvStartGate = { throw 'injected gate failure' }")):
            with self.subTest(step=label):
                out, spawns = self.run_native_fault(setup, "'-c', 'alias.dvhi=!echo hi', 'dvhi'", trace=True)
                self.assertIn("EXIT -1", out)
                self.assertIn("BYTES 0", out)
                self.assertEqual(spawns, [], "the suspended child was refused before it could run, and no second git was started")

    @unittest.skipUnless(sys.platform == "win32", "the suspended start is Windows only")
    def test_the_native_fault_seam_is_inert_when_no_fault_is_injected(self) -> None:
        # The happy path is byte-for-byte what it was: no seam entry, the real results are read.
        out, _ = self.run_native_fault("$script:DvNativeFault = @{}")
        self.assertIn("EXIT 0", out)
        self.assertIn("BYTES 5", out)   # 'true' plus the newline
        self.assertNotIn("ERR DV_GIT", out)

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


def _own_sleep_token(seconds: str = "60") -> str:
    """A duration no other process on the host runs: ``<seconds>.<nine random digits>`` (``sleep 60.482913706``; GNU and BSD sleep take a
    fraction). r2 (sol on #504): a before/after diff of every ``sleep 60`` pid is not identity, so two tests (or another lane) overlapping on
    one host counted and killed each other's child. The token is the identity, carried in the child's argv (an ``env -i`` child keeps its argv;
    it does not keep an environment marker). Both sleeper tests discover AND clean up by it, and only by it."""
    return f"{seconds}.{uuid.uuid4().int % 10**9:09d}"


def _sleeper_pids_from_ps(ps_output: str, token: str = "60") -> set[int]:
    """Parse `ps -axo pid=,args=` (macOS has no /proc): the pids whose argv is exactly `sleep <token>`."""
    found: set[int] = set()
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and os.path.basename(parts[1]) in ("sleep", "sleep.exe") and parts[2] == token:
            found.add(int(parts[0]))
    return found


def _sleepers(token: str) -> set[int]:
    """Pids of the processes running exactly `sleep <token>` (the straggler of the aliases above; token from _own_sleep_token). There is no
    before/after set: the token is the identity. HARDENING-2 (4): Windows asks CIM, Linux reads /proc, anything else with no /proc (macOS)
    asks ps."""
    found: set[int] = set()
    if sys.platform == "win32":
        pattern = re.compile(r'sleep(\.exe)?"?\s+' + re.escape(token) + r'\s*$', re.IGNORECASE)   # a sleep started by path reads `sleep.EXE`
        query = "Get-CimInstance Win32_Process -Filter \"Name='sleep.exe'\" | ForEach-Object { '' + $_.ProcessId + [char]9 + $_.CommandLine }"
        out = subprocess.run([PWSH, *PWSH_ARGS, "-Command", query], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        for line in out.splitlines():
            pid, _, cmd = line.partition("\t")
            if pid.strip().isdigit() and pattern.search(cmd.strip()):
                found.add(int(pid))
        return found
    if not Path("/proc").is_dir():
        return _sleeper_pids_from_ps(subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout, token)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) >= 2 and os.path.basename(argv[0]) in (b"sleep", b"sleep.exe") and argv[1] == token.encode():
            found.add(int(entry.name))
    return found


def _kill_sleepers(token: str) -> None:
    """Cleanup only (the assertion above is the check): a RED run must not leave its sleeper behind. It terminates what _sleepers(token) names,
    that is this test's own child and nothing else."""
    for pid in _sleepers(token):
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

    @unittest.skipIf(shutil.which("sleep") is None, "needs a sleep binary on PATH")
    def test_two_overlapping_sleeper_tests_each_count_and_terminate_only_their_own_child(self) -> None:
        # r2 (sol BLOCKER on #504): one process, no second pytest. Three same-duration sleepers are alive at once: this test's own, a plain
        # `sleep 60` (another lane on the host) and another overlapping test's token sleeper. Discovery names only the own pid, cleanup ends
        # only the own pid, and the other two are untouched until the test ends them itself by their Popen handles.
        sleep = shutil.which("sleep")
        own_token, other_token = _own_sleep_token("60"), _own_sleep_token("60")
        self.assertNotEqual(own_token, other_token)
        quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        own, other_test, other_lane = (subprocess.Popen([sleep, arg], **quiet) for arg in (own_token, other_token, "60"))
        for proc in (own, other_test, other_lane):
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
        self.assertEqual(_sleepers(own_token), {own.pid}, "discovery must name this test's child only")
        self.assertEqual(_sleepers(other_token), {other_test.pid})
        _kill_sleepers(own_token)
        own.wait(timeout=15)
        self.assertIsNone(other_test.poll(), "the cleanup of one test killed another overlapping test's sleeper")
        self.assertIsNone(other_lane.poll(), "the cleanup killed a same-duration sleeper that no test of this file started")
        self.assertEqual(_sleepers(other_token), {other_test.pid})

    @unittest.skipIf(sys.platform == "win32" or Path("/proc").is_dir(), "the ps path is for platforms without /proc (macOS)")
    def test_discovery_runs_where_there_is_no_proc(self) -> None:
        self.assertIsInstance(_sleepers(_own_sleep_token()), set)   # on macOS this used to raise FileNotFoundError at Path('/proc').iterdir()


if __name__ == "__main__":
    unittest.main()
