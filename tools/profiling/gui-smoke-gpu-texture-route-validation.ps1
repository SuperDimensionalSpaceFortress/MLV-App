# CUDA-S4-TEXTURE-ROUTE-CLAMP-1: the GPU recon texture-present route (CUDA
# reconstruction straight to a GL texture, no per-frame CPU readback) is only
# wired for playbackScaleFactor == 1. MainWindow::effectivePlaybackScaleFactorForRequest()
# clamps the effective playback scale to 1 at that single policy source
# whenever the route would otherwise be armed, so a launch that requests the
# route (in GL-window mode -- this rig has no separate CPU-only-viewport
# switch, so requesting the route at all is the GL-window-mode signal) must
# never see any non-texture-no-readback fallback frame in
# playback_smoke.gpu_summary.

function Get-GuiSmokeGpuTextureRouteObjectPropertyValue {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    $null
}

# Mirrors the C++ *_requested_by_environment() truthiness check in
# MainWindow.cpp: the value is trimmed FIRST (MainWindow.cpp:2362-2378), and
# only then is empty, "0", or "false" (case-insensitive) treated as "not
# requested". A whitespace-padded "0" or "false" must be treated exactly as
# its untrimmed form is in C++.
function Test-GuiSmokeEnvironmentPairTruthy {
    param($Env, [string]$Key)
    if ($null -eq $Env -or -not $Env.Contains($Key)) { return $false }
    $value = ([string]$Env[$Key]).Trim()
    return -not [string]::IsNullOrEmpty($value) -and
        $value -ne "0" -and
        $value.ToLowerInvariant() -ne "false"
}

# Resolves the single value the child process actually sees for one
# environment key: an explicit launch-env override (run-release-gui-smoke.ps1
# -ExtraEnvironment, folded into $launchEnv) wins outright; otherwise, if
# run-release-gui-smoke.ps1 clears the key for this run, it is unset;
# otherwise the ambient value inherited from the parent shell passes through
# unchanged, because System.Diagnostics.ProcessStartInfo.EnvironmentVariables
# is seeded from the parent process environment and run-release-gui-smoke.ps1
# does not clear MLVAPP_GPU_PLAYBACK_RECON or
# MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT for any run
# (run-release-gui-smoke.ps1:845-890 -- neither name appears in
# $experimentalEnvironment or any other clear list).
function Get-GuiSmokeEffectiveEnvironmentPair {
    param($LaunchEnv, $ClearedEnvironment, $AmbientEnvironment, [string]$Key)
    $effective = [ordered]@{}
    if ($LaunchEnv -and $LaunchEnv.Contains($Key)) {
        $effective[$Key] = $LaunchEnv[$Key]
    }
    elseif ($ClearedEnvironment -and ($ClearedEnvironment -contains $Key)) {
        # Explicitly cleared for this run: stays unset regardless of ambient.
    }
    elseif ($AmbientEnvironment -and $AmbientEnvironment.Contains($Key)) {
        $effective[$Key] = $AmbientEnvironment[$Key]
    }
    $effective
}

function Test-GuiSmokeGpuReconTexturePresentRouteRequested {
    param($LaunchEnv, $ClearedEnvironment = @(), $AmbientEnvironment = $null)
    $reconKey = "MLVAPP_GPU_PLAYBACK_RECON"
    $texturePresentKey = "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT"
    $effectiveRecon = Get-GuiSmokeEffectiveEnvironmentPair -LaunchEnv $LaunchEnv `
        -ClearedEnvironment $ClearedEnvironment -AmbientEnvironment $AmbientEnvironment -Key $reconKey
    $effectiveTexturePresent = Get-GuiSmokeEffectiveEnvironmentPair -LaunchEnv $LaunchEnv `
        -ClearedEnvironment $ClearedEnvironment -AmbientEnvironment $AmbientEnvironment -Key $texturePresentKey
    return (Test-GuiSmokeEnvironmentPairTruthy -Env $effectiveRecon -Key $reconKey) -and
        (Test-GuiSmokeEnvironmentPairTruthy -Env $effectiveTexturePresent -Key $texturePresentKey)
}

# Returns a validation-failure string when the launch requested the GPU
# recon texture-present route but the run's playback_smoke.gpu_summary shows
# at least one frame that did not reach the no-readback gpu_texture_no_readback
# path -- every one of cpu_frames, gpu_preview_frames,
# gpu_recon_readback_frames, and gpu_texture_readback_frames counts against
# the route's contract (or the summary telemetry is missing outright);
# returns $null when the route was not requested, or was requested and every
# frame reached gpu_texture_no_readback.
function Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure {
    param($LaunchEnv, $ClearedEnvironment = @(), $GpuSummary, $AmbientEnvironment = $null)

    if (-not (Test-GuiSmokeGpuReconTexturePresentRouteRequested `
            -LaunchEnv $LaunchEnv -ClearedEnvironment $ClearedEnvironment -AmbientEnvironment $AmbientEnvironment)) {
        return $null
    }
    if ($null -eq $GpuSummary) {
        return "gpu_texture_route_readback_regression: the launch requested the GPU recon " +
               "texture-present route (MLVAPP_GPU_PLAYBACK_RECON + " +
               "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT) but no " +
               "playback_smoke.gpu_summary telemetry was logged."
    }

    $fallbackCounters = [ordered]@{
        cpu_frames                  = [long](Get-GuiSmokeGpuTextureRouteObjectPropertyValue $GpuSummary "cpu_frames")
        gpu_preview_frames          = [long](Get-GuiSmokeGpuTextureRouteObjectPropertyValue $GpuSummary "gpu_preview_frames")
        gpu_recon_readback_frames   = [long](Get-GuiSmokeGpuTextureRouteObjectPropertyValue $GpuSummary "gpu_recon_readback_frames")
        gpu_texture_readback_frames = [long](Get-GuiSmokeGpuTextureRouteObjectPropertyValue $GpuSummary "gpu_texture_readback_frames")
    }
    $totalFallback = 0L
    $breakdown = @()
    foreach ($name in $fallbackCounters.Keys) {
        $count = $fallbackCounters[$name]
        if ($count -gt 0) {
            $totalFallback += $count
            $breakdown += "$name=$count"
        }
    }
    if ($totalFallback -gt 0) {
        return "gpu_texture_route_readback_regression: $totalFallback frame(s) did not reach " +
               "gpu_texture_no_readback ($($breakdown -join ', ')) while the GPU recon " +
               "texture-present no-readback route was requested in GL-window mode; every " +
               "frame must reach gpu_texture_no_readback."
    }
    return $null
}
