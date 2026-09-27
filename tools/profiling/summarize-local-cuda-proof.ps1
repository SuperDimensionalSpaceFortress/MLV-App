param(
    [string]$RepoRoot = ".",
    [string]$SummaryPath = "",
    [string]$Output = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

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

function Get-Field {
    param(
        [AllowNull()]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) {
        return $null
    }
    $prop.Value
}

function Convert-ToNullableDouble {
    param([object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
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

function Convert-ToInt64 {
    param([object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return 0L
    }
    [long]$Value
}

function Format-Nullable {
    param(
        [object]$Value,
        [string]$Suffix = "",
        [int]$Digits = 3
    )
    $number = Convert-ToNullableDouble $Value
    if ($null -eq $number) {
        return "n/a"
    }
    $rounded = [Math]::Round($number, $Digits)
    $rounded.ToString("0.###", [System.Globalization.CultureInfo]::InvariantCulture) + $Suffix
}

function Format-Percent {
    param([object]$Value)
    $number = Convert-ToNullableDouble $Value
    if ($null -eq $number) {
        return "n/a"
    }
    $sign = if ($number -gt 0) { "+" } else { "" }
    $sign + $number.ToString("0.###", [System.Globalization.CultureInfo]::InvariantCulture) + "%"
}

function Format-MetricDeltaLine {
    param(
        [string]$Label,
        [AllowNull()]$Metric,
        [string]$Unit = "",
        [switch]$LowerIsBetter
    )
    if ($null -eq $Metric) {
        return "${Label}: n/a"
    }
    $baseline = Format-Nullable (Get-Field $Metric "baseline") $Unit
    $candidate = Format-Nullable (Get-Field $Metric "candidate") $Unit
    $delta = Format-Nullable (Get-Field $Metric "delta") $Unit
    $pct = Format-Percent (Get-Field $Metric "deltaPercent")
    $direction = if ($LowerIsBetter) { "lower is better" } else { "higher is better" }
    "${Label}: CPU $baseline -> CUDA $candidate, delta $delta ($pct, $direction)"
}

function Format-CdngThroughputGroupLine {
    param([AllowNull()]$Group)

    $rollup = Get-Field $Group "rollup"
    if ($null -eq $rollup) {
        return $null
    }
    $groupBy = [string](Get-Field $Group "groupBy")
    if ([string]::IsNullOrWhiteSpace($groupBy)) {
        $groupBy = "group"
    }
    $value = [string](Get-Field $Group "value")
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = "unspecified"
    }
    $caseCount = Get-Field $Group "caseCount"
    "DNG throughput by ${groupBy} ${value}: status=$((Get-Field $rollup 'status')), cases=$caseCount, classified_runs=$((Get-Field $rollup 'classifiedRunCount')), wall_clock_improved=$((Get-Field $rollup 'wallClockImprovedCount')), attribution_hold=$((Get-Field $rollup 'attributionHoldCount')), not_promotable=$((Get-Field $rollup 'notPromotableCount')), default_promotion_candidates=$((Get-Field $rollup 'defaultPromotionCandidateCount')), suggested=$((Get-Field $rollup 'suggestedOptimization'))"
}

function New-SectionVerdict {
    param(
        [string]$Status,
        [bool]$Passed,
        [string[]]$Evidence,
        [string[]]$Blockers
    )
    [pscustomobject]@{
        status = $Status
        passed = [bool]$Passed
        evidence = @($Evidence)
        blockers = @($Blockers)
    }
}

function New-Diagnostic {
    param(
        [Parameter(Mandatory = $true)][string]$Area,
        [Parameter(Mandatory = $true)][string]$Code,
        [ValidateSet("blocker", "warning", "info")][string]$Severity = "blocker",
        [Parameter(Mandatory = $true)][string]$Message,
        [string[]]$Evidence = @(),
        [string]$NextAction = ""
    )
    [pscustomobject]@{
        area = $Area
        code = $Code
        severity = $Severity
        message = $Message
        evidence = @($Evidence | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
        nextAction = $NextAction
    }
}

function Add-Diagnostic {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Diagnostics,
        [Parameter(Mandatory = $true)][string]$Area,
        [Parameter(Mandatory = $true)][string]$Code,
        [ValidateSet("blocker", "warning", "info")][string]$Severity = "blocker",
        [Parameter(Mandatory = $true)][string]$Message,
        [string[]]$Evidence = @(),
        [string]$NextAction = ""
    )
    [void]$Diagnostics.Add((New-Diagnostic `
        -Area $Area `
        -Code $Code `
        -Severity $Severity `
        -Message $Message `
        -Evidence $Evidence `
        -NextAction $NextAction))
}

function Get-DiagnosticSummary {
    param([object[]]$Diagnostics)

    $blocking = @($Diagnostics | Where-Object { [string]$_.severity -eq "blocker" })
    $warnings = @($Diagnostics | Where-Object { [string]$_.severity -eq "warning" })
    $primaryPriority = @(
        "DRY_RUN_PLAN_ONLY",
        "NVIDIA_SMI_UNUSABLE",
        "TOP_LEVEL_STATUS_NOT_SUCCESS",
        "TOP_LEVEL_FAILURE",
        "PLAYBACK_PROOF_MISSING",
        "PLAYBACK_NO_GPU_TEXTURE_NR_FRAMES",
        "PLAYBACK_FALLBACK_FRAMES",
        "PLAYBACK_GL_PROBE_INACTIVE",
        "GL_RENDERER_NOT_REPORTED",
        "PLAYBACK_AB_MISSING",
        "CDNG_PROOF_MISSING",
        "DNG_HASH_MISSING"
    )
    $primary = $null
    foreach ($code in $primaryPriority) {
        $match = @($blocking | Where-Object { [string]$_.code -eq $code } | Select-Object -First 1)
        if ($match.Count -gt 0) {
            $primary = [string]$match[0].code
            break
        }
    }
    if ($null -eq $primary -and $blocking.Count -gt 0) {
        $primary = [string]$blocking[0].code
    }
    [pscustomobject]@{
        blockerCount = $blocking.Count
        warningCount = $warnings.Count
        infoCount = @($Diagnostics | Where-Object { [string]$_.severity -eq "info" }).Count
        primaryBlocker = $primary
        blockerCodes = @($blocking | ForEach-Object { [string]$_.code } | Select-Object -Unique)
        warningCodes = @($warnings | ForEach-Object { [string]$_.code } | Select-Object -Unique)
    }
}

function Test-DiagnosticCode {
    param(
        [object[]]$Diagnostics,
        [string[]]$Codes
    )
    @($Diagnostics | Where-Object { [string]$_.code -in $Codes }).Count -gt 0
}

function New-ActionStep {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [int]$Priority,
        [Parameter(Mandatory = $true)][string]$Area,
        [string[]]$ReasonCodes = @(),
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$Detail,
        [string[]]$Commands = @(),
        [string]$ProofBoundary = ""
    )
    [pscustomobject]@{
        id = $Id
        priority = $Priority
        area = $Area
        reasonCodes = @($ReasonCodes)
        title = $Title
        detail = $Detail
        commands = @($Commands | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
        proofBoundary = $ProofBoundary
    }
}

function Add-ActionStep {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Steps,
        [Parameter(Mandatory = $true)]$Step
    )
    if (@($Steps | Where-Object { [string]$_.id -eq [string]$Step.id }).Count -eq 0) {
        [void]$Steps.Add($Step)
    }
}

function New-ActionPlan {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [object[]]$Diagnostics,
        [AllowNull()]$DiagnosticSummary,
        [AllowNull()]$Summary
    )

    $steps = [System.Collections.Generic.List[object]]::new()
    $clip = [string](Get-Field $Summary.inputs "clipPath")
    if ([string]::IsNullOrWhiteSpace($clip)) {
        $clip = "<Dual ISO .MLV>"
    }
    $directProofCommand = "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\run-local-cuda-playback-dng-smoke.ps1 -RepoRoot . -Input '$clip'"
    $kitProofCommand = ".\RUN-CUDA-DOGFOOD.ps1 -Input '$clip'"
    $directLosslessAsyncProofCommand = "$directProofCommand -CdngCodecs lossless -CandidateUseAsyncWriter -CandidateUseAsyncWriterCompression -CandidateAsyncWriterQueueDepth 2 -CandidateAsyncWriterThreadCount 2"
    $kitLosslessAsyncProofCommand = "$kitProofCommand -DngCodecs lossless -DngAsyncWriter -DngAsyncWriterCompression -DngAsyncWriterQueueDepth 2 -DngAsyncWriterThreadCount 2"
    $directSummaryCommand = "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\summarize-local-cuda-proof.ps1 -RepoRoot . -SummaryPath <summary.json>"
    $importCommand = "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\import-local-cuda-proof-result.ps1 -RepoRoot . -PacketPath <mlvapp-local-cuda-proof.zip>"

    if ($Status -eq "QUOTABLE_PASS") {
        Add-ActionStep -Steps $steps -Step (New-ActionStep `
            -Id "QUOTE_WITH_BOUNDARIES" `
            -Priority 10 `
            -Area "claim-boundary" `
            -Title "Review and quote only host-scoped support/speed claims." `
            -Detail "The packet passed the fail-closed gate. Quote only the host, clip, receipt, codec set, max-frame count, release/backend hashes, and run order captured in this report." `
            -Commands @($directSummaryCommand, $importCommand) `
            -ProofBoundary "A QUOTABLE_PASS packet is still scoped evidence, not a universal GPU support claim.")
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "QUOTE_PLAYBACK_SUPPORT_NOT_SPEEDUP" `
                -Priority 20 `
                -Area "playback-speed" `
                -ReasonCodes @("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED") `
                -Title "Treat playback as support/correctness evidence, not a speedup." `
                -Detail "The packet can prove scoped playback support while also showing no presented-FPS improvement. Quote the support boundary and the measured delta, not a speedup." `
                -Commands @($directSummaryCommand) `
                -ProofBoundary "Clean playback proof without positive FPS delta is not a performance win.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("DNG_EXPORT_ELAPSED_REGRESSED", "DNG_COMPRESSION_DOMINATES_AFTER_GPU_GAIN")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "USE_PACKET_FOR_E3_THROUGHPUT_WORK" `
                -Priority 30 `
                -Area "e3-throughput" `
                -ReasonCodes @("DNG_EXPORT_ELAPSED_REGRESSED", "DNG_COMPRESSION_DOMINATES_AFTER_GPU_GAIN") `
                -Title "Use the packet as E3 throughput input, not a speedup claim." `
                -Detail "If Dual ISO GPU work improved but overall/lossless elapsed time did not, the next implementation target is compression/writer overlap rather than more recon tuning." `
                -Commands @("pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\compare-cdng-export-matrices.ps1 -RepoRoot . -Baseline <baseline-matrix.json> -Candidate <candidate-matrix.json>") `
                -ProofBoundary "Correctness plus a wall-time regression should not be presented as a throughput win.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("DNG_THROUGHPUT_ATTRIBUTION_HOLD")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "REVIEW_E3_THROUGHPUT_ATTRIBUTION_HOLD" `
                -Priority 35 `
                -Area "e3-throughput" `
                -ReasonCodes @("DNG_THROUGHPUT_ATTRIBUTION_HOLD") `
                -Title "Review async writer lag before default promotion." `
                -Detail "The packet improved wall-clock export time but still carried writer-completion attribution holds. Treat it as an opt-in throughput candidate until the promotion gate policy explicitly accepts or fixes that lag." `
                -Commands @("pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\compare-cdng-export-matrices.ps1 -RepoRoot . -Baseline <identity-matrix.json> -Candidate <async-matrix.json>") `
                -ProofBoundary "Wall-clock improvement is useful evidence, but it is not automatic default-promotion authority.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "REVIEW_E3_CODEC_SCOPED_THROUGHPUT" `
                -Priority 38 `
                -Area "e3-throughput" `
                -ReasonCodes @("DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL") `
                -Title "Use codec-scoped DNG evidence without promoting defaults." `
                -Detail "A codec group improved while the all-codec/default gate stayed stricter. Run or compare a lossless-only async-compress proof before deciding whether the next optimization belongs in lossless writer/compression policy or uncompressed wall-clock work." `
                -Commands @($kitLosslessAsyncProofCommand, $directLosslessAsyncProofCommand, "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\compare-cdng-export-matrices.ps1 -RepoRoot . -Baseline <identity-matrix.json> -Candidate <lossless-async-matrix.json>") `
                -ProofBoundary "Codec-scoped throughput evidence cannot prove default all-codec export speed or support.")
        }
    }
    else {
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("DRY_RUN_PLAN_ONLY")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "RUN_REAL_HOST_PROOF" `
                -Priority 10 `
                -Area "run" `
                -ReasonCodes @("DRY_RUN_PLAN_ONLY") `
                -Title "Run the proof for real on the NVIDIA host." `
                -Detail "The current packet is a dry-run plan. Run the same kit or repo command without -DryRun on Dell or UltraMagnus before making support or speed claims." `
                -Commands @($kitProofCommand, $directProofCommand) `
                -ProofBoundary "A dry-run packet proves script wiring only, not CUDA playback, DNG parity, or speed.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("NVIDIA_SMI_UNUSABLE")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "FIX_NVIDIA_DISCOVERY" `
                -Priority 20 `
                -Area "host" `
                -ReasonCodes @("NVIDIA_SMI_UNUSABLE") `
                -Title "Fix NVIDIA driver discovery before rerunning proof." `
                -Detail "The proof did not see a usable NVIDIA GPU. Confirm the NVIDIA driver and nvidia-smi are visible on the target host, then rerun the local proof." `
                -Commands @("nvidia-smi", $directProofCommand) `
                -ProofBoundary "No NVIDIA discovery means no CUDA support or speed claim.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("PLAYBACK_GL_PROBE_INACTIVE", "GL_RENDERER_NOT_REPORTED", "GL_RENDERER_DOES_NOT_NAME_NVIDIA")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "FIX_HYBRID_GL_CUDA_SELECTION" `
                -Priority 30 `
                -Area "hybrid-gpu" `
                -ReasonCodes @("PLAYBACK_GL_PROBE_INACTIVE", "GL_RENDERER_NOT_REPORTED", "GL_RENDERER_DOES_NOT_NAME_NVIDIA") `
                -Title "Verify CUDA and OpenGL land on the intended NVIDIA adapter." `
                -Detail "Hybrid laptops can load CUDA on the NVIDIA GPU while the viewport runs elsewhere. Keep high-performance GPU preference enabled and require GL renderer/probe evidence before trusting no-readback playback." `
                -Commands @($directProofCommand, "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\start-release-cuda-playback.ps1 -RepoRoot . -Input '$clip'") `
                -ProofBoundary "CUDA load alone is not no-readback playback proof on a hybrid-GPU laptop.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("PLAYBACK_CORRECTNESS_NOT_VALIDATED", "PLAYBACK_NO_GPU_TEXTURE_NR_FRAMES", "PLAYBACK_FALLBACK_FRAMES", "PLAYBACK_GL_PARITY_MISMATCH")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "INSPECT_PLAYBACK_PROOF_CHILD" `
                -Priority 40 `
                -Area "playback" `
                -ReasonCodes @("PLAYBACK_CORRECTNESS_NOT_VALIDATED", "PLAYBACK_NO_GPU_TEXTURE_NR_FRAMES", "PLAYBACK_FALLBACK_FRAMES", "PLAYBACK_GL_PARITY_MISMATCH") `
                -Title "Inspect the playback child proof before changing defaults." `
                -Detail "The P3 no-readback path did not produce clean correctness/no-fallback/GL parity evidence. Inspect fallback reasons, scoped receipt/settings, screenshots, and child output tails before treating FPS as meaningful." `
                -Commands @($directSummaryCommand) `
                -ProofBoundary "Playback FPS is not proof if pixels, fallback counts, or GL parity fail.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("PLAYBACK_AB_MISSING", "PLAYBACK_AB_STATUS_NOT_SUCCESS", "PLAYBACK_AB_COMPARE_MISSING", "PLAYBACK_AB_PROOF_FAILURE")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "RERUN_PLAYBACK_AB" `
                -Priority 50 `
                -Area "playback-speed" `
                -ReasonCodes @("PLAYBACK_AB_MISSING", "PLAYBACK_AB_STATUS_NOT_SUCCESS", "PLAYBACK_AB_COMPARE_MISSING", "PLAYBACK_AB_PROOF_FAILURE") `
                -Title "Rerun paired CPU-vs-CUDA playback A/B." `
                -Detail "Playback speed cannot be quoted without a clean same-release CPU baseline versus CUDA candidate comparison." `
                -Commands @("pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\run-release-cuda-playback-ab.ps1 -RepoRoot . -ClipPath '$clip'") `
                -ProofBoundary "P3 correctness packets and launcher success are not playback speed evidence.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("CDNG_PROOF_MISSING", "CDNG_VERDICT_NOT_PASS", "DNG_HASH_MISSING", "DNG_HASH_NOT_PASS", "CDNG_SPEED_MISSING", "CDNG_SPEED_EMPTY", "DNG_CANDIDATE_FRAME_COUNT_ZERO", "DNG_GPU_ATTEMPT_INCOMPLETE", "DNG_GPU_REPLACEMENT_INCOMPLETE", "DNG_GPU_TRUSTED_INCOMPLETE")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "RERUN_DNG_HASH_AND_EXPORT_PROOF" `
                -Priority 60 `
                -Area "dng-export" `
                -ReasonCodes @("CDNG_PROOF_MISSING", "CDNG_VERDICT_NOT_PASS", "DNG_HASH_MISSING", "DNG_HASH_NOT_PASS", "CDNG_SPEED_MISSING", "CDNG_SPEED_EMPTY", "DNG_CANDIDATE_FRAME_COUNT_ZERO", "DNG_GPU_ATTEMPT_INCOMPLETE", "DNG_GPU_REPLACEMENT_INCOMPLETE", "DNG_GPU_TRUSTED_INCOMPLETE") `
                -Title "Rerun DNG hash/export proof before claiming CUDA DNG support." `
                -Detail "DNG export needs CDNG PASS, DNG hash PASS, and full candidate GPU attempted/replaced coverage, plus trusted frames when trusted export is requested." `
                -Commands @($directProofCommand, "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\run-release-cdng-export-profile-matrix.ps1 -RepoRoot . -CasesPath <cases.json> -CandidateEnableGpuExport -RequireDngHashMatch") `
                -ProofBoundary "Export is not proven by backend load; CPU-vs-GPU DNG hashes must pass.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("DNG_EXPORT_ELAPSED_REGRESSED", "DNG_COMPRESSION_DOMINATES_AFTER_GPU_GAIN")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "USE_PACKET_FOR_E3_THROUGHPUT_WORK" `
                -Priority 70 `
                -Area "e3-throughput" `
                -ReasonCodes @("DNG_EXPORT_ELAPSED_REGRESSED", "DNG_COMPRESSION_DOMINATES_AFTER_GPU_GAIN") `
                -Title "Use the packet as E3 throughput input, not a speedup claim." `
                -Detail "If Dual ISO GPU work improved but overall/lossless elapsed time did not, the next implementation target is compression/writer overlap rather than more recon tuning." `
                -Commands @("pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\compare-cdng-export-matrices.ps1 -RepoRoot . -Baseline <baseline-matrix.json> -Candidate <candidate-matrix.json>") `
                -ProofBoundary "Correctness plus a wall-time regression should not be presented as a throughput win.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("DNG_THROUGHPUT_ATTRIBUTION_HOLD")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "REVIEW_E3_THROUGHPUT_ATTRIBUTION_HOLD" `
                -Priority 75 `
                -Area "e3-throughput" `
                -ReasonCodes @("DNG_THROUGHPUT_ATTRIBUTION_HOLD") `
                -Title "Review async writer lag before default promotion." `
                -Detail "The packet improved wall-clock export time but still carried writer-completion attribution holds. Treat it as an opt-in throughput candidate until the promotion gate policy explicitly accepts or fixes that lag." `
                -Commands @("pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\compare-cdng-export-matrices.ps1 -RepoRoot . -Baseline <identity-matrix.json> -Candidate <async-matrix.json>") `
                -ProofBoundary "Wall-clock improvement is useful evidence, but it is not automatic default-promotion authority.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "REVIEW_E3_CODEC_SCOPED_THROUGHPUT" `
                -Priority 78 `
                -Area "e3-throughput" `
                -ReasonCodes @("DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL") `
                -Title "Use codec-scoped DNG evidence without promoting defaults." `
                -Detail "A codec group improved while the all-codec/default gate stayed stricter. Keep any support/speed claim blocked until proof blockers are fixed, then rerun or compare a lossless-only async-compress proof." `
                -Commands @($kitLosslessAsyncProofCommand, $directLosslessAsyncProofCommand, "pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\profiling\compare-cdng-export-matrices.ps1 -RepoRoot . -Baseline <identity-matrix.json> -Candidate <lossless-async-matrix.json>") `
                -ProofBoundary "Codec-scoped throughput evidence cannot prove default all-codec export speed or support.")
        }
        if (Test-DiagnosticCode -Diagnostics $Diagnostics -Codes @("TOP_LEVEL_STATUS_NOT_SUCCESS", "TOP_LEVEL_FAILURE")) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "INSPECT_TOP_LEVEL_FAILURES" `
                -Priority 80 `
                -Area "run" `
                -ReasonCodes @("TOP_LEVEL_STATUS_NOT_SUCCESS", "TOP_LEVEL_FAILURE") `
                -Title "Inspect top-level failures and child output tails." `
                -Detail "The wrapper status or failure list was not clean. Read summary.json childProcesses output tails and fix the first failing stage." `
                -Commands @($directSummaryCommand) `
                -ProofBoundary "A failed wrapper status keeps all support and speed claims closed.")
        }
        if ($steps.Count -eq 0) {
            Add-ActionStep -Steps $steps -Step (New-ActionStep `
                -Id "INSPECT_DIAGNOSTIC_PACKET" `
                -Priority 90 `
                -Area "triage" `
                -Title "Inspect the diagnostic packet manually." `
                -Detail "The packet is NOT_QUOTABLE but did not match a known action-plan code. Inspect diagnostics, blockers, warnings, and child summaries before changing code." `
                -Commands @($directSummaryCommand, $importCommand) `
                -ProofBoundary "Unknown diagnostic shapes remain fail-closed.")
        }
    }

    $orderedSteps = @($steps | Sort-Object Priority, Id)
    [pscustomobject]@{
        status = if ($Status -eq "QUOTABLE_PASS") { "claim-review" } else { "blocked-triage" }
        primaryBlocker = if ($DiagnosticSummary) { $DiagnosticSummary.primaryBlocker } else { $null }
        stepCount = $orderedSteps.Count
        steps = $orderedSteps
        proofBoundary = @(
            "Action plans are triage guidance, not proof.",
            "Only QUOTABLE_PASS from the same host unlocks support or speed claims.",
            "If multiple steps are listed, address them in priority order and rerun the packet."
        )
    }
}

