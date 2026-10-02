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
$rawDirs = @{}
foreach ($r in $cuda, $cpu) {
    if ($r.outcome -ne 'PASS' -and $r.outcome -ne 'FAIL') { throw "PAIR_RECEIPT_INCOMPLETE receipt $($r.receiptId) is $($r.outcome), not PASS/FAIL" }
    # The evidence-bearing validator: the receipt's admission is re-derived from the committed blobs and its run from the hashed
    # evidence files (a receipt whose evidence is absent is INCOMPLETE). Nothing the receipt says about itself is believed.
    $validity = Test-DvReceiptValid -Receipt $r -RepoRoot $repoRoot
    if (-not $validity.valid) { throw "PAIR_RECEIPT_$($validity.status) receipt $($r.receiptId) is not valid evidence: $(@($validity.reasons) -join '; ')" }
    if ([string]$r.subject.clipId -cnotmatch '^[A-Za-z]\d{2}-\d{3,4}$') { throw 'PAIR_NOT_A_CONSENTED_CLIP only a consented clip id (never a fixture or a path) can be paired into a sheet' }
    # The frames are read from the verified evidence directory, never from a path the receipt asserts.
    $raw = Join-Path ([string]$r.evidence.localEvidenceDir) 'contact-sheet\raw'
    if (-not (Test-Path -LiteralPath $raw -PathType Container)) { throw "PAIR_NO_FRAMES receipt $($r.receiptId) has no raw contact-sheet frames in its evidence directory" }
    $rawDirs[[string]$r.receiptId] = $raw
}
foreach ($f in 'buildManifestSha256', 'legSpecSha256', 'clipId', 'clipContentSha256', 'lookFlavor') {
    if ([string]$cuda.subject.$f -ne [string]$cpu.subject.$f) { throw "PAIR_SUBJECT_DIFFERS the receipts differ in subject.$f" }
}
if ($cuda.venue.name -ne $cpu.venue.name -or $cuda.legId -ne $cpu.legId -or $cuda.card -ne $cpu.card) { throw 'PAIR_VENUE_OR_LEG_DIFFERS the receipts are not the same venue/leg/card' }

$venue = [string]$cuda.venue.name; $flavor = if ($cuda.subject.lookFlavor) { [string]$cuda.subject.lookFlavor } else { 'classic' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$sheet = Join-Path $OutDir "sheet-$venue-cuda-vs-cpu-$flavor.png"
$stats = Join-Path $OutDir "sheet-$venue-cuda-vs-cpu-$flavor.stats.json"
$composer = Join-Path $PSScriptRoot '..\make-contact-sheet.py'
$pyArgs = @('-3', $composer, '--frames-dir', $rawDirs[[string]$cuda.receiptId], '--pair-dir', $rawDirs[[string]$cpu.receiptId],
    '--sheet-out', $sheet, '--stats-out', $stats, '--clip-id', [string]$cuda.subject.clipId,
    '--host', [string]$cuda.venue.hostName, '--gpu', (@($cuda.venue.gpuNames) -join ' / '), '--build-sha', ([string]$cuda.subject.buildManifestSha256).Substring(0, 12),
    '--left-label', 'cuda', '--right-label', 'cpu', '--cols', '1')
& py @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PAIR_COMPOSE_FAILED make-contact-sheet.py exited $LASTEXITCODE" }

$record = [ordered]@{
    schema = 'mlv-app/dual-venue-sheet-pair/v1'
    venue = $venue; card = $cuda.card; legId = $cuda.legId; lookFlavor = $flavor
    lookFlavorHonored = 'unknown'
    ownerFootage = $true
    localOnly = 'never committed, attached to a PR, published to the bus or as an artifact'
    cudaReceiptId = $cuda.receiptId; cpuReceiptId = $cpu.receiptId
    sheet = [ordered]@{ path = $sheet; sha256 = (Get-DvSha256OfFile $sheet); statsPath = $stats; statsSha256 = (Get-DvSha256OfFile $stats) }
    pairedBy = 'frame_index'
    createdUtc = [DateTime]::UtcNow.ToString('o')
    owner_verdict = $null
    model_verdicts = @()
}
$recordPath = Join-Path $OutDir "sheet-pair-$venue-$flavor.json"
$stream = [IO.File]::Open($recordPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)   # append-only: never overwrite
try { $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($record | ConvertTo-Json -Depth 6) + "`n"); $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
Write-Output "DVE_SHEET_PAIR=$sheet"
Write-Output "DVE_SHEET_PAIR_RECORD=$recordPath"
