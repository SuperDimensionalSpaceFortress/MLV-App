# playback-attr-3-cuda-assemble.ps1 -- RUNS ON THE BOARD HOST (the machine with Qt + MinGW).
# Builds the Release MLV-App exe from a clean `git archive` of -SourceCommit, deploys the Qt
# runtime beside it together with the CUDA DLL pair built on Ultra-Magnus, and publishes the
# package into -OutDir in exactly the layout
# tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 (the attribution job) verifies:
#
#   MLVApp-playback-attr-3-cuda-<sha12>-pkg.zip   the deployed release dir, zipped as-is
#   MLVApp-playback-attr-3-cuda-<sha12>.exe       the stamped executable
#   igpu_recon_cuda-playback-attr-3-cuda-<sha12>.dll
#   playback-attr-3-cuda-<sha12>-build.json       written LAST; its presence means the rest is
#                                                 complete and hash-verified
#
# WHY THE BUILD IS SPLIT (swarm ruling 2026-09-16,
# .claude-state/fleet-runs/swarm-attr3-buildhost-20260916T2150Z/SYNTHESIS.md). Bachelor -- the
# measurement host -- has Visual Studio without the VC tools component and no CUDA toolkit, so
# nothing compiles there; Ultra-Magnus has nvcc but no qmake or MinGW, so the Qt exe cannot be
# built there either. The CUDA DLL pair comes from
# tools/profiling/ultramagnus/playback-attr-3-cuda-dll-job.ps1, whose published artifacts
# directory is this script's -DllPairDir; the exe is built here; the package is staged into the
# Bachelor cache by tools/profiling/bachelor/playback-attr-3-cuda-stage-job.ps1.
#
# The DLL pair is verified before it is deployed: the manifest's sourceCommit must equal
# -SourceCommit and every file's lowercase sha256 must match the bytes on disk. A package whose
# DLLs were built from other source is the exact failure this binding exists to stop.
#
# The build steps are the ones the retired compile-on-Bachelor job carried (build_buildinfo.h
# injection before qmake, qmake, mingw32-make -B release, windeployqt, the four MinGW runtime
# DLLs --compiler-runtime misses); the naming convention and the buildinfo header they share
# with the staging job are factored into AttrCudaArtifacts.psm1 beside this file. The rest of
# that job's body lived inside an emitted here-string template and is not reusable as code.
#
# NO FOOTAGE. This script compiles, packages and hashes. It never opens, names, globs or
# resolves a media file.
#
# Exit codes: 2 source, 3 DLL-pair manifest missing, 4 DLL-pair manifest does not bind,
# 12 qmake discovery, 13 qmake, 14 mingw32-make discovery, 15 make, 16 release exe missing,
# 17 windeployqt discovery, 18 windeployqt, 19 DLL-pair deploy, 21 MinGW runtime deploy,
# 23 launch probe, 20 packaging, 24 publish partials, 25 publish rename, 26 publish manifest.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-assemble.ps1 `
#       -SourceCommit <40-hex> -DllPairDir <dir> -OutDir <dir>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    # The Ultra-Magnus DLL-pair job's published artifacts directory (or a copy of it): holds
    # dll-pair-manifest.json plus the files it names.
    [Parameter(Mandatory = $true)]
    [string]$DllPairDir,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,

    [string]$QtBin = 'C:\Qt\6.10.2\mingw_64\bin',
    [string]$MingwBin = 'C:\Qt\Tools\mingw1310_64\bin',

    # Optional fully deployed release tree to mirror the reviewed non-Qt runtime payload from
    # (FFmpeg av*/sw*), exactly as tools/build-release.ps1 does. Absent, the package still
    # launches -- those DLLs are runtime-loaded, not load-time imports -- and build.json records
    # ffmpegRuntimeDeployed=false rather than implying a payload that is not there.
    [string]$RuntimeDonorDir = '',

    [int]$MakeJobs = 4,

    # The launch probe constructs a real QApplication with a sanitized PATH, so a missing
    # deploy fails here instead of on the measurement host. -SkipLaunchProbe exists for hosts
    # where no interactive session is available.
    [switch]$SkipLaunchProbe
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force

