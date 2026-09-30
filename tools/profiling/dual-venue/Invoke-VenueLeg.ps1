# Invoke-VenueLeg.ps1 -- run ONE leg on ONE venue and ALWAYS write a typed receipt.
# DUAL-VENUE-EVIDENCE-1 (C2). RUN THIS ON THE VM. Methodology: docs/dual-venue-evidence.md.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeg.ps1 `
#       -Venue ultra-magnus -LegSpec tools\profiling\dual-venue\legs\fixture-look-large.json `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> -Backend cpu
#
# The flow (each step a refusal the code enforces, each refusal a receipt):
#   1. read the leg spec (legSpecSha256 = sha256 of its bytes) and the venue table; the venue's ROLE
#      for the leg's card comes from venues.json, never from an argument (P3);
#   2. refuse an owner clip (only the tracked fixtures run; OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2)
#      BEFORE anything is submitted (P7);
#   3. bounded health probe on the venue (P5) -> VENUE_UNHEALTHY, the leg is NOT submitted;
#   4. P6: the declared -Venue must agree with Get-AttrCudaMeasurementVenue on the host the probe ran
#      on, and the host must be the venue table's expectedHost -> VENUE_HOST_MISMATCH;
#   5. the staged build, fixture and smoke-runner closure must be the ones named (never a different
#      build) -> DEVICE_UNAVAILABLE;
#   6. generate the job (tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 -Venue -Backend ...),
#      snapshot the venue's HKCU magiclantern.MLVApp QSettings, submit THROUGH um-run.ps1 (NA-7),
#      restore the QSettings afterwards (values are never printed);
#   7. read summary.json / evidence-manifest.json / artifact-index.json, copy metrics VERBATIM, map the
#      job's RESULT to a P4 outcome, evaluate the role's criteria, write the receipt.
#
# EXIT CODE IS NEVER EVIDENCE: 0 means "a receipt was written" (whatever its outcome), 2 means the
# receipt itself could not be written. Read the receipt.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('bachelor', 'ultra-magnus')][string]$Venue,
    [Parameter(Mandatory = $true)][string]$LegSpec,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [ValidateSet('cuda', 'cpu')][string]$Backend = '',
    # The owner's typed CLIP line for an owner-clip leg. Refused regardless until the owner-footage
    # cleanup class is gone (venues.json ownerFootage.cleanupClassGone).
    [string]$OwnerClipConsentLine = '',
    # Run only the health probe (and P6) and stop: records the venue's state without running the leg.
    [switch]$HealthOnly,
    [string]$Actor = '',
    # Where receipts go. Default: <main checkout>\.claude-state\dual-venue\receipts (gitignored).
    [string]$ReceiptRoot = '',
    # Where the LOOK sheet is copied as sheet-<venue>-<backend>-<flavor>.png (for showing the owner).
    [string]$SheetCopyDir = '',
    # Test seams (production passes none of these).
    [string]$VenueTablePath = '',
    [string]$RepoRoot = '',
    [string]$WorkDir = '',
    [string]$UmRunScript = '',
    [string]$GeneratorScript = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = (Resolve-Path (Join-Path $here '..\..\..')).Path }
