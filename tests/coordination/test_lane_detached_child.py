"""LANE-DETACHED-CHILD-SURVIVES-LANE-EXIT-1: pin which child a lane may leave running after it ends.

Invoke-Lane.ps1 puts the Claude lane host in a kill-on-close Windows job object and closes the handle when the
lane ends, so every descendant that stayed in the job dies with the lane. A process started through
Win32_Process.Create is parented to WmiPrvSE, outside that job, and survives. Measured 2026-10-21 through a
real throwaway lane (the launcher's own job, 63 s after the lane process ended): the WMI child was alive and
the Start-Process child was dead (probe receipts in the PR body).

This test replays that lifecycle with the job class taken from Invoke-Lane.ps1 itself: assign an inert host
to the job, let it start one child each way, let the host exit, close the job handle (the lane's `finally`),
wait SURVIVAL_WAIT_SEC, then read both children. Start-HiddenDetached.ps1 (board tooling, untracked) is the
sanctioned wrapper around the same single Create call; this test makes the call directly so it needs nothing
outside the tracked tree.

LANE-DETACHED-CHILD-TEST-HARDENING-1: the WMI child is outside the job, so nothing but this module can reap it.
The host records each child's pid and creation time the moment the child exists, and every run goes through
run_control(), whose finally kills each recorded pid whose creation time still matches (a reused pid is left
alone). That holds on the three failure paths a bare control-script `finally` cannot cover: the host fails
after the WMI Create, the host never exits, and the subprocess timeout kills the control script itself.
The failure-path runs start alongside the 60 s survival wait, so they add little wall time.

LANE-DETACHED-CHILD-TEST-HARDENING-2 closes the rows #545's keys left open:
  1. A child is reported reaped only after it was killed through a handle that carries SYNCHRONIZE and a bounded
     WaitForSingleObject on that handle returned WAIT_OBJECT_0; anything else raises ReapFailed.
  2. Every sleeper's command line carries a per-test token. After the record-based reap, run_control() sweeps by that
     token plus a CreationDate lower bound (never by image name), so a sleeper whose record was never published, or
     one created after the records were written, cannot escape.
  3. The live job's LimitFlags is read back with QueryInformationJobObject(job, 9) and must equal 0x2000, so an
     indirect write of a breakaway flag goes red even though the static one-write guard sees nothing.
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INVOKE_LANE = ROOT / "tools" / "coordination" / "Invoke-Lane.ps1"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract")
PWSH = "pwsh.exe"

# The card names 60 s: long enough that a kill-on-close that fires late (after the handle is closed) is seen.
SURVIVAL_WAIT_SEC = 60
SLEEPER_SEC = 150  # outlives the wait plus a loaded host's start-up; run_control() kills it by pid in any case
HOST_WAIT_SEC = 90  # how long the control script waits for the inert host to exit

HOST_PS1 = r"""
param([Parameter(Mandatory = $true)][string]$Dir)
$ErrorActionPreference = 'Stop'
$fault = $env:MLV_TEST_HOST_FAULT   # test-only: 'start-process-throws' | 'host-hangs' | 'late-wmi-child'
$pidDir = Join-Path $Dir 'pids'
# The per-test token rides on every sleeper's command line (never on the host's), so the Python finalizer can find a
# sleeper whose record was never published.
$token = ([IO.File]::ReadAllText((Join-Path $Dir 'token.txt'))).Trim()

