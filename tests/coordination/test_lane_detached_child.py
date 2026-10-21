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
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
$fault = $env:MLV_TEST_HOST_FAULT   # test-only: 'start-process-throws' | 'host-hangs'
$pidDir = Join-Path $Dir 'pids'

# Written the moment a child exists, before anything else can fail: the Python finalizer reads these.
function Save-ChildRecord([string]$Name, [int]$ChildPid, [int64]$Created) {
    $tmp = Join-Path $pidDir ($Name + '.json.tmp')
    $doc = [ordered]@{ pid = $ChildPid; created = $Created }
    [IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination (Join-Path $pidDir ($Name + '.json')) -Force
}

$go = Join-Path $Dir 'go.flag'
while (-not (Test-Path -LiteralPath $go)) { Start-Sleep -Milliseconds 25 }
$sleep = 'Start-Sleep -Seconds __SLEEPER_SEC__'
$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = ('pwsh.exe -NoProfile -NonInteractive -Command "' + $sleep + '"')
    CurrentDirectory = $Dir; ProcessStartupInformation = $si }
if ([int]$r.ReturnValue -ne 0) { throw ('wmi-create-rc-' + $r.ReturnValue) }
$wmiProc = Get-Process -Id ([int]$r.ProcessId)
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
if ($fault -eq 'host-hangs') { Start-Sleep -Seconds 600 }
""".replace("__SLEEPER_SEC__", str(SLEEPER_SEC))

CONTROL_PS1 = r"""
param(
    [Parameter(Mandatory = $true)][string]$LaneScript,
    [Parameter(Mandatory = $true)][string]$Dir,
    [Parameter(Mandatory = $true)][int]$SurvivalSec,
    [Parameter(Mandatory = $true)][int]$HostWaitSec
)
$ErrorActionPreference = 'Stop'
$src = [IO.File]::ReadAllText($LaneScript)
$m = [regex]::Match($src, "(?s)if \(-not \('MlvLaneJob' -as \[type\]\)\) \{\s*Add-Type -TypeDefinition @'\r?\n(.*?)\r?\n'@")
if (-not $m.Success) { throw 'job-class-not-found-in-Invoke-Lane' }
Add-Type -TypeDefinition $m.Groups[1].Value

function Test-Alive($Rec) {
    $p = Get-Process -Id ([int]$Rec.pid) -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $false }
    # same pid AND same creation time (a file-time integer; ConvertFrom-Json would reshape an ISO string),
    # so a reused pid is not mistaken for the child
    return ($p.StartTime.ToFileTimeUtc() -eq [int64]$Rec.created)
}

$job = [MlvLaneJob]::CreateKillOnClose()
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

_TERMINATE, _QUERY_LIMITED, _STILL_ACTIVE = 0x0001, 0x1000, 259


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def reap_child(pid, created, *, kill=True):
    """True when pid is a live process whose creation FILETIME equals `created` (killed first when `kill`).

    The kill goes through the handle the creation time was read from, so a pid recycled after this check
    cannot be hit; a pid that was already recycled has a different creation time and is never touched."""
    k = _kernel32()
    h = k.OpenProcess(_TERMINATE | _QUERY_LIMITED, False, pid)
    if not h:
        return False
    try:
        times = [ctypes.c_uint64() for _ in range(4)]
        if not k.GetProcessTimes(h, *[ctypes.byref(t) for t in times]) or times[0].value != created:
            return False
        code = ctypes.c_uint32()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return False  # exited; a leftover handle elsewhere keeps the pid openable
        if kill:
            k.TerminateProcess(h, 1)
            k.WaitForSingleObject(h, 5000)
        return True
    finally:
        k.CloseHandle(h)


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
    return d


