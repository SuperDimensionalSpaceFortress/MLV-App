<#
.SYNOPSIS
EXPORT-VENUE-LEG-1: generate ONE self-contained venue job that runs a headless CPU batch export of an owner clip and measures its per-stage cost.

.DESCRIPTION
The dual-venue chain's export leg (tools/profiling/dual-venue/Invoke-VenueLeg.ps1, legType "export") calls this generator the way a playback leg calls
playback-attr-3-cuda-job.ps1. It writes one <jobId>.job.ps1 that um-run.ps1 drops on the venue's agent share. The emitted job:
  * sweeps and creates its own work dir under the venue scratch root (Assert-UnderMlvTmp), verifies the staged build against the authenticated build
    manifest (packageZip, exe and DLL sha256), and verifies the batch receipt it carries against the leg's receiptSha256;
  * verifies every owner part by length, builds its private neutral-named views (symlink or verified copy, never a hard link) and hashes each view once;
  * applies gui-smoke-length-gate.ps1 to the clip (-ClipOnly: an export plays nothing; playSeconds is the clip-length floor) and refuses a clip shorter
    than maxFrames;
  * refuses EXPORT_TOOLING_ABSENT for a ProRes leg whose staged package has no ffmpeg.exe, then samples CPU quiescence (VENUE_NOT_QUIESCENT);
  * runs the staged MLVApp.exe --batch headless (QT_QPA_PLATFORM=windows, as run-release-cdng-export-profile.ps1 does) inside a bounded wait that kills
    the process tree on expiry, with the export stage profiler (CDNG lossless) or MLVAPP_STAGE_TIMING (ProRes);
  * classifies the run (EXPORT_CAPTURED, EXPORT_EXIT_NONZERO, EXPORT_TIMEOUT, EXPORT_KILL_UNCONFIRMED, EXPORT_NO_FRAMES, EXPORT_FRAMES_SHORT, EXPORT_PROFILE_ABSENT,
    EXPORT_OUTPUT_MISMATCH), publishes ONLY batch.log, export-profile.json or stage-timing.log, output-manifest.json and summary.json, and DELETES the
    exported frames / .mov from its venue-local work dir after the manifest. Exported footage never reaches the share.