$StepLog = [System.Collections.Specialized.OrderedDictionary]::new()
function Say([string]$Message) { Write-Host "[attr3-assemble] $Message" }
function Get-ShaLower([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Complete-Failed([int]$Code, [string]$Step, [string]$Message) {
    $StepLog[$Step] = $Code
    Write-Host "[attr3-assemble] FAIL step=$Step exit=$Code $Message" -ForegroundColor Red
    Write-Output "RESULT=ASSEMBLE_FAILED STEP=$Step EXIT=$Code"
    exit $Code
}

$names = Get-AttrCudaArtifactNames -SourceCommit $SourceCommit

# --- source ----------------------------------------------------------------------------------
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
& git -C $RepoRoot cat-file -e "$SourceCommit^{commit}" 2>$null
if ($LASTEXITCODE -ne 0) { Complete-Failed 2 'sourceCommit' "not a commit known to $RepoRoot : $SourceCommit" }

if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$Work = Join-Path $OutDir ".work-$($names.shortSha)"
Remove-AttrCudaTree -TrustedRoot $OutDir -Path $Work
New-Item -ItemType Directory -Path $Work -Force | Out-Null

# Job-owned TEMP under the work dir, set before any child process (qmake, mingw32-make,
# windeployqt, the launch probe) so none of them writes into the ambient machine TEMP.
$Scratch = Join-Path $Work '.job-tmp'
New-Item -ItemType Directory -Path $Scratch -Force | Out-Null
$env:TEMP = $Scratch
$env:TMP = $Scratch

$archivePath = Join-Path $Scratch "$($names.shortSha)-source.zip"
& git -C $RepoRoot archive --format=zip -o $archivePath $SourceCommit
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archivePath)) {
    Complete-Failed 2 'sourceArchive' "git archive failed for $SourceCommit"
}
# Same binding the Ultra-Magnus DLL-pair job applies to its own archive, through the same
# function (sol, PR #133 r2). The sha256 leg is a round-trip check here -- this process wrote
# the file moments ago -- but the COMMIT leg is not: it reads back the id git stamped into the
# zip comment and refuses an archive of anything other than $SourceCommit, which is the claim
# every later artifact name and the injected buildinfo header rest on. The verified sha is
# recorded in build.json, so the exe's source is auditable from the manifest alone.
$sourceArchiveSha256 = Get-ShaLower $archivePath
try {
    [void](Assert-AttrCudaSourceArchive -ArchivePath $archivePath -ExpectedSha256 $sourceArchiveSha256 -ExpectedCommit $SourceCommit)
} catch {
    Complete-Failed 2 'sourceArchive' $_.Exception.Message
}
Say "SOURCE ARCHIVE bound sha256=$sourceArchiveSha256 commit=$SourceCommit"
$SourceTree = Join-Path $Work 'src'
New-Item -ItemType Directory -Path $SourceTree -Force | Out-Null
Expand-Archive -LiteralPath $archivePath -DestinationPath $SourceTree -Force
$StepLog['sourceArchive'] = 0