def run_control(d, *, survival_sec, host_wait_sec=HOST_WAIT_SEC, timeout, fault=None):
    """Run the control script; whatever happens, kill every recorded child that is still the same process.

    Returns (completed_process_or_None, timed_out, records, reaped) where `reaped` names the children that were
    still alive when the finalizer ran (the orphans this module would otherwise leave behind)."""
    env = dict(os.environ)
    env.pop("MLV_TEST_HOST_FAULT", None)
    if fault:
        env["MLV_TEST_HOST_FAULT"] = fault
    cp, timed_out = None, False
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
        reaped = [name for name, (pid, created) in records.items() if reap_child(pid, created)]
    return cp, timed_out, records, reaped


def children_alive(records):
    return {name: reap_child(pid, created, kill=False) for name, (pid, created) in records.items()}


# name -> run_control arguments. Each leaves the WMI sleeper (outside the job) running unless the finalizer kills it.
FAILURE_PATHS = {
    "start-process-throws": dict(fault="start-process-throws", survival_sec=5, timeout=90),
    "host-does-not-exit": dict(fault="host-hangs", survival_sec=5, host_wait_sec=8, timeout=90),
    "subprocess-timeout": dict(survival_sec=SURVIVAL_WAIT_SEC, timeout=15),
}


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
    cp, timed_out, _records, _reaped = run_control(
        d, survival_sec=SURVIVAL_WAIT_SEC, host_wait_sec=HOST_WAIT_SEC, timeout=SURVIVAL_WAIT_SEC + 150)
    assert not timed_out, "control script timed out"
    assert cp.returncode == 0, f"control script failed rc={cp.returncode}\nstdout={cp.stdout}\nstderr={cp.stderr}"
    return json.loads(cp.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module", params=sorted(FAILURE_PATHS))
def failure_path(request, failure_paths_started):
    return request.param, failure_paths_started[request.param].result()


# ---- the job object itself --------------------------------------------------------------------------------

JOB_CLASS_RE = re.compile(r"(?s)if \(-not \('MlvLaneJob' -as \[type\]\)\) \{\s*Add-Type -TypeDefinition @'\r?\n(.*?)\r?\n'@")
# any assignment operator (=, |=, &=, ^=, +=, -=, <<=, ...) whose target is a LimitFlags member; == is a comparison
LIMIT_FLAGS_WRITE_RE = re.compile(r"\bLimitFlags\s*(?:<<|>>|[-+*/%&|^])?=(?!=)([^;]*)")


def limit_flag_writes(src):
    """Right-hand sides of every LimitFlags write inside the MlvLaneJob Add-Type block."""
    m = JOB_CLASS_RE.search(src)
    assert m, "MlvLaneJob Add-Type block not found in Invoke-Lane.ps1"
    return [rhs.strip() for rhs in LIMIT_FLAGS_WRITE_RE.findall(m.group(1))]


def assert_kill_on_close_only(src):
    writes = limit_flag_writes(src)
    assert len(writes) == 1, f"MlvLaneJob must write LimitFlags exactly once, found {len(writes)}: {writes}"
    assert re.fullmatch(r"0[xX][0-9A-Fa-f]+", writes[0]), f"LimitFlags write is not a plain hex literal: {writes[0]}"
    assert int(writes[0], 16) == 0x2000, "lane job must be KILL_ON_JOB_CLOSE only (0x2000)"


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
    name, (cp, timed_out, records, reaped) = failure_path
    # the path really happened, not a start-up error that never reached the WMI Create
    assert "wmi" in records, f"{name}: the host never recorded its WMI child; stdout={getattr(cp, 'stdout', None)}"
    if name == "subprocess-timeout":
        assert timed_out, "the control script finished inside the timeout; the timeout path was not exercised"
    else:
        assert not timed_out and cp.returncode != 0, f"{name}: expected the control script to fail, rc={cp and cp.returncode}"
    # the WMI child is outside the job, so it was still running when the finalizer ran: that is the orphan it killed
    assert "wmi" in reaped, f"{name}: the WMI sleeper was not alive for the finalizer to kill (reaped={reaped})"
    assert not any(children_alive(records).values()), f"{name}: a recorded sleeper is still alive: {children_alive(records)}"


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
