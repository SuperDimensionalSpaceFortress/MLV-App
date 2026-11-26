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

# R2-RESIDUE-1 r3 (sol 5add23cf A6, A7, A8; HUB RULING 2026-11-26 item 3): ONE STRUCTURE for every concurrent-rival race. Rounds r15 to r2 added a check per race and each check opened the next one.
#   1. summary.json is read ONCE into memory; its hash, its parse (host, source) and the copy the two summarizer children read are all those bytes (A8).
#   2. everything this run writes (the two reports, the manifest, the ZIP) is written in a fresh PRIVATE directory this run creates with a create-new call (it fails if the name exists); every hash and the
#      archive are taken from that directory or from the bytes held in memory, never from a name another packager can also write (A6).
#   3. the run publishes at the very end, ONE move per file. Nothing is removed at a shared name (no stale-report cleanup, no manifest or packet removal, no rollback of a published file): the only
#      deletes are of the files inside this run's own private directory, then that directory (A7).
# A finding that does not go green from this structure is left open, not given a special case.

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

function Get-BytesSha256 {
    # the upper-case hex Get-FileHash writes, of bytes this run holds
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Bytes))
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

function New-BytesArtifact {
    # the manifest entry of a file this run holds as bytes: the path it will be published at, and the hash of the very bytes the packet carries
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][datetime]$LastWriteTime
    )
    [pscustomobject]@{
        path = $Path
        exists = $true
        length = $Bytes.Length
        lastWriteTime = $LastWriteTime
        sha256 = Get-BytesSha256 -Bytes $Bytes
    }
}

function Get-RelativeArtifactPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    return ([System.IO.Path]::GetRelativePath($Root, $Path) -replace '\\', '/')
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

