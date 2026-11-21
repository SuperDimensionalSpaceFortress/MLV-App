# FpsArmGate.psm1 -- BENCH-LIGHT-ARM-GAIN-GATES-1. Pure helpers, no I/O.
#
# HUB RULING 2026-11-20T05:25:54Z item (2) MEASUREMENT ARM: a presented-fps gain or cost verdict comes only from LIGHT-arm matched
# pairs. A HEAVY-telemetry leg (per-frame stage logging; it costs about 20-27 % presented fps on M16-1457 CUDA fullscreen s1, so its fps
# measures the logger) gives STAGE METRICS ONLY. This module is the one place that decides which arm a leg ran and whether two legs may
# be compared on fps; every gain/cost path in tools/profiling imports it and refuses with a typed reason, so the rule is a guard and not
# a sentence in a prompt.
#
# THE NARROW PROMISE (HUB RULING 2026-11-21T06:10Z, r7/r8): a leg is LIGHT only on an exact, CLOSED shape; anything else is UNKNOWN and refused.
#   (a) its recorded arm word is exactly LIGHT (Test-FaTextEq);
#   (b) when a spec and a result are both present they agree (Get-FpsArm);
#   (c) on a venue leg the generator's own forwarded switches (the five generatorArgs keys of $script:FpsObserverControls) are all off;
#   (d) on a local launch or a saved record, the recorded launch ARGUMENTS and the recorded MLVAPP_* ENVIRONMENT match the producers' exact LIGHT
#       shape: the two data tables below ($script:FpsLightArgs, $script:FpsLightEnv). A flag or key not in a table, a value that differs, a
#       repeated key or flag, a missing record and an unparsable record are UNKNOWN. There is no denylist: a list of what is NOT LIGHT over an
#       open set never converged (ten adversary passes, 44 majors), so the gate names only what a LIGHT leg IS.
# A saved leg or a venue result that records neither a launch nor a LIGHT generatorArgs (and, for a result, is given no LIGHT spec) is UNKNOWN: the
# arm label alone is not proof.
#
# TYPED REASONS (the strings a refusal carries; tests pin them):
#   fps-verdict-refused:heavy-arm     any leg of the pair is HEAVY (a mixed LIGHT+HEAVY pair is this too)
#   fps-verdict-refused:unknown-arm   no leg is HEAVY, but some leg's arm is UNKNOWN
# What stays allowed: stage metrics from any arm, and the per-leg raw numbers (a single leg's own measurement is not a comparison).
# ASCII only (a PS 5.1 reader must parse this file).

Set-StrictMode -Version Latest

