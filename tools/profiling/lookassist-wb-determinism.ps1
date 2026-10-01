# lookassist-wb-determinism.ps1
# ACCEPTANCE GATE for the Look Assist auto-WB non-determinism bug
# (.claude-state/analysis/lookassist-wb-nondeterminism.md).
#
# The Look Assist auto-WB is non-deterministic run-to-run at a FIXED scale: the neutral-patch search
# and the floor-lifted safety guard fire differently on identical inputs, because the analysis renders
# through shared mlvObject cache/buffers and mutates the shared processingObject while the playback /
# cache threads run concurrently. A correct fix makes the analysis INDEPENDENT of that concurrent
# state, so the chosen WB + patch are STABLE across repeated runs.
#
# This gate runs Look Assist ON (record-only -- no screenshot needed) N times at one fixed scale and
# measures the run-to-run SPREAD of: the AWB patch (rawX/rawY), the raw pre-clamp WB (temp/tint), and
# the final applied WB (temp/tint). It also flags inconsistent firing of the floor-lifted safety guard.
# Requires an exe built WITH the env-gated MLVAPP_LOOK_ASSIST_WB_TRACE instrumentation in MainWindow.cpp.
#
# Verdict: STABLE (all spreads within tolerance AND the safety guard fires consistently) => a fix has
# made the auto-WB deterministic. UNSTABLE otherwise (the current 60130e3f baseline is UNSTABLE).
# Exit: 0 = STABLE ; 1 = UNSTABLE ; 2 = error / insufficient data.
param(
    [string]$RepoRoot = ".",
    [string]$ExePath = "",            # default: the deployed build-release exe
    [Alias("Input")]
    [string]$ClipPath = "",           # default: tests/fixtures/clips/large_dual_iso.mlv
    [int]$ScaleFactor = 2,            # FIXED scale -- determinism is measured at one scale
    [int]$Reps = 5,
    [int]$StartFrame = 10,
    [int]$Seconds = 20,       # ENFORCE-2 (owner rule 2026-09-30): the play window is >= 20 s (was 12; the runner
                              # refuses a shorter one, PLAY_WINDOW_TOO_SHORT). RULE 2026-06-26 (Layi): generous playback window. This
                              # gate is record-only + multi-rep, so doubled (not tripled) to keep the
                              # N-rep x 2-mode chain manageable while still giving MLV real play time.
    [int]$SettleMs = 4000,
    # Tolerances: a deterministic auto-WB should be essentially identical run-to-run. These are
    # deliberately tight; the current baseline blows past them by a wide margin.
    [int]$PatchTolerancePx = 8,
    [int]$RawTempToleranceK = 150,
    [int]$RawTintTolerance = 4,
    [int]$FinalTempToleranceK = 150,
    [int]$FinalTintTolerance = 4,
    [string]$OutputRoot = "",
    [switch]$SyncMode,                # run Look Assist via the synchronous path (MLVAPP_LOOK_ASSIST_SYNC=1)
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $RepoRoot).Path
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = Join-Path $root "platform\qt\build-release\release\MLVApp.exe"
}
$exe = (Resolve-Path -LiteralPath $ExePath).Path
if ([string]::IsNullOrWhiteSpace($ClipPath)) {
    $ClipPath = Join-Path $root "tests\fixtures\clips\large_dual_iso.mlv"
}
$clip = (Resolve-Path -LiteralPath $ClipPath).Path
$smoke = Join-Path $PSScriptRoot "run-release-gui-smoke.ps1"
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $root ".claude-state\profiling\lookassist-wb-determinism"
}
$OutputRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputRoot)
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

Write-Host ("WB-determinism gate: exe={0}" -f $exe)
Write-Host ("  clip={0} scale={1} reps={2} frame={3}" -f ([IO.Path]::GetFileName($clip)), $ScaleFactor, $Reps, $StartFrame)
if ($DryRun) { Write-Host "DRY-RUN (no runs performed)." -ForegroundColor Cyan; exit 0 }

# The instrumentation + interaction trace are gated by host env vars NOT in the smoke's clear-list,
# so they propagate to MLVApp via inheritance.
$env:MLVAPP_LOOK_ASSIST_WB_TRACE = "1"
$env:MLVAPP_INTERACTIVE_TRACE = "1"
if ($SyncMode) { $env:MLVAPP_LOOK_ASSIST_SYNC = "1"; Write-Host "  mode = SYNC (MLVAPP_LOOK_ASSIST_SYNC=1)" }
else { Remove-Item Env:\MLVAPP_LOOK_ASSIST_SYNC -ErrorAction SilentlyContinue; Write-Host "  mode = ASYNC (detached worker)" }

