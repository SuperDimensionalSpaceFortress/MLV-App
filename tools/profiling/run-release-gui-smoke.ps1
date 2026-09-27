param(
    [string]$RepoRoot = ".",
    [string]$ExePath = "",
    [Alias("Input")]
    [string]$ClipPath = "",
    [string]$Output = "",
    [double]$Seconds = 24,   # settled-playback window (s) AFTER settle. RULE 2026-06-26 (Layi): give MLV a
                           # generous time to actually play before closing -- ~3x the old 8s, toward the
                           # recommended 30s settled-validation window. Both lanes use a generous window.
    [ValidateRange(0, 3600000)]
    [int]$ProcessTimeoutMs = 0,
    [ValidateRange(0, 1000000)]
    [int]$TargetPresentedFrames = 0,
    [int]$StartFrame = 0,
    [ValidateSet("persisted", "on", "off")]
    [string]$DropFrameMode = "persisted",
    [int]$SettleMs = 2500,
    [switch]$NoLoop,          # default: pass --loop so a short clip plays continuously for the whole
                              # -Seconds window. Set this to play once then stop (e.g. a frame-matched A/B).
    [double]$SettleCpuPercent = 10,
    [int]$SettleCpuStableMs = 1000,
    [int]$SettleCpuMaxMs = 45000,
    [double]$SystemSettleCpuPercent = -1,
    [int]$SystemSettleCpuStableMs = 2000,
    [int]$SystemSettleCpuMaxMs = 60000,
    [string]$Threads = "auto",
    [string]$QualityMode = "auto",
    [string]$ScaleFactor = "4",
    [int]$ExpectedScaleRequest = 4,
    [int]$ExpectedVisualScaleRequest = -2,
    [int]$ExpectedQualityMode = 2,
    [string]$ExpectedStretchX = "",
    [string]$ExpectedStretchY = "",
    [int]$ExpectedHStretchIndex = -1,
    [int]$ExpectedVStretchIndex = -1,
    [string]$ExpectedAspectMode = "",
    [double]$ExpectedStretchTolerance = 0.0001,
    [switch]$UsePersistedPlaybackSettings,
    [switch]$PreferHqMean23,
    [switch]$FrameTelemetry,
    [switch]$RbfDetailTiming,
    [string]$GpuPlaybackReconBackend = "",
    [switch]$CaptureScreenshot,
    [switch]$RequireFreshScreenshotRender,
    [switch]$FailOnColorArtifact,
    [switch]$SkipWindowScreenshot,
    [string]$ScreenshotOutputDir = "",
    [int]$ScreenshotDelayMs = 2000,
    [int]$ScreenshotWindowWaitMs = 10000,
    [int]$ScreenshotCaptureTimeoutMs = 2500,
    [switch]$PreserveExperimentalEnvironment,
    [string[]]$ExtraEnvironment = @(),
    [string]$Receipt = "",
    [string]$Scope = "",
    [ValidateSet("", "auto", "receipt", "none", "simple", "bilinear", "lmmse", "igv", "amaze", "ahd", "rcd", "dcb", "amaze-cached")]
    [string]$PlaybackDebayer = "",
    [ValidateSet("", "auto", "receipt", "subset")]
    [string]$PlaybackProcessing = "",
    [ValidateSet("", "auto", "cpu", "gpu")]
    [string]$GpuPreviewProcessing = "",
    [ValidateSet("", "auto", "cpu", "gpu")]
    [string]$GpuBilinearDebayer = "",
    [ValidateSet("", "auto", "cpu", "gpu")]
    [string]$GpuAmazeDebayer = "",
    [switch]$GpuAmazeTexturePresent,
    [switch]$DisableLookAssist,
    [switch]$ExerciseClipLifecycleStress,
    [string]$StressSwitchInput = "",
    [int]$StressSwitchAtMs = 1000,
    [int]$StressSeekFrame = 8,
    [switch]$EnablePhase3QualityModes,
    [string]$StageLog = "",
    [ValidateSet("", "on", "off")]
    [string]$Zebras = "",
    [bool]$RequireLookAssist = $true,
    [bool]$RequireCpuSettled = $true,
    [string[]]$AdditionalArgs = @(),
    [switch]$DetectPlaybackArtifacts,
    [switch]$ArtifactCadenceAdvisory,
    [switch]$LegacyGuiSmokeOptions,
    [switch]$AllowZeroPresentedFrames,
    [switch]$LaunchOnlyProbe,
    [ValidateRange(0.0, 1.0)]
    [double]$MaxSkippedOrUnpresentedRatio = 0.5,
    [double]$HostLoadCpuPercentBar = 75,
    [int]$HostLoadTopProcessCount = 8,
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 3: cadence for interior host-load sampling while the
    # leg runs, not just before/after it. 4000ms on a 24-30s leg (per the round-2 evidence) yields
    # ~6-7 interior samples -- enough to catch an agent-job-length burst without materially adding
    # to the leg's own cost (see the "cost" note on $hostLoadDuringSamples below). 0 disables
    # interior sampling and reproduces round 2's bracket-only behaviour exactly.
    [ValidateRange(0, 60000)]
    [int]$HostLoadSampleIntervalMs = 4000,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$settledValidationRecommendedSeconds = 30
$validationWarnings = @()
. (Join-Path $PSScriptRoot 'gui-smoke-screenshot-provenance.ps1')
. (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')
. (Join-Path $PSScriptRoot 'gui-smoke-gpu-texture-route-validation.ps1')
Import-Module (Join-Path $PSScriptRoot 'gui-smoke-process-boundary.psm1') -Force
. (Join-Path $PSScriptRoot 'provenance-stamp.ps1')

if ($RequireFreshScreenshotRender -and -not $CaptureScreenshot) {
    throw "-RequireFreshScreenshotRender requires -CaptureScreenshot."
}
if ($FailOnColorArtifact -and -not $CaptureScreenshot) {
    # Without -CaptureScreenshot, $colorArtifactScan is never populated, so the
    # color-artifact failure gate below is unreachable and -FailOnColorArtifact would
    # silently do nothing -- fail loud at parameter validation instead of no-op.
    throw "-FailOnColorArtifact requires -CaptureScreenshot."
}
if ($RequireFreshScreenshotRender) {
    # V2 provenance binds the screenshot to playback frame/session/index and
    # request serial telemetry. Make the dependency intrinsic to the policy.
    $FrameTelemetry = $true
}

if ($DisableLookAssist) {
    if ($PSBoundParameters.ContainsKey("RequireLookAssist") -and $RequireLookAssist) {
        throw "-DisableLookAssist cannot be combined with -RequireLookAssist:`$true."
    }
    $RequireLookAssist = $false
}

# A zero-present result proves only that the process launched. It is never a
# playback, cadence, lifecycle-stress, screenshot, or A/B-quality result.
if ($AllowZeroPresentedFrames -and -not $LaunchOnlyProbe) {
    throw "-AllowZeroPresentedFrames requires -LaunchOnlyProbe; zero-present exemptions are forbidden for playback-quality evidence."
}
if ($LaunchOnlyProbe -and -not $AllowZeroPresentedFrames) {
    throw "-LaunchOnlyProbe requires -AllowZeroPresentedFrames."
}
if ($LaunchOnlyProbe -and ($FrameTelemetry -or $CaptureScreenshot -or
                           $DetectPlaybackArtifacts -or $ExerciseClipLifecycleStress)) {
    throw "-LaunchOnlyProbe cannot be combined with frame telemetry, screenshots, artifact detection, or lifecycle stress."
}
if ($ExerciseClipLifecycleStress -and $DetectPlaybackArtifacts) {
    throw "-ExerciseClipLifecycleStress cannot be combined with -DetectPlaybackArtifacts; switch/close/reopen is an intentional playback discontinuity. Run lifecycle correctness and uninterrupted cadence as separate legs."
}
if ($ArtifactCadenceAdvisory -and -not $DetectPlaybackArtifacts) {
    throw "-ArtifactCadenceAdvisory requires -DetectPlaybackArtifacts."
}
if ($LegacyGuiSmokeOptions) {
    if (-not $NoLoop) {
        throw "-LegacyGuiSmokeOptions requires -NoLoop because the pinned legacy CLI has no --loop option."
    }
    if ($DropFrameMode -ne "persisted") {
        throw "-LegacyGuiSmokeOptions supports only the legacy persisted drop-frame default."
    }
    if ($DisableLookAssist -and [string]::IsNullOrWhiteSpace($Receipt)) {
        throw "-LegacyGuiSmokeOptions requires a receipt that explicitly disables Look Assist when -DisableLookAssist is requested."
    }
}

if ($UsePersistedPlaybackSettings) {
    if (-not $PSBoundParameters.ContainsKey("QualityMode")) {
        $QualityMode = ""
    }
    if (-not $PSBoundParameters.ContainsKey("ScaleFactor")) {
        $ScaleFactor = ""
    }
    if (-not $PSBoundParameters.ContainsKey("ExpectedQualityMode")) {
        $ExpectedQualityMode = -1
    }
    if (-not $PSBoundParameters.ContainsKey("ExpectedScaleRequest")) {
        $ExpectedScaleRequest = -1
    }
}

# Opt-in temporal-artifact gate: turn on the interactive trace so the post-run detector can measure
# flicker / stalls / cadence jitter from per-present events. Auto-discounts the screenshot grab.
if (($DetectPlaybackArtifacts -or $RequireFreshScreenshotRender) -and
    -not ($ExtraEnvironment | Where-Object { $_ -match '^MLVAPP_INTERACTIVE_TRACE=' })) {
    $ExtraEnvironment += 'MLVAPP_INTERACTIVE_TRACE=1'
}

if (($CaptureScreenshot -or $FrameTelemetry) -and $Seconds -lt $settledValidationRecommendedSeconds) {
    $validationWarnings += (
        "Requested playback window of ${Seconds}s is shorter than the recommended " +
        "${settledValidationRecommendedSeconds}s settled-validation window."
    )
    Write-Warning (
        "GUI smoke requested ${Seconds}s playback, which is shorter than the recommended " +
        "${settledValidationRecommendedSeconds}s settled-validation window for screenshot-backed checks."
    )
}

function Convert-PlaybackLogLineToObject {
    param([string]$Line)

    $result = [ordered]@{}
    $matches = [regex]::Matches($Line, '(?<key>[A-Za-z0-9_]+)=(?<value>"[^"]*"|\S+)')
    foreach ($match in $matches) {
        $key = $match.Groups["key"].Value
        $rawValue = $match.Groups["value"].Value.Trim('"')

        $intValue = 0L
        $doubleValue = 0.0
        if ([long]::TryParse($rawValue, [ref]$intValue)) {
            $result[$key] = $intValue
        }
        elseif ([double]::TryParse(
            $rawValue,
            [System.Globalization.NumberStyles]::Float,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [ref]$doubleValue)) {
            $result[$key] = $doubleValue
        }
        else {
            $result[$key] = $rawValue
        }
    }
    [pscustomobject]$result
}

function Get-LogTimestampUtc {
    param([string]$Line)

    if ($Line -notmatch '^\[(?<ts>[^\]]+)\]') {
        return $null
    }

    [datetime]::Parse(
        $Matches["ts"],
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::AssumeUniversal -bor
            [System.Globalization.DateTimeStyles]::AdjustToUniversal)
}

function Get-SystemCpuPercent {
    $sample = Get-CimInstance `
        -ClassName Win32_PerfFormattedData_PerfOS_Processor `
        -Filter "Name='_Total'" `
        -ErrorAction Stop
    [double]$sample.PercentProcessorTime
}

function Wait-SystemCpuSettle {
    param(
        [double]$ThresholdPercent,
        [int]$StableMs,
        [int]$MaxMs
    )

    $requested = $ThresholdPercent -ge 0 -and $StableMs -gt 0 -and $MaxMs -gt 0
    $result = [ordered]@{
        requested = $requested
        settled = -not $requested
        thresholdPercent = $ThresholdPercent
        stableMs = 0
        requiredStableMs = $StableMs
        elapsedMs = 0
        maxMs = $MaxMs
        lastPercent = $null
        failure = $null
    }
    if (-not $requested) {
        return [pscustomobject]$result
    }

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $lastSampleMs = 0
    while ($watch.ElapsedMilliseconds -lt $MaxMs -and $result.stableMs -lt $StableMs) {
        Start-Sleep -Milliseconds 500
        try {
            $cpuPercent = Get-SystemCpuPercent
        }
        catch {
            # round 8 (fable minor 2): raw exception TEXT can embed a host/namespace identifier
            # (the same CIM-connection-failure class Get-HostLoadSnapshot already sanitizes below);
            # this result flows into the receipt via preLaunchSystemCpuSettle, so record the
            # exception's TYPE only, matching every other sanitized catch in this file.
            $result.failure = $_.Exception.GetType().Name
            break
        }

        $nowMs = [int]$watch.ElapsedMilliseconds
        $deltaMs = [Math]::Max(1, $nowMs - $lastSampleMs)
        $lastSampleMs = $nowMs
        $result.lastPercent = $cpuPercent

        if ($cpuPercent -le $ThresholdPercent) {
            $result.stableMs += $deltaMs
        }
        else {
            $result.stableMs = 0
        }
    }

    $result.elapsedMs = [int]$watch.ElapsedMilliseconds
    $result.settled = $result.stableMs -ge $StableMs
    [pscustomobject]$result
}

function Get-HostLoadSystemTimes {
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 7 (sol + astra BLOCKER -- replace per-process
    # accounting BY CONSTRUCTION): rounds 4-6 built non-subject load by enumerating every process
    # on the host and summing (or diffing) their individually-read TotalProcessorTime values. Each
    # round found a new way for that per-process mechanism to undercount without recording that it
    # had: a throwing getter stored as zero, a loader present in only one snapshot contributing
    # nothing, PID reuse matched without creation identity. This function retires that whole
    # mechanism: ONE Win32 syscall (GetSystemTimes) returns the host's cumulative idle/kernel/user
    # time directly from the kernel -- there is no process list to enumerate, so none of those
    # failure modes can exist here, by construction, not by another patch. Kernel time on Windows
    # INCLUDES idle time, so system-wide busy time is (kernel - idle) + user; see
    # Get-HostLoadNonSubjectCpuLoadPercent below for the subtraction that turns this into a
    # non-subject figure.
    #
    # round 9 (sol BLOCKER item 1): -SubjectProcess/-SubjectNotYetStarted read the subject's own
    # TotalProcessorTime HERE, immediately after the GetSystemTimes syscall, with nothing but a
    # null-check assignment between the two reads -- not by a separate caller-side read one or more
    # statements later (as Get-HostLoadSnapshot did through round 8). Both numbers now describe the
    # SAME instant by construction: there is no window left in which the subject can keep burning
    # CPU that then gets attributed, via subtraction, to a system-wide interval it was never
    # inside. sol's round-8 repro depended on exactly that gap (a delayed subject read pulling in
    # 0.88 CPU-seconds accrued after the system endpoint, turning a true 96% into a reported 74%
    # quiet); folding the read in here removes the gap rather than bounding it.
    #
    # Self-contained (the Add-Type guard lives inside this function, not at file scope) so the test
    # harness's verbatim-splice extraction (_extract_functions) can pull this function alone and it
    # still works standalone -- same precedent as capture-window-screenshot.ps1's NativeMethods
    # guard. Returns $null (never throws) on any failure: non-Windows platform, Add-Type failure,
    # or GetSystemTimes itself returning FALSE -- callers must treat $null as "could not collect",
    # the same fail-toward-unknown contract every other collection point in this file already uses.
    param(
        [AllowNull()][System.Diagnostics.Process]$SubjectProcess = $null,
        # round 7: the BEFORE snapshot is captured before the subject process is even started --
        # its cumulative CPU time is not "unreadable", it is DEFINITIONALLY zero. See
        # Get-HostLoadSnapshot's own parameter comment for the full rationale; carried here
        # verbatim now that the read itself lives in this function.
        [switch]$SubjectNotYetStarted
    )
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        return $null
    }
    try {
        if (-not ("HostLoadNative.NativeMethods" -as [type])) {
            # A literal (single-quoted) here-string: this is plain C# source text with no
            # PowerShell variable interpolation needed, and it must not accidentally get any.
            # NOTE for future edits: keep every closing brace off a line by itself (no line that
            # is JUST "}") -- the test harness's function-extraction regex looks for the literal
            # sequence newline-close-brace-newline to find where a PowerShell FUNCTION ends, and a
            # bare "}" line inside this embedded C# block (e.g. a namespace's own closing brace on
            # its own line) matches that same pattern and truncates the splice early. Kept on one
            # line together instead.
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace HostLoadNative {
    public static class NativeMethods {
        // FILETIME is two little-endian DWORDs (low then high); that is bit-identical to a
        // 64-bit integer's memory layout on a little-endian machine, so marshaling straight to
        // "long" reproduces the exact 100-nanosecond-tick value with no manual high/low combining.
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetSystemTimes(out long idleTime, out long kernelTime, out long userTime);
    } }
'@ -ErrorAction Stop
        }
    } catch {
        return $null
    }
    $idleTicks = [int64]0
    $kernelTicks = [int64]0
    $userTicks = [int64]0
    # round 8 (sol BLOCKER = fable minor 1 -- window misalignment): stamped immediately adjacent to
    # the syscall itself, not by the caller before its own CIM/Get-Process evidence work -- see
    # Get-HostLoadNonSubjectCpuLoadPercent, which now computes elapsed time from THIS timestamp on
    # both sides of an interval, never from Get-HostLoadSnapshot's top-of-function capturedAtUtc.
    $syscallAtUtc = [datetime]::UtcNow
    try {
        $ok = [HostLoadNative.NativeMethods]::GetSystemTimes([ref]$idleTicks, [ref]$kernelTicks, [ref]$userTicks)
    } catch {
        return $null
    }
    if (-not $ok) {
        return $null
    }
    # round 9 (sol BLOCKER item 1): read immediately -- the only statements between the syscall
    # above and this read are the two null checks above it, none of which can block or sleep.
    $subjectCpuSeconds = $null
    if ($SubjectNotYetStarted) {
        $subjectCpuSeconds = 0.0
    }
    elseif ($null -ne $SubjectProcess) {
        try {
            # Read straight off the caller's own held handle -- no process lookup, so PID reuse
            # and "the process already exited" cannot make this read the WRONG process's CPU
            # time. See Get-HostLoadSnapshot's former copy of this comment (round 4/7) for the
            # full rationale; unchanged, only relocated.
            $SubjectProcess.Refresh()
            $subjectTotalProcessorTime = $SubjectProcess.TotalProcessorTime
            # round 7: a disposed/invalid Process handle's TotalProcessorTime getter does NOT
            # always throw -- PowerShell's member-access can suppress the .NET property getter's
            # exception and simply yield $null instead. An explicit null check -- not exception
            # handling alone -- is what closes it; casting $null straight to [double] would
            # silently produce 0.0.
            if ($null -ne $subjectTotalProcessorTime) {
                $subjectCpuSeconds = [double]$subjectTotalProcessorTime.TotalSeconds
            }
        } catch {
            # A genuinely thrown exception is handled identically: subjectCpuSeconds stays at its
            # $null default (unreadable, the third state -- never guess zero).
        }
    }
    [pscustomobject]@{
        idleSeconds = $idleTicks / 1e7
        kernelSeconds = $kernelTicks / 1e7
        userSeconds = $userTicks / 1e7
        capturedAtUtc = $syscallAtUtc
        subjectCpuSeconds = $subjectCpuSeconds
    }
}

function Get-HostLoadSnapshot {
    # Records host state around a playback leg. BACHELOR is also the owner's interactive
    # workstation: an fps number measured while it is loaded is not a property of the build
    # (measured 2026-09-22 -- CPU_LOAD_PCT=96 turned a 4.8fps CPU reference into 1.2fps with
    # identical route counters). Top consumers are recorded BY NAME ONLY -- no command lines,
    # no paths -- this is host-load evidence, not process forensics.
    # round 4 (sol minor): collection is bounded with -OperationTimeoutSec so a slow/hung CIM
    # query cannot extend the measured leg without limit -- interior samples run inline in the
    # process-wait loop (see the main body's sampling loop), so an unbounded query here would
    # perturb the very thing this gate exists to keep clean.
    # round 4 (sol minor, fable minor -- privacy): $_.Exception.Message can embed a path, server
    # name, namespace, or other host/environment identifier (e.g. a WMI/CIM connection failure
    # names the machine and namespace it tried to reach). Record only the exception's TYPE, never
    # its message text -- kept inline (not a helper function) so this function stays
    # self-contained for the test harness's verbatim-splice extraction.
    # round 4 (fable minor -- subject exclusion): -SubjectProcess optionally identifies the
    # process actually being measured (MLVApp itself). When given, its cumulative CPU time
    # (TotalProcessorTime, seconds) is recorded as subjectCpuSeconds so a caller can later derive
    # how much of the host's system-wide busy time was the subject's OWN legitimate decode work
    # rather than exogenous host load (see Get-HostLoadNonSubjectCpuLoadPercent below) -- a
    # CPU-route decode leg can legitimately push a quiet host's total load past the bar, and that
    # is not the same thing as the host being loaded by something else.
    #
    # round 7 (both keys BLOCKER): cpuLoadPercent (Win32_Processor.LoadPercentage via CIM) and
    # topCpuConsumers (Get-Process, sorted) are kept for the receipt's own sake -- they are useful
    # evidence for a human reading a receipt -- but NEITHER feeds the quiet/exceeded decision
    # anymore; that decision is systemIdleSeconds/systemKernelSeconds/systemUserSeconds and
    # subjectCpuSeconds, both now read together by Get-HostLoadSystemTimes (one syscall plus one
    # subject-handle read, back to back) only. -SubjectProcess/-SubjectNotYetStarted are passed
    # straight through to that function -- see its own comment for the full rationale (why the
    # caller's held handle, never a fresh Get-Process -Id lookup; why "not yet started" is a
    # legitimate zero, distinct from "unreadable"). topCpuConsumers' own Get-Process call is
    # wrapped in its OWN try/catch: a failure gathering that purely-evidentiary list must not block
    # collection of the data the gate actually depends on.
    #
    # round 9 (sol BLOCKER item 1 + fable minor -- evidence stretches the final interval): the
    # syscall + subject-CPU reading (via Get-HostLoadSystemTimes) now happens FIRST, before any
    # CIM/Get-Process evidence collection, for EVERY snapshot -- not just during-leg ones. Through
    # round 8, the evidence block ran first and the syscall ran after it, so up to
    # ~2*OperationTimeoutSec of CIM latency plus an unbounded Get-Process could land between the
    # leg boundary and the syscall that is supposed to mark it: the BEFORE snapshot's syscall
    # landed late (shrinking the first interval) and the AFTER snapshot's syscall landed late
    # (stretching the final interval past leg end, diluting a leg-tail burst -- fable's round-8
    # residual). capturedAtUtc -- the general timestamp the main body's spacing guard
    # (hostLoadObservedMaxSampleGapMs) measures gaps from -- is now the syscall's own instant when
    # the syscall succeeds, not a separate top-of-function stamp taken before that evidence work,
    # so the guard can no longer be blind to the stretch either.
    param(
        [int]$TopProcessCount = 8,
        [int]$OperationTimeoutSec = 2,
        [AllowNull()][System.Diagnostics.Process]$SubjectProcess = $null,
        # round 7: the BEFORE snapshot is captured before the subject process is even started --
        # its cumulative CPU time is not "unreadable", it is DEFINITIONALLY zero (a process that
        # does not exist yet has accumulated no CPU time). This is a distinct, legitimate input
        # from "-SubjectProcess omitted because this caller isn't tracking a subject at all" (which
        # leaves subjectCpuSeconds at its $null/untracked default) -- conflating the two would
        # either wrongly force the process-start interval to read unknown forever, or wrongly
        # attribute a genuine read failure elsewhere to "zero used".
        [switch]$SubjectNotYetStarted,
        # round 8 (sol MAJOR item 2 -- evidence-only enumeration runs inside every measured leg,
        # unbounded): topCpuConsumers/processCount (Get-Process, unbounded) and cpuLoadPercent/
        # memory (CIM, bounded but still a round-trip) are receipt-only evidence -- neither feeds
        # the quiet/exceeded decision (see Get-HostLoadNonSubjectCpuLoadPercent). An interior
        # during-leg sample needs none of it, so when this switch is set the ENTIRE evidence block
        # below is skipped and a sample is exactly the syscall plus the subject-handle read: two
        # direct reads, no CIM round-trip, no process-table walk, nothing left inside a during-leg
        # sample that can hang or perturb the leg it is measuring. Before/after brackets are called
        # without this switch and keep full evidence for the receipt.
        [switch]$SkipEvidenceCollection
    )

    # round 9: this is now only a FALLBACK top-of-function stamp -- overwritten below with the
    # syscall's own instant whenever Get-HostLoadSystemTimes succeeds (the common Windows case).
    # It stays as the default so a host where the syscall is unavailable (non-Windows, or the
    # Add-Type/syscall failure path) still records a timestamp, matching the pre-round-9 contract.
    $fallbackCapturedAtUtc = [datetime]::UtcNow
    $snapshot = [ordered]@{
        capturedAtUtc = $fallbackCapturedAtUtc.ToString("o")
        collected = $false
        error = $null
        cpuLoadPercent = $null
        processCount = $null
        freePhysicalMemoryMb = $null
        totalVisibleMemoryMb = $null
        topCpuConsumers = @()
        subjectCpuSeconds = $null
        systemTimesCollected = $false
        systemTimesCapturedAtUtc = $null
        systemIdleSeconds = $null
        systemKernelSeconds = $null
        systemUserSeconds = $null
    }
    try {
        # hub (sol r9 contract BLOCKER, PARK/SPLIT ruling 2026-09-25): WHICH end of the evidence work the
        # syscall sits on depends on which leg boundary this snapshot marks. The AFTER bracket (and every
        # during-leg sample) marks the END of an interval that has already happened, so its syscall runs
        # FIRST (round 9) and evidence latency can no longer stretch the final interval past leg end. The
        # BEFORE bracket (-SubjectNotYetStarted; its only production caller runs Process.Start right after
        # it returns) marks the START of the first interval, so its syscall must run LAST, immediately
        # before the subject starts -- otherwise the evidence latency is pre-leg time folded into the
        # first interval, diluting an early-leg load burst toward quiet. subjectCpuSeconds is a constant
        # 0.0 on that path, so moving its syscall later cannot inflate subject CPU (round 8's blocker).
        $captureSystemTimes = {
            # round 9: syscall + subject-CPU reading FIRST, before any evidence collection -- see the
            # function-level comment above for why the ordering itself is the fix.
            $systemTimes = Get-HostLoadSystemTimes -SubjectProcess $SubjectProcess -SubjectNotYetStarted:$SubjectNotYetStarted
            if ($null -ne $systemTimes) {
                $snapshot.capturedAtUtc = $systemTimes.capturedAtUtc.ToString("o")
                $snapshot.systemIdleSeconds = [double]$systemTimes.idleSeconds
                $snapshot.systemKernelSeconds = [double]$systemTimes.kernelSeconds
                $snapshot.systemUserSeconds = [double]$systemTimes.userSeconds
                $snapshot.systemTimesCapturedAtUtc = $systemTimes.capturedAtUtc.ToString("o")
                $snapshot.systemTimesCollected = $true
                $snapshot.subjectCpuSeconds = $systemTimes.subjectCpuSeconds
            }
            else {
                # round 9: GetSystemTimes itself was unavailable here (non-Windows, or the native
                # syscall/Add-Type failed) -- the window-alignment fix this round exists for is moot on
                # this path (the interval is unknown regardless; see
                # Get-HostLoadNonSubjectCpuLoadPercent's systemTimesCollected gate), but the subject-CPU
                # field's own contract (SubjectNotYetStarted -> a genuine 0.0, an unreadable handle ->
                # $null, never guessed) must still hold independent of platform. This duplicates
                # Get-HostLoadSystemTimes' own subject-read block exactly, for this fallback path only.
                if ($SubjectNotYetStarted) {
                    $snapshot.subjectCpuSeconds = 0.0
                }
                elseif ($null -ne $SubjectProcess) {
                    try {
                        $SubjectProcess.Refresh()
                        $subjectTotalProcessorTime = $SubjectProcess.TotalProcessorTime
                        if ($null -ne $subjectTotalProcessorTime) {
                            $snapshot.subjectCpuSeconds = [double]$subjectTotalProcessorTime.TotalSeconds
                        }
                    } catch {
                        # stays $null -- unreadable, never guessed zero.
                    }
                }
            }
        }
        $collectEvidence = {
                $cpuLoad = (Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop `
                    -OperationTimeoutSec $OperationTimeoutSec |
                    Measure-Object -Property LoadPercentage -Average).Average
                $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop `
                    -OperationTimeoutSec $OperationTimeoutSec
                if ($null -eq $cpuLoad) {
                    $snapshot.error = "LoadPercentage unavailable from Win32_Processor."
                }
                else {
                    $snapshot.cpuLoadPercent = [double]$cpuLoad
                }
                $snapshot.freePhysicalMemoryMb = [Math]::Round($os.FreePhysicalMemory / 1024.0, 1)
                $snapshot.totalVisibleMemoryMb = [Math]::Round($os.TotalVisibleMemorySize / 1024.0, 1)

                try {
                    $processes = Get-Process -ErrorAction Stop
                    $snapshot.processCount = [int]$processes.Count
                    $snapshot.topCpuConsumers = @(
                        $processes | Sort-Object CPU -Descending | Select-Object -First $TopProcessCount |
                            ForEach-Object { $_.ProcessName }
                    )
                } catch {
                    # topCpuConsumers/processCount stay at their evidence-only defaults (empty/$null) --
                    # this must never block collection of the load-relevant data below.
                }
        }
        if ($SubjectNotYetStarted -and -not $SkipEvidenceCollection) {
            # An evidence failure (always, on non-Windows pwsh, where Get-CimInstance does not exist)
            # must not skip the timing capture: hold it, capture, then rethrow so the outer catch
            # records it exactly as it did when the syscall ran first.
            $evidenceFailure = $null
            try { . $collectEvidence } catch { $evidenceFailure = $_ }
            . $captureSystemTimes
            if ($null -ne $evidenceFailure) { throw $evidenceFailure }
        }
        else {
            . $captureSystemTimes
            if (-not $SkipEvidenceCollection) { . $collectEvidence }
        }

        $snapshot.collected = $snapshot.systemTimesCollected
    }
    catch {
        $snapshot.collected = $false
        $snapshot.error = if ($null -eq $_.Exception) {
            "collection failed (unknown exception)"
        } else {
            "collection failed: $($_.Exception.GetType().Name)"
        }
    }
    [pscustomobject]$snapshot
}

function Get-HostLoadNonSubjectCpuLoadPercent {
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 7 (both keys BLOCKER -- replace per-process
    # accounting BY CONSTRUCTION): non-subject load is now system-wide busy time (Win32
    # GetSystemTimes: kernel time INCLUDES idle time, so busy = (kernel - idle) + user) minus the
    # subject's own CPU time, both as cumulative deltas over the SAME [previous, current] window --
    # one syscall plus one process-handle read per snapshot (see Get-HostLoadSnapshot), no process
    # list involved anywhere in this computation. This retires the round 4-6 per-process-sum
    # mechanism (Get-HostLoadProcessCpuSecondsPairs, processCpuSecondsById, unreadableProcessCount,
    # totalCpuSeconds) and, with it, the whole class of failure modes that mechanism kept growing
    # new instances of every round: a throwing per-process getter silently reading as zero, a
    # process present in only one of the two snapshots contributing nothing to either side, and PID
    # reuse matched with no creation-identity check. None of those are inputs to this computation
    # anymore, so none of them are failure modes of it either.
    #
    # Third state, BY CONSTRUCTION, never a raw fallback: returns $null when EITHER snapshot's
    # system times could not be collected (systemTimesCollected=$false, i.e. Get-HostLoadSystemTimes
    # failed), OR either snapshot's subjectCpuSeconds is unreadable (including the subject having
    # exited mid-interval without a final reading), OR the elapsed window is non-positive, OR
    # ProcessorCount is invalid. $null here means the interval's coverage is UNKNOWN -- the caller
    # (Get-HostLoadVerdict) must never substitute a raw system-wide number for a missing value here;
    # that raw-fallback path is exactly what rounds 4-6 relied on and what let an honest-looking
    # LOW number actually be an INCOMPLETE one. cpuLoadPercent is retained on the snapshot purely
    # for the receipt (round 7 brief: "it must not feed the quiet decision on its own") and is never
    # read by this function at all.
    #
    # round 8 (sol BLOCKER = fable minor 1 -- window misalignment): elapsed time is now taken from
    # systemTimesCapturedAtUtc (stamped inside Get-HostLoadSystemTimes, immediately adjacent to the
    # GetSystemTimes syscall itself), never from the snapshot's general capturedAtUtc (stamped at
    # the TOP of Get-HostLoadSnapshot, before up to ~4s of CIM/Get-Process evidence work that
    # precedes the syscall). Busy-delta and elapsed must describe the SAME window; using the
    # earlier caller timestamp let variable pre-syscall collection latency stretch or shrink the
    # denominator independently of the numerator (sol's repro: a continuously 100%-busy host's
    # 1.015s counter interval divided by a 2.127s timestamp interval read 47.7%/quiet).
    param(
        [object]$CurrentSnapshot,
        [object]$PreviousSnapshot,
        [int]$ProcessorCount = [Environment]::ProcessorCount
    )

    if ($null -eq $CurrentSnapshot -or $null -eq $PreviousSnapshot -or $ProcessorCount -le 0) {
        return $null
    }
    if (-not [bool]$CurrentSnapshot.systemTimesCollected -or -not [bool]$PreviousSnapshot.systemTimesCollected) {
        return $null
    }
    if ($null -eq $CurrentSnapshot.systemIdleSeconds -or $null -eq $CurrentSnapshot.systemKernelSeconds -or
        $null -eq $CurrentSnapshot.systemUserSeconds -or $null -eq $PreviousSnapshot.systemIdleSeconds -or
        $null -eq $PreviousSnapshot.systemKernelSeconds -or $null -eq $PreviousSnapshot.systemUserSeconds) {
        return $null
    }
    if ($null -eq $CurrentSnapshot.subjectCpuSeconds -or $null -eq $PreviousSnapshot.subjectCpuSeconds) {
        return $null
    }
    if ($null -eq $CurrentSnapshot.systemTimesCapturedAtUtc -or $null -eq $PreviousSnapshot.systemTimesCapturedAtUtc) {
        return $null
    }
    $elapsedSec = ([datetime]$CurrentSnapshot.systemTimesCapturedAtUtc - [datetime]$PreviousSnapshot.systemTimesCapturedAtUtc).TotalSeconds
    if ($elapsedSec -le 0) {
        return $null
    }

    $idleDeltaSeconds = [double]$CurrentSnapshot.systemIdleSeconds - [double]$PreviousSnapshot.systemIdleSeconds
    $kernelDeltaSeconds = [double]$CurrentSnapshot.systemKernelSeconds - [double]$PreviousSnapshot.systemKernelSeconds
    $userDeltaSeconds = [double]$CurrentSnapshot.systemUserSeconds - [double]$PreviousSnapshot.systemUserSeconds
    $busyDeltaSeconds = ($kernelDeltaSeconds - $idleDeltaSeconds) + $userDeltaSeconds
    $subjectDeltaSeconds = [double]$CurrentSnapshot.subjectCpuSeconds - [double]$PreviousSnapshot.subjectCpuSeconds

    $percent = 100.0 * ($busyDeltaSeconds - $subjectDeltaSeconds) / ($elapsedSec * $ProcessorCount)
    [Math]::Max(0.0, [Math]::Min(100.0, $percent))
}

function Get-HostLoadVerdict {
    # Three outcomes, never two: quiet (usable), exceeded (usable-as-evidence, unusable-as-signal),
    # unknown (telemetry could not be collected -- treated as PROVISIONAL too, never as "quiet").
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1: a run whose load exceeds the bar, or whose load could
    # not be measured, is marked PROVISIONAL -- worth recording, never usable as a regression or
    # acceptance signal. Never fail the run for this; only mark it.
    #
    # round 3: -During carries zero or more interior samples taken WHILE the leg was running. THE
    # RULE is PEAK: the maximum of every measured interval's non-subject load decides
    # exceeded/quiet, not an average or a "sustained for N samples" threshold. Peak is deliberate: a
    # short interior burst is exactly the case bracketing misses and this round exists to catch, and
    # averaging or requiring sustain would dilute or miss it again. The failure direction is
    # asymmetric -- under-marking a loaded leg as clean is dangerous (a noisy fps number enters
    # comparisons as trustworthy), while over-marking a quiet leg as provisional is cheap
    # (provisional never fails a run, only removes it from comparisons) -- so peak is the
    # conservative choice on both counts.
    #
    # round 4 (sol BLOCKER): before/after-plus-interior sampling only bounds the risk, it does not
    # eliminate it. -SampleIntervalMs states the cadence the caller actually used for interior
    # sampling. When the caller declares a cadence, this leg can be "quiet" ONLY if sampling was
    # both enabled (SampleIntervalMs > 0) and it produced at least one interior sample -- otherwise
    # the leg's coverage never rose above bracket-only and the verdict must say the sampling could
    # not see a mid-leg burst (unknown/provisional), not "quiet".
    #
    # round 5 (sol MAJOR): -ObservedMaxSampleGapMs carries the REAL max gap between consecutive
    # sample timestamps (computed by the caller from each snapshot's own capturedAtUtc), and
    # coverage is unknown when that observed gap materially exceeds the declared cadence (1.5x
    # slack for ordinary scheduling jitter, not a loophole).
    #
    # round 7 (both keys BLOCKER -- replace per-process accounting BY CONSTRUCTION): peak
    # non-subject load is now computed HERE, centrally, as the maximum over every CONSECUTIVE pair's
    # matched-window delta in the full before/during/after sequence (Get-HostLoadNonSubjectCpuLoadPercent),
    # rather than requiring each call site to pre-attach a "nonSubjectCpuLoadPercent" property to
    # each sample by hand. That hand-wiring was exactly astra's round-6 finding: the
    # lastDuring-to-after interval (and, symmetrically, any interval a caller simply forgot to
    # compute) never got a value at all and silently fell back to raw cpuLoadPercent. Centralizing
    # the computation means every interval in the sequence is covered by construction -- there is no
    # call site left that could forget one. $Before is the sequence's reference point and is never
    # independently evaluated; every OTHER sample's value is the interval ending at it. When ANY
    # interval's load could not be computed (Get-HostLoadNonSubjectCpuLoadPercent returned $null --
    # see that function for the exact conditions), the whole leg is unknown; there is no raw
    # cpuLoadPercent fallback anywhere in this path, per the round-7 brief.
    param(
        [object]$Before,
        [object]$After,
        [object[]]$During = @(),
        [double]$Bar,
        [int]$ProcessorCount = [Environment]::ProcessorCount,
        [AllowNull()][System.Nullable[int]]$SampleIntervalMs = $null,
        [AllowNull()][System.Nullable[int]]$ObservedMaxSampleGapMs = $null
    )

    $samples = @($Before) + @($During) + @($After)
    $collectedSamples = @($samples | Where-Object { $_.collected })
    $collectionUnknown = ($collectedSamples.Count -lt $samples.Count)
    $samplingDeclared = ($null -ne $SampleIntervalMs)
    $samplingDisabled = ($samplingDeclared -and $SampleIntervalMs -le 0)
    $samplingBlind = ($samplingDeclared -and $SampleIntervalMs -gt 0 -and @($During).Count -eq 0)
    $samplingSpacingExceeded = (
        $samplingDeclared -and $SampleIntervalMs -gt 0 -and $null -ne $ObservedMaxSampleGapMs -and
        [double]$ObservedMaxSampleGapMs -gt ([double]$SampleIntervalMs * 1.5))
    $coverageUnknown = ($samplingDisabled -or $samplingBlind -or $samplingSpacingExceeded)

    $intervalLoads = [System.Collections.Generic.List[double]]::new()
    $intervalUnknown = $false
    for ($sampleIndex = 1; $sampleIndex -lt $samples.Count; $sampleIndex++) {
        $intervalPercent = Get-HostLoadNonSubjectCpuLoadPercent `
            -CurrentSnapshot $samples[$sampleIndex] -PreviousSnapshot $samples[$sampleIndex - 1] `
            -ProcessorCount $ProcessorCount
        if ($null -eq $intervalPercent) {
            $intervalUnknown = $true
        } else {
            $intervalLoads.Add([double]$intervalPercent)
        }
    }

    $unknown = ($collectionUnknown -or $coverageUnknown -or $intervalUnknown)
    $maxCpuLoadPercent = if ($intervalLoads.Count -gt 0) {
        ($intervalLoads | Measure-Object -Maximum).Maximum
    } else {
        $null
    }
    $exceeded = ($null -ne $maxCpuLoadPercent -and $maxCpuLoadPercent -gt $Bar)
    $state = if ($unknown) {
        "unknown"
    } elseif ($exceeded) {
        "exceeded"
    } else {
        "quiet"
    }
    $provisional = ($unknown -or $exceeded)
    $reason = if ($collectionUnknown) {
        $uncollectedCount = @($samples).Count - $collectedSamples.Count
        "Host load telemetry could not be collected for $uncollectedCount of $($samples.Count) " +
            "snapshot(s) (before.collected=$($Before.collected), during=$($During.Count) " +
            "sample(s), after.collected=$($After.collected)); an fps measurement under unknown " +
            "host load is never usable as a regression or acceptance signal."
    } elseif ($intervalUnknown) {
        "Non-subject host load could not be computed for at least one sampling interval in this " +
            "leg (system-wide CPU time or the subject's own CPU time was unreadable at one or both " +
            "ends of that interval, or the subject exited mid-interval without a final reading); " +
            "an fps measurement is never usable as a regression or acceptance signal without a " +
            "trustworthy load figure for the interval it was measured over."
    } elseif ($samplingDisabled) {
        "Interior host-load sampling was disabled (-HostLoadSampleIntervalMs 0); a mid-leg burst " +
            "confined between the before/after snapshots would be invisible to bracket-only " +
            "monitoring, so this leg's coverage cannot certify quiet."
    } elseif ($samplingBlind) {
        "Interior host-load sampling was enabled at $($SampleIntervalMs)ms but collected zero " +
            "samples during this leg (the leg ended before the first sampling tick); coverage " +
            "never rose above bracket-only, so this leg's coverage cannot certify quiet."
    } elseif ($samplingSpacingExceeded) {
        "Interior host-load sampling was declared at $($SampleIntervalMs)ms but the observed " +
            "max spacing between samples was $($ObservedMaxSampleGapMs)ms; coverage did not " +
            "actually meet the declared cadence (a slow OnSample callback can widen the true " +
            "blind interval beyond what was requested), so this leg's coverage cannot certify quiet."
    } elseif ($exceeded) {
        "Host non-subject CPU load reached $maxCpuLoadPercent% during this leg (peak across " +
            "$($samples.Count - 1) measured interval(s) spanning before/after plus " +
            "$($During.Count) interior sample(s)), exceeding the $Bar% bar; an fps number " +
            "measured on a loaded host is not a property of the build."
    } else {
        $null
    }

    [pscustomobject]@{
        bar = $Bar
        maxCpuLoadPercent = $maxCpuLoadPercent
        state = $state
        provisional = [bool]$provisional
        reason = $reason
    }
}

function Get-ObjectPropertyValue {
    param(
        [object]$Object,
        [string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }

    $null
}

function Convert-ToNullableDouble {
    param([object]$Value)

    if ($null -eq $Value) {
        return $null
    }

    $parsed = 0.0
    if ([double]::TryParse(
        [string]$Value,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed)) {
        return $parsed
    }

    $null
}

function Convert-ToNullableBool {
    param([object]$Value)

    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [bool]) {
        return [bool]$Value
    }

    $text = ([string]$Value).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    if ($text -match '^(1|true|yes|on)$') {
        return $true
    }
    if ($text -match '^(0|false|no|off)$') {
        return $false
    }

    $null
}

function Convert-FpsStatusTextToValue {
    param([object]$Value)

    if ($null -eq $Value) {
        return $null
    }

    $match = [regex]::Match(
        [string]$Value,
        '([-+]?[0-9]+(?:\.[0-9]+)?)\s*fps',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) {
        return $null
    }

    Convert-ToNullableDouble $match.Groups[1].Value
}

function Get-ScreenshotImageMetadata {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    Add-Type -AssemblyName System.Drawing
    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $item = Get-Item -LiteralPath $resolvedPath
    $image = [System.Drawing.Image]::FromFile($resolvedPath)
    try {
        $aspect = $null
        if ($image.Height -gt 0) {
            $aspect = [Math]::Round($image.Width / $image.Height, 6)
        }

        [pscustomobject]@{
            width = $image.Width
            height = $image.Height
            aspect = $aspect
            pixelFormat = $image.PixelFormat.ToString()
            length = $item.Length
            sha256 = (Get-FileHash -LiteralPath $resolvedPath -Algorithm SHA256).Hash
        }
    }
    finally {
        $image.Dispose()
    }
}

function Get-EvidenceFileBinding {
    param(
        [string]$Path,
        [string]$Label,
        [switch]$Optional
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or
        -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        if ($Optional) { return $null }
        throw "$Label file not found: $Path"
    }
    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $item = Get-Item -LiteralPath $resolvedPath
    [pscustomobject]@{
        path = $resolvedPath
        length = $item.Length
        sha256 = (Get-FileHash -LiteralPath $resolvedPath -Algorithm SHA256).Hash
    }
}

function New-FpsStatusProofCrop {
    param(
        [string]$SourcePath,
        [string]$OutputPath,
        [int]$CropWidth = 720,
        [int]$CropHeight = 72,
        [int]$Scale = 3
    )

    if ([string]::IsNullOrWhiteSpace($SourcePath) -or -not (Test-Path -LiteralPath $SourcePath)) {
        return $null
    }

    Add-Type -AssemblyName System.Drawing
    $resolvedSourcePath = (Resolve-Path -LiteralPath $SourcePath).Path
    $resolvedOutputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
    $outputDir = Split-Path -Parent $resolvedOutputPath
    if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
        New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    }

    $source = [System.Drawing.Image]::FromFile($resolvedSourcePath)
    try {
        $actualCropWidth = [Math]::Min($CropWidth, $source.Width)
        $actualCropHeight = [Math]::Min($CropHeight, $source.Height)
        $crop = [System.Drawing.Rectangle]::new(
            0,
            [Math]::Max(0, $source.Height - $actualCropHeight),
            $actualCropWidth,
            $actualCropHeight)
        $scaledWidth = [Math]::Max(1, $actualCropWidth * $Scale)
        $scaledHeight = [Math]::Max(1, $actualCropHeight * $Scale)
        $target = [System.Drawing.Bitmap]::new($scaledWidth, $scaledHeight)
        try {
            $graphics = [System.Drawing.Graphics]::FromImage($target)
            try {
                $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
                $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
                $graphics.DrawImage(
                    $source,
                    [System.Drawing.Rectangle]::new(0, 0, $scaledWidth, $scaledHeight),
                    $crop,
                    [System.Drawing.GraphicsUnit]::Pixel)
            }
            finally {
                $graphics.Dispose()
            }

            $target.Save($resolvedOutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
        }
        finally {
            $target.Dispose()
        }

        $item = Get-Item -LiteralPath $resolvedOutputPath
        [pscustomobject]@{
            outputPath = $item.FullName
            takenAtUtc = $item.LastWriteTimeUtc.ToString("o")
            method = "bottom-left-status-crop-from-window-grab"
            sourcePath = $resolvedSourcePath
            crop = [pscustomobject]@{
                x = $crop.X
                y = $crop.Y
                width = $crop.Width
                height = $crop.Height
                scale = $Scale
            }
            length = $item.Length
            image = Get-ScreenshotImageMetadata -Path $resolvedOutputPath
        }
    }
    finally {
        $source.Dispose()
    }
}

function New-ScreenshotAspectEvidence {
    param(
        [object]$ImageMetadata,
        [object]$VisualState
    )

    $stretchX = Convert-ToNullableDouble (Get-ObjectPropertyValue $VisualState "stretch_x")
    $stretchY = Convert-ToNullableDouble (Get-ObjectPropertyValue $VisualState "stretch_y")
    $hStretchIndex = Get-ObjectPropertyValue $VisualState "h_stretch_index"
    $vStretchIndex = Get-ObjectPropertyValue $VisualState "v_stretch_index"

    $hasStretchTelemetry = $null -ne $stretchX -and $null -ne $stretchY
    $activeStretch = $false
    if ($hasStretchTelemetry) {
        $activeStretch =
            [Math]::Abs($stretchX - 1.0) -gt 0.0001 -or
            [Math]::Abs($stretchY - 1.0) -gt 0.0001
    }

    $mode = "not-captured"
    $interpretation = "No screenshot was captured."
    if ($null -ne $ImageMetadata) {
        if (-not $hasStretchTelemetry) {
            $mode = "presented-frame-stretch-unknown"
            $interpretation = "Screenshot dimensions are from the presented frame, but stretch telemetry was unavailable."
        }
        elseif ($activeStretch) {
            $mode = "presented-playback-stretch"
            $interpretation = "Screenshot dimensions include the active playback stretch/de-squeeze state."
        }
        else {
            $mode = "neutral-presented-frame"
            $interpretation = "Screenshot dimensions are the presented frame with neutral stretch."
        }
    }

    [pscustomobject]@{
        mode = $mode
        interpretation = $interpretation
        width = Get-ObjectPropertyValue $ImageMetadata "width"
        height = Get-ObjectPropertyValue $ImageMetadata "height"
        aspect = Get-ObjectPropertyValue $ImageMetadata "aspect"
        stretchX = $stretchX
        stretchY = $stretchY
        hStretchIndex = $hStretchIndex
        vStretchIndex = $vStretchIndex
        activeStretch = $activeStretch
        hasStretchTelemetry = $hasStretchTelemetry
    }
}

function Add-EnvironmentPairs {
    param(
        [object]$Target,
        [string[]]$Pairs
    )

    $expandedPairs = @()
    foreach ($rawPair in $Pairs) {
        if ([string]::IsNullOrWhiteSpace($rawPair)) {
            continue
        }

        $parts = @($rawPair -split ',')
        if ($parts.Count -gt 1 -and ($parts | Where-Object { $_.IndexOf("=") -lt 1 }).Count -eq 0) {
            $expandedPairs += $parts
        }
        else {
            $expandedPairs += $rawPair
        }
    }

    foreach ($pair in $expandedPairs) {
        if ([string]::IsNullOrWhiteSpace($pair)) {
            continue
        }

        $separatorIndex = $pair.IndexOf("=")
        if ($separatorIndex -lt 1) {
            throw "Invalid -ExtraEnvironment entry '$pair'. Use KEY=VALUE."
        }

        $key = $pair.Substring(0, $separatorIndex).Trim()
        $value = $pair.Substring($separatorIndex + 1)
        if ([string]::IsNullOrWhiteSpace($key)) {
            throw "Invalid -ExtraEnvironment entry '$pair'. The key cannot be empty."
        }

        $Target[$key] = $value
    }
}

function Wait-ForLogLineMatch {
    param(
        [string]$LogRoot,
        [string]$Pattern,
        [datetime]$NotBeforeUtc = [datetime]::MinValue,
        [int]$TimeoutMs = 15000,
        [int]$PollMs = 250
    )

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt $TimeoutMs) {
        try {
            $latestLog = Get-ChildItem -LiteralPath $LogRoot -Filter "mlvapp-*.log" |
                Sort-Object LastWriteTimeUtc -Descending |
                Select-Object -First 1
            if ($latestLog) {
                $match = Select-String -LiteralPath $latestLog.FullName -Pattern $Pattern -SimpleMatch |
                    Select-Object -Last 1
                if ($match) {
                    $timestampUtc = Get-LogTimestampUtc -Line $match.Line
                    if ($timestampUtc -and $timestampUtc -lt $NotBeforeUtc) {
                        Start-Sleep -Milliseconds $PollMs
                        continue
                    }
                    return [pscustomobject]@{
                        path = $latestLog.FullName
                        line = $match.Line
                        timestampUtc = $timestampUtc
                    }
                }
            }
        }
        catch {
        }

        Start-Sleep -Milliseconds $PollMs
    }

    return $null
}

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
if ($TargetPresentedFrames -gt 0 -and $LegacyGuiSmokeOptions) {
    throw "-TargetPresentedFrames requires the current GUI-smoke option contract; it cannot be combined with -LegacyGuiSmokeOptions."
}
if ([double]::IsNaN($Seconds) -or [double]::IsInfinity($Seconds) -or $Seconds -lt 0) {
    throw "-Seconds must be a finite non-negative value."
}
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = Join-Path $root "platform\qt\build-release\release\MLVApp.exe"
}

$exe = (Resolve-Path -LiteralPath $ExePath).Path
if ([string]::IsNullOrWhiteSpace($ClipPath)) {
    throw "Missing -Input <clip.mlv>."
}
$resolvedClipPath = Resolve-Path -LiteralPath $ClipPath
$inputPath =
    if ($resolvedClipPath.ProviderPath) {
        $resolvedClipPath.ProviderPath
    }
    else {
        $resolvedClipPath.Path
    }
$smokeReceiptPath = if ([string]::IsNullOrWhiteSpace($Receipt)) {
    $null
} else {
    (Resolve-Path -LiteralPath $Receipt).ProviderPath
}

function Get-GuiSmokeImmutableFileBinding([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    [pscustomobject]@{
        path = $item.FullName
        length = [long]$item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
    }
}

$exeStamp = Get-MlvAppBuildStamp -ExePath $exe
$clipBaseName = [IO.Path]::GetFileNameWithoutExtension($inputPath)
$clipParts = @(Get-ChildItem -LiteralPath (Split-Path -Parent $inputPath) -File | Where-Object {
    $_.BaseName -ceq $clipBaseName -and $_.Extension -match '^\.M(?:LV|\d\d)$'
} | Sort-Object @{ Expression = { if ($_.Extension -ieq '.MLV') { -1 } else { [int]$_.Extension.Substring(2) } } } | ForEach-Object {
    Get-GuiSmokeImmutableFileBinding -Path $_.FullName
})
# The multipart binding is EVIDENCE ABOUT A REAL LAUNCH. Under -DryRun nothing launches and
# $launchInputBindings is never consumed (its only reader is the evidence object emitted after
# the -DryRun exit), so requiring a resolvable .MLV here would make a flag-contract dry run
# depend on real input files. Strictness is unchanged for every non-dry run.
if (-not $DryRun -and ($clipParts.Count -eq 0 -or -not $clipParts[0].path.Equals($inputPath, [StringComparison]::OrdinalIgnoreCase))) {
    throw 'Could not build an ordered immutable multipart clip binding rooted at the requested .MLV file.'
}
$launchInputBindings = [pscustomobject]@{
    executable = [pscustomobject]@{
        path = $exe
        length = [long](Get-Item -LiteralPath $exe).Length
        sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
        embeddedCommit = $exeStamp.sha
        dirty = $exeStamp.dirty
        stampFound = [bool]$exeStamp.found
    }
    clipParts = $clipParts
    receipt = if ($smokeReceiptPath) { Get-GuiSmokeImmutableFileBinding -Path $smokeReceiptPath } else { $null }
}

if ([string]::IsNullOrWhiteSpace($Output)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $clipBase = [IO.Path]::GetFileNameWithoutExtension($inputPath)
    $Output = Join-Path $root ".claude-state\profiling\$stamp-gui-smoke\$clipBase.json"
}
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$outputDir = Split-Path -Parent $outputPath
if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
    New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
}
$runNonce = [Guid]::NewGuid().ToString("N")
$outputStem = [IO.Path]::GetFileNameWithoutExtension($outputPath)
$logRoot = Join-Path $outputDir ("logs-{0}-{1}" -f $outputStem, $runNonce)
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

$screenshotPath = $null
$windowScreenshotPath = $null
$fpsStatusCropPath = $null
if ($CaptureScreenshot) {
    if ([string]::IsNullOrWhiteSpace($ScreenshotOutputDir)) {
        $ScreenshotOutputDir = Join-Path $outputDir "screenshots"
    }
    $screenshotDirPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ScreenshotOutputDir)
    New-Item -ItemType Directory -Force -Path $screenshotDirPath | Out-Null
    $clipBase = [IO.Path]::GetFileNameWithoutExtension($inputPath)
    $screenshotPath = Join-Path $screenshotDirPath ("{0}.png" -f $clipBase)
    if (-not $SkipWindowScreenshot) {
        $windowScreenshotPath = Join-Path $screenshotDirPath ("{0}-window.png" -f $clipBase)
        $fpsStatusCropPath = Join-Path $screenshotDirPath ("{0}-fps-status.png" -f $clipBase)
    }
}

$arguments = @(
    "--gui-smoke-playback",
    "--input", $inputPath,
    "--seconds", ([string]::Format(
        [System.Globalization.CultureInfo]::InvariantCulture,
        "{0}",
        $Seconds)),
    "--start-frame", [string]$StartFrame
)
if ($TargetPresentedFrames -gt 0 -and -not $LegacyGuiSmokeOptions) {
    $arguments += @("--presented-frames", [string]$TargetPresentedFrames)
}
if (-not $LegacyGuiSmokeOptions) {
    $arguments += @("--drop-frame-mode", $DropFrameMode)
}
$arguments += @(
    "--settle-ms", [string]$SettleMs,
    "--settle-cpu-percent", ([string]::Format(
        [System.Globalization.CultureInfo]::InvariantCulture,
        "{0}",
        $SettleCpuPercent)),
    "--settle-cpu-stable-ms", [string]$SettleCpuStableMs,
    "--settle-cpu-max-ms", [string]$SettleCpuMaxMs
)
# RULE 2026-06-26 (Layi): loop short clips so they play the whole -Seconds window (not one pass + stop).
if (-not $NoLoop) { $arguments += "--loop" }
if (-not [string]::IsNullOrWhiteSpace($Receipt)) {
    $arguments += @("--receipt", $smokeReceiptPath)
}
if (-not [string]::IsNullOrWhiteSpace($Scope)) {
    $arguments += @("--scope", $Scope)
}
if (-not [string]::IsNullOrWhiteSpace($PlaybackDebayer)) {
    $arguments += "--playback-debayer=$PlaybackDebayer"
}
if (-not [string]::IsNullOrWhiteSpace($PlaybackProcessing)) {
    $arguments += "--playback-processing=$PlaybackProcessing"
}
if (-not [string]::IsNullOrWhiteSpace($GpuPreviewProcessing)) {
    $arguments += "--gpu-preview-processing=$GpuPreviewProcessing"
}
if (-not [string]::IsNullOrWhiteSpace($GpuBilinearDebayer)) {
    $arguments += "--gpu-bilinear-debayer=$GpuBilinearDebayer"
}
if (-not [string]::IsNullOrWhiteSpace($GpuAmazeDebayer)) {
    $arguments += "--gpu-amaze-debayer=$GpuAmazeDebayer"
}
if ($GpuAmazeTexturePresent) {
    $arguments += "--gpu-amaze-texture-present"
}
if ($DisableLookAssist -and -not $LegacyGuiSmokeOptions) {
    $arguments += "--no-look-assist"
}
if ($ExerciseClipLifecycleStress) {
    $arguments += "--exercise-clip-lifecycle-stress"
    if (-not [string]::IsNullOrWhiteSpace($StressSwitchInput)) {
        $arguments += @("--stress-switch-input", (Resolve-Path -LiteralPath $StressSwitchInput).Path)
    }
    $arguments += @("--stress-switch-at-ms", [string]$StressSwitchAtMs)
    $arguments += @("--stress-seek-frame", [string]$StressSeekFrame)
}
if ($EnablePhase3QualityModes) {
    $arguments += "--enable-phase3-quality-modes"
}
if (-not [string]::IsNullOrWhiteSpace($StageLog)) {
    $stageLogPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StageLog)
    $stageLogDir = [IO.Path]::GetDirectoryName($stageLogPath)
    if (-not [string]::IsNullOrWhiteSpace($stageLogDir)) {
        New-Item -ItemType Directory -Force -Path $stageLogDir | Out-Null
    }
    $arguments += "--stage-log=$stageLogPath"
}
if ($CaptureScreenshot) {
    $arguments += @("--screenshot-output", $screenshotPath)
    if (-not [string]::IsNullOrWhiteSpace($windowScreenshotPath)) {
        $arguments += @("--window-screenshot-output", $windowScreenshotPath)
    }
}
if ($Zebras -eq "on") {
    $arguments += "--zebras"
}
elseif ($Zebras -eq "off") {
    $arguments += "--no-zebras"
}
if ($AdditionalArgs.Count -gt 0) {
    $arguments += $AdditionalArgs
}

$useAutoPlaybackThreads = [string]::IsNullOrWhiteSpace($Threads) -or
    $Threads.Trim().Equals("auto", [System.StringComparison]::OrdinalIgnoreCase) -or
    $Threads.Trim().Equals("0", [System.StringComparison]::OrdinalIgnoreCase)

$launchEnv = [ordered]@{}
if (-not $useAutoPlaybackThreads) {
    $launchEnv["MLVAPP_PLAYBACK_MAX_THREADS"] = $Threads
}
if ($FrameTelemetry) {
    $launchEnv["MLVAPP_PLAYBACK_SMOKE_TELEMETRY"] = "1"
}
$launchEnv["MLVAPP_CRASH_FORENSICS_LOG_DIR"] = $logRoot
if (-not [string]::IsNullOrWhiteSpace($QualityMode)) {
    $launchEnv["MLVAPP_PLAYBACK_QUALITY_MODE"] = $QualityMode
}
if ($EnablePhase3QualityModes) {
    $launchEnv["MLVAPP_PLAYBACK_PHASE3_UNATTENDED"] = "1"
}
if (-not [string]::IsNullOrWhiteSpace($ScaleFactor)) {
    $launchEnv["MLVAPP_PLAYBACK_SCALE_FACTOR"] = $ScaleFactor
}
if ($PreferHqMean23) {
    $launchEnv["MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"] = "1"
}
if ($RbfDetailTiming) {
    $launchEnv["MLVAPP_PLAYBACK_RBF_DETAIL_TIMING"] = "1"
}
if (-not [string]::IsNullOrWhiteSpace($GpuPlaybackReconBackend)) {
    $launchEnv["MLVAPP_GPU_PLAYBACK_RECON_BACKEND"] = $GpuPlaybackReconBackend
}
Add-EnvironmentPairs -Target $launchEnv -Pairs $ExtraEnvironment
if (-not [string]::IsNullOrWhiteSpace($env:OMP_NUM_THREADS)) {
    $launchEnv["OMP_NUM_THREADS"] = $env:OMP_NUM_THREADS
}

$experimentalEnvironment = @(
    "MLVAPP_ENABLE_SH_CURVE_INDEX_MASK",
    "MLVAPP_DISABLE_SH_CURVE_INDEX_MASK",
    "MLVAPP_SHADOWS_HIGHLIGHTS_PROBE",
    "MLVAPP_EXPERIMENTAL_PROCESSED8_PREFETCH",
    "MLVAPP_ENABLE_AVX2_INTRIN_DIRECT8",
    "MLVAPP_DISABLE_AVX2_INTRIN_DIRECT8",
    "MLVAPP_ENABLE_DUAL_ISO_FAST_X4_IN_HQ",
    "MLVAPP_PLAYBACK_RBF_DETAIL_TIMING",
    "MLVAPP_GPU_PLAYBACK_RECON_BACKEND",
    "MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_PROBE",
    "MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE",
    "MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_CREATIVE_PROBE",
    "MLVAPP_PROCESSING_CORE_COLOR_CAM_WB_PROBE",
    "MLVAPP_PROCESSING_CORE_COLOR_GAMMA_PROBE",
    "MLVAPP_PROCESSING_CORE_CREATIVE_PROBE",
    "MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS",
    "MLVAPP_DISABLE_RAW_UINT16_PREFETCH",
    "MLVAPP_DISABLE_PLAY_START_PREROLL"
)
$experimentalEnvironmentToClear = @(
    $experimentalEnvironment | Where-Object { -not $launchEnv.Contains($_) }
)
$clearedEnvironment = @()
if (-not $FrameTelemetry) {
    $clearedEnvironment += "MLVAPP_PLAYBACK_SMOKE_TELEMETRY"
}
if ([string]::IsNullOrWhiteSpace($QualityMode) -and -not $launchEnv.Contains("MLVAPP_PLAYBACK_QUALITY_MODE")) {
    $clearedEnvironment += "MLVAPP_PLAYBACK_QUALITY_MODE"
}
if (-not $EnablePhase3QualityModes -and -not $launchEnv.Contains("MLVAPP_PLAYBACK_PHASE3_UNATTENDED")) {
    $clearedEnvironment += "MLVAPP_PLAYBACK_PHASE3_UNATTENDED"
}
if (-not $RbfDetailTiming -and -not $launchEnv.Contains("MLVAPP_PLAYBACK_RBF_DETAIL_TIMING")) {
    $clearedEnvironment += "MLVAPP_PLAYBACK_RBF_DETAIL_TIMING"
}
if ([string]::IsNullOrWhiteSpace($ScaleFactor) -and -not $launchEnv.Contains("MLVAPP_PLAYBACK_SCALE_FACTOR")) {
    $clearedEnvironment += "MLVAPP_PLAYBACK_SCALE_FACTOR"
}
if (-not $PreferHqMean23 -and -not $launchEnv.Contains("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23")) {
    $clearedEnvironment += "MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"
}
if (-not $PreserveExperimentalEnvironment) {
    $clearedEnvironment += $experimentalEnvironmentToClear
}
$clearedEnvironment = @($clearedEnvironment | Select-Object -Unique)
$effectiveExpectedVisualScaleRequest = if ($ExpectedVisualScaleRequest -eq -2) {
    $ExpectedScaleRequest
} else {
    $ExpectedVisualScaleRequest
}

$derivedProcessTimeoutMs = [Math]::Ceiling(
    ([Math]::Max(0.0, $Seconds) * 1000.0) +
    [Math]::Max(0, $SettleMs) +
    [Math]::Max(0, $SettleCpuMaxMs) +
    [Math]::Max(0, $ScreenshotDelayMs) +
    [Math]::Max(0, $ScreenshotWindowWaitMs) +
    [Math]::Max(0, $ScreenshotCaptureTimeoutMs) +
    $(if ($ExerciseClipLifecycleStress) { [Math]::Max(0, $StressSwitchAtMs) + 60000 } else { 30000 })
)
if ($derivedProcessTimeoutMs -gt 3600000) {
    throw "Derived GUI-smoke process timeout exceeds the 3600000 ms safety ceiling; pass a smaller bounded workload."
}
$effectiveProcessTimeoutMs = if ($ProcessTimeoutMs -gt 0) {
    $ProcessTimeoutMs
} else {
    [Math]::Max(1000, [int]$derivedProcessTimeoutMs)
}

if ($DryRun) {
    $dryRunResult = [pscustomobject]@{
        exePath = $exe
        workingDirectory = $root
        arguments = $arguments
        environment = $launchEnv
        clearsEnvironment = $clearedEnvironment
        preserveExperimentalEnvironment = [bool]$PreserveExperimentalEnvironment
        preLaunchSystemCpuSettle = [pscustomobject]@{
            requested = $SystemSettleCpuPercent -ge 0
            thresholdPercent = $SystemSettleCpuPercent
            stableMs = $SystemSettleCpuStableMs
            maxMs = $SystemSettleCpuMaxMs
        }
        validationPolicy = [pscustomobject]@{
            requireLookAssist = $RequireLookAssist
            requireCpuSettled = $RequireCpuSettled
            expectedScaleRequest = $ExpectedScaleRequest
            expectedVisualScaleRequest = $effectiveExpectedVisualScaleRequest
            expectedQualityMode = $ExpectedQualityMode
            expectedStretchX = $ExpectedStretchX
            expectedStretchY = $ExpectedStretchY
            expectedHStretchIndex = $ExpectedHStretchIndex
            expectedVStretchIndex = $ExpectedVStretchIndex
            expectedAspectMode = $ExpectedAspectMode
            expectedStretchTolerance = $ExpectedStretchTolerance
            qualityModeOverride = $QualityMode
            scaleFactorOverride = $ScaleFactor
            usePersistedPlaybackSettings = [bool]$UsePersistedPlaybackSettings
            legacyGuiSmokeOptions = [bool]$LegacyGuiSmokeOptions
            processTimeoutMs = $effectiveProcessTimeoutMs
        }
        output = $outputPath
    }
    if ($RequireFreshScreenshotRender) {
        $dryRunResult.validationPolicy | Add-Member `
            -NotePropertyName requireFreshScreenshotRender `
            -NotePropertyValue $true
    }
    $dryRunResult | ConvertTo-Json -Depth 5
    return
}

$smokeMutex = [System.Threading.Mutex]::new($false, "Global\MLVAppGuiSmokeLogParser")
$smokeMutexAcquired = $smokeMutex.WaitOne([TimeSpan]::FromMinutes(10))
if (-not $smokeMutexAcquired) {
    throw "Timed out waiting for another GUI smoke run to release the shared MLVApp log parser."
}

try {

$buildManifestPath = Join-Path (Split-Path -Parent $exe) "build-manifest.json"
$receiptPath = $null
for ($argumentIndex = 0; $argumentIndex -lt $arguments.Count; ++$argumentIndex) {
    if ([string]$arguments[$argumentIndex] -eq "--receipt" -and
        $argumentIndex + 1 -lt $arguments.Count) {
        $receiptPath = [string]$arguments[$argumentIndex + 1]
        break
    }
}
$captureInputBindings = [pscustomobject]@{
    executable = Get-EvidenceFileBinding -Path $exe -Label "executable"
    clip = Get-EvidenceFileBinding -Path $inputPath -Label "clip"
    receipt = if ($receiptPath) {
        Get-EvidenceFileBinding -Path $receiptPath -Label "receipt"
    } else { $null }
    buildManifest = Get-EvidenceFileBinding `
        -Path $buildManifestPath -Label "build manifest" -Optional
}
$captureBindingFailures = @()

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $exe
$startInfo.WorkingDirectory = $root
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $false
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
foreach ($argument in $arguments) {
    [void]$startInfo.ArgumentList.Add($argument)
}

$envBlock = $startInfo.EnvironmentVariables
$envBlock["MLVAPP_GUI_SMOKE_RUN_NONCE"] = $runNonce
if (-not $useAutoPlaybackThreads) {
    $envBlock["MLVAPP_PLAYBACK_MAX_THREADS"] = $Threads
}
if ($FrameTelemetry) {
    $envBlock["MLVAPP_PLAYBACK_SMOKE_TELEMETRY"] = "1"
}
else {
    [void]$envBlock.Remove("MLVAPP_PLAYBACK_SMOKE_TELEMETRY")
}
$envBlock["MLVAPP_CRASH_FORENSICS_LOG_DIR"] = $logRoot
if (-not [string]::IsNullOrWhiteSpace($QualityMode)) {
    $envBlock["MLVAPP_PLAYBACK_QUALITY_MODE"] = $QualityMode
}
else {
    [void]$envBlock.Remove("MLVAPP_PLAYBACK_QUALITY_MODE")
}
if ($EnablePhase3QualityModes) {
    $envBlock["MLVAPP_PLAYBACK_PHASE3_UNATTENDED"] = "1"
}
else {
    [void]$envBlock.Remove("MLVAPP_PLAYBACK_PHASE3_UNATTENDED")
}
if (-not [string]::IsNullOrWhiteSpace($ScaleFactor)) {
    $envBlock["MLVAPP_PLAYBACK_SCALE_FACTOR"] = $ScaleFactor
}
else {
    [void]$envBlock.Remove("MLVAPP_PLAYBACK_SCALE_FACTOR")
}
if ($PreferHqMean23) {
    $envBlock["MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"] = "1"
}
else {
    [void]$envBlock.Remove("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23")
}
if ($RbfDetailTiming) {
    $envBlock["MLVAPP_PLAYBACK_RBF_DETAIL_TIMING"] = "1"
}
else {
    [void]$envBlock.Remove("MLVAPP_PLAYBACK_RBF_DETAIL_TIMING")
}
if (-not [string]::IsNullOrWhiteSpace($GpuPlaybackReconBackend)) {
    $envBlock["MLVAPP_GPU_PLAYBACK_RECON_BACKEND"] = $GpuPlaybackReconBackend
}
else {
    [void]$envBlock.Remove("MLVAPP_GPU_PLAYBACK_RECON_BACKEND")
}
Add-EnvironmentPairs -Target $envBlock -Pairs $ExtraEnvironment
if (-not $PreserveExperimentalEnvironment) {
    foreach ($name in $experimentalEnvironmentToClear) {
        [void]$envBlock.Remove($name)
    }
}

$preLaunchSystemCpuSettle = Wait-SystemCpuSettle `
    -ThresholdPercent $SystemSettleCpuPercent `
    -StableMs $SystemSettleCpuStableMs `
    -MaxMs $SystemSettleCpuMaxMs

# -SubjectNotYetStarted also makes Get-HostLoadSnapshot take its system-times counters AFTER its evidence
# collection, i.e. immediately before Process.Start below, so the first interval starts at leg start.
$hostLoadBefore = Get-HostLoadSnapshot -TopProcessCount $HostLoadTopProcessCount -SubjectNotYetStarted
$startUtc = [datetime]::UtcNow
$process = [System.Diagnostics.Process]::Start($startInfo)
$screenshotCapture = $null
$windowScreenshotCapture = $null
$fpsStatusCropCapture = $null
$colorArtifactScan = $null
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
# PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 3: before/after alone only BRACKET the leg -- a burst
# confined to the interior reads quiet. $hostLoadDuringSamples collects zero or more interior
# snapshots taken WHILE the leg runs.
#
# round 7 (sol + astra MAJOR -- "the new budget does not bound collection as a whole, and a
# non-returning sampler prevents both timeout handling and receipt publication"): rounds 3-6 ran
# this loop's polling AND its Get-HostLoadSnapshot calls INSIDE Wait-GuiSmokeProcessBounded's
# module scope via an -OnSample callback -- a callback that never returned could never be
# preempted from there, and the module had to guess at "too slow" using an unrelated
# -SampleIntervalMs multiplier. With item 1 retiring the per-process enumeration, a
# Get-HostLoadSnapshot call is now bounded entirely by its own CIM/syscall timeouts (each CIM call
# already carries -OperationTimeoutSec; GetSystemTimes and the subject handle read are direct,
# unbounded-loop-free reads), so the chunk-and-sample loop moves HERE, into the caller's OWN scope,
# with no callback indirection at all -- Wait-GuiSmokeProcessBounded (below) goes back to being
# exactly what its round-3 default branch always was: a single bounded wait plus the
# stream-drain/kill-tree finalize, nothing else. If Get-HostLoadSnapshot itself somehow ran long
# (a slow CIM query near its own timeout ceiling), THIS loop is still bounded overall by
# $effectiveProcessTimeoutMs -- $hostLoadRemainingMs is recomputed every iteration, so a slow
# sample simply shortens (never lengthens past its own worst case) how much chunked-wait budget is
# left, and the loop still terminates.
#
# round 8 (sol MAJOR item 2 -- "evidence-only enumeration runs inside every measured leg,
# unbounded"): every interior call now passes -SkipEvidenceCollection, so a during-leg sample is
# EXACTLY the GetSystemTimes syscall plus the subject-handle read -- no CIM round-trip (even a
# bounded one still perturbed the leg it was measuring), no Get-Process/Sort-Object at all (the
# one truly unbounded call in the prior shape). Bound on one during-leg sample: two direct,
# loop-free reads with no I/O and no timeout surface to hang on. Before/after brackets still call
# without the switch and keep full CIM/Get-Process evidence for the receipt. At the default
# 4000ms cadence a 24-30s leg still takes ~6-7 interior samples, now at a cost too small to
# meaningfully measure (no round-trip of any kind), and adding no extra wall-clock time at all
# (the sampling happens inside intervals the code was already spending blocked in WaitForExit).
$hostLoadDuringSamples = [System.Collections.Generic.List[object]]::new()
if ($HostLoadSampleIntervalMs -gt 0) {
    $hostLoadSampleLoopStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $hostLoadRemainingMs = $effectiveProcessTimeoutMs - [int]$hostLoadSampleLoopStopwatch.ElapsedMilliseconds
        if ($hostLoadRemainingMs -le 0) {
            break
        }
        $hostLoadChunkMs = [Math]::Min($HostLoadSampleIntervalMs, $hostLoadRemainingMs)
        if ($process.WaitForExit($hostLoadChunkMs)) {
            break
        }
        $newSample = Get-HostLoadSnapshot -TopProcessCount $HostLoadTopProcessCount -SubjectProcess $process `
            -SkipEvidenceCollection
        $hostLoadDuringSamples.Add($newSample)
    }
    $hostLoadSampleLoopElapsedMs = [int]$hostLoadSampleLoopStopwatch.ElapsedMilliseconds
}
else {
    $hostLoadSampleLoopElapsedMs = 0
}
# Whatever wall-clock time the sampling loop already spent waiting counts against the overall
# process timeout budget -- the finalize wait below only needs to cover what's left. The 1ms floor
# never actually extends an already-finished leg: WaitForExit on an already-exited process returns
# TRUE immediately regardless of the timeout value passed to it.
$hostLoadFinalizeTimeoutMs = [Math]::Max(1, $effectiveProcessTimeoutMs - $hostLoadSampleLoopElapsedMs)
$processBoundary = Wait-GuiSmokeProcessBounded `
    -Process $process `
    -StandardOutputTask $stdoutTask `
    -StandardErrorTask $stderrTask `
    -TimeoutMs $hostLoadFinalizeTimeoutMs
