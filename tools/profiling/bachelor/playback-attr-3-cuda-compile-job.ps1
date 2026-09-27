# playback-attr-3-cuda-compile-job.ps1 -- RETIRED 2026-09-16. IT NO LONGER EMITS ANYTHING.
#
# This generator emitted a job that compiled the CUDA backend DLL and the Qt exe ON BACHELOR.
# Bachelor cannot do either: it has Visual Studio and BuildTools installed WITHOUT the VC tools
# component (no cl.exe) and no CUDA toolkit at all. The job
# playback-attr-3-cuda-build-29dd4dd4ea26 exited 5 at msvcDiscovery, and so did the Aug 9
# precedent c2-submit-2-946d8bdb-compile. Installing a toolchain on Bachelor was rejected: it is
# a system change to the owner's laptop, and it is unnecessary.
#
# THE ROUTE THAT REPLACES IT (swarm ruling 2026-09-16,
# .claude-state/fleet-runs/swarm-attr3-buildhost-20260916T2150Z/SYNTHESIS.md), in order:
#   1. tools/profiling/ultramagnus/playback-attr-3-cuda-dll-job.ps1  -- CUDA DLL pair, sm_86
#   2. tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1    -- exe + package, board host
#   3. tools/profiling/bachelor/playback-attr-3-cuda-stage-job.ps1   -- into the Bachelor cache
#   4. tools/profiling/bachelor/playback-attr-3-cuda-job.ps1         -- attribution
# Runbook: docs/playback-attr-3-cuda.md.
#
# The body below is kept UNRUN as the record of the build steps the assembler inherited
# (buildinfo injection before qmake, qmake, mingw32-make -B release, windeployqt, the MinGW
# runtime set, the transactional cache publish). Do not re-enable it: the refusal is the point,
# and a generator that emits a job which cannot succeed is how PR #131 lost a day.
#
# ---------------------------------------------------------------------------------------------
# Historical header follows.
#
# playback-attr-3-cuda-compile-job.ps1 -- GENERATOR (runs locally / in a lane; nothing here runs
# on Bachelor). Emits a self-contained <jobId>.job.ps1 plus a <jobId>-source.zip for the
# Bachelor agent inbox (tools/profiling/ultra-magnus-agent.ps1 protocol: both files land in
# inbox\; the agent runs the job; results land in outbox\<jobId>.artifacts). The emitted job
# builds the CUDA backend DLL and the Release MLV-App exe from -SourceCommit, deploys the Qt
# runtime, and stages the exe, the DLL, and a packaged zip into the Bachelor agent cache
# (C:\mlvtmp\mlv-agent\cache) under the naming convention
# tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 (the attribution job) already expects.
#
# Modelled on:
#   - the CUDA nvcc backend compile steps (vswhere -> vcvars64 -> CUDA toolkit root -> nvcc
#     -gencode=arch=compute_86,code=sm_86 -shared, exports via .def, cmd-file + errorlevel
#     pattern) from a real Bachelor compile job -- see
#     .claude-state/fleet-runs/lane-PLAYBACK-ATTR-3-CUDA-S1B-20260916T175101Z/inputs/precedent-bachelor-compile.job.ps1.txt
#   - the Release MLV-App exe build steps (mingw32-make at C:\Qt\Tools\mingw1310_64\bin against
#     a platform\qt\build-release tree, backend DLL + cudart64_*.dll deployment into
#     platform\qt\build-release\release) from the Ultramagnus evidence script -- see
#     .claude-state/fleet-runs/lane-PLAYBACK-ATTR-3-CUDA-S1B-20260916T175101Z/inputs/precedent-ultramagnus-build-lines-480-720.ps1.txt
#
# INFERRED (not literally present in either precedent excerpt above; both corroborated by
# tools\build-release.ps1, the repo's current authoritative clean-build script, which targets
# the identical $QtBin='C:\Qt\6.10.2\mingw_64\bin' / $MingwBin='C:\Qt\Tools\mingw1310_64\bin'
# roots the Ultramagnus precedent's own $env:PATH prepend uses):
#   - the initial `qmake platform\qt\MLVApp.pro` invocation, run from a fresh
#     platform\qt\build-release working directory, that generates the Makefile the Ultramagnus
#     precedent's `mingw32-make -C ... -B release` then consumes. The precedent's own excerpt
#     starts from an ALREADY-CONFIGURED build-release tree (Ultramagnus keeps one resident); a
#     Bachelor job starts from a freshly expanded source zip every time, so qmake must run
#     first. qmake.exe is inferred to live beside windeployqt.exe in the same Qt bin dir the
#     precedent already prepends to PATH.
#   - the `windeployqt.exe --release --no-translations --compiler-runtime` invocation that
#     deploys the Qt runtime next to the exe (the card requires this; it falls outside the
#     given 480-720 line excerpt). Flags reused verbatim from tools\build-release.ps1.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-compile-job.ps1 `
#       -SourceCommit <40-hex> -OutDir <dir>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,

    [string]$CudaArch = 'sm_86'
)

