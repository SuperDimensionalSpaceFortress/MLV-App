#!/usr/bin/env pwsh
# lane-guard.ps1 -- pre-commit backstop. Blocks a commit whose STAGED set crosses dual-lane ownership.
# Invoked by tools/dual-lane/hooks/pre-commit. The active lane comes from env GIT_DUAL_LANE (per-session,
# the correct mechanism for a SHARED working tree where a single on-disk active-lane file would be clobbered
# by the other lane). Fail-closed: blocks if the lane is unset, the map is missing, or any staged path is
# unmapped. Loud escape hatch: GIT_DUAL_LANE_OVERRIDE=1 lets a deliberate human cross-lane commit through.
$ErrorActionPreference = 'Stop'
$root = (& git rev-parse --show-toplevel).Trim()
$ownerOf = Join-Path $root 'tools/dual-lane/owner-of.ps1'

$staged = @(& git -C $root -c core.quotepath=false diff --cached --name-only | Where-Object { $_ })
if ($staged.Count -eq 0) { exit 0 }

if ($env:GIT_DUAL_LANE_OVERRIDE -eq '1') {
    Write-Host "[lane-guard] GIT_DUAL_LANE_OVERRIDE=1 -- bypassing the dual-lane staged-path check for $($staged.Count) file(s) (audited)." -ForegroundColor Yellow
    exit 0
}

$lane = $env:GIT_DUAL_LANE
if (-not $lane) {
    Write-Host "[lane-guard] BLOCK: env GIT_DUAL_LANE is unset; a two-lane shared tree must declare which lane is committing." -ForegroundColor Red
    Write-Host "  Fix: `$env:GIT_DUAL_LANE='claude' (or 'codex') for this session, OR commit via tools/dual-lane/lane-commit.ps1." -ForegroundColor Red
    Write-Host "  Deliberate cross-lane/human commit: GIT_DUAL_LANE_OVERRIDE=1 (audited)." -ForegroundColor Red
    exit 1
}

$cross = @()
foreach ($s in $staged) {
    $o = (& pwsh -NoProfile -File $ownerOf -Path $s).Trim()
    if ($o -eq 'unknown') { Write-Host "[lane-guard] BLOCK: '$s' is unmapped in .dual-lane/ownership.json." -ForegroundColor Red; exit 1 }
    if ($o -ne $lane -and $o -ne 'shared') { $cross += "$s  [owner=$o]" }
}
if ($cross.Count) {
    Write-Host "[lane-guard] BLOCK: active lane '$lane' is staging paths owned by the other lane:" -ForegroundColor Red
    $cross | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    Write-Host "  Unstage them: git restore --staged <path>   (or commit via tools/dual-lane/lane-commit.ps1)" -ForegroundColor Red
    Write-Host "  Deliberate cross-lane commit: re-run with GIT_DUAL_LANE_OVERRIDE=1 (audited)." -ForegroundColor Red
    exit 1
}
exit 0
