# Filmstrip color-balance trace: per-frame mean R/G/B over the viewport region
# of validate-visible-playback cap-*.png filmstrips, plus arm-vs-arm divergence.
# Pure post-processing of existing captures - never touches measurement runs.
# Usage: filmstrip-balance-trace.ps1 -Dirs dirA,dirB [-ViewportTop 140] [-ViewportLeft 80]
param(
    [Parameter(Mandatory = $true)][string[]]$Dirs,
    [int]$ViewportTop = 140,     # skip title bar / toolbar / timecode strip
    [int]$ViewportLeft = 80,     # skip session panel
    [int]$SampleStride = 8       # sample every Nth pixel in both axes
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
# -File hands '-Dirs a,b' over as ONE string; split it unless the whole value is itself a real directory.
$Dirs = @($Dirs | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } } | ForEach-Object { $_.Trim() } | Where-Object { $_ })

function Get-FrameBalance([string]$path, [int]$top, [int]$left, [int]$stride) {
    $bmp = [System.Drawing.Bitmap]::FromFile($path)
    try {
        $rSum = [double]0; $gSum = [double]0; $bSum = [double]0; $n = 0
        for ($y = $top; $y -lt $bmp.Height - 8; $y += $stride) {
            for ($x = $left; $x -lt $bmp.Width - 8; $x += $stride) {
                $px = $bmp.GetPixel($x, $y)
                $rSum += $px.R; $gSum += $px.G; $bSum += $px.B; $n++
            }
        }
        if ($n -eq 0) { return $null }
        [pscustomobject]@{ R = [math]::Round($rSum / $n, 2); G = [math]::Round($gSum / $n, 2); B = [math]::Round($bSum / $n, 2) }
    } finally { $bmp.Dispose() }
}

$traces = @{}
foreach ($d in $Dirs) {
    $name = Split-Path $d -Leaf
    $rows = @()
    foreach ($cap in (Get-ChildItem $d -Filter 'cap-*.png' | Sort-Object Name)) {
        $bal = Get-FrameBalance $cap.FullName $ViewportTop $ViewportLeft $SampleStride
        if ($bal) {
            $rows += [pscustomobject]@{
                cap = $cap.Name; R = $bal.R; G = $bal.G; B = $bal.B
                # warm-cool axis (R-B) and green axis (G - (R+B)/2): cast signatures
                warmCool = [math]::Round($bal.R - $bal.B, 2)
                greenAxis = [math]::Round($bal.G - (($bal.R + $bal.B) / 2.0), 2)
            }
        }
    }
    $traces[$name] = $rows
    Write-Host "=== $name ==="
    $rows | Format-Table -AutoSize | Out-String | Write-Host
}

if ($Dirs.Count -eq 2) {
    $a = $traces[(Split-Path $Dirs[0] -Leaf)]
    $b = $traces[(Split-Path $Dirs[1] -Leaf)]
    $n = [math]::Min($a.Count, $b.Count)
    Write-Host "=== ARM DIVERGENCE (A - B per matched cap index) ==="
    $maxWarm = 0.0; $maxGreen = 0.0
    for ($i = 1; $i -lt $n; $i++) {  # skip cap-000 (pre-play/paused frame)
        $dw = [math]::Round($a[$i].warmCool - $b[$i].warmCool, 2)
        $dg = [math]::Round($a[$i].greenAxis - $b[$i].greenAxis, 2)
        if ([math]::Abs($dw) -gt [math]::Abs($maxWarm)) { $maxWarm = $dw }
        if ([math]::Abs($dg) -gt [math]::Abs($maxGreen)) { $maxGreen = $dg }
        Write-Host ("  {0}: dWarmCool={1,7}  dGreenAxis={2,7}" -f $a[$i].cap, $dw, $dg)
    }
    # Threshold heuristic: matched playback frames a step apart should agree within
    # a few counts; a uniform WB-class cast shows up as a consistent multi-count
    # offset on one axis. Content motion causes noise, so judge the MEDIAN trend.
    Write-Host ("BALANCE-TRACE worst divergence: warmCool={0} greenAxis={1} (|>6| sustained = investigate)" -f $maxWarm, $maxGreen)
}
