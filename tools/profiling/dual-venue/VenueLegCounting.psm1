# VenueLegCounting.psm1 -- VENUE-LEG-INLEASE-REQUALIFY-1. The pure leg-counting rules every bench analyzer imports.
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

function Test-VenueLegCountingFiniteNonNegative([object]$Value) {
    # A numeric type only (a string or bool is not a measurement), finite and >= 0.
    if ($null -eq $Value) { return $false }
    if (-not ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal] -or $Value -is [single] -or $Value -is [int16] -or $Value -is [byte])) { return $false }
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
        [AllowNull()][Nullable[double]]$AdmissionWaitedSec,
        [AllowNull()][object]$Admission,
        [AllowNull()][Nullable[double]]$InLeasePreLoadMean
    )
    $th = ConvertFrom-VenueAdmissionThermal -Admission $Admission
    $row = [pscustomobject]@{
        rule = $Rule; class = $BaseClass; eligible = $false; notCountedReason = $null; requalified = $false
        throttledAtGrant = $(if ($th.known) { $th.throttled } else { $null }); thermalKnown = [bool]$th.known
    }
    $class = $null
    if ($BaseClass -cne 'QUIET_PRELOAD_OK') {
        $class = $BaseClass   # the cooldown gate is not relaxed: any other base class passes through, not eligible
    } elseif ($null -eq $AdmissionWaitedSec -or [double]::IsNaN([double]$AdmissionWaitedSec)) {
        $class = 'QUIET_PRELOAD_UNVERIFIED_ADMISSION'
    } elseif ([double]::IsInfinity([double]$AdmissionWaitedSec) -or [double]$AdmissionWaitedSec -lt 0) {
        $class = 'NOT_COUNTED_INVALID_MEASUREMENT'   # a negative or infinite wait is not a measurement; it never reaches the <= 180 s path
    } elseif ([double]$AdmissionWaitedSec -le $script:StaleAdmissionSec) {
        $row.class = 'QUIET_PRELOAD_OK'; $row.eligible = $true
        return $row
    } elseif ($Rule -ceq 'stale-discard-180') {
        $class = 'QUIET_PRELOAD_STALE'
    } elseif ($null -eq $InLeasePreLoadMean) {
        $class = $(if ($Kind -ceq 'ref') { 'NOT_COUNTED_STALE_NO_INLEASE_PROBE' } else { 'NOT_COUNTED_INLEASE_PROBE_MISSING' })
    } elseif (-not [double]::IsNaN([double]$InLeasePreLoadMean) -and ([double]::IsInfinity([double]$InLeasePreLoadMean) -or [double]$InLeasePreLoadMean -lt 0)) {
        $class = 'NOT_COUNTED_INVALID_MEASUREMENT'   # a negative or infinite pre-load mean is not a measurement (NaN keeps refusing as HIGH)
    } elseif (-not ([double]$InLeasePreLoadMean -le $script:InLeasePreLoadMaxPercent)) {
        $class = 'NOT_COUNTED_INLEASE_PRELOAD_HIGH'   # written as -not (<= 30.0) so NaN refuses
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
