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
# Owner-footage receipts never reach here: the runner refuses owner clips, and this script refuses
# any receipt whose clip is not a tracked fixture id, so an owner sheet cannot be assembled by it.
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
foreach ($r in $cuda, $cpu) {
    if ($r.outcome -ne 'PASS' -and $r.outcome -ne 'FAIL') { throw "PAIR_RECEIPT_INCOMPLETE receipt $($r.receiptId) is $($r.outcome), not PASS/FAIL" }
    if ($null -eq $r.look -or $null -eq $r.look.contactSheet -or -not $r.look.contactSheet.rawFramesDir) { throw "PAIR_NO_FRAMES receipt $($r.receiptId) carries no raw contact-sheet frames" }
    if (@('tiny_dual_iso', 'large_dual_iso') -cnotcontains [string]$r.subject.clipId) { throw 'PAIR_NOT_A_FIXTURE only tracked fixtures can be paired into a sheet' }
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
$pyArgs = @('-3', $composer, '--frames-dir', $cuda.look.contactSheet.rawFramesDir, '--pair-dir', $cpu.look.contactSheet.rawFramesDir,
    '--sheet-out', $sheet, '--stats-out', $stats, '--clip-id', [string]$cuda.subject.clipId,
    '--host', [string]$cuda.venue.hostName, '--gpu', (@($cuda.venue.gpuNames) -join ' / '), '--build-sha', ([string]$cuda.subject.buildManifestSha256).Substring(0, 12),
    '--left-label', 'cuda', '--right-label', 'cpu', '--cols', '1')
& py @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PAIR_COMPOSE_FAILED make-contact-sheet.py exited $LASTEXITCODE" }

$record = [ordered]@{
    schema = 'mlv-app/dual-venue-sheet-pair/v1'
    venue = $venue; card = $cuda.card; legId = $cuda.legId; lookFlavor = $flavor
    lookFlavorHonored = 'unknown'
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
