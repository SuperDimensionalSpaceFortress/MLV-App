param(
    [string]$RepoRoot = ".",
    [Parameter(Mandatory = $true)]
    [string]$PacketPath,
    [string]$DestinationRoot = "",
    [string]$ExpectedHostName = "",
    [switch]$AllowRepoHeadMismatch,
    [switch]$StrictQuotable
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

function Add-Message {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$Message
    )
    if (-not [string]::IsNullOrWhiteSpace($Message)) {
        [void]$List.Add($Message)
    }
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

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
$packetFullPath = Resolve-RepoPath -Root $root -Path $PacketPath
if (!(Test-Path -LiteralPath $packetFullPath -PathType Leaf)) {
    throw "Packet not found: $packetFullPath"
}
$packetItem = Get-Item -LiteralPath $packetFullPath

if ([string]::IsNullOrWhiteSpace($DestinationRoot)) {
    $DestinationRoot = Join-Path $root ".claude-state\profiling\local-cuda-proof-packets\imported"
}
else {
    $DestinationRoot = Resolve-RepoPath -Root $root -Path $DestinationRoot
}
New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null

$packetBase = [System.IO.Path]::GetFileNameWithoutExtension($packetItem.Name)
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$extractRoot = Join-Path $DestinationRoot "$packetBase-$stamp"
if (Test-Path -LiteralPath $extractRoot) {
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
Expand-Archive -LiteralPath $packetItem.FullName -DestinationPath $extractRoot -Force

$failures = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()
$manifestPath = Join-Path $extractRoot "local-cuda-proof-packet-manifest.json"
$summaryPath = Join-Path $extractRoot "summary.json"
$proofReportJsonPath = Join-Path $extractRoot "proof-report.json"
$proofReportMarkdownPath = Join-Path $extractRoot "proof-report.md"

if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Add-Message $failures "Packet is missing local-cuda-proof-packet-manifest.json."
}
if (!(Test-Path -LiteralPath $summaryPath -PathType Leaf)) {
    Add-Message $failures "Packet is missing summary.json."
}
if (!(Test-Path -LiteralPath $proofReportJsonPath -PathType Leaf)) {
    Add-Message $failures "Packet is missing proof-report.json."
}
if (!(Test-Path -LiteralPath $proofReportMarkdownPath -PathType Leaf)) {
    Add-Message $warnings "Packet is missing proof-report.md."
}

$manifest = $null
$summary = $null
$proofReport = $null
if ($failures.Count -eq 0) {
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json -Depth 100
        $summary = Get-Content -LiteralPath $summaryPath -Raw | ConvertFrom-Json -Depth 100
        $proofReport = Get-Content -LiteralPath $proofReportJsonPath -Raw | ConvertFrom-Json -Depth 100
    }
    catch {
        Add-Message $failures "Failed to parse packet JSON: $($_.Exception.Message)"
    }
}

$fileChecks = @()
if ($manifest) {
    if ([string]$manifest.schema -ne "mlvapp-local-cuda-proof-packet.v1") {
        Add-Message $failures "Unexpected packet manifest schema: $($manifest.schema)"
    }
    if ([string]$manifest.summary.schema -ne "mlvapp-local-cuda-playback-dng-smoke.v1") {
        Add-Message $warnings "Unexpected summary schema in manifest: $($manifest.summary.schema)"
    }
    if ($summary -and [string]$summary.schema -ne "mlvapp-local-cuda-playback-dng-smoke.v1") {
        Add-Message $failures "Unexpected summary schema: $($summary.schema)"
    }
    if ($proofReport -and [string]$proofReport.schema -ne "mlvapp-local-cuda-proof-report.v1") {
        Add-Message $failures "Unexpected proof report schema: $($proofReport.schema)"
    }

    $currentHead = Get-GitScalar -Repo $root -Args @("rev-parse", "HEAD")
    if (!$AllowRepoHeadMismatch -and
        -not [string]::IsNullOrWhiteSpace([string]$manifest.source.repoHead) -and
        -not [string]::IsNullOrWhiteSpace([string]$currentHead) -and
        [string]$manifest.source.repoHead -ne [string]$currentHead) {
        Add-Message $failures "Current repo HEAD $currentHead does not match packet repo HEAD $($manifest.source.repoHead)."
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedHostName) -and
        [string]$manifest.source.host -ine $ExpectedHostName) {
        Add-Message $failures "Packet host was '$($manifest.source.host)'; expected '$ExpectedHostName'."
    }

    foreach ($file in @($manifest.files)) {
        $relative = [string]$file.relativePath
        if ([string]::IsNullOrWhiteSpace($relative)) {
            Add-Message $failures "Manifest contains a file entry without relativePath."
            continue
        }
        $candidate = Join-Path $extractRoot ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        if (!(Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $fileChecks += [pscustomobject]@{
                relativePath = $relative
                ok = $false
                reason = "missing"
                expectedSha256 = $file.sha256
                actualSha256 = $null
            }
            Add-Message $failures "Packet missing artifact listed in manifest: $relative"
            continue
        }
        $actualHash = Get-FileSha256 -Path $candidate
        $ok = ([string]$actualHash -eq [string]$file.sha256)
        $fileChecks += [pscustomobject]@{
            relativePath = $relative
            ok = $ok
            reason = if ($ok) { "ok" } else { "sha256_mismatch" }
            expectedSha256 = $file.sha256
            actualSha256 = $actualHash
            length = (Get-Item -LiteralPath $candidate).Length
        }
        if (-not $ok) {
            Add-Message $failures "Artifact hash mismatch for ${relative}: expected $($file.sha256), actual $actualHash."
        }
    }
}

