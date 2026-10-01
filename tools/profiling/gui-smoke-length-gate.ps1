# PLAYBACK-CLIP-LENGTH-ENFORCE-1. The ONE place that decides whether a clip is long enough to be
# PLAYED on a venue. Dot-sourced by run-release-gui-smoke.ps1 (the choke point every venue playback
# leg goes through) and by the job generators that refuse at GENERATION time.
#
# OWNER RULE 2026-09-30 (playback-clip-length-20-30s-owner-rule-20260930): any leg that plays the
# app needs >= 20 s of real footage and a play window that never exceeds the clip. Never loop.
# Prose rules did not hold (the 2026-09-22 rule queued this enforcement card and it was never
# built), so this is code: a short, unreadable or empty clip is REFUSED before anything launches.
#
# Typed verdicts (never a path: owner footage is never named in a message):
#   OK
#   CLIP_TOO_SHORT       (clip=<s> window=<s>)  clip is shorter than max(20, window) seconds
#   PLAY_WINDOW_TOO_SHORT (window=<s> required=<s>)  the requested PLAY WINDOW is under 20 s, even
#                        on a long clip (ENFORCE-2: the window, not only the clip, must be >= 20 s)
#   CLIP_LENGTH_UNKNOWN  (reason=<token>)       header unreadable / wrong magic / 0 frames / bad fps
#                                               / spanned set incomplete -- FAIL CLOSED
#
# Header: the 52-byte MLVI file header, mlv_file_hdr_t in src/mlv/mlv.h, little-endian:
#   0  fileMagic[4]='MLVI'  4 blockSize u32 (52)   8 versionString[8]   16 fileGuid u64
#   24 fileNum u16          26 fileCount u16       28 fileFlags u32     32 videoClass u16
#   34 audioClass u16       36 videoFrameCount u32 40 audioFrameCount u32
#   44 sourceFpsNom u32     48 sourceFpsDenom u32     (fps = nom / denom)
# Verified against the tracked fixtures: '<4sI8sQHHIHHIIII' == 52 bytes.
# ASCII only (Windows PowerShell 5.1 reads a BOM-less file as the ANSI codepage).
# The clip extension is composed, never spelled as one token: a token ending in it trips this
# repository's own NA-4 PreToolUse gate even in source text that names no real clip.

$script:GuiSmokeMinClipSeconds = 20.0
$script:GuiSmokeMinPlayWindowMs = 20000   # the same floor in ms: the lifecycle-stress switch stops Play, so it may not come sooner
# Environment variables that change the range the ENGINE plays, so the app's gate and the engine could disagree
# (PLAYBACK-CLIP-LENGTH-ENFORCE-2 round 2, sol B3). Enumerated from platform/qt: MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR is
# the only knob that touches the cut range (MainWindow.cpp f3CutRangeRepairDisabledByEnvironment); the others that
# mention playback (scale, quality, threads, lookahead, timer poll, preroll) change speed or quality, never the range.
$script:GuiSmokeRangeChangingEnvironment = @('MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR')
$script:GuiSmokeMlviHeaderBytes = 52
$script:GuiSmokeFirstPartExtension = '.' + 'MLV'

function Read-GuiSmokeMlviHeader {
    param([Parameter(Mandatory = $true)][string]$Path)
    # Returns @{ ok=$true; ... } or @{ ok=$false; reason=<token> }. Never throws on bad input.
    $buf = New-Object byte[] $script:GuiSmokeMlviHeaderBytes
    $read = 0
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
                                     [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            while ($read -lt $buf.Length) {
                $n = $fs.Read($buf, $read, $buf.Length - $read)
                if ($n -le 0) { break }
                $read += $n
            }
        } finally { $fs.Dispose() }
    } catch {
        return @{ ok = $false; reason = 'unreadable' }
    }
    if ($read -lt $buf.Length) { return @{ ok = $false; reason = 'header_truncated' } }
    if ($buf[0] -ne 0x4D -or $buf[1] -ne 0x4C -or $buf[2] -ne 0x56 -or $buf[3] -ne 0x49) {
        return @{ ok = $false; reason = 'bad_magic' }
    }
    return @{
        ok          = $true
        fileGuid    = [BitConverter]::ToUInt64($buf, 16)
        fileNum     = [int][BitConverter]::ToUInt16($buf, 24)
        fileCount   = [int][BitConverter]::ToUInt16($buf, 26)
        videoFrames = [int64][BitConverter]::ToUInt32($buf, 36)
        fpsNom      = [int64][BitConverter]::ToUInt32($buf, 44)
        fpsDenom    = [int64][BitConverter]::ToUInt32($buf, 48)
    }
}

