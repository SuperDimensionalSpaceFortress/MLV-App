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
# ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A6): the speed-authority predicate the importer shares
Import-Module (Join-Path $PSScriptRoot "FpsArmGate.psm1") -Force

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

function Get-RealFilePath {
    # The absolute path of a file with the first symbolic link or directory junction on its way (the file itself or any ancestor) resolved, so two spellings of one file compare equal.
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $below = [System.Collections.Generic.List[string]]::new()
    $current = $full
    while ($true) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and $null -ne $item.LinkTarget) {
            $target = $item.ResolveLinkTarget($true)
            if ($null -ne $target) {
                $resolved = $target.FullName
                for ($k = $below.Count - 1; $k -ge 0; $k--) { $resolved = Join-Path $resolved $below[$k] }
                return [IO.Path]::GetFullPath($resolved)
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $below.Add([IO.Path]::GetFileName($current))
        $current = $parent
    }
    $full
}

function Invoke-ProofSummarizer {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string]$Summary,
        [Parameter(Mandatory = $true)][string]$MarkdownReport,
        [Parameter(Mandatory = $true)][string]$JsonReport
    )

    # ENVELOPE-PREEMPT-R1-RESIDUE-1 r2 (hosted run 45786756378, Linux): a backslash is a file-name character on Linux, so the repo-relative path is joined with forward slashes (also valid on
    # Windows), and the child is `pwsh.exe` only where the directory separator is a backslash.
    $summaryScript = Join-Path $Repo "tools/profiling/summarize-local-cuda-proof.ps1"
    # (adversary 1ce472bc A2: the child is the running PowerShell's own binary under $PSHOME, so a pwsh that is not on PATH still launches; the bare name is the last resort)
    $pwshLeaf = if ([IO.Path]::DirectorySeparatorChar -eq [char]92) { "pwsh.exe" } else { "pwsh" }
    $pwshExe = Join-Path $PSHOME $pwshLeaf
    if (!(Test-Path -LiteralPath $pwshExe -PathType Leaf)) { $pwshExe = $pwshLeaf }
    if (!(Test-Path -LiteralPath $summaryScript -PathType Leaf)) {
        throw "Proof summarizer not found: $summaryScript"
    }
    # (adversary 1ce472bc A1: a report path that is the input summary (a -ReportPath named like the summary, whose .json twin is the summary) or the other report would be deleted by the
    # cleanup below or overwritten by a child, so it is refused before anything is removed; a directory given as a report path is refused for the same reason)
    $comparison = if ([IO.Path]::DirectorySeparatorChar -eq [char]92) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    # (adversary 698fffb1 A1: compared by REAL path, so a directory junction or symlink in front of a report path cannot hide that it is the input summary)
    $summaryFull = Get-RealFilePath $Summary
    $markdownFull = Get-RealFilePath $MarkdownReport
    $jsonFull = Get-RealFilePath $JsonReport
    foreach ($candidate in @($markdownFull, $jsonFull)) {
        if ([string]::Equals($candidate, $summaryFull, $comparison)) {
            throw "Proof report path $candidate is the input summary; refusing before anything is removed."
        }
    }
    if ([string]::Equals($markdownFull, $jsonFull, $comparison)) {
        throw "The Markdown and JSON proof reports share the path $markdownFull; refusing."
    }
    foreach ($candidate in @($markdownFull, $jsonFull)) {
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            throw "Proof report path $candidate is a directory; refusing."
        }
    }
    # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 8747fced A5): exit 1 also means the summarizer refused its input before writing (a repeated key), so a report an EARLIER run left here would be
    # packaged as this summary's. Both reports are removed first, and each must exist after its run.
    foreach ($stale in @($MarkdownReport, $JsonReport)) {
        if (Test-Path -LiteralPath $stale -PathType Leaf) { Remove-Item -LiteralPath $stale -Force }
    }
    # (adversary 1ce472bc A7: each child writes a name only this run knows, and the report is published from it, so a report another packager wrote at the same path between this run's two
    # children can never be taken as this run's own)
    # (adversary 698fffb1 A8: the temp name is a fixed-length name in the report's own directory, never the report's basename plus a suffix, so a report basename at the filesystem's limit still works)
    $token = [guid]::NewGuid().ToString("N")
    $markdownTemp = Join-Path ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($MarkdownReport))) "$token.md.tmp"
    $jsonTemp = Join-Path ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($JsonReport))) "$token.json.tmp"
    try {
        & $pwshExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
            -File $summaryScript `
            -RepoRoot $Repo `
            -SummaryPath $Summary `
            -Output $markdownTemp | Out-Null
        $markdownExit = $LASTEXITCODE
        if ($markdownExit -notin @(0, 1)) {
            throw "Markdown proof report generation exited with code $markdownExit."
        }
        if (!(Test-Path -LiteralPath $markdownTemp -PathType Leaf)) {
            throw "Markdown proof report generation exited with code $markdownExit and wrote no report at $MarkdownReport (the summary was refused)."
        }

        & $pwshExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
            -File $summaryScript `
            -RepoRoot $Repo `
            -SummaryPath $Summary `
            -Output $jsonTemp `
            -Json | Out-Null
        $jsonExit = $LASTEXITCODE
        if ($jsonExit -notin @(0, 1)) {
            throw "JSON proof report generation exited with code $jsonExit."
        }
        if (!(Test-Path -LiteralPath $jsonTemp -PathType Leaf)) {
            throw "JSON proof report generation exited with code $jsonExit and wrote no report at $JsonReport (the summary was refused)."
        }
        # (adversary 698fffb1 A2: two packagers on one RunRoot share the final report names, so this run keeps its OWN bytes and hashes from the private temp files: the caller judges and packages
        # those, never a re-read of the shared final path. The pair is published all or nothing, and a final report another packager replaced after this run published it is refused, not read.)
        $markdownBytes = [IO.File]::ReadAllBytes($markdownTemp)
        $jsonBytes = [IO.File]::ReadAllBytes($jsonTemp)
        $markdownSha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($markdownBytes)).ToLowerInvariant()
        $jsonSha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($jsonBytes)).ToLowerInvariant()
        Move-Item -LiteralPath $markdownTemp -Destination $MarkdownReport -Force
        try {
            Move-Item -LiteralPath $jsonTemp -Destination $JsonReport -Force
        }
        catch {
            # (sol c29f8dc1 A4: the rollback removes the Markdown report only while it is still THIS run's bytes: a rival packager that published its own pair in between keeps its report, and a removal
            # that fails is said in the error, never swallowed)
            $moveError = $_
            $rollbackNote = ""
            try {
                if (Test-Path -LiteralPath $MarkdownReport -PathType Leaf) {
                    $publishedNow = (Get-FileHash -LiteralPath $MarkdownReport -Algorithm SHA256).Hash
                    if ([string]::Equals($publishedNow, $markdownSha256, [StringComparison]::OrdinalIgnoreCase)) { Remove-Item -LiteralPath $MarkdownReport -Force -ErrorAction Stop }
                }
            }
            catch {
                $rollbackNote = " (the Markdown report this run published could not be rolled back: $($_.Exception.Message))"
            }
            throw [System.InvalidOperationException]::new("Publishing the JSON proof report failed: $($moveError.Exception.Message)$rollbackNote", $moveError.Exception)
        }
        foreach ($published in @(@($MarkdownReport, $markdownSha256), @($JsonReport, $jsonSha256))) {
            $publishedSha256 = if (Test-Path -LiteralPath $published[0] -PathType Leaf) { (Get-FileHash -LiteralPath $published[0] -Algorithm SHA256).Hash.ToLowerInvariant() } else { '' }
            if ($publishedSha256 -cne $published[1]) {
                throw "The proof report $($published[0]) was replaced by another process after this run published it; refusing to package a report this run did not write."
            }
        }
        $jsonText = [Text.UTF8Encoding]::new($false).GetString($jsonBytes).TrimStart([char]0xFEFF)
    }
    finally {
        foreach ($leftover in @($markdownTemp, $jsonTemp)) {
            if (Test-Path -LiteralPath $leftover -PathType Leaf) { Remove-Item -LiteralPath $leftover -Force -ErrorAction SilentlyContinue }
        }
    }

    [pscustomobject]@{
        markdownExitCode = $markdownExit
        jsonExitCode = $jsonExit
        jsonText = $jsonText
        markdownSha256 = $markdownSha256
        jsonSha256 = $jsonSha256
    }
}

function Assert-OwnedProofReports {
    # R2-RESIDUE-1 r2 (sol c29f8dc1 A3): the two reports at their shared final paths are still the bytes THIS run wrote (the hashes Invoke-ProofSummarizer returned). A rival packager on the same
    # RunRoot can replace them after the publication check, so the manifest and the archive must not take whatever is there now for this run's own.
    param(
        [Parameter(Mandatory = $true)][string]$MarkdownReport,
        [Parameter(Mandatory = $true)][string]$JsonReport,
        [Parameter(Mandatory = $true)]$Result
    )
    foreach ($owned in @(@($MarkdownReport, [string]$Result.markdownSha256), @($JsonReport, [string]$Result.jsonSha256))) {
        $actual = if (Test-Path -LiteralPath $owned[0] -PathType Leaf) { (Get-FileHash -LiteralPath $owned[0] -Algorithm SHA256).Hash } else { '' }
        if ([string]::IsNullOrEmpty($owned[1]) -or [string]::IsNullOrEmpty($actual) -or -not [string]::Equals($actual, $owned[1], [StringComparison]::OrdinalIgnoreCase)) {
            throw "The proof report $($owned[0]) is not the report this run wrote (replaced by another process, or removed); refusing to package a report this run did not write."
        }
    }
}

function Get-OwnedReportArtifact {
    # the manifest's own entry for one report: its sha256 must be the hash this run wrote, so the manifest never records another run's report as this one's (sol c29f8dc1 A3)
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$OwnedSha256
    )
    $artifact = Get-FileArtifact -Path $Path
    if ([string]::IsNullOrEmpty($OwnedSha256) -or [string]::IsNullOrEmpty([string]$artifact.sha256) -or -not [string]::Equals([string]$artifact.sha256, $OwnedSha256, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The proof report $Path is not the report this run wrote (replaced by another process, or removed); refusing to package a report this run did not write."
    }
    $artifact
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
        $SummaryPath = Join-Path $root ".claude-state/profiling/local-cuda-playback-dng-smoke-latest.json"
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
# (adversary 698fffb1 A2: the report this run judges is the text of the bytes it wrote, not a re-read of a path another packager may have replaced)
$proofReportRaw = $summarizerResult.jsonText
$proofReport = $proofReportRaw | ConvertFrom-Json -Depth 100

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
        markdown = Get-OwnedReportArtifact -Path $ReportPath -OwnedSha256 $summarizerResult.markdownSha256
        json = Get-OwnedReportArtifact -Path $jsonReportPath -OwnedSha256 $summarizerResult.jsonSha256
        status = $proofReport.status
        # r15 (adversary r14 pass 18 A6): speed authority is the report's claimAuthority.speed (exactly $true), never the support status; a report without it grants none
        # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A6): one predicate with the importer (Test-FpsProofReportSpeedQuotable): the PASS playbackSpeed section, no singleton arrays, no repeated key
        speedQuotable = (Test-FpsProofReportSpeedQuotable -Report $proofReport -RawText $proofReportRaw)
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
# (sol c29f8dc1 A3: the file listing above hashed the shared report paths a second time; they must still be this run's own bytes before the manifest is written)
Assert-OwnedProofReports -MarkdownReport $ReportPath -JsonReport $jsonReportPath -Result $summarizerResult
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
# (sol c29f8dc1 A3: the archive takes the run root as it is, so the reports are checked as this run's own right before it and right after it; a packet built over another run's report is removed, never kept)
Assert-OwnedProofReports -MarkdownReport $ReportPath -JsonReport $jsonReportPath -Result $summarizerResult
Compress-Archive -Path (Join-Path $runRootResolved "*") -DestinationPath $packetFullPath -Force
try {
    Assert-OwnedProofReports -MarkdownReport $ReportPath -JsonReport $jsonReportPath -Result $summarizerResult
}
catch {
    Remove-Item -LiteralPath $packetFullPath -Force -ErrorAction SilentlyContinue
    throw
}
$packetArtifact = Get-FileArtifact -Path $packetFullPath
$manifest["outputs"]["packet"] = $packetArtifact.path
$manifest["packet"] = $packetArtifact
Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath

$manifest | ConvertTo-Json -Depth 16
if ($StrictQuotable -and [string]$proofReport.status -ne "QUOTABLE_PASS") {
    exit 1
}
