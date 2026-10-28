# VenueThermalGate.psm1 -- BACHELOR-THERMAL-GATE-WAIT-1. Pure helpers for the lease-batch runner's thermal gate and heavy-leg spread.
#
# WHY. In PR493-CONTROL-ARM-QUIET-AB-1 r1, 3 of the 36 legs of batches 01-05 ran and were then discarded THERMAL_HOT (9/60 throttled rows at grant), and
# every one directly followed a leg of the CPU-saturating cell 15 (m16-1243-display-fullscreen-s1, cpu, about 10 fps). The trigger was the order, not the
# hardware. Two fixes, both here, both applied by Invoke-VenueLeaseBatch.ps1:
#   1. WAIT FOR COOL. After a QUIET in-lease probe, the leg launches only when the probe's heat reading is clear. Hot -> re-probe (wait) up to -CapSec
#      (180); still hot past the cap -> THERMAL_HOT_SKIPPED and the leg is re-queued, never run and discarded.
#   2. SPREAD. -PlanOrder ThermalSpread never puts two heavy legs back to back and follows a heavy leg with a light one. Heavy is MEASURED, never listed: a
#      leg key (legId|backend) is heavy when the leg that directly followed it on the same lease read hot (Get-VenueHeavyLegKeys).
#
# THE READING (not a second sensor). Bachelor's agent stamps every leg job with its heat at grant: ultra-magnus-agent.ps1 Get-SustainedHeatVerdict (:2428-2440),
# over its heavy-slot feed (HEAVY_SLOT_FEED, else C:\temp\hwinfo-log-adapter.csv, :56), recorded as admission.heat / admission.thermal (lease mode records,
# does not gate). The in-lease quiet probe (Wait-VenueQuiet.ps1) reads THE SAME feed with the agent's own reader copied (:527-546, the 60-row window) and
# prints it on stdout BEFORE the leg is launched: "  tele heat source=<feed> ... mean=<C>C throttled=<n>/<rows> W=<w>" (:1262-1264). That line is the
# reading this gate polls. Hot = the agent's rule exactly: throttled > 6 of the window, or mean >= 88 C (GateMaxThrottledRows / GateMeanMaxC, :60-61;
# test_venue_thermal_gate.py holds these constants to the agent's defaults). A probe with no readable heat line is 'unknown' and is NOT gated (the agent
# does not gate lease-mode heat either); the receipt says so.
#
# Pure: no file writes, no network, no process start, no clock of its own. Read-VenueLegHeatHistory only reads the receipts it is pointed at.

Set-StrictMode -Version Latest

$script:HeatMaxThrottledRows = 6
$script:HeatMeanMaxC = 88.0
$script:Inv = [System.Globalization.CultureInfo]::InvariantCulture