function ConvertTo-PlainFilePath {
    # R2-RESIDUE-1 r3 (sol 5add23cf A5): on Windows \\?\C:\x and \\.\C:\x name the same file as C:\x, and \\?\UNC\srv\share\x the same as \\srv\share\x, so a report path spelled with the device
    # prefix could not be seen to be the input summary. The prefix is dropped for the two forms that have a plain spelling; anything else (\\?\Volume{guid}\..) is left as it is.
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([IO.Path]::DirectorySeparatorChar -ne [char]92) { return $Path }
    foreach ($prefix in @('\\?\UNC\', '\\.\UNC\')) {
        if ($Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return '\\' + $Path.Substring($prefix.Length) }
    }
    foreach ($prefix in @('\\?\', '\\.\')) {
        if ($Path.StartsWith($prefix, [StringComparison]::Ordinal) -and $Path.Length -ge ($prefix.Length + 3) -and [char]::IsAsciiLetter($Path[$prefix.Length]) -and $Path[$prefix.Length + 1] -eq ':' -and $Path[$prefix.Length + 2] -eq [char]92) {
            return $Path.Substring($prefix.Length)
        }
    }
    $Path
}

function Get-RealFilePath {
    # The absolute path of a file with the first symbolic link or directory junction on its way (the file itself or any ancestor) resolved, so two spellings of one file compare equal.
    # (sol 5add23cf A5: a \\?\ device spelling is the same file too, so it is reduced to the plain spelling first and normalised after that.)
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath((ConvertTo-PlainFilePath $Path))
    $below = [System.Collections.Generic.List[string]]::new()
    $current = $full
    while ($true) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and $null -ne $item.LinkTarget) {
            $target = $item.ResolveLinkTarget($true)
            if ($null -ne $target) {
                $resolved = $target.FullName
                for ($k = $below.Count - 1; $k -ge 0; $k--) { $resolved = Join-Path $resolved $below[$k] }
                return [IO.Path]::GetFullPath((ConvertTo-PlainFilePath $resolved))
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) { break }
        $below.Add([IO.Path]::GetFileName($current))
        $current = $parent
    }
    $full
}

function Assert-ReportPathsAreNotTheInput {
    # (adversary 1ce472bc A1: a report path that is the input summary (a -ReportPath named like the summary, whose .json twin is the summary) or the other report would be overwritten by the publication
    # at the end, so it is refused before anything is written; a directory given as a report path is refused for the same reason)
    # (adversary 698fffb1 A1: compared by REAL path, so a directory junction or symlink in front of a report path cannot hide that it is the input summary; sol 5add23cf A5: the \\?\ spelling too)
    param(
        [Parameter(Mandatory = $true)][string]$Summary,
        [Parameter(Mandatory = $true)][string]$MarkdownReport,
        [Parameter(Mandatory = $true)][string]$JsonReport
    )
    $comparison = if ([IO.Path]::DirectorySeparatorChar -eq [char]92) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    $summaryFull = Get-RealFilePath $Summary
    $markdownFull = Get-RealFilePath $MarkdownReport
    $jsonFull = Get-RealFilePath $JsonReport
    foreach ($candidate in @($markdownFull, $jsonFull)) {
        if ([string]::Equals($candidate, $summaryFull, $comparison)) {
            throw "Proof report path $candidate is the input summary; refusing before anything is written."
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
}

function Invoke-ProofSummarizer {
    # Runs the two summarizer children on the private copy of the summary and leaves their reports in the private work directory. Publishes nothing and removes nothing at any other name.
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string]$Summary,
        [Parameter(Mandatory = $true)][string]$SummaryLabel,
        [Parameter(Mandatory = $true)][string]$WorkDir
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
    # (exit 1 also means the summarizer refused its input before writing (a repeated key); each child writes into the empty private directory, and each report must exist after its run, so a report an
    # earlier run left at a shared name can never be taken for this summary's)
    $markdownTemp = Join-Path $WorkDir "proof-report.md"
    $jsonTemp = Join-Path $WorkDir "proof-report.json"
    & $pwshExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $summaryScript `
        -RepoRoot $Repo `
        -SummaryPath $Summary `
        -SourceSummaryLabel $SummaryLabel `
        -Output $markdownTemp | Out-Null
    $markdownExit = $LASTEXITCODE
    if ($markdownExit -notin @(0, 1)) {
        throw "Markdown proof report generation exited with code $markdownExit."
    }
    if (!(Test-Path -LiteralPath $markdownTemp -PathType Leaf)) {
        throw "Markdown proof report generation exited with code $markdownExit and wrote no report (the summary was refused)."
    }

    & $pwshExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $summaryScript `
        -RepoRoot $Repo `
        -SummaryPath $Summary `
        -SourceSummaryLabel $SummaryLabel `
        -Output $jsonTemp `
        -Json | Out-Null
    $jsonExit = $LASTEXITCODE
    if ($jsonExit -notin @(0, 1)) {
        throw "JSON proof report generation exited with code $jsonExit."
    }
    if (!(Test-Path -LiteralPath $jsonTemp -PathType Leaf)) {
        throw "JSON proof report generation exited with code $jsonExit and wrote no report (the summary was refused)."
    }
    # this run keeps its OWN bytes: the caller judges and packages those
    $markdownBytes = [IO.File]::ReadAllBytes($markdownTemp)
    $jsonBytes = [IO.File]::ReadAllBytes($jsonTemp)
    [pscustomobject]@{
        markdownExitCode = $markdownExit
        jsonExitCode = $jsonExit
        markdownBytes = $markdownBytes
        jsonBytes = $jsonBytes
        jsonText = [Text.UTF8Encoding]::new($false).GetString($jsonBytes).TrimStart([char]0xFEFF)
    }
}

function Add-PacketFileEntry {
    # One file of the packet: its hash is taken over the very bytes written into the archive (or, with no archive, read once). -Bytes = a file this run holds in memory; otherwise -SourcePath is read once, in a stream.
    param(
        $Zip,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [string]$SourcePath = "",
        [byte[]]$Bytes = $null
    )
    $hasher = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
    $length = 0L
    try {
        $entryStream = $null
        if ($null -ne $Zip) { $entryStream = $Zip.CreateEntry($RelativePath, [IO.Compression.CompressionLevel]::Optimal).Open() }
        try {
            if ($null -ne $Bytes) {
                $hasher.AppendData($Bytes)
                $length = $Bytes.Length
                if ($null -ne $entryStream) { $entryStream.Write($Bytes, 0, $Bytes.Length) }
            }
            else {
                $source = [IO.File]::Open($SourcePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
                try {
                    $buffer = [byte[]]::new(81920)
                    while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $hasher.AppendData($buffer, 0, $read)
                        $length += $read
                        if ($null -ne $entryStream) { $entryStream.Write($buffer, 0, $read) }
                    }
                }
                finally { $source.Dispose() }
            }
        }
        finally { if ($null -ne $entryStream) { $entryStream.Dispose() } }
        [pscustomobject]@{ relativePath = $RelativePath; length = $length; sha256 = [Convert]::ToHexString($hasher.GetHashAndReset()) }
    }
    finally { $hasher.Dispose() }
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
Assert-ReportPathsAreNotTheInput -Summary $summaryFullPath -MarkdownReport $ReportPath -JsonReport $jsonReportPath

# 1. THE ONE READ of summary.json (sol 5add23cf A8): its hash, its parse, the copy the children read and the archived file are these bytes.
$summaryLastWrite = (Get-Item -LiteralPath $summaryFullPath).LastWriteTime
$summaryBytes = [IO.File]::ReadAllBytes($summaryFullPath)
$summary = [Text.UTF8Encoding]::new($false).GetString($summaryBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json -Depth 100

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
$comparison = if ([IO.Path]::DirectorySeparatorChar -eq [char]92) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }

$sourceHead = Get-GitScalar -Repo $root -Args @("rev-parse", "HEAD")
$sourceBranch = Get-GitScalar -Repo $root -Args @("rev-parse", "--abbrev-ref", "HEAD")
$sourceStatus = Get-GitStatusLines -Repo $root

# 2. THE PRIVATE DIRECTORY (sol 5add23cf A6, A7): a fresh name beside the run root, created with a call that fails when it exists (New-Item without -Force). A packager on the same run root has its own.
$privateParent = [IO.Path]::GetDirectoryName($runRootResolved)
if ([string]::IsNullOrEmpty($privateParent)) { $privateParent = $runRootResolved }
$privateDir = Join-Path $privateParent (".pkg-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $privateDir -ErrorAction Stop | Out-Null
try {
    $privateSummary = Join-Path $privateDir "summary.json"
    [IO.File]::WriteAllBytes($privateSummary, $summaryBytes)
    $summarizerResult = Invoke-ProofSummarizer -Repo $root -Summary $privateSummary -SummaryLabel $summaryFullPath -WorkDir $privateDir
    # (adversary 698fffb1 A2: the report this run judges is the text of the bytes it wrote, not a re-read of a path another packager may have replaced)
    $proofReportRaw = $summarizerResult.jsonText
    $proofReport = $proofReportRaw | ConvertFrom-Json -Depth 100
    $now = Get-Date
    $markdownArtifact = New-BytesArtifact -Path $ReportPath -Bytes $summarizerResult.markdownBytes -LastWriteTime $now
    $jsonArtifact = New-BytesArtifact -Path $jsonReportPath -Bytes $summarizerResult.jsonBytes -LastWriteTime $now

    # The files of the packet: every file under the run root (read once, hashed as it is written), except this run's own outputs, which come from memory at their relative names. A private directory
    # of any packager (".pkg-*") is never packaged, the manifest and the final packet name are written fresh, and the input summary / the two reports are replaced by this run's own bytes.
    $heldBytes = [System.Collections.Specialized.OrderedDictionary]::new($(if ($comparison -eq [StringComparison]::OrdinalIgnoreCase) { [StringComparer]::OrdinalIgnoreCase } else { [StringComparer]::Ordinal }))
    foreach ($held in @(@($summaryFullPath, $summaryBytes), @($ReportPath, $summarizerResult.markdownBytes), @($jsonReportPath, $summarizerResult.jsonBytes))) {
        $relative = [System.IO.Path]::GetRelativePath($runRootResolved, [IO.Path]::GetFullPath([string]$held[0]))
        if ($relative -eq ".." -or $relative.StartsWith(".." + [IO.Path]::DirectorySeparatorChar, [StringComparison]::Ordinal) -or [IO.Path]::IsPathRooted($relative)) { continue }
        $heldBytes[($relative -replace '\\', '/')] = $held[1]
    }
    $diskFiles = @(Get-ChildItem -LiteralPath $runRootResolved -Recurse -File | Sort-Object FullName | Where-Object {
            $relative = Get-RelativeArtifactPath -Root $runRootResolved -Path $_.FullName
            $isOwn = $heldBytes.Contains($relative)
            $isPrivate = @($relative.Split('/') | Where-Object { $_.StartsWith('.pkg-', [StringComparison]::Ordinal) }).Count -gt 0
            -not $isOwn -and -not $isPrivate -and -not [string]::Equals($_.FullName, $manifestPath, $comparison) -and -not [string]::Equals($_.FullName, $packetFullPath, $comparison)
        })

    $zip = $null
    $privatePacket = Join-Path $privateDir "packet.zip"
    if (-not $DryRun) {
        $packetParent = Split-Path -Parent $packetFullPath
        if ($packetParent) { New-Item -ItemType Directory -Force -Path $packetParent | Out-Null }
        $zip = [IO.Compression.ZipFile]::Open($privatePacket, [IO.Compression.ZipArchiveMode]::Create)
    }
    $entries = [System.Collections.Generic.List[object]]::new()
    try {
        foreach ($file in $diskFiles) {
            [void]$entries.Add((Add-PacketFileEntry -Zip $zip -RelativePath (Get-RelativeArtifactPath -Root $runRootResolved -Path $file.FullName) -SourcePath $file.FullName))
        }
        foreach ($name in $heldBytes.Keys) {
            [void]$entries.Add((Add-PacketFileEntry -Zip $zip -RelativePath $name -Bytes $heldBytes[$name]))
        }
        $sortedEntries = @($entries | Sort-Object relativePath)

        $manifest = [ordered]@{
            schema = "mlvapp-local-cuda-proof-packet.v1"
            status = if ($DryRun) { "planned" } else { "created" }
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
                artifact = New-BytesArtifact -Path $summaryFullPath -Bytes $summaryBytes -LastWriteTime $summaryLastWrite
                schema = $summary.schema
                status = $summary.status
                dryRun = [bool]$summary.dryRun
                capturedAtUtc = $summary.capturedAtUtc
            }
            proofReport = [ordered]@{
                markdown = $markdownArtifact
                json = $jsonArtifact
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
                packet = $packetFullPath
                manifest = $manifestPath
            }
            files = @($sortedEntries)
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

        # the manifest the packet carries is the one without the packet's own hash (the packet cannot name itself); the published manifest below adds it
        $privateManifest = Join-Path $privateDir "local-cuda-proof-packet-manifest.json"
        Write-JsonFile -Value ([pscustomobject]$manifest) -Path $privateManifest
        if ($null -ne $zip) {
            $manifestEntry = $zip.CreateEntry("local-cuda-proof-packet-manifest.json", [IO.Compression.CompressionLevel]::Optimal).Open()
            try { $manifestBytes = [IO.File]::ReadAllBytes($privateManifest); $manifestEntry.Write($manifestBytes, 0, $manifestBytes.Length) } finally { $manifestEntry.Dispose() }
        }
    }
    finally {
        if ($null -ne $zip) { $zip.Dispose() }
    }

    if ($DryRun) {
        $manifest["outputs"]["packet"] = $packetFullPath
    }
    else {
        $packetArtifact = Get-FileArtifact -Path $privatePacket
        $manifest["outputs"]["packet"] = $packetFullPath
        $manifest["packet"] = [pscustomobject]@{ path = $packetFullPath; exists = $true; length = $packetArtifact.length; lastWriteTime = $packetArtifact.lastWriteTime; sha256 = $packetArtifact.sha256 }
    }
    Write-JsonFile -Value ([pscustomobject]$manifest) -Path $privateManifest

    # 3. PUBLICATION: one move per file, last. Both reports first (the manifest names them), then the packet, then the manifest. A move that fails stops the run with the files already moved left where they
    # are: they are this run's bytes, and a rival's later write at the same name is the rival's to keep.
    foreach ($destination in @($ReportPath, $jsonReportPath)) {
        $destinationParent = Split-Path -Parent $destination
        if ($destinationParent) { New-Item -ItemType Directory -Force -Path $destinationParent | Out-Null }
    }
    Move-Item -LiteralPath (Join-Path $privateDir "proof-report.md") -Destination $ReportPath -Force
    Move-Item -LiteralPath (Join-Path $privateDir "proof-report.json") -Destination $jsonReportPath -Force
    if (-not $DryRun) { Move-Item -LiteralPath $privatePacket -Destination $packetFullPath -Force }
    Move-Item -LiteralPath $privateManifest -Destination $manifestPath -Force
}
finally {
    # the only deletes of the run: the files in this run's own private directory, then the (by then empty) directory itself; never recursive
    foreach ($leftover in @(Get-ChildItem -LiteralPath $privateDir -File -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $leftover.FullName -Force -ErrorAction SilentlyContinue
    }
    try { [IO.Directory]::Delete($privateDir) } catch { Write-Warning "The private directory $privateDir could not be removed: $($_.Exception.Message)" }
}

$manifest | ConvertTo-Json -Depth 16
if ($StrictQuotable -and [string]$proofReport.status -ne "QUOTABLE_PASS") {
    exit 1
}
exit 0
