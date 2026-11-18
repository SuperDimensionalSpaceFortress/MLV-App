<#
.SYNOPSIS
  The READ-ONLY evaluator of one heavy-slot A/B window (HEAVY-SLOT-SECOND-SLOT-MEASURED-1): reads the driver's files in
  -WindowDir plus the HeavySlot receipts and ledger and writes <WindowDir>\ab-verdict.json (and ab-headroom-summary.json).

.DESCRIPTION
  Inputs: <WindowDir>\ab-blocks.jsonl, ab-samples.jsonl, ab-receipt.json (driver); <HeavySlotDir>\receipts\*.json and ledger.jsonl.
  It never calls heavy-slot's reporting verb (that verb sweeps live waiters), never opens a heavy-slot mutex and never writes under the HeavySlot dir.

  DEAD HOLDERS are excluded from every wait and hold figure and are COUNTED: an admit that has no exit / tool-exit row by
  window end + 90 min, or a `reaped kind=held` ledger row (hub ruling 2026-11-18T00:10:38Z, condition a). Before window end
  + 90 min an admit with no exit row yet is `pending` (excluded, counted, evaluatedEarly=true).
  ATTRIBUTION: each ticket goes to the block it ENQUEUED in. A ticket whose wait crosses a block edge (enqueued in one block,
  admitted in another) is reported as SPILL and left out of the primary comparison. A ticket that timed out waiting (tool-exit
  75, never admitted) is a wait that happened: it is counted in the wait figures of its arm (never in the hold figures).
  REPORT, A vs B, overall and by class (label suffix build / pytest): n, wait_s p50/p90/p99, hold_s p50. Percentiles are
  nearest-rank (ceil(p/100 * n)-th smallest). From ab-samples, split by live holders (0, 1, 2+): temp_c_p95, temp_ge90_frac,
  freq_p50 under load (cpu >= 50 %), min free commit, minutes.

  VERDICT (every threshold is printed in the json):
    ABORTED       carried through from ab-receipt.json state ABORTED (reason copied).
    INCONCLUSIVE  no receipt; fewer than 20 live closed tickets per arm; fewer than 10 builds per arm; fewer than 30 min of
                  holders >= 2 time; A shows no queueing (A wait_p90 = 0); or no under-load frequency sample in either group.
    ADOPT         only if ALL hold: B wait_p90 <= 0.75 x A; B build wait_p90 <= 1.10 x A; B build hold_p50 <= 1.15 x A;
                  at holders >= 2 temp_ge90_frac <= 0.20 and temp_c_p95 <= 94; at holders >= 2 under load freq_p50 >= 0.90 x the
                  holders = 1 figure; no Event 2004; min free commit (all B samples) >= 10 GB.
    REJECT        otherwise.
  Exit: 0 always when a verdict was written (the verdict is the answer); 2 when the window dir has no usable input.
  ASCII-only on purpose.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WindowDir,
    [string]$HeavySlotDir = 'C:\ProgramData\HeavySlot',
    [string]$HostHeadroomScript = '',
    [switch]$SkipHeadroom,
    [string]$EventProbeFile = '',       # test seam: a file holding the number of System Event 2004 rows in the window
    [DateTime]$Now = [DateTime]::UtcNow,
    [int]$MinTicketsPerArm = 20,
    [int]$MinBuildsPerArm = 10,
    [double]$MinHolders2Minutes = 30,
    [double]$WaitP90Ratio = 0.75,
    [double]$BuildWaitP90Ratio = 1.10,
    [double]$BuildHoldP50Ratio = 1.15,
    [double]$MaxGe90Frac = 0.20,
    [double]$MaxTempP95 = 94,
    [double]$MinFreqRatio = 0.90,
    [double]$MinFreeCommitGB = 10,
    [double]$LoadCpuPct = 50,
    [double]$DeadGraceMinutes = 90
)