$root = (Resolve-Path -LiteralPath $RepoRoot).Path
if ([string]::IsNullOrWhiteSpace($SummaryPath)) {
    $SummaryPath = Join-Path $root ".claude-state\profiling\local-cuda-playback-dng-smoke-latest.json"
}
else {
    $SummaryPath = Resolve-RepoPath -Root $root -Path $SummaryPath
}
if (!(Test-Path -LiteralPath $SummaryPath -PathType Leaf)) {
    throw "Summary not found: $SummaryPath"
}

$summaryFullPath = (Resolve-Path -LiteralPath $SummaryPath).Path
$summary = Get-Content -LiteralPath $summaryFullPath -Raw | ConvertFrom-Json -Depth 100
$failures = @($summary.failures | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | ForEach-Object { [string]$_ })
$warnings = @($summary.warnings | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | ForEach-Object { [string]$_ })

$playback = $summary.proof.playback
$playbackAb = $summary.proof.playbackAb
$cdng = $summary.proof.cdng
$diagnostics = [System.Collections.Generic.List[object]]::new()

$nvidiaRows = @($summary.host.nvidiaSmi.rows | ForEach-Object { [string]$_ })
$nvidiaOk = [bool]$summary.host.nvidiaSmi.ok
$dryRun = [bool]$summary.dryRun
$topStatus = [string]$summary.status