$ErrorActionPreference = 'Stop'

# RETIRED: refuse before doing anything. Bachelor has no VC tools and no CUDA toolkit, so every
# job this generator could emit exits 5 at msvcDiscovery on that host.
throw @"
playback-attr-3-cuda-compile-job.ps1 is RETIRED and emits nothing.

Bachelor has Visual Studio without the VC tools component (no cl.exe) and no CUDA toolkit, so
the job this generator used to emit cannot build anything there: playback-attr-3-cuda-build-
29dd4dd4ea26 exited 5 at msvcDiscovery, as did the Aug 9 precedent c2-submit-2-946d8bdb-compile.

Use the split-build route instead:
  1. tools\profiling\ultramagnus\playback-attr-3-cuda-dll-job.ps1 -SourceCommit <40-hex> -OutDir <dir>
       -> CUDA DLL pair (sm_86) built on Ultra-Magnus
  2. tools\profiling\bachelor\playback-attr-3-cuda-assemble.ps1 -SourceCommit <40-hex> -DllPairDir <dir> -OutDir <dir>
       -> Qt/MinGW exe + package, built on the board host
  3. tools\profiling\bachelor\playback-attr-3-cuda-stage-job.ps1 -SourceCommit <40-hex> -BuildDir <dir> -OutDir <dir>
       -> stages the package into the Bachelor agent cache
  4. tools\profiling\bachelor\playback-attr-3-cuda-job.ps1 -- attribution, inside the owner-granted lane

Runbook: docs\playback-attr-3-cuda.md
Ruling: .claude-state\fleet-runs\swarm-attr3-buildhost-20260916T2150Z\SYNTHESIS.md
"@

# --- resolve provenance locally, BEFORE the job ever touches Bachelor -------------
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
& git -C $RepoRoot cat-file -e "$SourceCommit^{commit}" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "SourceCommit is not a commit known to the local repo at $RepoRoot : $SourceCommit"
}

$shortSha = $SourceCommit.Substring(0, 12)
$JobId = "playback-attr-3-cuda-build-$shortSha"
# Naming convention shared with tools/profiling/bachelor/playback-attr-3-cuda-job.ps1: that
# generator derives these SAME names from -SourceCommit independently, so the two jobs never
# need to exchange state to agree on what the cache will contain.
$ExeName = "MLVApp-playback-attr-3-cuda-$shortSha.exe"
$ReconName = "igpu_recon_cuda-playback-attr-3-cuda-$shortSha.dll"
$PkgName = "MLVApp-playback-attr-3-cuda-$shortSha-pkg.zip"

if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$archivePath = Join-Path $OutDir "$JobId-source.zip"
$jobPath = Join-Path $OutDir "$JobId.job.ps1"

if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
& git -C $RepoRoot archive --format=zip -o $archivePath $SourceCommit
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archivePath)) {
    throw "git archive failed for $SourceCommit"
}