$ErrorActionPreference = 'Stop'
$inv = [Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding($false)

function ConvertTo-UtcTime($v) {
    if ($null -eq $v -or "$v" -eq '') { return $null }
    if ($v -is [DateTime]) { return $v.ToUniversalTime() }
    if ($v -is [DateTimeOffset]) { return $v.UtcDateTime }
    try { return [DateTimeOffset]::Parse([string]$v, $inv, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime } catch { return $null }
}
function Read-JsonLines([string]$Path) {
    $out = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $out.ToArray() }
    foreach ($l in [IO.File]::ReadAllLines($Path)) {
        if (-not $l.Trim()) { continue }
        try { $out.Add(($l | ConvertFrom-Json)) } catch { }
    }
    return $out.ToArray()
}
function Get-Pct([double[]]$Values, [double]$P) {
    # nearest-rank percentile; $null for an empty list
    $v = @($Values | Sort-Object)
    if ($v.Count -eq 0) { return $null }
    $rank = [int][Math]::Ceiling($P / 100.0 * $v.Count)
    if ($rank -lt 1) { $rank = 1 }
    return [Math]::Round([double]$v[$rank - 1], 3)
}
function Get-Stat([double[]]$Waits, [double[]]$Holds) {
    return [ordered]@{
        n = @($Waits).Count; n_closed = @($Holds).Count
        wait_s_p50 = (Get-Pct $Waits 50); wait_s_p90 = (Get-Pct $Waits 90); wait_s_p99 = (Get-Pct $Waits 99)
        hold_s_p50 = (Get-Pct $Holds 50)
    }
}

$blocks = @(Read-JsonLines (Join-Path $WindowDir 'ab-blocks.jsonl'))
$samples = @(Read-JsonLines (Join-Path $WindowDir 'ab-samples.jsonl'))
$receiptPath = Join-Path $WindowDir 'ab-receipt.json'
$driverReceipt = $null
if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { try { $driverReceipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json } catch { $driverReceipt = $null } }
if ($blocks.Count -eq 0) { Write-Output 'AB_EVAL_NO_INPUT no ab-blocks.jsonl rows'; exit 2 }

$winStart = ConvertTo-UtcTime $blocks[0].windowStartUtc
$winPlannedEnd = ConvertTo-UtcTime $blocks[0].windowEndUtc
$winEnd = $null
if ($driverReceipt) { $winEnd = ConvertTo-UtcTime $driverReceipt.endedUtc }
if (-not $winEnd -and $samples.Count -gt 0) { $winEnd = ConvertTo-UtcTime $samples[-1].utc }
if (-not $winEnd) { $winEnd = $winPlannedEnd }
$blockList = @(foreach ($b in $blocks) { [pscustomobject]@{ index = [int]$b.block; state = [string]$b.state; start = (ConvertTo-UtcTime $b.startUtc); end = (ConvertTo-UtcTime $b.endUtc) } })
function Get-BlockOf($t) {
    if ($null -eq $t -or $t -lt $winStart -or $t -ge $winEnd) { return $null }
    foreach ($b in $blockList) { if ($t -ge $b.start -and $t -lt $b.end) { return $b } }
    return $null
}

# ---- HeavySlot ledger + receipts (read-only) ------------------------------------------------------------------------------------
$ledger = @(Read-JsonLines (Join-Path $HeavySlotDir 'ledger.jsonl'))
$exited = @{}
$toolExit = @{}
$reapedHeld = @{}
$admits = New-Object System.Collections.Generic.List[object]
foreach ($e in $ledger) {
    $at = ConvertTo-UtcTime $e.at
    if ($null -eq $at) { continue }
    switch ([string]$e.event) {
        'exit' { $exited[[string]$e.ticket] = $at }
        'tool-exit' { $toolExit[[string]$e.ticket] = $e }
        'reaped' { if ([string]$e.kind -eq 'held') { $reapedHeld[[string]$e.ticket] = $at } }
        'admit' { if ($at -ge $winStart -and $at -le $winEnd) { $admits.Add([pscustomobject]@{ ticket = [string]$e.ticket; label = [string]$e.label; at = $at }) } }
    }
}
$deadline = $winEnd.AddMinutes($DeadGraceMinutes)
$evaluatedEarly = ($Now.ToUniversalTime() -lt $deadline)
$dead = @{}
$pending = @{}
foreach ($a in $admits.ToArray()) {
    $t = $a.ticket
    $hasExit = ($exited.ContainsKey($t) -or $toolExit.ContainsKey($t))
    if ($reapedHeld.ContainsKey($t)) { $dead[$t] = $a.label; continue }
    if ($hasExit) { continue }
    if ($evaluatedEarly) { $pending[$t] = $a.label } else { $dead[$t] = $a.label }
}

$rdir = Join-Path $HeavySlotDir 'receipts'
$tickets = New-Object System.Collections.Generic.List[object]
if (Test-Path -LiteralPath $rdir -PathType Container) {
    foreach ($f in [IO.Directory]::GetFiles($rdir, '*.json')) {
        try { $r = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json } catch { continue }
        $enq = ConvertTo-UtcTime $r.enqueuedAt
        if ($null -eq $enq -or $enq -lt $winStart -or $enq -ge $winEnd) { continue }
        $tickets.Add([pscustomobject]@{
                ticket = [string]$r.ticket; label = [string]$r.label; enq = $enq; adm = (ConvertTo-UtcTime $r.admittedAt); fin = (ConvertTo-UtcTime $r.finishedAt)
                waitedMs = $r.waitedMs; hasExit = ($null -ne $r.exit); slotCount = $r.slotCount; slots = @($r.slots)
            })
    }
}

$cls = { param($label) if ($label -match ':build$') { 'build' } elseif ($label -match ':pytest$') { 'pytest' } else { 'other' } }
$arm = @{ A = @{ all = @{ w = @(); h = @() }; build = @{ w = @(); h = @() }; pytest = @{ w = @(); h = @() } }; B = @{ all = @{ w = @(); h = @() }; build = @{ w = @(); h = @() }; pytest = @{ w = @(); h = @() } } }
$spill = New-Object System.Collections.Generic.List[object]
$outside = 0
$excludedDead = 0
$excludedPending = 0
$timedOut = @{ A = 0; B = 0 }
$bPytestCount2 = 0
$bPytestN = 0
$holdCrossEdge = 0
foreach ($k in $tickets.ToArray()) {
    if ($dead.ContainsKey($k.ticket)) { $excludedDead++; continue }
    if ($pending.ContainsKey($k.ticket)) { $excludedPending++; continue }
    $be = Get-BlockOf $k.enq
    if (-not $be) { $outside++; continue }
    $c = & $cls $k.label
    $admitted = ($null -ne $k.adm -and $null -ne $k.fin -and $k.hasExit)
    if ($admitted) {
        $ba = Get-BlockOf $k.adm
        if (-not $ba -or $ba.index -ne $be.index) { $spill.Add([pscustomobject]@{ ticket = $k.ticket; label = $k.label; enqueuedBlock = $be.index; admittedBlock = $(if ($ba) { $ba.index } else { $null }) }); continue }
        $wait = if ($null -ne $k.waitedMs) { [double]$k.waitedMs / 1000.0 } else { ($k.adm - $k.enq).TotalSeconds }
        $hold = ($k.fin - $k.adm).TotalSeconds
        $bf = Get-BlockOf $k.fin
        if (-not $bf -or $bf.index -ne $be.index) { $holdCrossEdge++ }
        $arm[$be.state].all.w += $wait; $arm[$be.state].all.h += $hold
        if ($c -ne 'other') { $arm[$be.state][$c].w += $wait; $arm[$be.state][$c].h += $hold }
        if ($be.state -eq 'B' -and $c -eq 'pytest') { $bPytestN++; if ([string]$k.slotCount -eq '2') { $bPytestCount2++ } }
    }
    elseif ($toolExit.ContainsKey($k.ticket) -and [string]$toolExit[$k.ticket].code -eq '75') {
        $tAt = ConvertTo-UtcTime $toolExit[$k.ticket].at
        $wait = [Math]::Max(0.0, ($tAt - $k.enq).TotalSeconds)
        $arm[$be.state].all.w += $wait
        if ($c -ne 'other') { $arm[$be.state][$c].w += $wait }
        $timedOut[$be.state]++
    }
}

$armStats = [ordered]@{}
foreach ($s in 'A', 'B') {
    $armStats[$s] = [ordered]@{
        overall = (Get-Stat $arm[$s].all.w $arm[$s].all.h)
        build = (Get-Stat $arm[$s].build.w $arm[$s].build.h)
        pytest = (Get-Stat $arm[$s].pytest.w $arm[$s].pytest.h)
        timed_out_waiting = $timedOut[$s]
    }
}

# ---- samples, split by live holders --------------------------------------------------------------------------------------------
$pollGuess = if ($driverReceipt -and $driverReceipt.pollSeconds) { [double]$driverReceipt.pollSeconds } else { 15.0 }
$groups = @{ '0' = New-Object System.Collections.Generic.List[object]; '1' = New-Object System.Collections.Generic.List[object]; '2' = New-Object System.Collections.Generic.List[object] }
$allB = New-Object System.Collections.Generic.List[object]
for ($i = 0; $i -lt $samples.Count; $i++) {
    $s = $samples[$i]
    $t = ConvertTo-UtcTime $s.utc
    $next = if ($i + 1 -lt $samples.Count) { ConvertTo-UtcTime $samples[$i + 1].utc } else { $null }
    $dt = if ($next) { [Math]::Min(($next - $t).TotalSeconds, 2 * $pollGuess) } else { $pollGuess }
    $h = [int]$s.holders
    $key = if ($h -ge 2) { '2' } elseif ($h -eq 1) { '1' } else { '0' }
    $row = [pscustomobject]@{ dt = [Math]::Max(0.0, $dt); temp = $s.temp_c; cpu = $s.cpu_pct; freq = $s.freq_mhz; commit = $s.free_commit_gb; state = [string]$s.state }
    $groups[$key].Add($row)
    if ($row.state -eq 'B') { $allB.Add($row) }
}
function Get-Nums($rows, [string]$Prop) { return @($rows | Where-Object { $null -ne $_.$Prop } | ForEach-Object { [double]$_.$Prop }) }
$sampleStats = [ordered]@{}
foreach ($key in '0', '1', '2') {
    $rows = $groups[$key].ToArray()
    $temps = @(Get-Nums $rows 'temp')
    $load = @($rows | Where-Object { $null -ne $_.cpu -and [double]$_.cpu -ge $LoadCpuPct })
    $commits = @(Get-Nums $rows 'commit')
    $ge90 = @($temps | Where-Object { $_ -ge 90 }).Count
    $sampleStats[$key] = [ordered]@{
        samples = $rows.Count
        minutes = [Math]::Round((($rows | Measure-Object -Property dt -Sum).Sum) / 60.0, 2)
        temp_c_p95 = (Get-Pct $temps 95)
        temp_ge90_frac = $(if ($temps.Count -gt 0) { [Math]::Round($ge90 / $temps.Count, 4) } else { $null })
        under_load_samples = $load.Count
        freq_p50_under_load = (Get-Pct @(Get-Nums $load 'freq') 50)
        min_free_commit_gb = $(if ($commits.Count -gt 0) { ($commits | Measure-Object -Minimum).Minimum } else { $null })
    }
}
$minCommitB = $null
$cB = @(Get-Nums $allB.ToArray() 'commit')
if ($cB.Count -gt 0) { $minCommitB = ($cB | Measure-Object -Minimum).Minimum }

# ---- Event 2004 -----------------------------------------------------------------------------------------------------------------
$ev = 0
if ($EventProbeFile) { try { $ev = [int](Get-Content -LiteralPath $EventProbeFile -Raw).Trim() } catch { $ev = 0 } }
else {
    try { $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 2004; StartTime = $winStart.ToLocalTime(); EndTime = $winEnd.ToLocalTime() } -MaxEvents 5 -ErrorAction SilentlyContinue).Count } catch { $ev = 0 }
}

