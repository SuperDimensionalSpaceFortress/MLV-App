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

# The MLVAPP_* variables the local smoke tools themselves set that are configuration, not diagnostics (run-release-gui-smoke / run-release-cuda-playback-ab). Anything else MLVAPP_* is unknown.
$script:FpsArmKnownBenignEnv = [System.Collections.Generic.HashSet[string]]::new([string[]]@(
    'MLVAPP_PLAYBACK_SMOKE_TELEMETRY', 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG', 'MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG',
    'MLVAPP_CRASH_FORENSICS_LOG_DIR', 'MLVAPP_GUI_SMOKE_RUN_NONCE', 'MLVAPP_RUN_NONCE', 'MLVAPP_PLAY_PACE_MODE', 'MLVAPP_HUB_PLAYBACK_CI_FIXTURE',
    'MLVAPP_PLAYBACK_MAX_THREADS', 'MLVAPP_PLAYBACK_QUALITY_MODE', 'MLVAPP_PLAYBACK_PHASE3_UNATTENDED', 'MLVAPP_PLAYBACK_SCALE_FACTOR', 'MLVAPP_PLAYBACK_PREFER_HQ_MEAN23',
    'MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS', 'MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES', 'MLVAPP_PLAYBACK_TIMER_POLL_MS', 'MLVAPP_PHASE3_FRAME_SLOT_COUNT',
    'MLVAPP_RAW_UINT16_PREFETCH_DECODERS', 'MLVAPP_DISABLE_RAW_UINT16_PREFETCH', 'MLVAPP_DISABLE_PLAY_START_PREROLL', 'MLVAPP_EXPERIMENTAL_PROCESSED8_PREFETCH',
    'MLVAPP_DISABLE_AVX2_INTRIN_DIRECT8', 'MLVAPP_ENABLE_AVX2_INTRIN_DIRECT8', 'MLVAPP_DISABLE_SH_CURVE_INDEX_MASK', 'MLVAPP_ENABLE_SH_CURVE_INDEX_MASK', 'MLVAPP_ENABLE_DUAL_ISO_FAST_X4_IN_HQ',
    'MLVAPP_GPU_PLAYBACK_RECON', 'MLVAPP_GPU_PLAYBACK_RECON_BACKEND', 'MLVAPP_GPU_PLAYBACK_RECON_DLL', 'MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT',
    # MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT is NOT here (adversary pass 4 A2): it is a per-frame observer (see the observer list). Its SAMPLE_EVERY is inert without it and stays benign.
    'MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT_SAMPLE_EVERY',
    'MLVAPP_GPU_TEX_NR_ACQUIRE_LATEST_READY', 'MLVAPP_GPU_TEX_NR_IMMEDIATE_DRAIN_MAX', 'MLVAPP_GPU_TEX_NR_IMMEDIATE_DRAIN_READY',
    'MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT', 'MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT',
    'MLVAPP_CUDA_AMAZE_LIVE_DIRECT_RGBA_STORE', 'MLVAPP_CUDA_AMAZE_LIVE_FAST_LAUNCH_CHECKS', 'MLVAPP_CUDA_AMAZE_LIVE_TILE_STREAMS',
    # The per-present trace (generatorArgs.presentTrace) is NOT an observer here: it keeps its rows in memory and writes them after the Play stops, "so the trace never writes inside the timed window"
    # (platform/qt/PlaybackPresentTrace.h:9-12), and the shipped LIGHT headroom legs are built on it. The spec classifier agrees (presentTrace is not on the observer list).
    'MLVAPP_PLAYBACK_PRESENT_TRACE',
    # The standing environment of a venue job (playback-attr-3-cuda-job.ps1 $envs, and the leg switches the generator adds): the baseline of a LIGHT leg, not a discriminator of one, so the env
    # classifier must not call the generator's own LIGHT environment unknown (adversary pass 5 A2). MLVAPP_VIEWPORT_PRESENT_DIAG is NOT in this group any more (r4, luna r3 B2): the app logs from
    # its per-present paths when it is on, so it is an observer (see the observer list), and the job no longer sets it on a LIGHT leg.
    # test_every_variable_the_job_generator_emits_is_classified fails when the generator emits a name that is on neither list.
    'MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW', 'MLVAPP_PLAYBACK_PREVIEW_MODE', 'MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D', 'MLVAPP_EXPERIMENTAL_GPU_PROCESSING',
    'MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER', 'MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT', 'MLVAPP_GPU_WINDOW_PAINT_PER_SUBMIT', 'MLVAPP_SWAP_INTERVAL', 'MLVAPP_PLAYBACK_CONFORM',
    'MLVAPP_PLAYBACK_UNPACED', 'MLVAPP_PLAYBACK_RECON_OMP_THREADS', 'MLVAPP_PLAYBACK_PREP_OMP_THREADS', 'MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE', 'MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY',
    'MLVAPP_DISABLE_PREP_POINTWISE_FAST', 'MLVAPP_GPU_RECON_RELEASE_DEFERRED', 'MLVAPP_GPU_SAFE_START', 'MLVAPP_GPU_FAULT_INJECT', 'MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK',
    'MLVAPP_LOOK_ASSIST_FLAVOR', 'MLVAPP_AUTOMATION_SETTINGS_DIR'
), [StringComparer]::OrdinalIgnoreCase)

