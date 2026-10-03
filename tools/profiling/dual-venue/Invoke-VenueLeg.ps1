# Invoke-VenueLeg.ps1 -- run ONE leg on ONE venue and ALWAYS write a typed receipt.
# DUAL-VENUE-EVIDENCE-1 (C2). RUN THIS ON THE VM. Methodology: docs/dual-venue-evidence.md.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeg.ps1 `
#       -Venue ultra-magnus -LegSpec tools\profiling\dual-venue\legs\m16-1243-look.json `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> -Backend cpu
#
# The flow (each step a refusal the code enforces, each refusal a receipt):
#   1. read the leg spec (legSpecSha256 = sha256 of its bytes) and the venue table; the venue's ROLE
#      for the leg's card comes from venues.json, never from an argument (P3). PRODUCTION reads the venue table
#      AND the per-venue consent ONLY from the tracked files at the COMMITTED revision (git HEAD) and refuses when
#      the working copy differs (round 3): a file a caller writes is never consent. The path/script overrides are
#      test-only and refused unless -OfflineTestMode, which can never reach a venue (see below);
#   2. ADMIT THE CLIP (round 2, owner rule 2026-09-30): a leg names a CONSENTED CLIP ID, never a path. A tracked
#      fixture (2 / 16 frames) is refused by master's length gate (FIXTURE_REFUSED_CLIP_TOO_SHORT); a clip this
#      VENUE has no owner-typed record for in venue-clip-consent.json is refused (VENUE_CLIP_CONSENT_ABSENT);
#      a play window under 20 s is refused (PLAY_WINDOW_TOO_SHORT); the reviewed cleanup switch must be on
#      (OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2). All BEFORE anything is generated or submitted;
#   3. generate the job (tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 -ClipId -PlaySeconds -Venue -Backend ...):
#      the generator resolves the clip by id and refuses what master's clip-length gate refuses;
#   4. bounded health probe on the venue (P5) -> VENUE_UNHEALTHY, the leg is NOT submitted;
#   5. P6: the declared -Venue must agree with Get-AttrCudaMeasurementVenue on the host the probe ran
#      on, and the host must be the venue table's expectedHost -> VENUE_HOST_MISMATCH;
#   6. the staged build and smoke-runner closure must be the ones named (never a different build)
#      -> DEVICE_UNAVAILABLE; submit THROUGH um-run.ps1 (NA-7). (No registry snapshot is taken any more: master
#      isolates an automation run's settings store; the receipt proves it from the run log.)
#   7. read summary.json / evidence-manifest.json / artifact-index.json plus the launcher's result.json and the app's
#      run log, copy metrics VERBATIM, RE-DERIVE the receipt oracle's verdict from the run log (source_advanced /
#      required_source_frames / native fps / pace / fps override / wrap / the nonce the app echoed vs the nonce the
#      launcher generated), map the job's RESULT to a P4 outcome, evaluate the role's criteria, write the receipt. A
#      PASS/FAIL without a valid verdict is INVALID.
#
# PRODUCTION RECEIPTS ARE ADVISORY (DUAL-VENUE-EVIDENCE-2 round 2): no venue-held anchor exists, so a PASS/FAIL receipt that re-derives is written
# with verification.status = ADVISORY, outcomeDetail prefixed "ADVISORY (VENUE_ANCHOR_ABSENT; never a usable PASS)", and DVE_VERIFICATION=ADVISORY is
# printed; Test-DvReceiptValid returns valid=false for it. Production PASS verification is DUAL-VENUE-PASS-PROVENANCE-1 (docs/dual-venue-evidence.md).
#
# OFFLINE TEST MODE (-OfflineTestMode, tests only): lets a test supply its own consent file, venue table, generator
# and um-run stub. It can NEVER submit a job to a venue: -UmRunScript is required and may not be um-run.ps1 (by path or by
# content), and every venue's agentShare must be a local directory under the OS temp folder (never a UNC share). A receipt
# it writes says admission.mode = offline-test and is not evidence (Test-DvReceiptValid refuses it).
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
    # Run only the health probe (and P6) and stop: records the venue's state without running the leg.
    [switch]$HealthOnly,
    [string]$Actor = '',
    # Where receipts go. Default: <main checkout>\.claude-state\dual-venue\receipts (gitignored). Must sit under a
    # `.claude-state` directory in production (a receipt and its evidence name an owner clip's run).
    [string]$ReceiptRoot = '',
    # Where the LOOK sheet is copied as sheet-<legId>-<venue>-<backend>-<flavor>.png (for showing the owner; the leg id keeps two look legs of one venue apart). It is a sheet of OWNER
    # footage: it must sit under a `.claude-state` directory (the same guard New-VenueSheetPair.ps1 enforces).
    [string]$SheetCopyDir = '',
    # TEST SEAMS. Refused unless -OfflineTestMode (production passes none of these): the consent file, the venue table,
    # the repo root, the work dir, the generator and the um-run script are all things a caller must not be able to swap.
    [switch]$OfflineTestMode,
    [string]$ConsentPath = '',
    [string]$VenueTablePath = '',
    [string]$RepoRoot = '',
    [string]$WorkDir = '',
    [string]$UmRunScript = '',
    [string]$GeneratorScript = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force

