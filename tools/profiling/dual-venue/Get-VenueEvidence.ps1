<#
.SYNOPSIS
Dual-Venue Evidence reconciler (C3): reads per-venue receipts and reports them WITHOUT merging them.

.DESCRIPTION
Receipts (schema mlv-app/dual-venue-receipt/v1) live at
<root>\<card>\<legId>\<venue>\<receiptId>.json and are never edited. This tool only reads them:
  * each receipt is validated; a malformed one is reported as MALFORMED and never counted;
  * receipts group by card, leg and SUBJECT DIGEST; receipts of different digests are never compared;
  * per venue the LATEST receipt (finishedUtc; an exact tie goes to the non-signal, then FAIL, then PASS, then receiptId) is shown with its role and outcome;
    a refused run (looped, too short, ...) is that venue's latest attempt too, so the pair is INCOMPLETE with its reason;
  * a pair is COMPLETE only when every venue in the card's roles has a PASS/FAIL receipt at that digest,
    otherwise INCOMPLETE with the reason (missing | UNRESOLVED | RETRACTED | VENUE_UNHEALTHY | ...);
    the cross-venue delta is printed only for COMPLETE pairs and is labelled DIAGNOSTIC (P2: no merged verdict);
  * -AcceptanceFor <card> returns only receipts whose venue role is `acceptance` for that card (P3), or
    NO_ACCEPTANCE_EVIDENCE. A role recorded in a receipt that disagrees with venues.json is refused (ROLE_MISMATCH).
    The role is never taken from the receipt: acceptance REQUIRES a present, valid venue table that lists the card.
    No table (absent file, typo'd path) -> NO_ACCEPTANCE_EVIDENCE reason VENUE_TABLE_ABSENT; card not in the table ->
    reason CARD_NOT_IN_VENUE_TABLE. Report mode (no -AcceptanceFor) still shows recorded roles, marked UNVERIFIED.
  * ONLY THE NEWEST ATTEMPT PER LEG DECIDES (leg = card, legId, backend, lookFlavor, venue). Every receipt of a leg
    takes part in recency: valid PASS/FAIL, refused ones (INVALID_LOOPED, INVALID_CLIP_TOO_SHORT, VENUE_DETECTION_MISMATCH,
    ROLE_MISMATCH, DUPLICATE_RECEIPT_ID), withheld ones (UNRESOLVED, RETRACTED, VENUE_UNHEALTHY, ...), and receipt files
    that cannot be parsed (MALFORMED_NEWER_RECEIPT when the file has a readable finish time at least as new as the newest
    parsed receipt; UNORDERABLE_MALFORMED_RECEIPT when it has none). If the newest attempt is not a clean PASS/FAIL the
    leg is WITHHELD (reason named, the superseded PASS listed under supersededSignal) and the CARD is non-green:
    status NO_ACCEPTANCE_EVIDENCE, reason NEWEST_ATTEMPT_WITHHELD. There is never a fallback to an older receipt.
  * acceptance without -SubjectDigest/-BuildManifestSha256 lets the newest attempt speak at whatever digest it has
    (older digests are listed under olderDigests, never as evidence) and says so; each receipt shows
    buildManifestSha256, legSpecSha256, clipId, backend, lookFlavor and its outcome history. With a filter the newest
    attempt INSIDE the filter decides, and a newer attempt outside it is named under newerOutsideFilter.
  * a blank argument (-AcceptanceFor '', -SubjectDigest '', -LegId '', ...) is an error (exit 1), never "report mode"
    or "no filter". A *.json anywhere under the receipts root is seen; one misfiled off the layout is MALFORMED.
  * playback evidence rule (owner, 2026-09-30): a PASS/FAIL receipt counts only with metrics.clipSeconds >= 20
    (or clip_seconds) and metrics.wrapped == 0. wrapped=1 -> INVALID_LOOPED and clipSeconds < 20 ->
    INVALID_CLIP_TOO_SHORT (refused, never counted); fields absent -> UNVERIFIED_CLIP_LENGTH (excluded from acceptance).

Exit codes: 0 report produced; in acceptance mode 0 means acceptance evidence is present, every leg's newest attempt
is a clean PASS and nothing is withheld. 3 at least one returned receipt is FAIL. 2 acceptance mode and
NO_ACCEPTANCE_EVIDENCE (including a leg whose newest attempt is withheld). 1 tool error (unreadable or invalid venue table, bad filter, receipts over -MaxReceipts, ...).
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
    # A parameter that is BOUND but empty/blank is an error, never "use the default" / "no filter" / "report mode":
    # an unset caller variable must not silently change what is being asked. Only bound parameters are passed on.
    foreach ($n in 'ReceiptsRoot', 'VenueTable') {
        if ($PSBoundParameters.ContainsKey($n) -and [string]::IsNullOrWhiteSpace([string]$PSBoundParameters[$n])) {
            throw "-$n was supplied but is empty or blank; omit the parameter to use the default"
        }
    }
    if (-not $PSBoundParameters.ContainsKey('ReceiptsRoot')) { $ReceiptsRoot = Resolve-VeDefaultReceiptsRoot }
    if (-not $PSBoundParameters.ContainsKey('VenueTable')) { $VenueTable = Join-Path $PSScriptRoot 'venues.json' }
    $reportArgs = @{ ReceiptsRoot = $ReceiptsRoot; VenueTablePath = $VenueTable; MaxReceipts = $MaxReceipts }
    foreach ($n in 'Card', 'LegId', 'AcceptanceFor', 'SubjectDigest', 'BuildManifestSha256') {
        if ($PSBoundParameters.ContainsKey($n)) { $reportArgs[$n] = $PSBoundParameters[$n] }
    }
    $report = Get-VenueEvidenceReport @reportArgs
} catch {
    [Console]::Error.WriteLine("Get-VenueEvidence: $($_.Exception.Message)")
    exit 1
}

if ($Json) {
    Write-Output (ConvertTo-Json -InputObject $report -Depth 20)
} else {
    foreach ($line in (Format-VenueEvidenceText -Report $report)) { Write-Output $line }
}

if ($report.mode -eq 'acceptance') {
    # 3 first: a FAIL is never "no evidence". Then anything that is not a complete, current ACCEPTANCE_EVIDENCE is 2.
    if ($report.acceptance.failCount -gt 0) { exit 3 }
    if ($report.acceptance.status -ne 'ACCEPTANCE_EVIDENCE') { exit 2 }
}
exit 0