$proofReportStatus = if ($proofReport) { [string]$proofReport.status } else { $null }
if ($StrictQuotable -and $proofReportStatus -ne "QUOTABLE_PASS") {
    Add-Message $failures "Strict quotable import required QUOTABLE_PASS; packet proof report status was '$proofReportStatus'."
}
$proofDiagnostics = if ($proofReport) { @($proofReport.diagnostics) } else { @() }
$proofDiagnosticSummary = if ($proofReport) { $proofReport.diagnosticSummary } else { $null }
$proofActionPlan = if ($proofReport) { $proofReport.actionPlan } else { $null }

$status = if ($failures.Count -eq 0) { "imported" } else { "failed" }
$importSummaryPath = Join-Path $extractRoot "import-summary.json"
$result = [ordered]@{
    schema = "mlvapp-local-cuda-proof-packet-import.v1"
    importedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    status = $status
    packet = Get-FileArtifact -Path $packetItem.FullName
    extractRoot = $extractRoot
    manifest = if ($manifest) {
        [ordered]@{
            path = $manifestPath
            schema = $manifest.schema
            status = $manifest.status
            source = $manifest.source
            fileCount = @($manifest.files).Count
        }
    } else { $null }
    proofReport = if ($proofReport) {
        [ordered]@{
            path = $proofReportJsonPath
            status = $proofReport.status
            # r15 (adversary r14 pass 18 A6): speed authority is the report's claimAuthority.speed (exactly $true), never the support status; a report without it grants none
            speedQuotable = (($proofReport.status -ceq "QUOTABLE_PASS") -and ($null -ne $proofReport.claimAuthority) -and ($proofReport.claimAuthority.speed -is [bool]) -and $proofReport.claimAuthority.speed)
            claimAuthority = $proofReport.claimAuthority
            topLevelStatus = $proofReport.topLevelStatus
            blockers = @($proofReport.blockers)
            warnings = @($proofReport.warnings)
            diagnosticSummary = $proofDiagnosticSummary
            diagnosticCodes = @($proofDiagnostics | ForEach-Object { [string]$_.code } | Where-Object {
                    -not [string]::IsNullOrWhiteSpace($_)
                } | Select-Object -Unique)
            diagnostics = $proofDiagnostics
            actionPlan = $proofActionPlan
        }
    } else { $null }
    summary = if ($summary) {
        [ordered]@{
            path = $summaryPath
            schema = $summary.schema
            status = $summary.status
            dryRun = [bool]$summary.dryRun
            host = $summary.host.name
        }
    } else { $null }
    fileChecks = [ordered]@{
        checked = @($fileChecks).Count
        failed = @($fileChecks | Where-Object { -not [bool]$_.ok }).Count
        files = @($fileChecks)
    }
    boundaries = @(
        "Import validates packet structure and artifact hashes only.",
        "Hardware support remains quotable only when proofReport.status is QUOTABLE_PASS for the same host being claimed; playback speed only when proofReport.speedQuotable is also true.",
        "A NOT_QUOTABLE packet is an actionable diagnostic packet, not a success proof."
    )
    warnings = @($warnings)
    failures = @($failures)
}
Write-JsonFile -Value ([pscustomobject]$result) -Path $importSummaryPath
$result["importSummary"] = $importSummaryPath
Write-JsonFile -Value ([pscustomobject]$result) -Path $importSummaryPath
$result | ConvertTo-Json -Depth 16

if ($failures.Count -gt 0) {
    exit 1
}
