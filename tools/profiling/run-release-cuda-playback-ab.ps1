param(
    [string]$RepoRoot = ".",
    [string]$ExePath = "",
    [Alias("Input")]
    [string]$ClipPath = "C:\temp\MLV\M16-1327.MLV",
    [string]$Receipt = "receipts\FastProxy.marxml",
    [string]$BackendDll = "",
    [string]$AmazeBackendDll = "",
    [string]$OutputRoot = "",
    [int]$Seconds = 30,
    [int]$SettleMs = 2500,
    [int]$StartFrame = 0,
    [string]$Threads = "auto",
    [string]$QualityMode = "4",
    [string]$ScaleFactor = "1",
    [int]$ValidationSampleEvery = 10,
    [string]$GpuPlaybackReconBackend = "",
    [int]$CudaAmazeLiveTileStreams = 0,
    [switch]$CudaAmazeFastLaunchChecks,
    [switch]$CudaAmazeLiveDirectRgbaStore,
    [switch]$GpuPlaybackReconRetainDeviceOutput,
    [switch]$GpuTexNrAcquireLatestReady,
    [switch]$GpuTexNrImmediateDrainReady,
    [int]$GpuTexNrImmediateDrainMax = 0,
    [int]$Phase3FrameSlots = 0,
    [int]$PlaybackTimerPollMs = 0,
    [int]$PlaybackRenderLookaheadFrames = 0,
    [switch]$CandidateFirst,
    [switch]$NoScreenshot,
    [switch]$FailOnColorArtifact,
    [switch]$RequireCandidateImprovesPresentedFps,
    [switch]$RequireCandidateGlParity,
    [switch]$SeparateCandidateSpeedRun,
    [switch]$NoHighPerformancePreference,
    [switch]$SkipBackendBuild,
    [switch]$SkipAmazeBackendBuild,
    [switch]$AllowLegacyR16TextureCandidate,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "cuda-backend-architecture.ps1")

if ($Seconds -lt 1) {
    throw "-Seconds must be >= 1."
}
if ($SettleMs -lt 0) {
    throw "-SettleMs must be >= 0."
}
if ($GpuTexNrImmediateDrainMax -lt 0) {
    throw "-GpuTexNrImmediateDrainMax must be >= 0."
}
if ($ValidationSampleEvery -lt 1) {
    throw "-ValidationSampleEvery must be >= 1."
}
if ($CudaAmazeLiveTileStreams -lt 0) {
    throw "-CudaAmazeLiveTileStreams must be >= 0. Use 0 for backend default."
}
if ($Phase3FrameSlots -lt 0) {
    throw "-Phase3FrameSlots must be >= 0. Use 0 for renderer default."
}
if ($PlaybackTimerPollMs -lt 0) {
    throw "-PlaybackTimerPollMs must be >= 0. Use 0 for renderer default."
}
if ($PlaybackRenderLookaheadFrames -lt 0 -or $PlaybackRenderLookaheadFrames -gt 3) {
    throw "-PlaybackRenderLookaheadFrames must be between 0 and 3. Use 0 for renderer default."
}

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
    $Value | ConvertTo-Json -Depth 64 | Set-Content -LiteralPath $Path -Encoding UTF8
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

function Convert-PlaybackLogLineToObject {
    param([string]$Line)

    $result = [ordered]@{}
    $matches = [regex]::Matches($Line, '(?<key>[A-Za-z0-9_]+)=(?<value>"[^"]*"|\S+)')
    foreach ($match in $matches) {
        $key = $match.Groups["key"].Value
        $rawValue = $match.Groups["value"].Value.Trim('"')

        $longValue = 0L
        $doubleValue = 0.0
        if ([long]::TryParse($rawValue, [ref]$longValue)) {
            $result[$key] = $longValue
        }
        elseif ([double]::TryParse(
            $rawValue,
            [System.Globalization.NumberStyles]::Float,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [ref]$doubleValue)) {
            $result[$key] = $doubleValue
        }
        else {
            $result[$key] = $rawValue
        }
    }
    [pscustomobject]$result
}

function Get-PlaybackLogTimestampUtc {
    param([string]$Line)

    if ([string]::IsNullOrWhiteSpace($Line)) {
        return $null
    }
    $match = [regex]::Match($Line, '^\[(?<ts>[^\]]+)\]')
    if (!$match.Success) {
        return $null
    }

    $dto = [datetimeoffset]::MinValue
    if ([datetimeoffset]::TryParse(
        $match.Groups["ts"].Value,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::AssumeUniversal,
        [ref]$dto)) {
        return $dto.UtcDateTime
    }

    return $null
}

function Get-ScopedPlaybackLogLines {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][string]$Pattern,
        [AllowNull()][object]$StartedAtUtc,
        [AllowNull()][object]$EndedAtUtc
    )

    $startUtc = $null
    $endUtc = $null
    if ($StartedAtUtc) {
        $startUtc = ([datetimeoffset]$StartedAtUtc).UtcDateTime.AddSeconds(-1)
    }
    if ($EndedAtUtc) {
        $endUtc = ([datetimeoffset]$EndedAtUtc).UtcDateTime.AddSeconds(1)
    }

    $matches = @(Select-String -LiteralPath $LogPath -Pattern $Pattern)
    if ($null -eq $startUtc -and $null -eq $endUtc) {
        return $matches
    }

    $scoped = [System.Collections.Generic.List[object]]::new()
    foreach ($match in $matches) {
        $timestampUtc = Get-PlaybackLogTimestampUtc -Line $match.Line
        if ($null -eq $timestampUtc) {
            continue
        }
        if ($null -ne $startUtc -and $timestampUtc -lt $startUtc) {
            continue
        }
        if ($null -ne $endUtc -and $timestampUtc -gt $endUtc) {
            continue
        }
        $scoped.Add($match)
    }
    return @($scoped)
}

function Add-NullableDouble {
    param(
        [System.Collections.Generic.List[double]]$Values,
        [AllowNull()]$Value
    )

    if ($null -eq $Value) {
        return
    }

    $parsed = 0.0
    if ([double]::TryParse(
        [string]$Value,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed)) {
        [void]$Values.Add($parsed)
    }
}

function Get-AverageOrNull {
    param([System.Collections.Generic.List[double]]$Values)

    if ($null -eq $Values -or $Values.Count -eq 0) {
        return $null
    }

    [Math]::Round(($Values | Measure-Object -Average).Average, 3)
}

function Get-GpuPreferenceValue {
    param([Parameter(Mandatory = $true)][string]$Executable)
    $prefKey = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
    if (!(Test-Path -LiteralPath $prefKey)) {
        return $null
    }
    $item = Get-ItemProperty -LiteralPath $prefKey -Name $Executable -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        return $null
    }
    return [string]$item.$Executable
}