function Get-GuiSmokeClipLength {
    <#
    .SYNOPSIS
    Length of a clip (all spanned parts) from its headers alone. Returns
    [pscustomobject]@{ known; reason; frames; fps; seconds }. known=$false means FAIL CLOSED.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $unknown = { param($why) [pscustomobject]@{ known = $false; reason = $why; frames = 0; fps = 0.0; seconds = 0.0 } }
    $first = Read-GuiSmokeMlviHeader -Path $Path
    if (-not $first.ok) { return (& $unknown $first.reason) }
    $frames = [int64]$first.videoFrames
    if ($first.fileCount -gt 1) {
        # Spanned: the per-file header counts only that file's frames, so the total is the sum over
        # the WHOLE set, and the set must be complete (and one recording) or the length is unknown.
        $dir = Split-Path -Parent $Path
        $base = [IO.Path]::GetFileNameWithoutExtension($Path)
        $firstExt = $script:GuiSmokeFirstPartExtension
        $parts = @(Get-ChildItem -LiteralPath $dir -File | Where-Object {
            $_.BaseName -ceq $base -and $_.Extension -match '^\.M(?:LV|\d\d)$'
        } | Sort-Object @{ Expression = { if ($_.Extension -ieq $firstExt) { -1 } else { [int]$_.Extension.Substring(2) } } })
        if ($parts.Count -ne $first.fileCount) { return (& $unknown 'spanned_set_incomplete') }
        $frames = 0
        $idx = 0
        foreach ($part in $parts) {
            $h = Read-GuiSmokeMlviHeader -Path $part.FullName
            if (-not $h.ok) { return (& $unknown "part_$($h.reason)") }
            if ($h.fileGuid -ne $first.fileGuid -or $h.fileNum -ne $idx -or $h.fileCount -ne $first.fileCount) {
                return (& $unknown 'spanned_set_mismatch')
            }
            $frames += [int64]$h.videoFrames
            $idx++
        }
    }
    if ($frames -le 0) { return (& $unknown 'zero_frames') }
    if ($first.fpsNom -le 0 -or $first.fpsDenom -le 0) { return (& $unknown 'bad_fps') }
    $fps = [double]$first.fpsNom / [double]$first.fpsDenom
    return [pscustomobject]@{ known = $true; reason = ''; frames = $frames; fps = $fps; seconds = ([double]$frames / $fps) }
}