# Written the moment a child exists, before anything else can fail: the Python finalizer reads these.
function Save-ChildRecord([string]$Name, [int]$ChildPid, [int64]$Created) {
    $tmp = Join-Path $pidDir ($Name + '.json.tmp')
    $doc = [ordered]@{ pid = $ChildPid; created = $Created }
    [IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination (Join-Path $pidDir ($Name + '.json')) -Force
}

$go = Join-Path $Dir 'go.flag'
while (-not (Test-Path -LiteralPath $go)) { Start-Sleep -Milliseconds 25 }
$sleep = 'Start-Sleep -Seconds __SLEEPER_SEC__ # ' + $token
$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
function New-WmiSleeper {
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = ('pwsh.exe -NoProfile -NonInteractive -Command "' + $sleep + '"')
        CurrentDirectory = $Dir; ProcessStartupInformation = $si }
    if ([int]$r.ReturnValue -ne 0) { throw ('wmi-create-rc-' + $r.ReturnValue) }
    return [int]$r.ProcessId
}
$wmiProc = Get-Process -Id (New-WmiSleeper)
Save-ChildRecord 'wmi' $wmiProc.Id $wmiProc.StartTime.ToFileTimeUtc()
if ($fault -eq 'start-process-throws') { throw 'injected-start-process-failure' }
$sp = Start-Process -FilePath 'pwsh.exe' -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', $sleep) -WindowStyle Hidden -PassThru
Save-ChildRecord 'startProcess' $sp.Id $sp.StartTime.ToFileTimeUtc()
$doc = [ordered]@{
    wmi = [ordered]@{ pid = $wmiProc.Id; created = $wmiProc.StartTime.ToFileTimeUtc() }
    startProcess = [ordered]@{ pid = $sp.Id; created = $sp.StartTime.ToFileTimeUtc() }
}
$tmp = Join-Path $Dir 'children.json.tmp'
[IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $tmp -Destination (Join-Path $Dir 'children.json')
if ($fault -eq 'late-wmi-child') {
    # a third sleeper that exists after the records were written and is never recorded: only the tag finds it
    [void](New-WmiSleeper)
    throw 'injected-unrecorded-late-child'
}
if ($fault -eq 'host-hangs') { Start-Sleep -Seconds 600 }
""".replace("__SLEEPER_SEC__", str(SLEEPER_SEC))

CONTROL_PS1 = r"""
param(
    [Parameter(Mandatory = $true)][string]$LaneScript,
    [Parameter(Mandatory = $true)][string]$Dir,
    [Parameter(Mandatory = $true)][int]$SurvivalSec,
    [Parameter(Mandatory = $true)][int]$HostWaitSec,
    [switch]$ProbeOnly   # read the live job's limit flags back and stop: no host, no children
)
$ErrorActionPreference = 'Stop'
$src = [IO.File]::ReadAllText($LaneScript)
$m = [regex]::Match($src, "(?s)if \(-not \('MlvLaneJob' -as \[type\]\)\) \{\s*Add-Type -TypeDefinition @'\r?\n(.*?)\r?\n'@")
if (-not $m.Success) { throw 'job-class-not-found-in-Invoke-Lane' }
Add-Type -TypeDefinition $m.Groups[1].Value
if (-not ('MlvTestJobProbe' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class MlvTestJobProbe {
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool QueryInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length, out uint returned);
  // JobObjectExtendedLimitInformation (9): BasicLimitInformation.LimitFlags is the UInt32 at offset 16.
  public static uint LimitFlags(IntPtr job) {
    int size = IntPtr.Size == 8 ? 144 : 112;   // sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION); the call fails on any other length
    IntPtr mem = Marshal.AllocHGlobal(size);
    try {
      for (int i = 0; i < size; i += 8) Marshal.WriteInt64(mem, i, 0);
      uint returned;
      if (!QueryInformationJobObject(job, 9, mem, (uint)size, out returned))
        throw new Win32Exception(Marshal.GetLastWin32Error());
      if (returned < 20) throw new InvalidOperationException("short JobObjectExtendedLimitInformation: " + returned);
      return (uint)Marshal.ReadInt32(mem, 16);
    } finally { Marshal.FreeHGlobal(mem); }
  }
}
'@
}

function Test-Alive($Rec) {
    $p = Get-Process -Id ([int]$Rec.pid) -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $false }
    # same pid AND same creation time (a file-time integer; ConvertFrom-Json would reshape an ISO string),
    # so a reused pid is not mistaken for the child
    return ($p.StartTime.ToFileTimeUtc() -eq [int64]$Rec.created)
}