$rows = @()
for ($rep = 1; $rep -le $Reps; $rep++) {
    $repDir = Join-Path $OutputRoot ("rep{0}" -f $rep)
    New-Item -ItemType Directory -Force -Path $repDir | Out-Null
    $resultPath = Join-Path $repDir "result.json"
    $logPath = Join-Path $repDir "smoke.log"
    Write-Host ("[det] rep {0}/{1} (scale={2}) ..." -f $rep, $Reps, $ScaleFactor)
    # record-only (no -CaptureScreenshot): Look Assist still runs + the trace lands in result.json,
    # and we avoid the screenshot timing flakiness entirely.
    $smokeArgs = @(
        '-RepoRoot', $root, '-ExePath', $exe, '-Input', $clip,
        '-ScaleFactor', $ScaleFactor, '-ExpectedScaleRequest', $ScaleFactor,
        '-StartFrame', $StartFrame, '-Seconds', $Seconds, '-SettleMs', $SettleMs,
        '-Output', $resultPath
    )
    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke @smokeArgs *> $logPath
    # ENFORCE-3 r2: the runner is the receipt oracle (exit 43 = INVALID: the engine did not consume the source frames of
    # the window, or wrapped, or was paced by an override; any other non-zero = the app's typed failure). A run that is
    # not a clean pass is never a measurement, whatever result.json happens to hold.
    $smokeExit = $LASTEXITCODE
    if ($smokeExit -ne 0) {
        Write-Host ("  rep {0}: runner exit {1} (INVALID / typed failure) -- not a measurement (see {2})" -f $rep, $smokeExit, $logPath) -ForegroundColor Yellow
        $rows += [pscustomobject]@{ rep = $rep; ok = $false; reason = "runner exit $smokeExit" }
        continue
    }
    if (-not (Test-Path -LiteralPath $resultPath)) {
        Write-Host ("  rep {0}: no result.json (see {1})" -f $rep, $logPath) -ForegroundColor Yellow
        $rows += [pscustomobject]@{ rep = $rep; ok = $false; reason = "no result.json" }
        continue
    }
    $raw = Get-Content -LiteralPath $resultPath -Raw
    $worker = [regex]::Match($raw, 'WB_TRACE_WORKER preview_mode=(-?\d+) play_scale_active=(-?\d+) patch_valid=(\d+) patch_rawXY=(-?\d+)/(-?\d+) raw_wb_temp=(-?\d+) raw_wb_tint=(-?\d+)')
    $finalTemp = $null; $finalTint = $null; $safety = $null
    try {
        $obj = $raw | ConvertFrom-Json
        $vs = $obj.visualQuality.visualState
        if ($vs) { $finalTemp = [int]$vs.temperature; $finalTint = [int]$vs.tint }
        $safety = $obj.visualQuality.lookAssist.safetyWarning
    } catch {}
    if (-not $worker.Success) {
        Write-Host ("  rep {0}: no WB_TRACE_WORKER (instrumented exe? Look Assist applied?)" -f $rep) -ForegroundColor Yellow
        $rows += [pscustomobject]@{ rep = $rep; ok = $false; reason = "no WB_TRACE_WORKER"; finalTemp = $finalTemp; finalTint = $finalTint; safety = $safety }
        continue
    }
    $rows += [pscustomobject]@{
        rep         = $rep
        ok          = $true
        previewMode = [int]$worker.Groups[1].Value
        playScale   = [int]$worker.Groups[2].Value
        patchValid  = [int]$worker.Groups[3].Value
        patchX      = [int]$worker.Groups[4].Value
        patchY      = [int]$worker.Groups[5].Value
        rawTemp     = [int]$worker.Groups[6].Value
        rawTint     = [int]$worker.Groups[7].Value
        finalTemp   = $finalTemp
        finalTint   = $finalTint
        safety      = $safety
    }
    Write-Host ("  rep {0}: patch {1}/{2}  raw {3}/{4}  final {5}/{6}  safety={7}" -f `
        $rep, $worker.Groups[4].Value, $worker.Groups[5].Value, $worker.Groups[6].Value, `
        $worker.Groups[7].Value, $finalTemp, $finalTint, ($safety ?? "none"))
}

$good = @($rows | Where-Object { $_.ok })
if ($good.Count -lt 2) {
    Write-Host "ERROR: fewer than 2 valid reps -- cannot assess determinism." -ForegroundColor Red
    $report = [ordered]@{ schema = "mlvapp.lookassist-wb-determinism.v1"; verdict = "ERROR"; reason = "insufficient valid reps"; reps = $rows }
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputRoot "determinism-verdict.json") -Encoding UTF8
    exit 2
}

