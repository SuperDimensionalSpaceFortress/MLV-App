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
- The plan is a snapshot, so -Apply RE-VALIDATES each job just before moving it: the job's members in
  processed\ and outbox\ are re-listed and must be the same names with the same LastWriteTimeUtc, all still
  older than the cutoff, and no entry for the job may have appeared in inbox\, running\, orphaned\ or cancel\.
  Immediately before EACH member's own rename that whole-job check runs again over the not-yet-moved members (so a
  claim that appears mid-job is caught, and so is one that came and went but left a new or rewritten member), and
  the member is re-stat'ed. A claim that appears and vanishes between two checks leaving nothing behind cannot be
  seen by any re-check; it also leaves nothing to protect. Any difference skips the WHOLE job
  (stats.changedSinceScan); if it is detected after some members already moved, the run stops (exit 3,
  step=changed), the remaining members stay live, and a "stopped" manifest row names the partial job with its
  moved and remaining members. The residual window is the milliseconds between that probe and the rename.
- -Apply holds an exclusive lock (<share>\archive\rotation-manifests\rotation.lock, opened CreateNew and removed
  on close) so two -Apply runs never work the same share at once; a second run refuses with ROTATION_LOCKED.
- Moves are same-share renames (no data copy). In -Apply mode each move is bracketed in
  <share>\archive\rotation-manifests\<stamp>.jsonl: an "intent" row is appended BEFORE the rename and a
  "done" row (ok true/false) after it. If the intent row cannot be written, that entry is not moved. On the
  first failed rename or failed "done" row the run STOPS and exits 3, naming the partial job (stoppedOn in the
  report on stdout and in -ReportPath). An intent row records an ATTEMPTED move, not its outcome: an intent with
  no done row is reconciled by checking whether its `to` path exists (moved) or its `from` path does (not moved).
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
    [scriptblock]$TestHookBeforeMove = $null,
    # Test seam: a scriptblock run before each job's re-validation with the planned job (simulates a concurrent write).
    [scriptblock]$TestHookBeforeRevalidate = $null
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

function Get-JobEntriesNow([string]$key, [string[]]$dirs) {
    # This job's entries in $dirs as they are NOW: a server-filtered "<key>*" listing, kept to exactly this job.
    foreach ($d in $dirs) {
        $di = [IO.DirectoryInfo](Join-Path $AgentShare $d)
        if (-not $di.Exists) { continue }
        foreach ($e in $di.EnumerateFileSystemInfos("$key*")) {
            if (Test-AgentShareJobEntry -Name $e.Name -JobId $key) { [pscustomobject]@{ dir = $d; entry = $e } }
        }
    }
}

function Get-JobChange($c, [string[]]$moved = @()) {
    # $null when the job is exactly as planned; otherwise why it is not. Run before the job's first rename AND before
    # every later one, so a claim that came and went between two renames is still caught by what it left behind
    # (a new or rewritten member). $moved = the members this run already renamed, which are expected to be gone.
    $liveNow = @(Get-JobEntriesNow $c.key @('inbox', 'running', 'orphaned', 'cancel'))
    if ($liveNow.Count) { return "live entry appeared: $($liveNow[0].dir)\$($liveNow[0].entry.Name)" }
    $planned = @{}
    foreach ($m in $c.members) {
        $id = "$($m.dir)\$($m.entry.Name)"
        if ($id -notin $moved) { $planned[$id] = $m.plannedUtc }
    }
    $current = @(Get-JobEntriesNow $c.key $rotated)
    if ($current.Count -ne $planned.Count) { return "membership changed: planned $($planned.Count), now $($current.Count)" }
    foreach ($n in $current) {
        $id = "$($n.dir)\$($n.entry.Name)"
        if (-not $planned.ContainsKey($id)) { return "new member: $id" }
        if ($n.entry.LastWriteTimeUtc -ne $planned[$id]) { return "rewritten since scan: $id" }
        if ($n.entry.LastWriteTimeUtc -ge $cutoff) { return "younger than cutoff: $id" }
    }
    return $null
}

