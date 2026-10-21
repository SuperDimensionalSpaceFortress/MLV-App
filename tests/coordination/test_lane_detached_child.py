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
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
INVOKE_LANE = ROOT / "tools" / "coordination" / "Invoke-Lane.ps1"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract")
PWSH = "pwsh.exe"

# The card names 60 s: long enough that a kill-on-close that fires late (after the handle is closed) is seen.
SURVIVAL_WAIT_SEC = 60
SLEEPER_SEC = 150  # outlives the wait plus a loaded host's start-up; the test kills it by pid in any case

HOST_PS1 = r"""
param([Parameter(Mandatory = $true)][string]$Dir)
$ErrorActionPreference = 'Stop'
$go = Join-Path $Dir 'go.flag'
while (-not (Test-Path -LiteralPath $go)) { Start-Sleep -Milliseconds 25 }
$sleep = 'Start-Sleep -Seconds __SLEEPER_SEC__'
$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = ('pwsh.exe -NoProfile -NonInteractive -Command "' + $sleep + '"')
    CurrentDirectory = $Dir; ProcessStartupInformation = $si }
if ([int]$r.ReturnValue -ne 0) { throw ('wmi-create-rc-' + $r.ReturnValue) }
$sp = Start-Process -FilePath 'pwsh.exe' -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', $sleep) -WindowStyle Hidden -PassThru
$wmiProc = Get-Process -Id ([int]$r.ProcessId)
$doc = [ordered]@{
    wmi = [ordered]@{ pid = [int]$r.ProcessId; created = $wmiProc.StartTime.ToFileTimeUtc() }
    startProcess = [ordered]@{ pid = $sp.Id; created = $sp.StartTime.ToFileTimeUtc() }
}
$tmp = Join-Path $Dir 'children.json.tmp'
[IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $tmp -Destination (Join-Path $Dir 'children.json')
""".replace("__SLEEPER_SEC__", str(SLEEPER_SEC))

CONTROL_PS1 = r"""
param(
    [Parameter(Mandatory = $true)][string]$LaneScript,
    [Parameter(Mandatory = $true)][string]$Dir,
    [Parameter(Mandatory = $true)][int]$SurvivalSec
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
$recs = $null
try {
    [MlvLaneJob]::AssignOrThrow($job, $host_.Handle)            # the inert host is in the job before it does anything
    [IO.File]::WriteAllText((Join-Path $Dir 'go.flag'), 'go')
    if (-not $host_.WaitForExit(90000)) { throw 'host-did-not-exit' }
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
    if ($null -ne $recs) {
        foreach ($k in 'wmi', 'startProcess') { if (Test-Alive $recs.$k) { Stop-Process -Id ([int]$recs.$k.pid) -Force -ErrorAction SilentlyContinue } }
    }
}
"""


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory):
    d = tmp_path_factory.mktemp("detached-child")
    (d / "host.ps1").write_text(HOST_PS1, encoding="ascii")
    (d / "control.ps1").write_text(CONTROL_PS1, encoding="ascii")
    cp = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(d / "control.ps1"),
         "-LaneScript", str(INVOKE_LANE), "-Dir", str(d), "-SurvivalSec", str(SURVIVAL_WAIT_SEC)],
        capture_output=True, text=True, timeout=SURVIVAL_WAIT_SEC + 150)
    assert cp.returncode == 0, f"control script failed rc={cp.returncode}\nstdout={cp.stdout}\nstderr={cp.stderr}"
    return json.loads(cp.stdout.strip().splitlines()[-1])


def test_job_object_is_kill_on_close_and_not_breakaway():
    # The route below only works because the job has no way for a member to leave it except a parent outside it.
    src = INVOKE_LANE.read_text(encoding="utf-8")
    m = re.search(r"value\.BasicLimitInformation\.LimitFlags=(0x[0-9A-Fa-f]+);", src)
    assert m, "MlvLaneJob.CreateKillOnClose no longer sets LimitFlags in the expected form"
    assert int(m.group(1), 16) == 0x2000, "lane job must be KILL_ON_JOB_CLOSE only (0x2000)"


def test_both_children_are_running_before_the_lane_job_closes(lifecycle):
    assert lifecycle["aliveBeforeClose"] == {"wmi": True, "startProcess": True}


def test_wmi_created_child_is_alive_60s_after_the_lane_job_closes(lifecycle):
    assert lifecycle["waitedSec"] >= SURVIVAL_WAIT_SEC
    assert lifecycle["wmiAlive"] is True, (
        "a child started through Win32_Process.Create must outlive the lane (the sanctioned detached route)")


def test_start_process_child_dies_with_the_lane_job(lifecycle):
    assert lifecycle["startProcessAlive"] is False, (
        "a child started with Start-Process stays in the kill-on-close job and must die with the lane")
