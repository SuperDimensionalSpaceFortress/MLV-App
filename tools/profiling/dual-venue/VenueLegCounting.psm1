# VenueLegCounting.psm1 -- VENUE-LEG-INLEASE-REQUALIFY-1. The pure leg-counting rules for bench analyzers.
# No tracked driver or analyzer imports it (test_venue_leg_inlease_requalify.py test_24 re-derives that from tools/ and holds this line to it);
# its first importer is the untracked CPU-17V9-FPS-GAP-AB-1 r7 run-dir analyze.ps1 (CPU-17V9-R7-PREP-1).
#
# Pre-registered rule 'inlease-requalify-1' (HUB RULING, .claude-state/kernel/subject-ledger.md, tool stamp 2026-10-24T15:40:56Z, action (1)):
# a leg whose admission wait exceeded 180 s counts only if it already met every existing condition (class QUIET_PRELOAD_OK) AND its in-job,
# post-grant pre-load is <= 30.0 % AND it was throttled 0 at the lease grant. Every other stale leg is typed NOT_COUNTED_<reason>.
# It applies from round r7. Rows from r1-r6 keep their rule, 'stale-discard-180' (wait > 180 s = QUIET_PRELOAD_STALE, not counted);
# no r1-r6 row is re-counted. The <= 180 s path, the cooldown gate (COOLDOWN_UNMET / pre-load <= 30 %) and a missing wait are unchanged.
#
# Sources (fork/master 04c65cb7):
#   * admission.waitedSec = lease-grant time minus admission start: tools/profiling/bachelor/ultra-magnus-agent.ps1:2781 (lease mode),
#     :2207 (status mode). Lease mode records heat at grant and does not gate it: admission.thermal = heat.why (:2784, :2787).
#   * the thermal string: "throttled N/W, mean X C, max15 Y C, Z W" (ultra-magnus-agent.ps1:1736); a hot verdict prefixes it with
#     "sustained heat: " (:1737-1738); feed-unknown is the bare why text (:1733-1734). Status mode also has a numeric throttled/rows (:2210).
#   * the in-job pre-load of a chain leg: tools/profiling/bachelor/playback-attr-3-cuda-job.ps1:2588-2613 (3 samples after admission,
#     before the smoke launch at :2964-2966), published as summary.json cpuQuiescence.timeMeanPercent / timeUnknown (:3946-3949),
#     read lane-side by tools/profiling/dual-venue/Invoke-VenueChain.ps1:264-276 and classed at :556 (QUIET_PRELOAD_OK when <= 30.0).
#   * Block A (reference-frame-runner-job.ps1) has no in-job pre-load sample, so a stale ref leg is NOT_COUNTED_STALE_NO_INLEASE_PROBE.
#   * thresholds are reused, never new: 180 s (lane-CPU-17V9-FPS-GAP-AB-1-r6 venue-cpu17v9-ab.ps1:121 $StaleAdmissionSec, analyze.ps1:76,153);
#     30.0 % (Invoke-VenueChain.ps1:556, docs/cuda-playback-present-cadence.md:206-207); throttled = 0 at grant (the ruling above).
#   * the Look Assist arm check is the r6 lane analyze.ps1:100-106 logic, with a missing field never proven.
#
# Enum parameters (Rule, Kind, Arm) are CASE-EXACT: ValidateSet(..., IgnoreCase = $false). A case variant is rejected at the parameter
# binder, so it can never take a branch other than its canonical form's (dot 80f0e297 B1: the -ceq tests below used to disagree with
# the case-insensitive binder). A value that is not a measurement (negative or infinite wait or pre-load, a throttled count that is not
# a whole number >= 0) is refused: NOT_COUNTED_INVALID_MEASUREMENT / thermal-unknown, never rounded and never eligible.
# VENUE-LEG-COUNTING-HARDENING-1: the wait and the pre-load are numeric-typed or $null, as throttled already was. A string or bool is not a
# measurement and is NOT_COUNTED_INVALID_MEASUREMENT under both rules (a [Nullable[double]] binder turned '' and '   ' into 0, so an empty
# wait counted and an empty pre-load requalified). A NaN pre-load is NOT_COUNTED_INVALID_MEASUREMENT (it was refused as PRELOAD_HIGH);
# a NaN wait stays a missing wait (QUIET_PRELOAD_UNVERIFIED_ADMISSION). No finite numeric input changes class.
#
# Pure: no file, network or share access, no process start. Callers pass values they have already read.

Set-StrictMode -Version Latest

$script:StaleAdmissionSec = 180.0
$script:InLeasePreLoadMaxPercent = 30.0
$script:Inv = [System.Globalization.CultureInfo]::InvariantCulture

