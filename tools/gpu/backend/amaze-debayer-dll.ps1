# amaze-debayer-dll.ps1  (RUN ON ULTRA-MAGNUS, over SSH)
# Builds the CUDA AMaZE debayer backend DLL and verifies its exported C ABI.
param(
    [string]$Dir = $PSScriptRoot,
    [string]$Arch = 'portable',
    [switch]$RunGlTexture
)
$ErrorActionPreference = 'Stop'

if (-not $Dir) { $Dir = $PSScriptRoot }

$src = Join-Path $Dir 'igpu_amaze_debayer_cuda.cu'
$dll = Join-Path $Dir 'igpu_amaze_debayer_cuda.dll'
$def = Join-Path $Dir 'igpu_amaze_debayer_cuda.def'
$testsrc = Join-Path $Dir 'amaze_dll_test.cpp'
$testexe = Join-Path $Dir 'amaze_dll_test.exe'

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsRoot = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
if (-not $vsRoot) { throw "MSVC (VC tools) not found via vswhere" }
$vcvars = Join-Path $vsRoot 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "vcvars64 not found: $vcvars" }

$cudaSdkRoot = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
if (-not (Test-Path -LiteralPath $cudaSdkRoot)) {
    throw "CUDA Toolkit root not found: $cudaSdkRoot"
}
$cudaRoot = (Get-ChildItem -LiteralPath $cudaSdkRoot -Directory |
    Sort-Object Name -Descending | Select-Object -First 1).FullName
if (-not $cudaRoot) { throw "No CUDA Toolkit version directories found under $cudaSdkRoot" }
$nvcc = Join-Path $cudaRoot 'bin\nvcc.exe'
if (-not (Test-Path $nvcc)) { throw "nvcc not found under $cudaRoot" }

$gpuRoot = (Resolve-Path (Join-Path $Dir '..')).Path

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
    throw "Unsupported CUDA architecture token '$Name'. Use sm_86, sm_89, compute_89, 86, or portable."
}

$requestedArchitectures = @()
if ($Arch -and $Arch.Trim().Equals('portable', [System.StringComparison]::OrdinalIgnoreCase)) {
    $requestedArchitectures = @('sm_86', 'sm_89', 'compute_89')
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

$cmdFile = Join-Path $env:TEMP 'amaze_debayer_dll.cmd'
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
    '"%SystemRoot%\System32\where.exe" $INCLUDE:corecrt.h >"%MLV_HITS%" 2>nul & for /f "usebackq delims=" %%F in ("%MLV_HITS%") do if /i "%%~nxF"=="corecrt.h" type "%%F" >nul 2>&1 && set "MLV_UCRT_OK=1"',
    'setlocal EnableDelayedExpansion & set "MLV_INC="',
    'set "MLV_INC=!INCLUDE:"=!"',
    'setlocal DisableDelayedExpansion & rem MLV_INC is copied into this scope, which reads no variable by !name!',
    'if not defined MLV_UCRT_OK "%SystemRoot%\System32\where.exe" $MLV_INC:corecrt.h >"%MLV_HITS%" 2>nul & for /f "usebackq delims=" %%F in ("%MLV_HITS%") do if /i "%%~nxF"=="corecrt.h" type "%%F" >nul 2>&1 && set "MLV_UCRT_OK=1"',
    'del "%MLV_HITS%" 2>nul & endlocal & endlocal & endlocal & endlocal & set "MLV_UCRT_OK=%MLV_UCRT_OK%"',
    'if not defined MLV_UCRT_OK (echo FAIL step=msvcEnvironment reason=INCLUDE_LACKS_UCRT no INCLUDE entry holds corecrt.h after vcvars64& exit /b 9)',
    'echo === building igpu_amaze_debayer_cuda.dll (nvcc -shared --fmad=false, exports via .def) ===',
    "`"$nvcc`" $archArgLine -O3 --fmad=false -shared -allow-unsupported-compiler -Xcompiler `"/MD`" -Xlinker `"/DEF:$def`" -I `"$Dir`" -I `"$gpuRoot`" `"$src`" -o `"$dll`"",
    'if errorlevel 1 exit /b 11',
    'echo === building amaze_dll_test.exe (cl, LoadLibrary harness, optional WGL texture check) ===',
    "cl /nologo /EHsc /O2 /I `"$Dir`" /I `"$gpuRoot`" `"$testsrc`" /Fe:`"$testexe`" /Fo:`"$Dir\amaze_dll_test.obj`" opengl32.lib user32.lib gdi32.lib",
    'if errorlevel 1 exit /b 12',
    'exit /b 0'
) | Set-Content -Encoding ASCII $cmdFile