function Set-GpuPreferenceHighPerformance {
    param([Parameter(Mandatory = $true)][string]$Executable)
    $prefKey = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
    New-Item -Path $prefKey -Force | Out-Null
    New-ItemProperty `
        -Path $prefKey `
        -Name $Executable `
        -Value "GpuPreference=2;" `
        -PropertyType String `
        -Force | Out-Null
}

function Convert-ToNullableDouble {
    param([object]$Value)
    if ($null -eq $Value) {
        return $null
    }

    $parsed = 0.0
    if ([double]::TryParse(
        [string]$Value,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed)) {
        return $parsed
    }
    $null
}

function Convert-ToNullableInt64 {
    param([object]$Value)
    if ($null -eq $Value) {
        return $null
    }

    $parsed = 0L
    if ([long]::TryParse(
        [string]$Value,
        [System.Globalization.NumberStyles]::Integer,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed)) {
        return $parsed
    }
    $null
}

function Get-NestedValue {
    param(
        [object]$Object,
        [string]$Path
    )

    $current = $Object
    foreach ($part in $Path.Split(".")) {
        if ($null -eq $current) {
            return $null
        }
        $property = $current.PSObject.Properties[$part]
        if (-not $property) {
            return $null
        }
        $current = $property.Value
    }
    $current
}

function New-MetricDelta {
    param(
        [object]$BaselineValue,
        [object]$CandidateValue
    )

    $baseline = Convert-ToNullableDouble $BaselineValue
    $candidate = Convert-ToNullableDouble $CandidateValue
    $delta = $null
    $deltaPercent = $null
    if ($null -ne $baseline -and $null -ne $candidate) {
        $delta = [Math]::Round($candidate - $baseline, 6)
        if ([Math]::Abs($baseline) -gt 0.000001) {
            $deltaPercent = [Math]::Round((($candidate - $baseline) / $baseline) * 100.0, 3)
        }
    }

    [pscustomobject]@{
        baseline = $baseline
        candidate = $candidate
        delta = $delta
        deltaPercent = $deltaPercent
    }
}

function Get-CompareDeltaPercent {
    param(
        [object]$Compare,
        [string]$Metric
    )
    Convert-ToNullableDouble (Get-NestedValue $Compare "$Metric.deltaPercent")
}

function Test-DeltaAtLeast {
    param(
        [object]$Value,
        [double]$Threshold
    )
    $parsed = Convert-ToNullableDouble $Value
    ($null -ne $parsed -and $parsed -ge $Threshold)
}

function New-PlaybackAbAnalysis {
    param(
        [object]$Compare,
        [string[]]$ProofFailures,
        [string]$ComparisonBasis = "candidate"
    )

    $fpsDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "presentedFps"
    $presentIntervalDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgPresentIntervalMs"
    $renderWorkDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgRenderWorkMs"
    $queueWaitDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgQueueWaitMs"
    $llrawprocDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgLlrawprocMs"
    $drawTotalDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgDrawTotalMs"
    $presentUiSignalLatencyDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgPresentUiSignalLatencyMs"
    $presentDrawPresentDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgPresentDrawPresentMs"
    $presentPacingDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgPresentPacingMs"
    $prepBeforeFinishDeltaPct = Get-CompareDeltaPercent -Compare $Compare -Metric "avgPrepBeforeFinishMs"
    $candidateQueueWaitMs = Convert-ToNullableDouble (Get-NestedValue $Compare "avgQueueWaitMs.candidate")
    $candidateDrawTotalMs = Convert-ToNullableDouble (Get-NestedValue $Compare "avgDrawTotalMs.candidate")
    $candidateTextureTotalMs = Convert-ToNullableDouble (Get-NestedValue $Compare "avgTextureTotalMs.candidate")
    $candidateTextureWallMs = Convert-ToNullableDouble (Get-NestedValue $Compare "avgTextureWallMs.candidate")
    $candidatePresentIntervalMs = Convert-ToNullableDouble (Get-NestedValue $Compare "avgPresentIntervalMs.candidate")

    $dominant = "unknown"
    $suggestion = "rerun_playback_ab_for_stage_metrics"
    $confidence = "missing_metrics"

    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 3: this analysis is embedded in the summary JSON and
    # trusted verbatim downstream (compare-machine-perf.ps1's Get-ProofSummarySuggestion reads
    # .analysis.suggestedOptimization directly). A host-load-provisional leg must refuse the
    # delta-based bottleneck diagnosis here too, or the refusal never reaches that consumer -- the
    # fps deltas below ($fpsDeltaPct etc.) are still computed and kept in .evidence for
    # transparency, only the derived diagnosis is withheld.
    $hostLoadProofFailures = @($ProofFailures | Where-Object { $_ -match 'host-load-provisional' })

    if ($null -ne $fpsDeltaPct) {
        $confidence = "observed_delta"
        if ($fpsDeltaPct -lt -1.0) {
            if ((Test-DeltaAtLeast -Value $queueWaitDeltaPct -Threshold 25.0) -and
                ((Test-DeltaAtLeast -Value $drawTotalDeltaPct -Threshold 10.0) -or
                 (Test-DeltaAtLeast -Value $prepBeforeFinishDeltaPct -Threshold 10.0) -or
                 (Test-DeltaAtLeast -Value $presentIntervalDeltaPct -Threshold 10.0))) {
                $dominant = "present-bound"
                $suggestion = "reduce_gpu_present_draw_queue_sync"
                $confidence = "observed_stage_regression"
            }
            elseif ((Test-DeltaAtLeast -Value $queueWaitDeltaPct -Threshold 25.0) -or
                    (Test-DeltaAtLeast -Value $presentUiSignalLatencyDeltaPct -Threshold 25.0)) {
                $dominant = "queue-wait-bound"
                $suggestion = "reduce_render_queue_wait_or_present_signal_latency"
                $confidence = "observed_stage_regression"
            }
            elseif (Test-DeltaAtLeast -Value $presentPacingDeltaPct -Threshold 25.0) {
                $dominant = "frame-pacing-bound"
                $suggestion = "stabilize_playback_frame_pacing"
                $confidence = "observed_stage_regression"
            }
            elseif ((Test-DeltaAtLeast -Value $drawTotalDeltaPct -Threshold 15.0) -or
                    (Test-DeltaAtLeast -Value $presentDrawPresentDeltaPct -Threshold 15.0) -or
                    (Test-DeltaAtLeast -Value $prepBeforeFinishDeltaPct -Threshold 15.0) -or
                    (Test-DeltaAtLeast -Value $presentIntervalDeltaPct -Threshold 15.0)) {
                $dominant = "present-bound"
                $suggestion = "reduce_gpu_present_jitter"
                $confidence = "observed_stage_regression"
            }
            elseif ((Test-DeltaAtLeast -Value $llrawprocDeltaPct -Threshold 10.0) -or
                    (Test-DeltaAtLeast -Value $renderWorkDeltaPct -Threshold 10.0)) {
                $dominant = "recon-bound"
                $suggestion = "optimize_cuda_recon_kernel_or_state_setup"
                $confidence = "observed_stage_regression"
            }
            else {
                $dominant = "cadence-bound"
                $suggestion = "profile_playback_cadence_with_stage_timing"
            }
        }
        elseif ($fpsDeltaPct -lt 10.0) {
            $dominant = "mixed"
            $suggestion = "profile_present_decode_and_recon_stages"
        }
        else {
            if ($null -ne $candidateQueueWaitMs -and
                $candidateQueueWaitMs -ge 15.0 -and
                ($null -eq $candidateTextureTotalMs -or $candidateQueueWaitMs -ge $candidateTextureTotalMs)) {
                $dominant = "queue-wait-bound"
                $suggestion = "reduce_render_queue_wait_or_pipeline_gpu_tex_nr_present"
                $confidence = "observed_absolute_candidate_stage"
            }
            elseif ($null -ne $candidateDrawTotalMs -and
                   $candidateDrawTotalMs -ge 15.0 -and
                   ($null -eq $candidateTextureTotalMs -or $candidateDrawTotalMs -ge $candidateTextureTotalMs)) {
                $dominant = "present-bound"
                $suggestion = "reduce_gpu_present_draw_queue_sync"
                $confidence = "observed_absolute_candidate_stage"
            }
            elseif ($null -ne $candidateTextureTotalMs -and
                   $candidateTextureTotalMs -ge 10.0) {
                $dominant = "gpu-texture-bound"
                $suggestion = "optimize_direct_device_recon_amaze_texture_kernel"
                $confidence = "observed_absolute_candidate_stage"
            }
            else {
                $dominant = "none"
                $suggestion = "validate_same_clip_on_second_machine"
            }
        }
    }

    if ($hostLoadProofFailures.Count -gt 0) {
        $dominant = "host-load-provisional"
        $suggestion = "rerun_playback_ab_with_quiet_host"
        $confidence = "host_load_provisional"
    }

    [pscustomobject]@{
        schema = "mlvapp.playback_ab_analysis.v1"
        proofPassed = (@($ProofFailures).Count -eq 0)
        comparisonBasis = $ComparisonBasis
        dominantBottleneck = $dominant
        suggestedOptimization = $suggestion
        confidence = $confidence
        evidence = [pscustomobject]@{
            presentedFpsDeltaPercent = $fpsDeltaPct
            presentIntervalDeltaPercent = $presentIntervalDeltaPct
            renderWorkDeltaPercent = $renderWorkDeltaPct
            queueWaitDeltaPercent = $queueWaitDeltaPct
            llrawprocDeltaPercent = $llrawprocDeltaPct
            drawTotalDeltaPercent = $drawTotalDeltaPct
            presentUiSignalLatencyDeltaPercent = $presentUiSignalLatencyDeltaPct
            presentDrawPresentDeltaPercent = $presentDrawPresentDeltaPct
            presentPacingDeltaPercent = $presentPacingDeltaPct
            prepBeforeFinishDeltaPercent = $prepBeforeFinishDeltaPct
            candidateQueueWaitMs = $candidateQueueWaitMs
            candidateDrawTotalMs = $candidateDrawTotalMs
            candidateTextureTotalMs = $candidateTextureTotalMs
            candidateTextureWallMs = $candidateTextureWallMs
            candidatePresentIntervalMs = $candidatePresentIntervalMs
        }
        proofFailures = @($ProofFailures)
    }
}

function Resolve-NvidiaSmiCommand {
    $cmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        "C:\Windows\System32\nvidia-smi.exe",
        (Join-Path $env:ProgramFiles "NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
        (Join-Path $env:ProgramW6432 "NVIDIA Corporation\NVSMI\nvidia-smi.exe")
    )
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $null
}

function Get-NvidiaSmiSnapshot {
    $cmd = Resolve-NvidiaSmiCommand
    if (-not $cmd) {
        return [pscustomobject]@{
            found = $false
            path = $null
            output = @()
            error = "nvidia-smi not found"
        }
    }

    try {
        $output = & $cmd --query-gpu=name,driver_version,compute_cap,memory.total --format=csv,noheader 2>&1
        return [pscustomobject]@{
            found = $true
            path = $cmd
            output = @($output | ForEach-Object { [string]$_ })
            error = $null
        }
    }
    catch {
        return [pscustomobject]@{
            found = $true
            path = $cmd.Source
            output = @()
            error = $_.Exception.Message
        }
    }
}

function Convert-ComputeCapabilityToCudaToken {
    param([AllowNull()][string]$ComputeCapability)

    if ([string]::IsNullOrWhiteSpace($ComputeCapability)) {
        return $null
    }
    $trimmed = ([string]$ComputeCapability).Trim()
    if ($trimmed -match '^([0-9]+)\.([0-9]+)$') {
        return "sm_$($Matches[1])$($Matches[2])"
    }
    if ($trimmed -match '^([0-9]{2,3})$') {
        return "sm_$trimmed"
    }
    $null
}

function Get-NvidiaSmiComputeCapability {
    param([AllowNull()]$NvidiaSmi)

    if ($null -eq $NvidiaSmi -or -not [bool]$NvidiaSmi.found) {
        return $null
    }
    $row = @($NvidiaSmi.output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -First 1)
    if ($row.Count -eq 0) {
        return $null
    }
    $parts = @([string]$row[0] -split "," | ForEach-Object { ([string]$_).Trim() })
    if ($parts.Count -lt 3) {
        return $null
    }
    [string]$parts[2]
}

function Get-FirstNvidiaSmiFingerprintRow {
    param([AllowNull()]$NvidiaSmi)

    if ($null -eq $NvidiaSmi -or -not [bool]$NvidiaSmi.found) {
        return $null
    }
    $row = @($NvidiaSmi.output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -First 1)
    if ($row.Count -eq 0) {
        return $null
    }
    $parts = @([string]$row[0] -split "," | ForEach-Object { ([string]$_).Trim() })
    if ($parts.Count -lt 4) {
        return $null
    }
    $vram = $null
    $parsedVram = 0
    if ([int]::TryParse($parts[3], [ref]$parsedVram)) {
        $vram = $parsedVram
    }
    elseif ($parts[3] -match '([0-9]+)') {
        $parsedVram = [int]$Matches[1]
        $vram = $parsedVram
    }
    [pscustomobject]@{
        gpu_name = $parts[0]
        gpu_driver_version = $parts[1]
        gpu_compute_capability = $parts[2]
        gpu_vram_total_mb = $vram
    }
}

function Get-RepoBuildSha {
    param([string]$RepoRoot)
    try {
        $sha = (& git -C $RepoRoot rev-parse --verify HEAD 2>$null)
        if (-not [string]::IsNullOrWhiteSpace($sha)) {
            return [string]$sha
        }
    }
    catch {
    }
    $null
}

function Get-LocalMachineFingerprint {
    param(
        [string]$RepoRoot,
        [AllowNull()]$NvidiaSmi
    )

    $gpu = Get-FirstNvidiaSmiFingerprintRow -NvidiaSmi $NvidiaSmi
    $cpu = $null
    $computer = $null
    $os = $null
    try { $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 } catch {}
    try { $computer = Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 } catch {}
    try { $os = Get-CimInstance Win32_OperatingSystem | Select-Object -First 1 } catch {}

    [pscustomobject]@{
        schema = "machine-fingerprint.v1"
        build_sha = Get-RepoBuildSha -RepoRoot $RepoRoot
        gpu_name = if ($gpu) { $gpu.gpu_name } else { $null }
        gpu_driver_version = if ($gpu) { $gpu.gpu_driver_version } else { $null }
        gpu_compute_capability = if ($gpu) { $gpu.gpu_compute_capability } else { $null }
        gpu_vram_total_mb = if ($gpu) { $gpu.gpu_vram_total_mb } else { $null }
        cpu_model = if ($cpu) { [string]$cpu.Name } else { $null }
        cpu_cores = if ($cpu) { [int]$cpu.NumberOfCores } else { $null }
        cpu_threads = if ($cpu) { [int]$cpu.NumberOfLogicalProcessors } else { $null }
        ram_total_mb = if ($computer) { [int64]([math]::Round([double]$computer.TotalPhysicalMemory / 1MB)) } else { $null }
        os_version = if ($os) { "$($os.Caption) $($os.Version)" } else { $null }
    }
}

function Test-CudaBackendCompatibility {
    param(
        [AllowNull()]$ArchitectureInfo,
        [AllowNull()]$NvidiaSmi
    )

    $computeCapability = Get-NvidiaSmiComputeCapability -NvidiaSmi $NvidiaSmi
    $smToken = Convert-ComputeCapabilityToCudaToken -ComputeCapability $computeCapability
    $computeToken = if ($smToken) { $smToken -replace '^sm_', 'compute_' } else { $null }
    $tokens = @()
    if ($ArchitectureInfo -and $ArchitectureInfo.tokens) {
        $tokens = @($ArchitectureInfo.tokens | ForEach-Object { [string]$_ })
    }
    $compatible = $null
    $reason = "gpu_compute_capability_unavailable"
    if ($null -eq $ArchitectureInfo -or -not [bool]$ArchitectureInfo.exists) {
        $compatible = $false
        $reason = "backend_missing"
    }
    elseif ($null -eq $smToken) {
        $compatible = $null
        $reason = "gpu_compute_capability_unavailable"
    }
    elseif (-not [bool]$ArchitectureInfo.detectionReliable) {
        $compatible = $false
        $reason = "backend_architecture_detection_unreliable"
    }
    elseif ($tokens.Count -eq 0) {
        $compatible = $false
        $reason = "backend_architecture_metadata_unavailable"
    }
    elseif (($tokens -contains $smToken) -or ($tokens -contains $computeToken)) {
        $compatible = $true
        $reason = "compatible"
    }
    else {
        $compatible = $false
        $reason = "backend_architecture_missing_target"
    }

    [pscustomobject]@{
        schema = "mlvapp.cuda-backend-compatibility.v1"
        compatible = $compatible
        reason = $reason
        gpu_compute_capability = $computeCapability
        required_tokens = @(@($smToken, $computeToken) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
        backend_tokens = @($tokens)
        proofBoundary = "This is a preflight compatibility gate only. Runtime support still requires no-readback playback proof, fallback counts, GL parity, and timing evidence on the host."
    }
}

function Invoke-ChildPowerShell {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & pwsh.exe @Arguments 2>&1
    [pscustomobject]@{
        exitCode = $LASTEXITCODE
        output = @($output | ForEach-Object { [string]$_ })
    }
}

function Get-HostLoadProofFailures {
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1: this proof's presentedFps comparison is exactly the kind
    # of cross-run fps comparison a PROVISIONAL leg must never enter -- an fps number measured on
    # a loaded host (or one whose load could not be measured) is not a property of the build.
    # Refuse the whole A/B fps proof rather than silently proving an improvement that is really
    # host noise.
    param(
        [object]$BaselineSummary,
        [object]$CandidateSummary,
        [object]$CandidateSpeedSummary,
        [switch]$SeparateCandidateSpeedRun
    )

    $failures = @()
    if ($BaselineSummary.hostLoadProvisional) {
        $failures += (
            "baseline-host-load-provisional state=$($BaselineSummary.hostLoadState) " +
            "reason=$($BaselineSummary.hostLoadReason)"
        )
    }
    if ($CandidateSummary.hostLoadProvisional) {
        $failures += (
            "candidate-host-load-provisional state=$($CandidateSummary.hostLoadState) " +
            "reason=$($CandidateSummary.hostLoadReason)"
        )
    }
    if ($SeparateCandidateSpeedRun -and $CandidateSpeedSummary.hostLoadProvisional) {
        $failures += (
            "candidate-speed-host-load-provisional state=$($CandidateSpeedSummary.hostLoadState) " +
            "reason=$($CandidateSpeedSummary.hostLoadReason)"
        )
    }
    @($failures)
}

function Get-SmokeSummaryHostLoadFields {
    # A leg recorded before PLAYBACK-MEASURE-HOST-LOAD-GATE-1 (or one whose host-load capture
    # never made it into the JSON) never MEASURED its load, which is UNKNOWN, not "quiet".
    # round 3: same for a hostLoad block that is PRESENT but missing "provisional"/"state" --
    # [bool]$null -eq $false would silently read a degenerate block as clean, exactly backwards
    # from this gate's fail-toward-provisional stance. Factored out of Read-SmokeSummary so it is
    # independently testable, mirroring compare-release-gui-smoke-ab.ps1's
    # Get-HostLoadComparisonEvidence.
    param([object]$HostLoad)

    # round 4 (sol major): "provisional" and "state" used to be derived independently, so a block
    # carrying an explicit provisional=false alongside a missing/blank state read as
    # state=unknown PROVISIONAL=false -- an inconsistent, clean-reading combination that let
    # UNKNOWN enter an fps comparison unrefused. state=unknown now always forces provisional=true.
    # round 8 (sol MAJOR item 3): that fix only special-cased state=="unknown" -- a leg with
    # state="exceeded" and an inconsistent/fabricated provisional=false still read clean. THE
    # CANONICAL PREDICATE (identical at every one of this card's six sites -- see
    # compare-machine-perf.ps1's Get-PlaybackAbLegHostLoadProvisional for the full cross-file
    # note): clean iff state=="quiet" AND provisional==false; every other combination is
    # provisional.
    $provisionalRaw = if ($null -eq $HostLoad) { $null } else { Get-NestedValue $HostLoad "provisional" }
    $provisionalDeclared = if ($null -eq $HostLoad -or $null -eq $provisionalRaw) { $true } else { [bool]$provisionalRaw }
    $stateRaw = if ($null -eq $HostLoad) { $null } else { Get-NestedValue $HostLoad "state" }
    $state = if ($null -eq $HostLoad -or [string]::IsNullOrWhiteSpace([string]$stateRaw)) { "unknown" } else { [string]$stateRaw }
    $provisional = ($provisionalDeclared -or $state -ne "quiet")
    $reason = if ($null -eq $HostLoad) {
        "no hostLoad telemetry recorded in the smoke result"
    } else {
        [string](Get-NestedValue $HostLoad "reason")
    }

    [pscustomobject]@{
        provisional = $provisional
        state = $state
        reason = $reason
    }
}

function Read-SmokeSummary {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][int]$ExitCode
    )

    $exists = Test-Path -LiteralPath $Path -PathType Leaf
    if (-not $exists) {
        return [pscustomobject]@{
            path = $Path
            exists = $false
            exitCode = $ExitCode
            validationOk = $false
            hostLoadProvisional = $true
            hostLoadState = "unknown"
            hostLoadReason = "smoke result JSON does not exist"
        }
    }

    $json = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth 100
    $hostLoad = Get-NestedValue $json "hostLoad"
    $hostLoadFields = Get-SmokeSummaryHostLoadFields -HostLoad $hostLoad
    $hostLoadProvisional = $hostLoadFields.provisional
    $hostLoadState = $hostLoadFields.state
    $hostLoadReason = $hostLoadFields.reason
    $summary = Get-NestedValue $json "log.summary"
    $glProof = Get-NestedValue $json "visualQuality.glOutputProof"
    if ($null -eq $glProof) {
        $glProof = Get-NestedValue $json "validation.glOutputProof"
    }
    $validationOk = [bool](Get-NestedValue $json "validation.ok")
    $presentedFrames = Convert-ToNullableInt64 (Get-NestedValue $summary "presented_frames")
    if ($null -eq $presentedFrames) {
        $presentedFrames = Convert-ToNullableInt64 (Get-NestedValue $json "validation.presentedFrames")
    }
    $logPath = Get-NestedValue $json "log.path"
    $gpuTextureNoReadbackFrames = $null
    $gpuTextureReadbackFrames = $null
    $noReadbackCandidateFrameCount = 0
    $activeNoReadbackFrameCount = 0
    $cudaTextureSourceFrameCount = 0
    $cudaAmazeTextureSourceFrameCount = 0
    $cudaAmazeDirectTextureSourceFrameCount = 0
    $cudaAmazeRetainedTextureSourceFrameCount = 0
    $cudaAmazeAcceptedTextureSourceFrameCount = 0
    $cpuAmazeSkippedFrameCount = 0
    $fallbackFrameCount = 0
    $noReadbackFallbackReasons = [ordered]@{}
    $textureUploadMsValues = [System.Collections.Generic.List[double]]::new()
    $textureKernelMsValues = [System.Collections.Generic.List[double]]::new()
    $textureInteropMsValues = [System.Collections.Generic.List[double]]::new()
    $textureTotalMsValues = [System.Collections.Generic.List[double]]::new()
    $textureWallMsValues = [System.Collections.Generic.List[double]]::new()
    $textureHostGapMsValues = [System.Collections.Generic.List[double]]::new()
    $textureReconWallMsValues = [System.Collections.Generic.List[double]]::new()
    $textureAmazeWallMsValues = [System.Collections.Generic.List[double]]::new()
    $texturePostMsValues = [System.Collections.Generic.List[double]]::new()
    $processStartedAtUtc = Get-NestedValue $json "process.startedAtUtc"
    $processEndedAtUtc = Get-NestedValue $json "process.endedAtUtc"

    if ($logPath -and (Test-Path -LiteralPath $logPath -PathType Leaf)) {
        $gpuFrameLines = Get-ScopedPlaybackLogLines `
            -LogPath $logPath `
            -Pattern "playback_smoke\.gpu_frame" `
            -StartedAtUtc $processStartedAtUtc `
            -EndedAtUtc $processEndedAtUtc
        foreach ($line in $gpuFrameLines) {
            $frame = Convert-PlaybackLogLineToObject -Line $line.Line
            if ([int]$frame.texture_no_readback_candidate -eq 1) {
                $noReadbackCandidateFrameCount++
            }
            if ([int]$frame.texture_no_readback_active -eq 1) {
                $activeNoReadbackFrameCount++
            }
            if ([string]$frame.texture_source -eq "cuda_gl_r16_texture") {
                $cudaTextureSourceFrameCount++
            }
            if ([string]$frame.texture_source -eq "cuda_gl_rgba16_amaze_texture") {
                $cudaAmazeTextureSourceFrameCount++
            }
            if ([string]$frame.texture_source -eq "cuda_device_bayer16_rgba16_amaze_texture") {
                $cudaAmazeDirectTextureSourceFrameCount++
            }
            if ([string]$frame.texture_source -eq "cuda_retained_device_bayer16_rgba16_amaze_texture") {
                $cudaAmazeRetainedTextureSourceFrameCount++
            }
            if ([string]$frame.texture_source -eq "cuda_gl_rgba16_amaze_texture" -or
                [string]$frame.texture_source -eq "cuda_device_bayer16_rgba16_amaze_texture" -or
                [string]$frame.texture_source -eq "cuda_retained_device_bayer16_rgba16_amaze_texture") {
                $cudaAmazeAcceptedTextureSourceFrameCount++
            }
            if ([int]$frame.cpu_amaze_skip -eq 1) {
                $cpuAmazeSkippedFrameCount++
            }
            if ([string]$frame.texture_source -match "fallback") {
                $fallbackFrameCount++
            }
            $fallbackReason = [string]$frame.texture_no_readback_fallback_reason
            if (![string]::IsNullOrWhiteSpace($fallbackReason) -and $fallbackReason -ne "none") {
                if (!$noReadbackFallbackReasons.Contains($fallbackReason)) {
                    $noReadbackFallbackReasons[$fallbackReason] = 0
                }
                $noReadbackFallbackReasons[$fallbackReason] = [int]$noReadbackFallbackReasons[$fallbackReason] + 1
            }
            Add-NullableDouble $textureUploadMsValues $frame.texture_upload_ms
            Add-NullableDouble $textureKernelMsValues $frame.texture_kernel_ms
            Add-NullableDouble $textureInteropMsValues $frame.texture_interop_ms
            Add-NullableDouble $textureTotalMsValues $frame.texture_total_ms
            Add-NullableDouble $textureWallMsValues $frame.texture_wall_ms
            Add-NullableDouble $textureHostGapMsValues $frame.texture_host_gap_ms
            Add-NullableDouble $textureReconWallMsValues $frame.texture_recon_wall_ms
            Add-NullableDouble $textureAmazeWallMsValues $frame.texture_amaze_wall_ms
            Add-NullableDouble $texturePostMsValues $frame.texture_post_ms
        }

        $gpuSummaryLine = Get-ScopedPlaybackLogLines `
            -LogPath $logPath `
            -Pattern "playback_smoke\.gpu_summary" `
            -StartedAtUtc $processStartedAtUtc `
            -EndedAtUtc $processEndedAtUtc |
            Select-Object -Last 1
        if ($gpuSummaryLine) {
            $gpuSummary = Convert-PlaybackLogLineToObject -Line $gpuSummaryLine.Line
            $gpuTextureNoReadbackFrames = Convert-ToNullableInt64 $gpuSummary.gpu_texture_no_readback_frames
            $gpuTextureReadbackFrames = Convert-ToNullableInt64 $gpuSummary.gpu_texture_readback_frames
        }
    }

    [pscustomobject]@{
        path = (Resolve-Path -LiteralPath $Path).Path
        exists = $true
        exitCode = $ExitCode
        validationOk = $validationOk
        hostLoadProvisional = $hostLoadProvisional
        hostLoadState = $hostLoadState
        hostLoadReason = $hostLoadReason
        runMetadata = Get-NestedValue $json "log.runMetadata"
        validationFailures = @(Get-NestedValue $json "validation.failures")
        validationWarnings = @(Get-NestedValue $json "validation.warnings")
        presentedFrames = $presentedFrames
        presentedFps = Convert-ToNullableDouble (Get-NestedValue $summary "presented_fps")
        timelineFps = Convert-ToNullableDouble (Get-NestedValue $summary "timeline_fps")
        guiStatusText = Get-NestedValue $json "playbackFps.guiStatusText"
        guiStatusFps = Convert-ToNullableDouble (Get-NestedValue $json "playbackFps.guiStatusValue")
        visibleGuiStatusText = Get-NestedValue $json "playbackFps.visibleBottomLeftGuiStatusText"
        visibleGuiFps = Convert-ToNullableDouble (Get-NestedValue $json "playbackFps.visibleBottomLeftGuiFps")
        avgPresentIntervalMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_present_interval_ms")
        avgRenderTotalMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_render_total_ms")
        avgRenderWorkMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_render_work_ms")
        avgQueueWaitMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_queue_wait_ms")
        avgLlrawprocMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_llrawproc_ms")
        avgProcessed8Ms = Convert-ToNullableDouble (Get-NestedValue $summary "avg_processed8_ms")
        avgDrawTotalMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_draw_total_ms")
        avgPresentUiSignalLatencyMs = Convert-ToNullableDouble (Get-NestedValue $json "log.stageSplitSummary.avg_present_ui_signal_latency_ms")
        avgPresentDrawPresentMs = Convert-ToNullableDouble (Get-NestedValue $json "log.stageSplitSummary.avg_present_draw_present_ms")
        avgPresentOverlaysScopesMs = Convert-ToNullableDouble (Get-NestedValue $json "log.stageSplitSummary.avg_present_overlays_scopes_ms")
        avgPresentRenderSlotReleaseMs = Convert-ToNullableDouble (Get-NestedValue $json "log.stageSplitSummary.avg_present_render_slot_release_ms")
        avgPresentPacingMs = Convert-ToNullableDouble (Get-NestedValue $json "log.stageSplitSummary.avg_present_pacing_ms")
        avgPrepWorkerBuildMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_playback_prep_worker_build_ms")
        avgPrepWorkerTotalMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_playback_prep_worker_total_ms")
        avgPrepBeforeFinishMs = Convert-ToNullableDouble (Get-NestedValue $summary "avg_playback_prep_total_before_finish_ms")
        gpuTextureNoReadbackFrames = $gpuTextureNoReadbackFrames
        gpuTextureReadbackFrames = $gpuTextureReadbackFrames
        noReadbackCandidateFrameCount = $noReadbackCandidateFrameCount
        activeNoReadbackFrameCount = $activeNoReadbackFrameCount
        cudaTextureSourceFrameCount = $cudaTextureSourceFrameCount
        cudaAmazeTextureSourceFrameCount = $cudaAmazeTextureSourceFrameCount
        cudaAmazeDirectTextureSourceFrameCount = $cudaAmazeDirectTextureSourceFrameCount
        cudaAmazeRetainedTextureSourceFrameCount = $cudaAmazeRetainedTextureSourceFrameCount
        cudaAmazeAcceptedTextureSourceFrameCount = $cudaAmazeAcceptedTextureSourceFrameCount
        cpuAmazeSkippedFrameCount = $cpuAmazeSkippedFrameCount
        fallbackFrameCount = $fallbackFrameCount
        noReadbackFallbackReasons = [pscustomobject]$noReadbackFallbackReasons
        avgTextureUploadMs = Get-AverageOrNull $textureUploadMsValues
        avgTextureKernelMs = Get-AverageOrNull $textureKernelMsValues
        avgTextureInteropMs = Get-AverageOrNull $textureInteropMsValues
        avgTextureTotalMs = Get-AverageOrNull $textureTotalMsValues
        avgTextureWallMs = Get-AverageOrNull $textureWallMsValues
        avgTextureHostGapMs = Get-AverageOrNull $textureHostGapMsValues
        avgTextureReconWallMs = Get-AverageOrNull $textureReconWallMsValues
        avgTextureAmazeWallMs = Get-AverageOrNull $textureAmazeWallMsValues
        avgTexturePostMs = Get-AverageOrNull $texturePostMsValues
        scaleRequestLast = Convert-ToNullableInt64 (Get-NestedValue $summary "scale_request_last")
        qualityMode = Convert-ToNullableInt64 (Get-NestedValue $summary "quality_mode")
        glProof = [pscustomobject]@{
            requested = [bool](Get-NestedValue $glProof "requested")
            activeProbeCount = Convert-ToNullableInt64 (Get-NestedValue $glProof "activeProbeCount")
            parityCheckedCount = Convert-ToNullableInt64 (Get-NestedValue $glProof "parityCheckedCount")
            parityMatchCount = Convert-ToNullableInt64 (Get-NestedValue $glProof "parityMatchCount")
            mismatchCountTotal = Convert-ToNullableInt64 (Get-NestedValue $glProof "mismatchCountTotal")
            distinctTextureHashes = Convert-ToNullableInt64 (Get-NestedValue $glProof "distinctTextureHashes")
            renderers = @(Get-NestedValue $glProof "rendererDescriptions")
            backendDescriptions = @(Get-NestedValue $glProof "backendDescriptions")
            screenshotMethod = Get-NestedValue $glProof "screenshotMethod"
        }
        autoCapability = [pscustomobject]@{
            validatedNoReadbackObserved = Get-NestedValue $json "visualQuality.autoDecision.validatedNoReadbackCapabilityObservedBool"
            validatedNoReadbackDemoted = Get-NestedValue $json "visualQuality.autoDecision.validatedNoReadbackCapabilityDemotedLastBool"
            capabilityConsistent = Get-NestedValue $json "visualQuality.autoDecision.capabilityConsistent"
        }
    }
}

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([AllowNull()][string]$Value)

    if ($null -eq $Value) {
        return "''"
    }
    "'" + ([string]$Value).Replace("'", "''") + "'"
}

function Write-SmokeInvokeScript {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$SmokeScript,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string]$Clip,
        [Parameter(Mandatory = $true)][string]$Output,
        [Parameter(Mandatory = $true)][string]$ScreenshotDir,
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$Quality,
        [Parameter(Mandatory = $true)][string]$Scale,
        [Parameter(Mandatory = $true)][string]$ThreadSetting,
        [Parameter(Mandatory = $true)][int]$SecondsValue,
        [Parameter(Mandatory = $true)][int]$SettleMsValue,
        [Parameter(Mandatory = $true)][int]$StartFrameValue,
        [Parameter(Mandatory = $true)][string]$GpuPreviewProcessing,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$GpuBackend,
        [Parameter(Mandatory = $true)][bool]$GpuAmazeTexturePresentValue,
        [Parameter(Mandatory = $true)][string[]]$ExtraEnvironment,
        [Parameter(Mandatory = $true)][bool]$CaptureScreenshot,
        [Parameter(Mandatory = $true)][bool]$FailOnColorArtifactValue,
        [Parameter(Mandatory = $true)][bool]$DetectPlaybackArtifactsValue,
        [Parameter(Mandatory = $true)][bool]$DryRunValue
    )

    $expectedScale = -1
    [void][int]::TryParse($Scale, [ref]$expectedScale)
    $expectedQuality = -1
    [void][int]::TryParse($Quality, [ref]$expectedQuality)

    $envLiteral = if ($ExtraEnvironment.Count -gt 0) {
        "@(" + (($ExtraEnvironment | ForEach-Object {
            ConvertTo-PowerShellSingleQuotedLiteral $_
        }) -join ", ") + ")"
    }
    else {
        "@()"
    }
    $gpuBackendLiteral = ConvertTo-PowerShellSingleQuotedLiteral $GpuBackend

    $scriptText = @"
`$ErrorActionPreference = 'Stop'
`$envList = $envLiteral
`$smokeParams = @{
    RepoRoot = $(ConvertTo-PowerShellSingleQuotedLiteral $RepoRoot)
    ExePath = $(ConvertTo-PowerShellSingleQuotedLiteral $Exe)
    ClipPath = $(ConvertTo-PowerShellSingleQuotedLiteral $Clip)
    Output = $(ConvertTo-PowerShellSingleQuotedLiteral $Output)
    Receipt = $(ConvertTo-PowerShellSingleQuotedLiteral $ReceiptPath)
    Seconds = $SecondsValue
    SettleMs = $SettleMsValue
    StartFrame = $StartFrameValue
    Threads = $(ConvertTo-PowerShellSingleQuotedLiteral $ThreadSetting)
    Scope = 'none'
    PlaybackDebayer = 'amaze'
    PlaybackProcessing = 'subset'
    GpuPreviewProcessing = $(ConvertTo-PowerShellSingleQuotedLiteral $GpuPreviewProcessing)
    ScaleFactor = $(ConvertTo-PowerShellSingleQuotedLiteral $Scale)
    QualityMode = $(ConvertTo-PowerShellSingleQuotedLiteral $Quality)
    ExpectedScaleRequest = $expectedScale
    ExpectedVisualScaleRequest = $expectedScale
    ExpectedQualityMode = $expectedQuality
    RequireLookAssist = `$false
    DisableLookAssist = `$true
    EnablePhase3QualityModes = `$true
    PreserveExperimentalEnvironment = `$true
    FrameTelemetry = `$true
    ExtraEnvironment = `$envList
}
if (`$$DetectPlaybackArtifactsValue) {
    `$smokeParams['DetectPlaybackArtifacts'] = `$true
}
if (-not [string]::IsNullOrWhiteSpace($gpuBackendLiteral)) {
    `$smokeParams['GpuPlaybackReconBackend'] = $gpuBackendLiteral
}
if (`$$GpuAmazeTexturePresentValue) {
    `$smokeParams['GpuAmazeTexturePresent'] = `$true
}
if (`$$CaptureScreenshot) {
    `$smokeParams['CaptureScreenshot'] = `$true
    `$smokeParams['ScreenshotOutputDir'] = $(ConvertTo-PowerShellSingleQuotedLiteral $ScreenshotDir)
}
if (`$$FailOnColorArtifactValue) {
    `$smokeParams['FailOnColorArtifact'] = `$true
}
if (`$$DryRunValue) {
    `$smokeParams['DryRun'] = `$true
}
& $(ConvertTo-PowerShellSingleQuotedLiteral $SmokeScript) @smokeParams
exit `$LASTEXITCODE
"@
    $dir = Split-Path -Parent $Path
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $scriptText | Set-Content -LiteralPath $Path -Encoding UTF8

    [pscustomobject]@{
        invokeScript = $Path
        output = $Output
        screenshotDir = if ($CaptureScreenshot) { $ScreenshotDir } else { $null }
        gpuPreviewProcessing = $GpuPreviewProcessing
        gpuPlaybackReconBackend = $GpuBackend
        extraEnvironment = @($ExtraEnvironment)
        dryRun = $DryRunValue
    }
}

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = Join-Path $root "platform\qt\build-release\release\MLVApp.exe"
}
else {
    $ExePath = Resolve-RepoPath -Root $root -Path $ExePath
}
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$exeDir = Split-Path -Parent $exe
$gpuPreferenceBefore = Get-GpuPreferenceValue -Executable $exe
$gpuPreferenceAfter = $gpuPreferenceBefore
if (!$DryRun -and !$NoHighPerformancePreference) {
    Set-GpuPreferenceHighPerformance -Executable $exe
    $gpuPreferenceAfter = Get-GpuPreferenceValue -Executable $exe
}

