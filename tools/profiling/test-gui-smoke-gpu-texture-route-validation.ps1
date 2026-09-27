$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function New-GpuSummary([hashtable]$Overrides = @{}) {
    $summary = [pscustomobject]@{
        session                     = 1L
        cpu_frames                  = 0L
        gpu_preview_frames          = 0L
        gpu_recon_readback_frames   = 0L
        gpu_texture_readback_frames = 0L
        gpu_texture_no_readback_frames = 0L
    }
    foreach ($key in $Overrides.Keys) { $summary.$key = $Overrides[$key] }
    $summary
}

function New-LaunchEnv([hashtable]$Overrides = @{}) {
    $env = [ordered]@{}
    foreach ($key in $Overrides.Keys) { $env[$key] = $Overrides[$key] }
    $env
}

# --- Test-GuiSmokeEnvironmentPairTruthy -------------------------------------

Assert-True (-not (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv) -Key 'MLVAPP_GPU_PLAYBACK_RECON')) `
    'An unset key must not be truthy.'
Assert-True (-not (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = '0' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON')) `
    '"0" must not be truthy.'
Assert-True (-not (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = 'FALSE' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON')) `
    '"FALSE" (any case) must not be truthy.'
Assert-True (-not (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = '  ' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON')) `
    'Whitespace-only must not be truthy.'
Assert-True (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = '1' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON') `
    '"1" must be truthy.'
Assert-True (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = 'cuda' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON') `
    'A non-empty, non-"0"/"false" value must be truthy.'

# sol minor / fable minor 2: mirror C++'s trim-before-compare exactly
# (MainWindow.cpp:2362-2378) -- a padded "0" or "false" must trim down to the
# falsy literal, and a padded genuine value must still read truthy.
Assert-True (-not (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = ' 0 ' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON')) `
    '" 0 " must not be truthy (C++ trims before comparing to "0").'
Assert-True (-not (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = ' false ' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON')) `
    '" false " must not be truthy (C++ trims before the case-insensitive "false" compare).'
Assert-True (-not (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = "`t FALSE `n" }) -Key 'MLVAPP_GPU_PLAYBACK_RECON')) `
    'Tab/newline-padded "FALSE" must not be truthy.'
Assert-True (Test-GuiSmokeEnvironmentPairTruthy -Env (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = ' 1 ' }) -Key 'MLVAPP_GPU_PLAYBACK_RECON') `
    '" 1 " must be truthy (trims down to the genuine value "1").'

# --- Get-GuiSmokeEffectiveEnvironmentPair -----------------------------------
# fable minor 1: the route-requested decision must use the effective child
# environment (launch-env overrides plus inherited ambient values that
# run-release-gui-smoke.ps1 does not clear, ~:845-890), not only the
# launch-env overrides.

Assert-True (-not (Get-GuiSmokeEffectiveEnvironmentPair -LaunchEnv (New-LaunchEnv) `
    -ClearedEnvironment @() -AmbientEnvironment @{} -Key 'MLVAPP_GPU_PLAYBACK_RECON').Contains('MLVAPP_GPU_PLAYBACK_RECON')) `
    'No launch-env override, no ambient value: key stays unset.'

$ambientOnlyEffective = Get-GuiSmokeEffectiveEnvironmentPair -LaunchEnv (New-LaunchEnv) `
    -ClearedEnvironment @() -AmbientEnvironment @{ MLVAPP_GPU_PLAYBACK_RECON = '1' } -Key 'MLVAPP_GPU_PLAYBACK_RECON'
Assert-True ($ambientOnlyEffective['MLVAPP_GPU_PLAYBACK_RECON'] -eq '1') `
    'An ambient value not overridden or cleared must pass through as the effective value.'

$overrideWinsEffective = Get-GuiSmokeEffectiveEnvironmentPair -LaunchEnv (New-LaunchEnv @{ MLVAPP_GPU_PLAYBACK_RECON = '0' }) `
    -ClearedEnvironment @() -AmbientEnvironment @{ MLVAPP_GPU_PLAYBACK_RECON = '1' } -Key 'MLVAPP_GPU_PLAYBACK_RECON'
Assert-True ($overrideWinsEffective['MLVAPP_GPU_PLAYBACK_RECON'] -eq '0') `
    'An explicit launch-env override must win over the ambient value.'

Assert-True (-not (Get-GuiSmokeEffectiveEnvironmentPair -LaunchEnv (New-LaunchEnv) `
    -ClearedEnvironment @('MLVAPP_GPU_PLAYBACK_RECON') `
    -AmbientEnvironment @{ MLVAPP_GPU_PLAYBACK_RECON = '1' } -Key 'MLVAPP_GPU_PLAYBACK_RECON').Contains('MLVAPP_GPU_PLAYBACK_RECON')) `
    'A key that run-release-gui-smoke.ps1 clears for this run must stay unset even with an ambient value present.'

# --- Test-GuiSmokeGpuReconTexturePresentRouteRequested ----------------------

Assert-True (-not (Test-GuiSmokeGpuReconTexturePresentRouteRequested -LaunchEnv (New-LaunchEnv))) `
    'Neither flag set: route not requested.'
Assert-True (-not (Test-GuiSmokeGpuReconTexturePresentRouteRequested -LaunchEnv (New-LaunchEnv @{
    MLVAPP_GPU_PLAYBACK_RECON = '1'
}))) 'Recon alone (no texture-present flag): route not requested.'
Assert-True (-not (Test-GuiSmokeGpuReconTexturePresentRouteRequested -LaunchEnv (New-LaunchEnv @{
    MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'
}))) 'Texture-present alone (no recon flag): route not requested.'
Assert-True (Test-GuiSmokeGpuReconTexturePresentRouteRequested -LaunchEnv (New-LaunchEnv @{
    MLVAPP_GPU_PLAYBACK_RECON = '1'
    MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'
})) 'Both flags set: route requested.'

# Ambient-only route request: neither flag was passed via -ExtraEnvironment,
# but both are set in the ambient parent shell and neither is on the clear
# list -- run-release-gui-smoke.ps1 still arms the route in-app, so the
# validator must still see it as requested.
Assert-True (Test-GuiSmokeGpuReconTexturePresentRouteRequested -LaunchEnv (New-LaunchEnv) `
    -ClearedEnvironment @() -AmbientEnvironment @{
        MLVAPP_GPU_PLAYBACK_RECON = '1'
        MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'
    }) 'Both flags ambient-only (uncleared, unoverridden): route requested.'

# A cleared ambient flag must not arm the route.
Assert-True (-not (Test-GuiSmokeGpuReconTexturePresentRouteRequested -LaunchEnv (New-LaunchEnv) `
    -ClearedEnvironment @('MLVAPP_GPU_PLAYBACK_RECON') -AmbientEnvironment @{
        MLVAPP_GPU_PLAYBACK_RECON = '1'
        MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'
    })) 'Recon flag cleared for this run: route not requested even with both ambient.'

# --- Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure -------------------
# This is the GPU_S4 bachelor-job shape: MLVAPP_GPU_PLAYBACK_RECON=1,
# MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1, -ScaleFactor 4.

$routeRequestedEnv = New-LaunchEnv @{
    MLVAPP_GPU_PLAYBACK_RECON = '1'
    MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'
}

# Route not requested at all: never fails, regardless of what the (irrelevant)
# summary says.
Assert-True ($null -eq (Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv (New-LaunchEnv) -GpuSummary (New-GpuSummary @{ gpu_recon_readback_frames = 5L }))) `
    'Route not requested: must not fail even with recon-readback frames present.'
Assert-True ($null -eq (Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv (New-LaunchEnv) -GpuSummary $null)) `
    'Route not requested: must not fail even with no gpu_summary telemetry.'

# Route requested, every frame reached the no-readback texture path: passes.
Assert-True ($null -eq (Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv $routeRequestedEnv -GpuSummary (New-GpuSummary @{ gpu_texture_no_readback_frames = 10L }))) `
    'Route requested with zero fallback frames must pass.'

# Route requested, this is the exact regression the clamp exists to prevent:
# scale 4 silently rerouted every frame through gpu_recon_readback.
$regressed = Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv $routeRequestedEnv -GpuSummary (New-GpuSummary @{ gpu_recon_readback_frames = 16L })
Assert-True ($null -ne $regressed) `
    'Route requested with 16 recon-readback frames must fail.'
Assert-True ($regressed -match '^gpu_texture_route_readback_regression:') `
    "Failure must carry the named token; observed: $regressed"
Assert-True ($regressed -match '16 frame\(s\)') `
    "Failure must report the offending frame count; observed: $regressed"

# Route requested, even a single fallback frame must fail -- "every frame".
$oneFrameRegressed = Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv $routeRequestedEnv -GpuSummary (New-GpuSummary @{ gpu_recon_readback_frames = 1L })
Assert-True ($null -ne $oneFrameRegressed) `
    'Route requested with a single recon-readback frame must fail.'

# sol major: gpu_texture_readback_frames (the validation-mode CPU-readback
# counter, distinct from gpu_recon_readback_frames) must fail the run too --
# the contract is every frame reaches gpu_texture_no_readback, not just that
# gpu_recon_readback_frames is zero.
$textureReadbackRegressed = Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv $routeRequestedEnv -GpuSummary (New-GpuSummary @{ gpu_texture_readback_frames = 1L })
Assert-True ($null -ne $textureReadbackRegressed) `
    'Route requested with gpu_recon_readback_frames=0 but gpu_texture_readback_frames=1 must fail.'
Assert-True ($textureReadbackRegressed -match 'gpu_texture_readback_frames=1') `
    "Failure must name the offending counter; observed: $textureReadbackRegressed"

# sol major (extended): gpu_preview_frames and cpu_frames are also
# non-texture-no-readback fallbacks and must fail the run.
$previewRegressed = Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv $routeRequestedEnv -GpuSummary (New-GpuSummary @{ gpu_preview_frames = 3L })
Assert-True ($null -ne $previewRegressed) `
    'Route requested with gpu_preview_frames=3 (all other readback counters zero) must fail.'
$cpuRegressed = Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv $routeRequestedEnv -GpuSummary (New-GpuSummary @{ cpu_frames = 2L })
Assert-True ($null -ne $cpuRegressed) `
    'Route requested with cpu_frames=2 (all other readback counters zero) must fail.'

# Route requested but no gpu_summary telemetry was logged at all: loud, named failure.
$missingSummary = Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv $routeRequestedEnv -GpuSummary $null
Assert-True ($null -ne $missingSummary) `
    'Route requested with no gpu_summary telemetry must fail.'
Assert-True ($missingSummary -match '^gpu_texture_route_readback_regression:') `
    "Missing-telemetry failure must carry the named token; observed: $missingSummary"

# fable minor 1 end-to-end: ambient-only route request (no -ExtraEnvironment
# override) with a texture-readback fallback frame must still fail.
$ambientRouteFailure = Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure `
    -LaunchEnv (New-LaunchEnv) -ClearedEnvironment @() `
    -GpuSummary (New-GpuSummary @{ gpu_texture_readback_frames = 1L }) `
    -AmbientEnvironment @{
        MLVAPP_GPU_PLAYBACK_RECON = '1'
        MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'
    }
Assert-True ($null -ne $ambientRouteFailure) `
    'Ambient-only route request with a readback fallback frame must fail, not silently pass.'

Write-Host 'PASS: GUI smoke GPU texture-route scale-clamp validation tests'
