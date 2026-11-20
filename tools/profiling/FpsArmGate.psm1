# FpsArmGate.psm1 -- BENCH-LIGHT-ARM-GAIN-GATES-1. Pure helpers, no I/O.
#
# HUB RULING 2026-11-20T05:25:54Z item (2) MEASUREMENT ARM: a presented-fps gain or cost verdict comes only from LIGHT-arm matched
# pairs. A HEAVY-telemetry leg (per-frame stage logging; it costs about 20-27 % presented fps on M16-1457 CUDA fullscreen s1, so its fps
# measures the logger) gives STAGE METRICS ONLY. This module is the one place that decides which arm a leg ran and whether two legs may
# be compared on fps; every gain/cost path in tools/profiling imports it and refuses with a typed reason, so the rule is a guard and not
# a sentence in a prompt.
#
# THE ARM IS DERIVED, NEVER HAND-SET. Inputs, strongest first:
#   a recorded RESULT that carries `telemetryArm` (the job's provenance.json / evidence-manifest.json: what actually ran),
#   the resolved leg SPEC (`generatorArgs.telemetryArm`, the switch the generator reads),
#   the recorded launch ENVIRONMENT of a local smoke (the app's own per-frame-log rule).
# A missing, malformed or contradictory input is UNKNOWN, and UNKNOWN is refused exactly like HEAVY for an fps verdict: an arm nobody can
# prove LIGHT is not LIGHT. (The generator's own default for an absent telemetryArm is HEAVY, playback-attr-3-cuda-job.ps1 -TelemetryArm.)
#
# TYPED REASONS (the strings a refusal carries; tests pin them):
#   fps-verdict-refused:heavy-arm     any leg of the pair is HEAVY (a mixed LIGHT+HEAVY pair is this too)
#   fps-verdict-refused:unknown-arm   no leg is HEAVY, but some leg's arm is UNKNOWN
# What stays allowed: stage metrics from any arm, and the per-leg raw numbers (a single leg's own measurement is not a comparison).
# ASCII only (a PS 5.1 reader must parse this file).

Set-StrictMode -Version Latest

$script:FpsArmHeavyReason = 'fps-verdict-refused:heavy-arm'
$script:FpsArmUnknownReason = 'fps-verdict-refused:unknown-arm'

function Get-FaProp {
    # One property of a hashtable / ordered dictionary / PSCustomObject, $null when absent (StrictMode-safe).
    param([AllowNull()]$Object, [Parameter(Mandatory)][string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) { if ($Object.Contains($Name)) { return $Object[$Name] } else { return $null } }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    $p.Value
}

function New-FpsArm {
    param([Parameter(Mandatory)][ValidateSet('LIGHT', 'HEAVY', 'UNKNOWN')][string]$Arm, [Parameter(Mandatory)][string]$Source, [string]$Reason = '')
    [pscustomobject]@{ arm = $Arm; source = $Source; reason = $Reason }
}

function ConvertTo-FpsArmName {
    # The arm word of a value that is already an arm name or an arm object. Anything else (null, 'light', 'MEDIUM', a number) is UNKNOWN.
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return 'UNKNOWN' }
    $v = $Value
    if ($Value -isnot [string]) { $inner = Get-FaProp $Value 'arm'; if ($null -ne $inner) { $v = $inner } }
    if ($v -is [string] -and $v -cin @('LIGHT', 'HEAVY')) { return [string]$v }
    'UNKNOWN'
}

function Get-FpsArmFromLegSpec {
    <#
    .SYNOPSIS
    The arm a resolved dual-venue leg spec (mlv-app/dual-venue-leg/v1) will run, from generatorArgs.telemetryArm. UNKNOWN when the spec is absent, is an export leg (no playback
    fps), names no generatorArgs / no telemetryArm (the generator then defaults to HEAVY; it is still not proven LIGHT), or names a value other than LIGHT / HEAVY.
    #>
    param([AllowNull()]$Spec)
    if ($null -eq $Spec -or $Spec -is [string] -or $Spec -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'spec' 'no-spec' }
    $legType = Get-FaProp $Spec 'legType'
    if ($legType -is [string] -and $legType -ceq 'export') { return New-FpsArm 'UNKNOWN' 'spec' 'not-a-playback-leg' }
    $ga = Get-FaProp $Spec 'generatorArgs'
    $arm = Get-FaProp $ga 'telemetryArm'
    if ($null -eq $arm) { return New-FpsArm 'UNKNOWN' 'spec' 'telemetryArm-absent (the generator default is HEAVY)' }
    if ($arm -is [string] -and $arm -ceq 'LIGHT') { return New-FpsArm 'LIGHT' 'spec' 'generatorArgs.telemetryArm=LIGHT' }
    if ($arm -is [string] -and $arm -ceq 'HEAVY') { return New-FpsArm 'HEAVY' 'spec' 'generatorArgs.telemetryArm=HEAVY' }
    New-FpsArm 'UNKNOWN' 'spec' 'telemetryArm-invalid (not LIGHT or HEAVY)'
}

