param(
    [string]$RepoRoot = ".",
    [string]$RunRoot = "",
    [string]$SummaryPath = "",
    [string]$ReportPath = "",
    [string]$PacketPath = "",
    [switch]$StrictQuotable,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

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

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
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
        sha256 = Get-FileSha256 -Path $item.FullName
    }
}

function Get-RelativeArtifactPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    return ([System.IO.Path]::GetRelativePath($Root, $Path) -replace '\\', '/')
}

function Get-ArtifactFileEntries {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootPath = (Resolve-Path -LiteralPath $Root).Path
    return @(Get-ChildItem -LiteralPath $rootPath -Recurse -File | Sort-Object FullName | ForEach-Object {
        [pscustomobject]@{
            relativePath = Get-RelativeArtifactPath -Root $rootPath -Path $_.FullName
            length = $_.Length
            sha256 = Get-FileSha256 -Path $_.FullName
        }
    })
}

function Get-GitScalar {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string[]]$Args
    )
    $value = & git -C $Repo @Args 2>$null
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    [string]$value
}

function Get-GitStatusLines {
    param([Parameter(Mandatory = $true)][string]$Repo)
    @(& git -C $Repo status --short --branch 2>$null | ForEach-Object { [string]$_ })
}

function Invoke-ProofSummarizer {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string]$Summary,
        [Parameter(Mandatory = $true)][string]$MarkdownReport,
        [Parameter(Mandatory = $true)][string]$JsonReport
    )

    $summaryScript = Join-Path $Repo "tools\profiling\summarize-local-cuda-proof.ps1"
    if (!(Test-Path -LiteralPath $summaryScript -PathType Leaf)) {
        throw "Proof summarizer not found: $summaryScript"
    }

    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $summaryScript `
        -RepoRoot $Repo `
        -SummaryPath $Summary `
        -Output $MarkdownReport | Out-Null
    $markdownExit = $LASTEXITCODE
    if ($markdownExit -notin @(0, 1)) {
        throw "Markdown proof report generation exited with code $markdownExit."
    }

    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $summaryScript `
        -RepoRoot $Repo `
        -SummaryPath $Summary `
        -Output $JsonReport `
        -Json | Out-Null
    $jsonExit = $LASTEXITCODE
    if ($jsonExit -notin @(0, 1)) {
        throw "JSON proof report generation exited with code $jsonExit."
    }

    [pscustomobject]@{
        markdownExitCode = $markdownExit
        jsonExitCode = $jsonExit
    }
}

function Convert-ToInt64 {
    param([object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return 0L
    }
    [long]$Value
}

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
    if ([string]::IsNullOrWhiteSpace($RunRoot)) {
        $SummaryPath = Join-Path $root ".claude-state\profiling\local-cuda-playback-dng-smoke-latest.json"
    }
    else {
        $SummaryPath = Join-Path (Resolve-RepoPath -Root $root -Path $RunRoot) "summary.json"
    }
}
else {
    $SummaryPath = Resolve-RepoPath -Root $root -Path $SummaryPath
}
if (!(Test-Path -LiteralPath $SummaryPath -PathType Leaf)) {
    throw "Summary not found: $SummaryPath"
}

$summaryFullPath = (Resolve-Path -LiteralPath $SummaryPath).Path
if ([string]::IsNullOrWhiteSpace($RunRoot)) {
    $RunRoot = Split-Path -Parent $summaryFullPath
}
else {
    $RunRoot = Resolve-RepoPath -Root $root -Path $RunRoot
}
if (!(Test-Path -LiteralPath $RunRoot -PathType Container)) {
    throw "Run root not found: $RunRoot"
}
$runRootResolved = (Resolve-Path -LiteralPath $RunRoot).Path

if ([string]::IsNullOrWhiteSpace($ReportPath)) {
    $ReportPath = Join-Path $runRootResolved "proof-report.md"
}
else {
    $ReportPath = Resolve-RepoPath -Root $root -Path $ReportPath
}
$jsonReportPath = [System.IO.Path]::ChangeExtension($ReportPath, ".json")

$summary = Get-Content -LiteralPath $summaryFullPath -Raw | ConvertFrom-Json -Depth 100
$summarizerResult = Invoke-ProofSummarizer `
    -Repo $root `
    -Summary $summaryFullPath `
    -MarkdownReport $ReportPath `
    -JsonReport $jsonReportPath
$proofReport = Get-Content -LiteralPath $jsonReportPath -Raw | ConvertFrom-Json -Depth 100

$hostSafe = if ([string]::IsNullOrWhiteSpace([string]$summary.host.name)) {
    [Environment]::MachineName
}
else {
    [string]$summary.host.name
}
$hostSafe = ($hostSafe -replace '[^A-Za-z0-9_.-]', '-')
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
if ([string]::IsNullOrWhiteSpace($PacketPath)) {
    $packetParent = Split-Path -Parent $runRootResolved
    $PacketPath = Join-Path $packetParent "mlvapp-local-cuda-proof-$hostSafe-$stamp.zip"
}
else {
    $PacketPath = Resolve-RepoPath -Root $root -Path $PacketPath
}
$packetFullPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PacketPath)

$manifestPath = Join-Path $runRootResolved "local-cuda-proof-packet-manifest.json"
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    Remove-Item -LiteralPath $manifestPath -Force
}

$sourceHead = Get-GitScalar -Repo $root -Args @("rev-parse", "HEAD")
$sourceBranch = Get-GitScalar -Repo $root -Args @("rev-parse", "--abbrev-ref", "HEAD")
$sourceStatus = Get-GitStatusLines -Repo $root

$manifest = [ordered]@{
    schema = "mlvapp-local-cuda-proof-packet.v1"
    status = if ($DryRun) { "planned" } else { "creating" }
    createdAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    dryRun = [bool]$DryRun
    source = [ordered]@{
        host = $summary.host.name
        repoRoot = $root
        repoHead = $sourceHead
        branch = $sourceBranch
        gitStatus = @($sourceStatus)
    }
    summary = [ordered]@{
        path = $summaryFullPath
        artifact = Get-FileArtifact -Path $summaryFullPath
        schema = $summary.schema
        status = $summary.status
        dryRun = [bool]$summary.dryRun
        capturedAtUtc = $summary.capturedAtUtc
    }
    proofReport = [ordered]@{
        markdown = Get-FileArtifact -Path $ReportPath
        json = Get-FileArtifact -Path $jsonReportPath
        status = $proofReport.status
        # r15 (adversary r14 pass 18 A6): speed authority is the report's claimAuthority.speed (exactly $true), never the support status; a report without it grants none
        speedQuotable = (($proofReport.status -ceq "QUOTABLE_PASS") -and ($null -ne $proofReport.claimAuthority) -and ($proofReport.claimAuthority.speed -is [bool]) -and $proofReport.claimAuthority.speed)
        claimAuthority = $proofReport.claimAuthority
        topLevelStatus = $proofReport.topLevelStatus
        markdownExitCode = $summarizerResult.markdownExitCode
        jsonExitCode = $summarizerResult.jsonExitCode
        blockers = @($proofReport.blockers)
        warnings = @($proofReport.warnings)
        diagnosticSummary = $proofReport.diagnosticSummary
        diagnosticCodes = @($proofReport.diagnostics | ForEach-Object { [string]$_.code } | Where-Object {
                -not [string]::IsNullOrWhiteSpace($_)
            } | Select-Object -Unique)
        actionPlan = $proofReport.actionPlan
    }
    proof = [ordered]@{
        nvidiaRows = @($summary.host.nvidiaSmi.rows)
        releaseExeSha256 = $summary.artifacts.exe.sha256
        backendSha256 = $summary.artifacts.backend.sha256
        cudartSha256 = $summary.artifacts.cudart.sha256
        playbackStatus = $summary.proof.playback.status
        playbackCorrectnessValidated = [bool]$summary.proof.playback.correctnessValidated
        totalGpuTextureNoReadbackFrames = Convert-ToInt64 $summary.proof.playback.totalGpuTextureNoReadbackFrames
        totalFallbackFrames = Convert-ToInt64 $summary.proof.playback.totalFallbackFrames
        totalGlProbeActiveFrames = Convert-ToInt64 $summary.proof.playback.totalGlProbeActiveFrames
        totalGlParityMismatches = Convert-ToInt64 $summary.proof.playback.totalGlParityMismatches
        playbackAbStatus = $summary.proof.playbackAb.status
        cdngVerdict = $summary.proof.cdng.verdict
        dngHashVerdict = $summary.proof.cdng.dngHash.verdict
        candidateFrameCount = Convert-ToInt64 $summary.proof.cdng.candidateFrameCount
        candidateGpuExportAttemptedFrames = Convert-ToInt64 $summary.proof.cdng.candidateGpuExportAttemptedFrames
        candidateGpuExportReplacedFrames = Convert-ToInt64 $summary.proof.cdng.candidateGpuExportReplacedFrames
        candidateGpuExportTrustedFrames = Convert-ToInt64 $summary.proof.cdng.candidateGpuExportTrustedFrames
        candidateUseAsyncWriter = [bool]$summary.proof.cdng.asyncWriter.candidateUseAsyncWriter
        candidateUseAsyncWriterCompression = [bool]$summary.proof.cdng.asyncWriter.candidateUseAsyncWriterCompression
        candidateAsyncWriterOverlapRuns = Convert-ToInt64 $summary.proof.cdng.asyncWriter.candidateAsyncWriterOverlapRuns
        candidateAsyncWriterMaxActive = Convert-ToInt64 $summary.proof.cdng.asyncWriter.candidateAsyncWriterMaxActive
        candidateAsyncWriterMaxQueued = Convert-ToInt64 $summary.proof.cdng.asyncWriter.candidateAsyncWriterMaxQueued
        candidateAsyncWriterJobsStarted = Convert-ToInt64 $summary.proof.cdng.asyncWriter.candidateAsyncWriterJobsStarted
        candidateAsyncWriterJobsFinished = Convert-ToInt64 $summary.proof.cdng.asyncWriter.candidateAsyncWriterJobsFinished
        dngThroughputClassificationStatus = $summary.proof.cdng.throughputClassification.status
        dngThroughputClassifiedRuns = Convert-ToInt64 $summary.proof.cdng.throughputClassification.classifiedRunCount
        dngThroughputWallClockImprovedRuns = Convert-ToInt64 $summary.proof.cdng.throughputClassification.wallClockImprovedCount
        dngThroughputAttributionHoldRuns = Convert-ToInt64 $summary.proof.cdng.throughputClassification.attributionHoldCount
        dngThroughputDefaultPromotionCandidateRuns = Convert-ToInt64 $summary.proof.cdng.throughputClassification.defaultPromotionCandidateCount
        dngThroughputSuggestedOptimization = $summary.proof.cdng.throughputClassification.suggestedOptimization
        dngThroughputByCodec = if ($summary.proof.cdng.throughputClassificationByCodec) { @($summary.proof.cdng.throughputClassificationByCodec) } else { @() }
        dngThroughputByCase = if ($summary.proof.cdng.throughputClassificationByCase) { @($summary.proof.cdng.throughputClassificationByCase) } else { @() }
    }
    outputs = [ordered]@{
        runRoot = $runRootResolved
        packet = if ($DryRun) { $packetFullPath } else { $null }
        manifest = $manifestPath
    }
    files = @()
    proofBoundary = [ordered]@{
        packetOnly = $true
        provesDellSupport = $false
        provesRealtimePlayback = $false
        provesDngHashParity = $false
        notes = @(
            "This packet preserves the host-local CUDA proof outputs and their hashes.",
            "Quote hardware support only from proofReport.status=QUOTABLE_PASS for the same host being claimed; quote playback speed only when proofReport.speedQuotable is also true (a REFUSED playback-speed section carries no speed authority).",
            "A NOT_QUOTABLE packet is still useful for diagnosing fallback, missing NVIDIA/GL proof, DNG hash failures, or dry-run planning."
        )
    }
}

Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath
$manifest["files"] = @(Get-ArtifactFileEntries -Root $runRootResolved | Where-Object {
    $_.relativePath -ne "local-cuda-proof-packet-manifest.json"
})
Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath

if ($DryRun) {
    $manifest["status"] = "planned"
    Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath
    $manifest | ConvertTo-Json -Depth 16
    if ($StrictQuotable -and [string]$proofReport.status -ne "QUOTABLE_PASS") {
        exit 1
    }
    exit 0
}

$packetParent = Split-Path -Parent $packetFullPath
if ($packetParent) {
    New-Item -ItemType Directory -Force -Path $packetParent | Out-Null
}
if (Test-Path -LiteralPath $packetFullPath -PathType Leaf) {
    Remove-Item -LiteralPath $packetFullPath -Force
}
$manifest["status"] = "created"
$manifest["outputs"]["packet"] = $packetFullPath
Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath
Compress-Archive -Path (Join-Path $runRootResolved "*") -DestinationPath $packetFullPath -Force
$packetArtifact = Get-FileArtifact -Path $packetFullPath
$manifest["outputs"]["packet"] = $packetArtifact.path
$manifest["packet"] = $packetArtifact
Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath

$manifest | ConvertTo-Json -Depth 16
if ($StrictQuotable -and [string]$proofReport.status -ne "QUOTABLE_PASS") {
    exit 1
}
