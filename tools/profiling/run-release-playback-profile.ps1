param(
    [string]$RepoRoot = ".",
    [string]$ExePath = "",
    [Alias("Input")]
    [string]$ClipPath = "",
    [string]$Output = "",
    [int]$Frames = 3,
    [int]$StartFrame = 0,
    [int]$FrameStep = 1,
    # Default to auto-thread playback so the profile wrapper reflects real
    # release behavior. Pass 1 explicitly only when you need a controlled
    # single-thread diagnostic run.
    [string]$Threads = "auto",
    [string]$Receipt = "",
    [string]$QualityMode = "",
    [string]$ScaleFactor = "",
    [string]$PreviewMode = "",
    [switch]$ShowWindow,
    [switch]$WaitForPaint,
    [string[]]$AdditionalArgs = @(),
    [string]$GpuPlaybackReconBackend = "",
    [string[]]$ExtraEnvironment = @(),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
# PLAYBACK-CLIP-LENGTH-ENFORCE-1 round 2 (sol B3): this wrapper can make the app PLAY the clip
# (--exercise-play-action, and the Look Assist settle's warm-up play) with any -AdditionalArgs it is
# handed, so it goes through the same gate as run-release-gui-smoke.ps1. A pure decode benchmark
# (--profile-playback without a play-capable option) presents no playback and is not gated.
. (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')

function Resolve-FileSystemProviderPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )
    $resolved = Resolve-Path -LiteralPath $Path
    if (-not [string]::IsNullOrWhiteSpace($resolved.ProviderPath)) {
        return $resolved.ProviderPath
    }
    return $resolved.Path
}

$root = Resolve-FileSystemProviderPath -Path $RepoRoot
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = Join-Path $root "platform\qt\build-release\release\MLVApp.exe"
}

$exe = Resolve-FileSystemProviderPath -Path $ExePath
$exeDir = Split-Path -Parent $exe
$platformDir = Join-Path $exeDir "platforms"
$qwindows = Join-Path $platformDir "qwindows.dll"
if (-not (Test-Path -LiteralPath $qwindows)) {
    throw "Release playback profiling requires $qwindows. Rebuild/deploy the release tree before profiling."
}

$previousPlatform = $env:QT_QPA_PLATFORM
$previousPlatformPluginPath = $env:QT_QPA_PLATFORM_PLUGIN_PATH
$previousPluginPath = $env:QT_PLUGIN_PATH

function Add-EnvironmentPairs {
    param(
        [object]$Target,
        [string[]]$Pairs
    )

    $expandedPairs = @()
    foreach ($rawPair in $Pairs) {
        if ([string]::IsNullOrWhiteSpace($rawPair)) {
            continue
        }

        $parts = @($rawPair -split ',')
        if ($parts.Count -gt 1 -and ($parts | Where-Object { $_.IndexOf("=") -lt 1 }).Count -eq 0) {
            $expandedPairs += $parts
        }
        else {
            $expandedPairs += $rawPair
        }
    }

    foreach ($pair in $expandedPairs) {
        if ([string]::IsNullOrWhiteSpace($pair)) {
            continue
        }

        $separatorIndex = $pair.IndexOf("=")
        if ($separatorIndex -lt 1) {
            throw "Invalid -ExtraEnvironment entry '$pair'. Use KEY=VALUE."
        }

        $key = $pair.Substring(0, $separatorIndex).Trim()
        $value = $pair.Substring($separatorIndex + 1)
        if ([string]::IsNullOrWhiteSpace($key)) {
            throw "Invalid -ExtraEnvironment entry '$pair'. The key cannot be empty."
        }

        $Target[$key] = $value
    }
}