$stdout = $processBoundary.stdout
$stderr = $processBoundary.stderr
$processExitCode = [int]$processBoundary.exitCode
$captureBindingFailures += @($processBoundary.failures)
$endUtc = [datetime]::UtcNow
# round 7: -SubjectProcess is now passed to the AFTER snapshot too (it previously was not passed
# at all). Under the new interval-based verdict (Get-HostLoadVerdict computes each interval's
# non-subject load itself -- see that function), the FINAL interval (last-during-or-before ->
# after) needs the after-snapshot's own subjectCpuSeconds reading exactly the same way every
# interior sample already needed its own; omitting it would have made that last interval
# permanently unknown. $process's own handle (not a fresh Get-Process -Id lookup) stays readable
# here even though the subject has typically already exited/been killed by this point -- see
# Get-HostLoadSnapshot's own comment on -SubjectProcess for why.
$hostLoadAfter = Get-HostLoadSnapshot -TopProcessCount $HostLoadTopProcessCount -SubjectProcess $process
# round 5 (sol MAJOR): the real upper bound on the blind interval is the actual spacing between
# consecutive sample TIMESTAMPS, not the declared -HostLoadSampleIntervalMs -- derived here
# directly from each snapshot's own capturedAtUtc (set by Get-HostLoadSnapshot itself) across the
# full before/during/after sequence, so a slow sample (or any other source of drift between
# chunked waits) shows up honestly instead of being silently absorbed into a receipt field that
# only ever echoed the request.
$hostLoadSampleSequence = @($hostLoadBefore) + @($hostLoadDuringSamples) + @($hostLoadAfter)
$hostLoadObservedMaxSampleGapMs = $null
for ($i = 1; $i -lt $hostLoadSampleSequence.Count; $i++) {
    $gapMs = ([datetime]$hostLoadSampleSequence[$i].capturedAtUtc -
        [datetime]$hostLoadSampleSequence[$i - 1].capturedAtUtc).TotalMilliseconds
    if ($null -eq $hostLoadObservedMaxSampleGapMs -or $gapMs -gt $hostLoadObservedMaxSampleGapMs) {
        $hostLoadObservedMaxSampleGapMs = $gapMs
    }
}
$hostLoadVerdict = Get-HostLoadVerdict -Before $hostLoadBefore -After $hostLoadAfter `
    -During @($hostLoadDuringSamples) -Bar $HostLoadCpuPercentBar `
    -SampleIntervalMs $HostLoadSampleIntervalMs `
    -ObservedMaxSampleGapMs $(if ($null -ne $hostLoadObservedMaxSampleGapMs) { [int][Math]::Ceiling($hostLoadObservedMaxSampleGapMs) } else { $null })

if ($CaptureScreenshot) {
    if (-not (Test-Path -LiteralPath $screenshotPath)) {
        $captureBindingFailures += "GUI smoke completed but did not write screenshot: $screenshotPath"
    }
    else {
        try {
            $screenshotItem = Get-Item -LiteralPath $screenshotPath
            $screenshotImage = Get-ScreenshotImageMetadata -Path $screenshotPath
            $screenshotCapture = [pscustomobject]@{
                outputPath = $screenshotItem.FullName
                takenAtUtc = $screenshotItem.LastWriteTimeUtc.ToString("o")
                method = "app-internal-presented-frame-preferred"
                length = $screenshotItem.Length
                image = $screenshotImage
                requestedDelayMs = $ScreenshotDelayMs
                windowWaitMs = $ScreenshotWindowWaitMs
                captureTimeoutMs = $ScreenshotCaptureTimeoutMs
            }
            $colorArtifactScan = Get-ScreenshotColorArtifactScan -Path $screenshotPath
        }
        catch {
            # round 8 (fable minor 2): type name only, matching every other sanitized catch in this
            # file -- raw exception text can embed a path or host identifier beyond what this
            # receipt already discloses deliberately (e.g. screenshotItem.FullName above).
            $captureBindingFailures += "GUI smoke screenshot validation failed: $($_.Exception.GetType().Name)"
        }
    }
}
if (-not [string]::IsNullOrWhiteSpace($windowScreenshotPath)) {
    if (-not (Test-Path -LiteralPath $windowScreenshotPath)) {
        $captureBindingFailures += "GUI smoke completed but did not write window screenshot: $windowScreenshotPath"
    }
    else {
        try {
            $windowScreenshotItem = Get-Item -LiteralPath $windowScreenshotPath
            $windowScreenshotImage = Get-ScreenshotImageMetadata -Path $windowScreenshotPath
            $windowScreenshotCapture = [pscustomobject]@{
                outputPath = $windowScreenshotItem.FullName
                takenAtUtc = $windowScreenshotItem.LastWriteTimeUtc.ToString("o")
                method = "app-internal-window-grab"
                length = $windowScreenshotItem.Length
                image = $windowScreenshotImage
            }
            if (-not [string]::IsNullOrWhiteSpace($fpsStatusCropPath)) {
                $fpsStatusCropCapture = New-FpsStatusProofCrop `
                    -SourcePath $windowScreenshotPath `
                    -OutputPath $fpsStatusCropPath
            }
        }
        catch {
            # round 8 (fable minor 2): type name only, same rationale as the screenshot catch above.
            $captureBindingFailures += "GUI smoke window-screenshot validation failed: $($_.Exception.GetType().Name)"
        }
    }
}

$logFile = Get-ChildItem -LiteralPath $logRoot -Filter "mlvapp-*.log" |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1

$recentLines = @()
if ($logFile) {
    $since = $startUtc.AddSeconds(-2)
    foreach ($line in Get-Content -LiteralPath $logFile.FullName) {
        $timestamp = Get-LogTimestampUtc -Line $line
        if ($timestamp -and $timestamp -ge $since) {
            $recentLines += $line
        }
    }
}

# Preserve the exact lines consumed by this result in a per-run immutable
# snapshot.  The aggregate rotating app log is allowed to grow later and is
# therefore diagnostic only; comparison authority comes from this snapshot.
$runLogSnapshotPath = "$outputPath.run.log"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($runLogSnapshotPath, [string[]]$recentLines, $utf8NoBom)
$runLogSnapshotBinding = Get-EvidenceFileBinding `
    -Path $runLogSnapshotPath -Label "run log snapshot"

