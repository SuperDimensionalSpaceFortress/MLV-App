# measure-gpu-vram.ps1  (RUN ON ULTRA-MAGNUS, the RTX 4090 host)
#
# Answers the MLV Batch Orchestrator's GPU scaling questions empirically:
#   Q1  peak per-process VRAM + how it scales with resolution
#   Q2  do N concurrent CUDA processes parallelize or serialize on one GPU,
#       and what does N contexts cost in VRAM
#
# It reuses the already-built recon timing probe (gpu\cuda_recon_probe.exe),
# which allocates the full Dual-ISO recon working set + LUTs and runs the real
# kernel chain. Per-process VRAM is read straight from
# nvidia-smi --query-compute-apps=pid,used_memory (context + all allocations),
# which is exactly the number that sets the orchestrator's hard per-worker cap.
#
# CUDA compute is session-agnostic, so this is RDP/Session-0 safe (unlike the GL
# viewport profiler). No OpenGL is used here.
#
# Output: prints a table + JSON, and writes results\vram-measure-<stamp>.json.
#
# NOTE ON FIDELITY: cuda_recon_probe allocates 5x uint16 + 5x uint32 per-pixel
# buffers + LUTs (raw2ev int, ev2raw int, mix float, fullres float). The shipping
# igpu_recon backend allocates 6x uint16 + 5x uint32 per-pixel and carries mix +
# 2x fullres curves as DOUBLE. So the real backend footprint is approximately
# this measurement + ~16 MB (LUT double vs float, 2 fullres buffers) + 2 bytes
# per pixel. The dominant terms (CUDA context + 32 B/px working set) ARE captured.

[CmdletBinding()]
param(
    [string]$Bundle = 'G:\Temp\mlv-gpu-profile',
    [int]$SampleSeconds = 7,          # how long to sample nvidia-smi per resolution
    [int]$WarmSeconds   = 3           # let the probe allocate + warm before sampling
)
$ErrorActionPreference = 'Stop'