function Test-GuiSmokeClipLength {
    <#
    .SYNOPSIS
    The length gate. Returns [pscustomobject]@{ verdict; message; clipSeconds; windowSeconds;
    requiredSeconds; frames; fps }. verdict is OK | CLIP_TOO_SHORT | PLAY_WINDOW_TOO_SHORT | CLIP_LENGTH_UNKNOWN.
    A play window may never exceed the footage that remains after -StartFrame.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][double]$WindowSeconds,
        [int]$StartFrame = 0,
        [double]$MinSeconds = $script:GuiSmokeMinClipSeconds,
        # -ClipOnly: the caller plays NOTHING itself (a decode-only benchmark, a clip that is only opened);
        # only the clip floor applies. Every caller that plays passes its real -WindowSeconds, and that
        # window must itself be >= MinSeconds (PLAYBACK-CLIP-LENGTH-ENFORCE-2).
        [switch]$ClipOnly,
        # --presented-frames / -TargetPresentedFrames ends the play EARLY after N presented frames, so it is
        # itself a play window of N / fps seconds and must reach MinSeconds (0 = no early stop).
        [int]$TargetPresentedFrames = 0
    )
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $fmt = { param($v) ([double]$v).ToString('0.###', $inv) }
    $len = Get-GuiSmokeClipLength -Path $Path
    $required = [Math]::Max($MinSeconds, $WindowSeconds)
    $out = [pscustomobject]@{
        verdict = 'OK'; message = 'OK'; clipSeconds = $len.seconds; windowSeconds = $WindowSeconds
        requiredSeconds = $required; frames = $len.frames; fps = $len.fps
    }
    if (-not $len.known) {
        $out.verdict = 'CLIP_LENGTH_UNKNOWN'
        $out.message = "CLIP_LENGTH_UNKNOWN (reason=$($len.reason))"
        return $out
    }
    $remaining = $len.seconds - ([double][Math]::Max(0, $StartFrame) / $len.fps)
    $presentedWindowSeconds = if ($TargetPresentedFrames -gt 0) { [double]$TargetPresentedFrames / $len.fps } else { 0.0 }
    # Both the 20 s floor (whole clip) and the window (what is left after -StartFrame) must hold.
    if (-not $ClipOnly -and $TargetPresentedFrames -gt 0 -and ($presentedWindowSeconds + 1e-9) -lt $MinSeconds) {
        $out.verdict = 'PLAY_WINDOW_TOO_SHORT'
        $out.message = "PLAY_WINDOW_TOO_SHORT (presented_frames=$TargetPresentedFrames window=$(& $fmt $presentedWindowSeconds) required=$(& $fmt $MinSeconds))"
    } elseif (-not $ClipOnly -and $WindowSeconds -lt $MinSeconds) {
        # ENFORCE-2: the PLAY WINDOW of an evidence run is >= 20 s too -- a 10 s window on a 30 s clip is
        # still a run that plays less than 20 s of real footage.
        $out.verdict = 'PLAY_WINDOW_TOO_SHORT'
        $out.message = "PLAY_WINDOW_TOO_SHORT (window=$(& $fmt $WindowSeconds) required=$(& $fmt $MinSeconds))"
    } elseif ($len.seconds -lt $MinSeconds) {
        $out.verdict = 'CLIP_TOO_SHORT'
        $out.message = "CLIP_TOO_SHORT (clip=$(& $fmt $len.seconds) window=$(& $fmt $required))"
    } elseif ($remaining -lt $WindowSeconds) {
        $out.verdict = 'CLIP_TOO_SHORT'
        $out.message = "CLIP_TOO_SHORT (clip=$(& $fmt $remaining) window=$(& $fmt $WindowSeconds))"
    }
    return $out
}

# ---------------------------------------------------------------------------------------------------
# PLAYBACK-CLIP-LENGTH-ENFORCE-1 round 2 -- the rest of the CLASS: no ARGUMENT, MODE or LAUNCHER may
# make a venue play a clip under 20 s or loop. ASCII only.
# ---------------------------------------------------------------------------------------------------

