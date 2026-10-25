[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = ''
)

# AGENT-TIMESTAMP-PARSE-TZ-1 r1: the Bachelor agent (tools/profiling/bachelor/ultra-magnus-agent.ps1) must read a marker or
# meta.json timestamp as UTC on ANY host. PowerShell 7's ConvertFrom-Json hands an ISO "...Z" string back as a [DateTime] of Kind
# Utc; `[string]` on it drops the Kind, and Parse then assumes THIS host's offset, so on a Central-time host a time moved by 5 h.
# Windows PowerShell 5.1 hands back the string itself, so every case below is built both ways (read back through ConvertFrom-Json
# on whatever edition runs this, and a plain string, which is what 5.1 returns). The agent's REAL functions are lifted from its AST
# and driven with their disk, process and CIM steps stubbed. No agent loop, no share, no venue, no playback.
#   helper      - ConvertTo-UtcDateTime: DateTime by Kind (Unspecified is UTC), DateTimeOffset, strings (no offset = UTC), else $null
#   queue-age   - Get-QueueAgeSeconds reads submitUtc the same way on every shape; missing falls back to the file's creation time
#   find-child  - Find-LaunchedJobChild reads launchingUtc the same way; a missing or bad value is "unknown"
#   adopt       - Invoke-AdoptStartedJob (the restart-and-adopt path): the deadline is startedUtc + timeoutSec in UTC. A job past
#                 it is killed at once; a job inside it is not; a missing startedUtc still throws (fail closed)
# The shift only shows when this host is not on UTC; the host offset is recorded in the output. Prints one JSON object with
# status PASS, or throws.

$ErrorActionPreference = 'Stop'
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }

$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) "agent parses ($($parseErrors.Count) errors)"
# The text of one agent function, or $null when -Optional and the agent lacks it. The caller dot-sources it at script scope
# (inside a function the definition would stay local to that call).
function Get-AgentFunctionText([string]$Name, [switch]$Optional) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if ($null -eq $fn) {
        if ($Optional) { return $null }
        throw "FAIL: agent defines $Name"
    }
    return $fn.Extent.Text
}

$results = [ordered]@{}
$hostOffsetHours = [TimeZoneInfo]::Local.GetUtcOffset([DateTime]::UtcNow).TotalHours
$results['hostOffsetHours'] = $hostOffsetHours
$results['edition'] = "$($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"
$fx = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-ts-tz-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $fx | Out-Null

