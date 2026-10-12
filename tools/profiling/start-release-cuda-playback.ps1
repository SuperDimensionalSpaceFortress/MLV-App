param(
    [string]$RepoRoot = ".",
    [string]$ExePath = "",
    [Alias("Input")]
    [string]$ClipPath = "",
    [string]$BackendDll = "",
    [string]$OutputRoot = "",
    [string]$GpuPlaybackReconBackend = "cuda",
    [string]$QualityMode = "phase3_hq",
    [string]$ScaleFactor = "1",
    [switch]$ValidateOutput,
    [int]$ValidationSampleEvery = 10,
    [switch]$NoHighPerformancePreference,
    [switch]$Wait,
    [string[]]$AdditionalArgs = @(),
    [switch]$DryRun
)

# HUB-VM-NO-LOCAL-PLAYBACK-1 guard (tools/profiling/dual-venue/hub-hosts.json; pinned by tools/repo_hygiene/test_hub_vm_no_local_playback.py):
# no MLVApp playback, smoke or profile run starts on the hub VM, a guest on Ultra-Magnus. Run it as a venue job through the tracked venue chain.
$hubHostsNoLocalPlayback = @('VIRTUAL-TEN')
if (($hubHostsNoLocalPlayback -contains ([string]$env:COMPUTERNAME).Trim().ToUpperInvariant()) -and ($env:MLVAPP_HUB_PLAYBACK_CI_FIXTURE -cne 'CI_FIXTURE_ONLY')) {
    [Console]::Error.WriteLine("HUB_VM_LOCAL_PLAYBACK_REFUSED: host $env:COMPUTERNAME is the hub VM (a guest on Ultra-Magnus); run this as a venue job through the tracked venue chain")
    exit 17
}

$ErrorActionPreference = "Stop"

# PLAYBACK-CLIP-LENGTH-ENFORCE-1 round 2 (sol B3): this launcher forwards -AdditionalArgs to the app, so
# it must never be the way a play mode (--gui-smoke-playback, --profile-playback, --exercise-play-action,
# --loop, a lifecycle-stress clip switch, ...) reaches a clip with no length gate. Refused, typed, exit 44.
. (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')
$passThroughGate = Test-GuiSmokePassThroughArguments -Arguments $AdditionalArgs -Context 'launcher'
if ($passThroughGate.verdict -ne 'OK') {
    [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-1: $($passThroughGate.message)")
    exit 44
}
# ENFORCE-2: this opens the GUI on a clip, so an MLVAPP_AUTOPLAY_* variable inherited from the parent
# environment would Play it with no tool-side gate (the app's own gate is the backstop, not the plan).
$parentEnvironmentGate = Test-GuiSmokeParentEnvironment
if ($parentEnvironmentGate.verdict -ne 'OK') {
    [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-2: $($parentEnvironmentGate.message)")
    exit 44
}

if ($ValidationSampleEvery -lt 1) {
    throw "-ValidationSampleEvery must be >= 1."
}

function Resolve-RepoPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return (Join-Path $Root $Path)
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $dir = Split-Path -Parent $Path
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $Value | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-FileArtifact {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            path = $Path
            exists = $false
            length = $null
            lastWriteTime = $null
            sha256 = $null
        }
    }

    $item = Get-Item -LiteralPath $Path
    [pscustomobject]@{
        path = $item.FullName
        exists = $true
        length = $item.Length
        lastWriteTime = $item.LastWriteTime
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
    }
}

function Resolve-NvidiaSmiCommand {
    $cmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        "C:\Windows\System32\nvidia-smi.exe",
        (Join-Path $env:ProgramFiles "NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
        (Join-Path $env:ProgramW6432 "NVIDIA Corporation\NVSMI\nvidia-smi.exe")
    )
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $null
}

function Get-NvidiaSmiSnapshot {
    $cmd = Resolve-NvidiaSmiCommand
    if (-not $cmd) {
        return [pscustomobject]@{
            found = $false
            path = $null
            output = @()
            error = "nvidia-smi not found"
        }
    }

    try {
        $output = & $cmd --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
        return [pscustomobject]@{
            found = $true
            path = $cmd
            output = @($output | ForEach-Object { [string]$_ })
            error = $null
        }
    }
    catch {
        return [pscustomobject]@{
            found = $true
            path = $cmd
            output = @()
            error = $_.Exception.Message
        }
    }
}

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = Join-Path $root "platform\qt\build-release\release\MLVApp.exe"
}
else {
    $ExePath = Resolve-RepoPath -Root $root -Path $ExePath
}
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$exeDir = Split-Path -Parent $exe

if ([string]::IsNullOrWhiteSpace($BackendDll)) {
    $BackendDll = Join-Path $exeDir "igpu_recon_cuda.dll"
}
else {
    $BackendDll = Resolve-RepoPath -Root $root -Path $BackendDll
}
$amazeBackendDll = Join-Path $exeDir "igpu_amaze_debayer_cuda.dll"

$platformDir = Join-Path $exeDir "platforms"
$qwindows = Join-Path $platformDir "qwindows.dll"
if (-not (Test-Path -LiteralPath $qwindows -PathType Leaf)) {
    throw "Release CUDA playback launch requires $qwindows. Rebuild/deploy the release tree before launching."
}

