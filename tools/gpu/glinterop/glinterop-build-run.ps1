# glinterop-build-run.ps1  (RUN ON ULTRA-MAGNUS, over SSH or interactively)
#
# Builds and runs the CUDA<->GL zero-readback present probe on the RTX 4090.
# Unlike build-cuda.ps1 (generic), this links the Win32/GL libs the probe needs:
#   opengl32.lib gdi32.lib user32.lib   (+ CUDA's cudart, auto via nvcc)
#
# The probe exports the NvOptimus/AmdPowerXpress discrete-GPU request itself (no
# per-app Windows GPU preference is written -- that is the owner's state, NA-8),
# and prints the GL_RENDERER it actually got.
#
# Usage (on host):
#   pwsh -NoProfile -ExecutionPolicy Bypass -File glinterop-build-run.ps1 [-W 1808] [-H 2268] [-Iters 60]
param(
    [int]$W     = 1808,
    [int]$H     = 2268,
    [int]$Iters = 60,
    [string]$Arch = 'sm_89'   # Ada Lovelace = RTX 4090
)
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
if (-not $dir) { $dir = 'G:\Temp\mlv-gpu-profile\glinterop' }
$src = Join-Path $dir 'cuda_gl_present_probe.cu'
$out = Join-Path $dir 'cuda_gl_present_probe.exe'

# --- locate MSVC + CUDA (same discovery as build-cuda.ps1) ---
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsRoot = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
if (-not $vsRoot) { throw "MSVC (VC tools) not found via vswhere" }
$vcvars = Join-Path $vsRoot 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "vcvars64 not found: $vcvars" }

$cudaRoot = (Get-ChildItem 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA' -Directory |
    Sort-Object Name -Descending | Select-Object -First 1).FullName
$nvcc = Join-Path $cudaRoot 'bin\nvcc.exe'
if (-not (Test-Path $nvcc)) { throw "nvcc not found under $cudaRoot" }

# --- build: nvcc links Win32/GL libs after the -o ---
$cmdFile = Join-Path $env:TEMP 'build_cuda_gl_present_probe.cmd'
@(
    '@echo off',
    "call `"$vcvars`" >nul",
    "`"$nvcc`" -arch=$Arch -O3 -allow-unsupported-compiler `"$src`" -o `"$out`" -Xlinker `"opengl32.lib`" -Xlinker `"gdi32.lib`" -Xlinker `"user32.lib`"",
    'exit /b %ERRORLEVEL%'
) | Set-Content -Encoding ASCII $cmdFile

Write-Host "build: $src -> $out (arch=$Arch) +opengl32/gdi32/user32"
& cmd /c "`"$cmdFile`"" 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { Write-Host "BUILD FAILED (nvcc exit=$LASTEXITCODE)"; exit 1 }
Write-Host "build OK"

# NA-8: the owner's per-app Windows GPU preference is never written. The probe exports
# NvOptimusEnablement / AmdPowerXpressRequestHighPerformance itself; the GL_RENDERER it prints
# (and its VERDICT) record the adapter it actually got.

# also nudge NVIDIA driver to prefer the high-perf GPU for OpenGL in this process
$env:DXGI_ADAPTER_PREFERENCE = 'High'
$env:__GL_NextGenCompiler     = '1'

# --- run ---
$cudaBin = Join-Path $cudaRoot 'bin'
$env:PATH = "$cudaBin;$env:PATH"
Write-Host "--- running cuda_gl_present_probe ($W x $H, $Iters iters) ---"
& $out $W $H $Iters
$rc = $LASTEXITCODE
Write-Host "probe exit=$rc"
exit $rc