# THE LIGHT LAUNCH SHAPE (d). One row per allowed thing; each row cites the producer that emits it. Rules: 'int' / 'num' / 'word' are closed character
# classes, 'text' is non-empty without control characters (a path or a display name), 'exact:<v>' and 'oneof:<a>|<b>' are exact ordinal values.
# Forms (arguments): 'bare' = the flag alone, 'next' = the flag then its value as the next list element, 'eq' = one element flag=value.
# -Required marks a member EVERY producer launch records (an empty launch is not the producers' shape): Get-FpsArmFromLaunch refuses a LIGHT reading that lacks one (r8 adversary A8).
function New-FpsAllow {
    param([string]$Name, [string]$Form, [string]$Rule, [switch]$Required)
    [pscustomobject]@{ name = $Name; form = $Form; rule = $Rule; required = [bool]$Required }
}
# ARGUMENTS: tools/profiling/run-release-gui-smoke.ps1 builds the list (line numbers are in that file); the job's own command line is playback-attr-3-cuda-job.ps1:3092.
# PRODUCER ALIASES used by the row comments below: job = playback-attr-3-cuda-job.ps1, runner = run-release-gui-smoke.ps1 (a trailing row comment names a file by alias only, so the exit-code consumer scan sees each file once, here).
$script:FpsLightArgs = @(
    New-FpsAllow '--gui-smoke-playback' 'bare' '' -Required            # runner:1481 (required: the runner always emits :1481-1503 below)
    New-FpsAllow '--input' 'next' 'text' -Required                      # :1482
    New-FpsAllow '--seconds' 'next' 'num' -Required                     # :1483
    New-FpsAllow '--start-frame' 'next' 'int' -Required                 # :1487
    New-FpsAllow '--presented-frames' 'next' 'int'                      # :1490
    New-FpsAllow '--drop-frame-mode' 'next' 'exact:persisted'           # :1493 (default :27)
    New-FpsAllow '--settle-ms' 'next' 'int' -Required                   # :1496
    New-FpsAllow '--settle-cpu-percent' 'next' 'num' -Required          # :1497
    New-FpsAllow '--settle-cpu-stable-ms' 'next' 'int' -Required        # :1501
    New-FpsAllow '--settle-cpu-max-ms' 'next' 'int' -Required           # :1502
    New-FpsAllow '--receipt' 'next' 'text'                              # :1510
    New-FpsAllow '--scope' 'next' 'exact:none'                          # :1513, the job passes -Scope none (job:3092; producer cite, alias table in header)
    New-FpsAllow '--playback-debayer' 'eq' 'word'                       # :1516
    New-FpsAllow '--playback-processing' 'eq' 'word'                    # :1519
    New-FpsAllow '--gpu-preview-processing' 'eq' 'word'                 # :1522
    New-FpsAllow '--gpu-bilinear-debayer' 'eq' 'word'                   # :1525
    New-FpsAllow '--gpu-amaze-debayer' 'eq' 'word'                      # :1528
    New-FpsAllow '--gpu-amaze-texture-present' 'bare' ''                # :1531
    New-FpsAllow '--no-look-assist' 'bare' ''                           # :1534
    New-FpsAllow '--display-prefer' 'eq' 'text'                         # :1641 (the job forwards -DisplayPrefer, job:3105; producer cite, alias table in header)
)
# ENVIRONMENT (MLVAPP_* only; any other name is not the app's control surface). Names compare ignoring case (the Windows environment does).
$script:FpsLightEnv = @(
    New-FpsAllow 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY' 'env' 'exact:1'                     # runner:1693, job:3027  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG' 'env' 'oneof:1|0' # job:3028 (1 = LIGHT); 0 is the HEAVY arm's value (:3039)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG' 'env' 'exact:1'          # job:3029  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_CRASH_FORENSICS_LOG_DIR' 'env' 'text' -Required               # runner:1695 and :1717 (every launch sets it)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_MAX_THREADS' 'env' 'int'                             # runner:1690  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_QUALITY_MODE' 'env' 'exact:phase3_hq'                # job:3049  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_PHASE3_UNATTENDED' 'env' 'exact:1'                   # runner:1700, job:3053  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_SCALE_FACTOR' 'env' 'int'                            # runner:1703  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_PREFER_HQ_MEAN23' 'env' 'exact:1'                    # runner:1706  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAY_PACE_MODE' 'env' 'exact:informational'                   # runner:1719 (gui-smoke-length-gate.ps1:960)
    New-FpsAllow 'MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW' 'env' 'exact:0'                  # job:3050  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_PREVIEW_MODE' 'env' 'exact:sharp_smooth'             # job:3051  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_PLAYBACK_RECON' 'env' 'exact:1'                           # job:3054  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_PLAYBACK_RECON_BACKEND' 'env' 'exact:cuda'                # job:3055  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_PLAYBACK_RECON_DLL' 'env' 'text'                          # job:3056  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D' 'env' 'exact:0'                 # job:3057  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_EXPERIMENTAL_GPU_PROCESSING' 'env' 'exact:1'                  # job:3058  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER' 'env' 'exact:1'               # job:3059  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT' 'env' 'exact:1'              # job:3060  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT' 'env' 'exact:1'  # job:3062  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT' 'env' 'exact:1'       # job:3063  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT' 'env' 'exact:1'      # job:3064  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_WINDOW_PAINT_PER_SUBMIT' 'env' 'exact:0'                  # job:3069  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_SWAP_INTERVAL' 'env' 'oneof:0|1'                              # job:6319 (-SwapInterval is [ValidateRange(-1, 1)], -1 sets nothing)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_CONFORM' 'env' 'oneof:0|1'                           # job:6330  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_PRESENT_TRACE' 'env' 'exact:1'                       # job:6353 (in-memory trace, written after the Play: PlaybackPresentTrace.h:9-12)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_UNPACED' 'env' 'exact:1'                             # job:6531  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_AUTOMATION_SETTINGS_DIR' 'env' 'text'                         # job:6392  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_LOOK_ASSIST_FLAVOR' 'env' 'word'                              # job:6092  (producer cite, alias table in header)
)
$script:FpsLightArgIndex = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::Ordinal)
foreach ($row in $script:FpsLightArgs) { $script:FpsLightArgIndex[$row.name] = $row }
$script:FpsLightEnvIndex = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($row in $script:FpsLightEnv) { $script:FpsLightEnvIndex[$row.name] = $row }

function Get-FpsLightShape { [pscustomobject]@{ arguments = $script:FpsLightArgs; environment = $script:FpsLightEnv } }

# THE FORWARDED SWITCHES of a venue leg (c): the generatorArgs keys the generator reads that turn a per-launch logger or a sampler on inside the timed window. One closed list.
#   id        a short name for the reason text
#   specKey   the leg spec's generatorArgs key
#   specKind  'bool' (on unless exactly $false / null: the forwarder's [bool] cast reads the string 'false' as $true) or 'threshold' (a number; on for any value but one in [-1, 0): the
#             binder's floor is -1, so a number below it is a leg that cannot run, never a quiet one; the job's `-ge 0` starts the per-launch logger, so 0 is ON).
# test_every_generator_arg_the_venue_leg_forwards_is_classified makes each new forwarded key name itself observer or not.
function New-FpsObserver {
    param([string]$Id, [string]$SpecKey, [string]$SpecKind)
    [pscustomobject]@{ id = $Id; specKey = $SpecKey; specKind = $SpecKind }
}
$script:FpsObserverControls = @(
    New-FpsObserver 'cuda-launch-timing' 'cudaLaunchTimingMinMs' 'threshold'
    New-FpsObserver 'core-partition-telemetry' 'corePartitionTelemetry' 'bool'
    New-FpsObserver 'util-sample' 'utilSample' 'bool'
    New-FpsObserver 'first-present-capture' 'firstPresentCapture' 'bool'
    New-FpsObserver 'warm-reuse-miss-dump' 'warmReuseMissDump' 'bool'
)