$runMetadataLine = $recentLines |
    Where-Object { $_ -like "*run_metadata=*" -and $_ -like "*--gui-smoke-playback*" } |
    Select-Object -Last 1
$playbackStartLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.start*" } |
    Select-Object -Last 1
$summaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.summary*" } |
    Select-Object -Last 1
$cpuSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.cpu_summary *" } |
    Select-Object -Last 1
$gpuSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.gpu_summary*" } |
    Select-Object -Last 1
# Round-4: fields that used to sit beyond QString::arg's %99 limit (garbage)
# now arrive on a continuation line; merged into the same cpuSummary object.
$cpuSummaryExtLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.cpu_summary_ext*" } |
    Select-Object -Last 1
$stageSplitSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.stage_split_summary*" } |
    Select-Object -Last 1
$processingDetailSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.processing_detail_summary*" } |
    Select-Object -Last 1
$debayerDetailSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.debayer_detail_summary*" } |
    Select-Object -Last 1
$rbfDetailSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.rbf_detail_summary*" } |
    Select-Object -Last 1
$dualIsoSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.dual_iso_full20_summary*" } |
    Select-Object -Last 1
$dualIsoMixChromaSummaryLine = $recentLines |
    Where-Object { $_ -like "*playback_smoke.dual_iso_mix_chroma_summary*" } |
    Select-Object -Last 1
