param(
    [string]$RepoRoot = ".",
    [string]$ExePath = "",
    [Alias("Input")]
    [string]$ClipPath = "",
    [Alias("Output")]
    [string]$DngOutputDir = "",
    [string]$Receipt = "",
    [ValidateSet("", "uncompressed", "lossless", "fast-pass")]
    [string]$CdngCodec = "lossless",
    [ValidateSet("trusted", "shadow")]
    [string]$GpuExportMode = "trusted",
    [string]$GpuExportBackend = "cuda",
    [string]$GpuExportDll = "",
    [int]$MaxFrames = 0,
    [switch]$UseAsyncWriter,
    [switch]$UseAsyncWriterCompression,
    [int]$AsyncWriterQueueDepth = 0,
    [int]$AsyncWriterThreadCount = 0,
    [string]$OutputRoot = "",
    [string[]]$AdditionalArgs = @(),
    [switch]$DryRun
)

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
. (Join-Path $PSScriptRoot "cuda-backend-architecture.ps1")

if ($MaxFrames -lt 0) {
    throw "-MaxFrames must be >= 0."
}
if ($UseAsyncWriterCompression -and -not $UseAsyncWriter) {
    throw "-UseAsyncWriterCompression requires -UseAsyncWriter."
}
if ($AsyncWriterQueueDepth -lt 0) {
    throw "-AsyncWriterQueueDepth must be >= 0."
}
if ($AsyncWriterThreadCount -lt 0) {
    throw "-AsyncWriterThreadCount must be >= 0."
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
        $output = & $cmd --query-gpu=name,driver_version,compute_cap,memory.total --format=csv,noheader 2>&1
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
            path = $cmd.Source
            output = @()
            error = $_.Exception.Message
        }
    }
}

function Convert-ComputeCapabilityToCudaToken {
    param([AllowNull()][string]$ComputeCapability)

    if ([string]::IsNullOrWhiteSpace($ComputeCapability)) {
        return $null
    }
    $trimmed = ([string]$ComputeCapability).Trim()
    if ($trimmed -match '^([0-9]+)\.([0-9]+)$') {
        return "sm_$($Matches[1])$($Matches[2])"
    }
    if ($trimmed -match '^([0-9]{2,3})$') {
        return "sm_$trimmed"
    }
    $null
}

function Get-NvidiaSmiComputeCapability {
    param([AllowNull()]$NvidiaSmi)

    if ($null -eq $NvidiaSmi -or -not [bool]$NvidiaSmi.found) {
        return $null
    }
    $row = @($NvidiaSmi.output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -First 1)
    if ($row.Count -eq 0) {
        return $null
    }
    $parts = @([string]$row[0] -split "," | ForEach-Object { ([string]$_).Trim() })
    if ($parts.Count -lt 3) {
        return $null
    }
    [string]$parts[2]
}

