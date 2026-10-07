#!/usr/bin/env pwsh
# lane-commit.ps1 -- the ONLY sanctioned dual-lane commit path.
# Stages ONLY the calling lane's owned dirty paths via EXPLICIT pathspec (NEVER 'git add -A/-a'), so the
# other lane's uncommitted WIP in the shared working tree can NEVER be swept into this commit. Fail-closed.
#
#   pwsh -File tools/dual-lane/lane-commit.ps1 -Lane claude -DryRun
#   pwsh -File tools/dual-lane/lane-commit.ps1 -Lane claude -Message "subject line"
#
# WHAT THE `Dual-Lane:` TRAILER MEANS, AND WHAT IT DOES NOT (OWN-2)
#
# -Lane is CALLER-DECLARED. It is validated against the two-value set below and nothing else: no
# caller is identified, challenged, or authenticated. So the `Dual-Lane: <lane>` trailer this script
# writes is a PATH-OWNERSHIP LABEL and a typo-guard -- it records which lane's globs the staged paths
# fall under. It is NOT an authorship attestation and must never be read as one.
#
# This is not hypothetical. Landed commits 48e9cd2f and 90d49ee4 (CDX-3) carry `Dual-Lane: claude`
# on tools/** paths: the codex lane ran -Lane claude and nothing objected, because nothing could.
# The ledger reads as though the claude lane authored them. In a month nobody would remember it did
# not -- which is precisely why the label is written down here rather than inferred later.
#
# Same class as the content-review gate's `Seat:` line, which CLAUDE.md already states "validates a
# CLAIMED identity from the ledger; it does NOT authenticate that actor or session". A control that
# names an identity and authenticates nothing is useful as a guard-rail and dangerous as evidence.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('codex','claude')][string]$Lane,
    [string]$Message,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = (& git rev-parse --show-toplevel).Trim()
$ownerOf = Join-Path $PSScriptRoot 'owner-of.ps1'

# every dirty path (tracked-modified + untracked)
$dirty = @(& git -C $root -c core.quotepath=false status --porcelain --untracked-files=all |
    ForEach-Object { ($_ -replace '^..\s','').Trim() } | Where-Object { $_ })

$mine = @(); $foreign = @(); $shared = @(); $unknown = @()
foreach ($f in $dirty) {
    $o = (& pwsh -NoProfile -File $ownerOf -Path $f).Trim()
    if     ($o -eq $Lane)     { $mine    += $f }
    elseif ($o -eq 'shared')  { $shared  += $f }
    elseif ($o -eq 'unknown') { $unknown += $f }
    else                      { $foreign += $f }
}

Write-Host "[lane-commit] lane=$Lane  owned=$($mine.Count)  foreign=$($foreign.Count)  shared=$($shared.Count)  unknown=$($unknown.Count)"
if ($foreign.Count) { Write-Host "  SKIP (other lane, left untouched): $($foreign -join ', ')" -ForegroundColor Yellow }
if ($unknown.Count) {
    Write-Host "  BLOCK: unmapped paths -- add them to .dual-lane/ownership.json: $($unknown -join ', ')" -ForegroundColor Red
    exit 1
}
if ($shared.Count) { Write-Host "  NOTE shared paths NOT auto-staged (need a Two-Key commit): $($shared -join ', ')" -ForegroundColor Yellow }
if ($mine.Count -eq 0) { Write-Host "  nothing owned by lane '$Lane' is dirty -- nothing to commit." -ForegroundColor Red; exit 1 }

Write-Host "  STAGING (this lane only):" -ForegroundColor Green
$mine | ForEach-Object { Write-Host "    $_" }
if ($DryRun)        { Write-Host "[lane-commit] -DryRun: no staging/commit performed."; exit 0 }
if (-not $Message)  { Write-Host "  -Message is required to commit (use -DryRun to preview)." -ForegroundColor Red; exit 2 }

& git -C $root reset -q
foreach ($f in $mine) { & git -C $root add -- $f }

# paranoia: prove nothing foreign slipped into the index
foreach ($s in @(& git -C $root -c core.quotepath=false diff --cached --name-only)) {
    $o = (& pwsh -NoProfile -File $ownerOf -Path $s).Trim()
    if ($o -ne $Lane) {
        Write-Host "  ABORT: staged '$s' resolves to '$o' not '$Lane'; unstaging all." -ForegroundColor Red
        & git -C $root reset -q
        exit 3
    }
}

$parent = (& git -C $root rev-parse HEAD).Trim()
$trailer = $null
try { $trailer = ((Get-Content -LiteralPath (Join-Path $root '.dual-lane/ownership.json') -Raw | ConvertFrom-Json).lanes.$Lane.trailer) } catch {}
if (-not $trailer) { $trailer = "Dual-Lane: $Lane" }
$full = "$Message`n`n$trailer"
$full | & git -C $root commit -F -
$head = (& git -C $root rev-parse HEAD).Trim()
Write-Host "[lane-commit] committed $($head.Substring(0,12))   range=$parent..$head" -ForegroundColor Green
