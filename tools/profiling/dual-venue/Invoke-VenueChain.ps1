# Invoke-VenueChain.ps1 -- the TRACKED venue chain runner (VENUE-CHAIN-RUNNER-1). RUN THIS ON THE VM.
#
# Runs an ordered list of legs (-LegSpec paths, or a tracked -LegSet) on ONE venue through Invoke-VenueLeg.ps1, with Wait-VenueQuiet.ps1 in front of
# every leg. It replaces the hand-written per-lane chains under fleet-runs, which failed three ways on 2026-10-09: two waiters each treated the other's
# WAITING as venue use and deadlocked (MUTUAL WAIT); nothing honoured Wait-VenueQuiet exit 6 (EXIT 6); a chain held the venue through every ~550 s quiet
# wait (HOLD WHILE WAITING). The contract:
#   1. ONE CLAIM PER VENUE (VenueChain.psm1): <ClaimDir>\<venue>.claim.json, taken atomically (CreateNew), naming owner pid + process creation time + run
#      dir + purpose + expiry. A claim whose pid is dead, whose pid was reused or whose expiry passed is stale and is broken with one CLAIM_STALE_BROKEN line.
#   2. NO HOLD WHILE WAITING. Per leg: [-PreferQuietWindows: wait, unclaimed, for a better hour] -> quiet probe WITHOUT the claim (Wait-VenueQuiet is
#      read-only) -> take the claim (while another chain's LIVE claim stands, wait unclaimed and poll) -> re-probe ONCE under the claim (-MaxWaitSec 0)
#      -> run the leg -> release -> cooldown unclaimed. Nothing here scans processes or lane prompts: a waiting chain is never a venue user, and the only
#      thing that blocks a chain is a LIVE claim. (A GATE_BUSY re-probe under the claim releases it and goes back to waiting.)
#   3. EXIT 6. Wait-VenueQuiet exit 6 (VENUE_QUIET_UNKNOWN), any exit code it does not document, or exit 0 without a QUIET / COOLDOWN_UNMET line means the
#      venue was NOT MEASURED: the leg is NOT run (`LEG <name> NOT_RUN VENUE_QUIET_UNKNOWN reason=...`) and the chain stops (-OnUnknown Stop, the default)
#      or goes on to the next leg (-OnUnknown Continue). A measured COOLDOWN_UNMET still runs the leg, as before, but the LEG line carries verdict=,
#      quietMean= and the leg's preLoadMean= and class= (QUIET_PRELOAD_OK when QUIET and preLoad <= 30 %: docs/cuda-playback-present-cadence.md:206 COUNTED
#      = QUIET plus preLoad <= 30; PASS and the exe match stay lane-side). The quiet gate itself is unchanged: threshold and waits pass straight through.
#   4. QUIET WINDOWS. -QuietRateTable prints the per-UTC-hour QUIET rate of the venue's PROBE lines in -GateLogPath (default every
#      <main checkout>\.claude-state\fleet-runs\*\gate.log) and exits. -PreferQuietWindows: when the current hour's observed rate is under -PreferRatePercent
#      (30) on at least -MinWindowProbes probes and a later hour within -WindowHorizonHours is at or above it, the chain waits (unclaimed) for that hour.
#   5. LOG. -ChainLog (default <RunDir>\chain.log): one line per event, UTC stamp first; the last line is CHAIN_RESULT=<json> (also printed on stdout).
#      The leg's own output goes to <RunDir>\leg-<name>.out.txt, each probe's to <RunDir>\quiet-<name>-<phase>.out.txt, probes append to <RunDir>\gate.log.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueChain.ps1 -Venue bachelor -LegSet display-matrix -Backend cpu -RunDir <rd> `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> [-PreferQuietWindows] [-OnUnknown Continue] [-DeadlineUtc <iso>]
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueChain.ps1 -QuietRateTable [-Venue bachelor] [-GateLogPath <gate.log>...]
#
# Exit codes (a leg's OUTCOME is never the exit code: read CHAIN_RESULT and the receipts): 0 every leg ran; 2 usage (including a Wait-VenueQuiet usage
# error); 3 DEADLINE (-DeadlineUtc passed before every leg ran: queue busy, a live claim, or a window; the rest are NOT_RUN DEADLINE); 4 GATE_UNREADABLE
# and 5 HOST_MISMATCH from Wait-VenueQuiet (the chain stops); 6 at least one leg was NOT_RUN VENUE_QUIET_UNKNOWN.
# -QuietScript / -LegScript / -ClaimDir / -NowUtc are seams for the offline tests (tools/repo_hygiene/test_venue_chain_runner.py); production passes none.
[CmdletBinding(DefaultParameterSetName = 'Chain')]
param(
    [ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'bachelor',
    [Parameter(ParameterSetName = 'Chain')][string[]]$LegSpec = @(),
    [Parameter(ParameterSetName = 'Chain')][string]$LegSet = '',
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(1, 20)][int]$Repeats = 1,
    [Parameter(ParameterSetName = 'Chain')][ValidateSet('cuda', 'cpu')][string]$Backend,
    [Parameter(ParameterSetName = 'Chain', Mandatory = $true)][string]$RunDir,
    [Parameter(ParameterSetName = 'Chain', Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [Parameter(ParameterSetName = 'Chain', Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256,
    [Parameter(ParameterSetName = 'Chain')][string]$Purpose = '',
    [Parameter(ParameterSetName = 'Chain')][string]$SheetCopyDir = '',
    # passed through to Wait-VenueQuiet.ps1 (never relaxed here)
    [Parameter(ParameterSetName = 'Chain')][double]$ThresholdPercent = 20.0,
    [Parameter(ParameterSetName = 'Chain')][int]$QuietMaxWaitSec = 600,
    [Parameter(ParameterSetName = 'Chain')][int]$RecheckSec = 90,
    [Parameter(ParameterSetName = 'Chain')][int]$CooldownSec = 120,
    [Parameter(ParameterSetName = 'Chain')][string]$DeadlineUtc = '',
    [Parameter(ParameterSetName = 'Chain')][ValidateSet('Stop', 'Continue')][string]$OnUnknown = 'Stop',
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(60, 86400)][int]$ClaimTtlSec = 10800,
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(1, 3600)][int]$ClaimPollSec = 30,
    [Parameter(ParameterSetName = 'Chain')][string]$ChainLog = '',
    [Parameter(ParameterSetName = 'Chain')][switch]$PreferQuietWindows,
    [Parameter(ParameterSetName = 'Table', Mandatory = $true)][switch]$QuietRateTable,
    [string]$ClaimDir = '',
    [string[]]$GateLogPath = @(),
    [double]$PreferRatePercent = 30.0,
    [ValidateRange(1, 23)][int]$WindowHorizonHours = 6,
    [ValidateRange(1, 1000)][int]$MinWindowProbes = 3,
    [string]$NowUtc = '',
    [string]$QuietScript = '',
    [string]$LegScript = ''
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking

function Stop-Usage([string]$Message) { [Console]::Error.WriteLine("CHAIN_USAGE $Message"); exit 2 }

# `pwsh -File` passes an array argument as ONE string, so -LegSpec a.json,b.json and -GateLogPath x,y are split on commas (an entry that is itself an existing
# path is kept whole).
function Expand-PathList([string[]]$Items) {
    foreach ($i in $Items) {
        if ([string]::IsNullOrWhiteSpace($i)) { continue }
        if ((Test-Path -LiteralPath $i) -or $i -notlike '*,*') { $i; continue }
        $i -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    }
}
$LegSpec = @(Expand-PathList $LegSpec)
$GateLogPath = @(Expand-PathList $GateLogPath)

function Get-GateLogs {
    if ($GateLogPath.Count -gt 0) { return @($GateLogPath) }
    $fr = Join-Path (Get-VenueMainRoot -From $here) '.claude-state\fleet-runs'
    @(Get-ChildItem -Path (Join-Path $fr '*\gate.log') -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
}

function Get-RateTable {
    $logs = Get-GateLogs
    $records = ConvertFrom-VenueGateLog -Path $logs -Venue $Venue
    [pscustomobject]@{ logs = $logs; table = (Get-VenueQuietRateTable -Records $records) }
}

if ($PSCmdlet.ParameterSetName -eq 'Table') {
    $now = $(if ($NowUtc) { [DateTimeOffset]::Parse($NowUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime } else { [DateTime]::UtcNow })
    $rt = Get-RateTable
    Write-Output "QUIET_RATE_SOURCES logs=$($rt.logs.Count)"
    Format-VenueQuietRateTable -Table $rt.table -Venue $Venue
    $w = Select-VenueQuietWindow -Table $rt.table -NowUtc $now -PreferRatePercent $PreferRatePercent -HorizonHours $WindowHorizonHours -MinProbes $MinWindowProbes
    Write-Output ('WINDOW_CHOICE=' + ($w | ConvertTo-Json -Compress))
    exit 0
}

# ---------------------------------------------------------------- chain mode ----------------------------------------------------------------
if (-not $QuietScript) { $QuietScript = Join-Path $here 'Wait-VenueQuiet.ps1' }
if (-not $LegScript) { $LegScript = Join-Path $here 'Invoke-VenueLeg.ps1' }
foreach ($s in $QuietScript, $LegScript) { if (-not (Test-Path -LiteralPath $s -PathType Leaf)) { Stop-Usage "no script at '$s'" } }
if (($LegSpec.Count -gt 0) -eq [bool]$LegSet) { Stop-Usage 'name the legs with exactly one of -LegSpec <path>... or -LegSet <name>' }
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$RunDir = (Resolve-Path -LiteralPath $RunDir).Path
if (-not $ChainLog) { $ChainLog = Join-Path $RunDir 'chain.log' }
$t0 = [DateTime]::UtcNow
$deadline = $(if ($DeadlineUtc) { [DateTimeOffset]::Parse($DeadlineUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime } else { $t0.AddHours(6) })
$pwshExe = [Environment]::ProcessPath
if (-not $Purpose) { $Purpose = "Invoke-VenueChain $(Split-Path -Leaf $RunDir)" }

function L([string]$Message) {
    $line = "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message"
    Add-Content -LiteralPath $ChainLog -Value $line
    Write-Output $line
}

# The planned legs: name, spec path, backend ('' = whatever the spec lists, as Invoke-VenueLeg does with no -Backend).
$plan = [System.Collections.Generic.List[object]]::new()
if ($LegSet) {
    Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force
    $setPath = Join-Path $here "legsets\$LegSet.json"
    $sp = try { Get-DvLegSetPlan -LegSetPath $setPath -Repeats $Repeats -Backend $(if ($Backend) { $Backend } else { '' }) } catch { Stop-Usage $_.Exception.Message }
    foreach ($e in $sp.plan) { $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f $e.seq, $e.legId, $e.backend); spec = $e.specPath; backend = $e.backend }) }
} else {
    $seq = 0
    foreach ($ref in $LegSpec) {
        $seq++
        $path = $(if (Test-Path -LiteralPath $ref -PathType Leaf) { $ref } elseif (Test-Path -LiteralPath (Join-Path $here "legs\$ref") -PathType Leaf) { Join-Path $here "legs\$ref" } else { Stop-Usage "no leg spec at '$ref'" })
        $path = (Resolve-Path -LiteralPath $path).Path
        $legId = try { [string](([IO.File]::ReadAllText($path) | ConvertFrom-Json).legId) } catch { '' }
        if ($legId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$') { $legId = [IO.Path]::GetFileNameWithoutExtension($path) }
        for ($r = 1; $r -le $Repeats; $r++) {
            $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f ($plan.Count + 1), $legId, $(if ($Backend) { $Backend } else { 'spec' })); spec = $path; backend = $(if ($Backend) { $Backend } else { '' }) })
        }
    }
}

$results = [System.Collections.Generic.List[object]]::new()
$exitCode = 0
$stopReason = $null

function Invoke-QuietProbe([string]$Name, [string]$Phase, [int]$MaxWaitSec) {
    # One Wait-VenueQuiet run. verdict: QUIET | COOLDOWN_UNMET | GATE_BUSY | GATE_UNREADABLE | HOST_MISMATCH | USAGE | VENUE_QUIET_UNKNOWN (not measured).
    $out = Join-Path $RunDir "quiet-$Name-$Phase.out.txt"
    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript, '-Venue', $Venue, '-WorkDir', (Join-Path $RunDir 'quiet'), '-GateLog', (Join-Path $RunDir 'gate.log'),
        '-ThresholdPercent', [string]$ThresholdPercent, '-MaxWaitSec', [string]$MaxWaitSec, '-RecheckSec', [string]$RecheckSec)
    & $pwshExe @qArgs *> $out
    $code = $LASTEXITCODE
    $lines = @(if (Test-Path -LiteralPath $out) { Get-Content -LiteralPath $out })
    $v = $lines | Where-Object { $_ -match '^(QUIET|COOLDOWN_UNMET) mean=' } | Select-Object -Last 1
    $u = $lines | Where-Object { $_ -like 'VENUE_QUIET_UNKNOWN *' } | Select-Object -Last 1
    $mean = $null; $reason = $null
    $verdict = switch ($code) {
        0 { if ($v) { ($v -split ' ')[0] } else { $reason = 'exit0-without-verdict'; 'VENUE_QUIET_UNKNOWN' } }
        2 { 'USAGE' }
        3 { 'GATE_BUSY' }
        4 { 'GATE_UNREADABLE' }
        5 { 'HOST_MISMATCH' }
        6 { $reason = $(if ($u -and $u -match '\breason=(\S+)') { $Matches[1] } else { 'exit6' }); 'VENUE_QUIET_UNKNOWN' }
        default { $reason = "undocumented-exit-$code"; 'VENUE_QUIET_UNKNOWN' }
    }
    if ($v -and $v -match '\bmean=([0-9.]+)%') { $mean = [double]$Matches[1] }
    $waited = $(if (($v, $u | Where-Object { $_ }) -join ' ' -match '\bwaitedSec=(\d+)') { [int]$Matches[1] } else { $null })
    [pscustomobject]@{ verdict = $verdict; code = $code; mean = $mean; reason = $reason; waitedSec = $waited; out = $out }
}