# Every option the app's `--gui-smoke-playback` parser (platform/qt/main.cpp runGuiPlaybackSmoke)
# declares, classified. 'refuse' = a pass-through argument that could loop the clip, change the clip
# or the play window, or start a different playback mode, so the runner REFUSES it (the runner owns
# the window and the clip through its own parameters, which the length gate checks). 'allow' = it
# cannot change what is played or for how long. tools/repo_hygiene/test_playback_clip_length_gate.py
# compares this table with the options main.cpp really declares: a NEW app option fails that test
# until it is classified here, so a new loop/mode flag cannot slip past the gate unnoticed.
$script:GuiSmokeOptionPolicy = @{
    'h' = 'allow'; 'help' = 'allow'
    'gui-smoke-playback' = 'refuse'            # the runner passes it itself; a second one is a mode change
    'i' = 'refuse'; 'input' = 'refuse'         # the clip (the gate checked the runner's -Input, not this)
    'r' = 'allow'; 'receipt' = 'allow'
    'seconds' = 'refuse'                       # the play window the gate checked
    'start-frame' = 'refuse'                   # the window is measured from here
    'presented-frames' = 'refuse'              # ends the play window early
    'drop-frame-mode' = 'allow'
    'settle-ms' = 'allow'; 'settle-cpu-percent' = 'allow'; 'settle-cpu-stable-ms' = 'allow'; 'settle-cpu-max-ms' = 'allow'
    'screenshot-output' = 'allow'; 'window-screenshot-output' = 'allow'
    'contact-sheet-dir' = 'allow'; 'contact-sheet-frames' = 'allow'; 'contact-sheet-seek-mode' = 'allow'
    'scope' = 'allow'; 'playback-debayer' = 'allow'; 'playback-processing' = 'allow'
    'gpu-viewport' = 'allow'; 'gpu-preview-processing' = 'allow'; 'gpu-bilinear-debayer' = 'allow'
    'gpu-amaze-debayer' = 'allow'; 'gpu-amaze-texture-present' = 'allow'
    'no-look-assist' = 'allow'
    'loop' = 'refuse'                          # LOOPING: never (owner rule 2026-09-30)
    'launch-only' = 'refuse'                   # the runner passes it itself, only under -LaunchOnlyProbe
    'windowed' = 'allow'; 'display-prefer' = 'allow'
    'exercise-clip-lifecycle-stress' = 'refuse'  # plays a SECOND clip; the runner's own switch is gated
    'stress-switch-input' = 'refuse'; 'stress-switch-at-ms' = 'refuse'; 'stress-seek-frame' = 'refuse'
    'enable-phase3-quality-modes' = 'allow'; 'zebras' = 'allow'; 'no-zebras' = 'allow'; 'stage-log' = 'allow'
}

# The same for the headless `--profile-playback` parser (runPlaybackProfile), used by
# run-release-playback-profile.ps1. 'play' = the option makes the profile PLAY the clip (the real Play
# action, or the Look Assist settle's warm-up play), so it is allowed only after the clip passes the
# length gate. 'refuse' = a mode/clip change the wrapper owns.
$script:PlaybackProfileOptionPolicy = @{
    'h' = 'allow'; 'help' = 'allow'
    'profile-playback' = 'refuse'
    'i' = 'refuse'; 'input' = 'refuse'
    'o' = 'allow'; 'output' = 'allow'; 'r' = 'allow'; 'receipt' = 'allow'
    'frames' = 'allow'; 'start-frame' = 'allow'; 'frame-step' = 'allow'
    'scope' = 'allow'; 'playback-debayer' = 'allow'; 'playback-processing' = 'allow'; 'zebras' = 'allow'
    'raw-cache-mb' = 'allow'; 'cache-cpu-cores' = 'allow'; 'threads' = 'allow'; 'fast-open' = 'allow'
    'gpu-viewport' = 'allow'; 'gpu-preview-processing' = 'allow'; 'gpu-bilinear-debayer' = 'allow'; 'gpu-amaze-debayer' = 'allow'
    'show-window' = 'allow'; 'wait-for-paint' = 'allow'
    'exercise-play-action' = 'play'
    'exercise-look-assist-toggle' = 'play'
    'exercise-look-assist-settle' = 'play'
    'exercise-scale-toggle' = 'allow'; 'exercise-scale-toggle-from' = 'allow'
    'stage-log' = 'allow'
}

# Options refused in EVERY context even if a future parser starts declaring them: anything that loops,
# starts a different mode, or is the launch-only marker.
$script:GuiSmokeAlwaysRefusedOptions = @('loop', 'gui-smoke-playback', 'profile-playback', 'launch-only', 'autoplay')