function Spread($vals) {
    $v = @($vals | Where-Object { $null -ne $_ })
    if ($v.Count -eq 0) { return $null }
    return ([int](($v | Measure-Object -Maximum).Maximum) - [int](($v | Measure-Object -Minimum).Minimum))
}
$patchXSpread = Spread ($good | ForEach-Object { $_.patchX })
$patchYSpread = Spread ($good | ForEach-Object { $_.patchY })
$rawTempSpread = Spread ($good | ForEach-Object { $_.rawTemp })
$rawTintSpread = Spread ($good | ForEach-Object { $_.rawTint })
$finalTempSpread = Spread ($good | ForEach-Object { $_.finalTemp })
$finalTintSpread = Spread ($good | ForEach-Object { $_.finalTint })
$safetySet = @($good | ForEach-Object { if ($_.safety) { [string]$_.safety } else { "none" } } | Select-Object -Unique)
$safetyConsistent = ($safetySet.Count -le 1)

$checks = [ordered]@{
    patchX    = @{ spread = $patchXSpread; tol = $PatchTolerancePx; pass = ($null -ne $patchXSpread -and $patchXSpread -le $PatchTolerancePx) }
    patchY    = @{ spread = $patchYSpread; tol = $PatchTolerancePx; pass = ($null -ne $patchYSpread -and $patchYSpread -le $PatchTolerancePx) }
    rawTemp   = @{ spread = $rawTempSpread; tol = $RawTempToleranceK; pass = ($null -ne $rawTempSpread -and $rawTempSpread -le $RawTempToleranceK) }
    rawTint   = @{ spread = $rawTintSpread; tol = $RawTintTolerance; pass = ($null -ne $rawTintSpread -and $rawTintSpread -le $RawTintTolerance) }
    finalTemp = @{ spread = $finalTempSpread; tol = $FinalTempToleranceK; pass = ($null -ne $finalTempSpread -and $finalTempSpread -le $FinalTempToleranceK) }
    finalTint = @{ spread = $finalTintSpread; tol = $FinalTintTolerance; pass = ($null -ne $finalTintSpread -and $finalTintSpread -le $FinalTintTolerance) }
    safety    = @{ values = $safetySet; pass = $safetyConsistent }
}
$allPass = $safetyConsistent
foreach ($k in @('patchX','patchY','rawTemp','rawTint','finalTemp','finalTint')) { if (-not $checks[$k].pass) { $allPass = $false } }
$verdict = if ($allPass) { "STABLE" } else { "UNSTABLE" }

$report = [ordered]@{
    schema = "mlvapp.lookassist-wb-determinism.v1"
    verdict = $verdict
    capturedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    exe = $exe
    clip = $clip
    scaleFactor = $ScaleFactor
    mode = $(if ($SyncMode) { 'sync' } else { 'async' })
    reps = $Reps
    validReps = $good.Count
    checks = $checks
    tolerances = [ordered]@{ patchPx = $PatchTolerancePx; rawTempK = $RawTempToleranceK; rawTint = $RawTintTolerance; finalTempK = $FinalTempToleranceK; finalTint = $FinalTintTolerance }
    rows = $rows
    interpretation = "Look Assist auto-WB run-to-run at a FIXED scale. STABLE means a fix has made the analysis independent of concurrent playback/cache/processing state. The current 60130e3f baseline is UNSTABLE. patchX/Y are the AWB neutral-patch raw coords; raw* is the pre-clamp findMlvWhiteBalance WB; final* is the applied WB (clamp/fallback can mask raw instability, so the patch/raw checks are the strict ones)."
}
$reportPath = Join-Path $OutputRoot "determinism-verdict.json"
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportPath -Encoding UTF8

Write-Host ""
Write-Host ("spreads  patchX {0} / patchY {1} (tol {2}px)  rawWB {3}K/{4} (tol {5}K/{6})  finalWB {7}K/{8} (tol {9}K/{10})" -f `
    $patchXSpread, $patchYSpread, $PatchTolerancePx, $rawTempSpread, $rawTintSpread, $RawTempToleranceK, $RawTintTolerance, `
    $finalTempSpread, $finalTintSpread, $FinalTempToleranceK, $FinalTintTolerance)
Write-Host ("safety guard values across reps: {0} ({1})" -f ($safetySet -join ', '), $(if ($safetyConsistent) { 'consistent' } else { 'INCONSISTENT' }))
$color = if ($verdict -eq 'STABLE') { 'Green' } else { 'Red' }
Write-Host ("VERDICT: {0}  (report: {1})" -f $verdict, $reportPath) -ForegroundColor $color
if ($verdict -eq 'STABLE') { exit 0 } else { exit 1 }
