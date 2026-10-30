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
    # 'hot' | 'clear' | 'unknown'. The agent's Get-SustainedHeatVerdict rule (ultra-magnus-agent.ps1:2436): hot when throttled > 6 or mean >= 88 C.
    # r5 (sol r4 B1 / adversary A4): the throttled count is tested FIRST, so a mean that is NaN, missing or unparseable never masks a hot count (the feed parser accepts NaN and
    # Infinity, Wait-VenueQuiet.ps1:629). A mean of +Infinity is >= 88 and is hot, as the agent's comparison says. With throttled <= 6, a NaN, -Infinity or missing mean stays
    # unknown (fail closed, as before); so does a missing, unparseable or non-finite throttled count.
    param([AllowNull()][object]$Throttled, [AllowNull()][object]$MeanC)
    if ($null -eq $Throttled) { return 'unknown' }
    $t = 0.0; $m = 0.0
    if (-not [double]::TryParse([string]$Throttled, [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$t)) { return 'unknown' }
    if ([double]::IsNaN($t) -or [double]::IsInfinity($t) -or $t -lt 0) { return 'unknown' }
    if ($t -gt $script:HeatMaxThrottledRows) { return 'hot' }
    if ($null -eq $MeanC) { return 'unknown' }
    if (-not [double]::TryParse([string]$MeanC, [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$m)) { return 'unknown' }
    if ([double]::IsNaN($m) -or [double]::IsNegativeInfinity($m)) { return 'unknown' }
    if ($m -ge $script:HeatMeanMaxC) { 'hot' } else { 'clear' }
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
            # r3 A2: the probe rounds mean_c to 0.01 (87.998 -> 88.0, which the agent's rule calls hot while the exact mean is clear): the exact mean it also writes,
            # mean_c_exact, is the heat decision's value when present; the rounded mean_c only for a probe that predates it
            $meanSrc = $(if ($h.PSObject.Properties['mean_c_exact'] -and $null -ne $h.mean_c_exact) { $h.mean_c_exact } else { $h.mean_c })
            $okM = [double]::TryParse([string]$meanSrc, [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$mean)
            # r5 (B1): an unparseable mean is NaN, not a reason to drop the throttled count: Get-VenueHeatVerdict tests the count first
            if (-not $okM) { $mean = [double]::NaN }
            $v = $(if ($okT -and $okW) { Get-VenueHeatVerdict -Throttled $thr -MeanC $mean } else { 'unknown' })
            if ($v -cne 'unknown') {
                return [pscustomobject]@{ heat = $v; throttled = $thr; window = $win; meanC = $mean; text = "throttled $thr/$win, mean $($mean.ToString('0.00#', $script:Inv)) C"; source = 'raw-json' }
            }
        }
    }
    $line = @($Lines | Where-Object { $_ -match '^\s*tele heat ' }) | Select-Object -Last 1
    $none = [pscustomobject]@{ heat = 'unknown'; throttled = $null; window = $null; meanC = $null; text = $(if ($line) { ([string]$line).Trim() } else { 'no tele heat line' }); source = 'none' }
    if (-not $line) { return $none }
    # r3 pass 2 A4: the feed parser accepts signed temperatures (a sensor's -1 error value), so the mean may carry a sign: unsigned only, the line read unknown while throttled=7/60 is hot
    # r5 (B1): and it accepts NaN and Infinity, which the display prints as NaN, the culture's infinity symbol (U+221E) or Infinity: any non-numeric mean token is read as a
    # non-finite mean, so the throttled count that follows it still decides (hot > 6) instead of the whole line reading unknown
    # r5 pass 1 A1: a probe whose telemetry went through its JSON round trip holds the non-finite mean as a string, which its display prints as 'n/a' with NO unit
    # (Wait-VenueQuiet.ps1 Format-TelNum) and no mean_exact: 'mean=n/a throttled=7/60' is a non-finite mean with a readable count, so the count decides
    $m = [regex]::Match([string]$line, '\smean=(?:(\S+?)C|n/a)\s+throttled=([0-9]+)/([0-9]+)(?:\s|$)')
    if (-not $m.Success) { return $none }
    $mean = 0.0; $thr = 0; $win = 0
    # r3 A2: the display mean is rounded to 0.1 C (87.96 prints 88.0): a probe that also prints mean_exact=<R> gives the exact value, which is what the heat rule is applied to
    # r6 (adversary r5 A3): the probe prints mean_exact=Infinity / -Infinity / NaN for a non-finite mean (Wait-VenueQuiet.ps1 Format-TelMeanExact); a signed +Infinity is read too.
    # +Infinity is hot as on the raw-json path; NaN and -Infinity never decide alone (Get-VenueHeatVerdict)
    $mx = [regex]::Match([string]$line, '\smean_exact=(-?[0-9]+(?:[.,][0-9]+)?(?:[eE][-+]?[0-9]+)?|NaN|[-+]?Infinity)(?:\s|$)')
    $meanText = $(if ($mx.Success) { $mx.Groups[1].Value } elseif ($m.Groups[1].Success) { $m.Groups[1].Value } else { 'n/a' })
    $inf = [string][char]0x221E
    if ($meanText -ceq $inf -or $meanText -ceq "+$inf") { $mean = [double]::PositiveInfinity }
    elseif ($meanText -ceq "-$inf") { $mean = [double]::NegativeInfinity }
    elseif ($meanText -ceq '+Infinity') { $mean = [double]::PositiveInfinity }
    elseif ($meanText -match '^-?[0-9]+(?:[.,][0-9]+)?(?:[eE][-+]?[0-9]+)?$' -or $meanText -cin @('NaN', 'Infinity', '-Infinity')) {
        if (-not [double]::TryParse(($meanText -replace ',', '.'), [System.Globalization.NumberStyles]::Float, $script:Inv, [ref]$mean)) { return $none }
    }
    else { $mean = [double]::NaN }
    if (-not [int]::TryParse($m.Groups[2].Value, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$thr)) { return $none }   # advisory A10: an overflow is unknown, never a throw
    if (-not [int]::TryParse($m.Groups[3].Value, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$win)) { return $none }
    [pscustomobject]@{ heat = (Get-VenueHeatVerdict -Throttled $thr -MeanC $mean); throttled = $thr; window = $win; meanC = $mean; text = "throttled $thr/$win, mean $($mean.ToString('0.0##', $script:Inv)) C"; source = 'tele-line' }
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

function Get-VenueThermalProbeBudget {
    # r3 (adversary A1, Dot B2): the ONE decision "may another thermal re-probe start, and how long may the runner sleep first". Every probe start of an active thermal wait goes
    # through it: the retry sleep after a busy probe and the poll sleep after a hot one alike. The deadline is -CapSec from the first hot reading; -ElapsedSec is the time since it.
    # -SlowestReprobeSec is the slowest RE-probe seen in this wait. The probe that made the first hot reading is not a re-probe and is not counted (Dot B2: a 255 s first probe
    # under cap 180 used to leave no room and skipped the leg with no cooling at all): the first hot reading always gets a cooling sleep and one re-probe unless the deadline
    # has truly passed. A later re-probe is started only if the slowest so far predicts it ends inside the deadline: fitSec = cap - elapsed - slowest, and the sleep is
    # min(-WantSleepSec, fitSec), so the next probe starts no later than deadline - slowest. A probe's own duration cannot be bounded from the runner: the budget is
    # cap + one probe (the receipt names the excess as thermalWaitOvershootSec).
    # r4 (adversary r3 pass 2 A1, Dot c5946322): the sleep length above is only a PLAN. A log append that stalls or a wakeup that comes late moves the real start past it, so
    # the same function also answers mayDispatchNow, asked by the runner with the clock read IMMEDIATELY before the probe is dispatched (after any log write and any sleep):
    # the probe is admitted only if it starts no later than deadline - slowest, plus -DispatchGraceSec (default 1) for the whole-second elapsed the plan was computed from.
    # An expired deadline therefore ends the wait as a skip with no further probe: the soft budget is cap + at most ONE probe already started before the deadline, plus
    # that second; runner delay never buys another probe.
    param(
        [Parameter(Mandatory)][ValidateRange(0, 86400)][int]$CapSec,
        [Parameter(Mandatory)][ValidateRange(0, [int]::MaxValue)][int]$ElapsedSec,
        [Parameter(Mandatory)][ValidateRange(0, [int]::MaxValue)][int]$SlowestReprobeSec,
        [Parameter(Mandatory)][ValidateRange(1, [int]::MaxValue)][int]$WantSleepSec,
        [ValidateRange(0, 60)][int]$DispatchGraceSec = 1
    )
    $fit = [long]$CapSec - [long]$ElapsedSec - [long]$SlowestReprobeSec
    $may = ($fit -ge 1)
    $dispatch = ($CapSec -ge 1 -and ($fit + [long]$DispatchGraceSec) -ge 0)
    [pscustomobject]@{ mayStartProbe = $may; mayDispatchNow = $dispatch; fitSec = [int][Math]::Max([long][int]::MinValue, [Math]::Min([long][int]::MaxValue, $fit)); sleepSec = $(if ($may) { [int][Math]::Max(1L, [Math]::Min([long]$WantSleepSec, $fit)) } else { 0 }) }
}

function Get-VenueThermalNowUtc {
    # adversary r2b A4: the thermal wait is timed on the monotonic Stopwatch clock, not the wall clock. The wall clock can step (NTP, a VM resume, an operator), and a step
    # back made the elapsed wait read 0 (the cap not consumed, the next quiet wait too long, the overshoot receipt 0). This returns the instant the gate should treat as
    # now: -FirstHotUtc plus the Stopwatch time since -FirstHotTimestamp ([Diagnostics.Stopwatch]::GetTimestamp() taken when the first hot reading was recorded).
    param(
        [Parameter(Mandatory)][DateTime]$FirstHotUtc,
        [Parameter(Mandatory)][long]$FirstHotTimestamp,
        [long]$NowTimestamp = [Diagnostics.Stopwatch]::GetTimestamp()
    )
    # r3 (advisory A4): a first-hot date near DateTime.MaxValue cannot take the added seconds: it is then simply the latest instant, never a throw
    try { $FirstHotUtc.AddSeconds([Math]::Max(0.0, ($NowTimestamp - $FirstHotTimestamp) / [double][Diagnostics.Stopwatch]::Frequency)) } catch { [DateTime]::SpecifyKind([DateTime]::MaxValue, $FirstHotUtc.Kind) }
}

function Get-VenueThermalElapsedSec {
    # r4 (adversary r4 A3): whole seconds on the monotonic Stopwatch clock since -FirstHotTimestamp. The deadline admission reads it with -Ceiling, so a floored 181.5 s never
    # reads as 181 and the one second of grace in Get-VenueThermalProbeBudget stays one second (the soft budget is cap + one probe + 1 s, not + almost 2 s).
    param(
        [Parameter(Mandatory)][long]$FirstHotTimestamp,
        [long]$NowTimestamp = [Diagnostics.Stopwatch]::GetTimestamp(),
        [switch]$Ceiling
    )
    $d = [Math]::Max(0.0, ($NowTimestamp - $FirstHotTimestamp) / [double][Diagnostics.Stopwatch]::Frequency)
    [int][Math]::Min([double][int]::MaxValue, $(if ($Ceiling) { [Math]::Ceiling($d) } else { [Math]::Floor($d) }))
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
        # r8 (adversary r8 A3): every field is read through PSObject.Properties, so a row missing one is a barrier (no status, no key), never a strict-mode throw
        $f = @{}; foreach ($p in 'batch', 'key', 'status', 'heat') { $f[$p] = $(if ($r.PSObject.Properties[$p]) { $r.$p } else { $null }) }
        # r8 (adversary efe7699b A3): a row with no batch is a lease of its own, so two such rows are never on "the same" lease
        # r11 (sol r10 B1): an empty or whitespace batch is no identity either (two of them were one lease), so it is a lease of its own exactly like a missing one
        if ($null -eq $f.batch -or [string]::IsNullOrWhiteSpace([string]$f.batch)) { $lastBatch = $null; $lastRanKey = $null }
        # r12 (adversary r11 A2): the batch is compared ORDINALLY: PowerShell -ceq / -cne are case-sensitive but still culture-aware, so a soft hyphen (U+00AD) or another ignorable character made two distinct leases equal
        elseif ($lastBatch -isnot [string] -or -not [string]::Equals([string]$f.batch, $lastBatch, [StringComparison]::Ordinal)) { $lastBatch = [string]$f.batch; $lastRanKey = $null }
        if ([string]$f.heat -ceq 'hot' -and $null -ne $lastRanKey) { $heavy[$lastRanKey] = 1 + $(if ($heavy.Contains($lastRanKey)) { $heavy[$lastRanKey] } else { 0 }) }
        # adversary r1b A5: the predecessor is the last leg that LAUNCHED (RAN, LEG_FAILED, LEASE_EXPIRED); a row of unknown identity (a $null key) is a barrier, so
        # heat is never charged across it. A leg that never launched (THERMAL_HOT_SKIPPED, a RETRY whose own re-check failed) leaves the predecessor as it was.
        # r6 (adversary r6 A2): only a row KNOWN to have launched becomes the predecessor; one known never to have launched (NOT_RUN too) is passed over, and any other status (null,
        # empty, one this reader does not know) may or may not have launched, so it is a barrier like a row of unknown identity: heat after it is charged to nobody
        # r9 (sol r8 B3): the null-key barrier holds for EVERY status: a null-key (or empty-key) RETRY, THERMAL_HOT_SKIPPED or NOT_RUN row was passed over like a keyed one
        $st = [string]$f.status
        if (-not $f.key) { $lastRanKey = $null }
        elseif ($st -cin @('RAN', 'LEG_FAILED', 'LEASE_EXPIRED')) { $lastRanKey = [string]$f.key }
        elseif ($st -cnotin @('THERMAL_HOT_SKIPPED', 'RETRY', 'NOT_RUN')) { $lastRanKey = $null }
    }
    $heavy
}