$job = [MlvLaneJob]::CreateKillOnClose()
if ($ProbeOnly) {
    try { '{"limitFlags":' + [MlvTestJobProbe]::LimitFlags($job) + '}' } finally { [void][MlvLaneJob]::CloseHandle($job) }
    return
}
$psi = [Diagnostics.ProcessStartInfo]::new()
$psi.FileName = (Get-Command pwsh.exe -ErrorAction Stop).Source
foreach ($a in @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $Dir 'host.ps1'), '-Dir', $Dir)) { [void]$psi.ArgumentList.Add($a) }
$psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
$host_ = [Diagnostics.Process]::Start($psi)
try {
    [MlvLaneJob]::AssignOrThrow($job, $host_.Handle)            # the inert host is in the job before it does anything
    [IO.File]::WriteAllText((Join-Path $Dir 'go.flag'), 'go')
    if (-not $host_.WaitForExit($HostWaitSec * 1000)) { throw 'host-did-not-exit' }
    if ($host_.ExitCode -ne 0) { throw ('host-exit-' + $host_.ExitCode) }
    $recs = Get-Content -LiteralPath (Join-Path $Dir 'children.json') -Raw | ConvertFrom-Json
    $aliveBeforeClose = [ordered]@{ wmi = (Test-Alive $recs.wmi); startProcess = (Test-Alive $recs.startProcess) }
    [void][MlvLaneJob]::CloseHandle($job)                        # the lane's lane-end cleanup
    $job = [IntPtr]::Zero
    $endUtc = [DateTime]::UtcNow
    Start-Sleep -Seconds $SurvivalSec
    $result = [ordered]@{
        aliveBeforeClose = $aliveBeforeClose
        waitedSec = [math]::Round(([DateTime]::UtcNow - $endUtc).TotalSeconds, 1)
        wmiAlive = (Test-Alive $recs.wmi); startProcessAlive = (Test-Alive $recs.startProcess)
    }
    $result | ConvertTo-Json -Depth 4 -Compress
} finally {
    if ($job -ne [IntPtr]::Zero) { [void][MlvLaneJob]::CloseHandle($job) }
}
"""

_TERMINATE, _QUERY_LIMITED, _SYNCHRONIZE, _STILL_ACTIVE = 0x0001, 0x1000, 0x00100000, 259
_WAIT_OBJECT_0, _WAIT_TIMEOUT, _WAIT_FAILED = 0, 0x102, 0xFFFFFFFF
# SYNCHRONIZE is what WaitForSingleObject needs: on a handle without it the wait fails at once (error 5), so a kill
# that was never confirmed would read as a reap.
_REAP_ACCESS = _TERMINATE | _QUERY_LIMITED | _SYNCHRONIZE
REAP_WAIT_MS = 5000
SWEEP_QUERY_SEC = 90  # bound on the one CIM query a sweep makes
_FILETIME_UNIX_EPOCH = 116444736000000000
_TAG_RE = re.compile(r"MLVTAG[0-9a-f]{32}")  # also the only shape the LIKE filter below will ever see


class ReapFailed(RuntimeError):
    """A child that should be dead was not confirmed dead; never reported as reaped."""


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.WaitForSingleObject.restype = ctypes.c_uint32
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def reap_child(pid, created, *, kill=True, tolerance=0, not_before=0, access=None):
    """True when pid is a live process whose creation FILETIME matches `created` (killed first when `kill`).

    A kill is only reported once the process is confirmed terminated: the handle carries SYNCHRONIZE and the bounded
    wait on it must return WAIT_OBJECT_0, otherwise ReapFailed is raised. The kill goes through the handle the creation
    time was read from, so a pid recycled after this check cannot be hit; a pid that was already recycled has a
    different creation time and is never touched. `tolerance` (100 ns units) lets a creation time that came from a
    WMI CreationDate (microsecond resolution) match; `not_before` is a FILETIME lower bound on the creation time."""
    k = _kernel32()
    h = k.OpenProcess(_REAP_ACCESS if access is None else access, False, pid)
    if not h:
        return False
    try:
        times = [ctypes.c_uint64() for _ in range(4)]
        if not k.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
            return False
        if abs(times[0].value - created) > tolerance or times[0].value < not_before:
            return False
        code = ctypes.c_uint32()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return False  # exited; a leftover handle elsewhere keeps the pid openable
        if kill:
            if not k.TerminateProcess(h, 1):
                raise ReapFailed(f"pid {pid}: TerminateProcess failed (error {ctypes.get_last_error()})")
            waited = k.WaitForSingleObject(h, REAP_WAIT_MS)
            if waited != _WAIT_OBJECT_0:
                why = {_WAIT_TIMEOUT: "still running after the bounded wait", _WAIT_FAILED: "the wait failed"}
                raise ReapFailed(f"pid {pid}: not confirmed terminated ({why.get(waited, waited)}; "
                                 f"last error {ctypes.get_last_error()})")
        return True
    finally:
        k.CloseHandle(h)


def filetime_now():
    return int(time.time() * 10_000_000) + _FILETIME_UNIX_EPOCH


def creation_floor():
    """Lower bound for a sweep: now, less 2 s because the kernel stamps a process's creation time from a coarse tick
    clock that can read a few milliseconds behind time.time()."""
    return filetime_now() - 2 * 10_000_000


def find_tagged(token):
    """[(pid, creation FILETIME)] of every live process whose command line carries `token` (the CIM query's own
    process excluded). The token travels in the environment, so the query never matches itself by name or text."""
    assert _TAG_RE.fullmatch(token), f"not a sweep token: {token!r}"
    script = ("$t = $env:MLV_SWEEP_TAG; "
              "Get-CimInstance -ClassName Win32_Process -Filter (\"CommandLine LIKE '%\" + $t + \"%'\") | "
              "Where-Object { $_.ProcessId -ne $PID } | "
              "ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.CreationDate.ToFileTimeUtc() }")
    env = dict(os.environ, MLV_SWEEP_TAG=token)
    cp = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                        capture_output=True, text=True, timeout=SWEEP_QUERY_SEC, env=env)
    if cp.returncode != 0:
        raise ReapFailed(f"sweep query failed rc={cp.returncode}: {cp.stderr.strip()}")
    return [tuple(int(x) for x in line.split()) for line in cp.stdout.splitlines() if line.strip()]


def sweep_tagged(token, not_before, *, kill=True):
    """Kill (and confirm dead) every live process tagged `token` and created at or after `not_before`; return the pids.

    Matching is by the per-test command-line tag plus a CreationDate lower bound, never by image name, and each kill
    still goes through reap_child's creation-time check (WMI's microsecond CreationDate against the handle's)."""
    return [pid for pid, created in find_tagged(token)
            if reap_child(pid, created, kill=kill, tolerance=10_000, not_before=not_before)]


def probe_job_flags(d, lane_script=INVOKE_LANE):
    """LimitFlags of the job object the lane script's own MlvLaneJob class creates, read back with
    QueryInformationJobObject(job, JobObjectExtendedLimitInformation = 9)."""
    cp = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(d / "control.ps1"),
         "-LaneScript", str(lane_script), "-Dir", str(d), "-SurvivalSec", "0", "-HostWaitSec", "0", "-ProbeOnly"],
        capture_output=True, text=True, timeout=90)
    assert cp.returncode == 0, f"job probe failed rc={cp.returncode}\nstdout={cp.stdout}\nstderr={cp.stderr}"
    return json.loads(cp.stdout.strip().splitlines()[-1])["limitFlags"]


