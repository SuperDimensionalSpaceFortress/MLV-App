# run-ultra-magnus-profile.ps1
# Tier 1 GPU profiling runner. RUN THIS ON ULTRA-MAGNUS (the RTX 4090 host),
# not in the VM. It drives the already-built MinGW MLVApp.exe through the
# existing GPU seams (--gpu-preview-processing / --gpu-bilinear-debayer) at the
# baseline contract (x1 full-res, Sharp, HQ Dual ISO) and writes results next
# to itself so they can be pulled back over SMB.
#
# It is self-contained: it resolves the release exe, clip, and receipt relative
# to its own folder (the deployed bundle layout), confirms the real GL renderer
# (fails loud if it got llvmpipe / an RDP virtual adapter), and prints a CPU-vs-
# GPU comparison table.
#
# Bundle layout this expects (see deploy-to-ultra-magnus.ps1):
#   <bundle>\release\MLVApp.exe (+ Qt DLLs)
#   <bundle>\receipts\large_dual_iso_hq.marxml
#   <bundle>\clips\<clip>.mlv
#   <bundle>\run-ultra-magnus-profile.ps1   (this file)
#   <bundle>\results\                        (created; outputs land here)
#
# IMPORTANT: run in a PHYSICAL/console session on the host, not over RDP. RDP
# virtualizes the display adapter, so OpenGL/GL-present may not see the 4090.
# CUDA compute (Tier 2) is RDP-safe; the GL viewport path is not.

[CmdletBinding()]
param(
    [string]$Clip    = "large_dual_iso.mlv",
    [string]$Receipt = "large_dual_iso_hq.marxml",
    [int]$Frames     = 16,
    [int]$ScaleFactor = 1,
    [string]$ResultLabel = ""
)

$ErrorActionPreference = "Stop"
# Resolve our folder robustly (some host PowerShell contexts leave $PSScriptRoot
# empty during binding); shell-agnostic insurance regardless of pwsh vs 5.1.
$root = $PSScriptRoot
if (-not $root -and $PSCommandPath)               { $root = Split-Path -Parent $PSCommandPath }
if (-not $root -and $MyInvocation.MyCommand.Path) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $root) { throw "Could not resolve script directory; invoke as 'pwsh -File <path>'." }
$exe      = Join-Path $root "release\MLVApp.exe"
$clipPath = Join-Path $root "clips\$Clip"
$rcptPath = Join-Path $root "receipts\$Receipt"
$outDir   = Join-Path $root "results"
if ($ResultLabel) {
    $safeLabel = $ResultLabel -replace '[^A-Za-z0-9._-]', '_'
    $outDir = Join-Path $outDir $safeLabel
}

foreach ($p in @($exe, $clipPath, $rcptPath)) {
    if (-not (Test-Path $p)) { throw "Missing required bundle file: $p" }
}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Force the desktop GL stack (real driver) and x1 full-res preview.
$env:QT_OPENGL = "desktop"
$env:MLVAPP_PLAYBACK_SCALE_FACTOR = "$ScaleFactor"

# On hybrid Intel+NVIDIA hosts the GL context defaults to the iGPU. GpuPreference=2
# ("High performance") steers MLVApp's OpenGL onto the discrete 4090.
$prefKey = 'HKCU:\Software\Microsoft\DirectX\UserGpuPreferences'
New-Item -Path $prefKey -Force | Out-Null
New-ItemProperty -Path $prefKey -Name $exe -Value 'GpuPreference=2;' -PropertyType String -Force | Out-Null

# Profiling matrix: control (CPU) then the two GPU seams, then both.
$runs = @(
    @{ tag = "cpu_baseline";   args = @() },
    @{ tag = "gpu_processing";  args = @("--gpu-preview-processing","gpu") },
    @{ tag = "gpu_debayer";     args = @("--gpu-bilinear-debayer","gpu") },
    @{ tag = "gpu_both";        args = @("--gpu-preview-processing","gpu","--gpu-bilinear-debayer","gpu") }
)