Nothing here is copied from the playback generator. Its resolver (Resolve-AttrCudaOwnerClipParts, Resolve-AttrCudaOwnerPython), its venue-table reader
(Get-DualVenueEntry) and its owner-part path allowlist are loaded from playback-attr-3-cuda-job.ps1's own AST at run time. The job-side conventions
(Assert-UnderMlvTmp, Save-Json, Write-JobTrace, Get-Sha, Get-Mean) are extracted from that generator's job template. The AttrCuda helpers are embedded
verbatim from AttrCudaArtifacts.psm1 / AttrCudaOwnerFootage.psm1 by Get-AttrCudaEmbeddedFunctionSource, like every other generator. -DescribeTemplate
returns the template and the embedded names (no clip, no git): the static tests use it.
#>
[CmdletBinding()]
param(
    [switch]$DescribeTemplate,
    [ValidatePattern('^[0-9a-f]{40}\z')][string]$SourceCommit,
    [ValidatePattern('^[0-9a-f]{64}\z')][string]$BuildManifestSha256,
    # an owner clip id only: an export leg never runs a fixture (the fixtures are 2 and 16 frames)
    [ValidatePattern('^[A-Za-z]\d{2}-\d{3,4}\z')][string]$ClipId,
    [ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'bachelor',
    [ValidateSet('cpu')][string]$Backend = 'cpu',
    [ValidateSet('cdng-lossless', 'prores')][string]$Format,
    [ValidateRange(24, 100000)][int]$MaxFrames = 0,
    [string]$ReceiptPath,
    # r20 (adversary pass 24 A2): the leg spec's committed export.receiptSha256. The bytes this generator reads from -ReceiptPath (the ones it embeds) must hash to it, or the
    # typed DUAL_VENUE_EXPORT_RECEIPT_CHANGED refusal comes before any job is written: a receipt file replaced after the leg wrote it never becomes an export. Required to generate.
    [ValidatePattern('^[0-9a-f]{64}\z')][string]$ExpectReceiptSha256,
    [bool]$DualIsoExpected = $false,
    [ValidateRange(10, 3600)][int]$PlaySeconds = 20,
    # the leg schema's range, exactly: (0, 100] (adversary r1 A5: ValidateRange(0.1, 100) refused a schema-valid 0.05)
    [ValidateScript({ $_ -gt 0 -and $_ -le 100 })][double]$CpuQuiescenceThresholdPercent = 20,
    # test seam (adversary r1 A1): print the time budget for a clip of this many bytes and -MaxFrames, then stop (no clip, no git)
    [int64]$DescribeTimeBudgetBytes = -1,
    # r8 (adversary r7 A3): resolve the clip and print the job's time budget (or throw its typed DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED refusal), then stop: no receipt, build,
    # git or output file is read. The lease batch calls this BEFORE any claim or lease, so a window the agent would refuse is refused where no lease is spent on it.
    [switch]$PreflightBudgetOnly,
    [string]$OutFile,
    [string]$RepoRoot = '',
    [string]$VenueTablePath = ''
)

$ErrorActionPreference = 'Stop'
$PlaybackGenerator = Join-Path $PSScriptRoot 'playback-attr-3-cuda-job.ps1'
$LengthGatePath = Join-Path $PSScriptRoot '..\gui-smoke-length-gate.ps1'
$ArtifactsModule = Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1'
$OwnerFootageModule = Join-Path $PSScriptRoot 'AttrCudaOwnerFootage.psm1'
# EXPORT-RENDER-PIPE-TIMING-2 r4 (sol r3 B2): the central reader's one batch.log line grammar, embedded from the module that owns it (Get-BatchLineGrammarText)
$DualVenueModule = Join-Path $PSScriptRoot '..\dual-venue\DualVenueRunner.psm1'
# (EXPORT-RENDER-PIPE-TIMING-3 r1, sol r4 B4: and the reader's ONE binding verdict, Get-DvBatchExportBindingProblems, with the declaration reader and the tables they read)
$BatchLineGrammarTables = @('$script:ExportBatchLineShapes', '$script:ExportBatchLineGrammar', '$script:ExportBatchLineForbidden', '$script:ExportRenderStartCodec',
    '$script:ExportRenderStartContainer', '$script:ExportRenderStartFps', '$script:ExportRenderStartFpsMin', '$script:ExportRenderStartFpsMax', '$script:ExportCdngFrameLeaf')
$BatchLineGrammarFunctions = @('ConvertFrom-DvBatchLogLine', 'ConvertFrom-DvBatchExportLog', 'Get-DvRenderStartDeclaration', 'Get-DvBatchExportBindingProblems')
# The job-side conventions the playback job template defines, extracted from that template (never re-typed here).
$SharedTemplateFunctions = @('Assert-UnderMlvTmp', 'Save-Json', 'Write-JobTrace', 'Get-Sha', 'Get-Mean')
# The AttrCuda helpers the job calls directly; their callees are added by closure below.
$SeedEmbedded = @('Assert-AttrCudaBuildManifest', 'Get-AttrCudaBuildManifestClaimSha256', 'Publish-AttrCudaText', 'Publish-AttrCudaBytes', 'Publish-AttrCudaFileCopy',
    'New-AttrCudaDirectory', 'Assert-AttrCudaNoLinkBelowRoot', 'Remove-AttrCudaTree', 'Initialize-AttrCudaFileIdNative', 'Read-AttrCudaBase64Payload', 'Add-AttrCudaTraceLine',
    'Get-AttrCudaFileSha256Blocks', 'Test-AttrCudaFootagePart', 'ConvertTo-AttrCudaUtf8String', 'Get-AttrCudaQuiescenceSample', 'Get-AttrCudaProcessCpuSnapshot',
    'Get-AttrCudaTopCpuProcesses', 'Get-AttrCudaMeasurementVenue', 'ConvertTo-AttrCudaQuotedProcessArgument',
    'Get-AttrCudaOwnerFootageNeutralName', 'Assert-AttrCudaOwnerPartsNaming', 'New-AttrCudaFileSymlink', 'Test-AttrCudaSymlinkCapability', 'Open-AttrCudaReadOnlyHandle',
    'Assert-AttrCudaOwnerFootageCopySpace', 'New-AttrCudaOwnerFootageView', 'Assert-AttrCudaOwnerFootageViewsIntact', 'Clear-AttrCudaOwnerFootageLeftovers',
    'Close-AttrCudaOwnerFootageWorkspace')

# --- the job body (placeholders are substituted in ONE pass by Expand-AttrCudaTemplate) --------------------------------------------------------------------------
$template = @'
$ErrorActionPreference = 'Stop'
# EXPORT-VENUE-LEG-1 export-stage job (tools/profiling/bachelor/export-stage-job.ps1). Runs on the venue; carries no path of the owner's footage as text.
$SourceCommit = '__SOURCE_COMMIT__'
$BuildManifestSha256 = '__BUILD_MANIFEST_SHA256__'
$ClipId = '__CLIP_ID__'
$OwnerPartsJson = '__OWNER_PARTS_JSON__'
$ExeName = '__EXE_NAME__'
$ReconName = '__RECON_NAME__'
$BasePackageZip = '__BASE_PACKAGE_ZIP__'
$BasePackageExeName = 'MLVApp.exe'
$Format = '__FORMAT__'
$MaxFrames = __MAX_FRAMES__
$DualIsoExpected = __DUAL_ISO_EXPECTED__
$ReceiptBase64 = '__RECEIPT_BASE64__'
$ReceiptSha256 = '__RECEIPT_SHA256__'
$LengthGateBase64 = '__LENGTH_GATE_BASE64__'
$PlaySeconds = __PLAY_SECONDS__
$ExportTimeoutSec = __EXPORT_TIMEOUT_SEC__
$DeclaredVenue = '__DECLARED_VENUE__'
$cpuThresholdPercent = __CPU_QUIESCENCE_THRESHOLD_PERCENT__
$Root = '__AGENT_ROOT__'
$Cache = Join-Path $Root 'cache'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$JobId = "export-stage-$($SourceCommit.Substring(0,12))-$ClipId-$Format-$Stamp"
$Work = Join-Path '__SCRATCH_ROOT__' $JobId
$Pub = Join-Path $Root "outbox\$JobId.artifacts"
$Trace = Join-Path $Root "logs\$JobId.trace.txt"

# --- embedded VERBATIM from AttrCudaArtifacts.psm1 / AttrCudaOwnerFootage.psm1 ------------------------------------------------------------------------------------
__EMBEDDED_FUNCTIONS__
# --- the playback job template's own conventions, extracted from playback-attr-3-cuda-job.ps1 ---------------------------------------------------------------------
__SHARED_FUNCTIONS__
# --- the batch.log line grammar, extracted VERBATIM from tools/profiling/dual-venue/DualVenueRunner.psm1 (EXPORT-RENDER-PIPE-TIMING-2 r4, sol r3 B2) ---------------
__BATCH_LINE_GRAMMAR__
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------

foreach ($check in @(@{ path = $Root; label = 'Root' }, @{ path = $Work; label = 'Work' }, @{ path = $Pub; label = 'Pub' })) { Assert-UnderMlvTmp $check.path $check.label }
Write-JobTrace "export job start id=$JobId commit=$($SourceCommit.Substring(0,12)) clip=$ClipId format=$Format maxFrames=$MaxFrames"

function Write-ExportSummary([string]$Result, $Extra) {
    $s = [ordered]@{
        schema = 'export-stage-job.v1'; result = $Result; legType = 'export'; format = $Format; maxFrames = $MaxFrames; dualIsoExpected = $DualIsoExpected
        clipId = $ClipId; backend = 'cpu'; declaredVenue = $DeclaredVenue; venue = (Get-AttrCudaMeasurementVenue); sourceCommit = $SourceCommit
        buildManifestSha256 = $BuildManifestSha256; receiptSha256 = $ReceiptSha256; fixtureRehearsal = $false; artifactRoot = $Pub
    }
    if ($null -ne $Extra) { foreach ($k in @($Extra.Keys)) { $s[$k] = $Extra[$k] } }
    Save-Json $s (Join-Path $Pub 'summary.json')
}

$OwnerClipDir = $null
$ownerViewHandles = [System.Collections.Generic.List[object]]::new()
$ownerViews = [System.Collections.Generic.List[object]]::new()
$OwnerJournal = Join-Path $Work '.attrcuda-owned.jsonl'
[void](Assert-AttrCudaNoLinkBelowRoot -TrustedRoot '__SCRATCH_ROOT__' -Path (Join-Path $Work 'owner-clip'))
$ownerLeftovers = @(Clear-AttrCudaOwnerFootageLeftovers -Directory (Join-Path $Work 'owner-clip') -Journal $OwnerJournal)
$workSweep = Remove-AttrCudaTree -TrustedRoot '__SCRATCH_ROOT__' -Path $Work -OwnedJournal $OwnerJournal
[void](New-AttrCudaDirectory -Path (Join-Path $Root 'outbox'))
[void](New-AttrCudaDirectory -Path $Pub)
if ($ownerLeftovers.Count -gt 0 -or $workSweep.Left.Count -gt 0) {
    Write-ExportSummary 'OWNER_WORK_NOT_CLEAN' @{ left = ($ownerLeftovers.Count + $workSweep.Left.Count) }
    Write-Output "RESULT=OWNER_WORK_NOT_CLEAN ARTIFACTS=$Pub"
    exit 28
}
New-Item -ItemType Directory -Path $Work -Force | Out-Null
$Scratch = Join-Path $Work '.job-tmp'
New-Item -ItemType Directory -Path $Scratch -Force | Out-Null
$env:TEMP = $Scratch
$env:TMP = $Scratch
$outDir = Join-Path $Work 'out'
$pkgRoot = Join-Path $Work 'pkg'
Assert-UnderMlvTmp $outDir 'out'
Assert-UnderMlvTmp $pkgRoot 'pkg'

function Remove-ExportOutput {
    # The exported frames / .mov are derived from the owner's footage: they stay on the venue and are deleted here, never published. The out dir is a
    # journalled fresh root (New-AttrCudaOwnedRoot), so the delete is by proof (Remove-AttrCudaTree), never a recursive pathname delete.
    $sweep = Remove-AttrCudaTree -TrustedRoot '__SCRATCH_ROOT__' -Path $outDir -OwnedJournal $OwnerJournal
    [bool]($sweep.TreeRemoved -and @($sweep.Left).Count -eq 0)
}

try {
    # --- the staged build, authenticated by the manifest sha256 baked in at generation -----------------------------------------------------------------------------
    $buildManifestPath = Join-Path $Cache "playback-attr-3-cuda-$($SourceCommit.Substring(0,12))-build.json"
    $buildManifest = Assert-AttrCudaBuildManifest -Path $buildManifestPath -ExpectedSha256 $BuildManifestSha256 -ExpectedSourceCommit $SourceCommit
    $manifestChecks = @(
        @{ label = 'packageZip'; path = (Join-Path $Cache $BasePackageZip); expectedSha = (Get-AttrCudaBuildManifestClaimSha256 $buildManifest 'packageZip') },
        @{ label = 'exe'; path = (Join-Path $Cache $ExeName); expectedSha = (Get-AttrCudaBuildManifestClaimSha256 $buildManifest 'exe') },
        @{ label = 'dll'; path = (Join-Path $Cache $ReconName); expectedSha = (Get-AttrCudaBuildManifestClaimSha256 $buildManifest 'dll') }
    )
    $manifestHashes = @{}
    foreach ($check in $manifestChecks) {
        if (-not (Test-Path -LiteralPath $check.path)) { throw "cache missing $($check.label)" }
        if ([string]::IsNullOrWhiteSpace($check.expectedSha)) { throw "build manifest is missing a sha256 for $($check.label)" }
        $manifestHashes[$check.label] = Get-Sha $check.path "manifest-$($check.label)-hash"
        if ($manifestHashes[$check.label] -ne $check.expectedSha.ToUpperInvariant()) { throw "hash mismatch (vs build manifest) for $($check.label)" }
    }
    [void](Assert-AttrCudaBuildManifest -Path $buildManifestPath -ExpectedSha256 $BuildManifestSha256 -ExpectedSourceCommit $SourceCommit)
    Write-JobTrace 'step build-manifest-artifacts-verified'

    # --- the batch receipt this job carries, bound to the leg's receiptSha256 -----------------------------------------------------------------------------------------
    $receiptBytes = [Convert]::FromBase64String($ReceiptBase64)
    $receiptHasher = [System.Security.Cryptography.SHA256]::Create()
    try { $receiptActual = ([BitConverter]::ToString($receiptHasher.ComputeHash($receiptBytes)) -replace '-', '').ToLowerInvariant() } finally { $receiptHasher.Dispose() }
    if ($receiptActual -cne $ReceiptSha256) { throw 'EXPORT_RECEIPT_MISMATCH the carried batch receipt does not hash to the leg''s receiptSha256' }
    $receiptLocal = Join-Path $Work 'export-receipt.marxml'
    [void](Publish-AttrCudaBytes -Path $receiptLocal -Bytes $receiptBytes -OwnedJournal $OwnerJournal)

    # --- the owner footage: verified parts, private neutral-named views (the playback job's own sequence) ---------------------------------------------------------
    Write-JobTrace 'step footage-verify start'
    $ownerRawParts = @($OwnerPartsJson | ConvertFrom-Json)
    $ownerPartResults = [System.Collections.Generic.List[object]]::new()
    $ownerDecodedParts = [System.Collections.Generic.List[object]]::new()
    foreach ($rawPart in ($ownerRawParts | Sort-Object { [int]$_.index })) {
        $decoded = Read-AttrCudaBase64Payload -Base64 $rawPart.pathBase64
        $partPath = ConvertTo-AttrCudaUtf8String -Bytes $decoded.bytes
        $status = Test-AttrCudaFootagePart -Path $partPath -ExpectedLength ([int64]$rawPart.length) -ExpectedSha256 ([string]$rawPart.sha256) -LengthOnly
        Write-JobTrace "footage part=$($rawPart.index) screen status=$status length=$($rawPart.length)"
        [void]$ownerPartResults.Add([ordered]@{ index = [int]$rawPart.index; status = $status })
        [void]$ownerDecodedParts.Add([ordered]@{ index = [int]$rawPart.index; path = $partPath; length = [int64]$rawPart.length; sha256 = [string]$rawPart.sha256 })
    }
    if (@($ownerPartResults | Where-Object { $_.status -ne 'PASS' }).Count -gt 0) {
        Write-ExportSummary 'OWNER_FOOTAGE_NOT_VERIFIED' @{ parts = @($ownerPartResults) }
        Write-Output "RESULT=OWNER_FOOTAGE_NOT_VERIFIED ARTIFACTS=$Pub"
        exit 19
    }
    try { $ownerAssertedParts = Assert-AttrCudaOwnerPartsNaming -Parts $ownerDecodedParts }
    catch {
        Write-ExportSummary 'OWNER_PARTS_NOT_CONTIGUOUS' @{ partCount = $ownerDecodedParts.Count }
        Write-Output "RESULT=OWNER_PARTS_NOT_CONTIGUOUS ARTIFACTS=$Pub"
        exit 20
    }
    $OwnerClipDir = New-AttrCudaDirectory -Path (Join-Path $Work 'owner-clip')
    $ownerViewCapability = Test-AttrCudaSymlinkCapability -Directory $OwnerClipDir -Journal $OwnerJournal
    $ownerViewMode = if ($ownerViewCapability.Capable) { 'symlink' } else { 'copy' }
    Write-JobTrace "footage view mode=$ownerViewMode capability=$($ownerViewCapability.Result)"
    $clipPath = $null
    $part = $null
    try {
        if ($ownerViewMode -eq 'copy') { Assert-AttrCudaOwnerFootageCopySpace -Directory $OwnerClipDir -Parts @($ownerAssertedParts) }
        foreach ($part in $ownerAssertedParts) {
            $pin = Open-AttrCudaReadOnlyHandle -Path $part.path
            [void]$ownerViewHandles.Add($pin)
            $view = New-AttrCudaOwnerFootageView -Directory $OwnerClipDir -Index $part.index -SourcePath $part.path -PinStream $pin -Mode $ownerViewMode -Journal $OwnerJournal
            [void]$ownerViews.Add($view)
            [void]$ownerViewHandles.Add($view.ViewStream)
            $relinkStatus = Test-AttrCudaFootagePart -Path $view.Path -ExpectedLength $part.length -ExpectedSha256 $part.sha256 -TracePath $Trace -TraceLabel "footage-part$($part.index)-identity-hash"
            Write-JobTrace "footage part=$($part.index) identity status=$relinkStatus"
            if ($relinkStatus -ne 'PASS') { throw "OWNER_FOOTAGE_NOT_VERIFIED status=$relinkStatus" }
            if ($part.index -eq 0) { $clipPath = $view.Path }
        }
        Assert-AttrCudaOwnerFootageViewsIntact -Views @($ownerViews)
    } catch {
        Close-AttrCudaOwnerFootageWorkspace -Handles $ownerViewHandles -Views @($ownerViews)
        $OwnerClipDir = $null
        $viewToken = ([string]$_.Exception.Message -split '\s+')[0]
        if ($viewToken -notin @('OWNER_FOOTAGE_NOT_VERIFIED', 'OWNER_FOOTAGE_VIEW_NO_SPACE', 'OWNER_FOOTAGE_VIEW_CHANGED')) { $viewToken = 'OWNER_FOOTAGE_VIEW_FAILED' }
        $viewExit = switch ($viewToken) { 'OWNER_FOOTAGE_NOT_VERIFIED' { 19 } 'OWNER_FOOTAGE_VIEW_NO_SPACE' { 21 } 'OWNER_FOOTAGE_VIEW_CHANGED' { 24 } default { 22 } }
        Write-ExportSummary $viewToken @{ viewMode = $ownerViewMode; partIndex = $(if ($null -ne $part) { $part.index } else { -1 }) }
        Write-Output "RESULT=$viewToken ARTIFACTS=$Pub"
        exit $viewExit
    }
    Write-JobTrace 'step footage-verify done'

    # --- the clip-length gate (the smoke runner's own gate file, dot-sourced): an export plays nothing, so -ClipOnly; and the clip must hold maxFrames -----------
    $lengthGateLocal = Join-Path $Work 'gui-smoke-length-gate.ps1'
    [void](Publish-AttrCudaBytes -Path $lengthGateLocal -Bytes ([Convert]::FromBase64String($LengthGateBase64)) -OwnedJournal $OwnerJournal)
    . $lengthGateLocal
    $lengthGate = Test-GuiSmokeClipLength -Path $clipPath -WindowSeconds $PlaySeconds -ClipOnly
    $lengthGateRecord = [ordered]@{ verdict = [string]$lengthGate.verdict; frames = $lengthGate.frames; fps = $lengthGate.fps; playSeconds = $PlaySeconds; maxFrames = $MaxFrames }
    $lengthRefusal = $null
    if ([string]$lengthGate.verdict -ne 'OK') { $lengthRefusal = [string]$lengthGate.verdict }
    elseif ($null -eq $lengthGate.frames -or [int64]$lengthGate.frames -lt $MaxFrames) { $lengthRefusal = 'CLIP_TOO_SHORT' }
    if ($null -ne $lengthRefusal) {
        Write-ExportSummary $lengthRefusal @{ smokeRefusalReason = $lengthRefusal; lengthGate = $lengthGateRecord }
        Write-Output "RESULT=$lengthRefusal ARTIFACTS=$Pub"
        exit 47
    }

    # --- the staged package -------------------------------------------------------------------------------------------------------------------------------------
    Write-JobTrace 'step package-expand start'
    $pkgRoot = (New-AttrCudaOwnedRoot -TrustedRoot '__SCRATCH_ROOT__' -Path $pkgRoot -OwnedJournal $OwnerJournal).Path
    Expand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip) -DestinationPath $pkgRoot -Force
    $baseExe = Get-ChildItem -LiteralPath $pkgRoot -Recurse -Filter $BasePackageExeName | Select-Object -First 1
    if (-not $baseExe) { throw "base package executable not found: $BasePackageExeName" }
    $pkgDir = $baseExe.Directory.FullName
    $exePath = Join-Path $pkgDir $ExeName
    $reconDll = Join-Path $pkgDir 'igpu_recon_cuda.dll'
    [void](Publish-AttrCudaFileCopy -Source (Join-Path $Cache $ExeName) -Destination $exePath)
    [void](Publish-AttrCudaFileCopy -Source (Join-Path $Cache $ReconName) -Destination $reconDll)
    if ((Get-Sha $exePath 'deployed-exe-hash') -ne $manifestHashes['exe'] -or (Get-Sha $reconDll 'deployed-dll-hash') -ne $manifestHashes['dll']) { throw 'deployed artifact hash verification failed' }
    $ffmpegPath = Join-Path $pkgDir 'ffmpeg.exe'
    $ffmpegPresent = Test-Path -LiteralPath $ffmpegPath -PathType Leaf
    Write-JobTrace "step package-expand done ffmpegPresent=$ffmpegPresent"
    if ($Format -eq 'prores' -and -not $ffmpegPresent) {
        Write-ExportSummary 'EXPORT_TOOLING_ABSENT' @{ ffmpegPresent = $false; executableSha256 = $manifestHashes['exe']; lengthGate = $lengthGateRecord }
        Write-Output "RESULT=EXPORT_TOOLING_ABSENT ARTIFACTS=$Pub"
        exit 45
    }

    # --- CPU quiescence (the playback job's gate: busy TIME, three samples, fail closed) ------------------------------------------------------------------------------
    Write-JobTrace 'step quiescence-check start'
    $cpuProcessBefore = Get-AttrCudaProcessCpuSnapshot
    $cpuUtilitySamples = @(); $cpuTimeSamples = @(); $cpuTimeUnknownReason = $null
    for ($i = 0; $i -lt 3; $i++) {
        $sample = Get-AttrCudaQuiescenceSample
        $cpuUtilitySamples += $sample.utilityPercent
        if ($null -eq $sample.timePercent) { if ($null -eq $cpuTimeUnknownReason) { $cpuTimeUnknownReason = $sample.timePercentError } } else { $cpuTimeSamples += $sample.timePercent }
        if ($i -lt 2) { Start-Sleep -Seconds 12 }
    }
    $cpuProcessAfter = Get-AttrCudaProcessCpuSnapshot
    $topCpuProcesses = @(Get-AttrCudaTopCpuProcesses -Before $cpuProcessBefore -After $cpuProcessAfter -Count 10)
    $avgUtility = Get-Mean ($cpuUtilitySamples | Where-Object { $null -ne $_ })
    $cpuTimeUnknown = $cpuTimeSamples.Count -lt 3
    $avgTime = if ($cpuTimeUnknown) { $null } else { Get-Mean $cpuTimeSamples }
    # r2: the playback summary's own field names (utilityMeanPercent / timeMeanPercent / timeUnknown / thresholdPercent / pass): the leg batch reads cpuQuiescence.timeMeanPercent
    # (Get-VenueLegPreLoadMean); the first export spelling (cpuTimeMean) read UNKNOWN on every leg.
    $quiescence = [ordered]@{ utilitySamples = $cpuUtilitySamples; utilityMeanPercent = $avgUtility; timeSamples = $cpuTimeSamples; timeMeanPercent = $avgTime
                              timeUnknown = $cpuTimeUnknown; timeUnknownReason = $cpuTimeUnknownReason; thresholdPercent = $cpuThresholdPercent
                              pass = (-not $cpuTimeUnknown -and $avgTime -le $cpuThresholdPercent); topCpuProcesses = $topCpuProcesses }
    Write-JobTrace "step quiescence-check done cpuTimeMean=$avgTime cpuTimeUnknown=$cpuTimeUnknown"
    if ($cpuTimeUnknown -or -not ($avgTime -le $cpuThresholdPercent)) {
        Write-ExportSummary 'VENUE_NOT_QUIESCENT' @{ cpuQuiescence = $quiescence; executableSha256 = $manifestHashes['exe'] }
        $cpuTimeField = if ($cpuTimeUnknown) { 'unknown' } else { $avgTime }
        Write-Output "RESULT=VENUE_NOT_QUIESCENT CPU_TIME=$cpuTimeField CPU_UTILITY=$avgUtility ARTIFACTS=$Pub"
        exit 12
    }

    # --- the export: MLVApp.exe --batch, headless, one bounded wait -----------------------------------------------------------------------------------------------------
    $outDir = (New-AttrCudaOwnedRoot -TrustedRoot '__SCRATCH_ROOT__' -Path $outDir -OwnedJournal $OwnerJournal).Path
    $batchLog = Join-Path $Work 'batch.log'
    $profilePath = Join-Path $Work 'export-profile.json'
    $stageLog = Join-Path $Work 'stage-timing.log'
    $appArgs = @('--batch', '-i', $clipPath, '-o', $outDir, '-r', $receiptLocal)
    if ($Format -eq 'cdng-lossless') { $appArgs += @('--cdng-codec', 'lossless') }
    else { $appArgs += @('--export-format', 'rendered', '--rendered-codec', 'prores', '--rendered-container', 'mov', '--rendered-ffmpeg', $ffmpegPath) }
    $appArgs += @('--max-frames', [string]$MaxFrames, '--log', $batchLog)
    $psi = [Diagnostics.ProcessStartInfo]::new($exePath, (($appArgs | ForEach-Object { ConvertTo-AttrCudaQuotedProcessArgument $_ }) -join ' '))
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $pkgDir
    # r2 (adversary A1): the child inherits the venue agent's environment; an MLVAPP_GPU_EXPORT=1 there made the 'cpu' leg a GPU export under a cpu label. Every inherited
    # MLVAPP_* variable is removed before this job sets its own below, and the names removed are published in the summary.
    $mlvappEnvScrubbed = @()
    foreach ($envKey in @($psi.Environment.Keys | Where-Object { $_ -like 'MLVAPP_*' })) { [void]$psi.Environment.Remove($envKey); $mlvappEnvScrubbed += [string]$envKey }
    $psi.Environment['QT_QPA_PLATFORM'] = 'windows'
    $psi.Environment['QT_QPA_PLATFORM_PLUGIN_PATH'] = (Join-Path $pkgDir 'platforms')
    $psi.Environment['QT_PLUGIN_PATH'] = $pkgDir
    if ($Format -eq 'cdng-lossless') {
        $psi.Environment['MLVAPP_EXPORT_STAGE_PROFILER'] = '1'
        $psi.Environment['MLVAPP_EXPORT_STAGE_PROFILE_FILE'] = $profilePath
        $psi.Environment['MLVAPP_EXPORT_STAGE_PROFILE_BUILD_ID'] = $SourceCommit
    } else {
        $psi.Environment['MLVAPP_STAGE_TIMING'] = '1'
        $psi.Environment['MLVAPP_STAGE_TIMING_FILE'] = $stageLog
    }
    Write-JobTrace "step export start format=$Format maxFrames=$MaxFrames timeoutSec=$ExportTimeoutSec"
    # A1-CLOCK-FREE-ANCESTRY-1 r1: the identities (pid + creation stamp) of every process alive BEFORE the export root starts. The kill confirmation below calls only those, and what they
    # started directly, outsiders: no clock ordering decides ancestry. No listing ($null) is an unknown tree: an unconfirmed kill, never a confirmed one.
    # sol A4 on 91e548f8: a helper the job does not carry (its source could not be read while the job was generated) is an unknown tree and an unconfirmed kill, never an abort before the
    # root starts or before the typed terminal result (every call below is guarded).
    $exportBaseline = $null
    try { $exportBaseline = Get-AttrCudaProcessTreeSnapshot -BaselineOnly -OperationTimeoutSec 10 } catch { Write-JobTrace "step export baseline listing unavailable error=$([string]$_.CategoryInfo.Reason)" }
    $exportWatch = [Diagnostics.Stopwatch]::StartNew()
    $app = [Diagnostics.Process]::Start($psi)
    $timedOut = -not $app.WaitForExit([int]($ExportTimeoutSec * 1000))
    # r8 (adversary r7 pass A2): the kill is CONFIRMED or it is typed. A tree kill that threw (access denied: ffmpeg or a child may still hold the output), a fallback root-only
    # kill (the descendants are unknown) or a root still running after 30 s used to be swallowed: the job then read changing output, tried to delete it and published an ordinary
    # EXPORT_TIMEOUT. Now any of them ends EXPORT_KILL_UNCONFIRMED (exit 48, an INVALID export: no measurement and no FAIL claim about the product).
    # EXPORT-KILL-DESCENDANT-CONFIRM-1 (adversary pass 26 A1 on 559536d7): Kill($true) may return and the root's wait read true while a descendant (ffmpeg) is still alive, so
    # the root alone confirmed the tree. The descendants are recorded BEFORE the kill (pid + creation time: a reused pid is no survivor) and each must be seen gone within 15 s
    # after the root's wait. A survivor, or a tree that could not be read, leaves the kill unconfirmed (EXPORT_KILL_UNCONFIRMED, exit 48) and the survivors are traced.
    $killConfirmed = $true
    if ($timedOut) {
        $exportTree = try { Get-AttrCudaProcessTreeSnapshot -RootProcessId $app.Id -Baseline $exportBaseline } catch { $null }
        try { $app.Kill($true) } catch { $killConfirmed = $false; Write-JobTrace "step export kill tree error=$([string]$_.CategoryInfo.Reason)"; try { $app.Kill() } catch { Write-JobTrace "step export kill root error=$([string]$_.CategoryInfo.Reason)" } }
        if (-not $app.WaitForExit(30000)) { $killConfirmed = $false }
        $exportTreeGone = try { Wait-AttrCudaProcessTreeGone -Snapshot $exportTree -WaitMs 15000 } catch { [pscustomobject]@{ confirmed = $false; survivors = @(); error = 'tree-unknown: helper unavailable' } }
        if (-not $exportTreeGone.confirmed) { $killConfirmed = $false; Write-JobTrace "step export kill descendants unconfirmed reason=$($exportTreeGone.error) survivors=$(@($exportTreeGone.survivors) -join ',')" }
    }
    $exportWatch.Stop()
    $appExitCode = if ($timedOut) { $null } else { $app.ExitCode }
    Write-JobTrace "step export done timedOut=$timedOut exit=$appExitCode seconds=$([math]::Round($exportWatch.Elapsed.TotalSeconds, 1))"

    # --- classify from the app's own records -----------------------------------------------------------------------------------------------------------------------
    # r8 fix 3 (adversary r8 pass 10 A3): a survivor of an unconfirmed kill may hold the log (or the stage record) without read sharing. Reading or copying it must never cost the typed
    # EXPORT_KILL_UNCONFIRMED terminal, so the read is guarded (an unreadable log is no log: the classification below decides from the timeout first) and each publish copy is best effort.
    $logText = try { if (Test-Path -LiteralPath $batchLog -PathType Leaf) { [IO.File]::ReadAllText($batchLog) } else { '' } } catch { Write-JobTrace "step export batch.log read error=$([string]$_.CategoryInfo.Reason)"; '' }
    # EXPORT-RENDER-PIPE-TIMING-2 r4 (sol r3 B2): DONE and RENDER_VERIFIED are read by the central reader's ONE line grammar (embedded above, never re-typed), so a line
    # that reader refuses (a truncated verification, a field after the trailer, a count past u15, two DONE triplets) is no export here either, typed, never a throw
    $batchRead = ConvertFrom-DvBatchExportLog -BatchLogText $logText
    $exported = if ($null -eq $batchRead.done.problem -and $null -ne $batchRead.done.fields) { [int64]$batchRead.done.fields['exported'] } else { $null }
    $verifiedFrames = if ($null -eq $batchRead.verified.problem -and $null -ne $batchRead.verified.fields) { [int64]$batchRead.verified.fields['decoded_frames'] } else { $null }
    $stageEvidence = if ($Format -eq 'cdng-lossless') { $profilePath } else { $stageLog }
    # (adversary r11 A3: the same best-effort rule for the stage record's size: a survivor of an unconfirmed kill can remove or lock it between Test-Path and Get-Item, which threw into the finally
    # and cost the typed terminal; an unreadable record is an absent one, and the classification below decides from the timeout first)
    $stagePresent = try { (Test-Path -LiteralPath $stageEvidence -PathType Leaf) -and ((Get-Item -LiteralPath $stageEvidence -ErrorAction Stop).Length -gt 0) } catch { Write-JobTrace "step export stage record size error=$([string]$_.CategoryInfo.Reason)"; $false }
    $outputs = @()
    # r8 fix 2 (adversary r8 A2): with the tree NOT confirmed dead a survivor (ffmpeg) may still hold the .mov without read sharing, and hashing it threw into the finally before any
    # result was typed. Nothing is enumerated or hashed then: the output count is 0, the delete below is attempted and its failure is recorded (deleted=false), and the run
    # ends EXPORT_KILL_UNCONFIRMED with its summary published.
    if (-not ($timedOut -and -not $killConfirmed) -and (Test-Path -LiteralPath $outDir)) {
        $outputs = @(Get-ChildItem -LiteralPath $outDir -Recurse -File -Filter $(if ($Format -eq 'cdng-lossless') { '*.dng' } else { '*.mov' }) | Sort-Object FullName)
    }
    $manifest = [ordered]@{ schema = 'export-stage-output-manifest.v1'; kind = $(if ($Format -eq 'cdng-lossless') { 'dng' } else { 'mov' }); count = $outputs.Count
                            totalBytes = [int64](($outputs | Measure-Object -Property Length -Sum).Sum); first = $null; last = $null; deleted = $false }
    if ($outputs.Count -gt 0) {
        $manifest.first = [ordered]@{ name = $outputs[0].Name; bytes = $outputs[0].Length; sha256 = (Get-AttrCudaFileSha256Blocks -Path $outputs[0].FullName -TracePath $Trace -Label 'output-first').sha256 }
        $manifest.last = [ordered]@{ name = $outputs[-1].Name; bytes = $outputs[-1].Length; sha256 = (Get-AttrCudaFileSha256Blocks -Path $outputs[-1].FullName -TracePath $Trace -Label 'output-last').sha256 }
    }
    # EXPORT-RENDER-PIPE-TIMING-3 r1 (sol r4 B4): the central reader's ONE binding verdict (embedded verbatim): DONE's clip against the CDNG frames found, and a present
    # RENDER_START (its grammar, the declaration's checks, the order, the .mov leaf and the verified dimensions) on ProRes, so on those bindings a run the central reader
    # reads INVALID is never EXPORT_CAPTURED here. (r2 F3, key3 r1 row 1: an ABSENT RENDER_START is read only centrally, which requires it on an emitting build's pipe
    # route; the receipt still reads INVALID by the central re-derivation, Invoke-VenueLeg.ps1:760-765, and the app always prints it there, BatchRunner.cpp:471)
    # (READER-HARDENING-1 r1, sol r2 B2: parity is on those shared bindings only; export_scale and the stage window are central-only checks too)
    $bind = Get-DvBatchExportBindingProblems -Batch $batchRead -Format $Format -OutputCount $outputs.Count -FirstName $(if ($null -ne $manifest.first) { $manifest.first.name } else { $null }) `
        -LastName $(if ($null -ne $manifest.last) { $manifest.last.name } else { $null }) -ReadStart ($Format -eq 'prores' -and $batchRead.start.count -gt 0)
    $outputAgrees =if ($Format -eq 'cdng-lossless') { $null -ne $exported -and $outputs.Count -eq $exported } else { $outputs.Count -eq 1 -and $null -ne $verifiedFrames -and $null -ne $exported -and $verifiedFrames -eq $exported }
    $result = $(if ($timedOut -and -not $killConfirmed) { 'EXPORT_KILL_UNCONFIRMED' }
        elseif ($timedOut) { 'EXPORT_TIMEOUT' }
        elseif ($appExitCode -ne 0) { 'EXPORT_EXIT_NONZERO' }
        elseif ($null -eq $exported -or @($bind.problems).Count -gt 0) { 'EXPORT_OUTPUT_MISMATCH' }
        elseif ($exported -eq 0) { 'EXPORT_NO_FRAMES' }
        elseif ($exported -lt $MaxFrames) { 'EXPORT_FRAMES_SHORT' }
        elseif (-not $stagePresent) { 'EXPORT_PROFILE_ABSENT' }
        elseif (-not $outputAgrees) { 'EXPORT_OUTPUT_MISMATCH' }
        else { 'EXPORT_CAPTURED' })
    $exitFor = @{ EXPORT_CAPTURED = 0; EXPORT_TIMEOUT = 40; EXPORT_NO_FRAMES = 41; EXPORT_FRAMES_SHORT = 42; EXPORT_PROFILE_ABSENT = 43; EXPORT_OUTPUT_MISMATCH = 44; EXPORT_EXIT_NONZERO = 46; EXPORT_KILL_UNCONFIRMED = 48 }

    # --- publish ONLY the records; then delete the exported footage from the venue-local work dir -------------------------------------------------------------------
    $manifest.deleted = $(try { Remove-ExportOutput } catch { Write-JobTrace "export output delete error=$([string]$_.CategoryInfo.Reason)"; $false })   # a locked survivor makes this false, never a missing terminal
    try { if (Test-Path -LiteralPath $batchLog -PathType Leaf) { [void](Publish-AttrCudaFileCopy -Source $batchLog -Destination (Join-Path $Pub 'batch.log')) } } catch { Write-JobTrace "step export batch.log publish error=$([string]$_.CategoryInfo.Reason)" }
    try { if (Test-Path -LiteralPath $stageEvidence -PathType Leaf) { [void](Publish-AttrCudaFileCopy -Source $stageEvidence -Destination (Join-Path $Pub ([IO.Path]::GetFileName($stageEvidence)))) } } catch { Write-JobTrace "step export stage record publish error=$([string]$_.CategoryInfo.Reason)" }
    # (adversary r13 A3: after an unconfirmed kill a survivor may hold output-manifest.json or summary.json without sharing, so an unguarded write threw through the finally and
    # cost RESULT=EXPORT_KILL_UNCONFIRMED and exit 48. On that terminal the two records are best effort: the failure is traced, the typed RESULT line and the exit code still go out.
    # Any other terminal keeps the write fatal: a run that cannot publish its summary must not report a result it did not record.)
    $evidenceBestEffort = ($timedOut -and -not $killConfirmed)
    try { Save-Json $manifest (Join-Path $Pub 'output-manifest.json') } catch { if (-not $evidenceBestEffort) { throw }; Write-JobTrace "step export manifest write error=$([string]$_.CategoryInfo.Reason)" }
    try {
        Write-ExportSummary $result @{ appExitCode = $appExitCode; timedOut = $timedOut; killConfirmed = $killConfirmed;exportWallSeconds = [math]::Round($exportWatch.Elapsed.TotalSeconds, 3); exportTimeoutSec = $ExportTimeoutSec
                                       framesExported = $exported; outputCount = $outputs.Count; outputsDeleted = $manifest.deleted; ffmpegPresent = $ffmpegPresent
                                       executableSha256 = $manifestHashes['exe']; cpuQuiescence = $quiescence; lengthGate = $lengthGateRecord; mlvappEnvScrubbed = @($mlvappEnvScrubbed) }
    } catch { if (-not $evidenceBestEffort) { throw }; Write-JobTrace "step export summary write error=$([string]$_.CategoryInfo.Reason)" }
    Write-Output "RESULT=$result FRAMES=$exported ARTIFACTS=$Pub"
    exit $exitFor[$result]
} finally {
    if ($OwnerClipDir) { Close-AttrCudaOwnerFootageWorkspace -Handles $ownerViewHandles -Views @($ownerViews) }
    try { [void](Remove-ExportOutput) } catch { Write-JobTrace "export output cleanup error=$([string]$_.CategoryInfo.Reason)" }
    try { [void](Remove-AttrCudaTree -TrustedRoot '__SCRATCH_ROOT__' -Path $pkgRoot -OwnedJournal $OwnerJournal) } catch { Write-JobTrace "package cleanup error=$([string]$_.CategoryInfo.Reason)" }
}
'@

# --- the shared pieces, read from the files that own them ----------------------------------------------------------------------------------------------------------
function Get-PlaybackGeneratorAst {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($PlaybackGenerator, [ref]$tokens, [ref]$errors)
    if (@($errors).Count -gt 0) { throw 'DUAL_VENUE_EXPORT_PLAYBACK_GENERATOR_UNPARSABLE the playback generator does not parse' }
    $ast
}

function Get-SharedTemplateFunctionText {
    param($Ast)
    $assign = @($Ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -ceq '$template' }, $false))
    if ($assign.Count -eq 0) { throw 'DUAL_VENUE_EXPORT_TEMPLATE_ABSENT the playback generator assigns no $template' }
    $body = $assign[0].Right.Expression
    if ($body -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { throw 'DUAL_VENUE_EXPORT_TEMPLATE_ABSENT the playback template is not a literal here-string' }
    $text = $body.Value
    @(foreach ($name in $SharedTemplateFunctions) {
        $m = [regex]::Match($text, '(?ms)^function\s+' + [regex]::Escape($name) + '\b.*?^\}')
        if (-not $m.Success) { throw "DUAL_VENUE_EXPORT_TEMPLATE_FUNCTION_ABSENT the playback template defines no $name" }
        $m.Value
    }) -join "`r`n`r`n"
}

function Get-BatchLineGrammarText {
    # EXPORT-RENDER-PIPE-TIMING-2 r4 (sol r3 B2): the central reader's batch.log line grammar -- its three tables and its two readers -- taken VERBATIM from
    # DualVenueRunner.psm1's own AST (the source text of each statement), so the venue job classifies DONE and RENDER_VERIFIED by the same grammar, never a copy
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($DualVenueModule, [ref]$tokens, [ref]$errors)
    if (@($errors).Count -gt 0) { throw 'DUAL_VENUE_EXPORT_GRAMMAR_ABSENT DualVenueRunner.psm1 does not parse' }
    $parts = [System.Collections.Generic.List[string]]::new()
    foreach ($name in $BatchLineGrammarTables) {
        $def = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -ceq $name }, $false))
        if ($def.Count -ne 1) { throw "DUAL_VENUE_EXPORT_GRAMMAR_ABSENT DualVenueRunner.psm1 assigns no single $name" }
        $parts.Add($def[0].Extent.Text)
    }
    foreach ($name in $BatchLineGrammarFunctions) {
        $def = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name }, $false))
        if ($def.Count -ne 1) { throw "DUAL_VENUE_EXPORT_GRAMMAR_ABSENT DualVenueRunner.psm1 defines no single $name" }
        $parts.Add($def[0].Extent.Text)
    }
    $parts -join "`r`n`r`n"
}

function Get-ExportEmbedding {
    param([string]$SharedText)
    Import-Module $ArtifactsModule -Force
    $names = [System.Collections.Generic.List[string]]::new()
    $blocks = [System.Collections.Generic.List[string]]::new()
    $unresolved = [System.Collections.Generic.List[string]]::new()
    $pending = [System.Collections.Generic.Queue[string]]::new()
    # r2: (?:[A-Z][a-z]+)+ so a two-word verb (ConvertTo-, ConvertFrom-) is followed too; the one-word form left ConvertTo-AttrCudaFileIdObject out and the venue job died on it.
    $callPattern = '\b((?:[A-Z][a-z]+)+-AttrCuda[A-Za-z0-9]*)\b'
    foreach ($n in @($SeedEmbedded) + @([regex]::Matches($template + "`n" + $SharedText, $callPattern) | ForEach-Object { $_.Groups[1].Value })) { $pending.Enqueue($n) }
    while ($pending.Count -gt 0) {
        $n = $pending.Dequeue()
        if ($names.Contains($n) -or $unresolved.Contains($n)) { continue }
        $text = $null
        foreach ($module in $ArtifactsModule, $OwnerFootageModule) {
            try { $text = Get-AttrCudaEmbeddedFunctionSource -ModulePath $module -Name @($n); break } catch { $text = $null }
        }
        # a name only mentioned in a comment of an embedded function, or one the generator alone defines, is not embedded and is listed as such
        if ($null -eq $text) { $unresolved.Add($n); continue }
        $names.Add($n); $blocks.Add($text)
        foreach ($m in [regex]::Matches($text, $callPattern)) { $pending.Enqueue($m.Groups[1].Value) }
    }
    [pscustomobject]@{ names = @($names); text = ($blocks -join "`r`n`r`n"); unresolved = @($unresolved) }
}

