# New-VenueSheetPair.ps1 -- compose the side-by-side cuda|cpu contact sheet for ONE venue's LOOK leg
# (DUAL-VENUE-EVIDENCE-1, AMENDMENT 1 A2). RUN THIS ON THE VM.
#
# Takes the two per-backend receipts Invoke-VenueLeg.ps1 already wrote, refuses unless they are the
# same venue / leg / build / clip / flavor and differ only in backend, then pairs their raw captured
# frames BY FRAME INDEX with make-contact-sheet.py --pair-dir. Receipts are append-only and are not
# edited: the pair is written as its OWN record (mlv-app/dual-venue-sheet-pair/v1) that names both
# receipt ids and carries the sheet's sha256 and path.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\New-VenueSheetPair.ps1 `
#       -CudaReceipt <cuda receipt.json> -CpuReceipt <cpu receipt.json> -OutDir <dir>
#
# Round 2: every leg plays a CONSENTED OWNER CLIP (fixtures are never venue playback clips), so the sheet is a sheet
# of OWNER footage. It stays LOCAL: -OutDir must sit under a `.claude-state` directory (gitignored; never committed,
# PR-attached, bus-published or published as an artifact), and only a receipt that is itself valid evidence can be paired:
# Test-DvReceiptValid -RepoRoot (DUAL-VENUE-EVIDENCE-2) re-derives its admission from the committed consent / venue-table / leg-spec
# blobs and its run (>= 20 s of source frames, nonce, wrap, outcome) from the hashed evidence files; a receipt whose evidence is
# absent is INCOMPLETE and cannot be paired. The raw frames are read from the verified evidence directory, never from a receipt path.
#
# Round 2 (formal keys r1): (1) a PRODUCTION receipt is ADVISORY at best (no venue-held anchor: Test-DvReceiptValid returns status ADVISORY, valid=false,
# VENUE_ANCHOR_ABSENT), so the pair is a DIAGNOSTIC sheet from advisory receipts and its record says so; nothing here is a PASS. Any other status is
# refused. (2) The backend is derived by the validator from the hashed summary, so a cuda run can no longer be relabelled cpu to pair against itself.
# (3) Raw frames and sidecars are accepted ONLY as the hashed contact-frames manifest lists them (Read-DvContactFrames); they are staged from the
# very bytes that were hashed, and anything unlisted is refused. (4) Two receipts that share one evidence directory are refused.
# (5) subject.clipContentSha256, hostName and gpuNames are UNBOUND (no hashed artifact carries them): they are neither compared nor printed.
# DUAL-VENUE-EVIDENCE-3: a sheet shows ONLY the frames this run captured and the manifest lists. A listed sidecar may name no image but its own listed
# <stem>.png (Read-DvContactFrames refuses an absolute / parent-relative / other path), and the composer reads only the staged files, verifying each against
# the hash it is handed (--left-listed / --right-listed) at read time; it never falls back to a file outside the staging directory.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CudaReceipt,
    [Parameter(Mandatory = $true)][string]$CpuReceipt,
    [Parameter(Mandatory = $true)][string]$OutDir
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'DualVenueRunner.psm1') -Force

$cuda = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $CudaReceipt).Path) | ConvertFrom-Json
$cpu = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $CpuReceipt).Path) | ConvertFrom-Json

