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
#   unit  the agent's real functions, lifted from its AST:
#         inbox-job-left-alone       a job whose script is in inbox\ is the live set: no recovery runs, even with marker + result
#         own-claim-retire-retried   this agent's own unlaunched claim whose retire failed stays quarantined, then clears
# Prints one JSON object. Exit 0 = every row passed. Exit 1 = a row failed; the object says which (a RED run needs the rows, not the first throw).

$ErrorActionPreference = 'Stop'
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
$caseNames = @('reader-denied-completion', 'incident-fixture', 'tmp-only-fixture', 'live-job-left-alone', 'no-result-left-alone', 'locked-marker-quarantined', 'inbox-job-left-alone', 'own-claim-retire-retried')
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($n in $Only) { if ($caseNames -notcontains $n) { throw "unknown case '$n' (known: $($caseNames -join ', '))" } }
function Want([string]$Name) { return ($Only.Count -eq 0 -or $Only -contains $Name) }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }

$runId = 'agent-live-marker-{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID
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
            if ($AgentScript) { $results[$Name] = [ordered]@{ status = 'NA'; detail = 'the base agent has no Invoke-LiveMarkerReconcile' } }
            else { $results[$Name] = [ordered]@{ status = 'FAIL'; error = 'FAIL: the agent defines no Invoke-LiveMarkerReconcile' } }
        }
        return $true
    }

    if (-not (Skip-OnBase 'inbox-job-left-alone')) {
        Invoke-Row 'inbox-job-left-alone' {
            $c = New-Case 'inbox-job-left-alone'
            $script:running = $c.running; $script:outbox = $c.outbox; $script:inbox = (Join-Path $c.root 'inbox')
            $script:RecoveryQuarantine = @{}
            $script:Recovered = [System.Collections.Generic.List[string]]::new()
            foreach ($name in @('Get-MarkerTempFiles', 'Get-RunningMarkerJobIds', 'Invoke-LiveMarkerReconcile')) { . ([scriptblock]::Create((Get-AgentFunction $name).Extent.Text)) }
            function Resolve-JobRecovery { param([string]$JobId) $script:Recovered.Add($JobId); return $true }
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
            $script:RecoveryQuarantine = @{}; $script:OwnPendingMarkerTmp = @{}; $script:LastRecoveryWhy = $null
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