if ([string]::IsNullOrWhiteSpace($VenueTablePath)) { $VenueTablePath = Join-Path $here 'venues.json' }
if ([string]::IsNullOrWhiteSpace($UmRunScript)) { $UmRunScript = Join-Path $here '..\um-run.ps1' }
if ([string]::IsNullOrWhiteSpace($GeneratorScript)) { $GeneratorScript = Join-Path $here '..\bachelor\playback-attr-3-cuda-job.ps1' }
if ([string]::IsNullOrWhiteSpace($ReceiptRoot)) {
    # .claude-state is gitignored, so a worktree has none: resolve the MAIN checkout from the git common dir.
    $common = (& git -C $RepoRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
    $mainRoot = if ($LASTEXITCODE -eq 0 -and $common) { Split-Path -Parent ([string]$common) } else { $RepoRoot }
    $ReceiptRoot = Join-Path $mainRoot '.claude-state\dual-venue\receipts'
}
if ([string]::IsNullOrWhiteSpace($Actor)) { $Actor = "dual-venue-runner@$($env:COMPUTERNAME)" }
$runStamp = [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')

$table = Read-DvVenueTable -Path $VenueTablePath
$venueEntry = $table.venues.$Venue
if ($null -eq $venueEntry) { throw "DVE_UNKNOWN_VENUE the venue table has no entry for '$Venue'" }
$agentShare = [string]$venueEntry.agentShare
$agentRoot = [string]$venueEntry.agentRoot

$specBytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $LegSpec).Path)
$legSpecSha256 = Get-DvSha256OfBytes $specBytes
$spec = [Text.Encoding]::UTF8.GetString($specBytes) | ConvertFrom-Json
if ($spec.schema -ne 'mlv-app/dual-venue-leg/v1') { throw "DVE_LEG_SPEC_INVALID schema is '$($spec.schema)', expected mlv-app/dual-venue-leg/v1" }
if ([string]::IsNullOrWhiteSpace($Backend)) { $Backend = [string]@($spec.backends)[0] }
if ($Backend -notin @($spec.backends)) { throw "DVE_LEG_SPEC_INVALID backend '$Backend' is not one of this leg's backends ($(@($spec.backends) -join ', '))" }
$isLook = ($spec.legType -eq 'look')
$lookFlavor = $null
if ($isLook) { $lookFlavor = [string]$spec.look.lookFlavor; if ([string]::IsNullOrWhiteSpace($lookFlavor)) { $lookFlavor = 'classic' } }

$role = Get-DvVenueRole -Table $table -Card ([string]$spec.card) -Venue $Venue
$methodRel = 'tools/profiling/dual-venue/Invoke-VenueLeg.ps1'
$methodBlob = (& git -C $RepoRoot hash-object (Join-Path $here 'Invoke-VenueLeg.ps1') 2>$null)
$receipt = New-DvReceipt -Card ([string]$spec.card) -LegId ([string]$spec.legId) -DeclaredVenue $Venue -Role $role -Actor $Actor `
    -Method $methodRel -MethodBlobId ([string]$methodBlob) -Backend $Backend -LookFlavor $lookFlavor
$receipt.subject.buildManifestSha256 = $BuildManifestSha256
$receipt.subject.legSpecSha256 = $legSpecSha256
$receipt.subject.clipId = [string]$spec.clipId

function Complete-Receipt([string]$Outcome, [string]$Detail) {
    $receipt.outcome = $Outcome
    $receipt.outcomeDetail = $Detail
    $receipt.subject.digest = Get-DvSubjectDigest -BuildManifestSha256 $BuildManifestSha256 -LegSpecSha256 $legSpecSha256 `
        -ClipId $spec.clipId -ClipContentSha256 $receipt.subject.clipContentSha256 -Backend $Backend -LookFlavor $lookFlavor
    try {
        $path = Write-DvReceipt -Receipt $receipt -ReceiptRoot $ReceiptRoot
    } catch {
        Write-Output "DVE_RECEIPT_WRITE_FAILED $($_.Exception.Message)"
        exit 2
    }
    Write-Output "DVE_OUTCOME=$Outcome"
    Write-Output "DVE_DETAIL=$Detail"
    Write-Output "DVE_RECEIPT_PATH=$path"
    exit 0
}

