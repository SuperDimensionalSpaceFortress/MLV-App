# Wait-VenueQuiet.ps1 -- read-only CPU cooldown gate before a venue leg (LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1). RUN THIS ON THE VM.
#
# Two legs that are compared (Classic, then Cinematic) should each start on a venue that is not still busy from the previous one. This gate:
#   1. waits on the read-only queue gate: the venue agent's inbox holds no *.job.ps1 and running\ is empty (GATE_BUSY after -MaxGateSec);
#   2. submits ONE probe job through tools\profiling\um-run.ps1 (the only writer to a venue share), with a unique JobId venue-quiet-probe-<utc>.
#      The job reads 3 samples of \Processor(_Total)\% Processor Time, 12 s apart -- the counter and cadence of the attribution job's own
#      quiescence gate (playback-attr-3-cuda-job.ps1, Get-AttrCudaQuiescenceSample) -- plus the top 5 processes by CPU-seconds over that window;
#      schema v2 (VENUE-QUIET-ATTRIBUTION-1) ADDS attribution to the same job and decides nothing with it: from the same Get-Counter set per sample, the top 10
#      \Process(*)\% Processor Time instances (as percent of the WHOLE machine: counter / logical processors; _Total and Idle excluded from the ranking), the
#      Process _Total / Idle / System values, \Processor(_Total) privileged / user / DPC / interrupt time, and per-core \Processor Information(*) load with
#      P-cores and E-cores apart (core_class unknown where the topology cannot be derived). A counter the host lacks is null with a reason under `notes`.
#      The v1 `samples` and `top` fields are unchanged, and a v1 probe line still parses.
#   3. decides: QUIET when the UNROUNDED mean of the 3 samples is <= -ThresholdPercent (the printed mean is rounded to 0.1 for display only). A
#      failed counter read makes the check UNKNOWN, never quiet.
#   4. re-probes every -RecheckSec until QUIET or -MaxWaitSec, then prints `QUIET mean=<x>` or `COOLDOWN_UNMET mean=<x>` (a MEASURED busy venue) and the top
#      processes, or -- when the FINAL decision is UNKNOWN -- `VENUE_QUIET_UNKNOWN mean=UNKNOWN ... reason=<why>` and exit 6.
# A measured COOLDOWN_UNMET is recorded and the caller proceeds: it decides nothing. VENUE_QUIET_UNKNOWN means the venue was NOT MEASURED (no um-run.ps1, a failed
# submission, a result without a VENUE_QUIET line, or samples that are not 3 finite values in [0, 100]): the caller must fail or skip its leg loudly, never run
# it as if the venue had been measured. The probe never kills, stops or changes any process.
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
# Exit codes: 0 QUIET or a measured COOLDOWN_UNMET (read stdout); 3 GATE_BUSY (the queue never cleared); 4 GATE_UNREADABLE (the agent share could not be
# read -- typed line `GATE_UNREADABLE ...` then `DECISION UNKNOWN`, never an empty-queue line; no probe is submitted); 5 HOST_MISMATCH (the probe was
# answered by a host that is not the venue's); 6 VENUE_QUIET_UNKNOWN (the venue was not measured: typed line `VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=<t>%
# waitedSec=<n> reason=<probe-failed|no-VENUE_QUIET-line|invalid-samples>`, never a QUIET / COOLDOWN_UNMET line); 2 a usage error (including a -SamplesJson that is
# not JSON).
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

