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

  PER HOLD (hub ruling b): <WindowDir>\ab-holds.jsonl carries one row per live closed hold (wait_s, hold_s, the peak live-holder count over
  its samples, hottest / mean package temp, median frequency) and the verdict's holds_by_holders splits them (per arm and overall) into
  holders_1 (ran alone), holders_2_plus (overlapped) and holders_unknown (no sample). One directory is one window: a directory whose
  blocks file holds two windows, or whose receipt names another window, is INCONCLUSIVE; samples outside the window are ignored; an exit
  that landed after window end + grace is a dead holder; a System Event 2004 count that cannot be read is never read as zero.

  VERDICT (every threshold is printed in the json):
    ABORTED       carried through from ab-receipt.json state ABORTED (reason copied).
    INCONCLUSIVE  no receipt; fewer than 20 live closed tickets per arm; fewer than 10 builds per arm; fewer than 30 min of
                  holders >= 2 time; A shows no queueing (A wait_p90 = 0); or no under-load frequency sample in either group.
    ADOPT         only if ALL hold: B wait_p90 <= 0.75 x A; B build wait_p90 <= 1.10 x A; B build hold_p50 <= 1.15 x A;
                  at holders >= 2 temp_ge90_frac <= 0.20 and temp_c_p95 <= 94; at holders >= 2 under load freq_p50 >= 0.90 x the
                  holders = 1 figure; no Event 2004; min free commit (all B samples) >= 10 GB.
    REJECT        otherwise.
  ROUND 4: every threshold is compared against the EXACT figure (rounding is for printing only); a B sample with no free-commit reading, a
  receipt written during the window that cannot be read, and fewer than 10 B pytest tickets that really ran at slotCount 2 are each
  INCONCLUSIVE; a `reaped kind=held` row excludes its receipt even with no admit row; a never-admitted wait cut off in another block is
  SPILL; a holders value that is not a finite whole number 0..1000 is a typed skip; every unparseable input line is counted in
  input_problems; containment checks resolve junctions and short names.
  Exit: 0 always when a verdict was written (the verdict is the answer); 2 when the window dir has no usable input.
  ASCII-only on purpose.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WindowDir,
    [string]$HeavySlotDir = 'C:\ProgramData\HeavySlot',
    [string]$HostHeadroomScript = '',
    [switch]$SkipHeadroom,
    [switch]$WaitForGrace,              # wait until the driver's endedUtc + DeadGraceMinutes before reading anything (the hub's launcher uses it)
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
    [double]$DeadGraceMinutes = 90,
    [int]$MinBPytestAtSlotCount2 = 10    # ADOPT needs at least this many B pytest tickets whose receipt says they really ran at slotCount 2
)

