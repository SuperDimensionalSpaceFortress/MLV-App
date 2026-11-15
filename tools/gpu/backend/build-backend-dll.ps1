# build-backend-dll.ps1  (RUN ON ULTRA-MAGNUS, over SSH)
# Builds the igpu_recon CUDA backend DLL (+ import lib) with nvcc, then builds
# the host LoadLibrary test harness with MSVC cl, and runs it against the oracle
# vectors to prove 0-LSB parity THROUGH the ABI. The default Arch is "portable",
# which emits a small fat DLL for common NVIDIA laptop/desktop generations.
#
# Layout expected in $Dir (the backend deploy dir):
#   igpu_recon_cuda.cu, dll_test.cpp, igpu_recon.h
# Outputs (in $Dir):
#   igpu_recon_cuda.dll, igpu_recon_cuda.lib, igpu_recon_cuda.exp, dll_test.exe
param(
    [string]$Dir      = $PSScriptRoot,
    [string]$Vectors  = 'G:\Temp\mlv-gpu-profile\oracle\vectors',
    [string]$Arch     = 'portable',
    [string[]]$CudaArchitectures = @(),
    [switch]$RunGlTexture
)
$ErrorActionPreference = 'Stop'
if (-not $Dir) { $Dir = 'G:\Temp\mlv-gpu-profile\backend' }

$src     = Join-Path $Dir 'igpu_recon_cuda.cu'
$dll     = Join-Path $Dir 'igpu_recon_cuda.dll'
$archSidecar = Join-Path $Dir 'igpu_recon_cuda.arch.json'
$def     = Join-Path $Dir 'igpu_recon_cuda.def'
$testsrc = Join-Path $Dir 'dll_test.cpp'
$testexe = Join-Path $Dir 'dll_test.exe'
$gpuRoot = (Resolve-Path (Join-Path $Dir '..')).Path

# locate MSVC vcvars64 + nvcc (same discovery as build-cuda.ps1)
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsRoot = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
if (-not $vsRoot) { throw "MSVC (VC tools) not found via vswhere" }
$vcvars = Join-Path $vsRoot 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "vcvars64 not found: $vcvars" }

$cudaRoot = (Get-ChildItem 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA' -Directory |
    Sort-Object Name -Descending | Select-Object -First 1).FullName
$nvcc = Join-Path $cudaRoot 'bin\nvcc.exe'
if (-not (Test-Path $nvcc)) { throw "nvcc not found under $cudaRoot" }
$cudaBin = Join-Path $cudaRoot 'bin'

function Convert-CudaArchitectureToGencode {
    param([Parameter(Mandatory = $true)][string]$Name)

    $clean = $Name.Trim()
    if ([string]::IsNullOrWhiteSpace($clean)) {
        return @()
    }
    if ($clean -match '^sm_([0-9]+)$') {
        return @("-gencode=arch=compute_$($Matches[1]),code=$clean")
    }
    if ($clean -match '^compute_([0-9]+)$') {
        return @("-gencode=arch=$clean,code=$clean")
    }
    if ($clean -match '^([0-9]+)$') {
        return @("-gencode=arch=compute_$clean,code=sm_$clean")
    }
    throw "Unsupported CUDA architecture token '$Name'. Use sm_89, compute_89, 89, or portable."
}