# --- verify the DLL pair BEFORE it is deployed -------------------------------------------------
if (-not (Test-Path -LiteralPath $DllPairDir -PathType Container)) {
    Complete-Failed 3 'dllPairDir' "not a directory: $DllPairDir"
}
$DllPairDir = (Resolve-Path -LiteralPath $DllPairDir).Path
$dllPairManifestPath = Join-Path $DllPairDir 'dll-pair-manifest.json'
if (-not (Test-Path -LiteralPath $dllPairManifestPath)) {
    Complete-Failed 3 'dllPairManifest' "missing dll-pair-manifest.json in $DllPairDir (the DLL-pair job writes it LAST; its absence means that job did not complete)"
}
$dllPairManifest = Get-Content -Raw -LiteralPath $dllPairManifestPath | ConvertFrom-Json
$dllPairManifestSha256 = Get-ShaLower $dllPairManifestPath
if ($dllPairManifest.sourceCommit -ne $SourceCommit) {
    Complete-Failed 4 'dllPairBinding' "DLL pair was built from $($dllPairManifest.sourceCommit), not $SourceCommit"
}
if ($dllPairManifest.pendingSymbolPresence -isnot [bool]) {
    Complete-Failed 4 'dllPairBinding' "dll-pair-manifest.json has no boolean pendingSymbolPresence"
}
if (-not $dllPairManifest.files) { Complete-Failed 4 'dllPairBinding' 'dll-pair-manifest.json has no files map' }
$dllPairFiles = [System.Collections.Specialized.OrderedDictionary]::new()
foreach ($property in $dllPairManifest.files.PSObject.Properties) {
    $filePath = Join-Path $DllPairDir $property.Name
    if (-not (Test-Path -LiteralPath $filePath)) {
        Complete-Failed 4 'dllPairBinding' "manifest names $($property.Name), which is not in $DllPairDir"
    }
    $actual = Get-ShaLower $filePath
    if ($actual -ne ([string]$property.Value).ToLowerInvariant()) {
        Complete-Failed 4 'dllPairBinding' "sha256 mismatch for $($property.Name): manifest $($property.Value), on disk $actual"
    }
    $dllPairFiles[$property.Name] = $actual
}
foreach ($required in @('igpu_recon_cuda.dll', 'igpu_amaze_debayer_cuda.dll', 'cudart64_12.dll')) {
    if (-not $dllPairFiles.Contains($required)) {
        Complete-Failed 4 'dllPairBinding' "dll-pair-manifest.json does not carry $required; the package would fall back off the CUDA path"
    }
}
$pendingSymbolPresence = [bool]$dllPairManifest.pendingSymbolPresence
Say "DLL pair verified: $($dllPairFiles.Count) files, arch=$($dllPairManifest.cudaArch -join ','), builtOnHost=$($dllPairManifest.builtOnHost)"
$StepLog['dllPairBinding'] = 0

# --- build the exe -----------------------------------------------------------------------------
$qmakeExe = Join-Path $QtBin 'qmake.exe'
$makeExe = Join-Path $MingwBin 'mingw32-make.exe'
$windeployqtExe = Join-Path $QtBin 'windeployqt.exe'
$gccExe = Join-Path $MingwBin 'gcc.exe'
if (-not (Test-Path -LiteralPath $qmakeExe)) { Complete-Failed 12 'qmakeDiscovery' "qmake not found: $qmakeExe" }
$StepLog['qmakeDiscovery'] = 0
if (-not (Test-Path -LiteralPath $makeExe)) { Complete-Failed 14 'mingwMakeDiscovery' "mingw32-make not found: $makeExe" }
$StepLog['mingwMakeDiscovery'] = 0
$env:PATH = "$MingwBin;$QtBin;" + $env:PATH

$buildReleaseDir = Join-Path $SourceTree 'platform\qt\build-release'
New-Item -ItemType Directory -Path $buildReleaseDir -Force | Out-Null
$proFile = Join-Path $SourceTree 'platform\qt\MLVApp.pro'
if (-not (Test-Path -LiteralPath $proFile)) { Complete-Failed 2 'sourceArchive' "MLVApp.pro missing from the archived source: $proFile" }

[IO.File]::WriteAllText(
    (Join-Path $buildReleaseDir 'build_buildinfo.h'),
    (New-AttrCudaBuildInfoHeader -SourceCommit $SourceCommit),
    [Text.Encoding]::ASCII)
Say "BUILDINFO injected sha=$SourceCommit dirty=0"
$StepLog['buildInfoInject'] = 0

$logDir = Join-Path $Work 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Push-Location $buildReleaseDir
try {
    $qmakeOutput = @(& $qmakeExe $proFile 2>&1 | ForEach-Object { [string]$_ })
    $qmakeRc = $LASTEXITCODE
} finally { Pop-Location }
$qmakeOutput | Set-Content -LiteralPath (Join-Path $logDir 'qmake.log') -Encoding UTF8
if ($qmakeRc -ne 0) { Complete-Failed 13 'qmake' "qmake exit=$qmakeRc (see logs\qmake.log)" }
$StepLog['qmake'] = 0