$playbackBlockers = [System.Collections.Generic.List[string]]::new()
if ($null -eq $playback) {
    [void]$playbackBlockers.Add("playback proof did not run or did not produce a summary")
    Add-Diagnostic `
        -Diagnostics $diagnostics `
        -Area "playback" `
        -Code "PLAYBACK_PROOF_MISSING" `
        -Message "Playback no-readback proof did not run or did not produce a summary." `
        -NextAction "Run the local CUDA proof wrapper with playback enabled on the target NVIDIA host."
}
else {
    if (-not [bool]$playback.correctnessValidated) {
        [void]$playbackBlockers.Add("correctnessValidated is not true")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "playback" `
            -Code "PLAYBACK_CORRECTNESS_NOT_VALIDATED" `
            -Message "Playback proof did not set correctnessValidated=true." `
            -Evidence @("correctnessValidated=$($playback.correctnessValidated)") `
            -NextAction "Inspect the P3 playback child summary and screenshot/frame-diff failures before trusting FPS."
    }
    if ((Convert-ToInt64 $playback.totalGpuTextureNoReadbackFrames) -le 0) {
        [void]$playbackBlockers.Add("no GPU texture no-readback frames were reported")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "playback" `
            -Code "PLAYBACK_NO_GPU_TEXTURE_NR_FRAMES" `
            -Message "Playback did not report any CUDA-to-GL texture no-readback frames." `
            -Evidence @("gpu_texture_no_readback_frames=$($playback.totalGpuTextureNoReadbackFrames)") `
            -NextAction "Check backend load, scoped Dual ISO settings, CUDA-to-GL texture-present request, and GL/CUDA adapter selection."
    }
    if ((Convert-ToInt64 $playback.totalFallbackFrames) -ne 0) {
        [void]$playbackBlockers.Add("fallback frames were reported: $($playback.totalFallbackFrames)")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "playback" `
            -Code "PLAYBACK_FALLBACK_FRAMES" `
            -Message "Playback reported fallback frames instead of staying on the scoped GPU no-readback path." `
            -Evidence @("fallback_frames=$($playback.totalFallbackFrames)") `
            -NextAction "Inspect fallback reasons in the playback child summary; unsupported receipts/settings must stay CPU until the scoped gate widens."
    }
    if ((Convert-ToInt64 $playback.totalGlProbeActiveFrames) -le 0) {
        [void]$playbackBlockers.Add("GL parity probe did not report active frames")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "hybrid-gpu" `
            -Code "PLAYBACK_GL_PROBE_INACTIVE" `
            -Message "GL parity probing did not become active during playback." `
            -Evidence @("gl_probe_active_frames=$($playback.totalGlProbeActiveFrames)") `
            -NextAction "On hybrid laptops, verify the Windows high-performance GPU preference and GL renderer before claiming no-readback presentation."
    }
    if ((Convert-ToInt64 $playback.totalGlParityMismatches) -ne 0) {
        [void]$playbackBlockers.Add("GL parity mismatches were reported: $($playback.totalGlParityMismatches)")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "playback" `
            -Code "PLAYBACK_GL_PARITY_MISMATCH" `
            -Message "GL parity probe reported mismatches." `
            -Evidence @("gl_mismatches=$($playback.totalGlParityMismatches)") `
            -NextAction "Treat FPS as untrusted until screenshot/frame-diff evidence is clean."
    }
    $rendererDescriptions = @($playback.rendererDescriptions | Where-Object {
            -not [string]::IsNullOrWhiteSpace([string]$_)
        } | ForEach-Object { [string]$_ })
    if ($rendererDescriptions.Count -eq 0) {
        [void]$playbackBlockers.Add("GL renderer descriptions were not reported")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "hybrid-gpu" `
            -Code "GL_RENDERER_NOT_REPORTED" `
            -Message "Playback proof did not report a GL renderer description." `
            -NextAction "Require GL renderer evidence for Dell hybrid-GPU claims; CUDA load alone is insufficient."
    }
    elseif (@($rendererDescriptions | Where-Object { $_ -match "NVIDIA|GeForce|RTX|GTX|Quadro" }).Count -eq 0) {
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "hybrid-gpu" `
            -Code "GL_RENDERER_DOES_NOT_NAME_NVIDIA" `
            -Severity "warning" `
            -Message "The reported GL renderer does not name an NVIDIA adapter." `
            -Evidence @($rendererDescriptions | ForEach-Object { "renderer=$_" }) `
            -NextAction "For the Dell RTX 3060 laptop, confirm CUDA/GL interop is on the intended adapter before treating speed as representative."
    }
}
$playbackEvidence = @()
if ($playback) {
    $playbackEvidence = @(
        "status=$($playback.status)",
        "correctnessValidated=$($playback.correctnessValidated)",
        "gpu_texture_no_readback_frames=$($playback.totalGpuTextureNoReadbackFrames)",
        "fallback_frames=$($playback.totalFallbackFrames)",
        "gl_probe_active_frames=$($playback.totalGlProbeActiveFrames)",
        "gl_mismatches=$($playback.totalGlParityMismatches)"
    )
}
$playbackVerdict = New-SectionVerdict `
    -Status $(if ($playbackBlockers.Count -eq 0) { "PASS" } else { "BLOCKED" }) `
    -Passed ($playbackBlockers.Count -eq 0) `
    -Evidence $playbackEvidence `
    -Blockers @($playbackBlockers)

$playbackAbBlockers = [System.Collections.Generic.List[string]]::new()
$playbackAbSpeedCompare = $null
$playbackAbComparisonBasis = "candidate"
if ($playbackAb) {
    if ($playbackAb.speedCompare) {
        $playbackAbSpeedCompare = $playbackAb.speedCompare
        $playbackAbComparisonBasis = "candidateSpeed"
    }
    else {
        $playbackAbSpeedCompare = $playbackAb.compare
    }
}
if ($null -eq $playbackAb) {
    [void]$playbackAbBlockers.Add("playback A/B did not run or did not produce a summary")
    Add-Diagnostic `
        -Diagnostics $diagnostics `
        -Area "playback-speed" `
        -Code "PLAYBACK_AB_MISSING" `
        -Message "Playback CPU-vs-CUDA A/B did not run or did not produce a summary." `
        -NextAction "Run the local CUDA proof wrapper without -SkipPlaybackAb to quote playback speed on this host."
}
else {
    if ([string]$playbackAb.status -notin @("success", "planned")) {
        [void]$playbackAbBlockers.Add("playback A/B status was '$($playbackAb.status)'")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "playback-speed" `
            -Code "PLAYBACK_AB_STATUS_NOT_SUCCESS" `
            -Message "Playback A/B status was not success." `
            -Evidence @("status=$($playbackAb.status)") `
            -NextAction "Inspect the playback A/B child summary for GUI smoke, GL parity, or artifact-detection failures."
    }
    if ($null -eq $playbackAbSpeedCompare) {
        [void]$playbackAbBlockers.Add("playback A/B compare metrics were missing")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "playback-speed" `
            -Code "PLAYBACK_AB_COMPARE_MISSING" `
            -Message "Playback A/B compare metrics were missing." `
            -NextAction "Rerun the playback A/B stage; speed cannot be quoted without paired CPU/CUDA metrics."
    }
    foreach ($failure in @($playbackAb.proofFailures)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$failure)) {
            [void]$playbackAbBlockers.Add([string]$failure)
            Add-Diagnostic `
                -Diagnostics $diagnostics `
                -Area "playback-speed" `
                -Code "PLAYBACK_AB_PROOF_FAILURE" `
                -Message ([string]$failure) `
                -NextAction "Fix the A/B proof failure before quoting playback speed."
        }
    }
    # round 4 (sol major): relying only on $playbackAb.proofFailures let a legacy/degenerate
    # packet -- one produced before the host-load gate existed, or lacking the field for any other
    # reason -- pass this section cleanly even though its FPS numbers were never checked against
    # host load at all. Check the relevant legs' hostLoadProvisional independently, defaulting a
    # missing/absent property to provisional (fail toward provisional, same stance as every other
    # reader in this card).
    $playbackAbCandidateLeg = if ($playbackAbComparisonBasis -eq "candidateSpeed") {
        $playbackAb.candidateSpeed
    } else {
        $playbackAb.candidate
    }
    $playbackAbHostLoadLegs = @(
        [pscustomobject]@{ Name = "baseline"; Leg = $playbackAb.baseline },
        [pscustomobject]@{ Name = $playbackAbComparisonBasis; Leg = $playbackAbCandidateLeg }
    )
    # round 5 (sol BLOCKER): checking hostLoadProvisional alone repeats the exact inconsistency
    # round 4 closed at the producer -- a leg carrying an explicit hostLoadProvisional=false
    # alongside a missing/blank/"unknown" hostLoadState is a legacy or degenerate leg, not a clean
    # one. sol's repro: hostLoadProvisional=false with hostLoadState='unknown' checked only the
    # boolean here. Combine both fields, same as Get-Field's other callers in this section.
    # round 8 (sol MAJOR item 3): that fix only special-cased hostLoadState=="unknown" -- a leg
    # with hostLoadState="exceeded" and an inconsistent/fabricated hostLoadProvisional=false still
    # read clean. THE CANONICAL PREDICATE (identical at every one of this card's six sites --
    # see compare-machine-perf.ps1's Get-PlaybackAbLegHostLoadProvisional for the full cross-file
    # note): clean iff state=="quiet" AND provisional==false; every other combination is
    # provisional.
    $playbackAbHostLoadProvisional = $false
    foreach ($legEntry in $playbackAbHostLoadLegs) {
        $legProvisionalValue = Get-Field $legEntry.Leg "hostLoadProvisional"
        $legProvisionalDeclared = if ($null -eq $legProvisionalValue) { $true } else { [bool]$legProvisionalValue }
        $legStateValue = Get-Field $legEntry.Leg "hostLoadState"
        $legState = if ($null -eq $legStateValue -or [string]::IsNullOrWhiteSpace([string]$legStateValue)) {
            "unknown"
        } else {
            [string]$legStateValue
        }
        $legProvisional = ($legProvisionalDeclared -or $legState -ne "quiet")
        if ($legProvisional) {
            $playbackAbHostLoadProvisional = $true
            [void]$playbackAbBlockers.Add("playback A/B $($legEntry.Name) leg host load was PROVISIONAL or unrecorded")
            Add-Diagnostic `
                -Diagnostics $diagnostics `
                -Area "playback-speed" `
                -Code "PLAYBACK_AB_HOST_LOAD_PROVISIONAL" `
                -Message "Playback A/B $($legEntry.Name) leg host load was PROVISIONAL or unrecorded." `
                -Evidence @("leg=$($legEntry.Name)") `
                -NextAction "Rerun the playback A/B stage on a quiet host; an fps number measured under provisional or unrecorded host load is not a property of the build."
        }
    }
    if (-not $playbackAbHostLoadProvisional -and
        $playbackAbSpeedCompare -and $null -ne $playbackAbSpeedCompare.presentedFps) {
        $fpsDelta = Convert-ToNullableDouble (Get-Field $playbackAbSpeedCompare.presentedFps "delta")
        if ($null -ne $fpsDelta -and $fpsDelta -le 0) {
            Add-Diagnostic `
                -Diagnostics $diagnostics `
                -Area "playback-speed" `
                -Code "PLAYBACK_PRESENTED_FPS_NOT_IMPROVED" `
                -Severity "warning" `
                -Message "CUDA playback did not improve presented FPS in the paired A/B metrics." `
                -Evidence @(
                    "comparison_basis=$playbackAbComparisonBasis",
                    (Format-MetricDeltaLine -Label "presented FPS" -Metric $playbackAbSpeedCompare.presentedFps)
                ) `
                -NextAction "Treat the packet as a support/correctness result, not a speedup claim, and inspect per-stage timing deltas or the playback A/B analysis suggestion."
        }
    }
}
$playbackSpeedEvidence = @()
if ($playbackAb -and $playbackAbSpeedCompare) {
    $playbackSpeedEvidence = @(
        "comparison_basis=$playbackAbComparisonBasis",
        (Format-MetricDeltaLine -Label "presented FPS" -Metric $playbackAbSpeedCompare.presentedFps),
        (Format-MetricDeltaLine -Label "GUI status FPS" -Metric $playbackAbSpeedCompare.guiStatusFps),
        (Format-MetricDeltaLine -Label "avg present interval" -Metric $playbackAbSpeedCompare.avgPresentIntervalMs -Unit " ms" -LowerIsBetter),
        (Format-MetricDeltaLine -Label "render total" -Metric $playbackAbSpeedCompare.avgRenderTotalMs -Unit " ms" -LowerIsBetter)
    )
    if ($playbackAb.analysis -and $playbackAb.analysis.suggestedOptimization) {
        $playbackSpeedEvidence += "suggested_optimization=$($playbackAb.analysis.suggestedOptimization)"
    }
}
$playbackAbVerdict = New-SectionVerdict `
    -Status $(if ($playbackAbBlockers.Count -eq 0) { "PASS" } else { "BLOCKED" }) `
    -Passed ($playbackAbBlockers.Count -eq 0) `
    -Evidence $playbackSpeedEvidence `
    -Blockers @($playbackAbBlockers)

$trustedRequired = [bool]$summary.inputs.trustedGpuExport
$cdngBlockers = [System.Collections.Generic.List[string]]::new()
if ($null -eq $cdng) {
    [void]$cdngBlockers.Add("CDNG proof did not run or did not produce a matrix summary")
    Add-Diagnostic `
        -Diagnostics $diagnostics `
        -Area "dng-export" `
        -Code "CDNG_PROOF_MISSING" `
        -Message "CDNG export proof did not run or did not produce a matrix summary." `
        -NextAction "Run the local CUDA proof wrapper with CDNG enabled to prove DNG hash parity and export speed on this host."
}
else {
    if ([string]$cdng.verdict -ne "PASS") {
        [void]$cdngBlockers.Add("CDNG matrix verdict was '$($cdng.verdict)'")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export" `
            -Code "CDNG_VERDICT_NOT_PASS" `
            -Message "CDNG matrix verdict was not PASS." `
            -Evidence @("verdict=$($cdng.verdict)") `
            -NextAction "Inspect the CDNG matrix rows and DNG hash report before quoting export support."
    }
    if ($null -eq $cdng.dngHash) {
        [void]$cdngBlockers.Add("DNG hash result was missing")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export" `
            -Code "DNG_HASH_MISSING" `
            -Message "DNG hash result was missing from the CDNG proof." `
            -NextAction "Require CPU-vs-GPU DNG hash evidence before claiming export correctness."
    }
    elseif ([string]$cdng.dngHash.verdict -ne "PASS") {
        [void]$cdngBlockers.Add("DNG hash verdict was '$($cdng.dngHash.verdict)'")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export" `
            -Code "DNG_HASH_NOT_PASS" `
            -Message "DNG hash verdict was not PASS." `
            -Evidence @("dng_hash_verdict=$($cdng.dngHash.verdict)") `
            -NextAction "Do not use the GPU DNG path until CPU/GPU DNG hashes match."
    }
    if ($null -eq $cdng.speed) {
        [void]$cdngBlockers.Add("CDNG speed metrics were missing")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export-speed" `
            -Code "CDNG_SPEED_MISSING" `
            -Message "CDNG speed metrics were missing." `
            -NextAction "Run a CDNG matrix/A-B proof that records wall-time and stage deltas."
    }
    elseif ((Convert-ToInt64 $cdng.speed.runCount) -le 0) {
        [void]$cdngBlockers.Add("CDNG speed runCount was zero")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export-speed" `
            -Code "CDNG_SPEED_EMPTY" `
            -Message "CDNG speed metrics contained zero runs." `
            -Evidence @("runCount=$($cdng.speed.runCount)") `
            -NextAction "Rerun with at least one CDNG matrix case and repeat."
    }
    if ((Convert-ToInt64 $cdng.candidateFrameCount) -le 0) {
        [void]$cdngBlockers.Add("candidate frame count was zero")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export" `
            -Code "DNG_CANDIDATE_FRAME_COUNT_ZERO" `
            -Message "Candidate export frame count was zero." `
            -Evidence @("candidate_frames=$($cdng.candidateFrameCount)") `
            -NextAction "Confirm the clip, max-frame count, and batch export invocation produced measured frames."
    }
    if ((Convert-ToInt64 $cdng.candidateGpuExportAttemptedFrames) -lt (Convert-ToInt64 $cdng.candidateFrameCount)) {
        [void]$cdngBlockers.Add("candidate did not attempt GPU export for every frame")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export" `
            -Code "DNG_GPU_ATTEMPT_INCOMPLETE" `
            -Message "Candidate did not attempt GPU export for every measured frame." `
            -Evidence @("candidate_frames=$($cdng.candidateFrameCount)", "gpu_attempted=$($cdng.candidateGpuExportAttemptedFrames)") `
            -NextAction "Inspect GPU export skip codes; unsupported receipt/settings must stay CPU."
    }
    if ((Convert-ToInt64 $cdng.candidateGpuExportReplacedFrames) -lt (Convert-ToInt64 $cdng.candidateFrameCount)) {
        [void]$cdngBlockers.Add("candidate did not replace every frame with GPU output")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export" `
            -Code "DNG_GPU_REPLACEMENT_INCOMPLETE" `
            -Message "Candidate did not replace every measured frame with GPU output." `
            -Evidence @("candidate_frames=$($cdng.candidateFrameCount)", "gpu_replaced=$($cdng.candidateGpuExportReplacedFrames)") `
            -NextAction "Do not quote GPU export speed until the measured candidate path actually used GPU output for all required frames."
    }
    if ($trustedRequired -and
        (Convert-ToInt64 $cdng.candidateGpuExportTrustedFrames) -lt (Convert-ToInt64 $cdng.candidateFrameCount)) {
        [void]$cdngBlockers.Add("candidate trusted GPU export frames did not cover every frame")
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export" `
            -Code "DNG_GPU_TRUSTED_INCOMPLETE" `
            -Message "Trusted GPU export frames did not cover every measured frame." `
            -Evidence @("candidate_frames=$($cdng.candidateFrameCount)", "gpu_trusted=$($cdng.candidateGpuExportTrustedFrames)") `
            -NextAction "Use trusted GPU export only when the candidate fully bypasses the CPU oracle for the measured eligible shape."
    }
    if ($cdng.speed) {
        $elapsedDelta = Convert-ToNullableDouble $cdng.speed.avgElapsedDeltaMs
        if ($null -ne $elapsedDelta -and $elapsedDelta -gt 0) {
            Add-Diagnostic `
                -Diagnostics $diagnostics `
                -Area "dng-export-speed" `
                -Code "DNG_EXPORT_ELAPSED_REGRESSED" `
                -Severity "warning" `
                -Message "CUDA DNG export wall time regressed versus CPU in the measured packet." `
                -Evidence @("avgElapsedDeltaMs=$($cdng.speed.avgElapsedDeltaMs)", "avgElapsedDeltaPercent=$($cdng.speed.avgElapsedDeltaPercent)") `
                -NextAction "Treat this as correctness proof plus E3 throughput input, not a speedup claim."
        }
        $dualIsoDelta = Convert-ToNullableDouble $cdng.speed.avgLlrawprocDualIsoDeltaMs
        $compressDelta = Convert-ToNullableDouble $cdng.speed.avgDngCompressDeltaMs
        if ($null -ne $dualIsoDelta -and $dualIsoDelta -lt 0 -and
            $null -ne $compressDelta -and $compressDelta -gt 0) {
            Add-Diagnostic `
                -Diagnostics $diagnostics `
                -Area "dng-export-speed" `
                -Code "DNG_COMPRESSION_DOMINATES_AFTER_GPU_GAIN" `
                -Severity "warning" `
                -Message "Dual ISO GPU work improved while DNG compression regressed, suggesting E3 compression/writer overlap is the next throughput lever." `
                -Evidence @("avgLlrawprocDualIsoDeltaMs=$($cdng.speed.avgLlrawprocDualIsoDeltaMs)", "avgDngCompressDeltaMs=$($cdng.speed.avgDngCompressDeltaMs)") `
                -NextAction "Prioritize E3 compression/writer overlap for lossless throughput instead of more recon tuning."
        }
    }
    $cdngAsyncWriter = Get-Field $cdng "asyncWriter"
    if ($cdngAsyncWriter -and
        [bool](Get-Field $cdngAsyncWriter "candidateUseAsyncWriterCompression") -and
        (Convert-ToInt64 (Get-Field $cdngAsyncWriter "candidateAsyncWriterOverlapRuns")) -le 0) {
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export-speed" `
            -Code "DNG_ASYNC_COMPRESS_NO_OVERLAP" `
            -Severity "warning" `
            -Message "DNG async compression was requested, but no candidate run reported async_writer_can_overlap_dng_compress=true." `
            -Evidence @(
                "overlapRuns=$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterOverlapRuns'))",
                "maxActive=$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterMaxActive'))",
                "maxQueued=$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterMaxQueued'))"
            ) `
            -NextAction "Check candidate profile fields dng_compress_placement, async_writer_can_overlap_dng_compress, async_writer_max_active, and async_writer_max_queued."
    }
    $cdngThroughput = Get-Field $cdng "throughputClassification"
    if ($cdngThroughput -and
        (Convert-ToInt64 (Get-Field $cdngThroughput "attributionHoldCount")) -gt 0) {
        Add-Diagnostic `
            -Diagnostics $diagnostics `
            -Area "dng-export-speed" `
            -Code "DNG_THROUGHPUT_ATTRIBUTION_HOLD" `
            -Severity "warning" `
            -Message "DNG export wall-clock improved in at least one run, but writer-completion/frame-total attribution still holds default promotion." `
            -Evidence @(
                "status=$((Get-Field $cdngThroughput 'status'))",
                "attributionHoldCount=$((Get-Field $cdngThroughput 'attributionHoldCount'))",
                "wallClockImprovedCount=$((Get-Field $cdngThroughput 'wallClockImprovedCount'))",
                "suggested=$((Get-Field $cdngThroughput 'suggestedOptimization'))"
            ) `
            -NextAction "Keep the candidate opt-in and review the async writer completion-lag gate before changing default export behavior."
    }
    $cdngThroughputByCodec = @(Get-Field $cdng "throughputClassificationByCodec")
    foreach ($codecGroup in $cdngThroughputByCodec) {
        $rollup = Get-Field $codecGroup "rollup"
        if ($rollup -and
            (Convert-ToInt64 (Get-Field $rollup "wallClockImprovedCount")) -gt 0 -and
            [string](Get-Field $cdngThroughput "status") -ne "promotion_candidate") {
            Add-Diagnostic `
                -Diagnostics $diagnostics `
                -Area "dng-export-speed" `
                -Code "DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL" `
                -Severity "info" `
                -Message "At least one CDNG codec group has wall-clock improvement, but the default all-codec throughput gate remains stricter." `
                -Evidence @(
                    "groupBy=$((Get-Field $codecGroup 'groupBy'))",
                    "value=$((Get-Field $codecGroup 'value'))",
                    "status=$((Get-Field $rollup 'status'))",
                    "wallClockImprovedCount=$((Get-Field $rollup 'wallClockImprovedCount'))",
                    "notPromotableCount=$((Get-Field $rollup 'notPromotableCount'))",
                    "suggested=$((Get-Field $rollup 'suggestedOptimization'))"
                ) `
                -NextAction "Treat this as codec-scoped evidence only until the top-level all-codec gate passes."
        }
    }
}
$cdngEvidence = @()
if ($cdng) {
    $cdngEvidence = @(
        "verdict=$($cdng.verdict)",
        "dngHash=$($cdng.dngHash.verdict)",
        "candidate_frames=$($cdng.candidateFrameCount)",
        "gpu_attempted=$($cdng.candidateGpuExportAttemptedFrames)",
        "gpu_replaced=$($cdng.candidateGpuExportReplacedFrames)",
        "gpu_trusted=$($cdng.candidateGpuExportTrustedFrames)"
    )
    if ($cdng.speed) {
        $cdngEvidence += "DNG wall time: CPU $(Format-Nullable $cdng.speed.avgBaselineElapsedMs ' ms') -> CUDA $(Format-Nullable $cdng.speed.avgCandidateElapsedMs ' ms'), delta $(Format-Nullable $cdng.speed.avgElapsedDeltaMs ' ms') ($(Format-Percent $cdng.speed.avgElapsedDeltaPercent))"
        $cdngEvidence += "DNG frame total avg delta: $(Format-Nullable $cdng.speed.avgFrameTotalDeltaMs ' ms')"
        $cdngEvidence += "DNG Dual ISO avg delta: $(Format-Nullable $cdng.speed.avgLlrawprocDualIsoDeltaMs ' ms')"
        $cdngEvidence += "DNG compression avg delta: $(Format-Nullable $cdng.speed.avgDngCompressDeltaMs ' ms')"
    }
    $cdngAsyncWriter = Get-Field $cdng "asyncWriter"
    if ($cdngAsyncWriter) {
        $cdngEvidence += "DNG async writer: requested=$((Get-Field $cdngAsyncWriter 'candidateUseAsyncWriter')), compress_requested=$((Get-Field $cdngAsyncWriter 'candidateUseAsyncWriterCompression')), overlap_runs=$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterOverlapRuns')), max_active=$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterMaxActive')), max_queued=$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterMaxQueued')), jobs=$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterJobsStarted'))/$((Get-Field $cdngAsyncWriter 'candidateAsyncWriterJobsFinished'))"
    }
    $cdngThroughput = Get-Field $cdng "throughputClassification"
    if ($cdngThroughput) {
        $cdngEvidence += "DNG throughput classification: status=$((Get-Field $cdngThroughput 'status')), classified_runs=$((Get-Field $cdngThroughput 'classifiedRunCount')), wall_clock_improved=$((Get-Field $cdngThroughput 'wallClockImprovedCount')), attribution_hold=$((Get-Field $cdngThroughput 'attributionHoldCount')), default_promotion_candidates=$((Get-Field $cdngThroughput 'defaultPromotionCandidateCount')), suggested=$((Get-Field $cdngThroughput 'suggestedOptimization'))"
    }
    foreach ($codecGroup in @(Get-Field $cdng "throughputClassificationByCodec")) {
        $line = Format-CdngThroughputGroupLine -Group $codecGroup
        if (-not [string]::IsNullOrWhiteSpace($line)) {
            $cdngEvidence += $line
        }
    }
    foreach ($caseGroup in @(Get-Field $cdng "throughputClassificationByCase")) {
        $line = Format-CdngThroughputGroupLine -Group $caseGroup
        if (-not [string]::IsNullOrWhiteSpace($line)) {
            $cdngEvidence += $line
        }
    }
}
$cdngVerdict = New-SectionVerdict `
    -Status $(if ($cdngBlockers.Count -eq 0) { "PASS" } else { "BLOCKED" }) `
    -Passed ($cdngBlockers.Count -eq 0) `
    -Evidence $cdngEvidence `
    -Blockers @($cdngBlockers)

