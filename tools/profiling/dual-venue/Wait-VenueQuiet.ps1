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
#   pwsh -NoProfile -File tools\profiling\dual-venue\Wait-VenueQuiet.ps1 -Venue bachelor -WorkDir <dir> [-GateLog <file>]
#   pwsh -NoProfile -File tools\profiling\dual-venue\Wait-VenueQuiet.ps1 -SamplesJson '[12.5, 18.0, 21.0]'     # offline: the decision only
#
# -AgentShare overrides the venue table's share (tests; a share that is not the venue's own is the caller's responsibility).
# Exit codes: 0 QUIET or COOLDOWN_UNMET (read stdout); 3 GATE_BUSY (the queue never cleared); 4 GATE_UNREADABLE (the agent share could not be
# read -- typed line `GATE_UNREADABLE ...` then `DECISION UNKNOWN`, never an empty-queue line; no probe is submitted); 2 a usage error.
[CmdletBinding(DefaultParameterSetName = 'Live')]
param(
    [Parameter(ParameterSetName = 'Live')][ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'bachelor',
    [Parameter(ParameterSetName = 'Live', Mandatory = $true)][string]$WorkDir,
    [Parameter(ParameterSetName = 'Live')][string]$GateLog = '',
    [Parameter(ParameterSetName = 'Live')][string]$AgentShare = '',
    [Parameter(ParameterSetName = 'Live')][int]$RecheckSec = 90,
    [Parameter(ParameterSetName = 'Live')][int]$MaxWaitSec = 1800,
    [Parameter(ParameterSetName = 'Live')][int]$MaxGateSec = 2400,
    [Parameter(ParameterSetName = 'Offline', Mandatory = $true)][string]$SamplesJson,
    [double]$ThresholdPercent = 20.0
)
$ErrorActionPreference = 'Stop'

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
    $samples = @(ConvertFrom-Json -InputObject $SamplesJson -NoEnumerate)
    if ($samples.Count -eq 1 -and $samples[0] -is [array]) { $samples = @($samples[0]) }
    $d = Get-VenueQuietDecision -Samples $samples -Threshold $ThresholdPercent
    Write-Output "DECISION $($d.state) mean=$(Format-Mean $d.mean)"
    exit 0
}

$here = $PSScriptRoot
$table = [IO.File]::ReadAllText((Join-Path $here 'venues.json')) | ConvertFrom-Json
$agentShare = $(if ($AgentShare) { $AgentShare } else { [string]$table.venues.$Venue.agentShare })
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
function Wait-QueueGate([string]$Name) {
    $t0 = Get-Date
    while ($true) {
        $q = Get-GateCount "$agentShare\inbox" '*.job.ps1' $true
        $r = Get-GateCount "$agentShare\running" '' $false
        if ($null -eq $q -or $null -eq $r) {
            Write-Gate "GATE_UNREADABLE $Name $Venue share=$agentShare inboxReadable=$($null -ne $q) runningReadable=$($null -ne $r) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
            $script:GateState = 'UNREADABLE'; return
        }
        Write-Gate "GATE $Name $Venue queued=$q running=$r $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        if ($q -eq 0 -and $r -eq 0) { $script:GateState = 'CLEAR'; return }
        if (((Get-Date) - $t0).TotalSeconds -gt $MaxGateSec) { Write-Gate "GATE_BUSY $Name"; $script:GateState = 'BUSY'; return }
        Start-Sleep -Seconds 30
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

$t0 = Get-Date
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
    $samples = $(if ($null -ne $probe) { @($probe.samples) } else { @() })
    $d = Get-VenueQuietDecision -Samples $samples -Threshold $ThresholdPercent
    $last = [pscustomobject]@{ decision = $d; probe = $probe }
    $sampleText = $(if ($samples.Count) { ($samples | ForEach-Object { if ($null -eq $_) { 'null' } else { '{0:0.0}' -f [double]$_ } }) -join '/' } else { 'none' })
    Write-Gate "PROBE $jobId $Venue samples=$sampleText mean=$(Format-Mean $d.mean) state=$($d.state)$(if ($why) { " note=$why" }) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
    if ($d.state -ceq 'QUIET') { break }
    if (((Get-Date) - $t0).TotalSeconds + $RecheckSec -gt $MaxWaitSec) { break }
    Start-Sleep -Seconds $RecheckSec
}
$verdict = $(if ($last.decision.state -ceq 'QUIET') { 'QUIET' } else { 'COOLDOWN_UNMET' })
Write-Gate "$verdict mean=$(Format-Mean $last.decision.mean) threshold=$ThresholdPercent% waitedSec=$([int]((Get-Date) - $t0).TotalSeconds)"
if ($null -ne $last.probe) { foreach ($p in @($last.probe.top)) { Write-Gate ("  top {0} pid={1} cpuSeconds={2}" -f $p.name, $p.pid, $p.cpuSeconds) } }
exit 0