def read_child_records(pid_dir):
    recs = {}
    for f in sorted(pid_dir.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            recs[f.stem] = (int(doc["pid"]), int(doc["created"]))
        except (OSError, ValueError, KeyError):
            continue  # a half-written record is only ever the .tmp name; anything else unreadable is skipped
    return recs


def stage(d):
    (d / "pids").mkdir()
    (d / "host.ps1").write_text(HOST_PS1, encoding="ascii")
    (d / "control.ps1").write_text(CONTROL_PS1, encoding="ascii")
    (d / "token.txt").write_text("MLVTAG" + uuid.uuid4().hex, encoding="ascii")
    return d


def stage_token(d):
    return (d / "token.txt").read_text(encoding="ascii").strip()


class ControlRun(NamedTuple):
    cp: object      # CompletedProcess, or None when the subprocess timed out
    timed_out: bool
    records: dict   # name -> (pid, creation FILETIME), as the host published them
    reaped: list    # recorded children still alive when the finalizer ran
    swept: list     # pids found by the command-line tag alone (no record) and still alive when the sweep ran
    token: str


def run_control(d, *, survival_sec, host_wait_sec=HOST_WAIT_SEC, timeout, fault=None):
    """Run the control script; whatever happens, kill every recorded child that is still the same process, then
    sweep any leftover sleeper by the test's command-line tag (a child whose record was never published).

    `reaped` names the recorded children that were still alive when the finalizer ran (the orphans this module
    would otherwise leave behind); `swept` lists the unrecorded ones the tag found. A kill that cannot be confirmed
    raises ReapFailed once everything else has been tried."""
    env = dict(os.environ)
    env.pop("MLV_TEST_HOST_FAULT", None)
    if fault:
        env["MLV_TEST_HOST_FAULT"] = fault
    token = stage_token(d)
    floor = creation_floor()
    cp, timed_out, errors, reaped, swept = None, False, [], [], []
    try:
        cp = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(d / "control.ps1"),
             "-LaneScript", str(INVOKE_LANE), "-Dir", str(d), "-SurvivalSec", str(survival_sec),
             "-HostWaitSec", str(host_wait_sec)],
            capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        records = read_child_records(d / "pids")
        for name, (pid, created) in records.items():
            try:
                if reap_child(pid, created):
                    reaped.append(name)
            except ReapFailed as e:
                errors.append(f"{name}: {e}")
        try:
            swept = sweep_tagged(token, floor)
        except (ReapFailed, subprocess.SubprocessError) as e:
            errors.append(f"tag sweep: {e}")
    if errors:
        raise ReapFailed("; ".join(errors))
    return ControlRun(cp, timed_out, records, reaped, swept, token)


def children_alive(records):
    return {name: reap_child(pid, created, kill=False) for name, (pid, created) in records.items()}


def tagged_alive(token):
    """Pids still running with `token` on their command line (a zombie the CIM table still lists is not alive)."""
    return [pid for pid, created in find_tagged(token) if reap_child(pid, created, kill=False, tolerance=10_000)]


# name -> run_control arguments. Each leaves the WMI sleeper (outside the job) running unless the finalizer kills it.
FAILURE_PATHS = {
    "start-process-throws": dict(fault="start-process-throws", survival_sec=5, timeout=90),
    "host-does-not-exit": dict(fault="host-hangs", survival_sec=5, host_wait_sec=30, timeout=90),
    "subprocess-timeout": dict(survival_sec=SURVIVAL_WAIT_SEC, timeout=40),
    # a WMI sleeper created after the records were written and never recorded: only its command-line tag finds it
    "unrecorded-late-child": dict(fault="late-wmi-child", survival_sec=5, timeout=90),
}
# The rows above are sized for a loaded runner (start-up of pwsh plus the WMI Create can take many seconds) yet
# stay inside the 60 s survival wait they overlap, so they add no wall time: the host waits 30 s, the timeout is 40 s.