$artifactEvidence = [pscustomobject]@{
    exeSha256 = $summary.artifacts.exe.sha256
    backendSha256 = $summary.artifacts.backend.sha256
    cudartSha256 = $summary.artifacts.cudart.sha256
    exePath = $summary.artifacts.exe.path
    backendPath = $summary.artifacts.backend.path
    cudartPath = $summary.artifacts.cudart.path
}

$overallBlockers = [System.Collections.Generic.List[string]]::new()
if ($dryRun) {
    [void]$overallBlockers.Add("dryRun=true; this is a plan, not hardware proof")
    Add-Diagnostic `
        -Diagnostics $diagnostics `
        -Area "run" `
        -Code "DRY_RUN_PLAN_ONLY" `
        -Message "dryRun=true; this packet is a plan, not hardware proof." `
        -NextAction "Run the proof helper without -DryRun on the target NVIDIA host."
}
if ($topStatus -ne "success") {
    [void]$overallBlockers.Add("top-level status is '$topStatus'")
    Add-Diagnostic `
        -Diagnostics $diagnostics `
        -Area "run" `
        -Code "TOP_LEVEL_STATUS_NOT_SUCCESS" `
        -Message "Top-level local CUDA proof status was not success." `
        -Evidence @("status=$topStatus") `
        -NextAction "Inspect child process outputs and failures in the summary."
}
if (-not $nvidiaOk) {
    [void]$overallBlockers.Add("nvidia-smi did not report a usable NVIDIA GPU")
    Add-Diagnostic `
        -Diagnostics $diagnostics `
        -Area "host" `
        -Code "NVIDIA_SMI_UNUSABLE" `
        -Message "nvidia-smi did not report a usable NVIDIA GPU." `
        -Evidence @("nvidiaSmiPath=$($summary.host.nvidiaSmi.path)", "nvidiaSmiError=$($summary.host.nvidiaSmi.error)") `
        -NextAction "Run on Dell/UltraMagnus with the NVIDIA driver visible, or fix NVIDIA driver/PATH discovery."
}
foreach ($failure in $failures) {
    [void]$overallBlockers.Add($failure)
    Add-Diagnostic `
        -Diagnostics $diagnostics `
        -Area "run" `
        -Code "TOP_LEVEL_FAILURE" `
        -Message $failure `
        -NextAction "Use the child process output tail in summary.json to locate the failing proof stage."
}
foreach ($section in @($playbackVerdict, $playbackAbVerdict, $cdngVerdict)) {
    if (-not [bool]$section.passed) {
        foreach ($blocker in @($section.blockers)) {
            [void]$overallBlockers.Add($blocker)
        }
    }
}
$overallStatus = if ($overallBlockers.Count -eq 0) { "QUOTABLE_PASS" } else { "NOT_QUOTABLE" }
$diagnosticList = @($diagnostics)
$diagnosticSummary = Get-DiagnosticSummary -Diagnostics $diagnosticList
$actionPlan = New-ActionPlan `
    -Status $overallStatus `
    -Diagnostics $diagnosticList `
    -DiagnosticSummary $diagnosticSummary `
    -Summary $summary

