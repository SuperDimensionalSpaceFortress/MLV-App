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
#         retire-over-readonly-record  sol pass-4 A1: an older read-only processed\ record of the id does not keep a finished marker in running\ for good
#         identity-markers-kept      sol LM-01: j and k array markers, a mismatch, a number and an object are kept with nothing overwritten; a valid string retires beside them
#         identity-startup-kept      the same at start-up: no exit-127 receipt, no launch, no promotion, no replacement; a valid marker still retires or orphans
#         completion-tmp-denied-live  sol LM-02, native ACL deny: the completed record cannot be written, so the marker stays and nothing reads retired; the retry succeeds once it lifts
#         completion-tmp-denied-ordinary  the same by the ordinary completion route, with the job's script already gone from inbox\
#         completion-tmp-denied-startup  the same at start-up, by the completed and orphaned callers, which start-up recovery holds and retries
#   unit  the agent's real functions, lifted from its AST:
#         inbox-job-left-alone       a job whose script is in inbox\ is the live set: no recovery runs, even with marker + result
#         own-claim-retire-retried   this agent's own unlaunched claim whose retire failed stays quarantined, then clears
#         live-reconcile-result-withdrawn  a result deleted by a resubmitting client after the live check: no adopt/orphan recovery, no quarantine
#         completed-tmp-kept-on-failed-move  the marker moved but the completed tmp could not: the tmp is KEPT, the job quarantined, then the live retry completes it
#         live-withdrawn-stale-result-tmp  sol pass-3 A2: result deleted after the live check with an old tmp present (both tmp forms): not promoted, marker not retired
#         live-withdrawn-inside-complete  the result deleted inside Complete-StartedMarker, after the live re-check: nothing lands in the start-up quarantine
#         live-quarantine-lifecycle  a live-origin entry is dropped when the marker went, the result went, or a script came back; retried and released otherwise
#         live-path-no-direct-startup-call  the two live functions contain no direct call to a start-up recovery function, and the flags that used to guard it are gone
#         live-path-no-transitive-startup-call  the same over every helper they reach by a named call, followed through the agent's own functions
#         move-checked-readonly-destination  the helper every replacing rename shares: Overwrite replaces a read-only file; without Overwrite it still refuses
#         move-checked-failed-overwrite-contract  a failed overwrite leaves the destination's bytes intact and its ReadOnly bit cleared (missing source, reader-held target)
#         identity-reader-states     the reader's states over every artifact name form and each non-scalar field
#         identity-complete-direct   Complete-StartedMarker's own check, with no reader in front of it, for every status its callers pass
#         identity-reread-differs    the marker changes between the reader's read and Complete-StartedMarker's (the timing is a function wrap; the files are real)
#         completion-tmp-denied-retry  LM-02 on the real functions under a native ACL deny: the retry entry, one log line, state=kept, then a truthful completed record
#         requeue-retire-denied      r4 class sweep: a re-queue's denied retire is not logged as retired
# Prints one JSON object. Exit 0 = every row passed. Exit 1 = a row failed; the object says which (a RED run needs the rows, not the first throw).