function Test-VenueLegCountingNonNegativeInteger([object]$Value) {
    # A numeric type holding a whole number in [0, Int32.MaxValue]. A fraction, a negative, NaN or Infinity is not a count.
    if (-not (Test-VenueLegCountingFiniteNonNegative $Value)) { return $false }
    $d = [double]$Value
    ($d -le [int]::MaxValue) -and ($d -eq [math]::Floor($d))
}

function Get-VenueLegCountingProperty([object]$Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) { if ($Object.Contains($Name)) { return $Object[$Name] } else { return $null } }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    $p.Value
}

function Test-VenueLegCountingNumericType([object]$Value) {
    # A numeric type only: a string (empty, blank or numeric-looking) or a bool is not a measurement.
    ($null -ne $Value) -and ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal] -or $Value -is [single] -or $Value -is [int16] -or $Value -is [byte])
}

function Test-VenueLegCountingFiniteNonNegative([object]$Value) {
    # A numeric type only (a string or bool is not a measurement), finite and >= 0.
    if (-not (Test-VenueLegCountingNumericType $Value)) { return $false }
    $d = [double]$Value
    (-not [double]::IsNaN($d)) -and (-not [double]::IsInfinity($d)) -and $d -ge 0
}

function Get-VenueLegCountingRule {
    # 'r1'..'r6' -> stale-discard-180; 'r7' or higher -> inlease-requalify-1; anything else -> stale-discard-180 (the stricter fallback).
    param([AllowEmptyString()][AllowNull()][string]$Round)
    $m = [regex]::Match([string]$Round, '^r(\d+)$')
    if ($m.Success) {
        $n = 0L
        if ([long]::TryParse($m.Groups[1].Value, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$n) -and $n -ge 7) { return 'inlease-requalify-1' }
    }
    'stale-discard-180'
}

function ConvertFrom-VenueAdmissionThermal {
    # Throttled rows at grant. A numeric admission.throttled wins; else the thermal string; else unknown.
    # A present admission.throttled that is not a non-negative whole number is unknown: never rounded, never replaced by the string.
    param([AllowNull()][object]$Admission)
    $t = Get-VenueLegCountingProperty $Admission 'throttled'
    if ($null -ne $t) {
        if (-not (Test-VenueLegCountingNonNegativeInteger $t)) { return @{ throttled = $null; window = $null; known = $false; source = 'numeric-invalid' } }
        $w = Get-VenueLegCountingProperty $Admission 'rows'
        return @{ throttled = [int]$t; window = $(if (Test-VenueLegCountingNonNegativeInteger $w) { [int]$w } else { $null }); known = $true; source = 'numeric' }
    }
    $s = Get-VenueLegCountingProperty $Admission 'thermal'
    if ($s -is [string] -and $s) {
        $m = [regex]::Match($s, '^(?:sustained heat: )?throttled (\d+)/(\d+),')
        $n = 0; $win = 0
        if ($m.Success -and [int]::TryParse($m.Groups[1].Value, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$n) -and
            [int]::TryParse($m.Groups[2].Value, [System.Globalization.NumberStyles]::None, $script:Inv, [ref]$win)) {
            return @{ throttled = $n; window = $win; known = $true; source = 'thermal-string' }
        }
    }
    @{ throttled = $null; window = $null; known = $false; source = 'none' }
}