# The probe job: read-only. Prints ONE line VENUE_QUIET=<json>, schema v2 (v1 `samples` and `top` are unchanged; v2 only ADDS the attribution fields below).
# Each of the 3 probe samples is ONE Get-Counter call over every counter, so the extra counters cost one read, not one per counter; a counter the host lacks
# is recorded as null with a reason in `notes` and never fails the probe (if the combined call throws, each counter group is read on its own).
# PERCENT BASIS: every \Process(*) percent below is the counter value divided by cpu_count (logical processors), i.e. percent of the WHOLE machine, so it is
# comparable with \Processor(_Total)\% Processor Time (which the gate decides on). All v2 values are means over the sample sets that read cleanly.
$probeText = @'
$ErrorActionPreference = 'Stop'
$cpuCount = [Environment]::ProcessorCount
$groups = [ordered]@{
    processor = @('\Processor(_Total)\% Processor Time', '\Processor(_Total)\% Privileged Time', '\Processor(_Total)\% User Time', '\Processor(_Total)\% DPC Time', '\Processor(_Total)\% Interrupt Time')
    process   = @('\Process(*)\% Processor Time', '\Process(*)\ID Process')
    cores     = @('\Processor Information(*)\% Processor Time')
}
$allPaths = @($groups.Values | ForEach-Object { $_ })
function Get-GoodValue($s) {
    $st = $s.PSObject.Properties['Status']
    if ($null -eq $st -or ($st.Value -ne 0 -and $st.Value -ne 1) -or $null -eq $s.CookedValue) { return $null }
    $v = [double]$s.CookedValue
    if ([double]::IsNaN($v) -or [double]::IsInfinity($v) -or $v -lt 0) { return $null }
    return $v
}
# -ErrorAction SilentlyContinue, not Stop: with Stop, one process that exits mid-sample turns \Process(*) into "data ... not valid" for the WHOLE read (measured: 8 of 8
# reads failed on a busy host). The per-sample Status is checked in Get-GoodValue instead; a missing counter path still throws and is caught by the caller.
function Invoke-Counter($paths) { @((Get-Counter -Counter $paths -ErrorAction SilentlyContinue).CounterSamples) }
function Read-CounterSet {
    $raw = @(); $errs = @{}
    try { $raw = @(Invoke-Counter $allPaths) } catch { }
    if ($raw.Count -eq 0) {
        foreach ($g in $groups.Keys) {
            try { $part = @(Invoke-Counter $groups[$g]); if ($part.Count -eq 0) { $errs[$g] = 'Get-Counter returned no samples' } else { $raw += $part } } catch { $errs[$g] = [string]$_.Exception.Message }
        }
    }
    $set = [pscustomobject]@{ v1 = $null; cpu = @{}; proc = @{}; pids = @{}; core = @{}; errs = $errs }
    foreach ($s in $raw) {
        # the instance key comes from the PATH: CounterSample.InstanceName drops the "#n" suffix, which would merge every chrome#1, chrome#2 ... into one
        $lp = ([string]$s.Path).ToLowerInvariant()
        if ($lp -notmatch '^\\\\[^\\]*\\([^\\(]+)\(') { continue }
        $obj = $Matches[1]; $leaf = $lp.Substring($lp.LastIndexOf('\') + 1)
        $inst = $(if ($lp -match '\((.*)\)\\[^\\]*$') { $Matches[1] } else { '' })
        $val = Get-GoodValue $s
        if ($null -eq $val) { continue }
        if ($obj -eq 'processor') { if ($inst -eq '_total') { $set.cpu[$leaf] = $val } }
        elseif ($obj -eq 'process') {
            if ($leaf -eq '% processor time') { $set.proc[$inst] = $val } elseif ($leaf -eq 'id process') { $set.pids[$inst] = [int64]$val }
        }
        elseif ($obj -eq 'processor information') { if ($leaf -eq '% processor time' -and $inst -notmatch '_total') { $set.core[$inst] = $val } }
    }
    $t = $set.cpu['% processor time']
    if ($null -ne $t -and $t -le 100) { $set.v1 = $t }
    $set
}
function Get-CpuSnapshot { $h = @{}; foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) { try { if ($p.TotalProcessorTime) { $h[$p.Id] = @($p.Name, $p.TotalProcessorTime.TotalSeconds) } } catch { } }; $h }
function Get-Mean($values) { $v = @($values); if ($v.Count -eq 0) { return $null }; [math]::Round(($v | Measure-Object -Average).Average, 2) }
# Core topology: GetLogicalProcessorInformationEx(RelationAll) reports each physical core's EfficiencyClass and its logical processors (a mask per processor group),
# plus the NUMA nodes. The highest class is a P-core, a lower class an E-core; one class only is "uniform". The \Processor Information instance "a,b" is read as
# NUMA node a, processor b within that node when the host has several NUMA nodes in ONE processor group (measured: a 16-vCPU VM shows 0,0-0,7 and 1,0-1,7 while the API
# reports one group), and as processor group a, bit b otherwise; a counter instance the map does not contain is core_class "unknown", never guessed.
# Returns @{ map = @{ 'a,b' = class }; reason = $null } or an empty map with the reason.
function Get-CoreTopology {
    $map = @{}
    try {
        if ([IntPtr]::Size -ne 8) { return @{ map = $map; reason = 'not a 64-bit process' } }
        Add-Type -Namespace MlvQuiet -Name Cpu -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)] public static extern bool GetLogicalProcessorInformationEx(int RelationshipType, System.IntPtr Buffer, ref uint ReturnedLength);'
        $len = [uint32]0
        [void][MlvQuiet.Cpu]::GetLogicalProcessorInformationEx(0xFFFF, [IntPtr]::Zero, [ref]$len)
        if ($len -lt 40) { return @{ map = $map; reason = "GetLogicalProcessorInformationEx reported $len bytes" } }
        $buf = [Runtime.InteropServices.Marshal]::AllocHGlobal([int]$len)
        try {
            if (-not [MlvQuiet.Cpu]::GetLogicalProcessorInformationEx(0xFFFF, $buf, [ref]$len)) { return @{ map = $map; reason = 'GetLogicalProcessorInformationEx failed' } }
            $m = [Runtime.InteropServices.Marshal]
            $byGroupBit = @{}; $nodes = @{}; $groupsSeen = @{}
            $o = 0
            while ($o + 8 -le $len) {
                $size = $m::ReadInt32($buf, $o + 4)
                if ($size -lt 8) { break }
                $rel = $m::ReadInt32($buf, $o)
                if (($rel -eq 0 -or $rel -eq 1) -and $size -ge 48) {
                    $groupCount = [int]$m::ReadInt16($buf, $o + 30); if ($groupCount -lt 1) { $groupCount = 1 }
                    for ($g = 0; $g -lt $groupCount; $g++) {
                        $at = $o + 32 + 16 * $g
                        $mask = $m::ReadInt64($buf, $at); $grp = [int]$m::ReadInt16($buf, $at + 8)
                        for ($bit = 0; $bit -lt 64; $bit++) {
                            if (-not (($mask -shr $bit) -band 1)) { continue }
                            if ($rel -eq 0) { $byGroupBit["$grp,$bit"] = [int]$m::ReadByte($buf, $o + 9); $groupsSeen[$grp] = $true }
                            else { $node = [int]$m::ReadInt32($buf, $o + 8); if (-not $nodes.ContainsKey($node)) { $nodes[$node] = @() }; $nodes[$node] += ,@($grp, $bit) }
                        }
                    }
                }
                $o += $size
            }
            if ($byGroupBit.Count -eq 0) { return @{ map = $map; reason = 'GetLogicalProcessorInformationEx returned no cores' } }
            if ($nodes.Count -gt 1 -and $groupsSeen.Count -eq 1) {
                foreach ($n in $nodes.Keys) {
                    $ord = 0
                    foreach ($gb in @($nodes[$n] | Sort-Object { $_[0] * 1000 + $_[1] })) { $map["$n,$ord"] = $byGroupBit["$($gb[0]),$($gb[1])"]; $ord++ }
                }
            } else { $map = $byGroupBit }
        } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($buf) }
        return @{ map = $map; reason = $null }
    } catch { return @{ map = @{}; reason = [string]$_.Exception.Message } }
}
$before = Get-CpuSnapshot
$samples = @(); $sets = @()
for ($i = 0; $i -lt 3; $i++) { $set = Read-CounterSet; $sets += ,$set; $samples += ,$set.v1; if ($i -lt 2) { Start-Sleep -Seconds 12 } }
$after = Get-CpuSnapshot
$top = @($after.Keys | Where-Object { $before.ContainsKey($_) } | ForEach-Object { [pscustomobject]@{ name = $after[$_][0]; pid = $_; cpuSeconds = [math]::Round($after[$_][1] - $before[$_][1], 2) } } |
    Sort-Object cpuSeconds -Descending | Select-Object -First 5)
