# Wait-VenueQuiet.ps1 -- read-only CPU cooldown gate before a venue leg (LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1). RUN THIS ON THE VM.
#
# Two legs that are compared (Classic, then Cinematic) should each start on a venue that is not still busy from the previous one. This gate:
#   1. waits on the read-only queue gate: the venue agent's inbox holds no *.job.ps1 and running\ is empty (GATE_BUSY after -MaxGateSec);
#   2. submits ONE probe job through tools\profiling\um-run.ps1 (the only writer to a venue share), with a unique JobId venue-quiet-probe-<utc>.
#      The job reads 3 samples of \Processor(_Total)\% Processor Time, 12 s apart -- the counter and cadence of the attribution job's own
#      quiescence gate (playback-attr-3-cuda-job.ps1, Get-AttrCudaQuiescenceSample) -- plus the top 5 processes by CPU-seconds over that window;
#   3. decides: QUIET when the UNROUNDED mean of the 3 samples is <= -ThresholdPercent (the printed mean is rounded to 0.1 for display only). A
#      failed counter read makes the check UNKNOWN, never quiet.
#   4. re-probes every -RecheckSec until QUIET or -MaxWaitSec, then prints `QUIET mean=<x>` or `COOLDOWN_UNMET mean=<x>` and the top processes.
# COOLDOWN_UNMET is recorded and the caller proceeds: it decides nothing. The probe never kills, stops or changes any process.
#
# ONE DEADLINE: -MaxWaitSec runs from the start and bounds every queue-gate wait too (a re-probe does not get a fresh gate clock). A gate wait ends at the
# earlier of -MaxGateSec and that deadline (GATE_BUSY, exit 3). Only a probe already submitted can run past it, bounded by um-run.ps1's own timeouts.
# A read error on the agent share is retried -ReadAttempts times in all, backing off -ReadBackoffSec * attempt, inside the same deadline, before it is
# GATE_UNREADABLE; each retry prints a `GATE_READ_RETRY` line, never a `queued=0 running=0` line.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Wait-VenueQuiet.ps1 -Venue bachelor -WorkDir <dir> [-GateLog <file>]
#   pwsh -NoProfile -File tools\profiling\dual-venue\Wait-VenueQuiet.ps1 -SamplesJson '[12.5, 18.0, 21.0]'     # offline: the decision only
#
# The share and the work dir are BOUND to the named venue (SHARE-SCOPE): -AgentShare must be that venue's own agentShare or agentRoot in venues.json;
# another venue's share is SHARE_VENUE_MISMATCH and a share that is no venue's own is SHARE_NOT_VENUE_OWN unless -AllowShareOverride says the caller
# means it (tests); -WorkDir inside any venue's share or agent root is WORKDIR_IN_VENUE_SHARE (um-run.ps1 is the only writer to a venue share). All three
# exit 2 before anything is created, read or submitted, so a verdict can never be printed for a venue other than the one measured.
# -GateLog is bound the same way: GATELOG_IN_VENUE_SHARE (inside any venue's storage) or GATELOG_OUT_OF_SCOPE (neither under -WorkDir nor a .claude-state
# directory) are exit 2 before anything is created. HOST ECHO: the probe job reports $env:COMPUTERNAME; if it is not the venue's expectedHost in venues.json
# (case-insensitive; a missing host counts) the gate prints `HOST_MISMATCH ...` and exits 5 BEFORE any QUIET / COOLDOWN_UNMET line, so an alias of another
# host's share (acknowledged with -AllowShareOverride) can never yield a verdict for the named venue.
# Exit codes: 0 QUIET or COOLDOWN_UNMET (read stdout); 3 GATE_BUSY (the queue never cleared); 4 GATE_UNREADABLE (the agent share could not be
# read -- typed line `GATE_UNREADABLE ...` then `DECISION UNKNOWN`, never an empty-queue line; no probe is submitted); 5 HOST_MISMATCH (the probe was
# answered by a host that is not the venue's); 2 a usage error (including a -SamplesJson that is not JSON).
[CmdletBinding(DefaultParameterSetName = 'Live')]
param(
    [Parameter(ParameterSetName = 'Live')][ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'bachelor',
    [Parameter(ParameterSetName = 'Live', Mandatory = $true)][string]$WorkDir,
    [Parameter(ParameterSetName = 'Live')][string]$GateLog = '',
    [Parameter(ParameterSetName = 'Live')][string]$AgentShare = '',
    [Parameter(ParameterSetName = 'Live')][switch]$AllowShareOverride,
    [Parameter(ParameterSetName = 'Live')][int]$RecheckSec = 90,
    [Parameter(ParameterSetName = 'Live')][int]$MaxWaitSec = 1800,
    [Parameter(ParameterSetName = 'Live')][int]$MaxGateSec = 2400,
    [Parameter(ParameterSetName = 'Live')][int]$GatePollSec = 30,
    [Parameter(ParameterSetName = 'Live')][int]$ReadAttempts = 3,
    [Parameter(ParameterSetName = 'Live')][int]$ReadBackoffSec = 2,
    [Parameter(ParameterSetName = 'Offline', Mandatory = $true)][string]$SamplesJson,
    [double]$ThresholdPercent = 20.0
)
$ErrorActionPreference = 'Stop'
$t0 = Get-Date

function Get-VenueQuietDecision {
    # PURE: the decision from the samples alone. Exactly 3 finite samples in [0, 100] are needed; anything else (a $null = a failed read) is UNKNOWN.
    param([object[]]$Samples, [double]$Threshold)
    $vals = @($Samples)
    $ok = ($vals.Count -eq 3)
    foreach ($v in $vals) {
        if ($null -eq $v -or -not ($v -is [double] -or $v -is [int] -or $v -is [long] -or $v -is [decimal])) { $ok = $false; continue }
        $d = [double]$v
        if (-not [double]::IsFinite($d) -or $d -lt 0.0 -or $d -gt 100.0) { $ok = $false }
    }
    if (-not $ok) { return [pscustomobject]@{ state = 'UNKNOWN'; mean = $null } }
    # The UNROUNDED mean decides; Format-Mean rounds for display only (three 20.04% samples are BUSY at 20 although they print as 20.0%).
    $mean = (($vals | ForEach-Object { [double]$_ }) | Measure-Object -Average).Average
    [pscustomobject]@{ state = $(if ($mean -le $Threshold) { 'QUIET' } else { 'BUSY' }); mean = $mean }
}

function Format-Mean($m) { if ($null -eq $m) { 'UNKNOWN' } else { '{0:0.0}%' -f $m } }

if ($PSCmdlet.ParameterSetName -eq 'Offline') {
    try { $samples = @(ConvertFrom-Json -InputObject $SamplesJson -NoEnumerate) } catch {
        [Console]::Error.WriteLine("USAGE -SamplesJson is not valid JSON: $($_.Exception.Message)")
        exit 2
    }
    if ($samples.Count -eq 1 -and $samples[0] -is [array]) { $samples = @($samples[0]) }
    $d = Get-VenueQuietDecision -Samples $samples -Threshold $ThresholdPercent
    Write-Output "DECISION $($d.state) mean=$(Format-Mean $d.mean)"
    exit 0
}

$here = $PSScriptRoot
$table = [IO.File]::ReadAllText((Join-Path $here 'venues.json')) | ConvertFrom-Json
$agentShare = $(if ($AgentShare) { $AgentShare } else { [string]$table.venues.$Venue.agentShare })

# Bind the share and the work dir to the named venue BEFORE anything is created, read or submitted (see the header).
function ConvertTo-PathKey([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    $full = try { [IO.Path]::GetFullPath($Path) } catch { $Path }
    $full.TrimEnd([char[]]@('\', '/')).ToLowerInvariant()
}
function Test-UnderOrEqual([string]$Key, [string]$Root) { $Root -ne '' -and ($Key -ceq $Root -or $Key.StartsWith($Root + '\', [StringComparison]::Ordinal)) }
function Stop-Usage([string]$Message) { [Console]::Error.WriteLine($Message); exit 2 }
$ownKeys = @(@($table.venues.$Venue.agentShare, $table.venues.$Venue.agentRoot) | ForEach-Object { ConvertTo-PathKey ([string]$_) } | Where-Object { $_ })
$otherKeys = [ordered]@{}
foreach ($v in $table.venues.PSObject.Properties) {
    foreach ($p in @($v.Value.agentShare, $v.Value.agentRoot)) { $k = ConvertTo-PathKey ([string]$p); if ($k) { $otherKeys[$k] = $v.Name } }
}
if ($AgentShare) {
    $shareKey = ConvertTo-PathKey $AgentShare
    if ($ownKeys -notcontains $shareKey) {
        $foreign = @($otherKeys.Keys | Where-Object { $otherKeys[$_] -cne $Venue -and (Test-UnderOrEqual $shareKey $_) })
        if ($foreign.Count -gt 0) { Stop-Usage "SHARE_VENUE_MISMATCH -AgentShare '$AgentShare' is venue '$($otherKeys[$foreign[0]])' agent storage, not '$Venue': a verdict printed for '$Venue' would describe another host" }
        if (-not $AllowShareOverride) { Stop-Usage "SHARE_NOT_VENUE_OWN -AgentShare '$AgentShare' is not '$Venue' agentShare or agentRoot in venues.json; pass -AllowShareOverride only for a test share" }
    }
}
$workKey = ConvertTo-PathKey $WorkDir
foreach ($k in $otherKeys.Keys) {
    if (Test-UnderOrEqual $workKey $k) { Stop-Usage "WORKDIR_IN_VENUE_SHARE -WorkDir '$WorkDir' is inside venue '$($otherKeys[$k])' agent storage; um-run.ps1 is the only writer to a venue share" }
}
# -GateLog is appended to by this script, so it is bound too: never inside a venue's agent storage (um-run.ps1 is the only writer there), and only under the
# run's own -WorkDir or a .claude-state directory.
if ($GateLog) {
    $logKey = ConvertTo-PathKey $GateLog
    foreach ($k in $otherKeys.Keys) {
        if (Test-UnderOrEqual $logKey $k) { Stop-Usage "GATELOG_IN_VENUE_SHARE -GateLog '$GateLog' is inside venue '$($otherKeys[$k])' agent storage; um-run.ps1 is the only writer to a venue share" }
    }
    $logInScope = (Test-UnderOrEqual $logKey $workKey) -or (@($logKey.Split([char[]]@('\', '/')) | Where-Object { $_ -ceq '.claude-state' }).Count -gt 0)
    if (-not $logInScope) { Stop-Usage "GATELOG_OUT_OF_SCOPE -GateLog '$GateLog' is neither under -WorkDir nor under a .claude-state directory" }
}
$expectedHost = [string]$table.venues.$Venue.expectedHost
if (-not $expectedHost) { Stop-Usage "VENUE_HOST_UNKNOWN venues.json names no expectedHost for '$Venue': a probe answer could not be attributed to it" }
$umRun = Join-Path $here '..\um-run.ps1'
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

function Write-Gate([string]$Line) {
    Write-Output $Line
    if ($GateLog) { $Line | Add-Content -LiteralPath $GateLog }
}

# One directory count, or $null when the directory cannot be read: an unreachable share is NOT an empty queue. The inbox must exist; running\ may
# legitimately be absent while the share itself is readable (nothing has run), so only a read error on an existing running\ is unreadable.
function Get-GateCount([string]$Dir, [string]$Filter, [bool]$MustExist) {
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) {
        if ($MustExist -or -not (Test-Path -LiteralPath $agentShare -PathType Container)) { return $null }
        return 0
    }
    try {
        if ($Filter) { return @(Get-ChildItem -LiteralPath $Dir -Filter $Filter -ErrorAction Stop).Count }
        return @(Get-ChildItem -LiteralPath $Dir -ErrorAction Stop).Count
    } catch { return $null }
}

# Sets $script:GateState to 'CLEAR', 'BUSY' (the queue never cleared) or 'UNREADABLE' (the share could not be read: nothing is known, the decision
# is UNKNOWN). The state is not the function's output: its GATE lines are written to the pipeline and must reach stdout.
#
# ONE DEADLINE: the clock is the script's own $t0, never restarted. A gate wait ends at t0 + min(-MaxGateSec, -MaxWaitSec), so a re-probe that finds the
# queue busy again cannot stretch the whole wait past -MaxWaitSec. The share is read up to -ReadAttempts times (see Read-GateCounts) before it is unreadable.
$deadline = $t0.AddSeconds($MaxWaitSec)
$gateEnd = $t0.AddSeconds([Math]::Min($MaxGateSec, $MaxWaitSec))

# Both directory counts, retried on a read error. Results go to $script:GateQueued / $script:GateRunning ($null = still unreadable after the last attempt):
# like GateState they are not the function's output, because its GATE_READ_RETRY lines are written to the pipeline.
function Read-GateCounts([string]$Name) {
    for ($attempt = 1; $true; $attempt++) {
        $script:GateQueued = Get-GateCount "$agentShare\inbox" '*.job.ps1' $true
        $script:GateRunning = Get-GateCount "$agentShare\running" '' $false
        if (($null -ne $script:GateQueued -and $null -ne $script:GateRunning) -or $attempt -ge $ReadAttempts) { return }
        $delay = $ReadBackoffSec * $attempt
        if ((Get-Date).AddSeconds($delay) -gt $deadline) { return }   # no time left for another try: report what is known
        Write-Gate "GATE_READ_RETRY $Name $Venue attempt=$($attempt + 1)/$ReadAttempts inboxReadable=$($null -ne $script:GateQueued) runningReadable=$($null -ne $script:GateRunning) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        if ($delay -gt 0) { Start-Sleep -Seconds $delay }
    }
}

function Wait-QueueGate([string]$Name) {
    while ($true) {
        Read-GateCounts $Name
        $q = $script:GateQueued; $r = $script:GateRunning
        if ($null -eq $q -or $null -eq $r) {
            Write-Gate "GATE_UNREADABLE $Name $Venue share=$agentShare inboxReadable=$($null -ne $q) runningReadable=$($null -ne $r) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
            $script:GateState = 'UNREADABLE'; return
        }
        Write-Gate "GATE $Name $Venue queued=$q running=$r $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        if ($q -eq 0 -and $r -eq 0) { $script:GateState = 'CLEAR'; return }
        $now = Get-Date
        if ($now -gt $gateEnd) { Write-Gate "GATE_BUSY $Name waitedSec=$([int]($now - $t0).TotalSeconds)"; $script:GateState = 'BUSY'; return }
        # (the poll never sleeps past the end of the gate, so the wait ends when the deadline does)
        Start-Sleep -Milliseconds ([int][Math]::Max(10, [Math]::Min($GatePollSec * 1000, ($gateEnd - $now).TotalMilliseconds + 10)))
    }
}

# The probe job: read-only. Prints ONE line VENUE_QUIET=<json>.
$probeText = @'
$ErrorActionPreference = 'Stop'
function Get-TimeSample {
    try {
        $s = (Get-Counter -Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples[0]
        $st = $s.PSObject.Properties['Status']
        if ($null -eq $st -or ($st.Value -ne 0 -and $st.Value -ne 1) -or $null -eq $s.CookedValue) { return $null }
        $v = [double]$s.CookedValue
        if (-not [double]::IsFinite($v) -or $v -lt 0 -or $v -gt 100) { return $null }
        return $v
    } catch { return $null }
}
function Get-CpuSnapshot { $h = @{}; foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) { try { if ($p.TotalProcessorTime) { $h[$p.Id] = @($p.Name, $p.TotalProcessorTime.TotalSeconds) } } catch { } }; $h }
$before = Get-CpuSnapshot
$samples = @()
for ($i = 0; $i -lt 3; $i++) { $samples += ,(Get-TimeSample); if ($i -lt 2) { Start-Sleep -Seconds 12 } }
$after = Get-CpuSnapshot
$top = @($after.Keys | Where-Object { $before.ContainsKey($_) } | ForEach-Object { [pscustomobject]@{ name = $after[$_][0]; pid = $_; cpuSeconds = [math]::Round($after[$_][1] - $before[$_][1], 2) } } |
    Sort-Object cpuSeconds -Descending | Select-Object -First 5)
Write-Output ('VENUE_QUIET=' + ([ordered]@{ schema = 'mlv-app/venue-quiet-probe/v1'; host = $env:COMPUTERNAME; samples = $samples; top = $top } | ConvertTo-Json -Compress -Depth 4))
'@

$last = $null
while ($true) {
    Wait-QueueGate 'quiet-probe'
    if ($script:GateState -ceq 'UNREADABLE') { Write-Gate 'DECISION UNKNOWN mean=UNKNOWN'; exit 4 }
    if ($script:GateState -cne 'CLEAR') { exit 3 }
    $jobId = 'venue-quiet-probe-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $jobFile = Join-Path $WorkDir "$jobId.job.ps1"
    [IO.File]::WriteAllText($jobFile, $probeText, [Text.UTF8Encoding]::new($false))
    $probe = $null; $why = $null
    try {
        $out = @(& $umRun -ScriptPath $jobFile -JobId $jobId -AgentShare $agentShare -TimeoutSec 120 -MaxQueueWaitSec 300 -MaxClaimedWaitSec 120 6>$null)
        $r = if ($out.Count -gt 0) { $out[-1] } else { $null }
        $line = @(([string]$r.stdout) -split "`r?`n" | Where-Object { $_ -like 'VENUE_QUIET=*' }) | Select-Object -Last 1
        if ($line) { $probe = $line.Substring('VENUE_QUIET='.Length) | ConvertFrom-Json } else { $why = "no VENUE_QUIET line (exit $($r.exitCode))" }
    } catch { $why = [string]$_.Exception.Message }
    # HOST ECHO: the probe reports the machine it ran on. An acknowledged alias of ANOTHER host's share (or any share that is not this venue's) would measure that
    # host, so the answer must come from this venue's expectedHost (case-insensitive) before any verdict is printed for it.
    if ($null -ne $probe -and [string]$probe.host -ne $expectedHost) {
        Write-Gate "HOST_MISMATCH $jobId $Venue probe host='$([string]$probe.host)' is not expectedHost '$expectedHost': no verdict is printed for '$Venue' $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        exit 5
    }
    $samples = $(if ($null -ne $probe) { @($probe.samples) } else { @() })
    $d =Get-VenueQuietDecision -Samples $samples -Threshold $ThresholdPercent
    $last = [pscustomobject]@{ decision = $d; probe = $probe }
    $sampleText = $(if ($samples.Count) { ($samples | ForEach-Object { if ($null -eq $_) { 'null' } else { '{0:0.0}' -f [double]$_ } }) -join '/' } else { 'none' })
    Write-Gate "PROBE $jobId $Venue samples=$sampleText mean=$(Format-Mean $d.mean) state=$($d.state)$(if ($why) { " note=$why" }) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
    if ($d.state -ceq 'QUIET') { break }
    if ((Get-Date).AddSeconds($RecheckSec) -gt $deadline) { break }
    Start-Sleep -Seconds $RecheckSec
}
$verdict = $(if ($last.decision.state -ceq 'QUIET') { 'QUIET' } else { 'COOLDOWN_UNMET' })
Write-Gate "$verdict mean=$(Format-Mean $last.decision.mean) threshold=$ThresholdPercent% waitedSec=$([int]((Get-Date) - $t0).TotalSeconds)"
if ($null -ne $last.probe) { foreach ($p in @($last.probe.top)) { Write-Gate ("  top {0} pid={1} cpuSeconds={2}" -f $p.name, $p.pid, $p.cpuSeconds) } }
exit 0
