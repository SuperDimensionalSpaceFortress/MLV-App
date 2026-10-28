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
    # The LAST "tele heat" line of a quiet probe's stdout -> @{ heat; throttled; window; meanC; text }. No line, or n/a values -> heat 'unknown'.
    param([AllowNull()][AllowEmptyCollection()][string[]]$Lines)
    $line = @($Lines | Where-Object { $_ -match '^\s*tele heat ' }) | Select-Object -Last 1
    $none = [pscustomobject]@{ heat = 'unknown'; throttled = $null; window = $null; meanC = $null; text = $(if ($line) { ([string]$line).Trim() } else { 'no tele heat line' }) }
    if (-not $line) { return $none }
    $m = [regex]::Match([string]$line, '\smean=([0-9]+(?:\.[0-9]+)?)C\s+throttled=([0-9]+)/([0-9]+)(?:\s|$)')
    if (-not $m.Success) { return $none }
    $mean = [double]::Parse($m.Groups[1].Value, $script:Inv); $thr = [int]::Parse($m.Groups[2].Value, $script:Inv); $win = [int]::Parse($m.Groups[3].Value, $script:Inv)
    [pscustomobject]@{ heat = (Get-VenueHeatVerdict -Throttled $thr -MeanC $mean); throttled = $thr; window = $win; meanC = $mean; text = "throttled $thr/$win, mean $($mean.ToString('0.0', $script:Inv)) C" }
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
    $waited = $(if ($null -ne $first) { [int][Math]::Max(0, [Math]::Floor(($NowUtc - $first).TotalSeconds)) } else { 0 })
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
        if ([string]$r.status -ceq 'RAN') { $lastRanKey = [string]$r.key }
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
        foreach ($line in [IO.File]::ReadAllLines($f)) {
            if (-not $line.Trim()) { continue }
            $b = try { $line | ConvertFrom-Json } catch { $null }
            if ($null -eq $b -or -not $b.PSObject.Properties['legs']) { $skipped.Add("$f (unreadable line)"); continue }
            foreach ($l in @($b.legs)) {
                if ($null -eq $l -or -not $l.PSObject.Properties['name']) { continue }
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
                $rows.Add([pscustomobject]@{ batch = "$d.$($b.batch)"; key = (Get-VenueLegKeyFromName -Name ([string]$l.name) -Backend $backend -Arm $arm); name = [string]$l.name; status = [string]$l.status; heat = $heat })
            }
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
