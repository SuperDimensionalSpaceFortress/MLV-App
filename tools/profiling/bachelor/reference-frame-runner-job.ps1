# reference-frame-runner-job.ps1 -- GENERATOR (runs locally; nothing here runs on a venue).
# VENUE-REFERENCE-FRAME-JOB-1. Emits ONE job that runs tools/profiling/capture-reference-frame.ps1 on a venue
# (Bachelor, or Ultra-Magnus when Bachelor is held) against a STAGED build, on one consented clip, under the output-budget
# gate profile, and publishes the reference-frame candidate it produced.
#
# THE GAP THIS CLOSES (OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-1 design, NO_GO, 2026-10-19). No tracked venue job ran the capture
# script, and the script's argv did not match the gate's wb_locked profile (start frame 0 where the gate compares the clip's
# startFrame, no Look Assist or processing flags), so no known-good capture could be produced on a venue. This job is the
# missing consumer; capture-reference-frame.ps1 takes the profile as its own -StartFrame / -DisableLookAssist /
# -PlaybackProcessing arguments (defaults unchanged for every existing caller).
#
# WHAT IT DOES NOT DO. It never promotes anything: the candidate keeps status "candidate-unreviewed" (the capture script says
# so itself, and this job never writes "reviewed_instrumented_known_good"). It never weakens the capture script's receipt
# check -- source_advanced, the run nonce and exit 43 are the capture script's own, and the job treats any non-zero capture exit
# as a failed capture. It never writes venue-clip-consent.json: consent is READ through Get-DvClipAdmission, from the tracked
# file as COMMITTED (the owner's blanket record included, DualVenueRunner.psm1 Get-DvClipAdmission), and a clip it does not admit
# is refused here, with a typed code, before any job is written. It launches nothing at generation time.
#
# TWO COMMITS, NEVER ONE. -SourceCommit is the commit the staged build was built from (the build under test: it can be older than
# the gate-profile arguments). -ToolsCommit (default: HEAD of -RepoRoot) is the commit whose committed bytes supply the capture
# script, its dependency and the gate spec. A -ToolsCommit whose capture script predates -StartFrame / -DisableLookAssist /
# -PlaybackProcessing is refused (REFERENCE_FRAME_CAPTURE_SCRIPT_PREDATES_GATE_PROFILE) rather than failing at parameter binding on a venue.
#
# THE BUNDLE IS AN EXPLICIT, PINNED MANIFEST -- the pattern of attr3-stage-smoke-runner-job.ps1. The capture script is not
# launchable alone: it dot-sources gui-smoke-length-gate.ps1 through $PSScriptRoot. The closure (below, $CaptureClosure) is a
# fixed list. Every file's exact COMMITTED bytes (never the working tree) travel INLINE in the emitted job, base64-encoded, each
# with its baked sha256. On the venue the job decodes and verifies EVERY file before a single byte is written, writes them into
# its own fresh work directory, re-verifies each on disk, and re-verifies again immediately before launching the capture.
# Completeness is checked at generation time by a LITERAL REGEX over the committed capture script: every
# `Join-Path $PSScriptRoot '<name>'` it contains must name a bundle member, or the generator refuses
# (REFERENCE_FRAME_BUNDLE_INCOMPLETE). It is not Assert-AttrCudaClosureComplete's AST census (step 4 says why that one cannot be
# used here), so a load site spelled any other way is not seen by it; test_reference_frame_runner_job.py pins the same set.
#
# `<jobId>.job.ps1 -VerifyOnly` runs the read-only prefix (name safety, base64 decode, hash for every bundle file) and exits
# without creating or publishing anything.
#
# THE GATE PROFILE IS READ FROM THE COMMITTED SPEC, never typed: tools/gates/output-budget.json at -ToolsCommit supplies the
# profile (disableLookAssist, playbackProcessing, qualityMode) and the clip's startFrame, and the spec's sha256 is recorded in the
# job's result. A clip the spec does not name has no gate frame and is refused.
#
# RECEIPT PROFILES ARE REFUSED UNTIL THEY ARE SUPPORTED (CoS 81b6c553 on PR #529, r4). A profile whose playbackProcessing is 'receipt'
# (committed_receipt), or that names a receipt of its own, needs the committed .marxml the canonical gate passes as --receipt
# (compare-output-budget.ps1 hands its smoke runner spec.receipt.path). This job carries no receipt and capture-reference-frame.ps1 has no
# -Receipt, so it could only RECORD such a profile, never apply it. The generator refuses it (REFERENCE_FRAME_RECEIPT_PROFILE_UNSUPPORTED,
# nothing written), and an emitted job whose processing mode is 'receipt' refuses before it decodes, writes or launches anything (226
# receiptProfile). Hash-pinned receipt transport is the follow-up that would lift this.
#
# THE JOB'S IDENTITY IS ITS EFFECTIVE CONFIGURATION (r4). The job id -- and so its work tree and outbox name -- carries a digest of the build,
# the clip and its parts, the bundle, the gate profile id, the spec's sha256, every profile value the capture is given, -Seconds and
# -PresentedFrames. Two jobs that would capture differently never share a work tree or an outbox. The same configuration is the same job: a
# resubmission reuses the id, and the owned-root creation sets an unproven tree already standing there aside.
#
# THE CLIP IS A CLIP ID. Its parts are resolved by tools/gates/resolve_consented_clip.py (the one resolver; no path is ever typed
# or printed), carried in the job as base64, and each part's length and sha256 are re-checked on the venue before anything runs.
#
# NOTHING THIS JOB PUBLISHES NAMES A SOURCE PATH (F-01 footage-path-disclosure; the rule of playback-attr-3-cuda-job.ps1,
# ATTR3-FOOTAGE-BIND-1 PR-B). The capture writes its manifest (clip.path), its stdout and the app's log into the published outbox,
# so the capture is never handed the owner's path. The job pins each ORIGINAL part read-only (FileShare.Read, held until the
# capture has exited), builds the neutral owner-clip view of it under its own work directory (AttrCudaOwnerFootage.psm1: a file
# symbolic link where the venue can make one, else a verified byte copy, never a hard link), hashes each part THROUGH its view
# while pinned, proves the views intact before and after the capture, and passes -Clip only the view. The workspace is closed
# (handles released, view entries removed by identity) on every exit path after it was opened -- the bundle re-hash, the capture directory and
# the command build included (r4): a throw in that window is a typed step failure (219 / 227), never an untyped exit that leaves a view behind.
# A view that changed while the app read it (225) voids the capture: result.json says capture-failed AND the candidate manifest the capture
# wrote is stamped invalid in place (status 'invalid', invalid, invalid_reasons), so the manifest alone never reads as a candidate.
#
# NOT COVERED HERE (named, not hidden): no display wake and no PresentMon. If the venue's display is asleep the capture fails
# visibly (typed, non-zero) rather than producing a frame; the live run is OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-2.
#
# Usage:
#   pwsh -NoProfile -File "<repo>\tools\profiling\bachelor\reference-frame-runner-job.ps1" `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> -ClipId M16-1243 -OutFile <path>\<jobId>.job.ps1
# then submit the emitted job through um-run.ps1 like every other venue job (NA-7: um-run is the only writer to a venue share).
#
# Exit codes: 0 emitted; 21 refused (RESULT=REFERENCE_FRAME_JOB_REFUSED CODE=<token>, nothing written; a receipt profile is
# CODE=REFERENCE_FRAME_RECEIPT_PROFILE_UNSUPPORTED).
# The EMITTED job: 0 a candidate; a failed capture exits the capture's own code (all below 100: 43 its receipt check, 70 no exit code;
# one outside 1-199 is folded to 199); a failed step exits its own 2xx code, which no capture code reaches (204 bundleDecode,
# 206 artifactNameSafety, 208 buildVerify, 209 deploy, 218 footageVerify, 219 bundleReverify, 220 bundleWrite, 222 admissionMode,
# 223 publishResult, 224 footageView, 225 footageViewIntact, 226 receiptProfile, 227 captureSetup). The capture's raw code is result.json
# captureExitCode and the RESULT line's CAPTURE_EXIT (REFERENCE-FRAME-JOB-EXIT-CODE-SPACE-1).