function Test-CudaBackendCompatibility {
    param(
        [AllowNull()]$ArchitectureInfo,
        [AllowNull()]$NvidiaSmi
    )

    $computeCapability = Get-NvidiaSmiComputeCapability -NvidiaSmi $NvidiaSmi
    $smToken = Convert-ComputeCapabilityToCudaToken -ComputeCapability $computeCapability
    $computeToken = if ($smToken) { $smToken -replace '^sm_', 'compute_' } else { $null }
    $tokens = @()
    if ($ArchitectureInfo -and $ArchitectureInfo.tokens) {
        $tokens = @($ArchitectureInfo.tokens | ForEach-Object { [string]$_ })
    }
    $compatible = $null
    $reason = "gpu_compute_capability_unavailable"
    if ($null -eq $ArchitectureInfo -or -not [bool]$ArchitectureInfo.exists) {
        $compatible = $false
        $reason = "backend_missing"
    }
    elseif ($null -eq $smToken) {
        $compatible = $null
        $reason = "gpu_compute_capability_unavailable"
    }
    elseif (-not [bool]$ArchitectureInfo.detectionReliable) {
        $compatible = $false
        $reason = "backend_architecture_detection_unreliable"
    }
    elseif ($tokens.Count -eq 0) {
        $compatible = $false
        $reason = "backend_architecture_metadata_unavailable"
    }
    elseif (($tokens -contains $smToken) -or ($tokens -contains $computeToken)) {
        $compatible = $true
        $reason = "compatible"
    }
    else {
        $compatible = $false
        $reason = "backend_architecture_missing_target"
    }

    [pscustomobject]@{
        schema = "mlvapp.cuda-backend-compatibility.v1"
        compatible = $compatible
        reason = $reason
        gpu_compute_capability = $computeCapability
        required_tokens = @(@($smToken, $computeToken) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
        backend_tokens = @($tokens)
        proofBoundary = "This is a preflight compatibility gate only. Runtime support still requires DNG hash/GPU replacement proof on the host."
    }
}

function Get-DngOutputSummary {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or !(Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{
            path = $Path
            exists = $false
            count = 0
            totalBytes = 0
            firstFiles = @()
        }
    }

    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Filter "*.dng" -ErrorAction SilentlyContinue |
        Sort-Object FullName)
    $totalBytes = ($files | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $totalBytes) {
        $totalBytes = 0
    }
    [pscustomobject]@{
        path = (Resolve-Path -LiteralPath $Path).Path
        exists = $true
        count = $files.Count
        totalBytes = [int64]$totalBytes
        firstFiles = @($files | Select-Object -First 12 | ForEach-Object {
            [pscustomobject]@{
                path = $_.FullName
                length = $_.Length
                lastWriteTime = $_.LastWriteTime
            }
        })
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

if ([string]::IsNullOrWhiteSpace($GpuExportDll)) {
    $GpuExportDll = Join-Path $exeDir "igpu_recon_cuda.dll"
}
else {
    $GpuExportDll = Resolve-RepoPath -Root $root -Path $GpuExportDll
}
$gpuDll = (Resolve-Path -LiteralPath $GpuExportDll).Path

$platformDir = Join-Path $exeDir "platforms"
$qwindows = Join-Path $platformDir "qwindows.dll"
if (-not (Test-Path -LiteralPath $qwindows -PathType Leaf)) {
    throw "Release CUDA DNG export requires $qwindows. Rebuild/deploy the release tree before exporting."
}

$clipFullPath = ""
if (-not [string]::IsNullOrWhiteSpace($ClipPath)) {
    $clipFullPath = (Resolve-Path -LiteralPath (Resolve-RepoPath -Root $root -Path $ClipPath)).Path
}

$dngDir = ""
if (-not [string]::IsNullOrWhiteSpace($DngOutputDir)) {
    $dngDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        (Resolve-RepoPath -Root $root -Path $DngOutputDir))
}

$receiptPath = ""
if (-not [string]::IsNullOrWhiteSpace($Receipt)) {
    $receiptPath = (Resolve-Path -LiteralPath (Resolve-RepoPath -Root $root -Path $Receipt)).Path
}

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $OutputRoot = Join-Path $root ".claude-state\profiling\$stamp-cuda-dng-export"
}
$runRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    (Resolve-RepoPath -Root $root -Path $OutputRoot))
$summaryPath = Join-Path $runRoot "summary.json"
$latestPath = Join-Path $root ".claude-state\profiling\cuda-dng-export-latest.json"
$profilePath = Join-Path $runRoot "export-profile.json"
$stdoutPath = Join-Path $runRoot "stdout.txt"
$stderrPath = Join-Path $runRoot "stderr.txt"
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$arguments = @("--batch")
if (-not [string]::IsNullOrWhiteSpace($clipFullPath)) {
    $arguments += @("--input", $clipFullPath)
}
if (-not [string]::IsNullOrWhiteSpace($dngDir)) {
    $arguments += @("--output", $dngDir)
}
if (-not [string]::IsNullOrWhiteSpace($receiptPath)) {
    $arguments += @("--receipt", $receiptPath)
}
if (-not [string]::IsNullOrWhiteSpace($CdngCodec)) {
    $arguments += @("--cdng-codec", $CdngCodec)
}
if ($MaxFrames -gt 0) {
    $arguments += @("--max-frames", [string]$MaxFrames)
}
if ($AdditionalArgs.Count -gt 0) {
    $arguments += $AdditionalArgs
}

$environment = [ordered]@{
    QT_QPA_PLATFORM = "windows"
    QT_QPA_PLATFORM_PLUGIN_PATH = $platformDir
    QT_PLUGIN_PATH = $exeDir
    MLVAPP_EXPORT_STAGE_PROFILER = "1"
    MLVAPP_EXPORT_STAGE_PROFILE_FILE = $profilePath
    MLVAPP_EXPORT_STAGE_PROFILE_BUILD_ID = (& git -C $root rev-parse --verify HEAD 2>$null)
    MLVAPP_GPU_EXPORT = "1"
    MLVAPP_GPU_EXPORT_DLL = $gpuDll
}
if (-not [string]::IsNullOrWhiteSpace($GpuExportBackend)) {
    $environment["MLVAPP_GPU_EXPORT_BACKEND"] = $GpuExportBackend
}
if ($GpuExportMode -eq "trusted") {
    $environment["MLVAPP_GPU_EXPORT_TRUSTED"] = "1"
}
if ($UseAsyncWriter) {
    $environment["MLVAPP_CDNG_EXPORT_ASYNC_WRITER"] = "1"
    if ($UseAsyncWriterCompression) {
        $environment["MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS"] = "1"
    }
    if ($AsyncWriterQueueDepth -gt 0) {
        $environment["MLVAPP_CDNG_EXPORT_ASYNC_WRITER_QUEUE_DEPTH"] = [string]$AsyncWriterQueueDepth
    }
    if ($AsyncWriterThreadCount -gt 0) {
        $environment["MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS"] = [string]$AsyncWriterThreadCount
    }
}

$nvidiaSmi = Get-NvidiaSmiSnapshot
$backendArchitecture = Get-CudaBackendArchitectureInfo -Path $gpuDll
$backendCompatibility = Test-CudaBackendCompatibility `
    -ArchitectureInfo $backendArchitecture `
    -NvidiaSmi $nvidiaSmi