# ---- verdict ----------------------------------------------------------------------------------------------------------------------
$thresholds = [ordered]@{
    min_live_closed_tickets_per_arm = $MinTicketsPerArm; min_builds_per_arm = $MinBuildsPerArm; min_holders2_minutes = $MinHolders2Minutes
    b_wait_p90_max_ratio_of_a = $WaitP90Ratio; b_build_wait_p90_max_ratio_of_a = $BuildWaitP90Ratio; b_build_hold_p50_max_ratio_of_a = $BuildHoldP50Ratio
    holders2_temp_ge90_frac_max = $MaxGe90Frac; holders2_temp_c_p95_max = $MaxTempP95; holders2_freq_p50_min_ratio_of_holders1 = $MinFreqRatio
    min_free_commit_gb = $MinFreeCommitGB; under_load_cpu_pct = $LoadCpuPct; dead_holder_grace_minutes = $DeadGraceMinutes
}
$A = $armStats['A']; $B = $armStats['B']
$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Name, $Value, [string]$Limit, $Pass) { $checks.Add([ordered]@{ name = $Name; value = $Value; limit = $Limit; pass = $Pass }) }
$inconclusive = New-Object System.Collections.Generic.List[string]
$verdict = ''
$reason = ''
if ($driverReceipt -and [string]$driverReceipt.state -eq 'ABORTED') { $verdict = 'ABORTED'; $reason = [string]$driverReceipt.reason }
else {
    if (-not $driverReceipt) { $inconclusive.Add('no ab-receipt.json (the driver never finished)') }
    elseif ([string]$driverReceipt.state -ne 'DONE') { $inconclusive.Add("receipt state $($driverReceipt.state)") }
    if ($A.overall.n_closed -lt $MinTicketsPerArm -or $B.overall.n_closed -lt $MinTicketsPerArm) { $inconclusive.Add("live closed tickets per arm A=$($A.overall.n_closed) B=$($B.overall.n_closed) (need $MinTicketsPerArm each)") }
    if ($A.build.n_closed -lt $MinBuildsPerArm -or $B.build.n_closed -lt $MinBuildsPerArm) { $inconclusive.Add("builds per arm A=$($A.build.n_closed) B=$($B.build.n_closed) (need $MinBuildsPerArm each)") }
    if ([double]$sampleStats['2'].minutes -lt $MinHolders2Minutes) { $inconclusive.Add("holders>=2 time $($sampleStats['2'].minutes) min (need $MinHolders2Minutes)") }
    if ($null -ne $A.overall.wait_s_p90 -and [double]$A.overall.wait_s_p90 -eq 0) { $inconclusive.Add('A arm shows no queueing (wait_p90 = 0), nothing to reduce') }
    if ($null -eq $sampleStats['2'].freq_p50_under_load -or $null -eq $sampleStats['1'].freq_p50_under_load) { $inconclusive.Add('no under-load frequency sample at holders=1 or holders>=2') }
    if ($inconclusive.Count -gt 0) { $verdict = 'INCONCLUSIVE'; $reason = ($inconclusive -join '; ') }
    else {
        $aw = [double]$A.overall.wait_s_p90; $bw = [double]$B.overall.wait_s_p90
        Add-Check 'overall_wait_p90_b_vs_a' $bw ("<= {0} x {1} = {2}" -f $WaitP90Ratio, $aw, ($WaitP90Ratio * $aw)) ($bw -le $WaitP90Ratio * $aw)
        $abw = [double]$A.build.wait_s_p90; $bbw = [double]$B.build.wait_s_p90
        Add-Check 'build_wait_p90_b_vs_a' $bbw ("<= {0} x {1} = {2}" -f $BuildWaitP90Ratio, $abw, ($BuildWaitP90Ratio * $abw)) ($bbw -le $BuildWaitP90Ratio * $abw)
        $abh = [double]$A.build.hold_s_p50; $bbh = [double]$B.build.hold_s_p50
        Add-Check 'build_hold_p50_b_vs_a' $bbh ("<= {0} x {1} = {2}" -f $BuildHoldP50Ratio, $abh, ($BuildHoldP50Ratio * $abh)) ($bbh -le $BuildHoldP50Ratio * $abh)
        $s2 = $sampleStats['2']; $s1 = $sampleStats['1']
        Add-Check 'holders2_temp_ge90_frac' $s2.temp_ge90_frac "<= $MaxGe90Frac" ($null -ne $s2.temp_ge90_frac -and [double]$s2.temp_ge90_frac -le $MaxGe90Frac)
        Add-Check 'holders2_temp_c_p95' $s2.temp_c_p95 "<= $MaxTempP95" ($null -ne $s2.temp_c_p95 -and [double]$s2.temp_c_p95 -le $MaxTempP95)
        $f2 = [double]$s2.freq_p50_under_load; $f1 = [double]$s1.freq_p50_under_load
        Add-Check 'holders2_freq_p50_under_load_vs_holders1' $f2 (">= {0} x {1} = {2}" -f $MinFreqRatio, $f1, ($MinFreqRatio * $f1)) ($f2 -ge $MinFreqRatio * $f1)
        Add-Check 'no_event_2004' $ev '== 0' ($ev -eq 0)
        Add-Check 'min_free_commit_gb_in_b' $minCommitB ">= $MinFreeCommitGB" ($null -ne $minCommitB -and [double]$minCommitB -ge $MinFreeCommitGB)
        $failed = @($checks.ToArray() | Where-Object { -not $_.pass })
        if ($failed.Count -eq 0) { $verdict = 'ADOPT'; $reason = 'every adoption threshold held' }
        else { $verdict = 'REJECT'; $reason = ('failed: ' + (($failed | ForEach-Object { $_.name }) -join ', ')) }
    }
}