function Format-Mean($m) { if ($null -eq $m) { 'UNKNOWN' } else { '{0:0.0}' -f $m } }

function Get-LegPreLoadMean([string]$ReceiptPath) {
    # The leg's own pre-load mean, read from the run's summary.json (cpuQuiescence.timeMeanPercent) -- never from the receipt file. Invoke-VenueLeg writes the
    # receipt as <root>\<card>\<legId>\<venue>\<receiptId>.json and the run evidence to <parent of root>\evidence\<receiptId>. $null when absent.
    try {
        if (-not $ReceiptPath) { return $null }
        $root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $ReceiptPath)))
        $summary = Join-Path (Join-Path (Join-Path (Split-Path -Parent $root) 'evidence') ([IO.Path]::GetFileNameWithoutExtension($ReceiptPath))) 'summary.json'
        if (-not (Test-Path -LiteralPath $summary -PathType Leaf)) { return $null }
        $q = ([IO.File]::ReadAllText($summary) | ConvertFrom-Json).cpuQuiescence
        if ($null -eq $q -or $null -eq $q.timeMeanPercent) { return $null }
        [double]$q.timeMeanPercent
    } catch { $null }
}

function Add-Result([object]$Leg, [string]$Status, [hashtable]$Extra) {
    $r = [ordered]@{ name = $Leg.name; spec = $Leg.spec; backend = $Leg.backend; status = $Status }
    foreach ($k in $Extra.Keys) { $r[$k] = $Extra[$k] }
    $results.Add([pscustomobject]$r)
}

