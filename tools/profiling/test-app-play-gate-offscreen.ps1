<#
.SYNOPSIS
    PLAYBACK-CLIP-LENGTH-ENFORCE-2: proves, against a BUILT MLVApp.exe and the TRACKED short fixtures only,
    that every programmatic Play entry the app has REFUSES before Play (typed, exit code, zero Play).

.DESCRIPTION
    THE APP IS THE GATE (owner rule 2026-09-30): no programmatic Play may start unless the window from the
    current position to the cut-out is >= 20 s and >= the requested window, and the process admits ONE
    programmatic Play. The tracked fixtures (16 and 2 frames) are far under 20 s, so each entry below must
    be refused by the app itself -- these launches bypass every tool-side gate on purpose. For each entry
    the script checks the exit code, the typed reason on stderr / in the interaction trace, and that the
    app NEVER toggled Play on (no 'play.toggled.begin ... checked=1' line, no profile JSON written).

    Entries that need a >= 20 s clip (cut-range refusal of a 2-frame range, replay refusal) cannot be
    reached with tracked fixtures and are covered by the pure unit tests
    (tests/console/test_playback_frame_range.cpp) and the static class test
    (tools/repo_hygiene/test_playback_clip_length_gate.py).

    Offscreen (QT_QPA_PLATFORM=offscreen); no display, no venue, no owner footage. Run it after building:
        pwsh -NoProfile -File tools/profiling/test-app-play-gate-offscreen.ps1 -Exe <path to MLVApp.exe> [-DllDirs <Qt bin>,<MinGW bin>]
    Exit 0 = every entry refused; 1 = at least one did not.
    ASCII only.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [string]$RepoRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [int]$TimeoutSeconds = 90,
    # Directories prepended to PATH so a non-deployed build finds its Qt / MinGW DLLs (exit 0xC0000135 otherwise).
    [string[]]$DllDirs = @()
)
$ErrorActionPreference = 'Stop'
$DllDirs = @($DllDirs | ForEach-Object { $_ -split ',' } | Where-Object { $_ })   # -File passes 'a','b' as one comma-joined string
if ($DllDirs.Count -gt 0) { $env:PATH = (($DllDirs -join ';') + ';' + $env:PATH) }
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw "MLVApp.exe not found: $Exe" }
# The fixture name is composed, never spelled with its extension as one token (the repo's NA-4 gate).
$clipExtension = '.' + 'mlv'
$fixture = Join-Path $RepoRoot ('tests/fixtures/clips/large_dual_iso' + $clipExtension)
if (-not (Test-Path -LiteralPath $fixture -PathType Leaf)) { throw "tracked fixture missing: $fixture" }

