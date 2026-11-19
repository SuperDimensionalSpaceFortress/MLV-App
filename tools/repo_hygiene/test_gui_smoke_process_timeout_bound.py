"""VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1: the GUI-smoke process wait reports its whole budget and has a no-progress watchdog.

A venue leg that should take ~4 min held the Bachelor venue for 60 min, and its receipt said "exceeded the fail-closed process timeout of 1 ms"
(process.timeoutMs = 1) because run-release-gui-smoke.ps1 handed Wait-GuiSmokeProcessBounded only the final chunk REMAINDER of its budget.
These tests execute pwsh against the real module (tools/profiling/gui-smoke-process-boundary.psm1) with stub processes and temp logs only; no MLVApp
is launched. The runner's own wiring is pinned by reading its text, because a full GUI-smoke launch needs a real MLVApp.exe and clip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "gui-smoke-process-boundary.psm1"
RUNNER = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PWSH = shutil.which("pwsh")

_PRELUDE = r"""
$ErrorActionPreference = 'Stop'
Import-Module $env:GS_MODULE -Force
function New-Stub([string]$Command) {
    $psi = [Diagnostics.ProcessStartInfo]::new($env:GS_PWSH); $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    foreach ($a in @('-NoProfile', '-NonInteractive', '-Command', $Command)) { [void]$psi.ArgumentList.Add($a) }
    $p = [Diagnostics.Process]::Start($psi)
    [pscustomobject]@{ p = $p; o = $p.StandardOutput.ReadToEndAsync(); e = $p.StandardError.ReadToEndAsync() }
}
"""


def run_ps(body: str, **extra_env: str) -> dict:
    assert PWSH, "pwsh is required"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.ps1"
        script.write_text(_PRELUDE + body, encoding="utf-8")
        env = dict(os.environ, GS_MODULE=str(MODULE), GS_PWSH=PWSH, GS_TMP=tmp, **extra_env)
        proc = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, text=True, timeout=180, env=env, creationflags=_NO_WINDOW,
        )
    if proc.returncode != 0:
        raise AssertionError(f"probe failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")][-1]
    return json.loads(line)


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class ReportedTimeoutIsTheWholeBudget(unittest.TestCase):
    def test_timeout_receipt_names_the_whole_budget_not_the_final_remainder(self) -> None:
        out = run_ps(r"""
