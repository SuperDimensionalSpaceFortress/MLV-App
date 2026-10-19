[CmdletBinding()]
param(
    [string]$RepoRoot = '.',
    [string]$SpecPath = 'tools/gates/output-budget.json',
    [Parameter(Mandatory = $true)][string]$BaselineExe,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$BaselineCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9A-Fa-f]{64}$')][string]$BaselineSha256,
    [Parameter(Mandatory = $true)][string]$CandidateExe,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9A-Fa-f]{64}$')][string]$CandidateSha256,
    [string]$EvidencePath = '',
    [string]$OutputRoot = '.claude-state/profiling/output-budget',
    [switch]$EvidenceOnly
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $RepoRoot).ProviderPath
$resolvedSpec = if ([IO.Path]::IsPathRooted($SpecPath)) { $SpecPath } else { Join-Path $root $SpecPath }
$resolvedOutput = [IO.Path]::GetFullPath((Join-Path $root $OutputRoot))
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $root '.claude-state'))
if (-not $resolvedOutput.StartsWith($allowedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutputRoot must be below the repository .claude-state directory.'
}
[IO.Directory]::CreateDirectory($resolvedOutput) | Out-Null
$evaluationReport = Join-Path $resolvedOutput 'report.json'

function Write-OutputBudgetReportAtomic([object]$Report, [string]$Path) {
    $temporary = "$Path.tmp"
    $Report | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

Write-OutputBudgetReportAtomic -Path $evaluationReport -Report ([ordered]@{
    schema = 'mlvapp.output-budget-report.v1'
    phase = 'started'
    blockingVerdict = 'INDETERMINATE'
    cadenceVerdict = 'INDETERMINATE'
    authorizing = $false
    failures = @('comparison has not completed')
})
$preflightReport = Join-Path $resolvedOutput 'preflight.json'
$python = Join-Path $PSScriptRoot 'output_budget.py'
$pythonCommand = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$pythonPrefix = @('-3')
if ($null -eq $pythonCommand) {
    $pythonCommand = Get-Command python3 -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $pythonPrefix = @()
}
if ($null -eq $pythonCommand) {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $pythonPrefix = @()
}
if ($null -eq $pythonCommand) {
    throw 'Python 3.10+ is required (expected py, python3, or python on PATH).'
}
$pythonExe = $pythonCommand.Source
& $pythonExe @pythonPrefix -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) {
    throw 'The resolved Python interpreter is older than Python 3.10.'
}

& $pythonExe @pythonPrefix $python preflight --spec $resolvedSpec --repo-root $root --output $preflightReport `
    --baseline-exe $BaselineExe --baseline-commit $BaselineCommit --baseline-sha256 $BaselineSha256 `
    --candidate-exe $CandidateExe --candidate-commit $CandidateCommit --candidate-sha256 $CandidateSha256
$preflightExit = $LASTEXITCODE
if ($preflightExit -ne 0) {
    exit $preflightExit
}
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $spec = Get-Content -LiteralPath $resolvedSpec -Raw | ConvertFrom-Json
    $runner = Join-Path $root 'tools\profiling\run-release-gui-smoke.ps1'
    $provenanceHelper = Join-Path $root 'tools\profiling\gui-smoke-screenshot-provenance.ps1'
    . $provenanceHelper
    $pairs = [System.Collections.Generic.List[object]]::new()
    foreach ($clip in @($spec.clips | Where-Object { $_.required })) {
        foreach ($profile in @($spec.profiles)) {
            $accepted = $null
            for ($attempt = 1; $attempt -le 10; ++$attempt) {
                $attemptRoot = Join-Path $resolvedOutput (Join-Path 'runs' (Join-Path $clip.id (Join-Path $profile.id ("attempt-{0}" -f $attempt))))
                [IO.Directory]::CreateDirectory($attemptRoot) | Out-Null
                $results = @{}
                foreach ($leg in @(
                    [pscustomobject]@{ Name = 'baseline'; Exe = $BaselineExe },
                    [pscustomobject]@{ Name = 'candidate'; Exe = $CandidateExe }
                )) {
                    $legRoot = Join-Path $attemptRoot $leg.Name
                    [IO.Directory]::CreateDirectory($legRoot) | Out-Null
                    $resultPath = Join-Path $legRoot 'smoke.json'
                    $runnerArgs = @{
                        RepoRoot = $root
                        ExePath = $leg.Exe
                        ClipPath = [string]$clip.path
                        Output = $resultPath
                        Seconds = 1
                        StartFrame = [int]$clip.startFrame
                        NoLoop = $true
                        SettleMs = 500
                        Threads = '1'
                        QualityMode = [string]$profile.qualityMode
                        ScaleFactor = [string]$profile.scaleFactor
                        ExpectedScaleRequest = [int]$profile.expectedScaleRequest
                        ExpectedQualityMode = [int]$profile.expectedQualityMode
                        ExpectedStretchX = [string]$clip.aspect.stretchX
                        ExpectedStretchY = [string]$clip.aspect.stretchY
                        ExpectedHStretchIndex = [int]$clip.aspect.hStretchIndex
                        ExpectedVStretchIndex = [int]$clip.aspect.vStretchIndex
                        ExpectedAspectMode = [string]$clip.aspect.mode
                        FrameTelemetry = $true
                        CaptureScreenshot = $true
                        RequireFreshScreenshotRender = $true
                        ScreenshotOutputDir = (Join-Path $legRoot 'screenshots')
                        PlaybackProcessing = [string]$profile.playbackProcessing
                        DisableLookAssist = [bool]$profile.disableLookAssist
                        RequireLookAssist = $false
                    }
                    if ([string]$profile.playbackProcessing -eq 'receipt') {
                        # a repo-relative receipt (the tracked fixture) is resolved against the repo root, never the caller's cwd
                        $receiptPath = [string]$spec.receipt.path
                        if (-not [IO.Path]::IsPathRooted($receiptPath)) { $receiptPath = [IO.Path]::GetFullPath((Join-Path $root $receiptPath)) }
                        $runnerArgs.Receipt = $receiptPath
                    }
                    & $runner @runnerArgs | Out-Null
                    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
                        $failureReport = [ordered]@{
                            schema = 'mlvapp.output-budget-report.v1'
                            phase = 'collection'
                            blockingVerdict = 'INDETERMINATE'
                            cadenceVerdict = 'INDETERMINATE'
                            authorizing = $false
                            failures = @("$($leg.Name) smoke failed for $($clip.id)/$($profile.id), attempt $attempt")
                        }
                        Write-OutputBudgetReportAtomic -Report $failureReport -Path $evaluationReport
                        exit 5
                    }
                    $results[$leg.Name] = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
                    $results["$($leg.Name)Path"] = $resultPath
                }
                $pairCheck = Test-GuiSmokeScreenshotPair `
                    -Left $results.baseline.validation.screenshotProvenance `
                    -Right $results.candidate.validation.screenshotProvenance
                if ($pairCheck.validComparable) {
                    $accepted = [pscustomobject]@{
                        clipId = [string]$clip.id
                        profileId = [string]$profile.id
                        baselineResult = [string]$results.baselinePath
                        candidateResult = [string]$results.candidatePath
                    }
                    break
                }
            }
            if ($null -eq $accepted) {
                $failureReport = [ordered]@{
                    schema = 'mlvapp.output-budget-report.v1'
                    phase = 'collection'
                    blockingVerdict = 'INDETERMINATE'
                    cadenceVerdict = 'INDETERMINATE'
                    authorizing = $false
                    failures = @("frame/history alignment exhausted for $($clip.id)/$($profile.id) after 10 complete two-leg attempts")
                }
                Write-OutputBudgetReportAtomic -Report $failureReport -Path $evaluationReport
                exit 5
            }
            $pairs.Add($accepted)
        }
    }
    $evidence = [ordered]@{
        schema = 'mlvapp.output-budget-evidence.v1'
        specSha256 = (Get-FileHash -LiteralPath $resolvedSpec -Algorithm SHA256).Hash
        shippingDefaultsSha256 = [string]$spec.shippingDefaults.sha256
        baseline = [ordered]@{ path = (Resolve-Path -LiteralPath $BaselineExe).ProviderPath; commit = $BaselineCommit; sha256 = $BaselineSha256.ToUpperInvariant() }
        candidate = [ordered]@{ path = (Resolve-Path -LiteralPath $CandidateExe).ProviderPath; commit = $CandidateCommit; sha256 = $CandidateSha256.ToUpperInvariant() }
        pairs = @($pairs)
        cadence = $null
    }
    $EvidencePath = Join-Path $resolvedOutput 'evidence.json'
    $evidence | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $EvidencePath -Encoding utf8NoBOM
}
& $pythonExe @pythonPrefix $python evaluate --spec $resolvedSpec --evidence $EvidencePath --output $evaluationReport
$evaluationExit = $LASTEXITCODE
if ($EvidenceOnly -and $evaluationExit -eq 0) {
    $report = Get-Content -LiteralPath $evaluationReport -Raw | ConvertFrom-Json
    $report.authorizing = $false
    $report.evidenceOnly = $true
    Write-OutputBudgetReportAtomic -Report $report -Path $evaluationReport
    exit 2
}
exit $evaluationExit