# --- job body template (placeholders are substituted below; the body itself never touches
#     this generator's variables directly, so there is no accidental capture of this machine's
#     environment into the emitted script) ----------------------------------------------------
$template = @'
$ErrorActionPreference = 'Stop'
$SourceCommit = '__SOURCE_COMMIT__'
$CudaArch = '__CUDA_ARCH__'
$ExeName = '__EXE_NAME__'
$ReconName = '__RECON_NAME__'
$PkgName = '__PKG_NAME__'
$Root = 'C:\mlvtmp\mlv-agent'
$Cache = Join-Path $Root 'cache'
$ManifestName = 'playback-attr-3-cuda-__SHORT_SHA__-build.json'
$JobId = 'playback-attr-3-cuda-build-__SHORT_SHA__'
$Archive = Join-Path $Root "inbox\$JobId-source.zip"
$Work = Join-Path $Root $JobId
$Pub = Join-Path $Root "outbox\$JobId.artifacts"

function Say([string]$Message) { Write-Output "[$JobId] $Message" }
function Get-ShaLower([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

$StepLog = [System.Collections.Specialized.OrderedDictionary]::new()

function Complete-Failed([int]$Code, [string]$Step, [string]$Message) {
    $StepLog[$Step] = $Code
    Say "FAIL step=$Step exit=$Code $Message"
    if (Test-Path -LiteralPath $Pub) {
        $partial = [ordered]@{
            schema = 'mlvapp.playback-attr-3-cuda-build.v1'
            sourceCommit = $SourceCommit
            jobId = $JobId
            target = $CudaArch
            exitCode = $Code
            failedStep = $Step
            message = $Message
            steps = $StepLog
        }
        $partial | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $Pub 'result.json') -Encoding UTF8
    }
    Write-Output "RESULT=BUILD_FAILED STEP=$Step EXIT=$Code"
    exit $Code
}

Say "START source=$SourceCommit arch=$CudaArch"
if (-not (Test-Path -LiteralPath $Archive)) { Complete-Failed 3 'sourceArchive' "missing: $Archive" }
if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
if (Test-Path -LiteralPath $Pub) { Remove-Item -LiteralPath $Pub -Recurse -Force }
New-Item -ItemType Directory -Path $Work -Force | Out-Null
New-Item -ItemType Directory -Path $Pub -Force | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $Work -Force
$StepLog['sourceExpand'] = 0

# Machine-safety boundary: every file the invoked toolchain writes must live under
# C:\mlvtmp. A job-owned scratch dir under $Work (itself under C:\mlvtmp\mlv-agent)
# becomes TEMP/TMP for the rest of this process, so nvcc, qmake, mingw32-make and
# windeployqt all inherit it instead of the ambient (unconstrained) machine TEMP.
$Scratch = Join-Path $Work '.job-tmp'
New-Item -ItemType Directory -Path $Scratch -Force | Out-Null
$env:TEMP = $Scratch
$env:TMP = $Scratch

$Backend = Join-Path $Work 'tools\gpu\backend'
$Src = Join-Path $Backend 'igpu_recon_cuda.cu'
$Def = Join-Path $Backend 'igpu_recon_cuda.def'
if (-not (Test-Path -LiteralPath $Src) -or -not (Test-Path -LiteralPath $Def)) {
    Complete-Failed 4 'backendSourcesPresent' "extracted backend sources missing under $Backend"
}
$StepLog['backendSourcesPresent'] = 0

$pf86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
$vswhere = Join-Path $pf86 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsRoot = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
if (-not $vsRoot) { Complete-Failed 5 'msvcDiscovery' 'MSVC not found via vswhere' }
$vcvars = Join-Path $vsRoot 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { Complete-Failed 5 'msvcDiscovery' "vcvars64 not found: $vcvars" }
$StepLog['msvcDiscovery'] = 0

$cudaBase = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
$cudaRoot = Get-ChildItem -LiteralPath $cudaBase -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
if (-not $cudaRoot) { Complete-Failed 6 'cudaDiscovery' "CUDA toolkit root not found under $cudaBase" }
$nvcc = Join-Path $cudaRoot.FullName 'bin\nvcc.exe'
if (-not (Test-Path -LiteralPath $nvcc)) { Complete-Failed 6 'cudaDiscovery' "nvcc not found: $nvcc" }
$StepLog['cudaDiscovery'] = 0

$backendDll = Join-Path $Backend 'igpu_recon_cuda.dll'
$archFlag = "-gencode=arch=compute_$($CudaArch -replace '^sm_',''),code=$CudaArch"
$backendCmdFile = Join-Path $Work "$JobId-backend.cmd"
$backendCmdLines = @(
    '@echo off',
    "call `"$vcvars`" >nul",
    "`"$nvcc`" $archFlag -O3 --fmad=false -shared -allow-unsupported-compiler -Xcompiler `"/MD`" -Xlinker `"/DEF:$Def`" -I `"$Backend`" `"$Src`" -o `"$backendDll`""
)
$backendCmdLines | Set-Content -LiteralPath $backendCmdFile -Encoding ASCII
Say "BUILD backend nvcc target=$CudaArch"
$backendBuildOutput = @(& cmd /c "`"$backendCmdFile`"" 2>&1 | ForEach-Object { [string]$_ })
$backendRc = $LASTEXITCODE
$backendBuildOutput | Set-Content -LiteralPath (Join-Path $Pub 'backend-compile.log') -Encoding UTF8
if ($backendRc -ne 0 -or -not (Test-Path -LiteralPath $backendDll)) {
    Complete-Failed 11 'backendCompile' "nvcc exit=$backendRc"
}
$StepLog['backendCompile'] = 0

$QtBin = 'C:\Qt\6.10.2\mingw_64\bin'
$MingwBin = 'C:\Qt\Tools\mingw1310_64\bin'
$qmakeExe = Join-Path $QtBin 'qmake.exe'
$makeExe = Join-Path $MingwBin 'mingw32-make.exe'
$windeployqtExe = Join-Path $QtBin 'windeployqt.exe'
$gccExe = Join-Path $MingwBin 'gcc.exe'
if (-not (Test-Path -LiteralPath $qmakeExe)) { Complete-Failed 12 'qmakeDiscovery' "qmake not found: $qmakeExe" }
$StepLog['qmakeDiscovery'] = 0
if (-not (Test-Path -LiteralPath $makeExe)) { Complete-Failed 14 'mingwMakeDiscovery' "mingw32-make not found: $makeExe" }
$StepLog['mingwMakeDiscovery'] = 0

$env:PATH = "$MingwBin;$QtBin;" + $env:PATH

$buildReleaseDir = Join-Path $Work 'platform\qt\build-release'
New-Item -ItemType Directory -Path $buildReleaseDir -Force | Out-Null
$proFile = Join-Path $Work 'platform\qt\MLVApp.pro'

# Build identity fix (BLOCKER): the source comes from `git archive`, so there is no
# .git in $Work and MLVApp.pro's qmake-time git probe stamps "unknown". Write the
# SAME macro set tools/gen-buildinfo.ps1 emits directly into OUT_PWD (= $buildReleaseDir,
# where qmake is about to run) BEFORE qmake, mirroring tools/build-release.ps1's own
# gen-buildinfo.ps1 call, which also runs before qmake from the build dir. Pinned to
# -SourceCommit with dirty=0 -- gen-buildinfo.ps1's own fallback when git is unusable
# is to fall back GIT_DESCRIBE to the sha, so this does the same.
$buildInfoUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$buildInfoHeader = @"
/* AUTO-GENERATED by tools/gen-buildinfo.ps1 at build time. Do not edit; do not commit. */
#ifndef MLVAPP_BUILD_BUILDINFO_H
#define MLVAPP_BUILD_BUILDINFO_H
/* A qmake-time -D may be stale or "unknown" when qmake cannot execute git.
   This build-time header is generated immediately before compilation and is
   authoritative, so replace any earlier definition rather than preserving it. */
#ifdef MLVAPP_GIT_SHA
#undef MLVAPP_GIT_SHA
#endif
#define MLVAPP_GIT_SHA "$SourceCommit"
#define MLVAPP_BUILD_SHA "$SourceCommit"
#define MLVAPP_GIT_DIRTY 0
#define MLVAPP_GIT_DESCRIBE "$SourceCommit"
#define MLVAPP_BUILD_TIME_UTC "$buildInfoUtc"
#define MLVAPP_BUILD_STAMP "MLVAPP_BUILDSTAMP_v1|sha=$SourceCommit|dirty=0"
#endif
"@
[IO.File]::WriteAllText((Join-Path $buildReleaseDir 'build_buildinfo.h'), $buildInfoHeader, [Text.Encoding]::ASCII)
Say "BUILDINFO injected sha=$SourceCommit dirty=0 (pinned SourceCommit, no .git in archived source)"
$StepLog['buildInfoInject'] = 0

Push-Location $buildReleaseDir
try {
    $qmakeOutput = @(& $qmakeExe $proFile 2>&1 | ForEach-Object { [string]$_ })
    $qmakeRc = $LASTEXITCODE
} finally { Pop-Location }
$qmakeOutput | Set-Content -LiteralPath (Join-Path $Pub 'qmake.log') -Encoding UTF8
if ($qmakeRc -ne 0) { Complete-Failed 13 'qmake' "qmake exit=$qmakeRc" }
$StepLog['qmake'] = 0

$makeOutput = @(& $makeExe -C $buildReleaseDir -B release -j4 2>&1 | ForEach-Object { [string]$_ })
$makeRc = $LASTEXITCODE
$makeOutput | Set-Content -LiteralPath (Join-Path $Pub 'make.log') -Encoding UTF8
if ($makeRc -ne 0) { Complete-Failed 15 'make' "mingw32-make exit=$makeRc" }
$StepLog['make'] = 0

$releaseDir = Join-Path $buildReleaseDir 'release'
$releaseExe = Join-Path $releaseDir 'MLVApp.exe'
if (-not (Test-Path -LiteralPath $releaseExe)) { Complete-Failed 16 'releaseExePresent' "missing: $releaseExe" }
$StepLog['releaseExePresent'] = 0

if (-not (Test-Path -LiteralPath $windeployqtExe)) { Complete-Failed 17 'windeployqtDiscovery' "windeployqt not found: $windeployqtExe" }
$StepLog['windeployqtDiscovery'] = 0
$windeployOutput = @(& $windeployqtExe --release --no-translations --compiler-runtime $releaseExe 2>&1 | ForEach-Object { [string]$_ })
$windeployRc = $LASTEXITCODE
$windeployOutput | Set-Content -LiteralPath (Join-Path $Pub 'windeployqt.log') -Encoding UTF8
if ($windeployRc -ne 0) { Complete-Failed 18 'windeployqt' "windeployqt exit=$windeployRc" }
$StepLog['windeployqt'] = 0

# Self-contained package fix (MAJOR): --compiler-runtime does not cover libgomp-1
# (OpenMP), so copy the exact MinGW runtime set tools/build-release.ps1 copies
# explicitly. Missing any one is a 0xC0000135 (STATUS_DLL_NOT_FOUND) at launch.
$mingwRuntimeDlls = @('libgomp-1.dll', 'libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll')
foreach ($dllName in $mingwRuntimeDlls) {
    $mingwSrc = Join-Path $MingwBin $dllName
    if (-not (Test-Path -LiteralPath $mingwSrc)) { Complete-Failed 21 'mingwRuntimeDeploy' "required MinGW runtime DLL not found: $mingwSrc" }
    Copy-Item -LiteralPath $mingwSrc -Destination (Join-Path $releaseDir $dllName) -Force
}
$StepLog['mingwRuntimeDeploy'] = 0

$deployTargets = @(
    [ordered]@{ source = $backendDll; destination = (Join-Path $releaseDir 'igpu_recon_cuda.dll') }
)
$cudart = Get-ChildItem -LiteralPath (Join-Path $cudaRoot.FullName 'bin') -Filter 'cudart64_*.dll' -File -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $cudart) { Complete-Failed 22 'cudaRuntimeRequired' "cudart64_*.dll not found under $(Join-Path $cudaRoot.FullName 'bin'); the CUDA runtime is required for a self-contained package" }
$deployTargets += [ordered]@{ source = $cudart.FullName; destination = (Join-Path $releaseDir $cudart.Name) }
$StepLog['cudaRuntimeRequired'] = 0
foreach ($target in $deployTargets) {
    Copy-Item -LiteralPath ([string]$target.source) -Destination ([string]$target.destination) -Force
}
$deployedBackendDll = Join-Path $releaseDir 'igpu_recon_cuda.dll'
if (-not (Test-Path -LiteralPath $deployedBackendDll)) { Complete-Failed 19 'backendDeploy' "missing after deploy: $deployedBackendDll" }
$StepLog['backendDeploy'] = 0

$stagedPkgPath = Join-Path $Scratch $PkgName
$stagedExePath = Join-Path $Scratch $ExeName
$stagedDllPath = Join-Path $Scratch $ReconName
if (Test-Path -LiteralPath $stagedPkgPath) { Remove-Item -LiteralPath $stagedPkgPath -Force }
try {
    Compress-Archive -Path (Join-Path $releaseDir '*') -DestinationPath $stagedPkgPath -Force
    Copy-Item -LiteralPath $releaseExe -Destination $stagedExePath -Force
    Copy-Item -LiteralPath $deployedBackendDll -Destination $stagedDllPath -Force
} catch {
    Complete-Failed 20 'stageToCache' $_.Exception.Message
}
if (-not (Test-Path -LiteralPath $stagedPkgPath) -or -not (Test-Path -LiteralPath $stagedExePath) -or -not (Test-Path -LiteralPath $stagedDllPath)) {
    Complete-Failed 20 'stageToCache' 'one or more staged artifacts missing after packaging'
}
$stagedPkgSha = Get-ShaLower $stagedPkgPath
$stagedExeSha = Get-ShaLower $stagedExePath
$stagedDllSha = Get-ShaLower $stagedDllPath
$StepLog['stageToCache'] = 0

# Non-transactional publish fix (BLOCKER): write each artifact to the cache under a
# `.partial` name first and verify its sha256 round-tripped, THEN rename all three in
# sequence -- so the attribution job never sees a package zip paired with a stale or
# missing exe/DLL. The manifest (holding all three hashes) is written LAST, after the
# rename, so its mere presence means the triple is complete and verified. Any failure
# in this block removes only THIS job's `.partial` files, never a sibling job's.
$partialPkgPath = Join-Path $Cache "$PkgName.partial"
$partialExePath = Join-Path $Cache "$ExeName.partial"
$partialDllPath = Join-Path $Cache "$ReconName.partial"
$partialManifestPath = Join-Path $Cache "$ManifestName.partial"
function Remove-JobPartials {
    foreach ($p in @($partialPkgPath, $partialExePath, $partialDllPath, $partialManifestPath)) {
        Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    }
}

try {
    Copy-Item -LiteralPath $stagedPkgPath -Destination $partialPkgPath -Force
    Copy-Item -LiteralPath $stagedExePath -Destination $partialExePath -Force
    Copy-Item -LiteralPath $stagedDllPath -Destination $partialDllPath -Force
    if ((Get-ShaLower $partialPkgPath) -ne $stagedPkgSha -or
        (Get-ShaLower $partialExePath) -ne $stagedExeSha -or
        (Get-ShaLower $partialDllPath) -ne $stagedDllSha) {
        throw 'partial cache artifact sha256 did not round-trip'
    }
} catch {
    Remove-JobPartials
    Complete-Failed 23 'publishPartials' $_.Exception.Message
}
$StepLog['publishPartials'] = 0

try {
    Move-Item -LiteralPath $partialPkgPath -Destination (Join-Path $Cache $PkgName) -Force
    Move-Item -LiteralPath $partialExePath -Destination (Join-Path $Cache $ExeName) -Force
    Move-Item -LiteralPath $partialDllPath -Destination (Join-Path $Cache $ReconName) -Force
} catch {
    Remove-JobPartials
    Complete-Failed 24 'publishRename' $_.Exception.Message
}
$StepLog['publishRename'] = 0

$nvccVersionText = ((& $nvcc --version 2>&1) -join "`n").Trim()
$qmakeVersionText = ((& $qmakeExe -version 2>&1) -join "`n").Trim()
$gccVersionText = if (Test-Path -LiteralPath $gccExe) { ((& $gccExe --version 2>&1) -join "`n").Trim() } else { $null }

$cacheManifest = [ordered]@{
    schema = 'mlvapp.playback-attr-3-cuda-build-cache-manifest.v1'
    sourceCommit = $SourceCommit
    jobId = $JobId
    exe = [ordered]@{ name = $ExeName; sha256 = $stagedExeSha }
    dll = [ordered]@{ name = $ReconName; sha256 = $stagedDllSha }
    packageZip = [ordered]@{ name = $PkgName; sha256 = $stagedPkgSha }
}
try {
    $cacheManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $partialManifestPath -Encoding UTF8
    Move-Item -LiteralPath $partialManifestPath -Destination (Join-Path $Cache $ManifestName) -Force
} catch {
    Remove-JobPartials
    Complete-Failed 25 'publishManifest' $_.Exception.Message
}
$StepLog['publishManifest'] = 0

$exeItem = Get-Item -LiteralPath (Join-Path $Cache $ExeName)
$dllItem = Get-Item -LiteralPath (Join-Path $Cache $ReconName)
$pkgItem = Get-Item -LiteralPath (Join-Path $Cache $PkgName)
$result = [ordered]@{
    schema = 'mlvapp.playback-attr-3-cuda-build.v1'
    sourceCommit = $SourceCommit
    jobId = $JobId
    target = $CudaArch
    exitCode = 0
    exe = [ordered]@{ name = $exeItem.Name; length = $exeItem.Length; sha256 = $stagedExeSha }
    dll = [ordered]@{ name = $dllItem.Name; length = $dllItem.Length; sha256 = $stagedDllSha }
    packageZip = [ordered]@{ name = $pkgItem.Name; length = $pkgItem.Length; sha256 = $stagedPkgSha }
    cacheManifest = [ordered]@{ name = $ManifestName }
    identity = [ordered]@{ injectedFromPinnedSourceCommit = $true; sha = $SourceCommit; dirty = 0 }
    toolVersions = [ordered]@{ nvcc = $nvccVersionText; qmake = $qmakeVersionText; gcc = $gccVersionText }
    steps = $StepLog
}
$result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $Pub 'result.json') -Encoding UTF8
Say "RESULT=BUILD_OK exeSha256=$($exeItem.Name) dllSha256=$($dllItem.Name)"
Write-Output "RESULT=BUILD_OK SOURCE=$SourceCommit EXE=$($exeItem.Name) DLL=$($dllItem.Name) PKG=$($pkgItem.Name) ARTIFACTS=$Pub"
exit 0
'@

$text = $template.
    Replace('__SOURCE_COMMIT__', $SourceCommit).
    Replace('__SHORT_SHA__', $shortSha).
    Replace('__CUDA_ARCH__', $CudaArch).
    Replace('__EXE_NAME__', $ExeName).
    Replace('__RECON_NAME__', $ReconName).
    Replace('__PKG_NAME__', $PkgName)

[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

[pscustomobject]@{
    jobFile = $jobPath
    sourceArchive = $archivePath
    jobId = $JobId
    sourceCommit = $SourceCommit
    exeName = $ExeName
    reconName = $ReconName
    packageZipName = $PkgName
}