function Get-VenueHeatVerdict {
    # 'hot' | 'clear' | 'unknown'. The agent's Get-SustainedHeatVerdict rule: hot when throttled > 6 or mean >= 88 C. A missing or non-finite value is unknown.
    param([AllowNull()][object]$Throttled, [AllowNull()][object]$MeanC)
    if ($null -eq $Throttled -or $null -eq $MeanC) { return 'unknown' }
    $t = 0.0; $m = 0.0
    if (-not [double]::TryParse([string]$Throttled, [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$t)) { return 'unknown' }
    if (-not [double]::TryParse([string]$MeanC, [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$m)) { return 'unknown' }
    if ([double]::IsNaN($t) -or [double]::IsInfinity($t) -or $t -lt 0 -or [double]::IsNaN($m) -or [double]::IsInfinity($m)) { return 'unknown' }
    if ($t -gt $script:HeatMaxThrottledRows -or $m -ge $script:HeatMeanMaxC) { 'hot' } else { 'clear' }
}

function ConvertFrom-VenueProbeHeatLine {
    # A quiet probe's stdout -> @{ heat; throttled; window; meanC; text; source }. adversary r1 A2/A3: the probe's raw JSON (its "  raw <path>" line) carries
    # heat{} as numbers (mean_c to 0.01, culture-invariant JSON), so it is read FIRST; the "tele heat" display line (mean to 0.1 C, in the probe's culture,
    # a decimal comma included) is only the fallback. Neither readable, or n/a values -> heat 'unknown'.
    param([AllowNull()][AllowEmptyCollection()][string[]]$Lines)
    # adversary r1b A3: the LAST raw line, UNWRITABLE included, is the final probe's: an earlier probe's file is never read in place of a final write that failed
    $rawLine = @($Lines | Where-Object { $_ -match '^\s*raw ' }) | Select-Object -Last 1
    if ($rawLine -and $rawLine -notmatch '^\s*raw UNWRITABLE ' -and $rawLine -match '^\s*raw (.+\.json)\s*$') {
        $h = try { $p = $Matches[1].Trim(); if (Test-Path -LiteralPath $p -PathType Leaf) { ([IO.File]::ReadAllText($p) | ConvertFrom-Json).heat } else { $null } } catch { $null }
        if ($null -ne $h -and $h -is [System.Management.Automation.PSCustomObject] -and $h.PSObject.Properties['mean_c'] -and $h.PSObject.Properties['throttled'] -and $h.PSObject.Properties['window_rows']) {
            # adversary r1b A8: a count that is not a whole number in Int32 range is not a reading: the display line is used instead, never a throw
            $thr = 0; $win = 0; $mean = 0.0
            $okT = [int]::TryParse([string]$h.throttled, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$thr)
            $okW = [int]::TryParse([string]$h.window_rows, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$win)
            $okM = [double]::TryParse([string]$h.mean_c, [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$mean)
            $v = $(if ($okT -and $okW -and $okM) { Get-VenueHeatVerdict -Throttled $thr -MeanC $mean } else { 'unknown' })
            if ($v -cne 'unknown') {
                return [pscustomobject]@{ heat = $v; throttled = $thr; window = $win; meanC = $mean; text = "throttled $thr/$win, mean $($mean.ToString('0.00', $script:Inv)) C"; source = 'raw-json' }
            }
        }
    }
    $line = @($Lines | Where-Object { $_ -match '^\s*tele heat ' }) | Select-Object -Last 1
    $none = [pscustomobject]@{ heat = 'unknown'; throttled = $null; window = $null; meanC = $null; text = $(if ($line) { ([string]$line).Trim() } else { 'no tele heat line' }); source = 'none' }
    if (-not $line) { return $none }
    $m = [regex]::Match([string]$line, '\smean=([0-9]+(?:[.,][0-9]+)?)C\s+throttled=([0-9]+)/([0-9]+)(?:\s|$)')
    if (-not $m.Success) { return $none }
    $mean = 0.0; $thr = 0; $win = 0
    if (-not [double]::TryParse(($m.Groups[1].Value -replace ',', '.'), [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$mean)) { return $none }
    if (-not [int]::TryParse($m.Groups[2].Value, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$thr)) { return $none }   # advisory A10: an overflow is unknown, never a throw
    if (-not [int]::TryParse($m.Groups[3].Value, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$win)) { return $none }
    [pscustomobject]@{ heat = (Get-VenueHeatVerdict -Throttled $thr -MeanC $mean); throttled = $thr; window = $win; meanC = $mean; text = "throttled $thr/$win, mean $($mean.ToString('0.0', $script:Inv)) C"; source = 'tele-line' }
}

function Get-VenueThermalGateStep {
    # One gate decision after a QUIET probe. RUN (clear or unknown), WAIT (hot, under the cap since the first hot reading), SKIP (hot at or past the cap).
    # -FirstHotUtc is the caller's state ($null until the first hot reading); the returned firstHotUtc is the state to keep. waitSec = seconds since it.
    param(
        [Parameter(Mandatory)][ValidateSet('hot', 'clear', 'unknown', IgnoreCase = $false)][string]$Heat,
        [AllowNull()][Nullable[DateTime]]$FirstHotUtc,
        [Parameter(Mandatory)][DateTime]$NowUtc,
        [Parameter(Mandatory)][ValidateRange(0, 86400)][int]$CapSec
    )
    $first = $FirstHotUtc
    if ($Heat -ceq 'hot' -and $null -eq $first) { $first = $NowUtc }
    # adversary r1b A7: clamped before the Int32 cast (a decades-old first reading is simply past any cap)
    $waited = $(if ($null -ne $first) { [int][Math]::Min([double][int]::MaxValue, [Math]::Max(0.0, [Math]::Floor(($NowUtc - $first).TotalSeconds))) } else { 0 })
    $step = $(if ($Heat -cne 'hot') { 'RUN' } elseif ($waited -ge $CapSec) { 'SKIP' } else { 'WAIT' })
    [pscustomobject]@{ step = $step; firstHotUtc = $first; waitSec = $waited; remainingSec = $(if ($step -ceq 'WAIT') { $CapSec - $waited } else { 0 }) }
}

function Get-VenueLegKeyFromName {
    # A runner leg name -> legId|backend. Names are NN-<legId>-<backend> (scalar) or NN-<legId>-<backend>-<arm> (-LegPlan), Invoke-VenueLeaseBatch.ps1.
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Name, [AllowEmptyString()][string]$Backend = '', [AllowEmptyString()][string]$Arm = '')
    $n = $Name -replace '^[0-9]+-', ''
    if ($Arm -and $n.EndsWith("-$Arm", [StringComparison]::Ordinal)) { $n = $n.Substring(0, $n.Length - $Arm.Length - 1) }
    if ($Backend -and $n.EndsWith("-$Backend", [StringComparison]::Ordinal)) { $n = $n.Substring(0, $n.Length - $Backend.Length - 1) }
    "$n|$Backend"
}

function Get-VenueHeavyLegKeys {
    # Rows in run order: { batch, key, status, heat }. A key is HEAVY when a leg that directly followed a RAN leg of that key on the same batch read hot
    # (its heat before any wait, or at grant for rows written before this gate). Returns key -> number of hot successors (only heavy keys).
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows)
    $heavy = [ordered]@{}
    $lastBatch = $null; $lastRanKey = $null
    foreach ($r in $Rows) {
        if ($null -eq $r) { continue }
        if ([string]$r.batch -cne [string]$lastBatch) { $lastBatch = [string]$r.batch; $lastRanKey = $null }
        if ([string]$r.heat -ceq 'hot' -and $null -ne $lastRanKey) { $heavy[$lastRanKey] = 1 + $(if ($heavy.Contains($lastRanKey)) { $heavy[$lastRanKey] } else { 0 }) }
        # adversary r1b A5: the predecessor is the last leg that LAUNCHED (RAN, LEG_FAILED, LEASE_EXPIRED); a row of unknown identity (a $null key) is a barrier, so
        # heat is never charged across it. A leg that never launched (THERMAL_HOT_SKIPPED, a RETRY whose own re-check failed) leaves the predecessor as it was.
        if ([string]$r.status -cnotin @('THERMAL_HOT_SKIPPED', 'RETRY')) { $lastRanKey = $(if ($r.key) { [string]$r.key } else { $null }) }
    }
    $heavy
}

function Read-VenueLegHeatHistory {
    # The leg rows of earlier lease-batch runs, in run order, for Get-VenueHeavyLegKeys. Each -RunDir's batches.jsonl (mlv-app/venue-lease-batch/v1); a row's
    # heat is its own heatAtProbe (written by this gate), else the agent's admission.heat from agent-result-<jobId>.json beside it, else 'unknown'.
    # A dir with no batches.jsonl, or an unreadable line, is skipped and named in .skipped (never a throw).
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$RunDir)
    $rows = [System.Collections.Generic.List[object]]::new(); $skipped = [System.Collections.Generic.List[string]]::new()
    $d = 0
    foreach ($dir in $RunDir) {
        $d++
        $f = Join-Path $dir 'batches.jsonl'
        if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { $skipped.Add("$dir (no batches.jsonl)"); continue }
        # adversary r1 A7: a file that cannot be read (held exclusively, gone) is skipped and named, never a throw that refuses the whole plan
        $lines = try { [IO.File]::ReadAllLines($f) } catch { $skipped.Add("$f (unreadable: $($_.Exception.Message -replace '\s+', ' '))"); continue }
        foreach ($line in $lines) {
            if (-not $line.Trim()) { continue }
            $b = try { $line | ConvertFrom-Json } catch { $null }
            if ($null -eq $b -or $b -isnot [System.Management.Automation.PSCustomObject] -or -not $b.PSObject.Properties['legs']) { $skipped.Add("$f (unreadable line)"); continue }
            # adversary r1 A8: one lease is one batch receipt (token), not one batch number: a reused -RunDir numbers its batches from 1 again
            $lease = $(if ($b.PSObject.Properties['token'] -and $b.token) { [string]$b.token } elseif ($b.PSObject.Properties['batch']) { "batch$($b.batch)" } else { "line$($rows.Count)" })
            $unnamed = 0
            foreach ($l in @($b.legs)) {
                # adversary r1 A5: a scalar-run row carries no leg name (Add-PlanIdentity names -LegPlan rows only): counted and named in .skipped, never silently dropped.
                # r1b A5: it stays in the sequence as a barrier row (key $null), so a hot leg after it is never charged to the leg before it.
                $bad = ($null -eq $l -or $l -isnot [System.Management.Automation.PSCustomObject] -or -not $l.PSObject.Properties['name'] -or -not $l.name -or -not $l.PSObject.Properties['status'])
                if ($bad) { $unnamed++; $rows.Add([pscustomobject]@{ batch = "$d.$lease"; key = $null; name = $null; status = 'UNKNOWN'; heat = 'unknown' }); continue }
                $backend = $(if ($l.PSObject.Properties['backend']) { [string]$l.backend } else { '' })
                $arm = $(if ($l.PSObject.Properties['arm']) { [string]$l.arm } else { '' })
                $heat = 'unknown'
                if ($l.PSObject.Properties['heatAtProbe'] -and [string]$l.heatAtProbe -cin @('hot', 'clear')) { $heat = [string]$l.heatAtProbe }
                elseif ($l.PSObject.Properties['jobId'] -and $l.jobId) {
                    $ar = Join-Path $dir "agent-result-$($l.jobId).json"
                    if (Test-Path -LiteralPath $ar -PathType Leaf) {
                        $a = try { [IO.File]::ReadAllText($ar) | ConvertFrom-Json } catch { $null }
                        if ($null -ne $a -and $a.PSObject.Properties['admission'] -and $null -ne $a.admission -and $a.admission.PSObject.Properties['heat'] -and [string]$a.admission.heat -cin @('hot', 'clear')) { $heat = [string]$a.admission.heat }
                    }
                }
                $rows.Add([pscustomobject]@{ batch = "$d.$lease"; key = (Get-VenueLegKeyFromName -Name ([string]$l.name) -Backend $backend -Arm $arm); name = [string]$l.name; status = [string]$l.status; heat = $heat })
            }
            if ($unnamed -gt 0) { $skipped.Add("$f lease $lease ($unnamed leg rows with no leg name or status: a scalar run's rows cannot be keyed)") }
        }
    }
    [pscustomobject]@{ rows = @($rows); skipped = @($skipped) }
}

function Measure-VenueHeavyAdjacency {
    # How many neighbouring pairs are both heavy.
    param([Parameter(Mandatory)][AllowEmptyCollection()][bool[]]$Heavy)
    $n = 0
    for ($i = 1; $i -lt $Heavy.Count; $i++) { if ($Heavy[$i] -and $Heavy[$i - 1]) { $n++ } }
    $n
}

function Get-VenueThermalSpreadOrder {
    # -Items reordered so no two heavy items are adjacent and every heavy item is followed by a light one, staying as close to the given order as that allows:
    # at each step the EARLIEST remaining item whose choice still leaves a feasible rest (heavy left <= light left, plus one when the last placed is light).
    # More heavy items than light ones plus one cannot be spread: the excess runs back to back at the end (counted in heavyAdjacent, never dropped).
    # -Heavy is parallel to -Items. Returns @{ items; heavy (bool[] in the new order); heavyAdjacent; heavyAdjacentBefore }.
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Items, [Parameter(Mandatory)][AllowEmptyCollection()][bool[]]$Heavy)
    if ($Items.Count -ne $Heavy.Count) { throw "Get-VenueThermalSpreadOrder: $($Items.Count) items but $($Heavy.Count) heavy flags" }
    $left = [System.Collections.Generic.List[int]]::new(); for ($i = 0; $i -lt $Items.Count; $i++) { $left.Add($i) }
    $h = @($Heavy | Where-Object { $_ }).Count; $l = $Items.Count - $h
    $out = [System.Collections.Generic.List[int]]::new(); $lastHeavy = $false
    while ($left.Count -gt 0) {
        $pick = -1
        foreach ($i in $left) {
            if ($lastHeavy -and $Heavy[$i]) { continue }
            $h2 = $h - [int]$Heavy[$i]; $l2 = $l - [int](-not $Heavy[$i])
            if ($h2 -le ($l2 + [int](-not $Heavy[$i]))) { $pick = $i; break }
        }
        if ($pick -lt 0) { foreach ($i in $left) { if (-not ($lastHeavy -and $Heavy[$i])) { $pick = $i; break } } }
        if ($pick -lt 0) { $pick = $left[0] }
        [void]$left.Remove($pick); $out.Add($pick)
        if ($Heavy[$pick]) { $h-- } else { $l-- }
        $lastHeavy = $Heavy[$pick]
    }
    $newHeavy = [bool[]]@($out | ForEach-Object { $Heavy[$_] })
    [pscustomobject]@{ items = @($out | ForEach-Object { , $Items[$_] }); heavy = $newHeavy; heavyAdjacent = (Measure-VenueHeavyAdjacency -Heavy $newHeavy); heavyAdjacentBefore = (Measure-VenueHeavyAdjacency -Heavy $Heavy) }
}

Export-ModuleMember -Function Get-VenueHeatVerdict, ConvertFrom-VenueProbeHeatLine, Get-VenueThermalGateStep, Get-VenueLegKeyFromName, Get-VenueHeavyLegKeys, Read-VenueLegHeatHistory,
    Measure-VenueHeavyAdjacency, Get-VenueThermalSpreadOrder