function Get-ExportJobTimeBudget {
    # adversary r1 A1: an export plays nothing, so the playback smoke budget (whose 3600 s smoke ceiling the export wait overflowed) is not its shape. The
    # job's own wall is the ONE margined identity read of the clip (Get-AttrCudaLegTimeBudget's measured cold rate), the quiescence samples, then the
    # export, whose wait holds the app's re-read of the clip at that same cold rate plus a generous per-frame CPU ceiling (dual-ISO AMaZE at full
    # resolution), never under 15 minutes. No ceiling of the smoke runner applies: the export wait is the job's own WaitForExit.
    # r7 (adversary r6 A4, r5 A3): the job's submitted timeout must fit what the agent accepts. UmRunDrop and the agent take 1..86400 s for the whole job
    # ($UmRunMaxJobTimeoutSec); Invoke-VenueLeg adds $RunnerSubmitMarginSec to recommendedJobTimeoutSec before it submits. A window whose budget passes that is REFUSED
    # here with a typed reason, before any lease is spent -- never clamped, because an export cut short by a smaller wait is not the export that was asked for. The sums
    # are int64 so no out-of-range figure throws an untyped cast.
    param([int64]$ClipBytes, [int]$Frames)
    $UmRunMaxJobTimeoutSec = [int64]86400
    $RunnerSubmitMarginSec = [int64]120
    Import-Module $ArtifactsModule -Force
    try { $read = Get-AttrCudaLegTimeBudget -InputBytes $ClipBytes -LaunchSeconds 0 -PlaySeconds 0 -SettleSeconds 0 -RunnerSlackSeconds 0 -ShareSmokeCeiling }
    catch { throw "DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED a clip of $ClipBytes bytes is outside the cold-read budget model, so no job timeout can be derived for it" }
    $identityReadSec = [int64]$read.identityReadSec
    $exportTimeoutSec = [int64]([Math]::Max(900, 4 * [int64]$Frames)) + $identityReadSec
    $quiescenceSec = [int64](3 * 12 + 30)
    $jobTimeoutSec = $identityReadSec + $quiescenceSec + $exportTimeoutSec + 600
    if ($jobTimeoutSec + $RunnerSubmitMarginSec -gt $UmRunMaxJobTimeoutSec) {
        throw "DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED $Frames frames of a $ClipBytes-byte clip need a job timeout of $jobTimeoutSec s ($($jobTimeoutSec + $RunnerSubmitMarginSec) s submitted), past the agent's $UmRunMaxJobTimeoutSec s cap; export a shorter window"
    }
    [pscustomobject]@{ identityReadSec = [int]$identityReadSec; exportTimeoutSec = [int]$exportTimeoutSec; quiescenceSec = [int]$quiescenceSec; jobTimeoutSec = [int]$jobTimeoutSec }
}
if ($DescribeTimeBudgetBytes -ge 0) {
    if ($MaxFrames -lt 24) { throw 'DUAL_VENUE_EXPORT_ARG_MISSING -MaxFrames is required (24..100000)' }
    return (Get-ExportJobTimeBudget -ClipBytes $DescribeTimeBudgetBytes -Frames $MaxFrames)
}