$clipFullPath = ""
if (-not [string]::IsNullOrWhiteSpace($ClipPath)) {
    $clipFullPath = (Resolve-Path -LiteralPath (Resolve-RepoPath -Root $root -Path $ClipPath)).Path
}

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $OutputRoot = Join-Path $root ".claude-state\profiling\$stamp-cuda-playback-launch"
}
$runRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    (Resolve-RepoPath -Root $root -Path $OutputRoot))
$manifestPath = Join-Path $runRoot "launch-manifest.json"
$latestPath = Join-Path $root ".claude-state\profiling\cuda-playback-launch-latest.json"
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$arguments = @()
if (-not [string]::IsNullOrWhiteSpace($clipFullPath)) {
    $arguments += $clipFullPath
}
if ($AdditionalArgs.Count -gt 0) {
    $arguments += $AdditionalArgs
}

$environment = [ordered]@{
    QT_OPENGL = "desktop"
    QT_QPA_PLATFORM_PLUGIN_PATH = $platformDir
    QT_PLUGIN_PATH = $exeDir
    MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT = "1"
    MLVAPP_EXPERIMENTAL_GPU_PROCESSING = "1"
    MLVAPP_GPU_PLAYBACK_RECON = "1"
    MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = "1"
    MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER = "1"
    MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT = "1"
    MLVAPP_PLAYBACK_PHASE3_UNATTENDED = "1"
    MLVAPP_PLAYBACK_QUALITY_MODE = $QualityMode
    MLVAPP_PLAYBACK_SCALE_FACTOR = $ScaleFactor
    MLVAPP_GPU_PLAYBACK_RECON_DLL = (Resolve-Path -LiteralPath $BackendDll).Path
    MLVAPP_GPU_AMAZE_DEBAYER_DLL = (Resolve-Path -LiteralPath $amazeBackendDll).Path
}
if (-not [string]::IsNullOrWhiteSpace($GpuPlaybackReconBackend)) {
    $environment["MLVAPP_GPU_PLAYBACK_RECON_BACKEND"] = $GpuPlaybackReconBackend
}
if ($ValidateOutput) {
    $environment["MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT"] = "1"
    $environment["MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT_SAMPLE_EVERY"] = [string]$ValidationSampleEvery
}

# NA-8: this launcher never writes the owner's per-app GPU preference; MLVApp.exe requests the discrete GPU
# itself (NvOptimusEnablement / AmdPowerXpressRequestHighPerformance, platform/qt/main.cpp).

$manifest = [ordered]@{
    schema = "mlvapp-cuda-playback-launch.v1"
    status = $(if ($DryRun) { "planned" } else { "starting" })
    createdAt = (Get-Date).ToString("o")
    host = $env:COMPUTERNAME
    repoRoot = $root
    exe = Get-FileArtifact -Path $exe
    backend = Get-FileArtifact -Path $BackendDll
    amazeBackend = Get-FileArtifact -Path $amazeBackendDll
    cudart = Get-FileArtifact -Path (Join-Path $exeDir "cudart64_12.dll")
    qwindows = Get-FileArtifact -Path $qwindows
    clipPath = $clipFullPath
    arguments = $arguments
    environment = $environment
    nvidiaSmi = Get-NvidiaSmiSnapshot
    gpuPreference = [pscustomobject]@{
        attemptedHighPerformance = $false
        requestedBy = "app exports (NvOptimusEnablement, AmdPowerXpressRequestHighPerformance)"
    }
    proofBoundary = [pscustomobject]@{
        launchOnly = $true
        provesRealtimePlayback = $false
        provesNoReadback = $false
        proofCommand = "tools\profiling\run-local-cuda-playback-dng-smoke.ps1"
        notes = @(
            "This launcher requests the scoped CUDA no-readback playback path for interactive use.",
            "A manifest plus process start is not proof; use the local smoke wrapper for backend load, no-readback/fallback counts, FPS, and DNG hash evidence.",
            "The validated P3 shape still requires a compatible Dual ISO clip, Phase 3 HQ playback mode, x1 scale, the native GPU window viewport, GPU preview processing, hidden scopes, and caching off."
        )
    }
    waitForExit = [bool]$Wait
    processId = $null
    exitCode = $null
}

Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath
Copy-Item -LiteralPath $manifestPath -Destination $latestPath -Force

if ($DryRun) {
    Write-Output $manifestPath
    return
}

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $exe
$startInfo.WorkingDirectory = $root
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $false
foreach ($argument in $arguments) {
    [void]$startInfo.ArgumentList.Add($argument)
}
foreach ($key in $environment.Keys) {
    $startInfo.EnvironmentVariables[$key] = [string]$environment[$key]
}

$process = [System.Diagnostics.Process]::Start($startInfo)
$manifest["status"] = "started"
$manifest["processId"] = $process.Id
if ($Wait) {
    $process.WaitForExit()
    $manifest["status"] = if ($process.ExitCode -eq 0) { "exited" } else { "exited_nonzero" }
    $manifest["exitCode"] = $process.ExitCode
}

Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath
Copy-Item -LiteralPath $manifestPath -Destination $latestPath -Force
Write-Output $manifestPath