# --- test seams are closed in production (round 3, sol BLOCKER) -------------------------------------
foreach ($seam in 'ConsentPath', 'VenueTablePath', 'RepoRoot', 'WorkDir', 'UmRunScript', 'GeneratorScript') {
    if ($PSBoundParameters.ContainsKey($seam) -and -not $OfflineTestMode) {
        throw "DVE_TEST_SEAM_IN_PRODUCTION -$seam is a test-only override and is refused without -OfflineTestMode: consent and the venue table are read only from the committed tracked files"
    }
}
if ($OfflineTestMode) {
    if (-not $PSBoundParameters.ContainsKey('UmRunScript')) { throw 'DVE_OFFLINE_TEST_REQUIRES_STUB_UMRUN offline test mode needs an explicit -UmRunScript stub; the real um-run.ps1 is never a default here' }
    $realUmRun = [IO.Path]::GetFullPath((Join-Path $here '..\um-run.ps1'))
    $stubFull = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $UmRunScript).Path)
    if ($stubFull -ieq $realUmRun -or ((Test-Path -LiteralPath $realUmRun -PathType Leaf) -and (Get-DvSha256OfFile $stubFull) -eq (Get-DvSha256OfFile $realUmRun))) {
        throw 'DVE_OFFLINE_TEST_REFUSES_REAL_UMRUN offline test mode can never use um-run.ps1 (by path or content): it would submit a job'
    }
}
if (-not [string]::IsNullOrWhiteSpace($SheetCopyDir) -and -not (Test-DvUnderClaudeState -Path $SheetCopyDir)) {
    throw 'DVE_SHEET_COPY_MUST_STAY_LOCAL -SheetCopyDir must be under a .claude-state directory; a sheet of owner footage is never committed, attached or published'
}