function Get-FpsObserverControls { $script:FpsObserverControls }

function Test-FaTextEq {
    # Ordinal equality of two strings. PowerShell's -ceq / -cne / -cin compare with the INVARIANT CULTURE, which ignores zero-width and soft-hyphen characters: 'LIGHT' + U+00AD -ceq 'LIGHT' and
    # ('0' + U+00AD) -cne '0' is $false, so a recorded value carrying one passed as the word or the zero the native reader compares byte for byte (adversary r5 A1, introduced). Every comparison of
    # RECORDED text goes through here.
    param([AllowNull()]$A, [AllowNull()]$B)
    ($A -is [string]) -and ($B -is [string]) -and [string]::Equals($A, $B, [System.StringComparison]::Ordinal)
}

function Test-FaTextIn {
    param([AllowNull()]$A, [string[]]$Set)
    if ($A -isnot [string]) { return $false }
    foreach ($s in $Set) { if ([string]::Equals($A, $s, [System.StringComparison]::Ordinal)) { return $true } }
    $false
}

function Test-FaHasProp {
    # Is the key PRESENT (a null value counts as present)? The venue leg forwards a key by `.PSObject.Properties['key']`, i.e. by presence, so absent and null are different inputs.
    param([AllowNull()]$Object, [Parameter(Mandatory)][string]$Name)
    if ($null -eq $Object) { return $false }
    if ($Object -is [System.Collections.IDictionary]) { return [bool]$Object.Contains($Name) }
    $null -ne $Object.PSObject.Properties[$Name]
}

function Test-FpsSpecObserverOn {
    # Is this control ON, by the way the venue leg forwards it? Absent is off. 'threshold' (cudaLaunchTimingMinMs, Invoke-VenueLeg.ps1:538 `[double]` cast, the job's `-ge 0`): a PRESENT key
    # is on whatever it holds, a null (cast to 0) included, unless it is a negative number. 'bool': a null value is the forwarder's $false (`[bool]$null`, `-is [bool]`); only a
    # present value other than exactly $false or null is on.
    param([Parameter(Mandatory)]$Control, [bool]$Present, [AllowNull()]$Value)
    if (-not $Present) { return $false }
    # luna/adversary r5 A11: the generator binder is [ValidateRange(-1, 10000)] (playback-attr-3-cuda-job.ps1:442), so a number below -1 is a leg that cannot run: it is read as ON (UNKNOWN), never as a quiet LIGHT.
    # Off is a number in [-1, 0) (the default -1, or a fraction the job's `-ge 0` still reads as off).
    if ($Control.specKind -ceq 'threshold') { return -not (($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal]) -and $Value -lt 0 -and $Value -ge -1) }
    if ($null -eq $Value) { return $false }
    -not ($Value -is [bool] -and -not $Value)
}

function Get-FpsSpecObserverOn {
    # The first forwarded switch a spec's generatorArgs (or a record carrying the same keys, directly or under its own generatorArgs) switches on, else $null.
    param([AllowNull()]$Container)
    if ($null -eq $Container -or $Container -is [string] -or $Container -is [System.ValueType]) { return $null }
    foreach ($c in $script:FpsObserverControls) {
        if (Test-FpsSpecObserverOn $c (Test-FaHasProp $Container $c.specKey) (Get-FaProp $Container $c.specKey)) { return $c }
    }
    $null
}

function Get-FpsSpecHolderProblem {
    # A record's embedded `generatorArgs` read as a venue spec (c): an object whose telemetryArm is exactly the text LIGHT and whose forwarded switches are all off. Returns the problem text, else $null.
    param([AllowNull()]$Holder)
    if ($null -eq $Holder -or $Holder -is [string] -or $Holder -is [System.ValueType] -or $Holder -is [System.Array]) { return 'generatorArgs is present but is not an object' }
    if (-not (Test-FaHasProp $Holder 'telemetryArm')) { return 'its generatorArgs names no telemetryArm' }
    if (-not (Test-FaTextEq (Get-FaProp $Holder 'telemetryArm') 'LIGHT')) { return 'its generatorArgs.telemetryArm is not exactly LIGHT' }
    if (Test-FaHasProp $Holder 'observerControls') { return 'its generatorArgs carries an observerControls entry, which no producer records (outside the LIGHT shape)' }
    $observer = Get-FpsSpecObserverOn $Holder
    if ($null -ne $observer) { return "its generatorArgs.$($observer.specKey) enables an observer ($($observer.id))" }
    $null
}