function Get-VenueLegRequalification {
    param(
        [Parameter(Mandatory)][ValidateSet('stale-discard-180', 'inlease-requalify-1', IgnoreCase = $false)][string]$Rule,
        [Parameter(Mandatory)][ValidateSet('chain', 'ref', IgnoreCase = $false)][string]$Kind,
        [Parameter(Mandatory)][AllowEmptyString()][string]$BaseClass,
        [AllowNull()][object]$AdmissionWaitedSec,
        [AllowNull()][object]$Admission,
        [AllowNull()][object]$InLeasePreLoadMean
    )
    # Numeric-typed or $null only (HARDENING-1): a string or bool never reaches a comparison, so '' cannot become 0.
    $waitIsNumber = Test-VenueLegCountingNumericType $AdmissionWaitedSec
    $wait = $(if ($waitIsNumber) { [double]$AdmissionWaitedSec } else { $null })
    $preIsNumber = Test-VenueLegCountingNumericType $InLeasePreLoadMean
    $pre = $(if ($preIsNumber) { [double]$InLeasePreLoadMean } else { $null })
    $th = ConvertFrom-VenueAdmissionThermal -Admission $Admission
    $row = [pscustomobject]@{
        rule = $Rule; class = $BaseClass; eligible = $false; notCountedReason = $null; requalified = $false
        throttledAtGrant = $(if ($th.known) { $th.throttled } else { $null }); thermalKnown = [bool]$th.known
    }
    $class = $null
    if ($BaseClass -cne 'QUIET_PRELOAD_OK') {
        $class = $BaseClass   # the cooldown gate is not relaxed: any other base class passes through, not eligible
    } elseif ($null -ne $AdmissionWaitedSec -and -not $waitIsNumber) {
        $class = 'NOT_COUNTED_INVALID_MEASUREMENT'   # a string or bool wait is not a measurement, under both rules
    } elseif ($null -eq $wait -or [double]::IsNaN($wait)) {
        $class = 'QUIET_PRELOAD_UNVERIFIED_ADMISSION'
    } elseif ([double]::IsInfinity($wait) -or $wait -lt 0) {
        $class = 'NOT_COUNTED_INVALID_MEASUREMENT'   # a negative or infinite wait is not a measurement, under both rules; it never reaches the <= 180 s path
    } elseif ($wait -le $script:StaleAdmissionSec) {
        $row.class = 'QUIET_PRELOAD_OK'; $row.eligible = $true
        return $row
    } elseif ($Rule -ceq 'stale-discard-180') {
        $class = 'QUIET_PRELOAD_STALE'
    } elseif ($null -eq $InLeasePreLoadMean) {
        $class = $(if ($Kind -ceq 'ref') { 'NOT_COUNTED_STALE_NO_INLEASE_PROBE' } else { 'NOT_COUNTED_INLEASE_PROBE_MISSING' })
    } elseif (-not $preIsNumber -or [double]::IsNaN($pre) -or [double]::IsInfinity($pre) -or $pre -lt 0) {
        $class = 'NOT_COUNTED_INVALID_MEASUREMENT'   # a string, bool, NaN, infinite or negative pre-load mean is not a measurement (G2: NaN was typed HIGH)
    } elseif (-not ($pre -le $script:InLeasePreLoadMaxPercent)) {
        $class = 'NOT_COUNTED_INLEASE_PRELOAD_HIGH'
    } elseif (-not $th.known) {
        $class = 'NOT_COUNTED_THERMAL_UNKNOWN'
    } elseif ($th.throttled -gt 0) {
        $class = 'NOT_COUNTED_THROTTLED_AT_GRANT'
    } else {
        $row.class = 'QUIET_PRELOAD_REQUALIFIED_IN_LEASE'; $row.eligible = $true; $row.requalified = $true
        return $row
    }
    $row.class = $class; $row.notCountedReason = $class
    $row
}

function Test-VenueLegLookAssistArm {
    # The r6 lane analyze.ps1:100-106 check. A missing field (empty string, $null flag) is never proven and, alone, never a violation.
    param(
        [Parameter(Mandatory)][ValidateSet('on', 'off', IgnoreCase = $false)][string]$Arm,
        [AllowEmptyString()][AllowNull()][string]$LogLookAssist,
        [AllowEmptyString()][AllowNull()][string]$VisualQualityLookAssist,
        [AllowNull()][Nullable[bool]]$ArgNoLookAssist,
        [AllowNull()][Nullable[bool]]$LookAssistForced
    )
    $la1 = [string]$LogLookAssist; $la2 = [string]$VisualQualityLookAssist
    if ($Arm -ceq 'on') {
        $proven = ($ArgNoLookAssist -eq $false -and $la1 -ceq '1' -and $la2 -ceq '1' -and $LookAssistForced -eq $true)
        $violation = ($la1 -ceq '0' -or $la2 -ceq '0' -or $ArgNoLookAssist -eq $true)
        $bad = 'LOOK_ASSIST_NOT_APPLIED'
    } else {
        $proven = ($ArgNoLookAssist -eq $true -and $la1 -ceq '0' -and $la2 -ceq '0' -and ($null -eq $LookAssistForced -or $LookAssistForced -eq $false))
        $violation = ($la1 -ceq '1' -or $la2 -ceq '1' -or $ArgNoLookAssist -eq $false)
        $bad = 'LOOK_ASSIST_NOT_DISABLED'
    }
    @{ proven = [bool]$proven; violation = [bool]$violation; reason = $(if ($violation) { $bad } elseif ($proven) { 'LOOK_ASSIST_PROVEN' } else { 'LOOK_ASSIST_UNPROVEN' }) }
}

Export-ModuleMember -Function Get-VenueLegCountingRule, ConvertFrom-VenueAdmissionThermal, Get-VenueLegRequalification, Test-VenueLegLookAssistArm