$notes = [ordered]@{}
function Get-GroupWhy([string]$Group) {
    $e = @($sets | Where-Object { $_.errs.ContainsKey($Group) } | ForEach-Object { $_.errs[$Group] }) | Select-Object -First 1
    if ($e) { "counter group '$Group' unreadable: $e" } else { "counter group '$Group' returned no valid sample" }
}
# processor-wide breakdown
$cpuNames = [ordered]@{ total_percent = '% processor time'; privileged_percent = '% privileged time'; user_percent = '% user time'; dpc_percent = '% dpc time'; interrupt_percent = '% interrupt time' }
$processor = [ordered]@{}
foreach ($k in $cpuNames.Keys) {
    $processor[$k] = Get-Mean @($sets | Where-Object { $_.cpu.ContainsKey($cpuNames[$k]) } | ForEach-Object { $_.cpu[$cpuNames[$k]] })
    if ($null -eq $processor[$k]) { $notes["processor.$k"] = Get-GroupWhy 'processor' }
}
# per-process, as percent of the whole machine (counter / cpuCount); an instance absent from a clean set counts as 0 in that set
$procSets = @($sets | Where-Object { $_.proc.Count -gt 0 })
$procPct = @{}; $pidOf = @{}
foreach ($set in $procSets) {
    foreach ($k in $set.proc.Keys) { $procPct[$k] = [double]$procPct[$k] + $set.proc[$k] }
    foreach ($k in $set.pids.Keys) { if (-not $pidOf.ContainsKey($k)) { $pidOf[$k] = $set.pids[$k] } elseif ($pidOf[$k] -ne $set.pids[$k]) { $pidOf[$k] = -1 } }
}
foreach ($k in @($procPct.Keys)) { $procPct[$k] = $procPct[$k] / $procSets.Count / $cpuCount }
$pTotal = $null; $pIdle = $null; $pSystem = $null; $ranked = @(); $attributed = $null; $gap = $null
if ($procSets.Count -gt 0) {
    if ($procPct.ContainsKey('_total')) { $pTotal = [math]::Round($procPct['_total'], 2) }
    if ($procPct.ContainsKey('idle')) { $pIdle = [math]::Round($procPct['idle'], 2) }
    if ($procPct.ContainsKey('system')) { $pSystem = [math]::Round($procPct['system'], 2) }
    $ranked = @($procPct.Keys | Where-Object { $_ -ne '_total' -and $_ -ne 'idle' } | Sort-Object { $procPct[$_] } -Descending | Select-Object -First 10 | ForEach-Object {
        [pscustomobject]@{ instance = $_; pid = $(if ($pidOf.ContainsKey($_) -and $pidOf[$_] -ge 0) { $pidOf[$_] } else { $null }); percent = [math]::Round($procPct[$_], 2) } })
    # attributed = the top 10 plus System (counted once if System is already among them)
    $attributed = [math]::Round((@($ranked | ForEach-Object { $_.percent }) + $(if ($null -ne $pSystem -and @($ranked | Where-Object { $_.instance -eq 'system' }).Count -eq 0) { $pSystem } else { @() }) | Measure-Object -Sum).Sum, 2)
    if ($null -ne $processor.total_percent) { $gap = [math]::Round($processor.total_percent - $attributed, 2) }
} else {
    foreach ($f in 'process_total_percent', 'idle_percent', 'system_percent', 'top_processes', 'attributed_percent') { $notes[$f] = Get-GroupWhy 'process' }
}
if ($procSets.Count -gt 0) {
    if ($null -eq $pTotal) { $notes['process_total_percent'] = 'no _total instance in the process counters' }
    if ($null -eq $pIdle) { $notes['idle_percent'] = 'no idle instance in the process counters' }
    if ($null -eq $pSystem) { $notes['system_percent'] = 'no system instance in the process counters' }
}
# per-core, P-cores and E-cores apart
$topo = Get-CoreTopology
$coreSets = @($sets | Where-Object { $_.core.Count -gt 0 })
$coreSum = @{}
foreach ($set in $coreSets) { foreach ($k in $set.core.Keys) { $coreSum[$k] = [double]$coreSum[$k] + $set.core[$k] } }
$classes = @($topo.map.Values | Sort-Object -Unique)
$cores = @(); $classAgg = [ordered]@{}
if ($coreSets.Count -eq 0) { $notes['cores'] = Get-GroupWhy 'cores' }
if ($topo.map.Count -eq 0 -and $topo.reason) { $notes['core_class'] = "core_class unknown: $($topo.reason)" }
foreach ($k in @($coreSum.Keys | Sort-Object { $a = $_ -split ','; ([int]$a[0]) * 100000 + ([int]$a[1]) })) {
    $eff = $(if ($topo.map.ContainsKey($k)) { [int]$topo.map[$k] } else { $null })
    $cls = $(if ($null -eq $eff) { 'unknown' } elseif ($classes.Count -le 1) { 'uniform' } elseif ($eff -eq ($classes | Measure-Object -Maximum).Maximum) { 'P' } else { 'E' })
    $pct = [math]::Round($coreSum[$k] / $coreSets.Count, 2)
    $cores += [pscustomobject]@{ instance = $k; core_class = $cls; efficiency_class = $eff; percent = $pct }
}
foreach ($c in ($cores | Group-Object core_class)) {
    $vals = @($c.Group | ForEach-Object { $_.percent })
    $classAgg[$c.Name] = [ordered]@{ count = $vals.Count; mean_percent = Get-Mean $vals; max_percent = ($vals | Measure-Object -Maximum).Maximum }
}
Write-Output ('VENUE_QUIET=' + ([ordered]@{
    schema = 'mlv-app/venue-quiet-probe/v2'; host = $env:COMPUTERNAME; samples = $samples; top = $top
    cpu_count = $cpuCount
    percent_basis = 'per-process percents are percent of the whole machine (counter / cpu_count)'
    processor = $processor
    process_total_percent = $pTotal; idle_percent = $pIdle; system_percent = $pSystem
    top_processes = $ranked; attributed_percent = $attributed; attribution_gap_points = $gap
    core_topology = $(if ($topo.map.Count -gt 0) { 'logical-processor-information-ex' } else { 'unknown' })
    core_classes = $classAgg; cores = $cores
    notes = $notes
} | ConvertTo-Json -Compress -Depth 6))
'@