$rows = @()
$renderer = $null
foreach ($run in $runs) {
    $json = Join-Path $outDir ("um_{0}_x{1}.json" -f $run.tag, $ScaleFactor)
    $slog = Join-Path $outDir ("um_{0}_x{1}.stagelog" -f $run.tag, $ScaleFactor)
    $baseArgs = @(
        "--profile-playback",
        "--input", $clipPath,
        "--receipt", $rcptPath,
        "--frames", "$Frames",
        "--threads", "auto",
        "--raw-cache-mb", "0",
        "--output", $json,
        "--stage-log", $slog
    ) + $run.args
    Write-Host ("[RUN] {0}: MLVApp {1}" -f $run.tag, ($run.args -join " "))
    & $exe @baseArgs 2>&1 | Select-String 'PROFILE\] DONE' | ForEach-Object { Write-Host "      $($_.Line)" }

    if (-not (Test-Path $json)) { Write-Warning "no JSON for $($run.tag)"; continue }
    $j = Get-Content $json -Raw | ConvertFrom-Json
    $warm = $j.frames | Where-Object { $_.requested_frame -ge 2 }
    function wavg($p){ if ($warm) { [math]::Round((($warm | Measure-Object $p -Average).Average),1) } else { 0 } }
    $g = ($warm)[[math]::Min(4, $warm.Count-1)]
    if (-not $renderer) { $renderer = $j.metadata.gpu_bilinear_debayer_probe_renderer }
    $rows += [pscustomobject]@{
        run            = $run.tag
        gpu_proc_active = $g.gpu_preview_processing_active
        gpu_deb_active  = $g.gpu_bilinear_debayer_active
        llrawproc_ms   = wavg 'llrawproc_ms'
        processing_ms  = wavg 'processing_ms'
        debayer_ms     = wavg 'debayered_frame_ms'
        p16_total_ms   = wavg 'processed16_total_ms'
        render_ms      = wavg 'render_thread_total_ms'
        latency_ms     = [math]::Round($j.metadata.average_latency_ms,1)
        cadence_ms     = [math]::Round($j.metadata.average_cadence_ms,1)
        fps            = if ($j.metadata.average_cadence_ms) { [math]::Round(1000.0/$j.metadata.average_cadence_ms,1) } else { 0 }
        # round 6 (astra MAJOR, consumer sweep): this script has no PLAYBACK-MEASURE-HOST-LOAD-
        # GATE-1 telemetry at all (no Get-HostLoadSnapshot call anywhere in this file) -- its fps
        # was never checked against host load. Mark it unrecorded/provisional, same stance as
        # compare-machine-perf.ps1's New-ProfileRow/New-FieldLogRow for the identical reason,
        # rather than silently implying a clean signal. This script is outside the
        # compare-machine-perf.ps1 census's reach (a standalone script, not a New-*Row function in
        # that file) -- see PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 6 summary for why it is marked
        # rather than brought under real host-load sampling this round.
        host_load_provisional = $true
    }
}

$env:MLVAPP_PLAYBACK_SCALE_FACTOR = $null

Write-Host ""
Write-Host "=================== RENDERER ==================="
if ($renderer -match "llvmpipe|software|GDI") {
    Write-Host "  !! SOFTWARE GL DETECTED: '$renderer'" -ForegroundColor Red
    Write-Host "  !! Not the 4090. Run in a PHYSICAL/console session (not RDP)," -ForegroundColor Red
    Write-Host "  !! and confirm the NVIDIA desktop GL driver is active."        -ForegroundColor Red
} else {
    Write-Host "  GL renderer: $renderer"
}
Write-Host "==============================================="
Write-Host ""
$rows | Format-Table -AutoSize
$summary = Join-Path $outDir "summary.json"
[pscustomobject]@{
    renderer = $renderer
    clip = $Clip
    receipt = $Receipt
    frames = $Frames
    scale_factor = $ScaleFactor
    result_label = $ResultLabel
    runs = $rows
    host = $env:COMPUTERNAME
    captured = (Get-Date).ToString("o")
} |
    ConvertTo-Json -Depth 6 | Set-Content -Encoding ASCII $summary
Write-Host "Results + summary.json written to: $outDir"
Write-Host "Pull back to the VM with the results-copy step in deploy-to-ultra-magnus.ps1."
exit 0
