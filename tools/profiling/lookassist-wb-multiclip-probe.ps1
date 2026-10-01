# lookassist-wb-multiclip-probe.ps1 -- CROSS-CLIP Look Assist white-balance matrix (anti-overfit gate).
#
# WHY (2026-06-27, Layi): the WB / green-cast quality work was being judged on ONE clip
# (tests/fixtures/clips/large_dual_iso.mlv -- a daylight pool). A cross-clip probe showed the Look Assist
# auto-WB is CONTENT-DEPENDENT (tint ranged -35 / -26 / -26 / +18 across four clips), so a fix tuned to
# cancel the pool's green could push a magenta clip further off. This tool runs the Look-Assist-ON gui-smoke
# across a SET of clips and reports the applied temp/tint/scene + the settled greenAxis/warmCool measured
# from the actual presented frame, so a WB change is validated across conditions (day/night/indoor/dual-ISO),
# not one scene. Run it BEFORE and AFTER a WB change and diff the matrix.
#
# Repo path has a SPACE: launches the gui-smoke via -File with relative tool paths; the gui-smoke itself
# uses ProcessStartInfo (no Start-Process absolute-path splitting).
param(
    [string]$ExePath = "platform\qt\build-release\release\MLVApp.exe",
    [string]$ClipDir = "C:\temp\MLV",            # default: Layi's preferred realistic clip store
    [string[]]$Clips = @(),                      # explicit clip paths; overrides -ClipDir discovery
    [switch]$IncludeFixture,                     # also probe tests/fixtures/clips/large_dual_iso.mlv
    [int]$MaxClips = 6,                           # cap discovered clips (smallest-first for speed)
    [int]$Seconds = 32,
    [string]$ScaleFactor = "2",
    [string]$OutDir = "",
    [string]$QtBinPrepend = "C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin",
    [int]$ViewportTop = 140, [int]$ViewportLeft = 80, [int]$SampleStride = 8
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$smoke = Join-Path $PSScriptRoot "run-release-gui-smoke.ps1"
if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutDir = Join-Path $repoRoot ".claude-state\profiling\wb-multiclip-$stamp"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$env:PATH = $QtBinPrepend + ";" + $env:PATH

# Build the clip list.
$clipList = @()
if ($Clips.Count -gt 0) { $clipList = $Clips }
elseif (Test-Path $ClipDir) {
    $clipList = Get-ChildItem $ClipDir -Recurse -Filter *.mlv -ErrorAction SilentlyContinue |
        Sort-Object Length | Select-Object -First $MaxClips -Expand FullName
}
if ($IncludeFixture) {
    # PLAYBACK-CLIP-LENGTH-ENFORCE-1 (owner rule 2026-09-30): the tracked fixture is 16 frames (0.67 s) and
    # is never PLAYED; run-release-gui-smoke.ps1 would refuse it (CLIP_TOO_SHORT) and looping it is forbidden.
    throw "-IncludeFixture is refused: the tracked fixture is far under 20 s (CLIP_TOO_SHORT); pass real >= 20 s footage via -Clips."
}
if (-not $clipList -or $clipList.Count -eq 0) { throw "No clips found (-Clips or -ClipDir '$ClipDir')." }

function Get-FrameCast([string]$png) {
    if (-not (Test-Path -LiteralPath $png)) { return $null }
    $bmp = [System.Drawing.Bitmap]::FromFile((Resolve-Path -LiteralPath $png).Path)
    try {
        $r=0.0;$g=0.0;$b=0.0;$n=0
        for ($y=$ViewportTop; $y -lt $bmp.Height-8; $y+=$SampleStride) {
            for ($x=$ViewportLeft; $x -lt $bmp.Width-8; $x+=$SampleStride) {
                $px=$bmp.GetPixel($x,$y); $r+=$px.R; $g+=$px.G; $b+=$px.B; $n++
            }
        }
        if ($n -eq 0) { return $null }
        $R=$r/$n; $G=$g/$n; $B=$b/$n
        [pscustomobject]@{ greenAxis=[math]::Round($G-($R+$B)/2,2); warmCool=[math]::Round($R-$B,2) }
    } finally { $bmp.Dispose() }
}

$rows = @()
foreach ($clip in $clipList) {
    $name = [IO.Path]::GetFileNameWithoutExtension($clip)
    $cdir = Join-Path $OutDir $name
    New-Item -ItemType Directory -Force -Path $cdir | Out-Null
    Write-Host "[wb-matrix] probing $name ..." -ForegroundColor Cyan
    $smokeExit = -1
    try {
        & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $smoke `
            -RepoRoot $repoRoot -ExePath $exe -Input $clip -ScaleFactor $ScaleFactor -ExpectedScaleRequest $ScaleFactor `
            -CaptureScreenshot -Seconds $Seconds -Output (Join-Path $cdir "$name.json") *> (Join-Path $cdir "run.log")
        $smokeExit = $LASTEXITCODE
    } catch { }
    $temp='?';$tint='?';$scene='?';$floor='?'
    $log = Get-ChildItem (Join-Path $cdir "logs") -Filter 'mlvapp-*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    if ($log) {
        $line = Select-String -LiteralPath $log.FullName -Pattern 'auto_wb_async_applied','look_assist.apply.auto_wb' -ErrorAction SilentlyContinue | Select-Object -Last 1
        if ($line) {
            $L=$line.Line
            if ($L -match 'final_temp=(\d+)'){$temp=$Matches[1]}elseif($L -match 'awb_temp=(\d+)'){$temp=$Matches[1]}
            if ($L -match 'final_tint=(-?\d+)'){$tint=$Matches[1]}elseif($L -match 'awb_tint=(-?\d+)'){$tint=$Matches[1]}
            if ($L -match 'scene=(\w+)'){$scene=$Matches[1]}
            if ($L -match 'floor_lifted=(\d)'){$floor=$Matches[1]}
        }
    }
    $win = Get-ChildItem $cdir -Recurse -Filter '*-window.png' -ErrorAction SilentlyContinue | Select-Object -First 1
    $cast = if ($win) { Get-FrameCast $win.FullName } else { $null }
    $tintFlag = if ($tint -ne '?' -and [int]$tint -le -12) {'GREEN'} elseif ($tint -ne '?' -and [int]$tint -ge 12) {'MAGENTA'} else {'neutral-ish'}
    # ENFORCE-3 r2: the runner is the receipt oracle; a clip whose Play did not pass (exit 43 INVALID or any typed
    # failure) is reported as RUN_INVALID, never as a normal row of the matrix.
    if ($smokeExit -ne 0) {
        $temp = '?'; $tint = '?'; $scene = '?'; $floor = '?'; $tintFlag = "RUN_INVALID(runner exit $smokeExit)"; $cast = $null
    }
    $rows += [pscustomobject]@{
        Clip=$name; Temp=$temp; Tint=$tint; TintFlag=$tintFlag; Scene=$scene; Floor=$floor
        FrameGreenAxis=($cast.greenAxis); FrameWarmCool=($cast.warmCool); RunnerExit=$smokeExit
    }
}

"`n===== LOOK ASSIST WB CROSS-CLIP MATRIX (scale=$ScaleFactor, Look Assist ON) ====="
$rows | Format-Table -AutoSize
$matrix = [pscustomobject]@{ schema='mlvapp.lookassist-wb-multiclip.v1'; capturedAtUtc=(Get-Date).ToUniversalTime().ToString('o'); exe=$exe; scale=$ScaleFactor; rows=$rows }
$matrix | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $OutDir 'wb-matrix.json') -Encoding UTF8
"matrix -> $(Join-Path $OutDir 'wb-matrix.json')"
"NOTE: a WB fix must reduce the GREEN clips' tint toward 0 WITHOUT regressing the MAGENTA/neutral clips -- diff this matrix before vs after."