@pytest.fixture(scope="module")
def failure_paths_started(tmp_path_factory):
    pool = ThreadPoolExecutor(max_workers=len(FAILURE_PATHS))
    futures = {name: pool.submit(run_control, stage(tmp_path_factory.mktemp("failure-" + name)), **kw)
               for name, kw in FAILURE_PATHS.items()}
    yield futures
    pool.shutdown(wait=True)


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory, failure_paths_started):
    d = stage(tmp_path_factory.mktemp("detached-child"))
    cp, timed_out, *_rest = run_control(
        d, survival_sec=SURVIVAL_WAIT_SEC, host_wait_sec=HOST_WAIT_SEC, timeout=SURVIVAL_WAIT_SEC + 150)
    assert not timed_out, "control script timed out"
    assert cp.returncode == 0, f"control script failed rc={cp.returncode}\nstdout={cp.stdout}\nstderr={cp.stderr}"
    return json.loads(cp.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module", params=sorted(FAILURE_PATHS))
def failure_path(request, failure_paths_started):
    return request.param, failure_paths_started[request.param].result()


# ---- the job object itself --------------------------------------------------------------------------------

JOB_CLASS_RE = re.compile(r"(?s)if \(-not \('MlvLaneJob' -as \[type\]\)\) \{\s*Add-Type -TypeDefinition @'\r?\n(.*?)\r?\n'@")
# any assignment operator (=, |=, &=, ^=, +=, -=, <<=, ...) whose target is a LimitFlags member; == is a comparison.
# Whitespace and closing parens between the member and the operator are allowed, so `(x.LimitFlags) |= 1` is a write
# too (comments are blanked to whitespace first; an opening paren before the target needs no handling).
LIMIT_FLAGS_WRITE_RE = re.compile(r"\bLimitFlags[\s)]*(?:>>>|<<|>>|[-+*/%&|^])?=(?!=)([^;]*)")
# Second, independent count that does not parse anything: over the RAW MlvLaneJob block (nothing dropped, nothing
# blanked) the `LimitFlags` identifier occurs exactly as often as in the known-good block at fork/master, and so do the
# `UInt32 LimitFlags;` declarations. Both counts read the same text, so no comment, literal or interpolation hole can
# make one of them see less than the other. A comment that mentions LimitFlags in the block therefore fails closed.
LIMIT_FLAGS_TOKEN_RE = re.compile(r"\bLimitFlags\b")
LIMIT_FLAGS_DECL_RE = re.compile(r"\bUInt32\s+LimitFlags\s*;")
# Invoke-Lane.ps1 at fork/master: the field declaration (`public UInt32 LimitFlags;`) and the single `LimitFlags=0x2000;`.
KNOWN_GOOD_LIMIT_FLAGS_DECLS = 1
KNOWN_GOOD_LIMIT_FLAGS_TOKENS = 2
# C# tokens that can hide a write or fake one: string and char literals (kept, so a "//" inside one opens nothing)
# and comments (blanked to one space, which is also what separates tokens in C#).
CS_TOKEN_RE = re.compile(r"""@"(?:[^"]|"")*"|"(?:[^"\\\r\n]|\\.)*"|'(?:[^'\\\r\n]|\\.)*'|//[^\r\n]*|/\*.*?\*/""", re.S)


def blank_cs_comments(code):
    return CS_TOKEN_RE.sub(lambda m: m.group(0) if m.group(0)[0] in "@\"'" else " ", code)


def job_block(src):
    m = JOB_CLASS_RE.search(src)
    assert m, "MlvLaneJob Add-Type block not found in Invoke-Lane.ps1"
    return m.group(1)


def limit_flag_writes(src):
    """Right-hand sides of every LimitFlags write inside the MlvLaneJob Add-Type block, comments ignored."""
    return [rhs.strip() for rhs in LIMIT_FLAGS_WRITE_RE.findall(blank_cs_comments(job_block(src)))]


def limit_flag_tokens(src):
    """Raw LimitFlags token count in the MlvLaneJob block; nothing is dropped and nothing is parsed."""
    return len(LIMIT_FLAGS_TOKEN_RE.findall(job_block(src)))


def limit_flag_decls(src):
    """Raw `UInt32 LimitFlags;` declaration count in the same block text the token count reads."""
    return len(LIMIT_FLAGS_DECL_RE.findall(job_block(src)))


def assert_kill_on_close_only(src):
    writes = limit_flag_writes(src)
    assert len(writes) == 1, f"MlvLaneJob must write LimitFlags exactly once, found {len(writes)}: {writes}"
    assert re.fullmatch(r"0[xX][0-9A-Fa-f]+", writes[0]), f"LimitFlags write is not a plain hex literal: {writes[0]}"
    assert int(writes[0], 16) == 0x2000, "lane job must be KILL_ON_JOB_CLOSE only (0x2000)"
    # Fail closed on whatever the blanker misreads: both raw counts must equal the known-good block's.
    decls = limit_flag_decls(src)
    assert decls == KNOWN_GOOD_LIMIT_FLAGS_DECLS, (
        f"MlvLaneJob has {decls} LimitFlags declaration(s) in the raw block; expected {KNOWN_GOOD_LIMIT_FLAGS_DECLS}")
    tokens = limit_flag_tokens(src)
    assert tokens == KNOWN_GOOD_LIMIT_FLAGS_TOKENS, (
        f"MlvLaneJob has {tokens} LimitFlags token(s) in the raw block; expected {KNOWN_GOOD_LIMIT_FLAGS_TOKENS} "
        f"(the declaration and the single write)")


def test_job_object_is_kill_on_close_and_not_breakaway():
    # The route below only works because the job has no way for a member to leave it except a parent outside it.
    assert_kill_on_close_only(INVOKE_LANE.read_text(encoding="utf-8"))


WRITE_0x2000 = "value.BasicLimitInformation.LimitFlags=0x2000;"


@pytest.mark.parametrize("extra", [
    "value.BasicLimitInformation.LimitFlags|=0x800;",                    # BREAKAWAY_OK or'ed in after the assignment
    "value.BasicLimitInformation.LimitFlags = 0x2000 | 0x1000;",         # a second plain assignment
    "value.BasicLimitInformation.LimitFlags<<=1;",                       # shifted away from 0x2000
])
def test_a_second_limitflags_write_is_caught(extra):
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(WRITE_0x2000) == 1, "Invoke-Lane.ps1 no longer has the expected single LimitFlags write"
    with pytest.raises(AssertionError, match="exactly once"):
        assert_kill_on_close_only(src.replace(WRITE_0x2000, WRITE_0x2000 + "\n" + extra))


@pytest.mark.parametrize("extra", [
    "value.BasicLimitInformation.LimitFlags /* breakaway */ |= 0x800;",   # comment between the member and the operator
    "value.BasicLimitInformation.LimitFlags /* x */ |= 0x1000;",          # same, for 0x1000 (SILENT_BREAKAWAY_OK)
    "value.BasicLimitInformation.LimitFlags |= /* x */ 0x1000;",          # comment after the operator
    "value.BasicLimitInformation.LimitFlags/**/|=0x1000;",                # comment as the only separator
    'string s = "//"; value.BasicLimitInformation.LimitFlags |= 0x800;',  # "//" in a string does not open a comment
])
def test_a_limitflags_write_split_by_a_comment_is_caught(extra):
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(WRITE_0x2000) == 1, "Invoke-Lane.ps1 no longer has the expected single LimitFlags write"
    with pytest.raises(AssertionError, match="exactly once"):
        assert_kill_on_close_only(src.replace(WRITE_0x2000, WRITE_0x2000 + "\n" + extra))


def test_a_whole_line_commented_out_limitflags_write_is_rejected_fail_closed():
    # r4: flipped from accepted. The raw token count has no comment exemption, so even a whole-line // mention fails.
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(WRITE_0x2000) == 1, "Invoke-Lane.ps1 no longer has the expected single LimitFlags write"
    with pytest.raises(AssertionError, match="LimitFlags token"):
        assert_kill_on_close_only(src.replace(WRITE_0x2000, WRITE_0x2000 + "\n// LimitFlags = 0x800 removed"))


@pytest.mark.parametrize("comment", [
    "/* LimitFlags |= 0x800 */",
    "/* multi-line\n   LimitFlags = 0x1000; */",
])
def test_a_block_commented_limitflags_mention_is_rejected_fail_closed(comment):
    # a /* */ mention is rejected rather than parsed (r4: so is a whole-line // mention, see below).
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(WRITE_0x2000) == 1, "Invoke-Lane.ps1 no longer has the expected single LimitFlags write"
    with pytest.raises(AssertionError, match="LimitFlags token"):
        assert_kill_on_close_only(src.replace(WRITE_0x2000, WRITE_0x2000 + "\n" + comment))


@pytest.mark.parametrize("extra", [
    "(value.BasicLimitInformation.LimitFlags) |= 0x800;",                 # parenthesized lvalue
    "( value.BasicLimitInformation.LimitFlags ) |= 0x800;",               # whitespace inside the parens
    "(value.BasicLimitInformation.LimitFlags)|=0x1000;",                  # tight, for 0x1000
    "((value.BasicLimitInformation.LimitFlags)) |= 0x800;",               # doubly parenthesized
])
def test_a_parenthesized_limitflags_write_is_caught(extra):
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(WRITE_0x2000) == 1, "Invoke-Lane.ps1 no longer has the expected single LimitFlags write"
    with pytest.raises(AssertionError, match="exactly once"):
        assert_kill_on_close_only(src.replace(WRITE_0x2000, WRITE_0x2000 + "\n" + extra))


@pytest.mark.parametrize("extra", [
    # a nested literal inside an interpolation hole makes the blanker open a comment that is not one and swallow the write
    'string s = $"{ "/*" }"; value.BasicLimitInformation.LimitFlags |=0x800; // */',
    'string s = $"{ "//" }"; value.BasicLimitInformation.LimitFlags |=0x1000;',
])
def test_a_limitflags_write_hidden_by_a_nested_literal_is_caught(extra):
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(WRITE_0x2000) == 1, "Invoke-Lane.ps1 no longer has the expected single LimitFlags write"
    with pytest.raises(AssertionError, match="LimitFlags token"):
        assert_kill_on_close_only(src.replace(WRITE_0x2000, WRITE_0x2000 + "\n" + extra))


def _mutated(replacement):
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(WRITE_0x2000) == 1, "Invoke-Lane.ps1 no longer has the expected single LimitFlags write"
    return src.replace(WRITE_0x2000, replacement)


# r4: both raw counts must match the known-good block; a commented-out declaration can no longer balance a hidden write.
@pytest.mark.parametrize("extra", [
    # J-05 (sol r3): a commented declaration offsets a second write that a nested literal hides from the blanker
    '// UInt32 LimitFlags;\nstring s = $"{ "/*" }"; value.BasicLimitInformation.LimitFlags |=0x800; // */',
    '// UInt32 LimitFlags;\nstring s = $"{ "//" }"; value.BasicLimitInformation.LimitFlags |=0x1000;',
    # J-06 (opus r3): a write on a line that starts with // inside a verbatim interpolated string the blanker mis-ends
    'GC.KeepAlive($@"{ "x" }\n// "); value.BasicLimitInformation.LimitFlags |= 0x800;',
])
def test_a_commented_declaration_cannot_offset_a_hidden_limitflags_write(extra):
    with pytest.raises(AssertionError, match="LimitFlags (token|declaration)"):
        assert_kill_on_close_only(_mutated(WRITE_0x2000 + "\n" + extra))


def test_an_unsigned_right_shift_assignment_counts_as_a_limitflags_write():
    # C# 11 `>>>=` turns 0x2000 into 0x800
    assert len(limit_flag_writes(_mutated(WRITE_0x2000 + "\nvalue.BasicLimitInformation.LimitFlags >>>= 2;"))) == 2


def test_the_only_limitflags_write_must_be_0x2000():
    src = INVOKE_LANE.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="0x2000"):
        assert_kill_on_close_only(src.replace(WRITE_0x2000, "value.BasicLimitInformation.LimitFlags=0x2800;"))