$s = New-Stub 'Start-Sleep -Seconds 120'
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1500 -ReportedTimeoutMs 600000
[pscustomobject]@{ timedOut = $r.timedOut; timeoutMs = $r.timeoutMs; failures = @($r.failures); exitCode = $r.exitCode; hasExited = $s.p.HasExited; finalWaitMs = $r.finalWaitMs } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["timedOut"])
        self.assertEqual(600000, out["timeoutMs"], "the receipt must name the whole budget, never the last chunk remainder")
        self.assertEqual(1500, out["finalWaitMs"], "the remainder actually waited stays recorded, under its own name")
        text = " ".join(out["failures"])
        self.assertIn("600000 ms", text)
        self.assertNotIn("of 1500 ms", text)
        self.assertEqual(124, out["exitCode"])
        self.assertTrue(out["hasExited"])

    def test_without_a_reported_budget_the_wait_time_is_still_the_reported_timeout(self) -> None:
        out = run_ps(r"""
$s = New-Stub 'Start-Sleep -Seconds 120'
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1500
[pscustomobject]@{ timeoutMs = $r.timeoutMs; outcome = $r.outcome } | ConvertTo-Json -Compress
""")
        self.assertEqual(1500, out["timeoutMs"])
        self.assertEqual("timeout", out["outcome"])

    def test_runner_hands_the_whole_budget_to_the_wait(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        start = source.index("$processBoundary = Wait-GuiSmokeProcessBounded")
        call = source[start:source.index("\n$stdout = $processBoundary.stdout", start)]
        self.assertIn("-ReportedTimeoutMs $effectiveProcessTimeoutMs", call)
        self.assertIn("-NoProgressStalled:$noProgressStalled", call)


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class NoProgressWindowComesFromTheLegBudget(unittest.TestCase):
    def test_window_is_leg_budget_plus_margin_and_never_the_job_timeout(self) -> None:
        out = run_ps(r"""
[pscustomobject]@{
    derived = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 155000
    override = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 155000 -OverrideMs 90000
    capped = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 155000 -CeilingMs 100000
    cappedOverride = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 155000 -OverrideMs 500000 -CeilingMs 100000
} | ConvertTo-Json -Compress
""")
        self.assertEqual(155000 + 60000, out["derived"])
        self.assertEqual(90000, out["override"])
        self.assertEqual(100000, out["capped"])
        self.assertEqual(100000, out["cappedOverride"])

    def test_runner_derives_the_window_from_its_own_leg_inputs(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        start = source.index("$noProgressWindowMs = Get-GuiSmokeNoProgressWindowMs")
        call = source[start:source.index("\n", start)]
        self.assertIn("-LegBudgetMs $derivedProcessTimeoutMs", call)
        # r4 (adversary b0a33ba9 A5): the override is the runner's >= 1000 ms clamp of -NoProgressTimeoutMs ($noProgressOverrideMs)
        self.assertIn("-OverrideMs $noProgressOverrideMs", call)
        # -2 r1: capped at L (the watchdog limit derived from the hard deadline), the one cap both windows and an override share
        self.assertIn("-CeilingMs $noProgressLimitMs", call)
        self.assertIn("$noProgressLimitMs = Get-GuiSmokeWatchdogLimitMs -HardMs $effectiveProcessTimeoutMs -ProbeWaitMs $noProgressProbeWaitMs", source)
        self.assertNotIn("-BelowCeilingMs", _runner_snippet(*_WAIT_REGION), "-2 r1: no per-window margin; L carries the one margin M")
        # derived from the leg (playback window + settle + margin), so a caller's 3600 s job timeout cannot widen it
        self.assertNotIn("3600", call)


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class LogProgressWatchdog(unittest.TestCase):
    def test_a_log_that_stops_advancing_trips_and_one_that_keeps_advancing_does_not(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'logs'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1500
$early = $false
Add-Content -LiteralPath $log -Value 'line0'
for ($i = 0; $i -lt 14; $i++) { Add-Content -LiteralPath $log -Value "line$i"; Start-Sleep -Milliseconds 250; if (Test-GuiSmokeLogProgressStalled -Watch $w) { $early = $true } }
$sw = [Diagnostics.Stopwatch]::StartNew(); $tripped = $false
while ($sw.ElapsedMilliseconds -lt 8000 -and -not $tripped) { Start-Sleep -Milliseconds 200; if (Test-GuiSmokeLogProgressStalled -Watch $w) { $tripped = $true } }
[pscustomobject]@{ trippedWhileAdvancing = $early; tripped = $tripped; stallMs = [int]$sw.ElapsedMilliseconds } | ConvertTo-Json -Compress
""")
        self.assertFalse(out["trippedWhileAdvancing"], "a log that keeps advancing must not trip the watchdog")
        self.assertTrue(out["tripped"], "a log that stops advancing must trip it")
        self.assertGreaterEqual(out["stallMs"], 1000)
        self.assertLess(out["stallMs"], 6000)

    def test_a_run_whose_log_never_appeared_is_not_judged(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'empty'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 500
Start-Sleep -Milliseconds 1500
[pscustomobject]@{ stalled = [bool](Test-GuiSmokeLogProgressStalled -Watch $w) } | ConvertTo-Json -Compress
""")
        self.assertFalse(out["stalled"], "before the app writes its first log line the overall budget is the only bound")

    def test_no_progress_ends_the_wait_with_a_typed_outcome_and_kills_only_the_started_tree(self) -> None:
        out = run_ps(r"""
$decoy = New-Stub 'Start-Sleep -Seconds 120'
$s = New-Stub ('& "' + $env:GS_PWSH + '" -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 120"')
Start-Sleep -Milliseconds 1500
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 600000 -ReportedTimeoutMs 600000 -NoProgressStalled -NoProgressWindowMs 1500
$waitedMs = [int]$sw.ElapsedMilliseconds
$decoyAlive = -not $decoy.p.HasExited
if ($decoyAlive) { $decoy.p.Kill($true) }
[pscustomobject]@{ outcome = $r.outcome; timedOut = $r.timedOut; noProgress = $r.noProgress; noProgressWindowMs = $r.noProgressWindowMs; timeoutMs = $r.timeoutMs
    treeKillAttempted = $r.treeKillAttempted; treeKillSucceeded = $r.treeKillSucceeded; terminationConfirmed = $r.terminationConfirmed; exitCode = $r.exitCode
    failures = @($r.failures); hasExited = $s.p.HasExited; decoyAlive = $decoyAlive; waitedMs = $waitedMs } | ConvertTo-Json -Compress
""")
        self.assertEqual("process_no_progress", out["outcome"])
        self.assertTrue(out["timedOut"], "exit code 124 and the existing timed-out receipt path stay")
        self.assertTrue(out["noProgress"])
        self.assertEqual(1500, out["noProgressWindowMs"])
        self.assertEqual(600000, out["timeoutMs"])
        self.assertTrue(out["treeKillAttempted"] and out["treeKillSucceeded"] and out["terminationConfirmed"])
        self.assertEqual(124, out["exitCode"])
        self.assertTrue(out["hasExited"])
        self.assertTrue(out["decoyAlive"], "only the process this runner started may be killed, never one picked by image name")
        text = " ".join(out["failures"])
        self.assertIn("process_no_progress", text)
        self.assertNotIn("exceeded the fail-closed process timeout", text)
        self.assertLess(out["waitedMs"], 60000, "a stalled leg must end at once, not wait out the remaining budget")

    # adversary 797947a7 A1: after arming, a log that vanishes or is held unreadable, or a total that shrinks, is NOT progress.
    def test_a_deleted_or_locked_or_shrinking_log_is_no_progress(self) -> None:
        out = run_ps(r"""
function Trip([scriptblock]$Mutate, [int]$Window = 1200) {
    $dir = Join-Path $env:GS_TMP ([guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Set-Content -LiteralPath (Join-Path $dir 'mlvapp-1.log') -Value ('x' * 1000)
    Set-Content -LiteralPath (Join-Path $dir 'mlvapp-2.log') -Value ('y' * 1000)
    $w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs $Window
    [void](Test-GuiSmokeLogProgressStalled -Watch $w)
    $held = & $Mutate $dir
    $sw = [Diagnostics.Stopwatch]::StartNew(); $tripped = $false
    while ($sw.ElapsedMilliseconds -lt 6000 -and -not $tripped) { Start-Sleep -Milliseconds 150; if (Test-GuiSmokeLogProgressStalled -Watch $w) { $tripped = $true } }
    if ($held) { $held.Dispose() }
    Stop-GuiSmokeLogProgressWatch -Watch $w
    $tripped
}
[pscustomobject]@{
    deleted = [bool](Trip { param($d) Remove-Item -LiteralPath (Join-Path $d 'mlvapp-1.log'); Remove-Item -LiteralPath (Join-Path $d 'mlvapp-2.log'); $null })
    locked = [bool](Trip { param($d) [IO.File]::Open((Join-Path $d 'mlvapp-1.log'), 'Open', 'ReadWrite', 'None') | Out-Null; $null })
    shrunk = [bool](Trip { param($d) Remove-Item -LiteralPath (Join-Path $d 'mlvapp-2.log'); $null })
} | ConvertTo-Json -Compress
""")
        self.assertTrue(out["deleted"], "a vanished log must not read as 'no data, so not stalled' forever")
        self.assertTrue(out["shrunk"], "a total that shrinks is not progress")
        self.assertTrue(out["locked"], "a log held unreadable is not progress")

    # adversary 797947a7 A2: a chunked wait longer than the window would let a one-second window permit a minute or two.
    def test_poll_chunk_is_a_fraction_of_the_window_and_the_runner_uses_it(self) -> None:
        out = run_ps(r"""
[pscustomobject]@{ a = Get-GuiSmokeNoProgressChunkMs -WindowMs 1000; b = Get-GuiSmokeNoProgressChunkMs -WindowMs 8000; c = Get-GuiSmokeNoProgressChunkMs -WindowMs 215000 } | ConvertTo-Json -Compress
""")
        self.assertEqual(250, out["a"])
        self.assertEqual(2000, out["b"])
        self.assertEqual(4000, out["c"])
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("$noProgressChunkMs = Get-GuiSmokeNoProgressChunkMs -WindowMs $noProgressWindowMs", source)
        # -2 r1: each chunk wait is min(chunk, D - now, max(1, E - now)), read from the launch stamp by the one helper at the wait
        self.assertIn("$hostLoadChunkMs = Get-GuiSmokeChunkWaitMs -ChunkMs ([Math]::Min($HostLoadSampleIntervalMs, $noProgressChunkMs)) -LaunchTimestamp $startTimestamp -DeadlineMs $effectiveProcessTimeoutMs -ExpiryMs $noProgressWatch.ExpiryMs", source)
        self.assertIn("$noProgressChunkWaitMs = Get-GuiSmokeChunkWaitMs -ChunkMs $noProgressChunkMs -LaunchTimestamp $startTimestamp -DeadlineMs $effectiveProcessTimeoutMs -ExpiryMs $noProgressWatch.ExpiryMs", source)

    # adversary 797947a7 A3: an -ExtraEnvironment pair must not move the log directory the watchdog (and the runner's own evidence read) uses.
    def test_extra_environment_cannot_redirect_the_log_directory(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        for target in ("$launchEnv", "$envBlock"):
            pairs = source.index(f"Add-EnvironmentPairs -Target {target} -Pairs $ExtraEnvironment")
            nxt = source.index("\n", pairs)
            reassert = source.index(f'{target}["MLVAPP_CRASH_FORENSICS_LOG_DIR"] = $logRoot', nxt)
            self.assertLess(reassert - nxt, 700, f"{target}: the log directory must be re-asserted right after the extra pairs")

    # adversary 797947a7 A4: a filesystem call that hangs (a stalled share) must not hold the chunk loop past its own budget.
    def test_a_hung_log_probe_never_blocks_the_caller(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'hung'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeDelayMs 40000 -ProbeWaitMs 300
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = @(1..4 | ForEach-Object { [bool](Test-GuiSmokeLogProgressStalled -Watch $w) })
$ms = [int]$sw.ElapsedMilliseconds
$sw.Restart(); Stop-GuiSmokeLogProgressWatch -Watch $w; $stopMs = [int]$sw.ElapsedMilliseconds
[pscustomobject]@{ results = $r; ms = $ms; stopMs = $stopMs } | ConvertTo-Json -Compress
""")
        self.assertFalse(any(out["results"]), "a probe that has not answered is unknown, and the overall budget stays the bound")
        self.assertLess(out["ms"], 5000, "four polls with a hung probe must cost about 4 x 300 ms, not the probe's 40 s")
        self.assertLess(out["stopMs"], 5000)

    # adversary 9fdf9e1a A1 (r5): a log that is listed but whose open BLOCKS (a stalled share) still arms the watch, so the leg ends on its
    # no-progress window and not on the whole process budget. The probe announces every listed log before it opens any.
    def test_a_listed_log_whose_open_blocks_still_arms_the_watch_and_ends_the_leg(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'openblocks'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-1.log') -Value 'first line'
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeOpenDelayMs 40000 -ProbeWaitMs 300
$sw = [Diagnostics.Stopwatch]::StartNew(); $tripped = $false; $armedAt = -1
while ($sw.ElapsedMilliseconds -lt 9000 -and -not $tripped) {
    if (Test-GuiSmokeLogProgressStalled -Watch $w) { $tripped = $true }
    if ($w.Armed -and $armedAt -lt 0) { $armedAt = [int]$sw.ElapsedMilliseconds }
    Start-Sleep -Milliseconds 150
}
$stillPending = ($null -ne $w.Pending)
$ms = [int]$sw.ElapsedMilliseconds
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ tripped = $tripped; armed = [bool]$w.Armed; stillPending = $stillPending; ms = $ms } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["armed"], "the announced log arms the watch although its open never returns")
        self.assertTrue(out["tripped"], "a leg whose only log cannot be opened ends on the no-progress window")
        self.assertTrue(out["stillPending"], "the probe is still blocked on the open: the verdict did not wait for it")
        self.assertLess(out["ms"], 8000)

    # adversary 9fdf9e1a A2 (r5): a growth the probe has reported is credited at the poll that sees it, not when the whole batch (here held up
    # by a second slow open) ends, so a stalled log is not given a second no-progress window from the batch's end.
    def test_a_reported_growth_is_credited_when_seen_not_when_the_batch_ends(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'creditwhenseen'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$a = Join-Path $dir 'mlvapp-1.log'; $b = Join-Path $dir 'mlvapp-2.log'
Set-Content -LiteralPath $a -Value ('x' * 1000) -NoNewline
Set-Content -LiteralPath $b -Value ('y' * 1000) -NoNewline
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 3000 -ProbeWaitMs 300
$sw = [Diagnostics.Stopwatch]::StartNew()
do { [void](Test-GuiSmokeLogProgressStalled -Watch $w); Start-Sleep -Milliseconds 100 } while (($null -ne $w.Pending -or -not $w.Armed) -and $sw.ElapsedMilliseconds -lt 8000)
$w.ProbeOpenDelayMs = 2000
Add-Content -LiteralPath $a -Value 'z' -NoNewline
$growth = [Diagnostics.Stopwatch]::StartNew(); $tripped = $false
while ($growth.ElapsedMilliseconds -lt 12000 -and -not $tripped) {
    if (Test-GuiSmokeLogProgressStalled -Watch $w) { $tripped = $true }
    Start-Sleep -Milliseconds 100
}
$stalledAfterMs = [int]$growth.ElapsedMilliseconds
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ tripped = $tripped; stalledAfterMs = $stalledAfterMs } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["tripped"])
        # a is read at about 2 s and b at about 4 s: credited when seen the verdict is about 2 s + the 3 s window = 5 s, credited when the batch ends it is 4 s + 3 s = 7 s
        self.assertLess(out["stalledAfterMs"], 6200,"the growth must be credited at the poll that sees it, not at the end of the batch")

    # adversary 1b823896 A2 (r5 fix 2): the announcement arms the watch and starts the quiet clock; the log's FIRST read, arriving late, is only a
    # baseline. Unchanged data must not be granted a second window from the read.
    def test_a_late_first_read_of_an_announced_log_does_not_restart_the_quiet_clock(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'latefirstread'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-1.log') -Value ('x' * 1000) -NoNewline
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 3000 -ProbeOpenDelayMs 2750 -ProbeWaitMs 300
$sw = [Diagnostics.Stopwatch]::StartNew(); $tripped = $false; $armedAt = -1
while ($sw.ElapsedMilliseconds -lt 12000 -and -not $tripped) {
    if (Test-GuiSmokeLogProgressStalled -Watch $w) { $tripped = $true }
    if ($w.Armed -and $armedAt -lt 0) { $armedAt = [int]$sw.ElapsedMilliseconds }
    Start-Sleep -Milliseconds 100
}
$ms = [int]$sw.ElapsedMilliseconds
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ tripped = $tripped; armedAt = $armedAt; ms = $ms } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["tripped"])
        self.assertGreaterEqual(out["armedAt"], 0)
        # armed at the announcement, so the verdict is at about armedAt + 3 s; a first read credited as growth would move it to about armedAt + 5.75 s
        self.assertLess(out["ms"] - out["armedAt"], 4600, "unchanged data was granted a second window by its first read")

    # adversary 1b823896 A1 (r5 fix 2): the announcement of a listed log comes out of the PIPELINE as the listing yields it, never after the whole
    # listing is collected, so a listing that blocks after its first entry still arms the watch.
    def test_the_announcement_streams_from_the_listing_before_the_listing_completes(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        start = text.index("ForEach-Object -ThrottleLimit 1 -AsJob -Parallel {")
        probe = text[start:text.index("\n        }\n        $Watch.Hung = $false", start)]
        self.assertNotIn("@(Get-ChildItem", probe, "collecting the listing first would hold every announcement behind a blocked enumeration")
        self.assertIn("Get-ChildItem -LiteralPath $using:probeRoot -Filter 'mlvapp-*.log' -File -ErrorAction SilentlyContinue | ForEach-Object { '{0}|-2' -f $_.Name;", probe)
        self.assertLess(probe.index("|-2' -f $_.Name"), probe.index("[System.IO.File]::Open("), "every announcement precedes the first open")

    # adversary 57a7a5c9 A1: toggling read access to one log (total 2000 / 1000 / 2000 ...) must not read as growth -- restoration is not progress.
    def test_denying_and_restoring_read_access_cannot_manufacture_progress(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'toggle'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-1.log') -Value ('x' * 1000) -NoNewline
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-2.log') -Value ('y' * 1000) -NoNewline
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000
[void](Test-GuiSmokeLogProgressStalled -Watch $w)
$held = $null; $sw = [Diagnostics.Stopwatch]::StartNew(); $tripped = $false; $flip = 0
while ($sw.ElapsedMilliseconds -lt 7000 -and -not $tripped) {
    if (([int]($sw.ElapsedMilliseconds / 400)) -ne $flip) {
        $flip = [int]($sw.ElapsedMilliseconds / 400)
        if ($held) { $held.Dispose(); $held = $null } else { $held = [IO.File]::Open((Join-Path $dir 'mlvapp-2.log'), 'Open', 'ReadWrite', 'None') }
    }
    Start-Sleep -Milliseconds 100
    if (Test-GuiSmokeLogProgressStalled -Watch $w) { $tripped = $true }
}
if ($held) { $held.Dispose() }
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ tripped = $tripped } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["tripped"], "a log whose read access is toggled while its content never changes is a stalled leg")

    # adversary 57a7a5c9 A3: the first sign of a log arms the watch even when that log cannot be read at the first look.
    def test_a_log_unreadable_from_the_first_look_still_arms_the_watch(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'lockedfirst'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value 'first line'
$held = [IO.File]::Open($log, 'Open', 'ReadWrite', 'None')
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000
$sw = [Diagnostics.Stopwatch]::StartNew(); $tripped = $false
while ($sw.ElapsedMilliseconds -lt 6000 -and -not $tripped) { Start-Sleep -Milliseconds 150; if (Test-GuiSmokeLogProgressStalled -Watch $w) { $tripped = $true } }
$held.Dispose(); Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ tripped = $tripped } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["tripped"], "an app that logged once and is now unreadable-and-silent must not keep the whole job budget")

    # adversary 57a7a5c9 A2 (+ advisory A4): a hung probe is waited on ONCE, and never past what is left of the process budget.
    def test_probe_wait_is_bounded_by_the_remaining_budget_and_a_hung_probe_is_not_waited_on_twice(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'budget'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeDelayMs 40000 -ProbeWaitMs 1000
$sw = [Diagnostics.Stopwatch]::StartNew()
[void](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs ([int64](Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp) + 50))
$cappedMs = [int]$sw.ElapsedMilliseconds
$w2 = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeDelayMs 40000 -ProbeWaitMs 400
$sw.Restart()
1..5 | ForEach-Object { [void](Test-GuiSmokeLogProgressStalled -Watch $w2) }
$fiveMs = [int]$sw.ElapsedMilliseconds
Stop-GuiSmokeLogProgressWatch -Watch $w; Stop-GuiSmokeLogProgressWatch -Watch $w2
[pscustomobject]@{ cappedMs = $cappedMs; fiveMs = $fiveMs } | ConvertTo-Json -Compress
""")
        self.assertLess(out["cappedMs"], 500, "a probe wait must not exceed the budget the caller has left")
        self.assertLess(out["fiveMs"], 1200, "five polls of one hung probe cost about one 400 ms wait, not five")
        source = RUNNER.read_text(encoding="utf-8")
        calls = source.count("Test-GuiSmokeLogProgressStalled -Watch $noProgressWatch")
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(calls, source.count("Test-GuiSmokeLogProgressStalled -Watch $noProgressWatch -DeadlineMs $effectiveProcessTimeoutMs"),
                         "every runner call hands the hard deadline (ms since launch) to the probe wait")

    # adversary b0a33ba9 A1 (r4): a log unreadable at its first sighting is UNKNOWN, not length 0; becoming readable at an unchanged length is
    # not growth, and only growth beyond the first KNOWN length restarts the quiet clock.
    def test_a_log_unreadable_at_first_sight_then_readable_unchanged_is_not_progress(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'unknownfirst'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value ('x' * 1000) -NoNewline
$held = [IO.File]::Open($log, 'Open', 'ReadWrite', 'None')
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 600000 -ProbeWaitMs 5000
[void](Test-GuiSmokeLogProgressStalled -Watch $w)
$armed = [bool]$w.Armed
Start-Sleep -Milliseconds 1200
$held.Dispose()
[void](Test-GuiSmokeLogProgressStalled -Watch $w)
# -2 r1: the quiet time is now - P, both read on the watch's launch clock
$quietAfterRestore = [int]((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp) - $w.LastProgressMs)
$baseline = [int64]$w.Files['mlvapp-1.log']
Add-Content -LiteralPath $log -Value 'more' -NoNewline
[void](Test-GuiSmokeLogProgressStalled -Watch $w)
$quietAfterGrowth = [int]((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp) - $w.LastProgressMs)
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ armed = $armed; quietAfterRestore = $quietAfterRestore; baseline = $baseline; quietAfterGrowth = $quietAfterGrowth } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["armed"], "the unreadable first sighting still arms the watch")
        self.assertGreaterEqual(out["quietAfterRestore"], 1000, "becoming readable at an unchanged length must not restart the quiet clock")
        self.assertEqual(1000, out["baseline"], "the first readable length after unknown is the baseline")
        self.assertLess(out["quietAfterGrowth"], 600, "growth beyond a known baseline restarts the quiet clock")

    # adversary b0a33ba9 A2 (r4): no budget left = no probe and no wait; the wait is re-derived from an absolute end just before WaitOne;
    # a probe result that lands once the budget is spent is ignored.
    def test_probe_is_not_started_or_waited_on_without_budget_and_a_late_result_is_ignored(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'nobudget'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeWaitMs 1000
Start-Sleep -Milliseconds 20
$sw = [Diagnostics.Stopwatch]::StartNew()
# -2 r1: a deadline already reached (1 ms after the watch's launch) is the old zero budget
$stalled = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 1)
$zeroMs = [int]$sw.ElapsedMilliseconds
$pendingAfterZero = ($null -ne $w.Pending)
Stop-GuiSmokeLogProgressWatch -Watch $w
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-1.log') -Value ('x' * 500) -NoNewline
$late = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeDelayMs 600 -ProbeWaitMs 5000
$lateDeadline = [int64](Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $late.LaunchTimestamp) + 300
[void](Test-GuiSmokeLogProgressStalled -Watch $late -DeadlineMs $lateDeadline)
Start-Sleep -Milliseconds 1500
[void](Test-GuiSmokeLogProgressStalled -Watch $late -DeadlineMs $lateDeadline)
$lateArmed = [bool]$late.Armed
$lateFiles = @($late.Files.Keys).Count
Stop-GuiSmokeLogProgressWatch -Watch $late
[pscustomobject]@{ stalled = $stalled; zeroMs = $zeroMs; pendingAfterZero = $pendingAfterZero; lateArmed = $lateArmed; lateFiles = $lateFiles } | ConvertTo-Json -Compress
""")
        self.assertFalse(out["stalled"])
        self.assertFalse(out["pendingAfterZero"], "with no budget left no probe may be started")
        self.assertLess(out["zeroMs"], 300)
        self.assertFalse(out["lateArmed"], "a probe result that lands after the budget was spent is ignored")
        self.assertEqual(0, out["lateFiles"])
        text = MODULE.read_text(encoding="utf-8")
        body = text[text.index("function Test-GuiSmokeLogProgressStalled"):text.index("# r10 (adversary 3534e113 A1)")]
        # -2 r1: no entry stopwatch and no carried remainder: the probe wait is D minus the time since the watch's launch stamp, read just before WaitOne
        self.assertNotIn("Stopwatch]::StartNew()", body)
        self.assertNotIn("BudgetRemainingMs", body)
        self.assertIn("[double]$DeadlineMs - (Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch)", body)
        self.assertLess(body.index("[double]$DeadlineMs - (Get-GuiSmokeSinceLaunchMs"), body.index("Finished.WaitOne("))

    # adversary b0a33ba9 A4 (r4): the early no-progress stop must not also print the whole-timeout message.
    def test_runner_prints_the_no_progress_message_alone_for_a_no_progress_stop(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        start = source.index("if ($processBoundary.timedOut) {")
        block = source[start:source.index("if ($processExitCode -ne 0) {", start)]
        self.assertIn("$processBoundary.outcome -eq 'process_no_progress'", block)
        self.assertEqual(1, block.count("exceeded its fail-closed process timeout"))
        self.assertLess(block.index("$processBoundary.outcome -eq 'process_no_progress'"), block.index("exceeded its fail-closed process timeout"))
        self.assertIn("else {", block, "the whole-timeout message is the else branch of the typed outcome")

    # adversary b0a33ba9 A5 (r4): an explicit window below 1 s is clamped in the runner; the helper keeps its 250 ms floor for any window.
    def test_runner_clamps_an_explicit_no_progress_override_to_one_second(self) -> None:
        out = run_ps(r"""
[pscustomobject]@{ one = Get-GuiSmokeNoProgressChunkMs -WindowMs 1; two = Get-GuiSmokeNoProgressChunkMs -WindowMs 999 } | ConvertTo-Json -Compress
""")
        self.assertEqual(250, out["one"], "the helper's max(250, min(4000, window/4)) floor stays for a window of 1 ms")
        self.assertEqual(250, out["two"])
        source = RUNNER.read_text(encoding="utf-8")
        start = source.index("$noProgressWindowMs = Get-GuiSmokeNoProgressWindowMs")
        call = source[start:source.index("\n", start)]
        self.assertIn("-OverrideMs $noProgressOverrideMs", call)
        clamp = source[:start]
        self.assertIn("[Math]::Max(1000, $NoProgressTimeoutMs)", clamp)
        self.assertIn("$noProgressOverrideMs = if ($NoProgressTimeoutMs -gt 0)", clamp)

    def test_runner_checks_the_log_between_chunks_and_receipt_carries_the_outcome(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        loop_start = source.index("$hostLoadDuringSamples.Add($newSample)")
        loop_end = source.index("$hostLoadSampleLoopElapsedMs = [int]", loop_start)
        loop = source[loop_start:loop_end]
        self.assertIn("Test-GuiSmokeLogProgressStalled -Watch $noProgressWatch", loop)
        self.assertIn("$noProgressStalled = $true", loop)
        self.assertIn("New-GuiSmokeLogProgressWatch -LogRoot $logRoot", source)
        receipt_start = source.index("\n    process = [pscustomobject]@{\n")
        receipt = source[receipt_start:source.index("\n    }\n", receipt_start)]
        for field in ("outcome = ", "noProgressWindowMs = ", "legBudgetMs = ", "finalWaitMs = "):
            self.assertIn(field, receipt)
        self.assertIn("[int]$NoProgressTimeoutMs = 0", source)


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class NewLogAnnouncedMidRunIsASignOfLife(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r6 (adversary 2d33c39f A1): a log name first seen AFTER the watch armed is itself a sign of life, at its
    announcement; its delayed first read is only a baseline. The r5 -3 state credited the READ instead, so an announced-but-unread fresh log gave no
    life while an unchanged delayed read gave a whole second window."""

    def test_a_fresh_log_announced_mid_run_restarts_the_quiet_clock_at_its_announcement(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'freshannounce'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-a.log') -Value ('x' * 100) -NoNewline
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeOpenDelayMs 30000 -ProbeWaitMs 300
# an armed watch whose app log (a) has been quiet for longer than the window, as after a stall the watchdog has not yet judged (-2 r1: P is set to now)
$w.Files['mlvapp-a.log'] = [int64]100; $w.Armed = $true; $w.LastProgressMs = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp
Start-Sleep -Milliseconds 1500
$quietBefore = [int]((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp) - $w.LastProgressMs)
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-b.log') -Value ('y' * 100) -NoNewline
$stalledAtAnnouncement = [bool](Test-GuiSmokeLogProgressStalled -Watch $w)
$quietAfter = [int]((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp) - $w.LastProgressMs)
$known = @($w.Files.Keys) -contains 'mlvapp-b.log'
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ stalledAtAnnouncement = $stalledAtAnnouncement; quietBefore = $quietBefore; quietAfter = $quietAfter; known = $known } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["known"], "the fresh log was announced (its open never returns: the announcement is all the watch has)")
        self.assertGreaterEqual(out["quietBefore"], 1000, "the quiet clock had outrun the window")
        self.assertFalse(out["stalledAtAnnouncement"], "a log first seen mid-run is a sign of life at its announcement")
        self.assertLess(out["quietAfter"], 500, "the announcement restarted the quiet clock")

    def test_a_delayed_unchanged_first_read_of_a_mid_run_log_is_a_baseline_not_a_second_window(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'delayedbaseline'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-a.log') -Value ('x' * 100) -NoNewline
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-b.log') -Value ('y' * 100) -NoNewline
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 600000 -ProbeOpenDelayMs 1500 -ProbeWaitMs 300
$w.Files['mlvapp-a.log'] = [int64]100; $w.Armed = $true; $w.LastProgressMs = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp
$sw = [Diagnostics.Stopwatch]::StartNew(); $quietAtRead = -1
while ($sw.ElapsedMilliseconds -lt 15000 -and $quietAtRead -lt 0) {
    [void](Test-GuiSmokeLogProgressStalled -Watch $w)
    if ($w.Files.ContainsKey('mlvapp-b.log') -and [int64]$w.Files['mlvapp-b.log'] -ge 0) { $quietAtRead = [int]((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w.LaunchTimestamp) - $w.LastProgressMs) }
    Start-Sleep -Milliseconds 100
}
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ quietAtRead = $quietAtRead; baseline = [int64]$w.Files['mlvapp-b.log'] } | ConvertTo-Json -Compress
""")
        self.assertEqual(100, out["baseline"], "the first readable length of the fresh log is its baseline")
        # b is announced at the first poll and read about 3 s later (two 1.5 s opens: a then b); credited as growth, the read would restart the clock to about 0
        self.assertGreaterEqual(out["quietAtRead"], 2000, "the unchanged delayed read was granted a second quiet window")

    def test_the_watcher_has_no_announced_after_arming_state(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        body = code[code.index("function Test-GuiSmokeLogProgressStalled"):code.index("function Wait-GuiSmokeProcessBounded")]
        self.assertIsNone(re.search(r"(?<![\w.])-3(?!\d)", body), "a name announced mid-run is progress at its announcement; there is no -3 state")


def _runner_snippet(start_marker: str, end_marker: str) -> str:
    """The runner's OWN text between two markers, so a test runs the shipped wiring and not a copy of it."""
    source = RUNNER.read_text(encoding="utf-8")
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


# the runner's two hard-limit regions, executed verbatim: the derivation of the process budget, and the chunked wait + finalize (both sampling
# branches, the unarmed-watch path and the growing-log path share one bound)
_DERIVE_REGION = ("$effectiveProcessTimeoutMs = ", "\n\nif ($DryRun)")
_WAIT_REGION = ("$noProgressOverrideMs = if", "\n$stdout = $processBoundary.stdout")

_RUNNER_HARNESS = r"""
function Get-HostLoadSnapshot { param($TopProcessCount, $SubjectProcess, [switch]$SkipEvidenceCollection) [pscustomobject]@{ sample = 1 } }
$ProcessTimeoutMs = 3600000
# -2 r1 (adversary 81e7f8b2 A2): GS_OVERRIDE_MS is an explicit -NoProgressTimeoutMs, for a scaled leg whose window must fit before L after a late arming
$NoProgressTimeoutMs = if ($env:GS_OVERRIDE_MS) { [int]$env:GS_OVERRIDE_MS } else { 0 }
# -2 r1: GS_ALLOW_MS widens the hard deadline (H = 2000 + allowance) for a test that needs a scaled H with room for a late arming
$LegHardLimitAllowanceMs = if ($env:GS_ALLOW_MS) { [int]$env:GS_ALLOW_MS } else { 1000 }
$derivedProcessTimeoutMs = 2000
$HostLoadSampleIntervalMs = [int]$env:GS_SAMPLE_MS; $HostLoadTopProcessCount = 3
$hostLoadDuringSamples = [System.Collections.Generic.List[object]]::new()
$logRoot = Join-Path $env:GS_TMP 'legdeadline'; New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$env:GS_LOG = Join-Path $logRoot 'mlvapp-1.log'
# r11 fix 6: the runner's own monotonic launch stamp, read just before the process is started
$startUtc = [datetime]::UtcNow
$startTimestamp = [Diagnostics.Stopwatch]::GetTimestamp()
$s = New-Stub $env:GS_STUB
$process = $s.p; $stdoutTask = $s.o; $stderrTask = $s.e
. ([scriptblock]::Create($env:GS_DERIVE))
$hardLimitMs = [int64]$effectiveProcessTimeoutMs
# r11: GS_PREWAIT lets a stub finish BEFORE the wait region runs, the state a runner that was descheduled between its remaining-time read and its wait sees
if ($env:GS_PREWAIT) { [void]$process.WaitForExit(30000) }
# r16 (sol r15 B1): GS_SETUP_MS is runner setup time spent between the launch and the wait region (the app keeps running)
if ($env:GS_SETUP_MS) { Start-Sleep -Milliseconds ([int]$env:GS_SETUP_MS) }
$sw = [Diagnostics.Stopwatch]::StartNew()
. ([scriptblock]::Create($env:GS_WAIT))
$waitedMs = [int]$sw.ElapsedMilliseconds
$alive = -not $process.HasExited
if ($alive) { $process.Kill($true) }
[pscustomobject]@{ hardLimitMs = $hardLimitMs; waitedMs = $waitedMs; timedOut = [bool]$processBoundary.timedOut; outcome = [string]$processBoundary.outcome
    timeoutMs = [int]$processBoundary.timeoutMs; exitCode = [int]$processBoundary.exitCode; alive = $alive; samples = $hostLoadDuringSamples.Count
    loopEndMs = [int]$hostLoadSampleLoopElapsedMs; expiryMs = $noProgressWatch.ExpiryMs; armed = [bool]$noProgressWatch.Armed } | ConvertTo-Json -Compress
"""

_NEVER_EXITS_NO_LOG = "Start-Sleep -Seconds 20"
_NEVER_EXITS_GROWING_LOG = "1..40 | ForEach-Object { Add-Content -LiteralPath $env:GS_LOG -Value 'tick'; Start-Sleep -Milliseconds 500 }"


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class LegDerivedHardDeadline(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r6 (sol r5 B1; catalogue B-03): the GUI process has a hard deadline derived from its LEG (leg budget plus a
    stated setup/teardown allowance, never above the job's -ProcessTimeoutMs), so a short leg under a one-hour job timeout is ended at the leg's
    deadline whether its log never appeared (the watchdog is unarmed) or keeps growing (its quiet clock keeps restarting)."""

    def _run_runner_wiring(self, stub: str, sample_ms: int, prewait: bool = False) -> dict:
        extra = {"GS_PREWAIT": "1"} if prewait else {}
        return run_ps(_RUNNER_HARNESS, GS_STUB=stub, GS_SAMPLE_MS=str(sample_ms),
                      GS_DERIVE=_runner_snippet(*_DERIVE_REGION), GS_WAIT=_runner_snippet(*_WAIT_REGION), **extra)

    def _assert_ended_at_the_leg_deadline(self, out: dict) -> None:
        # the observed behaviour first: the stub never exits within 20 s, so only a leg-derived deadline can end it sooner
        self.assertLess(out["waitedMs"], 12000, "the process outlived the leg-derived deadline: the job timeout is still its only bound")
        self.assertTrue(out["timedOut"], "the process is ended at the leg-derived deadline, not left to the one-hour job timeout")
        self.assertFalse(out["alive"], "the owned tree is killed")
        self.assertEqual(124, out["exitCode"])
        self.assertEqual(3000, out["hardLimitMs"], "leg budget 2000 ms + 1000 ms allowance, under a 3600000 ms job timeout")
        self.assertEqual(3000, out["timeoutMs"], "the receipt names the leg-derived whole budget")
        # -2 r1 (adversary 81e7f8b2 A2): W = L = 1000 cannot run out by L once the watch has armed, so no silence was proven: the deadline decides
        # (a log that keeps growing every 500 ms is never a no-progress leg)
        self.assertEqual("timeout", out["outcome"], "a window that cannot run out by L made a silence verdict (expiry %s ms since launch)" % out["expiryMs"])

    def test_hard_limit_is_the_leg_plus_an_allowance_and_never_above_the_job_timeout(self) -> None:
        out = run_ps(r"""
[pscustomobject]@{
    withJobTimeout = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 155000 -JobTimeoutMs 3600000
    jobBelowLeg = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 155000 -JobTimeoutMs 200000
    noJobTimeout = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 155000 -JobTimeoutMs 0
    noJobTimeoutFloor = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 500 -JobTimeoutMs 0
    explicitAllowance = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 2000 -JobTimeoutMs 3600000 -AllowanceMs 1000
    safetyCeiling = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 3600000 -JobTimeoutMs 3600000
    tinyJob = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 155000 -JobTimeoutMs 500
} | ConvertTo-Json -Compress
""")
        self.assertEqual(155000 + 120000, out["withJobTimeout"], "the stated allowance is 120 s of app start-up and teardown")
        self.assertEqual(200000, out["jobBelowLeg"], "never above the job's own -ProcessTimeoutMs")
        self.assertEqual(155000, out["noJobTimeout"], "no -ProcessTimeoutMs: the runner's own derived leg budget, unchanged")
        self.assertEqual(1000, out["noJobTimeoutFloor"])
        self.assertEqual(3000, out["explicitAllowance"])
        self.assertEqual(3600000, out["safetyCeiling"])
        self.assertEqual(500, out["tinyJob"], "a job timeout below the leg is honoured as given")

    def test_runner_derives_its_process_budget_from_the_leg_and_exposes_the_allowance(self) -> None:
        derive = _runner_snippet(*_DERIVE_REGION)
        self.assertIn("Get-GuiSmokeLegHardLimitMs -LegBudgetMs $derivedProcessTimeoutMs -JobTimeoutMs $ProcessTimeoutMs -AllowanceMs $LegHardLimitAllowanceMs", derive)
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("[int]$LegHardLimitAllowanceMs = 0", source)
        self.assertNotIn("$effectiveProcessTimeoutMs = if ($ProcessTimeoutMs -gt 0)", source, "the job timeout is no longer the process deadline by itself")

    def test_a_short_leg_with_no_log_yet_is_ended_at_the_leg_deadline_without_host_load_sampling(self) -> None:
        self._assert_ended_at_the_leg_deadline(self._run_runner_wiring(_NEVER_EXITS_NO_LOG, 0))

    def test_a_short_leg_with_no_log_yet_is_ended_at_the_leg_deadline_with_host_load_sampling(self) -> None:
        out = self._run_runner_wiring(_NEVER_EXITS_NO_LOG, 500)
        self._assert_ended_at_the_leg_deadline(out)

    def test_a_short_leg_whose_log_keeps_growing_is_ended_at_the_leg_deadline_without_host_load_sampling(self) -> None:
        self._assert_ended_at_the_leg_deadline(self._run_runner_wiring(_NEVER_EXITS_GROWING_LOG, 0))

    def test_a_short_leg_whose_log_keeps_growing_is_ended_at_the_leg_deadline_with_host_load_sampling(self) -> None:
        out = self._run_runner_wiring(_NEVER_EXITS_GROWING_LOG, 500)
        self._assert_ended_at_the_leg_deadline(out)
        self.assertGreaterEqual(out["samples"], 1, "the sampling branch ran")

    def test_the_no_progress_window_can_never_outlast_the_leg_deadline(self) -> None:
        out = run_ps(r"""
$hard = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 24000 -JobTimeoutMs 3600000
[pscustomobject]@{ hard = $hard; window = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 24000 -OverrideMs 3600000 -CeilingMs $hard } | ConvertTo-Json -Compress
""")
        self.assertEqual(24000 + 120000, out["hard"])
        self.assertEqual(out["hard"], out["window"], "an explicit -NoProgressTimeoutMs of the whole job timeout is capped by the leg deadline")


# r6 (sol r5 A4 / H2, absorbing VENUE-SMOKE-KILL-PROSE-UNCONFIRMED-1): the failure text says termination was REQUESTED until the exit is confirmed.
_FAKE_PROCESS = r"""
class FakeProc : System.Diagnostics.Process {
    [bool]$KillThrows = $false
    [bool]$ExitsAfterKill = $false
    [bool]$Killed = $false
    [void]Kill([bool]$tree) { if ($this.KillThrows) { throw [System.InvalidOperationException]::new('denied C:\secret\path') }; $this.Killed = $true }
    [bool]WaitForExit([int]$ms) { return ($this.Killed -and $this.ExitsAfterKill) }
}
"""


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class TerminationProseIsOnlyAsStrongAsTheEvidence(unittest.TestCase):
    def _wait(self, kill_throws: str, exits_after_kill: str, no_progress: str) -> dict:
        return run_ps(_FAKE_PROCESS + r"""
$p = [FakeProc]::new(); $p.KillThrows = %s; $p.ExitsAfterKill = %s
$done = [System.Threading.Tasks.Task]::FromResult('')
$r = Wait-GuiSmokeProcessBounded -Process $p -StandardOutputTask $done -StandardErrorTask $done -TimeoutMs 50 -ReportedTimeoutMs 3000 -TerminationGraceMs 50 %s
[pscustomobject]@{ failures = @($r.failures); treeKillSucceeded = $r.treeKillSucceeded; terminationConfirmed = $r.terminationConfirmed; exitCode = $r.exitCode; outcome = $r.outcome } | ConvertTo-Json -Compress
""" % (kill_throws, exits_after_kill, no_progress))

    def test_a_failed_kill_and_unconfirmed_exit_never_claim_termination(self) -> None:
        for no_progress in ("-NoProgressStalled -NoProgressWindowMs 1500", ""):
            out = self._wait("$true", "$false", no_progress)
            text = " ".join(out["failures"])
            self.assertNotIn("was terminated", text, f"the kill threw and the exit was never seen (no_progress={bool(no_progress)})")
            self.assertIn("termination failed", text)
            self.assertIn("not confirmed terminated", text)
            self.assertFalse(out["terminationConfirmed"])
            self.assertFalse(out["treeKillSucceeded"])
            self.assertEqual(124, out["exitCode"])

    def test_runner_claims_termination_only_when_the_boundary_confirmed_the_exit(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        start = source.index("if ($processBoundary.timedOut) {")
        block = source[start:source.index("if ($processExitCode -ne 0) {", start)]
        self.assertIn("$processBoundary.terminationConfirmed", block)
        self.assertLess(block.index("$processBoundary.terminationConfirmed"), block.index('"was terminated."'))
        self.assertIn("its termination was not confirmed.", block)

    def test_no_progress_prose_says_requested_until_the_exit_is_confirmed(self) -> None:
        unconfirmed = " ".join(self._wait("$false", "$false", "-NoProgressStalled -NoProgressWindowMs 1500")["failures"])
        self.assertIn("process_no_progress", unconfirmed)
        self.assertIn("termination was requested", unconfirmed)
        self.assertNotIn("was terminated", unconfirmed)
        self.assertIn("not confirmed terminated", unconfirmed)
        confirmed = self._wait("$false", "$true", "-NoProgressStalled -NoProgressWindowMs 1500")
        self.assertTrue(confirmed["terminationConfirmed"])
        self.assertIn("termination was confirmed", " ".join(confirmed["failures"]), "the confirmation is its own statement, made only once the exit is seen")
        self.assertEqual("process_no_progress", confirmed["outcome"])


GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
BUDGET_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class OwnerInputLoadingAllowanceSurvivesTheLegDeadline(unittest.TestCase):
    """r7 (sol r6 B-04): the leg-derived hard deadline (leg budget + 120 s) discarded the size-derived loading allowance an owner-input leg is accepted with.

    Get-AttrCudaLegTimeBudget accepts a 24 GiB input at 32 MB/s with a 1152 s app read allowance (smokeProcessTimeoutMs 1285000), but the job forwarded only
    -ProcessTimeoutMs, so the runner's hard limit came out at its derived 187000 ms + 120000 ms = 307000 ms and a healthy load finishing at 400 s was killed.
    The generator now forwards -LegHardLimitAllowanceMs (setup/teardown 120 s + the app read allowance) when the runner committed at -SourceCommit declares it."""

    _BUDGET = r"""
Import-Module $env:GS_BUDGET -Force
$b = Get-AttrCudaLegTimeBudget -InputBytes 25769803776 -ColdReadMBps 32 -ReadMargin 1.5 -PlaySeconds 40
$legBudgetMs = 187000   # the runner's own derived leg budget for a 40 s play (sol r6 repro)
"""

    def test_a_24_gib_owner_input_keeps_its_accepted_read_allowance_in_the_hard_deadline(self) -> None:
        out = run_ps(self._BUDGET + r"""
$allow = Get-AttrCudaLegHardLimitAllowanceMs -AppReadAllowanceSec $b.appReadAllowanceSec
$limit = Get-GuiSmokeLegHardLimitMs -LegBudgetMs $legBudgetMs -JobTimeoutMs $b.smokeProcessTimeoutMs -AllowanceMs $allow
[pscustomobject]@{ read = $b.appReadAllowanceSec; job = $b.smokeProcessTimeoutMs; allow = $allow; limit = $limit
  today = Get-GuiSmokeLegHardLimitMs -LegBudgetMs $legBudgetMs -JobTimeoutMs $b.smokeProcessTimeoutMs } | ConvertTo-Json -Compress
""", GS_BUDGET=str(BUDGET_MODULE))
        self.assertEqual(1152, out["read"])
        self.assertEqual(307000, out["today"], "without the allowance the runner derives 307 s: the defect sol reproduced")
        self.assertGreaterEqual(out["limit"], 400000, "a healthy load that completes at 400 s is inside the accepted read allowance")
        self.assertGreaterEqual(out["limit"], 1152000, "the whole accepted app read allowance is kept")
        self.assertEqual(out["job"], out["limit"], "and the deadline still never exceeds the job's own process timeout")
        self.assertEqual(120000 + 1152000, out["allow"], "120 s setup/teardown plus the read allowance")

    def test_a_fixture_leg_and_every_ceiling_clamped_leg_stay_bounded(self) -> None:
        out = run_ps(r"""
Import-Module $env:GS_BUDGET -Force
$fx = Get-AttrCudaLegTimeBudget -InputBytes 0 -PlaySeconds 40
$clamp = Get-AttrCudaLegTimeBudget -InputBytes 314572800000 -ColdReadMBps 100 -ReadMargin 1.5 -PlaySeconds 40 -ShareSmokeCeiling
$huge = Get-AttrCudaLegHardLimitAllowanceMs -AppReadAllowanceSec 7200
[pscustomobject]@{
  fixtureAllow = Get-AttrCudaLegHardLimitAllowanceMs -AppReadAllowanceSec $fx.appReadAllowanceSec
  fixtureArgs = Get-AttrCudaLegHardLimitArgs -RunnerAcceptsAllowance $true -AppReadAllowanceSec $fx.appReadAllowanceSec
  clampRead = $clamp.appReadAllowanceSec; clampJob = $clamp.smokeProcessTimeoutMs
  clampLimit = Get-GuiSmokeLegHardLimitMs -LegBudgetMs 187000 -JobTimeoutMs $clamp.smokeProcessTimeoutMs -AllowanceMs (Get-AttrCudaLegHardLimitAllowanceMs -AppReadAllowanceSec $clamp.appReadAllowanceSec)
  huge = $huge
} | ConvertTo-Json -Compress
""", GS_BUDGET=str(BUDGET_MODULE))
        self.assertEqual(0, out["fixtureAllow"], "no input to read: the runner's own 120 s default stands")
        self.assertEqual("", out["fixtureArgs"], "a fixture leg's emitted job text is unchanged (byte identity)")
        self.assertEqual(3467, out["clampRead"], "300000 MB at 100 MB/s x1.5 = 4500 s is over the ceiling, so the read allowance is the ceiling's remainder")
        self.assertEqual(3600000, out["clampJob"])
        self.assertLessEqual(out["clampLimit"], 3600000, "never above the 3600000 ms safety ceiling")
        self.assertLessEqual(out["huge"], 3600000, "the allowance respects the runner's -LegHardLimitAllowanceMs range")

    def test_the_forwarded_argument_is_emitted_only_for_a_runner_that_declares_it(self) -> None:
        out = run_ps(r"""
Import-Module $env:GS_BUDGET -Force
[pscustomobject]@{
  declared = Get-AttrCudaLegHardLimitArgs -RunnerAcceptsAllowance $true -AppReadAllowanceSec 1152
  undeclared = Get-AttrCudaLegHardLimitArgs -RunnerAcceptsAllowance $false -AppReadAllowanceSec 1152
  noRead = Get-AttrCudaLegHardLimitArgs -RunnerAcceptsAllowance $true -AppReadAllowanceSec 0
} | ConvertTo-Json -Compress
""", GS_BUDGET=str(BUDGET_MODULE))
        self.assertEqual(" -LegHardLimitAllowanceMs 1272000", out["declared"])
        self.assertEqual("", out["undeclared"], "a runner committed before r6 would reject the parameter and the leg would die at launch")
        self.assertEqual("", out["noRead"])

    def test_the_setup_allowance_the_generator_adds_is_the_runners_own_default(self) -> None:
        module_text = MODULE.read_text(encoding="utf-8")
        budget_text = BUDGET_MODULE.read_text(encoding="utf-8")
        runner_default = re.search(r"\$allowance = if \(\$AllowanceMs -gt 0\) \{ \$AllowanceMs \} else \{ (\d+) \}", module_text)
        generator_default = re.search(r"\[int64\]\$SetupTeardownAllowanceMs = (\d+)", budget_text)
        self.assertTrue(runner_default and generator_default)
        self.assertEqual(runner_default.group(1), generator_default.group(1), "one setup/teardown allowance, not two drifting numbers")

    def test_the_generator_forwards_it_gated_on_the_committed_runner_and_expands_to_nothing_otherwise(self) -> None:
        text = GENERATOR.read_text(encoding="utf-8")
        self.assertRegex(text, r"\$runnerAcceptsLegHardLimitAllowance = \(\$LASTEXITCODE -eq 0\) -and\s*\(\$smokeRunnerCommittedText -match '\\\[int\\\]\\\$LegHardLimitAllowanceMs\\b'\)",
                         "decided from the runner bytes committed at -SourceCommit, like the verified-clip-binding probe")
        self.assertIn("-ProcessTimeoutMs $SmokeProcessTimeoutMs__LEG_HARD_LIMIT_ARGS__ -ScaleFactor __SCALE_FACTOR__", text,
                      "the placeholder sits directly after the timeout so an empty expansion leaves the job text byte-identical")
        self.assertRegex(text, r"LEG_HARD_LIMIT_ARGS = Get-AttrCudaLegHardLimitArgs -RunnerAcceptsAllowance \$runnerAcceptsLegHardLimitAllowance -AppReadAllowanceSec \$timeBudget\.appReadAllowanceSec")
        self.assertIn("Get-AttrCudaLegHardLimitAllowanceMs", BUDGET_MODULE.read_text(encoding="utf-8"))
        exports = BUDGET_MODULE.read_text(encoding="utf-8").split("Export-ModuleMember", 1)[1]
        for name in ("Get-AttrCudaLegHardLimitAllowanceMs", "Get-AttrCudaLegHardLimitArgs"):
            self.assertIn(name, exports)


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class OwnerInputLegSilenceWindowKeepsTheReadAllowance(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r9 (adversary 38406862 A1, major): r8 made the HARD deadline of an owner-input leg keep its accepted app read
    allowance, but the SILENCE window (leg budget + 60 s, armed at the app's first log line) still did not: a healthy synchronous load of the owner clip
    that writes no log line for longer than that window was killed at about 247 s despite a 1285000 ms hard deadline. Class: a budget the runner accepts
    for one wait is dropped by a sibling wait (the three Test-GuiSmokeLogProgressStalled call sites all read the one window)."""

    def test_helper_widens_the_window_by_the_read_allowance_and_the_hard_deadline_still_caps_it(self) -> None:
        out = run_ps(r"""
[pscustomobject]@{
    none = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 187000 -CeilingMs 1285000
    widened = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 187000 -ReadAllowanceMs 600000 -CeilingMs 1285000
    capped = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 187000 -ReadAllowanceMs 1152000 -CeilingMs 1285000
    overrideWins = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 187000 -ReadAllowanceMs 600000 -OverrideMs 90000 -CeilingMs 1285000
} | ConvertTo-Json -Compress
""")
        self.assertEqual(187000 + 60000, out["none"], "no read allowance: the window is unchanged")
        self.assertEqual(187000 + 60000 + 600000, out["widened"])
        self.assertEqual(1285000, out["capped"], "the window never outlasts the hard deadline")
        self.assertEqual(90000, out["overrideWins"], "an explicit -NoProgressTimeoutMs still replaces the derived window")

    def _runner_window(self, allowance_ms: int) -> int:
        # r16 (fable key3 r15 B1): the read allowance now lives in the LOAD window (the app's first silent stretch after arming), which is what this reads
        out = run_ps(
            "$NoProgressTimeoutMs = 0; $LegHardLimitAllowanceMs = " + str(allowance_ms) + "; $derivedProcessTimeoutMs = 187000; $effectiveProcessTimeoutMs = 1285000\n"
            "$logRoot = $env:GS_TMP\n" + _runner_snippet("$noProgressOverrideMs = if", "$noProgressWatch = New-GuiSmokeLogProgressWatch") +
            "\n[pscustomobject]@{ window = [int64]$noProgressLoadWindowMs } | ConvertTo-Json -Compress")
        return int(out["window"])

    def test_the_runner_hands_an_accepted_owner_read_allowance_to_the_silence_window(self) -> None:
        # the adversary's input: runner -LegHardLimitAllowanceMs 1272000 (120 s setup/teardown + 1152 s read) under a 1285000 ms hard deadline.
        # r16 (fable key3 r15 B1): the window keeps the whole read allowance the deadline leaves room for, and sits BELOW the deadline (it equalled it).
        # -2 r1: the cap is L = H - M, M = probe wait 1000 + 1000 (r16 pinned H - 1000)
        self.assertEqual(1285000 - 2000, self._runner_window(1272000), "the silence watchdog killed a healthy load at 247 s: it did not keep the read allowance")
        self.assertGreater(self._runner_window(1272000), 187000 + 60000 + 600000, "a 400 s silent load is still inside the load window")

    def test_a_leg_with_no_declared_allowance_keeps_its_leg_budget_window(self) -> None:
        self.assertEqual(187000 + 60000, self._runner_window(0))
        self.assertEqual(187000 + 60000, self._runner_window(120000), "the 120 s setup/teardown default is not a read allowance")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class DeadlineReachedWaitIsATimeoutEvenWhenTheProcessExitsInTheLastLook(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r10 (adversary 412ce3e0 A1, major, introduced): the chunked wait ends when the leg-derived hard deadline is
    exhausted with the process still running, but the finalize wait that follows keeps a 1 ms floor, so a process that exits inside that look (or
    between the last chunk and the check) was reported outcome=exited exitCode=0 timedOut=false. The base classified an unsuccessful wait at the
    deadline as a timeout. Class: a budget exhaustion is observed in one place and forgotten in the next (both sampling branches share it)."""

    def _wait(self, switch: str) -> dict:
        return run_ps(r"""
$s = New-Stub 'exit 0'
[void]$s.p.WaitForExit(30000)
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1 -ReportedTimeoutMs 3000 %s
[pscustomobject]@{ timedOut = $r.timedOut; outcome = $r.outcome; exitCode = $r.exitCode; failures = @($r.failures) } | ConvertTo-Json -Compress
""" % switch)

    def test_a_process_that_exits_in_the_last_look_after_the_deadline_is_still_a_timeout(self) -> None:
        out = self._wait("-DeadlineReached")
        self.assertTrue(out["timedOut"])
        self.assertEqual("timeout", out["outcome"])
        self.assertEqual(124, out["exitCode"], "an exit after the deadline is not a successful completion")
        self.assertIn("3000 ms", " ".join(out["failures"]), "the receipt names the whole budget")

    def test_without_the_deadline_flag_an_exited_process_is_still_a_clean_exit(self) -> None:
        out = self._wait("")
        self.assertFalse(out["timedOut"])
        self.assertEqual("exited", out["outcome"])
        self.assertEqual(0, out["exitCode"])

    def test_both_runner_wait_branches_record_the_exhausted_budget_and_hand_it_to_the_boundary(self) -> None:
        wait = _runner_snippet(*_WAIT_REGION)
        record = "$waitBudgetExhausted = Test-GuiSmokeWaitBudgetExhausted -Process $process -BudgetMs $effectiveProcessTimeoutMs -LaunchTimestamp $startTimestamp"
        # r15: the two expiry sites plus the three stall-verdict sites (AStallVerdictPastTheWholeBudgetIsATimeoutNotNoProgress pins those three by name)
        self.assertEqual(5, wait.count(record), "both branches record it at the expiry of the wait and at each stall verdict, from the process's own lifetime (r11 fix 6: bounded by the monotonic launch stamp)")
        self.assertNotIn("$waitBudgetExhausted = $true", wait, "elapsed time alone never says the app was still running at the deadline (r10 sol A1, 3534e113)")
        self.assertIn("-DeadlineReached:$waitBudgetExhausted", wait)
        # -2 r1: the expiry of the wait is the chunk helper's 0 (D reached), read at the wait
        for marker in ("if ($hostLoadChunkMs -le 0) {", "if ($noProgressChunkWaitMs -le 0) {"):
            start = wait.index(marker)
            self.assertIn(record, wait[start:wait.index("break", start)], marker)

    def _exhausted(self, command: str, budget_ms: int, wait_ms: int, alive_at_ms: int = -1) -> bool:
        # r16 (sol r15 B2): the call takes what the runner has, the monotonic launch stamp and its last monotonic sighting of the app running (-LastAliveMs);
        # alive_at_ms >= 0 takes that sighting at that time since launch, as the runner's chunk loop does (read the clock, then see the app still running)
        out = run_ps(r"""
$launch = [Diagnostics.Stopwatch]::GetTimestamp()
$s = New-Stub '%s'
$lastAlive = [double]0
if (%d -ge 0) {
    while ((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch) -lt %d) { Start-Sleep -Milliseconds 20 }
    $t = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch
    if (-not $s.p.HasExited) { $lastAlive = $t }
}
[void]$s.p.WaitForExit(%d)
$e = Test-GuiSmokeWaitBudgetExhausted -Process $s.p -BudgetMs %d -LaunchTimestamp $launch -LastAliveMs $lastAlive
if (-not $s.p.HasExited) { $s.p.Kill($true) }
[pscustomobject]@{ exhausted = [bool]$e; lastAlive = $lastAlive } | ConvertTo-Json -Compress
""" % (command, alive_at_ms, alive_at_ms, wait_ms, budget_ms))
        return bool(out["exhausted"])

    def test_a_process_that_exited_within_its_budget_is_never_a_deadline_hit(self) -> None:
        # the adversary's input: the chunked wait saw the app alive, a stalled probe then spent the rest of the budget, and the app exited 7 inside it
        self.assertFalse(self._exhausted("exit 7", 600000, 30000))

    def test_a_process_still_running_when_the_budget_ends_is_a_deadline_hit(self) -> None:
        self.assertTrue(self._exhausted("Start-Sleep -Seconds 20", 600000, 300), "an app still alive at the check: the budget ran out under it")

    def test_a_process_that_outlived_its_budget_before_exiting_is_a_deadline_hit(self) -> None:
        # r16: "outlived" is a monotonic fact (seen running at 900 ms of a 500 ms budget), no longer a wall lifetime
        self.assertTrue(self._exhausted("Start-Sleep -Seconds 3", 500, 30000, alive_at_ms=900), "the exit came after the deadline: not a completion")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class ProcessLifetimeIsMeasuredInUtcNotLocalClockTime(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r11 (adversary f24a2aed A1, major, introduced): Process.StartTime and Process.ExitTime are local times, so the
    lifetime that decides "did the app exit inside its budget" was wrong by the whole offset change when a daylight-saving step fell inside the process's
    life: an app that ran 700 ms across spring-forward measured 3600700 ms (a timeout, exit 124), one that ran 2000 ms across fall-back measured
    -3598000 ms. Class: a duration taken as the difference of two local wall-clock readings."""

    def test_the_lifetime_of_a_process_that_straddles_a_local_offset_change_is_its_real_duration(self) -> None:
        out = run_ps(r"""
$tz = [TimeZoneInfo]::Local
$rows = @()
$t = [datetime]::new(2026, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
$end = [datetime]::new(2027, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
while ($t -lt $end) {
    $n = $t.AddHours(1)
    if ($tz.GetUtcOffset($t) -ne $tz.GetUtcOffset($n)) {
        # the process starts 300 ms before the step and exits 700 ms after it: a real life of 1000 ms
        $start = $n.AddMilliseconds(-300).ToLocalTime(); $exit = $n.AddMilliseconds(700).ToLocalTime()
        $rows += [pscustomobject]@{ real = (Get-GuiSmokeProcessLifetimeMs -StartTime $start -ExitTime $exit); naive = ($exit - $start).TotalMilliseconds }
    }
    $t = $n
}
[pscustomobject]@{ rows = @($rows) } | ConvertTo-Json -Compress -Depth 4
""")
        rows = out["rows"]
        if not any(abs(row["naive"] - 1000) > 1 for row in rows):
            self.skipTest("this host's time zone has no offset change in 2026, so a local-time subtraction cannot be wrong here")
        for row in rows:
            self.assertAlmostEqual(1000.0, row["real"], delta=1, msg="the lifetime followed the local offset change (a 1000 ms life read as %s ms)" % row["naive"])

    def test_a_wall_clock_step_inside_the_life_never_decides_an_exit_that_the_monotonic_clock_bounds(self) -> None:
        # r11 fix 6 (adversary 34a08338 A2): UTC does not help against a clock STEP. r16 (sol r15 B2): no wall reading is an input at all any more; the
        # exit is bracketed by two monotonic facts (last seen running, exit seen by). The r11 inputs, restated as the facts the runner actually has:
        out = run_ps(r"""
function V($budget, $alive, $seen) { [string](Get-GuiSmokeExitBudgetVerdict -BudgetMs $budget -LastAliveMs $alive -ExitSeenByMs $seen) }
[pscustomobject]@{
    forwardStepInBudget = V 1000 0 700              # the adversary's input: +60 s step, a 700 ms life seen by 700 ms: inside, whatever the wall said
    backwardStepOverSeenAlive = V 1000 1500 2000    # a real 2000 ms life, seen running at 1500 ms of a 1000 ms budget: late, whatever the wall said
    observedAfterBudgetOnly = V 1000 0 1200         # seen only after the budget, never seen running past it: cannot be told
    noLaunchStamp = V 1000 0 -1
    seenAliveAtTheBudget = V 1000 1000 1200
    seenExactlyAtTheBudget = V 1000 0 1000
} | ConvertTo-Json -Compress
""")
        self.assertEqual("in_budget", out["forwardStepInBudget"], "a 700 ms life was read as 60700 ms and became a timeout")
        self.assertEqual("late", out["backwardStepOverSeenAlive"], "a 2000 ms life against a 1000 ms budget passed")
        self.assertEqual("uncertain", out["observedAfterBudgetOnly"], "r16: an exit the monotonic clock cannot place is neither clean nor late")
        self.assertEqual("uncertain", out["noLaunchStamp"], "r16: with no monotonic stamp only a wall reading could judge, and none does")
        self.assertEqual("late", out["seenAliveAtTheBudget"])
        self.assertEqual("in_budget", out["seenExactlyAtTheBudget"])

    def test_the_budget_check_takes_the_lifetime_from_the_utc_helper(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\.ExitTime\s*-\s*\$\w+\.StartTime", "no bare local-time subtraction of a process's start and exit")
        # the UTC helper stays exported and correct for any reader; r16 (sol r15 B2): no verdict reads a process's wall start or exit time at all
        self.assertIn("$ExitTime.ToUniversalTime() - $StartTime.ToUniversalTime()", code)
        self.assertRegex(text.split("Export-ModuleMember", 1)[1], r"Get-GuiSmokeProcessLifetimeMs")
        judging = code[code.index("function Get-GuiSmokeExitBudgetVerdict"):]
        self.assertNotIn(".ExitTime", judging)
        self.assertNotIn(".StartTime", judging)


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class AnExitSeenByTheWaitItselfAfterTheDeadlineIsATimeout(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r11 (adversary f24a2aed A2, major, introduced): r10 judged the deadline only on the branch where the remaining
    time reached zero. A runner descheduled between its remaining-time read (100 ms left) and its WaitForExit(100) saw WaitForExit return true on an app
    that exited at 1100 ms of a 1000 ms budget, broke without the lifetime check and reported outcome=exited exitCode=0 timedOut=false. The tests below
    reproduce that state by letting the stub finish before the wait region runs. Class: a budget exhaustion judged on one wait branch and forgotten on its
    sibling (both sampling branches, each with two exits)."""

    # the stub lives 3.3 s plus its own start-up against a 3000 ms leg-derived deadline (2000 ms leg + 1000 ms allowance)
    _LATE_EXIT = "Start-Sleep -Milliseconds 3300; exit 0"

    def _run_runner_wiring(self, stub: str, sample_ms: int, prewait: bool = False) -> dict:
        extra = {"GS_PREWAIT": "1"} if prewait else {}
        return run_ps(_RUNNER_HARNESS, GS_STUB=stub, GS_SAMPLE_MS=str(sample_ms),
                      GS_DERIVE=_runner_snippet(*_DERIVE_REGION), GS_WAIT=_runner_snippet(*_WAIT_REGION), **extra)

    def _late(self, sample_ms: int) -> dict:
        return self._run_runner_wiring(self._LATE_EXIT, sample_ms, prewait=True)

    def _assert_late_exit_is_never_clean(self, out: dict) -> None:
        # r16 (sol r15 B2): the harness let the stub finish before the wait region ran, so the runner never saw it running: the monotonic clock knows only
        # that it exited somewhere between its launch and a moment past the budget. That is reported as uncertain (r11-r15 called it a timeout from the WALL
        # lifetime, the clock sol showed a step can move); it is still never a clean exit.
        self.assertNotEqual("exited", out["outcome"], "an app that lived longer than the leg-derived deadline was reported as a clean exit")
        self.assertNotEqual(0, out["exitCode"])
        self.assertEqual("exit_time_uncertain", out["outcome"])
        self.assertEqual(125, out["exitCode"])
        self.assertEqual(3000, out["timeoutMs"], "the receipt names the whole budget")

    def test_a_late_exit_seen_by_the_wait_is_a_timeout_in_the_log_only_branch(self) -> None:
        self._assert_late_exit_is_never_clean(self._late(0))

    def test_a_late_exit_seen_by_the_wait_is_a_timeout_in_the_host_load_branch(self) -> None:
        self._assert_late_exit_is_never_clean(self._late(500))

    def _boundary(self, stub: str, budget_ms: int, stalled: bool, alive_at_ms: int = -1) -> dict:
        # the boundary call exactly as the runner makes it after a stall verdict or any other break: -LifetimeBudgetMs, the launch stamp and (r16) the
        # runner's last monotonic sighting of the app running, taken at alive_at_ms when it is >= 0
        return run_ps(r"""
$launch = [Diagnostics.Stopwatch]::GetTimestamp()
$s = New-Stub '%s'
$lastAlive = [double]0
if (%d -ge 0) {
    while ((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch) -lt %d) { Start-Sleep -Milliseconds 20 }
    $t = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch
    if (-not $s.p.HasExited) { $lastAlive = $t } else { throw 'the stub exited before its sighting' }
}
[void]$s.p.WaitForExit(30000)
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1 -ReportedTimeoutMs %d -LifetimeBudgetMs %d -LaunchTimestamp $launch -LastAliveMs $lastAlive %s
[pscustomobject]@{ timedOut = $r.timedOut; outcome = $r.outcome; exitCode = $r.exitCode } | ConvertTo-Json -Compress
""" % (stub, alive_at_ms, alive_at_ms, budget_ms, budget_ms, "-NoProgressStalled" if stalled else ""))

    def test_a_stall_verdict_followed_by_an_exit_after_the_budget_is_a_timeout(self) -> None:
        # r11 fix 6 (adversary 34a08338 A1, major, introduced): the stall break left the deadline flag false, so the 1 ms look saw an app that had already
        # outlived the budget exit 0 and reported outcome=exited. r16: "outlived" is the runner's monotonic sighting of it running at 300 ms of a 100 ms budget.
        out = self._boundary("Start-Sleep -Milliseconds 1500; exit 0", 100, stalled=True, alive_at_ms=300)
        self.assertTrue(out["timedOut"], "an app that exited after the whole budget was a clean exit behind a stall verdict")
        self.assertEqual("timeout", out["outcome"])
        self.assertEqual(124, out["exitCode"])

    def test_a_stall_verdict_followed_by_an_exit_inside_the_budget_keeps_the_native_exit(self) -> None:
        out = self._boundary("exit 7", 600000, stalled=True)
        self.assertFalse(out["timedOut"])
        self.assertEqual("exited", out["outcome"])
        self.assertEqual(7, out["exitCode"])

    def test_an_unjudged_call_keeps_the_native_exit(self) -> None:
        # a caller that passes no -LifetimeBudgetMs (0) is not judged: every other caller of the module is unchanged
        out = run_ps(r"""
$s = New-Stub 'exit 5'
[void]$s.p.WaitForExit(30000)
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1
[pscustomobject]@{ timedOut = $r.timedOut; exitCode = $r.exitCode } | ConvertTo-Json -Compress
""")
        self.assertFalse(out["timedOut"])
        self.assertEqual(5, out["exitCode"])

    def test_the_runner_hands_the_whole_budget_and_the_launch_stamp_to_the_boundary(self) -> None:
        wait = _runner_snippet(*_WAIT_REGION)
        call = wait[wait.index("$processBoundary = Wait-GuiSmokeProcessBounded"):]
        self.assertIn("-LifetimeBudgetMs $effectiveProcessTimeoutMs", call)
        self.assertIn("-LaunchTimestamp $startTimestamp", call)
        source = RUNNER.read_text(encoding="utf-8")
        self.assertLess(source.index("$startTimestamp = [System.Diagnostics.Stopwatch]::GetTimestamp()"), source.index("$process = [System.Diagnostics.Process]::Start($startInfo)"),
                        "the stamp is read BEFORE the launch, so the time since it bounds the life from above")

    def test_an_exit_inside_the_budget_keeps_its_native_exit_in_both_branches(self) -> None:
        for sample_ms in (0, 500):
            out = self._run_runner_wiring("exit 0", sample_ms, prewait=True)
            self.assertFalse(out["timedOut"], "sample_ms=%d: an app that exited inside its budget is not a timeout" % sample_ms)
            self.assertEqual("exited", out["outcome"])
            self.assertEqual(0, out["exitCode"])


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class AnOverrunRecognizedAfterAStallVerdictCarriesTheTimeoutReasonNotTheStallOne(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r14 (adversary 9af35de9 A2, major, introduced): once the boundary recognizes that a confirmed exit came after the
    whole budget (or the caller already said the deadline was reached) the leg is a timeout, but the stall flag the caller passed was still read afterwards,
    so the typed outcome stayed process_no_progress with a prose "ended before the whole budget" line for an app that outlived the budget (exit 124 was
    right, the reason was not). Class: a stale flag read after a state transition."""

    def _call(self, stub: str, switches: str, budget_args: str) -> dict:
        # r16 (sol r15 B2): an overrun is a monotonic fact, the app seen running past the budget; budget_args may carry -LastAliveMs $alive, taken at 300 ms
        return run_ps(r"""
$launch = [Diagnostics.Stopwatch]::GetTimestamp()
$s = New-Stub '%s'
while ((Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch) -lt 300) { Start-Sleep -Milliseconds 20 }
$t = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch
$alive = if (-not $s.p.HasExited) { $t } else { [double]0 }
# 29 s, not 30 s: the 30 s sleeper must still be running after the 300 ms sighting above
[void]$s.p.WaitForExit(29000)
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1 -ReportedTimeoutMs 3000 -NoProgressWindowMs 1000 -LaunchTimestamp $launch %s %s
[pscustomobject]@{ timedOut = $r.timedOut; outcome = $r.outcome; noProgress = $r.noProgress; window = $r.noProgressWindowMs; exitCode = $r.exitCode; failures = @($r.failures) } | ConvertTo-Json -Compress
""" % (stub, switches, budget_args))

    def _assert_plain_timeout(self, out: dict) -> None:
        self.assertTrue(out["timedOut"])
        self.assertEqual(124, out["exitCode"])
        self.assertEqual("timeout", out["outcome"], "an overrun is a timeout, whatever the caller's stall flag said before the look")
        self.assertFalse(out["noProgress"])
        self.assertIsNone(out["window"], "no stall window is reported for a leg that was not ended by a stall")
        text = " ".join(out["failures"])
        self.assertNotIn("process_no_progress", text)
        self.assertIn("exceeded the fail-closed process timeout of 3000 ms", text)

    def test_a_stall_verdict_followed_by_an_exit_after_the_budget_is_typed_as_a_timeout(self) -> None:
        # seen running at 300 ms of a 100 ms budget, then exits 0
        self._assert_plain_timeout(self._call("Start-Sleep -Milliseconds 1500; exit 0", "-NoProgressStalled", "-LifetimeBudgetMs 100 -LastAliveMs $alive"))

    def test_a_stall_flag_together_with_the_reached_deadline_is_typed_as_a_timeout(self) -> None:
        self._assert_plain_timeout(self._call("exit 0", "-NoProgressStalled -DeadlineReached", ""))

    def test_a_stall_verdict_over_a_process_still_running_is_still_process_no_progress(self) -> None:
        out = self._call("Start-Sleep -Seconds 30", "-NoProgressStalled", "-LifetimeBudgetMs 600000")
        self.assertTrue(out["timedOut"])
        self.assertEqual("process_no_progress", out["outcome"])
        self.assertTrue(out["noProgress"])
        self.assertEqual(1000, out["window"])
        self.assertEqual(124, out["exitCode"])

    def test_the_timeout_read_of_the_stall_flag_happens_after_the_overrun_is_decided(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("$noProgress = [bool]$NoProgressStalled\n", code, "the stall flag is combined with the overrun verdict, never copied alone")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class ALateExitIsJudgedFromTheLaunchInstantNotFromAStartTimeReadAfterTheExit(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r15 (hosted CI on 1477a490, ubuntu only; five late-exit tests of this file failed there and passed on Windows).

    Cause, from the code: Test-GuiSmokeWaitBudgetExhausted read Process.StartTime of a process that had ALREADY EXITED. On Windows that is a handle query and
    still answers. On Linux .NET derives StartTime from /proc/<pid>/stat, which is gone once the child is reaped, so the read throws; the function's catch
    (gui-smoke-process-boundary.psm1, `catch { return $false }`) turned the throw into "not exhausted", so every late exit read as a clean one -- exactly the
    five tests whose stub outlived its budget (the in-budget tests expect false and passed on both). The lifetime is now measured from the wall instant the
    runner reads just before Process.Start ($startUtc), which exists on every OS, so no read of a dead process's start time decides a verdict.
    Class: a clock read taken after the thing it describes is gone."""

    def test_the_launch_instant_decides_the_lifetime_whatever_the_process_start_time_would_read(self) -> None:
        # OS-independent proof (r16, sol r15 B2): the verdict is taken from the monotonic launch stamp and the caller's monotonic sighting only, so it is the
        # same on every OS and no read of a dead child's clock (StartTime throws on Linux once it is reaped) can turn a late exit into a clean one
        out = run_ps(r"""
$hz = [Diagnostics.Stopwatch]::Frequency
$s = New-Stub 'exit 0'
[void]$s.p.WaitForExit(30000)
$anHourAgo = [Diagnostics.Stopwatch]::GetTimestamp() - [int64](3600 * $hz)
[pscustomobject]@{
    seenRunningPastTheBudget = [bool](Test-GuiSmokeWaitBudgetExhausted -Process $s.p -BudgetMs 600000 -LaunchTimestamp $anHourAgo -LastAliveMs 3000000)
    justNow = [bool](Test-GuiSmokeWaitBudgetExhausted -Process $s.p -BudgetMs 600000 -LaunchTimestamp ([Diagnostics.Stopwatch]::GetTimestamp()))
} | ConvertTo-Json -Compress
""")
        self.assertTrue(out["seenRunningPastTheBudget"], "the app was seen running past its budget on the monotonic clock: that is a deadline hit")
        self.assertFalse(out["justNow"], "an exit inside the budget is not a deadline hit")

    def test_the_module_judges_an_exited_process_without_reading_its_start_time_when_a_launch_instant_is_given(self) -> None:
        # r16 (sol r15 B2): stronger than r15: no judging code reads any wall clock of the process, whatever the caller supplies
        text = MODULE.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        body = code[code.index("function Test-GuiSmokeWaitBudgetExhausted"):code.index("function Wait-GuiSmokeProcessBounded")]
        for wall in (".StartTime", ".ExitTime", "LaunchUtc", "UtcNow"):
            self.assertNotIn(wall, body)
        self.assertIn("Get-GuiSmokeExitBudgetVerdict -BudgetMs $BudgetMs -LastAliveMs $LastAliveMs -ExitSeenByMs $seenByMs", body)
        wait = code[code.index("function Wait-GuiSmokeProcessBounded"):]
        self.assertNotIn("LaunchUtc", wait)
        # -2 r1: the boundary's local bound is $exitBoundMs (PowerShell names are case-insensitive, so it cannot share the -ExitSeenByMs parameter's name)
        self.assertIn("Get-GuiSmokeExitBudgetVerdict -BudgetMs $LifetimeBudgetMs -LastAliveMs $LastAliveMs -ExitSeenByMs $exitBoundMs", wait)

    def test_the_runner_reads_the_launch_instant_before_the_launch_and_hands_it_to_every_judging_call(self) -> None:
        # the monotonic stamp is read before Process.Start (the time since it bounds the life from above); $startUtc stays the receipt's wall stamp, read before
        # it, and (r16) is handed to no judging call. Every judging call takes the stamp with the runner's last monotonic sighting of the app running.
        source = RUNNER.read_text(encoding="utf-8")
        utc = source.index("$startUtc = [datetime]::UtcNow")
        stamp = source.index("$startTimestamp = [System.Diagnostics.Stopwatch]::GetTimestamp()")
        start = source.index("$process = [System.Diagnostics.Process]::Start($startInfo)")
        self.assertLess(utc, stamp)
        self.assertLess(stamp, start)
        self.assertNotIn("$launchUtc", source, "one launch instant, not two")
        wait = _runner_snippet(*_WAIT_REGION)
        self.assertNotIn("-LaunchUtc", wait, "r16: no wall instant judges an exit")
        paired = len(re.findall(r"-LaunchTimestamp \$startTimestamp[ `\r\n]+-LastAliveMs \$lastAliveMs", wait))
        judging = wait.count("Test-GuiSmokeWaitBudgetExhausted -Process $process") + wait.count("$processBoundary = Wait-GuiSmokeProcessBounded")
        self.assertEqual(paired, judging,
                         "every judging call takes the monotonic stamp with the last sighting (the boundary call continues on the next line)")
        self.assertEqual(6, paired, "two budget checks, three stall sites and the boundary call")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class AStallVerdictPastTheWholeBudgetIsATimeoutNotNoProgress(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r15 (adversary d872def7 A2, major, introduced): a log probe that blocks past the whole budget returned stalled from the
    quiet clock; the runner broke without the deadline flag, the boundary's 1 ms look saw the app still running and typed process_no_progress (and the runner's
    prose said the leg ended before the whole budget) for an app that was alive AT the deadline. Exit 124 was right, the reason was not.
    Class: the whole budget judged on the branches that wait and forgotten on the branch that breaks on a stall verdict."""

    def _boundary(self, stub: str, since_launch_s: int, budget_ms: int) -> dict:
        return run_ps(r"""
$launch = [Diagnostics.Stopwatch]::GetTimestamp() - [int64](%d * [Diagnostics.Stopwatch]::Frequency)
$s = New-Stub '%s'
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1 -ReportedTimeoutMs 3000 -NoProgressWindowMs 1000 -NoProgressStalled -LifetimeBudgetMs %d -LaunchTimestamp $launch
[pscustomobject]@{ timedOut = $r.timedOut; outcome = $r.outcome; noProgress = $r.noProgress; window = $r.noProgressWindowMs; exitCode = $r.exitCode; failures = @($r.failures) } | ConvertTo-Json -Compress
""" % (since_launch_s, stub, budget_ms))

    def test_a_stall_verdict_over_an_app_alive_past_the_whole_budget_is_a_timeout(self) -> None:
        out = self._boundary("Start-Sleep -Seconds 30", 5, 1000)
        self.assertTrue(out["timedOut"])
        self.assertEqual(124, out["exitCode"])
        self.assertEqual("timeout", out["outcome"], "the app was alive at the deadline: that is a timeout, not a stall that ended the leg early")
        self.assertFalse(out["noProgress"])
        self.assertIsNone(out["window"])
        text = " ".join(out["failures"])
        self.assertNotIn("process_no_progress", text)
        self.assertIn("exceeded the fail-closed process timeout of 3000 ms", text)

    def test_a_stall_verdict_over_an_app_alive_inside_the_whole_budget_is_still_process_no_progress(self) -> None:
        out = self._boundary("Start-Sleep -Seconds 30", 0, 600000)
        self.assertEqual("process_no_progress", out["outcome"])
        self.assertTrue(out["noProgress"])
        self.assertEqual(1000, out["window"])

    def test_a_stall_verdict_with_no_budget_to_judge_against_is_unchanged(self) -> None:
        out = self._boundary("Start-Sleep -Seconds 30", 5, 0)
        self.assertEqual("process_no_progress", out["outcome"], "a caller that passes no -LifetimeBudgetMs is not judged")

    def test_the_probe_returns_no_stall_verdict_once_the_whole_budget_is_spent(self) -> None:
        # the adversary's input, scaled down: the watch is armed and quiet beyond its window, then a probe that blocks past the whole budget returns
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'spentbudget'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-1.log') -Value ('x' * 500) -NoNewline
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 50 -ProbeWaitMs 2000
$arm = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 20000)
Start-Sleep -Milliseconds 300
$w.ProbeDelayMs = 1500
$stalledWithBudgetLeft = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 20000)
Stop-GuiSmokeLogProgressWatch -Watch $w
$w2 = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 50 -ProbeWaitMs 2000
[void](Test-GuiSmokeLogProgressStalled -Watch $w2 -DeadlineMs 20000)
Start-Sleep -Milliseconds 300
$w2.ProbeDelayMs = 1500
# -2 r1: a deadline 200 ms after this moment, on the watch's launch clock (the old -BudgetRemainingMs 200)
$stalledPastBudget = [bool](Test-GuiSmokeLogProgressStalled -Watch $w2 -DeadlineMs ([int64](Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $w2.LaunchTimestamp) + 200))
Stop-GuiSmokeLogProgressWatch -Watch $w2
[pscustomobject]@{ arm = $arm; armed = [bool]$w.Armed; stalledWithBudgetLeft = $stalledWithBudgetLeft; stalledPastBudget = $stalledPastBudget } | ConvertTo-Json -Compress
""")
        self.assertFalse(out["arm"])
        self.assertTrue(out["armed"])
        self.assertTrue(out["stalledWithBudgetLeft"], "with budget left a quiet armed watch still reports the stall")
        self.assertFalse(out["stalledPastBudget"], "a probe that returned after the whole budget was spent must not pass a stall verdict: the whole budget decides")

    def test_every_runner_site_that_breaks_on_a_stall_verdict_judges_the_whole_budget(self) -> None:
        wait = _runner_snippet(*_WAIT_REGION)
        sites = [m.start() for m in re.finditer(re.escape("$noProgressStalled = $true"), wait)]
        self.assertEqual(3, len(sites), "the host-load branch has two stall sites and the log-only branch one")
        for site in sites:
            tail = wait[site:wait.index("break", site)]
            self.assertIn("Test-GuiSmokeWaitBudgetExhausted -Process $process -BudgetMs $effectiveProcessTimeoutMs -LaunchTimestamp $startTimestamp -LastAliveMs $lastAliveMs", tail,
                          "a stall verdict reached at or past the whole budget is judged against it before the boundary types the leg")
            self.assertIn("-ge $effectiveProcessTimeoutMs", tail)


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class TheProcessBudgetIsSpentFromTheLaunchNotFromALoopStopwatch(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r16 (sol r15 B1, blocker): both chunked wait branches took their remaining budget from a loop stopwatch started
    AFTER the launch and the runner's setup, so setup time between Process.Start and the loop was never spent: a 1000 ms budget with 3000 ms of setup still
    waited about another 1000 ms, and 900 ms of setup granted 1000 ms instead of the remaining 100 ms. Every remainder is now the whole budget minus the
    monotonic time since the launch stamp. Class: the process budget restarted on a second clock."""

    def _setup(self, setup_ms: int, sample_ms: int) -> dict:
        return run_ps(_RUNNER_HARNESS, GS_STUB=_NEVER_EXITS_NO_LOG, GS_SAMPLE_MS=str(sample_ms), GS_SETUP_MS=str(setup_ms),
                      GS_DERIVE=_runner_snippet(*_DERIVE_REGION), GS_WAIT=_runner_snippet(*_WAIT_REGION))

    def test_setup_time_after_the_launch_is_spent_from_the_budget_in_both_branches(self) -> None:
        for sample_ms in (0, 500):
            out = self._setup(2200, sample_ms)
            self.assertEqual(3000, out["hardLimitMs"])
            # 2200 ms of the 3000 ms budget were spent before the wait region: about 800 ms remain, never another whole 3000 ms
            self.assertLess(out["waitedMs"], 2000, "sample_ms=%d: the wait was granted a fresh budget after the setup" % sample_ms)
            self.assertTrue(out["timedOut"])
            self.assertEqual("timeout", out["outcome"])
            self.assertEqual(124, out["exitCode"])
            self.assertFalse(out["alive"])

    def test_no_wait_or_probe_starts_once_the_setup_spent_the_whole_budget(self) -> None:
        for sample_ms in (0, 500):
            out = self._setup(3300, sample_ms)
            self.assertLess(out["waitedMs"], 1500, "sample_ms=%d: a wait started after the launch deadline" % sample_ms)
            self.assertEqual(0, out["samples"], "no interior sample is taken once the budget is gone")
            self.assertTrue(out["timedOut"])
            self.assertEqual(124, out["exitCode"])

    def test_the_wait_region_has_no_loop_stopwatch(self) -> None:
        wait = _runner_snippet(*_WAIT_REGION)
        self.assertNotIn("Stopwatch]::StartNew()", wait, "a stopwatch started after the launch is a second budget clock")
        self.assertNotIn("LoopStopwatch", wait)
        # -2 r1: no remainder is carried from one statement to a later one: the chunk helper reads the launch clock at the wait, the probe takes D itself,
        # and the finalize computes its own wait (the precomputed $hostLoadFinalizeTimeoutMs is gone)
        self.assertEqual(2, wait.count("-LaunchTimestamp $startTimestamp -DeadlineMs $effectiveProcessTimeoutMs -ExpiryMs $noProgressWatch.ExpiryMs"))
        self.assertIn("$hostLoadSampleLoopElapsedMs = [int](Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $startTimestamp)", wait)
        for gone in ("$hostLoadRemainingMs", "$noProgressRemainingMs", "$hostLoadFinalizeTimeoutMs", "-BudgetRemainingMs"):
            self.assertNotIn(gone, wait)
        self.assertEqual(3, wait.count("Test-GuiSmokeLogProgressStalled -Watch $noProgressWatch -DeadlineMs $effectiveProcessTimeoutMs"),
                         "all three probe sites take the hard deadline on the launch clock")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class AnExitTheMonotonicClockCannotPlaceIsReportedAsUncertain(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r16 (sol r15 B2, blocker; fable VENUE-SMOKE-EXITTIME-UNREADABLE-FAILS-OPEN-1): the exit verdict mixed the monotonic
    time since launch with a WALL lifetime whenever the exit was seen after the budget, so a real 2000 ms life across a -1500 ms step (wall 500 ms, seen at
    2100 ms of a 1000 ms budget) kept exited/0, and a real 700 ms life across a +600 ms step (wall 1300 ms) became timeout/124. The exit is now bracketed on
    the monotonic clock alone (last seen running, exit seen by); when that cannot place it on either side of the budget the leg is reported as
    exit_time_uncertain (exit 125), never clean and never late. An unreadable process state reads as exhausted, never clean.
    Class: an exit judged on a clock a step can move, and a throw read as a clean answer."""

    def test_sols_two_stepped_lifetimes_are_both_uncertain_and_a_monotonic_sighting_decides(self) -> None:
        out = run_ps(r"""
function V($budget, $alive, $seen) { [string](Get-GuiSmokeExitBudgetVerdict -BudgetMs $budget -LastAliveMs $alive -ExitSeenByMs $seen) }
[pscustomobject]@{ backward = V 1000 0 2100; forward = V 1000 0 2100; seenRunningPast = V 1000 1500 2100; seenInside = V 1000 0 700 } | ConvertTo-Json -Compress
""")
        # both of sol's inputs are seen at 2100 ms of a 1000 ms budget with no sighting of the app running past it; the wall lifetime (500 / 1300) is no input
        self.assertEqual("uncertain", out["backward"])
        self.assertEqual("uncertain", out["forward"])
        self.assertEqual("late", out["seenRunningPast"])
        self.assertEqual("in_budget", out["seenInside"])

    def test_an_exit_seen_only_after_the_budget_is_never_a_clean_exit_whatever_the_wall_lifetime_says(self) -> None:
        # the stub really lived well under the 1000 ms budget by every wall clock, but the monotonic clock only knows it exited by 2100 ms after the launch
        # stamp and never saw it running: r15 let the short wall lifetime decide (exited/0); it cannot be placed, so it is uncertain
        out = run_ps(r"""
$s = New-Stub 'exit 0'
[void]$s.p.WaitForExit(30000)
$launch = [Diagnostics.Stopwatch]::GetTimestamp() - [int64](2.1 * [Diagnostics.Stopwatch]::Frequency)
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1 -ReportedTimeoutMs 1000 -LifetimeBudgetMs 1000 -LaunchTimestamp $launch
[pscustomobject]@{ timedOut = $r.timedOut; outcome = $r.outcome; exitCode = $r.exitCode; native = $r.nativeExitCode; confirmed = $r.terminationConfirmed; failures = @($r.failures) } | ConvertTo-Json -Compress
""")
        self.assertNotEqual("exited", out["outcome"], "an exit the monotonic clock cannot place inside the budget was reported as a clean one")
        self.assertEqual("exit_time_uncertain", out["outcome"])
        self.assertEqual(125, out["exitCode"])
        self.assertFalse(out["timedOut"], "it is not called a timeout either: the app may have exited inside the budget")
        self.assertTrue(out["confirmed"])
        self.assertEqual(0, out["native"], "the native exit is kept as data")
        self.assertIn("exit_time_uncertain", " ".join(out["failures"]))

    def test_an_exit_seen_inside_the_budget_keeps_its_native_exit_and_a_sighting_past_it_is_a_timeout(self) -> None:
        out = run_ps(r"""
$launch = [Diagnostics.Stopwatch]::GetTimestamp()
$s = New-Stub 'exit 7'
[void]$s.p.WaitForExit(30000)
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1 -ReportedTimeoutMs 600000 -LifetimeBudgetMs 600000 -LaunchTimestamp $launch
$s2 = New-Stub 'exit 0'
[void]$s2.p.WaitForExit(30000)
$old = [Diagnostics.Stopwatch]::GetTimestamp() - [int64](2.1 * [Diagnostics.Stopwatch]::Frequency)
$r2 = Wait-GuiSmokeProcessBounded -Process $s2.p -StandardOutputTask $s2.o -StandardErrorTask $s2.e -TimeoutMs 1 -ReportedTimeoutMs 1000 -LifetimeBudgetMs 1000 -LaunchTimestamp $old -LastAliveMs 1500
[pscustomobject]@{ inside = $r.outcome; insideCode = $r.exitCode; past = $r2.outcome; pastCode = $r2.exitCode } | ConvertTo-Json -Compress
""")
        self.assertEqual("exited", out["inside"])
        self.assertEqual(7, out["insideCode"])
        self.assertEqual("timeout", out["past"])
        self.assertEqual(124, out["pastCode"])

    def test_an_unreadable_process_state_reads_as_exhausted_never_as_clean(self) -> None:
        # a Process object with no process behind it throws on HasExited; r15's catch returned "not exhausted", so the leg's native exit was kept
        out = run_ps(r"""
$old = [Diagnostics.Stopwatch]::GetTimestamp() - [int64](2 * [Diagnostics.Stopwatch]::Frequency)
[pscustomobject]@{ exhausted = [bool](Test-GuiSmokeWaitBudgetExhausted -Process ([Diagnostics.Process]::new()) -BudgetMs 1000 -LaunchTimestamp $old) } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["exhausted"], "a throw was read as a clean answer")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class TheSilenceWindowIsBelowTheHardDeadlineOnTheGeneratorsOwnerLeg(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r16 (fable key3 r15 B1, blocker; catalogue row B-05): for every owner-input job the in-tree generator emits, the
    runner's hard deadline and its silence window both equalled the job's -ProcessTimeoutMs (r9 put the read allowance into the one window at all times), so
    the watchdog could never fire first and the incident leg (m16-1457-native-fullscreen-s1-nola, log ends at gui_smoke.foreground_request, clamped at the
    3600 s ceiling) was still held 3600 s. The read allowance now covers only the app's first silent stretch after the watch arms (the load); the first log
    growth after arming ends it, and both windows are derived strictly below the hard deadline. Class: one window serving two phases with different needs."""

    def _generated_clamped_leg(self) -> dict:
        # the generator's REAL output for the incident shape: Get-AttrCudaLegTimeBudget clamps at the share smoke ceiling and Get-AttrCudaLegHardLimitArgs
        # emits the runner argument; the runner's own derive and window text then runs as written
        out = run_ps(r"""
Import-Module $env:GS_BUDGET -Force
$clamp = Get-AttrCudaLegTimeBudget -InputBytes 314572800000 -ColdReadMBps 100 -ReadMargin 1.5 -PlaySeconds 40 -ShareSmokeCeiling
$emitted = Get-AttrCudaLegHardLimitArgs -RunnerAcceptsAllowance $true -AppReadAllowanceSec $clamp.appReadAllowanceSec
if ($emitted -notmatch '^ -LegHardLimitAllowanceMs (\d+)$') { throw "unexpected generator text: $emitted" }
$ProcessTimeoutMs = [int]$clamp.smokeProcessTimeoutMs; $LegHardLimitAllowanceMs = [int]$Matches[1]; $NoProgressTimeoutMs = 0
$derivedProcessTimeoutMs = 187000   # the runner's derived leg budget for -Seconds 40 (2000*40 + 107000, fable's trace)
. ([scriptblock]::Create($env:GS_DERIVE))
$logRoot = $env:GS_TMP
. ([scriptblock]::Create($env:GS_WINDOWS))
[pscustomobject]@{ emitted = $emitted; job = $ProcessTimeoutMs; hard = [int64]$effectiveProcessTimeoutMs; leg = [int64]$noProgressWindowMs; load = [int64]$noProgressLoadWindowMs } | ConvertTo-Json -Compress
""", GS_BUDGET=str(BUDGET_MODULE), GS_DERIVE=_runner_snippet(*_DERIVE_REGION),
            GS_WINDOWS=_runner_snippet("$noProgressOverrideMs = if", "$noProgressWatch = New-GuiSmokeLogProgressWatch"))
        return out

    def test_both_windows_are_strictly_below_the_hard_deadline_on_the_generated_clamped_owner_leg(self) -> None:
        out = self._generated_clamped_leg()
        self.assertEqual(" -LegHardLimitAllowanceMs 3587000", out["emitted"], "the generator's real argument for the clamped incident leg")
        self.assertEqual(3600000, out["hard"])
        self.assertLess(out["leg"], out["hard"], "the leg window equalled the hard deadline: the watchdog could never fire first")
        self.assertLess(out["load"], out["hard"], "the load window equalled the hard deadline: the watchdog could never fire first")
        # -2 r1: capped at L = H - M (M = 2000 at the default probe wait); r16 had H - 1000 = 3599000
        self.assertEqual(3600000 - 2000, out["load"])
        # a leg whose log stops after its load (the incident) is judged on the leg's own window: minutes, not the hour
        self.assertEqual(187000 + 60000, out["leg"])
        self.assertLessEqual(out["leg"], 300000)

    def test_after_the_first_growth_a_stopped_log_is_judged_on_the_leg_window_not_the_load_window(self) -> None:
        # scaled: load window 30 s, leg window 600 ms. The app writes its first line (arms), writes again (the load is over), then stops.
        # -2 r1: the load ends on the `gui_smoke.` marker that growth carries (the incident's gui_smoke.foreground_request line), not on any growth
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'loadspent'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value ('x' * 100)
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 600 -LoadWindowMs 30000 -ProbeWaitMs 2000
$arm = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 60000)
Start-Sleep -Milliseconds 900
$quietLoad = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 60000)
Add-Content -LiteralPath $log -Value '[2026-11-19T10:00:04.000Z] [info] [0x1a2b] gui_smoke.foreground_request requested=1 verified=1 (MainWindow.cpp:10277)'
$grew = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 60000)
Start-Sleep -Milliseconds 900
$stalled = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 60000)
$active = [int64]$w.ActiveWindowMs
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ arm = $arm; quietLoad = $quietLoad; grew = $grew; stalled = $stalled; active = $active } | ConvertTo-Json -Compress
""")
        self.assertFalse(out["arm"])
        self.assertFalse(out["quietLoad"], "a silent first stretch (the load) keeps its load window (r9: a healthy silent load is not a stall)")
        self.assertFalse(out["grew"])
        self.assertTrue(out["stalled"], "after the load a stopped log must be judged on the leg window, not the load window")
        self.assertEqual(600, out["active"], "the window reported is the leg window that fired")

    def test_the_runner_reports_the_window_in_force_and_its_comment_no_longer_claims_the_window_ignores_the_allowance(self) -> None:
        wait = _runner_snippet(*_WAIT_REGION)
        self.assertIn("-WindowMs $noProgressWindowMs -LoadWindowMs $noProgressLoadWindowMs", wait)
        self.assertIn("$noProgressWindowMs = [int64]$noProgressWatch.ActiveWindowMs", wait)
        self.assertLess(wait.index("$noProgressWindowMs = [int64]$noProgressWatch.ActiveWindowMs"), wait.index("$processBoundary = Wait-GuiSmokeProcessBounded"))
        source = RUNNER.read_text(encoding="utf-8")
        self.assertNotIn("so it does not widen with a caller's -ProcessTimeoutMs", source, "fable key3 r15 B1: that comment was false for the owner-input leg")


_INVARIANT = "No silence expiration is later than L = D - M, whatever time the watch arms; while loading, E is never below min(L, WL)."


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class TheSilenceModelIsLaunchAnchored(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1: r1-r16 of -1 built the silence wait from DURATIONS (a quiet stopwatch, a remaining budget handed from call to
    call, a finalize remainder computed before the call) and fixed one clock instance per round. One launch-anchored model replaces them: every time is ms
    since the runner's launch stamp; L = max(min(1000, H), H - M) with M = ProbeWaitMs + 1000; E = max(WL, P + W) while LOADING and P + W once LOADED,
    and a value past L is no expiry at all (adversary 81e7f8b2 A2: a window that cannot run out by L proves no silence, so the deadline decides).
    INVARIANT: "No silence expiration is later than L = D - M, whatever time the watch arms; while loading, E is never below min(L, WL)."
    """

    _H = (1000, 2500, 133000, 1285000, 3600000)

    def test_the_invariant_holds_over_the_property_table(self) -> None:
        out = run_ps(r"""
$rows = [System.Collections.Generic.List[object]]::new()
foreach ($h in 1000, 2500, 133000, 1285000, 3600000) {
    $l = Get-GuiSmokeWatchdogLimitMs -HardMs $h -ProbeWaitMs 1000
    foreach ($override in $false, $true) {
        # the runner's own derivation: leg 187000, the generator's largest read allowance, an override of the whole hard deadline (the widest it can ask)
        $o = if ($override) { $h } else { 0 }
        $w = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 187000 -OverrideMs $o -CeilingMs $l
        $wl = Get-GuiSmokeNoProgressWindowMs -LegBudgetMs 187000 -OverrideMs $o -CeilingMs $l -ReadAllowanceMs 3480000
        foreach ($p in 0, 1, 1000, 4000, [Math]::Floor($h / 2), ($h - $w), ($h - 2000), ($h - 1000), ($h - 1), $h) {
            $pp = [Math]::Max(0, $p)
            foreach ($loaded in $false, $true) {
                $e = Get-GuiSmokeSilenceExpiryMs -LimitMs $l -WindowMs $w -LoadWindowMs $wl -LastProgressMs $pp -Loaded:$loaded
                $rows.Add([pscustomobject]@{ h = $h; l = $l; w = $w; wl = $wl; p = $pp; loaded = $loaded; override = $override; e = $e })
            }
        }
    }
}
[pscustomobject]@{ rows = @($rows) } | ConvertTo-Json -Compress -Depth 4
""")
        rows = out["rows"]
        self.assertEqual(5 * 2 * 10 * 2, len(rows), "H x override x P x Loaded")
        none = 0
        for r in rows:
            label = "H=%(h)s P=%(p)s loaded=%(loaded)s override=%(override)s W=%(w)s WL=%(wl)s L=%(l)s E=%(e)s" % r
            self.assertLessEqual(r["e"], r["l"], "an expiry later than L: " + label)
            self.assertLessEqual(r["w"], r["l"], label)
            self.assertLessEqual(r["wl"], r["l"], label)
            if r["h"] >= 2000 + 1000:
                self.assertLess(r["l"], r["h"], "L is not below the hard deadline: " + label)
            # adversary 81e7f8b2 A2: a window that cannot run out by L is NO expiry (-1, the deadline decides), never one capped at L
            window_end = r["p"] + r["w"] if r["loaded"] else max(r["wl"], r["p"] + r["w"])
            if window_end > r["l"]:
                self.assertEqual(-1, r["e"], "a window that cannot run out by L still expires (capped at L, a silence verdict with less than one window): " + label)
                none += 1
                continue
            self.assertEqual(window_end, r["e"], label)
            if not r["loaded"]:
                self.assertGreaterEqual(r["e"], min(r["l"], r["wl"]), "a LOADING expiry below min(L, WL): " + label)
        self.assertGreater(none, 0, "the table holds rows whose window cannot run out by L")
        self.assertLess(none, len(rows), "the table holds rows whose window runs out by L")

    def test_the_limit_and_expiry_helpers_are_the_one_copy_of_the_arithmetic(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn(_INVARIANT, text, "the invariant sentence is stated verbatim in the module header")
        self.assertIn(_INVARIANT, self.__class__.__doc__ or "")
        exports = code.split("Export-ModuleMember", 1)[1]
        for name in ("Get-GuiSmokeWatchdogLimitMs", "Get-GuiSmokeSilenceExpiryMs", "Get-GuiSmokeChunkWaitMs", "Get-GuiSmokeLogProgressExpiryMs"):
            self.assertIn(name, exports)
        # the watch's E comes from the helper; the probe wait and the verdict read the launch clock, never a stopwatch
        watch = code[code.index("function Get-GuiSmokeLogProgressExpiryMs"):code.index("function Stop-GuiSmokeLogProgressWatch")]
        self.assertIn("Get-GuiSmokeSilenceExpiryMs -LimitMs", watch)
        stalled = code[code.index("function Test-GuiSmokeLogProgressStalled"):code.index("function Get-GuiSmokeProcessLifetimeMs")]
        self.assertIn("$Watch.ExpiryMs = Get-GuiSmokeLogProgressExpiryMs -Watch $Watch", stalled)
        self.assertNotIn("Stopwatch]::StartNew", code, "no stopwatch in the module: every clock is the launch stamp")
        self.assertNotIn(".Restart(", code)
        self.assertNotIn("Quiet", code)
        self.assertEqual(1, code.count("+ $WindowMs"), "P + W is computed once, in Get-GuiSmokeSilenceExpiryMs")
        self.assertEqual(1, code.count("$ProbeWaitMs + 1000"), "M is computed once, in Get-GuiSmokeWatchdogLimitMs")
        wait = _runner_snippet(*_WAIT_REGION)
        self.assertIn("-LimitMs $noProgressLimitMs -LaunchTimestamp $startTimestamp -ProbeWaitMs $noProgressProbeWaitMs", wait)
        self.assertNotIn("WindowMs +", wait)
        self.assertNotIn("+ $noProgress", wait)

    def test_the_chunk_wait_lands_on_the_expiry_and_never_passes_the_deadline(self) -> None:
        out = run_ps(r"""
$hz = [Diagnostics.Stopwatch]::Frequency
$at = { param($ms) [Diagnostics.Stopwatch]::GetTimestamp() - [int64]($ms * $hz / 1000) }
[pscustomobject]@{
    unarmed = Get-GuiSmokeChunkWaitMs -ChunkMs 4000 -LaunchTimestamp (& $at 1000) -DeadlineMs 600000 -ExpiryMs -1
    toExpiry = Get-GuiSmokeChunkWaitMs -ChunkMs 4000 -LaunchTimestamp (& $at 1000) -DeadlineMs 600000 -ExpiryMs 2000
    expiryPassed = Get-GuiSmokeChunkWaitMs -ChunkMs 4000 -LaunchTimestamp (& $at 5000) -DeadlineMs 600000 -ExpiryMs 2000
    toDeadline = Get-GuiSmokeChunkWaitMs -ChunkMs 4000 -LaunchTimestamp (& $at 599000) -DeadlineMs 600000 -ExpiryMs -1
    deadlinePassed = Get-GuiSmokeChunkWaitMs -ChunkMs 4000 -LaunchTimestamp (& $at 600500) -DeadlineMs 600000 -ExpiryMs 900000
} | ConvertTo-Json -Compress
""")
        self.assertEqual(4000, out["unarmed"])
        self.assertTrue(950 <= out["toExpiry"] <= 1000, out)
        self.assertEqual(1, out["expiryPassed"], "an expiry already passed is checked after a 1 ms look, never skipped")
        self.assertTrue(950 <= out["toDeadline"] <= 1000, out)
        self.assertEqual(0, out["deadlinePassed"], "no wait starts once D is reached")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class TheLoadAllowanceEndsOnTheGuiSmokeMarker(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1 (adversary r16 A1, major): r16 ended the load allowance at ANY growth after arming, so the generated
    owner-input leg (24 GiB at 32 MB/s, hard 1285000 ms, allowance 1272000 ms) that logs one start-up line and then loads silently was killed at about
    255 s. The phase is now LOADING until the app logs the ASCII bytes `gui_smoke.` (MainWindow logs interaction_trace event=gui_smoke.playback_policy
    once openMlvSet returns), and LOADED from then on (one-way)."""

    def test_a_start_up_growth_does_not_end_the_load_allowance(self) -> None:
        # scaled: W = 1500, WL = 6000; the log exists, grows once about 0.5 s after arming, then stays silent; judged at about 3 s since launch
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'startupgrowth'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value 'Qt start-up line' -NoNewline
$launch = [Diagnostics.Stopwatch]::GetTimestamp()
$since = { [double]([Diagnostics.Stopwatch]::GetTimestamp() - $launch) * 1000.0 / [Diagnostics.Stopwatch]::Frequency }
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1500 -LoadWindowMs 6000 -ProbeWaitMs 300
$stalledAt = -1; $armedAt = -1; $grownAt = -1
while ((& $since) -lt 3000 -and $stalledAt -lt 0) {
    if (Test-GuiSmokeLogProgressStalled -Watch $w) { $stalledAt = [int](& $since) }
    if ($w.Armed -and $armedAt -lt 0) { $armedAt = [int](& $since) }
    if ($armedAt -ge 0 -and $grownAt -lt 0 -and (& $since) -ge $armedAt + 500) { Add-Content -LiteralPath $log -Value 'a start-up growth line'; $grownAt = [int](& $since) }
    Start-Sleep -Milliseconds 100
}
$credited = [int64]$w.Files['mlvapp-1.log']
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ stalledAt = $stalledAt; armedAt = $armedAt; grownAt = $grownAt; credited = $credited; phase = $w.Phase; expiry = $w.ExpiryMs } | ConvertTo-Json -Compress
""")
        self.assertGreaterEqual(out["armedAt"], 0)
        self.assertGreaterEqual(out["grownAt"], 0)
        self.assertGreater(out["credited"], len("Qt start-up line"), "the growth was credited")
        self.assertEqual(-1, out["stalledAt"], "a start-up growth with no gui_smoke. line ended the load allowance: stalled at %s ms since launch, %s ms after the growth"
                         % (out["stalledAt"], out["stalledAt"] - out["grownAt"]))
        self.assertEqual("LOADING", out["phase"])
        self.assertGreaterEqual(out["expiry"], 6000 - 50, "while loading E is never below min(L, WL)")

    def test_the_gui_smoke_marker_switches_the_watch_to_loaded_and_the_leg_window(self) -> None:
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'marker'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value 'start'
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 800 -LoadWindowMs 20000 -LimitMs 30000 -ProbeWaitMs 1000
function Poll-Until([scriptblock]$Done) { $sw = [Diagnostics.Stopwatch]::StartNew(); while (-not (& $Done) -and $sw.ElapsedMilliseconds -lt 8000) { [void](Test-GuiSmokeLogProgressStalled -Watch $w); Start-Sleep -Milliseconds 50 } }
Poll-Until { $w.Armed -and $null -eq $w.Pending }
$len0 = [int64]$w.Files['mlvapp-1.log']
Add-Content -LiteralPath $log -Value '[2026-11-19T10:00:01.000Z] [info] [0x1a2b] a plain growth record without the marker (main.cpp:1)'
Poll-Until { [int64]$w.Files['mlvapp-1.log'] -gt $len0 -and $null -eq $w.Pending }
$plain = [pscustomobject]@{ phase = $w.Phase; expiry = $w.ExpiryMs; p = $w.LastProgressMs; active = $w.ActiveWindowMs }
# a marker record split across two reads: the probe re-reads the 257 bytes before the previous high-water mark
$len1 = [int64]$w.Files['mlvapp-1.log']
Add-Content -LiteralPath $log -Value '[2026-11-19T10:00:02.000Z] [info] [0x1a2b] interaction_trace event=gui_sm' -NoNewline
Poll-Until { [int64]$w.Files['mlvapp-1.log'] -gt $len1 -and $null -eq $w.Pending }
$half = [string]$w.Phase
$len2 = [int64]$w.Files['mlvapp-1.log']
Add-Content -LiteralPath $log -Value 'oke.playback_policy mode=realtime'
Poll-Until { [int64]$w.Files['mlvapp-1.log'] -gt $len2 -and $null -eq $w.Pending }
$marked = [pscustomobject]@{ phase = $w.Phase; expiry = $w.ExpiryMs; p = $w.LastProgressMs; active = $w.ActiveWindowMs }
Start-Sleep -Milliseconds 1000
$stalled = [bool](Test-GuiSmokeLogProgressStalled -Watch $w)
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ plain = $plain; half = $half; marked = $marked; stalled = $stalled } | ConvertTo-Json -Compress -Depth 4
""")
        plain, marked = out["plain"], out["marked"]
        self.assertEqual("LOADING", plain["phase"], "growth without gui_smoke. keeps the watch LOADING")
        # adversary 0a7510a1 A1: the window in force is the silence that runs out at E (E - P), not the load allowance from the launch
        self.assertEqual(round(plain["expiry"] - plain["p"]), plain["active"])
        self.assertAlmostEqual(max(20000, plain["p"] + 800), plain["expiry"], delta=0.01)
        self.assertEqual("LOADING", out["half"], "half a marker is not the marker")
        self.assertEqual("LOADED", marked["phase"], "interaction_trace event=gui_smoke.playback_policy switches the watch to LOADED (split across two reads)")
        self.assertEqual(800, marked["active"])
        self.assertAlmostEqual(marked["p"] + 800, marked["expiry"], delta=0.01, msg="once LOADED, E = P + W (it is below L here)")
        self.assertTrue(out["stalled"], "a LOADED log silent past P + W is a stall")

    def test_gui_smoke_bytes_inside_another_record_do_not_end_the_load_allowance(self) -> None:
        """adversary 81e7f8b2 A1 (major): CrashForensics logs the start-up run_metadata record with the whole command line, so an owner input whose path
        holds `gui_smoke.` put those bytes in the log at start-up and switched the watch to LOADED before the silent load. The marker is a RECORD whose
        message starts `gui_smoke.` (or `interaction_trace event=gui_smoke.`), at the start of a line."""
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'metadatapath'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value '[2026-11-19T10:00:00.000Z] [info] [0x1a2b] run_metadata={"command_line":["MLVApp.exe","--gui-smoke-play","X:\\owner-input\\gui_smoke.night\\take"]} (CrashForensics.cpp:904)'
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 800 -LoadWindowMs 20000 -LimitMs 30000 -ProbeWaitMs 1000
function Poll-Until([scriptblock]$Done) { $sw = [Diagnostics.Stopwatch]::StartNew(); while (-not (& $Done) -and $sw.ElapsedMilliseconds -lt 8000) { [void](Test-GuiSmokeLogProgressStalled -Watch $w); Start-Sleep -Milliseconds 50 } }
Poll-Until { $w.Armed -and $null -eq $w.Pending -and [int64]$w.Files['mlvapp-1.log'] -ge 0 }
$len0 = [int64]$w.Files['mlvapp-1.log']
# the same bytes later in the start-up, mid-record (a message that quotes the path)
Add-Content -LiteralPath $log -Value '[2026-11-19T10:00:01.000Z] [info] [0x1a2b] opening X:\owner-input\gui_smoke.night\take (MainWindow.cpp:9700)'
Poll-Until { [int64]$w.Files['mlvapp-1.log'] -gt $len0 -and $null -eq $w.Pending }
$start = [pscustomobject]@{ phase = $w.Phase; expiry = $w.ExpiryMs; p = $w.LastProgressMs }
$len1 = [int64]$w.Files['mlvapp-1.log']
Add-Content -LiteralPath $log -Value '[2026-11-19T10:00:09.000Z] [info] [0x1a2b] gui_smoke.launch_only played=0 presented_frames=0 frames=24 (MainWindow.cpp:9729)'
Poll-Until { [int64]$w.Files['mlvapp-1.log'] -gt $len1 -and $null -eq $w.Pending }
$marked = [string]$w.Phase
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ start = $start; marked = $marked } | ConvertTo-Json -Compress -Depth 4
""")
        self.assertEqual("LOADING", out["start"]["phase"], "the bytes gui_smoke. inside a start-up record (an input path) ended the load allowance")
        self.assertAlmostEqual(max(20000, out["start"]["p"] + 800), out["start"]["expiry"], delta=0.01, msg="the load allowance still holds")
        self.assertEqual("LOADED", out["marked"], "the app's own gui_smoke.launch_only record still ends the load")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class AWindowThatCannotRunOutByTheLimitMakesNoSilenceVerdict(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1 (adversary 81e7f8b2 A2, major): capping E at L made a log still growing at L expire AT L, so the poll that
    credited the growth also returned stalled, with no silence at all, and the leg was reported as process_no_progress for a whole window. A window that
    cannot run out by L proves no silence by L: there is no expiry (-1) and the hard deadline decides the leg (timeout)."""

    def test_a_loaded_log_that_keeps_growing_past_the_limit_is_never_stalled(self) -> None:
        # W = 1500, L = 3000, D = 4000; the app is LOADED and logs a record about every 200 ms until 3.9 s since launch (W leaves room for a probe that
        # starts slowly: a credited gap of a whole window would be a real stall)
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'growingatlimit'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value '[2026-11-19T10:00:00.000Z] [info] [0x1a2b] interaction_trace event=gui_smoke.playback_policy mode=realtime (MainWindow.cpp:9879)'
$launch = [Diagnostics.Stopwatch]::GetTimestamp()
$since = { [double]([Diagnostics.Stopwatch]::GetTimestamp() - $launch) * 1000.0 / [Diagnostics.Stopwatch]::Frequency }
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1500 -LimitMs 3000 -LaunchTimestamp $launch -ProbeWaitMs 1000
$stalledAt = -1; $lastP = -1; $noneSeen = $false; $n = 0
while ((& $since) -lt 3900 -and $stalledAt -lt 0) {
    $n++
    Add-Content -LiteralPath $log -Value ('[2026-11-19T10:00:01.000Z] [info] [0x1a2b] frame {0} presented (MainWindow.cpp:1)' -f $n)
    if (Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 4000) { $stalledAt = [int](& $since); $lastP = [int]$w.LastProgressMs }
    if ($w.Armed -and [double]$w.ExpiryMs -lt 0) { $noneSeen = $true }
    Start-Sleep -Milliseconds 200
}
$phase = [string]$w.Phase
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ stalledAt = $stalledAt; lastP = $lastP; noneSeen = $noneSeen; phase = $phase } | ConvertTo-Json -Compress
""")
        self.assertEqual("LOADED", out["phase"])
        self.assertEqual(-1, out["stalledAt"], "a log still growing at L was judged stalled at %s ms since launch, its last progress at %s ms"
                         % (out["stalledAt"], out["lastP"]))
        self.assertTrue(out["noneSeen"], "once P + W is past L the watch carries no expiry (-1)")

    def test_the_expiry_helper_gives_no_expiry_for_a_window_past_the_limit(self) -> None:
        out = run_ps(r"""
[pscustomobject]@{
    fits = Get-GuiSmokeSilenceExpiryMs -LimitMs 3000 -WindowMs 1000 -LastProgressMs 2000 -Loaded
    past = Get-GuiSmokeSilenceExpiryMs -LimitMs 3000 -WindowMs 1000 -LastProgressMs 2001 -Loaded
    loadFits = Get-GuiSmokeSilenceExpiryMs -LimitMs 6000 -WindowMs 1500 -LoadWindowMs 6000 -LastProgressMs 2000
    loadPast = Get-GuiSmokeSilenceExpiryMs -LimitMs 6000 -WindowMs 1500 -LoadWindowMs 6000 -LastProgressMs 4600
} | ConvertTo-Json -Compress
""")
        self.assertEqual(3000, out["fits"], "a window that runs out exactly at L expires at L")
        self.assertEqual(-1, out["past"], "a window that runs out after L is no expiry, never one capped at L")
        self.assertEqual(6000, out["loadFits"], "while LOADING the load allowance from the launch holds: E = max(WL, P + W)")
        self.assertEqual(-1, out["loadPast"])


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class AStallReportsTheSilenceThatRanOutNotTheLoadAllowance(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1 (adversary 0a7510a1 A1, major): while LOADING, E = max(WL, P + W) is anchored at the launch, so a log whose
    last growth was credited at P and that then stays silent expires at WL with WL - P of silence; the watch still reported WL as the window, and the
    boundary wrote "MLVApp made no log progress for WL ms" and published WL as noProgressWindowMs (owner leg: 1399000 ms claimed, 247000 ms real). The
    window in force is E - P."""

    def test_a_loading_stall_reports_wl_minus_p_and_the_boundary_prose_carries_it(self) -> None:
        # scaled: W = 800, WL = 3000; the log arms at once, grows (no marker) at about 1 s, then stays silent; E = WL = 3000, silence about 2000 ms
        out = run_ps(r"""
$dir = Join-Path $env:GS_TMP 'loadingstall'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'mlvapp-1.log'
Set-Content -LiteralPath $log -Value '[2026-11-19T10:00:00.000Z] [info] [0x1a2b] Qt start-up (main.cpp:1)'
$launch = [Diagnostics.Stopwatch]::GetTimestamp()
$since = { [double]([Diagnostics.Stopwatch]::GetTimestamp() - $launch) * 1000.0 / [Diagnostics.Stopwatch]::Frequency }
$w = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 800 -LoadWindowMs 3000 -LimitMs 30000 -LaunchTimestamp $launch -ProbeWaitMs 1000
$stalled = $false; $grown = $false
while ((& $since) -lt 6000 -and -not $stalled) {
    if (-not $grown -and (& $since) -ge 1000) { Add-Content -LiteralPath $log -Value '[2026-11-19T10:00:01.000Z] [info] [0x1a2b] a start-up growth (main.cpp:2)'; $grown = $true }
    $stalled = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -DeadlineMs 32000)
    Start-Sleep -Milliseconds 50
}
$s = New-Stub ('& "' + $env:GS_PWSH + '" -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 30"')
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 5000 -NoProgressStalled -NoProgressWindowMs ([int64]$w.ActiveWindowMs)
Stop-GuiSmokeLogProgressWatch -Watch $w
[pscustomobject]@{ stalled = $stalled; phase = $w.Phase; p = $w.LastProgressMs; expiry = $w.ExpiryMs; active = [int64]$w.ActiveWindowMs; window = $r.noProgressWindowMs
    failures = @($r.failures) } | ConvertTo-Json -Compress -Depth 4
""")
        self.assertTrue(out["stalled"])
        self.assertEqual("LOADING", out["phase"])
        self.assertEqual(3000, out["expiry"], "E = WL while the allowance from the launch is the later end")
        silence = round(out["expiry"] - out["p"])
        self.assertGreaterEqual(silence, 800, "the silence that ran out is at least one leg window")
        self.assertEqual(silence, out["active"], "a LOADING stall reported %s ms of silence, but the log was silent for %s ms (E - P)" % (out["active"], silence))
        self.assertEqual(silence, out["window"])
        self.assertTrue(any(("no log progress for %d ms" % silence) in f for f in out["failures"]), out["failures"])


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class NoSilenceExpiryIsLaterThanTheLimitWhateverTimeTheWatchArms(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1 (adversary r16 A3, major): r16 anchored the quiet clock at the FIRST observation of the log, so a window of
    H - 1000 armed at 4 s expired at about H + 3000 (owner leg 3603000 ms of 3600000; fixture 136000 ms of 133000): the whole budget always ran out first.
    The r16 test compared durations; these compare TIMES SINCE LAUNCH. The runner's own derivation and wait region run as written, with H = 8000
    (leg 2000 + allowance 6000), M = 2000, L = 6000, and the app's first log line written at about 2 s.

    Adversary 81e7f8b2 A2 re-specified the derived case: there W = L = 6000, so a watch armed at about 2 s cannot see a whole window of silence by L, and
    capping E at L would claim one. No silence verdict is passed (E = -1) and the hard deadline ends the leg as a timeout. A window that fits after the
    late arming (an explicit -NoProgressTimeoutMs of 2000 ms) still fires by L."""

    _DELAYED_ARMING = "Start-Sleep -Milliseconds 2000; Add-Content -LiteralPath $env:GS_LOG -Value 'first line'; Start-Sleep -Seconds 30"

    def _run(self, sample_ms: int, override_ms: int = 0) -> dict:
        extra = {"GS_OVERRIDE_MS": str(override_ms)} if override_ms else {}
        return run_ps(_RUNNER_HARNESS, GS_STUB=self._DELAYED_ARMING, GS_SAMPLE_MS=str(sample_ms), GS_ALLOW_MS="6000",
                      GS_DERIVE=_runner_snippet(*_DERIVE_REGION), GS_WAIT=_runner_snippet(*_WAIT_REGION), **extra)

    def _assert_fires_before_the_deadline(self, out: dict, branch: str) -> None:
        hard, margin = 8000, 2000
        self.assertEqual(hard, out["hardLimitMs"])
        self.assertTrue(out["armed"], branch)
        self.assertEqual("process_no_progress", out["outcome"],
                         "%s: the silence watchdog armed late never fired before the hard deadline (the loop ended at %s ms since launch)" % (branch, out["loopEndMs"]))
        self.assertGreaterEqual(out["expiryMs"], 0, branch)
        self.assertLessEqual(out["expiryMs"], hard - margin, "%s: the expiry, in ms since launch, is later than H - M" % branch)
        # the verdict follows one probe (at most ProbeWaitMs = M - 1000) after E: it lands no later than H - 1000
        self.assertLessEqual(out["loopEndMs"], hard - 1000, "%s: the stall verdict, in ms since launch, is later than H - M + ProbeWaitMs" % branch)
        self.assertTrue(out["timedOut"])
        self.assertEqual(124, out["exitCode"])
        self.assertFalse(out["alive"])

    def _assert_the_deadline_decides(self, out: dict, branch: str) -> None:
        hard = 8000
        self.assertEqual(hard, out["hardLimitMs"])
        self.assertTrue(out["armed"], branch)
        self.assertEqual("timeout", out["outcome"], "%s: a window that cannot run out by L made a silence verdict (expiry %s ms since launch, the loop ended at %s)"
                         % (branch, out["expiryMs"], out["loopEndMs"]))
        self.assertEqual(-1, out["expiryMs"], "%s: W = L armed at about 2 s cannot run out by L: no expiry" % branch)
        self.assertGreaterEqual(out["loopEndMs"], hard - 50, "%s: the leg ended before its hard deadline with no silence verdict" % branch)
        self.assertTrue(out["timedOut"])
        self.assertEqual(124, out["exitCode"])
        self.assertFalse(out["alive"])

    def test_a_late_arming_with_a_window_that_fits_expires_by_h_minus_m_in_the_log_only_branch(self) -> None:
        self._assert_fires_before_the_deadline(self._run(0, override_ms=2000), "log-only")

    def test_a_late_arming_with_a_window_that_fits_expires_by_h_minus_m_in_the_host_load_branch(self) -> None:
        self._assert_fires_before_the_deadline(self._run(500, override_ms=2000), "host-load")

    def test_a_late_arming_whose_window_cannot_run_out_by_l_is_left_to_the_deadline_in_the_log_only_branch(self) -> None:
        self._assert_the_deadline_decides(self._run(0), "log-only")

    def test_a_late_arming_whose_window_cannot_run_out_by_l_is_left_to_the_deadline_in_the_host_load_branch(self) -> None:
        self._assert_the_deadline_decides(self._run(500), "host-load")


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class TheFirstInBudgetExitSightingIsCarriedToTheVerdict(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1 (adversary r16 A2, major): the runner's chunk wait saw the app exit inside the budget and broke, but dropped
    that sighting; a runner descheduled before the finalize let the boundary date the exit again PAST the budget, so a clean exit 0 became
    exit_time_uncertain/125. The first sighting is now carried (-ExitSeenByMs) and the boundary never dates the exit again. A real exited stub stands in
    for the FakeProc here: Process.ExitCode is not virtual, so a FakeProc cannot carry the native exit this test must see kept."""

    def test_a_carried_in_budget_sighting_keeps_the_native_exit_although_the_finalize_runs_past_the_budget(self) -> None:
        out = run_ps(r"""
$s = New-Stub 'exit 7'
[void]$s.p.WaitForExit(30000)
# the launch was 2.1 s ago against a 1000 ms budget: now is past it; the app was last seen running at 500 ms and its exit was first seen at 900 ms
$launch = [Diagnostics.Stopwatch]::GetTimestamp() - [int64](2.1 * [Diagnostics.Stopwatch]::Frequency)
$r = Wait-GuiSmokeProcessBounded -Process $s.p -StandardOutputTask $s.o -StandardErrorTask $s.e -TimeoutMs 1 -ReportedTimeoutMs 1000 -LifetimeBudgetMs 1000 -LaunchTimestamp $launch -LastAliveMs 500 -ExitSeenByMs 900
[pscustomobject]@{ outcome = $r.outcome; exitCode = $r.exitCode; timedOut = $r.timedOut; native = $r.nativeExitCode; failures = @($r.failures) } | ConvertTo-Json -Compress
""")
        self.assertEqual("exited", out["outcome"], "the boundary dated an exit first seen inside the budget again, after it")
        self.assertEqual(7, out["exitCode"], "the native exit is kept")
        self.assertFalse(out["timedOut"])

    def test_the_budget_check_stores_the_first_sighting_and_never_replaces_it(self) -> None:
        out = run_ps(r"""
$s = New-Stub 'exit 0'
[void]$s.p.WaitForExit(30000)
$launch = [Diagnostics.Stopwatch]::GetTimestamp() - [int64](2.1 * [Diagnostics.Stopwatch]::Frequency)
$fresh = [double]-1
$e1 = Test-GuiSmokeWaitBudgetExhausted -Process $s.p -BudgetMs 1000 -LaunchTimestamp $launch -LastAliveMs 1500 -ExitSeenByMs ([ref]$fresh)
$kept = [double]900
$e2 = Test-GuiSmokeWaitBudgetExhausted -Process $s.p -BudgetMs 1000 -LaunchTimestamp $launch -LastAliveMs 0 -ExitSeenByMs ([ref]$kept)
$e3 = Test-GuiSmokeWaitBudgetExhausted -Process $s.p -BudgetMs 1000 -LaunchTimestamp $launch -LastAliveMs 0
[pscustomobject]@{ e1 = [bool]$e1; fresh = $fresh; e2 = [bool]$e2; kept = $kept; e3 = [bool]$e3 } | ConvertTo-Json -Compress
""")
        self.assertTrue(out["e1"], "seen running past the budget: a deadline hit")
        self.assertGreaterEqual(out["fresh"], 2000, "the sighting this call made is stored in the carrier")
        self.assertFalse(out["e2"], "a carried in-budget sighting is not a deadline hit")
        self.assertEqual(900, out["kept"], "a sighting once set is never replaced")
        self.assertFalse(out["e3"], "no carrier: unchanged (an exit seen only after the budget is uncertain, not a deadline hit here)")

    def test_the_runner_records_the_first_sighting_at_every_exit_and_hands_it_to_the_boundary(self) -> None:
        wait = _runner_snippet(*_WAIT_REGION)
        self.assertIn("$exitSeenByMs = [double]-1", wait)
        for chunk in ("$process.WaitForExit($hostLoadChunkMs)", "$process.WaitForExit($noProgressChunkWaitMs)"):
            start = wait.index(chunk)
            self.assertIn("if ($exitSeenByMs -lt 0) { $exitSeenByMs = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $startTimestamp }", wait[start:wait.index("break", start)], chunk)
        self.assertEqual(5, wait.count("-ExitSeenByMs ([ref]$exitSeenByMs)"), "every budget check carries the sighting by reference")
        self.assertEqual(wait.count("Test-GuiSmokeWaitBudgetExhausted -Process $process"), wait.count("-ExitSeenByMs ([ref]$exitSeenByMs)"))
        call = wait[wait.index("$processBoundary = Wait-GuiSmokeProcessBounded"):]
        self.assertIn("-ExitSeenByMs $exitSeenByMs", call)
        text = MODULE.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        boundary = code[code.index("function Wait-GuiSmokeProcessBounded"):]
        self.assertIn("$exitBoundMs = if ($ExitSeenByMs -ge 0) { $ExitSeenByMs }", boundary)


if __name__ == "__main__":
    unittest.main()
