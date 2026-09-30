# stage-probe-and-job.ps1 (UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1) -- build the Qt screen probe with the
# repo toolchain, package the minimal runtime, and emit the concrete UM job that runs it.
#
#   pwsh -NoProfile -File tools\profiling\qscreen-probe\stage-probe-and-job.ps1 [-OutDir <dir>]
#   pwsh -NoProfile -File tools\profiling\um-run.ps1 -ScriptPath <OutDir>\job.ps1 `
#        -SideFile <OutDir>\qscreen-probe-<sha12>.zip -TimeoutSec 300
#
# The job (tools\profiling\ultramagnus\um-display-mapping-probe-job.ps1) takes the zip name and hash
# as parameters, but the UM agent runs a job with NO arguments, so this script bakes them in by
# replacing that file's param() block with three assignments. Read-only on the display; the probe
# opens no window and reads no clip. Default OutDir is an ignored build directory (platform\build-*).
param(
    [string]$OutDir = (Join-Path $PSScriptRoot '..\..\..\platform\build-qscreen-probe\out'),
    [string]$QtBin = 'C:\Qt\6.10.2\mingw_64\bin',
    [string]$QtPlugins = 'C:\Qt\6.10.2\mingw_64\plugins',
    [string]$MingwBin = 'C:\Qt\Tools\mingw1310_64\bin'
)
$ErrorActionPreference = 'Stop'
$env:PATH = "$QtBin;$MingwBin;$env:PATH"
$here = (Resolve-Path $PSScriptRoot).Path
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory $OutDir -Force | Out-Null }
$sc = (Resolve-Path $OutDir).Path
Push-Location $sc
try {
    qmake "$here\qscreen-probe.pro" 2>&1 | Where-Object { $_ -notmatch 'stash' }
    if ($LASTEXITCODE -ne 0) { throw "qmake failed (exit $LASTEXITCODE)" }
    # An exe left by an earlier successful build must not satisfy the check below after a failed compile.
    if (Test-Path 'release\qscreen-probe.exe') { Remove-Item -LiteralPath 'release\qscreen-probe.exe' -Force }
    mingw32-make -j8 release 2>&1 | Where-Object { $_ -match 'error' }
    if ($LASTEXITCODE -ne 0) { throw "mingw32-make failed (exit $LASTEXITCODE)" }
    if (-not (Test-Path 'release\qscreen-probe.exe')) { throw 'qscreen-probe.exe was not built' }
    $min = Join-Path $sc 'min'
    New-Item -ItemType Directory (Join-Path $min 'platforms') -Force | Out-Null
    Copy-Item release\qscreen-probe.exe $min -Force
    foreach ($f in 'Qt6Core.dll', 'Qt6Gui.dll') { Copy-Item (Join-Path $QtBin $f) $min -Force }
    foreach ($f in 'libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll') { Copy-Item (Join-Path $MingwBin $f) $min -Force }
    Copy-Item (Join-Path $QtPlugins 'platforms\qwindows.dll') (Join-Path $min 'platforms') -Force
    Compress-Archive -Path (Join-Path $min '*') -DestinationPath (Join-Path $sc 'probe.zip') -Force
    $h = (Get-FileHash (Join-Path $sc 'probe.zip') -Algorithm SHA256).Hash.ToLower()
    $zn = "qscreen-probe-$($h.Substring(0, 12)).zip"
    Copy-Item (Join-Path $sc 'probe.zip') (Join-Path $sc $zn) -Force
    $src = Get-Content (Join-Path $here '..\ultramagnus\um-display-mapping-probe-job.ps1') -Raw
    $new = [regex]::Replace($src, '(?s)param\(.*?\r?\n\)\r?\n', "`$ProbeZipName = '$zn'`n`$ProbeZipSha256 = '$h'`n`$ProbeTimeoutSec = 60`n", 1)
    if ($new -eq $src) { throw 'the job param() block was not found' }
    [IO.File]::WriteAllText((Join-Path $sc 'job.ps1'), $new, [Text.UTF8Encoding]::new($false))
    "ZIP=$zn SHA=$h JOB=$(Join-Path $sc 'job.ps1')"
} finally {
    Pop-Location
}
