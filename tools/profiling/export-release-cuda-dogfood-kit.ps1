param(
    [string]$RepoRoot = ".",
    [string]$ExePath = "",
    [string]$OutputRoot = "",
    [string]$KitName = "",
    [Alias("Input")]
    [string]$ClipPath = "",
    [switch]$IncludeClip,
    [switch]$CreateZip,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "cuda-backend-architecture.ps1")
. (Join-Path $PSScriptRoot "provenance-stamp.ps1")

function Resolve-RepoPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return (Join-Path $Root $Path)
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $dir = Split-Path -Parent $Path
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $Value | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-FileArtifact {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            path = $Path
            exists = $false
            length = $null
            lastWriteTime = $null
            sha256 = $null
        }
    }

    $item = Get-Item -LiteralPath $Path
    [pscustomobject]@{
        path = $item.FullName
        exists = $true
        length = $item.Length
        lastWriteTime = $item.LastWriteTime
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
    }
}

function Copy-DirectoryChildren {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force |
        Copy-Item -Destination $Destination -Recurse -Force
}

function Copy-RepoFile {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$KitRoot
    )

    $source = Join-Path $Root $RelativePath
    if (!(Test-Path -LiteralPath $source -PathType Leaf)) {
        return [pscustomobject]@{
            source = $source
            destination = Join-Path $KitRoot $RelativePath
            copied = $false
            missing = $true
        }
    }

    $destination = Join-Path $KitRoot $RelativePath
    $dir = Split-Path -Parent $destination
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
    [pscustomobject]@{
        source = (Resolve-Path -LiteralPath $source).Path
        destination = $destination
        copied = $true
        missing = $false
    }
}

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([AllowNull()][string]$Value)

    if ($null -eq $Value) {
        return "''"
    }
    "'" + ([string]$Value).Replace("'", "''") + "'"
}