function Get-FpsRecordLightProblem {
    # Why a record (a saved leg or a venue result) whose arm word reads LIGHT is nevertheless not proven LIGHT, else $null. Every piece of evidence it carries must hold, and it must carry at least
    # one that PROVES: a recorded launch that matches the producers' LIGHT shape (d), or generatorArgs that read as a LIGHT spec (c), or (for a result) a LIGHT spec the caller judged it with.
    param([AllowNull()]$Record, [bool]$SpecGiven = $false)
    # Keys no LIGHT producer record carries, so a record that has one is outside the shape (a presence test, not a reading of the entries): an `observerControls` entry (no producer emits one; the
    # r6 reader that tried to read it is deleted, r8 adversary A4), a `look` block and any legType but 'speed' (a look leg captures its contact sheet inside the timed Play, r8 adversary A3).
    if (Test-FaHasProp $Record 'observerControls') { return 'it carries an observerControls entry, which no producer records (outside the LIGHT shape)' }
    if (Test-FaHasProp $Record 'look') { return 'it carries a look block (a look leg captures its contact sheet inside the timed Play)' }
    if ((Test-FaHasProp $Record 'legType') -and -not (Test-FaTextEq (Get-FaProp $Record 'legType') 'speed')) { return "its legType is not 'speed'" }
    $hasLaunch = Test-FaHasProp $Record 'launch'
    $hasGa = Test-FaHasProp $Record 'generatorArgs'
    if ($hasLaunch) {
        $derived = Get-FpsArmFromLaunch (Get-FaProp $Record 'launch')
        if (-not (Test-FaTextEq $derived.arm 'LIGHT')) { return "its recorded launch reads $($derived.arm) ($($derived.reason))" }
    }
    if ($hasGa) {
        $problem = Get-FpsSpecHolderProblem (Get-FaProp $Record 'generatorArgs')
        if ($null -ne $problem) { return $problem }
    }
    $observer = Get-FpsSpecObserverOn $Record
    if ($null -ne $observer) { return "it records $($observer.specKey) enabled ($($observer.id))" }
    if (-not $hasLaunch -and -not $hasGa -and -not $SpecGiven) { return 'it records neither a launch nor LIGHT generatorArgs (the arm label alone is not proof)' }
    $null
}

function Get-FpsArmFromLegRecord {
    <#
    .SYNOPSIS
    The arm of one leg from a RECORD the gain/cost readers hold (a saved A/B summary leg, a machine-perf record leg): its `telemetryArm` (an arm word, or the arm object a smoke summary
    carries) judged with the evidence the record carries (Get-FpsRecordLightProblem). A non-LIGHT arm is returned as it is; a LIGHT one that is not proven is UNKNOWN. No record is UNKNOWN.
    #>
    param([AllowNull()]$Leg)
    if ($null -eq $Leg -or $Leg -is [string] -or $Leg -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'record' 'no-leg-record' }
    $field = Get-FaProp $Leg 'telemetryArm'
    $name = ConvertTo-FpsArmName $field
    if ($name -cne 'LIGHT') {
        if ($null -ne $field -and $field -isnot [string] -and $null -ne (Get-FaProp $field 'arm')) { return $field }
        return New-FpsArm $name 'record' $(if ($name -ceq 'UNKNOWN') { 'record-carries-no-usable-telemetryArm' } else { 'telemetryArm=' + $name })
    }
    $problem = Get-FpsRecordLightProblem $Leg
    if ($null -ne $problem) { return New-FpsArm 'UNKNOWN' 'record' "telemetryArm=LIGHT but $problem" }
    if ($field -isnot [string]) { return $field }
    New-FpsArm 'LIGHT' 'record' 'telemetryArm=LIGHT'
}

$script:FpsArmHeavyReason = 'fps-verdict-refused:heavy-arm'
$script:FpsArmUnknownReason = 'fps-verdict-refused:unknown-arm'