try {
    # The user-facing Windows release deploys qwindows.dll, not qoffscreen.dll.
    # Forcing offscreen against this tree causes the Qt platform-plugin popup.
    $env:QT_QPA_PLATFORM = "windows"
    $env:QT_QPA_PLATFORM_PLUGIN_PATH = $platformDir
    $env:QT_PLUGIN_PATH = $exeDir

    # The gates run BEFORE anything launches (and before the -DryRun report), typed and path-free:
    #   exit 44 PASS_THROUGH_REFUSED   exit 41 CLIP_TOO_SHORT   exit 42 CLIP_LENGTH_UNKNOWN
    $passThroughGate = Test-GuiSmokePassThroughArguments -Arguments $AdditionalArgs -Context 'profile'
    if ($passThroughGate.verdict -ne 'OK') {
        [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-1: $($passThroughGate.message)")
        exit 44
    }
    $environmentGate = Test-GuiSmokeEnvironmentEntries -Entries $ExtraEnvironment
    if ($environmentGate.verdict -ne 'OK') {
        [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-1: $($environmentGate.message)")
        exit 44
    }
    $parentEnvironmentGate = Test-GuiSmokeParentEnvironment
    if ($parentEnvironmentGate.verdict -ne 'OK') {
        [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-2: $($parentEnvironmentGate.message)")
        exit 44
    }
    $clipLengthGate = 'NOT_PLAYING'
    if ($passThroughGate.playCapable) {
        if ([string]::IsNullOrWhiteSpace($ClipPath)) {
            [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-1: CLIP_LENGTH_UNKNOWN (reason=no_input)")
            exit 42
        }
        # ENFORCE-2: the app gates the window from the CURRENT position (--start-frame) to the cut-out at
        # >= 20 s, so the wrapper requires the same: 20 s must remain after -StartFrame.
        $profileClipGate = Test-GuiSmokeClipLength -Path (Resolve-FileSystemProviderPath -Path $ClipPath) -WindowSeconds 20 -StartFrame $StartFrame
        if ($profileClipGate.verdict -ne 'OK') {
            [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-1: $($profileClipGate.message)")
            exit (Get-GuiSmokeGateExitCode -Verdict $profileClipGate.verdict)
        }
        $clipLengthGate = 'OK'
    }

    if ($DryRun) {
        [pscustomobject]@{
            clipLengthGate = $clipLengthGate
            playCapable = [bool]$passThroughGate.playCapable
            exePath = $exe
            qtQpaPlatform = $env:QT_QPA_PLATFORM
            qtQpaPlatformPluginPath = $env:QT_QPA_PLATFORM_PLUGIN_PATH
            qtPluginPath = $env:QT_PLUGIN_PATH
            qwindowsExists = $true
            previousQtQpaPlatform = $previousPlatform
            gpuPlaybackReconBackend = $GpuPlaybackReconBackend
            environment = [pscustomobject]@{
                MLVAPP_GPU_PLAYBACK_RECON_BACKEND = $(if (-not [string]::IsNullOrWhiteSpace($GpuPlaybackReconBackend)) { $GpuPlaybackReconBackend } else { $null })
            }
        } | ConvertTo-Json -Depth 3
        return
    }

    if ([string]::IsNullOrWhiteSpace($ClipPath)) {
        throw "Missing -Input <clip.mlv>."
    }
    if ([string]::IsNullOrWhiteSpace($Output)) {
        throw "Missing -Output <profile.json>."
    }

    $inputPath = Resolve-FileSystemProviderPath -Path $ClipPath
    $outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
    $outputDir = Split-Path -Parent $outputPath
    if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
        New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    }

    $arguments = @(
        "--profile-playback",
        "--input", $inputPath,
        "--frames", [string]$Frames,
        "--start-frame", [string]$StartFrame,
        "--frame-step", [string]$FrameStep,
        "--output", $outputPath,
        "--threads", $Threads
    )
    if (-not [string]::IsNullOrWhiteSpace($Receipt)) {
        $arguments += @("--receipt", (Resolve-FileSystemProviderPath -Path $Receipt))
    }
    if ($ShowWindow) {
        $arguments += "--show-window"
    }
    if ($WaitForPaint) {
        $arguments += "--wait-for-paint"
    }
    if ($AdditionalArgs.Count -gt 0) {
        $arguments += $AdditionalArgs
    }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $exe
    $startInfo.WorkingDirectory = $root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = -not ($ShowWindow -or $WaitForPaint)
    foreach ($argument in $arguments) {
        [void]$startInfo.ArgumentList.Add($argument)
    }

    $envBlock = $startInfo.EnvironmentVariables
    $envBlock["MLVAPP_PLAYBACK_MAX_THREADS"] = $Threads
    if (-not [string]::IsNullOrWhiteSpace($QualityMode)) {
        $envBlock["MLVAPP_PLAYBACK_QUALITY_MODE"] = $QualityMode
    }
    elseif ($envBlock.ContainsKey("MLVAPP_PLAYBACK_QUALITY_MODE")) {
        [void]$envBlock.Remove("MLVAPP_PLAYBACK_QUALITY_MODE")
    }
    if (-not [string]::IsNullOrWhiteSpace($ScaleFactor)) {
        $envBlock["MLVAPP_PLAYBACK_SCALE_FACTOR"] = $ScaleFactor
    }
    elseif ($envBlock.ContainsKey("MLVAPP_PLAYBACK_SCALE_FACTOR")) {
        [void]$envBlock.Remove("MLVAPP_PLAYBACK_SCALE_FACTOR")
    }
    if (-not [string]::IsNullOrWhiteSpace($PreviewMode)) {
        $envBlock["MLVAPP_PLAYBACK_PREVIEW_MODE"] = $PreviewMode
        [void]$envBlock.Remove("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW")
    }
    elseif ($envBlock.ContainsKey("MLVAPP_PLAYBACK_PREVIEW_MODE")) {
        [void]$envBlock.Remove("MLVAPP_PLAYBACK_PREVIEW_MODE")
        [void]$envBlock.Remove("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW")
    }
    elseif ($envBlock.ContainsKey("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW")) {
        [void]$envBlock.Remove("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW")
    }
    if (-not [string]::IsNullOrWhiteSpace($GpuPlaybackReconBackend)) {
        $envBlock["MLVAPP_GPU_PLAYBACK_RECON_BACKEND"] = $GpuPlaybackReconBackend
    }
    else {
        [void]$envBlock.Remove("MLVAPP_GPU_PLAYBACK_RECON_BACKEND")
    }
    Add-EnvironmentPairs -Target $envBlock -Pairs $ExtraEnvironment

    $process = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $passThroughGate.playCapable) {
        # A pure decode benchmark presents no playback: nothing to prove about footage.
        $process.WaitForExit()
        exit $process.ExitCode
    }
    # PLAYBACK-CLIP-LENGTH-ENFORCE-3 round 2 (fable H3): a profile that PLAYS (--exercise-play-action / the Look Assist
    # settle) reports a result only if the app's own receipt proves its engine consumed the source frames of the window.
    # The wrapper never ends the app on a clock of its own: the wait below is the app's wall-clock ceiling for a whole
    # profile run (the Play itself ends on source-frame consumption inside the app, typed on failure); a process still
    # running past it is killed and the result is INVALID (PLAY_SAFETY_TIMEOUT). An exit code of 0 is NOT enough: a binary
    # that predates ENFORCE-3 exits 0 after a wall-clock hold, and writes no source_advanced, so it fails here.
    $profileKilled = $false
    if (-not $process.WaitForExit(3600000)) {
        $profileKilled = $true
        try { $process.Kill($true) } catch { try { $process.Kill() } catch {} }
        [void]$process.WaitForExit(5000)
    }
    if (-not $profileKilled -and $process.ExitCode -ne 0) { exit $process.ExitCode }   # the app's own typed refusal / failure
    $profileSummary = Get-GuiSmokeProfileReceiptSummary -Path $outputPath
    $profileVerdict = Get-GuiSmokeEvidencePlayVerdict -Summary $profileSummary `
        -ExitCode $(if ($profileKilled) { $null } else { $process.ExitCode }) -KilledByLauncher $profileKilled -WindowSeconds 20
    if ($profileVerdict.invalid) {
        foreach ($failure in $profileVerdict.failures) { [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-3: $failure") }
        [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-3: INVALID -- this profile is not playback evidence (exit 43).")
        exit $profileVerdict.exitCode
    }
    exit 0
}
finally {
    $env:QT_QPA_PLATFORM = $previousPlatform
    $env:QT_QPA_PLATFORM_PLUGIN_PATH = $previousPlatformPluginPath
    $env:QT_PLUGIN_PATH = $previousPluginPath
}
