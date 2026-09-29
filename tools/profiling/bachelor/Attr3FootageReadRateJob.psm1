# Attr3FootageReadRateJob.psm1 -- job-TEXT construction for attr3-footage-read-rate-job.ps1
# (BACHELOR-OWNER-CLIP-STAGE-STALL-1). Same split as Attr3FootagePresenceJob.psm1: a test can build
# a job from SYNTHETIC parts directly, while the CLI's only route to real parts is the resolver.
#
# WHAT THE EMITTED JOB MEASURES. Part 0 of one owner clip is read in bounded REGIONS (default
# 128 MiB each), never whole: a first version read the whole 2.2 GB part four times and was killed
# at its 1800 s cap with 0.8 GB done -- the cold rate on the measurement host is ~2 MB/s, so a
# whole-file pass costs ~17 min. Each region pass reports seconds, MB/s and the real-time
# scanner's (MsMpEng) CPU seconds during it:
#   COLD-4MiB   a region never read before, 4 MiB blocks, hashed as it goes (the identity read);
#   WARM-4MiB   the same region again (does the OS file cache serve a repeat read?);
#   WARM-4KiB   the same region again in 4 KiB blocks (the small-block penalty, warm);
#   COLD-4KiB   a second never-read region in 4 KiB blocks (is the cold rate block-size bound?);
#   CROSS-JOB   the start of the part (offset 0), which an earlier job may already have read.
# and HASH-ONLY: SHA-256 over an in-memory buffer (the hash CPU cost with no I/O at all).
# The job never changes a Defender setting (an exclusion is a security setting) -- it only reports
# the measured cost. Every step traces a line BEFORE it starts, so a killed job shows what it was
# doing, and there is no CIM/WMI call anywhere (on this host one Get-CimInstance took ~45 s).
#
# NO PATH EVER REACHES THE EMITTED JOB'S OUTPUT OR TRACE: a part's path travels as base64 of its
# UTF-8 bytes (see Attr3FootagePresenceJob.psm1's header), decoded in-process and used only
# through literal-path calls. Every line the job writes names part indexes, sizes and rates.

Set-StrictMode -Version Latest

if (-not (Get-Command -Name 'Assert-AttrCudaSafeArtifactName' -ErrorAction SilentlyContinue)) {
    Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Global -ErrorAction Stop -Verbose:$false
}

function New-Attr3FootageReadRateJob {
    <#
    .SYNOPSIS
    Build and write a <jobId>.job.ps1 footage read-rate probe from already-resolved parts.
    .DESCRIPTION
    Throws a distinguishable ATTR3_READRATE_* token on any refusal; returns a pscustomobject
    describing the emitted job (jobFile, jobId, traceName) otherwise.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
        [string]$ClipId,

        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Parts,

        [Parameter(Mandatory = $true)]
        [string]$OutDir,

        [string]$AgentRoot = 'C:\mlvtmp\mlv-agent',

        [ValidateRange(4, 1024)]
        [int]$RegionMB = 128
    )

    if ($Parts.Count -eq 0) { throw "ATTR3_READRATE_NO_PARTS zero parts supplied for '$ClipId'" }
    if ($AgentRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') {
        throw 'ATTR3_READRATE_AGENTROOT_INVALID -AgentRoot contains characters outside the allowlist'
    }
    foreach ($part in $Parts) {
        $path = [string]$part.path
        if ($path -notmatch '^[A-Za-z]:[\\/]' -or $path -match '[\x00-\x1f]') {
            throw "ATTR3_READRATE_PART_PATH_INVALID part $($part.index) is not a drive-letter path or carries a control character"
        }
        if ($part.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "ATTR3_READRATE_PART_SHA_INVALID part $($part.index) sha256 is not 64 lowercase hex"
        }
    }

    # Only part 0 is measured; it is the largest in the corpus and the one every leg reads first.
    $partsForJob = @($Parts | Sort-Object { [int]$_.index } | Select-Object -First 1 | ForEach-Object {
        [ordered]@{
            index = [int]$_.index
            pathBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$_.path))
            length = [int64]$_.length
            sha256 = [string]$_.sha256
        }
    })
    $partsJson = $partsForJob | ConvertTo-Json -Compress -Depth 5
    if ($partsForJob.Count -eq 1) { $partsJson = "[$partsJson]" }

    $jobId = "attr3-footage-read-rate-$ClipId-$([guid]::NewGuid().ToString('N').Substring(0, 10))"
    [void](Assert-AttrCudaSafeArtifactName -Name "$jobId.job.ps1")

    $embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
        'Read-AttrCudaBase64Payload', 'ConvertTo-AttrCudaUtf8String', 'Add-AttrCudaTraceLine'
    )

    $template = @'