$ast = Get-PlaybackGeneratorAst
$sharedText = Get-SharedTemplateFunctionText -Ast $ast
$embedding = Get-ExportEmbedding -SharedText $sharedText
$batchLineGrammar = Get-BatchLineGrammarText
if ($DescribeTemplate) {
    return [pscustomobject]@{ template = $template; embedded = @($embedding.names); unresolved = @($embedding.unresolved); shared = $SharedTemplateFunctions; lengthGateEmbedded = $template.Contains('__LENGTH_GATE_BASE64__')
                              batchLineGrammar = $batchLineGrammar }
}

# --- generation ------------------------------------------------------------------------------------------------------------------------------------------------------
foreach ($needed in $(if ($PreflightBudgetOnly) { @('ClipId', 'Format') } else { @('SourceCommit', 'BuildManifestSha256', 'ClipId', 'Format', 'ReceiptPath', 'ExpectReceiptSha256', 'OutFile') })) {
    if ([string]::IsNullOrWhiteSpace([string](Get-Variable -Name $needed -ValueOnly))) { throw "DUAL_VENUE_EXPORT_ARG_MISSING -$needed is required" }
}
if ($MaxFrames -lt 24) { throw 'DUAL_VENUE_EXPORT_ARG_MISSING -MaxFrames is required (24..100000)' }
if (-not $PreflightBudgetOnly -and -not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) { throw 'DUAL_VENUE_EXPORT_RECEIPT_ABSENT -ReceiptPath names no file' }
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Join-Path $PSScriptRoot '..\..\..' }
try { $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path } catch { throw 'DUAL_VENUE_EXPORT_REPOROOT_INVALID -RepoRoot does not resolve to an existing path' }