$lookAssistSettleLine = $recentLines |
    Where-Object { $_ -like "*gui_smoke.look_assist_settle*" } |
    Select-Object -Last 1
$lookAssistApplyLine = $recentLines |
    Where-Object { $_ -like "*look_assist.apply.result*" } |
    Select-Object -Last 1
# Since the async Look Assist apply (master 8ddddce2), the default path finishes with
# look_assist.apply.auto_wb_async_applied and never emits look_assist.apply.result
# (that event is sync-fallback-only). Accept the async terminal event as apply
# evidence; the scene then comes from the settle line, which fires only after the
# diagnostics turned valid.
$lookAssistAsyncApplyLine = $recentLines |
    Where-Object { $_ -like "*look_assist.apply.auto_wb_async_applied*" } |
    Select-Object -Last 1
# RULE 2026-06-27: attribute the safety guard to the MEASURED ANALYSIS apply (the one that produced
# visual_state), NOT a later frame=0 PLAYBACK reapply. The Look Assist analysis runs before playback
# starts, so scan only the lines BEFORE the first play.toggled.begin. Taking the global last
# safety_fallback conflated the playback reapply's warning (auto_wb valid=0, output-neutral) with the
# measured analysis, which made lookassist-wb-determinism.ps1 read sync as falsely UNSTABLE
# (codex.md SEQ182 / claude.md SEQ37).
$playStartIdx = -1
for ($i = 0; $i -lt $recentLines.Count; $i++) {
    if ($recentLines[$i] -like "*play.toggled.begin*") { $playStartIdx = $i; break }
}
$safetyScanLines = if ($playStartIdx -gt 0) { $recentLines[0..($playStartIdx - 1)] }
                   elseif ($playStartIdx -eq 0) { @() }
                   else { $recentLines }