function Get-FpsArmFromJobResult {
    <#
    .SYNOPSIS
    The arm a RECORDED venue result carries: an object with a `telemetryArm` field (the job's provenance.json or evidence-manifest.json, both written from the arm that actually ran).
    UNKNOWN when the object is absent or carries no / an invalid field.
    #>
    param([AllowNull()]$Result)
    if ($null -eq $Result -or $Result -is [string] -or $Result -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'result' 'no-result' }
    $arm = Get-FaProp $Result 'telemetryArm'
    if ($null -eq $arm) { return New-FpsArm 'UNKNOWN' 'result' 'result-carries-no-telemetryArm' }
    if ($arm -is [string] -and $arm -ceq 'LIGHT') { return New-FpsArm 'LIGHT' 'result' 'telemetryArm=LIGHT' }
    if ($arm -is [string] -and $arm -ceq 'HEAVY') { return New-FpsArm 'HEAVY' 'result' 'telemetryArm=HEAVY' }
    New-FpsArm 'UNKNOWN' 'result' 'telemetryArm-invalid (not LIGHT or HEAVY)'
}

function Get-FpsArm {
    <#
    .SYNOPSIS
    The arm of one leg from whatever is known: the recorded result and / or the resolved spec. Result alone or spec alone classifies; both must AGREE (a recorded LIGHT result under a
    HEAVY spec, or the reverse, is UNKNOWN: the spec is not what ran, or the result is not this leg's). Neither -> UNKNOWN.
    #>
    param([AllowNull()]$Spec, [AllowNull()]$Result)
    $fromResult = if ($null -ne $Result) { Get-FpsArmFromJobResult $Result } else { $null }
    $fromSpec = if ($null -ne $Spec) { Get-FpsArmFromLegSpec $Spec } else { $null }
    if ($null -eq $fromResult -and $null -eq $fromSpec) { return New-FpsArm 'UNKNOWN' 'none' 'neither a spec nor a result was given' }
    if ($null -eq $fromSpec) { return $fromResult }
    if ($null -eq $fromResult) { return $fromSpec }
    if ($fromResult.arm -ceq 'UNKNOWN') { return New-FpsArm 'UNKNOWN' 'spec+result' "the result carries no usable arm ($($fromResult.reason)); spec says $($fromSpec.arm)" }
    if ($fromSpec.arm -ceq 'UNKNOWN') { return $fromResult }
    if ($fromSpec.arm -cne $fromResult.arm) { return New-FpsArm 'UNKNOWN' 'spec+result' "spec says $($fromSpec.arm) but the recorded result says $($fromResult.arm)" }
    New-FpsArm $fromResult.arm 'spec+result' "spec and recorded result agree on $($fromResult.arm)"
}

function Test-FpsEnvFlagOn {
    # The app's own truthiness (platform/qt/MainWindow.cpp environmentFlagEnabled): set, and not 0 / false / off / no.
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $false }
    $t = ([string]$Value).Trim().ToLowerInvariant()
    ($t -ne '' -and $t -ne '0' -and $t -ne 'false' -and $t -ne 'off' -and $t -ne 'no')
}

function ConvertTo-FpsEnvMap {
    # KEY=VALUE strings, a hashtable / dictionary or a parsed-JSON object -> an ordinal dictionary. $null when the input is none of them.
    param([AllowNull()]$Environment)
    if ($null -eq $Environment) { return $null }
    if (($Environment -is [string]) -and [string]::IsNullOrWhiteSpace($Environment)) { return $null }
    $map = [System.Collections.Generic.Dictionary[string, string]]::new([StringComparer]::Ordinal)
    if ($Environment -is [System.Collections.IDictionary]) {
        foreach ($k in $Environment.Keys) { $map[[string]$k] = [string]$Environment[$k] }
        return $map
    }
    if ($Environment -is [System.Management.Automation.PSCustomObject]) {
        foreach ($p in $Environment.PSObject.Properties) { $map[[string]$p.Name] = [string]$p.Value }
        return $map
    }
    foreach ($e in @($Environment)) {
        $s = [string]$e
        $i = $s.IndexOf('=')
        if ($i -lt 1) { return $null }
        $map[$s.Substring(0, $i)] = $s.Substring($i + 1)
    }
    $map
}

function Get-FpsArmFromEnvironment {
    <#
    .SYNOPSIS
    The arm of a local GUI smoke from its RECORDED launch environment (run-release-gui-smoke.ps1 result.launch.environment, or the KEY=VALUE list a caller passed). The app's rule: per-frame
    rows are logged when MLVAPP_PLAYBACK_SMOKE_TELEMETRY is on and MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG is not 1/true/yes/on. HEAVY when any per-frame diagnostic is on (timeline,
    detailed timeline, stage timing, perf field log, RBF detail timing) or the frame log is on; LIGHT when telemetry is off or only the counters run (frame log opted out). An absent environment
    or an ambiguous opt-out value (the app's two readers disagree on it) is UNKNOWN. An inherited variable the launch did not record is the limit of this reading.
    #>
    param([AllowNull()]$Environment)
    $map = ConvertTo-FpsEnvMap $Environment
    if ($null -eq $map) { return New-FpsArm 'UNKNOWN' 'environment' 'no-environment-recorded' }
    foreach ($k in @('MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY', 'MLVAPP_PLAYBACK_DETAILED_TIMELINE_TELEMETRY', 'MLVAPP_STAGE_TIMING', 'MLVAPP_PERF_FIELD_LOG', 'MLVAPP_PLAYBACK_RBF_DETAIL_TIMING')) {
        if ($map.ContainsKey($k) -and (Test-FpsEnvFlagOn $map[$k])) { return New-FpsArm 'HEAVY' 'environment' "$k is on" }
    }
    $telemetry = $map.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY') -and (Test-FpsEnvFlagOn $map['MLVAPP_PLAYBACK_SMOKE_TELEMETRY'])
    if (-not $telemetry) { return New-FpsArm 'LIGHT' 'environment' 'frame telemetry is off' }
    if (-not $map.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG')) { return New-FpsArm 'HEAVY' 'environment' 'frame telemetry is on and the per-frame log is not opted out' }
    $d = $map['MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG'].Trim().ToLowerInvariant()
    if ($d -in @('1', 'true', 'yes', 'on')) { return New-FpsArm 'LIGHT' 'environment' 'frame telemetry on, per-frame log opted out' }
    if ($d -in @('', '0', 'false', 'off', 'no')) { return New-FpsArm 'HEAVY' 'environment' 'frame telemetry on, per-frame log opt-out is explicitly off' }
    New-FpsArm 'UNKNOWN' 'environment' "per-frame log opt-out value '$d' is read differently by the app's two readers"
}

function Get-FpsPairVerdict {
    <#
    .SYNOPSIS
    May two legs be compared on presented fps? `allowed` only when BOTH are LIGHT. Any HEAVY leg (a mixed LIGHT+HEAVY pair too) -> fps-verdict-refused:heavy-arm; otherwise any UNKNOWN leg ->
    fps-verdict-refused:unknown-arm. `stageMetricsAllowed` is always true: HEAVY legs stay valid for stage attribution. Each side is an arm name, an arm object, or $null (UNKNOWN).
    #>
    param([AllowNull()]$Baseline, [AllowNull()]$Candidate, [string]$BaselineName = 'baseline', [string]$CandidateName = 'candidate')
    $b = ConvertTo-FpsArmName $Baseline
    $c = ConvertTo-FpsArmName $Candidate
    $why = { param($n, $a, $x) $r = if ($null -ne $x) { Get-FaProp $x 'reason' } else { $null }; "$n=$a" + $(if ($r) { " ($r)" } else { '' }) }
    $detail = "$(& $why $BaselineName $b $Baseline); $(& $why $CandidateName $c $Candidate)"
    $reason = ''
    if ($b -ceq 'HEAVY' -or $c -ceq 'HEAVY') {
        $reason = $script:FpsArmHeavyReason
        if (($b -ceq 'LIGHT') -or ($c -ceq 'LIGHT')) { $detail = "mixed pair; $detail" }
    } elseif ($b -ceq 'UNKNOWN' -or $c -ceq 'UNKNOWN') {
        $reason = $script:FpsArmUnknownReason
    }
    [pscustomobject]@{
        allowed = ($reason -ceq '')
        reason = $reason
        detail = $detail
        baselineArm = $b
        candidateArm = $c
        stageMetricsAllowed = $true
    }
}

function Get-FpsArmsVerdict {
    # The same rule over any number of named legs (a matrix, a plan): allowed only when every leg is LIGHT. Legs: array of [pscustomobject]@{ name; arm }.
    param([AllowNull()][object[]]$Legs)
    $legs = @($Legs | Where-Object { $null -ne $_ })
    $names = @($legs | ForEach-Object { [pscustomobject]@{ name = [string](Get-FaProp $_ 'name'); arm = (ConvertTo-FpsArmName (Get-FaProp $_ 'arm')) } })
    if ($names.Count -eq 0) {
        return [pscustomobject]@{ allowed = $false; reason = $script:FpsArmUnknownReason; detail = 'no legs'; heavy = @(); unknown = @(); stageMetricsAllowed = $true }
    }
    $heavy = @($names | Where-Object { $_.arm -ceq 'HEAVY' } | ForEach-Object { $_.name })
    $unknown = @($names | Where-Object { $_.arm -ceq 'UNKNOWN' } | ForEach-Object { $_.name })
    $reason = if ($heavy.Count -gt 0) { $script:FpsArmHeavyReason } elseif ($unknown.Count -gt 0) { $script:FpsArmUnknownReason } else { '' }
    [pscustomobject]@{
        allowed = ($reason -ceq ''); reason = $reason
        detail = "heavy=[$($heavy -join ',')] unknown=[$($unknown -join ',')] legs=$($names.Count)"
        heavy = $heavy; unknown = $unknown; stageMetricsAllowed = $true
    }
}

function Get-FpsLegPlanRefusal {
    <#
    .SYNOPSIS
    The leg-plan checker's rule (Invoke-VenueLeaseBatch -LegPlan). A plan may declare `purpose`: 'fps-ab' (its legs are compared on presented fps) or 'stage-attribution' (stage metrics only).
    An 'fps-ab' plan with any HEAVY leg is refused FPS_AB_HEAVY_ARM, with any UNKNOWN leg FPS_AB_UNKNOWN_ARM; any other purpose value is BAD_PURPOSE. 'stage-attribution' and an undeclared
    purpose are not refused here (the verdict paths refuse an fps gain from HEAVY legs themselves). Returns @(code, item, detail); code '' = fine.
    Items: array of [pscustomobject]@{ item; arm (name or object) }.
    #>
    param([AllowNull()]$Purpose, [AllowNull()][object[]]$Items)
    if ($null -eq $Purpose) { return @('', 0, '') }
    if ($Purpose -isnot [string] -or $Purpose -cnotin @('fps-ab', 'stage-attribution')) {
        return @('BAD_PURPOSE', 0, "purpose must be 'fps-ab' or 'stage-attribution'")
    }
    if ($Purpose -cne 'fps-ab') { return @('', 0, '') }
    $arms = @($Items | Where-Object { $null -ne $_ } | ForEach-Object { [pscustomobject]@{ item = [int](Get-FaProp $_ 'item'); arm = (ConvertTo-FpsArmName (Get-FaProp $_ 'arm')) } })
    $h = @($arms | Where-Object { $_.arm -ceq 'HEAVY' } | Select-Object -First 1)
    if ($h.Count -gt 0) { return @('FPS_AB_HEAVY_ARM', $h[0].item, "the plan is labelled an fps A/B ($($script:FpsArmHeavyReason)) but this leg runs the HEAVY telemetry arm; a HEAVY leg's fps measures the logger. Run it as purpose 'stage-attribution' or use a LIGHT spec") }
    $u = @($arms | Where-Object { $_.arm -ceq 'UNKNOWN' } | Select-Object -First 1)
    if ($u.Count -gt 0) { return @('FPS_AB_UNKNOWN_ARM', $u[0].item, "the plan is labelled an fps A/B ($($script:FpsArmUnknownReason)) but this leg's arm cannot be derived from its spec (set generatorArgs.telemetryArm LIGHT)") }
    @('', 0, '')
}

Export-ModuleMember -Function Get-FaProp, New-FpsArm, ConvertTo-FpsArmName, Get-FpsArmFromLegSpec, Get-FpsArmFromJobResult, Get-FpsArm, Test-FpsEnvFlagOn, ConvertTo-FpsEnvMap, Get-FpsArmFromEnvironment, Get-FpsPairVerdict, Get-FpsArmsVerdict, Get-FpsLegPlanRefusal