if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = (Resolve-Path (Join-Path $here '..\..\..')).Path }
if ([string]::IsNullOrWhiteSpace($UmRunScript)) { $UmRunScript = Join-Path $here '..\um-run.ps1' }
if ([string]::IsNullOrWhiteSpace($GeneratorScript)) { $GeneratorScript = Join-Path $here '..\bachelor\playback-attr-3-cuda-job.ps1' }
if ([string]::IsNullOrWhiteSpace($ReceiptRoot)) {
    # .claude-state is gitignored, so a worktree has none: resolve the MAIN checkout from the git common dir.
    $common = (& git -C $RepoRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
    $mainRoot = if ($LASTEXITCODE -eq 0 -and $common) { Split-Path -Parent ([string]$common) } else { $RepoRoot }
    $ReceiptRoot = Join-Path $mainRoot '.claude-state\dual-venue\receipts'
}
if (-not $OfflineTestMode -and -not (Test-DvUnderClaudeState -Path $ReceiptRoot)) {
    throw 'DVE_RECEIPT_ROOT_MUST_STAY_LOCAL -ReceiptRoot must be under a .claude-state directory: a receipt and its evidence name an owner clip''s run'
}
# The hashed run evidence (summary, manifest, result, run log, um-run record) goes in a sibling of the receipts directory. It names an
# owner clip's run too, so in production that directory must ALSO sit under .claude-state (a ReceiptRoot of `.claude-state` itself would not).
$evidenceBase = Join-Path (Split-Path -Parent $ReceiptRoot) 'evidence'
if (-not $OfflineTestMode -and -not (Test-DvUnderClaudeState -Path $evidenceBase)) {
    throw 'DVE_RECEIPT_ROOT_MUST_STAY_LOCAL the evidence directory beside -ReceiptRoot must be under a .claude-state directory: the run evidence names an owner clip''s run'
}
if ([string]::IsNullOrWhiteSpace($Actor)) { $Actor = "dual-venue-runner@$($env:COMPUTERNAME)" }
$runStamp = [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')

# --- where admission reads from: the COMMITTED tracked files (production) ---------------------------
$sources = Resolve-DvAdmissionSources -RepoRoot $RepoRoot -ConsentPath $ConsentPath -VenueTablePath $VenueTablePath -OfflineTestMode:$OfflineTestMode
$admissionRefusal = $null
$table = $null
if (-not $sources.ok) { $admissionRefusal = [string]$sources.reason }
else {
    try { $table = ConvertFrom-DvVenueTableText $sources.tableText } catch { $admissionRefusal = 'VENUE_TABLE_INVALID' }
}
$venueEntry = $null; $agentShare = ''; $agentRoot = ''
if ($null -ne $table) {
    $venueEntry = $table.venues.$Venue
    if ($null -eq $venueEntry) { throw "DVE_UNKNOWN_VENUE the venue table has no entry for '$Venue'" }
    $agentShare = [string]$venueEntry.agentShare
    $agentRoot = [string]$venueEntry.agentRoot
    if ($OfflineTestMode) {
        # Offline test mode can never reach a venue: every venue's share must be a LOCAL directory under the OS temp folder.
        $tempRoots = @([IO.Path]::GetTempPath(), $env:TEMP, $env:TMP) | Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('\') + '\' }
        foreach ($v in $table.venues.PSObject.Properties) {
            $share = [IO.Path]::GetFullPath([string]$v.Value.agentShare)
            if ($share.StartsWith('\\') -or @($tempRoots | Where-Object { $share.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }).Count -eq 0) {
                throw "DVE_OFFLINE_TEST_SHARE_NOT_LOCAL offline test mode needs every venue's agentShare under the OS temp folder (a UNC or real share is refused): '$($v.Name)'"
            }
        }
    }
}

$specBytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $LegSpec).Path)
# Line endings are normalised on BOTH sides of the committed-spec lookup (Get-DvLegSpecSha256): on this VM's default checkout (autocrlf=true) the working
# copy is CRLF and the committed blob is LF, and a raw-byte hash would refuse every committed spec as LEG_SPEC_NOT_COMMITTED (fable r1 B2).
$legSpecSha256 = Get-DvLegSpecSha256 $specBytes
$spec = [Text.Encoding]::UTF8.GetString($specBytes) | ConvertFrom-Json
if ($spec.schema -ne 'mlv-app/dual-venue-leg/v1') { throw "DVE_LEG_SPEC_INVALID schema is '$($spec.schema)', expected mlv-app/dual-venue-leg/v1" }
if ([string]::IsNullOrWhiteSpace($Backend)) { $Backend = [string]@($spec.backends)[0] }
if ($Backend -notin @($spec.backends)) { throw "DVE_LEG_SPEC_INVALID backend '$Backend' is not one of this leg's backends ($(@($spec.backends) -join ', '))" }
$isLook = ($spec.legType -eq 'look')
# The settled-playback window the job passes to the smoke runner (generator -PlaySeconds). The generator's own default
# is 25; the owner's floor is 20 and is enforced here before anything is submitted.
$playSeconds = 25
if ($null -ne $spec.PSObject.Properties['playSeconds']) { $playSeconds = [int]$spec.playSeconds }
$lookFlavor = $null
if ($isLook) { $lookFlavor = [string]$spec.look.lookFlavor; if ([string]::IsNullOrWhiteSpace($lookFlavor)) { $lookFlavor = 'classic' } }

$role = $null
if ($null -ne $table) { $role = Get-DvVenueRole -Table $table -Card ([string]$spec.card) -Venue $Venue }
$methodRel = 'tools/profiling/dual-venue/Invoke-VenueLeg.ps1'
$methodBlob = (& git -C $RepoRoot hash-object (Join-Path $here 'Invoke-VenueLeg.ps1') 2>$null)
$receipt = New-DvReceipt -Card ([string]$spec.card) -LegId ([string]$spec.legId) -DeclaredVenue $Venue -Role $role -Actor $Actor `
    -Method $methodRel -MethodBlobId ([string]$methodBlob) -Backend $Backend -LookFlavor $lookFlavor
$receipt.subject.buildManifestSha256 = $BuildManifestSha256
$receipt.subject.legSpecSha256 = $legSpecSha256
$receipt.subject.clipId = [string]$spec.clipId
# DVE-SCALE2-LOOK-LEG-1 r2: EVERY receipt says the playback scale the leg asked for; the scale the app rendered at is UNKNOWN until a run log says (step 7).
$scale = Get-DvScaleEvidence -Spec $spec -Backend $Backend -LogText $null
$receipt.scale = $scale
# What the leg is admitted ON: the committed consent-file and venue-table blob ids (production) -- recorded even for a refusal.
$receipt.admission = [ordered]@{
    mode = $sources.mode
    consentBlobSha = $sources.consentBlobSha
    venueTableBlobSha = $sources.venueTableBlobSha
    headCommit = $sources.headCommit
    consentLastCommit = $sources.consentLastCommit
    ownerLineSha256 = $null
    ownerRecordedUtc = $null
}

function Complete-Receipt([string]$Outcome, [string]$Detail) {
    $receipt.outcome = $Outcome
    $receipt.outcomeDetail = $Detail
    $receipt.subject.digest = Get-DvSubjectDigest -BuildManifestSha256 $BuildManifestSha256 -LegSpecSha256 $legSpecSha256 `
        -ClipId $spec.clipId -ClipContentSha256 $receipt.subject.clipContentSha256 -Backend $Backend -LookFlavor $lookFlavor
    try {
        $path = Write-DvReceipt -Receipt $receipt -ReceiptRoot $ReceiptRoot -RepoRoot $RepoRoot -OfflineTestMode:$OfflineTestMode
    } catch {
        $refusal = [string]$_.Exception.Message
        # DVE-LEG-TERMINALS-1 r2: a leg that ran never ends without a receipt. A PASS/FAIL the production validator refuses -- for ANY reason (a backend the counters
        # contradict, a proof that does not re-derive, ...) -- is written as a typed INVALID instead: the receipt already holds the leg's evidence, and the validator's
        # own reasons are named in outcomeDetail. Anything else the writer throws (an unwritable path, a repeat receipt id) still ends the run as DVE_RECEIPT_WRITE_FAILED.
        $invalidPrefix = 'DVE_RECEIPT_INVALID '
        if ($Outcome -cin @('PASS', 'FAIL') -and $refusal.StartsWith($invalidPrefix, [StringComparison]::Ordinal)) {
            $receipt.outcome = 'INVALID'
            $receipt.outcomeDetail = "INVALID: the job result was $Outcome ($Detail) but the receipt writer refuses it as a ${Outcome}: $($refusal.Substring($invalidPrefix.Length))"
            $Outcome = 'INVALID'
            try {
                $path = Write-DvReceipt -Receipt $receipt -ReceiptRoot $ReceiptRoot -RepoRoot $RepoRoot -OfflineTestMode:$OfflineTestMode
            } catch {
                Write-Output "DVE_RECEIPT_WRITE_FAILED $($_.Exception.Message)"
                exit 2
            }
        } else {
            Write-Output "DVE_RECEIPT_WRITE_FAILED $refusal"
            exit 2
        }
    }
    Write-Output "DVE_OUTCOME=$Outcome"
    # (the writer relabels an advisory production PASS/FAIL in outcomeDetail and stamps receipt.verification: say so here too)
    Write-Output "DVE_DETAIL=$($receipt.outcomeDetail)"
    if ($null -ne $receipt['verification']) { Write-Output "DVE_VERIFICATION=$($receipt['verification'].status)" }
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
    # A terminal reached mid-flow. Thrown (not exited) so every terminal funnels through the one catch below and
    # Complete-Receipt writes the receipt.
    throw [System.Management.Automation.RuntimeException]::new("DVE_TERMINAL|$Outcome|$Detail")
}