Write-Host "build: $src -> $dll (arch=$archLabel)"
& cmd /c "`"$cmdFile`"" 2>&1 | ForEach-Object { Write-Host $_ }
$rc = $LASTEXITCODE
Write-Host "build exit=$rc"
if ($rc -ne 0) { Write-Host 'BUILD FAILED'; exit $rc }

Write-Host '--- artifacts ---'
Get-ChildItem -LiteralPath $Dir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @('igpu_amaze_debayer_cuda.dll', 'igpu_amaze_debayer_cuda.lib', 'igpu_amaze_debayer_cuda.exp', 'amaze_dll_test.exe') } |
    Select-Object Name,Length | Format-Table -AutoSize

Write-Host '--- export verification ---'
$dumpbinCommand = Get-Command dumpbin.exe -ErrorAction SilentlyContinue
$dumpbinPath = $null
if ($dumpbinCommand) {
    $dumpbinPath = $dumpbinCommand.Source
    if (-not $dumpbinPath) { $dumpbinPath = $dumpbinCommand.Path }
}
if (-not $dumpbinPath) {
    $dumpbinFile = Get-ChildItem "$vsRoot\VC\Tools\MSVC" -Recurse -Filter dumpbin.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($dumpbinFile) { $dumpbinPath = $dumpbinFile.FullName }
}
if (-not $dumpbinPath) { throw "dumpbin.exe not found after build" }

$exports = & $dumpbinPath /EXPORTS $dll
$required = @(
    'igpu_amaze_debayer_create',
    'igpu_amaze_debayer_destroy',
    'igpu_amaze_debayer_abi_version',
    'igpu_amaze_debayer_describe',
    'igpu_amaze_debayer_run',
    'igpu_amaze_debayer_run_gl_texture',
    'igpu_amaze_debayer_run_post_wb_gl_texture',
    'igpu_amaze_debayer_run_post_wb_gl_texture_from_r16_gl_texture',
    'igpu_amaze_debayer_run_post_wb_gl_texture_from_device_bayer16',
    'igpu_amaze_debayer_reset_live_gl_texture_resources',
    'igpu_amaze_debayer_set_reduced_hnyquist',
    'igpu_amaze_debayer_last_timing',
    'igpu_amaze_debayer_arm_probe',
    'igpu_amaze_debayer_last_probe',
    'igpu_amaze_debayer_clock_kick_begin',
    'igpu_amaze_debayer_clock_kick_end',
    'igpu_amaze_debayer_capture_input_bayer16',
    'igpu_amaze_debayer_input_bayer16_matches_capture',
    'igpu_amaze_debayer_release_input_capture',
    'igpu_amaze_debayer_download_bayer16'
)
$missing = @()
foreach ($name in $required) {
    if (-not ($exports | Select-String -SimpleMatch $name -Quiet)) {
        $missing += $name
    }
}
if ($missing.Count -gt 0) {
    Write-Host "missing exports: $($missing -join ', ')"
    exit 12
}
Write-Host "export verification: PASS"

if ($RunGlTexture) {
    $cudaBin = Join-Path $cudaRoot 'bin'
    $env:PATH = "$cudaBin;$Dir;$env:PATH"
    Write-Host '--- running amaze_dll_test --gl-texture ---'
    Push-Location $Dir
    & $testexe --gl-texture $dll
    $trc = $LASTEXITCODE
    Pop-Location
    Write-Host "amaze_dll_test exit=$trc"
    exit $trc
}
exit 0