$ErrorActionPreference = 'Stop'
Import-Module -Force (Join-Path $PSScriptRoot 'HeavySlotAb.psm1')    # Resolve-HeavySlotAbRealPath
$inv = [Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding($false)

function ConvertTo-UtcTime($v) {
    if ($null -eq $v -or "$v" -eq '') { return $null }
    if ($v -is [DateTime]) { return $v.ToUniversalTime() }
    if ($v -is [DateTimeOffset]) { return $v.UtcDateTime }
    try { return [DateTimeOffset]::Parse([string]$v, $inv, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime } catch { return $null }
}
# Any unexpected terminating error ends as a typed line and exit 3, never as a bare stack trace (adversary r2 A6).
trap { Write-Output ("AB_EVAL_ERROR {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message); exit 3 }

function ConvertTo-DoubleOrNull($v) {
    if ($null -eq $v -or "$v" -eq '') { return $null }
    $d = 0.0
    if ([double]::TryParse([string]$v, [Globalization.NumberStyles]::Float, $inv, [ref]$d)) { return $d }
    return $null
}
function Test-PathInside([string]$Child, [string]$Parent) {
    # junctions, symlinks and short names are resolved to the real path first, so an alias into the HeavySlot dir cannot slip past (adversary r3 A1)
    $c = (Resolve-HeavySlotAbRealPath $Child).TrimEnd('\', '/')
    $p = (Resolve-HeavySlotAbRealPath $Parent).TrimEnd('\', '/')
    return ($c -ieq $p) -or $c.StartsWith($p + '\', [StringComparison]::OrdinalIgnoreCase)
}
# This script is read-only against HeavySlot: the verdict, holds log and headroom summary go under -WindowDir, which therefore may
# never be (or lie under) the HeavySlot dir (adversary r2 A7).
if (Test-PathInside $WindowDir $HeavySlotDir) { Write-Output "AB_EVAL_REFUSED path-overlap (the window dir $WindowDir is, or lies under, the HeavySlot dir $HeavySlotDir; this evaluator never writes there)"; exit 2 }

# Every input line that does not parse is COUNTED per file name, never silently dropped: the verdict prints the counts (input_problems).
$script:badLines = @{}
function Read-JsonLines([string]$Path) {
    $out = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $out.ToArray() }
    $name = [IO.Path]::GetFileName($Path)
    foreach ($l in [IO.File]::ReadAllLines($Path)) {
        if (-not $l.Trim()) { continue }
        try { $out.Add(($l | ConvertFrom-Json)) } catch { $script:badLines[$name] = [int]$script:badLines[$name] + 1 }
    }
    return $out.ToArray()
}
function Get-Pct([double[]]$Values, [double]$P) {
    # nearest-rank percentile (an element of the data, so no arithmetic happens); $null for an empty list. NOT rounded: a threshold is
    # compared against the exact value and only printed text is ever rounded (adversary r3 A8).
    $v = @($Values | Sort-Object)
    if ($v.Count -eq 0) { return $null }
    $rank = [int][Math]::Ceiling($P / 100.0 * $v.Count)
    if ($rank -lt 1) { $rank = 1 }
    return [double]$v[$rank - 1]
}
function Get-Stat([double[]]$Waits, [double[]]$Holds) {
    return [ordered]@{
        n = @($Waits).Count; n_closed = @($Holds).Count
        wait_s_p50 = (Get-Pct $Waits 50); wait_s_p90 = (Get-Pct $Waits 90); wait_s_p99 = (Get-Pct $Waits 99)
        hold_s_p50 = (Get-Pct $Holds 50)
    }
}

function Write-EarlyVerdict([string]$Verdict, [string]$Reason) {
    # A verdict that needs none of the analysis below (the directory cannot be read as ONE finished window).
    $o = [ordered]@{ schema = 'heavy-slot-ab-verdict.v1'; verdict = $Verdict; reason = $Reason; evaluatedUtc = $Now.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', $inv) }
    [IO.File]::WriteAllText((Join-Path $WindowDir 'ab-verdict.json'), (($o | ConvertTo-Json -Depth 4) + "`n"), $utf8)
    Write-Output ("AB_VERDICT {0} reason={1}" -f $Verdict, $Reason)
    exit 0
}

$blocksRaw = @(Read-JsonLines (Join-Path $WindowDir 'ab-blocks.jsonl'))
# a block row is usable only when every field the evaluation reads parses (adversary r1 A10: a stray {} must not reach the date maths)
$blocks = @($blocksRaw | Where-Object {
        $null -ne $_ -and $null -ne $_.PSObject.Properties['block'] -and ($_.block -is [int] -or $_.block -is [long]) -and ([string]$_.state -cin 'A', 'B') -and
        $null -ne (ConvertTo-UtcTime $_.startUtc) -and $null -ne (ConvertTo-UtcTime $_.endUtc) -and $null -ne (ConvertTo-UtcTime $_.windowStartUtc) -and
        $null -ne (ConvertTo-UtcTime $_.windowEndUtc) -and (ConvertTo-UtcTime $_.windowStartUtc) -lt (ConvertTo-UtcTime $_.windowEndUtc) })
$samples = @(Read-JsonLines (Join-Path $WindowDir 'ab-samples.jsonl'))
$receiptPath = Join-Path $WindowDir 'ab-receipt.json'
$driverReceipt = $null
if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { try { $driverReceipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json } catch { $driverReceipt = $null } }
if ($blocks.Count -eq 0) { Write-Output 'AB_EVAL_NO_INPUT no usable ab-blocks.jsonl rows'; exit 2 }
if ($WaitForGrace -and $driverReceipt) {
    # The hub arms this from the receipt, i.e. at window end. A ticket still running then must be able to close (and a dead holder be
    # told from a slow one) before anything is judged, so wait here for window end + the grace, then re-read the clock.
    $endedAt = ConvertTo-UtcTime $driverReceipt.endedUtc
    if ($endedAt) {
        $until = $endedAt.AddMinutes($DeadGraceMinutes)
        while ([DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds ([int][Math]::Max(100, [Math]::Min(30000, ($until - [DateTime]::UtcNow).TotalMilliseconds))) }
        $Now = [DateTime]::UtcNow
    }
}

# ONE directory = ONE window (the driver refuses to reuse a directory; this is the same rule on the reading side, adversary r1 A1)
$windowStarts = @($blocks | ForEach-Object { (ConvertTo-UtcTime $_.windowStartUtc).Ticks } | Sort-Object -Unique)
if ($windowStarts.Count -gt 1) {
    Write-EarlyVerdict 'INCONCLUSIVE' ("ab-blocks.jsonl holds {0} windows (windowStartUtc {1}): the append-only streams must not be blended, evaluate one window per directory" -f $windowStarts.Count, ((@($windowStarts | ForEach-Object { ([DateTime]::new([long]$_, [DateTimeKind]::Utc)).ToString('yyyy-MM-ddTHH:mm:ssZ', $inv) })) -join ', '))
}
$winStart = ConvertTo-UtcTime $blocks[0].windowStartUtc
if ($driverReceipt -and $driverReceipt.windowStartUtc -and (ConvertTo-UtcTime $driverReceipt.windowStartUtc).Ticks -ne $winStart.Ticks) {
    Write-EarlyVerdict 'INCONCLUSIVE' ("ab-receipt.json belongs to another window (receipt windowStartUtc {0}, blocks windowStartUtc {1})" -f $driverReceipt.windowStartUtc, $winStart.ToString('yyyy-MM-ddTHH:mm:ss.fffZ', $inv))
}
$winPlannedEnd = ConvertTo-UtcTime $blocks[0].windowEndUtc
$winEnd = $null
if ($driverReceipt) { $winEnd = ConvertTo-UtcTime $driverReceipt.endedUtc }
if (-not $winEnd -and $samples.Count -gt 0) { $winEnd = ConvertTo-UtcTime $samples[-1].utc }
if (-not $winEnd) { $winEnd = $winPlannedEnd }
# only this window's samples (a stray row from outside it never reaches the split)
$samplesSkipped = 0
$samples = @($samples | Where-Object { $t0 = ConvertTo-UtcTime $_.utc; if ($null -eq $t0) { $script:samplesSkipped++ }; $null -ne $t0 -and $t0 -ge $winStart -and $t0 -le $winEnd })
# every sample row is parsed ONCE here; a row whose utc or holders does not parse (or is not a finite whole number 0..1000) is a typed SKIP
# counted in input_problems.samples_skipped, a numeric field that does not parse reads as unknown (adversary r1 A11, r2 A7)
$psamples = @(foreach ($sm in $samples) {
        $hd = ConvertTo-DoubleOrNull $sm.holders
        if ($null -eq $hd -or [double]::IsNaN($hd) -or [double]::IsInfinity($hd) -or $hd -lt 0 -or $hd -gt 1000 -or $hd -ne [Math]::Floor($hd)) { $samplesSkipped++; continue }
        [pscustomobject]@{ t = (ConvertTo-UtcTime $sm.utc); holders = [int]$hd; temp = (ConvertTo-DoubleOrNull $sm.temp_c); cpu = (ConvertTo-DoubleOrNull $sm.cpu_pct)
            freq = (ConvertTo-DoubleOrNull $sm.freq_mhz); commit = (ConvertTo-DoubleOrNull $sm.free_commit_gb); state = [string]$sm.state }
    })
$psamples = @($psamples | Sort-Object { $_.t.Ticks })
# A block lasts from the moment its state was REALLY applied (startUtc) until the next block was applied (or the window ended): the
# scheduled endUtc is informational, so a slow poll that skipped or delayed a block cannot make the attribution lie (adversary r2 A2).
$orderedBlocks = @($blocks | Sort-Object { (ConvertTo-UtcTime $_.startUtc).Ticks })
$blockList = @(for ($bi = 0; $bi -lt $orderedBlocks.Count; $bi++) {
        $b = $orderedBlocks[$bi]
        $bStart = if ($bi -eq 0) { $winStart } else { ConvertTo-UtcTime $b.startUtc }
        $bEnd = if ($bi + 1 -lt $orderedBlocks.Count) { ConvertTo-UtcTime $orderedBlocks[$bi + 1].startUtc } else { $winEnd }
        [pscustomobject]@{ index = [int]$b.block; state = [string]$b.state; start = $bStart; end = $bEnd }
    })
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
    # an exit only counts when it landed BY window end + grace; a later one is still a dead (or far too slow) holder (adversary r1 A2)
    $hasExit = (($exited.ContainsKey($t) -and $exited[$t] -le $deadline) -or ($toolExit.ContainsKey($t) -and $null -ne (ConvertTo-UtcTime $toolExit[$t].at) -and (ConvertTo-UtcTime $toolExit[$t].at) -le $deadline))
    if ($reapedHeld.ContainsKey($t)) { $dead[$t] = $a.label; continue }
    if ($hasExit) { continue }
    if ($evaluatedEarly) { $pending[$t] = $a.label } else { $dead[$t] = $a.label }
}

$rdir = Join-Path $HeavySlotDir 'receipts'
$tickets = New-Object System.Collections.Generic.List[object]
$receiptsUnreadable = New-Object System.Collections.Generic.List[string]
if (Test-Path -LiteralPath $rdir -PathType Container) {
    foreach ($f in [IO.Directory]::GetFiles($rdir, '*.json')) {
        try { $r = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json; if ($null -eq $r) { throw 'empty receipt' } }
        catch {
            # A receipt that cannot be read is never skipped silently (adversary r2 A7): one last written at or after the window start MAY be a
            # ticket of this window, so the verdict becomes INCONCLUSIVE; an older one cannot be in the window and is not counted.
            $lw = $null
            try { $lw = (Get-Item -LiteralPath $f -ErrorAction Stop).LastWriteTimeUtc } catch { $lw = $null }
            if ($null -eq $lw -or $lw -ge $winStart) { $receiptsUnreadable.Add([IO.Path]::GetFileName($f)) }
            continue
        }
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
$holds = New-Object System.Collections.Generic.List[object]
foreach ($k in $tickets.ToArray()) {
    # A receipt that closed after window end + grace is a dead (or hopelessly slow) holder even when its ledger admit row is missing
    # (the producer's ledger append is advisory): the receipt itself says when it finished (adversary r2 A1).
    if ($null -ne $k.fin -and $k.fin -gt $deadline -and -not $dead.ContainsKey($k.ticket)) { $dead[$k.ticket] = $k.label }
    # A holder the status sweep reaped (`reaped kind=held`) is dead even when its ledger admit row is missing: the receipt is excluded the
    # same way (adversary r3 A4; the admit row is advisory, the reaped row is the proof).
    if ($reapedHeld.ContainsKey($k.ticket) -and -not $dead.ContainsKey($k.ticket)) { $dead[$k.ticket] = $k.label }
    if ($dead.ContainsKey($k.ticket)) { $excludedDead++; continue }
    if ($pending.ContainsKey($k.ticket)) { $excludedPending++; continue }
    $be = Get-BlockOf $k.enq
    if (-not $be) { $outside++; continue }
    $c = & $cls $k.label
    $admitted = ($null -ne $k.adm -and $null -ne $k.fin -and $k.hasExit)
    if ($admitted) {
        $ba = Get-BlockOf $k.adm
        if (-not $ba -or $ba.index -ne $be.index) { $spill.Add([pscustomobject]@{ ticket = $k.ticket; label = $k.label; enqueuedBlock = $be.index; admittedBlock = $(if ($ba) { $ba.index } else { $null }) }); continue }
        $wms = ConvertTo-DoubleOrNull $k.waitedMs
        $wait = if ($null -ne $wms) { $wms / 1000.0 } else { ($k.adm - $k.enq).TotalSeconds }
        $hold = ($k.fin - $k.adm).TotalSeconds
        $bf = Get-BlockOf $k.fin
        if (-not $bf -or $bf.index -ne $be.index) { $holdCrossEdge++ }
        $arm[$be.state].all.w += $wait; $arm[$be.state].all.h += $hold
        if ($c -ne 'other') { $arm[$be.state][$c].w += $wait; $arm[$be.state][$c].h += $hold }
        $holds.Add([pscustomobject]@{ ticket = $k.ticket; label = $k.label; cls = $c; arm = $be.state; block = $be.index; wait = $wait; hold = $hold; adm = $k.adm; fin = $k.fin })
        if ($be.state -eq 'B' -and $c -eq 'pytest') { $bPytestN++; if ([string]$k.slotCount -eq '2') { $bPytestCount2++ } }
    }
    elseif ($toolExit.ContainsKey($k.ticket) -and [string]$toolExit[$k.ticket].code -eq '75') {
        $tAt = ConvertTo-UtcTime $toolExit[$k.ticket].at
        # A wait that was cut off in ANOTHER block (or after the window) crossed an applied edge: it ran partly under each state, so it is
        # SPILL like an admitted ticket that crossed one, never a timed-out wait of the block it enqueued in (adversary r3 A5).
        $endBlock = Get-BlockOf $tAt
        if (-not $endBlock -or $endBlock.index -ne $be.index) {
            $spill.Add([pscustomobject]@{ ticket = $k.ticket; label = $k.label; enqueuedBlock = $be.index; admittedBlock = $null; endedBlock = $(if ($endBlock) { $endBlock.index } else { $null }); kind = 'timed-out-wait' })
            continue
        }
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

# ---- per hold: wait, frequency and temperature logged for EACH hold and split by the live holders it ran beside (hub ruling b) --------
# For every live closed hold: the peak live-holder count over its samples ([admit, finish]; with none inside, the first sample at or
# after admit within two polls, else the last one before), the hottest and mean package temp and the median frequency over those samples.
# holders_peak >= 2 means the hold overlapped another; <= 1 means it ran alone. ab-holds.jsonl carries one row per hold (adversary r1 A6).
$pollGuess = 15.0
if ($driverReceipt) { $pg = ConvertTo-DoubleOrNull $driverReceipt.pollSeconds; if ($null -ne $pg -and $pg -gt 0) { $pollGuess = $pg } }
$sampRows = @(foreach ($ps in $psamples) { [pscustomobject]@{ t = $ps.t.Ticks; holders = $ps.holders; temp = $ps.temp; freq = $ps.freq } })
$sTicks = [long[]]@($sampRows | ForEach-Object { $_.t })
$holdRows = New-Object System.Collections.Generic.List[object]
foreach ($h0 in $holds.ToArray()) {
    $picked = New-Object System.Collections.Generic.List[object]
    $nearest = $false
    if ($sTicks.Length -gt 0) {
        $lo = [Array]::BinarySearch($sTicks, [long]$h0.adm.Ticks); if ($lo -lt 0) { $lo = -bnot $lo }
        for ($ix = $lo; $ix -lt $sTicks.Length -and $sTicks[$ix] -le $h0.fin.Ticks; $ix++) { $picked.Add($sampRows[$ix]) }
        if ($picked.Count -eq 0) {
            $nearest = $true
            if ($lo -lt $sTicks.Length -and ($sTicks[$lo] - $h0.adm.Ticks) -le [long](2 * $pollGuess * 1e7)) { $picked.Add($sampRows[$lo]) }
            elseif ($lo -gt 0) { $picked.Add($sampRows[$lo - 1]) }
        }
    }
    $pr = $picked.ToArray()
    $temps0 = @($pr | Where-Object { $null -ne $_.temp } | ForEach-Object { [double]$_.temp })
    $freqs0 = @($pr | Where-Object { $null -ne $_.freq } | ForEach-Object { [double]$_.freq })
    $peak = if ($pr.Count -gt 0) { ($pr | Measure-Object -Property holders -Maximum).Maximum } else { $null }
    $holdRows.Add([pscustomobject][ordered]@{
            ticket = $h0.ticket; label = $h0.label; class = $h0.cls; arm = $h0.arm; block = $h0.block
            wait_s = [Math]::Round($h0.wait, 3); hold_s = [Math]::Round($h0.hold, 3); holders_peak = $peak
            temp_c_max = $(if ($temps0.Count -gt 0) { ($temps0 | Measure-Object -Maximum).Maximum } else { $null })
            temp_c_mean = $(if ($temps0.Count -gt 0) { [Math]::Round(($temps0 | Measure-Object -Average).Average, 2) } else { $null })
            freq_mhz_p50 = (Get-Pct $freqs0 50); samples_in_hold = $pr.Count; nearest_sample = $nearest
        })
}
[IO.File]::WriteAllText((Join-Path $WindowDir 'ab-holds.jsonl'), ((@($holdRows.ToArray() | ForEach-Object { $_ | ConvertTo-Json -Compress }) -join "`n") + "`n"), $utf8)
function Get-CohortStat($Rows) {
    $r = @($Rows)
    return [ordered]@{
        n = $r.Count
        wait_s_p50 = (Get-Pct @($r | ForEach-Object { [double]$_.wait_s }) 50); wait_s_p90 = (Get-Pct @($r | ForEach-Object { [double]$_.wait_s }) 90); wait_s_p99 = (Get-Pct @($r | ForEach-Object { [double]$_.wait_s }) 99)
        hold_s_p50 = (Get-Pct @($r | ForEach-Object { [double]$_.hold_s }) 50)
        hold_temp_c_max_p95 = (Get-Pct @($r | Where-Object { $null -ne $_.temp_c_max } | ForEach-Object { [double]$_.temp_c_max }) 95)
        hold_freq_mhz_p50 = (Get-Pct @($r | Where-Object { $null -ne $_.freq_mhz_p50 } | ForEach-Object { [double]$_.freq_mhz_p50 }) 50)
    }
}
$holdsAll = $holdRows.ToArray()
$holdsByHolders = [ordered]@{}
foreach ($grp in 'A', 'B', 'all') {
    $inGrp = @($holdsAll | Where-Object { $grp -eq 'all' -or $_.arm -eq $grp })
    $holdsByHolders[$grp] = [ordered]@{
        holders_1 = (Get-CohortStat @($inGrp | Where-Object { $null -ne $_.holders_peak -and $_.holders_peak -le 1 }))
        holders_2_plus = (Get-CohortStat @($inGrp | Where-Object { $null -ne $_.holders_peak -and $_.holders_peak -ge 2 }))
        holders_unknown = (Get-CohortStat @($inGrp | Where-Object { $null -eq $_.holders_peak }))
    }
}

# ---- samples, split by live holders --------------------------------------------------------------------------------------------
$groups = @{ '0' = New-Object System.Collections.Generic.List[object]; '1' = New-Object System.Collections.Generic.List[object]; '2' = New-Object System.Collections.Generic.List[object] }
$allB = New-Object System.Collections.Generic.List[object]
for ($i = 0; $i -lt $psamples.Count; $i++) {
    $s = $psamples[$i]
    $t = $s.t
    # the time a sample stands for: up to the next sample, but never more than two polls and never past the window end
    $toEnd = [Math]::Max(0.0, ($winEnd - $t).TotalSeconds)
    $dt = if ($i + 1 -lt $psamples.Count) { [Math]::Min(($psamples[$i + 1].t - $t).TotalSeconds, 2 * $pollGuess) } else { $pollGuess }
    $dt = [Math]::Min($dt, $toEnd)
    $h = $s.holders
    $key = if ($h -ge 2) { '2' } elseif ($h -eq 1) { '1' } else { '0' }
    $row = [pscustomobject]@{ dt = [Math]::Max(0.0, $dt); temp = $s.temp; cpu = $s.cpu; freq = $s.freq; commit = $s.commit; state = $s.state }
    $groups[$key].Add($row)
    if ($row.state -eq 'B') { $allB.Add($row) }
}
function Get-Nums($rows, [string]$Prop) { return @($rows | Where-Object { $null -ne $_.$Prop } | ForEach-Object { [double]$_.$Prop }) }
$sampleStats = [ordered]@{}
$rawStats = @{}    # the EXACT figures every threshold is compared against; the json carries rounded copies for reading only (adversary r3 A8)
foreach ($key in '0', '1', '2') {
    $rows = $groups[$key].ToArray()
    $temps = @(Get-Nums $rows 'temp')
    $load = @($rows | Where-Object { $null -ne $_.cpu -and [double]$_.cpu -ge $LoadCpuPct })
    $commits = @(Get-Nums $rows 'commit')
    $ge90 = @($temps | Where-Object { $_ -ge 90 }).Count
    $sumDt = [double](($rows | Measure-Object -Property dt -Sum).Sum)
    $fracRaw = $(if ($temps.Count -gt 0) { $ge90 / [double]$temps.Count } else { $null })
    $rawStats[$key] = @{ minutes = ($sumDt / 60.0); frac = $fracRaw }
    $sampleStats[$key] = [ordered]@{
        samples = $rows.Count
        minutes = [Math]::Round($sumDt / 60.0, 3)
        temp_c_p95 = (Get-Pct $temps 95)
        temp_ge90_frac = $(if ($null -ne $fracRaw) { [Math]::Round($fracRaw, 4) } else { $null })
        under_load_samples = $load.Count
        freq_p50_under_load = (Get-Pct @(Get-Nums $load 'freq') 50)
        min_free_commit_gb = $(if ($commits.Count -gt 0) { ($commits | Measure-Object -Minimum).Minimum } else { $null })
        free_commit_unknown_samples = ($rows.Count - $commits.Count)
    }
}
# A B sample with no free-commit reading is NOT dropped from the minimum (adversary r3 A6): the minimum over the readings that exist says
# nothing about the one that does not, so any unreadable B sample makes the B minimum unknown and the verdict INCONCLUSIVE.
$minCommitB = $null
$allBRows = $allB.ToArray()
$cB = @(Get-Nums $allBRows 'commit')
$bCommitUnknown = $allBRows.Count - $cB.Count
if ($cB.Count -gt 0) { $minCommitB = ($cB | Measure-Object -Minimum).Minimum }

# ---- Event 2004 -----------------------------------------------------------------------------------------------------------------
# $null = the count could not be read (an unreadable probe, a log query that failed): never read as zero (adversary r1 A3). "No such
# events" is a real zero (Get-WinEvent reports NoMatchingEventsFound).
$ev = $null
if ($EventProbeFile) {
    try { $n0 = 0; if ([int]::TryParse(([IO.File]::ReadAllText($EventProbeFile)).Trim(), [ref]$n0) -and $n0 -ge 0) { $ev = $n0 } } catch { $ev = $null }
}
else {
    try { $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 2004; StartTime = $winStart.ToLocalTime(); EndTime = $winEnd.ToLocalTime() } -MaxEvents 5 -ErrorAction Stop).Count }
    catch { if ([string]$_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $ev = 0 } else { $ev = $null } }
}

# ---- verdict ----------------------------------------------------------------------------------------------------------------------
$thresholds = [ordered]@{
    min_live_closed_tickets_per_arm = $MinTicketsPerArm; min_builds_per_arm = $MinBuildsPerArm; min_holders2_minutes = $MinHolders2Minutes
    b_wait_p90_max_ratio_of_a = $WaitP90Ratio; b_build_wait_p90_max_ratio_of_a = $BuildWaitP90Ratio; b_build_hold_p50_max_ratio_of_a = $BuildHoldP50Ratio
    holders2_temp_ge90_frac_max = $MaxGe90Frac; holders2_temp_c_p95_max = $MaxTempP95; holders2_freq_p50_min_ratio_of_holders1 = $MinFreqRatio
    min_free_commit_gb = $MinFreeCommitGB; under_load_cpu_pct = $LoadCpuPct; dead_holder_grace_minutes = $DeadGraceMinutes
    min_b_pytest_at_slotcount_2 = $MinBPytestAtSlotCount2
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
    if ([double]$rawStats['2'].minutes -lt $MinHolders2Minutes) { $inconclusive.Add(("holders>=2 time {0:N3} min (need {1})" -f [double]$rawStats['2'].minutes, $MinHolders2Minutes)) }
    if ($bCommitUnknown -gt 0) { $inconclusive.Add("$bCommitUnknown B sample(s) have no free commit reading, so the B minimum free commit is unknown") }
    if ($receiptsUnreadable.Count -gt 0) { $inconclusive.Add(("{0} receipt(s) written during the window cannot be read ({1}): the tickets in them are unknown" -f $receiptsUnreadable.Count, (($receiptsUnreadable | Select-Object -First 3) -join ', '))) }
    if ($bPytestCount2 -lt $MinBPytestAtSlotCount2) { $inconclusive.Add("B pytest tickets that ran at slotCount 2: $bPytestCount2 of $bPytestN (need $MinBPytestAtSlotCount2): the switch may never have reached the tool") }
    if ($null -ne $A.overall.wait_s_p90 -and [double]$A.overall.wait_s_p90 -eq 0) { $inconclusive.Add('A arm shows no queueing (wait_p90 = 0), nothing to reduce') }
    if ($null -eq $sampleStats['2'].freq_p50_under_load -or $null -eq $sampleStats['1'].freq_p50_under_load) { $inconclusive.Add('no under-load frequency sample at holders=1 or holders>=2') }
    if ($null -eq $ev) { $inconclusive.Add('the System Event 2004 count could not be read (event probe or log query failed), so "no Event 2004" cannot be shown') }
    if ($inconclusive.Count -gt 0) { $verdict = 'INCONCLUSIVE'; $reason = ($inconclusive -join '; ') }
    else {
        $aw = [double]$A.overall.wait_s_p90; $bw = [double]$B.overall.wait_s_p90
        Add-Check 'overall_wait_p90_b_vs_a' $bw ("<= {0} x {1} = {2}" -f $WaitP90Ratio, $aw, ($WaitP90Ratio * $aw)) ($bw -le $WaitP90Ratio * $aw)
        $abw = [double]$A.build.wait_s_p90; $bbw = [double]$B.build.wait_s_p90
        Add-Check 'build_wait_p90_b_vs_a' $bbw ("<= {0} x {1} = {2}" -f $BuildWaitP90Ratio, $abw, ($BuildWaitP90Ratio * $abw)) ($bbw -le $BuildWaitP90Ratio * $abw)
        $abh = [double]$A.build.hold_s_p50; $bbh = [double]$B.build.hold_s_p50
        Add-Check 'build_hold_p50_b_vs_a' $bbh ("<= {0} x {1} = {2}" -f $BuildHoldP50Ratio, $abh, ($BuildHoldP50Ratio * $abh)) ($bbh -le $BuildHoldP50Ratio * $abh)
        $s2 = $sampleStats['2']; $s1 = $sampleStats['1']
        Add-Check 'holders2_temp_ge90_frac' $s2.temp_ge90_frac "<= $MaxGe90Frac" ($null -ne $rawStats['2'].frac -and [double]$rawStats['2'].frac -le $MaxGe90Frac)
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
    input_problems = [ordered]@{
        receipts_unreadable = $receiptsUnreadable.Count
        ledger_lines_unparseable = [int]$script:badLines['ledger.jsonl']
        blocks_lines_unparseable = [int]$script:badLines['ab-blocks.jsonl']
        samples_lines_unparseable = [int]$script:badLines['ab-samples.jsonl']
        samples_skipped = $samplesSkipped
        b_samples_without_free_commit = $bCommitUnknown
    }
    arms = $armStats
    holds_by_holders = $holdsByHolders
    holds_log = 'ab-holds.jsonl'
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