# THE OBSERVER CONTROLS: one list, read by all three classifiers (env, spec, result) -- adversary pass 3 A1 and A2 were one defect, two classifiers keeping their own lists. A LIGHT proof
# excludes every control here. Per control:
#   id        a short name for the reason text
#   specKey   the leg spec's generatorArgs key that turns it on ('' = no spec key: an env-only diagnostic)
#   specKind  'bool' (on unless exactly $false / null: the forwarder's [bool] cast reads the string 'false' as $true) or 'threshold' (a number; on for any value but one in [-1, 0) (r5: the binder's floor is -1, so a number below it is a leg that cannot run, never a quiet one),
#             because the job's `-ge 0` is what starts the per-launch logger, so 0 is ON). Anything else present is read toward ON.
#   envVar    the environment variable the job / app reads ('' = none: the control is a helper process the environment cannot show)
#   kind      how the CONSUMER reads the variable, read from its source: 'flag' = on unless exactly "0" (StageTiming.h:74, MainWindow.cpp:185-186; "" reads toward on, as for every flag),
#             'value' = DATA, on whenever it is non-empty, "0" included (a file name: RenderFrameThread.cpp:60-62 opens MLVAPP_PHASE3_TEL_PATH, StageTiming.h:87-90 opens
#             MLVAPP_STAGE_TIMING_FILE, GpuDisplayWindow.cpp:146 dumps into MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR; a threshold: MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS=0 is zero ms),
#             'truthy' = a flag whose consumer reads ONLY 1 / true / yes / on (ASCII-trimmed, any case) as on and everything else (0, false, off, no, empty) as off
#             (GpuDisplayViewport.cpp:51-60 envFlagEnabled, read once by :66-71 for MLVAPP_VIEWPORT_PRESENT_DIAG); a value this module cannot read that way (non-ASCII text) is read toward ON.
#   severity  what the env classifier answers when it is on: HEAVY (a per-frame stage / timeline logger) or UNKNOWN (an observer the arm word does not describe)
# The generator forwards more generatorArgs keys than these; test_every_generator_arg_the_venue_leg_forwards_is_classified makes each new one name itself observer or not.
function New-FpsObserver {
    param([string]$Id, [string]$SpecKey = '', [string]$SpecKind = '', [string]$EnvVar = '', [string]$Kind = 'flag', [string]$Severity = 'UNKNOWN')
    [pscustomobject]@{ id = $Id; specKey = $SpecKey; specKind = $SpecKind; envVar = $EnvVar; kind = $Kind; severity = $Severity }
}
$script:FpsObserverControls = @(
    New-FpsObserver 'smoke-timeline' -EnvVar 'MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY' -Severity 'HEAVY'
    New-FpsObserver 'detailed-timeline' -EnvVar 'MLVAPP_PLAYBACK_DETAILED_TIMELINE_TELEMETRY' -Severity 'HEAVY'
    New-FpsObserver 'stage-timing' -EnvVar 'MLVAPP_STAGE_TIMING' -Severity 'HEAVY'
    New-FpsObserver 'perf-field-log' -EnvVar 'MLVAPP_PERF_FIELD_LOG' -Severity 'HEAVY'
    New-FpsObserver 'rbf-detail-timing' -EnvVar 'MLVAPP_PLAYBACK_RBF_DETAIL_TIMING' -Severity 'HEAVY'
    New-FpsObserver 'interactive-trace' -EnvVar 'MLVAPP_INTERACTIVE_TRACE' -Severity 'HEAVY'
    New-FpsObserver 'overlap-trace' -EnvVar 'MLVAPP_GPU_TEX_NR_OVERLAP_TRACE' -Severity 'HEAVY'
    # the viewport's per-present trace (GpuDisplayViewport.cpp:1226-1231 setPresentedImage, :2014-2019 updateTextureIfNeeded and the paintGL paths log on every present when it is on); luna r3 B2
    New-FpsObserver 'viewport-present-diag' -EnvVar 'MLVAPP_VIEWPORT_PRESENT_DIAG' -Kind 'truthy' -Severity 'HEAVY'
    # the GL-vs-CPU-oracle parity check: a GL texture readback, SHA256 hashing and a full CPU recon replay per presented frame (MainWindow.cpp:404-409 and :421-428 name it "very heavy ...
    # collapses an AMaZE playback run to a few fps"; RenderFrameThread.cpp:696-702 reads it the same way: set and not "0"), so it is a flag
    New-FpsObserver 'recon-output-validation' -EnvVar 'MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT' -Severity 'HEAVY'
    New-FpsObserver 'phase3-stage-csv' -EnvVar 'MLVAPP_PHASE3_TEL_PATH' -Kind 'value' -Severity 'HEAVY'
    New-FpsObserver 'stage-timing-file' -EnvVar 'MLVAPP_STAGE_TIMING_FILE' -Kind 'value'
    New-FpsObserver 'perf-field-log-path' -EnvVar 'MLVAPP_PERF_FIELD_LOG_PATH' -Kind 'value'
    New-FpsObserver 'perf-field-log-dir' -EnvVar 'MLVAPP_PERF_FIELD_LOG_DIR' -Kind 'value'
    New-FpsObserver 'cuda-launch-timing' -SpecKey 'cudaLaunchTimingMinMs' -SpecKind 'threshold' -EnvVar 'MLVAPP_CUDA_LAUNCH_TIMING'
    New-FpsObserver 'cuda-launch-timing-min-ms' -EnvVar 'MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS' -Kind 'value'
    New-FpsObserver 'core-partition-telemetry' -SpecKey 'corePartitionTelemetry' -SpecKind 'bool'
    New-FpsObserver 'util-sample' -SpecKey 'utilSample' -SpecKind 'bool'
    New-FpsObserver 'first-present-capture' -SpecKey 'firstPresentCapture' -SpecKind 'bool' -EnvVar 'MLVAPP_SMOKE_FIRST_PRESENT_CAPTURE'
    New-FpsObserver 'warm-reuse-miss-dump' -SpecKey 'warmReuseMissDump' -SpecKind 'bool' -EnvVar 'MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR' -Kind 'value'
)

