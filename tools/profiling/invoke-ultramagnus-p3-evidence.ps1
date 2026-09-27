param(
    [string]$RepoRoot = ".",
    [string]$RemoteHostName = "ultra-magnus",
    [string]$ExpectedEvidenceHostName = "ULTRA-MAGNUS",
    [string]$AgentRoot = "\\ultra-magnus\G\Temp\mlv-gpu-profile\agent",
    [string]$RemoteStagingShare = "\\ultra-magnus\G\Temp\mlvapp-p3-evidence",
    [string]$RemoteStagingLocal = "G:\Temp\mlvapp-p3-evidence",
    [string]$RemoteRepoRoot = "",
    [string]$RemotePacketPath = "",
    [string]$LocalPacketRoot = ".claude-state\profiling\ultramagnus-p3-texture-present\remote-packets",
    [string]$ImportOutputRoot = ".claude-state\profiling\ultramagnus-p3-texture-present\imported",
    [string]$ClipRoot = "G:\Temp\mlv-gpu-profile\clips",
    [string[]]$ClipNames = @("M16-1327.MLV"),
    [string[]]$ClipPaths = @(),
    [int]$Seconds = 30,
    [int]$SettleMs = 1000,
    [int]$ValidationSampleEvery = 10,
    [ValidateSet("persisted", "on", "off")]
    [string]$DropFrameMode = "on",
    [string]$GpuPlaybackReconBackend = "",
    [int]$CudaAmazeLiveTileStreams = 0,
    [switch]$CudaAmazeFastLaunchChecks,
    [switch]$CudaAmazeLiveDirectRgbaStore,
    [switch]$GpuPlaybackReconRetainDeviceOutput,
    [switch]$GpuTexNrAcquireLatestReady,
    [switch]$GpuTexNrImmediateDrainReady,
    [int]$GpuTexNrImmediateDrainMax = 0,
    [int]$Phase3FrameSlots = 0,
    [int]$PlaybackTimerPollMs = 0,
    [int]$PlaybackRenderLookaheadFrames = 0,
    [int]$AgentTimeoutSec = 2700,
    [switch]$SpeedLeg,
    [switch]$SkipRemoteBuild,
    [switch]$SkipBackendBuild,
    [switch]$SkipStageRepo,
    [string]$ImportOnlyPacket = ""
)

$ErrorActionPreference = "Stop"

