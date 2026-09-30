<#
.SYNOPSIS
    Produce a REFERENCE FRAME CANDIDATE: one presented frame plus the full identity of what
    produced it. Emits reference-frame-candidate.v1. It does NOT promote anything.

.DESCRIPTION
    WHY THIS EXISTS.
    tools/gates/output-budget.json has carried
        "baseline": { "status": "pending_instrumented_known_good_bridge",
                      "commit": null, "executableSha256": null, "artifact": null }
    since 2026-08-18. output_budget.py's own validator says a promoted baseline needs
    status "reviewed_instrumented_known_good" plus a 40-hex commit, a 64-hex executableSha256
    and a nonempty artifact identifier. The gate machinery was built and never bound - the
    missing piece is the BRIDGE named in that status string, which is this script.

    WHAT IT DELIBERATELY WILL NOT DO.
    It never writes "reviewed_instrumented_known_good". The contract's own word is REVIEWED, and
    an actor that promotes its own capture to reviewed-known-good has granted itself the review.
    This emits status "candidate-unreviewed" and stops. A reviewer promotes it, or nobody does.

    WHY A CAPTURE WITHOUT IDENTITY IS WORTHLESS.
    On 2026-09-03 a single frame with no reference produced a confident "blue cast regression"
    verdict that a reference arm then refuted - the older build measured MARGINALLY BLUER on the
    same clip. A frame is evidence only when you can say exactly which binary, which clip, and
    which settings produced it, so the manifest carries all three by HASH, not by name.

.NOTES
    ASCII-only by project convention. Run ON the bench that has the GPU and the clips.
    NEVER screen-capture the bench: this uses the app's OWN --screenshot-output, because the
    bench is a machine its owner uses interactively and a desktop grab captures their screen,
    not the application. (Rule earned the hard way, 2026-09-03.)
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$Clip,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$Commit = '',
    [string]$ProfileId = 'shipping-default',
    [int]$Seconds = 30,
    [int]$SettleMs = 3000,
    # PIN THE FRAME. Measured 2026-09-03: five builds captured with --loop and a wall-clock
    # --seconds bound landed on FIVE DIFFERENT FRAMES of the clip (mean|luma diff| 10.2..27.3,
    # max 198 between consecutive arms). Whole-frame COLOUR statistics survive that; any SPATIAL
    # statistic does not, and a col:row striping ratio scattered 1.63..17.46 with its own control
    # moving in lockstep - an uncontrolled measurement wearing decimal places.
    # --presented-frames stops after exactly N FRESH presented frames ("--seconds remains a
    # fail-closed timeout", main.cpp:1193-1198), so the stop point is a frame index rather than a
    # race against machine speed. 0 restores the old time-based behaviour.
    [int]$PresentedFrames = 24,
    # --presented-frames pins the COUNT of presented frames, NOT the timeline position. With
    # drop-frame pacing on, the timeline advances by wall clock, so a slower build reaches a LATER
    # timeline frame by the same count - and two builds then capture different moments while both
    # honouring the pin. Measured 2026-09-03: same binary twice = byte-identical, but base vs
    # candidate differed by mean|px| 28 across 99.92% of pixels, which is a different-frame
    # signature, not a colour change. 'off' makes presented-frame N equal timeline frame N.
    [ValidateSet('off','on','persisted')][string]$DropFrameMode = 'off',
    [hashtable]$ExtraEnv = @{}
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

foreach ($p in @($Exe, $Clip)) {
    if (-not (Test-Path -LiteralPath $p)) { Write-Output "CAPTURE: CANNOT-DETERMINE - missing $p"; exit 3 }
}

# The SHIPPING DEFAULT is the absence of overrides, so this profile sets none. Only telemetry
# and unattended flags, which change what is PRINTED, not what is rendered.
$env:MLVAPP_PLAYBACK_PHASE3_UNATTENDED = '1'
$env:MLVAPP_PLAYBACK_SMOKE_TELEMETRY   = '1'
$env:MLVAPP_INTERACTIVE_TRACE          = '1'
# MLVApp writes its telemetry to a LOG FILE, not to stderr: playback_smoke.summary carries
# presented_fps (MainWindow.cpp:25179) via qInfo(), and on a Windows GUI-subsystem app that does
# NOT reach the redirected stderr -- every arm of the 2026-09-04 scale sweep produced a 0-byte
# capture.err.txt and no rate estimator at all. run-release-gui-smoke.ps1 has always known this:
# it sets MLVAPP_CRASH_FORENSICS_LOG_DIR and then reads mlvapp-*.log out of it. This harness
# never did, so a capture could measure colour but never a rate.
$env:MLVAPP_CRASH_FORENSICS_LOG_DIR    = $OutDir