$ErrorActionPreference = 'Stop'
# PowerShell variables are case-insensitive: assigning $agentScript below also assigns $AgentScript, so the override is captured first.
$agentScriptOverridden = [bool]$AgentScript
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
$caseNames = @('reader-denied-completion', 'incident-fixture', 'tmp-only-fixture', 'live-job-left-alone', 'no-result-left-alone', 'locked-marker-quarantined', 'inbox-job-left-alone', 'own-claim-retire-retried', 'completed-tmp-kept-on-failed-move', 'live-reconcile-result-withdrawn', 'live-locked-then-withdrawn', 'live-locked-stale-tmp-then-withdrawn', 'completion-denied-then-withdrawn', 'live-withdrawn-stale-result-tmp', 'live-withdrawn-inside-complete', 'live-quarantine-lifecycle', 'live-path-no-direct-startup-call', 'live-path-no-transitive-startup-call', 'retire-over-readonly-record', 'move-checked-readonly-destination', 'move-checked-failed-overwrite-contract', 'identity-markers-kept', 'identity-startup-kept', 'identity-reader-states', 'identity-complete-direct', 'identity-reread-differs', 'completion-tmp-denied-live', 'completion-tmp-denied-ordinary', 'completion-tmp-denied-startup', 'completion-tmp-denied-retry', 'requeue-retire-denied')
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
# A marker whose id is literal JSON, so a row can write what New-MarkerJson's typed parameter cannot.
function New-RawIdMarker([string]$IdJson) { return '{"schema":"mlvapp.agent.started.v1","jobId":' + $IdJson + ',"phase":"launched","status":"started","childPid":4,"childStartUtc":"2026-11-04T08:42:09.0000000Z","childStartEpochMs":1793695329000,"childImagePath":"C:\\fixture\\pwsh.exe","startedUtc":"2026-11-04T08:42:09.0000000Z","timeoutSec":60,"agentPid":4,"sourceInboxPath":"C:\\fixture\\absent.job.ps1","outFile":"C:\\fixture\\absent.out.txt","errFile":"C:\\fixture\\absent.err.txt"}' }
function Get-Bytes([string]$Path) { return (Get-FileHash -LiteralPath $Path).Hash }
# The native denial LM-02 needs: nothing new can be created in the directory, while a file already in it can still be moved out. Always paired with Remove-CreateFilesDeny in a finally.
function Add-CreateFilesDeny([string]$Dir) {
    $di = [IO.DirectoryInfo]::new($Dir)
    $acl = [IO.FileSystemAclExtensions]::GetAccessControl($di)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new([System.Security.Principal.WindowsIdentity]::GetCurrent().User, [System.Security.AccessControl.FileSystemRights]::CreateFiles, [System.Security.AccessControl.AccessControlType]::Deny)
    $acl.AddAccessRule($rule)
    [IO.FileSystemAclExtensions]::SetAccessControl($di, $acl)
    # A denial that did not take would make every row after it prove nothing, so it fails the row here.
    $probe = Join-Path $Dir 'deny-probe.txt'
    $took = $false
    try { [IO.File]::WriteAllText($probe, 'x') } catch [UnauthorizedAccessException] { $took = $true }
    if (-not $took) { [void]$acl.RemoveAccessRule($rule); [IO.FileSystemAclExtensions]::SetAccessControl($di, $acl); [IO.File]::Delete($probe); throw "FAIL: the create-files deny on $Dir did not take effect" }
    return $rule
}
function Remove-CreateFilesDeny([string]$Dir, $Rule) {
    if ($null -eq $Rule) { return }
    $di = [IO.DirectoryInfo]::new($Dir)
    $acl = [IO.FileSystemAclExtensions]::GetAccessControl($di)
    [void]$acl.RemoveAccessRule($Rule)
    [IO.FileSystemAclExtensions]::SetAccessControl($di, $acl)
}
function Set-UnitState($c) {
    $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox'); $script:processed = $c.processed; $script:orphaned = (Join-Path $c.root 'orphaned')
    $script:RecoveryQuarantine = @{}; $script:LiveRetireQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
    $script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $script:Logged = [System.Collections.Generic.List[string]]::new()
}
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
    # One poll, measured from eligibility (Dot I4). The agent logs the poll it retired on (R). The test reads the poll counter on both sides of publishing the result (g1, g2), so the result
    # became eligible at a poll E with g1 <= E <= g2, and a retire on the first eligible poll has R <= E + 1 <= g2 + 1 however long the publisher was paused. A sample with g1 = g2 is tight: a retire
    # a poll late gives R = g2 + 2. Only the instant between that poll's heartbeat and its live check gives such a retire R = g2 + 1, so three tight samples are required. Time the test spends
    # after the retire counts for nothing, because R is the agent's own number.
    Invoke-Row 'incident-fixture' {
        $c = New-Case 'incident-fixture'
        try {
            Start-Agent $c
            $samples = [System.Collections.Generic.List[object]]::new()
            $tight = 0
            # Sample 0 is Dot's case on purpose: the publisher pauses for three polls after its first read. A count from that read says four or more; the retire is still on the first eligible poll.
            for ($i = 0; $i -le 12 -and $tight -lt 3; $i++) {
                $job = "lease-batch-open-20261104T083925Z-lbfixture$i"
                $tmp = "$job.started.json.335c4d6c403943309f1e6486e293baab.tmp"
                Put-File (Join-Path $c.running "$job.started.json") (New-MarkerJson $job)
                Put-File (Join-Path $c.running $tmp) (New-MarkerJson $job 'launched' 'completed')
                $g1 = Get-HeartbeatGeneration $c
                if ($i -eq 0) { Wait-Polls $c 3 }
                Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
                $g2 = Get-HeartbeatGeneration $c
                $cleared = Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 8
                $left = Get-RunningCounted $c
                Assert-True $cleared "a Wait-VenueQuiet-style count of running\ reads 0 within 8 s at a 1 s poll: left [$($left -join ',')]"
                Assert-True (Test-Path -LiteralPath (Join-Path $c.processed "$job.started.completed.json")) 'the marker went to processed\ by the start-up path (status completed)'
                Assert-True (@(Get-ChildItem -LiteralPath $c.running -Filter "$tmp.stale-*").Count -eq 1) 'the tmp is preserved as a set-aside record, never deleted'
                # The agent logs after it retires, so the line can land a few ms after running\ empties: wait for it, then count.
                Assert-True (Wait-Until { @(Get-LogLines $c "live marker reconcile job=$job").Count -ge 1 } 8) 'the agent logs the reconcile'
                $lines = @(Get-LogLines $c "live marker reconcile job=$job")
                Assert-True ($lines.Count -eq 1) 'one agent log line for the job'
                Assert-True ((Get-Content -LiteralPath (Join-Path $c.outbox "$job.result.json") -Raw | ConvertFrom-Json).fixture -eq $true) 'the published result is untouched'
                $m = [regex]::Match($lines[0], 'generation=(\d+)')
                Assert-True $m.Success "the agent's reconcile line names the poll it retired on: $($lines[0])"
                $r = [int]$m.Groups[1].Value
                Assert-True ($r -ge $g1) "retired no earlier than the result was published: g1=$g1 g2=$g2 retired=$r"
                Assert-True ($r -le ($g2 + 1)) "retired on the first eligible poll: g1=$g1 g2=$g2 retired=$r"
                if ($i -eq 0) { Assert-True (($r - $g1) -gt 2) "the paused sample really paused across polls, or it would prove nothing: g1=$g1 g2=$g2 retired=$r" }
                elseif ($g1 -eq $g2) { $tight++ }
                $samples.Add([ordered]@{ g1 = $g1; g2 = $g2; retired = $r })
            }
            Assert-True ($tight -ge 3) "three samples kept the publisher inside one poll, so the measure measured something: only $tight of $($samples.Count)"
            return [ordered]@{ tightSamples = $tight; samples = @($samples); runningCounted = 0 }
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

    # sol pass-4 A1: the base retired with Move-Item -Force, which replaces a read-only destination. A bare replacing rename is denied by one, so a reused id's older
    # read-only record in processed\ would deny every retire and read GATE_BUSY for good.
    Invoke-Row 'retire-over-readonly-record' {
        $c = New-Case 'retire-over-readonly-record'
        try {
            Start-Agent $c
            $job = 'readonly-record'
            $record = Join-Path $c.processed "$job.started.completed.json"
            Put-File $record '{"jobId":"readonly-record","status":"completed","fixture":"older record"}'
            [IO.File]::SetAttributes($record, [IO.FileAttributes]::ReadOnly)
            Put-File (Join-Path $c.running "$job.started.json") (New-MarkerJson $job)
            Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
            Assert-True (Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 12) "running\ empties past the read-only record: left [$((Get-RunningCounted $c) -join ',')]; $((Get-LogLines $c "job=$job") -join ' | ')"
            $now = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
            Assert-True ($null -eq $now.fixture -and $now.status -eq 'completed') "processed\ holds this retire's record: $($now | ConvertTo-Json -Compress)"
            return 'retired'
        }
        finally { Stop-Agent $c }
    }

    # sol LM-01: a singleton-array id passed the reader and the live check, and both of these filed as processed\System.Object[].started.completed.json, the second replacing the first.
    # Each malformed marker is kept as found, one quarantine line per job. The valid marker retiring beside them proves the agent looked at this poll and that strings still retire.
    # NOT COVERED by this binding, on purpose: a marker with the right id and false contents. That rests on the ownership model (one agent under its mutex; clients do not write running markers).
    Invoke-Row 'identity-markers-kept' {
        $c = New-Case 'identity-markers-kept'
        try {
            Start-Agent $c
            $bad = @(@('j', '["j"]'), @('k', '["k"]'), @('m', '"n"'), @('5', '5'), @('o', '{"a":1}'))
            $bytes = @{}
            foreach ($b in $bad) {
                $path = Join-Path $c.running "$($b[0]).started.json"
                Put-File $path (New-RawIdMarker $b[1])
                $bytes[$b[0]] = Get-Bytes $path
                Put-File (Join-Path $c.outbox "$($b[0]).result.json") (New-ResultJson $b[0])
            }
            Put-File (Join-Path $c.running 'v.started.json') (New-MarkerJson 'v')
            Put-File (Join-Path $c.outbox 'v.result.json') (New-ResultJson 'v')
            Assert-True (Wait-Until { Test-Path -LiteralPath (Join-Path $c.processed 'v.started.completed.json') } 12) "the valid marker retires as normal: $((Get-LogLines $c 'job=v') -join ' | ')"
            Wait-Polls $c 3
            foreach ($b in $bad) {
                $path = Join-Path $c.running "$($b[0]).started.json"
                Assert-True (Test-Path -LiteralPath $path) "$($b[0]): the malformed marker stays in running\"
                Assert-True ((Get-Bytes $path) -eq $bytes[$b[0]]) "$($b[0]): it is byte-identical"
                $q = @(Get-LogLines $c "QUARANTINED job=$($b[0]) ")
                Assert-True ($q.Count -eq 1) "$($b[0]): one quarantine line, not one per poll: $($q.Count)"
            }
            $filed = @(Get-ChildItem -LiteralPath $c.processed | ForEach-Object Name)
            Assert-True (($filed -join ',') -eq 'v.started.completed.json') "only the valid marker reached processed\: [$($filed -join ',')]"
            Assert-True ((Get-RunningCounted $c).Count -eq $bad.Count) "the accepted cost: a kept malformed marker still counts as busy for Wait-VenueQuiet: [$((Get-RunningCounted $c) -join ',')]"
            return 'kept, with the valid marker retired'
        }
        finally { Stop-Agent $c }
    }

    # A stricter reader changes what start-up recovery sees. Each row here is one shape an agent can find when it starts.
    Invoke-Row 'identity-startup-kept' {
        $c = New-Case 'identity-startup-kept'
        try {
            $inboxDir = Join-Path $c.root 'inbox'
            $hex = '0123456789abcdef0123456789abcdef'
            # array marker + result: kept, not filed under a coerced name
            Put-File (Join-Path $c.running 'ja.started.json') (New-RawIdMarker '["ja"]')
            Put-File (Join-Path $c.outbox 'ja.result.json') (New-ResultJson 'ja')
            # array marker + fresh script, no result: no exit-127 receipt and no launch
            Put-File (Join-Path $c.running 'jb.started.json') (New-RawIdMarker '["jb"]')
            Put-File (Join-Path $inboxDir 'jb.job.ps1') '"ran"'
            # array tmp alone + result: not promoted into a final marker
            Put-File (Join-Path $c.running "jc.started.json.$hex.tmp") (New-RawIdMarker '["jc"]')
            Put-File (Join-Path $c.outbox 'jc.result.json') (New-ResultJson 'jc')
            # array final marker + a good child tmp + result: the tmp does not replace the marker
            Put-File (Join-Path $c.running 'jd.started.json') (New-RawIdMarker '["jd"]')
            Put-File (Join-Path $c.running "jd.started.json.$hex.tmp") (New-MarkerJson 'jd')
            Put-File (Join-Path $c.outbox 'jd.result.json') (New-ResultJson 'jd')
            # valid marker, no result, an array result tmp: that tmp is not this job's result, so it stays and the ordinary orphan rule runs
            Put-File (Join-Path $c.running 'je.started.json') (New-MarkerJson 'je')
            Put-File (Join-Path $c.outbox "je.result.$hex.tmp") '{"jobId":["je"],"exitCode":0,"stdout":"OLD RESULT"}'
            # valid marker + result: retires at start-up as before
            Put-File (Join-Path $c.running 'jf.started.json') (New-MarkerJson 'jf')
            Put-File (Join-Path $c.outbox 'jf.result.json') (New-ResultJson 'jf')
            $kept = @('ja.started.json', 'jb.started.json', "jc.started.json.$hex.tmp", 'jd.started.json', "jd.started.json.$hex.tmp")
            $bytes = @{}; foreach ($n in $kept) { $bytes[$n] = Get-Bytes (Join-Path $c.running $n) }
            Start-Agent $c
            Wait-Polls $c 3
            foreach ($n in $kept) {
                Assert-True (Test-Path -LiteralPath (Join-Path $c.running $n)) "$n stays in running\"
                Assert-True ((Get-Bytes (Join-Path $c.running $n)) -eq $bytes[$n]) "$n is byte-identical"
            }
            foreach ($id in @('ja', 'jb', 'jc', 'jd')) {
                $q = @(Get-LogLines $c "QUARANTINED job=$id ")
                Assert-True ($q.Count -eq 1) "${id}: one quarantine line: $($q.Count)"
                Assert-True (@(Get-ChildItem -LiteralPath $c.processed -Filter "$id.*").Count -eq 0) "${id}: nothing reached processed\"
            }
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $c.outbox 'jb.result.json'))) "jb: no receipt was published: $((Get-LogLines $c 'job=jb') -join ' | ')"
            Assert-True (Test-Path -LiteralPath (Join-Path $inboxDir 'jb.job.ps1')) 'jb: its script stays in inbox\'
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $c.root 'logs\jb.out.txt'))) 'jb: its script was not launched'
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $c.running 'jc.started.json'))) 'jc: the array tmp was not promoted'
            Assert-True ((Get-Content -LiteralPath (Join-Path $c.outbox 'ja.result.json') -Raw | ConvertFrom-Json).fixture -eq $true) 'ja: its result is untouched'
            $je = Get-Content -LiteralPath (Join-Path $c.outbox 'je.result.json') -Raw | ConvertFrom-Json
            Assert-True ($je.exitCode -eq 127 -and $je.stdout -notmatch 'OLD RESULT') "je: the array result tmp was not promoted, and the orphan rule ran: $($je | ConvertTo-Json -Compress -Depth 2)"
            Assert-True (Test-Path -LiteralPath (Join-Path $c.outbox "je.result.$hex.tmp")) 'je: that tmp stays where it was'
            Assert-True (Test-Path -LiteralPath (Join-Path $c.processed 'je.started.orphaned.json')) 'je: a valid marker still retires as orphaned'
            Assert-True (Test-Path -LiteralPath (Join-Path $c.processed 'jf.started.completed.json')) 'jf: a valid marker still retires at start-up'
            return 'kept; valid markers unaffected'
        }
        finally { Stop-Agent $c }
    }

    # sol LM-02 by a native denial: Set-Content cannot create the completion tmp in running\, and the marker could still be moved out. Moving it filed status=started as the completed record.
    Invoke-Row 'completion-tmp-denied-live' {
        $c = New-Case 'completion-tmp-denied-live'
        $rule = $null
        try {
            Start-Agent $c
            $job = 'tmp-denied-live'
            $markerPath = Join-Path $c.running "$job.started.json"
            $recordPath = Join-Path $c.processed "$job.started.completed.json"
            $resultPath = Join-Path $c.outbox "$job.result.json"
            Put-File $markerPath (New-MarkerJson $job)
            $before = Get-Bytes $markerPath
            $rule = Add-CreateFilesDeny $c.running
            Put-File $resultPath (New-ResultJson $job)
            Assert-True (Wait-Until { @(Get-LogLines $c "marker kept job=$job").Count -ge 1 -or (Test-Path -LiteralPath $recordPath) } 12) 'the agent reached the denied write'
            Wait-Polls $c 3
            Assert-True (Test-Path -LiteralPath $markerPath) "the marker stays in running\: $((Get-LogLines $c "job=$job") -join ' | ')"
            Assert-True ((Get-Bytes $markerPath) -eq $before) 'it is byte-identical'
            Assert-True (-not (Test-Path -LiteralPath $recordPath)) 'no record was filed'
            $kept = @(Get-LogLines $c "marker kept job=$job")
            Assert-True ($kept.Count -eq 1 -and $kept[0] -match 'could not be written') "the failure is logged once, with its reason: $($kept -join ' | ')"
            Assert-True (@(Get-LogLines $c 'marker artifacts retired').Count -eq 0) 'nothing reads retired'
            Remove-CreateFilesDeny $c.running $rule; $rule = $null
            Assert-True (Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 12) "the retry retires it once the denial lifts: left [$((Get-RunningCounted $c) -join ',')]"
            $rec = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
            Assert-True ($rec.status -eq 'completed' -and $rec.resultPath -eq $resultPath -and $null -ne $rec.completedUtc) "a truthful completed record: $($rec | ConvertTo-Json -Compress)"
            return 'kept, then completed'
        }
        finally { Remove-CreateFilesDeny $c.running $rule; Stop-Agent $c }
    }

    # The same denial at the route that matters most: the job just finished, and its script leaves inbox\ right after the failed retire, so only the live retry is left to clear the marker.
    Invoke-Row 'completion-tmp-denied-ordinary' {
        $c = New-Case 'completion-tmp-denied-ordinary'
        $rule = $null
        try {
            Start-Agent $c
            $job = 'venue-quiet-probe-denied'
            Submit-Job $c $job 'Start-Sleep -Seconds 3; "ran"'
            Assert-True (Wait-Launched $c $job 30) 'the job launched'
            $markerPath = Join-Path $c.running "$job.started.json"
            $recordPath = Join-Path $c.processed "$job.started.completed.json"
            $resultPath = Join-Path $c.outbox "$job.result.json"
            $rule = Add-CreateFilesDeny $c.running
            Assert-True (Wait-Until { Test-Path -LiteralPath (Join-Path $c.processed "$job.job.ps1") } 30) 'the job finished and its script reached processed\'
            Wait-Polls $c 2
            Assert-True (Test-Path -LiteralPath $markerPath) "the marker stays in running\: $((Get-LogLines $c "job=$job") -join ' | ')"
            Assert-True (-not (Test-Path -LiteralPath $recordPath)) 'no record was filed'
            $kept = @(Get-LogLines $c "marker kept job=$job")
            Assert-True ($kept.Count -eq 1 -and $kept[0] -match 'could not be written') "the failure is logged once, with its reason: $($kept -join ' | ')"
            Assert-True ((Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json).exitCode -eq 0) "the job's receipt is published"
            Remove-CreateFilesDeny $c.running $rule; $rule = $null
            Assert-True (Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 12) "the retry retires it once the denial lifts: left [$((Get-RunningCounted $c) -join ',')]"
            $rec = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
            Assert-True ($rec.status -eq 'completed' -and $rec.resultPath -eq $resultPath -and $null -ne $rec.completedUtc -and $null -ne $rec.childPid) "a truthful completed record: $($rec | ConvertTo-Json -Compress)"
            return 'kept, then completed'
        }
        finally { Remove-CreateFilesDeny $c.running $rule; Stop-Agent $c }
    }

    # The start-up callers under the same denial: a marker with its result (completed) and one with none (orphaned, which publishes exit 127 first). Neither files a record, start-up recovery holds
    # both, and its retry retires both once the denial lifts. The orphan's retry reads its published result and files as completed, the name BACHELOR-AGENT-RETIRE-RETRY-KEEPS-ORIGINAL-STATUS-1 carries.
    Invoke-Row 'completion-tmp-denied-startup' {
        $c = New-Case 'completion-tmp-denied-startup'
        $rule = $null
        try {
            Put-File (Join-Path $c.running 'done.started.json') (New-MarkerJson 'done')
            Put-File (Join-Path $c.outbox 'done.result.json') (New-ResultJson 'done')
            Put-File (Join-Path $c.running 'orph.started.json') (New-MarkerJson 'orph')
            $rule = Add-CreateFilesDeny $c.running
            Start-Agent $c
            Wait-Polls $c 3
            foreach ($id in @('done', 'orph')) {
                Assert-True (Test-Path -LiteralPath (Join-Path $c.running "$id.started.json")) "${id}: the marker stays in running\: $((Get-LogLines $c "job=$id") -join ' | ')"
                Assert-True (@(Get-ChildItem -LiteralPath $c.processed -Filter "$id.*").Count -eq 0) "${id}: no record was filed"
                Assert-True (@(Get-LogLines $c "marker kept job=$id").Count -eq 1) "${id}: logged once: $((Get-LogLines $c "marker kept job=$id") -join ' | ')"
                Assert-True (@(Get-LogLines $c "QUARANTINED job=$id ").Count -eq 1) "${id}: start-up recovery holds it: $((Get-LogLines $c "QUARANTINED job=$id ") -join ' | ')"
            }
            $orphanReceipt = Join-Path $c.outbox 'orph.result.json'
            Assert-True ((Get-Content -LiteralPath $orphanReceipt -Raw | ConvertFrom-Json).exitCode -eq 127) 'orph: the orphan receipt is published before the failed retire'
            $receiptBytes = Get-Bytes $orphanReceipt
            Remove-CreateFilesDeny $c.running $rule; $rule = $null
            Assert-True (Wait-Until { (Get-RunningCounted $c).Count -eq 0 } 12) "the retry retires both once the denial lifts: left [$((Get-RunningCounted $c) -join ',')]"
            foreach ($id in @('done', 'orph')) {
                $rec = Get-Content -LiteralPath (Join-Path $c.processed "$id.started.completed.json") -Raw | ConvertFrom-Json
                Assert-True ($rec.status -eq 'completed' -and $rec.resultPath -eq (Join-Path $c.outbox "$id.result.json") -and $null -ne $rec.completedUtc) "${id}: a truthful completed record: $($rec | ConvertTo-Json -Compress)"
            }
            Assert-True ((Get-Bytes $orphanReceipt) -eq $receiptBytes) 'orph: the retry did not publish a second receipt'
            return 'kept, then completed'
        }
        finally { Remove-CreateFilesDeny $c.running $rule; Stop-Agent $c }
    }

    # ------------------------------------------------------------ unit: the agent's real functions, lifted from its AST
    $tokens = $null; $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
    Assert-True ($parseErrors.Count -eq 0) "the agent parses ($($parseErrors.Count) errors)"
    function Get-AgentFunction([string]$Name) { return $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true) }
    $hasFix = $null -ne (Get-AgentFunction 'Invoke-LiveMarkerReconcile')
    # r4's identity helpers: absent from an older agent, so every lift below skips a name that agent does not define (the RED run lifts the same lists).
    $identityLift = @('Get-MarkerArtifactFileId', 'Get-MarkerIdentityFault')
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
                    'Get-MarkerTempFiles', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-JobMarkerTemps', 'Resolve-StrandedResult', 'Resolve-StartedMarkerFile', 'Get-JobRecoveryPending', 'Resolve-JobRecovery') + $identityLift) {
                $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) }
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
                'Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-JobMarkerTemps', 'Invoke-LiveMarkerRetire', 'Invoke-LiveMarkerReconcile') + $identityLift
            foreach ($name in $lift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
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
                'Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-StrandedResult', 'Resolve-StartedMarkerFile', 'Get-JobRecoveryPending', 'Resolve-JobRecovery', 'Invoke-LiveMarkerRetire', 'Invoke-LiveMarkerReconcile') + $identityLift
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

    # The class behind pass-4 A1: every replacing rename goes through this helper (marker write, result publish, marker retire, tmp promotion).
    if (-not (Skip-OnBase 'move-checked-readonly-destination')) {
        Invoke-Row 'move-checked-readonly-destination' {
            $c = New-Case 'move-checked-readonly-destination'
            . ([scriptblock]::Create((Get-AgentFunction 'Move-FileChecked').Extent.Text))
            $src = Join-Path $c.root 'src.json'; $dst = Join-Path $c.root 'dst.json'
            Put-File $src 'NEW'; Put-File $dst 'OLD'
            [IO.File]::SetAttributes($dst, [IO.FileAttributes]::ReadOnly)
            $refused = Move-FileChecked -Source $src -Destination $dst -Overwrite $false -Attempts 1
            Assert-True (-not $refused.ok) 'without Overwrite an existing file is refused'
            Assert-True ([IO.File]::ReadAllText($dst) -eq 'OLD' -and [IO.File]::GetAttributes($dst).HasFlag([IO.FileAttributes]::ReadOnly)) 'and it is left exactly as found'
            $replaced = Move-FileChecked -Source $src -Destination $dst -Overwrite $true -Attempts 1
            Assert-True $replaced.ok "Overwrite replaces a read-only destination: $($replaced.error)"
            Assert-True ([IO.File]::ReadAllText($dst) -eq 'NEW' -and -not (Test-Path -LiteralPath $src)) 'the new file is in place and the source is gone'
            $missing = Move-FileChecked -Source (Join-Path $c.root 'absent.json') -Destination (Join-Path $c.root 'absent2.json') -Overwrite $true -Attempts 1
            Assert-True (-not $missing.ok) 'a failed rename is still reported as failed'
            return 'replaced'
        }
    }

    # Dot I2: the failure contract of that bit-clear, which is deliberate. The destination stays until a rename lands, so a failed overwrite costs only the attribute; it is not restored.
    if (-not (Skip-OnBase 'move-checked-failed-overwrite-contract')) {
        Invoke-Row 'move-checked-failed-overwrite-contract' {
            $c = New-Case 'move-checked-failed-overwrite-contract'
            . ([scriptblock]::Create((Get-AgentFunction 'Move-FileChecked').Extent.Text))
            $dst = Join-Path $c.root 'dst.json'
            Put-File $dst 'OLD'
            [IO.File]::SetAttributes($dst, [IO.FileAttributes]::ReadOnly)
            $missing = Move-FileChecked -Source (Join-Path $c.root 'absent.json') -Destination $dst -Overwrite $true -Attempts 1
            Assert-True (-not $missing.ok) 'a missing source is reported as failed'
            Assert-True ([IO.File]::ReadAllText($dst) -eq 'OLD') 'missing source: the destination bytes are intact'
            Assert-True (-not [IO.File]::GetAttributes($dst).HasFlag([IO.FileAttributes]::ReadOnly)) 'missing source: the ReadOnly bit stays cleared'
            [IO.File]::SetAttributes($dst, [IO.FileAttributes]::ReadOnly)
            $src = Join-Path $c.root 'src.json'
            Put-File $src 'NEW'
            $reader = [IO.File]::Open($dst, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            try { $held = Move-FileChecked -Source $src -Destination $dst -Overwrite $true -Attempts 1 } finally { $reader.Dispose() }
            Assert-True (-not $held.ok) 'a reader-held destination is reported as failed'
            Assert-True ([IO.File]::ReadAllText($dst) -eq 'OLD' -and [IO.File]::ReadAllText($src) -eq 'NEW') 'reader-held: both files are intact'
            Assert-True (-not [IO.File]::GetAttributes($dst).HasFlag([IO.FileAttributes]::ReadOnly)) 'reader-held: the ReadOnly bit stays cleared'
            return 'bytes intact, bit cleared'
        }
    }

    # Every function a live-reconcile row needs. A name this agent does not define is skipped: the RED run lifts the same list from an older agent.
    $liveLift = @('Move-FileChecked', 'Write-JsonFileAtomic', 'Read-JsonFile', 'Complete-StartedMarker', 'Set-MarkerRetireQuarantine', 'Set-OwnPendingMarkerTmp', 'Clear-OwnPendingMarkerTmp',
        'Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Read-MarkerArtifact', 'Move-MarkerTmpAside', 'Resolve-JobMarkerTemps', 'Resolve-StrandedResult', 'Resolve-StartedMarkerFile', 'Get-JobRecoveryPending', 'Resolve-JobRecovery', 'Invoke-LiveMarkerRetire', 'Invoke-LiveMarkerReconcile') + $identityLift

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

    # The reader's states, by artifact name form and by field.
    if (-not (Skip-OnBase 'identity-reader-states')) {
        Invoke-Row 'identity-reader-states' {
            $c = New-Case 'identity-reader-states'
            foreach ($name in $liveLift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
            $hex = '0123456789abcdef0123456789abcdef'
            $table = @(
                @('j.started.json', '{"jobId":"j"}', 'ok'), @('abc.started.json', '{"jobId":"ABC"}', 'ok'),
                @("j.started.json.$hex.tmp", '{"jobId":"j"}', 'ok'), @('j.started.json.tmp', '{"jobId":"j"}', 'ok'), @("j.result.$hex.tmp", '{"jobId":"j"}', 'ok'), @('j.result.tmp', '{"jobId":"j"}', 'ok'),
                @('j.started.json', '{"jobId":"j","childPid":null,"phase":"launched","writeId":"w","childStartUtc":"2026-11-04T08:42:09.0000000Z","childStartEpochMs":1793695329000,"admission":{"label":"clear"}}', 'ok'),
                @('j.started.json', '{"jobId":["j"]}', 'malformed'), @("j.started.json.$hex.tmp", '{"jobId":["j"]}', 'malformed'), @('j.started.json.tmp', '{"jobId":["j"]}', 'malformed'), @("j.result.$hex.tmp", '{"jobId":["j"]}', 'malformed'), @('j.result.tmp', '{"jobId":["j"]}', 'malformed'),
                @('j.started.json', '{"jobId":"k"}', 'malformed'), @("j.started.json.$hex.tmp", '{"jobId":"k"}', 'malformed'), @("j.result.$hex.tmp", '{"jobId":"k"}', 'malformed'),
                @('5.started.json', '{"jobId":5}', 'malformed'), @('true.started.json', '{"jobId":true}', 'malformed'), @('j.started.json', '{"jobId":{"id":"j"}}', 'malformed'), @('j.started.json', '{"jobId":" j"}', 'malformed'), @('j.started.json', '[{"jobId":"j"},{"jobId":"j"}]', 'malformed'),
                @('j.json', '{"jobId":"j"}', 'malformed'),
                @('j.started.json', '', 'corrupt'), @('j.started.json', '{"jobId"', 'corrupt'), @('j.started.json', '{}', 'corrupt'), @('j.started.json', '{"jobId":[]}', 'corrupt'), @('j.started.json', '{"jobId":""}', 'corrupt'), @('j.started.json', '"j"', 'corrupt')
            )
            foreach ($field in @('phase', 'writeId', 'childPid', 'childStartUtc', 'childStartEpochMs', 'childImagePath', 'launchImagePath', 'launchScriptPath', 'sourceInboxPath', 'outFile', 'errFile')) {
                $table += , @('j.started.json', ('{"jobId":"j","' + $field + '":["x"]}'), 'malformed')
                $table += , @('j.started.json', ('{"jobId":"j","' + $field + '":{"a":1}}'), 'malformed')
            }
            $i = 0
            foreach ($row in $table) {
                $i++
                $dir = Join-Path $c.root "r$i"
                New-Item -ItemType Directory -Force -Path $dir | Out-Null
                $path = Join-Path $dir $row[0]
                [IO.File]::WriteAllText($path, $row[1])
                $a = Read-MarkerArtifact -Path $path
                Assert-True ($a.state -eq $row[2]) "$($row[0]) holding $($row[1]) reads $($row[2]): read $($a.state) ($($a.why))"
                Assert-True (($a.state -eq 'ok') -eq ($null -ne $a.marker)) "$($row[0]) holding $($row[1]): only ok hands back a marker"
            }
            Assert-True ((Read-MarkerArtifact -Path (Join-Path $c.root 'absent.started.json')).state -eq 'missing') 'a missing file reads missing'
            return "$($table.Count) rows"
        }
    }

    # Complete-StartedMarker's own check, reached with no reader in front of it, for every status its callers pass.
    if (-not (Skip-OnBase 'identity-complete-direct')) {
        Invoke-Row 'identity-complete-direct' {
            $c = New-Case 'identity-complete-direct'
            Set-UnitState $c
            foreach ($name in $liveLift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            $bad = @(@('j', (New-RawIdMarker '["j"]')), @('k', (New-RawIdMarker '["k"]')), @('m', (New-RawIdMarker '"n"')), @('5', (New-RawIdMarker '5')), @('o', (New-RawIdMarker '{"a":1}')), @('none', '{"phase":"launched","childPid":4}'))
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            try {
                foreach ($b in $bad) {
                    $path = Join-Path $c.running "$($b[0]).started.json"
                    $res = Join-Path $c.outbox "$($b[0]).result.json"
                    Put-File $path $b[1]
                    Put-File $res (New-ResultJson $b[0])
                    $before = Get-Bytes $path
                    foreach ($status in @('completed', 'orphaned', 'adopted', 'queue-expired', 'cancelled-in-gate', 'expired-not-after')) {
                        Complete-StartedMarker -MarkerPath $path -Status $status -ResultPath $res
                        Assert-True ((Test-Path -LiteralPath $path) -and (Get-Bytes $path) -eq $before) "$($b[0]) status=${status}: the marker stays, byte-identical"
                    }
                    Assert-True ($script:LiveRetireQuarantine.ContainsKey($b[0])) "$($b[0]): the retry entry is under the filename's id"
                    $lines = @($script:Logged | Where-Object { $_ -like "marker kept job=$($b[0]):*" })
                    Assert-True ($lines.Count -eq 1) "$($b[0]): logged once: $($script:Logged -join ' | ')"
                }
                Assert-True (@(Get-ChildItem -LiteralPath $c.processed).Count -eq 0) "nothing reached processed\: [$((Get-ChildItem -LiteralPath $c.processed).Name -join ',')]"
                Assert-True (@(Get-ChildItem -LiteralPath $c.running | Where-Object { $_.Name -match '\.tmp$' }).Count -eq 0) 'no tmp was left behind'
                # The marker's own id quarantined nothing: a marker in x's file naming y, with its result missing, files no entry for y.
                $path = Join-Path $c.running 'x.started.json'
                Put-File $path (New-RawIdMarker '"y"')
                Complete-StartedMarker -MarkerPath $path -Status 'completed' -ResultPath (Join-Path $c.outbox 'x.result.json')
                Assert-True ($script:RecoveryQuarantine.Count -eq 0) "no start-up quarantine under a marker's id: [$($script:RecoveryQuarantine.Keys -join ',')]"
                Assert-True (-not $script:LiveRetireQuarantine.ContainsKey('y')) 'no retry entry under the id the marker names'
                # Case only: NTFS has one file for both spellings, so this is the same job, and its record is named from the file.
                $path = Join-Path $c.running 'abc.started.json'
                $res = Join-Path $c.outbox 'abc.result.json'
                Put-File $path (New-RawIdMarker '"ABC"')
                Put-File $res (New-ResultJson 'abc')
                Complete-StartedMarker -MarkerPath $path -Status 'completed' -ResultPath $res
                $filed = @(Get-ChildItem -LiteralPath $c.processed | ForEach-Object Name)
                Assert-True (($filed -join ',') -ceq 'abc.started.completed.json') "the record is named from the file's id: [$($filed -join ',')]"
            }
            finally { $ErrorActionPreference = 'Stop' }
            return 'kept; valid retired'
        }
    }

    # The marker changes between the reader's read and Complete-StartedMarker's. The timing is a seam (a wrap on Read-JsonFile, which writes the new bytes and then reads them); the files are real.
    if (-not (Skip-OnBase 'identity-reread-differs')) {
        Invoke-Row 'identity-reread-differs' {
            foreach ($swap in @(@('array', '["swap-j"]'), @('other', '"swap-other"'))) {
                $c = New-Case "identity-reread-differs-$($swap[0])"
                Set-UnitState $c
                foreach ($name in $liveLift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
                function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
                $job = 'swap-j'
                $path = Join-Path $c.running "$job.started.json"
                Put-File $path (New-MarkerJson $job)
                Put-File (Join-Path $c.outbox "$job.result.json") (New-ResultJson $job)
                $newBytes = New-RawIdMarker $swap[1]
                function Read-JsonFile { param([string]$Path) [IO.File]::WriteAllText($Path, $newBytes); return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json) }
                $ErrorActionPreference = 'Continue'   # the agent's own policy
                try { $r = Invoke-LiveMarkerRetire -JobId $job } finally { $ErrorActionPreference = 'Stop' }
                Assert-True ($r.state -eq 'kept') "$($swap[0]): the live route does not report retired: $($r.state)"
                Assert-True ([IO.File]::ReadAllText($path) -eq $newBytes) "$($swap[0]): the marker stays as found"
                Assert-True (@(Get-ChildItem -LiteralPath $c.processed).Count -eq 0) "$($swap[0]): nothing reached processed\: [$((Get-ChildItem -LiteralPath $c.processed).Name -join ',')]"
                Assert-True ($script:LiveRetireQuarantine.ContainsKey($job)) "$($swap[0]): the retry entry is under the filename's id"
                Assert-True ($script:RecoveryQuarantine.Count -eq 0) "$($swap[0]): nothing reached the start-up quarantine"
            }
            return 'kept, both swaps'
        }
    }

    # LM-02 on the real functions, where the retry entry can be read. Native denial, real files.
    if (-not (Skip-OnBase 'completion-tmp-denied-retry')) {
        Invoke-Row 'completion-tmp-denied-retry' {
            $c = New-Case 'completion-tmp-denied-retry'
            Set-UnitState $c
            foreach ($name in $liveLift) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            $job = 'tmp-denied'
            $path = Join-Path $c.running "$job.started.json"
            $res = Join-Path $c.outbox "$job.result.json"
            $record = Join-Path $c.processed "$job.started.completed.json"
            Put-File $path (New-MarkerJson $job)
            Put-File $res (New-ResultJson $job)
            $before = Get-Bytes $path
            $rule = Add-CreateFilesDeny $c.running
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            try {
                $r = Invoke-LiveMarkerRetire -JobId $job
                Assert-True ($r.state -eq 'kept') "the live route does not report retired: $($r.state)"
                Assert-True ((Test-Path -LiteralPath $path) -and (Get-Bytes $path) -eq $before) 'the marker stays, byte-identical'
                Assert-True (-not (Test-Path -LiteralPath $record)) 'no record was filed'
                Assert-True ($script:LiveRetireQuarantine.ContainsKey($job) -and $script:LiveRetireQuarantine[$job] -match 'could not be written') "the retry entry is kept, with the reason: $($script:LiveRetireQuarantine[$job])"
                Assert-True (-not $script:RecoveryQuarantine.ContainsKey($job)) 'nothing reached the start-up quarantine'
                $lines = $script:Logged.Count
                Assert-True ($lines -eq 1 -and $script:Logged[0] -like "marker kept job=${job}:*") "one log line: $($script:Logged -join ' | ')"
                Invoke-LiveMarkerReconcile
                Invoke-LiveMarkerReconcile
                Assert-True ($script:Logged.Count -eq $lines) "a retry that fails again logs nothing new: $($script:Logged -join ' | ')"
                Assert-True ($script:LiveRetireQuarantine.ContainsKey($job)) 'the entry stays while the retire is owed'
                Remove-CreateFilesDeny $c.running $rule; $rule = $null
                Invoke-LiveMarkerReconcile
            }
            finally { $ErrorActionPreference = 'Stop'; Remove-CreateFilesDeny $c.running $rule }
            Assert-True (-not (Test-Path -LiteralPath $path)) "the retry retires it once the denial lifts: $($script:Logged -join ' | ')"
            Assert-True (-not $script:LiveRetireQuarantine.ContainsKey($job)) 'the entry is released'
            $rec = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
            Assert-True ($rec.status -eq 'completed' -and $rec.resultPath -eq $res -and $null -ne $rec.completedUtc) "a truthful completed record: $($rec | ConvertTo-Json -Compress)"
            Assert-True (@($script:Logged | Where-Object { $_ -like "live retire reconciled job=$job*" }).Count -eq 1) "the release is logged once: $($script:Logged -join ' | ')"
            return 'kept, retried, completed'
        }
    }

    # r4 class sweep ("a failed write is followed by acting as if it landed"): this was an unchecked Move-Item, and its next line logged the claim as retired.
    if (-not (Skip-OnBase 'requeue-retire-denied')) {
        Invoke-Row 'requeue-retire-denied' {
            $c = New-Case 'requeue-retire-denied'
            Set-UnitState $c
            foreach ($name in ($liveLift + 'Invoke-RequeueAdmissionWait')) { $f = Get-AgentFunction $name; if ($null -ne $f) { . ([scriptblock]::Create($f.Extent.Text)) } }
            function Add-AgentLogLine { param([string]$Message) $script:Logged.Add($Message) }
            $job = 'requeue-denied'
            $path = Join-Path $c.running "$job.started.json"
            Put-File $path ([ordered]@{ jobId = $job; phase = 'admission-wait'; status = 'admission-wait'; childPid = $null } | ConvertTo-Json -Compress)
            $marker = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $ErrorActionPreference = 'Continue'   # the agent's own policy
            try {
                $reader = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
                try { Invoke-RequeueAdmissionWait -Marker $marker -MarkerPath $path 2>$null } finally { $reader.Dispose() }
                Assert-True (Test-Path -LiteralPath $path) 'the marker is still there'
                Assert-True (@($script:Logged | Where-Object { $_ -like '*claim retired*' }).Count -eq 0) "a denied move is not logged as retired: $($script:Logged -join ' | ')"
                Assert-True (@($script:Logged | Where-Object { $_ -like "marker kept job=${job}:*" }).Count -eq 1) "the failure is logged: $($script:Logged -join ' | ')"
                Invoke-RequeueAdmissionWait -Marker $marker -MarkerPath $path
            }
            finally { $ErrorActionPreference = 'Stop' }
            Assert-True (Test-Path -LiteralPath (Join-Path $c.processed "$job.started.requeue-no-job.json")) 'it retires once the reader lets go'
            Assert-True (@($script:Logged | Where-Object { $_ -like '*claim retired*' }).Count -eq 1) "and only then is logged as retired: $($script:Logged -join ' | ')"
            return 'not claimed, then retired'
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
            # A marker naming another job is held here, never filed or retired under that other id. This binds the id only; a marker with the right id and false contents rests on the
            # bounded ownership model (one agent under its mutex; clients do not write running markers).
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

    # The common cause of pass 3: a flag guards only the branches it was placed in front of. Dot I1: this row searches those two functions' own bodies and claims nothing past them.
    $startupRecovery = @('Resolve-JobRecovery', 'Resolve-StartedMarkerFile', 'Resolve-StrandedResult', 'Invoke-OrphanResult', 'Invoke-AdoptStartedJob', 'Invoke-RequeueAdmissionWait', 'Resolve-LaunchingMarker', 'Invoke-QuarantineReconcile')
    if (-not (Skip-OnBase 'live-path-no-direct-startup-call')) {
        Invoke-Row 'live-path-no-direct-startup-call' {
            foreach ($fn in @('Invoke-LiveMarkerReconcile', 'Invoke-LiveMarkerRetire')) {
                $def = Get-AgentFunction $fn
                Assert-True ($null -ne $def) "the agent defines $fn"
                $calls = @($def.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() } | Where-Object { $_ -in $startupRecovery })
                Assert-True ($calls.Count -eq 0) "$fn calls no start-up recovery directly: [$($calls -join ',')]"
            }
            $text = $ast.Extent.Text
            Assert-True ($text -notmatch 'LiveReconcileOnly|LiveResultVanished') 'the flags are gone'
            return 'no direct call'
        }
    }

    # Dot I1, the transitive half: every helper those two reach by a named call, followed through the agent's own functions. Bounded by the number of functions the agent defines, each visited once.
    # A call the AST cannot name (& $x) cannot be followed, so one anywhere in the reach fails the row rather than hiding a path. It does not see Complete-StartedMarker's write to RecoveryQuarantine,
    # which is data and not a call: Invoke-LiveMarkerRetire removes that entry, and live-withdrawn-inside-complete covers it.
    if (-not (Skip-OnBase 'live-path-no-transitive-startup-call')) {
        Invoke-Row 'live-path-no-transitive-startup-call' {
            $defined = @{}
            foreach ($d in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) { $defined[$d.Name] = $d }
            $seen = @{}
            $work = [System.Collections.Generic.Queue[string]]::new()
            foreach ($root in @('Invoke-LiveMarkerReconcile', 'Invoke-LiveMarkerRetire')) { Assert-True $defined.ContainsKey($root) "the agent defines $root"; $work.Enqueue($root) }
            $dynamic = [System.Collections.Generic.List[string]]::new()
            $hits = [System.Collections.Generic.List[string]]::new()
            while ($work.Count -gt 0) {
                $fn = $work.Dequeue()
                if ($seen.ContainsKey($fn)) { continue }
                $seen[$fn] = $true
                foreach ($call in $defined[$fn].FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
                    $name = $call.GetCommandName()
                    if ([string]::IsNullOrEmpty($name)) { $dynamic.Add("$fn -> $($call.Extent.Text)"); continue }
                    if ($startupRecovery -contains $name) { $hits.Add("$fn -> $name") }
                    if ($defined.ContainsKey($name)) { $work.Enqueue($name) }
                }
            }
            # The walk must really have gone through the shared helpers, or the empty result below would prove nothing.
            foreach ($must in @('Complete-StartedMarker', 'Resolve-JobMarkerTemps', 'Write-JsonFileAtomic', 'Move-FileChecked', 'Read-MarkerArtifact', 'Set-MarkerRetireQuarantine')) { Assert-True $seen.ContainsKey($must) "the walk reached ${must}: [$($seen.Keys -join ',')]" }
            Assert-True ($hits.Count -eq 0) "no reachable helper calls start-up recovery: [$($hits -join '; ')]"
            Assert-True ($dynamic.Count -eq 0) "every call in reach can be named: [$($dynamic -join '; ')]"
            return [ordered]@{ helpersWalked = $seen.Count }
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