$workDirResolved = if ([string]::IsNullOrWhiteSpace($WorkDir)) { Join-Path ([IO.Path]::GetTempPath()) "dve-$runStamp-$([guid]::NewGuid().ToString('N').Substring(0,8))" } else { $WorkDir }
New-Item -ItemType Directory -Force -Path $workDirResolved | Out-Null
$jobStem = "dve-$($spec.legId)-$Venue-$Backend-$runStamp"

$terminal = $null
$run = $null
try {
    # --- 2. clip admission (P7 + the clip-length class) -- before anything is generated or submitted ---------
    function Stop-Refused([string]$Token) {
        # No P4 enum value names a refusal; DEVICE_UNAVAILABLE carries no signal and the typed reason is in
        # refusal / outcomeDetail. Path-free by construction: tokens only.
        $receipt.refusal = $Token
        Stop-Leg 'DEVICE_UNAVAILABLE' $Token
    }
    # The consent and the venue table must be the COMMITTED ones (production) before anything else is decided.
    if ($null -ne $admissionRefusal) { Stop-Refused $admissionRefusal }
    if ($playSeconds -lt 20) { Stop-Refused 'PLAY_WINDOW_TOO_SHORT' }
    $admission = Get-DvClipAdmission -ClipId ([string]$spec.clipId) -Venue $Venue -Table $table -ConsentText ([string]$sources.consentText) -RepoRoot $RepoRoot
    if (-not $admission.admitted) { Stop-Refused ([string]$admission.reason) }
    $receipt.admission.ownerLineSha256 = $admission.ownerLineSha256
    $receipt.admission.ownerRecordedUtc = $admission.recordedUtc
    # In production the leg spec (its criteria decide PASS vs FAIL) must be a spec COMMITTED at the revision the consent was read from:
    # Test-DvReceiptValid finds it there by its sha256, so a spec the caller wrote could never validate and is refused up front.
    if (-not $OfflineTestMode) {
        $committedSpec = Find-DvCommittedLegSpec -RepoRoot $RepoRoot -Commit ([string]$sources.headCommit) -LegSpecSha256 $legSpecSha256
        if (-not $committedSpec.ok) { Stop-Refused 'LEG_SPEC_NOT_COMMITTED' }
    }

    # --- 3. generate the job (local, no I/O on the venue) ----------------------------------------------
    # The generator resolves the clip by ID (it refuses a path, a fixture under 20 s, an unknown or unconsented id).
    # Generating BEFORE the health probe means a resolver refusal submits nothing at all.
    $jobFile = Join-Path $workDirResolved "$jobStem.job.ps1"
    $gen = @{
        SourceCommit = $SourceCommit; BuildManifestSha256 = $BuildManifestSha256; ClipId = [string]$spec.clipId
        OutFile = $jobFile; RepoRoot = $RepoRoot; PlaySeconds = $playSeconds
        Venue = $Venue; Backend = $Backend; ScaleFactor = [int]$spec.scaleFactor
    }
    # -UsePersistedPlaybackSettings leaves the smoke runner's own scale check OFF unless the job is told what to expect. The CUDA texture route clamps the request before the
    # runner reads it, so the value is the scale this backend is accepted to render at (the request, unless the spec declares a clamp): the check is live where the route allows.
    $gen['ExpectedScaleRequest'] = [int]$scale.acceptedEffectiveScale
    if ($OfflineTestMode -and -not [string]::IsNullOrWhiteSpace($VenueTablePath)) { $gen['VenueTablePath'] = $VenueTablePath }
    if ($null -ne $spec.PSObject.Properties['generatorArgs']) {
        if ($spec.generatorArgs.PSObject.Properties['telemetryArm']) { $gen['TelemetryArm'] = [string]$spec.generatorArgs.telemetryArm }
        if ($spec.generatorArgs.PSObject.Properties['cpuQuiescenceThresholdPercent']) { $gen['CpuQuiescenceThresholdPercent'] = [double]$spec.generatorArgs.cpuQuiescenceThresholdPercent }
    }
    if ($isLook) {
        $gen['ContactSheet'] = $true; $gen['ContactSheetFrames'] = [int]$spec.look.contactSheetFrames
        $gen['ForceLookAssist'] = $true; $gen['LookFlavor'] = $lookFlavor
    }
    try {
        $genOut = @(& $GeneratorScript @gen)
    } catch {
        # A typed generator refusal (owner id unknown / not consented / clip too short / bad argument) is a refusal
        # receipt, recorded as its TOKEN only (a message may echo a value). Anything untyped is a runner error.
        $first = ([string]$_.Exception.Message -split '\s+')[0]
        if ($first -match '^(PLAYBACK_ATTR3|DUAL_VENUE)_[A-Z0-9_]+$') { Stop-Refused "GENERATOR_REFUSED_$first" }
        throw
    }
    $generated = $genOut[-1]
    if ($null -ne $generated.PSObject.Properties['fixtureRehearsal'] -and [bool]$generated.fixtureRehearsal) { Stop-Refused 'FIXTURE_REFUSED_GENERATED_AS_REHEARSAL' }
    $contentSha = [string]$generated.clipContentSha256
    if ($contentSha -cnotmatch '^[0-9a-f]{64}$') { Stop-Refused 'CLIP_CONTENT_UNBOUND' }
    $receipt.subject.clipContentSha256 = $contentSha

    # --- 4. health probe (P5), bounded -------------------------------------------------------------
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

    # --- 5. the staged build / runner closure must be exactly the ones named -------------------------
    # (The clip itself is resolved and verified by the job at the venue, by id: the runner never stages, opens or names it.)
    $short = $SourceCommit.Substring(0, 12)
    $cacheShare = $agentShare.TrimEnd('\') + '\cache'
    $buildJson = "$cacheShare\playback-attr-3-cuda-$short-build.json"
    if (-not (Test-Path -LiteralPath $buildJson -PathType Leaf)) { Stop-Leg 'DEVICE_UNAVAILABLE' "BUILD_NOT_STAGED: no build manifest for $short on this venue" }
    if ((Get-DvSha256OfFile $buildJson) -ne $BuildManifestSha256) { Stop-Leg 'DEVICE_UNAVAILABLE' "BUILD_NOT_STAGED: the staged build manifest for $short is not sha256 $BuildManifestSha256 (a different build is never substituted)" }
    $runnerDir = "$cacheShare\$($generated.smokeRunnerClosureDirName)"
    if (-not (Test-Path -LiteralPath $runnerDir -PathType Container)) { Stop-Leg 'DEVICE_UNAVAILABLE' "SMOKE_RUNNER_NOT_STAGED: $($generated.smokeRunnerClosureDirName) is not in this venue's cache" }

    # --- 6. submit (the venue's settings are isolated by the app; the receipt proves it from the run log) -----
    $timeoutSec = [int]$generated.recommendedJobTimeoutSec + 120
    $run = Submit-VenueJob -ScriptPath $jobFile -JobId $jobStem -TimeoutSec $timeoutSec -QueueSec ([int]$spec.timeouts.queueWaitSec) -ClaimedExtraSec ([int]$spec.timeouts.extraClaimedWaitSec)
    $receipt.evidence.umRunOutcome = $run.umOutcome
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

$artifactsShare = $null
if ($stdout -match 'ARTIFACTS=(?<p>\S+)') { $artifactsShare = ConvertTo-ShareSidePath $Matches['p'] }
$summary = $null; $manifest = $null
$resultJson = $null; $runLogText = $null; $runLogSha = $null
$evidenceDir = Join-Path $evidenceBase $receipt.receiptId
if ($artifactsShare -and (Test-Path -LiteralPath $artifactsShare -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    # The run's OWN records are copied into the LOCAL evidence directory (under .claude-state in production: they name an owner clip's
    # run and stay local), hashed THERE, and everything below is parsed from those local copies -- so the receipt's sha256 claims
    # are over the exact bytes the proof was derived from, and Test-DvReceiptValid can re-hash and re-derive it later.
    foreach ($name in 'summary.json', 'evidence-manifest.json', 'artifact-index.json', 'result.json') {
        $src = Join-Path $artifactsShare $name
        if (Test-Path -LiteralPath $src -PathType Leaf) { Copy-Item -LiteralPath $src -Destination (Join-Path $evidenceDir $name) }
    }
    $logShare = Join-Path $artifactsShare 'logs\smoke-run.log'
    if (Test-Path -LiteralPath $logShare -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path (Join-Path $evidenceDir 'logs') | Out-Null
        Copy-Item -LiteralPath $logShare -Destination (Join-Path $evidenceDir 'logs\smoke-run.log')
    }
    # DVE-LEG-TERMINALS-1 item 3: a failed smoke run's FULL stdout and stderr (the job publishes them on SMOKE_RUN_FAILED; the typed tail in summary.json is only
    # the last 4000 chars) are kept beside the rest of the evidence and named by sha256 in the receipt, so the receipt can say why. The located app log of a run
    # that wrote no result.json is kept the same way. Optional: the job publishes them on SMOKE_RUN_FAILED and on a completed run, so any receipt with artifacts can carry them.
    foreach ($smokeFile in 'smoke-stderr.txt', 'smoke-stdout.txt') {
        $smokeSrc = Join-Path $artifactsShare $smokeFile
        if (Test-Path -LiteralPath $smokeSrc -PathType Leaf) {
            Copy-Item -LiteralPath $smokeSrc -Destination (Join-Path $evidenceDir $smokeFile)
            $receipt.evidence[$(if ($smokeFile -ceq 'smoke-stderr.txt') { 'smokeStderrSha256' } else { 'smokeStdoutSha256' })] = Get-DvSha256OfFile (Join-Path $evidenceDir $smokeFile)
        }
    }
    $failedAppLogShare = Join-Path $artifactsShare 'logs\smoke-failed-app.log'
    if (Test-Path -LiteralPath $failedAppLogShare -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path (Join-Path $evidenceDir 'logs') | Out-Null
        Copy-Item -LiteralPath $failedAppLogShare -Destination (Join-Path $evidenceDir 'logs\smoke-failed-app.log')
    }
    # The job's exit code and RESULT token are not in any file the job wrote: record them as a hashed file too, so the outcome is
    # re-derivable (a capture that exited non-zero is not evidence).
    $umRunLocal = Join-Path $evidenceDir 'um-run.json'
    $umRunBytes = [Text.UTF8Encoding]::new($false).GetBytes(([ordered]@{ schema = 'mlv-app/dual-venue-um-run/v1'; exitCode = $exitCode; resultToken = $token } | ConvertTo-Json -Compress) + "`n")
    [IO.File]::WriteAllBytes($umRunLocal, $umRunBytes)
    $receipt.evidence.umRunJsonSha256 = Get-DvSha256OfBytes $umRunBytes
    $summaryLocal = Join-Path $evidenceDir 'summary.json'
    $manifestLocal = Join-Path $evidenceDir 'evidence-manifest.json'
    $resultLocal = Join-Path $evidenceDir 'result.json'
    $logLocal = Join-Path $evidenceDir 'logs\smoke-run.log'
    if (Test-Path -LiteralPath $summaryLocal) {
        $receipt.evidence.summaryJsonSha256 = Get-DvSha256OfFile $summaryLocal
        $summary = [IO.File]::ReadAllText($summaryLocal) | ConvertFrom-Json
    }
    if (Test-Path -LiteralPath $manifestLocal) {
        $receipt.evidence.evidenceManifestSha256 = Get-DvSha256OfFile $manifestLocal
        $manifest = [IO.File]::ReadAllText($manifestLocal) | ConvertFrom-Json
    }
    $receipt.evidence.artifactIndexPath = $artifactsShare.TrimEnd('\') + '\artifact-index.json'
    $receipt.evidence.localEvidenceDir = $evidenceDir
    # The launcher's result.json (the nonce it generated, the sha256 of the run-log snapshot) and the app's run log (the summary line
    # the proof is re-derived from).
    if (Test-Path -LiteralPath $resultLocal -PathType Leaf) {
        $receipt.evidence.resultJsonSha256 = Get-DvSha256OfFile $resultLocal
        try { $resultJson = [IO.File]::ReadAllText($resultLocal) | ConvertFrom-Json } catch { $resultJson = $null }
    }
    if (Test-Path -LiteralPath $logLocal -PathType Leaf) {
        $logBytes = [IO.File]::ReadAllBytes($logLocal)
        $runLogSha = Get-DvSha256OfBytes $logBytes
        $receipt.evidence.logSha256 = $runLogSha
        $runLogText = [Text.Encoding]::UTF8.GetString($logBytes)
    }
}
if ($null -ne $summary) { $receipt.metrics = Get-DvVerbatimMetrics -Summary $summary -EvidenceManifest $manifest }
# The receipt oracle's verdict, RE-DERIVED from the run log and the launcher's result (not from a summary the job wrote about
# itself) and cross-checked against the job's own block.
$playback = $null
if ($null -ne $summary -or $null -ne $runLogText) {
    $playback = Get-DvPlaybackEvidence -Summary $summary -EvidenceManifest $manifest -ResultJson $resultJson -LogText $runLogText -LogSha256 $runLogSha -ExpectedClipId ([string]$spec.clipId)
    $receipt.playback = $playback
}
# The scale the app RENDERED at, from its own run log (never from the spec): the receipt carries both numbers and the leg cannot PASS under a scale it did not render at.
$scale = Get-DvScaleEvidence -Spec $spec -Backend $Backend -LogText $runLogText
$receipt.scale = $scale
$scaleNote = "requested scale $($scale.requestedScale), rendered at $($scale.effectiveScale)"
$smokeRefusalReason = $(if ($null -ne $summary -and $summary.PSObject.Properties['smokeRefusalReason']) { [string]$summary.smokeRefusalReason } else { '' })
$resolved = Resolve-DvJobOutcome -ResultToken $token -ExitCode $exitCode -SmokeRefusalReason $smokeRefusalReason
# A consumer of an evidence launcher ACTS on its exit code (master's consumer scan pins this statement): a job that prints a
# capture and then exits non-zero contradicts itself, so its capture is not evidence.
if ($exitCode -ne 0 -and $resolved.outcome -eq 'CAPTURED') {
    $resolved = [pscustomobject]@{ outcome = 'INVALID'; detail = "$($resolved.detail) but the job exited $exitCode; a capture that disagrees with its own exit code is not evidence" }
}

# DVE-LEG-TERMINALS-1 item 3: the receipt says why a smoke run failed, and where the evidence that proves it is kept (never a path: tokens and counts only).
if ($token -ceq 'SMOKE_RUN_FAILED' -and $null -ne $summary) {
    $smokeExit = $(if ($summary.PSObject.Properties['smokeExitCode']) { [string]$summary.smokeExitCode } else { '' })
    $kept = @()
    if ($receipt.evidence['smokeStderrSha256']) { $kept += 'stderr' }
    if ($receipt.evidence['smokeStdoutSha256']) { $kept += 'stdout' }
    if ($runLogSha) { $kept += 'run log' }
    $resolved.detail = "$($resolved.detail): smoke exit $smokeExit; " + $(if ($kept.Count -gt 0) { 'kept as hashed local evidence: ' + ($kept -join ', ') } else { 'no stderr, stdout or run log was published' })
}

# P6 again, from the job's own record: a summary that names a different venue than declared is a mismatch.
if ($null -ne $summary -and $summary.PSObject.Properties['display'] -and $summary.display -and $summary.display.PSObject.Properties['venue']) {
    $jobVenue = [string]$summary.display.venue
    if ($jobVenue -and $jobVenue -ne $Venue) {
        Complete-Receipt 'VENUE_HOST_MISMATCH' "the job's own display block says venue '$jobVenue' but '$Venue' was declared"
    }
}

# LOOK: copy the job's contact sheet + raw frames locally and bind the sheet by sha256. A venue that cannot compose the sheet
# (no Python/Pillow) publishes the raw frames and a compose-status marker; that is a venue condition, kept for local composition.
$composeUnavailable = $null
if ($isLook -and $artifactsShare) {
    $sheetShare = Join-Path $artifactsShare 'contact-sheet\sheet.png'
    $sheetDir = Join-Path $evidenceDir 'contact-sheet'
    $sheetInfo = $null
    $rawLocal = $null
    $rawShare = Join-Path $artifactsShare 'artifacts\contact-sheet\raw'
    if (-not (Test-Path -LiteralPath $rawShare -PathType Container)) { $rawShare = Join-Path $artifactsShare 'contact-sheet\raw' }
    $composeShare = Join-Path $artifactsShare 'contact-sheet\compose-status.txt'
    if ((Test-Path -LiteralPath $sheetShare -PathType Leaf) -or (Test-Path -LiteralPath $composeShare -PathType Leaf)) {
        New-Item -ItemType Directory -Force -Path $sheetDir | Out-Null
        foreach ($n in 'stats.json', 'compose-status.txt') {
            $s = Join-Path $artifactsShare "contact-sheet\$n"
            if (Test-Path -LiteralPath $s -PathType Leaf) { Copy-Item -LiteralPath $s -Destination (Join-Path $sheetDir $n) }
        }
        if (Test-Path -LiteralPath $rawShare -PathType Container) {
            Copy-Item -LiteralPath $rawShare -Destination (Join-Path $sheetDir 'raw') -Recurse
            $rawLocal = Join-Path $sheetDir 'raw'
            # Hash EVERY captured frame and sidecar into a manifest the receipt names (sol r1 B4): the sheet reader takes only what it lists.
            # Top-level entries by NAME (never by a path prefix: a hosted runner's temp dir is an 8.3 short path whose FullName prefix differs). A
            # directory is listed as "<name>\" with a zero hash: that is not a plain frame name, so the validator refuses it (nothing nested is ever composed).
            $listing = @(Get-ChildItem -LiteralPath $rawLocal -Force | Sort-Object Name | ForEach-Object {
                [ordered]@{ name = $(if ($_.PSIsContainer) { $_.Name + '\' } else { $_.Name }); sha256 = $(if ($_.PSIsContainer) { '0' * 64 } else { (Get-DvSha256OfFile $_.FullName) }) } })
            $framesBytes = [Text.UTF8Encoding]::new($false).GetBytes(([ordered]@{ schema = 'mlv-app/dual-venue-contact-frames/v1'; files = $listing } | ConvertTo-Json -Depth 4) + "`n")
            [IO.File]::WriteAllBytes((Join-Path $evidenceDir 'contact-frames.json'), $framesBytes)
            $receipt.evidence.contactFramesJsonSha256 = Get-DvSha256OfBytes $framesBytes
        }
    }
    if (Test-Path -LiteralPath $sheetShare -PathType Leaf) {
        Copy-Item -LiteralPath $sheetShare -Destination (Join-Path $sheetDir 'sheet.png')
        $sheetLocal = Join-Path $sheetDir 'sheet.png'
        $sheetInfo = [ordered]@{ path = $sheetLocal; sha256 = (Get-DvSha256OfFile $sheetLocal); bytes = (Get-Item -LiteralPath $sheetLocal).Length; frames = [int]$spec.look.contactSheetFrames; backend = $Backend; rawFramesDir = $rawLocal }
        if (-not [string]::IsNullOrWhiteSpace($SheetCopyDir)) {
            New-Item -ItemType Directory -Force -Path $SheetCopyDir | Out-Null
            Copy-Item -LiteralPath $sheetLocal -Destination (Join-Path $SheetCopyDir "sheet-$($spec.legId)-$Venue-$Backend-$lookFlavor.png") -Force
        }
    } elseif (Test-Path -LiteralPath $composeShare -PathType Leaf) {
        $marker = ([IO.File]::ReadAllText($composeShare) -split "`r?`n")[0]
        if ($marker -match '^CONTACT_SHEET_COMPOSE_UNAVAILABLE') { $composeUnavailable = $marker }
    }
    # The flavor the APP says it applied (the job copies gui_smoke.visual_state look_assist_flavor into its summary): honoured only
    # when it equals the flavor this leg asked for. No summary at all = the job never ran = 'unknown'.
    $lookFlavorReported = $null
    $lookFlavorHonored = 'unknown'
    if ($null -ne $summary -and $summary.PSObject.Properties['lookFlavorReported']) {
        $lookFlavorReported = [string]$summary.lookFlavorReported
        $lookFlavorHonored = ($lookFlavorReported -ceq $lookFlavor)
    }
    $receipt.look = [ordered]@{
        legType = 'look'; lookAssistForced = $true; lookFlavor = $lookFlavor
        lookFlavorReported = $lookFlavorReported
        lookFlavorHonored = $lookFlavorHonored
        contactSheet = $sheetInfo
        composeStatus = $composeUnavailable
        rawFramesDir = $(if ($null -eq $sheetInfo) { $rawLocal } else { $null })
    }
}

$outcome = $resolved.outcome; $detail = $resolved.detail
if ($outcome -eq 'CAPTURED') {
    $criteria = $spec.criteria.$role.$Backend
    $verdictCriteria = Test-DvCriteria -Criteria $criteria -Metrics $receipt.metrics
    $sheetMissing = $isLook -and ($null -eq $receipt.look -or $null -eq $receipt.look.contactSheet)
    # (a capture that published NO run log has no proof at all -- no rendered scale to read, no source frames -- and ends INVALID below, not here)
    if (-not $scale.honoured -and $null -ne $runLogText) {
        $declared = $(if ([int]$scale.acceptedEffectiveScale -ne [int]$scale.requestedScale) { "the spec declares this backend renders at $($scale.acceptedEffectiveScale)" } else { 'no clamp is declared for this backend' })
        $outcome = 'SCALE_NOT_HONOURED'
        $detail = "CAPTURED but $scaleNote (read from: $($scale.effectiveScaleSource); $declared). A run that did not render at the scale this leg names is no signal under it: the leg is not passed or failed"
    }
    elseif ($sheetMissing -and $null -ne $composeUnavailable) { $outcome = 'VENUE_TOOLING'; $detail = "CAPTURED but the venue could not compose the LOOK contact sheet ($composeUnavailable); the raw frames are kept locally for composition elsewhere. A venue condition, not a product result" }
    elseif ($sheetMissing) { $outcome = 'FAIL'; $detail = 'CAPTURED but the LOOK leg produced no contact sheet' }
    elseif (-not $verdictCriteria.pass) { $outcome = 'FAIL'; $detail = 'CAPTURED; criteria failed: ' + ($verdictCriteria.failures -join '; ') }
    else { $outcome = 'PASS'; $detail = $(if ($verdictCriteria.informational) { 'CAPTURED; no gating criteria for this role/backend (informational)' } else { 'CAPTURED; every criterion for this role/backend held' }) }
    # A declared clamp (the CUDA texture route renders at 1 whatever the request) still passes, but never silently: the detail carries both numbers.
    if ($outcome -in @('PASS', 'FAIL') -and $scale.verdict -ceq 'DECLARED_CLAMP') { $detail += "; $scaleNote (the spec declares this backend renders at $($scale.acceptedEffectiveScale))" }
}
# A signal needs its proof. A PASS or FAIL whose receipt-oracle verdict is not valid (under 20 s of source frames, a wrap,
# a foreign run, a fixture, or the verdict simply absent) is INVALID: a FAIL on footage that cannot be shown to be long
# enough is not a product result either. A product FAIL the job reached AFTER its oracle passed (GPU_RECON_FRAMES_ZERO,
# CPU_FALLBACK_DETECTED, CPU_BACKEND_PATH_MISMATCH) carries the same re-derived proof from the run log, so it stays a FAIL; a
# terminal with no run log (SMOKE_LOG_UNAVAILABLE, a SMOKE_RUN_FAILED whose run published none, ...) has none and is INVALID (a SMOKE_RUN_FAILED
# that did publish its run log is INVALID by its result token, whatever the log proves). (Write-DvReceipt refuses a proofless PASS/FAIL a second
# time, and Complete-Receipt then writes whatever it refuses as a typed INVALID.)
$proofProblems = @()
if ($outcome -in @('PASS', 'FAIL')) {
    if ($null -eq $playback) { $proofProblems += 'the job published no summary.json and no run log, so no source-frame proof exists' }
    else {
        $proofProblems += @($playback.invalidReasons)
        if ($resolved.outcome -eq 'CAPTURED' -and -not $playback.jobOracleBlockPresent) { $proofProblems += 'RECEIPT_FIELD_ABSENT: the captured job''s summary.json carries no sourceFrames block (its own oracle did not run)' }
    }
}
# DVE-LEG-TERMINALS-1 item 1: a PASS/FAIL the production writer would refuse because the hashed summary names no backend (the PresentMon-wait-failure shape of
# 2026-10-02 carried no counters) is no signal: the leg ran, so it ends as a typed INVALID receipt that keeps its evidence -- never exit 2 with no receipt.
$backendNotDerivable = Get-DvBackendNotDerivable -Summary $summary -Backend $Backend -RequireCpuBackendField (-not $OfflineTestMode)
if ($outcome -in @('PASS', 'FAIL') -and $null -ne $summary -and $null -ne $backendNotDerivable) { $proofProblems += $backendNotDerivable }
if ($outcome -in @('PASS', 'FAIL') -and $proofProblems.Count -gt 0) {
    $detail = "INVALID: the job result was $outcome ($detail) but the receipt-oracle verdict is not valid: $($proofProblems -join '; ')"
    $outcome = 'INVALID'
}
Complete-Receipt $outcome $detail