# The playback generator's resolver, venue-table reader and owner-part path allowlist, loaded from ITS AST (never copied): the scriptblocks keep that file as
# their origin, so $PSScriptRoot inside them is the bachelor directory, as when the playback generator runs them.
foreach ($fn in 'Resolve-AttrCudaOwnerPython', 'Resolve-AttrCudaOwnerClipParts', 'Get-DualVenueEntry') {
    $def = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $fn }, $false))
    if ($def.Count -ne 1) { throw "DUAL_VENUE_EXPORT_RESOLVER_ABSENT the playback generator defines no single $fn" }
    Set-Item -Path "function:script:$fn" -Value $def[0].Body.GetScriptBlock()
}
$patternAssign = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -ceq '$OwnerPartPathPattern' }, $false))
if ($patternAssign.Count -ne 1 -or $patternAssign[0].Right.Expression -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { throw 'DUAL_VENUE_EXPORT_RESOLVER_ABSENT the playback generator sets no single owner-part path allowlist' }
$OwnerPartPathPattern = $patternAssign[0].Right.Expression.Value

# venue roots: bachelor is the playback job's default; any other venue reads the venue table through the playback generator's own reader
$agentRoot = 'C:\mlvtmp\mlv-agent'; $scratchRoot = 'C:\mlvtmp'
if ($Venue -ne 'bachelor') {
    $entry = Get-DualVenueEntry -VenueName $Venue
    $agentRoot = [string]$entry.agentRoot; $scratchRoot = [string]$entry.scratchRoot
}
if ($scratchRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$' -or $agentRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$' -or -not $agentRoot.StartsWith($scratchRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'DUAL_VENUE_TABLE_INVALID the venue table entry has a root outside the allowlist, or the agent root is not under the scratch root'
}

$ownerParts = @(Resolve-AttrCudaOwnerClipParts -ClipId $ClipId -RepoRoot $RepoRoot)
$ownerPartsJson = $ownerParts | ConvertTo-Json -Compress -Depth 5
if ($ownerParts.Count -eq 1) { $ownerPartsJson = "[$ownerPartsJson]" }

if ($PreflightBudgetOnly) {
    if ($MaxFrames -lt 24) { throw 'DUAL_VENUE_EXPORT_ARG_MISSING -MaxFrames is required (24..100000)' }
    $preflightBytes = [int64]0
    foreach ($p in $ownerParts) { $preflightBytes += [int64]$p.length }
    return (Get-ExportJobTimeBudget -ClipBytes $preflightBytes -Frames $MaxFrames)   # the same function, the same refusal the generation below makes
}

# r20 (adversary pass 24 A2): the batch receipt is read ONCE, here (after the preflight's return: r8 A3, that mode reads no receipt), before git or the job text; these bytes are
# the ones embedded below, and they must hash to the leg spec's committed receiptSha256 (-ExpectReceiptSha256). The self-derived hash alone proved only that the job carries
# what this generator read, not what the spec requires: a receipt file replaced after the leg wrote it is the typed refusal, and no job is written.
if (-not $PreflightBudgetOnly) {
    $receiptBytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $ReceiptPath).Path)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try { $receiptSha256 = ([BitConverter]::ToString($hasher.ComputeHash($receiptBytes)) -replace '-', '').ToLowerInvariant() } finally { $hasher.Dispose() }
    if ($receiptSha256 -cne $ExpectReceiptSha256) { throw "DUAL_VENUE_EXPORT_RECEIPT_CHANGED the batch receipt read from -ReceiptPath hashes to $receiptSha256, not the $ExpectReceiptSha256 the leg spec commits (export.receiptSha256); no job is written" }
}