[CmdletBinding()]
param(
    # The commit the STAGED BUILD was built from (cache file names, the build manifest's sourceCommit, the capture's recorded identity).
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    # The commit whose COMMITTED bytes supply the capture script, its dependencies and tools/gates/output-budget.json. It is NOT the build's
    # commit: the build under test may be older than the capture script's gate-profile arguments (the June-era build the known-good bridge needs).
    # Default: the HEAD of -RepoRoot, the tooling checkout this generator runs from.
    [ValidatePattern('^([0-9a-f]{40})?$')][string]$ToolsCommit = '',
    # The sha the assembler printed (MANIFEST_SHA256=) for the staged build: the job authenticates the cache's build.json
    # against it BEFORE reading a field of it, exactly as playback-attr-3-cuda-job.ps1 does.
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_-]{1,40}$')][string]$ClipId,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'bachelor',
    # The gate profile id in tools/gates/output-budget.json (wb_locked is the one the gate's wb arm compares). A profile that needs a committed
    # receipt (committed_receipt) is refused: this job cannot hand the capture a receipt yet.
    [ValidatePattern('^[a-z0-9_]{1,40}$')][string]$GateProfile = 'wb_locked',
    # capture-reference-frame.ps1's own -Seconds (its fail-closed timeout) and -PresentedFrames (-1 = auto: the smallest N that plays the floor).
    [ValidateRange(10, 3600)][int]$Seconds = 30,
    [ValidateRange(-1, 100000)][int]$PresentedFrames = -1,
    # Default: the venue table's agentRoot.
    [string]$AgentRoot = '',
    [string]$RepoRoot = '',
    # TEST SEAMS. Refused unless -OfflineTestMode (production reads the consent and the venue table only as committed). A job
    # generated in offline-test mode refuses to run on any host (it is not evidence), except under -VerifyOnly.
    [switch]$OfflineTestMode,
    [string]$ConsentPath = '',
    [string]$VenueTablePath = ''
)

$ErrorActionPreference = 'Stop'

foreach ($seam in 'ConsentPath', 'VenueTablePath') {
    if ($PSBoundParameters.ContainsKey($seam) -and -not $OfflineTestMode) {
        throw "REFERENCE_FRAME_TEST_SEAM_IN_PRODUCTION -$seam is a test-only override and is refused without -OfflineTestMode: consent and the venue table are read only from the committed tracked files"
    }
}

# A refusal is typed, path-free, and happens before anything is generated: nothing below this function has written a file.
function Stop-Refused([string]$Code) {
    if ([string]::IsNullOrWhiteSpace($Code)) { $Code = 'REFERENCE_FRAME_REFUSAL_UNTYPED' }
    Write-Output "RESULT=REFERENCE_FRAME_JOB_REFUSED CODE=$Code"
    exit 21
}

$dvDir = Join-Path $PSScriptRoot '..\dual-venue'
Import-Module (Join-Path $dvDir 'DualVenueRunner.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force

if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Join-Path $PSScriptRoot '..\..\..' }
try { $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path } catch { throw 'REFERENCE_FRAME_REPOROOT_INVALID -RepoRoot does not resolve to an existing path' }
if ([string]::IsNullOrWhiteSpace($ToolsCommit)) {
    $ToolsCommit = ([string](& git -C $RepoRoot rev-parse --verify HEAD 2>$null)).Trim()
    if ($ToolsCommit -cnotmatch '^[0-9a-f]{40}$') { Stop-Refused 'REFERENCE_FRAME_TOOLS_COMMIT_UNRESOLVED' }
}

# --- 1. consent: the ONE gate, from the committed files, before anything is resolved or generated ---------------------------------
$sourceArgs = @{ RepoRoot = $RepoRoot }
if ($OfflineTestMode) {
    $sourceArgs['OfflineTestMode'] = $true
    if ($PSBoundParameters.ContainsKey('ConsentPath')) { $sourceArgs['ConsentPath'] = $ConsentPath }
    if ($PSBoundParameters.ContainsKey('VenueTablePath')) { $sourceArgs['VenueTablePath'] = $VenueTablePath }
}
$sources = Resolve-DvAdmissionSources @sourceArgs
if (-not $sources.ok) { Stop-Refused ([string]$sources.reason) }
try { $table = ConvertFrom-DvVenueTableText $sources.tableText } catch { Stop-Refused 'VENUE_TABLE_INVALID' }
if ($null -eq $table.venues.PSObject.Properties[$Venue]) { Stop-Refused 'VENUE_UNKNOWN' }
$admission = Get-DvClipAdmission -ClipId $ClipId -Venue $Venue -Table $table -ConsentText ([string]$sources.consentText) -RepoRoot $RepoRoot
if (-not $admission.admitted) { Stop-Refused ([string]$admission.reason) }
$admissionMode = if ($OfflineTestMode) { 'offline-test' } else { 'production' }

