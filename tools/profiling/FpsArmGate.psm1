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
#   (c) on a venue leg the generator's own forwarded switches (the seven generatorArgs keys of $script:FpsObserverControls) are all off, and they carry no observerControls entry;
#   (d) on a local launch or a saved record, the recorded launch ARGUMENTS and the recorded MLVAPP_* / DUALISO_* ENVIRONMENT match the producers' exact LIGHT
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
    New-FpsAllow '--drop-frame-mode' 'next' 'oneof:persisted|off'       # :1493 (default :27); R2-RESIDUE-1 r1 (698fffb1 A5): an unpaced leg (spec key unpaced) forwards -DropFrameMode off (job:6550) beside MLVAPP_PLAYBACK_UNPACED, a row below
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
    New-FpsAllow '--windowed' 'bare' ''                                 # :1681 via -AdditionalArgs; R2-RESIDUE-1 r1 (698fffb1 A5): a windowed leg (spec key displayMode) passes it (job:6330); the display mode is the A/B variable of the display matrix, not a logger
    # R2-RESIDUE-1 r3 (sol 5add23cf A3): the venue leg keeps window captures unless its spec opts out (visualCapture), so EVERY default speed leg is launched with --visual-capture-dir <dir> (job:6677 appends it to the
    # smoke command's one -AdditionalArgs array; its pre-launches carry the same flag with their own directory, job:6429). The app captures the window AFTER the measured Play has closed (the length gate's own
    # option policy classifies it 'allow' for the same reason), so it is not a logger inside the timed window. The spec reader reads such a spec LIGHT, so the launch reader must read the launch it emits LIGHT too.
    New-FpsAllow '--visual-capture-dir' 'next' 'text'                   # job:6677 (a directory path, the next list element)
)
# ENVIRONMENT (MLVAPP_* only; any other name is not the app's control surface). Names compare ignoring case (the Windows environment does).
$script:FpsLightEnv = @(
    New-FpsAllow 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY' 'env' 'exact:1'                     # runner:1693, job:3027  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG' 'env' 'oneof:1|0' # job:3028 (1 = LIGHT); 0 is the HEAVY arm's value (:3039)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG' 'env' 'exact:1'          # job:3029  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_CRASH_FORENSICS_LOG_DIR' 'env' 'text' -Required               # runner:1695 and :1717 (every launch sets it)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_MAX_THREADS' 'env' 'int'                             # runner:1690  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_QUALITY_MODE' 'env' 'oneof:phase3_hq|hq'            # job:3049; 'hq' is the spec key qualityMode on a cpu leg (job:421 ValidateSet, :5951)  (producer cite, alias table in header)
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
    # r14 (adversary r14 pass 18 A4, DISTINGUISHED): the CUDA A/B wrapper's own speed leg sets the recon output validator to 0, a name this table deliberately does not carry (r8: UNKNOWN at any
    # value, pinned by test_the_recon_output_validation_flag_is_outside_the_light_shape); making the wrapper's legs LIGHT is the queued successor BENCH-WRAPPER-LIGHT-ENV-1, not a row here.
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
    # R2-RESIDUE-1 r1 (adversary 698fffb1 A5, 1ce472bc A6): the venue leg forwards these isolation / A/B arms from its spec for any leg (Invoke-VenueLeg.ps1:572-607), and the SPEC reader reads such a
    # leg as LIGHT, so the launch reader must read the launch it emits as LIGHT too (a spec and its own launch that disagree admit a plan whose recorded run then fails the gate). Each is a decode,
    # pacing or scheduling switch, not a logger or a sampler; each row carries the exact value domain the generator writes. Unknown stays UNKNOWN: a name or value not here is still refused.
    New-FpsAllow 'MLVAPP_RAW_UINT16_PREFETCH_DECODERS' 'env' 'oneof:1|2|3|4'           # job:6126 (spec key rawPrefetchDecoders 1..4, ValidateRange -1..4)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_DISABLE_RAW_UINT16_PREFETCH' 'env' 'exact:1'                  # job:6126 (rawPrefetchDecoders 0)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_DISABLE_PREP_POINTWISE_FAST' 'env' 'exact:1'                  # job:6565 (spec key disablePrepPointwiseFast)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE' 'env' 'exact:1'                 # job:6577 (spec key prepConflatedMove)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_RECON_RELEASE_DEFERRED' 'env' 'exact:0'                   # job:6589 (spec key disableReconReleaseDeferred)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY' 'env' 'exact:post'                # job:6601 (spec key dueWakeDeliveryPost)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_RECON_OMP_THREADS' 'env' 'int'                       # job:6655 (spec key reconOmpThreads)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_PREP_OMP_THREADS' 'env' 'int'                        # job:6655 (spec key prepOmpThreads)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES' 'env' 'int'                 # job:6120 (spec key playbackRenderLookaheadFrames, a forced-look pace leg)  (producer cite, alias table in header)
    New-FpsAllow 'MLVAPP_GPU_SAFE_START' 'env' 'exact:clear'                           # job:6416 (spec key gpuSafeStart, ValidateSet '' | clear)  (producer cite, alias table in header)
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
    # r13 (adversary pass 15 A1): not a logger but a hook that moves the presentation surface (fullscreen / windowed / restore / maximize) inside the measured Play (Invoke-VenueLeg.ps1:566 forwards it,
    # the job emits MLVAPP_GUI_SMOKE_MID_PLAY_WINDOW_TOGGLES); a spec that names it must not read LIGHT while the same setting recorded as a launch environment reads UNKNOWN.
    New-FpsObserver 'mid-play-window-toggles' 'midPlayWindowToggles' 'bool'
    # r14 (sol r13 B1): a timed GPU device-loss injection inside the measured Play (Invoke-VenueLeg.ps1:540 forwards it by presence, the job emits MLVAPP_GPU_FAULT_INJECT, which the launch reader
    # already refuses); a spec that names it must not read LIGHT either.
    New-FpsObserver 'gpu-fault-inject' 'gpuFaultInject' 'bool'
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

function Test-FpsSpeedLeg {
    # r10 (G10): THE one rule for "this leg measures presented fps": the container carries a legType and it is exactly the text 'speed' (ordinal; absent, null, 'Speed', ' speed', a number and every other
    # value are not). The record check and the spec reader both call this, so a record and a spec that are otherwise LIGHT cannot disagree about it. Private (not exported).
    param([AllowNull()]$Container)
    (Test-FaHasProp $Container 'legType') -and (Test-FaTextEq (Get-FaProp $Container 'legType') 'speed')
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
    $scopeProblem = Get-FpsSpecScopeProblem $Holder
    if ($null -ne $scopeProblem) { return "its $scopeProblem" }
    $null
}

function Get-FpsSpecScopeProblem {
    # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A3, 8747fced A3): the spec and record readers read a scopes leg (scope histogram, scopeIntervalMs 0, scopesDuringPlayback throttled) as LIGHT,
    # while the launch it emits is outside the LIGHT launch shape (--scope exact:none; no MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS or MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK row), so the launch reader
    # says UNKNOWN. The spec side now predicts the same: by the way Invoke-VenueLeg forwards each key (scope [string], scopeIntervalMs [int], the job's `-ge 0`; scopesDuringPlayback [string], the
    # job's non-empty test), a scope other than exactly 'none', an interval other than the default -1, or a non-empty scopes mode is outside the shape. Returns the problem text, else $null.
    param([AllowNull()]$Holder)
    if ((Test-FaHasProp $Holder 'scope') -and -not (Test-FaTextEq (Get-FaProp $Holder 'scope') 'none')) { return "generatorArgs.scope is not 'none' (the LIGHT launch passes --scope none only)" }
    if (Test-FaHasProp $Holder 'scopeIntervalMs') {
        $v = Get-FaProp $Holder 'scopeIntervalMs'
        if (-not (($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal]) -and $v -eq -1)) { return 'generatorArgs.scopeIntervalMs sets MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS (outside the LIGHT launch shape)' }
    }
    if (Test-FaHasProp $Holder 'scopesDuringPlayback') {
        $v = Get-FaProp $Holder 'scopesDuringPlayback'
        if (-not ($null -eq $v -or ($v -is [string] -and $v.Length -eq 0))) { return 'generatorArgs.scopesDuringPlayback sets MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK (outside the LIGHT launch shape)' }
    }
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
    if ((Test-FaHasProp $Record 'legType') -and -not (Test-FpsSpeedLeg $Record)) { return "its legType is not 'speed'" }
    $hasLaunch = Test-FaHasProp $Record 'launch'
    $hasGa = Test-FaHasProp $Record 'generatorArgs'
    # r10 (G10): generatorArgs are a venue SPEC's fields, and a spec proves LIGHT only on a speed leg (Get-FpsArmFromLegSpec), so a record that leans on them ALONE carries the same legType. A record
    # proven by its recorded launch (the local smoke producers write no legType) is judged on the launch, and its generatorArgs only have to agree (checked below).
    if ($hasGa -and -not $hasLaunch -and -not (Test-FpsSpeedLeg $Record)) { return "its generatorArgs prove LIGHT only on a speed leg and it records no legType 'speed'" }
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
    # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A2): an arm OBJECT was returned unchecked, so {arm:'LIGHT', observerControls:{...}} read LIGHT; it is a LIGHT arm only in the closed shape
    $shapeProblem = Get-FpsArmObjectShapeProblem $field
    if ($null -ne $shapeProblem) { return New-FpsArm 'UNKNOWN' 'record' "telemetryArm=LIGHT but $shapeProblem" }
    if ($field -isnot [string]) { return $field }
    New-FpsArm 'LIGHT' 'record' 'telemetryArm=LIGHT'
}