# ---- the lifecycle ----------------------------------------------------------------------------------------

def test_both_children_are_running_before_the_lane_job_closes(lifecycle):
    assert lifecycle["aliveBeforeClose"] == {"wmi": True, "startProcess": True}


def test_wmi_created_child_is_alive_60s_after_the_lane_job_closes(lifecycle):
    assert lifecycle["waitedSec"] >= SURVIVAL_WAIT_SEC
    assert lifecycle["wmiAlive"] is True, (
        "a child started through Win32_Process.Create must outlive the lane (the sanctioned detached route)")


def test_start_process_child_dies_with_the_lane_job(lifecycle):
    assert lifecycle["startProcessAlive"] is False, (
        "a child started with Start-Process stays in the kill-on-close job and must die with the lane")


# ---- no orphaned sleeper, whatever fails ------------------------------------------------------------------

def test_no_sleeper_is_left_behind_on_a_failure_path(failure_path):
    name, run = failure_path
    cp, timed_out, records, reaped = run.cp, run.timed_out, run.records, run.reaped
    # the path really happened, not a start-up error that never reached the WMI Create
    assert "wmi" in records, f"{name}: the host never recorded its WMI child; stdout={getattr(cp, 'stdout', None)}"
    if name == "subprocess-timeout":
        assert timed_out, "the control script finished inside the timeout; the timeout path was not exercised"
    else:
        assert not timed_out and cp.returncode != 0, f"{name}: expected the control script to fail, rc={cp and cp.returncode}"
    # the WMI child is outside the job, so it was still running when the finalizer ran: that is the orphan it killed
    assert "wmi" in reaped, f"{name}: the WMI sleeper was not alive for the finalizer to kill (reaped={reaped})"
    assert not any(children_alive(records).values()), f"{name}: a recorded sleeper is still alive: {children_alive(records)}"
    # the record is not the only way to find a sleeper: nothing carrying this test's tag may be running either
    assert tagged_alive(run.token) == [], f"{name}: a sleeper carrying the test tag is still alive"


