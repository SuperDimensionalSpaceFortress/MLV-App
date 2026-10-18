<#
.SYNOPSIS
Retention/rotation for a venue agent mailbox: moves processed\ and outbox\ entries older than N days into
<share>\archive\<dir>\<yyyy-MM>\. Never deletes. Dry-run unless -Apply.

.DESCRIPTION
HOST-CPU-SATURATION 2026-10-17: processed\ held 20.7k files and outbox\ 7k, and one unbounded `ls -t`
of them cost about one host core for over 10 minutes. This keeps the live directories small.

Rules:
- Only processed\ and outbox\ are rotated. inbox\, running\, orphaned\, cancel\, slot-holders\ and the
  agent's own files are never touched.
- Entries are grouped by job id (the name minus the agent's known suffixes: .job.ps1, .meta.json,
  .started.<status>.json, .result[.<hex>].json|tmp, .artifacts[...], .cancel). A group moves only when
  EVERY member is older than the cutoff, so one job is never split between live and archive.
- A job id present in inbox\ or running\ is skipped whatever its age.
- The month folder is the newest member's LastWriteTimeUtc (yyyy-MM).
- A destination that already exists is never overwritten: that group is skipped and reported.
- Moves are same-share renames (no data copy). Each applied move is appended to
  <share>\archive\rotation-manifests\<stamp>.jsonl as it happens, so a partial run is still accounted for.