function Write-DogfoodReadme {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Manifest
    )

    $sourceHead = [string]$Manifest.sourceHead
    $exeSha = [string]$Manifest.artifacts.exe.sha256
    $backendSha = [string]$Manifest.artifacts.backend.sha256
    $cudartSha = [string]$Manifest.artifacts.cudart.sha256
    $zipPath = [string]$Manifest.zipPath
    $backendArch = $Manifest.backendArchitecture
    $backendTokens = @()
    if ($backendArch -and $backendArch.tokens) {
        $backendTokens = @($backendArch.tokens | ForEach-Object { [string]$_ })
    }
    $backendDetector = if ($backendArch) { [string]$backendArch.detector } else { "unknown" }
    $backendReliable = if ($backendArch) { [bool]$backendArch.detectionReliable } else { $false }
    $commands = $Manifest.commands

    $lines = [System.Collections.Generic.List[string]]::new()
    [void]$lines.Add("# MLVApp CUDA Dogfood Kit")
    [void]$lines.Add("")
    [void]$lines.Add("This folder is a portable test kit for Dell/UltraMagnus CUDA playback and DNG proof runs.")
    [void]$lines.Add('It is not hardware proof by itself. Support and speed claims require a host-local proof report with `status=QUOTABLE_PASS`.')
    [void]$lines.Add("")
    [void]$lines.Add("## Build Identity")
    [void]$lines.Add("- Source head: ``$sourceHead``")
    [void]$lines.Add("- Release exe SHA256: ``$exeSha``")
    [void]$lines.Add("- CUDA backend SHA256: ``$backendSha``")
    [void]$lines.Add("- CUDA backend architecture tokens: ``$(if ($backendTokens.Count -gt 0) { $backendTokens -join ', ' } else { 'unknown' })``")
    [void]$lines.Add("- CUDA backend architecture detector: ``$backendDetector``; reliable: ``$backendReliable``")
    [void]$lines.Add("- CUDA runtime SHA256: ``$cudartSha``")
    if (-not [string]::IsNullOrWhiteSpace($zipPath)) {
        [void]$lines.Add("- Kit zip path at creation: ``$zipPath``")
    }
    if ($backendArch -and (-not [bool]$backendArch.detectionReliable)) {
        [void]$lines.Add("")
        [void]$lines.Add("> Dell RTX 3060 Laptop note: this packaged backend does not have reliable architecture proof. Include a hash-matched `igpu_recon_cuda.arch.json` sidecar from `tools\gpu\backend\build-backend-dll.ps1 -Arch portable` before treating this zip as Dell-ready.")
    }
    elseif ($backendArch -and -not [bool]$backendArch.dellAmpereReady) {
        [void]$lines.Add("")
        [void]$lines.Add("> Dell RTX 3060 Laptop note: this packaged backend does not advertise `sm_86`. Rebuild/deploy `igpu_recon_cuda.dll` with `tools\gpu\backend\build-backend-dll.ps1 -Arch portable` before treating this zip as Dell-ready.")
    }
    [void]$lines.Add("")
    [void]$lines.Add("## Run On The NVIDIA Host")
    [void]$lines.Add("From this kit root, run:")
    [void]$lines.Add("")
    [void]$lines.Add('```powershell')
    [void]$lines.Add([string]$commands.proof)
    [void]$lines.Add('```')
    [void]$lines.Add("")
    [void]$lines.Add('Use a real Dual ISO `.MLV` path. Do not add `-DryRun` for proof.')
    [void]$lines.Add('The proof helper runs playback no-readback validation, paired CPU-vs-CUDA playback A/B, DNG hash/export proof, writes `proof-report.md` / `proof-report.json`, and packages a `mlvapp-local-cuda-proof-<host>-<stamp>.zip` return packet.')
    [void]$lines.Add('The playback A/B stage separates CUDA GL-parity proof from the speed leg by default, so cadence/FPS is measured without the heavy GL-vs-oracle readback. Use `-SkipPlaybackAbSpeedRun` only when intentionally reproducing the older two-leg shape.')
    [void]$lines.Add("")
    [void]$lines.Add("## Useful Modes")
    [void]$lines.Add("- Full proof: ``$($commands.proof)``")
    [void]$lines.Add("- Launch only: ``$($commands.launchOnly)``")
    [void]$lines.Add("- DNG trial only: ``$($commands.dngOnly)``")
    [void]$lines.Add("- DNG async-compress trial: ``$($commands.dngAsyncCompress)``")
    [void]$lines.Add("- Full proof with DNG async-compress candidate: ``$($commands.proofAsyncCompress)``")
    [void]$lines.Add("- Full proof with lossless-only DNG async-compress candidate: ``$($commands.proofLosslessAsyncCompress)``")
    [void]$lines.Add("- Read an existing summary: ``$($commands.summaryOnly)``")
    [void]$lines.Add("- Compare machine perf JSON/profile outputs: ``$($commands.compareMachinePerf)``")
    [void]$lines.Add("- Write versioned machine perf comparison JSON: ``$($commands.compareMachinePerfJson)``")
    [void]$lines.Add("- Repackage a proof run: ``$($commands.proofPacket)``")
    [void]$lines.Add("")
    [void]$lines.Add("## GUI Performance Profiling")
    [void]$lines.Add("For normal interactive dogfooding, launch `platform\qt\build-release\release\MLVApp.exe`, then use `Playback -> Performance Profiling...`.")
    [void]$lines.Add("The dialog shows the current machine fingerprint/build SHA, can copy that identity as JSON, and can enable compact field-log summaries, the scoped CUDA playback profiling preset, and the DNG async-compress profiling preset without hand-editing environment variables.")
    [void]$lines.Add("This writes compact playback/export JSONL summaries under the app logs folder; use the dialog's `Open Logs Folder` button to find them.")
    [void]$lines.Add("Compare Dell and UltraMagnus playback/profile/field-log/proof-summary JSON, including imported UltraMagnus P3/CDNG validation summaries, with:")
    [void]$lines.Add("")
    [void]$lines.Add('```powershell')
    [void]$lines.Add([string]$commands.compareMachinePerf)
    [void]$lines.Add('```')
    [void]$lines.Add("")
    [void]$lines.Add("This comparison is measurement evidence only. Keep host, clip, settings, release hash, fallback count, and no-readback percentage attached to any speed claim.")
    [void]$lines.Add('Use `-Json` or `-OutputJson <compare.json>` when the Dell/UltraMagnus comparison needs to be archived as `mlvapp.compare-machine-perf.v1` instead of copied from the console table.')
    [void]$lines.Add("For proof summaries and export field-log rows, the same table also carries CDNG/DNG hash/export coverage, DNG wall-time delta, throughput classification, and export-side suggested optimization when present.")
    [void]$lines.Add("")
    [void]$lines.Add("## DNG E3 Async Compression Trial")
    [void]$lines.Add("Lossless DNG compression-overlap remains opt-in. Use the async-compress commands above to measure whether moving LJ92 compression into the writer worker helps on this machine.")
    [void]$lines.Add("When the report emits `DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL`, rerun the lossless-only proof command before treating the signal as an E3 optimization target.")
    [void]$lines.Add("A useful first pass is queue depth 2 and thread count 2; keep DNG hash PASS, release hash, codec, frame count, and bottleneck fields with any speed claim.")
    [void]$lines.Add("")
    [void]$lines.Add("## Return Packet Import")
    [void]$lines.Add('Copy the generated `mlvapp-local-cuda-proof-*.zip` back to the repo machine and run:')
    [void]$lines.Add("")
    [void]$lines.Add('```powershell')
    [void]$lines.Add([string]$commands.importProofPacket)
    [void]$lines.Add('```')
    [void]$lines.Add("")
    [void]$lines.Add('Use `-StrictQuotable` only when automation must fail unless the packet is a real `QUOTABLE_PASS`.')
    [void]$lines.Add("")
    [void]$lines.Add("## Proof Boundaries")
    [void]$lines.Add('- Dell RTX 3060 Laptop support requires a Dell-local `QUOTABLE_PASS` packet.')
    [void]$lines.Add("- UltraMagnus RTX 4090 proof does not prove the Dell laptop, and Dell proof does not prove UltraMagnus.")
    [void]$lines.Add("- `MLVApp.exe` carries NVIDIA/AMD high-performance GPU request exports, and the proof wrappers also set Windows per-app `GpuPreference=2;` unless disabled.")
    [void]$lines.Add("- Backend architecture is a preflight gate. Dell RTX 3060 Laptop requires reliable `sm_86` proof from a hash-matched sidecar or `cuobjdump`; UltraMagnus RTX 4090 requires `sm_89` or compatible PTX. Runtime support still needs the proof packet.")
    [void]$lines.Add("- On the Dell hybrid-GPU path, CUDA discovery alone is insufficient. The packet must show GL renderer/probe evidence, no-readback frames, zero fallback frames, clean GL parity, DNG hash PASS, and playback/DNG speed metrics.")
    [void]$lines.Add('- `NOT_QUOTABLE` packets are still useful diagnostics. Read `diagnosticSummary`, `diagnostics`, and `actionPlan` in `proof-report.json` or imported `import-summary.json`.')
    [void]$lines.Add("- Rendered-video/NVENC export is not part of this kit.")
    [void]$lines.Add("")
    [void]$lines.Add("## Expected Artifacts")
    [void]$lines.Add('- `cuda-dogfood-manifest.json`: kit identity, release/backend hashes, commands, and proof boundaries.')
    [void]$lines.Add('- `.claude-state\profiling\*-cuda-dogfood\proof\summary.json`: raw local proof summary.')
    [void]$lines.Add('- `.claude-state\profiling\*-cuda-dogfood\proof\proof-report.md`: readable proof report.')
    [void]$lines.Add('- `.claude-state\profiling\*-cuda-dogfood\proof\proof-report.json`: machine-readable status, diagnostics, and action plan.')
    [void]$lines.Add('- AppData logs `mlvapp-perf-field-YYYYMMDD.jsonl`: optional GUI-enabled field log for interactive playback/export sessions.')
    [void]$lines.Add('- `mlvapp-local-cuda-proof-<host>-<stamp>.zip`: return packet for import.')

    $dir = Split-Path -Parent $Path
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    ($lines -join [Environment]::NewLine) | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Write-DogfoodLauncher {
    param([Parameter(Mandatory = $true)][string]$Path)

    $scriptText = @'
param(
    [Parameter(Mandatory = $true)]
    [Alias("Input")]
    [string]$ClipPath,
    [string]$Receipt = "receipts\FastProxy.marxml",
    [ValidateSet("uncompressed", "lossless", "fast-pass")]
    [string[]]$DngCodecs = @("uncompressed", "lossless"),
    [string]$OutputRoot = "",
    [string]$SummaryPath = "",
    [switch]$DngAsyncWriter,
    [switch]$DngAsyncWriterCompression,
    [int]$DngAsyncWriterQueueDepth = 0,
    [int]$DngAsyncWriterThreadCount = 0,
    [switch]$LaunchOnly,
    [switch]$DngOnly,
    [switch]$ProofOnly,
    [switch]$SummaryOnly,
    [switch]$NoProofPacket,
    [switch]$SkipPlaybackAbSpeedRun,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $OutputRoot = Join-Path $repoRoot ".claude-state\profiling\$stamp-cuda-dogfood"
}
$dryArgs = @()
if ($DryRun) {
    $dryArgs += "-DryRun"
}
if ($DngAsyncWriterCompression -and -not $DngAsyncWriter) {
    throw "-DngAsyncWriterCompression requires -DngAsyncWriter."
}
if ($DngAsyncWriterQueueDepth -lt 0) {
    throw "-DngAsyncWriterQueueDepth must be >= 0."
}
if ($DngAsyncWriterThreadCount -lt 0) {
    throw "-DngAsyncWriterThreadCount must be >= 0."
}
$dngOnlyOverlapArgs = @()
$proofOverlapArgs = @()
if ($DngAsyncWriter) {
    $dngOnlyOverlapArgs += "-UseAsyncWriter"
    $proofOverlapArgs += "-CandidateUseAsyncWriter"
    if ($DngAsyncWriterCompression) {
        $dngOnlyOverlapArgs += "-UseAsyncWriterCompression"
        $proofOverlapArgs += "-CandidateUseAsyncWriterCompression"
    }
    if ($DngAsyncWriterQueueDepth -gt 0) {
        $dngOnlyOverlapArgs += @("-AsyncWriterQueueDepth", [string]$DngAsyncWriterQueueDepth)
        $proofOverlapArgs += @("-CandidateAsyncWriterQueueDepth", [string]$DngAsyncWriterQueueDepth)
    }
    if ($DngAsyncWriterThreadCount -gt 0) {
        $dngOnlyOverlapArgs += @("-AsyncWriterThreadCount", [string]$DngAsyncWriterThreadCount)
        $proofOverlapArgs += @("-CandidateAsyncWriterThreadCount", [string]$DngAsyncWriterThreadCount)
    }
}
if ($SkipPlaybackAbSpeedRun) {
    $proofOverlapArgs += "-SkipPlaybackAbSpeedRun"
}

if ($SummaryOnly) {
    if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
        $SummaryPath = Join-Path $repoRoot ".claude-state\profiling\local-cuda-playback-dng-smoke-latest.json"
    }
    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $repoRoot "tools\profiling\summarize-local-cuda-proof.ps1") `
        -RepoRoot $repoRoot `
        -SummaryPath $SummaryPath
    exit $LASTEXITCODE
}