# --- 2. the gate profile and the clip's start frame, from the spec COMMITTED at -ToolsCommit ----------------------------------------
$specRelativePath = 'tools/gates/output-budget.json'
$specText = $null
$specSha256 = $null
$specTemp = Join-Path ([IO.Path]::GetTempPath()) "refframe-spec-$([Guid]::NewGuid().ToString('N')).tmp"
try {
    $specBlob = Resolve-AttrCudaCommittedBlobId -RepoRoot $RepoRoot -Commit $ToolsCommit -RepoRelativePath $specRelativePath
    $specSha256 = Save-AttrCudaCommittedBlobBytes -RepoRoot $RepoRoot -BlobId $specBlob -Destination $specTemp
    $specText = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($specTemp))
} catch {
    Stop-Refused 'REFERENCE_FRAME_GATE_SPEC_UNRESOLVED'
} finally {
    if (Test-Path -LiteralPath $specTemp) { Remove-Item -LiteralPath $specTemp -Force -ErrorAction SilentlyContinue }
}
try { $spec = $specText | ConvertFrom-Json } catch { Stop-Refused 'REFERENCE_FRAME_GATE_SPEC_INVALID' }
$gateProfileSpec = @($spec.profiles | Where-Object { [string]$_.id -ceq $GateProfile }) | Select-Object -First 1
if ($null -eq $gateProfileSpec) { Stop-Refused 'REFERENCE_FRAME_GATE_PROFILE_UNKNOWN' }
# A receipt profile is refused, never recorded and dropped (header: RECEIPT PROFILES ARE REFUSED UNTIL THEY ARE SUPPORTED).
if ([string]$gateProfileSpec.playbackProcessing -ceq 'receipt' -or $null -ne $gateProfileSpec.PSObject.Properties['receipt']) { Stop-Refused 'REFERENCE_FRAME_RECEIPT_PROFILE_UNSUPPORTED' }
$specClip = @($spec.clips | Where-Object { [string]$_.id -ceq $ClipId }) | Select-Object -First 1
if ($null -eq $specClip) { Stop-Refused 'REFERENCE_FRAME_CLIP_NOT_IN_GATE_SPEC' }
$gateStartFrame = 0
if (-not [int]::TryParse([string]$specClip.startFrame, [ref]$gateStartFrame) -or $gateStartFrame -lt 0) { Stop-Refused 'REFERENCE_FRAME_GATE_START_FRAME_INVALID' }
$gateProcessing = [string]$gateProfileSpec.playbackProcessing
if ($gateProcessing -cnotin @('', 'auto', 'subset')) { Stop-Refused 'REFERENCE_FRAME_GATE_PROCESSING_INVALID' }
$gateQualityMode = [string]$gateProfileSpec.qualityMode
if ($gateQualityMode -cnotmatch '^[0-9]{0,2}$') { Stop-Refused 'REFERENCE_FRAME_GATE_QUALITY_MODE_INVALID' }
if ($gateProfileSpec.disableLookAssist -isnot [bool]) { Stop-Refused 'REFERENCE_FRAME_GATE_LOOK_ASSIST_INVALID' }
$disableLookAssistLiteral = if ([bool]$gateProfileSpec.disableLookAssist) { '$true' } else { '$false' }

# --- 3. the clip's parts, by id (generator-side only; no path is printed) -----------------------------------------------------------
# Mirrors playback-attr-3-cuda-job.ps1 Resolve-AttrCudaOwnerClipParts: the resolver is a CHILD PROCESS that writes paths only into a
# private --emit-json temp file; this generator reads it in-process and deletes it. Each part's path travels as base64 from here on.
function Resolve-ReferenceFrameClipParts([string]$Id) {
    $resolverPath = Join-Path $PSScriptRoot '..\..\gates\resolve_consented_clip.py'
    if (-not (Test-Path -LiteralPath $resolverPath -PathType Leaf)) { Stop-Refused 'REFERENCE_FRAME_CLIP_RESOLVER_MISSING' }
    $resolverPath = (Resolve-Path -LiteralPath $resolverPath).Path
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    $pyArgs = @()
    $pyExe = $null
    if ($null -ne $python) {
        $major = @(& $python.Source -c 'import sys; print(sys.version_info[0])' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $major.Count -eq 1 -and $major[0] -eq '3') { $pyExe = $python.Source }
    }
    if ($null -eq $pyExe) {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($null -eq $launcher) { Stop-Refused 'REFERENCE_FRAME_NO_PYTHON' }
        $pyExe = $launcher.Source; $pyArgs = @('-3')
    }
    $emitPath = Join-Path ([IO.Path]::GetTempPath()) "refframe-clip-$([Guid]::NewGuid().ToString('N')).json"
    try {
        [void](& $pyExe @pyArgs $resolverPath '--clip-id' $Id '--repo-root' $RepoRoot '--emit-json' $emitPath 2>&1)
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $emitPath -PathType Leaf)) { Stop-Refused 'REFERENCE_FRAME_CLIP_RESOLVE_REFUSED' }
        $resolved = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($emitPath)) | ConvertFrom-Json
    } finally {
        if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force -ErrorAction SilentlyContinue }
    }
    if ([string]$resolved.clipId -cne $Id -or @($resolved.parts).Count -eq 0) { Stop-Refused 'REFERENCE_FRAME_CLIP_RESOLVE_REFUSED' }
    $pathPattern = '^[A-Za-z]:(?:/[A-Za-z0-9_.-]+)+$'
    @(@($resolved.parts) | Sort-Object { [int]$_.index } | ForEach-Object {
        $sha = ([string]$_.sha256).ToLowerInvariant()
        if (([string]$_.path) -notmatch $pathPattern -or $sha -notmatch '^[0-9a-f]{64}$') { Stop-Refused 'REFERENCE_FRAME_CLIP_PART_INVALID' }
        [ordered]@{ index = [int]$_.index; pathBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$_.path)); length = [int64]$_.length; sha256 = $sha }
    })
}
$ownerParts = @(Resolve-ReferenceFrameClipParts -Id $ClipId)
$ownerPartsJson = ConvertTo-Json -InputObject @($ownerParts) -Compress -Depth 5

# --- 4. the hash-pinned bundle: the capture script's pinned closure, as COMMITTED at -ToolsCommit --------------------------------------
$CaptureClosure = @(
    'tools/profiling/capture-reference-frame.ps1',
    'tools/profiling/gui-smoke-length-gate.ps1'
)
$closure = @(Resolve-AttrCudaSmokeRunnerClosure -RepoRoot $RepoRoot -Commit $ToolsCommit -RepoRelativePaths $CaptureClosure)

function ConvertTo-ReferenceFramePsLiteral([string]$Value) { "'" + $Value.Replace("'", "''") + "'" }