$preflightFailures = [System.Collections.Generic.List[string]]::new()
if (!$DryRun -and [bool]$nvidiaSmi.found -and $backendCompatibility.compatible -ne $true) {
    [void]$preflightFailures.Add(("CUDA backend architecture is not compatible with this GPU: compute_capability={0}; backend_tokens=[{1}]; reason={2}. Rebuild/deploy igpu_recon_cuda.dll with tools\\gpu\\backend\\build-backend-dll.ps1 -Arch portable before using this run for Dell/UltraMagnus claims." -f `
        $backendCompatibility.gpu_compute_capability,
        (($backendCompatibility.backend_tokens | ForEach-Object { [string]$_ }) -join ","),
        $backendCompatibility.reason))
}

$status = if ($DryRun) { "planned" } else { "starting" }
$summary = [ordered]@{
    schema = "mlvapp-cuda-dng-export.v1"
    status = $status
    createdAt = (Get-Date).ToString("o")
    host = $env:COMPUTERNAME
    repoRoot = $root
    exe = Get-FileArtifact -Path $exe
    backend = Get-FileArtifact -Path $gpuDll
    backendArchitecture = $backendArchitecture
    backendCompatibility = $backendCompatibility
    cudart = Get-FileArtifact -Path (Join-Path $exeDir "cudart64_12.dll")
    qwindows = Get-FileArtifact -Path $qwindows
    clipPath = $clipFullPath
    receiptPath = $receiptPath
    dngOutputDir = $dngDir
    cdngCodec = $CdngCodec
    maxFrames = $MaxFrames
    gpuExportMode = $GpuExportMode
    useAsyncWriter = [bool]$UseAsyncWriter
    useAsyncWriterCompression = [bool]$UseAsyncWriterCompression
    asyncWriterQueueDepth = $AsyncWriterQueueDepth
    asyncWriterThreadCount = $AsyncWriterThreadCount
    arguments = $arguments
    environment = $environment
    nvidiaSmi = $nvidiaSmi
    preflightFailures = @($preflightFailures)
    profilePath = $profilePath
    stdoutPath = $stdoutPath
    stderrPath = $stderrPath
    exitCode = $null
    dngOutput = Get-DngOutputSummary -Path $dngDir
    proofBoundary = [pscustomobject]@{
        exportRunner = $true
        provesDellSupport = $false
        provesDngHashParity = $false
        proofCommand = "tools\profiling\run-local-cuda-playback-dng-smoke.ps1"
        notes = @(
            "This runner performs a real release --batch DNG export with explicit CUDA export environment.",
            "Trusted mode is an opt-in CUDA output path for the scoped Dual ISO shape; unsupported states still need telemetry/profile review.",
            "Async-writer compression is opt-in E3 measurement only; use it to test lossless compression overlap, not as a default behavior change.",
            "Use the local smoke wrapper for CPU-baseline-vs-candidate DNG hash proof on a specific host."
        )
    }
}

Write-JsonFile -Value ([pscustomobject]$summary) -Path $summaryPath
Copy-Item -LiteralPath $summaryPath -Destination $latestPath -Force

if ($DryRun) {
    Write-Output $summaryPath
    return
}
if ($preflightFailures.Count -gt 0) {
    $summary["status"] = "failed"
    Write-JsonFile -Value ([pscustomobject]$summary) -Path $summaryPath
    Copy-Item -LiteralPath $summaryPath -Destination $latestPath -Force
    throw ($preflightFailures -join "; ")
}

if ([string]::IsNullOrWhiteSpace($clipFullPath)) {
    throw "Missing -Input <clip-or-folder>."
}
if ([string]::IsNullOrWhiteSpace($dngDir)) {
    throw "Missing -Output <dng-output-directory>."
}
New-Item -ItemType Directory -Force -Path $dngDir | Out-Null

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $exe
$startInfo.WorkingDirectory = $root
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
foreach ($argument in $arguments) {
    [void]$startInfo.ArgumentList.Add($argument)
}
foreach ($key in $environment.Keys) {
    $startInfo.EnvironmentVariables[$key] = [string]$environment[$key]
}

$process = [System.Diagnostics.Process]::Start($startInfo)
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()
$stdout | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
$stderr | Set-Content -LiteralPath $stderrPath -Encoding UTF8

$summary["exitCode"] = $process.ExitCode
$summary["status"] = if ($process.ExitCode -eq 0) { "success" } else { "failed" }
$summary["dngOutput"] = Get-DngOutputSummary -Path $dngDir
$summary["profileExists"] = Test-Path -LiteralPath $profilePath -PathType Leaf
Write-JsonFile -Value ([pscustomobject]$summary) -Path $summaryPath
Copy-Item -LiteralPath $summaryPath -Destination $latestPath -Force

if ($process.ExitCode -eq 0 -and -not [bool]$summary["profileExists"]) {
    throw "CUDA DNG export exited 0 but did not write export profile: $profilePath"
}
exit $process.ExitCode
