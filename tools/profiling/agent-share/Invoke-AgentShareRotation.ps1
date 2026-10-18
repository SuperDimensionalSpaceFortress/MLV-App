<#
.SYNOPSIS
Retention/rotation for a venue agent mailbox: moves whole jobs older than N days out of processed\ and outbox\
into <share>\archive\<dir>\<yyyy-MM>\. Never deletes. Dry-run unless -Apply.

.DESCRIPTION
HOST-CPU-SATURATION 2026-10-17: processed\ held 20.7k files and outbox\ 7k, and one unbounded `ls -t`
of them cost about one host core for over 10 minutes. This keeps the live directories small.

Rules:
- Only processed\ and outbox\ are rotated. inbox\, running\, orphaned\, cancel\, slot-holders\ and the
  agent's own files are never touched.
- Entries are grouped by JOB across BOTH directories (AgentShare.psm1 holds the suffix rules). Every decision
  is per job: a job moves only when EVERY member in processed\ AND outbox\ is older than the cutoff, no member's
  destination already exists, and it is inside -MaxGroups. One job is never split between live and archive.
- A job id with any entry in inbox\, running\ (final marker, unresolved .tmp marker, or set-aside record),
  orphaned\ or cancel\ is skipped whatever its age.
- The month folder is the job's newest member's LastWriteTimeUtc (yyyy-MM), the same month for both halves.
- Moves are same-share renames (no data copy). In -Apply mode each move is bracketed in
  <share>\archive\rotation-manifests\<stamp>.jsonl: an "intent" row is appended BEFORE the rename and a
  "done" row (ok true/false) after it. If the intent row cannot be written, that entry is not moved. On the
  first failed rename or failed "done" row the run STOPS and exits 3, naming the partial job; the manifest's
  intent rows say exactly which members moved, so the job can be moved back or finished by hand.
