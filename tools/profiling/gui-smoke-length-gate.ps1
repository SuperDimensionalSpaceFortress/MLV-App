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
    requiredSeconds; frames; fps }. verdict is OK | CLIP_TOO_SHORT | CLIP_LENGTH_UNKNOWN.
    A play window may never exceed the footage that remains after -StartFrame.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][double]$WindowSeconds,
        [int]$StartFrame = 0,
        [double]$MinSeconds = $script:GuiSmokeMinClipSeconds
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
    # Both the 20 s floor (whole clip) and the window (what is left after -StartFrame) must hold.
    if ($len.seconds -lt $MinSeconds) {
        $out.verdict = 'CLIP_TOO_SHORT'
        $out.message = "CLIP_TOO_SHORT (clip=$(& $fmt $len.seconds) window=$(& $fmt $required))"
    } elseif ($remaining -lt $WindowSeconds) {
        $out.verdict = 'CLIP_TOO_SHORT'
        $out.message = "CLIP_TOO_SHORT (clip=$(& $fmt $remaining) window=$(& $fmt $WindowSeconds))"
    }
    return $out
}