if ($LaunchOnly) {
    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $repoRoot "tools\profiling\start-release-cuda-playback.ps1") `
        -RepoRoot $repoRoot `
        -Input $ClipPath `
        -OutputRoot (Join-Path $OutputRoot "launch") `
        @dryArgs
    exit $LASTEXITCODE
}

if ($DngOnly) {
    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $repoRoot "tools\profiling\run-release-cuda-dng-export.ps1") `
        -RepoRoot $repoRoot `
        -Input $ClipPath `
        -Output (Join-Path $OutputRoot "dng-out") `
        -Receipt $Receipt `
        -CdngCodec lossless `
        -OutputRoot (Join-Path $OutputRoot "dng-export") `
        @dngOnlyOverlapArgs `
        @dryArgs
    exit $LASTEXITCODE
}

& pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File (Join-Path $repoRoot "tools\profiling\run-local-cuda-playback-dng-smoke.ps1") `
    -RepoRoot $repoRoot `
    -Input $ClipPath `
    -Receipt $Receipt `
    -OutputRoot (Join-Path $OutputRoot "proof") `
    -CdngCodecs ($DngCodecs -join ',') `
    @proofOverlapArgs `
    @dryArgs
$proofExit = $LASTEXITCODE
$proofSummary = Join-Path $OutputRoot "proof\summary.json"
if (Test-Path -LiteralPath $proofSummary -PathType Leaf) {
    & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $repoRoot "tools\profiling\summarize-local-cuda-proof.ps1") `
        -RepoRoot $repoRoot `
        -SummaryPath $proofSummary `
        -Output (Join-Path $OutputRoot "proof\proof-report.md")
    if (-not $NoProofPacket) {
        & pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
            -File (Join-Path $repoRoot "tools\profiling\package-local-cuda-proof-result.ps1") `
            -RepoRoot $repoRoot `
            -RunRoot (Join-Path $OutputRoot "proof") `
            @dryArgs
    }
}
exit $proofExit
'@

    $dir = Split-Path -Parent $Path
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $scriptText | Set-Content -LiteralPath $Path -Encoding UTF8
}

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = Join-Path $root "platform\qt\build-release\release\MLVApp.exe"
}
else {
    $ExePath = Resolve-RepoPath -Root $root -Path $ExePath
}
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$releaseDir = Split-Path -Parent $exe

# P0-3: name the kit from the EXE's OWN embedded provenance stamp -- never from the
# worktree HEAD. A kit dir whose name disagrees with the binary it ships is exactly
# the lie this whole provenance pass exists to kill (e.g. "mlvapp-cuda-dogfood-6f6a50f4"
# that shipped an exe embedding 3f27b7b0). Hard-fail if the stamp is missing, the
# embedded SHA != worktree HEAD, the binary was built dirty, or the tree is dirty now.
$buildStamp = Get-MlvAppBuildStamp -ExePath $exe
$worktreeHead = (& git -C $root rev-parse --verify HEAD 2>$null)
if ($worktreeHead) { $worktreeHead = $worktreeHead.Trim() }
$worktreePorcelain = @(& git -C $root status --porcelain 2>$null | Where-Object { $_ })
$worktreeDirty = ($worktreePorcelain.Count -gt 0)

$provenanceErrors = [System.Collections.Generic.List[string]]::new()
if (-not $buildStamp.found) {
    [void]$provenanceErrors.Add("exe has no MLVAPP_BUILDSTAMP_v1 provenance field: $exe")
}
if ($buildStamp.found -and $worktreeHead -and ($buildStamp.sha -ne $worktreeHead)) {
    [void]$provenanceErrors.Add("embedded SHA $($buildStamp.sha) != worktree HEAD $worktreeHead")
}
if ($buildStamp.found -and ($buildStamp.dirty -ne 0)) {
    [void]$provenanceErrors.Add("exe was built from a DIRTY tree (embedded dirty=$($buildStamp.dirty))")
}
if ($worktreeDirty) {
    [void]$provenanceErrors.Add("working tree is dirty ($($worktreePorcelain.Count) path(s)); commit/clean before packaging a kit")
}
if ($provenanceErrors.Count -gt 0) {
    Write-Host "ABORT (P0-3 provenance gate): refusing to package a kit whose name could lie about its binary." -ForegroundColor Red
    $provenanceErrors | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Write-Host "  Build a trustworthy, SHA-named release first: pwsh -NoProfile -File tools\build-release.ps1" -ForegroundColor Yellow
    exit 1
}
$embeddedSha = $buildStamp.sha
$embeddedShaShort = $embeddedSha.Substring(0, 8)

if ([string]::IsNullOrWhiteSpace($KitName)) {
    $KitName = "mlvapp-cuda-dogfood-$embeddedShaShort"
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $OutputRoot = Join-Path $root ".claude-state\profiling\$stamp-cuda-dogfood-kit"
}
$outputRootFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    (Resolve-RepoPath -Root $root -Path $OutputRoot))
$kitRoot = Join-Path $outputRootFull $KitName
$manifestPath = Join-Path $kitRoot "cuda-dogfood-manifest.json"
$zipPath = Join-Path $outputRootFull "$KitName.zip"

$backendDllPath = Join-Path $releaseDir "igpu_recon_cuda.dll"
$backendSidecarPath = Get-CudaBackendArchitectureSidecarPath -Path $backendDllPath
$artifacts = [pscustomobject]@{
    exe = Get-FileArtifact -Path $exe
    backend = Get-FileArtifact -Path $backendDllPath
    backendArchitectureSidecar = Get-FileArtifact -Path $backendSidecarPath
    cudart = Get-FileArtifact -Path (Join-Path $releaseDir "cudart64_12.dll")
    qwindows = Get-FileArtifact -Path (Join-Path $releaseDir "platforms\qwindows.dll")
}
$backendArchitecture = Get-CudaBackendArchitectureInfo -Path $backendDllPath
$missing = @($artifacts.PSObject.Properties |
    Where-Object { $_.Name -ne "backendArchitectureSidecar" -and -not [bool]$_.Value.exists } |
    ForEach-Object { $_.Name })

$status = @(& git -C $root status --short --branch 2>$null | ForEach-Object { [string]$_ })
$clipFullPath = ""
if (-not [string]::IsNullOrWhiteSpace($ClipPath)) {
    $clipFullPath = Resolve-RepoPath -Root $root -Path $ClipPath
    if (-not $DryRun) {
        $clipFullPath = (Resolve-Path -LiteralPath $clipFullPath).Path
    }
}

$copyPlan = @(
    [pscustomobject]@{ source = $releaseDir; destination = "platform\qt\build-release\release"; kind = "release-tree" },
    [pscustomobject]@{ source = (Join-Path $root "tools\profiling"); destination = "tools\profiling"; kind = "profiling-tools" },
    [pscustomobject]@{ source = (Join-Path $root "receipts"); destination = "receipts"; kind = "receipts" }
)
if ($IncludeClip -and -not [string]::IsNullOrWhiteSpace($clipFullPath)) {
    $copyPlan += [pscustomobject]@{ source = $clipFullPath; destination = ("clips\" + [System.IO.Path]::GetFileName($clipFullPath)); kind = "optional-clip" }
}

$manifest = [ordered]@{
    schema = "mlvapp-cuda-dogfood-kit.v1"
    status = if ($missing.Count -eq 0) { if ($DryRun) { "planned" } else { "starting" } } else { "failed" }
    createdAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    dryRun = [bool]$DryRun
    repoRoot = $root
    sourceHead = $embeddedSha
    embeddedSha = $embeddedSha
    worktreeHead = $worktreeHead
    worktreeDirty = $worktreeDirty
    provenanceStampField = $buildStamp.stampField
    provenanceStampShaMatchesHead = ($embeddedSha -eq $worktreeHead)
    sourceStatus = $status
    kitRoot = $kitRoot
    zipPath = if ($CreateZip) { $zipPath } else { $null }
    operatorGuide = "README-CUDA-DOGFOOD.md"
    artifacts = $artifacts
    backendArchitecture = $backendArchitecture
    backendCompatibility = [pscustomobject]@{
        schema = "mlvapp.cuda-backend-kit-compatibility.v1"
        dellAmpereSm86Ready = [bool]$backendArchitecture.dellAmpereReady
        ultraMagnusAdaSm89Ready = [bool]$backendArchitecture.ultraMagnusAdaReady
        detectionReliable = [bool]$backendArchitecture.detectionReliable
        detector = [string]$backendArchitecture.detector
        warning = if ([bool]$backendArchitecture.dellAmpereReady -and [bool]$backendArchitecture.detectionReliable) {
            $null
        } elseif (-not [bool]$backendArchitecture.detectionReliable) {
            "Packaged backend architecture proof is not reliable; include a hash-matched igpu_recon_cuda.arch.json sidecar or run on a host with cuobjdump before Dell RTX 3060 Laptop proof."
        } else {
            "Packaged backend does not advertise sm_86; rebuild/deploy portable backend before Dell RTX 3060 Laptop proof."
        }
    }
    missingRequiredArtifacts = @($missing)
    copyPlan = $copyPlan
    includedClip = if ($IncludeClip -and -not [string]::IsNullOrWhiteSpace($clipFullPath)) {
        Get-FileArtifact -Path $clipFullPath
    } else { $null }
    commands = [pscustomobject]@{
        proof = ".\RUN-CUDA-DOGFOOD.ps1 -Input <clip.mlv>"
        launchOnly = ".\RUN-CUDA-DOGFOOD.ps1 -Input <clip.mlv> -LaunchOnly"
        dngOnly = ".\RUN-CUDA-DOGFOOD.ps1 -Input <clip.mlv> -DngOnly"
        dngAsyncCompress = ".\RUN-CUDA-DOGFOOD.ps1 -Input <clip.mlv> -DngOnly -DngAsyncWriter -DngAsyncWriterCompression -DngAsyncWriterQueueDepth 2 -DngAsyncWriterThreadCount 2"
        proofAsyncCompress = ".\RUN-CUDA-DOGFOOD.ps1 -Input <clip.mlv> -DngAsyncWriter -DngAsyncWriterCompression -DngAsyncWriterQueueDepth 2 -DngAsyncWriterThreadCount 2"
        proofLosslessAsyncCompress = ".\RUN-CUDA-DOGFOOD.ps1 -Input <clip.mlv> -DngCodecs lossless -DngAsyncWriter -DngAsyncWriterCompression -DngAsyncWriterQueueDepth 2 -DngAsyncWriterThreadCount 2"
        summaryOnly = ".\RUN-CUDA-DOGFOOD.ps1 -Input <clip.mlv> -SummaryOnly -SummaryPath <summary.json>"
        proofPacket = ".\tools\profiling\package-local-cuda-proof-result.ps1 -RepoRoot . -RunRoot <proof-run-root>"
        importProofPacket = ".\tools\profiling\import-local-cuda-proof-result.ps1 -RepoRoot . -PacketPath <mlvapp-local-cuda-proof.zip>"
        compareMachinePerf = ".\tools\profiling\compare-machine-perf.ps1 <dell-summary-or-profile-or-jsonl> <ultramagnus-summary-or-validation-summary-or-jsonl>"
        compareMachinePerfJson = ".\tools\profiling\compare-machine-perf.ps1 -OutputJson .\machine-compare.json <dell-summary-or-profile-or-jsonl> <ultramagnus-summary-or-validation-summary-or-jsonl>"
        directProof = ".\tools\profiling\run-local-cuda-playback-dng-smoke.ps1 -RepoRoot . -Input <clip.mlv>"
        directSummary = ".\tools\profiling\summarize-local-cuda-proof.ps1 -RepoRoot . -SummaryPath <summary.json>"
    }
    proofBoundary = [pscustomobject]@{
        packageOnly = $true
        provesDellSupport = $false
        provesRealtimePlayback = $false
        provesDngHashParity = $false
        notes = @(
            "This kit preserves the release tree and profiling scripts needed to run CUDA playback and DNG/CDNG proof locally on a NVIDIA host.",
            "A copied kit is not hardware proof. Quote Dell or UltraMagnus support only from a passing local proof summary produced on that host.",
            "Rendered-video export remains out of scope for this kit."
        )
    }
}

New-Item -ItemType Directory -Force -Path $kitRoot | Out-Null
if ($missing.Count -gt 0) {
    Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath
    $manifest | ConvertTo-Json -Depth 16
    exit 1
}

if (-not $DryRun) {
    Copy-DirectoryChildren -Source $releaseDir -Destination (Join-Path $kitRoot "platform\qt\build-release\release")
    Copy-DirectoryChildren -Source (Join-Path $root "tools\profiling") -Destination (Join-Path $kitRoot "tools\profiling")
    Copy-DirectoryChildren -Source (Join-Path $root "receipts") -Destination (Join-Path $kitRoot "receipts")
    [void](Copy-RepoFile -Root $root -RelativePath "docs\gpu-lane-playback-export-roadmap.md" -KitRoot $kitRoot)

    if ($IncludeClip -and -not [string]::IsNullOrWhiteSpace($clipFullPath)) {
        $clipDestinationDir = Join-Path $kitRoot "clips"
        New-Item -ItemType Directory -Force -Path $clipDestinationDir | Out-Null
        Copy-Item -LiteralPath $clipFullPath -Destination (Join-Path $clipDestinationDir ([System.IO.Path]::GetFileName($clipFullPath))) -Force
    }

    Write-DogfoodLauncher -Path (Join-Path $kitRoot "RUN-CUDA-DOGFOOD.ps1")
    Write-DogfoodReadme -Path (Join-Path $kitRoot "README-CUDA-DOGFOOD.md") -Manifest ([pscustomobject]$manifest)
}

$manifest["status"] = if ($DryRun) { "planned" } else { "created" }
Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath

if ($CreateZip -and -not $DryRun) {
    Compress-Archive -Path (Join-Path $kitRoot "*") -DestinationPath $zipPath -Force
    $zipArtifact = Get-FileArtifact -Path $zipPath
    $manifest["zip"] = $zipArtifact
    Write-JsonFile -Value ([pscustomobject]$manifest) -Path $manifestPath
}

$manifest | ConvertTo-Json -Depth 16