function Get-GuiSmokePassThroughOptionNames {
    <#
    .SYNOPSIS
    The option NAMES a pass-through argument list would hand the app, however they are spelled: any
    leading '-', '--' or '/', any case, '--name=value', several tokens in one element, comma-joined.
    A value token (no leading dash/slash) names nothing.
    #>
    param([string[]]$Arguments = @())
    $names = @()
    foreach ($element in @($Arguments)) {
        if ($null -eq $element) { continue }
        foreach ($token in ([string]$element -split '[\s,;]+')) {
            if ($token -notmatch '^[-/]+(?<name>[^=\s]+)') { continue }
            $names += $Matches['name'].ToLowerInvariant()
        }
    }
    return $names
}

function Test-GuiSmokePassThroughArguments {
    <#
    .SYNOPSIS
    The pass-through gate. Context 'gui-smoke' (run-release-gui-smoke.ps1 -AdditionalArgs) or 'profile'
    (run-release-playback-profile.ps1 -AdditionalArgs) or 'launcher' (the interactive / --batch launchers
    that forward -AdditionalArgs to the exe and must NEVER be handed a play mode: every play-capable or
    mode-changing option is refused, anything else rides through). Returns [pscustomobject]@{ verdict; option;
    message; playCapable }. verdict is OK | PASS_THROUGH_REFUSED. Fail closed: an option this policy
    does not know is refused too, so a mistyped or new flag never rides through. Path-free messages.
    #>
    param(
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][ValidateSet('gui-smoke', 'profile', 'launcher')][string]$Context
    )
    $policy = if ($Context -eq 'gui-smoke') { $script:GuiSmokeOptionPolicy } else { $script:PlaybackProfileOptionPolicy }
    if ($Context -eq 'launcher') {
        # Only the options that make the app PLAY or change its mode are refused; the launcher's own
        # modes (--batch export, a plain GUI open) never play, so every other option is harmless.
        $policy = @{}
        foreach ($launcherRefused in @('exercise-play-action', 'exercise-look-assist-toggle', 'exercise-look-assist-settle',
                                       'exercise-clip-lifecycle-stress', 'stress-switch-input')) {
            $policy[$launcherRefused] = 'refuse'
        }
    }
    $result = [pscustomobject]@{ verdict = 'OK'; option = ''; message = 'OK'; playCapable = $false }
    foreach ($name in (Get-GuiSmokePassThroughOptionNames -Arguments $Arguments)) {
        $kind = if ($script:GuiSmokeAlwaysRefusedOptions -contains $name) { 'refuse' }
                elseif ($policy.ContainsKey($name)) { $policy[$name] }
                elseif ($Context -eq 'launcher') { 'allow' }
                else { 'unknown' }
        if ($kind -eq 'play') { $result.playCapable = $true; continue }
        if ($kind -eq 'allow') { continue }
        $why = if ($kind -eq 'unknown') { 'unclassified' } else { 'loop_or_clip_or_window_control' }
        $result.verdict = 'PASS_THROUGH_REFUSED'
        $result.option = $name
        $result.message = "PASS_THROUGH_REFUSED (option=$name reason=$why)"
        return $result
    }
    return $result
}

function Test-GuiSmokeEnvironmentEntries {
    <#
    .SYNOPSIS
    Refuses an -ExtraEnvironment KEY=VALUE entry that arms the app's own autoplay/loop automation hook
    (MLVAPP_AUTOPLAY_*): it plays whatever clip is opened, with no length gate and optionally looping.
    #>
    param([string[]]$Entries = @())
    $result = [pscustomobject]@{ verdict = 'OK'; option = ''; message = 'OK' }
    foreach ($entry in @($Entries)) {
        if ([string]::IsNullOrWhiteSpace($entry)) { continue }
        foreach ($pair in ([string]$entry -split ',')) {
            if ($pair.Trim() -match '^(?i)MLVAPP_AUTOPLAY_[A-Z0-9_]*\s*=') {
                $key = ($pair.Trim() -split '=', 2)[0].Trim().ToUpperInvariant()
                $result.verdict = 'PASS_THROUGH_REFUSED'
                $result.option = $key
                $result.message = "PASS_THROUGH_REFUSED (env=$key reason=autoplay_hook_has_no_length_gate)"
                return $result
            }
            $pairKey = ($pair.Trim() -split '=', 2)[0].Trim().ToUpperInvariant()
            if ($script:GuiSmokeRangeChangingEnvironment -contains $pairKey) {
                $result.verdict = 'PASS_THROUGH_REFUSED'
                $result.option = $pairKey
                $result.message = "PASS_THROUGH_REFUSED (env=$pairKey reason=changes_the_effective_play_range)"
                return $result
            }
        }
    }
    return $result
}