$requestedArchitectures = @()
if ($CudaArchitectures -and $CudaArchitectures.Count -gt 0) {
    $requestedArchitectures = @($CudaArchitectures)
} elseif ($Arch -and $Arch.Trim().Equals('portable', [System.StringComparison]::OrdinalIgnoreCase)) {
    $requestedArchitectures = @('sm_61', 'sm_75', 'sm_86', 'sm_89', 'compute_89')
} else {
    $requestedArchitectures = @($Arch -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$archFlags = @()
foreach ($architecture in $requestedArchitectures) {
    $archFlags += Convert-CudaArchitectureToGencode $architecture
}
if ($archFlags.Count -eq 0) {
    throw "No CUDA architectures requested."
}
$archLabel = ($requestedArchitectures -join ',')
$archArgLine = ($archFlags -join ' ')

function Get-CudaArchitectureTokensFromText {
    param([AllowNull()][string[]]$Text)

    @($Text |
        ForEach-Object { [string]$_ } |
        ForEach-Object { [regex]::Matches($_, "(?:sm|compute)_[0-9]{2,3}") } |
        ForEach-Object { $_.Value } |
        Sort-Object -Unique)
}

function Write-CudaBackendArchitectureSidecar {
    param(
        [Parameter(Mandatory = $true)][string]$DllPath,
        [Parameter(Mandatory = $true)][string]$SidecarPath,
        [Parameter(Mandatory = $true)][string]$CuobjdumpPath,
        [Parameter(Mandatory = $true)][string[]]$RequestedArchitectures,
        [Parameter(Mandatory = $true)][string[]]$ArchFlags
    )

    if (!(Test-Path -LiteralPath $CuobjdumpPath -PathType Leaf)) {
        throw "cuobjdump not found: $CuobjdumpPath"
    }
    if (!(Test-Path -LiteralPath $DllPath -PathType Leaf)) {
        throw "backend DLL not found for architecture sidecar: $DllPath"
    }

    $listElf = @(& $CuobjdumpPath --list-elf $DllPath 2>&1 | ForEach-Object { [string]$_ })
    $listElfExit = $LASTEXITCODE
    $ptx = @(& $CuobjdumpPath --dump-ptx $DllPath 2>&1 | ForEach-Object { [string]$_ })
    $dumpPtxExit = $LASTEXITCODE
    $ptxArchLines = @($ptx |
        Where-Object { $_ -match "\.(version|target)|arch\s*=\s*(?:sm|compute)_[0-9]{2,3}|(?:sm|compute)_[0-9]{2,3}" } |
        Select-Object -First 120)
    $tokens = Get-CudaArchitectureTokensFromText -Text (@($listElf) + @($ptxArchLines))
    if ($listElfExit -ne 0 -or $tokens.Count -eq 0) {
        throw "cuobjdump architecture inspection failed; list-elf exit=$listElfExit tokens=$($tokens.Count)"
    }

    $item = Get-Item -LiteralPath $DllPath
    $sidecar = [ordered]@{
        schema = 'mlvapp.cuda-backend-architecture-sidecar.v1'
        createdAtUtc = (Get-Date).ToUniversalTime().ToString('o')
        backend = [ordered]@{
            fileName = $item.Name
            length = $item.Length
            sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
        }
        requestedArchitectures = @($RequestedArchitectures)
        archFlags = @($ArchFlags)
        detector = 'cuobjdump'
        detectionReliable = $true
        tokens = @($tokens)
        hasSm86 = [bool]($tokens -contains 'sm_86')
        hasSm89 = [bool]($tokens -contains 'sm_89')
        dellAmpereReady = [bool]($tokens -contains 'sm_86')
        ultraMagnusAdaReady = [bool](($tokens -contains 'sm_89') -or ($tokens -contains 'compute_89'))
        tool = [ordered]@{
            cuobjdump = $CuobjdumpPath
        }
        raw = [ordered]@{
            listElfExit = $listElfExit
            listElf = @($listElf | Select-Object -First 120)
            dumpPtxExit = $dumpPtxExit
            ptxArchLines = @($ptxArchLines)
        }
    }
    $sidecar | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $SidecarPath -Encoding UTF8
}

# Single .cmd: set MSVC env once, then build DLL (nvcc -shared) + test (cl).
# nvcc -shared emits the .dll AND the import .lib/.exp automatically.
# --fmad=false for IEEE float parity with the CPU oracle (same as parity probe).
$cmdFile = Join-Path $env:TEMP 'build_backend_dll.cmd'
@(
    '@echo off',
    "call `"$vcvars`" >nul",
    # UM-DLLPAIR-VCVARS-PATH-OVERFLOW-1: an INCLUDE without the SDK's corecrt.h (an overgrown PATH broke winsdk.bat on 2026-11-15) is exit 9, typed,
    # before nvcc -- never a bare C1083 at compile time.
    'set "MLV_UCRT_OK=" & if not defined INCLUDE set "INCLUDE=;"',
    'for %%I in ("%INCLUDE:;=" "%") do if exist "%%~I\corecrt.h" if not exist "%%~I\corecrt.h\" set "MLV_UCRT_OK=1"',
    'if not defined MLV_UCRT_OK (echo FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT no INCLUDE entry holds corecrt.h after vcvars64& exit /b 9)',
    'echo === building igpu_recon_cuda.dll (nvcc -shared --fmad=false, exports via .def) ===',
    "`"$nvcc`" $archArgLine -O3 --fmad=false -shared -allow-unsupported-compiler -Xcompiler `"/MD`" -Xlinker `"/DEF:$def`" -I `"$Dir`" -I `"$gpuRoot`" `"$src`" -o `"$dll`"",
    'if errorlevel 1 exit /b 11',
    'echo === building dll_test.exe (cl, LoadLibrary harness, no CUDA link) ===',
    "cl /nologo /EHsc /O2 /I `"$Dir`" /I `"$gpuRoot`" `"$testsrc`" /Fe:`"$testexe`" /Fo:`"$Dir\dll_test.obj`" opengl32.lib user32.lib gdi32.lib",
    'if errorlevel 1 exit /b 12',
    'exit /b 0'
) | Set-Content -Encoding ASCII $cmdFile

Write-Host "build: $src -> $dll  ;  $testsrc -> $testexe  (arch=$archLabel)"
& cmd /c "`"$cmdFile`"" 2>&1 | ForEach-Object { Write-Host $_ }
$rc = $LASTEXITCODE
Write-Host "build exit=$rc"
if ($rc -ne 0) { Write-Host 'BUILD FAILED'; exit $rc }

$cuobjdump = Join-Path $cudaBin 'cuobjdump.exe'
Write-Host '--- writing architecture sidecar (cuobjdump, hash-bound) ---'
Write-CudaBackendArchitectureSidecar `
    -DllPath $dll `
    -SidecarPath $archSidecar `
    -CuobjdumpPath $cuobjdump `
    -RequestedArchitectures $requestedArchitectures `
    -ArchFlags $archFlags
Write-Host "architecture sidecar: $archSidecar"

# show emitted artifacts
Write-Host '--- artifacts ---'
Get-ChildItem $Dir -Include igpu_recon_cuda.dll,igpu_recon_cuda.lib,igpu_recon_cuda.exp,igpu_recon_cuda.arch.json,dll_test.exe -File -ErrorAction SilentlyContinue |
    Select-Object Name,Length | Format-Table -AutoSize

# run the LoadLibrary parity harness (needs cudart on PATH)
$env:PATH = "$cudaBin;$Dir;$env:PATH"
Write-Host '--- running dll_test (LoadLibrary + GetProcAddress + run + compare) ---'
Push-Location $Dir
if ($RunGlTexture) {
    & $testexe --gl-texture $Vectors $dll
} else {
    & $testexe $Vectors $dll
}
$prc = $LASTEXITCODE
Pop-Location
Write-Host "dll_test exit=$prc"
exit $prc