$lookAssistSafetyFallbackLine = $safetyScanLines |
    Where-Object { $_ -like "*look_assist.apply.safety_fallback*" } |
    Select-Object -Last 1
$visualStateLine = $recentLines |
    Where-Object { $_ -like "*gui_smoke.visual_state*" } |
    Select-Object -Last 1
$playbackPolicyLine = $recentLines |
    Where-Object { $_ -like "*gui_smoke.playback_policy*" } |
    Select-Object -Last 1
$cpuSettleLine = $recentLines |
    Where-Object { $_ -like "*gui_smoke.cpu_settle*" } |
    Select-Object -Last 1
$clipLifecycleStressLine = $recentLines |
    Where-Object { $_ -like "*gui_smoke.clip_lifecycle_stress*" } |
    Select-Object -Last 1
$windowScreenshotLine = $recentLines |
    Where-Object { $_ -like "*gui_smoke.window_screenshot*" } |
    Select-Object -Last 1
$screenshotLine = $recentLines |
    Where-Object { $_ -like "*gui_smoke.screenshot*" } |
    Select-Object -Last 1
$glProbeLines = @($recentLines |
    Where-Object { $_ -like "*playback_smoke.gl_probe*" })

$runMetadata = $null
if ($runMetadataLine -and $runMetadataLine -match 'run_metadata=(?<json>\{.*\})') {
    $runMetadata = $Matches["json"] | ConvertFrom-Json
}
if ($null -eq $runMetadata) {
    $captureBindingFailures += "Run metadata was missing from the per-run log snapshot."
}
else {
    if ([string](Get-ObjectPropertyValue $runMetadata "gui_smoke_run_nonce") -ne $runNonce) {
        $captureBindingFailures += "Run metadata nonce did not match the launcher nonce."
    }
    if ([int64](Get-ObjectPropertyValue $runMetadata "process_id") -ne [int64]$process.Id) {
        $captureBindingFailures += "Run metadata process id did not match the launched process."
    }
}

