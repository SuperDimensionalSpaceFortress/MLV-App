[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = '',
    [string[]]$Only = @()
)

# BACHELOR-AGENT-LIVE-MARKER-RECONCILE-1: a live Bachelor agent retires a finished job's leaked running\ marker artifacts.
# 2026-11-04: running\<job>.started.json plus its completion tmp stayed after outbox\<job>.result.json was published, and every
# Wait-VenueQuiet probe read GATE_BUSY for 2.4 h, because the agent only reconciled running\ at start-up.
#   e2e   the real agent, polling at 1 s, on a private root. No playback, no venue, no share, no heavy-slot.py (every job is a light one):
#         reader-denied-completion   the race itself: a reader without delete sharing holds a running job's marker as it completes
#         incident-fixture           a finished job's marker + readable tmp + result -> cleared within one poll, Wait-VenueQuiet's count reads 0
#         tmp-only-fixture           the 2026-10-10 shape: tmp + result and no final marker -> cleared
#         live-job-left-alone        a running job's marker is untouched although a result for its id already exists
#         no-result-left-alone       a marker with no published result is untouched
#         locked-marker-quarantined  a marker nobody can read is quarantined with one log line, then retired once the reader lets go
#         live-locked-then-withdrawn  sol pass-3 A1: locked through a live pass, then the result withdrawn: no exit-127 receipt, the marker is not orphaned
#         live-locked-stale-tmp-then-withdrawn  the same with an old result tmp on disk: the tmp is not promoted
#         completion-denied-then-withdrawn  the same class by the ordinary completion route: a retire denied as the job finished, then the result withdrawn
#   unit  the agent's real functions, lifted from its AST:
#         inbox-job-left-alone       a job whose script is in inbox\ is the live set: no recovery runs, even with marker + result
#         own-claim-retire-retried   this agent's own unlaunched claim whose retire failed stays quarantined, then clears
#         live-reconcile-result-withdrawn  a result deleted by a resubmitting client after the live check: no adopt/orphan recovery, no quarantine
#         completed-tmp-kept-on-failed-move  the marker moved but the completed tmp could not: the tmp is KEPT, the job quarantined, then the live retry completes it
#         live-withdrawn-stale-result-tmp  sol pass-3 A2: result deleted after the live check with an old tmp present (both tmp forms): not promoted, marker not retired
#         live-withdrawn-inside-complete  the result deleted inside Complete-StartedMarker, after the live re-check: nothing lands in the start-up quarantine
#         live-quarantine-lifecycle  a live-origin entry is dropped when the marker went, the result went, or a script came back; retried and released otherwise
#         live-path-no-startup-recovery  the live functions call no start-up recovery, and the flags that used to guard it are gone
# Prints one JSON object. Exit 0 = every row passed. Exit 1 = a row failed; the object says which (a RED run needs the rows, not the first throw).

$ErrorActionPreference = 'Stop'
# PowerShell variables are case-insensitive: assigning $agentScript below also assigns $AgentScript, so the override is captured first.
$agentScriptOverridden = [bool]$AgentScript
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
$caseNames = @('reader-denied-completion', 'incident-fixture', 'tmp-only-fixture', 'live-job-left-alone', 'no-result-left-alone', 'locked-marker-quarantined', 'inbox-job-left-alone', 'own-claim-retire-retried', 'completed-tmp-kept-on-failed-move', 'live-reconcile-result-withdrawn', 'live-locked-then-withdrawn', 'live-locked-stale-tmp-then-withdrawn', 'completion-denied-then-withdrawn', 'live-withdrawn-stale-result-tmp', 'live-withdrawn-inside-complete', 'live-quarantine-lifecycle', 'live-path-no-startup-recovery')
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($n in $Only) { if ($caseNames -notcontains $n) { throw "unknown case '$n' (known: $($caseNames -join ', '))" } }
function Want([string]$Name) { return ($Only.Count -eq 0 -or $Only -contains $Name) }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }

$runId = 'agent-live-marker-{0}-{1}-{2}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID, [guid]::NewGuid().ToString('N').Substring(0, 8)
$base = Join-Path $RepoRoot ".claude-state\testing\$runId"
New-Item -ItemType Directory -Force -Path $base | Out-Null

# Wait-VenueQuiet's own rule for what counts as busy in running\, read from it and not restated here.
$venueScript = [IO.File]::ReadAllText((Join-Path $RepoRoot 'tools\profiling\dual-venue\Wait-VenueQuiet.ps1'))
$setAsideMatch = [regex]::Match($venueScript, '(?m)^\$setAsideRecordPattern = ''([^'']+)''')
Assert-True $setAsideMatch.Success 'Wait-VenueQuiet.ps1 still defines $setAsideRecordPattern'
$setAsideRecordPattern = $setAsideMatch.Groups[1].Value

$results = [ordered]@{}
$agents = [System.Collections.Generic.List[object]]::new()

