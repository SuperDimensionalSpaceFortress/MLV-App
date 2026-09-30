<#
.SYNOPSIS
Dual-Venue Evidence reconciler (C3): reads per-venue receipts and reports them WITHOUT merging them.

.DESCRIPTION
Receipts (schema mlv-app/dual-venue-receipt/v1) live at
<root>\<card>\<legId>\<venue>\<receiptId>.json and are never edited. This tool only reads them:
  * each receipt is validated; a malformed one is reported as MALFORMED and never counted;
  * receipts group by card, leg and SUBJECT DIGEST; receipts of different digests are never compared;
  * per venue the LATEST receipt (finishedUtc, tie -> receiptId) is shown with its role and outcome;
  * a pair is COMPLETE only when every venue in the card's roles has a PASS/FAIL receipt at that digest,
    otherwise INCOMPLETE with the reason (missing | UNRESOLVED | RETRACTED | VENUE_UNHEALTHY | ...);
    the cross-venue delta is printed only for COMPLETE pairs and is labelled DIAGNOSTIC (P2: no merged verdict);
  * -AcceptanceFor <card> returns only receipts whose venue role is `acceptance` for that card (P3), or
    NO_ACCEPTANCE_EVIDENCE. A role recorded in a receipt that disagrees with venues.json is refused (ROLE_MISMATCH).

Exit codes: 0 report produced (acceptance mode: acceptance evidence present); 2 acceptance mode and
NO_ACCEPTANCE_EVIDENCE; 1 tool error (unreadable venue table, receipts over -MaxReceipts, ...). An exit code is a
convenience only: read the status field, never treat 0 as "accepted".

.PARAMETER ReceiptsRoot
Receipts root. Default: <main checkout>\.claude-state\dual-venue\receipts (MLV_DUAL_VENUE_RECEIPTS_ROOT overrides).
.PARAMETER VenueTable
venues.json (roles per card). Default: venues.json beside this script. If the file does not exist, recorded roles
are used but flagged unverified; a file that exists and is invalid is an error.
.PARAMETER Card
Limit the report to one card.
.PARAMETER LegId
Limit the report to one leg.
.PARAMETER AcceptanceFor
Acceptance query for one card.
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
    [switch]$Json,
    [int]$MaxReceipts = 20000
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'VenueEvidence.psm1') -Force

try {
    if (-not $ReceiptsRoot) { $ReceiptsRoot = Resolve-VeDefaultReceiptsRoot }
    if (-not $VenueTable) { $VenueTable = Join-Path $PSScriptRoot 'venues.json' }
    $report = Get-VenueEvidenceReport -ReceiptsRoot $ReceiptsRoot -VenueTablePath $VenueTable -Card $Card -LegId $LegId `
        -AcceptanceFor $AcceptanceFor -MaxReceipts $MaxReceipts
} catch {
    [Console]::Error.WriteLine("Get-VenueEvidence: $($_.Exception.Message)")
    exit 1
}

if ($Json) {
    Write-Output (ConvertTo-Json -InputObject $report -Depth 20)
} else {
    foreach ($line in (Format-VenueEvidenceText -Report $report)) { Write-Output $line }
}

if ($AcceptanceFor -and $report.acceptance.status -ne 'ACCEPTANCE_EVIDENCE') { exit 2 }
exit 0