function Get-FaProp {
    # One property of a hashtable / ordered dictionary / PSCustomObject, $null when absent (StrictMode-safe).
    param([AllowNull()]$Object, [Parameter(Mandatory)][string]$Name)
    # `return ,$x`: a one-element array (a parsed-JSON ["LIGHT"]) must stay an array and so fail the string check, never unroll to the scalar it contains (adversary A5).
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) { if ($Object.Contains($Name)) { return , $Object[$Name] } else { return $null } }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return , $p.Value
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
    if (Test-FaTextIn $v @('LIGHT', 'HEAVY')) { return [string]$v }
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
    if (Test-FaTextEq $legType 'export') { return New-FpsArm 'UNKNOWN' 'spec' 'not-a-playback-leg' }
    $ga = Get-FaProp $Spec 'generatorArgs'
    $arm = Get-FaProp $ga 'telemetryArm'
    if ($null -eq $arm) { return New-FpsArm 'UNKNOWN' 'spec' 'telemetryArm-absent (the generator default is HEAVY)' }
    if (Test-FaTextEq $arm 'LIGHT') {
        # r8 (sibling sweep of r6 A1, the spec side): a LIGHT word is proof only on a leg that measures presented fps, the exact legType 'speed'. A 'look' leg grabs its contact sheet from the
        # framebuffer inside the timed Play (Invoke-VenueLeg.ps1:586-589, playback-attr-3-cuda-job.ps1:3125), so it is not a presented-fps measurement whatever its arm word says (52 committed specs).
        if (-not (Test-FaTextEq $legType 'speed')) { return New-FpsArm 'UNKNOWN' 'spec' 'generatorArgs.telemetryArm=LIGHT but the leg is not a speed leg (a look leg captures its contact sheet inside the timed Play)' }
        # the arm word says LIGHT, but a spec that also switches on a forwarded observer (a per-launch logger, a sampler) is not proof of it (adversary pass 3 A2)
        $observer = Get-FpsSpecObserverOn $ga
        if ($null -ne $observer) { return New-FpsArm 'UNKNOWN' 'spec' "generatorArgs.telemetryArm=LIGHT but generatorArgs.$($observer.specKey) enables an observer ($($observer.id)) inside the timed window" }
        return New-FpsArm 'LIGHT' 'spec' 'generatorArgs.telemetryArm=LIGHT'
    }
    if (Test-FaTextEq $arm 'HEAVY') { return New-FpsArm 'HEAVY' 'spec' 'generatorArgs.telemetryArm=HEAVY' }
    New-FpsArm 'UNKNOWN' 'spec' 'telemetryArm-invalid (not LIGHT or HEAVY)'
}