try {
    # One timestamp, in the four shapes a writer or an edition can produce. 'json' is read back through ConvertFrom-Json on this
    # edition (pwsh 7: a [DateTime]; 5.1: a string); the rest are plain values, the 5.1 shapes and the explicit-offset one.
    function New-Shapes([DateTime]$At) {
        $z = $At.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        $noOff = $At.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss')
        $json = (ConvertFrom-Json ('{"t":"' + $z + '"}')).t
        return [ordered]@{
            'json-readback' = $json
            'z-string'      = $z
            'plus0-string'  = $At.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffff') + '+00:00'
            'no-offset'     = $noOff
        }
    }

    # ---------------------------------------------------------------- helper
    $helperText = Get-AgentFunctionText 'ConvertTo-UtcDateTime' -Optional
    $hasHelper = $null -ne $helperText
    if ($hasHelper) { . ([scriptblock]::Create($helperText)) }
    if ($hasHelper) {
        $base = [DateTime]::new(2026, 10, 25, 11, 0, 0, [DateTimeKind]::Utc)
        $want = $base.Ticks
        $helperCases = [ordered]@{}
        foreach ($kv in (New-Shapes $base).GetEnumerator()) {
            $v = ConvertTo-UtcDateTime $kv.Value
            Assert-True ($null -ne $v -and $v.Kind -eq [DateTimeKind]::Utc -and $v.Ticks -eq $want) "helper: $($kv.Key) reads as 2026-10-25T11:00:00Z, got '$v'"
            $helperCases[$kv.Key] = 'ok'
        }
        $vLocal = ConvertTo-UtcDateTime ([DateTime]::SpecifyKind($base.ToLocalTime(), [DateTimeKind]::Local))
        Assert-True ($vLocal.Ticks -eq $want) "helper: a Local [DateTime] is converted, got '$vLocal'"
        $vUnspec = ConvertTo-UtcDateTime ([DateTime]::SpecifyKind($base, [DateTimeKind]::Unspecified))
        Assert-True ($vUnspec.Ticks -eq $want -and $vUnspec.Kind -eq [DateTimeKind]::Utc) "helper: an Unspecified [DateTime] is read as UTC, got '$vUnspec'"
        $vDto = ConvertTo-UtcDateTime ([DateTimeOffset]::new([DateTime]::SpecifyKind($base.AddHours(3), [DateTimeKind]::Unspecified), [TimeSpan]::FromHours(3)))
        Assert-True ($vDto.Ticks -eq $want) "helper: a DateTimeOffset is its UtcDateTime, got '$vDto'"
        $vPlus2 = ConvertTo-UtcDateTime '2026-10-25T13:00:00+02:00'
        Assert-True ($vPlus2.Ticks -eq $want) "helper: a string that names an offset keeps it, got '$vPlus2'"
        foreach ($bad in @($null, '', '   ', 'not a time', 12345)) {
            Assert-True ($null -eq (ConvertTo-UtcDateTime $bad)) "helper: '$bad' is unparseable and reads as `$null"
        }
        $helperCases['kinds'] = 'ok'
        $results['helper'] = $helperCases
    }
    else {
        $results['helper'] = 'ABSENT'
    }

    # ---------------------------------------------------------------- queue-age
    . ([scriptblock]::Create((Get-AgentFunctionText 'Get-QueueAgeSeconds')))
    $ageCases = [ordered]@{}
    $failures = [System.Collections.Generic.List[string]]::new()
    $submitted = [DateTime]::UtcNow.AddSeconds(-90)
    $job = [pscustomobject]@{ CreationTimeUtc = [DateTime]::UtcNow.AddSeconds(-400) }
    foreach ($kv in (New-Shapes $submitted).GetEnumerator()) {
        $age = Get-QueueAgeSeconds -Job $job -Metadata ([pscustomobject]@{ submitUtc = $kv.Value })
        $ageCases[$kv.Key] = $age
        if ([Math]::Abs($age - 90) -gt 15) { $failures.Add("queue-age $($kv.Key): expected about 90 s, got $age") }
    }
    $ageMissing = Get-QueueAgeSeconds -Job $job -Metadata ([pscustomobject]@{ submitUtc = $null })
    $ageCases['missing'] = $ageMissing
    if ([Math]::Abs($ageMissing - 400) -gt 15) { $failures.Add("queue-age missing: expected the file creation time (about 400 s), got $ageMissing") }
    $ageBad = Get-QueueAgeSeconds -Job $job -Metadata ([pscustomobject]@{ submitUtc = 'not a time' })
    $ageCases['unparseable'] = $ageBad
    if ([Math]::Abs($ageBad - 400) -gt 15) { $failures.Add("queue-age unparseable: expected the file creation time (about 400 s), got $ageBad") }
    $results['queueAge'] = $ageCases

    # ---------------------------------------------------------------- find-child
    . ([scriptblock]::Create((Get-AgentFunctionText 'Find-LaunchedJobChild')))
    # A live process whose creation is 1 s after the launching write; the agent's notBefore is launchingUtc - 2 s. A launchingUtc
    # read 5 h late puts notBefore after the process (no hit); read 5 h early, before it (still a hit, which proves nothing), so
    # the case also asks for a process 3 s BEFORE the launching write, which only an early reading would count.
    $launching = [DateTime]::UtcNow.AddSeconds(-30)
    $script:ProcCreation = $launching.AddSeconds(1)
    function Get-CimInstance {
        [CmdletBinding()] param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
        return [pscustomobject]@{ CommandLine = 'pwsh.exe -File C:\jobs\the-job.ps1'; ExecutablePath = 'C:\pwsh.exe'; CreationDate = $script:ProcCreation.ToLocalTime(); ProcessId = 4242 }
    }
    function Get-ProcessIdentity { param([int]$ProcessId) return [pscustomobject]@{ exists = $true; processId = $ProcessId; startUtc = $null; startEpochMs = $null; imagePath = 'C:\pwsh.exe'; commandLine = 'x' } }
    function Add-AgentLogLine { param($Line) }
    $childCases = [ordered]@{}
    foreach ($kv in (New-Shapes $launching).GetEnumerator()) {
        $script:ProcCreation = $launching.AddSeconds(1)
        $found = Find-LaunchedJobChild -Marker ([pscustomobject]@{ launchScriptPath = 'C:\jobs\the-job.ps1'; launchImagePath = ''; launchingUtc = $kv.Value })
        $childCases[$kv.Key] = [string]$found.state
        if ($found.state -ne 'found') { $failures.Add("find-child $($kv.Key): a process started 1 s after the launching write must be found, got $($found.state) ($($found.why))") }
        $script:ProcCreation = $launching.AddSeconds(-3)
        $early = Find-LaunchedJobChild -Marker ([pscustomobject]@{ launchScriptPath = 'C:\jobs\the-job.ps1'; launchImagePath = ''; launchingUtc = $kv.Value })
        $childCases["$($kv.Key)-predates"] = [string]$early.state
        if ($early.state -ne 'none') { $failures.Add("find-child $($kv.Key): a process that predates the launching write must not be found, got $($early.state)") }
    }
    $script:ProcCreation = $launching.AddSeconds(1)
    foreach ($badValue in @($null, '', 'not a time')) {
        $unk = Find-LaunchedJobChild -Marker ([pscustomobject]@{ launchScriptPath = 'C:\jobs\the-job.ps1'; launchImagePath = ''; launchingUtc = $badValue })
        $childCases["bad-$badValue"] = [string]$unk.state
        if ($unk.state -ne 'unknown') { $failures.Add("find-child '$badValue': expected unknown, got $($unk.state)") }
    }
    $results['findChild'] = $childCases

    # ---------------------------------------------------------------- adopt
    . ([scriptblock]::Create((Get-AgentFunctionText 'Invoke-AdoptStartedJob')))
    $PollSeconds = 0
    $script:Calls = [System.Collections.Generic.List[string]]::new()
    $script:IdentitySeq = $null
    function Set-AgentKeepAwake { param([bool]$Enable) return $true }
    function Write-AgentHeartbeat { param($Activity) }
    function Start-Sleep { param([int]$Milliseconds, [int]$Seconds) }
    function Test-ProcessIdentityMatch {
        param([int]$ProcessId, [string]$ExpectedStartUtc, $ExpectedStartEpochMs, [string]$ExpectedImagePath)
        if ($script:IdentitySeq.Count -eq 0) { throw 'SENTINEL: the adopt loop is still waiting on the child (its deadline is in the future)' }
        $m = $script:IdentitySeq[0]; $script:IdentitySeq.RemoveAt(0)
        return [pscustomobject]@{ matches = $m; identity = $null; reason = 'stub' }
    }
    function Stop-ProcessTree { param([int]$RootProcessId, [string]$RootStartUtc, $RootStartEpochMs, [string]$RootImagePath) $script:Calls.Add('kill'); return @($RootProcessId) }
    function Get-RecoveredAdmission { param($Admission) return $null }
    function Publish-JobResult { param([string]$JobId, $Result) $script:Published = $Result; return (Join-Path $fx "$JobId.result.json") }
    function Move-JobArtifacts { param($JobId, $JobPath, $DestinationDirectory) }
    function Complete-StartedMarker { param($MarkerPath, $Status, $ResultPath) $script:Calls.Add("complete:$Status") }
    $errFile = Join-Path $fx 'adopt.err'; $outFile = Join-Path $fx 'adopt.out'
    function New-AdoptMarker($StartedValue, [int]$TimeoutSec) {
        return [pscustomobject]@{ jobId = 'j-adopt'; childPid = 4242; childStartUtc = $null; childStartEpochMs = 1; childImagePath = 'C:\pwsh.exe'
            startedUtc = $StartedValue; timeoutSec = $TimeoutSec; admission = $null; errFile = $errFile; outFile = $outFile; sourceInboxPath = '' }
    }
    function Invoke-AdoptCase([string]$Name, $StartedValue, [int]$TimeoutSec, [bool[]]$Identity, [string]$Expect) {
        $script:Calls.Clear(); $script:Published = $null
        $script:IdentitySeq = [System.Collections.Generic.List[bool]]::new()
        foreach ($b in $Identity) { $script:IdentitySeq.Add($b) }
        $outcome = 'adopted'
        try { Invoke-AdoptStartedJob -Marker (New-AdoptMarker $StartedValue $TimeoutSec) -MarkerPath (Join-Path $fx 'm.json') -Match ([pscustomobject]@{ matches = $true }) }
        catch { $outcome = if ($_.Exception.Message -like 'SENTINEL*') { 'held-past-deadline' } else { 'threw' }; $why = $_.Exception.Message }
        $killed = $script:Calls.Contains('kill')
        $label = "$outcome/killed=$killed"
        $script:AdoptSummary[$Name] = $label
        if ($label -ne $Expect) { $failures.Add("adopt $Name`: expected $Expect, got $label $why") }
    }
    $script:AdoptSummary = [ordered]@{}
    # Started 10 s ago with a 2 s budget: the deadline is 8 s past. The child is still alive, so it is killed at once (one
    # identity check at the loop top is NOT made: the loop is not entered). A reading that moves the deadline 5 h later holds it.
    $started = [DateTime]::UtcNow.AddSeconds(-10)
    foreach ($kv in (New-Shapes $started).GetEnumerator()) {
        Invoke-AdoptCase "expired-$($kv.Key)" $kv.Value 2 @($true) 'adopted/killed=True'
    }
    # Started 10 s ago with a 3600 s budget: the child is not yet due. The loop's first look finds it gone, so it ends unkilled.
    # A reading that moves the deadline 5 h EARLIER (a host east of UTC) kills it.
    foreach ($kv in (New-Shapes $started).GetEnumerator()) {
        Invoke-AdoptCase "inside-$($kv.Key)" $kv.Value 3600 @($false, $false) 'adopted/killed=False'
    }
    # Missing or unreadable startedUtc: no deadline can be computed, so the adopt still throws before waiting or killing.
    Invoke-AdoptCase 'missing' $null 2 @($true) 'threw/killed=False'
    Invoke-AdoptCase 'unparseable' 'not a time' 2 @($true) 'threw/killed=False'
    $results['adopt'] = $script:AdoptSummary

    if ($failures.Count -gt 0) { throw ("FAIL: host offset $hostOffsetHours h, edition $($results['edition']):`n  " + ($failures -join "`n  ")) }
    $results['status'] = 'PASS'
    $results | ConvertTo-Json -Depth 6
}
finally {
    # The fixture holds at most the adopt cases' stderr file: remove it by name, then the (now empty) directory.
    Remove-Item -LiteralPath (Join-Path $fx 'adopt.err') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $fx -Force -ErrorAction SilentlyContinue
}