$rateTable = $null
if ($PreferQuietWindows) {
    $rt = Get-RateTable
    $rateTable = $rt.table
    L "QUIET_RATE_TABLE logs=$($rt.logs.Count) hours=$(@($rateTable).Count) (for -PreferQuietWindows)"
}
L "CHAIN_START venue=$Venue legs=$($plan.Count) runDir=$RunDir claim=$(Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir) deadline=$($deadline.ToString('o')) onUnknown=$OnUnknown threshold=$ThresholdPercent quietMaxWaitSec=$QuietMaxWaitSec preferQuietWindows=$([bool]$PreferQuietWindows) pid=$PID quietScript=$QuietScript legScript=$LegScript"

$legIndex = 0
foreach ($leg in $plan) {
    $legIndex++
    if ($null -ne $stopReason) { Add-Result $leg 'NOT_RUN' @{ verdict = $stopReason; reason = 'chain-stopped' }; L "LEG $($leg.name) NOT_RUN $stopReason reason=chain-stopped"; continue }
    $ran = $false
    while (-not $ran) {
        if ([DateTime]::UtcNow -ge $deadline) { $stopReason = 'DEADLINE'; $exitCode = 3; Add-Result $leg 'NOT_RUN' @{ verdict = 'DEADLINE'; reason = 'deadline-before-leg' }; L "LEG $($leg.name) NOT_RUN DEADLINE reason=deadline-before-leg"; break }
        # (a) a better quiet window, waited for WITHOUT the claim
        if ($PreferQuietWindows) {
            $w = Select-VenueQuietWindow -Table $rateTable -NowUtc ([DateTime]::UtcNow) -PreferRatePercent $PreferRatePercent -HorizonHours $WindowHorizonHours -MinProbes $MinWindowProbes -DeadlineUtc $deadline
            L "WINDOW leg=$($leg.name) action=$($w.action) reason=$($w.reason) currentHour=$('{0:00}' -f $w.currentHour)Z currentRate=$($w.currentRate) currentProbes=$($w.currentProbes)$(if ($w.action -ceq 'WAIT') { " targetHour=$('{0:00}' -f $w.targetHour)Z targetRate=$($w.targetRate) waitSec=$($w.waitSec) (no claim held)" })"
            if ($w.action -ceq 'WAIT') {
                $until = [DateTimeOffset]::Parse($w.targetUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
                while ([DateTime]::UtcNow -lt $until -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Seconds ([int][Math]::Max(1, [Math]::Min(60, ($until - [DateTime]::UtcNow).TotalSeconds))) }
                continue
            }
        }
        # (b) the quiet wait, WITHOUT the claim
        $pre = Invoke-QuietProbe $leg.name 'pre-claim' $QuietMaxWaitSec
        L "QUIET leg=$($leg.name) phase=pre-claim exit=$($pre.code) verdict=$($pre.verdict) mean=$(Format-Mean $pre.mean) waitedSec=$($pre.waitedSec)$(if ($pre.reason) { " reason=$($pre.reason)" }) (no claim held)"
        if ($pre.verdict -ceq 'USAGE') { $stopReason = 'USAGE'; $exitCode = 2; Add-Result $leg 'NOT_RUN' @{ verdict = 'USAGE'; reason = 'wait-venuequiet-usage' }; L "LEG $($leg.name) NOT_RUN USAGE reason=wait-venuequiet-usage"; break }
        if ($pre.verdict -in @('GATE_UNREADABLE', 'HOST_MISMATCH')) { $stopReason = $pre.verdict; $exitCode = $(if ($pre.verdict -ceq 'GATE_UNREADABLE') { 4 } else { 5 }); Add-Result $leg 'NOT_RUN' @{ verdict = $pre.verdict; reason = 'phase=pre-claim' }; L "LEG $($leg.name) NOT_RUN $($pre.verdict) phase=pre-claim"; break }
        if ($pre.verdict -ceq 'VENUE_QUIET_UNKNOWN') {
            Add-Result $leg 'NOT_RUN' @{ verdict = 'VENUE_QUIET_UNKNOWN'; reason = $pre.reason; phase = 'pre-claim' }
            L "LEG $($leg.name) NOT_RUN VENUE_QUIET_UNKNOWN reason=$($pre.reason) phase=pre-claim (the venue was not measured; onUnknown=$OnUnknown)"
            $exitCode = 6; if ($OnUnknown -ceq 'Stop') { $stopReason = 'VENUE_QUIET_UNKNOWN' }
            break
        }
        if ($pre.verdict -ceq 'GATE_BUSY') { if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -lt $deadline) { Start-Sleep -Seconds $ClaimPollSec }; continue }
        # (c) the claim: only now, about to run. A LIVE claim of another chain is waited out unclaimed; stale claims are broken once each.
        $claim = $null; $lastHolder = ''
        while ($true) {
            $req = Request-VenueClaim -Venue $Venue -RunDir $RunDir -Purpose "$Purpose leg $($leg.name)" -TtlSec $ClaimTtlSec -ClaimDir $ClaimDir
            foreach ($b in $req.broken) { L "CLAIM_STALE_BROKEN venue=$Venue reason=$($b.reason) ownerPid=$($b.claim.ownerPid) ownerCreatedUtc=$($b.claim.ownerCreatedUtc) runDir=$($b.claim.runDir) expiresUtc=$($b.claim.expiresUtc)" }
            if ($req.granted) { $claim = $req.claim; break }
            $holder = $(if ($req.claim) { "pid=$($req.claim.ownerPid) runDir=$($req.claim.runDir) purpose=$($req.claim.purpose) expiresUtc=$($req.claim.expiresUtc)" } else { $req.state })
            if ($holder -cne $lastHolder) { L "CLAIM_HELD leg=$($leg.name) state=$($req.state) holder $holder -> wait (no claim held)"; $lastHolder = $holder }
            if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -ge $deadline) { break }
            Start-Sleep -Seconds $ClaimPollSec
        }
        if ($null -eq $claim) { $stopReason = 'DEADLINE'; $exitCode = 3; Add-Result $leg 'NOT_RUN' @{ verdict = 'DEADLINE'; reason = 'claim-held' }; L "LEG $($leg.name) NOT_RUN DEADLINE reason=claim-held"; break }
        L "CLAIM_TAKEN leg=$($leg.name) nonce=$($claim.nonce) expiresUtc=$($claim.expiresUtc)"
        $released = $false
        try {
            # (d) ONE re-probe under the claim: the measurement nearest the leg is the one its line carries
            $re = Invoke-QuietProbe $leg.name 'under-claim' 0
            L "QUIET leg=$($leg.name) phase=under-claim exit=$($re.code) verdict=$($re.verdict) mean=$(Format-Mean $re.mean)$(if ($re.reason) { " reason=$($re.reason)" })"
            if ($re.verdict -ceq 'GATE_BUSY') {
                L "CLAIM_RELEASED leg=$($leg.name) result=$(Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir) reason=gate-busy-under-claim (back to waiting)"; $released = $true
                continue
            }
            if ($re.verdict -ceq 'USAGE') { $stopReason = 'USAGE'; $exitCode = 2; Add-Result $leg 'NOT_RUN' @{ verdict = 'USAGE'; reason = 'phase=under-claim' }; L "LEG $($leg.name) NOT_RUN USAGE phase=under-claim"; break }
            if ($re.verdict -in @('GATE_UNREADABLE', 'HOST_MISMATCH')) { $stopReason = $re.verdict; $exitCode = $(if ($re.verdict -ceq 'GATE_UNREADABLE') { 4 } else { 5 }); Add-Result $leg 'NOT_RUN' @{ verdict = $re.verdict; reason = 'phase=under-claim' }; L "LEG $($leg.name) NOT_RUN $($re.verdict) phase=under-claim"; break }
            if ($re.verdict -ceq 'VENUE_QUIET_UNKNOWN') {
                Add-Result $leg 'NOT_RUN' @{ verdict = 'VENUE_QUIET_UNKNOWN'; reason = $re.reason; phase = 'under-claim' }
                L "LEG $($leg.name) NOT_RUN VENUE_QUIET_UNKNOWN reason=$($re.reason) phase=under-claim (the venue was not measured; onUnknown=$OnUnknown)"
                $exitCode = 6; if ($OnUnknown -ceq 'Stop') { $stopReason = 'VENUE_QUIET_UNKNOWN' }
                break
            }
            # (e) the leg
            $out = Join-Path $RunDir "leg-$($leg.name).out.txt"
            $lArgs = @('-NoProfile', '-NonInteractive', '-File', $LegScript, '-Venue', $Venue, '-LegSpec', $leg.spec, '-SourceCommit', $SourceCommit, '-BuildManifestSha256', $BuildManifestSha256)
            if ($leg.backend) { $lArgs += @('-Backend', $leg.backend) }
            if ($SheetCopyDir) { $lArgs += @('-SheetCopyDir', $SheetCopyDir) }
            L "LEG $($leg.name) RUN verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) spec=$($leg.spec) backend=$(if ($leg.backend) { $leg.backend } else { 'spec' })"
            & $pwshExe @lArgs *> $out
            $legCode = $LASTEXITCODE
            $legLines = @(if (Test-Path -LiteralPath $out) { Get-Content -LiteralPath $out })
            $o = $legLines | Where-Object { $_ -match '^DVE_OUTCOME=(\S+)' } | Select-Object -Last 1
            $outcome = $(if ($o -and $o -match '^DVE_OUTCOME=(\S+)') { $Matches[1] } else { 'NONE' })
            $p = $legLines | Where-Object { $_ -match '^DVE_RECEIPT_PATH=(.+)$' } | Select-Object -Last 1
            $receipt = $(if ($p -and $p -match '^DVE_RECEIPT_PATH=(.+)$') { $Matches[1].Trim() } else { $null })
            $preLoad = Get-LegPreLoadMean $receipt
            $class = $(if ($re.verdict -ceq 'COOLDOWN_UNMET') { 'COOLDOWN_UNMET' } elseif ($null -eq $preLoad) { 'QUIET_PRELOAD_UNKNOWN' } elseif ($preLoad -le 30.0) { 'QUIET_PRELOAD_OK' } else { 'QUIET_PRELOAD_HIGH' })
            Add-Result $leg 'RAN' @{ verdict = $re.verdict; quietMean = $re.mean; preClaimVerdict = $pre.verdict; preClaimMean = $pre.mean; preLoadMean = $preLoad; class = $class; outcome = $outcome; legExit = $legCode; receipt = $receipt }
            L "LEG $($leg.name) RAN exit=$legCode outcome=$outcome verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) preLoadMean=$(Format-Mean $preLoad) class=$class receipt=$receipt"
            $ran = $true
        } finally {
            if (-not $released) { L "CLAIM_RELEASED leg=$($leg.name) result=$(Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir)" }
        }
        if (-not $ran) { break }
    }
    if ($ran -and $legIndex -lt $plan.Count -and $null -eq $stopReason -and $CooldownSec -gt 0) {
        L "COOLDOWN ${CooldownSec} s (no claim held)"
        Start-Sleep -Seconds $CooldownSec
    }
}

$summary = [ordered]@{
    schema = 'mlv-app/venue-chain-result/v1'; venue = $Venue; runDir = $RunDir; startedUtc = $t0.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o')
    exitCode = $exitCode; stopReason = $stopReason; legs = @($results)
    ran = @($results | Where-Object { $_.status -ceq 'RAN' }).Count; notRun = @($results | Where-Object { $_.status -ceq 'NOT_RUN' }).Count
}
$resultLine = 'CHAIN_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
Add-Content -LiteralPath $ChainLog -Value $resultLine
Write-Output $resultLine
exit $exitCode