$report = [pscustomobject]@{
    schema = "mlvapp-local-cuda-proof-report.v1"
    createdAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    sourceSummary = $summaryFullPath
    status = $overallStatus
    topLevelStatus = $topStatus
    dryRun = $dryRun
    host = [pscustomobject]@{
        name = $summary.host.name
        nvidiaSmiOk = $nvidiaOk
        nvidiaSmiPath = $summary.host.nvidiaSmi.path
        nvidiaRows = $nvidiaRows
        gpuPreference = $summary.host.gpuPreference
    }
    artifacts = $artifactEvidence
    inputs = $summary.inputs
    playback = $playbackVerdict
    playbackSpeed = $playbackAbVerdict
    dngExport = $cdngVerdict
    warnings = @($warnings)
    blockers = @($overallBlockers | Select-Object -Unique)
    diagnostics = $diagnosticList
    diagnosticSummary = $diagnosticSummary
    actionPlan = $actionPlan
    proofBoundary = @(
        "Quote support or speed only when status is QUOTABLE_PASS and this summary was produced on the same host being claimed.",
        "Playback proof requires correctnessValidated=true, GPU Tex NR/no-readback frames, zero fallback frames, active GL parity probes, and zero GL mismatches.",
        "DNG proof requires CDNG PASS, DNG hash PASS, candidate GPU attempted/replaced frames for the full measured set, and trusted GPU frames when trusted export was requested.",
        "Speed deltas are scoped to this host, clip, receipt, codec set, max-frame count, release/backend hashes, and run order."
    )
}

