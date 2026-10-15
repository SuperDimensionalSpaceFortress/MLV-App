# P0-2: the ONE authoritative clean-build path. Produces a trustworthy, SELF-CONTAINED artifact:
#  - refuses (or -AllowDirty marks) a dirty tree
#  - FRESH out-of-tree build (no cross-commit object mixing)
#  - fresh qmake (so the embedded SHA is current even without the P0-1 gen-target)
#  - names the exe MLVApp-<sha>[-dirty].exe + writes build-manifest.json (sha, checksum, env)
#  - VERIFIES the produced exe's embedded SHA == HEAD (fail-closed)
#  - DEPLOYS the runtime next to the stamped exe so the artifact dir is self-contained and
#    runnable standalone: windeployqt (Qt DLLs + platforms/ imageformats/ ... plugin subdirs)
#    + the MinGW runtime DLLs (incl. libgomp-1, which --compiler-runtime misses) + the non-Qt
#    payload (FFmpeg av*/sw*, CUDA cudart64_12 + igpu_recon_cuda, pixel_maps/releases data)
#    admitted from a fully deployed donor release tree. Copying ONLY the exe used to leave the
#    artifact with no DLLs beside it, so launching it died in the loader with 0xC0000135
#    (STATUS_DLL_NOT_FOUND) BEFORE main(), silently breaking any reviewer/gate pointed at it.
#  - PROVES runnability: launches the stamped exe with a sanitized PATH (system dirs only) so
#    the only resolvable DLLs are the deployed ones, and fails closed on a 0xC0000135 loader death.
# Toolchain: Qt 6.10.2 / mingw1310 (see -QtBin/-MingwBin defaults).
# Usage: pwsh -NoProfile -File tools\build-release.ps1 [-AllowDirty] [-OutRoot <dir>] [-RuntimeDonorDir <dir>]
# Exit codes: 5=dirty tree (no -AllowDirty); 6=provenance stamp sha != HEAD; 7=runtime deploy
#   failed (windeployqt or a required MinGW runtime DLL missing); 8=artifact not self-contained
#   (launch probe died with a DLL-load NTSTATUS, e.g. 0xC0000135).
param(
    [string]$SourceRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$QtBin      = "C:\Qt\6.10.2\mingw_64\bin",
    [string]$MingwBin   = "C:\Qt\Tools\mingw1310_64\bin",
    [string]$OutRoot    = "$env:USERPROFILE\mlvapp-artifacts",
    [string]$RuntimeDonorDir = "",
    [switch]$AllowDirty
)
$ErrorActionPreference = 'Stop'
$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
Import-Module (Join-Path $SourceRoot 'tools\release-runtime-admission.psm1') -Force
$env:Path = "$QtBin;$MingwBin;$env:Path"
$head  = (& git -C $SourceRoot rev-parse HEAD).Trim()
$porcelain = (& git -C $SourceRoot status --porcelain)
$dirty = [bool]($porcelain -and ($porcelain | Where-Object { $_ }).Count -gt 0)
if ($dirty -and -not $AllowDirty) {
    Write-Host "ABORT: working tree is dirty (use -AllowDirty to build a -dirty artifact):" -ForegroundColor Red
    $porcelain | ForEach-Object { Write-Host "  $_" }
    exit 5
}
$tag = $head.Substring(0,12) + $(if ($dirty) { '-dirty' } else { '' })
$bd  = Join-Path ([System.IO.Path]::GetTempPath()) ("mlvbuild-" + $tag)
if (Test-Path -LiteralPath $bd) { Remove-Item -LiteralPath $bd -Recurse -Force }   # fresh, no object reuse
New-Item -ItemType Directory -Force -Path $bd | Out-Null
Write-Host "Clean build: HEAD=$head dirty=$dirty -> $bd"
Push-Location $bd
try {
    # qmake/mingw32-make are launched through the exclusive runner. Some
    # Windows process chains resolve the top-level executable by its absolute
    # path but lose PATH for nested tools (notably windres -> gcc and qmake ->
    # git). Put tiny, build-local forwarding shims in the working directory;
    # Windows searches it before PATH, so every nested invocation remains bound
    # to the exact toolchain selected above instead of silently falling back or
    # failing with an unhelpful "gcc is not recognized" message.
    $gitExe = (Get-Command git.exe -ErrorAction Stop).Source
    $toolShims = [ordered]@{
        'gcc.cmd' = (Join-Path $MingwBin 'gcc.exe')
        'g++.cmd' = (Join-Path $MingwBin 'g++.exe')
        'windres.cmd' = (Join-Path $MingwBin 'windres.exe')
        'git.cmd' = $gitExe
    }
    foreach ($shim in $toolShims.GetEnumerator()) {
        if (-not (Test-Path -LiteralPath $shim.Value -PathType Leaf)) {
            throw "Required build tool not found: $($shim.Value)"
        }
        $shimText = "@echo off`r`n`"$($shim.Value)`" %*`r`n"
        [System.IO.File]::WriteAllText(
            (Join-Path $bd $shim.Key),
            $shimText,
            [System.Text.Encoding]::ASCII)
    }
    # P0-1: stamp the build-time provenance header INTO the build dir before qmake/compile,
    # so the artifact self-identifies its real commit + dirty state (not a qmake-pinned label).
    & pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "$SourceRoot\tools\gen-buildinfo.ps1" `
        -SrcRoot "$SourceRoot" -OutHeader (Join-Path $bd "build_buildinfo.h") 2>&1 | Out-Host
    $qmakeArgsJson = ConvertTo-Json -InputObject @("$SourceRoot\platform\qt\MLVApp.pro") -Compress
    $qmakeArgsBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($qmakeArgsJson))
    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$SourceRoot\tools\coordination\invoke-mlv-exclusive.ps1" `
        -RepoRoot $SourceRoot -Owner "qmake-release:$env:USERNAME" -FilePath "$QtBin\qmake.exe" `
        -WorkingDirectory $bd -ArgumentListBase64 $qmakeArgsBase64
    if ($LASTEXITCODE -ne 0) { throw "qmake failed ($LASTEXITCODE)" }
    # BUILD-GUARD-1: the compile runs under the host-global build gate (memory floor, one build slot,
    # -j4 or -j2 beside another MinGW build) instead of a fixed job count; a refusal exits 3 (BUILD_MEM_REFUSED).
    . (Join-Path $SourceRoot 'tools\coordination\build-guard.ps1') -Library
    Invoke-BuildGuard -Label "build-release:$tag" -Action {
        param($Jobs)
        $makeArgsJson = ConvertTo-Json -InputObject @('-f', 'Makefile.Release', "-j$Jobs") -Compress
        $makeArgsBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($makeArgsJson))
        & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$SourceRoot\tools\coordination\invoke-mlv-exclusive.ps1" `
            -RepoRoot $SourceRoot -Owner "build-release:$env:USERNAME" -FilePath "$MingwBin\mingw32-make.exe" -WorkingDirectory $bd `
            -ArgumentListBase64 $makeArgsBase64
        if ($LASTEXITCODE -ne 0) { throw "make failed ($LASTEXITCODE)" }
    }
} finally { Pop-Location }
$builtExe = Join-Path $bd "release\MLVApp.exe"
if (-not (Test-Path $builtExe)) { throw "no exe produced" }

# fail-closed: verify the EXACT uniquely-tagged provenance stamp compiled into the binary == HEAD.
# (Strict: not "some 40-hex string in the exe matches" -- match MLVAPP_BUILDSTAMP_v1|sha=...|dirty=...)
$exeText  = [System.Text.Encoding]::ASCII.GetString([System.IO.File]::ReadAllBytes($builtExe))
$stamp    = [regex]::Match($exeText, 'MLVAPP_BUILDSTAMP_v1\|sha=([0-9a-f]{40})\|dirty=([01])')
$embedded = [regex]::Matches($exeText, '[0-9a-f]{40}') | ForEach-Object { $_.Value } | Select-Object -Unique  # diagnostic only
$stampSha = if ($stamp.Success) { $stamp.Groups[1].Value } else { '' }
$stampDirty = if ($stamp.Success) { [int]$stamp.Groups[2].Value } else { -1 }
$shaOk = ($stamp.Success -and $stampSha -eq $head)
if (-not $stamp.Success) { Write-Host "FAIL: no MLVAPP_BUILDSTAMP_v1 provenance field in the exe" -ForegroundColor Red }
elseif (-not $shaOk)     { Write-Host "FAIL: provenance stamp sha=$stampSha != HEAD $head" -ForegroundColor Red }

$dest = Join-Path $OutRoot $tag
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$destExe = Join-Path $dest ("MLVApp-" + $tag + ".exe")
Copy-Item -LiteralPath $builtExe -Destination $destExe -Force

# ---------------------------------------------------------------------------
# Make the artifact SELF-CONTAINED. The exe load-time imports the Qt6 DLLs and the
# MinGW runtime (objdump -p MLVApp.exe: Qt6Core/Gui/Multimedia/Network/OpenGL/
# OpenGLWidgets/Widgets, libgcc_s_seh-1, libgomp-1, libstdc++-6, libwinpthread-1).
# With only the exe in the artifact dir, the loader fails those imports BEFORE main()
# and the process dies with 0xC0000135 (STATUS_DLL_NOT_FOUND), silently breaking any
# reviewer/gate that points -ExePath at the artifact. Deploy the runtime next to it.
# ---------------------------------------------------------------------------
$deploy = [ordered]@{
    windeployqt        = $false
    mingwRuntimeDlls   = @()
    runtimeExtras      = @()
    runtimeExtrasDonor = $null
    selfContained      = $false
}

# 1) Qt runtime + plugin subdirs (platforms/, imageformats/, iconengines/, styles/, tls/,
#    networkinformation/, generic/, multimedia/) + opengl32sw + D3Dcompiler, plus the MinGW
#    base runtime via --compiler-runtime. windeployqt scans the exe's imports, so it deploys
#    exactly the Qt set THIS binary links (and pulls e.g. Qt6Svg for the svg image/icon plugins).
$windeployqt = Join-Path $QtBin "windeployqt.exe"
if (-not (Test-Path -LiteralPath $windeployqt)) {
    Write-Host "FAIL: windeployqt not found at $windeployqt -- cannot make the artifact self-contained" -ForegroundColor Red
    exit 7
}
& $windeployqt --release --no-translations --compiler-runtime $destExe 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: windeployqt failed ($LASTEXITCODE)" -ForegroundColor Red; exit 7 }
$deploy.windeployqt = $true

# 2) MinGW runtime DLLs. libgomp-1 (OpenMP) is NOT covered by --compiler-runtime, so copy the
#    full load-time set explicitly. Missing any one of these is a 0xC0000135 at launch.
$mingwRuntime = @('libgomp-1.dll','libgcc_s_seh-1.dll','libstdc++-6.dll','libwinpthread-1.dll')
foreach ($dll in $mingwRuntime) {
    $src = Join-Path $MingwBin $dll
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host "FAIL: required MinGW runtime DLL not found: $src" -ForegroundColor Red
        exit 7
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $dest $dll) -Force
    $deploy.mingwRuntimeDlls += $dll
}

# 3) Non-Qt runtime payload windeployqt cannot know about: FFmpeg (av*/sw*), CUDA (cudart64_12,
#    igpu_recon_cuda + its .arch.json), and app data files (pixel_maps, releases). These are
#    runtime-loaded (not load-time imports) so they don't gate startup, but a reviewer/gate that
#    exercises GPU recon or FFmpeg needs them. Admit only the exact reviewed payload names from a
#    fully deployed donor release tree. A broad donor-directory mirror can smuggle an old
#    hash-named MLVApp executable, stale manifest, or unrelated file into a new artifact and make
#    its provenance ambiguous even when the launched executable is correctly stamped.
$donorCandidates = @(
    $RuntimeDonorDir,
    (Join-Path $SourceRoot 'platform\qt\build-release\release'),
    (Join-Path $SourceRoot 'platform\qt\build-current\release')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
$donor = $donorCandidates | Select-Object -First 1
if ($donor) {
    $deploy.runtimeExtrasDonor = (Resolve-Path -LiteralPath $donor).Path
    foreach ($item in Get-ChildItem -LiteralPath $donor -Force) {
        if (-not (Test-ReviewedReleaseRuntimeExtraName -Name $item.Name)) { continue }
        if ($item.PSIsContainer) {
            throw "Reviewed runtime extra must be a file, not a directory: $($item.FullName)"
        }
        $target = Join-Path $dest $item.Name
        if (Test-Path -LiteralPath $target) { continue }   # already deployed by windeployqt / step 2
        Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        $deploy.runtimeExtras += $item.Name
    }
} else {
    Write-Host "WARN: no runtime donor dir found (looked at -RuntimeDonorDir, platform/qt/build-release|build-current/release);" -ForegroundColor Yellow
    Write-Host "      FFmpeg/CUDA/data not mirrored. The exe will START (Qt+MinGW deployed) but FFmpeg/GPU features are unavailable in this artifact." -ForegroundColor Yellow
}

# 4) Prove runnability. Launch the stamped exe with a SANITIZED PATH (system dirs only) so the
#    ONLY DLLs that can resolve are the ones just deployed beside it -- otherwise the Qt/MinGW
#    bins still on this process's PATH would mask a missing deploy and give a false pass.
#    `--batch --help` constructs the real QApplication (forcing the loader to resolve every
#    Qt6/MinGW import AND load the platforms/ plugin), prints help, and exits 0. A DLL-load
#    NTSTATUS here (0xC0000135 etc.) proves the deploy is incomplete -> fail closed.
$dllLoadFailures = @(-1073741515, -1073741701, -1073741511)  # 0xC0000135, 0xC000007B, 0xC0000139
$probeDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mlvprobe-" + $tag)
if (Test-Path -LiteralPath $probeDir) { Remove-Item -LiteralPath $probeDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $probeDir | Out-Null
$cleanPath = @(
    (Join-Path $env:SystemRoot 'System32'),
    $env:SystemRoot,
    (Join-Path $env:SystemRoot 'System32\Wbem')
) -join ';'
$probeExit = $null; $probeTimedOut = $false
$savedPath = $env:Path
try {
    $env:Path = $cleanPath
    $p = Start-Process -FilePath $destExe -ArgumentList '--batch','--help' `
        -WorkingDirectory $probeDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $probeDir 'out.txt') `
        -RedirectStandardError  (Join-Path $probeDir 'err.txt')
    if ($p.WaitForExit(30000)) { $probeExit = $p.ExitCode }
    else { try { $p.Kill() } catch {}; $probeTimedOut = $true }
} finally {
    $env:Path = $savedPath
    Remove-Item -LiteralPath $probeDir -Recurse -Force -ErrorAction SilentlyContinue
}
$probeDllFailure = ($null -ne $probeExit -and $dllLoadFailures -contains $probeExit)
$deploy.selfContained = (-not $probeDllFailure -and -not $probeTimedOut)
$launchProbe = [ordered]@{
    mode             = '--batch --help'
    sanitizedPath    = $true
    exitCode         = $probeExit
    timedOut         = $probeTimedOut
    dllLoadFailure   = $probeDllFailure
    statusDllNotFound = ($probeExit -eq -1073741515)
}
if ($probeDllFailure) {
    Write-Host ("FAIL: artifact is NOT self-contained -- launch probe exited 0x{0:X8} (DLL load failure)" -f ([uint32]$probeExit)) -ForegroundColor Red
} elseif ($probeTimedOut) {
    Write-Host "WARN: launch probe timed out after 30s (killed); could not confirm self-contained launch" -ForegroundColor Yellow
} elseif ($probeExit -ne 0) {
    Write-Host "NOTE: launch probe resolved all DLLs (no 0xC0000135) but --batch --help returned $probeExit (likely environment, e.g. no interactive desktop)" -ForegroundColor Yellow
} else {
    Write-Host "OK: launch probe (--batch --help, sanitized PATH) exited 0 -- artifact is self-contained" -ForegroundColor Green
}

$manifest = [ordered]@{
    head = $head; dirty = $dirty; tag = $tag
    describe = (& git -C $SourceRoot describe --always --dirty --abbrev=40).Trim()
    provenanceStampShaMatchesHead = $shaOk; provenanceStampSha = $stampSha; provenanceStampDirty = $stampDirty
    embeddedShas = @($embedded)
    exe = (Split-Path $destExe -Leaf); sha256 = (Get-FileHash -LiteralPath $destExe -Algorithm SHA256).Hash
    qt = "6.10.2"; toolchain = "mingw1310"; builtUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    host = $env:COMPUTERNAME
    selfContained = $deploy.selfContained; deploy = $deploy; launchProbe = $launchProbe
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $dest "build-manifest.json") -Encoding UTF8
Write-Host "OK -> $destExe" -ForegroundColor Green
Write-Host "    embedded-SHA==HEAD: $shaOk ; self-contained: $($deploy.selfContained) ; manifest written"
if (-not $shaOk) { exit 6 }
if ($probeDllFailure) { exit 8 }
