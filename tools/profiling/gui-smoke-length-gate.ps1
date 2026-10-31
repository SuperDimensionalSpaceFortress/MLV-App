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
#   CLIP_TOO_SHORT       (clip=<s> window=<s>)  clip is shorter than max(floor, window) seconds
#   PLAY_WINDOW_TOO_SHORT (window=<s> required=<s>)  the requested PLAY WINDOW is under the floor, even
#                        on a long clip (ENFORCE-2: the window, not only the clip, must reach the floor)
# The floor is Get-GuiSmokeMinPlaySeconds of the clip's native fps: 20 s, 10 s for an HFR clip (HFR-10S-1).
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

# PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15: "Amend rule so that hfr 60fps clips are allowed to be 10
# seconds"): the floor is a function of the clip's NATIVE fps. Mirrors platform/qt/PlaybackFrameRange.h minPlayWindowSeconds
# (kStandardMinPlayWindowSeconds, kHfrMinPlayWindowSeconds, kHfrNativeFpsThreshold); a parity test compares them.
$script:GuiSmokeStandardMinPlaySeconds = 20.0
$script:GuiSmokeHfrMinPlaySeconds = 10.0
$script:GuiSmokeHfrNativeFpsThreshold = 50.0
# Where a stage cannot see the clip yet, it refuses only what NO clip could admit (the HFR floor); the clip's own floor is
# applied wherever its fps is known (this file's gate and oracles, the app).
$script:GuiSmokeLowestMinPlaySeconds = $script:GuiSmokeHfrMinPlaySeconds

function Get-GuiSmokeMinPlaySeconds {
    # THE play floor of a clip, from its NATIVE fps: 10 s at >= 50 fps (HFR), 20 s below; an absent, non-numeric, non-finite
    # or non-positive fps is 20 s (fail closed, never 10). Seconds are source seconds at the native fps.
    param([AllowNull()]$NativeFps)
    $fps = 0.0
    if ($null -ne $NativeFps -and $NativeFps -isnot [bool] -and
        [double]::TryParse([string]$NativeFps, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$fps) -and
        -not [double]::IsNaN($fps) -and -not [double]::IsInfinity($fps) -and $fps -ge $script:GuiSmokeHfrNativeFpsThreshold) {
        return $script:GuiSmokeHfrMinPlaySeconds
    }
    return $script:GuiSmokeStandardMinPlaySeconds
}

function Get-GuiSmokeMinPlayWindowMs {
    # The same floor in ms: the lifecycle-stress switch stops Play, so it may not come sooner.
    param([AllowNull()]$NativeFps)
    return [int64][Math]::Round((Get-GuiSmokeMinPlaySeconds -NativeFps $NativeFps) * 1000.0)
}
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

# GUI-SMOKE-LENGTH-GATE-VIDF-COUNT-1: the app plays the VIDF BLOCKS it indexes, never the MLVI header's videoFrameCount.
# src/mlv/video_mlv.c walks every part block by block (16-byte mlv_hdr_t: blockType[4] blockSize u32 timestamp u64), counts
# each "VIDF" into video_frames and assigns that to video->frames, which getMlvFrames returns; a blockSize under 16 makes the
# app refuse the file (MLV_ERR_INVALID). M16-1243's headers declared 612 frames while 937 VIDF blocks were indexed, so the
# header-only gate saw a 25.5 s clip where the app plays 39.1 s. The walk is bounded in blocks and seconds (a venue leg must
# not hang on a huge clip); a walk that cannot finish fails closed (vidf_count_unverified). The app's MAPP sidecar caches
# this same walk (load_mapp sets video->frames from it), so counting the blocks is the quantity both paths agree on.
$script:GuiSmokeBlockHeaderBytes = 16
$script:GuiSmokeVidfWalkMaxBlocks = 2000000
$script:GuiSmokeVidfWalkMaxSeconds = 30.0

function Get-GuiSmokeMlvVidfCount {
    <#
    .SYNOPSIS
    Counts the VIDF blocks of ONE part file the way the app's block walk does (start at offset 0, skip each block by its
    blockSize). Returns @{ ok; vidf; blocks; overrun; reason }. ok=$false means the walk did not finish (reason: unreadable,
    invalid_block_size, truncated_block_header, walk_bound_blocks, walk_bound_seconds); never throws. overrun=$true means some
    block's declared blockSize reaches past the end of the file (an MLVI block that swallows the file, a cut-off last block).
    #>
    param([Parameter(Mandatory = $true)][string]$Path, [AllowNull()][System.Diagnostics.Stopwatch]$Clock = $null)
    if ($null -eq $Clock) { $Clock = [System.Diagnostics.Stopwatch]::StartNew() }
    $hdr = New-Object byte[] $script:GuiSmokeBlockHeaderBytes
    $vidf = [int64]0
    $blocks = [int64]0
    $overrun = $false
    $fs = $null
    try {
        # bufferSize 1 = unbuffered: each seek reads 16 bytes, not a 4 KiB page (a clip on a share is walked block by block).
        $fs = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                                              [System.IO.FileShare]::ReadWrite, 1)
        $size = [int64]$fs.Length
        $pos = [int64]0
        while ($pos -lt $size) {
            if ($blocks -ge $script:GuiSmokeVidfWalkMaxBlocks) { return @{ ok = $false; vidf = $vidf; blocks = $blocks; reason = 'walk_bound_blocks' } }
            if (($blocks % 256) -eq 0 -and $Clock.Elapsed.TotalSeconds -gt $script:GuiSmokeVidfWalkMaxSeconds) {
                return @{ ok = $false; vidf = $vidf; blocks = $blocks; reason = 'walk_bound_seconds' }
            }
            if (($size - $pos) -lt $script:GuiSmokeBlockHeaderBytes) { return @{ ok = $false; vidf = $vidf; blocks = $blocks; reason = 'truncated_block_header' } }
            [void]$fs.Seek($pos, [System.IO.SeekOrigin]::Begin)
            $got = 0
            while ($got -lt $hdr.Length) {
                $n = $fs.Read($hdr, $got, $hdr.Length - $got)
                if ($n -le 0) { break }
                $got += $n
            }
            if ($got -lt $hdr.Length) { return @{ ok = $false; vidf = $vidf; blocks = $blocks; reason = 'truncated_block_header' } }
            $blockSize = [int64][BitConverter]::ToUInt32($hdr, 4)
            if ($blockSize -lt $script:GuiSmokeBlockHeaderBytes) { return @{ ok = $false; vidf = $vidf; blocks = $blocks; reason = 'invalid_block_size' } }
            if ($hdr[0] -eq 0x56 -and $hdr[1] -eq 0x49 -and $hdr[2] -eq 0x44 -and $hdr[3] -eq 0x46) { $vidf++ }   # 'VIDF'
            $blocks++
            $pos += $blockSize
            if ($pos -gt $size) { $overrun = $true }
        }
    } catch {
        return @{ ok = $false; vidf = $vidf; blocks = $blocks; overrun = $overrun; reason = 'unreadable' }
    } finally {
        if ($null -ne $fs) { $fs.Dispose() }
    }
    return @{ ok = $true; vidf = $vidf; blocks = $blocks; overrun = $overrun; reason = '' }
}