function Get-MemberChange($c, $m, [string[]]$moved) {
    # Immediately before ONE member's rename: the WHOLE job is re-validated (no live claim in any inbox/running/
    # orphaned/cancel shape, nonce .tmp markers included; the not-yet-moved members are exactly the planned ones with
    # their planned times, all older than the cutoff; nothing new has appeared), then the member itself is re-stat'ed.
    $why = Get-JobChange $c $moved
    if ($why) { return $why }
    $m.entry.Refresh()
    if (-not $m.entry.Exists) { return "vanished since scan" }
    if ($m.entry.LastWriteTimeUtc -ne $m.plannedUtc) { return "rewritten since scan" }
    return $null
}

$stats.changedSinceScan = 0
$stats.changed = @()
if ($Apply -and $plan.Count) {
    New-Item -ItemType Directory -Force -Path $manifestDir | Out-Null
    $lockPath = Join-Path $manifestDir 'rotation.lock'
    try {
        $lock = [IO.FileStream]::new($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)
    } catch {
        # Only "file exists" (Win32 80, or 183) means another run holds the lock; anything else (access denied, share
        # unreachable) is a different failure and says so. DeleteOnClose removes the file when the holder's handle closes,
        # including on a crash; only a lost SMB session can leave it until the server times that session out.
        $ex = $_.Exception
        while ($ex.InnerException) { $ex = $ex.InnerException }
        $code = $ex.HResult -band 0xFFFF
        if ($ex -is [IO.IOException] -and $code -in 80, 183) {
            throw "ROTATION_LOCKED $lockPath exists: another -Apply run holds this share (a lost SMB session can leave it until the server times the session out)"
        }
        throw "ROTATION_LOCK_FAILED ${lockPath}: $($ex.Message)"
    }
    try {
    foreach ($c in $plan) { foreach ($m in $c.members) { $m | Add-Member -NotePropertyName plannedUtc -NotePropertyValue $m.entry.LastWriteTimeUtc -Force } }
    :jobs foreach ($c in $plan) {
        $movedHere = @()
        if ($TestHookBeforeRevalidate) { & $TestHookBeforeRevalidate $c }
        $why = Get-JobChange $c
        if ($why) {
            $stats.changedSinceScan++
            if ($stats.changed.Count -lt 10) { $stats.changed += "$($c.key): $why" }
            continue jobs
        }
        foreach ($m in $c.members) {
            $why = Get-MemberChange $c $m $movedHere
            if ($why) {
                if (-not $movedHere.Count) {
                    $stats.changedSinceScan++
                    if ($stats.changed.Count -lt 10) { $stats.changed += "$($c.key): $($m.dir)\$($m.entry.Name) $why" }
                    continue jobs
                }
                $report.stoppedOn = [ordered]@{ job = $c.key; step = 'changed'; entry = "$($m.dir)\$($m.entry.Name)"; error = $why; movedMembers = $movedHere }
                break jobs
            }
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
    if ($report.stoppedOn) {
        # Name the partial job in the manifest too, with what moved and what stayed live, so the manifest alone
        # accounts for it. A failure to write this row is reported, never hidden (the stdout report still has it).
        $sj = @($plan | Where-Object { $_.key -eq $report.stoppedOn.job })[0]
        $moved = @($report.stoppedOn.movedMembers)
        $report.stoppedOn.remainingMembers = @($sj.members | ForEach-Object { "$($_.dir)\$($_.entry.Name)" } | Where-Object { $_ -notin $moved })
        try {
            $row = [ordered]@{ phase = 'stopped' } + $report.stoppedOn
            Add-ManifestRow $row
        } catch { $report.stoppedOn.stoppedRowError = $_.Exception.Message }
    }
    } finally { $lock.Dispose() }
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