$smokeScript = Join-Path $root "tools\profiling\run-release-gui-smoke.ps1"
if (-not (Test-Path -LiteralPath $smokeScript -PathType Leaf)) {
    throw "Missing GUI smoke wrapper: $smokeScript"
}

$backendDir = Join-Path $root "tools\gpu\backend"
$backendBuildScript = Join-Path $backendDir "build-backend-dll.ps1"
$backendBuildResult = [ordered]@{
    skipped = [bool]$SkipBackendBuild
    attempted = $false
    buildScript = $backendBuildScript
    exitCode = $null
    output = @()
}
$backendDllExplicit = $PSBoundParameters.ContainsKey("BackendDll") -and
    -not [string]::IsNullOrWhiteSpace($BackendDll)
$releaseBackendDll = Join-Path $exeDir "igpu_recon_cuda.dll"
$releaseBackendArchSidecar = Join-Path $exeDir "igpu_recon_cuda.arch.json"
if ([string]::IsNullOrWhiteSpace($BackendDll)) {
    $BackendDll = $releaseBackendDll
    $toolBackendDll = Join-Path $backendDir "igpu_recon_cuda.dll"
    $toolBackendArchSidecar = Join-Path $backendDir "igpu_recon_cuda.arch.json"
    if (!$DryRun -and !$SkipBackendBuild) {
        if (Test-Path -LiteralPath $backendBuildScript -PathType Leaf) {
            $backendBuildResult.attempted = $true
            $buildResult = Invoke-ChildPowerShell -Arguments @(
                "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", $backendBuildScript,
                "-Dir", $backendDir,
                "-Arch", "portable"
            )
            $backendBuildResult.exitCode = $buildResult.exitCode
            $backendBuildResult.output = @($buildResult.output)
        }
    }
    if (!$DryRun -and (Test-Path -LiteralPath $toolBackendDll -PathType Leaf)) {
        Copy-Item -LiteralPath $toolBackendDll -Destination $releaseBackendDll -Force
        if (Test-Path -LiteralPath $toolBackendArchSidecar -PathType Leaf) {
            Copy-Item -LiteralPath $toolBackendArchSidecar -Destination $releaseBackendArchSidecar -Force
        }
        $cudaRoot = Get-ChildItem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            Select-Object -First 1
        if ($cudaRoot) {
            $cudart = Get-ChildItem -LiteralPath (Join-Path $cudaRoot.FullName "bin") -Filter "cudart64_*.dll" -File -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                Select-Object -First 1
            if ($cudart) {
                Copy-Item -LiteralPath $cudart.FullName -Destination (Join-Path $exeDir $cudart.Name) -Force
            }
        }
    }
}
else {
    $BackendDll = Resolve-RepoPath -Root $root -Path $BackendDll
}
$backend = (Resolve-Path -LiteralPath $BackendDll).Path
$amazeBackendBuildScript = Join-Path $backendDir "amaze-debayer-dll.ps1"
$amazeBackendBuildResult = [ordered]@{
    skipped = [bool]$SkipAmazeBackendBuild
    attempted = $false
    buildScript = $amazeBackendBuildScript
    exitCode = $null
    output = @()
}
if ([string]::IsNullOrWhiteSpace($AmazeBackendDll)) {
    $releaseAmazeBackendDll = Join-Path $exeDir "igpu_amaze_debayer_cuda.dll"
    $toolAmazeBackendDll = Join-Path $backendDir "igpu_amaze_debayer_cuda.dll"
    if (Test-Path -LiteralPath $releaseAmazeBackendDll -PathType Leaf) {
        $AmazeBackendDll = $releaseAmazeBackendDll
    }
    elseif (Test-Path -LiteralPath $toolAmazeBackendDll -PathType Leaf) {
        $AmazeBackendDll = $toolAmazeBackendDll
    }
    else {
        $AmazeBackendDll = $toolAmazeBackendDll
    }
}
else {
    $AmazeBackendDll = Resolve-RepoPath -Root $root -Path $AmazeBackendDll
}
if (!$DryRun -and
    !(Test-Path -LiteralPath $AmazeBackendDll -PathType Leaf) -and
    !$SkipAmazeBackendBuild) {
    if (Test-Path -LiteralPath $amazeBackendBuildScript -PathType Leaf) {
        $amazeBackendBuildResult.attempted = $true
        $buildResult = Invoke-ChildPowerShell -Arguments @(
            "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", $amazeBackendBuildScript,
            "-Dir", $backendDir,
            "-Arch", "portable"
        )
        $amazeBackendBuildResult.exitCode = $buildResult.exitCode
        $amazeBackendBuildResult.output = @($buildResult.output)
    }
}
$amazeBackendSource = if (Test-Path -LiteralPath $AmazeBackendDll -PathType Leaf) {
    (Resolve-Path -LiteralPath $AmazeBackendDll).Path
} else {
    $AmazeBackendDll
}
$amazeBackendLaunchPath = Join-Path $exeDir "igpu_amaze_debayer_cuda.dll"
if (!$DryRun -and (Test-Path -LiteralPath $amazeBackendSource -PathType Leaf)) {
    if ($amazeBackendSource -ne $amazeBackendLaunchPath) {
        Copy-Item -LiteralPath $amazeBackendSource -Destination $amazeBackendLaunchPath -Force
    }
}
if (Test-Path -LiteralPath $amazeBackendLaunchPath -PathType Leaf) {
    $amazeBackendLaunchPath = (Resolve-Path -LiteralPath $amazeBackendLaunchPath).Path
}
$nvidiaSmi = Get-NvidiaSmiSnapshot
$machineFingerprint = Get-LocalMachineFingerprint -RepoRoot $root -NvidiaSmi $nvidiaSmi
$backendArchitecture = Get-CudaBackendArchitectureInfo -Path $backend
$backendCompatibility = Test-CudaBackendCompatibility `
    -ArchitectureInfo $backendArchitecture `
    -NvidiaSmi $nvidiaSmi