# v2 attribution, printed AFTER the verdict and top lines so the gate log says what kept the venue busy. Display only: nothing here feeds the decision, and a
# probe line that is not v2 (or whose v2 fields are malformed) prints nothing / one `attr` note and never changes the verdict or the exit code.
function Format-Pct($v) { if ($null -eq $v) { 'n/a' } else { '{0:0.0}%' -f [double]$v } }
function Write-AttributionLines($Probe) {
    if ([string]$Probe.schema -cne 'mlv-app/venue-quiet-probe/v2') { return }
    try {
        $pr = $Probe.processor
        Write-Gate ("  attr cpus={0} processor total={1} privileged={2} user={3} dpc={4} interrupt={5}" -f $Probe.cpu_count, (Format-Pct $pr.total_percent), (Format-Pct $pr.privileged_percent), (Format-Pct $pr.user_percent), (Format-Pct $pr.dpc_percent), (Format-Pct $pr.interrupt_percent))
        Write-Gate ("  attr process total={0} idle={1} system={2} attributed={3} gapVsProcessorTotal={4}" -f (Format-Pct $Probe.process_total_percent), (Format-Pct $Probe.idle_percent), (Format-Pct $Probe.system_percent), (Format-Pct $Probe.attributed_percent), $(if ($null -eq $Probe.attribution_gap_points) { 'n/a' } else { '{0:0.0} points' -f [double]$Probe.attribution_gap_points }))
        foreach ($p in @($Probe.top_processes)) { Write-Gate ("  attr proc {0} pid={1} pct={2}" -f $p.instance, $(if ($null -eq $p.pid) { 'n/a' } else { $p.pid }), (Format-Pct $p.percent)) }
        $cls = @($Probe.core_classes.PSObject.Properties | ForEach-Object { "{0} n={1} mean={2} max={3}" -f $_.Name, $_.Value.count, (Format-Pct $_.Value.mean_percent), (Format-Pct $_.Value.max_percent) })
        Write-Gate ("  attr cores topology={0} {1}" -f $Probe.core_topology, $(if ($cls.Count) { $cls -join ' | ' } else { 'none' }))
        foreach ($n in @($Probe.notes.PSObject.Properties)) { Write-Gate ("  attr note {0}: {1}" -f $n.Name, $n.Value) }
    } catch { Write-Gate ("  attr UNPRINTABLE {0}" -f $_.Exception.Message) }
}