$bundleTemp = Join-Path ([IO.Path]::GetTempPath()) "refframe-bundle-$([Guid]::NewGuid().ToString('N'))"
$bundleLines = @()
try {
    [void](New-Item -ItemType Directory -Path $bundleTemp -Force)
    foreach ($entry in $closure) {
        [void](Assert-AttrCudaSafeArtifactName -Name $entry.name)
        if ($entry.sha256 -notmatch '^[0-9a-f]{64}$') { throw "ATTRCUDA_BLOB_SHA_MALFORMED bundle file sha256 is not 64 lowercase hex for $($entry.name)" }
        $local = Join-Path $bundleTemp $entry.name
        $saved = Save-AttrCudaCommittedBlobBytes -RepoRoot $RepoRoot -BlobId $entry.blobId -Destination $local
        if ($saved -ne $entry.sha256) { throw "ATTRCUDA_BLOB_SHA_MALFORMED re-saved bytes for $($entry.name) hash to $saved, expected $($entry.sha256)" }
        # Completeness tripwire (the AST census Assert-AttrCudaClosureComplete cannot be used here: it does not classify the capture
        # script's own app launch, and its pinned exclusions live in a module this card does not touch). Every sibling the capture
        # script loads through $PSScriptRoot must be a bundle member, or the job would die at its own first load line on the venue.
        if ($entry.name -ceq 'capture-reference-frame.ps1') {
            # The tooling commit must carry the gate-profile arguments the job passes; one that predates them would die at parameter binding on the venue.
            $captureText = [IO.File]::ReadAllText($local)
            foreach ($needed in '\[int\]\$StartFrame', '\[switch\]\$DisableLookAssist', '\[string\]\$PlaybackProcessing') {
                if ($captureText -cnotmatch $needed) { Stop-Refused 'REFERENCE_FRAME_CAPTURE_SCRIPT_PREDATES_GATE_PROFILE' }
            }
            foreach ($loaded in [regex]::Matches([IO.File]::ReadAllText($local), "Join-Path \`$PSScriptRoot '([^']+)'")) {
                if (@($closure | Where-Object { $_.name -ceq $loaded.Groups[1].Value }).Count -eq 0) { Stop-Refused 'REFERENCE_FRAME_BUNDLE_INCOMPLETE' }
            }
        }
        $bundleLines += "    [pscustomobject]@{ name = $(ConvertTo-ReferenceFramePsLiteral $entry.name); sha256 = '$($entry.sha256)'; base64 = '$([Convert]::ToBase64String([IO.File]::ReadAllBytes($local)))' }"
    }
} finally {
    if (Test-Path -LiteralPath $bundleTemp) { Remove-Item -LiteralPath $bundleTemp -Recurse -Force -ErrorAction SilentlyContinue }
}
$bundleEntriesLiteral = "@(`r`n" + ($bundleLines -join ",`r`n") + "`r`n)"
$bundleDigest = Get-AttrCudaClosureDigestHex -Closure $closure

# --- 5. the staged build's cache names and the venue's agent root -------------------------------------------------------------------
$names = Get-AttrCudaArtifactNames -SourceCommit $SourceCommit
foreach ($n in @($names.exeName, $names.reconName, $names.packageZipName, $names.buildManifestName, $names.packageExeName)) { [void](Assert-AttrCudaSafeArtifactName -Name $n) }
$venueEntry = $table.venues.PSObject.Properties[$Venue].Value
if ([string]::IsNullOrWhiteSpace($AgentRoot)) { $AgentRoot = [string]$venueEntry.agentRoot }
if ($AgentRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.~\\-]+$') { throw 'REFERENCE_FRAME_AGENTROOT_INVALID -AgentRoot contains characters outside the allowlist' }

# The job's identity is its effective configuration (header: THE JOB'S IDENTITY IS ITS EFFECTIVE CONFIGURATION).
$identityText = @(
    'mlvapp.reference-frame-job-identity.v1', "source=$SourceCommit", "buildManifestSha256=$BuildManifestSha256", "venue=$Venue", "clip=$ClipId",
    "parts=$(@($ownerParts | ForEach-Object { $_.sha256 }) -join ',')", "bundle=$bundleDigest", "gateProfile=$GateProfile", "gateSpecSha256=$specSha256",
    "startFrame=$gateStartFrame", "disableLookAssist=$disableLookAssistLiteral", "playbackProcessing=$gateProcessing", "qualityMode=$gateQualityMode",
    "seconds=$Seconds", "presentedFrames=$PresentedFrames"
) -join "`n"
$jobIdentitySha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($identityText))).ToLowerInvariant()
$jobId = "reference-frame-$($SourceCommit.Substring(0,12))-$ClipId-$($jobIdentitySha256.Substring(0,12))"
[void](Assert-AttrCudaSafeArtifactName -Name $jobId)

$embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
    'Assert-AttrCudaSafeArtifactName',
    'Assert-AttrCudaDirectChild',
    'Assert-AttrCudaNoLinkBelowRoot',
    'Assert-AttrCudaWritableFileSlot',
    'Test-AttrCudaPathIsReparsePoint',
    'Read-AttrCudaBase64Payload',
    'Publish-AttrCudaBytes',
    'Publish-AttrCudaText',
    'Publish-AttrCudaFileCopy',
    'New-AttrCudaDirectory',
    'Remove-AttrCudaTree',
    'Remove-AttrCudaPartialFile',
    'Initialize-AttrCudaFileIdNative',
    'ConvertTo-AttrCudaFileIdObject',
    'Get-AttrCudaFileId',
    'Remove-AttrCudaFileByProof',
    'Remove-AttrCudaFileById',
    'Add-AttrCudaOwnedRecord',
    'Read-AttrCudaOwnedJournal',
    'Get-AttrCudaOwnershipProof',
    'New-AttrCudaOwnedRoot',
    'New-AttrCudaOwnedFileStream',
    'Add-AttrCudaTraceLine',
    'Get-AttrCudaFileSha256Blocks',
    'Test-AttrCudaFootagePart',
    'Assert-AttrCudaBuildManifest'
)
# F-01: the neutral owner-clip view, the read-only pin and the identity-checked cleanup, embedded VERBATIM from
# AttrCudaOwnerFootage.psm1 -- the same functions, extracted the same way, as playback-attr-3-cuda-job.ps1 embeds them.
$embeddedFunctions = $embeddedFunctions + "`r`n`r`n" + (Get-AttrCudaEmbeddedFunctionSource -ModulePath (Join-Path $PSScriptRoot 'AttrCudaOwnerFootage.psm1') -Name @(
    'Get-AttrCudaOwnerFootageNeutralName',
    'Assert-AttrCudaOwnerPartsNaming',
    'New-AttrCudaFileSymlink',
    'Test-AttrCudaSymlinkCapability',
    'Open-AttrCudaReadOnlyHandle',
    'Assert-AttrCudaOwnerFootageCopySpace',
    'New-AttrCudaOwnerFootageView',
    'Assert-AttrCudaOwnerFootageViewsIntact',
    'Close-AttrCudaOwnerFootageWorkspace'
))

# --- job body template (placeholders are substituted below, in one pass) -----------------------------------------------------------
$template = @'
# -VerifyOnly runs the read-only prefix (name safety, base64 decode, hash for every bundle file) and exits WITHOUT creating
# anything. The agent never passes it; the behavioural tests do.
param([switch]$VerifyOnly)