$ErrorActionPreference = 'Stop'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$JobId = '__JOB_ID__'
$ClipId = '__CLIP_ID__'
$PartsJson = '__PARTS_JSON__'
$Root = '__AGENT_ROOT__'
$RegionBytes = [int64]__REGION_MB__ * 1048576
$Trace = Join-Path $Root "logs\$JobId.trace.txt"

__EMBEDDED_FUNCTIONS__

function Say([string]$Message) {
    Write-Output "[$JobId] $Message"
    Add-AttrCudaTraceLine -TracePath $Trace -Message $Message
}
function Get-DefenderCpuSeconds {
    $proc = Get-Process -Name MsMpEng -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $proc) { return $null }
    [double]$proc.TotalProcessorTime.TotalSeconds
}
# Read [Offset, Offset+RegionBytes) of the file in Block-sized reads; optionally SHA-256 it as it goes.
function Read-Region([string]$Path, [int64]$Offset, [int]$Block, [bool]$Hash) {
    $incremental = $null
    if ($Hash) { $incremental = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256) }
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read, $Block, [IO.FileOptions]::SequentialScan)
    $total = [int64]0
    try {
        [void]$stream.Seek($Offset, [IO.SeekOrigin]::Begin)
        $buffer = [byte[]]::new($Block)
        while ($total -lt $RegionBytes) {
            $want = [int][math]::Min($Block, $RegionBytes - $total)
            $n = $stream.Read($buffer, 0, $want)
            if ($n -le 0) { break }
            if ($Hash) { $incremental.AppendData($buffer, 0, $n) }
            $total += $n
        }
    } finally {
        $stream.Dispose()
        if ($Hash) { $incremental.Dispose() }
    }
    $total
}
function Measure-Region([string]$Label, [string]$Path, [int64]$Offset, [int]$Block, [bool]$Hash) {
    Say "pass=$Label start offsetMB=$([math]::Round($Offset / 1MB)) regionMB=$([math]::Round($RegionBytes / 1MB)) blockBytes=$Block hash=$Hash"
    $cpuBefore = Get-DefenderCpuSeconds
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $bytes = Read-Region $Path $Offset $Block $Hash
    $watch.Stop()
    $cpuAfter = Get-DefenderCpuSeconds
    $seconds = [math]::Max(0.001, $watch.Elapsed.TotalSeconds)
    $defenderCpu = if ($null -ne $cpuBefore -and $null -ne $cpuAfter) { [math]::Round($cpuAfter - $cpuBefore, 2) } else { $null }
    $row = [ordered]@{ pass = $Label; offsetMB = [math]::Round($Offset / 1MB); bytes = $bytes; blockBytes = $Block; hashed = $Hash
        seconds = [math]::Round($seconds, 2); mbPerSec = [math]::Round($bytes / 1MB / $seconds, 2); defenderCpuSeconds = $defenderCpu }
    Say "pass=$Label done bytes=$bytes seconds=$($row.seconds) MBps=$($row.mbPerSec) defenderCpuSec=$defenderCpu"
    $row
}