if ($cuda.subject.backend -ne 'cuda' -or $cpu.subject.backend -ne 'cpu') { throw 'PAIR_BACKENDS_WRONG the first receipt must be the cuda backend and the second the cpu backend' }
# The sheet is owner footage: it may only be written where it stays local.
$outFull = [IO.Path]::GetFullPath($OutDir)
if (@($outFull.Split([char[]]@('\', '/')) | Where-Object { $_ -ceq '.claude-state' }).Count -eq 0) {
    throw 'PAIR_OWNER_SHEET_MUST_STAY_LOCAL -OutDir must be under a .claude-state directory; a sheet of owner footage is never committed, attached or published'
}
# The repo whose COMMITTED consent and venue table a receipt is verified against is the one this script lives in (never a caller's).
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$evidenceDirs = @{}
$listedFrames = @{}
foreach ($r in $cuda, $cpu) {
    if ($r.outcome -ne 'PASS' -and $r.outcome -ne 'FAIL') { throw "PAIR_RECEIPT_INCOMPLETE receipt $($r.receiptId) is $($r.outcome), not PASS/FAIL" }
    # The evidence-bearing validator: the receipt's admission is re-derived from the committed blobs and its run from the hashed
    # evidence files (a receipt whose evidence is absent is INCOMPLETE). Nothing the receipt says about itself is believed.
    $validity = Test-DvReceiptValid -Receipt $r -RepoRoot $repoRoot
    if ($validity.status -cne 'ADVISORY') { throw "PAIR_RECEIPT_$($validity.status) receipt $($r.receiptId) is not valid evidence: $(@($validity.reasons) -join '; ')" }
    # The leg type is the COMMITTED spec's (found by the validator), not a field of the receipt: only a look leg has a sheet to pair.
    if ([string]$validity.legType -cne 'look') { throw "PAIR_NOT_A_LOOK_LEG receipt $($r.receiptId) is not a LOOK leg per its committed leg spec; only a look leg's frames can be paired" }
    if ([string]$r.subject.clipId -cnotmatch '^[A-Za-z]\d{2}-\d{3,4}$') { throw 'PAIR_NOT_A_CONSENTED_CLIP only a consented clip id (never a fixture or a path) can be paired into a sheet' }
    # The frames are read from the directory the validator verified, and ONLY as the hashed contact-frames manifest lists them.
    $evDir = [string]$validity.evidenceDir
    $listed = Read-DvContactFrames -Receipt $r -EvidenceDir $evDir
    if (-not $listed.ok) { throw "PAIR_FRAMES_NOT_LISTED receipt $($r.receiptId): $(@($listed.reasons) -join '; ')" }
    $evidenceDirs[[string]$r.receiptId] = [IO.Path]::GetFullPath($evDir).TrimEnd('\').ToLowerInvariant()
    $listedFrames[[string]$r.receiptId] = $listed
}
if ($evidenceDirs[[string]$cuda.receiptId] -ceq $evidenceDirs[[string]$cpu.receiptId]) { throw 'PAIR_SHARED_EVIDENCE the two receipts name one evidence directory; a cuda and a cpu leg have separate evidence' }
foreach ($f in 'buildManifestSha256', 'legSpecSha256', 'clipId', 'lookFlavor') {
    if ([string]$cuda.subject.$f -ne [string]$cpu.subject.$f) { throw "PAIR_SUBJECT_DIFFERS the receipts differ in subject.$f" }
}
if ($cuda.venue.name -ne $cpu.venue.name -or $cuda.legId -ne $cpu.legId -or $cuda.card -ne $cpu.card) { throw 'PAIR_VENUE_OR_LEG_DIFFERS the receipts are not the same venue/leg/card' }

$venue = [string]$cuda.venue.name; $flavor = if ($cuda.subject.lookFlavor) { [string]$cuda.subject.lookFlavor } else { 'classic' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
# Stage exactly the bytes that were hashed (a frame edited after the check can no longer be composed), one fresh directory per pair.
$stageRoot = Join-Path (Join-Path $OutDir '.pair-staging') ([guid]::NewGuid().ToString('N'))
$stageDirs = @{}
$stageListings = @{}
foreach ($label in 'cuda', 'cpu') {
    $rid = $(if ($label -ceq 'cuda') { [string]$cuda.receiptId } else { [string]$cpu.receiptId })
    $dir = Join-Path $stageRoot $label
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    foreach ($file in $listedFrames[$rid].files) { [IO.File]::WriteAllBytes((Join-Path $dir $file.name), [byte[]]$file.bytes) }
    $stageDirs[$label] = $dir
    # The hashes the composer verifies each staged file against AT READ TIME (beside the staging directory, never inside it): it reads only these files,
    # refuses any other, and refuses a sidecar that names an image outside its own directory (DUAL-VENUE-EVIDENCE-3).
    $listing = @($listedFrames[$rid].files | ForEach-Object { [ordered]@{ name = $_.name; sha256 = $_.sha256 } })
    $listingFile = Join-Path $stageRoot "$label.listed.json"
    [IO.File]::WriteAllBytes($listingFile, [Text.UTF8Encoding]::new($false).GetBytes(([ordered]@{ files = $listing } | ConvertTo-Json -Depth 4)))
    $stageListings[$label] = $listingFile
}
# The leg id is in every file name: two look legs of one venue and flavor (scale 4 and scale 2) must never overwrite or refuse each other's pair.
$legId = [string]$cuda.legId
$sheet = Join-Path $OutDir "sheet-$legId-$venue-cuda-vs-cpu-$flavor.png"
$stats = Join-Path $OutDir "sheet-$legId-$venue-cuda-vs-cpu-$flavor.stats.json"
$composer = Join-Path $PSScriptRoot '..\make-contact-sheet.py'
$pyArgs = @('-3', $composer, '--frames-dir', $stageDirs['cuda'], '--pair-dir', $stageDirs['cpu'],
    '--left-listed', $stageListings['cuda'], '--right-listed', $stageListings['cpu'],
    '--sheet-out', $sheet, '--stats-out', $stats, '--clip-id', [string]$cuda.subject.clipId,
    '--host', $venue, '--build-sha', ([string]$cuda.subject.buildManifestSha256).Substring(0, 12),
    '--left-label', 'cuda', '--right-label', 'cpu', '--cols', '1')
& py @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PAIR_COMPOSE_FAILED make-contact-sheet.py exited $LASTEXITCODE" }

# True only when both legs' own receipts say the app applied the requested flavor; false when either says it did not; 'unknown'
# for a receipt that predates the report (no look.lookFlavorHonored boolean).
$honoredCuda = $(if ($cuda.look -and $cuda.look.PSObject.Properties['lookFlavorHonored']) { $cuda.look.lookFlavorHonored } else { 'unknown' })
$honoredCpu = $(if ($cpu.look -and $cpu.look.PSObject.Properties['lookFlavorHonored']) { $cpu.look.lookFlavorHonored } else { 'unknown' })
$pairHonored = 'unknown'
if (($honoredCuda -is [bool]) -and ($honoredCpu -is [bool])) { $pairHonored = ($honoredCuda -and $honoredCpu) }
elseif (($honoredCuda -is [bool] -and -not $honoredCuda) -or ($honoredCpu -is [bool] -and -not $honoredCpu)) { $pairHonored = $false }

$record = [ordered]@{
    schema = 'mlv-app/dual-venue-sheet-pair/v1'
    venue = $venue; card = $cuda.card; legId = $cuda.legId; lookFlavor = $flavor
    lookFlavorHonored = $pairHonored
    ownerFootage = $true
    advisory = $true
    evidenceStatus = 'ADVISORY'
    advisoryNote = 'both receipts re-derive from committed consent and hashed run evidence, but no venue-held anchor exists (VENUE_ANCHOR_ABSENT): a diagnostic sheet, never a PASS'
    unbound = @('subject.clipContentSha256', 'venue.hostName', 'venue.gpuNames')
    localOnly = 'never committed, attached to a PR, published to the bus or as an artifact'
    cudaReceiptId = $cuda.receiptId; cpuReceiptId = $cpu.receiptId
    # The scale each side asked for and rendered at (re-derived by the validator above): a pair whose sides rendered at different scales shows a SCALE difference
    # as well as a backend one, and says so.
    cudaScale = [ordered]@{ requestedScale = $cuda.scale.requestedScale; effectiveScale = $cuda.scale.effectiveScale }
    cpuScale = [ordered]@{ requestedScale = $cpu.scale.requestedScale; effectiveScale = $cpu.scale.effectiveScale }
    scalesDiffer = ([string]$cuda.scale.effectiveScale -ne [string]$cpu.scale.effectiveScale)
    sheet = [ordered]@{ path = $sheet; sha256 = (Get-DvSha256OfFile $sheet); statsPath = $stats; statsSha256 = (Get-DvSha256OfFile $stats) }
    pairedBy = 'frame_index'
    createdUtc = [DateTime]::UtcNow.ToString('o')
    owner_verdict = $null
    model_verdicts = @()
}
$recordPath = Join-Path $OutDir "sheet-pair-$legId-$venue-$flavor.json"
$stream = [IO.File]::Open($recordPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)   # append-only: never overwrite
try { $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($record | ConvertTo-Json -Depth 6) + "`n"); $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
Write-Output "DVE_SHEET_PAIR=$sheet"
Write-Output "DVE_SHEET_PAIR_RECORD=$recordPath"