Say "MAKE release -j$MakeJobs"
# "-j$MakeJobs" is QUOTED on purpose: unquoted, PowerShell hands mingw32-make a bare `-j`
# and the value separately, and make refuses with "the '-j' option requires a positive
# integer argument" -- observed here, not theorised.
$makeOutput = @(& $makeExe -C $buildReleaseDir -B release "-j$MakeJobs" 2>&1 | ForEach-Object { [string]$_ })
$makeRc = $LASTEXITCODE
$makeOutput | Set-Content -LiteralPath (Join-Path $logDir 'make.log') -Encoding UTF8
if ($makeRc -ne 0) { Complete-Failed 15 'make' "mingw32-make exit=$makeRc (see logs\make.log)" }
$StepLog['make'] = 0

$releaseDir = Join-Path $buildReleaseDir 'release'
$releaseExe = Join-Path $releaseDir $names.packageExeName
if (-not (Test-Path -LiteralPath $releaseExe)) { Complete-Failed 16 'releaseExePresent' "missing: $releaseExe" }
$StepLog['releaseExePresent'] = 0

# --- deploy ------------------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $windeployqtExe)) { Complete-Failed 17 'windeployqtDiscovery' "windeployqt not found: $windeployqtExe" }
$StepLog['windeployqtDiscovery'] = 0
$windeployOutput = @(& $windeployqtExe --release --no-translations --compiler-runtime $releaseExe 2>&1 | ForEach-Object { [string]$_ })
$windeployRc = $LASTEXITCODE
$windeployOutput | Set-Content -LiteralPath (Join-Path $logDir 'windeployqt.log') -Encoding UTF8
if ($windeployRc -ne 0) { Complete-Failed 18 'windeployqt' "windeployqt exit=$windeployRc" }
$StepLog['windeployqt'] = 0

# --compiler-runtime does not cover libgomp-1 (OpenMP); missing any one of these is a
# 0xC0000135 (STATUS_DLL_NOT_FOUND) at launch on the measurement host.
foreach ($dllName in @('libgomp-1.dll', 'libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll')) {
    $runtimeSource = Join-Path $MingwBin $dllName
    if (-not (Test-Path -LiteralPath $runtimeSource)) { Complete-Failed 21 'mingwRuntimeDeploy' "required MinGW runtime DLL not found: $runtimeSource" }
    Copy-Item -LiteralPath $runtimeSource -Destination (Join-Path $releaseDir $dllName) -Force
}
$StepLog['mingwRuntimeDeploy'] = 0

foreach ($name in $dllPairFiles.Keys) {
    $source = Join-Path $DllPairDir $name
    $destination = Join-Path $releaseDir $name
    Copy-Item -LiteralPath $source -Destination $destination -Force
    if ((Get-ShaLower $destination) -ne $dllPairFiles[$name]) {
        Complete-Failed 19 'dllPairDeploy' "copy of $name into the release dir did not round-trip its sha256"
    }
}
$StepLog['dllPairDeploy'] = 0

# Reviewed non-Qt runtime payload (FFmpeg), mirrored by exact name from a donor release tree --
# never a directory sweep, which can smuggle a stale hash-named executable into the package.
$ffmpegRuntimeDeployed = $false
$ffmpegNames = @()
if (-not [string]::IsNullOrWhiteSpace($RuntimeDonorDir)) {
    if (-not (Test-Path -LiteralPath $RuntimeDonorDir -PathType Container)) {
        Complete-Failed 22 'runtimeDonor' "-RuntimeDonorDir is not a directory: $RuntimeDonorDir"
    }
    foreach ($item in Get-ChildItem -LiteralPath $RuntimeDonorDir -File) {
        if ($item.Name -notmatch '^(av|sw)[a-z]+-[0-9]+\.dll$') { continue }
        $target = Join-Path $releaseDir $item.Name
        if (Test-Path -LiteralPath $target) { continue }
        Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        $ffmpegNames += $item.Name
    }
    $ffmpegRuntimeDeployed = ($ffmpegNames.Count -gt 0)
}
$StepLog['runtimeDonor'] = 0

