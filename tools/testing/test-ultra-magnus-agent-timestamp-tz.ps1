[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = '',
    # Set by the driver on its child runs: the UTC offset, in minutes, the child forces on its own process (-300 = UTC-5, 330 =
    # UTC+5:30). Empty = driver mode: run one child per offset (plus the spelling mutants) on THIS PowerShell edition.
    [string]$ForceUtcOffsetMinutes = '',
    # Child runs of the spelling mutants only: skip the helper's own unit cases so the SITE cases are what fail.
    [switch]$SkipHelperUnit
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
#   out of range - AGENT-LAUNCH-MARKER-MINVALUE-GUARD-1: a time at DateTime.MinValue or MaxValue never throws out of the three
#                 sites above (a throw there ends the agent): queue age is clamped to [0, Int32.MaxValue], find-child is
#                 unknown, and an adopt deadline that cannot be represented reads as passed
# r2: the shift only shows when the process is NOT on UTC, so this test never trusts the host's zone. It FORCES the zone of the
# process: once at UTC-5 (what moves a deadline 5 h LATE on a pwsh 7 JSON read-back) and once at UTC+5:30 (what moves it early
# and kills a live job), and asserts the effective offset is non-zero and the forced one BEFORE any other assertion; a zero or
# unforced offset FAILS, it never passes vacuously on a UTC host. Windows has no per-process zone and the host zone must not be
# changed, so the child process swaps the runtime's cached local TimeZoneInfo for a custom fixed-offset zone (reflection, the
# same field set on pwsh 7 and Windows PowerShell 5.1) and shims Get-Date, which the runtime does not redirect, to that zone.
# Also: a self-check that the four wrong ways to read the value (an [string] cast then Parse, [datetime]"$x", Parse("$($x)"),
# DateTime.Parse) really move a time under the forced zone, and a mutant run that swaps each of them into the agent's helper and
# requires the queue-age, find-child and adopt cases to FAIL on both offsets (a behaviour check, not a text pin).
# Prints one JSON object with status PASS, or throws.

$ErrorActionPreference = 'Stop'
# Not a param default: $PSScriptRoot is empty there on Windows PowerShell 5.1.
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path (Split-Path -Parent $PSCommandPath) '..\..')).Path }
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }

# The wrong spellings, as expressions over $Value that return a UTC [DateTime]. Each is what an editor might write instead of
# ConvertTo-UtcDateTime; each drops the Kind of a pwsh 7 JSON [DateTime] and/or reads a bare string in the process's zone.
$wrongSpellings = [ordered]@{
    'dto-parse-string-cast'      = '[DateTimeOffset]::Parse([string]$Value).UtcDateTime'
    'datetime-parse-string-cast' = '[DateTime]::Parse([string]$Value).ToUniversalTime()'
    'datetime-cast-interpolated' = '([datetime]"$Value").ToUniversalTime()'
    'dto-parse-interpolated'     = '[DateTimeOffset]::Parse("$($Value)").UtcDateTime'
}

# Swap the runtime's cached local zone for a fixed-offset one. Field names: pwsh 7 _localTimeZone/_oneYearLocalFromUtc, Windows
# PowerShell 5.1 m_localTimeZone/m_oneYearLocalFromUtc. A layout change throws instead of leaving the host zone in place.
function Set-ProcessTimeZoneOffset([int]$Minutes) {
    $custom = [TimeZoneInfo]::CreateCustomTimeZone("Forced$Minutes", [TimeSpan]::FromMinutes($Minutes), "forced $Minutes min", "forced $Minutes min")
    $cache = [TimeZoneInfo].GetField('s_cachedData', [Reflection.BindingFlags]'NonPublic,Static')
    if ($null -eq $cache) { throw 'FAIL: cannot force the time zone: TimeZoneInfo.s_cachedData not found (runtime layout changed)' }
    $data = $cache.GetValue($null)
    $fields = $data.GetType().GetFields([Reflection.BindingFlags]'NonPublic,Instance')
    $localField = @($fields | Where-Object { $_.Name -match '^(_|m_)localTimeZone$' })
    Assert-True ($localField.Count -eq 1) "the cached local zone field is found exactly once, found $($localField.Count)"
    $localField[0].SetValue($data, $custom)
    foreach ($f in @($fields | Where-Object { $_.Name -match '^(_|m_)oneYearLocalFromUtc$' })) { $f.SetValue($data, $null) }
}