$lines = [System.Collections.Generic.List[string]]::new()
[void]$lines.Add("# MLVApp Local CUDA Proof Report")
[void]$lines.Add("")
[void]$lines.Add("- Status: $($report.status)")
[void]$lines.Add("- Source summary: $summaryFullPath")
[void]$lines.Add("- Host: $($report.host.name)")
[void]$lines.Add("- NVIDIA: $(if ($nvidiaRows.Count -gt 0) { $nvidiaRows -join '; ' } else { 'not reported' })")
[void]$lines.Add("- Dry run: $dryRun")
[void]$lines.Add("")
[void]$lines.Add("## Artifacts")
[void]$lines.Add("- EXE SHA256: $($artifactEvidence.exeSha256)")
[void]$lines.Add("- CUDA backend SHA256: $($artifactEvidence.backendSha256)")
[void]$lines.Add("- CUDA runtime SHA256: $($artifactEvidence.cudartSha256)")
[void]$lines.Add("")
[void]$lines.Add("## Playback No-Readback")
[void]$lines.Add("- Verdict: $($playbackVerdict.status)")
foreach ($item in @($playbackVerdict.evidence)) {
    [void]$lines.Add("- $item")
}
if ($playbackVerdict.blockers.Count -gt 0) {
    [void]$lines.Add("- Blockers: $($playbackVerdict.blockers -join '; ')")
}
[void]$lines.Add("")
[void]$lines.Add("## Playback Speed")
[void]$lines.Add("- Verdict: $($playbackAbVerdict.status)")
foreach ($item in @($playbackAbVerdict.evidence)) {
    [void]$lines.Add("- $item")
}
if ($playbackAbVerdict.blockers.Count -gt 0) {
    [void]$lines.Add("- Blockers: $($playbackAbVerdict.blockers -join '; ')")
}
[void]$lines.Add("")
[void]$lines.Add("## DNG Export")
[void]$lines.Add("- Verdict: $($cdngVerdict.status)")
foreach ($item in @($cdngVerdict.evidence)) {
    [void]$lines.Add("- $item")
}
if ($cdngVerdict.blockers.Count -gt 0) {
    [void]$lines.Add("- Blockers: $($cdngVerdict.blockers -join '; ')")
}
[void]$lines.Add("")
[void]$lines.Add("## Boundaries")
foreach ($item in @($report.proofBoundary)) {
    [void]$lines.Add("- $item")
}
if ($warnings.Count -gt 0) {
    [void]$lines.Add("")
    [void]$lines.Add("## Warnings")
    foreach ($item in $warnings) {
        [void]$lines.Add("- $item")
    }
}
if ($overallBlockers.Count -gt 0) {
    [void]$lines.Add("")
    [void]$lines.Add("## Not Quotable Yet")
    foreach ($item in @($overallBlockers | Select-Object -Unique)) {
        [void]$lines.Add("- $item")
    }
}
if ($diagnosticList.Count -gt 0) {
    [void]$lines.Add("")
    [void]$lines.Add("## Diagnostic Classification")
    foreach ($diagnostic in $diagnosticList) {
        $line = "- [$($diagnostic.severity)] $($diagnostic.area)/$($diagnostic.code): $($diagnostic.message)"
        if (-not [string]::IsNullOrWhiteSpace([string]$diagnostic.nextAction)) {
            $line += " Next: $($diagnostic.nextAction)"
        }
        [void]$lines.Add($line)
        foreach ($item in @($diagnostic.evidence)) {
            [void]$lines.Add("  - evidence: $item")
        }
    }
}
if ($actionPlan -and @($actionPlan.steps).Count -gt 0) {
    [void]$lines.Add("")
    [void]$lines.Add("## Action Plan")
    [void]$lines.Add("- Status: $($actionPlan.status)")
    if (-not [string]::IsNullOrWhiteSpace([string]$actionPlan.primaryBlocker)) {
        [void]$lines.Add("- Primary blocker: $($actionPlan.primaryBlocker)")
    }
    foreach ($step in @($actionPlan.steps)) {
        [void]$lines.Add("- P$($step.priority) $($step.id): $($step.title)")
        [void]$lines.Add("  - detail: $($step.detail)")
        foreach ($command in @($step.commands)) {
            [void]$lines.Add("  - command: $command")
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$step.proofBoundary)) {
            [void]$lines.Add("  - boundary: $($step.proofBoundary)")
        }
    }
}

$markdown = $lines -join [Environment]::NewLine
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $outputFullPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
        (Resolve-RepoPath -Root $root -Path $Output))
    $outputDir = Split-Path -Parent $outputFullPath
    if ($outputDir) {
        New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    }
    if ($Json) {
        $report | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $outputFullPath -Encoding UTF8
    }
    else {
        $markdown | Set-Content -LiteralPath $outputFullPath -Encoding UTF8
    }
}

if ($Json) {
    $report | ConvertTo-Json -Depth 32
}
else {
    $markdown
}

if ($overallStatus -ne "QUOTABLE_PASS") {
    exit 1
}