$playbackStart = if ($playbackStartLine) { Convert-PlaybackLogLineToObject $playbackStartLine } else { $null }
$playbackSummary = if ($summaryLine) { Convert-PlaybackLogLineToObject $summaryLine } else { $null }
$cpuSummary = if ($cpuSummaryLine) { Convert-PlaybackLogLineToObject $cpuSummaryLine } else { $null }
$gpuSummary = if ($gpuSummaryLine) { Convert-PlaybackLogLineToObject $gpuSummaryLine } else { $null }
if ($cpuSummary -and $cpuSummaryExtLine) {
    $cpuSummaryExt = Convert-PlaybackLogLineToObject $cpuSummaryExtLine
    if ($cpuSummaryExt) {
        foreach ($p in $cpuSummaryExt.PSObject.Properties) {
            if ($p.Name -ne 'session') {
                $cpuSummary | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force
            }
        }
    }
}
$stageSplitSummary = if ($stageSplitSummaryLine) { Convert-PlaybackLogLineToObject $stageSplitSummaryLine } else { $null }
$processingDetailSummary = if ($processingDetailSummaryLine) { Convert-PlaybackLogLineToObject $processingDetailSummaryLine } else { $null }
$debayerDetailSummary = if ($debayerDetailSummaryLine) { Convert-PlaybackLogLineToObject $debayerDetailSummaryLine } else { $null }
$rbfDetailSummary = if ($rbfDetailSummaryLine) { Convert-PlaybackLogLineToObject $rbfDetailSummaryLine } else { $null }
$dualIsoFull20Summary = if ($dualIsoSummaryLine) { Convert-PlaybackLogLineToObject $dualIsoSummaryLine } else { $null }
$dualIsoMixChromaSummary = if ($dualIsoMixChromaSummaryLine) { Convert-PlaybackLogLineToObject $dualIsoMixChromaSummaryLine } else { $null }
$lookAssistSettle = if ($lookAssistSettleLine) { Convert-PlaybackLogLineToObject $lookAssistSettleLine } else { $null }
$lookAssistApply = if ($lookAssistApplyLine) { Convert-PlaybackLogLineToObject $lookAssistApplyLine } else { $null }
$lookAssistAsyncApply = if ($lookAssistAsyncApplyLine) { Convert-PlaybackLogLineToObject $lookAssistAsyncApplyLine } else { $null }
$lookAssistSafetyFallback = if ($lookAssistSafetyFallbackLine) { Convert-PlaybackLogLineToObject $lookAssistSafetyFallbackLine } else { $null }
$visualState = if ($visualStateLine) { Convert-PlaybackLogLineToObject $visualStateLine } else { $null }
$playbackPolicy = if ($playbackPolicyLine) { Convert-PlaybackLogLineToObject $playbackPolicyLine } else { $null }
$cpuSettle = if ($cpuSettleLine) { Convert-PlaybackLogLineToObject $cpuSettleLine } else { $null }
$clipLifecycleStress = if ($clipLifecycleStressLine) { Convert-PlaybackLogLineToObject $clipLifecycleStressLine } else { $null }
$windowScreenshotLog = if ($windowScreenshotLine) { Convert-PlaybackLogLineToObject $windowScreenshotLine } else { $null }
$screenshotLog = if ($screenshotLine) { Convert-PlaybackLogLineToObject $screenshotLine } else { $null }
$screenshotProvenance = if ($RequireFreshScreenshotRender) {
    Get-GuiSmokeScreenshotProvenance `
        -OrderedLogLines $recentLines `
        -RequestedStartFrame $StartFrame
} else {
    $null
}
if ($screenshotCapture -and $screenshotLog -and (Get-ObjectPropertyValue $screenshotLog "method")) {
    $screenshotCapture.method = [string](Get-ObjectPropertyValue $screenshotLog "method")
    $screenshotCapture | Add-Member -NotePropertyName frame `
        -NotePropertyValue (Get-ObjectPropertyValue $screenshotLog "frame") -Force
    $screenshotCapture | Add-Member -NotePropertyName serial `
        -NotePropertyValue (Get-ObjectPropertyValue $screenshotLog "serial") -Force
    $screenshotCapture | Add-Member -NotePropertyName generation `
        -NotePropertyValue (Get-ObjectPropertyValue $screenshotLog "generation") -Force
    $screenshotCapture | Add-Member -NotePropertyName eventSha256 `
        -NotePropertyValue (Get-ObjectPropertyValue $screenshotLog "sha256") -Force
    $screenshotCapture | Add-Member -NotePropertyName eventLength `
        -NotePropertyValue (Get-ObjectPropertyValue $screenshotLog "bytes") -Force
    if ([string]$screenshotCapture.image.sha256 -ne [string]$screenshotCapture.eventSha256 -or
        [int64]$screenshotCapture.length -ne [int64]$screenshotCapture.eventLength -or
        [int]$screenshotCapture.image.width -ne [int](Get-ObjectPropertyValue $screenshotLog "width") -or
        [int]$screenshotCapture.image.height -ne [int](Get-ObjectPropertyValue $screenshotLog "height")) {
        $captureBindingFailures += "Screenshot bytes/dimensions do not match the app capture event."
    }
}
elseif ($screenshotCapture) {
    $captureBindingFailures += "Screenshot capture event was missing."
}
if ($RequireFreshScreenshotRender -and $screenshotCapture) {
    $screenshotCapture | Add-Member `
        -NotePropertyName provenance `
        -NotePropertyValue $screenshotProvenance `
        -Force
}
if ($RequireFreshScreenshotRender -and $screenshotCapture) {
    $screenshotCapture | Add-Member `
        -NotePropertyName provenance `
        -NotePropertyValue $screenshotProvenance `
        -Force
}
$windowScreenshotFpsStatusText = Get-ObjectPropertyValue $windowScreenshotLog "fps_status"
$windowScreenshotFpsStatusValue = Convert-FpsStatusTextToValue $windowScreenshotFpsStatusText
$requestedPlaybackDurationMs = [int]([Math]::Max(100, $Seconds * 1000))
$sustainedGuiFpsSample =
    "end-of-requested-duration window screenshot after ${Seconds}s playback"

$glProbeObjects = @($glProbeLines | ForEach-Object { Convert-PlaybackLogLineToObject $_ })
$glProbeActiveObjects = @($glProbeObjects | Where-Object {
    [int](Get-ObjectPropertyValue $_ "active") -eq 1
})
$glTextureHashes = @($glProbeActiveObjects | ForEach-Object {
    [string](Get-ObjectPropertyValue $_ "texture_hash")
} | Where-Object { $_ -and $_ -ne "none" })
$glRendererDescriptions = @($glProbeActiveObjects | ForEach-Object {
    [string](Get-ObjectPropertyValue $_ "renderer")
} | Where-Object { $_ -and $_ -ne "none" } | Select-Object -Unique)
$glBackendResolvedPaths = @($glProbeActiveObjects | ForEach-Object {
    [string](Get-ObjectPropertyValue $_ "backend_resolved_path")
} | Where-Object { $_ -and $_ -ne "none" } | Select-Object -Unique)
$glBackendDescriptions = @($glProbeActiveObjects | ForEach-Object {
    [string](Get-ObjectPropertyValue $_ "backend_description")
} | Where-Object { $_ -and $_ -ne "none" } | Select-Object -Unique)
$glTextureReadbackOkCount = @($glProbeActiveObjects | Where-Object {
    [int](Get-ObjectPropertyValue $_ "texture_readback_ok") -eq 1
}).Count
$glParityCheckedCount = @($glProbeActiveObjects | Where-Object {
    [int](Get-ObjectPropertyValue $_ "parity_checked") -eq 1
}).Count
$glParityMatchCount = @($glProbeActiveObjects | Where-Object {
    [int](Get-ObjectPropertyValue $_ "parity_match") -eq 1
}).Count
$glMismatchTotal = 0L
$glMismatchMaxAbs = 0L
foreach ($probe in $glProbeActiveObjects) {
    $mismatchCount = [long](Get-ObjectPropertyValue $probe "mismatch_count")
    $mismatchMaxAbs = [long](Get-ObjectPropertyValue $probe "mismatch_max_abs")
    $glMismatchTotal += $mismatchCount
    if ($mismatchMaxAbs -gt $glMismatchMaxAbs) {
        $glMismatchMaxAbs = $mismatchMaxAbs
    }
}
$glOutputValidationRequested =
    $launchEnv.Contains("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT") -and
    ([string]$launchEnv["MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT"]) -ne "0"
$glOutputProof = [pscustomobject]@{
    requested = [bool]$glOutputValidationRequested
    probeCount = @($glProbeObjects).Count
    activeProbeCount = @($glProbeActiveObjects).Count
    textureReadbackOkCount = $glTextureReadbackOkCount
    parityCheckedCount = $glParityCheckedCount
    parityMatchCount = $glParityMatchCount
    mismatchCountTotal = $glMismatchTotal
    mismatchMaxAbs = $glMismatchMaxAbs
    distinctTextureHashes = @($glTextureHashes | Select-Object -Unique).Count
    textureHashes = @($glTextureHashes | Select-Object -Unique)
    rendererDescriptions = $glRendererDescriptions
    backendResolvedPaths = $glBackendResolvedPaths
    backendDescriptions = $glBackendDescriptions
    screenshotMethod = if ($screenshotCapture) { $screenshotCapture.method } else { $null }
    rawProbeLines = $glProbeLines
}

$lookAssistApplied =
    ($null -ne $lookAssistSettle) -and
    ($lookAssistSettle.enabled -eq 1) -and
    ($lookAssistSettle.diagnostics_valid -eq 1) -and
    ( ( ($null -ne $lookAssistApply) -and
        (-not [string]::IsNullOrWhiteSpace([string]$lookAssistApply.scene)) ) -or
      ( ($null -ne $lookAssistAsyncApply) -and
        (-not [string]::IsNullOrWhiteSpace([string]$lookAssistSettle.scene)) ) -or
      ( ($null -ne $lookAssistSafetyFallback) -and
        (-not [string]::IsNullOrWhiteSpace([string]$lookAssistSettle.scene)) ) )

$scaleRequestStart = Get-ObjectPropertyValue $playbackStart "scale_request"
$scaleRequestLast = Get-ObjectPropertyValue $playbackSummary "scale_request_last"
$scaleActiveLast = Get-ObjectPropertyValue $playbackSummary "scale_active_last"
$qualityModeStart = Get-ObjectPropertyValue $playbackStart "quality_mode"
$qualityModeLast = Get-ObjectPropertyValue $playbackSummary "quality_mode"
$presentedFrames = Get-ObjectPropertyValue $playbackSummary "presented_frames"
$firstPresentedFrame = Get-ObjectPropertyValue $playbackSummary "first_presented_frame"
$lastPresentedFrame = Get-ObjectPropertyValue $playbackSummary "last_presented_frame"
$timelineDeltaAbs = Get-ObjectPropertyValue $playbackSummary "timeline_delta_abs"
$skippedOrUnpresentedFrames = Get-ObjectPropertyValue $playbackSummary "skipped_or_unpresented_frames"
$skippedOrUnpresentedRatio = $null
if ($null -ne $timelineDeltaAbs -and [double]$timelineDeltaAbs -gt 0 -and
    $null -ne $skippedOrUnpresentedFrames) {
    $skippedOrUnpresentedRatio = [double]$skippedOrUnpresentedFrames / [double]$timelineDeltaAbs
}
$validatedScaleRequest = if ($null -ne $scaleRequestLast) { $scaleRequestLast } else { $scaleRequestStart }
$validatedQualityMode = if ($null -ne $qualityModeLast) { $qualityModeLast } else { $qualityModeStart }
$autoDecision = [pscustomobject]@{
    expectedAuto = ($ExpectedQualityMode -eq 2)
    source = if ($null -ne $playbackSummary) { "playback_smoke.summary" } else { $null }
    targetFps = Get-ObjectPropertyValue $playbackSummary "auto_target_fps"
    reason = Get-ObjectPropertyValue $playbackSummary "auto_reason_last"
    averageMs = Get-ObjectPropertyValue $playbackSummary "auto_avg_ms"
    budgetMs = Get-ObjectPropertyValue $playbackSummary "auto_budget_ms"
    sampleCount = Get-ObjectPropertyValue $playbackSummary "auto_sample_count"
    averageFpsEquivalent = Get-ObjectPropertyValue $playbackSummary "auto_avg_fps_equivalent"
    budgetFpsEquivalent = Get-ObjectPropertyValue $playbackSummary "auto_budget_fps_equivalent"
    headroomCapabilityLast = Get-ObjectPropertyValue $playbackSummary "auto_headroom_capability_last"
    validatedNoReadbackCapabilityObserved =
        Get-ObjectPropertyValue $playbackSummary "auto_validated_no_readback_capability_observed"
    validatedNoReadbackCapabilityDemotedLast =
        Get-ObjectPropertyValue $playbackSummary "auto_validated_no_readback_capability_demoted_last"
}
$autoDecisionFieldsPresent =
    ($null -ne $autoDecision.targetFps) -and
    ($null -ne $autoDecision.reason) -and
    ($null -ne $autoDecision.averageMs) -and
    ($null -ne $autoDecision.budgetMs) -and
    ($null -ne $autoDecision.sampleCount) -and
    ($null -ne $autoDecision.averageFpsEquivalent) -and
    ($null -ne $autoDecision.budgetFpsEquivalent) -and
    ($null -ne $autoDecision.headroomCapabilityLast) -and
    ($null -ne $autoDecision.validatedNoReadbackCapabilityObserved) -and
    ($null -ne $autoDecision.validatedNoReadbackCapabilityDemotedLast)
$autoDecision | Add-Member -NotePropertyName fieldsPresent -NotePropertyValue ([bool]$autoDecisionFieldsPresent)
$autoHeadroomCapabilityLast = Convert-ToNullableBool $autoDecision.headroomCapabilityLast
$autoValidatedNoReadbackObserved =
    Convert-ToNullableBool $autoDecision.validatedNoReadbackCapabilityObserved
$autoValidatedNoReadbackDemoted =
    Convert-ToNullableBool $autoDecision.validatedNoReadbackCapabilityDemotedLast
$autoDecisionCapabilityFailures = @()
if ($autoDecisionFieldsPresent) {
    if ($autoHeadroomCapabilityLast -eq $true -and
        $autoValidatedNoReadbackObserved -ne $true) {
        $autoDecisionCapabilityFailures += "Auto headroom capability was true without validated no-readback capability observed."
    }
    if ($autoValidatedNoReadbackObserved -eq $true -and
        $autoValidatedNoReadbackDemoted -eq $true) {
        $autoDecisionCapabilityFailures += "Auto no-readback capability was both observed and demoted in the same summary."
    }
    if ([string]$autoDecision.reason -eq "headroom_non_dual_iso_sharper_hq" -and
        ($autoHeadroomCapabilityLast -ne $true -or
         $autoValidatedNoReadbackObserved -ne $true -or
         $autoValidatedNoReadbackDemoted -eq $true)) {
        $autoDecisionCapabilityFailures += "Auto sharpened for headroom without an active validated no-readback capability latch."
    }
}
$autoDecision | Add-Member -NotePropertyName headroomCapabilityLastBool `
    -NotePropertyValue $autoHeadroomCapabilityLast
$autoDecision | Add-Member -NotePropertyName validatedNoReadbackCapabilityObservedBool `
    -NotePropertyValue $autoValidatedNoReadbackObserved
$autoDecision | Add-Member -NotePropertyName validatedNoReadbackCapabilityDemotedLastBool `
    -NotePropertyValue $autoValidatedNoReadbackDemoted
$autoDecision | Add-Member -NotePropertyName capabilityConsistent `
    -NotePropertyValue ($autoDecisionCapabilityFailures.Count -eq 0)
$autoDecision | Add-Member -NotePropertyName capabilityFailures `
    -NotePropertyValue $autoDecisionCapabilityFailures

$cpuSettled = $true
if ($SettleCpuMaxMs -gt 0 -or $SettleCpuStableMs -gt 0) {
    $cpuSettled = $false
    if ($cpuSettle) {
        $cpuSettled = ($cpuSettle.settled -eq 1)
    }
}
$colorArtifactFailureVerdicts = Get-ColorArtifactFailingVerdicts
$colorArtifactVerdict =
    if ($null -ne $colorArtifactScan) { [string]$colorArtifactScan.verdict } else { "not-captured" }
$colorArtifactScanPassed =
    -not ($colorArtifactFailureVerdicts -contains $colorArtifactVerdict)

$validationFailures = @()
$validationFailures += $captureBindingFailures
if ($processBoundary.timedOut) {
    $validationFailures += "MLVApp exceeded its fail-closed process timeout and was terminated."
}
if ($processExitCode -ne 0) {
    $validationFailures += "MLVApp exited with effective code $processExitCode."
}
if ($RequireLookAssist -and -not $lookAssistApplied) {
    $validationFailures += "Look Assist did not settle/apply before playback."
}
if ($null -eq $visualState) {
    $validationFailures += "GUI visual state telemetry was missing."
}
if ($RequireLookAssist -and
    $null -ne $visualState -and
    ([int](Get-ObjectPropertyValue $visualState "look_assist_enabled") -ne 1 -or
     [int](Get-ObjectPropertyValue $visualState "look_assist_diagnostics_valid") -ne 1)) {
    $validationFailures += "GUI visual state did not have settled Look Assist enabled."
}
if ($DisableLookAssist -and
    $null -ne $visualState -and
    [int](Get-ObjectPropertyValue $visualState "look_assist_enabled") -ne 0) {
    $validationFailures += "GUI visual state did not have Look Assist disabled."
}
if ($RequireCpuSettled -and -not $cpuSettled) {
    $validationFailures += "CPU did not settle before playback."
}
if ($RequireCpuSettled -and
    $preLaunchSystemCpuSettle.requested -and
    -not $preLaunchSystemCpuSettle.settled) {
    $validationFailures += "System CPU did not settle before launching MLVApp."
}
if (-not $LaunchOnlyProbe) {
    if ($null -eq $presentedFrames) {
        $validationFailures += "Playback summary did not report presented_frames."
    }
    elseif ([int]$presentedFrames -lt 2) {
        $validationFailures += "Playback-quality evidence requires at least 2 presented frames; observed $presentedFrames."
    }
    if ($null -eq $firstPresentedFrame -or $null -eq $lastPresentedFrame) {
        $validationFailures += "Playback summary did not report first and last presented frame ids."
    }
    elseif ([int64]$firstPresentedFrame -eq [int64]$lastPresentedFrame) {
        $validationFailures += "Displayed playback did not advance: first and last presented frame are both $firstPresentedFrame."
    }
    if ($null -eq $skippedOrUnpresentedRatio) {
        $validationFailures += "Playback summary could not establish a skipped/unpresented-frame ratio."
    }
    elseif ($skippedOrUnpresentedRatio -gt $MaxSkippedOrUnpresentedRatio) {
        $validationFailures += ("Skipped/unpresented-frame ratio {0:P2} exceeds the playback-quality limit {1:P2}." -f
            $skippedOrUnpresentedRatio, $MaxSkippedOrUnpresentedRatio)
    }
}
if ($FailOnColorArtifact -and
    $null -ne $colorArtifactScan -and
    -not $colorArtifactScanPassed) {
    $validationFailures += "Color artifact scan verdict was $colorArtifactVerdict."
}
if ($RequireFreshScreenshotRender -and
    ($null -eq $screenshotProvenance -or -not $screenshotProvenance.validFresh)) {
    $provenanceFailures = if ($screenshotProvenance) {
        $screenshotProvenance.failures -join ', '
    } else {
        'missing-provenance'
    }
    $validationFailures += "Screenshot fresh-render provenance failed: $provenanceFailures."
}
if ($glOutputValidationRequested) {
    if ($glOutputProof.activeProbeCount -le 0) {
        $validationFailures += "GL no-readback output validation was requested but no active playback_smoke.gl_probe rows were logged."
    }
    if ($glOutputProof.activeProbeCount -gt 0 -and
        $glOutputProof.textureReadbackOkCount -ne $glOutputProof.activeProbeCount) {
        $validationFailures += "GL texture readback did not succeed for every active probe ($($glOutputProof.textureReadbackOkCount)/$($glOutputProof.activeProbeCount))."
    }
    if ($glOutputProof.activeProbeCount -gt 1 -and
        $glOutputProof.distinctTextureHashes -le 1) {
        $validationFailures += "GL no-readback texture content did not advance: distinctTextureHashes=$($glOutputProof.distinctTextureHashes)."
    }
    if ($glOutputProof.activeProbeCount -gt 0 -and
        $glOutputProof.parityCheckedCount -ne $glOutputProof.activeProbeCount) {
        $validationFailures += "GL texture parity was not checked for every active probe ($($glOutputProof.parityCheckedCount)/$($glOutputProof.activeProbeCount))."
    }
    if ($glOutputProof.activeProbeCount -gt 0 -and
        $glOutputProof.parityMatchCount -ne $glOutputProof.activeProbeCount) {
        $validationFailures += "GL texture parity did not match the CPU oracle for every active probe ($($glOutputProof.parityMatchCount)/$($glOutputProof.activeProbeCount), mismatches=$($glOutputProof.mismatchCountTotal))."
    }
    if ($CaptureScreenshot -and
        $glOutputProof.activeProbeCount -gt 0 -and
        [string]$glOutputProof.screenshotMethod -notin @(
            "app_internal_gl_viewport_grab", "gl_window_framebuffer_readback")) {
        $validationFailures += "Screenshot color scan did not use a GL-presented surface (method=$($glOutputProof.screenshotMethod))."
    }
}
$gpuTextureRouteReadbackRegressionFailure =
    Get-GuiSmokeGpuTextureRouteReadbackRegressionFailure -LaunchEnv $launchEnv `
        -ClearedEnvironment $clearedEnvironment -GpuSummary $gpuSummary `
        -AmbientEnvironment ([System.Environment]::GetEnvironmentVariables())
if ($null -ne $gpuTextureRouteReadbackRegressionFailure) {
    $validationFailures += $gpuTextureRouteReadbackRegressionFailure
}
if ($ExpectedScaleRequest -ge 0 -and
    ($null -eq $validatedScaleRequest -or [int]$validatedScaleRequest -ne $ExpectedScaleRequest)) {
    $validationFailures += "Playback scale request was $validatedScaleRequest; expected $ExpectedScaleRequest."
}
if ($effectiveExpectedVisualScaleRequest -ge 0 -and
    $null -ne $visualState -and
    [int](Get-ObjectPropertyValue $visualState "scale_request") -ne $effectiveExpectedVisualScaleRequest) {
    $validationFailures += "GUI visual state scale request was $(Get-ObjectPropertyValue $visualState "scale_request"); expected $effectiveExpectedVisualScaleRequest."
}
if ($ExpectedQualityMode -ge 0 -and
    ($null -eq $validatedQualityMode -or [int]$validatedQualityMode -ne $ExpectedQualityMode)) {
    $validationFailures += "Quality mode was $validatedQualityMode; expected $ExpectedQualityMode."
}
if ($ExpectedQualityMode -ge 0 -and
    $null -ne $visualState -and
    [int](Get-ObjectPropertyValue $visualState "quality_mode") -ne $ExpectedQualityMode) {
    $validationFailures += "GUI visual state quality mode was $(Get-ObjectPropertyValue $visualState "quality_mode"); expected $ExpectedQualityMode."
}
if ($ExpectedQualityMode -eq 2 -and -not $autoDecisionFieldsPresent -and -not $LegacyGuiSmokeOptions) {
    $validationFailures += "Auto playback quality telemetry was missing from playback_smoke.summary."
}
elseif ($ExpectedQualityMode -eq 2 -and -not $autoDecisionFieldsPresent -and $LegacyGuiSmokeOptions) {
    $validationWarnings += "Pinned legacy build predates auto-decision telemetry; quality_mode=2 remains verified from visual-state and summary telemetry."
}
if ($ExpectedQualityMode -eq 2 -and $autoDecisionFieldsPresent) {
    $validationFailures += $autoDecisionCapabilityFailures
}

$screenshotAspectEvidence = if ($CaptureScreenshot) {
    New-ScreenshotAspectEvidence `
        -ImageMetadata (Get-ObjectPropertyValue $screenshotCapture "image") `
        -VisualState $visualState
} else {
    $null
}

$expectedStretchXValue = if ([string]::IsNullOrWhiteSpace($ExpectedStretchX)) { $null } else { Convert-ToNullableDouble $ExpectedStretchX }
$expectedStretchYValue = if ([string]::IsNullOrWhiteSpace($ExpectedStretchY)) { $null } else { Convert-ToNullableDouble $ExpectedStretchY }
if (-not [string]::IsNullOrWhiteSpace($ExpectedStretchX) -and $null -eq $expectedStretchXValue) {
    $validationFailures += "ExpectedStretchX '$ExpectedStretchX' could not be parsed as a number."
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedStretchY) -and $null -eq $expectedStretchYValue) {
    $validationFailures += "ExpectedStretchY '$ExpectedStretchY' could not be parsed as a number."
}
if ($null -ne $expectedStretchXValue -and
    ($null -eq $screenshotAspectEvidence -or
     $null -eq $screenshotAspectEvidence.stretchX -or
     [Math]::Abs([double]$screenshotAspectEvidence.stretchX - [double]$expectedStretchXValue) -gt $ExpectedStretchTolerance)) {
    $validationFailures += "Screenshot stretchX was $($screenshotAspectEvidence.stretchX); expected $expectedStretchXValue (+/- $ExpectedStretchTolerance)."
}
if ($null -ne $expectedStretchYValue -and
    ($null -eq $screenshotAspectEvidence -or
     $null -eq $screenshotAspectEvidence.stretchY -or
     [Math]::Abs([double]$screenshotAspectEvidence.stretchY - [double]$expectedStretchYValue) -gt $ExpectedStretchTolerance)) {
    $validationFailures += "Screenshot stretchY was $($screenshotAspectEvidence.stretchY); expected $expectedStretchYValue (+/- $ExpectedStretchTolerance)."
}
if ($ExpectedHStretchIndex -ge 0 -and
    ($null -eq $screenshotAspectEvidence -or
     $null -eq $screenshotAspectEvidence.hStretchIndex -or
     [int]$screenshotAspectEvidence.hStretchIndex -ne $ExpectedHStretchIndex)) {
    $validationFailures += "Screenshot hStretchIndex was $($screenshotAspectEvidence.hStretchIndex); expected $ExpectedHStretchIndex."
}
if ($ExpectedVStretchIndex -ge 0 -and
    ($null -eq $screenshotAspectEvidence -or
     $null -eq $screenshotAspectEvidence.vStretchIndex -or
     [int]$screenshotAspectEvidence.vStretchIndex -ne $ExpectedVStretchIndex)) {
    $validationFailures += "Screenshot vStretchIndex was $($screenshotAspectEvidence.vStretchIndex); expected $ExpectedVStretchIndex."
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedAspectMode) -and
    ($null -eq $screenshotAspectEvidence -or
     [string]$screenshotAspectEvidence.mode -ne $ExpectedAspectMode)) {
    $validationFailures += "Screenshot aspect mode was '$($screenshotAspectEvidence.mode)'; expected '$ExpectedAspectMode'."
}