$ErrorActionPreference = 'Stop'
$JobId = '__JOB_ID__'
$JobIdentitySha256 = '__JOB_IDENTITY_SHA256__'
$AgentRoot = '__AGENT_ROOT__'
$SourceCommit = '__SOURCE_COMMIT__'
$ToolsCommit = '__TOOLS_COMMIT__'
$BuildManifestSha256 = '__BUILD_MANIFEST_SHA256__'
$ClipId = '__CLIP_ID__'
$Venue = '__VENUE__'
$AdmissionMode = '__ADMISSION_MODE__'
$ConsentKind = '__CONSENT_KIND__'
$GateProfileId = '__GATE_PROFILE_ID__'
$GateSpecSha256 = '__GATE_SPEC_SHA256__'
$StartFrame = __START_FRAME__
$PlaybackProcessing = '__PLAYBACK_PROCESSING__'
$QualityMode = '__QUALITY_MODE__'
$DisableLookAssist = __DISABLE_LOOK_ASSIST__
$Seconds = __SECONDS__
$PresentedFrames = __PRESENTED_FRAMES__
$ExeName = '__EXE_NAME__'
$ReconName = '__RECON_NAME__'
$PackageZipName = '__PACKAGE_ZIP_NAME__'
$PackageExeName = '__PACKAGE_EXE_NAME__'
$BuildManifestName = '__BUILD_MANIFEST_NAME__'
$OwnerPartsJson = '__OWNER_PARTS_JSON__'
$BundleEntries = __BUNDLE_ENTRIES__
$Cache = Join-Path $AgentRoot 'cache'
$Work = Join-Path $AgentRoot "work\$JobId"
$Pub = Join-Path $AgentRoot "outbox\$JobId.artifacts"
$OwnedJournal = Join-Path $AgentRoot '.attrcuda-owned.jsonl'

function Say([string]$Message) { Write-Output "[$JobId] $Message" }
function Get-ShaLower([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

# --- verifiers, embedded VERBATIM from tools/profiling/bachelor/AttrCudaArtifacts.psm1 --------
__EMBEDDED_FUNCTIONS__
# --- end embedded verifiers -------------------------------------------------------------------

$StepLog = [System.Collections.Specialized.OrderedDictionary]::new()
$PubReady = $false
# The owner-clip workspace (F-01): the pins on the originals, the view entries and their held handles. Every failure exit closes it.
$ownerViews = [System.Collections.Generic.List[object]]::new()
$ownerViewHandles = [System.Collections.Generic.List[object]]::new()
function Close-OwnerClipWorkspace {
    if ($ownerViewHandles.Count -gt 0 -or $ownerViews.Count -gt 0) {
        Close-AttrCudaOwnerFootageWorkspace -Handles @($ownerViewHandles) -Views @($ownerViews)
        $ownerViewHandles.Clear()
        $ownerViews.Clear()
    }
}

function Complete-Failed([int]$Code, [string]$Step, [string]$Message) {
    Close-OwnerClipWorkspace
    $StepLog[$Step] = $Code
    Say "FAIL step=$Step exit=$Code $Message"
    if ($PubReady -and (Test-Path -LiteralPath $Pub)) {
        try {
            [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value (([ordered]@{
                schema = 'mlvapp.reference-frame-runner.v1'; jobId = $JobId; exitCode = $Code; failedStep = $Step; message = $Message
                stagedOnHost = $env:COMPUTERNAME; steps = $StepLog
            }) | ConvertTo-Json -Depth 10))
        } catch {
            Say "RESULT_JSON_NOT_WRITTEN $($_.Exception.Message)"
        }
    }
    Write-Output "RESULT=REFERENCE_FRAME_FAILED STEP=$Step EXIT=$Code"
    exit $Code
}

function Assert-UnderAgentRoot([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($AgentRoot)
    if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "job-owned path '$Label' resolves outside the agent root: $full"
    }
}
foreach ($check in @(@{ path = $Cache; label = 'Cache' }, @{ path = $Work; label = 'Work' }, @{ path = $Pub; label = 'Pub' })) { Assert-UnderAgentRoot $check.path $check.label }

Say "START clip=$ClipId profile=$GateProfileId startFrame=$StartFrame bundleFiles=$($BundleEntries.Count) admission=$AdmissionMode"

# --- name safety, before anything is decoded, written or run -----------------------------------------------------------------------
try {
    foreach ($entry in $BundleEntries) { [void](Assert-AttrCudaSafeArtifactName -Name $entry.name) }
    foreach ($n in @($ExeName, $ReconName, $PackageZipName, $PackageExeName, $BuildManifestName)) { [void](Assert-AttrCudaSafeArtifactName -Name $n) }
} catch {
    Complete-Failed 206 'artifactNameSafety' $_.Exception.Message
}
$StepLog['artifactNameSafety'] = 0

# A receipt profile needs the committed receipt the canonical gate passes as --receipt; this job carries none, so it could only record the
# profile, never apply it. Refused before anything is decoded, written or launched (the generator refuses it too).
if ($PlaybackProcessing -ceq 'receipt') {
    Complete-Failed 226 'receiptProfile' 'RECEIPT_PROFILE_UNSUPPORTED this job carries no committed receipt, so it cannot run a receipt profile'
}

# A job generated in offline-test mode is not evidence and never runs (VerifyOnly writes nothing, so it is allowed).
if ($AdmissionMode -ne 'production' -and -not $VerifyOnly) {
    Complete-Failed 222 'admissionMode' 'OFFLINE_TEST_JOB_REFUSED this job was generated with test seams and can never run'
}

# --- decode and verify EVERY bundle file BEFORE a single byte reaches disk --------------------------------------------------------
$decoded = @{}
try {
    foreach ($entry in $BundleEntries) {
        $payload = Read-AttrCudaBase64Payload -Base64 $entry.base64
        if ($payload.sha256 -ne $entry.sha256) {
            throw "sha256 mismatch for $($entry.name): baked $($entry.sha256), decoded $($payload.sha256)"
        }
        $decoded[$entry.name] = $payload.bytes
    }
} catch {
    Complete-Failed 204 'bundleDecode' $_.Exception.Message
}
$StepLog['bundleDecode'] = 0

if ($VerifyOnly) {
    Write-Output "RESULT=VERIFY_ONLY_OK FILES=$($BundleEntries.Count) CLIP=$ClipId PROFILE=$GateProfileId"
    exit 0
}

# --- job-owned directories, created fresh and recorded ------------------------------------------------------------------------------
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'work'))
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'outbox'))
$workRoot = New-AttrCudaOwnedRoot -TrustedRoot $AgentRoot -Path $Work -OwnedJournal $OwnedJournal
$pubRoot = New-AttrCudaOwnedRoot -TrustedRoot $AgentRoot -Path $Pub -OwnedJournal $OwnedJournal
if ($workRoot.Quarantined -ne '') { Say "SCRATCH an unproven tree stood at the work path; left untouched at $($workRoot.Quarantined)" }
if ($pubRoot.Quarantined -ne '') { Say "SCRATCH an unproven tree stood at the outbox path; left untouched at $($pubRoot.Quarantined)" }
$PubReady = $true
$Scratch = Join-Path $Work '.job-tmp'
[void](New-AttrCudaDirectory -Path $Scratch)
$env:TEMP = $Scratch
$env:TMP = $Scratch

