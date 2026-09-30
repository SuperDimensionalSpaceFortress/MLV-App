<#
.SYNOPSIS
Dual-Venue Evidence reconciler (C3): reads per-venue receipts and reports them WITHOUT merging them.

.DESCRIPTION
Receipts (schema mlv-app/dual-venue-receipt/v1) live at
<root>\<card>\<legId>\<venue>\<receiptId>.json and are never edited. This tool only reads them:
  * each receipt is validated; a malformed one is reported as MALFORMED and never counted;
  * receipts group by card, leg and SUBJECT DIGEST; receipts of different digests are never compared;
  * per venue the LATEST receipt (finishedUtc; an exact tie goes to the non-signal, then FAIL, then PASS, then receiptId) is shown with its role and outcome;
  * a pair is COMPLETE only when every venue in the card's roles has a PASS/FAIL receipt at that digest,
    otherwise INCOMPLETE with the reason (missing | UNRESOLVED | RETRACTED | VENUE_UNHEALTHY | ...);
    the cross-venue delta is printed only for COMPLETE pairs and is labelled DIAGNOSTIC (P2: no merged verdict);
  * -AcceptanceFor <card> returns only receipts whose venue role is `acceptance` for that card (P3), or
    NO_ACCEPTANCE_EVIDENCE. A role recorded in a receipt that disagrees with venues.json is refused (ROLE_MISMATCH).
    The role is never taken from the receipt: acceptance REQUIRES a present, valid venue table that lists the card.
    No table (absent file, typo'd path) -> NO_ACCEPTANCE_EVIDENCE reason VENUE_TABLE_ABSENT; card not in the table ->
    reason CARD_NOT_IN_VENUE_TABLE. Report mode (no -AcceptanceFor) still shows recorded roles, marked UNVERIFIED.
  * acceptance without -SubjectDigest/-BuildManifestSha256 reports the NEWEST subject digest per leg/backend/venue
    (older digests are listed under olderDigests, never as evidence) and says so; each receipt shows
    buildManifestSha256, legSpecSha256, clipId, backend, lookFlavor and its outcome history.
  * playback evidence rule (owner, 2026-09-30): a PASS/FAIL receipt counts only with metrics.clipSeconds >= 20
    (or clip_seconds) and metrics.wrapped == 0. wrapped=1 -> INVALID_LOOPED and clipSeconds < 20 ->
    INVALID_CLIP_TOO_SHORT (refused, never counted); fields absent -> UNVERIFIED_CLIP_LENGTH (excluded from acceptance).

Exit codes: 0 report produced; in acceptance mode 0 means acceptance evidence is present AND every returned
receipt is PASS. 3 acceptance evidence is present but at least one returned receipt is FAIL. 2 acceptance mode and
NO_ACCEPTANCE_EVIDENCE. 1 tool error (unreadable or invalid venue table, bad filter, receipts over -MaxReceipts, ...).
Still read the status field: an exit code is a convenience, never treat 0 as "accepted" without it.

.PARAMETER ReceiptsRoot
Receipts root. Default: <main checkout>\.claude-state\dual-venue\receipts (MLV_DUAL_VENUE_RECEIPTS_ROOT overrides).
.PARAMETER VenueTable
venues.json (roles per card). Default: venues.json beside this script. Report mode: if the file does not exist,
recorded roles are shown but flagged UNVERIFIED. Acceptance mode: a missing file is NO_ACCEPTANCE_EVIDENCE
(VENUE_TABLE_ABSENT). A file that exists and is invalid is an error in both modes (exit 1).
.PARAMETER Card
Limit the report to one card.
.PARAMETER LegId
Limit the report to one leg.
.PARAMETER AcceptanceFor
Acceptance query for one card.
.PARAMETER SubjectDigest
Acceptance only: report receipts at exactly this subject digest (64-hex). Bad format is a tool error (exit 1).
.PARAMETER BuildManifestSha256
Acceptance only: report receipts whose subject.buildManifestSha256 equals this (64-hex), to bind evidence to the
candidate build. Combine with -SubjectDigest to require both.
.PARAMETER Json
Emit the report as JSON (schema mlv-app/dual-venue-evidence-report/v1).
.PARAMETER MaxReceipts
Upper bound on receipt files scanned; exceeding it is an error, never a silent truncation.
#>
[CmdletBinding()]
param(
    [string]$ReceiptsRoot,
    [string]$VenueTable,
    [string]$Card,
    [string]$LegId,
    [string]$AcceptanceFor,
    [string]$SubjectDigest,
    [string]$BuildManifestSha256,
    [switch]$Json,
    [int]$MaxReceipts = 20000
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'VenueEvidence.psm1') -Force

try {
    if (-not $ReceiptsRoot) { $ReceiptsRoot = Resolve-VeDefaultReceiptsRoot }
    if (-not $VenueTable) { $VenueTable = Join-Path $PSScriptRoot 'venues.json' }
    $report = Get-VenueEvidenceReport -ReceiptsRoot $ReceiptsRoot -VenueTablePath $VenueTable -Card $Card -LegId $LegId `
        -AcceptanceFor $AcceptanceFor -SubjectDigest $SubjectDigest -BuildManifestSha256 $BuildManifestSha256 -MaxReceipts $MaxReceipts
} catch {
    [Console]::Error.WriteLine("Get-VenueEvidence: $($_.Exception.Message)")
    exit 1
}

if ($Json) {
    Write-Output (ConvertTo-Json -InputObject $report -Depth 20)
} else {
    foreach ($line in (Format-VenueEvidenceText -Report $report)) { Write-Output $line }
}

if ($AcceptanceFor) {
    if ($report.acceptance.status -ne 'ACCEPTANCE_EVIDENCE') { exit 2 }
    if ($report.acceptance.failCount -gt 0) { exit 3 }
}
exit 0