$out = [ordered]@{
    schema = 'heavy-slot-ab-verdict.v1'; verdict = $verdict; reason = $reason; evaluatedUtc = $Now.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', $inv)
    window = [ordered]@{ startUtc = $winStart.ToString('yyyy-MM-ddTHH:mm:ss.fffZ', $inv); endUtc = $winEnd.ToString('yyyy-MM-ddTHH:mm:ss.fffZ', $inv); blocks = $blockList.Count; driverState = $(if ($driverReceipt) { [string]$driverReceipt.state } else { $null }); evaluatedEarly = $evaluatedEarly }
    thresholds = $thresholds
    dead_holders = [ordered]@{ count = $dead.Count; excluded_receipts = $excludedDead; pending_before_grace = $pending.Count; tickets = @($dead.Keys | Sort-Object) }
    spill = [ordered]@{ count = $spill.Count; tickets = $spill.ToArray() }
    outside_window_or_block = $outside
    hold_crosses_block_edge = $holdCrossEdge
    b_pytest = [ordered]@{ tickets = $bPytestN; ran_at_slotcount_2 = $bPytestCount2 }
    event_2004 = $ev
    arms = $armStats
    samples_by_live_holders = [ordered]@{ holders_0 = $sampleStats['0']; holders_1 = $sampleStats['1']; holders_2_plus = $sampleStats['2'] }
    min_free_commit_gb_in_b = $minCommitB
    checks = $checks.ToArray()
}
[IO.File]::WriteAllText((Join-Path $WindowDir 'ab-verdict.json'), (($out | ConvertTo-Json -Depth 8) + "`n"), $utf8)

if (-not $SkipHeadroom) {
    $hh = if ($HostHeadroomScript) { $HostHeadroomScript } else { Join-Path $env:USERPROFILE '.claude\machine\host-headroom\Run-HostHeadroom.ps1' }
    try {
        $hs = @(& pwsh -NoProfile -File $hh -Mode Summary 2>&1 | ForEach-Object { "$_" })
        [IO.File]::WriteAllText((Join-Path $WindowDir 'ab-headroom-summary.json'), (($hs -join "`n") + "`n"), $utf8)
    }
    catch { [IO.File]::WriteAllText((Join-Path $WindowDir 'ab-headroom-summary.json'), ('{"error":"' + ($_.Exception.Message -replace '"', "'") + '"}' + "`n"), $utf8) }
}
Write-Output ("AB_VERDICT {0} reason={1}" -f $verdict, $reason)
exit 0