# Temporal playback-artifact gate (flicker / stall / cadence jitter) parsed from the interactive trace.
$playbackArtifacts = $null
if ($DetectPlaybackArtifacts) {
    $detectorScript = Join-Path $root "tools/profiling/detect-playback-artifacts.ps1"
    $traceLogPath = if ($logFile) { $logFile.FullName } else { $null }
    if ((Test-Path -LiteralPath $detectorScript) -and $traceLogPath -and (Test-Path -LiteralPath $traceLogPath)) {
        $detectorPwsh = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
        if (-not $detectorPwsh) { $detectorPwsh = (Get-Command powershell.exe).Source }
        $detectorArgs = @("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-File", $detectorScript, "-TraceLog", $traceLogPath)
        if ($ArtifactCadenceAdvisory) { $detectorArgs += "-CadenceAdvisory" }
        $detectorOut = & $detectorPwsh @detectorArgs 2>&1
        $detectorExit = $LASTEXITCODE
        $detectorText = ($detectorOut | Out-String).Trim()
        $checkLine = ($detectorOut | Where-Object { $_ -match '^ARTIFACT-CHECK ' } | Select-Object -First 1)
        $getField = { param($text, $key) $m = [regex]::Match([string]$text, $key + '=([^\s]+)'); if ($m.Success) { $m.Groups[1].Value } else { $null } }
        $playbackArtifacts = [pscustomobject]@{
            enabled = $true
            cadenceAdvisory = [bool]$ArtifactCadenceAdvisory
            verdict = (& $getField $checkLine 'verdict')
            presents = [int](& $getField $checkLine 'presents')
            medianFps = [double](& $getField $checkLine 'median_fps')
            p90Ms = [int](& $getField $checkLine 'p90_ms')
            p99Ms = [int](& $getField $checkLine 'p99_ms')
            maxIntervalMs = [int](& $getField $checkLine 'max_interval_ms')
            hitchFraction = [double](& $getField $checkLine 'hitch_frac')
            flickerBackJumps = [int](& $getField $checkLine 'flicker_back_jumps')
            maxBackJump = [int](& $getField $checkLine 'max_back_jump')
            maxLag = [int](& $getField $checkLine 'max_lag')
            contentEvents = [int](& $getField $checkLine 'content_events')
            distinctHashes = [int](& $getField $checkLine 'distinct_hashes')
            frozenContentRuns = [int](& $getField $checkLine 'frozen_content_runs')
            longestFrozenRun = [int](& $getField $checkLine 'longest_frozen_run')
            detectorExitCode = $detectorExit
            traceLogPath = $traceLogPath
            detectorScript = $detectorScript
            detail = $detectorText
        }
    } else {
        $playbackArtifacts = [pscustomobject]@{
            enabled = $true
            verdict = "no-data"
            detail = "detector or trace log not found (detector=$detectorScript, trace=$traceLogPath)"
        }
    }
}

if ($ExerciseClipLifecycleStress) {
    if ($null -eq $clipLifecycleStress) {
        $validationFailures += "Clip lifecycle stress telemetry was missing."
    }
    elseif ((Get-ObjectPropertyValue $clipLifecycleStress "ok") -ne 1) {
        $stressFailure = Get-ObjectPropertyValue $clipLifecycleStress "failure"
        $validationFailures += "Clip lifecycle stress failed: $stressFailure."
    }
}