# --- prove the package launches ----------------------------------------------------------------
# Sanitized PATH so the ONLY DLLs that can resolve are the ones just deployed beside the exe;
# the Qt/MinGW bins still on this process's PATH would otherwise mask an incomplete deploy.
$launchProbe = [ordered]@{ ran = $false; exitCode = $null; timedOut = $false }
if (-not $SkipLaunchProbe) {
    $probeDir = Join-Path $Scratch 'launch-probe'
    New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
    $savedPath = $env:PATH
    try {
        $env:PATH = @(
            (Join-Path $env:SystemRoot 'System32'),
            $env:SystemRoot,
            (Join-Path $env:SystemRoot 'System32\Wbem')
        ) -join ';'
        $probe = Start-Process -FilePath $releaseExe -ArgumentList '--batch', '--help' `
            -WorkingDirectory $probeDir -PassThru -WindowStyle Hidden
        if (-not $probe.WaitForExit(120000)) {
            try { $probe.Kill() } catch { }
            $launchProbe.timedOut = $true
        } else {
            $launchProbe.exitCode = $probe.ExitCode
        }
        $launchProbe.ran = $true
    } finally { $env:PATH = $savedPath }
    if ($launchProbe.timedOut -or [int]$launchProbe.exitCode -ne 0) {
        Complete-Failed 23 'launchProbe' "the deployed exe did not run standalone (exit=$($launchProbe.exitCode) timedOut=$($launchProbe.timedOut)); a DLL-load status here means the package is not self-contained"
    }
}
$StepLog['launchProbe'] = 0

# --- package -----------------------------------------------------------------------------------
$stagedPkgPath = Join-Path $Scratch $names.packageZipName
$stagedExePath = Join-Path $Scratch $names.exeName
$stagedDllPath = Join-Path $Scratch $names.reconName
try {
    if (Test-Path -LiteralPath $stagedPkgPath) { Remove-Item -LiteralPath $stagedPkgPath -Force }
    Compress-Archive -Path (Join-Path $releaseDir '*') -DestinationPath $stagedPkgPath -Force
    Copy-Item -LiteralPath $releaseExe -Destination $stagedExePath -Force
    Copy-Item -LiteralPath (Join-Path $releaseDir 'igpu_recon_cuda.dll') -Destination $stagedDllPath -Force
} catch {
    Complete-Failed 20 'package' $_.Exception.Message
}
$stagedPkgSha = Get-ShaLower $stagedPkgPath
$stagedExeSha = Get-ShaLower $stagedExePath
$stagedDllSha = Get-ShaLower $stagedDllPath
$StepLog['package'] = 0

# --- transactional publish: .partial, verify, rename, build.json LAST ---------------------------
$publishSet = @(
    [ordered]@{ name = $names.packageZipName; source = $stagedPkgPath; sha = $stagedPkgSha },
    [ordered]@{ name = $names.exeName; source = $stagedExePath; sha = $stagedExeSha },
    [ordered]@{ name = $names.reconName; source = $stagedDllPath; sha = $stagedDllSha }
)
$partialManifestPath = Join-Path $OutDir "$($names.buildManifestName).partial"
function Remove-RunPartials {
    # Files only: never -Recurse, never through a link (sol PR #133 r3; see Remove-AttrCudaPartialFile).
    foreach ($item in $script:publishSet) {
        [void](Remove-AttrCudaPartialFile -TrustedRoot $OutDir -Path (Join-Path $OutDir "$($item.name).partial"))
    }
    [void](Remove-AttrCudaPartialFile -TrustedRoot $OutDir -Path $script:partialManifestPath)
}

try {
    foreach ($item in $publishSet) {
        $partial = Join-Path $OutDir "$($item.name).partial"
        [void](Assert-AttrCudaWritableFileSlot -Path $partial)
        Copy-Item -LiteralPath ([string]$item.source) -Destination $partial -Force
        if ((Get-ShaLower $partial) -ne [string]$item.sha) { throw "sha256 did not round-trip for $($item.name)" }
    }
} catch {
    Remove-RunPartials
    Complete-Failed 24 'publishPartials' $_.Exception.Message
}
$StepLog['publishPartials'] = 0

try {
    foreach ($item in $publishSet) {
        [void](Assert-AttrCudaWritableFileSlot -Path (Join-Path $OutDir ([string]$item.name)))
        Move-Item -LiteralPath (Join-Path $OutDir "$($item.name).partial") -Destination (Join-Path $OutDir ([string]$item.name)) -Force
    }
} catch {
    Remove-RunPartials
    Complete-Failed 25 'publishRename' $_.Exception.Message
}
$StepLog['publishRename'] = 0

$qmakeVersionText = ((& $qmakeExe -version 2>&1) -join "`n").Trim()
$gccVersionText = if (Test-Path -LiteralPath $gccExe) { ((& $gccExe --version 2>&1) -join "`n").Trim() } else { $null }
$buildManifest = [ordered]@{
    schema = 'mlvapp.playback-attr-3-cuda-build-cache-manifest.v1'
    sourceCommit = $SourceCommit
    jobId = "playback-attr-3-cuda-assemble-$($names.shortSha)"
    exe = [ordered]@{ name = $names.exeName; sha256 = $stagedExeSha }
    dll = [ordered]@{ name = $names.reconName; sha256 = $stagedDllSha }
    packageZip = [ordered]@{ name = $names.packageZipName; sha256 = $stagedPkgSha }
    # Read by the attribution job, which has no way to derive it: this host builds the exe but
    # not the DLLs, and the measurement host inspects neither.
    pendingSymbolPresence = $pendingSymbolPresence
    dllPairManifestSha256 = $dllPairManifestSha256
    exeBuiltOnHost = $env:COMPUTERNAME
    dllPair = [ordered]@{
        sourceCommit = $dllPairManifest.sourceCommit
        cudaArch = @($dllPairManifest.cudaArch)
        builtOnHost = $dllPairManifest.builtOnHost
        nvccVersion = $dllPairManifest.nvccVersion
        files = $dllPairFiles
    }
    ffmpegRuntimeDeployed = $ffmpegRuntimeDeployed
    ffmpegRuntimeFiles = @($ffmpegNames)
    launchProbe = $launchProbe
    identity = [ordered]@{ injectedFromPinnedSourceCommit = $true; sha = $SourceCommit; dirty = 0 }
    # The verified archive the exe was compiled from; its zip comment declared $SourceCommit.
    sourceArchiveSha256 = $sourceArchiveSha256
    toolVersions = [ordered]@{ qmake = $qmakeVersionText; gcc = $gccVersionText }
    steps = $StepLog
    assembledAtUtc = (Get-Date).ToUniversalTime().ToString('o')
}
try {
    [void](Assert-AttrCudaWritableFileSlot -Path $partialManifestPath)
    [void](Assert-AttrCudaWritableFileSlot -Path (Join-Path $OutDir $names.buildManifestName))
    $buildManifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $partialManifestPath -Encoding UTF8
    Move-Item -LiteralPath $partialManifestPath -Destination (Join-Path $OutDir $names.buildManifestName) -Force
} catch {
    Remove-RunPartials
    Complete-Failed 26 'publishManifest' $_.Exception.Message
}
$StepLog['publishManifest'] = 0

# MANIFEST_SHA256 is the value the hub hands to the attribution generator as
# -BuildManifestSha256; without it that job would trust whichever same-named build.json is in
# the mutable Bachelor cache. Hashed after the atomic rename, so it describes the published file.
$publishedManifestSha256 = Get-ShaLower (Join-Path $OutDir $names.buildManifestName)
Write-Output "RESULT=ASSEMBLE_OK SOURCE=$SourceCommit EXE=$($names.exeName) DLL=$($names.reconName) PKG=$($names.packageZipName) MANIFEST=$($names.buildManifestName) MANIFEST_SHA256=$publishedManifestSha256 OUT=$OutDir"
exit 0