function Resolve-VenueHistoryChild {
    # r3 (pass 1 A2): -Dir\-Child as an absolute path, string-only. A relative -Dir (-HeatFrom '.') is relative to the POWERSHELL location, which File.ReadAllLines does not follow
    # (it uses the process directory), so it is resolved here. r5 (sol r4 B2 / adversary A6): the resolution is the PowerShell provider's own,
    # GetUnresolvedProviderPathFromPSPath, so a drive-relative -Dir ('C:.', 'C:sub') follows that drive's PowerShell location too (Path.Combine and GetFullPath resolved it against
    # the PROCESS directory). It is string-only: the path is not required to exist. A -Dir that does not resolve to a FileSystem path (the location sits in the registry) is relative
    # to the FileSystem location; a fully qualified path on a drive PowerShell has not registered is taken as written. A path that cannot be built (an empty entry, illegal
    # characters, an unmounted drive) is $null, never a throw: the caller skips and names it.
    param([AllowNull()][AllowEmptyString()][string]$Dir, [Parameter(Mandatory)][string]$Child)
    try {
        if ([string]::IsNullOrWhiteSpace($Dir)) { return $null }
        $abs = $null
        try {
            $prov = $null; $drv = $null
            $r = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir, [ref]$prov, [ref]$drv)
            if ($null -ne $prov -and $prov.Name -ceq 'FileSystem') { $abs = $r }
        } catch [System.Management.Automation.DriveNotFoundException] {
            if ([IO.Path]::IsPathFullyQualified($Dir)) { $abs = $Dir }
        }
        if ($null -eq $abs) { $abs = [IO.Path]::Combine((Get-Location -PSProvider FileSystem).ProviderPath, $Dir) }
        [IO.Path]::GetFullPath([IO.Path]::Combine($abs, $Child))
    } catch { $null }
}