$applied = @('MLVAPP_PLAYBACK_PHASE3_UNATTENDED','MLVAPP_PLAYBACK_SMOKE_TELEMETRY','MLVAPP_INTERACTIVE_TRACE','MLVAPP_CRASH_FORENSICS_LOG_DIR')
foreach ($k in $ExtraEnv.Keys) { Set-Item -Path ("env:" + $k) -Value ([string]$ExtraEnv[$k]); $applied += $k }

# PLAYBACK-CLIP-LENGTH-ENFORCE-1 (owner rule 2026-09-30): this launches the app directly, so it runs the
# same clip-length gate run-release-gui-smoke.ps1 does (>= 20 s of footage, window never exceeds it).
# A pinned-frame capture (-PresentedFrames N) has no wall-clock window (-Seconds is only its fail-closed
# timeout), so only the 20 s floor applies there. Exit 41 CLIP_TOO_SHORT / 42 CLIP_LENGTH_UNKNOWN.
. (Join-Path $PSScriptRoot 'gui-smoke-clip-length.ps1')
$clipLengthGate = Test-GuiSmokeClipLength -Path $Clip -WindowSeconds $(if ($PresentedFrames -gt 0) { 0 } else { $Seconds })
if ($clipLengthGate.verdict -ne 'OK') {
    Write-Output "CAPTURE: REFUSED - $($clipLengthGate.message)"
    exit $(if ($clipLengthGate.verdict -eq 'CLIP_TOO_SHORT') { 41 } else { 42 })
}

$shot = Join-Path $OutDir 'reference-frame.png'
# --loop is DELIBERATELY ABSENT when the frame is pinned: looping wraps the timeline and
# reintroduces exactly the which-frame ambiguity --presented-frames exists to remove.
$playArgs = @('--gui-smoke-playback','--input',$Clip,'--scope','none','--no-zebras',
              '--seconds',[string]$Seconds,'--settle-ms',[string]$SettleMs,'--start-frame','0','--drop-frame-mode',$DropFrameMode)