# --- write the bundle into its own directory and re-verify every file on disk ---------------------------------------------------------
$BundleDir = Join-Path $Work 'bundle'
try {
    [void](New-AttrCudaDirectory -Path $BundleDir)
    foreach ($entry in $BundleEntries) {
        $entryPath = Join-Path $BundleDir $entry.name
        [void](Publish-AttrCudaBytes -Path $entryPath -Bytes $decoded[$entry.name] -OwnedJournal $OwnedJournal)
        if ((Get-ShaLower $entryPath) -ne $entry.sha256) { throw "sha256 did not round-trip onto disk for $($entry.name)" }
    }
} catch {
    Complete-Failed 220 'bundleWrite' $_.Exception.Message
}
$StepLog['bundleWrite'] = 0

# --- the staged build: authenticate the manifest BEFORE reading a field of it, then every cached artifact against it ------------------
try {
    $buildManifest = Assert-AttrCudaBuildManifest -Path (Join-Path $Cache $BuildManifestName) -ExpectedSha256 $BuildManifestSha256 -ExpectedSourceCommit $SourceCommit
    $artifactChecks = @(
        @{ label = 'packageZip'; path = (Join-Path $Cache $PackageZipName); expectedSha = [string]$buildManifest.packageZip.sha256 },
        @{ label = 'exe'; path = (Join-Path $Cache $ExeName); expectedSha = [string]$buildManifest.exe.sha256 },
        @{ label = 'dll'; path = (Join-Path $Cache $ReconName); expectedSha = [string]$buildManifest.dll.sha256 }
    )
    $artifactHashes = @{}
    foreach ($check in $artifactChecks) {
        if (-not (Test-Path -LiteralPath $check.path)) { throw "cache missing $($check.path)" }
        if ([string]::IsNullOrWhiteSpace($check.expectedSha)) { throw "build manifest $BuildManifestName is missing a sha256 for $($check.label)" }
        $artifactHashes[$check.label] = Get-ShaLower $check.path
        if ($artifactHashes[$check.label] -ne $check.expectedSha.ToLowerInvariant()) { throw "hash mismatch (vs build manifest $BuildManifestName) for $($check.path)" }
    }
} catch {
    Complete-Failed 208 'buildVerify' $_.Exception.Message
}
$StepLog['buildVerify'] = 0

# --- the clip: every part's existence and length, on THIS host, before anything runs (the content is hashed once, below, through its pinned
# view). A failure names the part index and a token, never a path. ---
$verifiedParts = @()
try {
    foreach ($p in @($OwnerPartsJson | ConvertFrom-Json)) {
        $partPath = ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$p.pathBase64))).Replace('/', '\')
        if ($partPath -notmatch '^[A-Za-z]:\\[A-Za-z0-9_.\\-]+$') { throw "part $($p.index) path is outside the allowlist" }
        $status = Test-AttrCudaFootagePart -Path $partPath -ExpectedLength ([int64]$p.length) -ExpectedSha256 ([string]$p.sha256) -LengthOnly
        if ($status -ne 'PASS') { throw "FOOTAGE_PART_$status part=$($p.index)" }
        $verifiedParts += [pscustomobject]@{ index = [int]$p.index; path = $partPath; length = [int64]$p.length; sha256 = [string]$p.sha256 }
    }
    if ($verifiedParts.Count -eq 0) { throw 'no clip parts' }
} catch {
    Complete-Failed 218 'footageVerify' $_.Exception.Message
}

# --- deploy: the package, then the staged exe and the DLL pair over it, each verified after the copy ------------------------------------------
try {
    $PkgRoot = Join-Path $Work 'pkg'
    Expand-Archive -LiteralPath (Join-Path $Cache $PackageZipName) -DestinationPath $PkgRoot -Force
    $baseExe = Get-ChildItem -LiteralPath $PkgRoot -Recurse -Filter $PackageExeName | Select-Object -First 1
    if (-not $baseExe) { throw "base package executable not found: $PackageExeName" }
    $pkgDir = $baseExe.Directory.FullName
    $exePath = Join-Path $pkgDir $ExeName
    $reconDll = Join-Path $pkgDir 'igpu_recon_cuda.dll'
    [void](Publish-AttrCudaFileCopy -Source (Join-Path $Cache $ExeName) -Destination $exePath)
    [void](Publish-AttrCudaFileCopy -Source (Join-Path $Cache $ReconName) -Destination $reconDll)
    if ((Get-ShaLower $exePath) -ne $artifactHashes['exe'] -or (Get-ShaLower $reconDll) -ne $artifactHashes['dll']) {
        throw 'deployed artifact hash verification failed (copy from cache did not round-trip)'
    }
} catch {
    Complete-Failed 209 'deploy' $_.Exception.Message
}
$StepLog['deploy'] = 0