$probe = Join-Path $Bundle 'gpu\cuda_recon_probe.exe'
if (-not (Test-Path $probe)) { throw "probe not found: $probe" }
$resultsDir = Join-Path $Bundle 'results'
New-Item -ItemType Directory -Force -Path $resultsDir | Out-Null
$tmpDir = Join-Path $env:TEMP ('vram_probe_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

function Smi-Apps {
    # returns hashtable pid -> usedMB for current compute apps
    $map = @{}
    $lines = & nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>$null
    foreach ($ln in $lines) {
        if ($ln -match '^\s*(\d+)\s*,\s*(\d+)') { $map[[int]$Matches[1]] = [int]$Matches[2] }
    }
    return $map
}
function Smi-Gpu {
    $ln = (& nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
    if ($ln -match '(\d+)\s*,\s*(\d+)\s*,\s*(\d+)') {
        return [pscustomobject]@{ memUsedMB=[int]$Matches[1]; memTotalMB=[int]$Matches[2]; utilPct=[int]$Matches[3] }
    }
    return [pscustomobject]@{ memUsedMB=0; memTotalMB=0; utilPct=0 }
}
function Parse-ProbeOut([string]$path) {
    $o = [pscustomobject]@{ frameTotalMs=$null; fpsEquiv=$null; kernelsMs=$null; device=$null }
    if (-not (Test-Path $path)) { return $o }
    foreach ($l in (Get-Content $path)) {
        if ($l -match 'device=(.+?)\s+\d+x\d+') { $o.device = $Matches[1].Trim() }
        if ($l -match 'FRAME TOTAL\s+([\d.]+)\s*ms') { $o.frameTotalMs = [double]$Matches[1] }
        if ($l -match '=>\s*([\d.]+)\s*fps-equivalent') { $o.fpsEquiv = [double]$Matches[1] }
        if ($l -match 'device kernels only.*?:\s*([\d.]+)\s*ms') { $o.kernelsMs = [double]$Matches[1] }
    }
    return $o
}

$idle = Smi-Gpu
Write-Host ("Idle GPU: memUsed={0} MB / {1} MB, util={2}%" -f $idle.memUsedMB,$idle.memTotalMB,$idle.utilPct)

# ---------- Q1: per-resolution single-process VRAM ----------
# iters sized so each resolution runs ~14s (completes -> prints timing) while we
# sample VRAM throughout. Hard cap below kills only a hung probe.
$resList = @(
    @{ w=1808; h=2268; label='4.1 MP (test clip)'; iters=4000 },
    @{ w=3840; h=2160; label='4K UHD (8.3 MP)';    iters=2200 },
    @{ w=5568; h=3132; label='5.5K (17.4 MP)';     iters=1100 },
    @{ w=8192; h=4320; label='8K (35.4 MP)';       iters=650  }
)
$resRows = @()
foreach ($r in $resList) {
    $out = Join-Path $tmpDir ("res_{0}x{1}.out" -f $r.w,$r.h)
    $p = Start-Process -FilePath $probe -ArgumentList @("$($r.w)","$($r.h)","$($r.iters)") `
                       -PassThru -RedirectStandardOutput $out -WindowStyle Hidden
    $peakProc = 0; $peakUtil = 0; $peakGpuMem = 0
    $cap = (Get-Date).AddSeconds(60)
    while (-not $p.HasExited -and (Get-Date) -lt $cap) {
        $apps = Smi-Apps
        if ($apps.ContainsKey($p.Id) -and $apps[$p.Id] -gt $peakProc) { $peakProc = $apps[$p.Id] }
        $g = Smi-Gpu
        if ($g.utilPct -gt $peakUtil) { $peakUtil = $g.utilPct }
        if ($g.memUsedMB -gt $peakGpuMem) { $peakGpuMem = $g.memUsedMB }
        Start-Sleep -Milliseconds 400
    }
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 600
    $tm = Parse-ProbeOut $out
    $mp = [math]::Round(($r.w * $r.h) / 1e6, 1)
    # WDDM fallback: per-PID used_memory is often [N/A] on consumer Windows GPUs,
    # so derive the single-process footprint from the global memory.used delta too.
    $globalDelta = $peakGpuMem - $idle.memUsedMB
    $footprint   = if ($peakProc -gt 0) { $peakProc } else { $globalDelta }
    $row = [pscustomobject]@{
        label              = $r.label
        device             = $tm.device
        width              = $r.w
        height             = $r.h
        megapixels         = $mp
        procVramMB         = $peakProc            # per-PID (0/blank if WDDM hides it)
        globalDeltaMB      = $globalDelta         # peak gpu memory.used minus idle
        footprintMB        = $footprint           # best available single-process VRAM
        gpuMemUsedMB       = $peakGpuMem
        gpuUtilPct         = $peakUtil
        frameTotalMs       = $tm.frameTotalMs
        fpsEquiv           = $tm.fpsEquiv
        kernelsMs          = $tm.kernelsMs
        bytesPerPixel      = if ($mp -gt 0 -and $footprint -gt 0) { [math]::Round(($footprint * 1MB) / ($r.w * $r.h), 1) } else { $null }
    }
    $resRows += $row
    Write-Host ("[res] {0,-20} {1,5} MP  procVRAM={2,6} MB  gpuUtilPeak={3,3}%  frame={4} ms ({5} fps-eq)" -f `
        $r.label,$mp,$peakProc,$peakUtil,$tm.frameTotalMs,$tm.fpsEquiv)
}

# ---------- Q2: concurrency (1 / 2 / 4 processes at 4.1 MP) ----------
# Moderate iters so each self-terminates; we compare per-process warm FRAME TOTAL
# (constant => parallel; inflates ~Nx => time-sliced/serialized) and total VRAM.
$concRows = @()
foreach ($N in @(1,2,4)) {
    $procs = @()
    for ($i=0; $i -lt $N; $i++) {
        $out = Join-Path $tmpDir ("conc_N{0}_{1}.out" -f $N,$i)
        $procs += [pscustomobject]@{
            out = $out
            proc = Start-Process -FilePath $probe -ArgumentList @("1808","2268","4000") `
                                 -PassThru -RedirectStandardOutput $out -WindowStyle Hidden
        }
    }
    Start-Sleep -Seconds 2
    $peakTotalProc = 0; $peakUtil = 0; $peakGpuMem = 0
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $running = $procs | Where-Object { -not $_.proc.HasExited }
        $apps = Smi-Apps
        $sum = 0
        foreach ($pp in $procs) { if ($apps.ContainsKey($pp.proc.Id)) { $sum += $apps[$pp.proc.Id] } }
        if ($sum -gt $peakTotalProc) { $peakTotalProc = $sum }
        $g = Smi-Gpu
        if ($g.utilPct -gt $peakUtil) { $peakUtil = $g.utilPct }
        if ($g.memUsedMB -gt $peakGpuMem) { $peakGpuMem = $g.memUsedMB }
        if (-not $running) { break }
        Start-Sleep -Milliseconds 400
    }
    foreach ($pp in $procs) { if (-not $pp.proc.HasExited) { Stop-Process -Id $pp.proc.Id -Force -ErrorAction SilentlyContinue } }
    Start-Sleep -Milliseconds 600
    $perProc = @()
    foreach ($pp in $procs) { $perProc += (Parse-ProbeOut $pp.out).frameTotalMs }
    $valid = $perProc | Where-Object { $null -ne $_ }
    $avgMs = if ($valid.Count) { [math]::Round((($valid | Measure-Object -Average).Average),3) } else { $null }
    $concGlobalDelta = $peakGpuMem - $idle.memUsedMB
    $concRows += [pscustomobject]@{
        processes          = $N
        avgFrameTotalMs    = $avgMs
        perProcessMs       = $perProc
        peakTotalVramMB    = $peakTotalProc      # sum of per-PID (0 if WDDM hides it)
        globalDeltaMB      = $concGlobalDelta    # peak gpu memory.used minus idle (N contexts)
        peakGpuUtilPct     = $peakUtil
    }
    Write-Host ("[conc] N={0}  avgFrame={1} ms  totalVRAM(perPID)={2} MB  globalDelta={3} MB  gpuUtilPeak={4}%" -f $N,$avgMs,$peakTotalProc,$concGlobalDelta,$peakUtil)
}

$result = [pscustomobject]@{
    schema     = 'gpu-vram-measure.v1'
    host       = $env:COMPUTERNAME
    captured   = (Get-Date).ToString('o')
    probe      = $probe
    idleGpu    = $idle
    perResolution = $resRows
    concurrency   = $concRows
    notes      = 'procVramMB = nvidia-smi per-PID used_memory (CUDA context + all device allocations). Real igpu_recon backend ~= this + 16 MB + 2 B/px (double LUTs + extra buffers).'
}
$stamp = (Get-Date -Format 'yyyyMMdd_HHmmss')
$jsonPath = Join-Path $resultsDir "vram-measure-$stamp.json"
$result | ConvertTo-Json -Depth 8 | Set-Content -Encoding ASCII $jsonPath

Write-Host ''
Write-Host '================= PER-RESOLUTION ================='
$resRows | Format-Table label,megapixels,footprintMB,procVramMB,globalDeltaMB,gpuUtilPct,frameTotalMs,fpsEquiv,bytesPerPixel -AutoSize
Write-Host '================= CONCURRENCY (4.1 MP) ============'
$concRows | Format-Table processes,avgFrameTotalMs,peakTotalVramMB,globalDeltaMB,peakGpuUtilPct -AutoSize
Write-Host ''
Write-Host "JSON written: $jsonPath"
Write-Host '--- BEGIN JSON ---'
Get-Content $jsonPath -Raw
Write-Host '--- END JSON ---'