$result = [pscustomobject]@{
    schema = "mlvapp-gui-smoke-result.v2"
    capturedAtUtc = $endUtc.ToString("o")
    repoRoot = $root
    exePath = $exe
    clipPath = $inputPath
    inputBindings = $launchInputBindings
    outputPath = $outputPath
    evidence = [pscustomobject]@{
        runNonce = $runNonce
        inputs = $captureInputBindings
        runLogSnapshot = $runLogSnapshotBinding
    }
    launch = [pscustomobject]@{
        workingDirectory = $root
        arguments = $arguments
        environment = $launchEnv
        validationPolicy = [pscustomobject]@{
            requireLookAssist = $RequireLookAssist
            requireCpuSettled = $RequireCpuSettled
            failOnColorArtifact = [bool]$FailOnColorArtifact
            allowZeroPresentedFrames = [bool]$AllowZeroPresentedFrames
            launchOnlyProbe = [bool]$LaunchOnlyProbe
            maxSkippedOrUnpresentedRatio = $MaxSkippedOrUnpresentedRatio
            expectedScaleRequest = $ExpectedScaleRequest
            expectedVisualScaleRequest = $effectiveExpectedVisualScaleRequest
            expectedQualityMode = $ExpectedQualityMode
            expectedStretchX = $ExpectedStretchX
            expectedStretchY = $ExpectedStretchY
            expectedHStretchIndex = $ExpectedHStretchIndex
            expectedVStretchIndex = $ExpectedVStretchIndex
            expectedAspectMode = $ExpectedAspectMode
            expectedStretchTolerance = $ExpectedStretchTolerance
            usePersistedPlaybackSettings = [bool]$UsePersistedPlaybackSettings
            legacyGuiSmokeOptions = [bool]$LegacyGuiSmokeOptions
        }
        matchedUserShellDefaults = [pscustomobject]@{
            frameTelemetry = [bool]$FrameTelemetry
            rbfDetailTiming = [bool]$RbfDetailTiming
            playbackMaxThreads = if ($useAutoPlaybackThreads) { "auto/unset" } else { $Threads }
            scaleFactorUnsetUnlessRequested = [string]::IsNullOrWhiteSpace($ScaleFactor)
            preferHqMean23UnsetUnlessRequested = -not $PreferHqMean23
            experimentalEnvironmentCleared = -not $PreserveExperimentalEnvironment
            clearedEnvironment = $clearedEnvironment
            qtPlatformNotForced = $true
        }
        preLaunchSystemCpuSettle = $preLaunchSystemCpuSettle
    }
    visualQuality = [pscustomobject]@{
        scaleRequestStart = $scaleRequestStart
        scaleRequestLast = $scaleRequestLast
        scaleActiveLast = $scaleActiveLast
        qualityModeStart = $qualityModeStart
        qualityModeLast = $qualityModeLast
        autoDecision = $autoDecision
        visualState = $visualState
        playbackPolicy = $playbackPolicy
        aspectEvidence = $screenshotAspectEvidence
        colorArtifactScan = $colorArtifactScan
        glOutputProof = $glOutputProof
        lookAssist = [pscustomobject]@{
            applied = [bool]$lookAssistApplied
            asyncApplied = [bool]($null -ne $lookAssistAsyncApply)
            asyncDecision = Get-ObjectPropertyValue $lookAssistAsyncApply "decision"
            safetyFallback = [bool]($null -ne $lookAssistSafetyFallback)
            safetyWarning = Get-ObjectPropertyValue $lookAssistSafetyFallback "warning"
            safetyDecision = Get-ObjectPropertyValue $lookAssistSafetyFallback "decision"
            safetyFrame = Get-ObjectPropertyValue $lookAssistSafetyFallback "frame"
            safetyChromaSmooth = Get-ObjectPropertyValue $lookAssistSafetyFallback "chroma_smooth"
            safetyChromaAuto = Get-ObjectPropertyValue $lookAssistSafetyFallback "chroma_auto"
            safetyGreenRatio = Get-ObjectPropertyValue $lookAssistSafetyFallback "green_ratio"
            safetyGreenAxis = Get-ObjectPropertyValue $lookAssistSafetyFallback "green_axis"
            safetyVisibleGreenAxis = Get-ObjectPropertyValue $lookAssistSafetyFallback "visible_green_axis"
            enabled = Get-ObjectPropertyValue $lookAssistSettle "enabled"
            diagnosticsValid = Get-ObjectPropertyValue $lookAssistSettle "diagnostics_valid"
            waitMs = Get-ObjectPropertyValue $lookAssistSettle "wait_ms"
            analysis = Get-ObjectPropertyValue $lookAssistApply "analysis"
            scene = Get-ObjectPropertyValue $lookAssistApply "scene"
            median = Get-ObjectPropertyValue $lookAssistApply "median"
            p05 = Get-ObjectPropertyValue $lookAssistApply "p05"
            p95 = Get-ObjectPropertyValue $lookAssistApply "p95"
            p99 = Get-ObjectPropertyValue $lookAssistApply "p99"
            presetExposure = Get-ObjectPropertyValue $lookAssistApply "preset_exp"
            presetContrast = Get-ObjectPropertyValue $lookAssistApply "preset_contrast"
            presetPivot = Get-ObjectPropertyValue $lookAssistApply "preset_pivot"
            presetShadows = Get-ObjectPropertyValue $lookAssistApply "preset_shadows"
            presetHighlights = Get-ObjectPropertyValue $lookAssistApply "preset_highlights"
            presetVibrance = Get-ObjectPropertyValue $lookAssistApply "preset_vibrance"
            presetTemperatureDelta = Get-ObjectPropertyValue $lookAssistApply "preset_temp_delta"
            presetTintDelta = Get-ObjectPropertyValue $lookAssistApply "preset_tint_delta"
            finalTemperature = Get-ObjectPropertyValue $lookAssistApply "final_temp"
            finalTint = Get-ObjectPropertyValue $lookAssistApply "final_tint"
        }
        clipLifecycleStress = [pscustomobject]@{
            requested = [bool]$ExerciseClipLifecycleStress
            telemetry = $clipLifecycleStress
            ok = if ($clipLifecycleStress) { (Get-ObjectPropertyValue $clipLifecycleStress "ok") -eq 1 } else { $false }
            failure = Get-ObjectPropertyValue $clipLifecycleStress "failure"
        }
    }
    playbackFps = [pscustomobject]@{
        requestedPlaybackSeconds = $Seconds
        requestedPlaybackDurationMs = $requestedPlaybackDurationMs
        guiStatusText = Get-ObjectPropertyValue $playbackSummary "gui_fps_status_text"
        guiStatusValue = Get-ObjectPropertyValue $playbackSummary "gui_fps_status_value"
        guiStatusSample = "end-of-run playback summary; this can differ from the visible screenshot-time label"
        visibleBottomLeftGuiStatusText = $windowScreenshotFpsStatusText
        visibleBottomLeftGuiFps = $windowScreenshotFpsStatusValue
        visibleBottomLeftGuiFpsSample = $sustainedGuiFpsSample
        visibleBottomLeftGuiFpsSource = "gui_smoke.window_screenshot fps_status from screenshot.windowCapture"
        visibleBottomLeftGuiFpsTakenAtUtc = if ($windowScreenshotCapture) { $windowScreenshotCapture.takenAtUtc } else { $null }
        visibleBottomLeftGuiProofPath = $fpsStatusCropPath
        visibleBottomLeftGuiProof = $fpsStatusCropCapture
        sustainedBottomLeftGuiStatusText = $windowScreenshotFpsStatusText
        sustainedBottomLeftGuiFps = $windowScreenshotFpsStatusValue
        sustainedBottomLeftGuiFpsSample = $sustainedGuiFpsSample
        sustainedBottomLeftGuiFpsTakenAtUtc = if ($windowScreenshotCapture) { $windowScreenshotCapture.takenAtUtc } else { $null }
        sustainedBottomLeftGuiProofPath = $fpsStatusCropPath
        sustainedBottomLeftGuiProof = $fpsStatusCropCapture
        screenshotGuiStatusText = $windowScreenshotFpsStatusText
        screenshotGuiStatusValue = $windowScreenshotFpsStatusValue
        smokePresentedFps = Get-ObjectPropertyValue $playbackSummary "presented_fps"
        smokeTimelineFps = Get-ObjectPropertyValue $playbackSummary "timeline_fps"
        reportGuidance = "When citing bottom-left GUI FPS, cite sustainedBottomLeftGuiFps/visibleBottomLeftGuiFps and include the sustainedBottomLeftGuiProofPath/*-fps-status.png crop. For stable FPS, run long enough for playback to settle, for example -Seconds 30. Do not use the presented-frame screenshot as FPS proof because it intentionally omits GUI chrome."
        note = "sustainedBottomLeftGuiFps/visibleBottomLeftGuiFps/screenshotGuiStatusValue is the bottom-left Playback FPS label visible after the requested playback duration in screenshot.windowCapture and enlarged in playbackFps.sustainedBottomLeftGuiProof; guiStatusValue is the later end-of-run summary sample and can differ; smokePresentedFps and smokeTimelineFps are smoke-run telemetry over the full requested duration, and per-stage FPS-equivalent values are 1000 / stage_ms."
    }
    playbackArtifacts = $playbackArtifacts
    hostLoad = [pscustomobject]@{
        schema = "mlvapp-gui-smoke-host-load.v1"
        bar = [pscustomobject]@{ cpuLoadPercent = $HostLoadCpuPercentBar }
        before = $hostLoadBefore
        during = @($hostLoadDuringSamples)
        duringSampleIntervalMs = $HostLoadSampleIntervalMs
        # round 5 (sol MAJOR): duringSampleIntervalMs above is the REQUESTED cadence only, never
        # an upper bound -- observedMaxSampleGapMs is the actual max spacing between consecutive
        # sample timestamps (before/during/after), which is what Get-HostLoadVerdict's coverage
        # rule now checks against the declared cadence (see -ObservedMaxSampleGapMs).
        observedMaxSampleGapMs = $hostLoadObservedMaxSampleGapMs
        after = $hostLoadAfter
        maxCpuLoadPercent = $hostLoadVerdict.maxCpuLoadPercent
        state = $hostLoadVerdict.state
        provisional = [bool]$hostLoadVerdict.provisional
        reason = $hostLoadVerdict.reason
    }
    process = [pscustomobject]@{
        id = $process.Id
        exitCode = $processExitCode
        nativeExitCode = $processBoundary.nativeExitCode
        timedOut = [bool]$processBoundary.timedOut
        timeoutMs = [int]$processBoundary.timeoutMs
        treeKillAttempted = [bool]$processBoundary.treeKillAttempted
        treeKillSucceeded = [bool]$processBoundary.treeKillSucceeded
        terminationConfirmed = [bool]$processBoundary.terminationConfirmed
        stdoutDrained = [bool]$processBoundary.stdoutDrained
        stderrDrained = [bool]$processBoundary.stderrDrained
        startedAtUtc = $startUtc.ToString("o")
        endedAtUtc = $endUtc.ToString("o")
        stdout = $stdout.Trim()
        stderr = $stderr.Trim()
    }
    screenshot = [pscustomobject]@{
        requested = [bool]$CaptureScreenshot
        path = $screenshotPath
        capture = $screenshotCapture
        windowPath = $windowScreenshotPath
        windowCapture = $windowScreenshotCapture
        fpsStatusCropPath = $fpsStatusCropPath
        fpsStatusCrop = $fpsStatusCropCapture
        visibleBottomLeftGuiProofPath = $fpsStatusCropPath
        visibleBottomLeftGuiProof = $fpsStatusCropCapture
    }
    log = [pscustomobject]@{
        path = $runLogSnapshotPath
        aggregateSourcePath = if ($logFile) { $logFile.FullName } else { $null }
        runMetadata = $runMetadata
        playbackStart = $playbackStart
        summary = $playbackSummary
    cpuSummary = $cpuSummary
    stageSplitSummary = $stageSplitSummary
    processingDetailSummary = $processingDetailSummary
        debayerDetailSummary = $debayerDetailSummary
        rbfDetailSummary = $rbfDetailSummary
        dualIsoFull20Summary = $dualIsoFull20Summary
        dualIsoMixChromaSummary = $dualIsoMixChromaSummary
        lookAssistSettle = $lookAssistSettle
        lookAssistApply = $lookAssistApply
        lookAssistSafetyFallback = $lookAssistSafetyFallback
        visualState = $visualState
        playbackPolicy = $playbackPolicy
        cpuSettle = $cpuSettle
        clipLifecycleStress = $clipLifecycleStress
        screenshot = $screenshotCapture
        windowScreenshot = $windowScreenshotCapture
        windowScreenshotEvent = $windowScreenshotLog
        screenshotEvent = $screenshotLog
        fpsStatusCrop = $fpsStatusCropCapture
        raw = [pscustomobject]@{
            runMetadata = $runMetadataLine
            playbackStart = $playbackStartLine
            summary = $summaryLine
            cpuSummary = $cpuSummaryLine
            processingDetailSummary = $processingDetailSummaryLine
            debayerDetailSummary = $debayerDetailSummaryLine
            rbfDetailSummary = $rbfDetailSummaryLine
            dualIsoFull20Summary = $dualIsoSummaryLine
            dualIsoMixChromaSummary = $dualIsoMixChromaSummaryLine
            lookAssistSettle = $lookAssistSettleLine
            lookAssistApply = $lookAssistApplyLine
            lookAssistSafetyFallback = $lookAssistSafetyFallbackLine
            visualState = $visualStateLine
            playbackPolicy = $playbackPolicyLine
            cpuSettle = $cpuSettleLine
            clipLifecycleStress = $clipLifecycleStressLine
            screenshot = if ($screenshotCapture) { $screenshotCapture.outputPath } else { $null }
            screenshotEvent = $screenshotLine
            windowScreenshot = if ($windowScreenshotCapture) { $windowScreenshotCapture.outputPath } else { $null }
            fpsStatusCrop = if ($fpsStatusCropCapture) { $fpsStatusCropCapture.outputPath } else { $null }
            windowScreenshotEvent = $windowScreenshotLine
        }
    }
}

if ($DetectPlaybackArtifacts -and $playbackArtifacts -and $playbackArtifacts.verdict -eq "FAIL") {
    $validationFailures += ("Playback artifact detector verdict FAIL: " + (($playbackArtifacts.detail) -replace '\s+', ' '))
}

$result | Add-Member -NotePropertyName validation -NotePropertyValue ([pscustomobject]@{
    ok = ($validationFailures.Count -eq 0)
    lookAssistApplied = [bool]$lookAssistApplied
    cpuSettled = [bool]$cpuSettled
    systemCpuSettled = [bool]$preLaunchSystemCpuSettle.settled
    presentedFrames = $presentedFrames
    allowZeroPresentedFrames = [bool]$AllowZeroPresentedFrames
    launchOnlyProbe = [bool]$LaunchOnlyProbe
    firstPresentedFrame = $firstPresentedFrame
    lastPresentedFrame = $lastPresentedFrame
    displayedFrameAdvanced = ($null -ne $firstPresentedFrame -and
        $null -ne $lastPresentedFrame -and
        [int64]$firstPresentedFrame -ne [int64]$lastPresentedFrame)
    skippedOrUnpresentedRatio = $skippedOrUnpresentedRatio
    maxSkippedOrUnpresentedRatio = $MaxSkippedOrUnpresentedRatio
    colorArtifactScanPassed = [bool]$colorArtifactScanPassed
    colorArtifactScanVerdict = $colorArtifactVerdict
    glOutputProof = $glOutputProof
    autoDecisionRequired = ($ExpectedQualityMode -eq 2)
    autoDecisionFieldsPresent = [bool]$autoDecisionFieldsPresent
    autoDecisionCapabilityConsistent = ($autoDecisionCapabilityFailures.Count -eq 0)
    scaleRequestMatched = ($ExpectedScaleRequest -lt 0 -or
        ($null -ne $validatedScaleRequest -and [int]$validatedScaleRequest -eq $ExpectedScaleRequest))
    qualityModeMatched = ($ExpectedQualityMode -lt 0 -or
        ($null -ne $validatedQualityMode -and [int]$validatedQualityMode -eq $ExpectedQualityMode))
    aspectMatched =
        (($null -eq $expectedStretchXValue -or
          ($null -ne $screenshotAspectEvidence -and $null -ne $screenshotAspectEvidence.stretchX -and
           [Math]::Abs([double]$screenshotAspectEvidence.stretchX - [double]$expectedStretchXValue) -le $ExpectedStretchTolerance)) -and
         ($null -eq $expectedStretchYValue -or
          ($null -ne $screenshotAspectEvidence -and $null -ne $screenshotAspectEvidence.stretchY -and
           [Math]::Abs([double]$screenshotAspectEvidence.stretchY - [double]$expectedStretchYValue) -le $ExpectedStretchTolerance)) -and
         ($ExpectedHStretchIndex -lt 0 -or
          ($null -ne $screenshotAspectEvidence -and $null -ne $screenshotAspectEvidence.hStretchIndex -and
           [int]$screenshotAspectEvidence.hStretchIndex -eq $ExpectedHStretchIndex)) -and
         ($ExpectedVStretchIndex -lt 0 -or
          ($null -ne $screenshotAspectEvidence -and $null -ne $screenshotAspectEvidence.vStretchIndex -and
           [int]$screenshotAspectEvidence.vStretchIndex -eq $ExpectedVStretchIndex)) -and
         ([string]::IsNullOrWhiteSpace($ExpectedAspectMode) -or
          ($null -ne $screenshotAspectEvidence -and [string]$screenshotAspectEvidence.mode -eq $ExpectedAspectMode)))
    failures = $validationFailures
    warnings = $validationWarnings
    settledValidationRecommendedSeconds = $settledValidationRecommendedSeconds
})

if ($RequireFreshScreenshotRender) {
    $result.launch.validationPolicy | Add-Member `
        -NotePropertyName requireFreshScreenshotRender `
        -NotePropertyValue $true
    $result.log | Add-Member `
        -NotePropertyName screenshotProvenance `
        -NotePropertyValue $screenshotProvenance
    $result.validation | Add-Member `
        -NotePropertyName freshScreenshotRenderRequired `
        -NotePropertyValue $true
    $result.validation | Add-Member `
        -NotePropertyName freshScreenshotRenderValidated `
        -NotePropertyValue ($null -ne $screenshotProvenance -and $screenshotProvenance.validFresh)
    $result.validation | Add-Member `
        -NotePropertyName screenshotProvenance `
        -NotePropertyValue $screenshotProvenance
}

$result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $outputPath -Encoding UTF8
$result | ConvertTo-Json -Depth 8
$scriptExitCode = 0
if ($processExitCode -ne 0) {
    $scriptExitCode = $processExitCode
}
elseif ($validationFailures.Count -gt 0) {
    foreach ($failure in $validationFailures) {
        Write-Error $failure
    }
    $scriptExitCode = 2
}
}
finally {
    if ($smokeMutexAcquired) {
        $smokeMutex.ReleaseMutex()
        $smokeMutex.Dispose()
    }
}
exit $scriptExitCode