# --- the neutral owner-clip view (F-01), the pattern of playback-attr-3-cuda-job.ps1 ------------------------------------------------------------
# Each ORIGINAL part is pinned read-only (FileShare.Read: no other process can write, truncate, rename or delete it) until the capture has
# exited, a neutrally-named view of it is built under $Work\owner-clip (never under $Pub), and the part's one full content read -- the
# sha256 against the consent record -- goes THROUGH that view while the pin is held, so the bytes the app reads are the bytes that were
# hashed. The capture is handed only the view of part 0; the app finds the continuation parts beside it under their neutral names.
# A failure carries only a token and the part index: an exception from opening an original can carry its path, so its text is never used.
$clipPath = $null
$ownerViewMode = ''
$part = $null
try {
    $ownerAssertedParts = Assert-AttrCudaOwnerPartsNaming -Parts @($verifiedParts)
    $OwnerClipDir = New-AttrCudaDirectory -Path (Join-Path $Work 'owner-clip')
    $ownerViewCapability = Test-AttrCudaSymlinkCapability -Directory $OwnerClipDir -Journal $OwnedJournal
    $ownerViewMode = if ($ownerViewCapability.Capable) { 'symlink' } else { 'copy' }
    if ($ownerViewMode -eq 'copy') { Assert-AttrCudaOwnerFootageCopySpace -Directory $OwnerClipDir -Parts @($ownerAssertedParts) }
    foreach ($part in $ownerAssertedParts) {
        $pin = Open-AttrCudaReadOnlyHandle -Path $part.path
        [void]$ownerViewHandles.Add($pin)
        $view = New-AttrCudaOwnerFootageView -Directory $OwnerClipDir -Index $part.index -SourcePath $part.path -PinStream $pin -Mode $ownerViewMode -Journal $OwnedJournal
        [void]$ownerViews.Add($view)
        [void]$ownerViewHandles.Add($view.ViewStream)
        $status = Test-AttrCudaFootagePart -Path $view.Path -ExpectedLength $part.length -ExpectedSha256 $part.sha256
        if ($status -ne 'PASS') { throw "FOOTAGE_PART_$status part=$($part.index)" }
        if ($part.index -eq 0) { $clipPath = $view.Path }
    }
    Assert-AttrCudaOwnerFootageViewsIntact -Views @($ownerViews)
    if ([string]::IsNullOrEmpty($clipPath)) { throw 'OWNER_FOOTAGE_VIEW_FAILED part 0' }
} catch {
    $message = [string]$_.Exception.Message
    if ($message -cmatch '^FOOTAGE_PART_[A-Z0-9_]+ part=\d+$') { Complete-Failed 218 'footageVerify' $message }
    $token = if ($message -cmatch '^OWNER_[A-Z_]+(?: part \d+)?') { $Matches[0] } else { "OWNER_FOOTAGE_VIEW_FAILED part $(if ($null -ne $part) { $part.index } else { -1 })" }
    Complete-Failed 224 'footageView' $token
}
$viewsIntactAfterCapture = $false
# THE CLEANUP WINDOW (r4): from the moment the views exist until the capture has returned, every exit closes the owner-clip workspace -- the
# finally below spans the bundle re-hash, the capture directory and the command build as well as the capture, and a throw in that window is
# a typed step failure (219 bundleReverify, 227 captureSetup), never an untyped exit that leaves a view behind.
try {
    $StepLog['footageVerify'] = 0
    $StepLog['footageView'] = 0
    Say "footage view mode=$ownerViewMode parts=$($ownerViews.Count) pinned"

    # --- run the capture script FROM THE VERIFIED BUNDLE, under the gate profile --------------------------------------------------------------
    $bundleCapture = Join-Path $BundleDir 'capture-reference-frame.ps1'
    foreach ($entry in $BundleEntries) {
        $onDisk = 'unreadable'
        try { $onDisk = Get-ShaLower (Join-Path $BundleDir $entry.name) } catch { $onDisk = 'unreadable' }
        if ($onDisk -ne $entry.sha256) { Complete-Failed 219 'bundleReverify' "bundle file $($entry.name) changed on disk after it was verified ($onDisk)" }
    }
    try {
        $captureOut = Join-Path $Pub 'capture'
        [void](New-AttrCudaDirectory -Path $captureOut)
        $captureCommand = "& '$bundleCapture' -Exe '$exePath' -Clip '$clipPath' -OutDir '$captureOut' -Commit '$SourceCommit' -ProfileId $GateProfileId -Seconds $Seconds -StartFrame $StartFrame"
        if ($DisableLookAssist) { $captureCommand += ' -DisableLookAssist' }
        if ($PlaybackProcessing -ne '') { $captureCommand += " -PlaybackProcessing $PlaybackProcessing" }
        if ($PresentedFrames -ge 0) { $captureCommand += " -PresentedFrames $PresentedFrames" }
        if ($QualityMode -ne '') { $captureCommand += " -ExtraEnv @{ MLVAPP_PLAYBACK_QUALITY_MODE = '$QualityMode' }" }
        # FAIL CLOSED: a statement-terminating error inside the capture command (a parameter that does not bind, say) leaves $LASTEXITCODE null,
        # and `exit $null` is exit 0 -- a capture that never ran must never read as one that passed. 70 = the capture command produced no exit code.
        $captureCommand += '; if ($null -eq $LASTEXITCODE) { exit 70 }; exit $LASTEXITCODE'
        Say "step capture start profile=$GateProfileId startFrame=$StartFrame lookAssistDisabled=$DisableLookAssist processing=$PlaybackProcessing quality=$QualityMode"
    } catch {
        Complete-Failed 227 'captureSetup' $_.Exception.Message
    }
    $captureOutput = @(& pwsh -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $captureCommand 2>&1 | ForEach-Object { "$_" })
    $captureRc = $LASTEXITCODE
    # The views must still be the ones built and hashed above: a view that changed while the app read it voids the capture.
    try { Assert-AttrCudaOwnerFootageViewsIntact -Views @($ownerViews); $viewsIntactAfterCapture = $true } catch { $viewsIntactAfterCapture = $false }
} finally {
    Close-OwnerClipWorkspace
}
try { [void](Publish-AttrCudaText -Path (Join-Path $Pub 'capture-stdout.txt') -Value ($captureOutput -join "`n")) } catch { Say "CAPTURE_STDOUT_NOT_WRITTEN $($_.Exception.Message)" }
# The capture script's own exit code is the verdict (0 = a valid candidate; 43 = its receipt check refused). It is never reinterpreted:
# a failed capture's code is the job's exit code, and no step code above shares it (REFERENCE-FRAME-JOB-EXIT-CODE-SPACE-1, below).
$StepLog['capture'] = $captureRc
# REFERENCE-FRAME-JOB-EXIT-CODE-SPACE-1: the capture's own code stays the job's exit code; the job's OWN step codes are 2xx, which no
# capture code reaches (the capture script's codes are all below 100). A code outside 1-199 (a crashed host, say) is folded to 199 so it
# cannot land in the step space either; the raw code is result.json captureExitCode and the RESULT line's CAPTURE_EXIT in every case.
$captureExitCode = $captureRc
if ($captureRc -ne 0 -and ($captureRc -lt 1 -or $captureRc -gt 199)) { $captureRc = 199 }
$viewsChanged = ($captureRc -eq 0 -and -not $viewsIntactAfterCapture)
if ($viewsChanged) { $StepLog['footageViewIntact'] = 225 }
$candidateOk = ($captureRc -eq 0 -and -not $viewsChanged)

