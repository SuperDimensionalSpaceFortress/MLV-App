<#
.SYNOPSIS
Select-ReviewKeyB.ps1 - pick the lane that holds review key B for a card (no self-review).

.DESCRIPTION
MODEL-POSTURE-R16-1 hub ruling 2026-10-12. A formal review round has two keys:
  key A = sol   (found 51 blockers fable missed vs 1 the other way over 191 paired reviews)
  key B = opus, EXCEPT when the PR's implementing lane was opus -> then fable (no self-review).
fable stays the fallback key B and the planning/guidance lane.

The producer is read from the card's NEWEST PRODUCER receipt: every receipt under
<RunsDir>\<run>\*.receipt.json whose `card` equals -Card and whose `allowEdits` is true (a review
key never edits, so its receipts can never name the producer), ordered by `startedUtc`. Run
directories are named after their card by the dispatch convention, so only directories whose name
contains the card are opened; the receipt's own `card` field still has to match exactly. A card
with no producer receipt (a PR written by hand outside the lane runner) gets key B = opus with
producerKnown=false, so the caller can see the "no self-review" check was not evidence-backed.

Run it as a CHILD PROCESS, never dot-sourced: its param block (-Card, -RunsDir, -ProducerLane,
-Json) would overwrite the same-named variables of the script that dot-sources it.
    pwsh -NoProfile -File Select-ReviewKeyB.ps1 -Card <CARD> [-RunsDir DIR] [-ProducerLane LANE] [-Json]
It prints the key-B lane name (or, with -Json, one object: card, keyA, keyB, producerLane,
producerKnown, producerReceipt, reason, warnings). Exit 0 on a selection, 2 on a usage error
(unreadable RunsDir, empty card, unknown -ProducerLane).

HUB WIRING: see tools\coordination\review-key-pairing.md for the exact patch to
.claude-state\fleet-runs\hub-tools\new-review-keys.ps1 (board state, not tracked).

ASCII only. PowerShell 7+.
#>
[CmdletBinding()]
param(
    [string]$Card = '',
    # Where the lane run directories live. Default: the board's fleet-runs dir of the MAIN checkout.
    [string]$RunsDir = '',
    # Skip the receipt lookup and take the producer as given (a hub that already knows it).
    [string]$ProducerLane = '',
    [switch]$Json
)

$SELECT_KEY_B_VALID_LANES = @('opus', 'sonnet', 'fable', 'sol', 'luna', 'astra')
$SELECT_KEY_B_KEY_A = 'sol'

function Get-DefaultFleetRunsDir {
    $common = (& git -C $PSScriptRoot rev-parse --git-common-dir 2>$null)
    if ($LASTEXITCODE -eq 0 -and $common) {
        $root = Split-Path -Parent ([System.IO.Path]::GetFullPath(([string]$common).Trim(), $PSScriptRoot))
        return (Join-Path $root '.claude-state\fleet-runs')
    }
    return (Join-Path (Get-Location).Path '.claude-state\fleet-runs')
}

# -> the newest producer receipt of the card as @{ lane; path; startedUtc }, or $null. Never throws
# on one bad receipt: an unreadable or lane-less file is skipped.
function Get-CardProducerReceipt {
    param([Parameter(Mandatory = $true)][string]$Card, [Parameter(Mandatory = $true)][string]$RunsDir)
    $best = $null
    $dirs = @(Get-ChildItem -LiteralPath $RunsDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name.IndexOf($Card, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 })
    foreach ($dir in $dirs) {
        foreach ($file in @(Get-ChildItem -LiteralPath $dir.FullName -Filter '*.receipt.json' -File -ErrorAction SilentlyContinue)) {
            $receipt = $null
            try { $receipt = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json } catch { continue }
            if ($receipt -isnot [System.Management.Automation.PSCustomObject]) { continue }
            $props = $receipt.PSObject.Properties
            if ($null -eq $props['card'] -or [string]$props['card'].Value -cne $Card) { continue }
            if ($null -eq $props['allowEdits'] -or $props['allowEdits'].Value -ne $true) { continue }
            if ($null -eq $props['lane'] -or [string]::IsNullOrWhiteSpace([string]$props['lane'].Value)) { continue }
            $started = [DateTimeOffset]::MinValue
            $startedProp = $props['startedUtc']
            if ($null -eq $startedProp -or -not [DateTimeOffset]::TryParse([string]$startedProp.Value,
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    [System.Globalization.DateTimeStyles]::AssumeUniversal, [ref]$started)) { continue }
            if ($null -eq $best -or $started -gt $best.started) {
                $best = @{ lane = [string]$props['lane'].Value; path = $file.FullName; started = $started }
            }
        }
    }
    return $best
}

function Select-ReviewKeyB {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Card,
        [string]$RunsDir = '',
        [string]$ProducerLane = ''
    )
    if ([string]::IsNullOrWhiteSpace($Card)) { throw 'Select-ReviewKeyB: -Card is required' }
    $warnings = [System.Collections.Generic.List[string]]::new()
    $chosenProducer = $null; $chosenReceipt = $null; $reason = $null
    if ($ProducerLane) {
        if ($SELECT_KEY_B_VALID_LANES -cnotcontains $ProducerLane) {
            throw "Select-ReviewKeyB: unknown -ProducerLane '$ProducerLane' (expected one of $($SELECT_KEY_B_VALID_LANES -join ', '))"
        }
        $chosenProducer = $ProducerLane; $reason = 'producer-given'
    } else {
        if (-not $RunsDir) { $RunsDir = Get-DefaultFleetRunsDir }
        if (-not (Test-Path -LiteralPath $RunsDir -PathType Container)) { throw "Select-ReviewKeyB: RunsDir not found: $RunsDir" }
        $found = Get-CardProducerReceipt -Card $Card -RunsDir $RunsDir
        if ($null -ne $found) {
            $chosenProducer = $found.lane; $chosenReceipt = $found.path; $reason = 'producer-receipt'
        } else {
            $reason = 'no-producer-receipt'
            $warnings.Add("no producer receipt (allowEdits=true, card '$Card') under $RunsDir; the no-self-review check had no evidence, key B defaults to opus")
        }
    }
    $keyB = if ($chosenProducer -ceq 'opus') { 'fable' } else { 'opus' }
    if ($chosenProducer -ceq 'opus') { $reason += '; opus implemented, so key B falls back to fable (no self-review)' }
    if ($chosenProducer -ceq $SELECT_KEY_B_KEY_A) { $warnings.Add('producer lane is sol, which is also key A: key A would review its own work; the hub must pick another key A') }
    return [pscustomobject][ordered]@{
        card = $Card; keyA = $SELECT_KEY_B_KEY_A; keyB = $keyB; producerLane = $chosenProducer
        producerKnown = ($null -ne $chosenProducer); producerReceipt = $chosenReceipt
        reason = $reason; warnings = @($warnings)
    }
}

try {
    $selection = Select-ReviewKeyB -Card $Card -RunsDir $RunsDir -ProducerLane $ProducerLane
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 2
}
if ($Json) { $selection | ConvertTo-Json -Depth 4 } else { $selection.keyB }
exit 0