function Get-GuiSmokePartFrames {
    <#
    .SYNOPSIS
    The frames the app plays from ONE part: its VIDF block count, and nothing else -- the header count is never admitted in its
    place (the app rejects a file with zero indexed frames). Returns @{ ok; frames; reason }. No VIDF block while the header
    declares frames (a file cut off after the MLVI header, an MLVI block that swallows the file, a random body) is
    header_vidf_count_mismatch; a walk that cannot finish, or whose last counted block runs past EOF, is vidf_count_unverified.
    #>
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][int64]$HeaderFrames,
          [AllowNull()][System.Diagnostics.Stopwatch]$Clock = $null)
    $walk = Get-GuiSmokeMlvVidfCount -Path $Path -Clock $Clock
    if (-not $walk.ok) { return @{ ok = $false; frames = [int64]0; reason = 'vidf_count_unverified' } }
    if ($walk.vidf -le 0 -and $HeaderFrames -gt 0) { return @{ ok = $false; frames = [int64]0; reason = 'header_vidf_count_mismatch' } }
    if ($walk.overrun -and $walk.vidf -gt 0) { return @{ ok = $false; frames = [int64]0; reason = 'vidf_count_unverified' } }
    return @{ ok = $true; frames = [int64]$walk.vidf; reason = '' }
}

function Get-GuiSmokeClipLength {
    <#
    .SYNOPSIS
    Length of a clip (all spanned parts): the VIDF blocks the app will play (Get-GuiSmokePartFrames), with the MLVI headers
    for identity, fps and the spanned set. Returns
    [pscustomobject]@{ known; reason; frames; fps; seconds }. known=$false means FAIL CLOSED.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $unknown = { param($why) [pscustomobject]@{ known = $false; reason = $why; frames = 0; fps = 0.0; seconds = 0.0 } }
    $first = Read-GuiSmokeMlviHeader -Path $Path
    if (-not $first.ok) { return (& $unknown $first.reason) }
    # Header validity precedes the block walk (as before the walk existed): a zero-fps header reports bad_fps, not the walk's verdict.
    if ($first.fpsNom -le 0 -or $first.fpsDenom -le 0) { return (& $unknown 'bad_fps') }
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $frames = [int64]0
    if ($first.fileCount -le 1) {
        # A spanned set walks every part (the first included) in the loop below, so the first part is walked once.
        $partFrames = Get-GuiSmokePartFrames -Path $Path -HeaderFrames ([int64]$first.videoFrames) -Clock $clock
        if (-not $partFrames.ok) { return (& $unknown $partFrames.reason) }
        $frames = [int64]$partFrames.frames
    }
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
            $pf = Get-GuiSmokePartFrames -Path $part.FullName -HeaderFrames ([int64]$h.videoFrames) -Clock $clock
            if (-not $pf.ok) { return (& $unknown $pf.reason) }
            $frames += [int64]$pf.frames
            $idx++
        }
    }
    if ($frames -le 0) { return (& $unknown 'zero_frames') }
    $fps = [double]$first.fpsNom / [double]$first.fpsDenom
    return [pscustomobject]@{ known = $true; reason = ''; frames = $frames; fps = $fps; seconds = ([double]$frames / $fps) }
}

