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
        self.assertIn("-CeilingMs $effectiveProcessTimeoutMs", call)
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
        # [Math]::Min takes two arguments (a three-argument call would throw), so the three bounds nest
        self.assertIn("$hostLoadChunkMs = [Math]::Min($HostLoadSampleIntervalMs, [Math]::Min($hostLoadRemainingMs, $noProgressChunkMs))", source)
        self.assertIn("[Math]::Min($noProgressChunkMs, $noProgressRemainingMs)", source)

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
[void](Test-GuiSmokeLogProgressStalled -Watch $w -BudgetRemainingMs 50)
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
        self.assertEqual(calls, source.count("Test-GuiSmokeLogProgressStalled -Watch $noProgressWatch -BudgetRemainingMs"),
                         "every runner call hands the remaining process budget to the probe wait")

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
$quietAfterRestore = [int]$w.Quiet.ElapsedMilliseconds
$baseline = [int64]$w.Files['mlvapp-1.log']
Add-Content -LiteralPath $log -Value 'more' -NoNewline
[void](Test-GuiSmokeLogProgressStalled -Watch $w)
$quietAfterGrowth = [int]$w.Quiet.ElapsedMilliseconds
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
$sw = [Diagnostics.Stopwatch]::StartNew()
$stalled = [bool](Test-GuiSmokeLogProgressStalled -Watch $w -BudgetRemainingMs 0)
$zeroMs = [int]$sw.ElapsedMilliseconds
$pendingAfterZero = ($null -ne $w.Pending)
Stop-GuiSmokeLogProgressWatch -Watch $w
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-1.log') -Value ('x' * 500) -NoNewline
$late = New-GuiSmokeLogProgressWatch -LogRoot $dir -WindowMs 1000 -ProbeDelayMs 600 -ProbeWaitMs 5000
[void](Test-GuiSmokeLogProgressStalled -Watch $late -BudgetRemainingMs 300)
Start-Sleep -Milliseconds 1500
[void](Test-GuiSmokeLogProgressStalled -Watch $late -BudgetRemainingMs 0)
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
        body = text[text.index("function Test-GuiSmokeLogProgressStalled"):text.index("function Wait-GuiSmokeProcessBounded")]
        self.assertIn("[System.Diagnostics.Stopwatch]::StartNew()", body, "an absolute endpoint is taken at function entry")
        self.assertLess(body.index("[System.Diagnostics.Stopwatch]::StartNew()"), body.index("Finished.WaitOne("))
        self.assertIn("$BudgetRemainingMs - ", body, "the wait is re-derived from the entry stopwatch just before WaitOne")

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
# an armed watch whose app log (a) has been quiet for longer than the window, as after a stall the watchdog has not yet judged
$w.Files['mlvapp-a.log'] = [int64]100; $w.Armed = $true; $w.Quiet.Restart()
Start-Sleep -Milliseconds 1500
$quietBefore = [int]$w.Quiet.ElapsedMilliseconds
Set-Content -LiteralPath (Join-Path $dir 'mlvapp-b.log') -Value ('y' * 100) -NoNewline
$stalledAtAnnouncement = [bool](Test-GuiSmokeLogProgressStalled -Watch $w)
$quietAfter = [int]$w.Quiet.ElapsedMilliseconds
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
$w.Files['mlvapp-a.log'] = [int64]100; $w.Armed = $true; $w.Quiet.Restart()
$sw = [Diagnostics.Stopwatch]::StartNew(); $quietAtRead = -1
while ($sw.ElapsedMilliseconds -lt 15000 -and $quietAtRead -lt 0) {
    [void](Test-GuiSmokeLogProgressStalled -Watch $w)
    if ($w.Files.ContainsKey('mlvapp-b.log') -and [int64]$w.Files['mlvapp-b.log'] -ge 0) { $quietAtRead = [int]$w.Quiet.ElapsedMilliseconds }
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
$ProcessTimeoutMs = 3600000; $NoProgressTimeoutMs = 0; $LegHardLimitAllowanceMs = 1000
$derivedProcessTimeoutMs = 2000
$HostLoadSampleIntervalMs = [int]$env:GS_SAMPLE_MS; $HostLoadTopProcessCount = 3
$hostLoadDuringSamples = [System.Collections.Generic.List[object]]::new()
$logRoot = Join-Path $env:GS_TMP 'legdeadline'; New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$env:GS_LOG = Join-Path $logRoot 'mlvapp-1.log'
$s = New-Stub $env:GS_STUB
$process = $s.p; $stdoutTask = $s.o; $stderrTask = $s.e
. ([scriptblock]::Create($env:GS_DERIVE))
$hardLimitMs = [int64]$effectiveProcessTimeoutMs
$sw = [Diagnostics.Stopwatch]::StartNew()
. ([scriptblock]::Create($env:GS_WAIT))
$waitedMs = [int]$sw.ElapsedMilliseconds
$alive = -not $process.HasExited
if ($alive) { $process.Kill($true) }
[pscustomobject]@{ hardLimitMs = $hardLimitMs; waitedMs = $waitedMs; timedOut = [bool]$processBoundary.timedOut; outcome = [string]$processBoundary.outcome
    timeoutMs = [int]$processBoundary.timeoutMs; exitCode = [int]$processBoundary.exitCode; alive = $alive; samples = $hostLoadDuringSamples.Count } | ConvertTo-Json -Compress
"""

_NEVER_EXITS_NO_LOG = "Start-Sleep -Seconds 20"
_NEVER_EXITS_GROWING_LOG = "1..40 | ForEach-Object { Add-Content -LiteralPath $env:GS_LOG -Value 'tick'; Start-Sleep -Milliseconds 500 }"


@unittest.skipUnless(PWSH, "pwsh not on PATH")
class LegDerivedHardDeadline(unittest.TestCase):
    """VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r6 (sol r5 B1; catalogue B-03): the GUI process has a hard deadline derived from its LEG (leg budget plus a
    stated setup/teardown allowance, never above the job's -ProcessTimeoutMs), so a short leg under a one-hour job timeout is ended at the leg's
    deadline whether its log never appeared (the watchdog is unarmed) or keeps growing (its quiet clock keeps restarting)."""

    def _run_runner_wiring(self, stub: str, sample_ms: int) -> dict:
        return run_ps(_RUNNER_HARNESS, GS_STUB=stub, GS_SAMPLE_MS=str(sample_ms),
                      GS_DERIVE=_runner_snippet(*_DERIVE_REGION), GS_WAIT=_runner_snippet(*_WAIT_REGION))

    def _assert_ended_at_the_leg_deadline(self, out: dict) -> None:
        # the observed behaviour first: the stub never exits within 20 s, so only a leg-derived deadline can end it sooner
        self.assertLess(out["waitedMs"], 12000, "the process outlived the leg-derived deadline: the job timeout is still its only bound")
        self.assertTrue(out["timedOut"], "the process is ended at the leg-derived deadline, not left to the one-hour job timeout")
        self.assertFalse(out["alive"], "the owned tree is killed")
        self.assertEqual(124, out["exitCode"])
        self.assertEqual(3000, out["hardLimitMs"], "leg budget 2000 ms + 1000 ms allowance, under a 3600000 ms job timeout")
        self.assertEqual(3000, out["timeoutMs"], "the receipt names the leg-derived whole budget")

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


if __name__ == "__main__":
    unittest.main()