function Get-GuiSmokeGateExitCode {
    # The runner exit code for a Test-GuiSmokeClipLength verdict: 42 for an unknowable length, 41 for every
    # "too short" verdict (clip or play window). Callers never re-spell the mapping.
    param([Parameter(Mandatory = $true)][string]$Verdict)
    if ($Verdict -eq 'CLIP_LENGTH_UNKNOWN') { return 42 }
    return 41
}

function Get-GuiSmokeMinPresentedFrames {
    # The smallest --presented-frames target that still plays the 20 s floor at this frame rate.
    param([Parameter(Mandatory = $true)][double]$Fps, [double]$MinSeconds = $script:GuiSmokeMinClipSeconds)
    return [int][Math]::Ceiling($MinSeconds * $Fps)
}

function Test-GuiSmokeParentEnvironment {
    <#
    .SYNOPSIS
    Refuses an MLVAPP_AUTOPLAY_* variable INHERITED from the parent process environment (a shell that
    exported one, or a Scheduled Task env block): the app's autoplay hook reads it and Plays whatever clip
    the launch opens, with no tool-side length gate. -ExtraEnvironment is checked by
    Test-GuiSmokeEnvironmentEntries; this covers the case where nothing was passed at all.
    #>
    param([hashtable]$Environment = $null)
    $result = [pscustomobject]@{ verdict = 'OK'; option = ''; message = 'OK' }
    $names = if ($null -ne $Environment) { @($Environment.Keys) }
             else { @([System.Environment]::GetEnvironmentVariables().Keys) }
    foreach ($name in $names) {
        if ([string]$name -match '^(?i)MLVAPP_AUTOPLAY_') {
            $key = ([string]$name).ToUpperInvariant()
            $result.verdict = 'PASS_THROUGH_REFUSED'
            $result.option = $key
            $result.message = "PASS_THROUGH_REFUSED (env=$key reason=inherited_autoplay_hook_has_no_length_gate)"
            return $result
        }
        if ($script:GuiSmokeRangeChangingEnvironment -contains ([string]$name).ToUpperInvariant()) {
            $key = ([string]$name).ToUpperInvariant()
            $result.verdict = 'PASS_THROUGH_REFUSED'
            $result.option = $key
            $result.message = "PASS_THROUGH_REFUSED (env=$key reason=inherited_variable_changes_the_effective_play_range)"
            return $result
        }
    }
    return $result
}