$isChild = $ForceUtcOffsetMinutes -ne ''
$results = [ordered]@{}
$results['edition'] = "$($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"
if ($isChild) {
    $forced = [int]$ForceUtcOffsetMinutes
    $realHostMinutes = [TimeZoneInfo]::Local.GetUtcOffset([DateTime]::UtcNow).TotalMinutes
    # FIRST assertion of the child: a zero offset would let every case pass without exercising the bug.
    Assert-True ($forced -ne 0) 'the forced UTC offset is non-zero (a UTC process cannot show the time zone bug)'
    Set-ProcessTimeZoneOffset $forced
    # Every way the agent's parse reaches the zone must now report the forced offset, not the host's.
    $effLocal = [TimeZoneInfo]::Local.GetUtcOffset([DateTime]::UtcNow).TotalMinutes
    $effNow = [DateTimeOffset]::Now.Offset.TotalMinutes
    $effParse = [DateTimeOffset]::Parse('2026-10-25T11:00:00').Offset.TotalMinutes
    Assert-True ($effLocal -ne 0 -and $effNow -ne 0 -and $effParse -ne 0) "the effective offset is non-zero (local $effLocal, now $effNow, parse $effParse min)"
    Assert-True ($effLocal -eq $forced -and $effNow -eq $forced -and $effParse -eq $forced) "the effective offset is the forced $forced min (local $effLocal, now $effNow, parse $effParse)"
    $results['forcedOffsetMinutes'] = $forced
    $results['effectiveOffsetMinutes'] = $effLocal
    $results['realHostOffsetMinutes'] = $realHostMinutes
    # [DateTime]::Now / Get-Date read a separate runtime cache that the swap does not reach; the agent calls Get-Date, so the
    # test supplies one in the forced zone (a Local-kind DateTime), as it supplies its other stubs.
    function Get-Date { [CmdletBinding()] param([Parameter(ValueFromRemainingArguments = $true)]$Ignored) return [DateTimeOffset]::UtcNow.LocalDateTime }
    Assert-True ([Math]::Abs(((Get-Date).ToUniversalTime() - [DateTime]::UtcNow).TotalSeconds) -lt 5) 'the Get-Date shim agrees with UTC now'
}

$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) "agent parses ($($parseErrors.Count) errors)"
# The AST node of one agent function, or $null when -Optional and the agent lacks it.
function Get-AgentFunctionAst([string]$Name, [switch]$Optional) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if ($null -eq $fn -and -not $Optional) { throw "FAIL: agent defines $Name" }
    return $fn
}
# The text of one agent function, or $null when -Optional and the agent lacks it. The caller dot-sources it at script scope
# (inside a function the definition would stay local to that call).
function Get-AgentFunctionText([string]$Name, [switch]$Optional) {
    $fn = Get-AgentFunctionAst $Name -Optional:$Optional
    if ($null -eq $fn) { return $null }
    return $fn.Extent.Text
}

$fx = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-ts-tz-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $fx | Out-Null