function Read-VenueLegHeatHistory {
    # The leg rows of earlier lease-batch runs, in run order, for Get-VenueHeavyLegKeys. Each -RunDir's batches.jsonl (mlv-app/venue-lease-batch/v1); a row's
    # heat is its own heatAtProbe (written by this gate), else its own heatAtGrant (the agent's admission.heat, which the runner records on the row), else the
    # agent's admission.heat from agent-result-<jobId>.json beside it, else 'unknown'. r7 (adversary r7 A2): heatAtGrant was never read, so a history
    # without its sidecars read a hot grant as unknown. The agent's heat (either source) is taken only from a row not marked off the batch's lease.
    # A dir with no batches.jsonl, or an unreadable line, is skipped and named in .skipped (never a throw).
    param([Parameter(Mandatory)][AllowEmptyCollection()][AllowEmptyString()][string[]]$RunDir)
    $rows = [System.Collections.Generic.List[object]]::new(); $skipped = [System.Collections.Generic.List[string]]::new()
    $d = 0
    foreach ($dir in $RunDir) {
        $d++
        # adversary r3 A3: every path is built and probed INSIDE the protected read. Join-Path resolves the drive and throws DriveNotFoundException for an unmounted one (and for an
        # empty entry), which refused the whole plan as LEG_PLAN_INVALID; [IO.Path]::Combine is string-only, and a path that cannot be built or probed is skipped and named
        $f = Resolve-VenueHistoryChild -Dir $dir -Child 'batches.jsonl'
        $isFile = $(if ($null -eq $f) { $false } else { try { [bool](Test-Path -LiteralPath $f -PathType Leaf) } catch { $false } })
        if (-not $isFile) { $skipped.Add("$dir (no batches.jsonl)"); continue }
        # adversary r1 A7: a file that cannot be read (held exclusively, gone) is skipped and named, never a throw that refuses the whole plan
        $lines = try { [IO.File]::ReadAllLines($f) } catch { $skipped.Add("$f (unreadable: $($_.Exception.Message -replace '\s+', ' '))"); continue }
        $lineNo = 0
        foreach ($line in $lines) {
            if (-not $line.Trim()) { continue }
            $b = try { $line | ConvertFrom-Json } catch { $null }
            if ($null -eq $b -or $b -isnot [System.Management.Automation.PSCustomObject] -or -not $b.PSObject.Properties['legs']) { $skipped.Add("$f (unreadable line)"); continue }
            # adversary r1 A8: one lease is one batch receipt (token), not one batch number: a reused -RunDir numbers its batches from 1 again
            # r8 (adversary efe7699b A2): a receipt with no token is a lease of its own (its line), never grouped with another by batch number: two leases L1 and L2
            # both numbered batch 1 became one sequence and L2's hot first leg was charged to L1's last
            $lineNo++
            # r11 (sol r10 B1 / adversary A2): the identity is the lease (leaseId) PLUS a normalised, non-empty token, length-framed so no leaseId/token pair can spell another's. A
            # whitespace token is no token, and the generated fallback ("line:<n>") lives in its own namespace ("tok:" never starts it), so neither can equal another lease's identity
            $tokenText = $(if ($b.PSObject.Properties['token'] -and $null -ne $b.token) { ([string]$b.token).Trim() } else { '' })
            # r12 (adversary r11 A1 / A2): the leaseId is the RAW string, never trimmed (' L1' is not 'L1'), and the identity is compared ordinally (Get-VenueHeavyLegKeys). A non-empty but
            # whitespace-only leaseId is no identity at all, so it is a lease of its own (a barrier) like a missing token; an absent or empty one is a receipt older than the lease id,
            # still identified by its token
            $leaseText = $(if ($b.PSObject.Properties['leaseId'] -and $null -ne $b.leaseId) { [string]$b.leaseId } else { '' })
            $leaseBlank = ($leaseText.Length -gt 0 -and [string]::IsNullOrWhiteSpace($leaseText))
            $lease = $(if ($tokenText -and -not $leaseBlank) { "tok:$($leaseText.Length):${leaseText}:$tokenText" } else { "line:$lineNo" })
            $leaseLabel = $(if ($tokenText) { $tokenText } else { "line $lineNo" })
            $unnamed = 0
            foreach ($l in @($b.legs)) {
                # adversary r1 A5: a scalar-run row carries no leg name (Add-PlanIdentity names -LegPlan rows only): counted and named in .skipped, never silently dropped.
                # r1b A5: it stays in the sequence as a barrier row (key $null), so a hot leg after it is never charged to the leg before it.
                $bad = ($null -eq $l -or $l -isnot [System.Management.Automation.PSCustomObject] -or -not $l.PSObject.Properties['name'] -or -not $l.name -or -not $l.PSObject.Properties['status'])
                if ($bad) { $unnamed++; $rows.Add([pscustomobject]@{ batch = "$d.$lease"; key = $null; name = $null; status = 'UNKNOWN'; heat = 'unknown' }); continue }
                $backend = $(if ($l.PSObject.Properties['backend']) { [string]$l.backend } else { '' })
                $arm = $(if ($l.PSObject.Properties['arm']) { [string]$l.arm } else { '' })
                $heat = 'unknown'
                # r7 (adversary r7 A1 on 83fd79de): the agent's heat (heatAtGrant, or the sidecar's admission.heat) was read at the job's grant, which a row with onLease
                # other than exactly true held on ANOTHER lease (a replacement after LEASE_LOST): charged to this lease's predecessor, it made a key heavy from a reading
                # it never followed. Such a row keeps only the probe's reading, which is always this lease's.
                # r8 (adversary r8 A2): a row with NO onLease field (LEG_FAILED, LEASE_EXPIRED: the runner never read their agent result) in a batch that names its leaseId
                # is not known to be on that lease: its recorded grant heat is not taken, and its sidecar's admission.heat only when the sidecar itself shows the job on the
                # batch's lease, by the runner's own onLease rule (admission.leaseBatch.leaseId ordinally equal to the batch's leaseId and slotContinuity not 'lost'). A batch with no
                # leaseId (receipts older than the lease id) reads as before.
                $batchLease = $(if ($b.PSObject.Properties['leaseId'] -and $b.leaseId) { [string]$b.leaseId } else { $null })
                $hasOnLease = [bool]$l.PSObject.Properties['onLease']
                $offLease = ($hasOnLease -and -not ($l.onLease -is [bool] -and $l.onLease))
                $needLeaseProof = (-not $hasOnLease -and $null -ne $batchLease)
                if ($l.PSObject.Properties['heatAtProbe'] -and [string]$l.heatAtProbe -cin @('hot', 'clear')) { $heat = [string]$l.heatAtProbe }
                elseif ($offLease) { }
                elseif (-not $needLeaseProof -and $l.PSObject.Properties['heatAtGrant'] -and [string]$l.heatAtGrant -cin @('hot', 'clear')) { $heat = [string]$l.heatAtGrant }
                elseif ($l.PSObject.Properties['jobId'] -and $l.jobId) {
                    $ar = Resolve-VenueHistoryChild -Dir $dir -Child "agent-result-$($l.jobId).json"
                    # r3 pass 1 A1: `(try { } catch { })` is not an expression (PowerShell ran `try` as a command): the probe is its own statement
                    $arOk = $(if ($null -eq $ar) { $false } else { try { [bool](Test-Path -LiteralPath $ar -PathType Leaf) } catch { $false } })
                    if ($arOk) {
                        $a = try { [IO.File]::ReadAllText($ar) | ConvertFrom-Json } catch { $null }
                        $adm = $(if ($null -ne $a -and $a.PSObject.Properties['admission'] -and $a.admission -is [System.Management.Automation.PSCustomObject]) { $a.admission } else { $null })
                        $sameLease = $true
                        if ($needLeaseProof) {
                            $lb = $(if ($null -ne $adm -and $adm.PSObject.Properties['leaseBatch'] -and $adm.leaseBatch -is [System.Management.Automation.PSCustomObject]) { $adm.leaseBatch } else { $null })
                            $jl = $(if ($null -ne $lb -and $lb.PSObject.Properties['leaseId'] -and $null -ne $lb.leaseId) { [string]$lb.leaseId } else { $null })
                            $sc = $(if ($null -ne $adm -and $adm.PSObject.Properties['slotContinuity']) { [string]$adm.slotContinuity } else { '' })
                            # r12 (adversary r11 A2): ordinal, on the raw ids; a blank batch lease id can never be proven
                            $sameLease = ($null -ne $jl -and -not [string]::IsNullOrWhiteSpace($batchLease) -and [string]::Equals($jl, $batchLease, [StringComparison]::Ordinal) -and $sc -cne 'lost')
                        }
                        if ($sameLease -and $null -ne $adm -and $adm.PSObject.Properties['heat'] -and [string]$adm.heat -cin @('hot', 'clear')) { $heat = [string]$adm.heat }
                    }
                }
                $rows.Add([pscustomobject]@{ batch = "$d.$lease"; key = (Get-VenueLegKeyFromName -Name ([string]$l.name) -Backend $backend -Arm $arm); name = [string]$l.name; status = [string]$l.status; heat = $heat })
            }
            if ($unnamed -gt 0) { $skipped.Add("$f lease $leaseLabel ($unnamed leg rows with no leg name or status: a scalar run's rows cannot be keyed)") }
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
    # r3 pass 2 A2: with more heavy items than light ones plus one, the EXCESS (the last heavy items in the given order) is set aside and appended at the end, so the rest is spread
    # completely and the unavoidable adjacencies are exactly H - L - 1 (the greedy pick used to spend them early: L1,H1,H2,H3 gave two where H1,L1,H2,H3 needs one)
    $defer = [System.Collections.Generic.List[int]]::new()
    $excess = $h - ($l + 1)
    if ($excess -gt 0) {
        $heavyAt = @(for ($i = 0; $i -lt $Items.Count; $i++) { if ($Heavy[$i]) { $i } })
        foreach ($i in @($heavyAt | Select-Object -Last $excess)) { $defer.Add([int]$i); [void]$left.Remove([int]$i) }
        $h -= $excess
    }
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
    foreach ($i in $defer) { $out.Add($i) }
    $newHeavy = [bool[]]@($out | ForEach-Object { $Heavy[$_] })
    [pscustomobject]@{ items = @($out | ForEach-Object { , $Items[$_] }); heavy = $newHeavy; heavyAdjacent = (Measure-VenueHeavyAdjacency -Heavy $newHeavy); heavyAdjacentBefore = (Measure-VenueHeavyAdjacency -Heavy $Heavy) }
}

Export-ModuleMember -Function Get-VenueHeatVerdict, ConvertFrom-VenueProbeHeatLine, Get-VenueThermalGateStep, Get-VenueThermalProbeBudget, Get-VenueThermalNowUtc, Get-VenueThermalElapsedSec, Get-VenueLegKeyFromName, Get-VenueHeavyLegKeys, Read-VenueLegHeatHistory,
    Measure-VenueHeavyAdjacency, Get-VenueThermalSpreadOrder