if ($CudaAmazeLiveTileStreams -lt 0) {
    throw "-CudaAmazeLiveTileStreams must be >= 0. Use 0 for backend default."
}
if ($GpuTexNrImmediateDrainMax -lt 0) {
    throw "-GpuTexNrImmediateDrainMax must be >= 0. Use 0 for default."
}
if ($Phase3FrameSlots -lt 0) {
    throw "-Phase3FrameSlots must be >= 0. Use 0 for renderer default."
}
if ($PlaybackTimerPollMs -lt 0) {
    throw "-PlaybackTimerPollMs must be >= 0. Use 0 for renderer default."
}
if ($PlaybackRenderLookaheadFrames -lt 0 -or $PlaybackRenderLookaheadFrames -gt 3) {
    throw "-PlaybackRenderLookaheadFrames must be between 0 and 3. Use 0 for renderer default."
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

function Add-Failure {
    param(
        [System.Collections.Generic.List[string]]$Failures,
        [string]$Message
    )
    [void]$Failures.Add($Message)
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
    $Value | ConvertTo-Json -Depth 18 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Convert-ToPowerShellSingleQuotedString {
    param([AllowNull()][string]$Value)
    return "'" + (($Value -replace "'", "''")) + "'"
}

function Convert-ToPowerShellArrayLiteral {
    param([AllowNull()][string[]]$Values)
    $quoted = @($Values | ForEach-Object {
        Convert-ToPowerShellSingleQuotedString ([string]$_)
    })
    return "@(" + ($quoted -join ", ") + ")"
}

function Convert-AgentExitCode {
    param($Value)
    if ($null -eq $Value) {
        return $null
    }
    if ($Value.PSObject.Properties.Name -contains "value") {
        return [int]$Value.value
    }
    return [int]$Value
}

function Join-WindowsPathLiteral {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Child
    )
    return ($Root.TrimEnd("\") + "\" + $Child.TrimStart("\"))
}

function Convert-RemoteLocalPathToSharePath {
    param(
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [Parameter(Mandatory = $true)][string]$RemoteLocalRoot,
        [Parameter(Mandatory = $true)][string]$RemoteShareRoot
    )
    if ($LocalPath.StartsWith("\\", [System.StringComparison]::OrdinalIgnoreCase)) {
        return $LocalPath
    }
    $localRoot = $RemoteLocalRoot.TrimEnd("\")
    if (!$LocalPath.StartsWith($localRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $null
    }
    $relative = $LocalPath.Substring($localRoot.Length).TrimStart("\")
    if ([string]::IsNullOrWhiteSpace($relative)) {
        return $RemoteShareRoot
    }
    return (Join-Path $RemoteShareRoot $relative)
}

function Invoke-ImportVerifier {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string]$ValidatorScript,
        [Parameter(Mandatory = $true)][string]$PacketPath,
        [Parameter(Mandatory = $true)][string]$ImportRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedHost
    )

    $output = & pwsh.exe `
        -NoLogo `
        -NoProfile `
        -NonInteractive `
        -ExecutionPolicy Bypass `
        -File $ValidatorScript `
        -RepoRoot $Repo `
        -ExpectedHostName $ExpectedHost `
        -ImportEvidencePacket $PacketPath `
        -ImportOutputRoot $ImportRoot 2>&1

    return [pscustomobject]@{
        exitCode = $LASTEXITCODE
        output = @($output | ForEach-Object { [string]$_ })
    }
}

function Invoke-RobocopyMirror {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $args = @(
        $Source,
        $Destination,
        "/MIR",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NP",
        "/R:1",
        "/W:1",
        "/XD",
        ".claude-state",
        ".scratch",
        ".vs"
    )
    $output = & robocopy.exe @args 2>&1
    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{
        exitCode = $exitCode
        output = @($output | ForEach-Object { [string]$_ })
        ok = ($exitCode -lt 8)
    }
}

function Test-AgentHeartbeat {
    param([Parameter(Mandatory = $true)][string]$Root)
    $heartbeat = Join-Path $Root "heartbeat.txt"
    if (!(Test-Path -LiteralPath $heartbeat)) {
        return [pscustomobject]@{
            ok = $false
            path = $heartbeat
            ageSeconds = $null
            text = $null
            reason = "missing heartbeat"
        }
    }
    $item = Get-Item -LiteralPath $heartbeat
    $age = [int]((Get-Date).ToUniversalTime() - $item.LastWriteTimeUtc).TotalSeconds
    $text = (Get-Content -LiteralPath $heartbeat -Raw).Trim()
    return [pscustomobject]@{
        ok = ($age -le 120)
        path = $heartbeat
        ageSeconds = $age
        text = $text
        reason = if ($age -le 120) { "ok" } else { "stale heartbeat" }
    }
}

function Submit-AgentJob {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)][string]$ScriptText,
        [Parameter(Mandatory = $true)][int]$TimeoutSec
    )

    $inbox = Join-Path $Root "inbox"
    $outbox = Join-Path $Root "outbox"
    New-Item -ItemType Directory -Force -Path $inbox | Out-Null
    New-Item -ItemType Directory -Force -Path $outbox | Out-Null

    $tmp = Join-Path $inbox "$JobId.job.tmp"
    $job = Join-Path $inbox "$JobId.job.ps1"
    $result = Join-Path $outbox "$JobId.result.json"
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    if (Test-Path -LiteralPath $job) { Remove-Item -LiteralPath $job -Force }
    if (Test-Path -LiteralPath $result) { Remove-Item -LiteralPath $result -Force }

    $ScriptText | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $job

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $result) {
            $raw = Get-Content -LiteralPath $result -Raw
            return [pscustomobject]@{
                timedOut = $false
                resultPath = $result
                result = ($raw | ConvertFrom-Json)
            }
        }
        Start-Sleep -Seconds 2
    }

    return [pscustomobject]@{
        timedOut = $true
        resultPath = $result
        result = $null
    }
}

$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$validatorScript = Join-Path $repo "tools\profiling\run-ultramagnus-p3-validation.ps1"
$localPacketRootResolved = Resolve-RepoPath -Root $repo -Path $LocalPacketRoot
$importOutputRootResolved = Resolve-RepoPath -Root $repo -Path $ImportOutputRoot
if ([string]::IsNullOrWhiteSpace($RemoteRepoRoot)) {
    $RemoteRepoRoot = Join-WindowsPathLiteral -Root $RemoteStagingLocal -Child "repo"
}
if ([string]::IsNullOrWhiteSpace($RemotePacketPath)) {
    $RemotePacketPath = Join-WindowsPathLiteral `
        -Root $RemoteStagingLocal `
        -Child "mlvapp-p3-evidence-latest.zip"
}
$remoteRepoShare = Convert-RemoteLocalPathToSharePath `
    -LocalPath $RemoteRepoRoot `
    -RemoteLocalRoot $RemoteStagingLocal `
    -RemoteShareRoot $RemoteStagingShare
$remotePacketShare = Convert-RemoteLocalPathToSharePath `
    -LocalPath $RemotePacketPath `
    -RemoteLocalRoot $RemoteStagingLocal `
    -RemoteShareRoot $RemoteStagingShare
$remoteJobOutputPath = Join-WindowsPathLiteral `
    -Root $RemoteStagingLocal `
    -Child "latest-agent-job-output.json"
$remoteJobOutputShare = Convert-RemoteLocalPathToSharePath `
    -LocalPath $remoteJobOutputPath `
    -RemoteLocalRoot $RemoteStagingLocal `
    -RemoteShareRoot $RemoteStagingShare

$stamp = Get-Date -Format "yyyyMMddTHHmmss"
$summaryPath = Join-Path $localPacketRootResolved "latest-remote-invoke.json"
$failures = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()
$stageResult = $null
$agentHeartbeat = $null
$agentSubmission = $null
$remoteJobOutput = $null
$copiedPacket = $null
$importResult = $null
$localRepoHead = ""
$localRepoBranch = ""
$localRepoStatus = @()
$localLlrawprocBlobId = ""
$LlrawprocRelativePath = "src/mlv/llrawproc/llrawproc.c"

if (!(Test-Path -LiteralPath $validatorScript)) {
    Add-Failure $failures "Missing validator script: $validatorScript"
}

New-Item -ItemType Directory -Force -Path $localPacketRootResolved | Out-Null

if ($failures.Count -eq 0 -and ![string]::IsNullOrWhiteSpace($ImportOnlyPacket)) {
    $packetPath = Resolve-RepoPath -Root $repo -Path $ImportOnlyPacket
    if (!(Test-Path -LiteralPath $packetPath)) {
        Add-Failure $failures "Import-only packet does not exist: $packetPath"
    }
    else {
        $importResult = Invoke-ImportVerifier `
            -Repo $repo `
            -ValidatorScript $validatorScript `
            -PacketPath $packetPath `
            -ImportRoot $importOutputRootResolved `
            -ExpectedHost $ExpectedEvidenceHostName
        if ($importResult.exitCode -ne 0) {
            Add-Failure $failures "Import verifier rejected packet with exit code $($importResult.exitCode)."
        }
        $copiedPacket = [pscustomobject]@{
            path = $packetPath
            source = "import-only"
        }
    }
}
elseif ($failures.Count -eq 0) {
    try {
        [System.Net.Dns]::GetHostEntry($RemoteHostName) | Out-Null
    }
    catch {
        Add-Failure $failures "Remote host '$RemoteHostName' did not resolve: $($_.Exception.Message)"
    }

    if ($failures.Count -eq 0 -and !(Test-Connection -ComputerName $RemoteHostName -Count 1 -Quiet)) {
        Add-Failure $failures "Remote host '$RemoteHostName' did not respond to ping."
    }

    if ($failures.Count -eq 0 -and !(Test-Path -LiteralPath $RemoteStagingShare)) {
        try {
            New-Item -ItemType Directory -Force -Path $RemoteStagingShare | Out-Null
        }
        catch {
            Add-Failure $failures "Remote staging share is unavailable: $RemoteStagingShare ($($_.Exception.Message))"
        }
    }

    if ($failures.Count -eq 0 -and [string]::IsNullOrWhiteSpace($remoteRepoShare)) {
        Add-Failure $failures "RemoteRepoRoot '$RemoteRepoRoot' is not under RemoteStagingLocal '$RemoteStagingLocal'; cannot map it to SMB share '$RemoteStagingShare'."
    }
    if ($failures.Count -eq 0 -and [string]::IsNullOrWhiteSpace($remotePacketShare)) {
        Add-Failure $failures "RemotePacketPath '$RemotePacketPath' is not under RemoteStagingLocal '$RemoteStagingLocal'; cannot map it to SMB share '$RemoteStagingShare'."
    }

    if ($failures.Count -eq 0) {
        $localRepoHeadOutput = @(& git -C $repo rev-parse HEAD 2>$null)
        $gitExitCode = $LASTEXITCODE
        if ($gitExitCode -ne 0) {
            Add-Failure $failures "Unable to inspect local git HEAD before staging."
        }
        else {
            $localRepoHead = ([string]($localRepoHeadOutput | Select-Object -First 1)).Trim()
        }
    }

    if ($failures.Count -eq 0) {
        # See tools\profiling\invoke-ultramagnus-cdng-export-evidence.ps1 for why this
        # travels with the job: source-sha alone does not bind a build, so the blob id
        # of the CPU reference and the built DLL's own sha256 go along too, letting a
        # result self-bind instead of being trusted by filename adjacency.
        $localLlrawprocBlobIdOutput = @(& git -C $repo rev-parse "${localRepoHead}:${LlrawprocRelativePath}" 2>$null)
        $gitExitCode = $LASTEXITCODE
        if ($gitExitCode -ne 0) {
            Add-Failure $failures "Unable to resolve blob id for ${LlrawprocRelativePath} at local HEAD ${localRepoHead}."
        }
        else {
            $localLlrawprocBlobId = ([string]($localLlrawprocBlobIdOutput | Select-Object -First 1)).Trim()
        }
    }

    if ($failures.Count -eq 0) {
        $localRepoBranchOutput = @(& git -C $repo rev-parse --abbrev-ref HEAD 2>$null)
        $gitExitCode = $LASTEXITCODE
        if ($gitExitCode -ne 0) {
            Add-Failure $failures "Unable to inspect local git branch before staging."
        }
        else {
            $localRepoBranch = ([string]($localRepoBranchOutput | Select-Object -First 1)).Trim()
        }
    }

    if ($failures.Count -eq 0) {
        $localRepoStatusOutput = @(& git -C $repo status --short --branch 2>$null)
        $gitExitCode = $LASTEXITCODE
        if ($gitExitCode -ne 0) {
            Add-Failure $failures "Unable to inspect local git status before staging."
        }
        else {
            $localRepoStatus = @($localRepoStatusOutput | ForEach-Object { [string]$_ })
        }
    }

    if ($failures.Count -eq 0 -and !$SkipStageRepo) {
        $localDirty = @($localRepoStatus | Where-Object { !([string]$_).StartsWith("## ") })
        if ($localDirty.Count -gt 0) {
            Add-Failure $failures "Local repo is dirty; commit or explicitly rerun after a clean tree before producing authoritative UltraMagnus evidence: $($localDirty -join '; ')"
        }
    }

    if ($failures.Count -eq 0 -and !$SkipStageRepo) {
        $stageResult = Invoke-RobocopyMirror -Source $repo -Destination $remoteRepoShare
        if (!$stageResult.ok) {
            Add-Failure $failures "Repo staging to '$remoteRepoShare' failed with robocopy exit code $($stageResult.exitCode)."
        }
    }
    elseif ($SkipStageRepo) {
        [void]$warnings.Add("Skipped repo staging; remote repo root must already be current and clean: $RemoteRepoRoot")
    }

    if ($failures.Count -eq 0) {
        $agentHeartbeat = Test-AgentHeartbeat -Root $AgentRoot
        if (!$agentHeartbeat.ok) {
            Add-Failure $failures "UltraMagnus SMB agent is not live at '$AgentRoot': $($agentHeartbeat.reason)."
        }
    }

    if ($failures.Count -eq 0) {
        $packetParentShare = Split-Path -Parent $remotePacketShare
        if ($packetParentShare) {
            New-Item -ItemType Directory -Force -Path $packetParentShare | Out-Null
        }
        if (Test-Path -LiteralPath $remotePacketShare) {
            Remove-Item -LiteralPath $remotePacketShare -Force
        }
        if (Test-Path -LiteralPath $remoteJobOutputShare) {
            Remove-Item -LiteralPath $remoteJobOutputShare -Force
        }

        $jobId = "mlvapp-p3-$stamp"
        $skipBuildLiteral = if ($SkipRemoteBuild) { '$true' } else { '$false' }
        $skipBackendBuildLiteral = if ($SkipBackendBuild) { '$true' } else { '$false' }
        $speedLegLiteral = if ($SpeedLeg) { '$true' } else { '$false' }
        $clipNamesLiteral = Convert-ToPowerShellArrayLiteral $ClipNames
        $clipPathsLiteral = Convert-ToPowerShellArrayLiteral $ClipPaths
        $localRepoHeadLiteral = Convert-ToPowerShellSingleQuotedString $localRepoHead
        $localRepoBranchLiteral = Convert-ToPowerShellSingleQuotedString $localRepoBranch
        $localRepoStatusLiteral = Convert-ToPowerShellArrayLiteral $localRepoStatus
        $localLlrawprocBlobIdLiteral = Convert-ToPowerShellSingleQuotedString $localLlrawprocBlobId
        $remoteProvenanceFileLiteral = Convert-ToPowerShellSingleQuotedString (Join-Path $AgentRoot "outbox\$jobId.provenance.json")
        $dropFrameModeLiteral = Convert-ToPowerShellSingleQuotedString $DropFrameMode
        $gpuPlaybackReconBackendLiteral = Convert-ToPowerShellSingleQuotedString $GpuPlaybackReconBackend
        $cudaAmazeLiveTileStreamsLiteral = [string]$CudaAmazeLiveTileStreams
        $cudaAmazeFastLaunchChecksLiteral = if ($CudaAmazeFastLaunchChecks) { '$true' } else { '$false' }
        $cudaAmazeLiveDirectRgbaStoreLiteral = if ($CudaAmazeLiveDirectRgbaStore) { '$true' } else { '$false' }
        $gpuPlaybackReconRetainDeviceOutputLiteral = if ($GpuPlaybackReconRetainDeviceOutput) { '$true' } else { '$false' }
        $gpuTexNrAcquireLatestReadyLiteral = if ($GpuTexNrAcquireLatestReady) { '$true' } else { '$false' }
        $gpuTexNrImmediateDrainReadyLiteral = if ($GpuTexNrImmediateDrainReady) { '$true' } else { '$false' }
        $gpuTexNrImmediateDrainMaxLiteral = [string]$GpuTexNrImmediateDrainMax
        $phase3FrameSlotsLiteral = [string]$Phase3FrameSlots
        $playbackTimerPollMsLiteral = [string]$PlaybackTimerPollMs
        $playbackRenderLookaheadFramesLiteral = [string]$PlaybackRenderLookaheadFrames
        $validationSampleEveryLiteral = [string]$ValidationSampleEvery
        $jobScript = @"
`$ErrorActionPreference = 'Stop'
`$repo = $(Convert-ToPowerShellSingleQuotedString $RemoteRepoRoot)
`$packet = $(Convert-ToPowerShellSingleQuotedString $RemotePacketPath)
`$jobOutput = $(Convert-ToPowerShellSingleQuotedString $remoteJobOutputPath)
`$expectedHost = $(Convert-ToPowerShellSingleQuotedString $ExpectedEvidenceHostName)
`$clipRoot = $(Convert-ToPowerShellSingleQuotedString $ClipRoot)
`$clipNames = $clipNamesLiteral
`$clipPaths = $clipPathsLiteral
`$evidenceRepoHead = $localRepoHeadLiteral
`$evidenceBranch = $localRepoBranchLiteral
`$evidenceGitStatus = $localRepoStatusLiteral
`$rangeHeadSha = $localRepoHeadLiteral
`$llrawprocBlobId = $localLlrawprocBlobIdLiteral
`$provenanceFile = $remoteProvenanceFileLiteral
`$dllSha256 = `$null
`$pendingSymbolPresence = `$null
`$dropFrameMode = $dropFrameModeLiteral
`$skipBackendBuild = $skipBackendBuildLiteral
`$speedLeg = $speedLegLiteral
`$gpuPlaybackReconBackend = $gpuPlaybackReconBackendLiteral
`$cudaAmazeLiveTileStreams = [int]'$cudaAmazeLiveTileStreamsLiteral'
`$cudaAmazeFastLaunchChecks = $cudaAmazeFastLaunchChecksLiteral
`$cudaAmazeLiveDirectRgbaStore = $cudaAmazeLiveDirectRgbaStoreLiteral
`$gpuPlaybackReconRetainDeviceOutput = $gpuPlaybackReconRetainDeviceOutputLiteral
`$gpuTexNrAcquireLatestReady = $gpuTexNrAcquireLatestReadyLiteral
`$gpuTexNrImmediateDrainReady = $gpuTexNrImmediateDrainReadyLiteral
`$gpuTexNrImmediateDrainMax = [int]'$gpuTexNrImmediateDrainMaxLiteral'
`$phase3FrameSlots = [int]'$phase3FrameSlotsLiteral'
`$playbackTimerPollMs = [int]'$playbackTimerPollMsLiteral'
`$playbackRenderLookaheadFrames = [int]'$playbackRenderLookaheadFramesLiteral'
`$validationSampleEvery = [int]'$validationSampleEveryLiteral'
`$validator = Join-Path `$repo 'tools\profiling\run-ultramagnus-p3-validation.ps1'
`$backendDir = Join-Path `$repo 'tools\gpu\backend'
`$backendBuildScript = Join-Path `$backendDir 'build-backend-dll.ps1'
`$amazeBackendBuildScript = Join-Path `$backendDir 'amaze-debayer-dll.ps1'
`$backendDll = Join-Path `$backendDir 'igpu_recon_cuda.dll'
`$backendArchSidecar = Join-Path `$backendDir 'igpu_recon_cuda.arch.json'
`$amazeBackendDll = Join-Path `$backendDir 'igpu_amaze_debayer_cuda.dll'
`$releaseDir = Join-Path `$repo 'platform\qt\build-release\release'
`$psExe = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not `$psExe) { `$psExe = 'powershell.exe' }

function Write-ProvenanceFile {
    # Written for the agent to pick up and echo into its generic job result
    # (outbox\<jobId>.result.json), so the result self-binds instead of being
    # trusted by filename adjacency to a submit-time claim it never proved. Called
    # once up front (rangeHeadSha/llrawprocBlobId only - both known before this job
    # ever runs) and again once the backend build resolves dllSha256 and
    # pendingSymbolPresence, so a job that dies mid-build still leaves the source
    # binding on record.
    [ordered]@{
        rangeHeadSha = `$rangeHeadSha
        llrawprocBlobId = `$llrawprocBlobId
        pendingSymbolPresence = `$pendingSymbolPresence
        dllSha256 = `$dllSha256
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath `$provenanceFile -Encoding UTF8
}

function Test-BackendDllExportsPending {
    # Mirrors tools\gpu\backend\verify-exports.ps1's dumpbin discovery: proves the
    # just-built DLL actually exports the igpu_recon_ ABI rather than being a stale
    # or stub artifact. `$null (never `$false) when the check itself could not run,
    # so an inconclusive venue is never reported as a failed proof.
    param([string]`$DllPath)
    if (!(Test-Path -LiteralPath `$DllPath)) { return `$null }
    try {
        `$vswhereExe = "`${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
        if (!(Test-Path -LiteralPath `$vswhereExe)) { return `$null }
        `$vsInstallPath = & `$vswhereExe -latest -property installationPath
        if ([string]::IsNullOrWhiteSpace(`$vsInstallPath)) { return `$null }
        `$dumpbinExe = Get-ChildItem -Path (Join-Path `$vsInstallPath 'VC\Tools\MSVC') -Recurse -Filter dumpbin.exe -ErrorAction SilentlyContinue |
            Where-Object { `$_.FullName -match '\\x64\\' } | Select-Object -First 1
        if (!`$dumpbinExe) { return `$null }
        `$exportLines = @(& `$dumpbinExe.FullName /EXPORTS `$DllPath 2>`$null | Select-String 'igpu_recon_')
        return (`$exportLines.Count -gt 0)
    }
    catch {
        return `$null
    }
}

`$payload = [ordered]@{
    schema = 'mlvapp-p3-agent-job-output.v1'
    startedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    host = [Environment]::MachineName
    repoRoot = `$repo
    packet = `$packet
    validator = `$validator
    provenance = [ordered]@{
        rangeHeadSha = `$rangeHeadSha
        llrawprocBlobId = `$llrawprocBlobId
        dllSha256 = `$dllSha256
        pendingSymbolPresence = `$pendingSymbolPresence
    }
    backend = [ordered]@{
        skipped = [bool]`$skipBackendBuild
        dir = `$backendDir
        buildScript = `$backendBuildScript
        buildExitCode = `$null
        amazeBuildScript = `$amazeBackendBuildScript
        amazeBuildExitCode = `$null
        deployDir = `$releaseDir
        artifacts = @()
        deployedArtifacts = @()
        output = @()
        amazeOutput = @()
    }
    gpuPlaybackReconBackend = `$gpuPlaybackReconBackend
    cudaAmazeLiveTileStreams = `$cudaAmazeLiveTileStreams
    cudaAmazeFastLaunchChecks = `$cudaAmazeFastLaunchChecks
    cudaAmazeLiveDirectRgbaStore = `$cudaAmazeLiveDirectRgbaStore
    dropFrameMode = `$dropFrameMode
    gpuPlaybackReconRetainDeviceOutput = `$gpuPlaybackReconRetainDeviceOutput
    gpuTexNrAcquireLatestReady = `$gpuTexNrAcquireLatestReady
    gpuTexNrImmediateDrainReady = `$gpuTexNrImmediateDrainReady
    phase3FrameSlots = `$phase3FrameSlots
    playbackTimerPollMs = `$playbackTimerPollMs
    playbackRenderLookaheadFrames = `$playbackRenderLookaheadFrames
    validatorExitCode = `$null
    packetInfo = `$null
}
Write-ProvenanceFile
try {
    if (!(Test-Path -LiteralPath `$validator)) {
        throw "Missing validator script: `$validator"
    }
    if (-not `$skipBackendBuild) {
        if (!(Test-Path -LiteralPath `$backendBuildScript)) {
            throw "Missing backend build script: `$backendBuildScript"
        }
        if (!(Test-Path -LiteralPath `$amazeBackendBuildScript)) {
            throw "Missing AMaZE backend build script: `$amazeBackendBuildScript"
        }
        `$backendOutput = & `$psExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `$backendBuildScript -Dir `$backendDir 2>&1
        `$backendExit = `$LASTEXITCODE
        `$payload.backend.buildExitCode = `$backendExit
        `$payload.backend.output = @(`$backendOutput | ForEach-Object { [string]`$_ })
        `$amazeBackendOutput = & `$psExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `$amazeBackendBuildScript -Dir `$backendDir -Arch portable 2>&1
        `$amazeBackendExit = `$LASTEXITCODE
        `$payload.backend.amazeBuildExitCode = `$amazeBackendExit
        `$payload.backend.amazeOutput = @(`$amazeBackendOutput | ForEach-Object { [string]`$_ })
        `$payload.backend.artifacts = @(Get-ChildItem -LiteralPath `$backendDir -File -ErrorAction SilentlyContinue |
            Where-Object { `$_.Name -match '^(igpu_recon_cuda\.(dll|lib|exp|arch\.json)|igpu_amaze_debayer_cuda\.(dll|lib|exp)|dll_test\.exe|amaze_dll_test\.exe)$' } |
            Sort-Object Name |
            ForEach-Object {
                [ordered]@{
                    name = `$_.Name
                    path = `$_.FullName
                    length = `$_.Length
                    sha256 = (Get-FileHash -LiteralPath `$_.FullName -Algorithm SHA256).Hash
                }
            })
        if (`$backendExit -ne 0) {
            throw "Backend DLL build failed with exit code `$backendExit"
        }
        if (`$amazeBackendExit -ne 0) {
            throw "AMaZE backend DLL build failed with exit code `$amazeBackendExit"
        }
        if (!(Test-Path -LiteralPath `$backendDll)) {
            throw "Backend DLL build did not produce `$backendDll"
        }
        if (!(Test-Path -LiteralPath `$backendArchSidecar)) {
            throw "Backend DLL build did not produce architecture sidecar `$backendArchSidecar"
        }
        if (!(Test-Path -LiteralPath `$amazeBackendDll)) {
            throw "AMaZE backend DLL build did not produce `$amazeBackendDll"
        }
        New-Item -ItemType Directory -Force -Path `$releaseDir | Out-Null
        `$deployTargets = @()
        `$deployTargets += [ordered]@{
            source = `$backendDll
            destination = (Join-Path `$releaseDir 'igpu_recon_cuda.dll')
        }
        `$deployTargets += [ordered]@{
            source = `$backendArchSidecar
            destination = (Join-Path `$releaseDir 'igpu_recon_cuda.arch.json')
        }
        `$deployTargets += [ordered]@{
            source = `$amazeBackendDll
            destination = (Join-Path `$releaseDir 'igpu_amaze_debayer_cuda.dll')
        }
        `$cudaRoot = (Get-ChildItem 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA' -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            Select-Object -First 1).FullName
        if (`$cudaRoot) {
            `$cudart = Get-ChildItem -LiteralPath (Join-Path `$cudaRoot 'bin') -Filter 'cudart64_*.dll' -File -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                Select-Object -First 1
            if (`$cudart) {
                `$deployTargets += [ordered]@{
                    source = `$cudart.FullName
                    destination = (Join-Path `$releaseDir `$cudart.Name)
                }
            }
        }
        foreach (`$target in `$deployTargets) {
            Copy-Item -LiteralPath ([string]`$target.source) -Destination ([string]`$target.destination) -Force
        }
        `$payload.backend.deployedArtifacts = @(`$deployTargets | ForEach-Object {
            `$item = Get-Item -LiteralPath ([string]`$_.destination)
            [ordered]@{
                name = `$item.Name
                path = `$item.FullName
                length = `$item.Length
                sha256 = (Get-FileHash -LiteralPath `$item.FullName -Algorithm SHA256).Hash
            }
        })
        `$dllSha256 = (@(`$payload.backend.artifacts | Where-Object { `$_.name -eq 'igpu_recon_cuda.dll' }) | Select-Object -First 1).sha256
        # The PR #67 validator requires a lowercase digest; Get-FileHash returns uppercase (GPU-PROVENANCE-SELF-BINDING-1).
        if (`$dllSha256) { `$dllSha256 = ([string]`$dllSha256).ToLowerInvariant() }
        `$pendingSymbolPresence = Test-BackendDllExportsPending -DllPath `$backendDll
        `$payload.provenance.dllSha256 = `$dllSha256
        `$payload.provenance.pendingSymbolPresence = `$pendingSymbolPresence
        Write-ProvenanceFile
    }
    `$args = @(
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        `$validator,
        '-RepoRoot',
        `$repo,
        '-ExpectedHostName',
        `$expectedHost,
        '-EvidencePacketPath',
        `$packet,
        '-EvidenceRepoHead',
        `$evidenceRepoHead,
        '-EvidenceBranch',
        `$evidenceBranch,
        '-EvidenceGitStatus',
        `$evidenceGitStatus,
        '-Seconds',
        '$( [string]$Seconds )',
        '-SettleMs',
        '$( [string]$SettleMs )',
        '-ValidationSampleEvery',
        [string]`$validationSampleEvery
    )
    if (-not [string]::IsNullOrWhiteSpace(`$gpuPlaybackReconBackend)) {
        `$args += @('-GpuPlaybackReconBackend', `$gpuPlaybackReconBackend)
    }
    if (`$cudaAmazeLiveTileStreams -gt 0) {
        `$args += @('-CudaAmazeLiveTileStreams', [string]`$cudaAmazeLiveTileStreams)
    }
    if (`$cudaAmazeFastLaunchChecks) {
        `$args += '-CudaAmazeFastLaunchChecks'
    }
    if (`$cudaAmazeLiveDirectRgbaStore) {
        `$args += '-CudaAmazeLiveDirectRgbaStore'
    }
    if (`$gpuPlaybackReconRetainDeviceOutput) {
        `$args += '-GpuPlaybackReconRetainDeviceOutput'
    }
    if (`$gpuTexNrAcquireLatestReady) {
        `$args += '-GpuTexNrAcquireLatestReady'
    }
    if (`$gpuTexNrImmediateDrainReady) {
        `$args += '-GpuTexNrImmediateDrainReady'
    }
    if (`$gpuTexNrImmediateDrainMax -gt 0) {
        `$args += @('-GpuTexNrImmediateDrainMax', [string]`$gpuTexNrImmediateDrainMax)
    }
    if (`$phase3FrameSlots -gt 0) {
        `$args += @('-Phase3FrameSlots', [string]`$phase3FrameSlots)
    }
    if (`$playbackTimerPollMs -gt 0) {
        `$args += @('-PlaybackTimerPollMs', [string]`$playbackTimerPollMs)
    }
    if (`$playbackRenderLookaheadFrames -gt 0) {
        `$args += @('-PlaybackRenderLookaheadFrames', [string]`$playbackRenderLookaheadFrames)
    }
    `$args += @('-DropFrameMode', `$dropFrameMode)
    if (`$clipPaths.Count -gt 0) {
        `$args += '-ClipPaths'
        `$args += `$clipPaths
    }
    else {
        `$args += '-ClipRoot'
        `$args += `$clipRoot
        `$args += '-ClipNames'
        `$args += `$clipNames
    }
    if ($skipBuildLiteral) {
        `$args += '-SkipBuild'
    }
    if (`$speedLeg) {
        `$args += '-SpeedLeg'
    }
    `$output = & `$psExe @args 2>&1
    `$exitCode = `$LASTEXITCODE
    `$payload.validatorExitCode = `$exitCode
    `$payload.output = @(`$output | ForEach-Object { [string]`$_ })
    if (Test-Path -LiteralPath `$packet) {
        `$item = Get-Item -LiteralPath `$packet
        `$payload.packetInfo = [ordered]@{
            path = `$item.FullName
            length = `$item.Length
            sha256 = (Get-FileHash -LiteralPath `$item.FullName -Algorithm SHA256).Hash
        }
    }
    `$payload.endedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    `$payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath `$jobOutput -Encoding UTF8
    `$output | ForEach-Object { [string]`$_ }
    exit `$exitCode
}
catch {
    `$payload.error = [string]`$_.Exception.Message
    `$payload.endedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    `$payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath `$jobOutput -Encoding UTF8
    Write-Error `$_.Exception.Message
    exit 1
}
"@

        $agentSubmission = Submit-AgentJob `
            -Root $AgentRoot `
            -JobId $jobId `
            -ScriptText $jobScript `
            -TimeoutSec $AgentTimeoutSec
        if ($agentSubmission.timedOut) {
            Add-Failure $failures "Timed out waiting for UltraMagnus SMB agent job '$jobId' after $AgentTimeoutSec seconds."
        }
        else {
            $agentExitCode = Convert-AgentExitCode $agentSubmission.result.exitCode
            if ($agentExitCode -ne 0) {
                Add-Failure $failures "UltraMagnus SMB agent job '$jobId' exited with code $agentExitCode."
            }
        }
    }

    if (Test-Path -LiteralPath $remoteJobOutputShare) {
        $remoteJobOutput = Get-Content -LiteralPath $remoteJobOutputShare -Raw | ConvertFrom-Json
    }

    if ($failures.Count -eq 0 -and $remoteJobOutput) {
        if ($remoteJobOutput.validatorExitCode -ne 0) {
            Add-Failure $failures "Remote UltraMagnus validator exited with code $($remoteJobOutput.validatorExitCode)."
        }
    }
    elseif ($failures.Count -eq 0) {
        Add-Failure $failures "Remote job did not publish job output: $remoteJobOutputShare"
    }

    if ($failures.Count -eq 0) {
        if (!(Test-Path -LiteralPath $remotePacketShare)) {
            Add-Failure $failures "Remote validator did not produce an evidence packet at '$remotePacketShare'."
        }
        else {
            $localPacketPath = Join-Path $localPacketRootResolved ("$RemoteHostName-$stamp-" + (Split-Path -Leaf $remotePacketShare))
            Copy-Item -LiteralPath $remotePacketShare -Destination $localPacketPath -Force
            $localPacket = Get-Item -LiteralPath $localPacketPath
            $remotePacketHash = (Get-FileHash -LiteralPath $remotePacketShare -Algorithm SHA256).Hash
            $copiedPacket = [pscustomobject]@{
                path = $localPacket.FullName
                length = $localPacket.Length
                sha256 = (Get-FileHash -LiteralPath $localPacket.FullName -Algorithm SHA256).Hash
                remote = [pscustomobject]@{
                    path = $remotePacketShare
                    length = (Get-Item -LiteralPath $remotePacketShare).Length
                    sha256 = $remotePacketHash
                }
            }
            if ($copiedPacket.sha256 -ne $remotePacketHash) {
                Add-Failure $failures "Copied packet hash mismatch: remote $remotePacketHash, local $($copiedPacket.sha256)."
            }
        }
    }

    if ($failures.Count -eq 0) {
        $importResult = Invoke-ImportVerifier `
            -Repo $repo `
            -ValidatorScript $validatorScript `
            -PacketPath ([string]$copiedPacket.path) `
            -ImportRoot $importOutputRootResolved `
            -ExpectedHost $ExpectedEvidenceHostName
        if ($importResult.exitCode -ne 0) {
            Add-Failure $failures "Import verifier rejected copied packet with exit code $($importResult.exitCode)."
        }
    }
}

$status = if ($failures.Count -eq 0) { "success" } else { "failed" }
$summary = [pscustomobject]@{
    schema = "mlvapp-ultramagnus-p3-remote-evidence.v2"
    capturedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    status = $status
    localHost = [Environment]::MachineName
    transport = "smb-agent"
    remoteHost = $RemoteHostName
    expectedEvidenceHost = $ExpectedEvidenceHostName
    agentRoot = $AgentRoot
    agentHeartbeat = $agentHeartbeat
    remoteStagingShare = $RemoteStagingShare
    remoteStagingLocal = $RemoteStagingLocal
    remoteRepoRoot = $RemoteRepoRoot
    remoteRepoShare = $remoteRepoShare
    remotePacketPath = $RemotePacketPath
    remotePacketShare = $remotePacketShare
    localPacketRoot = $localPacketRootResolved
    stageResult = $stageResult
    agentSubmission = $agentSubmission
    remoteJobOutput = $remoteJobOutput
    copiedPacket = $copiedPacket
    importResult = $importResult
    options = [pscustomobject]@{
        gpuPlaybackReconBackend = $GpuPlaybackReconBackend
        gpuTexNrAcquireLatestReady = [bool]$GpuTexNrAcquireLatestReady
        gpuTexNrImmediateDrainReady = [bool]$GpuTexNrImmediateDrainReady
        gpuTexNrImmediateDrainMax = if ($GpuTexNrImmediateDrainMax -gt 0) { $GpuTexNrImmediateDrainMax } else { $null }
        phase3FrameSlots = if ($Phase3FrameSlots -gt 0) { $Phase3FrameSlots } else { $null }
        playbackTimerPollMs = if ($PlaybackTimerPollMs -gt 0) { $PlaybackTimerPollMs } else { $null }
        playbackRenderLookaheadFrames = if ($PlaybackRenderLookaheadFrames -gt 0) { $PlaybackRenderLookaheadFrames } else { $null }
        validationSampleEvery = $ValidationSampleEvery
    }
    warnings = @($warnings)
    failures = @($failures)
}

Write-JsonFile -Value $summary -Path $summaryPath
$summary | ConvertTo-Json -Depth 14

if ($status -eq "success") {
    exit 0
}
exit 2