try {
    # ================================================================ driver mode
    if (-not $isChild) {
        $exe = (Get-Process -Id $PID).Path
        $offsets = [ordered]@{ 'utc-5' = -300; 'utc+5:30' = 330 }
        function Invoke-OffsetChild([int]$Minutes, [string]$Agent, [switch]$Mutant) {
            $childArgs = @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $PSCommandPath, '-ForceUtcOffsetMinutes', "$Minutes", '-RepoRoot', $RepoRoot, '-AgentScript', $Agent)
            if ($Mutant) { $childArgs += '-SkipHelperUnit' }
            # A failing child writes to stderr; under 'Stop' Windows PowerShell 5.1 turns that merged stream into a throw here.
            $ErrorActionPreference = 'Continue'
            $text = (& $exe @childArgs 2>&1 | ForEach-Object { "$_" } | Out-String)
            return [pscustomobject]@{ code = $LASTEXITCODE; text = $text }
        }
        $driverFailures = [System.Collections.Generic.List[string]]::new()
        $byOffset = [ordered]@{}
        foreach ($kv in $offsets.GetEnumerator()) {
            $r = Invoke-OffsetChild $kv.Value $agentScript
            if ($r.code -ne 0) { $driverFailures.Add("offset $($kv.Key) ($($kv.Value) min), exit $($r.code):`n$($r.text.TrimEnd())"); continue }
            $byOffset[$kv.Key] = ($r.text | ConvertFrom-Json)
        }
        # Mutants: the agent's own helper replaced by each wrong spelling. The site cases must catch every one at both offsets.
        $helperAst = Get-AgentFunctionAst 'ConvertTo-UtcDateTime' -Optional
        $mutants = [ordered]@{}
        if ($null -ne $helperAst) {
            $agentText = [IO.File]::ReadAllText($agentScript)
            foreach ($sp in $wrongSpellings.GetEnumerator()) {
                $mutantFn = 'function ConvertTo-UtcDateTime { param($Value) try { return ' + $sp.Value + ' } catch { return $null } }'
                $mutantText = $agentText.Substring(0, $helperAst.Extent.StartOffset) + $mutantFn + $agentText.Substring($helperAst.Extent.EndOffset)
                $mutantPath = Join-Path $fx "mutant-$($sp.Key).ps1"
                [IO.File]::WriteAllText($mutantPath, $mutantText, (New-Object System.Text.UTF8Encoding($false)))
                foreach ($kv in $offsets.GetEnumerator()) {
                    $r = Invoke-OffsetChild $kv.Value $mutantPath -Mutant
                    $caught = $r.code -ne 0 -and $r.text -match 'queue-age' -and $r.text -match 'find-child' -and $r.text -match 'adopt'
                    $mutants["$($sp.Key)@$($kv.Key)"] = if ($caught) { 'caught' } else { 'MISSED' }
                    if (-not $caught) { $driverFailures.Add("mutant $($sp.Key) at $($kv.Key) was not failed by queue-age, find-child and adopt cases (exit $($r.code)):`n$($r.text.TrimEnd())") }
                }
            }
        }
        else { $mutants['note'] = 'ABSENT (no helper in this agent)' }
        if ($driverFailures.Count -gt 0) { throw ("FAIL on $($results['edition']):`n" + ($driverFailures -join "`n")) }
        $results['status'] = 'PASS'
        $results['offsets'] = $byOffset
        $results['spellingMutants'] = $mutants
        $results | ConvertTo-Json -Depth 8
        return
    }

    # ================================================================ child mode (zone forced above)
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
    # One agent call under an error preference, inside a try as the agent's are: the agent runs under 'Continue', but its whole
    # body (startup recovery and the poll loop) sits in one try/finally, so a failed .NET call unwinds to it and the agent exits
    # under either preference. Returns @{ value; threw } (threw = the message).
    function Invoke-UnderPreference([string]$Preference, [scriptblock]$Call) {
        $ErrorActionPreference = $Preference
        try { return @{ value = (& $Call 2>$null); threw = $null } }
        catch { return @{ value = $null; threw = $_.Exception.Message } }
    }

    # ---------------------------------------------------------------- spelling self-check
    # Under the forced zone each wrong spelling must give a different instant for a bare string (both editions) and for the
    # pwsh 7 JSON [DateTime]; if one did not, the shapes above could not tell it from the right code.
    $wantTicks = [DateTime]::new(2026, 10, 25, 11, 0, 0, [DateTimeKind]::Utc).Ticks
    $shapes0 = New-Shapes ([DateTime]::new(2026, 10, 25, 11, 0, 0, [DateTimeKind]::Utc))
    $controls = [ordered]@{}
    foreach ($sp in $wrongSpellings.GetEnumerator()) {
        $fn = [scriptblock]::Create('param($Value) ' + $sp.Value)
        $bare = & $fn $shapes0['no-offset']
        Assert-True ($bare.Ticks -ne $wantTicks) "spelling $($sp.Key) moves a bare 11:00:00 under the forced zone (it read $($bare.ToString('o')))"
        $json = & $fn $shapes0['json-readback']
        if ($shapes0['json-readback'] -is [DateTime]) {
            Assert-True ($json.Ticks -ne $wantTicks) "spelling $($sp.Key) moves the pwsh 7 JSON [DateTime] under the forced zone (it read $($json.ToString('o')))"
        }
        $controls[$sp.Key] = 'moves a bare string' + $(if ($shapes0['json-readback'] -is [DateTime]) { ' and the JSON [DateTime]' } else { '' })
    }
    $results['spellingControls'] = $controls

    # ---------------------------------------------------------------- helper
    $helperText = Get-AgentFunctionText 'ConvertTo-UtcDateTime' -Optional
    $hasHelper = $null -ne $helperText
    if ($hasHelper) { . ([scriptblock]::Create($helperText)) }
    if ($hasHelper -and -not $SkipHelperUnit) {
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
    elseif ($hasHelper) {
        $results['helper'] = 'SKIPPED (mutant run)'
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
    # AGENT-LAUNCH-MARKER-MINVALUE-GUARD-1: an age that does not fit an [int] is clamped to [0, Int32.MaxValue] (a submitUtc at
    # DateTime.MinValue is as old as an age can be; one at MaxValue is in the future, 0). Before, the [int] cast threw out of the
    # poll loop and ended the agent. Every age the cast could hold keeps its old answer: r1 adversary A1 (2147483647.x s old,
    # -2147483648.x s old, the latter beside a 1950 file time) and A2 (no submitUtc, a 1950 file time; base gap, same class).
    $oldJob = [pscustomobject]@{ CreationTimeUtc = [DateTime]::new(1950, 1, 1, 0, 0, 0, [DateTimeKind]::Utc) }
    $maxInt = [int]::MaxValue
    $edges = [ordered]@{
        'minvalue'    = @{ at = [DateTime]::MinValue; job = $job; want = $maxInt; shapes = $null }
        'maxvalue'    = @{ at = [DateTime]::MaxValue.AddSeconds(-1); job = $job; want = 0; shapes = $null }
        'a1-posmax'   = @{ at = [DateTime]::UtcNow.AddSeconds(-2147483647.2); job = $job; want = $maxInt; shapes = @('json-readback', 'z-string') }
        'a1-negmin'   = @{ at = [DateTime]::UtcNow.AddSeconds(2147483648.2); job = $job; want = 0; shapes = @('json-readback', 'z-string') }
        'a1-old-file' = @{ at = [DateTime]::UtcNow.AddSeconds(2147483648.2); job = $oldJob; want = 0; shapes = @('json-readback', 'z-string') }
    }
    foreach ($edge in $edges.GetEnumerator()) {
        foreach ($kv in (New-Shapes ([DateTime]::SpecifyKind($edge.Value.at, [DateTimeKind]::Utc))).GetEnumerator()) {
            if ($null -ne $edge.Value.shapes -and $kv.Key -notin $edge.Value.shapes) { continue }
            foreach ($pref in @('Continue', 'Stop')) {
                $r = Invoke-UnderPreference $pref { Get-QueueAgeSeconds -Job $edge.Value.job -Metadata ([pscustomobject]@{ submitUtc = $kv.Value }) }
                $ageCases["$($edge.Key)-$($kv.Key)-$pref"] = if ($r.threw) { 'threw' } else { $r.value }
                if ($r.threw) { $failures.Add("queue-age $($edge.Key) $($kv.Key) ($pref): must not throw, threw $($r.threw)") }
                elseif ($r.value -ne $edge.Value.want) { $failures.Add("queue-age $($edge.Key) $($kv.Key) ($pref): expected $($edge.Value.want), got '$($r.value)'") }
            }
        }
    }
    foreach ($pref in @('Continue', 'Stop')) {
        $r = Invoke-UnderPreference $pref { Get-QueueAgeSeconds -Job $oldJob -Metadata ([pscustomobject]@{ submitUtc = $null }) }
        $ageCases["a2-missing-old-file-$pref"] = if ($r.threw) { 'threw' } else { $r.value }
        if ($r.threw) { $failures.Add("queue-age a2 missing submitUtc, 1950 file ($pref): must not throw, threw $($r.threw)") }
        elseif ($r.value -ne $maxInt) { $failures.Add("queue-age a2 missing submitUtc, 1950 file ($pref): expected $maxInt, got '$($r.value)'") }
    }
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
    # AGENT-LAUNCH-MARKER-MINVALUE-GUARD-1: a launchingUtc at or within 2 s of DateTime.MinValue cannot have the 2 s slack taken
    # off it. It is unknown and never throws, under the agent's own 'Continue' preference and under 'Stop'. Before, AddSeconds
    # threw out of startup recovery and ended the agent, and the next start read the same marker.
    foreach ($edgeSec in @(0, 1)) {
        foreach ($kv in (New-Shapes ([DateTime]::SpecifyKind([DateTime]::MinValue.AddSeconds($edgeSec), [DateTimeKind]::Utc))).GetEnumerator()) {
            foreach ($pref in @('Continue', 'Stop')) {
                $r = Invoke-UnderPreference $pref { Find-LaunchedJobChild -Marker ([pscustomobject]@{ launchScriptPath = 'C:\jobs\the-job.ps1'; launchImagePath = ''; launchingUtc = $kv.Value }) }
                $label = if ($r.threw) { 'threw' } else { [string]$r.value.state }
                $childCases["minvalue+${edgeSec}s-$($kv.Key)-$pref"] = $label
                if ($r.threw) { $failures.Add("find-child minvalue+${edgeSec}s $($kv.Key) ($pref): must not throw, threw $($r.threw)") }
                elseif ($r.value.state -ne 'unknown' -or [string]$r.value.why -notmatch 'launchingUtc') { $failures.Add("find-child minvalue+${edgeSec}s $($kv.Key) ($pref): expected unknown naming launchingUtc, got $($r.value.state) ($($r.value.why))") }
            }
        }
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
    function Invoke-AdoptCase([string]$Name, $StartedValue, [int]$TimeoutSec, [bool[]]$Identity, [string]$Expect, [string]$Preference = 'Stop') {
        $script:Calls.Clear(); $script:Published = $null
        $script:IdentitySeq = [System.Collections.Generic.List[bool]]::new()
        foreach ($b in $Identity) { $script:IdentitySeq.Add($b) }
        $outcome = 'adopted'
        $ErrorActionPreference = $Preference
        try { Invoke-AdoptStartedJob -Marker (New-AdoptMarker $StartedValue $TimeoutSec) -MarkerPath (Join-Path $fx 'm.json') -Match ([pscustomobject]@{ matches = $true }) 2>$null }
        catch { $outcome = if ($_.Exception.Message -like 'SENTINEL*') { 'held-past-deadline' } else { 'threw' }; $why = $_.Exception.Message }
        $killed = $script:Calls.Contains('kill')
        $label = "$outcome/killed=$killed"
        $script:AdoptSummary[$Name] = $label
        if ($label -ne $Expect) { $failures.Add("adopt $Name`: expected $Expect, got $label $why") }
    }
    $script:AdoptSummary = [ordered]@{}
    # Started 10 s ago with a 2 s budget: the deadline is 8 s past. The child is still alive, so it is killed at once (the loop
    # is not entered; the final look is the first). A reading that moves the deadline 5 h later enters the loop and holds it.
    $started = [DateTime]::UtcNow.AddSeconds(-10)
    foreach ($kv in (New-Shapes $started).GetEnumerator()) {
        Invoke-AdoptCase "expired-$($kv.Key)" $kv.Value 2 @($true) 'adopted/killed=True'
    }
    # Started 10 s ago with a 3600 s budget: the child is not yet due. The loop looks (alive), looks again (gone) and the final
    # look finds it gone, so it ends unkilled. A reading that moves the deadline EARLIER (a process east of UTC) never enters the
    # loop, so its one look is the FINAL one, which finds the child alive and kills it.
    foreach ($kv in (New-Shapes $started).GetEnumerator()) {
        Invoke-AdoptCase "inside-$($kv.Key)" $kv.Value 3600 @($true, $false, $false) 'adopted/killed=False'
    }
    # Missing or unreadable startedUtc: no deadline can be computed, so the adopt still throws before waiting or killing.
    Invoke-AdoptCase 'missing' $null 2 @($true) 'threw/killed=False'
    Invoke-AdoptCase 'unparseable' 'not a time' 2 @($true) 'threw/killed=False'
    # AGENT-LAUNCH-MARKER-MINVALUE-GUARD-1: a startedUtc whose deadline (startedUtc + timeoutSec) cannot be represented reads as a
    # deadline already passed: the live child is killed at once, never an uncaught AddSeconds (which ended the agent).
    foreach ($kv in (New-Shapes ([DateTime]::SpecifyKind([DateTime]::MaxValue.AddSeconds(-1), [DateTimeKind]::Utc))).GetEnumerator()) {
        foreach ($pref in @('Continue', 'Stop')) {
            Invoke-AdoptCase "maxvalue-$($kv.Key)-$pref" $kv.Value 3600 @($true) 'adopted/killed=True' $pref
        }
    }
    $results['adopt'] = $script:AdoptSummary

    if ($failures.Count -gt 0) { throw ("FAIL: forced offset $forced min, edition $($results['edition']):`n  " + ($failures -join "`n  ")) }
    $results['status'] = 'PASS'
    $results | ConvertTo-Json -Depth 6
}
finally {
    # The fixture holds the adopt cases' stderr file and, in driver mode, the mutant agents: remove them by name, then the
    # (now empty) directory.
    Remove-Item -LiteralPath (Join-Path $fx 'adopt.err') -Force -ErrorAction SilentlyContinue
    foreach ($m in @(Get-ChildItem -LiteralPath $fx -Filter 'mutant-*.ps1' -File -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $m.FullName -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $fx -Force -ErrorAction SilentlyContinue
}