- Consumers that probe outbox\<id>.result.json (um-run, UmRunDrop's JOBID_IN_USE pre-check) see only the
  live window; a re-used job id older than the cutoff is no longer refused by that pre-check, which is
  documented there as a fast check, not the protection (the inbox claim rename is).

Enumeration uses [IO.DirectoryInfo]::EnumerateFileSystemInfos (no file opens; see Get-AgentShareRecent.ps1).

.EXAMPLE
pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\profiling\agent-share\Invoke-AgentShareRotation.ps1"
pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\profiling\agent-share\Invoke-AgentShareRotation.ps1" -OlderThanDays 14 -Apply
#>
[CmdletBinding()]
param(
    [string]$AgentShare = '\\bachelor\mlv-agent',
    [ValidateRange(7, 3650)]
    [int]$OlderThanDays = 14,
    [switch]$Apply,
    # Optional cap on JOBS moved in one run, oldest first (0 = no cap).
    [ValidateRange(0, 1000000)]
    [int]$MaxGroups = 0,
    # Where to write the plan/receipt JSON (default: none; the report goes to stdout).
    [string]$ReportPath = '',
    # Test seam: evaluate ages against this instant instead of now.
    [Nullable[datetime]]$NowUtc = $null,
    # Test seam: a scriptblock run before each rename with the member; a throw simulates a failed rename.
    [scriptblock]$TestHookBeforeMove = $null
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'AgentShare.psm1') -Force
$now = if ($NowUtc) { ([datetime]$NowUtc).ToUniversalTime() } else { [DateTime]::UtcNow }
$cutoff = $now.AddDays(-$OlderThanDays)
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$rotated = @('processed', 'outbox')

if (-not (Test-Path -LiteralPath $AgentShare -PathType Container)) { throw "SHARE_UNREADABLE $AgentShare (an unreachable share is not an empty one)" }
# Sanity: this must be an agent mailbox root, not an arbitrary folder.
foreach ($must in @('inbox', 'running', 'processed', 'outbox')) {
    if (-not (Test-Path -LiteralPath (Join-Path $AgentShare $must) -PathType Container)) { throw "NOT_AN_AGENT_ROOT $AgentShare lacks $must\" }
}

# Jobs the agent may still act on: any entry in these directories protects its job id.
$live = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($d in @('inbox', 'running', 'orphaned', 'cancel')) {
    $di = [IO.DirectoryInfo](Join-Path $AgentShare $d)
    if (-not $di.Exists) { continue }
    foreach ($e in $di.EnumerateFileSystemInfos()) { [void]$live.Add((Get-AgentShareJobKey -Name $e.Name)) }
}

$sw = [Diagnostics.Stopwatch]::StartNew()
$jobs = [System.Collections.Generic.Dictionary[string, System.Collections.Generic.List[object]]]::new([StringComparer]::OrdinalIgnoreCase)
$entryCount = [ordered]@{}
foreach ($d in $rotated) {
    $entryCount[$d] = 0
    foreach ($e in ([IO.DirectoryInfo](Join-Path $AgentShare $d)).EnumerateFileSystemInfos()) {
        $entryCount[$d]++
        $k = Get-AgentShareJobKey -Name $e.Name
        if (-not $jobs.ContainsKey($k)) { $jobs[$k] = [System.Collections.Generic.List[object]]::new() }
        $jobs[$k].Add([pscustomobject]@{ dir = $d; entry = $e })
    }
}

$stats = [ordered]@{ jobs = $jobs.Count; keptYoung = 0; keptLive = 0; keptCap = 0; skippedCollision = 0; eligibleJobs = 0
    eligibleEntries = [ordered]@{ processed = 0; outbox = 0 }; byMonth = [ordered]@{}; movedEntries = 0; samples = @()
    collisions = @(); oldestEligibleUtc = $null; newestEligibleUtc = $null }

$candidates = foreach ($k in $jobs.Keys) {
    $members = $jobs[$k]
    $newest = ($members | ForEach-Object { $_.entry.LastWriteTimeUtc } | Measure-Object -Maximum).Maximum
    if ($newest -ge $cutoff) { $stats.keptYoung++; continue }
    if ($live.Contains($k)) { $stats.keptLive++; continue }
    [pscustomobject]@{ key = $k; newest = $newest; month = $newest.ToString('yyyy-MM'); members = $members }
}
$candidates = @($candidates | Sort-Object newest, key)

# Collision is decided for the whole job before anything moves: one existing destination keeps every member live.
$destExists = @{}
$plan = foreach ($c in $candidates) {
    $clash = $null
    foreach ($m in $c.members) {
        $destDir = Join-Path (Join-Path (Join-Path $AgentShare 'archive') $m.dir) $c.month
        if (-not $destExists.ContainsKey($destDir)) { $destExists[$destDir] = Test-Path -LiteralPath $destDir -PathType Container }
        $m | Add-Member -NotePropertyName dest -NotePropertyValue (Join-Path $destDir $m.entry.Name) -Force
        if ($destExists[$destDir] -and (Test-Path -LiteralPath $m.dest)) { $clash = "$($m.dir)\$($m.entry.Name)" }
    }
    if ($clash) { $stats.skippedCollision++; if ($stats.collisions.Count -lt 10) { $stats.collisions += "$($c.key): $clash" }; continue }
    $c
}
$plan = @($plan)
if ($MaxGroups -gt 0 -and $plan.Count -gt $MaxGroups) { $stats.keptCap = $plan.Count - $MaxGroups; $plan = @($plan | Select-Object -First $MaxGroups) }

$stats.eligibleJobs = $plan.Count
if ($plan.Count) { $stats.oldestEligibleUtc = $plan[0].newest.ToString('o'); $stats.newestEligibleUtc = $plan[-1].newest.ToString('o') }
foreach ($c in $plan) {
    foreach ($m in $c.members) { $stats.eligibleEntries[$m.dir]++ }
    if (-not $stats.byMonth.Contains($c.month)) { $stats.byMonth[$c.month] = 0 }
    $stats.byMonth[$c.month] += $c.members.Count
    if ($stats.samples.Count -lt 5) { $stats.samples += "$($c.key) -> $($c.month) ($(@($c.members | ForEach-Object { $_.dir }) -join '+'))" }
}

$manifestDir = Join-Path (Join-Path $AgentShare 'archive') 'rotation-manifests'
$manifest = Join-Path $manifestDir "$stamp.jsonl"
$report = [ordered]@{
    schema = 'agent-share-rotation.v2'; share = $AgentShare; mode = $(if ($Apply) { 'apply' } else { 'dry-run' })
    nowUtc = $now.ToString('o'); cutoffUtc = $cutoff.ToString('o'); olderThanDays = $OlderThanDays; maxGroups = $MaxGroups
    manifest = $(if ($Apply) { $manifest } else { $null }); entries = $entryCount; stats = $stats
    stoppedOn = $null; elapsedMs = 0
}

function Add-ManifestRow([System.Collections.IDictionary]$row) {
    $row.tsUtc = [DateTime]::UtcNow.ToString('o')
    Add-Content -LiteralPath $manifest -Value ($row | ConvertTo-Json -Compress) -Encoding utf8 -ErrorAction Stop
}

if ($Apply -and $plan.Count) {
    New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null
    :jobs foreach ($c in $plan) {
        $movedHere = @()
        foreach ($m in $c.members) {
            $base = [ordered]@{ job = $c.key; dir = $m.dir; name = $m.entry.Name; from = $m.entry.FullName; to = $m.dest
                lastWriteTimeUtc = $m.entry.LastWriteTimeUtc.ToString('o') }
            try {
                $intent = [ordered]@{ phase = 'intent' } + $base
                Add-ManifestRow $intent
            } catch {
                $report.stoppedOn = [ordered]@{ job = $c.key; step = 'intent-row'; entry = "$($m.dir)\$($m.entry.Name)"; error = $_.Exception.Message; movedMembers = $movedHere }
                break jobs
            }
            $destDir = Split-Path -Parent $m.dest
            $moveError = $null
            try {
                if (-not $destExists[$destDir]) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null; $destExists[$destDir] = $true }
                if ($TestHookBeforeMove) { & $TestHookBeforeMove $m }
                # Same-share rename; Move-Item refuses an existing destination (no -Force), so nothing is overwritten.
                Move-Item -LiteralPath $m.entry.FullName -Destination $m.dest -ErrorAction Stop
            } catch { $moveError = $_.Exception.Message }
            if (-not $moveError) { $movedHere += "$($m.dir)\$($m.entry.Name)"; $stats.movedEntries++ }
            try {
                $done = [ordered]@{ phase = 'done'; ok = (-not $moveError); error = $moveError } + $base
                Add-ManifestRow $done
            } catch {
                $report.stoppedOn = [ordered]@{ job = $c.key; step = 'done-row'; entry = "$($m.dir)\$($m.entry.Name)"; error = $_.Exception.Message; movedMembers = $movedHere }
                break jobs
            }
            if ($moveError) {
                $report.stoppedOn = [ordered]@{ job = $c.key; step = 'move'; entry = "$($m.dir)\$($m.entry.Name)"; error = $moveError; movedMembers = $movedHere }
                break jobs
            }
        }
    }
}

$report.elapsedMs = $sw.ElapsedMilliseconds
$json = $report | ConvertTo-Json -Depth 6
if ($ReportPath) {
    $parent = Split-Path -Parent $ReportPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [IO.File]::WriteAllText($ReportPath, $json, [Text.UTF8Encoding]::new($false))
}
$json
if ($report.stoppedOn) { exit 3 }