function Get-FpsArmFromJobResult {
    <#
    .SYNOPSIS
    The arm a RECORDED venue result carries: an object with a `telemetryArm` field (the job's provenance.json or evidence-manifest.json, both written from the arm that actually ran).
    UNKNOWN when the object is absent or carries no / an invalid field. A LIGHT word is proof only beside LIGHT evidence (Get-FpsRecordLightProblem): the provenance carries the word alone.
    #>
    # -ObserversFromSpec <spec>: the leg SPEC the caller judged the result with (Get-FpsArm). It must itself read LIGHT; a bare switch is a binding error, and a null, a scalar, a boolean, an
    # empty list or object or a HEAVY spec is no spec (the result's LIGHT word is then UNKNOWN as without the override).
    param([AllowNull()]$Result, [AllowNull()]$ObserversFromSpec)
    $specGiven = ($null -ne $ObserversFromSpec -and $ObserversFromSpec -isnot [string] -and $ObserversFromSpec -isnot [System.ValueType] -and (Get-FpsArmFromLegSpec $ObserversFromSpec).arm -ceq 'LIGHT')
    if ($null -eq $Result -or $Result -is [string] -or $Result -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'result' 'no-result' }
    $arm = Get-FaProp $Result 'telemetryArm'
    if ($null -eq $arm) { return New-FpsArm 'UNKNOWN' 'result' 'result-carries-no-telemetryArm' }
    if (Test-FaTextEq $arm 'LIGHT') {
        $problem = Get-FpsRecordLightProblem $Result $specGiven
        if ($null -ne $problem) { return New-FpsArm 'UNKNOWN' 'result' "telemetryArm=LIGHT but $problem; judge it with its leg spec" }
        return New-FpsArm 'LIGHT' 'result' 'telemetryArm=LIGHT'
    }
    if (Test-FaTextEq $arm 'HEAVY') { return New-FpsArm 'HEAVY' 'result' 'telemetryArm=HEAVY' }
    New-FpsArm 'UNKNOWN' 'result' 'telemetryArm-invalid (not LIGHT or HEAVY)'
}

function Get-FpsArm {
    <#
    .SYNOPSIS
    The arm of one leg from whatever is known: the recorded result and / or the resolved spec. Result alone or spec alone classifies; both must AGREE (a recorded LIGHT result under a
    HEAVY spec, or the reverse, is UNKNOWN: the spec is not what ran, or the result is not this leg's). Neither -> UNKNOWN.
    #>
    param([AllowNull()]$Spec, [AllowNull()]$Result)
    $fromResult = if ($null -ne $Result) { Get-FpsArmFromJobResult $Result -ObserversFromSpec $Spec } else { $null }
    $fromSpec = if ($null -ne $Spec) { Get-FpsArmFromLegSpec $Spec } else { $null }
    if ($null -eq $fromResult -and $null -eq $fromSpec) { return New-FpsArm 'UNKNOWN' 'none' 'neither a spec nor a result was given' }
    if ($null -eq $fromSpec) { return $fromResult }
    if ($null -eq $fromResult) { return $fromSpec }
    if ($fromResult.arm -ceq 'UNKNOWN') { return New-FpsArm 'UNKNOWN' 'spec+result' "the result carries no usable arm ($($fromResult.reason)); spec says $($fromSpec.arm)" }
    # a spec that was GIVEN but names no usable arm cannot be set aside in favour of a LIGHT result: the two inputs must agree (adversary A8)
    if ($fromSpec.arm -ceq 'UNKNOWN') { return New-FpsArm 'UNKNOWN' 'spec+result' "the spec carries no usable arm ($($fromSpec.reason)); result says $($fromResult.arm)" }
    if ($fromSpec.arm -cne $fromResult.arm) { return New-FpsArm 'UNKNOWN' 'spec+result' "spec says $($fromSpec.arm) but the recorded result says $($fromResult.arm)" }
    New-FpsArm $fromResult.arm 'spec+result' "spec and recorded result agree on $($fromResult.arm)"
}

function ConvertTo-FpsEnvMap {
    # KEY=VALUE strings, a hashtable / dictionary or a parsed-JSON object -> @{ state; map; why }. state 'absent' (nothing given), 'malformed' (a value that is not a scalar, an entry that is not
    # KEY=VALUE, a key repeated at all, whatever its values) or 'ok'. Keys compare IGNORING CASE: the Windows process environment is case-insensitive, so mlvapp_stage_timing=1 is MLVAPP_STAGE_TIMING
    # (adversary A2). Values are checked before they are coerced to text (a nested array or object is malformed, never its text).
    param([AllowNull()]$Environment)
    $result = { param($state, $map, $why) [pscustomobject]@{ state = $state; map = $map; why = $why } }
    if ($null -eq $Environment) { return (& $result 'absent' $null 'no-environment-recorded') }
    if (($Environment -is [string]) -and [string]::IsNullOrWhiteSpace($Environment)) { return (& $result 'absent' $null 'no-environment-recorded') }
    $map = [System.Collections.Generic.Dictionary[string, string]]::new([StringComparer]::OrdinalIgnoreCase)
    $pairs = [System.Collections.Generic.List[object]]::new()
    if ($Environment -is [System.Collections.IDictionary]) {
        foreach ($k in $Environment.Keys) { $pairs.Add(@([string]$k, $Environment[$k])) }
    } elseif ($Environment -is [System.Management.Automation.PSCustomObject]) {
        foreach ($p in $Environment.PSObject.Properties) { $pairs.Add(@([string]$p.Name, $p.Value)) }
    } else {
        foreach ($e in @($Environment)) {
            if ($e -isnot [string]) { return (& $result 'malformed' $null 'an environment entry is not a KEY=VALUE string') }
            $i = $e.IndexOf('=')
            if ($i -lt 1) { return (& $result 'malformed' $null 'an environment entry is not KEY=VALUE') }
            $pairs.Add(@($e.Substring(0, $i), $e.Substring($i + 1)))
        }
    }
    foreach ($pair in $pairs) {
        $v = $pair[1]
        if ($null -ne $v -and $v -isnot [string] -and $v -isnot [System.ValueType]) { return (& $result 'malformed' $null "environment value of $($pair[0]) is not a scalar") }
        if ($map.ContainsKey($pair[0])) { return (& $result 'malformed' $null "$($pair[0]) is repeated") }
        $map[$pair[0]] = $(if ($null -eq $v) { '' } else { [string]$v })
    }
    & $result 'ok' $map ''
}

function Test-FpsAllowedValue {
    # Does a recorded text value match a table rule? Ordinal throughout; \z, never $ (a trailing newline must not pass).
    param([string]$Rule, [AllowNull()]$Value)
    if ($Value -isnot [string] -or $Value.Length -eq 0) { return $false }
    if ($Rule.StartsWith('exact:', [System.StringComparison]::Ordinal)) { return [string]::Equals($Value, $Rule.Substring(6), [System.StringComparison]::Ordinal) }
    if ($Rule.StartsWith('oneof:', [System.StringComparison]::Ordinal)) { return (Test-FaTextIn $Value $Rule.Substring(6).Split('|')) }
    switch -CaseSensitive ($Rule) {
        'int' { return [regex]::IsMatch($Value, '^[0-9]{1,9}\z') }
        'num' { return [regex]::IsMatch($Value, '^[0-9]{1,9}(\.[0-9]{1,6})?\z') }
        'word' { return [regex]::IsMatch($Value, '^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}\z') }
        'text' { return ($Value.Length -le 1024 -and -not $Value.StartsWith('-', [System.StringComparison]::Ordinal) -and -not [regex]::IsMatch($Value, '[\x00-\x1F\x7F]')) }
    }
    $false
}

function Get-FpsLaunchArgumentsProblem {
    # Do the recorded launch arguments match the LIGHT shape table exactly? Returns the first problem, else $null. Every element must be text and a table flag; a flag appears once; a
    # 'next' flag takes the following element as its value, an 'eq' flag carries its value after '='; a 'bare' flag carries none.
    param([AllowNull()]$Arguments)
    if ($Arguments -isnot [System.Collections.IList]) { return 'the arguments are not a list' }
    $items = @($Arguments)
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    for ($i = 0; $i -lt $items.Count; $i++) {
        $a = $items[$i]
        if ($a -isnot [string]) { return 'an argument is not text' }
        $flag = $a; $inline = $null
        $eq = $a.IndexOf('=')
        if ($a.StartsWith('--', [System.StringComparison]::Ordinal) -and $eq -gt 2) { $flag = $a.Substring(0, $eq); $inline = $a.Substring($eq + 1) }
        $row = $null
        if (-not $script:FpsLightArgIndex.TryGetValue($flag, [ref]$row)) { return "the argument '$flag' is not in the LIGHT launch shape" }
        if (-not $seen.Add($flag)) { return "the argument '$flag' is repeated" }
        switch ($row.form) {
            'bare' { if ($null -ne $inline) { return "the argument '$flag' takes no value" } }
            'eq' { if ($null -eq $inline -or -not (Test-FpsAllowedValue $row.rule $inline)) { return "the argument '$flag' has a value the LIGHT shape does not allow" } }
            'next' {
                if ($null -ne $inline -or ($i + 1) -ge $items.Count -or -not (Test-FpsAllowedValue $row.rule $items[$i + 1])) { return "the argument '$flag' has a value the LIGHT shape does not allow" }
                $i++
            }
        }
    }
    $null
}

function Get-FpsLaunchEnvironmentProblem {
    # Do the recorded MLVAPP_* variables match the LIGHT shape table exactly (names ignoring case, values ordinal)? Returns the first problem, else $null. Other names are not judged.
    param($Map)
    foreach ($name in $Map.Keys) {
        if (-not $name.StartsWith('MLVAPP_', [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $row = $null
        if (-not $script:FpsLightEnvIndex.TryGetValue($name, [ref]$row)) { return "$name is not in the LIGHT launch shape" }
        if (-not (Test-FpsAllowedValue $row.rule $Map[$name])) { return "$name has a value the LIGHT launch shape does not allow" }
    }
    $null
}

function Get-FpsArmFromEnvironment {
    <#
    .SYNOPSIS
    The arm of a local GUI smoke from its RECORDED launch (the local GUI smoke runner's result.launch: environment AND arguments; the environment is a KEY=VALUE list, a dictionary or a parsed
    object). LIGHT only when both match the producers' LIGHT shape (the tables above); the app's frame-log rule then decides the arm: per-frame rows are logged when
    MLVAPP_PLAYBACK_SMOKE_TELEMETRY is on and MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG is not 1, so telemetry on without the opt-out is HEAVY, and telemetry off or the opt-out is LIGHT.
    An absent environment or argument list, an unparsable one, and anything outside the tables is UNKNOWN.
    #>
    param([AllowNull()]$Environment, [AllowNull()]$Arguments = $null)
    $parsed = ConvertTo-FpsEnvMap $Environment
    if ($parsed.state -ceq 'absent') { return New-FpsArm 'UNKNOWN' 'environment' 'no-environment-recorded' }
    if ($parsed.state -cne 'ok') { return New-FpsArm 'UNKNOWN' 'environment' "malformed-environment ($($parsed.why))" }
    if ($null -eq $Arguments) { return New-FpsArm 'UNKNOWN' 'environment' 'no-arguments-recorded' }
    $problem = Get-FpsLaunchArgumentsProblem $Arguments
    if ($null -ne $problem) { return New-FpsArm 'UNKNOWN' 'environment' "outside the LIGHT launch shape ($problem)" }
    $map = $parsed.map
    $problem = Get-FpsLaunchEnvironmentProblem $map
    if ($null -ne $problem) { return New-FpsArm 'UNKNOWN' 'environment' "outside the LIGHT launch shape ($problem)" }
    if (-not $map.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY')) { return New-FpsArm 'LIGHT' 'environment' 'frame telemetry is off' }
    if ($map.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG') -and (Test-FaTextEq $map['MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG'] '1')) { return New-FpsArm 'LIGHT' 'environment' 'frame telemetry on, per-frame log opted out' }
    New-FpsArm 'HEAVY' 'environment' 'frame telemetry is on and the per-frame log is not opted out'
}

function Get-FpsArmFromLaunch {
    <#
    .SYNOPSIS
    The arm of a local GUI smoke from the `launch` block its own result recorded (environment AND arguments). A result with no launch block, no environment or no argument list is UNKNOWN:
    the argument list is part of the shape, so an arm judged from the environment alone is not proof of LIGHT.
    #>
    param([AllowNull()]$Launch)
    if ($null -eq $Launch -or $Launch -is [string] -or $Launch -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'environment' 'no-launch-block-recorded' }
    if (-not (Test-FaHasProp $Launch 'environment')) { return New-FpsArm 'UNKNOWN' 'environment' 'no-environment-recorded' }
    if (-not (Test-FaHasProp $Launch 'arguments')) { return New-FpsArm 'UNKNOWN' 'environment' 'no-arguments-recorded' }
    $launchArgs = Get-FaProp $Launch 'arguments'
    # r6 (adversary pass 5 A2): the recorded argument list must BE a list (`@($x)` made the JSON string "--playback-smoke" a one-element list and a null a list holding one null).
    if ($launchArgs -isnot [System.Collections.IList]) { return New-FpsArm 'UNKNOWN' 'environment' 'malformed-arguments (the recorded arguments are not a list)' }
    $recordedEnvironment = Get-FaProp $Launch 'environment'
    $arm = Get-FpsArmFromEnvironment -Environment $recordedEnvironment -Arguments $launchArgs
    # r8 (adversary A8): LIGHT also needs the members every producer launch records (the rows marked -Required); an empty launch ({"arguments":[],"environment":{}}) matches the tables vacuously
    if ($arm.arm -ceq 'LIGHT') {
        $have = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach ($a in @($launchArgs)) { $cut = $a.IndexOf('='); [void]$have.Add($(if ($a.StartsWith('--', [System.StringComparison]::Ordinal) -and $cut -gt 2) { $a.Substring(0, $cut) } else { $a })) }
        $map = (ConvertTo-FpsEnvMap $recordedEnvironment).map
        foreach ($row in $script:FpsLightArgs) { if ($row.required -and -not $have.Contains($row.name)) { return New-FpsArm 'UNKNOWN' 'environment' "the launch lacks $($row.name), which every producer launch records" } }
        foreach ($row in $script:FpsLightEnv) { if ($row.required -and -not $map.ContainsKey($row.name)) { return New-FpsArm 'UNKNOWN' 'environment' "the launch lacks $($row.name), which every producer launch records" } }
    }
    $arm
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
    # A $null entry is a leg whose arm nobody supplied: UNKNOWN, never dropped (adversary A7).
    $names = @()
    if ($null -ne $Legs) {
        for ($i = 0; $i -lt $Legs.Count; $i++) {
            $leg = $Legs[$i]
            $n = if ($null -eq $leg) { '' } else { [string](Get-FaProp $leg 'name') }
            $names += [pscustomobject]@{ name = $(if ($n) { $n } else { "leg-$($i + 1)" }); arm = $(if ($null -eq $leg) { 'UNKNOWN' } else { ConvertTo-FpsArmName (Get-FaProp $leg 'arm') }) }
        }
    }
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
    # -Declared: the plan names a `purpose` key at all. A declared purpose must be the scalar string, so a declared $null, a number or an array (a one-element ["fps-ab"] must not read as 'fps-ab')
    # is BAD_PURPOSE (adversary A3); only a plan with no purpose key is undeclared.
    param([AllowNull()]$Purpose, [AllowNull()][object[]]$Items, [switch]$Declared)
    if ($null -eq $Purpose -and -not $Declared) { return @('', 0, '') }
    if (-not (Test-FaTextIn $Purpose @('fps-ab', 'stage-attribution'))) {
        return @('BAD_PURPOSE', 0, "purpose must be 'fps-ab' or 'stage-attribution'")
    }
    if (-not (Test-FaTextEq $Purpose 'fps-ab')) { return @('', 0, '') }
    # A $null item is an item whose arm nobody supplied: UNKNOWN at its position, never dropped (adversary A7).
    $arms = @()
    if ($null -ne $Items) {
        for ($i = 0; $i -lt $Items.Count; $i++) {
            $it = $Items[$i]
            $arms += [pscustomobject]@{ item = $(if ($null -eq $it) { $i + 1 } else { [int](Get-FaProp $it 'item') }); arm = $(if ($null -eq $it) { 'UNKNOWN' } else { ConvertTo-FpsArmName (Get-FaProp $it 'arm') }) }
        }
    }
    $h = @($arms | Where-Object { $_.arm -ceq 'HEAVY' } | Select-Object -First 1)
    if ($h.Count -gt 0) { return @('FPS_AB_HEAVY_ARM', $h[0].item, "the plan is labelled an fps A/B ($($script:FpsArmHeavyReason)) but this leg runs the HEAVY telemetry arm; a HEAVY leg's fps measures the logger. Run it as purpose 'stage-attribution' or use a LIGHT spec") }
    $u = @($arms | Where-Object { $_.arm -ceq 'UNKNOWN' } | Select-Object -First 1)
    if ($u.Count -gt 0) { return @('FPS_AB_UNKNOWN_ARM', $u[0].item, "the plan is labelled an fps A/B ($($script:FpsArmUnknownReason)) but this leg's arm cannot be derived from its spec (set generatorArgs.telemetryArm LIGHT)") }
    @('', 0, '')
}

Export-ModuleMember -Function Get-FpsObserverControls, Get-FpsLightShape, Get-FpsArmFromLegRecord, Get-FaProp, New-FpsArm, ConvertTo-FpsArmName, Get-FpsArmFromLegSpec, Get-FpsArmFromJobResult, Get-FpsArm, ConvertTo-FpsEnvMap, Get-FpsArmFromEnvironment, Get-FpsArmFromLaunch, Get-FpsPairVerdict, Get-FpsArmsVerdict, Get-FpsLegPlanRefusal
