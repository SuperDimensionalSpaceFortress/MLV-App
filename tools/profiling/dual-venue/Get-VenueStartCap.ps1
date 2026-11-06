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
# Reads no venue and takes no lease. The lease caps default to the old mirrored constants (the tightest); pass the live ones (the run's CAPS line) when known.
[CmdletBinding()]
param(
    [int]$LegCount = 0,
    [string[]]$LegSpec = @(),
    [int]$PlaySeconds = 25,
    [int]$LegOverheadSec = 210,
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
$LegSpec = @($LegSpec | ForEach-Object { $_ -split ',' } | Where-Object { $_ })   # -File passes one string: a comma list names several specs
if ($LegSpec.Count -gt 0) {
    $secs = @($LegSpec | ForEach-Object { Get-VenueLegPlaySeconds -SpecPath $_ } | Where-Object { $null -ne $_ })
    if ($secs.Count -gt 0) { $PlaySeconds = [int][Math]::Ceiling(($secs | Measure-Object -Average).Average) }
    $LegCount = $LegSpec.Count
}
if ($LegCount -lt 1) { [Console]::Error.WriteLine('Get-VenueStartCap: name the planned legs with -LegCount <n> or -LegSpec <json>...'); exit 2 }
$now = $(if ($NowUtc) { [DateTimeOffset]::Parse($NowUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime } else { [DateTime]::UtcNow })
$args2 = @{ NowUtc = $now; LegCount = $LegCount; PlaySeconds = $PlaySeconds; LegOverheadSec = $LegOverheadSec; CapSec = $CapSec; CooldownSec = $CooldownSec; WindowSec = $WindowSec; BudgetSec = $BudgetSec }
if ($HardEndUtc) { $args2.HardBoundUtc = $HardEndUtc }
if ($ExplicitCapUtc) { $args2.ExplicitCapUtc = $ExplicitCapUtc }
$r = Get-VenueRunStartCap @args2
$r.lines | ForEach-Object { Write-Output $_ }
Write-Output ("StartCapUtc=" + $r.capUtc.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"))
exit 0