& git -C $RepoRoot cat-file -e "$SourceCommit^{commit}" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'DUAL_VENUE_EXPORT_SOURCE_COMMIT_UNKNOWN -SourceCommit is not a commit known to the local repo' }

$clipBytes = [int64]0
foreach ($p in $ownerParts) { $clipBytes += [int64]$p.length }
$timeBudget = Get-ExportJobTimeBudget -ClipBytes $clipBytes -Frames $MaxFrames
$exportTimeoutSec = $timeBudget.exportTimeoutSec
$jobTimeoutSec = $timeBudget.jobTimeoutSec

$short = $SourceCommit.Substring(0, 12)
$tokens = [ordered]@{
    SOURCE_COMMIT = $SourceCommit
    BUILD_MANIFEST_SHA256 = $BuildManifestSha256
    CLIP_ID = $ClipId
    OWNER_PARTS_JSON = $ownerPartsJson.Replace("'", "''")
    EXE_NAME = "MLVApp-playback-attr-3-cuda-$short.exe"
    RECON_NAME = "igpu_recon_cuda-playback-attr-3-cuda-$short.dll"
    BASE_PACKAGE_ZIP = "MLVApp-playback-attr-3-cuda-$short-pkg.zip"
    FORMAT = $Format
    MAX_FRAMES = [string]$MaxFrames
    DUAL_ISO_EXPECTED = $(if ($DualIsoExpected) { '$true' } else { '$false' })
    RECEIPT_BASE64 = [Convert]::ToBase64String($receiptBytes)
    RECEIPT_SHA256 = $receiptSha256
    LENGTH_GATE_BASE64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $LengthGatePath).Path))
    PLAY_SECONDS = [string]$PlaySeconds
    EXPORT_TIMEOUT_SEC = [string]$exportTimeoutSec
    DECLARED_VENUE = $Venue
    CPU_QUIESCENCE_THRESHOLD_PERCENT = $CpuQuiescenceThresholdPercent.ToString([Globalization.CultureInfo]::InvariantCulture)
    AGENT_ROOT = $agentRoot
    SCRATCH_ROOT = $scratchRoot
    EMBEDDED_FUNCTIONS = $embedding.text
    SHARED_FUNCTIONS = $sharedText
    BATCH_LINE_GRAMMAR = $batchLineGrammar
}
# (one pass: a substituted value is never rescanned; the embedded and shared texts carry __SCRATCH_ROOT__ themselves, so they are expanded first)
$tokens.EMBEDDED_FUNCTIONS = $embedding.text
$sharedExpanded = $sharedText.Replace('__SCRATCH_ROOT__', $scratchRoot)
$tokens.SHARED_FUNCTIONS = $sharedExpanded
$text = Expand-AttrCudaTemplate -Template $template -Tokens $tokens
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$parseErrors)
if (@($parseErrors).Count -gt 0) { throw "DUAL_VENUE_EXPORT_JOB_UNPARSABLE the emitted export job does not parse ($(@($parseErrors)[0].Message))" }

