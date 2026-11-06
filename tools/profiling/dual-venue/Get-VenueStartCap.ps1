# Get-VenueStartCap.ps1 -- BENCH-START-CAP-FROM-LEG-COUNT-1: the start cap of a venue run, sized from its planned leg count and leg duration
# instead of a hand-typed window. A lane-local driver (bench-ab.ps1) that takes -StartCapUtc calls this once at dispatch and passes the value on; the lease-batch
# runner (Invoke-VenueLeaseBatch.ps1) calls the same Get-VenueRunStartCap itself when it is given no -DeadlineUtc.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Get-VenueStartCap.ps1 -LegCount 12 [-PlaySeconds 25 | -LegSpec <json>...] [-CooldownSec 300 -BudgetSec 9720]
#        [-HardEndUtc <iso>] [-ExplicitCapUtc <iso>]
#
# Prints the typed START_CAP line (and START_CAP_CLAMPED_TO_HARD_BOUND when the run's hard end is what limits the cap), then a last line `StartCapUtc=<iso>`.
# -LegSpec: one leg spec per planned leg (a spec may repeat; under -File several are one comma-separated value); -LegCount comes from them, and each leg strides by ITS OWN
#   playSeconds (the per-leg list, not the mean).
# -NowUtc, -HardEndUtc, -ExplicitCapUtc: a time with a zone keeps it; a zoneless time (no Z, no offset) is UTC on every host (the one parser, ConvertTo-VenueStartCapTime). Text that is not a
#   time is refused: a START_CAP_BAD_TIME line and exit 2, never a silently skipped bound.
# -HardEndUtc: the existing safety bound (the run's hard end); the computed cap is never later than it. -ExplicitCapUtc: kept exactly as given.
# -LegCooldownSec: the runner's own -CooldownSec (slept after every completed leg). Exit 3 + a START_CAP_UNSIZABLE line when no leg fits a lease or a spec cannot be read.
# Reads no venue and takes no lease. The lease caps default to the old mirrored constants (the tightest); pass the live ones (the run's CAPS line) when known.
[CmdletBinding()]
param(
    [int]$LegCount = 0,
    [string[]]$LegSpec = @(),
    [int]$PlaySeconds = 25,
    [int]$LegOverheadSec = 210,
    [int]$LegCooldownSec = 0,
    [int]$MaxPlaySeconds = 0,
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
# a typed refusal is a START_CAP_* line on stdout and a non-zero exit: 2 for input that is not usable (a time, a leg), 3 for a plan that cannot be sized
function Stop-StartCap([string]$Message) {
    $m = "$Message" -replace '\s+', ' '
    if ($m -notmatch '^START_CAP_') { $m = "START_CAP_UNSIZABLE reason=$m" }
    Write-Output $m
    exit $(if ($m -match '^START_CAP_(BAD_TIME|BAD_LEG|USAGE)') { 2 } else { 3 })
}
$legSeconds = @()
$specNote = $null
# the spec files are read inside the try: a spec that cannot be read is a typed line (and the caller's explicit cap still stands), never an untyped crash (adversary A4)
try {
    # -File passes one string: a comma list names several specs, but an entry that is itself an existing path is kept whole (the batch runner's own rule)
    $LegSpec = @($LegSpec | Where-Object { $_ })
    $LegSpec = @($LegSpec | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($LegSpec.Count -gt 0) {
        $LegCount = $LegSpec.Count
        $planSecs = Get-VenuePlanPlaySeconds -SpecPaths $LegSpec
        $PlaySeconds = $planSecs.playSeconds; $MaxPlaySeconds = $planSecs.maxPlaySeconds; $legSeconds = @($planSecs.legSeconds)
        if ($planSecs.line) { Write-Output $planSecs.line }
    }
} catch {
    if (-not $ExplicitCapUtc) { Stop-StartCap "START_CAP_UNSIZABLE reading the leg specs failed: $($_.Exception.Message)" }
    Write-Output ("START_CAP_PLAYSECONDS_UNKNOWN unknown=$($LegSpec.Count) known=0 usedSec=$PlaySeconds reason=reading the leg specs failed: $($_.Exception.Message) (the explicit cap stands)" -replace '\s+', ' ')
    $legSeconds = @()
}
if ($LegCount -lt 1) { [Console]::Error.WriteLine('Get-VenueStartCap: name the planned legs with -LegCount <n> or -LegSpec <json>...'); exit 2 }
# START_CAP_UNSIZABLE (a lease that holds no leg, a value outside the DateTime range) and START_CAP_BAD_TIME are typed refusals, never a made-up cap
try {
    $now = $(if ($NowUtc) { ConvertTo-VenueStartCapTime $NowUtc 'NowUtc' } else { [DateTime]::UtcNow })
    $args2 = @{ NowUtc = $now; LegCount = $LegCount; PlaySeconds = $PlaySeconds; LegOverheadSec = $LegOverheadSec; CapSec = $CapSec; CooldownSec = $CooldownSec; WindowSec = $WindowSec; BudgetSec = $BudgetSec }
    if ($HardEndUtc) { $args2.HardBoundUtc = ConvertTo-VenueStartCapTime $HardEndUtc 'HardEndUtc' }
    if ($ExplicitCapUtc) { $args2.ExplicitCapUtc = ConvertTo-VenueStartCapTime $ExplicitCapUtc 'ExplicitCapUtc' }
    if ($LegCooldownSec -gt 0) { $args2.LegCooldownSec = $LegCooldownSec }
    if ($MaxPlaySeconds -gt 0) { $args2.MaxPlaySeconds = $MaxPlaySeconds }
    if ($legSeconds.Count -eq $LegCount) { $args2.LegPlaySeconds = $legSeconds }
    $r = Get-VenueRunStartCap @args2
} catch { Stop-StartCap $_.Exception.Message }
$r.lines | ForEach-Object { Write-Output $_ }
Write-Output ("StartCapUtc=" + $r.capText)
exit 0