- Consumers that probe outbox\<id>.result.json (um-run, UmRunDrop's JOBID_IN_USE pre-check) see only the
  live window; a re-used job id older than the cutoff is no longer refused by that pre-check, which is
  documented there as a fast check, not the protection (the inbox claim rename is).

Enumeration uses [IO.DirectoryInfo]::EnumerateFileSystemInfos (no file opens; see Get-AgentShareRecent.ps1).

.EXAMPLE
pwsh -NoProfile -File tools\profiling\agent-share\Invoke-AgentShareRotation.ps1                # dry run, 14 days
pwsh -NoProfile -File tools\profiling\agent-share\Invoke-AgentShareRotation.ps1 -OlderThanDays 14 -Apply
#>
[CmdletBinding()]
param(
    [string]$AgentShare = '\\bachelor\mlv-agent',
    [ValidateSet('processed', 'outbox')]
    [string[]]$Directory = @('processed', 'outbox'),
    [ValidateRange(7, 3650)]
    [int]$OlderThanDays = 14,
    [switch]$Apply,
    # Optional cap on groups moved per directory in one run (0 = no cap).
    [ValidateRange(0, 1000000)]
    [int]$MaxGroups = 0,
    # Where to write the plan/receipt JSON (default: none; summary goes to stdout).
    [string]$ReportPath = '',
    # Test seam: evaluate ages against this instant instead of now.
    [Nullable[datetime]]$NowUtc = $null
)

$ErrorActionPreference = 'Stop'
$now = if ($NowUtc) { ([datetime]$NowUtc).ToUniversalTime() } else { [DateTime]::UtcNow }
$cutoff = $now.AddDays(-$OlderThanDays)
$stamp = $now.ToString('yyyyMMddTHHmmssZ')

if (-not (Test-Path -LiteralPath $AgentShare -PathType Container)) { throw "SHARE_UNREADABLE $AgentShare (an unreachable share is not an empty one)" }
# Sanity: this must be an agent mailbox root, not an arbitrary folder.
foreach ($must in @('inbox', 'processed', 'outbox')) {
    if (-not (Test-Path -LiteralPath (Join-Path $AgentShare $must) -PathType Container)) { throw "NOT_AN_AGENT_ROOT $AgentShare lacks $must\" }
}

$suffixRx = '\.(job\.ps1|meta\.json|started(\.[A-Za-z0-9-]+)?\.json|result(\.[0-9a-f]{32})?\.(json|tmp)|artifacts(\..*)?|cancel)$'
function Get-JobKey([string]$name) {
    $k = $name -replace $suffixRx, ''
    if ($k) { return $k } else { return $name }
}

function Get-LiveJobKeys {
    $keys = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($d in @('inbox', 'running')) {
        $di = [IO.DirectoryInfo](Join-Path $AgentShare $d)
        if (-not $di.Exists) { continue }
        foreach ($e in $di.EnumerateFileSystemInfos()) { [void]$keys.Add((Get-JobKey $e.Name)) }
    }
    return , $keys
}

$live = Get-LiveJobKeys
$manifestDir = Join-Path (Join-Path $AgentShare 'archive') 'rotation-manifests'
$manifest = Join-Path $manifestDir "$stamp.jsonl"
$report = [ordered]@{
    schema = 'agent-share-rotation.v1'; share = $AgentShare; mode = $(if ($Apply) { 'apply' } else { 'dry-run' })
    nowUtc = $now.ToString('o'); cutoffUtc = $cutoff.ToString('o'); olderThanDays = $OlderThanDays
    manifest = $(if ($Apply) { $manifest } else { $null }); directories = [ordered]@{}
}

foreach ($dirName in $Directory) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $src = Join-Path $AgentShare $dirName
    $archiveBase = Join-Path (Join-Path (Join-Path $AgentShare 'archive') $dirName) ''
    $groups = @{}
    $destExists = @{}
    $entryCount = 0
    foreach ($e in ([IO.DirectoryInfo]$src).EnumerateFileSystemInfos()) {
        $entryCount++
        $k = Get-JobKey $e.Name
        if (-not $groups.ContainsKey($k)) { $groups[$k] = [System.Collections.Generic.List[IO.FileSystemInfo]]::new() }
        $groups[$k].Add($e)
    }
    $stats = [ordered]@{ entries = $entryCount; groups = $groups.Count; eligibleGroups = 0; eligibleEntries = 0
        keptYoung = 0; keptLive = 0; skippedCollision = 0; moved = 0; failed = 0; byMonth = [ordered]@{}; samples = @(); errors = @()
        oldestEligibleUtc = $null; newestEligibleUtc = $null }
    $eligible = foreach ($k in $groups.Keys) {
        $members = $groups[$k]
        $newest = ($members | Measure-Object -Property LastWriteTimeUtc -Maximum).Maximum
        if ($newest -ge $cutoff) { $stats.keptYoung++; continue }
        if ($live.Contains($k)) { $stats.keptLive++; continue }
        [pscustomobject]@{ key = $k; newest = $newest; members = $members }
    }
    $eligible = @($eligible | Sort-Object newest)
    if ($MaxGroups -gt 0 -and $eligible.Count -gt $MaxGroups) { $eligible = @($eligible | Select-Object -First $MaxGroups) }
    if ($eligible.Count) {
        $stats.oldestEligibleUtc = $eligible[0].newest.ToString('o')
        $stats.newestEligibleUtc = $eligible[-1].newest.ToString('o')
    }
    foreach ($g in $eligible) {
        $month = $g.newest.ToString('yyyy-MM')
        $destDir = Join-Path $archiveBase $month
        # Probe members only when the month folder exists (one probe per month otherwise, not one per file).
        if (-not $destExists.ContainsKey($destDir)) { $destExists[$destDir] = Test-Path -LiteralPath $destDir -PathType Container }
        $collide = if ($destExists[$destDir]) { @($g.members | Where-Object { Test-Path -LiteralPath (Join-Path $destDir $_.Name) }) } else { @() }
        if ($collide.Count) { $stats.skippedCollision++; continue }
        $stats.eligibleGroups++
        $stats.eligibleEntries += $g.members.Count
        if (-not $stats.byMonth.Contains($month)) { $stats.byMonth[$month] = 0 }
        $stats.byMonth[$month] += $g.members.Count
        if ($stats.samples.Count -lt 5) { $stats.samples += "$($g.key) -> archive\$dirName\$month ($($g.members.Count) entries)" }
        if (-not $Apply) { continue }
        if (-not $destExists[$destDir]) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null; $destExists[$destDir] = $true }
        if (-not (Test-Path -LiteralPath $manifestDir)) { New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null }
        foreach ($m in $g.members) {
            $dest = Join-Path $destDir $m.Name
            $row = [ordered]@{ tsUtc = [DateTime]::UtcNow.ToString('o'); dir = $dirName; job = $g.key; name = $m.Name
                from = $m.FullName; to = $dest; lastWriteTimeUtc = $m.LastWriteTimeUtc.ToString('o') }
            try {
                # Same-share rename; Move-Item refuses an existing destination (no -Force), so nothing is overwritten.
                Move-Item -LiteralPath $m.FullName -Destination $dest -ErrorAction Stop
                $row.ok = $true; $stats.moved++
            } catch {
                $row.ok = $false; $row.error = $_.Exception.Message; $stats.failed++
                if ($stats.errors.Count -lt 10) { $stats.errors += "$($m.Name): $($_.Exception.Message)" }
            }
            Add-Content -LiteralPath $manifest -Value ($row | ConvertTo-Json -Compress) -Encoding utf8
        }
    }
    $stats.elapsedMs = $sw.ElapsedMilliseconds
    $report.directories[$dirName] = $stats
}

$json = $report | ConvertTo-Json -Depth 6
if ($ReportPath) {
    $parent = Split-Path -Parent $ReportPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [IO.File]::WriteAllText($ReportPath, $json, [Text.UTF8Encoding]::new($false))
}
$json
$failed = ($report.directories.Values | Measure-Object -Property failed -Sum).Sum
if ($failed) { exit 3 }