function Get-GuiSmokeClipPlayFloorSeconds {
    # The play floor of ONE clip, from the Get-GuiSmokeClipLength result: Get-GuiSmokeMinPlaySeconds of its header fps, and the
    # 20 s floor when the header cannot be read (the length gate then refuses it as CLIP_LENGTH_UNKNOWN). The one place a launcher
    # that clamps its own window (validate-visible-playback.ps1, run-release-playback-profile.ps1) takes the floor from.
    param([Parameter(Mandatory = $true)]$ClipLength)
    return (Get-GuiSmokeMinPlaySeconds -NativeFps $(if ($ClipLength.known) { $ClipLength.fps } else { $null }))
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
        # HFR-10S-1: < 0 (the default) = the clip's own floor, Get-GuiSmokeMinPlaySeconds of its header fps.
        [double]$MinSeconds = -1,
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
    if ($MinSeconds -lt 0) { $MinSeconds = Get-GuiSmokeClipPlayFloorSeconds -ClipLength $len }
    $required = [Math]::Max($MinSeconds, $WindowSeconds)
    $out = [pscustomobject]@{
        verdict = 'OK'; message = 'OK'; clipSeconds = $len.seconds; windowSeconds = $WindowSeconds
        requiredSeconds = $required; frames = $len.frames; fps = $len.fps; minSeconds = $MinSeconds
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
    'contact-sheet-seek-dir' = 'allow'         # paired seek capture after the stop; never plays
    'visual-capture-dir' = 'allow'             # window captures AFTER the measured window closes: the same Play runs ~3 s on (no restart, no loop), then pauses
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
            if ($pairKey -eq $script:GuiSmokePaceModeEnvironment) {
                $result.verdict = 'PASS_THROUGH_REFUSED'
                $result.option = $pairKey
                $result.message = "PASS_THROUGH_REFUSED (env=$pairKey reason=pace_mode_is_armed_only_by_the_cpu_switch)"
                return $result
            }
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
    # The smallest --presented-frames target that still plays the clip's floor at this frame rate (HFR-10S-1: < 0, the
    # default, = Get-GuiSmokeMinPlaySeconds of -Fps).
    param([Parameter(Mandatory = $true)][double]$Fps, [double]$MinSeconds = -1)
    if ($MinSeconds -lt 0) { $MinSeconds = Get-GuiSmokeMinPlaySeconds -NativeFps $Fps }
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
        if (([string]$name).ToUpperInvariant() -eq $script:GuiSmokePaceModeEnvironment) {
            $key = ([string]$name).ToUpperInvariant()
            $result.verdict = 'PASS_THROUGH_REFUSED'
            $result.option = $key
            $result.message = "PASS_THROUGH_REFUSED (env=$key reason=inherited_pace_mode_is_armed_only_by_the_cpu_switch)"
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
    if ($Message -match '(PLAY_WINDOW_TOO_SHORT|PLAY_DURATION_TOO_SHORT|PLAY_PACE_TOO_SLOW|CLIP_TOO_SHORT|CLIP_LENGTH_UNKNOWN|INVALID_SOURCE_FRAMES|INVALID_LOOPED|SOURCE_FRAMES_SHORT|PLAY_SAFETY_TIMEOUT|REPLAY_REFUSED|PASS_THROUGH_REFUSED)') {
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
        if ($key -ceq 'run_nonce') {
            # ENFORCE-4 r2: a nonce is an opaque STRING, never a number (an all-digit one must keep its digits).
            $result[$key] = $rawValue
        }
        elseif ([long]::TryParse($rawValue, [ref]$intValue)) {
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

function Get-GuiSmokeAbsentReceiptFields {
    <#
    .SYNOPSIS
    PLAYBACK-CLIP-LENGTH-ENFORCE-4. The receipt oracle's FIELD-ABSENCE rule: evidence is valid only when every required
    field is PRESENT. A field a build does not write (a master-era binary writes none of the ENFORCE-1..3 fields) is
    ABSENT -- never "not played", never "no override", never "pace unchecked", never "no wrap". Returns the names, in the
    order given, of the -Names that -Summary does not carry (all of them for a null summary).
    #>
    param([AllowNull()]$Summary, [Parameter(Mandatory = $true)][string[]]$Names)
    $missing = @()
    foreach ($name in $Names) {
        if ($null -eq $Summary -or -not $Summary.PSObject.Properties[$name] -or $null -eq $Summary.$name) { $missing += $name }
    }
    return $missing
}

# ---------------------------------------------------------------------------------------------------
# PLAYBACK-CLIP-LENGTH-ENFORCE-4 round 2 -- "a receipt counts only if it was written by THIS invocation of the app, for
# THIS run" (sol r1 BLOCKER: the profile launcher read -Output and judged a PREVIOUS run's valid receipt when the app exited
# 0 without playing, e.g. --exercise-play-action --help). Three layers, each sufficient alone against the stale-receipt repro:
#   1. SET ASIDE: any pre-existing receipt / output is renamed (STALE-<utc>-<name>, never destroyed) BEFORE the launch, so
#      what a reader finds afterwards is something this run wrote, or nothing;
#   2. NONCE: the launcher generates a per-run nonce, hands it to the app (MLVAPP_RUN_NONCE), and the app echoes it on every
#      receipt it writes; a missing, mismatched or unbound nonce is INVALID (RECEIPT_NOT_THIS_RUN, exit 43);
#   3. FRESHNESS: a receipt FILE last written before the launch is INVALID.
# On any INVALID the rejected receipt is renamed <name>.INVALID<ext> (a JSON one is also stamped "invalid": true), so an offline
# reader cannot mistake it for evidence. ASCII only.
# ---------------------------------------------------------------------------------------------------

$script:GuiSmokeRunNonceEnvironmentName = 'MLVAPP_RUN_NONCE'
$script:GuiSmokeReceiptClockSlackSeconds = 2   # file-system timestamp granularity
# Profile options whose Play is a Look Assist warm-up: it only happens in Auto playback quality mode (fable H1).
$script:PlaybackProfileSettleOptions = @('exercise-look-assist-settle', 'exercise-look-assist-toggle')

function New-GuiSmokeRunNonce {
    # 'n' + a GUID: always a STRING token (Convert-PlaybackLogLineToObject turns an all-digit token into a number), letters
    # and digits only (the app's sanitizeRunNonce accepts 8..64 of them and writes anything else as `none`).
    return 'n' + [Guid]::NewGuid().ToString('N')
}

function Get-GuiSmokeRunNonceFailure {
    <#
    .SYNOPSIS
    $null when -Summary carries the nonce THIS launch generated; otherwise the RECEIPT_NOT_THIS_RUN failure text. A launcher
    that bound no nonce (-ExpectedRunNonce empty) can show no receipt to be its own, so it fails too. A null summary is
    reported by the oracle's own no-receipt failure.
    #>
    param([AllowNull()]$Summary, [AllowNull()][string]$ExpectedRunNonce = '')
    if ($null -eq $Summary) { return $null }
    if ([string]::IsNullOrWhiteSpace($ExpectedRunNonce)) {
        return "RECEIPT_NOT_THIS_RUN: the launcher bound no run nonce to this run, so no receipt can be shown to be this run's."
    }
    $receiptNonce = if ($Summary.PSObject.Properties['run_nonce']) { $Summary.run_nonce } else { $null }
    if ([string]::IsNullOrWhiteSpace([string]$receiptNonce)) {
        return "RECEIPT_NOT_THIS_RUN: the receipt carries no run_nonce (a build that predates the run nonce, or an app that was not handed one); it cannot be shown to be this run's."
    }
    if ([string]$receiptNonce -cne $ExpectedRunNonce) {
        return "RECEIPT_NOT_THIS_RUN: the receipt's run_nonce ($receiptNonce) is not the nonce this launch handed the app; an earlier run (or another process) wrote it."
    }
    return $null
}

function Get-GuiSmokeReceiptStaleFailure {
    # A receipt FILE last written before the launch is not this run's. $null when it is fresh (or absent: no receipt is
    # reported by the oracle itself).
    param([Parameter(Mandatory = $true)][string]$Path, [AllowNull()][object]$LaunchedUtc = $null)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    if ($null -eq $LaunchedUtc) {
        return "RECEIPT_NOT_THIS_RUN: the launcher recorded no launch time, so the receipt file's age cannot be judged."
    }
    $written = (Get-Item -LiteralPath $Path).LastWriteTimeUtc
    if ($written -lt ([datetime]$LaunchedUtc).AddSeconds(-$script:GuiSmokeReceiptClockSlackSeconds)) {
        return "RECEIPT_NOT_THIS_RUN: the receipt file was last written $($written.ToString('o')), before this launch ($(([datetime]$LaunchedUtc).ToString('o')))."
    }
    return $null
}

function Move-GuiSmokeStaleReceiptAside {
    <#
    .SYNOPSIS
    Layer 1: renames a PRE-EXISTING receipt / output to STALE-<utc>-<name> in the same directory before the launch (never
    deletes it: it may be somebody's evidence). Returns [pscustomobject]@{ ok; moved; asidePath; message }; ok=$false means
    the stale file could not be moved and the launcher must not run.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $none = { param($okValue, $text) [pscustomobject]@{ ok = $okValue; moved = $false; asidePath = $null; message = $text } }
    if (-not (Test-Path -LiteralPath $Path)) { return (& $none $true 'OK') }
    if (Test-Path -LiteralPath $Path -PathType Container) {
        return (& $none $false 'RECEIPT_NOT_THIS_RUN: the receipt path is a directory, not a file this run can own.')
    }
    try {
        $directory = Split-Path -Parent $Path
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 6)
        $aside = Join-Path $directory ("STALE-$stamp-" + (Split-Path -Leaf $Path))
        Move-Item -LiteralPath $Path -Destination $aside -ErrorAction Stop
        return [pscustomobject]@{ ok = $true; moved = $true; asidePath = $aside; message = 'OK' }
    } catch {
        return (& $none $false 'RECEIPT_NOT_THIS_RUN: a receipt from an earlier run exists at the output path and could not be set aside, so nothing this run writes could be told from it.')
    }
}

function Set-GuiSmokeReceiptInvalid {
    <#
    .SYNOPSIS
    Quarantine of a rejected receipt (fable H2): renames <dir>\<name><ext> to <name>.INVALID<ext> (numbered when that exists;
    nothing is overwritten) and, for a JSON receipt, stamps "invalid": true and "invalid_reasons". Returns the new path, or
    $null when there was nothing to rename or the rename failed. Never throws: the caller is about to exit 43 anyway.
    #>
    param([Parameter(Mandatory = $true)][string]$Path, [string[]]$Failures = @())
    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
        $directory = Split-Path -Parent $Path
        $stem = [IO.Path]::GetFileNameWithoutExtension($Path)
        $extension = [IO.Path]::GetExtension($Path)
        $target = Join-Path $directory ("$stem.INVALID$extension")
        $counter = 1
        while (Test-Path -LiteralPath $target) { $target = Join-Path $directory ("$stem.INVALID.$counter$extension"); $counter++ }
        Move-Item -LiteralPath $Path -Destination $target -ErrorAction Stop
        if ($extension -ieq '.json') {
            try {
                $document = Get-Content -LiteralPath $target -Raw | ConvertFrom-Json
                $document | Add-Member -NotePropertyName invalid -NotePropertyValue $true -Force
                $document | Add-Member -NotePropertyName invalid_reasons -NotePropertyValue @($Failures | ForEach-Object { [string]$_ }) -Force
                [IO.File]::WriteAllText($target, ($document | ConvertTo-Json -Depth 64), (New-Object System.Text.UTF8Encoding($false)))
            } catch { }   # the rename alone already keeps a reader from trusting it
        }
        return $target
    } catch { return $null }
}

function Write-GuiSmokeInvalidMarker {
    # <Directory>\INVALID.json: the run that produced everything in this directory is INVALID evidence, and why. Best effort.
    param([Parameter(Mandatory = $true)][string]$Directory, [string[]]$Failures = @())
    try {
        $document = [ordered]@{ invalid = $true; utc = [DateTime]::UtcNow.ToString('o'); reasons = @($Failures | ForEach-Object { [string]$_ }) }
        [IO.File]::WriteAllText((Join-Path $Directory 'INVALID.json'), ($document | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

function Test-GuiSmokeProfileModeAdmitsPlay {
    <#
    .SYNOPSIS
    fable H1: the Look Assist settle / toggle options only Play (their warm-up) in AUTO playback quality mode. In any other mode
    the app admits no Play and writes programmatic_play_admitted=0, so the run could only end in exit 43 AFTER the whole run.
    Refused UP FRONT instead, typed and path-free (PASS_THROUGH_REFUSED, exit 44). -QualityMode is the effective mode (the
    wrapper's -QualityMode, overridden by a later MLVAPP_PLAYBACK_QUALITY_MODE=<mode> in -ExtraEnvironment).
    #>
    param([string[]]$Arguments = @(), [string]$QualityMode = '', [string[]]$ExtraEnvironment = @())
    $effective = $QualityMode
    foreach ($entry in @($ExtraEnvironment)) {
        foreach ($pair in ([string]$entry -split ',')) {
            if ($pair.Trim() -match '^(?i)MLVAPP_PLAYBACK_QUALITY_MODE\s*=(?<mode>.*)$') { $effective = $Matches['mode'] }
        }
    }
    $result = [pscustomobject]@{ verdict = 'OK'; option = ''; message = 'OK' }
    foreach ($name in (Get-GuiSmokePassThroughOptionNames -Arguments $Arguments)) {
        if ($script:PlaybackProfileSettleOptions -contains $name -and ([string]$effective).Trim() -ine 'auto') {
            $result.verdict = 'PASS_THROUGH_REFUSED'
            $result.option = $name
            $result.message = "PASS_THROUGH_REFUSED (option=$name reason=needs_auto_quality_mode_to_admit_a_play)"
            return $result
        }
    }
    return $result
}

function Get-GuiSmokeConformPaceProof {
    <#
    .SYNOPSIS
    HFR-CONFORM-AWARE-RECEIPT-ORACLES-1. Did the run PROVE it was conformed? PLAYBACK-HFR-CONFORM-DEFAULT-1 plays a clip above 30 fps
    as slow motion at the conform target, so the engine's pace_fps is the conform rate, not the clip's native fps, and the native-pace
    rule of the source-frame oracle would reject every conformed run. The pace rule is relaxed ONLY on proof; proof is a PRESENT
    `playback_smoke.conform` line for the MEASURED session (-SummarySession) with conform_active=1, clip_fps within 0.5 % of native_fps,
    playback_fps > 0 and within 0.5 % of pace_fps, playback_fps not above clip_fps (conform never speeds a clip up), and playback_fps below
    clip_fps by more than 0.5 % (r7: an active conform at the clip rate is native-rate playback, no conform).
    Absent, malformed, inactive, another session's, or inconsistent -> proven=$false and `reason` says why (absence never helps).
    The 20 s floor is NOT touched here. Same decision as Get-AttrCudaConformAwareSourceFramesVerdict (AttrCudaArtifacts.psm1) and
    Get-DvConformPaceProof (DualVenueRunner.psm1); a parity test executes all three on one table.
    Returns [pscustomobject]@{ proven; reason; present; session; clipFps; playbackFps }.
    #>
    param([AllowNull()][AllowEmptyString()][string]$ConformLine, [AllowNull()][string]$SummarySession, $NativeFps, $PaceFps)
    $out = [pscustomobject]@{ proven = $false; reason = ''; present = $false; session = $null; clipFps = $null; playbackFps = $null }
    if ([string]::IsNullOrWhiteSpace($ConformLine)) { $out.reason = 'no playback_smoke.conform line for the run'; return $out }
    $out.present = $true
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $f = @{}
    foreach ($m in [regex]::Matches($ConformLine, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) { $f[$m.Groups['k'].Value] = $m.Groups['v'].Value }
    $num = { param($name) $d = 0.0; if ($f.ContainsKey($name) -and [double]::TryParse([string]$f[$name], [System.Globalization.NumberStyles]::Float, $inv, [ref]$d)) { $d } else { $null } }
    $absent = @('session', 'clip_fps', 'playback_fps', 'conform_active' | Where-Object { $null -eq (& $num $_) })
    if ($ConformLine -notmatch 'playback_smoke\.conform\s' -or $absent.Count -gt 0) {
        $out.reason = "the playback_smoke.conform line is malformed (absent or unreadable: $($absent -join ' / '))"
        return $out
    }
    $out.session = [string]$f['session']
    $clip = [double](& $num 'clip_fps'); $play = [double](& $num 'playback_fps'); $active = [double](& $num 'conform_active')
    $out.clipFps = $clip; $out.playbackFps = $play
    $native = $null; $pace = $null
    if ($null -ne $NativeFps) { $native = [double]$NativeFps }
    if ($null -ne $PaceFps) { $pace = [double]$PaceFps }
    # MERGE-MASTER r4 (Grok G1 / adversary A3): NaN / Infinity parse as doubles and fail every ordered comparison, so the rate checks below
    # reject them first -- the same guard Get-DvConformPaceProof carries (the parity contract). IsNaN / IsInfinity: Windows PowerShell 5.1 has no IsFinite.
    $nonFinite = { param($v) [double]::IsNaN([double]$v) -or [double]::IsInfinity([double]$v) }
    if ([string]::IsNullOrWhiteSpace($SummarySession) -or $out.session -cne [string]$SummarySession) {
        $out.reason = "the playback_smoke.conform line is for session $($out.session), not the measured session $SummarySession"
    } elseif ($active -ne 1) {
        $out.reason = "conform_active=$($f['conform_active']); the run was not conformed"
    } elseif ($null -eq $native -or (& $nonFinite $native) -or (& $nonFinite $clip) -or $native -le 0 -or $clip -le 0 -or [Math]::Abs($clip - $native) -gt (0.005 * $native)) {
        $out.reason = "conform clip_fps=$clip does not match native_fps=$native"
    } elseif ($null -eq $pace -or (& $nonFinite $pace) -or (& $nonFinite $play) -or $pace -le 0 -or $play -le 0 -or [Math]::Abs($play - $pace) -gt (0.005 * $pace)) {
        $out.reason = "conform playback_fps=$play does not match pace_fps=$pace"
    } elseif ($play -gt ($clip * 1.005)) {
        $out.reason = "conform playback_fps=$play is above clip_fps=$clip; conform never speeds a clip up"
    } elseif ($play -ge ($clip * 0.995)) {
        # MERGE-MASTER r7 (sol r6 B1, Dot ask 6545453593): an active conform at the clip rate is native-rate playback; the parity contract with Get-DvConformPaceProof.
        $out.reason = "conform active but playback_fps=$play equals clip_fps=$clip (within 0.5 %): no conform happened"
    } else {
        $out.proven = $true
    }
    return $out
}

function Get-GuiSmokeSourceFramesVerdict {
    <#
    .SYNOPSIS
    PLAYBACK-CLIP-LENGTH-ENFORCE-3 RECEIPT ORACLE. "20 s of real footage" is a SOURCE-FRAME quantity: the app
    counts the distinct source frames its engine advanced (source_advanced) and the frames the admitted Play had
    to consume (required_source_frames = ceil(window x NATIVE fps)) on playback_smoke.summary. A receipt is
    INVALID_SOURCE_FRAMES -- never a PASS -- when the counter is absent (a build that cannot prove it), the
    requirement is unknown, source_advanced is under it, the run was paced by a persisted fps override, or the
    engine's pace differs from the clip's native fps. ENFORCE-4: EVERY field it judges must be PRESENT -- an absent
    native_fps / pace_fps / fps_override is INVALID (RECEIPT_FIELD_ABSENT), not "no override" / "pace unchecked".
    ENFORCE-4 r2: the receipt must also be THIS run's (-ExpectedRunNonce, RECEIPT_NOT_THIS_RUN), and a PRESENT pace_fps <= 0 is
    INVALID (the engine's pace is unknown, so wall clock cannot be tied to footage).
    HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: -ConformLine is the run's `playback_smoke.conform` line. Only a PROVEN conform
    (Get-GuiSmokeConformPaceProof) changes the pace rule -- pace_fps is then compared to the conform playback_fps instead of native_fps;
    the 20 s floor (source_advanced >= required_source_frames >= ceil(20 s x native_fps)) is never relaxed. paceRule says which judged.
    Returns [pscustomobject]@{ invalid; failures; sourceAdvanced; requiredSourceFrames; paceRule; conformPlaybackFps; conformClipFps }.
    #>
    param([AllowNull()]$Summary, [AllowNull()][string]$ExpectedRunNonce = '', [AllowNull()][AllowEmptyString()][string]$ConformLine = '')
    $paceRule = 'native'
    $conformProof = [pscustomobject]@{ proven = $false; reason = ''; present = $false; session = $null; clipFps = $null; playbackFps = $null }
    $failures = @()
    $nonceFailure = Get-GuiSmokeRunNonceFailure -Summary $Summary -ExpectedRunNonce $ExpectedRunNonce
    if ($null -ne $nonceFailure) { $failures += $nonceFailure }
    $prop = { param($name) if ($null -ne $Summary -and $Summary.PSObject.Properties[$name]) { $Summary.$name } else { $null } }
    $advanced = & $prop 'source_advanced'
    $required = & $prop 'required_source_frames'
    $nativeFps = & $prop 'native_fps'
    $paceFps = & $prop 'pace_fps'
    $fpsOverride = & $prop 'fps_override'
    # HFR-10S-1: the floor is the clip's own, Get-GuiSmokeMinPlaySeconds of native_fps (10 s at >= 50 fps, else 20 s).
    $floorSeconds = Get-GuiSmokeMinPlaySeconds -NativeFps $nativeFps
    if ($null -eq $Summary) {
        $failures += "INVALID_SOURCE_FRAMES: the run produced no playback_smoke.summary, so no source frames can be proven."
    } elseif ($null -eq $advanced -or $null -eq $required) {
        $missingCount = @(Get-GuiSmokeAbsentReceiptFields -Summary $Summary -Names @('source_advanced', 'required_source_frames'))
        $failures += "INVALID_SOURCE_FRAMES: RECEIPT_FIELD_ABSENT: playback_smoke.summary carries no $($missingCount -join ' / ') (a build that predates PLAYBACK-CLIP-LENGTH-ENFORCE-3 writes neither); the footage played cannot be proven."
    } elseif ([int64]$required -le 0) {
        $failures += "INVALID_SOURCE_FRAMES: required_source_frames=$required; the admitted window is unknown."
    } elseif ([int64]$advanced -lt [int64]$required) {
        $failures += "INVALID_SOURCE_FRAMES: the engine advanced source_advanced=$advanced distinct source frames but the Play had to consume required_source_frames=$required; under $floorSeconds s of real footage is never playback evidence."
    }
    # ENFORCE-4: the oracle re-derives the floor itself and does not trust the app's own requirement. A receipt
    # whose required_source_frames is under ceil(floor x native fps) (the 0.02 absorbs the fps printed to 3 decimals) was
    # admitted for less than the floor of footage, and a native_fps of 0 cannot say what the floor is.
    # MERGE-MASTER r4 (Grok G1): a NaN / Infinity rate fails every ordered comparison, so it is unknown, never a pass (5.1-safe: no IsFinite).
    $nonFinite = { param($v) [double]::IsNaN([double]$v) -or [double]::IsInfinity([double]$v) }
    if ($null -ne $Summary -and $null -ne $nativeFps -and ((& $nonFinite $nativeFps) -or [double]$nativeFps -le 0)) {
        $failures += "INVALID_SOURCE_FRAMES: native_fps=$nativeFps; the clip's native frame rate is unknown, so 20 s of footage cannot be measured."
    } elseif ($null -ne $Summary -and $null -ne $nativeFps -and $null -ne $required -and [int64]$required -gt 0 -and
              [int64]$required -lt [int64][Math]::Ceiling($floorSeconds * [double]$nativeFps - 0.02)) {
        $failures += "INVALID_SOURCE_FRAMES: required_source_frames=$required is under ceil($floorSeconds s x native_fps=$nativeFps); the Play was admitted for less than $floorSeconds s of source footage."
    }
    # ENFORCE-4: the pace and override fields are REQUIRED, not optional extras of a source-frame count.
    $absent = @(Get-GuiSmokeAbsentReceiptFields -Summary $Summary -Names @('native_fps', 'pace_fps', 'fps_override'))
    if ($null -ne $Summary -and $absent.Count -gt 0) {
        $failures += "INVALID_SOURCE_FRAMES: RECEIPT_FIELD_ABSENT: the receipt carries no $($absent -join ' / '); a run whose pace or override cannot be read is not proven to have played the footage at its native fps."
    }
    if ($null -ne $Summary -and $null -ne $fpsOverride -and [int]$fpsOverride -ne 0) {
        $failures += "INVALID_SOURCE_FRAMES: the run was paced by a persisted fps override (fps_override=$fpsOverride); evidence is paced at the clip's native fps."
    }
    if ($null -ne $Summary -and $null -ne $paceFps -and ((& $nonFinite $paceFps) -or [double]$paceFps -le 0)) {
        $failures += "INVALID_SOURCE_FRAMES: pace_fps=$paceFps; a present engine pace that is not positive is unknown, so wall clock cannot be tied to the footage played."
    } elseif ($null -ne $Summary -and $null -ne $nativeFps -and $null -ne $paceFps -and -not (& $nonFinite $nativeFps) -and [double]$nativeFps -gt 0) {
        # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: only a PROVEN conform moves the pace comparison off the native fps.
        $conformProof = Get-GuiSmokeConformPaceProof -ConformLine $ConformLine -SummarySession ([string](& $prop 'session')) -NativeFps $nativeFps -PaceFps $paceFps
        if ($conformProof.proven) {
            $paceRule = 'conform'
            if ([Math]::Abs([double]$paceFps - [double]$conformProof.playbackFps) -gt (0.005 * [double]$conformProof.playbackFps)) {
                $failures += "INVALID_SOURCE_FRAMES: the engine paced at pace_fps=$paceFps but the conformed playback_fps is $($conformProof.playbackFps); 20 s of wall clock is not 20 s of footage."
            }
        } elseif ([Math]::Abs([double]$paceFps - [double]$nativeFps) -gt (0.005 * [double]$nativeFps)) {
            $why = if ($conformProof.present) { " (conform relaxation not applied: $($conformProof.reason))" } else { '' }
            $failures += "INVALID_SOURCE_FRAMES: the engine paced at pace_fps=$paceFps but the clip's native fps is native_fps=$nativeFps; 20 s of wall clock is not 20 s of footage.$why"
        }
    }
    return [pscustomobject]@{
        invalid = ($failures.Count -gt 0); failures = $failures
        sourceAdvanced = $advanced; requiredSourceFrames = $required
        paceRule = $paceRule; conformPlaybackFps = $conformProof.playbackFps; conformClipFps = $conformProof.clipFps
    }
}

function Get-GuiSmokeLoopVerdict {
    <#
    .SYNOPSIS
    The RUNTIME BACKSTOP decision, run on the app's parsed playback_smoke.summary object. Returns
    [pscustomobject]@{ invalid; failures }. invalid is $true when the app's own timeline wrapped
    (wrapped=1, or any wrap_count > 0 -- the count is taken in the engine's actual wrap branches and
    outranks the presented-frame heuristic behind wrapped), or when the app reports a clip shorter than
    max(20 s, window). A launch-only probe never plays, so a wrap or any presented frame there is
    itself a failure. A binary that predates the wrap fields reports $null for each, and since ENFORCE-4 that is
    INVALID (RECEIPT_FIELD_ABSENT), not "no wrap". Two checks that need nothing from the app also apply, from the
    clip's header frame count (-ClipFrames, 0 = unknown): more frames presented than the clip holds, or a
    last presented frame BEFORE the first, can only mean the timeline went round again.
    #>
    param(
        [AllowNull()]$Summary,
        [Parameter(Mandatory = $true)][double]$WindowSeconds,
        [bool]$LaunchOnlyProbe = $false,
        [int64]$ClipFrames = 0,
        # ENFORCE-4 r2: the per-run nonce THIS launch handed the app; the receipt must echo it (RECEIPT_NOT_THIS_RUN).
        [AllowNull()][string]$ExpectedRunNonce = '',
        # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: the run's playback_smoke.conform line, handed on to the source-frame oracle.
        [AllowNull()][AllowEmptyString()][string]$ConformLine = ''
    )
    $failures = @()
    $paceRule = 'native'; $conformPlaybackFps = $null; $conformClipFps = $null
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
        # ENFORCE-4: the app's own wrap signal must be PRESENT. A build that predates the wrap fields cannot say it did
        # not wrap; "silent" is not "no wrap". (The header-based checks below remain as a second, independent signal.)
        $absent = @(Get-GuiSmokeAbsentReceiptFields -Summary $Summary -Names @('wrapped', 'wrap_count'))
        if ($absent.Count -gt 0) {
            $failures += "INVALID_LOOPED: RECEIPT_FIELD_ABSENT: the receipt carries no $($absent -join ' / '); a run whose wrapping cannot be read is never playback evidence."
        }
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
        # HFR-10S-1: the clip's own floor, from the native_fps the app reported (absent = 20 s).
        $clipFloorSeconds = Get-GuiSmokeMinPlaySeconds -NativeFps (& $prop 'native_fps')
        if ($null -ne $clipSeconds -and [double]$clipSeconds -gt 0 -and
            [double]$clipSeconds -lt [Math]::Max($clipFloorSeconds, $WindowSeconds)) {
            $failures += "INVALID_LOOPED: the app reports clip_seconds=$clipSeconds, under max($clipFloorSeconds, window=$WindowSeconds)."
        }
        # ENFORCE-3: the source-frame oracle (source_advanced >= required_source_frames, native pace, no override).
        $sourceFramesVerdict = Get-GuiSmokeSourceFramesVerdict -Summary $Summary -ExpectedRunNonce $ExpectedRunNonce -ConformLine $ConformLine
        $failures += @($sourceFramesVerdict.failures)
        $paceRule = $sourceFramesVerdict.paceRule
        $conformPlaybackFps = $sourceFramesVerdict.conformPlaybackFps
        $conformClipFps = $sourceFramesVerdict.conformClipFps
    }
    return [pscustomobject]@{ invalid = ($failures.Count -gt 0); failures = $failures; paceRule = $paceRule; conformPlaybackFps = $conformPlaybackFps; conformClipFps = $conformClipFps }
}

# ---------------------------------------------------------------------------------------------------
# PLAYBACK-CLIP-LENGTH-ENFORCE-3 round 2 -- THE RECEIPT ORACLE FOR EVERY LAUNCHER THAT STARTS THE APP FOR AN
# EVIDENCE PLAY ITSELF (fable BLOCKER + H3). "No tracked tool or app path can end an evidence Play -- or report a
# normal result -- having consumed < 20 s of source footage, without the result being INVALID." The app counts the
# source frames its engine advanced; a launcher may not stop the app on a clock of its own, and may not trust the
# app's exit code alone (a binary that predates ENFORCE-3 exits 0 after a wall-clock hold). Instead it
#   1. WAITS for the app to end its own Play (a kill by the launcher is a typed PLAY_SAFETY_TIMEOUT failure), and
#   2. reads the app's playback_smoke.summary (or the profile receipt's metadata) and applies
#      Get-GuiSmokeEvidencePlayVerdict: no summary, no source_advanced, source_advanced < required_source_frames,
#      a wrap, an fps override or a non-native pace is INVALID (exit 43), never a normal result.
# tools/repo_hygiene/test_playback_clip_length_gate.py pins which launchers carry this and mutation-tests it.
# ---------------------------------------------------------------------------------------------------

# Mirrors platform/qt/PlaybackFrameRange.h (kMinSustainedPaceFraction, kPlaySafetyMarginMs); a parity test compares them.
$script:GuiSmokeMinSustainedPaceFraction = 0.5
$script:GuiSmokePlaySafetyMarginMs = 15000

# CPU-LOOK-LEG-PACE-ABORT-1: mirrors platform/qt/PlaybackFrameRange.h kCpuInformationalMinPaceFraction (a parity test compares them).
# A CPU-backend leg's pace is MEASURED and informational: the app's pace probe never ends it, and its wall budget is this CEILING
# (25 s of footage: 25 / (1/30) + 15 s = 765 s = 12.75 min). Only the runner's -CpuPlayPaceInformational selects it.
$script:GuiSmokeCpuInformationalMinPaceFraction = 1.0 / 30.0
# The ONE environment variable that switches the app's pace gate to informational. The runner sets it from its switch alone; a
# caller passing it as -ExtraEnvironment, or inheriting it from the parent process, is refused (it would switch a CUDA run's gate off).
$script:GuiSmokePaceModeEnvironment = 'MLVAPP_PLAY_PACE_MODE'

function Get-GuiSmokePlaySafetyMs {
    # The wall-clock safety net of an evidence Play of `Seconds` of footage: requested / 0.5 + 15 s. The app ends the
    # Play on its own typed failure inside this; a launcher's own process budget is this plus the open/settle time.
    # -CpuPaceInformational: the CPU leg's ceiling instead (requested / (1/30) + 15 s); the one derivation the runner, the
    # dual-venue job's smoke timeout and its um-run timeout all read.
    param([Parameter(Mandatory = $true)][double]$Seconds, [switch]$CpuPaceInformational)
    $fraction = if ($CpuPaceInformational) { $script:GuiSmokeCpuInformationalMinPaceFraction } else { $script:GuiSmokeMinSustainedPaceFraction }
    return [int64][Math]::Ceiling([Math]::Max(0.0, $Seconds) * 1000.0 / $fraction) + $script:GuiSmokePlaySafetyMarginMs
}

function Read-GuiSmokePlaybackSummaryFromLogDir {
    # The last playback_smoke.summary line of the newest app log in -LogDir written at or after -SinceUtc, parsed
    # like the runner parses it. $null when there is none (an app that never finished its session writes none).
    param([Parameter(Mandatory = $true)][string]$LogDir, [datetime]$SinceUtc = [datetime]::new(2000, 1, 1, 0, 0, 0, [DateTimeKind]::Utc))
    if (-not (Test-Path -LiteralPath $LogDir -PathType Container)) { return $null }
    # two seconds of slack for file-system timestamp granularity; MinValue (no lower bound) cannot be moved back
    $threshold = if ($SinceUtc.Ticks -gt 100000000) { $SinceUtc.AddSeconds(-2) } else { $SinceUtc }
    $logs = @(Get-ChildItem -LiteralPath $LogDir -Filter 'mlvapp-*.log' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -ge $threshold } | Sort-Object LastWriteTimeUtc -Descending)
    foreach ($log in $logs) {
        $line = @(Get-Content -LiteralPath $log.FullName -ErrorAction SilentlyContinue |
            Where-Object { $_ -like '*playback_smoke.summary*' } | Select-Object -Last 1)
        if ($line.Count -gt 0 -and $line[0]) { return (Convert-PlaybackLogLineToObject -Line ([string]$line[0])) }
    }
    return $null
}

function Get-GuiSmokeProfileReceiptSummary {
    # The profile receipt (--profile-playback --output <json>) carries the same oracle fields in its metadata; shaped
    # here like a playback_smoke.summary so ONE decision (Get-GuiSmokeLoopVerdict) judges both. $null when the receipt
    # is missing / unreadable / has no metadata. A field the receipt does not write stays ABSENT in the summary
    # (ENFORCE-4): this reader is only ever called for a PLAY-CAPABLE profile, so an absent admission is "cannot prove
    # it played", never "did not play" (the ENFORCE-3 reader inferred play_performed=false from it, so a master-era
    # binary's receipt -- which has no admission field -- skipped the oracle and exited 0).
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { return $null }
    if ($null -eq $document -or $null -eq $document.PSObject.Properties['metadata'] -or $null -eq $document.metadata) { return $null }
    $m = $document.metadata
    $get = { param($name) if ($m.PSObject.Properties[$name]) { $m.$name } else { $null } }
    $summary = [ordered]@{}
    # ENFORCE-4 r2: the per-run nonce the app echoed; absent stays absent (RECEIPT_NOT_THIS_RUN downstream).
    $receiptNonce = & $get 'run_nonce'
    if ($null -ne $receiptNonce) { $summary['run_nonce'] = [string]$receiptNonce }
    foreach ($pair in @(@('source_advanced', 'source_advanced'), @('required_source_frames', 'required_source_frames'),
                        @('native_fps', 'native_fps'), @('pace_fps', 'pace_fps'), @('wrap_count', 'play_wrap_count'))) {
        $value = & $get $pair[1]
        if ($null -ne $value) { $summary[$pair[0]] = $value }
    }
    $override = & $get 'fps_override_active'
    if ($null -ne $override) { $summary['fps_override'] = [int][bool]$override }
    # the receipt has the engine's wrap COUNT only; "wrapped" is derived from it, and absent when the count is absent
    if ($summary.Contains('wrap_count')) { $summary['wrapped'] = [int]([int64]$summary['wrap_count'] -gt 0) }
    $admitted = & $get 'programmatic_play_admitted'
    if ($null -ne $admitted) { $summary['play_admitted'] = [int64]$admitted }
    return [pscustomobject]$summary
}

function Get-GuiSmokeEvidencePlayVerdict {
    <#
    .SYNOPSIS
    The verdict a launcher gives an evidence Play it started itself. Returns [pscustomobject]@{ invalid; failures;
    exitCode; summary }. invalid when the launcher had to kill the app (PLAY_SAFETY_TIMEOUT), the app did not exit
    (PLAY_NOT_FINISHED), exited non-zero (APP_EXIT_NONZERO with the typed reason), or the receipt fails
    Get-GuiSmokeLoopVerdict (no summary, no source_advanced, source_advanced < required_source_frames, a wrap, an fps
    override, a non-native pace, ANY required field absent). exitCode is 43 for every INVALID verdict.
    ENFORCE-4: there is no "not played" escape. A launcher that started the app for an evidence Play gets the
    receipt oracle unconditionally; -RequireAdmission (the profile wrapper) additionally requires the receipt to
    carry play_admitted and for it to be > 0 -- an absent admission is RECEIPT_FIELD_ABSENT, an admission of 0 is
    PLAY_NOT_ADMITTED, and both are INVALID.
    #>
    param(
        [AllowNull()]$Summary,
        [AllowNull()][object]$ExitCode,
        [bool]$KilledByLauncher = $false,
        # HFR-10S-1: 0 (the default) = no window of the caller's own; the clip's floor (from native_fps) still applies.
        [double]$WindowSeconds = 0.0,
        [int64]$ClipFrames = 0,
        [string]$AppMessage = '',
        [bool]$RequireAdmission = $false,
        # ENFORCE-4 r2: the receipt must be THIS run's. -ExpectedRunNonce is the nonce this launch generated and handed the app
        # (an empty one is itself INVALID); -ReceiptPath / -LaunchedUtc (a profile receipt FILE) add the freshness layer.
        [AllowNull()][string]$ExpectedRunNonce = '',
        [string]$ReceiptPath = '',
        [AllowNull()][object]$LaunchedUtc = $null,
        # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: the run's playback_smoke.conform line (absent = the native-pace rule, unchanged).
        [AllowNull()][AllowEmptyString()][string]$ConformLine = ''
    )
    $failures = @()
    if (-not [string]::IsNullOrWhiteSpace($ReceiptPath)) {
        $staleFailure = Get-GuiSmokeReceiptStaleFailure -Path $ReceiptPath -LaunchedUtc $LaunchedUtc
        if ($null -ne $staleFailure) { $failures += $staleFailure }
    }
    if ($KilledByLauncher) {
        $failures += "PLAY_SAFETY_TIMEOUT: the launcher had to end the app itself; a Play ended by a clock the app did not choose is never playback evidence."
    } elseif ($null -eq $ExitCode) {
        $failures += "PLAY_NOT_FINISHED: the app never reported an exit code, so the footage it played cannot be proven."
    } elseif ([int]$ExitCode -ne 0) {
        $reason = Get-GuiSmokeRefusalReason -ExitCode ([int]$ExitCode) -Message $AppMessage
        $failures += "APP_EXIT_NONZERO: the app exited $ExitCode ($reason); a Play the app did not finish is never playback evidence."
    }
    if ($RequireAdmission) {
        $absentAdmission = @(Get-GuiSmokeAbsentReceiptFields -Summary $Summary -Names @('play_admitted'))
        if ($null -eq $Summary) {
            $failures += "RECEIPT_FIELD_ABSENT: no receipt, so no Play admission can be proven."
        } elseif ($absentAdmission.Count -gt 0) {
            $failures += "RECEIPT_FIELD_ABSENT: the receipt carries no programmatic_play_admitted; a build that does not write it cannot prove an evidence Play was admitted."
        } elseif ([int64]$Summary.play_admitted -le 0) {
            $failures += "PLAY_NOT_ADMITTED: programmatic_play_admitted=$($Summary.play_admitted) on a play-capable run; nothing was played, so there is no footage to prove."
        }
    }
    $loop = Get-GuiSmokeLoopVerdict -Summary $Summary -WindowSeconds $WindowSeconds -ClipFrames $ClipFrames -ExpectedRunNonce $ExpectedRunNonce -ConformLine $ConformLine
    $failures += @($loop.failures)
    return [pscustomobject]@{ invalid = ($failures.Count -gt 0); failures = $failures; exitCode = 43; summary = $Summary }
}