function Submit-VenueJob([string]$ScriptPath, [string]$JobId, [int]$TimeoutSec, [int]$QueueSec, [int]$ClaimedExtraSec) {
    # um-run.ps1 is the ONLY writer to a venue share (NA-7). Its three outcomes: a receipt object,
    # RETRACTED, or UNRESOLVED -- a thrown UNRESOLVED/RETRACTED is data here, never an error.
    try {
        $out = @(& $UmRunScript -ScriptPath $ScriptPath -JobId $JobId -AgentShare $agentShare -TimeoutSec $TimeoutSec `
            -MaxQueueWaitSec $QueueSec -MaxClaimedWaitSec $ClaimedExtraSec 6>$null)
        $r = if ($out.Count -gt 0) { $out[-1] } else { $null }
        [pscustomobject]@{ umOutcome = 'RECEIPT'; result = $r; message = $null }
    } catch {
        $m = [string]$_.Exception.Message
        $kind = if ($m -match '^RETRACTED:') { 'RETRACTED' } elseif ($m -match '^UNRESOLVED:') { 'UNRESOLVED' } else { 'ERROR' }
        [pscustomobject]@{ umOutcome = $kind; result = $null; message = $m }
    }
}

function ConvertTo-ShareSidePath([string]$AgentSidePath) {
    # A path the job printed (under the venue's agent root) -> the same location through the share.
    if (-not $AgentSidePath.StartsWith($agentRoot, [StringComparison]::OrdinalIgnoreCase)) { return $null }
    $relative = $AgentSidePath.Substring($agentRoot.Length).TrimStart('\')
    $agentShare.TrimEnd('\') + '\' + $relative
}

function Stop-Leg([string]$Outcome, [string]$Detail) {
    # A terminal reached mid-flow. Thrown (not exited) so the QSettings restore in the `finally`
    # below still runs BEFORE the receipt is written and can be recorded in it.
    throw [System.Management.Automation.RuntimeException]::new("DVE_TERMINAL|$Outcome|$Detail")
}

$workDirResolved = if ([string]::IsNullOrWhiteSpace($WorkDir)) { Join-Path ([IO.Path]::GetTempPath()) "dve-$runStamp-$([guid]::NewGuid().ToString('N').Substring(0,8))" } else { $WorkDir }
New-Item -ItemType Directory -Force -Path $workDirResolved | Out-Null
$jobStem = "dve-$($spec.legId)-$Venue-$Backend-$runStamp"

$snapshotTaken = $false
$snapshotTag = "$jobStem"
$terminal = $null
$run = $null
try {
try {
    # --- 2. clip admission (P7) -- before anything is generated or submitted ---------------------
    $fixture = Get-DvTrackedFixture -ClipId ([string]$spec.clipId) -RepoRoot $RepoRoot
    if ($null -eq $fixture) {
        $admission = Test-DvOwnerClipAdmitted -Table $table -ConsentLine $OwnerClipConsentLine
        if (-not $admission.admitted) {
            $receipt.refusal = [string]$admission.reason
            # No P4 enum value names a refusal; DEVICE_UNAVAILABLE carries no signal and the typed
            # reason is in refusal/outcomeDetail. (Schema note for the hub: see the PR description.)
            Stop-Leg 'DEVICE_UNAVAILABLE' ([string]$admission.reason)
        }
        throw 'DVE_OWNER_CLIP_ADMITTED_BUT_UNSUPPORTED this runner only runs tracked fixtures; the owner-clip route is not built yet'
    }
    $receipt.subject.clipContentSha256 = $fixture.sha256

    # --- 3. health probe (P5), bounded -------------------------------------------------------------
    $probeJob = Join-Path $workDirResolved "$jobStem-health.job.ps1"
    [IO.File]::WriteAllText($probeJob, (New-DvHealthProbeJobText -AgentRoot $agentRoot), [Text.UTF8Encoding]::new($false))
    $probeRun = Submit-VenueJob -ScriptPath $probeJob -JobId "$jobStem-health" -TimeoutSec 120 -QueueSec 120 -ClaimedExtraSec 60
    $receipt.evidence.umRunOutcome = $probeRun.umOutcome
    $probe = $null
    if ($probeRun.umOutcome -eq 'RECEIPT' -and $null -ne $probeRun.result) { $probe = ConvertFrom-DvProbeStdout ([string]$probeRun.result.stdout) }
    if ($null -eq $probe) {
        $receipt.health.outcome = 'UNHEALTHY'
        $why = if ($probeRun.umOutcome -ne 'RECEIPT') { "health probe ended $($probeRun.umOutcome): $($probeRun.message)" } else { 'health probe returned no DVE_PROBE line' }
        Stop-Leg 'VENUE_UNHEALTHY' $why
    }
    $receipt.health.pwshColdStartMs = $probe.pwshColdStartMs
    $receipt.health.smallHashMs = $probe.smallHashMs
    $receipt.health.freeDiskGiB = $probe.freeDiskGiB
    $receipt.health.commitUsedGiB = $probe.commitUsedGiB
    $receipt.health.commitLimitGiB = $probe.commitLimitGiB
    $receipt.venue.hostName = [string]$probe.hostName
    $receipt.venue.gpuNames = @($probe.gpuNames)
    $receipt.venue.driverVersion = $probe.driverVersion
    $receipt.venue.displayDevice = $probe.displayDevice
    $receipt.venue.instrumentDigests.presentmon = $probe.presentmonSha256

    # P6: one source of truth for which venue a host is.
    Import-Module (Join-Path $here '..\bachelor\AttrCudaArtifacts.psm1') -Force
    $detected = Get-AttrCudaMeasurementVenue -ComputerName ([string]$probe.hostName)
    $receipt.venue.detected = $detected
    if ($detected -ne $Venue -or [string]$probe.hostName -ine [string]$venueEntry.expectedHost) {
        $receipt.health.outcome = 'HOST_MISMATCH'
        Stop-Leg 'VENUE_HOST_MISMATCH' "declared '$Venue' (expected host '$($venueEntry.expectedHost)') but the host is '$($probe.hostName)' and Get-AttrCudaMeasurementVenue says '$detected'"
    }
    $verdict = Get-DvHealthVerdict -Probe $probe -Thresholds $venueEntry.health
    if (-not $verdict.healthy) {
        $receipt.health.outcome = 'UNHEALTHY'
        Stop-Leg 'VENUE_UNHEALTHY' ('leg not submitted: ' + ($verdict.reasons -join '; '))
    }
    $receipt.health.outcome = 'HEALTHY'
    if ($HealthOnly) {
        Stop-Leg 'UNRESOLVED' 'HEALTH_ONLY_NO_LEG_RUN: the venue is healthy and the host check passed; no leg was submitted, so nothing was measured'
    }

    # --- 5. the staged build / fixture / runner must be exactly the ones named -----------------------
    $short = $SourceCommit.Substring(0, 12)
    $cacheShare = $agentShare.TrimEnd('\') + '\cache'
    $buildJson = "$cacheShare\playback-attr-3-cuda-$short-build.json"
    if (-not (Test-Path -LiteralPath $buildJson -PathType Leaf)) { Stop-Leg 'DEVICE_UNAVAILABLE' "BUILD_NOT_STAGED: no build manifest for $short on this venue" }
    if ((Get-DvSha256OfFile $buildJson) -ne $BuildManifestSha256) { Stop-Leg 'DEVICE_UNAVAILABLE' "BUILD_NOT_STAGED: the staged build manifest for $short is not sha256 $BuildManifestSha256 (a different build is never substituted)" }
    $fixtureCache = "$cacheShare\$($spec.clipId)" + '.' + 'mlv'
    if (-not (Test-Path -LiteralPath $fixtureCache -PathType Leaf)) { Stop-Leg 'DEVICE_UNAVAILABLE' 'FIXTURE_NOT_STAGED: the fixture is not in this venue''s cache' }
    if ((Get-DvSha256OfFile $fixtureCache) -ne $fixture.sha256) { Stop-Leg 'DEVICE_UNAVAILABLE' 'FIXTURE_NOT_STAGED: the cached fixture bytes do not match the tracked fixture' }

    # --- 6. generate ------------------------------------------------------------------------------
    $jobFile = Join-Path $workDirResolved "$jobStem.job.ps1"
    $gen = @{
        SourceCommit = $SourceCommit; BuildManifestSha256 = $BuildManifestSha256; ClipId = [string]$spec.clipId
        FixtureSha256 = $fixture.sha256; OutFile = $jobFile; RepoRoot = $RepoRoot
        Venue = $Venue; Backend = $Backend; ScaleFactor = [int]$spec.scaleFactor
    }
    if ($VenueTablePath -ne (Join-Path $here 'venues.json')) { $gen['VenueTablePath'] = $VenueTablePath }
    if ($null -ne $spec.PSObject.Properties['generatorArgs']) {
        if ($spec.generatorArgs.PSObject.Properties['telemetryArm']) { $gen['TelemetryArm'] = [string]$spec.generatorArgs.telemetryArm }
        if ($spec.generatorArgs.PSObject.Properties['cpuQuiescenceThresholdPercent']) { $gen['CpuQuiescenceThresholdPercent'] = [double]$spec.generatorArgs.cpuQuiescenceThresholdPercent }
    }
    if ($isLook) {
        $gen['ContactSheet'] = $true; $gen['ContactSheetFrames'] = [int]$spec.look.contactSheetFrames
        $gen['ForceLookAssist'] = $true; $gen['LookFlavor'] = $lookFlavor
    }
    $genOut = @(& $GeneratorScript @gen)
    $generated = $genOut[-1]
    $runnerDir = "$cacheShare\$($generated.smokeRunnerClosureDirName)"
    if (-not (Test-Path -LiteralPath $runnerDir -PathType Container)) { Stop-Leg 'DEVICE_UNAVAILABLE' "SMOKE_RUNNER_NOT_STAGED: $($generated.smokeRunnerClosureDirName) is not in this venue's cache" }

    # --- 6b. protect the venue's QSettings, submit, restore -------------------------------------
    $snapJob = Join-Path $workDirResolved "$jobStem-regsnap.job.ps1"
    [IO.File]::WriteAllText($snapJob, (New-DvRegSnapshotJobText -AgentRoot $agentRoot -Tag $snapshotTag), [Text.UTF8Encoding]::new($false))
    $snapRun = Submit-VenueJob -ScriptPath $snapJob -JobId "$jobStem-regsnap" -TimeoutSec 120 -QueueSec 300 -ClaimedExtraSec 60
    $snapInfo = if ($snapRun.umOutcome -eq 'RECEIPT') { ConvertFrom-DvMarkerLine -Stdout ([string]$snapRun.result.stdout) -Marker 'DVE_REG_SNAPSHOT' } else { $null }
    if ($null -eq $snapInfo -or $snapInfo['ok'] -ne 'True') {
        $receipt.registry = [ordered]@{ snapshotTaken = $false; restored = $null }
        Stop-Leg 'VENUE_UNHEALTHY' 'leg not submitted: the venue QSettings snapshot could not be taken (the app rewrites per-user settings, so an unprotected run is refused)'
    }
    $snapshotTaken = $true

    $timeoutSec = [int]$generated.recommendedJobTimeoutSec + 120
    $run = Submit-VenueJob -ScriptPath $jobFile -JobId $jobStem -TimeoutSec $timeoutSec -QueueSec ([int]$spec.timeouts.queueWaitSec) -ClaimedExtraSec ([int]$spec.timeouts.extraClaimedWaitSec)
    $receipt.evidence.umRunOutcome = $run.umOutcome
} finally {
    if ($snapshotTaken) {
        $restoreJob = Join-Path $workDirResolved "$jobStem-regrestore.job.ps1"
        [IO.File]::WriteAllText($restoreJob, (New-DvRegRestoreJobText -AgentRoot $agentRoot -Tag $snapshotTag), [Text.UTF8Encoding]::new($false))
        $restoreRun = Submit-VenueJob -ScriptPath $restoreJob -JobId "$jobStem-regrestore" -TimeoutSec 120 -QueueSec 600 -ClaimedExtraSec 120
        $restoreInfo = if ($restoreRun.umOutcome -eq 'RECEIPT') { ConvertFrom-DvMarkerLine -Stdout ([string]$restoreRun.result.stdout) -Marker 'DVE_REG_RESTORE' } else { $null }
        $receipt.registry = [ordered]@{
            snapshotTaken = $true
            restored = $(if ($null -ne $restoreInfo) { $restoreInfo['restored'] -eq 'True' } else { $null })
            restoreUmRunOutcome = $restoreRun.umOutcome
        }
    }
}
} catch {
    $message = [string]$_.Exception.Message
    if ($message.StartsWith('DVE_TERMINAL|')) {
        $parts = $message.Split('|', 3)
        $terminal = [pscustomobject]@{ outcome = $parts[1]; detail = $parts[2] }
    } else {
        # Never a silent failure and never a verdict: the runner broke, so this leg says UNRESOLVED.
        $terminal = [pscustomobject]@{ outcome = 'UNRESOLVED'; detail = "RUNNER_ERROR: $message" }
    }
}
if ($null -ne $terminal) { Complete-Receipt $terminal.outcome $terminal.detail }

# --- 7. read the evidence and write the receipt ---------------------------------------------------------
if ($run.umOutcome -in @('RETRACTED', 'UNRESOLVED')) {
    Complete-Receipt $run.umOutcome $run.message
}
if ($run.umOutcome -ne 'RECEIPT') { Complete-Receipt 'UNRESOLVED' "um-run ended in an unexpected error: $($run.message)" }

$stdout = [string]$run.result.stdout
$exitCode = [int]$run.result.exitCode
$token = Get-DvResultToken $stdout
$resolved = Resolve-DvJobOutcome -ResultToken $token -ExitCode $exitCode

$artifactsShare = $null
if ($stdout -match 'ARTIFACTS=(?<p>\S+)') { $artifactsShare = ConvertTo-ShareSidePath $Matches['p'] }
$summary = $null; $manifest = $null
$evidenceDir = Join-Path (Split-Path -Parent $ReceiptRoot) ("evidence\" + $receipt.receiptId)
if ($artifactsShare -and (Test-Path -LiteralPath $artifactsShare -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    foreach ($name in 'summary.json', 'evidence-manifest.json', 'artifact-index.json') {
        $src = Join-Path $artifactsShare $name
        if (Test-Path -LiteralPath $src -PathType Leaf) { Copy-Item -LiteralPath $src -Destination (Join-Path $evidenceDir $name) }
    }
    $summaryLocal = Join-Path $evidenceDir 'summary.json'
    $manifestLocal = Join-Path $evidenceDir 'evidence-manifest.json'
    if (Test-Path -LiteralPath $summaryLocal) {
        $receipt.evidence.summaryJsonSha256 = Get-DvSha256OfFile $summaryLocal
        $summary = [IO.File]::ReadAllText($summaryLocal) | ConvertFrom-Json
    }
    if (Test-Path -LiteralPath $manifestLocal) {
        $receipt.evidence.evidenceManifestSha256 = Get-DvSha256OfFile $manifestLocal
        $manifest = [IO.File]::ReadAllText($manifestLocal) | ConvertFrom-Json
    }
    $receipt.evidence.artifactIndexPath = $artifactsShare.TrimEnd('\') + '\artifact-index.json'
    $receipt.evidence['localEvidenceDir'] = $evidenceDir
}
if ($null -ne $summary) { $receipt.metrics = Get-DvVerbatimMetrics -Summary $summary -EvidenceManifest $manifest }

# P6 again, from the job's own record: a summary that names a different venue than declared is a mismatch.
if ($null -ne $summary -and $summary.PSObject.Properties['display'] -and $summary.display -and $summary.display.PSObject.Properties['venue']) {
    $jobVenue = [string]$summary.display.venue
    if ($jobVenue -and $jobVenue -ne $Venue) {
        Complete-Receipt 'VENUE_HOST_MISMATCH' "the job's own display block says venue '$jobVenue' but '$Venue' was declared"
    }
}

# LOOK: copy the job's contact sheet + raw frames locally and bind the sheet by sha256.
if ($isLook -and $artifactsShare) {
    $sheetShare = Join-Path $artifactsShare 'contact-sheet\sheet.png'
    $sheetInfo = $null
    if (Test-Path -LiteralPath $sheetShare -PathType Leaf) {
        $sheetDir = Join-Path $evidenceDir 'contact-sheet'
        New-Item -ItemType Directory -Force -Path $sheetDir | Out-Null
        Copy-Item -LiteralPath $sheetShare -Destination (Join-Path $sheetDir 'sheet.png')
        foreach ($n in 'stats.json', 'compose-status.txt') {
            $s = Join-Path $artifactsShare "contact-sheet\$n"
            if (Test-Path -LiteralPath $s -PathType Leaf) { Copy-Item -LiteralPath $s -Destination (Join-Path $sheetDir $n) }
        }
        $rawShare = Join-Path $artifactsShare 'artifacts\contact-sheet\raw'
        if (-not (Test-Path -LiteralPath $rawShare -PathType Container)) { $rawShare = Join-Path $artifactsShare 'contact-sheet\raw' }
        if (Test-Path -LiteralPath $rawShare -PathType Container) { Copy-Item -LiteralPath $rawShare -Destination (Join-Path $sheetDir 'raw') -Recurse }
        $sheetLocal = Join-Path $sheetDir 'sheet.png'
        $sheetInfo = [ordered]@{ path = $sheetLocal; sha256 = (Get-DvSha256OfFile $sheetLocal); bytes = (Get-Item -LiteralPath $sheetLocal).Length; frames = [int]$spec.look.contactSheetFrames; backend = $Backend; rawFramesDir = $(if (Test-Path (Join-Path $sheetDir 'raw')) { Join-Path $sheetDir 'raw' } else { $null }) }
        if (-not [string]::IsNullOrWhiteSpace($SheetCopyDir)) {
            New-Item -ItemType Directory -Force -Path $SheetCopyDir | Out-Null
            Copy-Item -LiteralPath $sheetLocal -Destination (Join-Path $SheetCopyDir "sheet-$Venue-$Backend-$lookFlavor.png") -Force
        }
    }
    $receipt.look = [ordered]@{
        legType = 'look'; lookAssistForced = $true; lookFlavor = $lookFlavor
        lookFlavorHonored = 'unknown'   # the app does not read MLVAPP_LOOK_ASSIST_FLAVOR yet (LOOK-ASSIST-FLAVORS-1)
        contactSheet = $sheetInfo
    }
}

$outcome = $resolved.outcome; $detail = $resolved.detail
if ($outcome -eq 'CAPTURED') {
    $criteria = $spec.criteria.$role.$Backend
    $verdictCriteria = Test-DvCriteria -Criteria $criteria -Metrics $receipt.metrics
    $sheetMissing = $isLook -and ($null -eq $receipt.look -or $null -eq $receipt.look.contactSheet)
    if ($sheetMissing) { $outcome = 'FAIL'; $detail = 'CAPTURED but the LOOK leg produced no contact sheet' }
    elseif (-not $verdictCriteria.pass) { $outcome = 'FAIL'; $detail = 'CAPTURED; criteria failed: ' + ($verdictCriteria.failures -join '; ') }
    else { $outcome = 'PASS'; $detail = $(if ($verdictCriteria.informational) { 'CAPTURED; no gating criteria for this role/backend (informational)' } else { 'CAPTURED; every criterion for this role/backend held' }) }
}
Complete-Receipt $outcome $detail