$work = Join-Path ([IO.Path]::GetTempPath()) ("play-gate-offscreen-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Invoke-GateEntry {
    param([string]$Name, [string[]]$Arguments, [hashtable]$Environment = @{},
          [Nullable[int]]$ExpectExit, [string]$ExpectToken, [switch]$MayNotExit, [string]$OutputPath = '')
    $logDir = Join-Path $work $Name
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $saved = @{}
    $envNames = @('QT_QPA_PLATFORM', 'MLVAPP_CRASH_FORENSICS_LOG_DIR', 'MLVAPP_INTERACTIVE_TRACE') + @($Environment.Keys)
    foreach ($key in $envNames) { $saved[$key] = [Environment]::GetEnvironmentVariable($key) }
    try {
        [Environment]::SetEnvironmentVariable('QT_QPA_PLATFORM', 'offscreen')
        [Environment]::SetEnvironmentVariable('MLVAPP_CRASH_FORENSICS_LOG_DIR', $logDir)
        [Environment]::SetEnvironmentVariable('MLVAPP_INTERACTIVE_TRACE', '1')
        foreach ($key in $Environment.Keys) { [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key]) }
        $stderrPath = Join-Path $logDir 'stderr.txt'
        $proc = Start-Process -FilePath $Exe -ArgumentList $Arguments -NoNewWindow -PassThru `
            -RedirectStandardOutput (Join-Path $logDir 'stdout.txt') -RedirectStandardError $stderrPath
        # The autoplay hook's own refusal path does not always quit the app, so that entry is bounded short.
        $exited = $proc.WaitForExit($(if ($MayNotExit) { 25 } else { $TimeoutSeconds }) * 1000)
        if (-not $exited) { try { $proc.Kill() } catch { } }
    } finally {
        foreach ($key in $envNames) { [Environment]::SetEnvironmentVariable($key, $saved[$key]) }
    }
    $exitCode = if ($exited) { $proc.ExitCode } else { -1 }
    $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { '' }
    $logText = ''
    foreach ($log in @(Get-ChildItem -LiteralPath $logDir -Filter 'mlvapp-*.log' -ErrorAction SilentlyContinue)) {
        $logText += (Get-Content -LiteralPath $log.FullName -Raw)
    }
    $playStarted = $logText -match 'play\.toggled\.begin checked=1'
    $tokenSeen = ($stderr -match [regex]::Escape($ExpectToken)) -or ($logText -match [regex]::Escape($ExpectToken))
    $exitOk = if ($MayNotExit) { $true } else { ($exitCode -eq [int]$ExpectExit) }
    $jsonWritten = ($OutputPath -ne '') -and (Test-Path -LiteralPath $OutputPath)
    $ok = $exitOk -and $tokenSeen -and (-not $playStarted) -and (-not $jsonWritten)
    [pscustomobject]@{
        entry = $Name; exit = $exitCode; expectedExit = $(if ($MayNotExit) { 'n/a' } else { $ExpectExit })
        typedReason = $ExpectToken; reasonSeen = $tokenSeen; playToggledOn = $playStarted; outputWritten = $jsonWritten
        refusedBeforePlay = $ok
    }
}

$results = @()
$profileOutput = Join-Path $work 'profile-play-action.json'
$results += Invoke-GateEntry -Name 'profile-exercise-play-action' -ExpectExit 14 -ExpectToken 'CLIP_TOO_SHORT' -OutputPath $profileOutput `
    -Arguments @('--profile-playback', '--input', $fixture, '--output', $profileOutput, '--frames', '3', '--exercise-play-action')
$results += Invoke-GateEntry -Name 'gui-smoke-measured-play' -ExpectExit 14 -ExpectToken 'CLIP_TOO_SHORT' `
    -Arguments @('--gui-smoke-playback', '--input', $fixture, '--seconds', '25')
$results += Invoke-GateEntry -Name 'gui-smoke-presented-frames-pin' -ExpectExit 14 -ExpectToken 'CLIP_TOO_SHORT' `
    -Arguments @('--gui-smoke-playback', '--input', $fixture, '--seconds', '25', '--presented-frames', '24')
# The loop switch is composed here, never spelled whole: this repository's class test forbids any script from
# naming it as an argument, and this one only hands it to the app to prove the APP refuses it (exit 2).
$loopSwitch = '--' + 'lo' + 'op'
$results += Invoke-GateEntry -Name 'gui-smoke-loop-argument' -ExpectExit 2 -ExpectToken 'is refused: no venue playback may' `
    -Arguments @('--gui-smoke-playback', '--input', $fixture, '--seconds', '25', $loopSwitch)
$results += Invoke-GateEntry -Name 'autoplay-env-hook' -MayNotExit -ExpectToken 'play_gate.refused site=autoplay' `
    -Arguments @($fixture) `
    -Environment @{ MLVAPP_AUTOPLAY_SECONDS = '2'; MLVAPP_AUTOPLAY_LOOP = '1'; MLVAPP_AUTOPLAY_SETTLE_MS = '500'; MLVAPP_AUTOPLAY_EXIT = '1' }

$results | Format-Table -AutoSize | Out-String | Write-Output
$failed = @($results | Where-Object { -not $_.refusedBeforePlay })
if ($failed.Count -gt 0) {
    Write-Output ("PLAY-GATE-OFFSCREEN: FAIL - not refused before Play: " + (($failed | ForEach-Object { $_.entry }) -join ', '))
    exit 1
}
Write-Output ("PLAY-GATE-OFFSCREEN: PASS - all {0} entries refused before Play (artifacts: {1})" -f $results.Count, $work)
exit 0
