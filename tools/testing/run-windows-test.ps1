param(
    [string]$RepoRoot = ".",
    [ValidateSet("", "console", "pipeline", "gui", "perf")]
    [string]$Suite = "",
    [string]$ExePath = "",
    [string[]]$TestArgs = @(),
    [switch]$SkipDeployRuntime
)

$ErrorActionPreference = "Stop"

function Enable-NoPopupErrorMode {
    if ($env:OS -ne "Windows_NT") {
        return
    }
    if (-not ("Win32ErrorMode" -as [type])) {
        Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class Win32ErrorMode
{
    [DllImport("kernel32.dll")]
    public static extern UInt32 SetErrorMode(UInt32 uMode);
}
"@
    }

    $semFailCriticalErrors = 0x0001
    $semNoGpFaultErrorBox = 0x0002
    $semNoOpenFileErrorBox = 0x8000
    [void][Win32ErrorMode]::SetErrorMode(
        $semFailCriticalErrors -bor
        $semNoGpFaultErrorBox -bor
        $semNoOpenFileErrorBox)
}

function Get-ExistingDirectoryList {
    param([string[]]$Candidates)

    $seen = @{}
    $dirs = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        try {
            $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
        } catch {
            continue
        }
        $key = $resolved.ToLowerInvariant()
        if (-not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            $dirs.Add($resolved)
        }
    }
    return $dirs.ToArray()
}

function Get-TestExeName {
    param([string]$SuiteName)

    switch ($SuiteName) {
        "console" { return "console_tests.exe" }
        "pipeline" { return "pipeline_tests.exe" }
        "gui" { return "gui_tests.exe" }
        "perf" { return "perf_tests.exe" }
    }
    throw "Pass -Suite or -ExePath."
}

function Resolve-TestExe {
    param(
        [string]$Root,
        [string]$SuiteName,
        [string]$RequestedExePath
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedExePath)) {
        return (Resolve-Path -LiteralPath $RequestedExePath -ErrorAction Stop).Path
    }

    $exeName = Get-TestExeName -SuiteName $SuiteName
    $candidateRelativePaths = @(
        "tests\build-ci-$SuiteName\release\$exeName",
        "tests\build-$SuiteName\release\$exeName",
        "tests\build-release-$SuiteName\release\$exeName",
        "tests\build-codex-ci-$SuiteName\release\$exeName"
    )

    foreach ($relativePath in $candidateRelativePaths) {
        $candidate = Join-Path $Root $relativePath
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $found = Get-ChildItem -LiteralPath (Join-Path $Root "tests") `
        -Recurse `
        -Filter $exeName `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($found) {
        return $found.FullName
    }

    throw "Could not find $exeName under $Root\tests. Build the suite first."
}

$root = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
$exe = Resolve-TestExe -Root $root -SuiteName $Suite -RequestedExePath $ExePath
$exeDir = Split-Path -Parent $exe

# SUITE-RUNNER-REPO-ROOT-PIN-1: an explicit -RepoRoot is the checkout the test reads its fixtures from. The exe refuses it
# (exit 86, "[repo_paths] REFUSED REPO_ROOT_MISMATCH ...") when it was built from a different checkout; tests/common/repo_paths.h.
if ($PSBoundParameters.ContainsKey('RepoRoot')) {
    $env:MLVAPP_TEST_REPO_ROOT = $root
    Write-Host ("[run-windows-test] repo_root_pin={0}" -f $root)
}

Enable-NoPopupErrorMode

$pathDirs = @()
if (-not [string]::IsNullOrWhiteSpace($env:PATH)) {
    $pathDirs = $env:PATH -split ';'
}

$qtRootBin = ""
if (-not [string]::IsNullOrWhiteSpace($env:QT_ROOT_DIR)) {
    $qtRootBin = Join-Path $env:QT_ROOT_DIR "bin"
}

$mingwRootBin = ""
if (-not [string]::IsNullOrWhiteSpace($env:MINGW_ROOT)) {
    $mingwRootBin = Join-Path $env:MINGW_ROOT "bin"
}

$runtimeDirs = Get-ExistingDirectoryList @(
    $exeDir,
    $qtRootBin,
    "C:\Qt\6.10.2\mingw_64\bin",
    $mingwRootBin,
    "C:\Qt\Tools\mingw1310_64\bin",
    $pathDirs
)

if ($runtimeDirs.Count -eq 0) {
    throw "No Qt/MinGW runtime directories were found."
}

$env:PATH = ($runtimeDirs -join ';') + ';' + $env:PATH

if (-not $SkipDeployRuntime -and $env:OS -eq "Windows_NT") {
    $deployScript = Join-Path $root "tools\testing\deploy-windows-test-runtime.ps1"
    if (Test-Path -LiteralPath $deployScript -PathType Leaf) {
        & $deployScript `
            -TargetDir $exeDir `
            -QtBinDir "C:\Qt\6.10.2\mingw_64\bin" `
            -ExeName (Split-Path -Leaf $exe)
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
}

Write-Host ("[run-windows-test] exe={0}" -f $exe)
Write-Host ("[run-windows-test] path_prefix={0}" -f (($runtimeDirs | Select-Object -First 5) -join ';'))
$runner = Join-Path $root "tools\coordination\invoke-mlv-exclusive.ps1"
$testArgsJson = ConvertTo-Json -InputObject @($TestArgs) -Compress
$testArgsBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($testArgsJson))
& pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner `
    -RepoRoot $root -Owner "test:${Suite}:$env:USERNAME" -FilePath $exe -ArgumentListBase64 $testArgsBase64
exit $LASTEXITCODE
