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
    # before nvcc -- never a bare C1083 at compile time. INCLUDE never reaches a command line (a long one overflowed cmd's 8191-character limit, exit 1):
    # where.exe reads the list from its own environment, first as is (no size limit), then with quotes stripped by DELAYED expansion (& and ! in the
    # value are not re-parsed; past cmd's limit that copy is empty, so a quoted header entry in an over-long INCLUDE is refused typed, never exit 1).
    # where matches files only, so a DIRECTORY named corecrt.h is not the header. where also appends each PATHEXT extension, so both calls run with
    # PATHEXT cleared (a corecrt.h.cmd is not the header). A NAME IS NOT A READABLE HEADER (adversary A1 on 8f761e39): where finds a file another process
    # holds exclusively without opening it, so each hit is TYPEd to nul and counts only if that open succeeds; a locked corecrt.h is exit 9, not C1083.
    # The loops run with delayed expansion OFF so a '!' in a found path reaches `type` unchanged; only the quote-strip copy is made with it ON. The four
    # setlocals unwind in one line, which carries only the verdict out and, before nvcc, gives PATHEXT and the delayed-expansion default back.
    # NO PATH DERIVED FROM SystemRoot REACHES A RE-PARSED COMMAND LINE (adversary A1+A2 on 48fb6bf9): a FOR /F command string is parsed again by a child cmd,
    # which expands a literal %OS% in C:\Win%OS%, and a line read under delayed expansion ON (the registry default) expands !OS! in C:\Win!OS!. So the first
    # command turns delayed expansion off, and where.exe is run DIRECTLY by the batch, by its absolute path (one parse, a value is not re-scanned), with its hit
    # list written to a temp file that the FOR /F then READS (a file name is not a command string). The current directory never changes (adversary A1 on
    # a256baf1: a pushd into System32 made a relative INCLUDE entry resolve there). A System32 without where.exe is exit 9 typed (SYSTEMROOT_UNUSABLE).
    'setlocal DisableDelayedExpansion & set "MLV_UCRT_OK=" & set "MLV_INC=" & set "MLV_HITS=%TEMP%\mlv_ucrt_hits_%RANDOM%%RANDOM%.txt" & if not exist "%SystemRoot%\System32\where.exe" (echo FAIL step=msvcEnvironment reason=SYSTEMROOT_UNUSABLE SystemRoot System32 has no where.exe& exit /b 9)',
    'setlocal DisableDelayedExpansion & set "PATHEXT=" & set "MLV_INC=" & if not defined INCLUDE set "INCLUDE=;"',
    'del /f /q "%MLV_HITS%" 2>nul & "%SystemRoot%\System32\where.exe" $INCLUDE:corecrt.h >"%MLV_HITS%" 2>nul && for /f "usebackq delims=" %%F in ("%MLV_HITS%") do if /i "%%~nxF"=="corecrt.h" type "%%F" >nul 2>&1 && set "MLV_UCRT_OK=1"',
    'setlocal EnableDelayedExpansion & set "MLV_INC="',
    'set "MLV_INC=!INCLUDE:"=!"',
    'setlocal DisableDelayedExpansion & rem MLV_INC is copied into this scope, which reads no variable by !name!',
    'if not defined MLV_UCRT_OK del /f /q "%MLV_HITS%" 2>nul & if not defined MLV_UCRT_OK "%SystemRoot%\System32\where.exe" $MLV_INC:corecrt.h >"%MLV_HITS%" 2>nul && for /f "usebackq delims=" %%F in ("%MLV_HITS%") do if /i "%%~nxF"=="corecrt.h" type "%%F" >nul 2>&1 && set "MLV_UCRT_OK=1"',
    'del /f /q "%MLV_HITS%" 2>nul & endlocal & endlocal & endlocal & endlocal & set "MLV_UCRT_OK=%MLV_UCRT_OK%"',
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