$preflightFailures = @()
if (!$DryRun -and [bool]$backendBuildResult.attempted -and
    $backendBuildResult.exitCode -ne 0) {
    $preflightFailures += "CUDA recon backend DLL build failed with exit code $($backendBuildResult.exitCode)."
}
if (!$DryRun -and [bool]$nvidiaSmi.found -and $backendCompatibility.compatible -ne $true) {
    $preflightFailures += ("CUDA backend architecture is not compatible with this GPU: compute_capability={0}; backend_tokens=[{1}]; reason={2}. Rebuild/deploy igpu_recon_cuda.dll with tools\\gpu\\backend\\build-backend-dll.ps1 -Arch portable before using this run for Dell/UltraMagnus claims." -f `
        $backendCompatibility.gpu_compute_capability,
        (($backendCompatibility.backend_tokens | ForEach-Object { [string]$_ }) -join ","),
        $backendCompatibility.reason)
}
if (!$DryRun -and [bool]$amazeBackendBuildResult.attempted -and
    $amazeBackendBuildResult.exitCode -ne 0) {
    $preflightFailures += "AMaZE backend DLL build failed with exit code $($amazeBackendBuildResult.exitCode)."
}
if (!$DryRun -and !(Test-Path -LiteralPath $amazeBackendLaunchPath -PathType Leaf)) {
    $preflightFailures += "AMaZE backend DLL is missing for the live texture path: $amazeBackendLaunchPath."
}

$clip = (Resolve-Path -LiteralPath (Resolve-RepoPath -Root $root -Path $ClipPath)).Path
$receiptPath = (Resolve-Path -LiteralPath (Resolve-RepoPath -Root $root -Path $Receipt)).Path

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $OutputRoot = Join-Path $root ".claude-state\profiling\$stamp-cuda-playback-ab"
}
$runRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    (Resolve-RepoPath -Root $root -Path $OutputRoot))
$summaryPath = Join-Path $runRoot "summary.json"
$latestPath = Join-Path $root ".claude-state\profiling\cuda-playback-ab-latest.json"
$latestParent = Split-Path -Parent $latestPath
if ($latestParent) {
    New-Item -ItemType Directory -Force -Path $latestParent | Out-Null
}
$baselineOutput = Join-Path $runRoot "baseline-cpu.json"
$candidateOutput = Join-Path $runRoot "candidate-cuda.json"
$candidateSpeedOutput = Join-Path $runRoot "candidate-cuda-speed.json"
$baselineScreenshots = Join-Path $runRoot "baseline-screenshots"
$candidateScreenshots = Join-Path $runRoot "candidate-screenshots"
$candidateSpeedScreenshots = Join-Path $runRoot "candidate-speed-screenshots"
$baselineInvokeScript = Join-Path $runRoot "invoke-baseline.ps1"
$candidateInvokeScript = Join-Path $runRoot "invoke-candidate.ps1"
$candidateSpeedInvokeScript = Join-Path $runRoot "invoke-candidate-speed.ps1"
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$captureScreenshot = -not [bool]$NoScreenshot
$baselineEnv = @(
    "QT_OPENGL=desktop"
)
$candidateEnv = @(
    "QT_OPENGL=desktop",
    "MLVAPP_GPU_PLAYBACK_RECON=1",
    "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1",
    "MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT=1",
    "MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=1",
    "MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT_SAMPLE_EVERY=$ValidationSampleEvery"
)
if ($backendDllExplicit) {
    $candidateEnv += "MLVAPP_GPU_PLAYBACK_RECON_DLL=$backend"
}
if ($CudaAmazeLiveTileStreams -gt 0) {
    $candidateEnv += "MLVAPP_CUDA_AMAZE_LIVE_TILE_STREAMS=$CudaAmazeLiveTileStreams"
}
if ($CudaAmazeFastLaunchChecks) {
    $candidateEnv += "MLVAPP_CUDA_AMAZE_LIVE_FAST_LAUNCH_CHECKS=1"
}
if ($CudaAmazeLiveDirectRgbaStore) {
    $candidateEnv += "MLVAPP_CUDA_AMAZE_LIVE_DIRECT_RGBA_STORE=1"
}
if ($GpuPlaybackReconRetainDeviceOutput) {
    $candidateEnv += "MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT=1"
}
if ($GpuTexNrAcquireLatestReady) {
    $candidateEnv += "MLVAPP_GPU_TEX_NR_ACQUIRE_LATEST_READY=1"
}
if ($GpuTexNrImmediateDrainReady) {
    $candidateEnv += "MLVAPP_GPU_TEX_NR_IMMEDIATE_DRAIN_READY=1"
}
if ($GpuTexNrImmediateDrainMax -gt 0) {
    $candidateEnv += "MLVAPP_GPU_TEX_NR_IMMEDIATE_DRAIN_MAX=$GpuTexNrImmediateDrainMax"
}
if ($Phase3FrameSlots -gt 0) {
    $candidateEnv += "MLVAPP_PHASE3_FRAME_SLOT_COUNT=$Phase3FrameSlots"
}
if ($PlaybackTimerPollMs -gt 0) {
    $candidateEnv += "MLVAPP_PLAYBACK_TIMER_POLL_MS=$PlaybackTimerPollMs"
}
if ($PlaybackRenderLookaheadFrames -gt 0) {
    $candidateEnv += "MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=$PlaybackRenderLookaheadFrames"
}
$candidateSpeedEnv = @(
    "QT_OPENGL=desktop",
    "MLVAPP_GPU_PLAYBACK_RECON=1",
    "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1",
    "MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT=1",
    "MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=0"
)
if ($backendDllExplicit) {
    $candidateSpeedEnv += "MLVAPP_GPU_PLAYBACK_RECON_DLL=$backend"
}
if ($CudaAmazeLiveTileStreams -gt 0) {
    $candidateSpeedEnv += "MLVAPP_CUDA_AMAZE_LIVE_TILE_STREAMS=$CudaAmazeLiveTileStreams"
}
if ($CudaAmazeFastLaunchChecks) {
    $candidateSpeedEnv += "MLVAPP_CUDA_AMAZE_LIVE_FAST_LAUNCH_CHECKS=1"
}
if ($CudaAmazeLiveDirectRgbaStore) {
    $candidateSpeedEnv += "MLVAPP_CUDA_AMAZE_LIVE_DIRECT_RGBA_STORE=1"
}
if ($GpuPlaybackReconRetainDeviceOutput) {
    $candidateSpeedEnv += "MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT=1"
}
if ($GpuTexNrAcquireLatestReady) {
    $candidateSpeedEnv += "MLVAPP_GPU_TEX_NR_ACQUIRE_LATEST_READY=1"
}
if ($GpuTexNrImmediateDrainReady) {
    $candidateSpeedEnv += "MLVAPP_GPU_TEX_NR_IMMEDIATE_DRAIN_READY=1"
}
if ($GpuTexNrImmediateDrainMax -gt 0) {
    $candidateSpeedEnv += "MLVAPP_GPU_TEX_NR_IMMEDIATE_DRAIN_MAX=$GpuTexNrImmediateDrainMax"
}
if ($Phase3FrameSlots -gt 0) {
    $candidateSpeedEnv += "MLVAPP_PHASE3_FRAME_SLOT_COUNT=$Phase3FrameSlots"
}
if ($PlaybackTimerPollMs -gt 0) {
    $candidateSpeedEnv += "MLVAPP_PLAYBACK_TIMER_POLL_MS=$PlaybackTimerPollMs"
}
if ($PlaybackRenderLookaheadFrames -gt 0) {
    $candidateSpeedEnv += "MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=$PlaybackRenderLookaheadFrames"
}

$baselineCommand = Write-SmokeInvokeScript `
    -Path $baselineInvokeScript `
    -SmokeScript $smokeScript `
    -RepoRoot $root `
    -Exe $exe `
    -Clip $clip `
    -Output $baselineOutput `
    -ScreenshotDir $baselineScreenshots `
    -ReceiptPath $receiptPath `
    -Quality $QualityMode `
    -Scale $ScaleFactor `
    -ThreadSetting $Threads `
    -SecondsValue $Seconds `
    -SettleMsValue $SettleMs `
    -StartFrameValue $StartFrame `
    -GpuPreviewProcessing "cpu" `
    -GpuBackend "" `
    -GpuAmazeTexturePresentValue $false `
    -ExtraEnvironment $baselineEnv `
    -CaptureScreenshot $captureScreenshot `
    -FailOnColorArtifactValue ([bool]$FailOnColorArtifact) `
    -DetectPlaybackArtifactsValue $true `
    -DryRunValue ([bool]$DryRun)

$candidateCommand = Write-SmokeInvokeScript `
    -Path $candidateInvokeScript `
    -SmokeScript $smokeScript `
    -RepoRoot $root `
    -Exe $exe `
    -Clip $clip `
    -Output $candidateOutput `
    -ScreenshotDir $candidateScreenshots `
    -ReceiptPath $receiptPath `
    -Quality $QualityMode `
    -Scale $ScaleFactor `
    -ThreadSetting $Threads `
    -SecondsValue $Seconds `
    -SettleMsValue $SettleMs `
    -StartFrameValue $StartFrame `
    -GpuPreviewProcessing "gpu" `
    -GpuBackend $GpuPlaybackReconBackend `
    -GpuAmazeTexturePresentValue $true `
    -ExtraEnvironment $candidateEnv `
    -CaptureScreenshot $captureScreenshot `
    -FailOnColorArtifactValue ([bool]$FailOnColorArtifact) `
    -DetectPlaybackArtifactsValue (-not [bool]$SeparateCandidateSpeedRun) `
    -DryRunValue ([bool]$DryRun)

$candidateSpeedCommand = $null
if ($SeparateCandidateSpeedRun) {
    $candidateSpeedCommand = Write-SmokeInvokeScript `
        -Path $candidateSpeedInvokeScript `
        -SmokeScript $smokeScript `
        -RepoRoot $root `
        -Exe $exe `
        -Clip $clip `
        -Output $candidateSpeedOutput `
        -ScreenshotDir $candidateSpeedScreenshots `
        -ReceiptPath $receiptPath `
        -Quality $QualityMode `
        -Scale $ScaleFactor `
        -ThreadSetting $Threads `
        -SecondsValue $Seconds `
        -SettleMsValue $SettleMs `
        -StartFrameValue $StartFrame `
        -GpuPreviewProcessing "gpu" `
        -GpuBackend $GpuPlaybackReconBackend `
        -GpuAmazeTexturePresentValue $true `
        -ExtraEnvironment $candidateSpeedEnv `
        -CaptureScreenshot $captureScreenshot `
        -FailOnColorArtifactValue ([bool]$FailOnColorArtifact) `
        -DetectPlaybackArtifactsValue $true `
        -DryRunValue ([bool]$DryRun)
}

$summary = [ordered]@{
    schema = "mlvapp-cuda-playback-ab.v1"
    status = if ($DryRun) { "planned" } else { "starting" }
    createdAt = (Get-Date).ToString("o")
    host = $env:COMPUTERNAME
    repoRoot = $root
    exe = Get-FileArtifact -Path $exe
    backend = Get-FileArtifact -Path $backend
    backendBuild = [pscustomobject]$backendBuildResult
    amazeBackend = Get-FileArtifact -Path $amazeBackendLaunchPath
    amazeBackendSource = Get-FileArtifact -Path $amazeBackendSource
    amazeBackendBuild = [pscustomobject]$amazeBackendBuildResult
    backendArchitecture = $backendArchitecture
    backendCompatibility = $backendCompatibility
    cudart = Get-FileArtifact -Path (Join-Path $exeDir "cudart64_12.dll")
    clipPath = $clip
    receiptPath = $receiptPath
    seconds = $Seconds
    settleMs = $SettleMs
    qualityMode = $QualityMode
    scaleFactor = $ScaleFactor
    validationSampleEvery = $ValidationSampleEvery
    cudaAmazeLiveTileStreams = if ($CudaAmazeLiveTileStreams -gt 0) { $CudaAmazeLiveTileStreams } else { $null }
    cudaAmazeFastLaunchChecks = [bool]$CudaAmazeFastLaunchChecks
    cudaAmazeLiveDirectRgbaStore = [bool]$CudaAmazeLiveDirectRgbaStore
    gpuPlaybackReconRetainDeviceOutput = [bool]$GpuPlaybackReconRetainDeviceOutput
    gpuTexNrAcquireLatestReady = [bool]$GpuTexNrAcquireLatestReady
    gpuTexNrImmediateDrainReady = [bool]$GpuTexNrImmediateDrainReady
    gpuTexNrImmediateDrainMax = if ($GpuTexNrImmediateDrainMax -gt 0) { $GpuTexNrImmediateDrainMax } else { $null }
    phase3FrameSlots = if ($Phase3FrameSlots -gt 0) { $Phase3FrameSlots } else { $null }
    playbackTimerPollMs = if ($PlaybackTimerPollMs -gt 0) { $PlaybackTimerPollMs } else { $null }
    playbackRenderLookaheadFrames = if ($PlaybackRenderLookaheadFrames -gt 0) { $PlaybackRenderLookaheadFrames } else { $null }
    separateCandidateSpeedRun = [bool]$SeparateCandidateSpeedRun
    allowLegacyR16TextureCandidate = [bool]$AllowLegacyR16TextureCandidate
    runOrder = if ($CandidateFirst) {
        if ($SeparateCandidateSpeedRun) { @("candidate", "candidate_speed", "baseline") } else { @("candidate", "baseline") }
    } else {
        if ($SeparateCandidateSpeedRun) { @("baseline", "candidate", "candidate_speed") } else { @("baseline", "candidate") }
    }
    machineFingerprint = $machineFingerprint
    nvidiaSmi = $nvidiaSmi
    preflightFailures = @($preflightFailures)
    gpuPreference = [pscustomobject]@{
        attemptedHighPerformance = [bool](!$NoHighPerformancePreference)
        mutated = [bool](!$DryRun -and !$NoHighPerformancePreference)
        before = $gpuPreferenceBefore
        after = $gpuPreferenceAfter
        expected = "GpuPreference=2;"
    }
    commands = [pscustomobject]@{
        baseline = $baselineCommand
        candidate = $candidateCommand
        candidateSpeed = $candidateSpeedCommand
    }
    outputs = [pscustomobject]@{
        baseline = $baselineOutput
        candidate = $candidateOutput
        candidateSpeed = if ($SeparateCandidateSpeedRun) { $candidateSpeedOutput } else { $null }
        summary = $summaryPath
    }
    proofBoundary = [pscustomobject]@{
        sameExecutableCpuVsCudaPlayback = $true
        hostLocalOnly = $true
        provesRealtimePlayback = $false
        provesDellSupport = $false
        notes = @(
            "This wrapper compares CPU baseline and scoped CUDA no-readback candidate on the same release executable.",
            "By default the CUDA candidate must use the direct-device CUDA AMaZE texture source; pass -AllowLegacyR16TextureCandidate only for legacy bridge investigations.",
            "When separateCandidateSpeedRun is true, candidate proves GL parity without the temporal detector and candidateSpeed measures cadence without heavy GL-vs-oracle readback.",
            "On Windows hybrid-GPU laptops, the wrapper sets the per-app high-performance GPU preference before real runs unless -NoHighPerformancePreference is passed.",
            "A speed gain is quotable only for the host, clip, settings, and run duration captured in this summary.",
            "UltraMagnus RTX 4090 results do not prove Dell laptop support; run this wrapper on the Dell laptop for that claim."
        )
    }
}
Write-JsonFile -Value ([pscustomobject]$summary) -Path $summaryPath
Copy-Item -LiteralPath $summaryPath -Destination $latestPath -Force

if ($DryRun) {
    Write-Output $summaryPath
    return
}
if ($preflightFailures.Count -gt 0) {
    $summary["status"] = "failed"
    $summary["proofFailures"] = @($preflightFailures)
    Write-JsonFile -Value ([pscustomobject]$summary) -Path $summaryPath
    Copy-Item -LiteralPath $summaryPath -Destination $latestPath -Force
    throw ($preflightFailures -join "; ")
}

$baselineResult = $null
$candidateResult = $null
$candidateSpeedResult = $null
$steps = if ($CandidateFirst) {
    $items = @(
        [pscustomobject]@{ label = "candidate"; invokeScript = $candidateCommand.invokeScript },
        [pscustomobject]@{ label = "baseline"; invokeScript = $baselineCommand.invokeScript }
    )
    if ($SeparateCandidateSpeedRun) {
        $items = @(
            [pscustomobject]@{ label = "candidate"; invokeScript = $candidateCommand.invokeScript },
            [pscustomobject]@{ label = "candidate_speed"; invokeScript = $candidateSpeedCommand.invokeScript },
            [pscustomobject]@{ label = "baseline"; invokeScript = $baselineCommand.invokeScript }
        )
    }
    $items
}
else {
    $items = @(
        [pscustomobject]@{ label = "baseline"; invokeScript = $baselineCommand.invokeScript },
        [pscustomobject]@{ label = "candidate"; invokeScript = $candidateCommand.invokeScript }
    )
    if ($SeparateCandidateSpeedRun) {
        $items = @(
            [pscustomobject]@{ label = "baseline"; invokeScript = $baselineCommand.invokeScript },
            [pscustomobject]@{ label = "candidate"; invokeScript = $candidateCommand.invokeScript },
            [pscustomobject]@{ label = "candidate_speed"; invokeScript = $candidateSpeedCommand.invokeScript }
        )
    }
    $items
}

foreach ($step in $steps) {
    $result = Invoke-ChildPowerShell -Arguments @(
        "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", $step.invokeScript
    )
    if ($step.label -eq "baseline") {
        $baselineResult = $result
    }
    elseif ($step.label -eq "candidate") {
        $candidateResult = $result
    }
    else {
        $candidateSpeedResult = $result
    }
}

$baselineSummary = Read-SmokeSummary -Path $baselineOutput -ExitCode $baselineResult.exitCode
$candidateSummary = Read-SmokeSummary -Path $candidateOutput -ExitCode $candidateResult.exitCode
$candidateSpeedSummary = if ($SeparateCandidateSpeedRun) {
    Read-SmokeSummary -Path $candidateSpeedOutput -ExitCode $candidateSpeedResult.exitCode
} else {
    $null
}

$observedBuildSha = Get-NestedValue $candidateSummary "runMetadata.build_sha"
if ([string]::IsNullOrWhiteSpace([string]$observedBuildSha)) {
    $observedBuildSha = Get-NestedValue $baselineSummary "runMetadata.build_sha"
}
$missingFingerprintBuildSha =
    [string]::IsNullOrWhiteSpace([string]$summary["machineFingerprint"].build_sha)
if (-not [string]::IsNullOrWhiteSpace([string]$observedBuildSha) -and
    $missingFingerprintBuildSha) {
    $summary["machineFingerprint"].build_sha = [string]$observedBuildSha
}

$proofFailures = @()
if ($baselineResult.exitCode -ne 0) {
    $proofFailures += "baseline-smoke-exit-code=$($baselineResult.exitCode)"
}
if ($candidateResult.exitCode -ne 0) {
    $proofFailures += "candidate-smoke-exit-code=$($candidateResult.exitCode)"
}
if ($SeparateCandidateSpeedRun -and $candidateSpeedResult.exitCode -ne 0) {
    $proofFailures += "candidate-speed-smoke-exit-code=$($candidateSpeedResult.exitCode)"
}
if (-not $NoHighPerformancePreference -and $gpuPreferenceAfter -ne "GpuPreference=2;") {
    $proofFailures += "windows-high-performance-gpu-preference-not-set actual=$gpuPreferenceAfter"
}
if (-not $baselineSummary.validationOk) {
    $proofFailures += "baseline-validation-not-ok"
}
if (-not $candidateSummary.validationOk) {
    $proofFailures += "candidate-validation-not-ok"
}
if ($SeparateCandidateSpeedRun -and -not $candidateSpeedSummary.validationOk) {
    $proofFailures += "candidate-speed-validation-not-ok"
}
$proofFailures += @(Get-HostLoadProofFailures `
    -BaselineSummary $baselineSummary `
    -CandidateSummary $candidateSummary `
    -CandidateSpeedSummary $candidateSpeedSummary `
    -SeparateCandidateSpeedRun:$SeparateCandidateSpeedRun)
$candidateTextureProofSummaries = @(
    [pscustomobject]@{ label = "candidate"; summary = $candidateSummary }
)
if ($SeparateCandidateSpeedRun) {
    $candidateTextureProofSummaries += [pscustomobject]@{
        label = "candidate-speed"
        summary = $candidateSpeedSummary
    }
}
foreach ($item in $candidateTextureProofSummaries) {
    $label = [string]$item.label
    $candidateTextureSummary = $item.summary
    $noReadbackFrames = Convert-ToNullableInt64 $candidateTextureSummary.gpuTextureNoReadbackFrames
    $activeNoReadbackFrames = Convert-ToNullableInt64 $candidateTextureSummary.activeNoReadbackFrameCount
    $directAmazeFrames = Convert-ToNullableInt64 $candidateTextureSummary.cudaAmazeDirectTextureSourceFrameCount
    $retainedAmazeFrames = Convert-ToNullableInt64 $candidateTextureSummary.cudaAmazeRetainedTextureSourceFrameCount
    $acceptedAmazeFrames = Convert-ToNullableInt64 $candidateTextureSummary.cudaAmazeAcceptedTextureSourceFrameCount
    $legacyR16Frames = Convert-ToNullableInt64 $candidateTextureSummary.cudaTextureSourceFrameCount
    $fallbackFrames = Convert-ToNullableInt64 $candidateTextureSummary.fallbackFrameCount
    $cpuAmazeSkippedFrames = Convert-ToNullableInt64 $candidateTextureSummary.cpuAmazeSkippedFrameCount

    if ($null -eq $noReadbackFrames -or $noReadbackFrames -le 0) {
        $proofFailures += "$label-gpu-texture-no-readback-frames=$noReadbackFrames"
    }
    if ($null -eq $activeNoReadbackFrames -or $activeNoReadbackFrames -le 0) {
        $proofFailures += "$label-active-no-readback-frames=$activeNoReadbackFrames"
    }
    if ($null -ne $fallbackFrames -and $fallbackFrames -gt 0) {
        $proofFailures += "$label-fallback-frames=$fallbackFrames"
    }
    if (-not $AllowLegacyR16TextureCandidate) {
        if ($null -eq $acceptedAmazeFrames -or $acceptedAmazeFrames -le 0) {
            $proofFailures += "$label-accepted-amaze-texture-frames=$acceptedAmazeFrames direct=$directAmazeFrames retained=$retainedAmazeFrames"
        }
        if ($null -ne $legacyR16Frames -and $legacyR16Frames -gt 0) {
            $proofFailures += "$label-legacy-r16-texture-frames=$legacyR16Frames"
        }
        if ($null -eq $cpuAmazeSkippedFrames -or $cpuAmazeSkippedFrames -le 0) {
            $proofFailures += "$label-cpu-amaze-skipped-frames=$cpuAmazeSkippedFrames"
        }
        if ($null -ne $activeNoReadbackFrames -and $activeNoReadbackFrames -gt 0 -and
            ($null -eq $acceptedAmazeFrames -or $acceptedAmazeFrames -ne $activeNoReadbackFrames)) {
            $proofFailures += "$label-accepted-amaze-texture-frames=$acceptedAmazeFrames/$activeNoReadbackFrames"
        }
    }
}
if ($RequireCandidateGlParity) {
    if (-not $candidateSummary.glProof.requested) {
        $proofFailures += "candidate-gl-parity-not-requested"
    }
    if ($candidateSummary.glProof.activeProbeCount -lt 1) {
        $proofFailures += "candidate-gl-active-probe-count=$($candidateSummary.glProof.activeProbeCount)"
    }
    if ($candidateSummary.glProof.parityCheckedCount -lt 1) {
        $proofFailures += "candidate-gl-parity-checked-count=$($candidateSummary.glProof.parityCheckedCount)"
    }
    if ($candidateSummary.glProof.mismatchCountTotal -ne 0) {
        $proofFailures += "candidate-gl-parity-mismatches=$($candidateSummary.glProof.mismatchCountTotal)"
    }
}
if ($RequireCandidateImprovesPresentedFps) {
    $candidateForSpeed = if ($SeparateCandidateSpeedRun) { $candidateSpeedSummary } else { $candidateSummary }
    # round 4 (sol major): this requirement used to compare presentedFps unconditionally, even
    # when Get-HostLoadProofFailures (above) had already flagged one of these exact legs as
    # host-load provisional -- the overall proof still failed either way, but this check ALSO
    # emitted a "candidate-presented-fps-not-improved" line computed from noisy data, which
    # misrepresents a host-load problem as an fps regression. Refuse the fps comparison itself
    # when either leg is provisional instead of computing it from untrustworthy numbers.
    if ($baselineSummary.hostLoadProvisional -or $candidateForSpeed.hostLoadProvisional) {
        $proofFailures += (
            "candidate-presented-fps-improvement-not-evaluated host-load-provisional " +
            "baseline=$($baselineSummary.hostLoadProvisional) candidate=$($candidateForSpeed.hostLoadProvisional)"
        )
    }
    else {
        $baselineFps = Convert-ToNullableDouble $baselineSummary.presentedFps
        $candidateFps = Convert-ToNullableDouble $candidateForSpeed.presentedFps
        if ($null -eq $baselineFps -or $null -eq $candidateFps -or $candidateFps -le $baselineFps) {
            $proofFailures += "candidate-presented-fps-not-improved baseline=$baselineFps candidate=$candidateFps"
        }
    }
}

function New-PlaybackCompare {
    param(
        [object]$BaselineSummary,
        [object]$CandidateSummary
    )

    [pscustomobject]@{
        presentedFps = New-MetricDelta -BaselineValue $BaselineSummary.presentedFps -CandidateValue $CandidateSummary.presentedFps
        timelineFps = New-MetricDelta -BaselineValue $BaselineSummary.timelineFps -CandidateValue $CandidateSummary.timelineFps
        guiStatusFps = New-MetricDelta -BaselineValue $BaselineSummary.guiStatusFps -CandidateValue $CandidateSummary.guiStatusFps
        visibleGuiFps = New-MetricDelta -BaselineValue $BaselineSummary.visibleGuiFps -CandidateValue $CandidateSummary.visibleGuiFps
        avgPresentIntervalMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPresentIntervalMs -CandidateValue $CandidateSummary.avgPresentIntervalMs
        avgRenderTotalMs = New-MetricDelta -BaselineValue $BaselineSummary.avgRenderTotalMs -CandidateValue $CandidateSummary.avgRenderTotalMs
        avgRenderWorkMs = New-MetricDelta -BaselineValue $BaselineSummary.avgRenderWorkMs -CandidateValue $CandidateSummary.avgRenderWorkMs
        avgQueueWaitMs = New-MetricDelta -BaselineValue $BaselineSummary.avgQueueWaitMs -CandidateValue $CandidateSummary.avgQueueWaitMs
        avgLlrawprocMs = New-MetricDelta -BaselineValue $BaselineSummary.avgLlrawprocMs -CandidateValue $CandidateSummary.avgLlrawprocMs
        avgProcessed8Ms = New-MetricDelta -BaselineValue $BaselineSummary.avgProcessed8Ms -CandidateValue $CandidateSummary.avgProcessed8Ms
        avgDrawTotalMs = New-MetricDelta -BaselineValue $BaselineSummary.avgDrawTotalMs -CandidateValue $CandidateSummary.avgDrawTotalMs
        avgPresentUiSignalLatencyMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPresentUiSignalLatencyMs -CandidateValue $CandidateSummary.avgPresentUiSignalLatencyMs
        avgPresentDrawPresentMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPresentDrawPresentMs -CandidateValue $CandidateSummary.avgPresentDrawPresentMs
        avgPresentOverlaysScopesMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPresentOverlaysScopesMs -CandidateValue $CandidateSummary.avgPresentOverlaysScopesMs
        avgPresentRenderSlotReleaseMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPresentRenderSlotReleaseMs -CandidateValue $CandidateSummary.avgPresentRenderSlotReleaseMs
        avgPresentPacingMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPresentPacingMs -CandidateValue $CandidateSummary.avgPresentPacingMs
        avgPrepWorkerBuildMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPrepWorkerBuildMs -CandidateValue $CandidateSummary.avgPrepWorkerBuildMs
        avgPrepWorkerTotalMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPrepWorkerTotalMs -CandidateValue $CandidateSummary.avgPrepWorkerTotalMs
        avgPrepBeforeFinishMs = New-MetricDelta -BaselineValue $BaselineSummary.avgPrepBeforeFinishMs -CandidateValue $CandidateSummary.avgPrepBeforeFinishMs
        gpuTextureNoReadbackFrames = New-MetricDelta -BaselineValue $BaselineSummary.gpuTextureNoReadbackFrames -CandidateValue $CandidateSummary.gpuTextureNoReadbackFrames
        activeNoReadbackFrameCount = New-MetricDelta -BaselineValue $BaselineSummary.activeNoReadbackFrameCount -CandidateValue $CandidateSummary.activeNoReadbackFrameCount
        cudaAmazeDirectTextureSourceFrameCount = New-MetricDelta -BaselineValue $BaselineSummary.cudaAmazeDirectTextureSourceFrameCount -CandidateValue $CandidateSummary.cudaAmazeDirectTextureSourceFrameCount
        cudaAmazeRetainedTextureSourceFrameCount = New-MetricDelta -BaselineValue $BaselineSummary.cudaAmazeRetainedTextureSourceFrameCount -CandidateValue $CandidateSummary.cudaAmazeRetainedTextureSourceFrameCount
        cpuAmazeSkippedFrameCount = New-MetricDelta -BaselineValue $BaselineSummary.cpuAmazeSkippedFrameCount -CandidateValue $CandidateSummary.cpuAmazeSkippedFrameCount
        fallbackFrameCount = New-MetricDelta -BaselineValue $BaselineSummary.fallbackFrameCount -CandidateValue $CandidateSummary.fallbackFrameCount
        avgTextureTotalMs = New-MetricDelta -BaselineValue $BaselineSummary.avgTextureTotalMs -CandidateValue $CandidateSummary.avgTextureTotalMs
        avgTextureWallMs = New-MetricDelta -BaselineValue $BaselineSummary.avgTextureWallMs -CandidateValue $CandidateSummary.avgTextureWallMs
        avgTextureReconWallMs = New-MetricDelta -BaselineValue $BaselineSummary.avgTextureReconWallMs -CandidateValue $CandidateSummary.avgTextureReconWallMs
        avgTextureAmazeWallMs = New-MetricDelta -BaselineValue $BaselineSummary.avgTextureAmazeWallMs -CandidateValue $CandidateSummary.avgTextureAmazeWallMs
    }
}

$compare = New-PlaybackCompare -BaselineSummary $baselineSummary -CandidateSummary $candidateSummary
$speedCompare = if ($SeparateCandidateSpeedRun) {
    New-PlaybackCompare -BaselineSummary $baselineSummary -CandidateSummary $candidateSpeedSummary
} else {
    $null
}
$analysisCompare = if ($SeparateCandidateSpeedRun) { $speedCompare } else { $compare }
$analysisBasis = if ($SeparateCandidateSpeedRun) { "candidateSpeed" } else { "candidate" }

$summary["status"] = if ($proofFailures.Count -eq 0) { "success" } else { "failed" }
$summary["baseline"] = $baselineSummary
$summary["candidate"] = $candidateSummary
$summary["candidateSpeed"] = $candidateSpeedSummary
$summary["compare"] = $compare
$summary["speedCompare"] = $speedCompare
$summary["proofFailures"] = @($proofFailures)
$summary["analysis"] = New-PlaybackAbAnalysis -Compare $analysisCompare -ProofFailures @($proofFailures) -ComparisonBasis $analysisBasis
$summary["childOutput"] = [pscustomobject]@{
    baseline = $baselineResult.output
    candidate = $candidateResult.output
    candidateSpeed = if ($SeparateCandidateSpeedRun) { $candidateSpeedResult.output } else { $null }
}
Write-JsonFile -Value ([pscustomobject]$summary) -Path $summaryPath
Copy-Item -LiteralPath $summaryPath -Destination $latestPath -Force

Write-Output $summaryPath
if ($proofFailures.Count -gt 0) {
    exit 1
}