function New-Case([string]$Name) {
    $root = Join-Path $base $Name
    foreach ($d in @('inbox', 'outbox', 'running', 'processed', 'logs')) { New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null }
    return [pscustomobject]@{ name = $Name; root = $root; running = (Join-Path $root 'running'); outbox = (Join-Path $root 'outbox'); processed = (Join-Path $root 'processed'); log = (Join-Path $root 'logs\agent.log'); agent = $null }
}
function Start-Agent($c) {
    $si = [Diagnostics.ProcessStartInfo]::new((Get-Command pwsh.exe).Source)
    foreach ($a in @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $agentScript, '-Root', $c.root, '-PollSeconds', '1',
            '-MutexName', "Local\MlvLiveMarkerTest-$PID-$($c.name)", '-GateIdleSampleSec', '0', '-JobTimeoutSec', '120', '-HeavySlotMode', 'status')) { [void]$si.ArgumentList.Add($a) }
    $si.UseShellExecute = $false; $si.CreateNoWindow = $true
    $si.RedirectStandardOutput = $true; $si.RedirectStandardError = $true
    $p = [Diagnostics.Process]::Start($si)
    $agents.Add($p)
    $c.agent = $p
    $hb = Join-Path $c.root 'heartbeat.txt'
    Assert-True (Wait-Until { Test-Path -LiteralPath $hb } 30) "$($c.name): the agent heartbeats"
}
function Stop-Agent($c) {
    if ($null -ne $c.agent -and -not $c.agent.HasExited) { try { $c.agent.Kill($true) } catch { }; [void]$c.agent.WaitForExit(10000) }
}
function Wait-Until([scriptblock]$Cond, [double]$TimeoutSec) {
    $dl = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $dl) { if (& $Cond) { return $true }; Start-Sleep -Milliseconds 100 }
    return [bool](& $Cond)
}
# The agent's poll counter. A read can land in the instant the heartbeat is rewritten, so it retries.
function Get-HeartbeatGeneration($c) {
    $hb = Join-Path $c.root 'heartbeat.txt'
    for ($i = 0; $i -lt 50; $i++) {
        try {
            $fs = [IO.File]::Open($hb, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite, Delete')
            try { $t = ([IO.StreamReader]::new($fs)).ReadToEnd() } finally { $fs.Dispose() }
            $m = [regex]::Match($t, 'generation=(\d+)')
            if ($m.Success) { return [int]$m.Groups[1].Value }
        } catch { }
        Start-Sleep -Milliseconds 50
    }
    throw "FAIL: no heartbeat generation could be read from $hb"
}
# The artifacts Wait-VenueQuiet counts as a running job.
function Get-RunningCounted($c) { return @(Get-ChildItem -LiteralPath $c.running -ErrorAction Stop | Where-Object { $_.Name -notmatch $setAsideRecordPattern } | ForEach-Object Name) }
function Get-LogLines($c, [string]$Text) {
    if (-not (Test-Path -LiteralPath $c.log)) { return @() }
    $raw = $null
    $fs = [IO.File]::Open($c.log, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite, Delete')
    try { $raw = ([IO.StreamReader]::new($fs)).ReadToEnd() } finally { $fs.Dispose() }
    return @($raw -split "`r?`n" | Where-Object { $_.Contains($Text) })
}
# Drops a file whole: a poll can never read a half-written fixture.
function Put-File([string]$Path, [string]$Text) {
    $t = "$Path.fixture"
    [IO.File]::WriteAllText($t, $Text)
    [IO.File]::Move($t, $Path)
}
function New-MarkerJson([string]$JobId, [string]$Phase = 'launched', [string]$Status = 'started') {
    # childPid 4 is never a job child: nothing in these rows may adopt or stop it.
    return ([ordered]@{ schema = 'mlvapp.agent.started.v1'; jobId = $JobId; phase = $Phase; status = $Status; childPid = 4; childStartUtc = '2026-11-04T08:42:09.0000000Z'; childStartEpochMs = 1793695329000; childImagePath = 'C:\fixture\pwsh.exe'; startedUtc = '2026-11-04T08:42:09.0000000Z'; timeoutSec = 60; agentPid = 4; sourceInboxPath = 'C:\fixture\absent.job.ps1'; outFile = 'C:\fixture\absent.out.txt'; errFile = 'C:\fixture\absent.err.txt' } | ConvertTo-Json -Compress)
}
function New-ResultJson([string]$JobId) { return ([ordered]@{ jobId = $JobId; exitCode = 0; fixture = $true } | ConvertTo-Json -Compress) }
function Submit-Job($c, [string]$JobId, [string]$Body) {
    Put-File (Join-Path $c.root "inbox\$JobId.job.ps1") $Body
}
function Wait-Launched($c, [string]$JobId, [double]$TimeoutSec) {
    $marker = Join-Path $c.running "$JobId.started.json"
    return (Wait-Until {
            try {
                $fs = [IO.File]::Open($marker, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite, Delete')
                try { $t = ([IO.StreamReader]::new($fs)).ReadToEnd() } finally { $fs.Dispose() }
                return (($t | ConvertFrom-Json).phase -eq 'launched')
            } catch { return $false }
        } $TimeoutSec)
}
# Waits out $Polls of the agent's own polls, so "nothing happened" means the agent looked and left it alone.
function Wait-Polls($c, [int]$Polls) {
    $from = Get-HeartbeatGeneration $c
    Assert-True (Wait-Until { ((Get-HeartbeatGeneration $c) - $from) -ge $Polls } 30) "$($c.name): the agent polled $Polls more times"
}
# What a withdrawn job must look like after those polls: no receipt and no recovery, and the fresh submission is free to arrive.
function Assert-WithdrawnLeftAlone($c, [string]$JobId) {
    $res = Join-Path $c.outbox "$JobId.result.json"
    $got = if (Test-Path -LiteralPath $res) { Get-Content -LiteralPath $res -Raw } else { '' }
    Assert-True (-not (Test-Path -LiteralPath $res)) "nothing republished a result for the withdrawn id: $got"
    Assert-True (Test-Path -LiteralPath (Join-Path $c.running "$JobId.started.json")) 'the marker is still in running\'
    Assert-True (@(Get-ChildItem -LiteralPath $c.processed -Filter "$JobId.started.*").Count -eq 0) 'the marker was not retired against nothing'
    Assert-True (@(Get-LogLines $c "orphaned job=$JobId").Count -eq 0) "no orphan recovery ran: $((Get-LogLines $c "orphaned job=$JobId") -join ' | ')"
}
function Invoke-Row([string]$Name, [scriptblock]$Body) {
    if (-not (Want $Name)) { return }
    try { $results[$Name] = [ordered]@{ status = 'PASS'; detail = (& $Body) } }
    catch { $results[$Name] = [ordered]@{ status = 'FAIL'; error = $_.Exception.Message } }
}

try {
    # THE RACE: a reader without delete sharing (um-run.ps1's Get-Content, or a handle a network flap left open) holds the marker as the job completes.
    Invoke-Row 'reader-denied-completion' {
        $c = New-Case 'reader-denied-completion'
        try {
            Start-Agent $c
            $job = 'venue-quiet-probe-race'
            Submit-Job $c $job 'Start-Sleep -Seconds 4; "ran"'
            Assert-True (Wait-Launched $c $job 30) 'the job launched'
            $markerPath = Join-Path $c.running "$job.started.json"
            $reader = [IO.File]::Open($markerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            $resultPath = Join-Path $c.outbox "$job.result.json"
            try {
                Assert-True (Wait-Until { Test-Path -LiteralPath $resultPath } 30) 'the result is published while a reader holds the marker'
                # Past Move-FileChecked's bounded retry (about 1.8 s), so the failure is the one that outlasts it.
                Start-Sleep -Seconds 4
                $held = Get-RunningCounted $c
                Assert-True ($held -contains "$job.started.json") "the marker is still in running\ while the reader holds it: [$($held -join ',')]"
            }
            finally { $reader.Dispose() }
            $released = Get-Date
            $cleared = Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 8
            $left = Get-RunningCounted $c
            Assert-True $cleared "after the reader lets go, running\ empties within 8 s at a 1 s poll: left [$($left -join ',')]"
            $clearedSec = [Math]::Round(((Get-Date) - $released).TotalSeconds, 1)
            Assert-True (@(Get-LogLines $c "marker kept job=$job").Count -eq 1) "the failed retire logged exactly one 'marker kept' line, not one per retry: $(@(Get-LogLines $c "marker kept job=$job").Count)"
            $record = Get-Content -LiteralPath (Join-Path $c.processed "$job.started.completed.json") -Raw | ConvertFrom-Json
            Assert-True ($record.status -eq 'completed' -and $record.resultPath -eq $resultPath) "processed\ holds the completed record: $($record | ConvertTo-Json -Compress)"
            Assert-True (Test-Path -LiteralPath (Join-Path $c.processed "$job.job.ps1")) 'the job script reached processed\'
            Assert-True (@(Get-ChildItem -LiteralPath $c.running | Where-Object { $_.Name -match '\.tmp$' }).Count -eq 0) 'no unresolved tmp is left'
            return [ordered]@{ clearedSecAfterRelease = $clearedSec }
        }
        finally { Stop-Agent $c }
    }

    # Row 1: the incident's on-disk shape, seen by an agent that is already live. The result lands last, so nothing is eligible until the fixture is whole.
    Invoke-Row 'incident-fixture' {
        $c = New-Case 'incident-fixture'
        try {
            Start-Agent $c
            $job = 'lease-batch-open-20261104T083925Z-lbfixture'
            $tmp = "$job.started.json.335c4d6c403943309f1e6486e293baab.tmp"
            Put-File (Join-Path $c.running "$job.started.json") (New-MarkerJson $job)
            Put-File (Join-Path $c.running $tmp) (New-MarkerJson $job 'launched' 'completed')
            $genBefore = Get-HeartbeatGeneration $c
            Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
            $cleared = Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 8
            $polls = (Get-HeartbeatGeneration $c) - $genBefore
            $left = Get-RunningCounted $c
            Assert-True $cleared "a Wait-VenueQuiet-style count of running\ reads 0 within 8 s at a 1 s poll: left [$($left -join ',')]"
            # Counted in the agent's own polls, not seconds, so a loaded host cannot redden it: the next poll clears it, plus one for this read arriving late.
            Assert-True ($polls -le 2) "cleared within one poll: the heartbeat generation moved $polls"
            Assert-True (Test-Path -LiteralPath (Join-Path $c.processed "$job.started.completed.json")) 'the marker went to processed\ by the start-up path (status completed)'
            Assert-True (@(Get-ChildItem -LiteralPath $c.running -Filter "$tmp.stale-*").Count -eq 1) 'the tmp is preserved as a set-aside record, never deleted'
            # The agent logs after it retires, so the line can land a few ms after running\ empties: wait for it, then count.
            Assert-True (Wait-Until { @(Get-LogLines $c "live marker reconcile job=$job").Count -ge 1 } 8) 'the agent logs the reconcile'
            Assert-True (@(Get-LogLines $c "live marker reconcile job=$job").Count -eq 1) 'one agent log line for the job'
            Assert-True ((Get-Content -LiteralPath (Join-Path $c.outbox "$job.result.json") -Raw | ConvertFrom-Json).fixture -eq $true) 'the published result is untouched'
            return [ordered]@{ agentPollsToClear = $polls; runningCounted = 0 }
        }
        finally { Stop-Agent $c }
    }

    # The 2026-10-10 shape: the tmp alone.
    Invoke-Row 'tmp-only-fixture' {
        $c = New-Case 'tmp-only-fixture'
        try {
            Start-Agent $c
            $job = 'tmp-only-job'
            Put-File (Join-Path $c.running "$job.started.json.0123456789abcdef0123456789abcdef.tmp") (New-MarkerJson $job 'launched' 'completed')
            Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
            $cleared = Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 8
            Assert-True $cleared "running\ empties: left [$((Get-RunningCounted $c) -join ',')]"
            Assert-True (Test-Path -LiteralPath (Join-Path $c.processed "$job.started.completed.json")) 'the promoted tmp was completed to processed\'
            return 'cleared'
        }
        finally { Stop-Agent $c }
    }

    # Row 2: a live job's marker, with a result for its id already there, is left alone. The job then finishes and retires it normally.
    Invoke-Row 'live-job-left-alone' {
        $c = New-Case 'live-job-left-alone'
        try {
            Start-Agent $c
            $job = 'venue-quiet-probe-live'
            $resultPath = Join-Path $c.outbox "$job.result.json"
            Put-File $resultPath (New-ResultJson $job)
            Submit-Job $c $job 'Start-Sleep -Seconds 7; "ran"'
            Assert-True (Wait-Launched $c $job 30) 'the job launched'
            # Five polls with the job running: its marker and its job file stay, nothing is retired, nothing is logged for it.
            $until = (Get-Date).AddSeconds(5)
            while ((Get-Date) -lt $until) {
                Assert-True (Test-Path -LiteralPath (Join-Path $c.running "$job.started.json")) 'the live marker stays in running\'
                Assert-True (-not (Test-Path -LiteralPath (Join-Path $c.processed "$job.started.completed.json"))) 'the live marker was not retired'
                Start-Sleep -Milliseconds 200
            }
            Assert-True (@(Get-LogLines $c "live marker reconcile job=$job").Count -eq 0) 'no reconcile line for a live job'
            Assert-True (Wait-Until { -not (Test-Path -LiteralPath (Join-Path $c.running "$job.started.json")) } 30) 'the job finished and retired its own marker'
            $r = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
            Assert-True ($r.exitCode -eq 0 -and $null -eq $r.fixture -and $r.stdout -match 'ran') "the job's own result replaced the fixture: $($r | ConvertTo-Json -Compress -Depth 2)"
            return 'left alone while running'
        }
        finally { Stop-Agent $c }
    }

    # Row 3: no published result, so no live reconcile: the start-up rules for such a marker (adopt, orphan, re-queue) stay start-up only.
    Invoke-Row 'no-result-left-alone' {
        $c = New-Case 'no-result-left-alone'
        try {
            Start-Agent $c
            $job = 'no-result-job'
            $markerPath = Join-Path $c.running "$job.started.json"
            $tmpPath = Join-Path $c.running "$job.started.json.0123456789abcdef0123456789abcdef.tmp"
            Put-File $markerPath (New-MarkerJson $job)
            Put-File $tmpPath (New-MarkerJson $job)
            $before = (Get-FileHash -LiteralPath $markerPath).Hash + (Get-FileHash -LiteralPath $tmpPath).Hash
            Start-Sleep -Seconds 5
            Assert-True (((Get-FileHash -LiteralPath $markerPath).Hash + (Get-FileHash -LiteralPath $tmpPath).Hash) -eq $before) 'marker and tmp are byte-identical after five polls'
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $c.outbox "$job.result.json"))) 'no orphan result was published'
            Assert-True (@(Get-LogLines $c "job=$job").Count -eq 0) "nothing was logged for it: $((Get-LogLines $c "job=$job") -join ' | ')"
            return 'left alone'
        }
        finally { Stop-Agent $c }
    }

    # A marker nobody can open: quarantined, one line, nothing touched; retired once the reader lets go.
    Invoke-Row 'locked-marker-quarantined' {
        $c = New-Case 'locked-marker-quarantined'
        try {
            Start-Agent $c
            $job = 'locked-job'
            $markerPath = Join-Path $c.running "$job.started.json"
            Put-File $markerPath (New-MarkerJson $job)
            $reader = [IO.File]::Open($markerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
            try {
                Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
                Assert-True (Wait-Until { @(Get-LogLines $c "QUARANTINED job=$job").Count -ge 1 } 8) 'the unreadable marker is quarantined'
                Start-Sleep -Seconds 4
                Assert-True (@(Get-LogLines $c "live marker reconcile job=$job").Count -eq 1) "one reconcile line, not one per poll: $(@(Get-LogLines $c "live marker reconcile job=$job").Count)"
                Assert-True (@(Get-LogLines $c "QUARANTINED job=$job").Count -eq 1) "one QUARANTINED line, not one per poll: $(@(Get-LogLines $c "QUARANTINED job=$job").Count)"
            }
            finally { $reader.Dispose() }
            Assert-True (Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 8) "retired once the reader lets go: left [$((Get-RunningCounted $c) -join ',')]"
            return 'quarantined, then retired'
        }
        finally { Stop-Agent $c }
    }

    # sol pass-3 A1: locked through one live pass, so it is held for a retry; then a resubmitting client deletes the result. The retry must not run the
    # start-up recovery, which publishes exit 127 for the reused id before its fresh script arrives. The result goes first and the reader second: the same state at the next poll, with no window for an honest retire.
    Invoke-Row 'live-locked-then-withdrawn' {
        $c = New-Case 'live-locked-then-withdrawn'
        try {
            Start-Agent $c
            $job = 'resubmitted'
            $markerPath = Join-Path $c.running "$job.started.json"
            Put-File $markerPath (New-MarkerJson $job)
            $before = (Get-FileHash -LiteralPath $markerPath).Hash
            $reader = [IO.File]::Open($markerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
            try {
                Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
                Assert-True (Wait-Until { @(Get-LogLines $c "QUARANTINED job=$job").Count -ge 1 } 8) 'the locked marker is held for a retry'
                Remove-Item -LiteralPath (Join-Path $c.outbox "$job.result.json") -Force
            }
            finally { $reader.Dispose() }
            Wait-Polls $c 4
            Assert-WithdrawnLeftAlone $c $job
            Assert-True ((Get-FileHash -LiteralPath $markerPath).Hash -eq $before) 'the marker is byte-identical'
            return 'left alone'
        }
        finally { Stop-Agent $c }
    }

    # The same, with an old result tmp on disk: the retry must not restore it as the job's receipt either.
    Invoke-Row 'live-locked-stale-tmp-then-withdrawn' {
        $c = New-Case 'live-locked-stale-tmp-then-withdrawn'
        try {
            Start-Agent $c
            $job = 'resubmitted-tmp'
            $markerPath = Join-Path $c.running "$job.started.json"
            $staleTmp = Join-Path $c.outbox "$job.result.0123456789abcdef0123456789abcdef.tmp"
            Put-File $staleTmp (([ordered]@{ jobId = $job; exitCode = 0; stdout = 'OLD RESULT' }) | ConvertTo-Json -Compress)
            Put-File $markerPath (New-MarkerJson $job)
            $reader = [IO.File]::Open($markerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
            try {
                Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
                Assert-True (Wait-Until { @(Get-LogLines $c "QUARANTINED job=$job").Count -ge 1 } 8) 'the locked marker is held for a retry'
                Remove-Item -LiteralPath (Join-Path $c.outbox "$job.result.json") -Force
            }
            finally { $reader.Dispose() }
            Wait-Polls $c 4
            Assert-WithdrawnLeftAlone $c $job
            Assert-True (Test-Path -LiteralPath $staleTmp) 'the old result tmp was not promoted'
            return 'left alone'
        }
        finally { Stop-Agent $c }
    }

    # The class, by the route that does not start in the live pass: a job finishes under a reader that denies its retire, then its result is withdrawn.
    Invoke-Row 'completion-denied-then-withdrawn' {
        $c = New-Case 'completion-denied-then-withdrawn'
        try {
            Start-Agent $c
            $job = 'venue-quiet-probe-withdrawn'
            Submit-Job $c $job 'Start-Sleep -Seconds 4; "ran"'
            Assert-True (Wait-Launched $c $job 30) 'the job launched'
            $markerPath = Join-Path $c.running "$job.started.json"
            $reader = [IO.File]::Open($markerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            try {
                Assert-True (Wait-Until { @(Get-LogLines $c "marker kept job=$job").Count -ge 1 } 30) 'the denied retire is held for a retry'
                Remove-Item -LiteralPath (Join-Path $c.outbox "$job.result.json") -Force
            }
            finally { $reader.Dispose() }
            Wait-Polls $c 4
            Assert-WithdrawnLeftAlone $c $job
            return 'left alone'
        }
        finally { Stop-Agent $c }
    }

    # ------------------------------------------------------------ unit: the agent's real functions, lifted from its AST
    $tokens = $null; $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
    Assert-True ($parseErrors.Count -eq 0) "the agent parses ($($parseErrors.Count) errors)"
    function Get-AgentFunction([string]$Name) { return $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true) }
    $hasFix = $null -ne (Get-AgentFunction 'Invoke-LiveMarkerReconcile')
    # An agent with no such function can only be the RED run's base copy; on the tracked agent its absence is a failure, never a skip.
    function Skip-OnBase([string]$Name) {
        if ($hasFix) { return $false }
        if (Want $Name) {
            if ($agentScriptOverridden) { $results[$Name] = [ordered]@{ status = 'NA'; detail = 'the base agent has no Invoke-LiveMarkerReconcile' } }
            else { $results[$Name] = [ordered]@{ status = 'FAIL'; error = 'FAIL: the agent defines no Invoke-LiveMarkerReconcile' } }
        }
        return $true
    }

    if (-not (Skip-OnBase 'inbox-job-left-alone')) {
        Invoke-Row 'inbox-job-left-alone' {
            $c = New-Case 'inbox-job-left-alone'
            $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox')
            $script:RecoveryQuarantine = @{}; $script:LiveRetireQuarantine = @{}
            $script:Recovered = [System.Collections.Generic.List[string]]::new()
            foreach ($name in @('Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Invoke-LiveMarkerReconcile')) { . ([scriptblock]::Create((Get-AgentFunction $name).Extent.Text)) }
            function Invoke-LiveMarkerRetire { param([string]$JobId) $script:Recovered.Add($JobId); return [pscustomobject]@{ state = 'retired'; why = $null } }
            function Add-AgentLogLine { param([string]$Message) }
            foreach ($job in @('in-inbox', 'not-in-inbox')) {
                Put-File (Join-Path $c.running "$job.started.json") (New-MarkerJson $job)
                Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
            }
            Put-File (Join-Path $script:inbox 'in-inbox.job.ps1') '"queued"'
            Invoke-LiveMarkerReconcile
            Assert-True (($script:Recovered -join ',') -eq 'not-in-inbox') "only the job with no script in inbox\ is reconciled: [$($script:Recovered -join ',')]"
            return 'left alone'
        }
    }

    if (-not (Skip-OnBase 'own-claim-retire-retried')) {
        Invoke-Row 'own-claim-retire-retried' {
            $c = New-Case 'own-claim-retire-retried'
            $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox'); $script:processed = $c.processed
            $script:RecoveryQuarantine = @{}; $script:LiveRetireQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
            $script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            $script:Logged = [System.Collections.Generic.List[string]]::new()
            foreach ($name in @('Move-FileChecked', 'Write-JsonFileAtomic', 'Read-JsonFile', 'Complete-StartedMarker', 'Set-MarkerRetireQuarantine', 'Set-OwnPendingMarkerTmp', 'Clear-OwnPendingMarkerTmp',
                    'Get-MarkerTempFiles', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-JobMarkerTemps', 'Resolve-StrandedResult', 'Resolve-StartedMarkerFile', 'Get-JobRecoveryPending', 'Resolve-JobRecovery')) {
                . ([scriptblock]::Create((Get-AgentFunction $name).Extent.Text))
            }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            $job = 'own-claim'
            $markerPath = Join-Path $c.running "$job.started.json"
            $writeId = [guid]::NewGuid().ToString('N')
            [void]$script:OwnMarkerWriteIds.Add($writeId)
            Put-File $markerPath ([ordered]@{ jobId = $job; phase = 'admission-wait'; status = 'admission-wait'; childPid = $null; writeId = $writeId } | ConvertTo-Json -Compress)
            Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            $reader = [IO.File]::Open($markerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            try {
                $first = Resolve-JobRecovery -JobId $job
                Assert-True (-not $first) 'its retire failed, so recovery does not report it reconciled'
                Assert-True ($script:RecoveryQuarantine.ContainsKey($job)) 'the job stays quarantined'
                Assert-True (Test-Path -LiteralPath $markerPath) 'the marker is still there'
                Assert-True (@(Get-ChildItem -LiteralPath $c.running | Where-Object { $_.Name -ne "$job.started.json" }).Count -eq 0) 'a failed retire leaves no tmp or set-aside record behind, so retries cannot pile up'
                $null = Resolve-JobRecovery -JobId $job
                Assert-True (@($script:Logged | Where-Object { $_ -like 'QUARANTINED*' }).Count -le 1) "a repeat failure logs nothing new: $($script:Logged -join ' | ')"
            }
            finally { $reader.Dispose(); $ErrorActionPreference = 'Stop' }
            $ErrorActionPreference = 'Continue'
            try { $second = Resolve-JobRecovery -JobId $job } finally { $ErrorActionPreference = 'Stop' }
            Assert-True $second 'once the reader lets go it is reconciled'
            Assert-True (-not (Test-Path -LiteralPath $markerPath)) 'the marker is retired'
            Assert-True (-not $script:RecoveryQuarantine.ContainsKey($job)) 'the quarantine is released'
            return 'retried, then retired'
        }
    }

    # sol A1: the marker moved to processed\ but the replacing rename of the completion had not landed, so the completed record is only the tmp.
    # If that tmp cannot move either, deleting it leaves processed\ holding status=started with nothing left to retry. Keep it; recovery completes it.
    if (-not (Skip-OnBase 'completed-tmp-kept-on-failed-move')) {
        Invoke-Row 'completed-tmp-kept-on-failed-move' {
            $c = New-Case 'completed-tmp-kept-on-failed-move'
            $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox'); $script:processed = $c.processed
            $script:RecoveryQuarantine = @{}; $script:LiveRetireQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
            $script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            $script:Logged = [System.Collections.Generic.List[string]]::new()
            $lift = @('Move-FileChecked', 'Write-JsonFileAtomic', 'Read-JsonFile', 'Complete-StartedMarker', 'Set-MarkerRetireQuarantine', 'Set-OwnPendingMarkerTmp', 'Clear-OwnPendingMarkerTmp',
                'Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-JobMarkerTemps', 'Invoke-LiveMarkerRetire', 'Invoke-LiveMarkerReconcile')
            foreach ($name in $lift) { . ([scriptblock]::Create((Get-AgentFunction $name).Extent.Text)) }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            $job = 'tmp-kept'
            $markerPath = Join-Path $c.running "$job.started.json"
            $resultPath = Join-Path $c.outbox "$job.result.json"
            Put-File $markerPath (New-MarkerJson $job)
            Put-File $resultPath (New-ResultJson $job)
            # Filesystem sharing violations, simulated: the replacing rename never lands (its tmp stays), the marker move works, the tmp move is denied.
            function Write-JsonFileAtomic {
                param([string]$Path, $InputObject, [int]$Depth = 8)
                $tmp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
                [IO.File]::WriteAllText($tmp, ($InputObject | ConvertTo-Json -Depth 8))
                return [pscustomobject]@{ ok = $false; tmp = $tmp; error = 'denied' }
            }
            function Move-FileChecked {
                param([string]$Source, [string]$Destination, [bool]$Overwrite = $false, [int]$Attempts = 10, [int]$DelayMs = 200)
                if ($Source -like '*.tmp') { return [pscustomobject]@{ ok = $false; attempts = $Attempts; error = 'IOException: denied' } }
                [IO.File]::Move($Source, $Destination, $Overwrite)
                return [pscustomobject]@{ ok = $true; attempts = 1; error = $null }
            }
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            try { Complete-StartedMarker -MarkerPath $markerPath -Status 'completed' -ResultPath $resultPath } finally { $ErrorActionPreference = 'Stop' }
            $tmps = @(Get-ChildItem -LiteralPath $c.running | Where-Object { $_.Name -match '\.tmp$' })
            Assert-True ($tmps.Count -eq 1) "the completed tmp is kept while its move is denied: [$((Get-ChildItem -LiteralPath $c.running).Name -join ',')]"
            Assert-True ((Get-Content -LiteralPath $tmps[0].FullName -Raw | ConvertFrom-Json).status -eq 'completed') 'the kept tmp is the completed record'
            Assert-True ($script:LiveRetireQuarantine.ContainsKey($job)) "the job is quarantined so the live reconcile retries it: $($script:Logged -join ' | ')"
            Assert-True (-not $script:RecoveryQuarantine.ContainsKey($job)) 'start-up recovery does not retry it'
            # The sharing violation ends: the REAL move and write functions are back, and the per-poll retry finishes the job.
            foreach ($name in @('Move-FileChecked', 'Write-JsonFileAtomic')) { . ([scriptblock]::Create((Get-AgentFunction $name).Extent.Text)) }
            $ErrorActionPreference = 'Continue'
            try { Invoke-LiveMarkerReconcile } finally { $ErrorActionPreference = 'Stop' }
            Assert-True (-not $script:LiveRetireQuarantine.ContainsKey($job)) "the live reconcile finishes it once the reader lets go: $($script:Logged -join ' | ')"
            Assert-True (@(Get-ChildItem -LiteralPath $c.running | Where-Object { $_.Name -notmatch $setAsideRecordPattern }).Count -eq 0) 'running\ is empty'
            $rec = Get-Content -LiteralPath (Join-Path $c.processed "$job.started.completed.json") -Raw | ConvertFrom-Json
            Assert-True ($rec.status -eq 'completed') "processed\ holds the completed record: $($rec | ConvertTo-Json -Compress)"
            return 'kept, then completed'
        }
    }

    # sol pass-2 A1: the live reconcile saw the result, then a submitter reusing the job id deleted it (Submit-AgentJob removes the old result before it publishes
    # its new script). The no-result recovery (adopt / orphan / re-queue) is start-up only: it must not run, and the job must not be quarantined for a retry.
    if (-not (Skip-OnBase 'live-reconcile-result-withdrawn')) {
        Invoke-Row 'live-reconcile-result-withdrawn' {
            $c = New-Case 'live-reconcile-result-withdrawn'
            $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox'); $script:processed = $c.processed; $script:orphaned = (Join-Path $c.root 'orphaned')
            $script:RecoveryQuarantine = @{}; $script:LiveRetireQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
            $script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            $script:Logged = [System.Collections.Generic.List[string]]::new(); $script:Orphaned = [System.Collections.Generic.List[string]]::new()
            $lift = @('Move-FileChecked', 'Write-JsonFileAtomic', 'Read-JsonFile', 'Complete-StartedMarker', 'Set-MarkerRetireQuarantine', 'Set-OwnPendingMarkerTmp', 'Clear-OwnPendingMarkerTmp',
                'Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-StrandedResult', 'Resolve-StartedMarkerFile', 'Get-JobRecoveryPending', 'Resolve-JobRecovery', 'Invoke-LiveMarkerRetire', 'Invoke-LiveMarkerReconcile')
            foreach ($name in $lift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            $job = 'resubmitted'
            $markerPath = Join-Path $c.running "$job.started.json"
            $resultPath = Join-Path $c.outbox "$job.result.json"
            Put-File $markerPath (New-MarkerJson $job)
            Put-File $resultPath (New-ResultJson $job)
            # The race, simulated at its first step after the live check (settling the tmps): the submitter deletes the result. The no-result branches are recorded, not run.
            function Resolve-JobMarkerTemps { param([string]$JobId) Remove-Item -LiteralPath $resultPath -Force; return $true }
            function Test-ProcessIdentityMatch { param($ProcessId, $ExpectedStartUtc, $ExpectedStartEpochMs, $ExpectedImagePath) return [pscustomobject]@{ matches = $false; reason = 'fixture'; startMatches = $false; imageMatches = $false; identity = [pscustomobject]@{ startUtc = $null; startEpochMs = $null; imagePath = $null } } }
            function Invoke-OrphanResult { param($Marker, [string]$MarkerPath, [string]$Reason) $script:Orphaned.Add([string]$Marker.jobId) }
            function Invoke-AdoptStartedJob { param($Marker, [string]$MarkerPath, $Match) $script:Orphaned.Add("adopt:$($Marker.jobId)") }
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            try { Invoke-LiveMarkerReconcile } finally { $ErrorActionPreference = 'Stop' }
            Assert-True ($script:Orphaned.Count -eq 0) "no orphan/adopt recovery ran for a job whose result was withdrawn: [$($script:Orphaned -join ',')]"
            Assert-True (-not $script:RecoveryQuarantine.ContainsKey($job)) "it is not quarantined (a retry would orphan it): $($script:RecoveryQuarantine[$job])"
            Assert-True (Test-Path -LiteralPath $markerPath) 'the marker is left as found'
            Assert-True (-not (Test-Path -LiteralPath $resultPath)) 'nothing republished a result'
            return 'left alone'
        }
    }

    # Every function a live-reconcile row needs. A name this agent does not define is skipped: the RED run lifts the same list from an older agent.
    $liveLift = @('Move-FileChecked', 'Write-JsonFileAtomic', 'Read-JsonFile', 'Complete-StartedMarker', 'Set-MarkerRetireQuarantine', 'Set-OwnPendingMarkerTmp', 'Clear-OwnPendingMarkerTmp',
        'Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-JobMarkerTemps', 'Resolve-StrandedResult', 'Resolve-StartedMarkerFile', 'Get-JobRecoveryPending', 'Resolve-JobRecovery', 'Invoke-LiveMarkerRetire', 'Invoke-LiveMarkerReconcile')

    # sol pass-3 A2: the result is deleted after the live check, with an old result tmp on disk. Promoting that tmp and retiring the marker against it hands the
    # resubmitting client the OLD RESULT. Both tmp forms; the deletion is simulated at the first step after the live check.
    if (-not (Skip-OnBase 'live-withdrawn-stale-result-tmp')) {
        Invoke-Row 'live-withdrawn-stale-result-tmp' {
            foreach ($form in @('0123456789abcdef0123456789abcdef.tmp', 'tmp')) {
                $c = New-Case "live-withdrawn-stale-result-tmp-$($form.Length)"
                $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox'); $script:processed = $c.processed; $script:orphaned = (Join-Path $c.root 'orphaned')
                $script:RecoveryQuarantine = @{}; $script:LiveRetireQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
                $script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                $script:Logged = [System.Collections.Generic.List[string]]::new()
                foreach ($name in $liveLift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
                function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
                $job = 'resubmitted'
                $markerPath = Join-Path $c.running "$job.started.json"
                $resultPath = Join-Path $c.outbox "$job.result.json"
                $staleTmp = Join-Path $c.outbox "$job.result.$form"
                Put-File $markerPath (New-MarkerJson $job)
                Put-File $resultPath (([ordered]@{ jobId = $job; exitCode = 0; stdout = 'CURRENT' }) | ConvertTo-Json -Compress)
                Put-File $staleTmp (([ordered]@{ jobId = $job; exitCode = 0; stdout = 'OLD RESULT' }) | ConvertTo-Json -Compress)
                function Resolve-JobMarkerTemps { param([string]$JobId) Remove-Item -LiteralPath $resultPath -Force; return $true }
                $ErrorActionPreference = 'Continue'   # the agent's own policy
                try { Invoke-LiveMarkerReconcile } finally { $ErrorActionPreference = 'Stop' }
                $got = if (Test-Path -LiteralPath $resultPath) { Get-Content -LiteralPath $resultPath -Raw } else { '' }
                Assert-True (-not (Test-Path -LiteralPath $resultPath)) "form ${form}: the old tmp was not promoted to the job's result: $got"
                Assert-True (Test-Path -LiteralPath $staleTmp) "form ${form}: the old tmp stays where it was"
                Assert-True (Test-Path -LiteralPath $markerPath) "form ${form}: the marker was not retired against the old tmp"
                Assert-True (@(Get-ChildItem -LiteralPath $c.processed).Count -eq 0) "form ${form}: nothing reached processed\"
                Assert-True (-not $script:RecoveryQuarantine.ContainsKey($job)) "form ${form}: nothing is left for the start-up retry"
            }
            return 'left alone, both forms'
        }
    }

    # The result goes after the live re-check, inside Complete-StartedMarker but before its own check (the first thing it does is read the marker). That check
    # quarantines for start-up recovery, which is right for a result that never landed and wrong here: the next poll's retry would orphan the id.
    if (-not (Skip-OnBase 'live-withdrawn-inside-complete')) {
        Invoke-Row 'live-withdrawn-inside-complete' {
            $c = New-Case 'live-withdrawn-inside-complete'
            $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox'); $script:processed = $c.processed; $script:orphaned = (Join-Path $c.root 'orphaned')
            $script:RecoveryQuarantine = @{}; $script:LiveRetireQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
            $script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            $script:Logged = [System.Collections.Generic.List[string]]::new(); $script:Orphaned = [System.Collections.Generic.List[string]]::new()
            foreach ($name in $liveLift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            function Invoke-OrphanResult { param($Marker, [string]$MarkerPath, [string]$Reason) $script:Orphaned.Add([string]$Marker.jobId) }
            function Test-ProcessIdentityMatch { param($ProcessId, $ExpectedStartUtc, $ExpectedStartEpochMs, $ExpectedImagePath) return [pscustomobject]@{ matches = $false; reason = 'fixture'; startMatches = $false; imageMatches = $false; identity = [pscustomobject]@{ startUtc = $null; startEpochMs = $null; imagePath = $null } } }
            $job = 'resubmitted'
            $markerPath = Join-Path $c.running "$job.started.json"
            $resultPath = Join-Path $c.outbox "$job.result.json"
            Put-File $markerPath (New-MarkerJson $job)
            Put-File $resultPath (New-ResultJson $job)
            function Read-JsonFile { param([string]$Path) if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath -Force }; return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json) }
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            try { Invoke-LiveMarkerReconcile } finally { $ErrorActionPreference = 'Stop' }
            Assert-True (-not $script:RecoveryQuarantine.ContainsKey($job)) "nothing is left for the start-up retry: $($script:RecoveryQuarantine[$job])"
            Assert-True ($script:Orphaned.Count -eq 0) 'no orphan recovery ran'
            Assert-True (Test-Path -LiteralPath $markerPath) 'the marker is left as found'
            Assert-True (-not (Test-Path -LiteralPath $resultPath)) 'nothing republished a result'
            return 'left alone'
        }
    }

    # A live-origin entry lasts only while a retire is still owed.
    if (-not (Skip-OnBase 'live-quarantine-lifecycle')) {
        Invoke-Row 'live-quarantine-lifecycle' {
            $c = New-Case 'live-quarantine-lifecycle'
            $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox'); $script:processed = $c.processed; $script:orphaned = (Join-Path $c.root 'orphaned')
            $script:RecoveryQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
            $script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            $script:Logged = [System.Collections.Generic.List[string]]::new(); $script:Recovered = [System.Collections.Generic.List[string]]::new()
            foreach ($name in $liveLift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            function Resolve-JobRecovery { param([string]$JobId) $script:Recovered.Add($JobId); return $true }
            $script:LiveRetireQuarantine = @{ 'marker-gone' = 'fixture'; 'result-gone' = 'fixture'; 'script-back' = 'fixture'; 'retry-ok' = 'fixture' }
            Put-File (Join-Path $c.running 'result-gone.started.json') (New-MarkerJson 'result-gone')
            Put-File (Join-Path $c.running 'script-back.started.json') (New-MarkerJson 'script-back')
            Put-File (Join-Path $c.outbox 'script-back.result.json') (New-ResultJson 'script-back')
            Put-File (Join-Path $script:inbox 'script-back.job.ps1') '"fresh"'
            Put-File (Join-Path $c.running 'retry-ok.started.json') (New-MarkerJson 'retry-ok')
            Put-File (Join-Path $c.outbox 'retry-ok.result.json') (New-ResultJson 'retry-ok')
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            try { Invoke-LiveMarkerReconcile; $linesAfterFirst = $script:Logged.Count; Invoke-LiveMarkerReconcile } finally { $ErrorActionPreference = 'Stop' }
            Assert-True ($script:LiveRetireQuarantine.Count -eq 0) "every entry is dropped: [$($script:LiveRetireQuarantine.Keys -join ',')]"
            Assert-True ($script:Recovered.Count -eq 0) "no start-up recovery ran: [$($script:Recovered -join ',')]"
            Assert-True (Test-Path -LiteralPath (Join-Path $c.running 'result-gone.started.json')) 'a marker whose result went stays'
            Assert-True (Test-Path -LiteralPath (Join-Path $c.running 'script-back.started.json')) 'a marker whose script came back stays: the inbox loop owns it'
            Assert-True (Test-Path -LiteralPath (Join-Path $c.processed 'retry-ok.started.completed.json')) 'a marker with its result retires'
            Assert-True (@($script:Logged | Where-Object { $_ -like '*job=retry-ok*released*' }).Count -eq 1) "the release is logged once: $($script:Logged -join ' | ')"
            Assert-True ($script:Logged.Count -eq $linesAfterFirst) "the second pass logs nothing new: $($script:Logged -join ' | ')"
            # A marker naming another job is held here, never filed or retired under that other id.
            Put-File (Join-Path $c.running 'file-id.started.json') (New-MarkerJson 'other-id')
            Put-File (Join-Path $c.outbox 'file-id.result.json') (New-ResultJson 'file-id')
            $ErrorActionPreference = 'Continue'
            try { Invoke-LiveMarkerReconcile } finally { $ErrorActionPreference = 'Stop' }
            Assert-True ($script:LiveRetireQuarantine.ContainsKey('file-id')) 'a marker naming another job is held for a retry'
            Assert-True ($script:RecoveryQuarantine.Count -eq 0) 'nothing reached the start-up quarantine'
            Assert-True (@(Get-ChildItem -LiteralPath $c.processed -Filter 'other-id.*').Count -eq 0) 'nothing was retired under the other id'
            return 'dropped when owed nothing'
        }
    }

    # The common cause of pass 3: a flag guards only the branches it was placed in front of. The live functions must not be able to reach a start-up branch at all.
    if (-not (Skip-OnBase 'live-path-no-startup-recovery')) {
        Invoke-Row 'live-path-no-startup-recovery' {
            $forbidden = @('Resolve-JobRecovery', 'Resolve-StartedMarkerFile', 'Resolve-StrandedResult', 'Invoke-OrphanResult', 'Invoke-AdoptStartedJob', 'Invoke-RequeueAdmissionWait', 'Resolve-LaunchingMarker', 'Invoke-QuarantineReconcile')
            foreach ($fn in @('Invoke-LiveMarkerReconcile', 'Invoke-LiveMarkerRetire')) {
                $def = Get-AgentFunction $fn
                Assert-True ($null -ne $def) "the agent defines $fn"
                $calls = @($def.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() } | Where-Object { $_ -in $forbidden })
                Assert-True ($calls.Count -eq 0) "$fn calls no start-up recovery: [$($calls -join ',')]"
            }
            $text = $ast.Extent.Text
            Assert-True ($text -notmatch 'LiveReconcileOnly|LiveResultVanished') 'the flags are gone'
            return 'none reachable'
        }
    }
}
finally {
    foreach ($p in $agents) { try { if (-not $p.HasExited) { $p.Kill($true); [void]$p.WaitForExit(10000) } } catch { } }
}

$pass = @($results.Values | Where-Object { $_.status -eq 'PASS' }).Count
$fail = @($results.Values | Where-Object { $_.status -eq 'FAIL' }).Count
$na = @($results.Values | Where-Object { $_.status -eq 'NA' }).Count
if ($Only.Count -eq 0 -and ($pass + $fail + $na) -ne $caseNames.Count) { $fail++; $results['row-count'] = [ordered]@{ status = 'FAIL'; error = "ran $($pass + $fail + $na) of $($caseNames.Count) rows" } }
[ordered]@{ status = $(if ($fail -eq 0) { 'PASS' } else { 'FAIL' }); pass = $pass; fail = $fail; na = $na; agent = $agentScript; base = $base; rows = $results } | ConvertTo-Json -Depth 6
if ($fail -gt 0) { exit 1 }
exit 0