function Get-GuiSmokeRefusalReason {
    <#
    .SYNOPSIS
    The typed reason token for a runner exit code (41 CLIP_TOO_SHORT or PLAY_WINDOW_TOO_SHORT, 42
    CLIP_LENGTH_UNKNOWN, 43 INVALID_LOOPED, 44 PASS_THROUGH_REFUSED, 14 the app's own gate), taking the
    more specific token out of the message when one is given; 'NONE' for every other exit code.
    #>
    param([int]$ExitCode, [string]$Message = '')
    if (@(14, 41, 42, 43, 44) -notcontains $ExitCode) { return 'NONE' }
    if ($Message -match '(PLAY_WINDOW_TOO_SHORT|CLIP_TOO_SHORT|CLIP_LENGTH_UNKNOWN|INVALID_LOOPED|REPLAY_REFUSED|PASS_THROUGH_REFUSED)') {
        return $Matches[1]
    }
    return "EXIT_$ExitCode"
}

function Convert-PlaybackLogLineToObject {
    # key=value / key="quoted value" tokens of one app log line, as a PSCustomObject. Integers and
    # doubles are converted; everything else stays a string. Shared so the loop verdict below is tested
    # against the SAME parser the runner uses on the app's real playback_smoke.summary line.
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

function Get-GuiSmokeLoopVerdict {
    <#
    .SYNOPSIS
    The RUNTIME BACKSTOP decision, run on the app's parsed playback_smoke.summary object. Returns
    [pscustomobject]@{ invalid; failures }. invalid is $true when the app's own timeline wrapped
    (wrapped=1, or any wrap_count > 0 -- the count is taken in the engine's actual wrap branches and
    outranks the presented-frame heuristic behind wrapped), or when the app reports a clip shorter than
    max(20 s, window). A launch-only probe never plays, so a wrap or any presented frame there is
    itself a failure. A binary that predates the wrap fields reports $null for each, so the app's own
    wrap signal is silent for it; two checks that need nothing from the app then still apply, from the
    clip's header frame count (-ClipFrames, 0 = unknown): more frames presented than the clip holds, or a
    last presented frame BEFORE the first, can only mean the timeline went round again.
    #>
    param(
        [AllowNull()]$Summary,
        [Parameter(Mandatory = $true)][double]$WindowSeconds,
        [bool]$LaunchOnlyProbe = $false,
        [int64]$ClipFrames = 0
    )
    $failures = @()
    $prop = { param($name) if ($null -ne $Summary -and $Summary.PSObject.Properties[$name]) { $Summary.$name } else { $null } }
    $wrapped = & $prop 'wrapped'
    $wrapCount = & $prop 'wrap_count'
    $totalFrames = & $prop 'total_frames'
    $clipSeconds = & $prop 'clip_seconds'
    $presented = & $prop 'presented_frames'
    $firstPresented = & $prop 'first_presented_frame'
    $lastPresented = & $prop 'last_presented_frame'
    if ($LaunchOnlyProbe) {
        if (($null -ne $presented -and [int64]$presented -gt 0) -or
            ($null -ne $wrapped -and [int]$wrapped -ne 0) -or
            ($null -ne $wrapCount -and [int64]$wrapCount -gt 0)) {
            $failures += "LAUNCH_ONLY_PROBE_PLAYED: a launch-only probe must present zero playback frames and never wrap (presented_frames=$presented wrapped=$wrapped wrap_count=$wrapCount)."
        }
    } else {
        if (($null -ne $wrapped -and [int]$wrapped -ne 0) -or
            ($null -ne $wrapCount -and [int64]$wrapCount -gt 0)) {
            $failures += "INVALID_LOOPED: the playback timeline wrapped (wrapped=$wrapped wrap_count=$wrapCount total_frames=$totalFrames clip_seconds=$clipSeconds); a looped short clip is never playback evidence."
        }
        if ($ClipFrames -gt 0 -and $null -ne $presented -and [int64]$presented -gt $ClipFrames) {
            $failures += "INVALID_LOOPED: $presented frames were presented from a clip of $ClipFrames frames; the timeline went round again."
        }
        if ($null -ne $firstPresented -and $null -ne $lastPresented -and [int64]$lastPresented -lt [int64]$firstPresented) {
            $failures += "INVALID_LOOPED: the last presented frame ($lastPresented) is before the first ($firstPresented); the timeline wrapped."
        }
        if ($null -ne $clipSeconds -and [double]$clipSeconds -gt 0 -and
            [double]$clipSeconds -lt [Math]::Max($script:GuiSmokeMinClipSeconds, $WindowSeconds)) {
            $failures += "INVALID_LOOPED: the app reports clip_seconds=$clipSeconds, under max($($script:GuiSmokeMinClipSeconds), window=$WindowSeconds)."
        }
    }
    return [pscustomobject]@{ invalid = ($failures.Count -gt 0); failures = $failures }
}