$candidatePath = Join-Path $captureOut 'reference-frame-candidate.json'
$candidateSha256 = if ($candidateOk -and (Test-Path -LiteralPath $candidatePath)) { Get-ShaLower $candidatePath } else { $null }
# A capture voided by a changed view (225) leaves a manifest that still calls itself a candidate. Two layers say otherwise, and the in-place
# stamp is only the second: the manifest is rewritten invalid IN PLACE (the stamps of gui-smoke-length-gate.ps1 Set-GuiSmokeReceiptInvalid --
# invalid, invalid_reasons -- plus status 'invalid'), which is best effort, because it needs the manifest to parse and the rewrite to publish.
# So a sibling marker, reference-frame-candidate.invalid, is written FIRST and depends on neither: marker present = not a candidate, whatever
# the manifest says (tools/profiling/frame_provenance.py honours it). If neither layer can be written the manifest is moved out of the
# candidate name. $null = there was nothing to invalidate (or, for the quarantine, it was not needed); $false = that layer failed
# (result.json says capture-failed and the job exits 225 in every case).
$candidateMarkerPath = Join-Path $captureOut 'reference-frame-candidate.invalid'
$candidateInvalidated = $null
$candidateInvalidMarker = $null
$candidateQuarantined = $null
if ($viewsChanged -and (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
    $candidateInvalidated = $false
    $candidateInvalidMarker = $false
    $candidateInvalidReasons = @('OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE', 'reference-frame-runner step footageViewIntact exit 225')
    try {
        [void](Publish-AttrCudaText -Path $candidateMarkerPath -Value ([ordered]@{ schema = 'mlvapp.reference-frame-candidate-invalid.v1'; invalid = $true; invalid_reasons = $candidateInvalidReasons } | ConvertTo-Json -Depth 4))
        $candidateInvalidMarker = $true
    } catch {
        Say "CANDIDATE_MARKER_NOT_WRITTEN $($_.Exception.Message)"
    }
    try {
        $candidateDoc = [IO.File]::ReadAllText($candidatePath) | ConvertFrom-Json
        $candidateDoc | Add-Member -NotePropertyName status -NotePropertyValue 'invalid' -Force
        $candidateDoc | Add-Member -NotePropertyName invalid -NotePropertyValue $true -Force
        $candidateDoc | Add-Member -NotePropertyName invalid_reasons -NotePropertyValue $candidateInvalidReasons -Force
        [void](Publish-AttrCudaText -Path $candidatePath -Value ($candidateDoc | ConvertTo-Json -Depth 64))
        $candidateInvalidated = $true
    } catch {
        Say "CANDIDATE_NOT_INVALIDATED $($_.Exception.Message)"
    }
    if (-not $candidateInvalidated -and -not $candidateInvalidMarker) {
        $candidateQuarantined = $false
        try {
            Move-Item -LiteralPath $candidatePath -Destination (Join-Path $captureOut 'reference-frame-candidate.INVALID.json') -ErrorAction Stop
            $candidateQuarantined = $true
        } catch {
            Say "CANDIDATE_NOT_QUARANTINED $($_.Exception.Message)"
        }
    }
}
try { [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value (([ordered]@{
    schema = 'mlvapp.reference-frame-runner.v1'; jobId = $JobId; jobIdentitySha256 = $JobIdentitySha256
    exitCode = $(if ($viewsChanged) { 225 } else { $captureRc }); captureExitCode = $captureExitCode
    status = $(if ($candidateOk) { 'candidate-unreviewed' } else { 'capture-failed' })
    sourceCommit = $SourceCommit; toolsCommit = $ToolsCommit; buildManifestSha256 = $BuildManifestSha256; clipId = $ClipId; venue = $Venue
    admission = [ordered]@{ mode = $AdmissionMode; consentKind = $ConsentKind }
    gateProfile = [ordered]@{ id = $GateProfileId; specSha256 = $GateSpecSha256; startFrame = $StartFrame; disableLookAssist = $DisableLookAssist; playbackProcessing = $PlaybackProcessing; qualityMode = $QualityMode }
    bundle = @($BundleEntries | ForEach-Object { [ordered]@{ name = $_.name; sha256 = $_.sha256 } })
    captureParameters = [ordered]@{ seconds = $Seconds; presentedFrames = $PresentedFrames }
    exeSha256 = $artifactHashes['exe']; candidateSha256 = $candidateSha256; candidateInvalidated = $candidateInvalidated
    candidateInvalidMarker = $candidateInvalidMarker; candidateQuarantined = $candidateQuarantined
    clipParts = @($verifiedParts | ForEach-Object { [ordered]@{ index = $_.index; length = $_.length; sha256 = $_.sha256 } })
    footageView = [ordered]@{ mode = $ownerViewMode; intactAfterCapture = $viewsIntactAfterCapture }
    stagedOnHost = $env:COMPUTERNAME; steps = $StepLog
}) | ConvertTo-Json -Depth 10)) } catch { Complete-Failed 223 'publishResult' $_.Exception.Message }
if ($viewsChanged) {
    Write-Output "RESULT=REFERENCE_FRAME_FAILED STEP=footageViewIntact EXIT=225 ARTIFACTS=$Pub"
    exit 225
}
if ($captureRc -ne 0) {
    Write-Output "RESULT=REFERENCE_FRAME_CAPTURE_FAILED EXIT=$captureRc CAPTURE_EXIT=$captureExitCode ARTIFACTS=$Pub"
    exit $captureRc
}
Write-Output "RESULT=REFERENCE_FRAME_CAPTURE_OK CLIP=$ClipId PROFILE=$GateProfileId CANDIDATE_SHA256=$candidateSha256 ARTIFACTS=$Pub"
exit 0
'@

$jobDir = Split-Path -Parent ([IO.Path]::GetFullPath($OutFile))
if (-not (Test-Path -LiteralPath $jobDir)) { [void](New-Item -ItemType Directory -Path $jobDir -Force) }
$text = Expand-AttrCudaTemplate -Template $template -Tokens ([ordered]@{
    JOB_ID = $jobId
    JOB_IDENTITY_SHA256 = $jobIdentitySha256
    AGENT_ROOT = $AgentRoot
    SOURCE_COMMIT = $SourceCommit
    TOOLS_COMMIT = $ToolsCommit
    BUILD_MANIFEST_SHA256 = $BuildManifestSha256
    CLIP_ID = $ClipId
    VENUE = $Venue
    ADMISSION_MODE = $admissionMode
    CONSENT_KIND = ([string]$admission.consentKind)
    GATE_PROFILE_ID = $GateProfile
    GATE_SPEC_SHA256 = $specSha256
    START_FRAME = [string]$gateStartFrame
    PLAYBACK_PROCESSING = $gateProcessing
    QUALITY_MODE = $gateQualityMode
    DISABLE_LOOK_ASSIST = $disableLookAssistLiteral
    SECONDS = [string]$Seconds
    PRESENTED_FRAMES = [string]$PresentedFrames
    EXE_NAME = $names.exeName
    RECON_NAME = $names.reconName
    PACKAGE_ZIP_NAME = $names.packageZipName
    PACKAGE_EXE_NAME = $names.packageExeName
    BUILD_MANIFEST_NAME = $names.buildManifestName
    OWNER_PARTS_JSON = $ownerPartsJson
    BUNDLE_ENTRIES = $bundleEntriesLiteral
    EMBEDDED_FUNCTIONS = $embeddedFunctions
})
[IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($false))

Write-Output "RESULT=REFERENCE_FRAME_JOB_EMITTED SOURCE=$SourceCommit TOOLS=$ToolsCommit CLIP=$ClipId VENUE=$Venue GATE_PROFILE=$GateProfile START_FRAME=$gateStartFrame ADMISSION=$admissionMode BUNDLE_DIGEST=$bundleDigest JOB=$OutFile"

[pscustomobject]@{
    jobFile = $OutFile
    jobId = $jobId
    jobIdentitySha256 = $jobIdentitySha256
    sourceCommit = $SourceCommit
    toolsCommit = $ToolsCommit
    clipId = $ClipId
    venue = $Venue
    gateProfile = $GateProfile
    startFrame = $gateStartFrame
    bundleDigest = $bundleDigest
    files = @($closure | ForEach-Object { [ordered]@{ name = $_.name; sha256 = $_.sha256 } })
    agentRoot = $AgentRoot
}