function Get-FpsArmObjectShapeProblem {
    # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A2): an arm OBJECT (what New-FpsArm writes, and the smoke summaries carry) has exactly the keys arm, source and reason. Any other key (an
    # observerControls entry, a forwarded switch) is outside that shape, so the object is not proof of LIGHT. A plain arm word has no shape to check. Returns the problem text, else $null.
    param([AllowNull()]$Field)
    if ($null -eq $Field -or $Field -is [string]) { return $null }
    $names = if ($Field -is [System.Collections.IDictionary]) { @($Field.Keys) } else { @($Field.PSObject.Properties | ForEach-Object { $_.Name }) }
    foreach ($name in $names) {
        if (-not (Test-FaTextIn ([string]$name) @('arm', 'source', 'reason'))) { return "its telemetryArm object carries '$name', outside the arm object shape (arm, source, reason)" }
    }
    $null
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
        if (-not (Test-FpsSpeedLeg $Spec)) { return New-FpsArm 'UNKNOWN' 'spec' 'generatorArgs.telemetryArm=LIGHT but the leg is not a speed leg (a look leg captures its contact sheet inside the timed Play)' }
        # the arm word says LIGHT, but a spec that also switches on a forwarded observer (a per-launch logger, a sampler) is not proof of it (adversary pass 3 A2)
        $observer = Get-FpsSpecObserverOn $ga
        if ($null -ne $observer) { return New-FpsArm 'UNKNOWN' 'spec' "generatorArgs.telemetryArm=LIGHT but generatorArgs.$($observer.specKey) enables an observer ($($observer.id)) inside the timed window" }
        # r14 (sol r13 B2): the record reader's rule for the same generatorArgs (Get-FpsSpecHolderProblem): an observerControls entry is outside the LIGHT shape, so a spec and a record that
        # embeds the same generatorArgs cannot disagree about it.
        if (Test-FaHasProp $ga 'observerControls') { return New-FpsArm 'UNKNOWN' 'spec' 'generatorArgs.telemetryArm=LIGHT but generatorArgs carries an observerControls entry, which no producer records (outside the LIGHT shape)' }
        $scopeProblem = Get-FpsSpecScopeProblem $ga
        if ($null -ne $scopeProblem) { return New-FpsArm 'UNKNOWN' 'spec' "generatorArgs.telemetryArm=LIGHT but $scopeProblem" }
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

function Get-FpsCompareBindingProblem {
    <#
    .SYNOPSIS
    R2-RESIDUE-1 r3 (sol 5add23cf A10): the speed compare is bound to the judged pair in EVERY metric it publishes, not in presentedFps alone. r1/r2 bound presentedFps (and its delta / deltaPercent), then the
    readers printed the compare's guiStatusFps, avgPresentIntervalMs and the rest from an unbound compare (judged legs both 40 fps / 40 GUI fps, compare GUI 10 -> 20 = +100 %, interval 50 -> 25 = -50 %).
    Every metric object the compare carries is checked with the same rule: each endpoint is the judged leg's own number of that name (float noise only), the delta and deltaPercent are recomputed from the
    endpoints with the producer's formulas. A metric whose figures cannot be bound (the legs record no such number, a published figure that does not follow) refuses the whole compare, so an unbound
    metric is never published as speed evidence. Returns the problem text, else $null.
    #>
    param([AllowNull()]$Compare, [AllowNull()]$Baseline, [AllowNull()]$Candidate)
    if ($null -eq $Compare -or $Compare -is [string] -or $Compare -is [System.ValueType]) { return $null }
    $leaf = { param($o, $n) if ($o -is [System.Collections.IDictionary]) { if ($o.Contains($n)) { , $o[$n] } else { $null } } elseif ($null -ne $o -and $null -ne $o.PSObject.Properties[$n]) { , $o.PSObject.Properties[$n].Value } else { $null } }
    $names = if ($Compare -is [System.Collections.IDictionary]) { @($Compare.GetEnumerator() | ForEach-Object { [string]$_.Key }) } else { @($Compare.PSObject.Properties | ForEach-Object { $_.Name }) }
    foreach ($name in $names) {
        $metric = & $leaf $Compare $name
        if ($null -eq $metric) { continue }
        if ([string]::Equals($name, 'presentedFps', [System.StringComparison]::OrdinalIgnoreCase)) {
            $problem = Get-FpsPresentedFpsBindingProblem -Compare $Compare -Baseline $Baseline -Candidate $Candidate
        } else {
            $problem = Get-FpsMetricBindingProblem -Name $name -Metric $metric -Baseline $Baseline -Candidate $Candidate
        }
        if ($null -ne $problem) { return $problem }
    }
    $null
}

function Test-FpsFigureMatches {
    # R2-RESIDUE-1 r3 (sol 5add23cf A6, A7): a published delta or deltaPercent is EXACTLY the figure the producer's formula gives (the same arithmetic over the same two doubles, rounded to 6 and 3 places), and
    # that figure must be a finite number. A tolerance of 1e-9 let -1.0000000005 stand for the producer's -1.000 and flip a threshold read, and an overflowing figure made the tolerance Infinity, which
    # accepted any published value. Anything that does not compare exactly, and anything not finite on either side, does not match.
    param($Published, [double]$Expected)
    $null -ne $Published -and [double]::IsFinite([double]$Expected) -and [double]::IsFinite([double]$Published) -and ([double]$Published -eq $Expected)
}

function Get-FpsMetricBindingProblem {
    # R2-RESIDUE-1 r3 (sol 5add23cf A10): one NON-presentedFps metric of the speed compare (a New-MetricDelta object: baseline, candidate, delta, deltaPercent). The legs record the same-named figure
    # (the producer builds the metric from the two leg summaries), so an endpoint must equal it (float noise only; a figure the leg does not record binds to nothing), and the published delta / deltaPercent
    # must be what the producer's formulas give for those endpoints (delta = Round(c - b, 6); deltaPercent = Round((c - b) / b * 100, 3) only when |b| > 1e-6, otherwise none). A metric that carries
    # no figure at all (the producer's all-null object) publishes nothing and binds trivially. Returns the problem text, else $null.
    param([string]$Name, [AllowNull()]$Metric, [AllowNull()]$Baseline, [AllowNull()]$Candidate)
    $isNumber = { param($v) $null -ne $v -and ($v -is [double] -or $v -is [single] -or $v -is [decimal] -or $v -is [int] -or $v -is [long]) -and [double]::IsFinite([double]$v) }
    $leaf = { param($o, $n) if ($o -is [System.Collections.IDictionary]) { if ($o.Contains($n)) { , $o[$n] } else { $null } } elseif ($null -ne $o -and $null -ne $o.PSObject.Properties[$n]) { , $o.PSObject.Properties[$n].Value } else { $null } }
    if ($Metric -is [string] -or $Metric -is [System.ValueType] -or $Metric -is [System.Array]) { return "the compare's $Name is not a metric object (baseline, candidate, delta, deltaPercent), so it cannot be bound to the judged legs" }
    $figure = @(); foreach ($field in 'baseline', 'candidate', 'delta', 'deltaPercent') { if ($null -ne (& $leaf $Metric $field)) { $figure += $field } }
    if ($figure.Count -eq 0) { return $null }
    $endpoints = @{}
    foreach ($side in @(@('baseline', $Baseline), @('candidate', $Candidate))) {
        $own = & $leaf $side[1] $Name
        $compared = & $leaf $Metric $side[0]
        $ownIsNumber = & $isNumber $own
        if ($null -eq $compared -and -not $ownIsNumber) { $endpoints[$side[0]] = $null; continue }
        if (-not $ownIsNumber) { return "the judged $($side[0]) leg records no finite $Name, so the compare's $Name $($side[0]) cannot be bound to it" }
        if (-not (& $isNumber $compared)) { return "the compare's $Name $($side[0]) is not a finite number (the judged $($side[0]) leg measured $own)" }
        if ([Math]::Abs([double]$compared - [double]$own) -gt (0.000000001 * [Math]::Abs([double]$own))) { return "the compare's $Name $($side[0]) is $compared but the judged $($side[0]) leg measured $own" }
        $endpoints[$side[0]] = [double]$compared
    }
    $publishedDelta = & $leaf $Metric 'delta'
    $publishedPercent = & $leaf $Metric 'deltaPercent'
    if ($null -eq $endpoints['baseline'] -or $null -eq $endpoints['candidate']) {
        if ($null -ne $publishedDelta -or $null -ne $publishedPercent) { return "the compare's $Name publishes a delta or deltaPercent although an endpoint is missing" }
        return $null
    }
    $base = $endpoints['baseline']; $cand = $endpoints['candidate']
    if ($null -ne $publishedDelta) {
        if (-not (& $isNumber $publishedDelta)) { return "the compare's $Name delta is not a finite number" }
        $expectedDelta = [Math]::Round($cand - $base, 6)
        if (-not (Test-FpsFigureMatches $publishedDelta $expectedDelta)) { return "the compare's $Name delta is $publishedDelta but candidate $cand minus baseline $base is $expectedDelta" }
    }
    if ($null -ne $publishedPercent) {
        if (-not (& $isNumber $publishedPercent)) { return "the compare's $Name deltaPercent is not a finite number" }
        if ([Math]::Abs($base) -le 0.000001) { return "the compare's $Name deltaPercent is $publishedPercent beside a baseline of $base (the producer publishes no percent there)" }
        $expectedPercent = [Math]::Round((($cand - $base) / $base) * 100.0, 3)
        if (-not (Test-FpsFigureMatches $publishedPercent $expectedPercent)) { return "the compare's $Name deltaPercent is $publishedPercent but (candidate $cand - baseline $base) / baseline * 100 is $expectedPercent" }
    }
    $null
}

function Get-FpsPresentedFpsBindingProblem {
    # ENVELOPE-PREEMPT-R1-RESIDUE-1 / R2-RESIDUE-1 r1 (adversary 698fffb1 A7): the speed compare and the LIGHT legs the arm gate judged are separate inputs of the packet, so a verdict could be judged on
    # one pair (a LIGHT candidateSpeed leg at 40 fps) and compared on another (the ordinary HEAVY candidate at 60 fps, +20 %). The compare's presentedFps baseline and candidate must be the very numbers
    # the judged baseline leg and the judged candidate leg recorded (a finite number on each, equal). Returns the problem text, else $null.
    param([AllowNull()]$Compare, [AllowNull()]$Baseline, [AllowNull()]$Candidate)
    $isNumber = { param($v) $null -ne $v -and ($v -is [double] -or $v -is [single] -or $v -is [decimal] -or $v -is [int] -or $v -is [long]) -and [double]::IsFinite([double]$v) }
    $leaf = { param($o, $n) if ($o -is [System.Collections.IDictionary]) { if ($o.Contains($n)) { , $o[$n] } else { $null } } elseif ($null -ne $o -and $null -ne $o.PSObject.Properties[$n]) { , $o.PSObject.Properties[$n].Value } else { $null } }
    $metric = & $leaf $Compare "presentedFps"
    foreach ($side in @(@("baseline", $Baseline), @("candidate", $Candidate))) {
        $own = & $leaf $side[1] "presentedFps"
        $compared = & $leaf $metric $side[0]
        if (-not (& $isNumber $own)) { return "the judged $($side[0]) leg records no finite presentedFps, so the compare's $($side[0]) cannot be bound to it" }
        if (-not (& $isNumber $compared)) { return "the compare's presentedFps $($side[0]) is not a finite number" }
        # r2 (sol c29f8dc1 A1, sol 5add23cf A2): the producer copies the leg's number into the compare unrounded, so the two are the same double and only float noise (1e-9 relative) is owed: slack of
        # 1e-6 let a leg at 0 fps bind to a compare of 1e-7 / 2e-7 (a +100 % gain), and a relative 1e-6 let 49.999975 / 50.000025 stand for two legs at 50 (a tie published as a gain).
        if ([Math]::Abs([double]$compared - [double]$own) -gt (0.000000001 * [Math]::Abs([double]$own))) {
            return "the compare's presentedFps $($side[0]) is $compared but the judged $($side[0]) leg measured $own"
        }
    }
    # R2-RESIDUE-1 r2 (luna r1b A1): the published delta and deltaPercent must follow from the two bound endpoints. Both legs at 40 fps with delta 40 / deltaPercent 100 bound on the endpoints alone
    # and published a gain that no leg measured. The values are recomputed with the PRODUCER'S OWN formulas (run-release-cuda-playback-ab.ps1 New-MetricDelta: delta = Round(candidate - baseline, 6),
    # deltaPercent = Round((candidate - baseline) / baseline * 100, 3), no percent at all for |baseline| <= 1e-6) and compared with float noise only (sol c29f8dc1 A2, sol 5add23cf A1: a tolerance of
    # half a rounding step accepted -1 for the producer's -1.001, and a percent beside a 1e-7 baseline, which the producer never publishes).
    # A present delta or deltaPercent that is not a finite number is refused; an absent one is left to the reader's own null check.
    $base = [double](& $leaf $metric "baseline")
    $cand = [double](& $leaf $metric "candidate")
    if ($base -le 0.000001) { return "the compare's presentedFps baseline is $base, not above 0.000001 (the producer publishes no deltaPercent there), so no gain or cost can follow from it" }
    $publishedDelta = & $leaf $metric "delta"
    if ($null -ne $publishedDelta) {
        if (-not (& $isNumber $publishedDelta)) { return "the compare's presentedFps delta is not a finite number" }
        $expectedDelta = [Math]::Round($cand - $base, 6)
        if (-not (Test-FpsFigureMatches $publishedDelta $expectedDelta)) { return "the compare's presentedFps delta is $publishedDelta but candidate $cand minus baseline $base is $expectedDelta" }
    }
    $publishedPercent = & $leaf $metric "deltaPercent"
    if ($null -ne $publishedPercent) {
        if (-not (& $isNumber $publishedPercent)) { return "the compare's presentedFps deltaPercent is not a finite number" }
        $expectedPercent = [Math]::Round((($cand - $base) / $base) * 100.0, 3)
        if (-not (Test-FpsFigureMatches $publishedPercent $expectedPercent)) { return "the compare's presentedFps deltaPercent is $publishedPercent but (candidate $cand - baseline $base) / baseline * 100 is $expectedPercent" }
    }
    $null
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
        # r14 (key3 r13 B1, dictionary-member-shadow): never `$Environment.Keys`: on an IDictionary PowerShell resolves `.Keys` to an ENTRY named Keys (any case) before the property, so a
        # recorded variable named Keys would hide every other name. The enumerator is a method call, which no entry name can shadow.
        foreach ($entry in $Environment.GetEnumerator()) { $pairs.Add(@([string]$entry.Key, $entry.Value)) }
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
    # Do the recorded app-control variables match the LIGHT shape table exactly (names ignoring case, values ordinal)? Returns the first problem, else $null. The app's control names are
    # MLVAPP_* and DUALISO_* (r14, sol r13 B6: src/mlv/llrawproc/dualiso.c reads DUALISO_DEBUG_SOURCE_SMOOTH_DIR and dumps image planes inside the measured Play); no table row is DUALISO_*, so
    # any such name is outside the shape. Other names are not judged.
    param($Map)
    # r14 (key3 r13 B1, dictionary-member-shadow): the entries are enumerated, never read through `$Map.Keys`, which an entry named Keys (any case) shadows on an IDictionary.
    foreach ($entry in $Map.GetEnumerator()) {
        $name = [string]$entry.Key
        if (-not ($name.StartsWith('MLVAPP_', [System.StringComparison]::OrdinalIgnoreCase) -or $name.StartsWith('DUALISO_', [System.StringComparison]::OrdinalIgnoreCase))) { continue }
        $row = $null
        if (-not $script:FpsLightEnvIndex.TryGetValue($name, [ref]$row)) { return "$name is not in the LIGHT launch shape" }
        if (-not (Test-FpsAllowedValue $row.rule $entry.Value)) { return "$name has a value the LIGHT launch shape does not allow" }
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
    # R2-RESIDUE-1 r3 (sol 5add23cf A4): the HEAVY recognizer runs BEFORE the LIGHT table refuses anything, so a launch that carries the HEAVY arm's own telemetry (frame telemetry exactly on, the per-frame log NOT
    # opted out) is HEAVY whatever else it names (the HEAVY producer also sets timeline telemetry, stage timing and the perf field log, names no LIGHT row carries) and never UNKNOWN. Both refuse a fps verdict;
    # the typed reason differs (heavy-arm / unknown-arm). A launch that opts the frame log out is still judged by the LIGHT table below, and the recognizer needs the exact text '1' (anything else stays UNKNOWN).
    $earlyMap = $parsed.map
    if ($earlyMap.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY') -and (Test-FaTextEq $earlyMap['MLVAPP_PLAYBACK_SMOKE_TELEMETRY'] '1')) {
        $optOut = if ($earlyMap.ContainsKey('MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG')) { $earlyMap['MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG'] } else { $null }
        if ($null -eq $optOut -or (Test-FaTextEq $optOut '0')) { return New-FpsArm 'HEAVY' 'environment' 'frame telemetry is on and the per-frame log is not opted out' }
    }
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

function Get-FpsArmFromSmokeRecord {
    <#
    .SYNOPSIS
    The arm of a local GUI smoke from its WHOLE result record: its recorded `launch` block (Get-FpsArmFromLaunch), and when that reads LIGHT, the record rule over everything else it carries
    (Get-FpsRecordLightProblem: an observerControls entry, a look block, a legType but 'speed', an enabled forwarded switch). r14 (sol r13 B2): reading the launch alone discarded record-level
    evidence the saved-leg reader refuses. The smoke runner's own result carries none of those keys, so a producer record is judged on its launch as before.
    #>
    param([AllowNull()]$Record)
    # Get-FaProp keeps a one-element array whole (adversary r5 A6), so "launch": [ { ... } ] is refused by Get-FpsArmFromLaunch.
    $arm = Get-FpsArmFromLaunch (Get-FaProp $Record 'launch')
    if ($arm.arm -ceq 'LIGHT') {
        $problem = Get-FpsRecordLightProblem $Record
        if ($null -ne $problem) { return New-FpsArm 'UNKNOWN' 'environment' "the recorded launch reads LIGHT but $problem" }
        # r14 (adversary r14 pass 18 A2): the smoke runner's result carries no arm word, so one that does must agree with its launch; telemetryArm HEAVY (or anything but LIGHT) beside a
        # LIGHT launch is contradictory evidence, never a LIGHT the derived arm overrides.
        if ((Test-FaHasProp $Record 'telemetryArm') -and (ConvertTo-FpsArmName (Get-FaProp $Record 'telemetryArm')) -cne 'LIGHT') {
            return New-FpsArm 'UNKNOWN' 'environment' 'the recorded launch reads LIGHT but the record names a telemetryArm that is not LIGHT'
        }
        # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A2, sibling :495): a LIGHT arm object beside the launch must be in the closed arm-object shape too
        $shapeProblem = if (Test-FaHasProp $Record 'telemetryArm') { Get-FpsArmObjectShapeProblem (Get-FaProp $Record 'telemetryArm') } else { $null }
        if ($null -ne $shapeProblem) { return New-FpsArm 'UNKNOWN' 'environment' "the recorded launch reads LIGHT but $shapeProblem" }
    }
    $arm
}

# r15 (adversary r14 pass 18 A1): the keys every judged reader walks by member access (`$x.proof.playbackAb.compare.presentedFps.delta`, `$smoke.launch.environment`, `$r.fps.smokePresentedFps`).
# Each is a JSON object in every producer (run-release-gui-smoke.ps1, run-release-cuda-playback-ab.ps1, run-local-cuda-playback-dng-smoke.ps1, compare-release-gui-smoke-ab.ps1); an array under one of
# these names is refused by Get-FpsJsonRepeatedKeyProblem. Names compared ignoring case, as PowerShell member access does.
$script:FpsJsonObjectEnvelopes = [System.Collections.Generic.HashSet[string]]::new([string[]]@(
        'proof', 'playback', 'playbackAb', 'cdng', 'compare', 'speedCompare', 'analysis', 'fpsVerdict',
        'baseline', 'candidate', 'candidateSpeed', 'launch', 'environment', 'observerControls', 'generatorArgs',
        'presentedFps', 'guiStatusFps', 'fps', 'smokePresentedFps', 'visibleBottomLeftGuiFps', 'smokeTimelineFps', 'autoDecision',
        'machineFingerprint', 'inputs', 'host'), [StringComparer]::OrdinalIgnoreCase)
# ('evidence', 'before' and 'after' are left out on purpose: section verdicts and diagnostics carry `evidence` as a list of lines, and the readers reach analysis.evidence through Get-FaProp,
# which keeps an array whole, so an array there reads as no evidence and Test-FpsCachedAnalysisBound returns $false.)

function Test-FpsCachedAnalysisBound {
    <#
    .SYNOPSIS
    r15 (adversary r14 pass 18 A7, sol r13 B3): a cached playback A/B `analysis` (its dominantBottleneck / suggestedOptimization) is derived from ONE compared pair's presented-fps delta. A reader
    that gated pair P may publish that cached diagnosis only when the analysis is provably P's own: its comparisonBasis names P's candidate leg (ordinal) AND its evidence.presentedFpsDeltaPercent
    is the same number as P's compare.presentedFps.deltaPercent. A record whose analysis names another basis (the HEAVY candidate pair beside a LIGHT candidateSpeed pair), carries no basis or no
    evidence (a record written before the binding), or a different delta is NOT bound: the reader re-derives from the gated compare or withholds, never repeats the cached text.
    #>
    param([AllowNull()]$Analysis, [AllowNull()]$Compare, [string]$ComparisonBasis)
    if ($null -eq $Analysis -or $null -eq $Compare -or [string]::IsNullOrEmpty($ComparisonBasis)) { return $false }
    $basis = Get-FaProp $Analysis 'comparisonBasis'
    # ENVELOPE-PREEMPT-RESIDUE-1 r1 (adversary r1 A2): ORDINAL, not -cne: PowerShell's culture-aware compare ignores a soft hyphen, so 'candidate<U+00AD>Speed' matched 'candidateSpeed'
    if ($basis -isnot [string] -or -not [string]::Equals($basis, $ComparisonBasis, [StringComparison]::Ordinal)) { return $false }
    $evidence = Get-FaProp $Analysis 'evidence'
    # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 0e9672e5 A1): and FINITE: valid JSON 1e400 parses to +Infinity, and two different overflowing numbers then compare equal
    $isNumber = { param($v) $null -ne $v -and ($v -is [double] -or $v -is [single] -or $v -is [decimal] -or $v -is [int] -or $v -is [long] -or $v -is [int16] -or $v -is [byte]) -and [double]::IsFinite([double]$v) }
    # The presented-fps delta must be a number on both sides (the diagnosis is undefined without it).
    $cached = Get-FaProp $evidence 'presentedFpsDeltaPercent'
    $judged = Get-FaProp (Get-FaProp $Compare 'presentedFps') 'deltaPercent'
    if (-not (& $isNumber $cached) -or -not (& $isNumber $judged)) { return $false }
    # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A4, 8747fced A2): EXACT, never within a tolerance (-1.0000004 and -1.0 sit on opposite sides of the -1 % diagnosis threshold), and
    # over EVERY input the producer's diagnosis read (New-PlaybackAbAnalysis, run-release-cuda-playback-ab.ps1), not the fps delta alone: cached queue-wait advice beside a compare with no
    # queue-wait regression is another pair's diagnosis. Each recorded evidence field must equal the compare's own value (both absent / null, or the same number); anything else is not bound.
    foreach ($pair in $script:FpsCachedAnalysisEvidence) {
        $c = Get-FaProp $evidence $pair[0]
        $j = Get-FaProp (Get-FaProp $Compare $pair[1]) $pair[2]
        if ($null -eq $c -and $null -eq $j) { continue }
        if (-not (& $isNumber $c) -or -not (& $isNumber $j)) { return $false }
        # (adversary 1ce472bc A4: exact on integers too: [double] collapses 9007199254740993 and 9007199254740992, so two integers compare as integers, and an integer past 2^53 is no double's equal)
        $bothIntegral = ($c -is [int] -or $c -is [long] -or $c -is [int16] -or $c -is [byte]) -and ($j -is [int] -or $j -is [long] -or $j -is [int16] -or $j -is [byte])
        if ($bothIntegral) {
            if ([long]$c -ne [long]$j) { return $false }
            continue
        }
        if (([double]$c) -ne ([double]$j)) { return $false }
        foreach ($side in @($c, $j)) {
            if (($side -is [int] -or $side -is [long]) -and [Math]::Abs([double]$side) -ge 9007199254740992) { return $false }
        }
    }
    # R3-MERGE-RESIDUE-1 (sol 07d50621 A11): matching INPUTS did not make the cached TEXT the diagnosis of those inputs (fps +10 % with every stage field absent matched a cached
    # queue-wait-bound, where the producer says none / validate_same_clip_on_second_machine). A label the cached analysis carries must be the one the producer's own ladder gives for
    # the evidence that was just proved equal to the judged compare's; a label that is not (or is not a string) leaves the analysis unbound, so no reader repeats it.
    $diagnosis = Get-FpsEvidenceDiagnosis $evidence
    foreach ($field in @(@('dominantBottleneck', 0), @('suggestedOptimization', 1))) {
        $cachedText = Get-FaProp $Analysis $field[0]
        if ($null -eq $cachedText -or ($cachedText -is [string] -and $cachedText.Length -eq 0)) { continue }
        if ($cachedText -isnot [string] -or -not [string]::Equals($cachedText, [string]$diagnosis[$field[1]], [StringComparison]::Ordinal)) { return $false }
    }
    $true
}

function Get-FpsEvidenceDiagnosis {
    <#
    .SYNOPSIS
    R3-MERGE-RESIDUE-1 (sol 07d50621 A11): the (dominantBottleneck, suggestedOptimization) pair New-PlaybackAbAnalysis (the CUDA playback A/B runner's analysis) gives for a recorded analysis.evidence
    object (a gated pair: neither host-load provisional nor arm refused). The thresholds and the order of the branches are the producer's own; a field that is absent or not a finite number
    reads as no regression, as the producer's Test-DeltaAtLeast reads a null delta.
    #>
    param([AllowNull()]$Evidence)
    $num = {
        param($name)
        $v = Get-FaProp $Evidence $name
        if ($null -ne $v -and ($v -is [double] -or $v -is [single] -or $v -is [decimal] -or $v -is [int] -or $v -is [long] -or $v -is [int16] -or $v -is [byte]) -and [double]::IsFinite([double]$v)) { [double]$v } else { $null }
    }
    $atLeast = { param($v, $t) $null -ne $v -and $v -ge $t }
    $fps = & $num 'presentedFpsDeltaPercent'
    $presentInterval = & $num 'presentIntervalDeltaPercent'; $renderWork = & $num 'renderWorkDeltaPercent'; $queueWait = & $num 'queueWaitDeltaPercent'
    $llrawproc = & $num 'llrawprocDeltaPercent'; $drawTotal = & $num 'drawTotalDeltaPercent'; $uiSignal = & $num 'presentUiSignalLatencyDeltaPercent'
    $drawPresent = & $num 'presentDrawPresentDeltaPercent'; $pacing = & $num 'presentPacingDeltaPercent'; $prepBeforeFinish = & $num 'prepBeforeFinishDeltaPercent'
    $candQueue = & $num 'candidateQueueWaitMs'; $candDraw = & $num 'candidateDrawTotalMs'; $candTexture = & $num 'candidateTextureTotalMs'
    if ($null -eq $fps) { return @('unknown', 'rerun_playback_ab_for_stage_metrics') }
    if ($fps -lt -1.0) {
        if ((& $atLeast $queueWait 25.0) -and ((& $atLeast $drawTotal 10.0) -or (& $atLeast $prepBeforeFinish 10.0) -or (& $atLeast $presentInterval 10.0))) { return @('present-bound', 'reduce_gpu_present_draw_queue_sync') }
        if ((& $atLeast $queueWait 25.0) -or (& $atLeast $uiSignal 25.0)) { return @('queue-wait-bound', 'reduce_render_queue_wait_or_present_signal_latency') }
        if (& $atLeast $pacing 25.0) { return @('frame-pacing-bound', 'stabilize_playback_frame_pacing') }
        if ((& $atLeast $drawTotal 15.0) -or (& $atLeast $drawPresent 15.0) -or (& $atLeast $prepBeforeFinish 15.0) -or (& $atLeast $presentInterval 15.0)) { return @('present-bound', 'reduce_gpu_present_jitter') }
        if ((& $atLeast $llrawproc 10.0) -or (& $atLeast $renderWork 10.0)) { return @('recon-bound', 'optimize_cuda_recon_kernel_or_state_setup') }
        return @('cadence-bound', 'profile_playback_cadence_with_stage_timing')
    }
    if ($fps -lt 10.0) { return @('mixed', 'profile_present_decode_and_recon_stages') }
    if ($null -ne $candQueue -and $candQueue -ge 15.0 -and ($null -eq $candTexture -or $candQueue -ge $candTexture)) { return @('queue-wait-bound', 'reduce_render_queue_wait_or_pipeline_gpu_tex_nr_present') }
    if ($null -ne $candDraw -and $candDraw -ge 15.0 -and ($null -eq $candTexture -or $candDraw -ge $candTexture)) { return @('present-bound', 'reduce_gpu_present_draw_queue_sync') }
    if ($null -ne $candTexture -and $candTexture -ge 10.0) { return @('gpu-texture-bound', 'optimize_direct_device_recon_amaze_texture_kernel') }
    @('none', 'validate_same_clip_on_second_machine')
}
# analysis.evidence field -> compare metric and the metric's field, as New-PlaybackAbAnalysis (run-release-cuda-playback-ab.ps1) records them
$script:FpsCachedAnalysisEvidence = @(
    , @('presentedFpsDeltaPercent', 'presentedFps', 'deltaPercent')
    , @('presentIntervalDeltaPercent', 'avgPresentIntervalMs', 'deltaPercent')
    , @('renderWorkDeltaPercent', 'avgRenderWorkMs', 'deltaPercent')
    , @('queueWaitDeltaPercent', 'avgQueueWaitMs', 'deltaPercent')
    , @('llrawprocDeltaPercent', 'avgLlrawprocMs', 'deltaPercent')
    , @('drawTotalDeltaPercent', 'avgDrawTotalMs', 'deltaPercent')
    , @('presentUiSignalLatencyDeltaPercent', 'avgPresentUiSignalLatencyMs', 'deltaPercent')
    , @('presentDrawPresentDeltaPercent', 'avgPresentDrawPresentMs', 'deltaPercent')
    , @('presentPacingDeltaPercent', 'avgPresentPacingMs', 'deltaPercent')
    , @('prepBeforeFinishDeltaPercent', 'avgPrepBeforeFinishMs', 'deltaPercent')
    , @('candidateQueueWaitMs', 'avgQueueWaitMs', 'candidate')
    , @('candidateDrawTotalMs', 'avgDrawTotalMs', 'candidate')
    , @('candidateTextureTotalMs', 'avgTextureTotalMs', 'candidate')
    , @('candidateTextureWallMs', 'avgTextureWallMs', 'candidate')
    , @('candidatePresentIntervalMs', 'avgPresentIntervalMs', 'candidate')
)

function Test-FpsProofReportSpeedQuotable {
    <#
    .SYNOPSIS
    ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A6, 8747fced A6): may a local CUDA proof report (summarize-local-cuda-proof.ps1 -Json) be quoted for playback SPEED? The packager and the
    importer both ask this one question. Only when its raw text repeats no key, its status is the string QUOTABLE_PASS, its claimAuthority is ONE object whose speed is exactly $true, and its
    playbackSpeed section is ONE object whose status is the string PASS. A REFUSED section, a singleton array, a repeated key or any other shape grants no speed authority.
    #>
    param([AllowNull()]$Report, [AllowNull()][string]$RawText)
    if ($null -eq $Report -or [string]::IsNullOrWhiteSpace($RawText)) { return $false }
    if ($null -ne (Get-FpsJsonRepeatedKeyProblem $RawText)) { return $false }
    if (-not (Test-FaTextEq (Get-FaProp $Report 'status') 'QUOTABLE_PASS')) { return $false }
    $authority = Get-FaProp $Report 'claimAuthority'
    if ($authority -isnot [System.Management.Automation.PSCustomObject]) { return $false }
    $speed = Get-FaProp $authority 'speed'
    if (-not ($speed -is [bool] -and $speed)) { return $false }
    $section = Get-FaProp $Report 'playbackSpeed'
    if ($section -isnot [System.Management.Automation.PSCustomObject]) { return $false }
    Test-FaTextEq (Get-FaProp $section 'status') 'PASS'
}

function Get-VolumeNameComparison {
    <#
    .SYNOPSIS
    R2-RESIDUE-1 r3 (sol 5add23cf A8): how the volume holding an EXISTING file compares file names, asked of the volume itself and not guessed from the path separator (an NTFS directory can be case
    sensitive, and a slash-separated path can sit on a case-insensitive volume). The file is looked up again with the case of its name flipped: found = names compare ignoring case, not found = ordinal.
    A name with no letters to flip cannot tell, so it falls back on the platform default (OrdinalIgnoreCase where the separator is a backslash).
    #>
    param([Parameter(Mandatory)][string]$ExistingFile)
    $platformDefault = if ([IO.Path]::DirectorySeparatorChar -eq [char]92) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    $leaf = [IO.Path]::GetFileName($ExistingFile)
    $directory = [IO.Path]::GetDirectoryName($ExistingFile)
    if ([string]::IsNullOrEmpty($leaf) -or [string]::IsNullOrEmpty($directory) -or -not (Test-Path -LiteralPath $ExistingFile -PathType Leaf)) { return $platformDefault }
    $flipped = -join @($leaf.ToCharArray() | ForEach-Object { if ([char]::IsUpper($_)) { [char]::ToLowerInvariant($_) } elseif ([char]::IsLower($_)) { [char]::ToUpperInvariant($_) } else { $_ } })
    if ([string]::Equals($flipped, $leaf, [StringComparison]::Ordinal)) { return $platformDefault }
    if (Test-Path -LiteralPath (Join-Path $directory $flipped) -PathType Leaf) { return [StringComparison]::OrdinalIgnoreCase }
    [StringComparison]::Ordinal
}

function Get-FpsJsonRepeatedKeyProblem {
    <#
    .SYNOPSIS
    r14 (sol r13 B4, adversary r14 A2): ConvertFrom-Json keeps only the LAST value of a key an object repeats, so a recorded opt-out written "0" then "1" (or a plan purpose written twice) reached
    the gate as one clean value and ConvertTo-FpsEnvMap's repeat check never saw it. This reads the RAW text with System.Text.Json (whose object enumeration keeps every repeat) and returns
    "<path> repeats the key '<name>'" for the first object that names a key twice (names compared ignoring case, as the Windows environment and ConvertTo-FpsEnvMap do), else $null. Text
    the strict reader cannot parse is a problem too (fail closed): it cannot be shown free of repeats.
    #>
    param([AllowNull()][string]$Text, [switch]$AllowArrayRoot, [switch]$ParseLevelOnly)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $options = [System.Text.Json.JsonDocumentOptions]::new()
    $options.AllowTrailingCommas = $true
    $options.CommentHandling = [System.Text.Json.JsonCommentHandling]::Skip
    # ENVELOPE-PREEMPT-RESIDUE-1 r1 (sol r16 + key3 fable r16): -ParseLevelOnly is the check a reader runs BEFORE its own positional shape checks. It keeps only the parse-level rules (a
    # repeated key; text the strict reader cannot parse at all) and drops the shape rules (root kind, envelope arrays, the depth-256 cap), which that reader applies AFTER its own checks by
    # calling again without the switch. Its depth bound is above ConvertFrom-Json's own (-Depth 1024), so the depth refusal is the parser's, never this check's.
    $options.MaxDepth = $(if ($ParseLevelOnly) { 2048 } else { 256 })
    $doc = $null
    try { $doc = [System.Text.Json.JsonDocument]::Parse($Text, $options) } catch { return "the JSON cannot be read strictly enough to rule out a repeated key ($($_.Exception.Message))" }
    try {
        # r15 (adversary r14 pass 18 A1): ConvertFrom-Json unwraps a one-element array root, and PowerShell member enumeration projects `.launch`, `.proof`, `.playbackAb`, `.compare` ...
        # straight through an array, so [ { ...valid LIGHT record... } ] (or "proof": [ { ... } ]) reached the gate as the record inside it. Every judged record, smoke result, summary
        # and plan is a JSON OBJECT, and every envelope the readers walk by member access is an object by contract: a root that is not an object, or an envelope key holding an
        # array, is a shape problem here (this is the one raw check every judged reader already calls before it parses), never a record the parser silently unwraps.
        # A1-ITEM-SHAPE-RESIDUE-1 r1: -AllowArrayRoot is for a reader whose document legitimately IS a list and which shape-checks every entry itself (the venue lease batch's leg plan: a bare array of
        # items is a valid plan, and a null or non-object entry must reach that reader's own BAD_ITEM_SHAPE item=<n>, not a generic unreadable refusal here). Repeated keys and the envelope-array
        # rule below still apply to the whole text, and a scalar root is still refused.
        $rootIsAllowedList = $AllowArrayRoot -and $doc.RootElement.ValueKind -eq [System.Text.Json.JsonValueKind]::Array
        if (-not $ParseLevelOnly -and -not $rootIsAllowedList -and $doc.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) { return "`$ is a JSON $($doc.RootElement.ValueKind), not an object (a judged record is one JSON object; an array root is unwrapped by the parser)" }
        $pending = [System.Collections.Generic.Stack[object]]::new()
        $pending.Push(@('$', $doc.RootElement))
        while ($pending.Count -gt 0) {
            $item = $pending.Pop()
            $path = [string]$item[0]
            $element = $item[1]
            if ($element.ValueKind -eq [System.Text.Json.JsonValueKind]::Object) {
                $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
                foreach ($property in $element.EnumerateObject()) {
                    if (-not $seen.Add($property.Name)) { return "$path repeats the key '$($property.Name)'" }
                    if (-not $ParseLevelOnly -and $property.Value.ValueKind -eq [System.Text.Json.JsonValueKind]::Array -and $script:FpsJsonObjectEnvelopes.Contains($property.Name)) {
                        return "$path.$($property.Name) is an array, but '$($property.Name)' is an object envelope the readers walk by member access (member enumeration would project through it)"
                    }
                    $kind = $property.Value.ValueKind
                    if ($kind -eq [System.Text.Json.JsonValueKind]::Object -or $kind -eq [System.Text.Json.JsonValueKind]::Array) { $pending.Push(@("$path.$($property.Name)", $property.Value)) }
                }
            } elseif ($element.ValueKind -eq [System.Text.Json.JsonValueKind]::Array) {
                $index = 0
                foreach ($value in $element.EnumerateArray()) {
                    $kind = $value.ValueKind
                    if ($kind -eq [System.Text.Json.JsonValueKind]::Object -or $kind -eq [System.Text.Json.JsonValueKind]::Array) { $pending.Push(@("$path[$index]", $value)) }
                    $index++
                }
            }
        }
    } finally { $doc.Dispose() }
    $null
}

function Assert-FpsJsonNoRepeatedKey {
    # Throws "FPS_JSON_REPEATED_KEY: <problem> (<label>)" when Get-FpsJsonRepeatedKeyProblem finds one; every reader that parses a record, a smoke result or a plan the arm gate judges calls this on
    # the raw text first, so its own error path (a typed refusal, an UNKNOWN arm or a failed read) handles the repeat instead of a parser picking one value.
    param([AllowNull()][string]$Text, [string]$Label = 'JSON', [switch]$AllowArrayRoot, [switch]$ParseLevelOnly)
    $problem = Get-FpsJsonRepeatedKeyProblem $Text -AllowArrayRoot:$AllowArrayRoot -ParseLevelOnly:$ParseLevelOnly
    if ($null -ne $problem) { throw "FPS_JSON_REPEATED_KEY: $problem ($Label)" }
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

Export-ModuleMember -Function Get-VolumeNameComparison, Get-FpsCompareBindingProblem, Get-FpsObserverControls, Get-FpsLightShape, Get-FpsArmFromLegRecord, Get-FaProp, New-FpsArm, ConvertTo-FpsArmName, Get-FpsArmFromLegSpec, Get-FpsArmFromJobResult, Get-FpsArm, ConvertTo-FpsEnvMap, Get-FpsArmFromEnvironment, Get-FpsArmFromLaunch, Get-FpsArmFromSmokeRecord, Get-FpsJsonRepeatedKeyProblem, Assert-FpsJsonNoRepeatedKey, Test-FpsCachedAnalysisBound, Test-FpsProofReportSpeedQuotable, Get-FpsPairVerdict, Get-FpsArmsVerdict, Get-FpsLegPlanRefusal