$last = $null
while ($true) {
    Wait-QueueGate 'quiet-probe'
    if ($script:GateState -ceq 'UNREADABLE') { Write-Gate 'DECISION UNKNOWN mean=UNKNOWN'; exit 4 }
    if ($script:GateState -cne 'CLEAR') { exit 3 }
    $jobId = 'venue-quiet-probe-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $jobFile = Join-Path $WorkDir "$jobId.job.ps1"
    [IO.File]::WriteAllText($jobFile, $probeText, [Text.UTF8Encoding]::new($false))
    $probe = $null; $why = $null; $whyKind = $null
    try {
        $out = @(& $umRun -ScriptPath $jobFile -JobId $jobId -AgentShare $agentShare -TimeoutSec 120 -MaxQueueWaitSec 300 -MaxClaimedWaitSec 120 6>$null)
        $r = if ($out.Count -gt 0) { $out[-1] } else { $null }
        $line = @(([string]$r.stdout) -split "`r?`n" | Where-Object { $_ -like 'VENUE_QUIET=*' }) | Select-Object -Last 1
        if ($line) { $probe = $line.Substring('VENUE_QUIET='.Length) | ConvertFrom-Json } else { $why = "no VENUE_QUIET line (exit $($r.exitCode))"; $whyKind = 'no-VENUE_QUIET-line' }
    } catch { $why = [string]$_.Exception.Message; $whyKind = 'probe-failed' }
    # HOST ECHO: the probe reports the machine it ran on. An acknowledged alias of ANOTHER host's share (or any share that is not this venue's) would measure that
    # host, so the answer must come from this venue's expectedHost (case-insensitive) before any verdict is printed for it.
    if ($null -ne $probe -and [string]$probe.host -ne $expectedHost) {
        Write-Gate "HOST_MISMATCH $jobId $Venue probe host='$([string]$probe.host)' is not expectedHost '$expectedHost': no verdict is printed for '$Venue' $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        exit 5
    }
    $samples = $(if ($null -ne $probe) { @($probe.samples) } else { @() })
    $d =Get-VenueQuietDecision -Samples $samples -Threshold $ThresholdPercent
    $last = [pscustomobject]@{ decision = $d; probe = $probe; why = $why; whyKind = $(if ($whyKind) { $whyKind } else { 'invalid-samples' }) }
    $sampleText = $(if ($samples.Count) { ($samples | ForEach-Object { if ($null -eq $_) { 'null' } else { '{0:0.0}' -f [double]$_ } }) -join '/' } else { 'none' })
    Write-Gate "PROBE $jobId $Venue samples=$sampleText mean=$(Format-Mean $d.mean) state=$($d.state)$(if ($why) { " note=$why" }) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
    if ($d.state -ceq 'QUIET') { break }
    if ((Get-Date).AddSeconds($RecheckSec) -gt $deadline) { break }
    Start-Sleep -Seconds $RecheckSec
}
# UNKNOWN is not a measured busy venue: it gets its own typed verdict and exit code (6), so a caller can never read it as COOLDOWN_UNMET (exit 0, proceed).
if ($last.decision.state -ceq 'UNKNOWN') {
    Write-Gate "VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=$ThresholdPercent% waitedSec=$([int]((Get-Date) - $t0).TotalSeconds) reason=$($last.whyKind)$(if ($last.why) { " note=$($last.why)" })"
    exit 6
}
$verdict = $(if ($last.decision.state -ceq 'QUIET') { 'QUIET' } else { 'COOLDOWN_UNMET' })
Write-Gate "$verdict mean=$(Format-Mean $last.decision.mean) threshold=$ThresholdPercent% waitedSec=$([int]((Get-Date) - $t0).TotalSeconds)"
if ($null -ne $last.probe) { foreach ($p in @($last.probe.top)) { Write-Gate ("  top {0} pid={1} cpuSeconds={2}" -f $p.name, $p.pid, $p.cpuSeconds) } }
if ($null -ne $last.probe) { Write-AttributionLines $last.probe }
exit 0