if ($PresentedFrames -gt 0) { $playArgs += @('--presented-frames',[string]$PresentedFrames) }
# PLAYBACK-CLIP-LENGTH-ENFORCE-1 (owner rule 2026-09-30): the old time-based fallback passed --loop.
# Looping is never allowed; a time-based capture plays once over a window the clip outlasts.
$playArgs += @('--screenshot-output',$shot)
$proc = Start-Process -FilePath $Exe -NoNewWindow -PassThru -Wait `
    -ArgumentList $playArgs `
    -RedirectStandardOutput (Join-Path $OutDir 'capture.out.txt') `
    -RedirectStandardError  (Join-Path $OutDir 'capture.err.txt')

# A FAILED LAUNCH LEAVES $proc NULL and every downstream check silently passes against nothing.
# Measured 2026-09-03: an exe that died 0xC0000135 in the loader still produced a manifest full of
# stale numbers because nothing asserted the process had started.
if ($null -eq $proc -or $null -eq $proc.ExitCode) {
    Write-Output "CAPTURE: FAILED - process-never-started ($Exe)"; exit 4
}
if ($proc.ExitCode -ne 0) { Write-Output "CAPTURE: FAILED - exe exited $($proc.ExitCode)"; exit 4 }
if (-not (Test-Path -LiteralPath $shot)) { Write-Output "CAPTURE: FAILED - no frame written"; exit 5 }

$img = Get-Item -LiteralPath $shot
# A degenerate grab is a REAL outcome, not a pass: on the GL-window path this same flag yielded a
# 150x45 / 0.2 KB image because the embedded widget was not the surface being drawn to.
if ($img.Length -lt 51200) {
    Write-Output ("CAPTURE: FAILED - frame is {0} bytes; a degenerate grab is not a reference" -f $img.Length)
    exit 6
}

if (-not $Commit) {
    $Commit = (& git -C (Split-Path $PSScriptRoot -Parent | Split-Path -Parent) rev-parse HEAD 2>$null)
    if ($Commit) { $Commit = $Commit.Trim() }
}

# The persisted APPLICATION configuration the run inherited. The harness has always recorded
# its own arguments thoroughly, but never this - so two candidate files could differ in
# playback path, debayer or caching with nothing in the record to show it. On 2026-09-03 that
# gap let a false claim about which code path the captures exercised stand for a full
# iteration; the value cost one probe to read. QSettings(UserScope,"magiclantern.MLVApp",
# "MLVApp") maps to HKCU on Windows (MainWindow.cpp restore/save of these exact keys).
$appSettings = [ordered]@{ hive = 'HKCU:\Software\magiclantern.MLVApp\MLVApp'; user = $env:USERNAME }
try {
    if (Test-Path $appSettings.hive) {
        $k = Get-ItemProperty -Path $appSettings.hive -ErrorAction Stop
        foreach ($n in @('playbackProcessingSubset','playbackDebayerMode','caching','resizeEnable','resizeWidth')) {
            $appSettings[$n] = if ($k.PSObject.Properties.Name -contains $n) { [string]$k.$n } else { '(unset)' }
        }
        $appSettings.present = $true
    } else {
        # Absent hive is NOT the same as defaults: it means this user has never run the GUI, so
        # the app will apply its own compiled defaults. Say which, rather than implying a reading.
        $appSettings.present = $false
        $appSettings.note = 'no hive for this user - app will use compiled defaults, values NOT read'
    }
} catch {
    $appSettings.present = 'error'
    $appSettings.note = ('could not read: ' + $_.Exception.Message)
}

$manifest = [ordered]@{
    schema      = 'mlvapp.reference-frame-candidate.v1'
    # NOT "reviewed_instrumented_known_good". This script cannot review its own output.
    status      = 'candidate-unreviewed'
    promotionRequires = 'a reviewer sets output-budget.json baseline.status=reviewed_instrumented_known_good with these identity fields'
    capturedUtc = (Get-Date).ToUniversalTime().ToString('o')
    host        = $env:COMPUTERNAME
    profileId   = $ProfileId
    commit      = $Commit
    executable  = [ordered]@{ path = $Exe; sha256 = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash; bytes = (Get-Item -LiteralPath $Exe).Length }
    clip        = [ordered]@{ path = $Clip; sha256 = (Get-FileHash -LiteralPath $Clip -Algorithm SHA256).Hash; bytes = (Get-Item -LiteralPath $Clip).Length }
    artifact    = [ordered]@{ path = $shot; sha256 = (Get-FileHash -LiteralPath $shot -Algorithm SHA256).Hash; bytes = $img.Length }
    appSettings = $appSettings
    settings    = [ordered]@{
        seconds         = $Seconds
        settleMs        = $SettleMs
        presentedFrames = $PresentedFrames
        dropFrameMode   = $DropFrameMode
        # Records WHY the frame is (or is not) comparable across builds, so a reader never has to
        # infer it from the flag list.
        framePinned     = ($PresentedFrames -gt 0)
        spatialComparable = if ($PresentedFrames -gt 0 -and $DropFrameMode -eq 'off') { $true }
                            elseif ($PresentedFrames -gt 0) { 'COUNT pinned but pacing ON - timeline position may differ ACROSS builds' }
                            else { 'NO - time-bounded capture lands on an arbitrary frame; colour only' }
        scopeFlags      = '--scope none --no-zebras'
        envApplied      = $applied
        appLog          = (Get-ChildItem -LiteralPath $OutDir -Filter 'mlvapp-*.log' -ErrorAction SilentlyContinue |
                           Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1 -ExpandProperty Name)
    }
    exitCode    = $proc.ExitCode
}
$mf = Join-Path $OutDir 'reference-frame-candidate.json'
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $mf -Encoding utf8
Write-Output ("CAPTURE: CANDIDATE commit={0} exe={1} frame={2} bytes={3}" -f `
    $Commit.Substring(0,[Math]::Min(12,$Commit.Length)), $manifest.executable.sha256.Substring(0,12),
    $manifest.artifact.sha256.Substring(0,12), $img.Length)
Write-Output "CAPTURE: status=candidate-unreviewed (this script does not promote) -> $mf"
exit 0