$script:FpsObserverEnvNames = [System.Collections.Generic.HashSet[string]]::new([string[]]@($script:FpsObserverControls | Where-Object { $_.envVar } | ForEach-Object { $_.envVar }), [StringComparer]::OrdinalIgnoreCase)

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
    # The first observer control a spec's generatorArgs (or a result carrying the same keys, directly or under its own generatorArgs) switches on, else $null.
    param([AllowNull()]$Container)
    if ($null -eq $Container -or $Container -is [string] -or $Container -is [System.ValueType]) { return $null }
    foreach ($c in $script:FpsObserverControls) {
        if (-not $c.specKey) { continue }
        if (Test-FpsSpecObserverOn $c (Test-FaHasProp $Container $c.specKey) (Get-FaProp $Container $c.specKey)) { return $c }
    }
    $null
}

function Read-FpsRecordedObserverControls {
    # The `observerControls` entry of a RECORD (a leg record, a job result, or its generatorArgs), judged by one rule for every reader (luna r3 B1: Get-FpsArmFromLegRecord ignored it). Returns
    # @{ present; entries; problem } where problem is $null when nothing is enabled and nothing is unreadable, else @{ kind = 'enabled' | 'unreadable'; text }.
    #   absent                       -> nothing recorded (no problem)
    #   present but not an object    -> unreadable (fail closed): $null, a string, a number, a boolean, an array
    #   a LISTED control's entry     -> by that control's own rule (Test-FpsSpecObserverOn: a bool key is on unless exactly $false / null, the threshold is on unless a negative number)
    #   an UNLISTED entry            -> $true, a non-zero number or a non-empty string is ENABLED; $false and 0 are off; null, an empty string, an array or an object cannot be read as a boolean (unreadable)
    param([AllowNull()]$Container)
    $none = [pscustomobject]@{ present = $false; entries = 0; problem = $null }
    if ($null -eq $Container -or $Container -is [string] -or $Container -is [System.ValueType]) { return $none }
    # adversary r5 A14: a HOLDER (a record's generatorArgs) that is an array is a record this reader cannot read at all; an outer all-off list must not corroborate it
    if ($Container -is [System.Array]) { return [pscustomobject]@{ present = $true; entries = 0; problem = [pscustomobject]@{ kind = 'unreadable'; text = 'the record holder is an array, not an object' } } }
    if (-not (Test-FaHasProp $Container 'observerControls')) { return $none }
    $controls = Get-FaProp $Container 'observerControls'
    $fail = { param($kind, $text, $n) [pscustomobject]@{ present = $true; entries = $n; problem = [pscustomobject]@{ kind = $kind; text = $text } } }
    $pairs = [System.Collections.Generic.List[object]]::new()
    if ($null -eq $controls -or $controls -is [string] -or $controls -is [System.ValueType] -or $controls -is [System.Array]) {
        return (& $fail 'unreadable' 'observerControls is present but is not an object' 0)
    }
    if ($controls -is [System.Collections.IDictionary]) {
        foreach ($k in $controls.Keys) { $pairs.Add(@([string]$k, $controls[$k])) }
    } elseif ($controls -is [System.Management.Automation.PSCustomObject]) {
        foreach ($p in $controls.PSObject.Properties) { $pairs.Add(@([string]$p.Name, $p.Value)) }
    } else {
        return (& $fail 'unreadable' 'observerControls is present but is not an object' 0)
    }
    foreach ($pair in $pairs) {
        $name = [string]$pair[0]; $v = $pair[1]
        # ordinal, ignoring case only (-ieq is a culture compare: "cudaLaunchTimingMinMs" + U+00AD matched the listed key; adversary r5 pass 2 A1)
        $listed = @($script:FpsObserverControls | Where-Object { $_.specKey -and [string]::Equals([string]$_.specKey, $name, [System.StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1)
        if ($listed.Count -gt 0) {
            if (Test-FpsSpecObserverOn $listed[0] $true $v) { return (& $fail 'enabled' "observerControls.$name is enabled ($($listed[0].id))" $pairs.Count) }
            continue
        }
        if ($v -is [bool]) { if ($v) { return (& $fail 'enabled' "observerControls.$name is true" $pairs.Count) }; continue }
        if ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal]) { if ($v -ne 0) { return (& $fail 'enabled' "observerControls.$name is $v" $pairs.Count) }; continue }
        if ($v -is [string] -and $v.Length -gt 0) { return (& $fail 'enabled' "observerControls.$name is the non-empty text '$v'" $pairs.Count) }
        return (& $fail 'unreadable' "observerControls.$name cannot be read as a boolean" $pairs.Count)
    }
    [pscustomobject]@{ present = $true; entries = $pairs.Count; problem = $null }
}

function Get-FpsRecordShapeProblem {
    # A record whose `generatorArgs` is PRESENT but is not an object (a scalar, null, an array) cannot be read for observers at all; an outer all-off list must not corroborate it (adversary r5 A14, pass 2 A4).
    param([AllowNull()]$Record)
    if ($null -eq $Record -or $Record -is [string] -or $Record -is [System.ValueType]) { return $null }
    if (Test-FaHasProp $Record 'generatorArgs') {
        $ga = Get-FaProp $Record 'generatorArgs'
        if ($null -eq $ga -or $ga -is [string] -or $ga -is [System.ValueType] -or $ga -is [System.Array]) { return 'generatorArgs is present but is not an object' }
    }
    $null
}

function Get-FpsRecordContradiction {
    # Does a LIGHT record's other evidence say something else? Its recorded `launch` (when it has one) must read LIGHT, and a `generatorArgs.telemetryArm` it embeds must be exactly LIGHT (adversary r5 pass 2 A2).
    param([AllowNull()]$Record)
    $launch = Get-FaProp $Record 'launch'
    if ($null -ne $launch) {
        $derived = Get-FpsArmFromLaunch $launch
        if (-not (Test-FaTextEq $derived.arm 'LIGHT')) { return "the record's own launch reads $($derived.arm) ($($derived.reason))" }
    }
    $ga = Get-FaProp $Record 'generatorArgs'
    if ($null -ne $ga -and $ga -isnot [string] -and $ga -isnot [System.ValueType] -and (Test-FaHasProp $ga 'telemetryArm')) {
        $embedded = ConvertTo-FpsArmName (Get-FaProp $ga 'telemetryArm')
        if (-not (Test-FaTextEq $embedded 'LIGHT')) { return "the record's generatorArgs.telemetryArm reads $embedded" }
    }
    $null
}

function Test-FpsRecordHasObserverEvidence {
    # Does a RECORD (a leg record or a job result) say anything about its observer controls? True when its `observerControls` (directly or under its generatorArgs) has an entry, or it names
    # a listed control's key (at its top level, under its generatorArgs, or inside `observerControls`), on OR off. An arm WORD alone is not evidence that no sampler or per-launch logger ran
    # (adversary pass 5 A2 for a result; BENCH-LIGHT-ARM-GAIN-GATES-1 r5, Dot on f51e0821, the same rule for a saved leg record): one rule for both readers.
    param([AllowNull()]$Record)
    if ($null -eq $Record -or $Record -is [string] -or $Record -is [System.ValueType]) { return $false }
    $generatorArgs = Get-FaProp $Record 'generatorArgs'
    foreach ($holder in @($Record, $generatorArgs)) {
        if ((Read-FpsRecordedObserverControls $holder).entries -gt 0) { return $true }
    }
    foreach ($container in @($Record, $generatorArgs, (Get-FaProp $Record 'observerControls'))) {
        if ($null -eq $container -or $container -is [string] -or $container -is [System.ValueType]) { continue }
        foreach ($c in $script:FpsObserverControls) { if ($c.specKey -and (Test-FaHasProp $container $c.specKey)) { return $true } }
    }
    $false
}

function Get-FpsArmFromLegRecord {
    <#
    .SYNOPSIS
    The arm of one leg from a RECORD the gain/cost readers hold (a saved A/B summary leg, a machine-perf record leg): its `telemetryArm` (an arm word, or the arm object a smoke summary
    carries) judged together with the observer controls the record carries (adversary pass 4 A3: a reader that projected only the arm word let a record with utilSample set pass as LIGHT).
    A non-LIGHT arm is returned as it is; a LIGHT one that records an enabled observer, directly or under its generatorArgs, is UNKNOWN. No record is UNKNOWN.
    #>
    param([AllowNull()]$Leg)
    if ($null -eq $Leg -or $Leg -is [string] -or $Leg -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'record' 'no-leg-record' }
    $field = Get-FaProp $Leg 'telemetryArm'
    $name = ConvertTo-FpsArmName $field
    if ($name -cne 'LIGHT') {
        if ($null -ne $field -and $field -isnot [string] -and $null -ne (Get-FaProp $field 'arm')) { return $field }
        return New-FpsArm $name 'record' $(if ($name -ceq 'UNKNOWN') { 'record-carries-no-usable-telemetryArm' } else { 'telemetryArm=' + $name })
    }
    $observer = Get-FpsSpecObserverOn $Leg
    if ($null -eq $observer) { $observer = Get-FpsSpecObserverOn (Get-FaProp $Leg 'generatorArgs') }
    if ($null -ne $observer) { return New-FpsArm 'UNKNOWN' 'record' "telemetryArm=LIGHT but the record carries $($observer.specKey) enabled ($($observer.id))" }
    # luna r3 B1: the `observerControls` object a record carries (directly or under its generatorArgs) is judged too: any enabled entry, or one that cannot be read, is not LIGHT
    foreach ($holder in @($Leg, (Get-FaProp $Leg 'generatorArgs'))) {
        $oc = Read-FpsRecordedObserverControls $holder
        if ($null -ne $oc.problem) { return New-FpsArm 'UNKNOWN' 'record' "telemetryArm=LIGHT but the record's observerControls fail closed ($($oc.problem.kind): $($oc.problem.text))" }
    }
    # adversary r5 pass 2 A4: a generatorArgs that is present but not an object (a scalar, null, an array) is unreadable, so no outer list may corroborate the record
    $shape = Get-FpsRecordShapeProblem $Leg
    if ($null -ne $shape) { return New-FpsArm 'UNKNOWN' 'record' "telemetryArm=LIGHT but the record fails closed ($shape)" }
    # adversary r5 pass 2 A2: the other evidence the record carries must agree with the LIGHT label, for the arm word and the arm object alike: its recorded `launch` must read LIGHT and a
    # generatorArgs.telemetryArm it embeds must be exactly LIGHT
    $contradiction = Get-FpsRecordContradiction $Leg
    if ($null -ne $contradiction) { return New-FpsArm 'UNKNOWN' 'record' "telemetryArm=LIGHT but $contradiction" }
    # The label (a bare word, or an arm object whatever its `source` says: adversary r5 pass 2 A3) is not proof by itself. Evidence is a recorded launch (just judged LIGHT above) or a recorded
    # observer list, the same rule as a recorded result (Get-FpsArmFromJobResult).
    if ($null -eq (Get-FaProp $Leg 'launch') -and -not (Test-FpsRecordHasObserverEvidence $Leg)) {
        return New-FpsArm 'UNKNOWN' 'record' 'telemetryArm=LIGHT but the record records neither its launch nor its observer controls (the arm label alone is not proof)'
    }
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
        # the arm word says LIGHT, but a spec that also switches on an observer control the job forwards (a per-launch logger, a sampler) is not proof of it (adversary pass 3 A2)
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
    UNKNOWN when the object is absent or carries no / an invalid field.
    #>
    # -ObserversFromSpec: the caller judges the observer controls from the leg SPEC (Get-FpsArm with both inputs), so a result that records none is not refused for that alone.
    param([AllowNull()]$Result, [switch]$ObserversFromSpec)
    if ($null -eq $Result -or $Result -is [string] -or $Result -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'result' 'no-result' }
    $arm = Get-FaProp $Result 'telemetryArm'
    if ($null -eq $arm) { return New-FpsArm 'UNKNOWN' 'result' 'result-carries-no-telemetryArm' }
    if (Test-FaTextEq $arm 'LIGHT') {
        # a result that records an enabled observer control (the same keys as a spec: at its top level, under its generatorArgs, or under an `observerControls` object) is not LIGHT
        # either (adversary pass 3 A2)
        $containers = @($Result, (Get-FaProp $Result 'generatorArgs'), (Get-FaProp $Result 'observerControls'))
        $shape = Get-FpsRecordShapeProblem $Result
        if ($null -ne $shape) { return New-FpsArm 'UNKNOWN' 'result' "telemetryArm=LIGHT but the result fails closed ($shape)" }
        $contradiction = Get-FpsRecordContradiction $Result
        if ($null -ne $contradiction) { return New-FpsArm 'UNKNOWN' 'result' "telemetryArm=LIGHT but $contradiction" }
        # the same rule as a leg record's (Read-FpsRecordedObserverControls): an `observerControls` that is not an object, an enabled entry of any name, or an entry that cannot be read fails closed
        foreach ($holder in @($Result, (Get-FaProp $Result 'generatorArgs'))) {
            $oc = Read-FpsRecordedObserverControls $holder
            if ($null -ne $oc.problem) { return New-FpsArm 'UNKNOWN' 'result' "telemetryArm=LIGHT but the result's observerControls fail closed ($($oc.problem.kind): $($oc.problem.text))" }
        }
        foreach ($container in $containers) {
            if ($null -eq $container -or $container -is [string] -or $container -is [System.ValueType]) { continue }
            $observer = Get-FpsSpecObserverOn $container
            if ($null -ne $observer) { return New-FpsArm 'UNKNOWN' 'result' "telemetryArm=LIGHT but the result records $($observer.specKey) enabled ($($observer.id))" }
        }
        # the job's provenance.json / evidence-manifest.json carry the arm WORD and no observer record, so a result alone cannot prove that no sampler or per-launch logger ran (adversary pass 5
        # A2): without a recorded observer list (or a spec to judge them from) a LIGHT word is UNKNOWN
        $recorded = Test-FpsRecordHasObserverEvidence $Result
        if (-not $recorded -and -not $ObserversFromSpec) { return New-FpsArm 'UNKNOWN' 'result' 'telemetryArm=LIGHT but the result records no observer controls (provenance carries the arm word only); judge it with its leg spec' }
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
    $fromResult = if ($null -ne $Result) { Get-FpsArmFromJobResult $Result -ObserversFromSpec:($null -ne $Spec) } else { $null }
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

function Test-FpsEnvFlagOn {
    # A per-frame diagnostic variable counts as ON unless it is exactly "0". The app's readers disagree on every other value (MainWindow.cpp environmentFlagEnabled reads false / off / no /
    # empty as off, GpuDisplayWindow.cpp swapTelemetryEnabled reads them as on, and other readers are other things), so the only value that proves OFF is the one both agree on; every
    # other set value is read toward HEAVY (adversary A1: TELEMETRY=false and STAGE_TIMING=false are on in the native readers).
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $false }
    -not (Test-FaTextEq ([string]$Value) '0')
}

function Test-FpsEnvVarOn {
    # Is an environment variable ON, by the rule of its consumer? kind 'value' (a path, a file name, a threshold: DATA) is on whenever it is non-empty, because "0" is a file name or a zero
    # threshold, not off (adversary pass 3 A1); kind 'flag' follows Test-FpsEnvFlagOn.
    param([string]$Kind, [AllowNull()]$Value)
    if ($Kind -ceq 'value') { return (-not [string]::IsNullOrEmpty([string]$Value)) }
    if ($Kind -ceq 'truthy') {
        # the consumer's own rule (GpuDisplayViewport.cpp:51-60): empty is off; trimmed (ASCII whitespace) and lowercased, only 1 / true / yes / on are on. Text this module cannot read
        # the way the native reader does (non-ASCII) is read toward ON, never toward off.
        $text = [string]$Value
        if ([string]::IsNullOrEmpty($text)) { return $false }
        if ($text -notmatch '^[\x00-\x7F]*$') { return $true }
        return (Test-FaTextIn ($text.Trim(" `t`n`r`v`f").ToLowerInvariant()) @('1', 'true', 'yes', 'on'))
    }
    Test-FpsEnvFlagOn $Value
}

function ConvertTo-FpsEnvMap {
    # KEY=VALUE strings, a hashtable / dictionary or a parsed-JSON object -> @{ state; map; why }. state 'absent' (nothing given), 'malformed' (a value that is not a scalar, an entry that is not
    # KEY=VALUE, a key repeated with a different value) or 'ok'. Keys compare IGNORING CASE: the Windows process environment is case-insensitive, so mlvapp_stage_timing=1 is MLVAPP_STAGE_TIMING
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
        $text = if ($null -eq $v) { '' } else { [string]$v }
        if ($map.ContainsKey($pair[0]) -and -not (Test-FaTextEq $map[$pair[0]] $text)) { return (& $result 'malformed' $null "$($pair[0]) is repeated with different values") }
        $map[$pair[0]] = $text
    }
    & $result 'ok' $map ''
}

function Get-FpsArmFromEnvironment {
    <#
    .SYNOPSIS
    The arm of a local GUI smoke from its RECORDED launch environment (the local GUI smoke runner's result.launch.environment, or the KEY=VALUE list a caller passed). The app's rule: per-frame
    rows are logged when MLVAPP_PLAYBACK_SMOKE_TELEMETRY is on and MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG is not 1/true/yes/on. HEAVY when any per-frame diagnostic is on (timeline,
    detailed timeline, stage timing, perf field log, RBF detail timing) or the frame log is on; LIGHT when telemetry is off or only the counters run (frame log opted out). An absent environment
    or an ambiguous opt-out value (the app's two readers disagree on it) is UNKNOWN. An inherited variable the launch did not record is the limit of this reading.
    #>
    param([AllowNull()]$Environment, [AllowNull()]$Arguments = $null)
    $parsed = ConvertTo-FpsEnvMap $Environment
    if ($parsed.state -ceq 'absent') { return New-FpsArm 'UNKNOWN' 'environment' 'no-environment-recorded' }
    if ($parsed.state -cne 'ok') { return New-FpsArm 'UNKNOWN' 'environment' "malformed-environment ($($parsed.why))" }
    $map = $parsed.map
    # The app turns the stage logger on from its own command line too (--stage-log=<file>, main.cpp): a launch that passed it ran per-frame stage timing whatever its environment says
    # (adversary A3). Arguments given must all be text; any form of the option (--stage-log, -stage-log, /stage-log, with = or : or a following value) is HEAVY.
    if ($null -ne $Arguments) {
        foreach ($a in @($Arguments)) {
            if ($a -isnot [string]) { return New-FpsArm 'UNKNOWN' 'environment' 'malformed-arguments (an argument is not text)' }
            if ($a -imatch '^(--?|/)stage-log([=:]|$)') { return New-FpsArm 'HEAVY' 'environment' "the launch passed $($a.Split('=')[0]) (the stage logger)" }
        }
    }
    # The observer list is shared with the spec and result classifiers; each variable is read by ITS consumer's rule (adversary pass 3 A1): a flag is on unless exactly "0", a value (a file
    # name, a threshold) is on whenever it is non-empty, "0" included. HEAVY outranks UNKNOWN.
    $unknownObserver = $null
    foreach ($c in $script:FpsObserverControls) {
        if (-not $c.envVar -or -not $map.ContainsKey($c.envVar)) { continue }
        if (-not (Test-FpsEnvVarOn $c.kind $map[$c.envVar])) { continue }
        if ($c.severity -ceq 'HEAVY') { return New-FpsArm 'HEAVY' 'environment' "$($c.envVar) is on ($($c.id))" }
        if ($null -eq $unknownObserver) { $unknownObserver = $c }
    }
    if ($null -ne $unknownObserver) { return New-FpsArm 'UNKNOWN' 'environment' "$($unknownObserver.envVar) is set ($($unknownObserver.id)): an observer control a LIGHT proof excludes" }
    # LIGHT is PROVEN only against a closed list: the app has more per-frame diagnostics than any denylist here names (adversary A1: overlap trace, interactive trace, phase-3 telemetry
    # path, ...). Any other MLVAPP_* variable that is SET, to any value, is a control this reading does not know, so the arm is UNKNOWN, not LIGHT. "0" is no proof of off for it (adversary
    # pass 5 A1: MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE=0 is a probe MODE, raw_processing.c:346), and neither is an empty value (qEnvironmentVariableIsSet is true for it). Only a
    # variable this module lists is read by its consumer's rule above.
    foreach ($name in $map.Keys) {
        # a listed observer was judged above by its own rule (and found off); it is not a stranger
        if ($name -imatch '^MLVAPP_' -and -not $script:FpsArmKnownBenignEnv.Contains($name) -and -not $script:FpsObserverEnvNames.Contains($name)) {
            return New-FpsArm 'UNKNOWN' 'environment' "$name is an MLVAPP_* control this gate does not recognise (it may enable a per-frame diagnostic)"
        }
    }
    $telemetry = $map.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY') -and (Test-FpsEnvFlagOn $map['MLVAPP_PLAYBACK_SMOKE_TELEMETRY'])
    if (-not $telemetry) { return New-FpsArm 'LIGHT' 'environment' 'frame telemetry is off' }
    if (-not $map.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG')) { return New-FpsArm 'HEAVY' 'environment' 'frame telemetry is on and the per-frame log is not opted out' }
    # No Trim, ASCII only (adversary A2): .NET trims U+00A0 and other Unicode spaces that the native readers' ASCII-only trim keeps, so "<U+00A0>1<U+00A0>" is not the opt-out they see.
    $raw = $map['MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG']
    if ($raw -notmatch '^[\x00-\x7F]*$' -or $raw -match '\s') { return New-FpsArm 'UNKNOWN' 'environment' 'the per-frame log opt-out holds whitespace or non-ASCII text, which the native readers handle differently' }
    $d = $raw.ToLowerInvariant()
    # ordinal, not -in (culture compare): "1" + U+0001 is not the "1" the native reader sees (adversary r5 pass 2 A1)
    if (Test-FaTextIn $d @('1', 'true', 'yes', 'on')) { return New-FpsArm 'LIGHT' 'environment' 'frame telemetry on, per-frame log opted out' }
    if (Test-FaTextIn $d @('', '0', 'false', 'off', 'no')) { return New-FpsArm 'HEAVY' 'environment' 'frame telemetry on, per-frame log opt-out is explicitly off' }
    New-FpsArm 'UNKNOWN' 'environment' "per-frame log opt-out value '$d' is read differently by the app's two readers"
}

function Get-FpsArmFromLaunch {
    <#
    .SYNOPSIS
    The arm of a local GUI smoke from the `launch` block its own result recorded (environment AND arguments). A result with no launch block, no environment or no argument list is UNKNOWN: the
    argument list can carry --stage-log, so an arm judged from the environment alone is not proof of LIGHT.
    #>
    param([AllowNull()]$Launch)
    if ($null -eq $Launch -or $Launch -is [string] -or $Launch -is [System.ValueType]) { return New-FpsArm 'UNKNOWN' 'environment' 'no-launch-block-recorded' }
    $hasEnv = if ($Launch -is [System.Collections.IDictionary]) { $Launch.Contains('environment') } else { $null -ne $Launch.PSObject.Properties['environment'] }
    $hasArgs = if ($Launch -is [System.Collections.IDictionary]) { $Launch.Contains('arguments') } else { $null -ne $Launch.PSObject.Properties['arguments'] }
    if (-not $hasEnv) { return New-FpsArm 'UNKNOWN' 'environment' 'no-environment-recorded' }
    if (-not $hasArgs) { return New-FpsArm 'UNKNOWN' 'environment' 'no-arguments-recorded (the argument list can carry --stage-log)' }
    $launchArgs = Get-FaProp $Launch 'arguments'
    Get-FpsArmFromEnvironment -Environment (Get-FaProp $Launch 'environment') -Arguments @($launchArgs)
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

Export-ModuleMember -Function Get-FpsObserverControls, Get-FpsArmFromLegRecord, Get-FaProp, New-FpsArm, ConvertTo-FpsArmName, Get-FpsArmFromLegSpec, Get-FpsArmFromJobResult, Get-FpsArm, Test-FpsEnvFlagOn, ConvertTo-FpsEnvMap, Get-FpsArmFromEnvironment, Get-FpsArmFromLaunch, Get-FpsPairVerdict, Get-FpsArmsVerdict, Get-FpsLegPlanRefusal