def test_a_sleeper_with_no_published_record_is_found_by_its_tag(failure_paths_started):
    run = failure_paths_started["unrecorded-late-child"].result()
    assert set(run.records) == {"wmi", "startProcess"}, f"the late sleeper must stay unrecorded: {run.records}"
    # the recorded children are accounted for by their records; the third sleeper only the tag could reach
    assert len(run.swept) == 1, f"expected exactly the one unrecorded sleeper from the sweep, got {run.swept}"
    assert run.swept[0] not in {pid for pid, _created in run.records.values()}


def _spawn_sleeper(tag=""):
    """A pwsh sleeper; `tag` rides on its command line the way the host's sleepers carry the test token."""
    return subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                             "Start-Sleep -Seconds 60" + (" # " + tag if tag else "")])


def _wait_registered(proc, token):
    """Poll (bounded) until the CIM table lists `proc` under `token`."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.pid in [pid for pid, _c in find_tagged(token)]:
            return
        time.sleep(0.5)
    raise AssertionError("the tagged sleeper never appeared in the CIM process table")


def test_the_sweep_kills_a_tagged_sleeper_and_confirms_it_dead():
    token = "MLVTAG" + uuid.uuid4().hex
    floor = creation_floor()
    p = _spawn_sleeper(token)
    try:
        _wait_registered(p, token)
        assert p.pid in sweep_tagged(token, floor)
        assert p.poll() is not None, "sweep_tagged returned before the sleeper was confirmed terminated"
    finally:
        p.kill()
        p.wait()


def test_the_sweep_never_matches_by_image_name_alone():
    token = "MLVTAG" + uuid.uuid4().hex
    floor = creation_floor()
    bystander = _spawn_sleeper()  # a pwsh.exe like the sleepers, but without the tag
    tagged = _spawn_sleeper(token)
    try:
        _wait_registered(tagged, token)
        assert tagged.pid in sweep_tagged(token, floor)
        assert bystander.poll() is None, "an untagged pwsh.exe was killed: the sweep matched by image name"
    finally:
        for p in (bystander, tagged):
            p.kill()
            p.wait()


def test_the_sweep_honours_the_creation_date_lower_bound():
    token = "MLVTAG" + uuid.uuid4().hex
    p = _spawn_sleeper(token)
    try:
        _wait_registered(p, token)
        assert sweep_tagged(token, filetime_now() + 600 * 10_000_000) == [], "a sleeper older than the bound was swept"
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()


# ---- a reap is only reported once the child is confirmed terminated ----------------------------------------------

def _creation_filetime(pid):
    k = _kernel32()
    h = k.OpenProcess(_QUERY_LIMITED, False, pid)
    assert h, f"cannot open pid {pid}"
    try:
        times = [ctypes.c_uint64() for _ in range(4)]
        assert k.GetProcessTimes(h, *[ctypes.byref(t) for t in times])
        return times[0].value
    finally:
        k.CloseHandle(h)


def test_reap_child_opens_with_synchronize_and_returns_only_after_the_child_is_dead():
    assert _REAP_ACCESS & _SYNCHRONIZE, "the reap handle must carry SYNCHRONIZE or its wait cannot succeed"
    p = _spawn_sleeper()
    try:
        created = _creation_filetime(p.pid)
        assert reap_child(p.pid, created) is True
        assert p.poll() is not None, "reap_child reported the child reaped while it was still running"
    finally:
        p.kill()
        p.wait()


def test_a_handle_without_synchronize_never_reads_as_reaped():
    p = _spawn_sleeper()
    try:
        created = _creation_filetime(p.pid)
        with pytest.raises(ReapFailed, match="not confirmed terminated"):
            reap_child(p.pid, created, access=_TERMINATE | _QUERY_LIMITED)  # WaitForSingleObject -> WAIT_FAILED (5)
    finally:
        p.kill()
        p.wait()


def test_run_control_does_not_report_a_reap_it_could_not_confirm(tmp_path, monkeypatch):
    # the finalizer's own guard: a recorded child whose termination is not confirmed fails the run, loudly
    p = _spawn_sleeper()
    d = stage(tmp_path)
    try:
        created = _creation_filetime(p.pid)
        (d / "pids" / "wmi.json").write_text(json.dumps({"pid": p.pid, "created": created}), encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "INVOKE_LANE", tmp_path / "missing-lane.ps1")  # control fails fast
        monkeypatch.setattr(sys.modules[__name__], "_REAP_ACCESS", _TERMINATE | _QUERY_LIMITED)
        with pytest.raises(ReapFailed, match="wmi: pid"):
            run_control(d, survival_sec=0, host_wait_sec=1, timeout=60)
    finally:
        p.kill()
        p.wait()


# ---- the live job object's flags, read back from the kernel -----------------------------------------------------

JOB_FLAGS_KILL_ON_CLOSE_ONLY = 0x2000


@pytest.fixture(scope="module")
def job_flags(tmp_path_factory):
    return probe_job_flags(stage(tmp_path_factory.mktemp("job-probe")))


def test_the_live_lane_job_reads_back_kill_on_close_and_nothing_else(job_flags):
    # QueryInformationJobObject(job, 9) on the job Invoke-Lane.ps1's own class creates: an indirect write of a breakaway
    # flag (Marshal.WriteInt32 on the marshalled struct, a second struct copy) changes this value even though the text
    # guard above sees one plain `LimitFlags=0x2000;`.
    assert job_flags == JOB_FLAGS_KILL_ON_CLOSE_ONLY, (
        f"the lane job's LimitFlags is {job_flags:#x}, expected {JOB_FLAGS_KILL_ON_CLOSE_ONLY:#x} (KILL_ON_JOB_CLOSE only)")


STRUCT_TO_PTR = "Marshal.StructureToPtr(value,mem,false);"
# Writes that never spell the LimitFlags identifier a second time, so neither the write regex nor the raw token count
# of the static guard can see them; each ORs BREAKAWAY_OK (0x800) into the marshalled struct before it is applied.
INDIRECT_BREAKAWAY_WRITES = {
    "Marshal.WriteInt32 constant": "Marshal.WriteInt32(mem,16,0x2000|0x800);",
    "Marshal.WriteInt32 read-modify-write": "Marshal.WriteInt32(mem,16,Marshal.ReadInt32(mem,16)|0x800);",
}


@pytest.mark.parametrize("name", sorted(INDIRECT_BREAKAWAY_WRITES))
def test_an_indirect_breakaway_write_is_invisible_to_the_text_guard_but_goes_red_at_runtime(name, tmp_path):
    src = INVOKE_LANE.read_text(encoding="utf-8")
    assert src.count(STRUCT_TO_PTR) == 1, "Invoke-Lane.ps1 no longer marshals the job struct the expected way"
    mutated = src.replace(STRUCT_TO_PTR, STRUCT_TO_PTR + INDIRECT_BREAKAWAY_WRITES[name])
    assert_kill_on_close_only(mutated)  # the static guard still passes: that is the gap this read-back closes
    lane_copy = tmp_path / "Invoke-Lane.mutated.ps1"
    lane_copy.write_text(mutated, encoding="utf-8")
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    flags = probe_job_flags(stage(probe_dir), lane_copy)
    assert flags == 0x2800, f"{name}: the runtime read-back saw {flags:#x}, expected KILL_ON_JOB_CLOSE|BREAKAWAY_OK 0x2800"
    assert flags != JOB_FLAGS_KILL_ON_CLOSE_ONLY


def test_the_finalizer_leaves_a_recycled_pid_alone():
    # A live process whose creation time differs from the recorded one is some other process: never touched.
    p = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 30"])
    try:
        time.sleep(0.5)
        assert reap_child(p.pid, 1) is False
        assert p.poll() is None, "a process with a different creation time must not be killed"
    finally:
        p.kill()
        p.wait()