$outDir = Split-Path -Parent $OutFile
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
[IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($false))

# the clip's CONTENT identity for the receipt's subject digest (the playback generator's derivation: one part = its sha256; several = sha256 of the LF-joined part sha256s)
$partShas = @($ownerParts | Sort-Object { [int]$_.index } | ForEach-Object { ([string]$_.sha256).ToLowerInvariant() })
$clipContentSha256 = if ($partShas.Count -eq 1) { $partShas[0] } else {
    $h = [System.Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes(($partShas -join "`n")))) -replace '-', '').ToLowerInvariant() } finally { $h.Dispose() }
}

[pscustomobject]@{
    outFile = $OutFile
    legType = 'export'
    sourceCommit = $SourceCommit
    buildManifestSha256 = $BuildManifestSha256
    clipId = $ClipId
    clipContentSha256 = $clipContentSha256
    playSeconds = $PlaySeconds
    fixtureRehearsal = $false
    venue = $Venue
    backend = 'cpu'
    format = $Format
    maxFrames = $MaxFrames
    dualIsoExpected = $DualIsoExpected
    receiptSha256 = $receiptSha256
    exportTimeoutSec = $exportTimeoutSec
    recommendedJobTimeoutSec = $jobTimeoutSec
    smokeRunnerClosureDirName = $null
    embedded = @($embedding.names)
}
