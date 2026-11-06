# Get-VenueStartCap.ps1 -- BENCH-START-CAP-FROM-LEG-COUNT-1: the start cap of a venue run, sized from its planned leg count and leg duration
# instead of a hand-typed window. A lane-local driver (bench-ab.ps1) that takes -StartCapUtc calls this once at dispatch and passes the value on; the lease-batch
# runner (Invoke-VenueLeaseBatch.ps1) calls the same Get-VenueRunStartCap itself when it is given no -DeadlineUtc.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Get-VenueStartCap.ps1 -LegCount 12 [-PlaySeconds 25 | -LegSpec <json>...] [-CooldownSec 300 -BudgetSec 9720]
#        [-HardEndUtc <iso>] [-ExplicitCapUtc <iso>]
#
# Prints the typed START_CAP line (and START_CAP_CLAMPED_TO_HARD_BOUND when the run's hard end is what limits the cap), then a last line `StartCapUtc=<iso>`.
# -LegSpec: one leg spec per planned leg (a spec may repeat; under -File several are one comma-separated value); -LegCount and -PlaySeconds (the mean of the specs' playSeconds) come from them.
# -HardEndUtc: the existing safety bound (the run's hard end); the computed cap is never later than it. -ExplicitCapUtc: kept exactly as given.
# -LegCooldownSec: the runner's own -CooldownSec (slept after every completed leg). Exit 3 + a START_CAP_UNSIZABLE line when no leg fits a lease.
# Reads no venue and takes no lease. The lease caps default to the old mirrored constants (the tightest); pass the live ones (the run's CAPS line) when known.
[CmdletBinding()]
param(
    [int]$LegCount = 0,
    [string[]]$LegSpec = @(),
    [int]$PlaySeconds = 25,
    [int]$LegOverheadSec = 210,
    [int]$LegCooldownSec = 0,
    [int]$CapSec = 2700,
    [int]$CooldownSec = 1800,
    [int]$WindowSec = 10800,
    [int]$BudgetSec = 5400,
    [string]$NowUtc = '',
    [string]$HardEndUtc = '',
    [string]$ExplicitCapUtc = ''
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking
# -File passes one string: a comma list names several specs, but an entry that is itself an existing path is kept whole (the batch runner's own rule)
$LegSpec = @($LegSpec | Where-Object { $_ })
$LegSpec = @($LegSpec | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($LegSpec.Count -gt 0) {
    $planSecs = Get-VenuePlanPlaySeconds -SpecPaths $LegSpec
    $PlaySeconds = $planSecs.playSeconds
    if ($planSecs.line) { Write-Output $planSecs.line }
    $LegCount = $LegSpec.Count
}
if ($LegCount -lt 1) { [Console]::Error.WriteLine('Get-VenueStartCap: name the planned legs with -LegCount <n> or -LegSpec <json>...'); exit 2 }
$now = $(if ($NowUtc) { [DateTimeOffset]::Parse($NowUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime } else { [DateTime]::UtcNow })
$args2 = @{ NowUtc = $now; LegCount = $LegCount; PlaySeconds = $PlaySeconds; LegOverheadSec = $LegOverheadSec; CapSec = $CapSec; CooldownSec = $CooldownSec; WindowSec = $WindowSec; BudgetSec = $BudgetSec }
if ($HardEndUtc) { $args2.HardBoundUtc = $HardEndUtc }
if ($ExplicitCapUtc) { $args2.ExplicitCapUtc = $ExplicitCapUtc }
if ($LegCooldownSec -gt 0) { $args2.LegCooldownSec = $LegCooldownSec }
# START_CAP_UNSIZABLE (a lease that holds no leg, a value outside the DateTime range) is a typed refusal, exit 3, never a made-up cap
try { $r = Get-VenueRunStartCap @args2 } catch { Write-Output ("$($_.Exception.Message)" -replace '\s+', ' '); exit 3 }
$r.lines | ForEach-Object { Write-Output $_ }
Write-Output ("StartCapUtc=" + $r.capUtc.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"))
exit 0