Say "job process started"
try {
    $RawParts = @($PartsJson | ConvertFrom-Json)
    $rawPart = $RawParts[0]
    $decoded = Read-AttrCudaBase64Payload -Base64 $rawPart.pathBase64
    $partPath = ConvertTo-AttrCudaUtf8String -Bytes $decoded.bytes
    $length = [int64]$rawPart.length
    Say "part=$($rawPart.index) length=$length regionMB=$([math]::Round($RegionBytes / 1MB))"
    if ($length -lt (4 * $RegionBytes)) { throw 'part too small for four disjoint regions' }

    $align = 4194304
    $coldA = [int64]([math]::Floor($length * 0.70 / $align) * $align)
    $coldB = [int64]([math]::Floor($length * 0.85 / $align) * $align)
    $rows = New-Object System.Collections.Generic.List[object]

    [void]$rows.Add((Measure-Region 'COLD-4MiB' $partPath $coldA 4194304 $true))
    [void]$rows.Add((Measure-Region 'WARM-4MiB' $partPath $coldA 4194304 $true))
    [void]$rows.Add((Measure-Region 'WARM-4KiB' $partPath $coldA 4096 $false))
    [void]$rows.Add((Measure-Region 'COLD-4KiB' $partPath $coldB 4096 $false))
    [void]$rows.Add((Measure-Region 'CROSS-JOB-OFFSET0-4MiB' $partPath 0 4194304 $false))

    Say 'pass=HASH-ONLY start (in-memory SHA-256, no I/O)'
    $memBuffer = [byte[]]::new(4194304)
    $inc = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
    $hashWatch = [Diagnostics.Stopwatch]::StartNew()
    $hashed = [int64]0
    while ($hashed -lt $RegionBytes) { $inc.AppendData($memBuffer, 0, $memBuffer.Length); $hashed += $memBuffer.Length }
    $hashWatch.Stop(); $inc.Dispose()
    $hashMBps = [math]::Round($hashed / 1MB / [math]::Max(0.001, $hashWatch.Elapsed.TotalSeconds), 1)
    Say "pass=HASH-ONLY done MBps=$hashMBps"

    Write-Output "RESULT=FOOTAGE_READ_RATE_DONE CLIP=$ClipId TRACE=logs\$JobId.trace.txt"
    Write-Output (([ordered]@{
        schema = 'mlvapp.attr3-footage-read-rate.v2'
        jobId = $JobId
        clipId = $ClipId
        partIndex = [int]$rawPart.index
        partLength = $length
        passes = $rows
        hashOnlyMBps = $hashMBps
    }) | ConvertTo-Json -Compress -Depth 6)
    exit 0
} catch {
    # Fixed token, never the exception text: it can carry the part's real path.
    Say "RESULT=FOOTAGE_READ_RATE_JOB_ERROR exceptionType=$($_.Exception.GetType().Name)"
    Write-Output "RESULT=FOOTAGE_READ_RATE_JOB_ERROR CLIP=$ClipId"
    exit 4
}
'@

    $text = Expand-AttrCudaTemplate -Template $template -Tokens ([ordered]@{
        JOB_ID = $jobId
        CLIP_ID = $ClipId
        PARTS_JSON = $partsJson
        AGENT_ROOT = $AgentRoot
        REGION_MB = [string]$RegionMB
        EMBEDDED_FUNCTIONS = $embeddedFunctions
    })

    if (-not (Test-Path -LiteralPath $OutDir)) { [void](New-Item -ItemType Directory -Path $OutDir -Force) }
    $OutDir = (Resolve-Path -LiteralPath $OutDir).Path
    $jobPath = Join-Path $OutDir "$jobId.job.ps1"
    [IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

    Write-Output "RESULT=FOOTAGE_READ_RATE_JOB_EMITTED CLIP=$ClipId PARTS=$($partsForJob.Count) JOB=$jobId TRACE=logs\$jobId.trace.txt"
    [pscustomobject]@{ jobFile = $jobPath; jobId = $jobId; clipId = $ClipId; partCount = $partsForJob.Count; traceName = "$jobId.trace.txt" }
}

Export-ModuleMember -Function New-Attr3FootageReadRateJob
