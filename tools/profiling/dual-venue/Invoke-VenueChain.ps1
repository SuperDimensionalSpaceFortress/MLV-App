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
#   2b. FIFO (VENUE-CLAIM-FIFO-1). The claim wait is a LINE, not a polling race: a chain that passed its quiet probe takes a TICKET (VenueChain.psm1 header) and
#      only the oldest LIVE ticket may try the claim; the others log CLAIM_HELD state=QUEUED with the whole line (position, pid, run dir, age). A ticket whose
#      owner is dead or silent for 3 x -ClaimPollSec is removed by the next waiter (TICKET_STALE_REMOVED); while its owner sits in a quiet probe the window is that
#      probe's real upper bound (QuietMaxWaitSec + um-run's waits + -ProbeAdmissionWaitSec, which the chain passes to the probe as -MaxAdmissionWaitSec), and a dead pid
#      is stale at once. A GATE_BUSY under the claim KEEPS the ticket for at most -TicketKeepMax (1) such cycles (TICKET_KEPT keeps=n/max); the next one, or a pre-claim
#      probe that is not quiet, gives the ticket up (TICKET_DROPPED keeps=n/max), so a waiter that is not ready never blocks one that is. The ticket is removed when the leg starts. A ticket holder polls its own turn every -TicketPollSec (5), faster than -ClaimPollSec; the venue
#      probe cadence is unchanged. Chains on the OLD code take no ticket: they still take any free claim and are never waited on (the line orders ticket holders
#      only). -NoTicket reverts a chain to the old race. -ClaimStatus prints the claim and the line (owner run dir, pid, age) and exits.
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
#   6. SUPPLEMENTARY LEGS (VENUE-UM-SUPPLEMENTARY-WHEN-BACHELOR-HELD-1; owner directive 2026-10-18: every playback chain ALSO runs its legs on ultra-magnus when that agent is idle).
#      Only a -Venue bachelor chain, ON by default, off with -NoSupplementary. The same leg is offered on ultra-magnus (role supplementary) (a) after each Bachelor leg ran, and (b) once a leg
#      has waited more than -SupplementaryHeldSec (1800), checked each time the wait loop comes round (queue/heat gate that returns GATE_BUSY, claim line) AND once more when a Bachelor pre-claim
#      or under-claim quiet probe ends VENUE_QUIET_UNKNOWN (under the claim: Bachelor's claim is released first), before the leg is stopped. COVERAGE LIMIT: one probe BLOCKS the loop, and a heavy-slot admission hold inside it can last up to -ProbeAdmissionWaitSec
#      (7200), so the held offer for an admission hold is made when that probe returns (at the latest at the admission ceiling), NOT at 30 minutes; a hold inside the leg child is offered after the leg. An offer needs: the spec's
#      criteria.supplementary block for the backend; ultra-magnus's own claim FREE and no live ticket (Get-VenueSupplementaryIdle, never a process scan); the build ALREADY staged in its
#      cache (the chain stages and builds nothing); the venue's claim taken (one shot, never waited on) and, UNDER it before any work, still no live ticket (else YIELDED_TO_TICKET:
#      released at once, so a waiter that queued meanwhile is served first); one quiet probe (-MaxWaitSec 0) QUIET or COOLDOWN_UNMET. Anything else is one typed
#      `SUPPLEMENTARY_SKIPPED leg=<n> trigger=<t> reason=<R>` line (NO_SUPPLEMENTARY_CRITERIA, BACKEND_NOT_IN_SPEC, SPEC_UNREADABLE, CHAIN_STOPPED, DEADLINE_NEAR, UM_BUSY, YIELDED_TO_TICKET, UM_NOT_QUIET,
#      UM_SHARE_UNREACHABLE, UM_SHARE_UNKNOWN, UM_REMOTE_JOB_UNRESOLVED, BUILD_NOT_STAGED, SUPPLEMENTARY_ERROR) and the chain goes on. A run writes `LEG <n> SUPPLEMENTARY_RUN` then SUPPLEMENTARY_RAN | SUPPLEMENTARY_FAILED |
#      SUPPLEMENTARY_TIMEOUT, each with `venue=ultra-magnus role=supplementary`, its own receipt (Invoke-VenueLeg) and leg-<n>-ultra-magnus-supplementary.out.txt. It is a bounded child
#      (-SupplementaryMaxSec, 1800, and never past the chain deadline). A supplementary result lives ONLY in CHAIN_RESULT's `supplementary` list: it is never in `legs`, never in ran / failed /
#      notRun, never moves exitCode, the streak rule or a verdict, and an exception in it is a typed skip, never a chain failure. The ultra-magnus build is hashed from ITS cache.
#      REMOTE JOB IDENTITY (VENUE-UM-SUPP-REMOTE-IDENTITY-AND-CACHE-BOUND-1): killing a timed-out client does not stop a remote job it already submitted, and the claim the chain then releases says
#      nothing about that job. The ids the client printed (`DVE_JOB_ID=<id>` lines of its stdout, see Get-VenueRemoteJobIds) go on the TIMEOUT entry (remoteJobIds, remoteJobState, venueIdle) and in
#      one `SUPPLEMENTARY_REMOTE_JOB` line, and are reconciled with named reads of <share>\outbox\<id>.result.json (Get-VenueRemoteJobState): FINISHED (every result landed) is the only idle answer.
#      NOT_FINISHED, or UNKNOWN (no id printed, an unusable id, the share silent) keeps the venue busy for the rest of the chain: every later offer is `SUPPLEMENTARY_SKIPPED ... reason=UM_REMOTE_JOB_UNRESOLVED
#      detail=jobId=<ids> ...` (never a run, never silently idle) until the result lands (`SUPPLEMENTARY_REMOTE_JOB_RECONCILED`). The cache read behind BUILD_NOT_STAGED / UM_SHARE_UNREACHABLE is bounded by
#      its 30 s -TimeoutSec: a read that does not answer is abandoned, never joined or disposed.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueChain.ps1 -Venue bachelor -LegSet display-matrix -Backend cpu -RunDir <rd> `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> [-PreferQuietWindows] [-OnUnknown Continue] [-DeadlineUtc <iso>]
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueChain.ps1 -QuietRateTable [-Venue bachelor] [-GateLogPath <gate.log>...]
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueChain.ps1 -ClaimStatus [-Venue bachelor]
#
# Exit codes (a leg's OUTCOME is never the exit code: read CHAIN_RESULT and the receipts): 0 every leg ran (Invoke-VenueLeg exited 0 for each); 2 usage (including a Wait-VenueQuiet usage
# error); 3 DEADLINE (-DeadlineUtc passed before every leg ran: queue busy, a live claim, or a window; the rest are NOT_RUN DEADLINE); 4 GATE_UNREADABLE
# and 5 HOST_MISMATCH from Wait-VenueQuiet (the chain stops); 6 at least one leg was NOT_RUN VENUE_QUIET_UNKNOWN; 7 (r2) at least one leg process exited
# non-zero, so it is LEG_FAILED / outcome INVALID in CHAIN_RESULT whatever DVE_OUTCOME it printed (a higher-priority code above is kept); 8 (r1, VENUE-CHAIN-UNHEALTHY-STREAK-STOP-1)
# the chain stopped on a deterministic venue fault: -UnhealthyStreakStop consecutive RAN legs were VENUE_UNHEALTHY with the same DVE_DETAIL key (r1 of
# VENUE-CHAIN-STREAK-DETAIL-NORMALISE-1: the leading reason token, else the text with every number replaced by #, so 'freeDiskGiB 42.4 < 50' and '41.9 < 50' are one fault),
# or the first one carried an owner-only detail token; the rest are NOT_RUN VENUE_UNHEALTHY_STREAK. 8 replaces 0 or 7 (it stops the chain, 7 does not); codes 2-6 above are kept.
# -UnhealthyStreakStop 0 switches off the COUNT rule only: the owner-only first-occurrence stop applies whatever N is.
# -QuietScript / -LegScript / -ClaimDir / -NowUtc / -SupplementaryAgentShare / -TestSupplementaryCapSec (a bound under 60 s for the supplementary child) are seams for the offline tests (tools/repo_hygiene/test_venue_chain_runner.py); production passes none.
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
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(1, 3600)][int]$TicketPollSec = 5,
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(0, 20)][int]$TicketKeepMax = 1,
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(1, 86400)][int]$ProbeAdmissionWaitSec = 7200,
    [Parameter(ParameterSetName = 'Chain')][switch]$NoTicket,
    [Parameter(ParameterSetName = 'Chain')][switch]$NoSupplementary,
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(0, 86400)][int]$SupplementaryHeldSec = 1800,
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(60, 14400)][int]$SupplementaryMaxSec = 1800,
    [Parameter(ParameterSetName = 'Chain')][string]$SupplementaryAgentShare = '',
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(0, 600)][int]$TestSupplementaryCapSec = 0,
    [Parameter(ParameterSetName = 'Chain')][string]$ChainLog = '',
    [Parameter(ParameterSetName = 'Chain')][switch]$PreferQuietWindows,
    [Parameter(ParameterSetName = 'Chain')][ValidateRange(0, 50)][int]$UnhealthyStreakStop = 3,
    [Parameter(ParameterSetName = 'Table', Mandatory = $true)][switch]$QuietRateTable,
    [Parameter(ParameterSetName = 'Status', Mandatory = $true)][switch]$ClaimStatus,
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

if ($PSCmdlet.ParameterSetName -eq 'Status') {
    Format-VenueClaimStatus -Status (Get-VenueClaimStatus -Venue $Venue -ClaimDir $ClaimDir)
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
# VENUE-CHAIN-UNHEALTHY-STREAK-STOP-1: the running count of consecutive RAN legs that were VENUE_UNHEALTHY with the SAME DVE_DETAIL KEY (and the receipts that formed it).
# These details are owner-only (a retry cannot clear them): the first occurrence stops the chain whatever -UnhealthyStreakStop is, 0 included (0 only switches off the count rule).
# test_venue_chain_runner.py reads the tokens the emitters print and asserts each is in this list.
$ownerOnlyDetails = @('SCREENSAVER_SECURE_OWNER_ONLY', 'SESSION_LOCKED', 'SESSION_LOCKED_OWNER_ONLY')
$streakKey = $null
$streakRaw = $null
$streakIds = [System.Collections.Generic.List[string]]::new()
$streakStop = $null

function Get-UnhealthyDetailKey([string]$Detail) {
    # VENUE-CHAIN-STREAK-DETAIL-NORMALISE-1: the stable identity of a VENUE_UNHEALTHY fault. A detail that opens with a reason token (KEEPALIVE_FAILED,
    # SCREENSAVER_SECURE_OWNER_ONLY, ...) is that token. Free text ('leg not submitted: freeDiskGiB 42.4 < 50') is compared with every number (signed,
    # decimal, exponent) replaced by '#', so a measurement that moves between legs does not hide a deterministic fault.
    if ($Detail -cmatch '^([A-Z][A-Z0-9_]{2,})(?=$|[\s:;,.])') { return $Matches[1] }
    ([regex]::Replace($Detail, '[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', '#')).Trim()
}

function Invoke-QuietProbe([string]$Name, [string]$Phase, [int]$MaxWaitSec, [string]$ProbeVenue = '', [int]$AdmissionWaitSec = 0) {
    # One Wait-VenueQuiet run. verdict: QUIET | COOLDOWN_UNMET | GATE_BUSY | GATE_UNREADABLE | HOST_MISMATCH | USAGE | VENUE_QUIET_UNKNOWN (not measured).
    # -ProbeVenue / -AdmissionWaitSec are used only by the supplementary (ultra-magnus) probe; the chain's own probes pass neither.
    $pv = $(if ($ProbeVenue) { $ProbeVenue } else { $Venue })
    $ProbeAdmissionWaitSec = $(if ($AdmissionWaitSec -gt 0) { $AdmissionWaitSec } else { $ProbeAdmissionWaitSec })   # a LOCAL shadow: the script's own value is untouched
    $out = Join-Path $RunDir "quiet-$Name-$Phase.out.txt"
    # VENUE-CHAIN-PROBE-SCRIPT-MISSING-NAMED-1: the probe script vanished after the start-up check (a worktree removed under a live chain): name it, do not launch.
    if (-not (Test-Path -LiteralPath $QuietScript -PathType Leaf)) {
        return [pscustomobject]@{ verdict = 'VENUE_QUIET_UNKNOWN'; code = 'none'; mean = $null; reason = 'PROBE_SCRIPT_MISSING'; waitedSec = $null; out = $out }
    }
    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript, '-Venue', $pv, '-WorkDir', (Join-Path $RunDir 'quiet'), '-GateLog', (Join-Path $RunDir 'gate.log'),
        '-ThresholdPercent', [string]$ThresholdPercent, '-MaxWaitSec', [string]$MaxWaitSec, '-RecheckSec', [string]$RecheckSec, '-MaxAdmissionWaitSec', [string]$ProbeAdmissionWaitSec)
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
        default { $reason = $(if ($code -eq 64 -and -not (Test-Path -LiteralPath $QuietScript -PathType Leaf)) { 'PROBE_SCRIPT_MISSING' } else { "undocumented-exit-$code" }); 'VENUE_QUIET_UNKNOWN' }
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

# VENUE-CLAIM-FIFO-1: this chain's place in the line for the leg being run ($null while it has none). It is taken at the claim wait (after a quiet pre-probe), kept
# across a GATE_BUSY under the claim for one more probe, and removed when the leg starts or the leg is given up. A ticket must out-live a quiet probe (Wait-VenueQuiet
# blocks the heartbeat), so the probe phases widen its stale-after to the probe's real upper bound, derived from what the probe can take (-QuietMaxWaitSec plus
# um-run's waits plus -ProbeAdmissionWaitSec, which is passed to the probe); while merely polling for the claim it is 3 x -ClaimPollSec (10 s floor).
# $ticketKeeps counts the GATE_BUSY-under-claim cycles this ticket has been kept through (-TicketKeepMax allows that many; the next one drops it).
$ticket = $null
$ticketKeeps = 0
$ticketPollStaleSec = [Math]::Max(10, 3 * $ClaimPollSec)
$ticketProbeStaleSec = Get-VenueTicketProbeStaleSec -QuietMaxWaitSec $QuietMaxWaitSec -AdmissionWaitSec $ProbeAdmissionWaitSec -MarginSec $ticketPollStaleSec

function Close-LegTicket([string]$LegName, [string]$Event, [string]$Why) {
    if ($null -eq $script:ticket) { return }
    $r = Remove-VenueTicket -Ticket $script:ticket
    L "$Event leg=$LegName ticket=$($script:ticket.name) result=$r reason=$Why keeps=$($script:ticketKeeps)/$TicketKeepMax"
    $script:ticket = $null
    $script:ticketKeeps = 0
}

# SUPPLEMENTARY LEGS (header item 6). Everything below is best-effort and OUTSIDE the chain's verdicts: its results go to $suppResults only (never $results, $exitCode, $stopReason or the
# streak), and Invoke-Supplementary never throws. The ultra-magnus probe waits at most $suppProbeAdmissionSec for admission (the Bachelor probes keep -ProbeAdmissionWaitSec), so the
# longest a supplementary offer can hold the chain is that probe plus -SupplementaryMaxSec; the leg's own Bachelor ticket is told so (phase 'supplementary') and is not lost meanwhile.
$suppVenue = 'ultra-magnus'
$suppEnabled = ($Venue -ceq 'bachelor') -and -not $NoSupplementary
$suppResults = [System.Collections.Generic.List[object]]::new()
$suppState = @{}               # leg name -> @{ done; nextUtc }: offered until a leg ran or a skip that cannot change (spec, build); a transient skip is not retried inside $suppRetrySec
$suppRetrySec = 300
$suppProbeAdmissionSec = 120
$suppProbeStaleSec = Get-VenueTicketProbeStaleSec -QuietMaxWaitSec 0 -AdmissionWaitSec $suppProbeAdmissionSec -MarginSec $ticketPollStaleSec
# A supplementary client killed at its timeout may have SUBMITTED a remote job that keeps running on ultra-magnus: that job, not the (released) claim, decides whether the venue is idle.
# $null, or @{ leg; jobIds; state } while the killed client's jobs are not all proven finished; every later offer must reconcile it first (header item 6).
$script:suppRemote = $null

function Add-SuppResult([object]$Leg, [string]$Trigger, [string]$Status, [hashtable]$Extra) {
    $r = [ordered]@{ name = $Leg.name; spec = $Leg.spec; backend = $Leg.backend; venue = $suppVenue; role = 'supplementary'; trigger = $Trigger; status = $Status }
    foreach ($k in $Extra.Keys) { $r[$k] = $Extra[$k] }
    $suppResults.Add([pscustomobject]$r)
}

function Format-RemoteJobIds($Ids) { $v = @($Ids | ForEach-Object { if ($_) { $_ } else { 'UNUSABLE' } }); if ($v.Count -gt 0) { $v -join ',' } else { 'UNKNOWN' } }

function Write-SuppSkip([object]$Leg, [string]$Trigger, [string]$Reason, [string]$Detail, [bool]$Final) {
    Add-SuppResult $Leg $Trigger 'SKIPPED' @{ reason = $Reason; detail = $Detail }
    L "SUPPLEMENTARY_SKIPPED leg=$($Leg.name) trigger=$Trigger reason=$Reason venue=$suppVenue role=supplementary$(if ($Detail) { " detail=$Detail" })"
    $st = $suppState[$Leg.name]
    if ($Final) { $st.done = $true } else { $st.nextUtc = [DateTime]::UtcNow.AddSeconds($suppRetrySec) }
}

function Invoke-SupplementaryCore([object]$Leg, [string]$Trigger) {
    $st = $suppState[$Leg.name]
    if ($null -eq $st) { $st = @{ done = $false; nextUtc = [DateTime]::MinValue }; $suppState[$Leg.name] = $st }
    if ($st.done) { return }
    if ($Trigger -ceq 'held' -and [DateTime]::UtcNow -lt $st.nextUtc) { return }
    $why = Get-VenueSupplementarySpecSkip -SpecPath $Leg.spec -Backend $Leg.backend
    if ($why) { Write-SuppSkip $Leg $Trigger $why '' $true; return }
    if ($null -ne $stopReason) { Write-SuppSkip $Leg $Trigger 'CHAIN_STOPPED' "stopReason=$stopReason" $true; return }
    $remainSec = [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds)
    if ($remainSec -lt 60) { Write-SuppSkip $Leg $Trigger 'DEADLINE_NEAR' "remainingSec=$remainSec" $false; return }
    $idle = Get-VenueSupplementaryIdle -Venue $suppVenue -ClaimDir $ClaimDir
    if (-not $idle.idle) { Write-SuppSkip $Leg $Trigger 'UM_BUSY' "holder $($idle.holder) liveTickets=$($idle.liveTickets)" $false; return }
    $share = $SupplementaryAgentShare
    if (-not $share) { $share = try { [string](([IO.File]::ReadAllText((Join-Path $here 'venues.json')) | ConvertFrom-Json).venues.$suppVenue.agentShare) } catch { '' } }
    if (-not $share) { Write-SuppSkip $Leg $Trigger 'UM_SHARE_UNKNOWN' 'venues.json names no ultra-magnus agentShare' $true; return }
    if ($null -ne $script:suppRemote) {
        # A released claim is not proof the venue is idle: the client of an earlier supplementary leg was killed with remote jobs it had submitted. Reconcile them (named, bounded reads) before any offer.
        $rem = $script:suppRemote
        $rs = Get-VenueRemoteJobState -AgentShare $share -JobId $rem.jobIds -TimeoutSec 30
        if ($rs.state -cne 'FINISHED') { Write-SuppSkip $Leg $Trigger 'UM_REMOTE_JOB_UNRESOLVED' "jobId=$(Format-RemoteJobIds $rem.jobIds) state=$($rs.state) from leg=$($rem.leg) (a released claim is not proof ultra-magnus is idle)" $false; return }
        L "SUPPLEMENTARY_REMOTE_JOB_RECONCILED leg=$($Leg.name) jobId=$(Format-RemoteJobIds $rem.jobIds) state=FINISHED from leg=$($rem.leg) venue=$suppVenue role=supplementary"
        $script:suppRemote = $null
    }
    $built = Get-VenueStagedBuildManifest -AgentShare $share -SourceCommit $SourceCommit -TimeoutSec 30
    if ($built.state -ceq 'UNREACHABLE') { Write-SuppSkip $Leg $Trigger 'UM_SHARE_UNREACHABLE' "$($built.error)" $false; return }
    if ($built.state -ceq 'NOT_STAGED') { Write-SuppSkip $Leg $Trigger 'BUILD_NOT_STAGED' "no staged build manifest for $($SourceCommit.Substring(0, 12)) at $($built.path); the chain stages and builds nothing" $true; return }
    $req = Request-VenueClaim -Venue $suppVenue -RunDir $RunDir -Purpose "$Purpose supplementary leg $($Leg.name)" -TtlSec ([Math]::Max(60, [Math]::Min($SupplementaryMaxSec, $remainSec) + $suppProbeStaleSec)) -ClaimDir $ClaimDir
    foreach ($b in $req.broken) { L "CLAIM_STALE_BROKEN venue=$suppVenue reason=$($b.reason) ownerPid=$($b.claim.ownerPid) runDir=$($b.claim.runDir) expiresUtc=$($b.claim.expiresUtc)" }
    if (-not $req.granted) { Write-SuppSkip $Leg $Trigger 'UM_BUSY' "claim state=$($req.state)" $false; return }
    $t0s = [DateTime]::UtcNow
    try {
        L "SUPPLEMENTARY_CLAIM_TAKEN leg=$($Leg.name) trigger=$Trigger venue=$suppVenue nonce=$($req.claim.nonce)"
        # Revalidate UNDER the claim, before any work: a UM waiter writes its ticket THEN tries the claim, this path wrote the claim THEN reads the line, so one of the two always
        # sees the other. A ticket that queued since the idle check above is served first: yield (the finally releases the claim) and do nothing (Dot 180e287f).
        $own = Get-VenueSupplementaryIdle -Venue $suppVenue -ClaimDir $ClaimDir -OwnNonce $req.claim.nonce
        if (-not $own.idle) { Write-SuppSkip $Leg $Trigger 'YIELDED_TO_TICKET' "liveTickets=$($own.liveTickets) claimState=$($own.claimState) (queued after the idle check)" $false; return }
        if ($script:ticket) { [void](Update-VenueTicket -Ticket $script:ticket -Phase 'supplementary' -StaleAfterSec ($suppProbeStaleSec + $SupplementaryMaxSec)) }
        $sq = Invoke-QuietProbe $Leg.name "um-suppl-$Trigger" 0 $suppVenue $suppProbeAdmissionSec
        L "SUPPLEMENTARY_PROBE leg=$($Leg.name) venue=$suppVenue exit=$($sq.code) verdict=$($sq.verdict) mean=$(Format-Mean $sq.mean)$(if ($sq.reason) { " reason=$($sq.reason)" })"
        if ($sq.verdict -cnotin @('QUIET', 'COOLDOWN_UNMET')) { Write-SuppSkip $Leg $Trigger 'UM_NOT_QUIET' "verdict=$($sq.verdict) mean=$(Format-Mean $sq.mean)$(if ($sq.reason) { " reason=$($sq.reason)" })" $false; return }
        $capSec = [Math]::Min($SupplementaryMaxSec, [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds))
        if ($TestSupplementaryCapSec -gt 0) { $capSec = $TestSupplementaryCapSec }   # offline test seam only (header): production passes none
        if ($capSec -lt 60 -and $TestSupplementaryCapSec -eq 0) { Write-SuppSkip $Leg $Trigger 'DEADLINE_NEAR' "remainingSec=$capSec after the probe" $false; return }
        $sOut = Join-Path $RunDir "leg-$($Leg.name)-ultra-magnus-supplementary.out.txt"
        $sArgs = @('-NoProfile', '-NonInteractive', '-File', $LegScript, '-Venue', $suppVenue, '-LegSpec', $Leg.spec, '-SourceCommit', $SourceCommit, '-BuildManifestSha256', $built.sha256)
        if ($Leg.backend) { $sArgs += @('-Backend', $Leg.backend) }
        if ($SheetCopyDir) { $sArgs += @('-SheetCopyDir', $SheetCopyDir) }
        L "LEG $($Leg.name) SUPPLEMENTARY_RUN venue=$suppVenue role=supplementary trigger=$Trigger quietVerdict=$($sq.verdict) quietMean=$(Format-Mean $sq.mean) capSec=$capSec umManifestSha256=$($built.sha256) spec=$($Leg.spec) backend=$(if ($Leg.backend) { $Leg.backend } else { 'spec' })"
        $sr = Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $sArgs -TimeoutSec $capSec
        [IO.File]::WriteAllText($sOut, $sr.stdout + $(if ($sr.stderr) { "`n--- stderr ---`n$($sr.stderr)" } else { '' }))
        $sLines = @($sr.stdout -split "`r?`n")
        $so = $sLines | Where-Object { $_ -match '^DVE_OUTCOME=(\S+)' } | Select-Object -Last 1
        $sOutcome = $(if ($so -and $so -match '^DVE_OUTCOME=(\S+)') { $Matches[1] } else { 'NONE' })
        $sp = $sLines | Where-Object { $_ -match '^DVE_RECEIPT_PATH=(.+)$' } | Select-Object -Last 1
        $sReceipt = $(if ($sp -and $sp -match '^DVE_RECEIPT_PATH=(.+)$') { $Matches[1].Trim() } else { $null })
        $sd = $sLines | Where-Object { $_ -match '^DVE_DETAIL=' } | Select-Object -Last 1
        $sDetail = $(if ($sd) { ($sd -replace '^DVE_DETAIL=', '').Trim() } else { '' })
        $sRole = $null
        try { if ($sReceipt -and (Test-Path -LiteralPath $sReceipt -PathType Leaf)) { $sRole = [string](([IO.File]::ReadAllText($sReceipt) | ConvertFrom-Json).venue.role) } } catch { }
        $sStatus = $(if ($sr.timedOut) { 'TIMEOUT' } elseif ($sr.exit -ne 0) { 'FAILED' } else { 'RAN' })
        # Whatever the leg printed, a leg that died or timed out is INVALID evidence, never a measurement (the Bachelor rule); and nothing here is read as a timing verdict.
        $shownOutcome = $(if ($sStatus -ceq 'RAN') { $sOutcome } else { 'INVALID' })
        $extra = @{ quietVerdict = $sq.verdict; quietMean = $sq.mean; outcome = $shownOutcome; printedOutcome = $sOutcome; legExit = $sr.exit; timedOut = $sr.timedOut; receipt = $sReceipt; receiptRole = $sRole
            umManifestSha256 = $built.sha256; elapsedSec = [int]([DateTime]::UtcNow - $t0s).TotalSeconds; outcomeDetail = $sDetail }
        if ($sr.timedOut) {
            # The client was killed, the job it submitted was not: keep the job's identity (what the client printed before it went silent) and reconcile it BEFORE the venue is read as idle.
            # No id printed = nothing to look up = the venue is busy-or-unknown, never open. The claim is still released below; it is not the proof.
            $rjIds = @(Get-VenueRemoteJobIds -Text $sr.stdout)
            $script:suppRemote = @{ leg = $Leg.name; jobIds = $rjIds; state = 'UNKNOWN' }   # fail closed first: an exception below must not leave the venue read as idle
            $rjs = Get-VenueRemoteJobState -AgentShare $share -JobId $rjIds -TimeoutSec 30
            $extra.remoteJobIds = $rjIds; $extra.remoteJobState = $rjs.state; $extra.venueIdle = ($rjs.state -ceq 'FINISHED')
            L "SUPPLEMENTARY_REMOTE_JOB leg=$($Leg.name) jobId=$(Format-RemoteJobIds $rjIds) state=$($rjs.state) venueIdle=$($extra.venueIdle)$(if ($rjs.error) { " reason=$($rjs.error)" }) venue=$suppVenue role=supplementary (a released claim is not proof of idleness; later offers reconcile this first)"
            if ($rjs.state -ceq 'FINISHED') { $script:suppRemote = $null } else { $script:suppRemote.state = $rjs.state }
        }
        Add-SuppResult $Leg $Trigger $sStatus $extra
        L "LEG $($Leg.name) SUPPLEMENTARY_$sStatus venue=$suppVenue role=supplementary trigger=$Trigger exit=$($sr.exit) outcome=$shownOutcome printedOutcome=$sOutcome receipt=$sReceipt receiptRole=$sRole quietVerdict=$($sq.verdict) quietMean=$(Format-Mean $sq.mean)$(if ($sDetail) { " detail=$sDetail" }) (supplementary: no timing verdict, no effect on the chain result)"
        $st.done = $true
    } finally { L "SUPPLEMENTARY_CLAIM_RELEASED leg=$($Leg.name) venue=$suppVenue result=$(Release-VenueClaim -Venue $suppVenue -Nonce $req.claim.nonce -ClaimDir $ClaimDir)" }
}

function Invoke-Supplementary([object]$Leg, [string]$Trigger) {
    if (-not $suppEnabled) { return }
    try { Invoke-SupplementaryCore $Leg $Trigger }
    catch {
        if (-not $suppState.ContainsKey($Leg.name)) { $suppState[$Leg.name] = @{ done = $false; nextUtc = [DateTime]::MinValue } }
        Write-SuppSkip $Leg $Trigger 'SUPPLEMENTARY_ERROR' ($_.Exception.Message -replace '\s+', ' ') $true
    }
}

$rateTable = $null
if ($PreferQuietWindows) {
    $rt = Get-RateTable
    $rateTable = $rt.table
    L "QUIET_RATE_TABLE logs=$($rt.logs.Count) hours=$(@($rateTable).Count) (for -PreferQuietWindows)"
}
L "CHAIN_START venue=$Venue legs=$($plan.Count) runDir=$RunDir claim=$(Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir) deadline=$($deadline.ToString('o')) onUnknown=$OnUnknown threshold=$ThresholdPercent quietMaxWaitSec=$QuietMaxWaitSec preferQuietWindows=$([bool]$PreferQuietWindows) pid=$PID quietScript=$QuietScript legScript=$LegScript supplementary=$([bool]$suppEnabled) supplementaryHeldSec=$SupplementaryHeldSec supplementaryMaxSec=$SupplementaryMaxSec"

$legIndex = 0
foreach ($leg in $plan) {
    $legIndex++
    if ($null -ne $stopReason) { Add-Result $leg 'NOT_RUN' @{ verdict = $stopReason; reason = 'chain-stopped' }; L "LEG $($leg.name) NOT_RUN $stopReason reason=chain-stopped"; continue }
    $ran = $false
    $ticket = $null; $ticketKeeps = 0
    $legWaitStart = [DateTime]::UtcNow   # the supplementary 'held' clock: how long this leg has waited in any gate
    while (-not $ran) {
        if ([DateTime]::UtcNow -ge $deadline) { $stopReason = 'DEADLINE'; $exitCode = 3; Add-Result $leg 'NOT_RUN' @{ verdict = 'DEADLINE'; reason = 'deadline-before-leg' }; L "LEG $($leg.name) NOT_RUN DEADLINE reason=deadline-before-leg"; break }
        # (a0) held more than -SupplementaryHeldSec in a gate: offer the same leg on ultra-magnus (header item 6); the Bachelor path below is unchanged
        if ($suppEnabled -and ([DateTime]::UtcNow - $legWaitStart).TotalSeconds -ge $SupplementaryHeldSec) { Invoke-Supplementary $leg 'held' }
        # (a) a better quiet window, waited for WITHOUT the claim
        if ($PreferQuietWindows) {
            $w = Select-VenueQuietWindow -Table $rateTable -NowUtc ([DateTime]::UtcNow) -PreferRatePercent $PreferRatePercent -HorizonHours $WindowHorizonHours -MinProbes $MinWindowProbes -DeadlineUtc $deadline
            L "WINDOW leg=$($leg.name) action=$($w.action) reason=$($w.reason) currentHour=$('{0:00}' -f $w.currentHour)Z currentRate=$($w.currentRate) currentProbes=$($w.currentProbes)$(if ($w.action -ceq 'WAIT') { " targetHour=$('{0:00}' -f $w.targetHour)Z targetRate=$($w.targetRate) waitSec=$($w.waitSec) (no claim held)" })"
            if ($w.action -ceq 'WAIT') {
                Close-LegTicket $leg.name 'TICKET_DROPPED' 'waiting-for-a-better-window'
                $until = [DateTimeOffset]::Parse($w.targetUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
                while ([DateTime]::UtcNow -lt $until -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Seconds ([int][Math]::Max(1, [Math]::Min(60, ($until - [DateTime]::UtcNow).TotalSeconds))) }
                continue
            }
        }
        # (b) the quiet wait, WITHOUT the claim
        if ($ticket) { [void](Update-VenueTicket -Ticket $ticket -Phase 'probing' -StaleAfterSec $ticketProbeStaleSec) }   # a ticket kept across a GATE_BUSY: held for this ONE probe
        $pre = Invoke-QuietProbe $leg.name 'pre-claim' $QuietMaxWaitSec
        L "QUIET leg=$($leg.name) phase=pre-claim exit=$($pre.code) verdict=$($pre.verdict) mean=$(Format-Mean $pre.mean) waitedSec=$($pre.waitedSec)$(if ($pre.reason) { " reason=$($pre.reason)" }) (no claim held)"
        if ($pre.verdict -ceq 'USAGE') { $stopReason = 'USAGE'; $exitCode = 2; Add-Result $leg 'NOT_RUN' @{ verdict = 'USAGE'; reason = 'wait-venuequiet-usage' }; L "LEG $($leg.name) NOT_RUN USAGE reason=wait-venuequiet-usage"; break }
        if ($pre.verdict -in @('GATE_UNREADABLE', 'HOST_MISMATCH')) { $stopReason = $pre.verdict; $exitCode = $(if ($pre.verdict -ceq 'GATE_UNREADABLE') { 4 } else { 5 }); Add-Result $leg 'NOT_RUN' @{ verdict = $pre.verdict; reason = 'phase=pre-claim' }; L "LEG $($leg.name) NOT_RUN $($pre.verdict) phase=pre-claim"; break }
        if ($pre.verdict -ceq 'VENUE_QUIET_UNKNOWN') {
            # (a1) the probe blocked the wait loop (heavy-slot admission can hold it up to -ProbeAdmissionWaitSec) and ended not measured: the leg has been held, so make the held
            # offer NOW, before -OnUnknown Stop sets stopReason (a stopped chain skips every offer as CHAIN_STOPPED). The Bachelor verdict, exit code and stop below are unchanged.
            if ($suppEnabled -and ([DateTime]::UtcNow - $legWaitStart).TotalSeconds -ge $SupplementaryHeldSec) {
                Invoke-Supplementary $leg 'held'
            }
            Add-Result $leg 'NOT_RUN' @{ verdict = 'VENUE_QUIET_UNKNOWN'; reason = $pre.reason; phase = 'pre-claim' }
            L "LEG $($leg.name) NOT_RUN VENUE_QUIET_UNKNOWN reason=$($pre.reason) phase=pre-claim (the venue was not measured; onUnknown=$OnUnknown)"
            $exitCode = 6; if ($OnUnknown -ceq 'Stop') { $stopReason = 'VENUE_QUIET_UNKNOWN' }
            break
        }
        if ($pre.verdict -ceq 'GATE_BUSY') { Close-LegTicket $leg.name 'TICKET_DROPPED' 'not-quiet-after-one-probe'; if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -lt $deadline) { Start-Sleep -Seconds $ClaimPollSec }; continue }
        # (c) the claim: only now, about to run. A LIVE claim of another chain is waited out unclaimed; stale claims are broken once each.
        $claim = $null; $lastHolder = ''
        $claimSleepSec = $(if ($NoTicket) { $ClaimPollSec } else { [Math]::Min($ClaimPollSec, $TicketPollSec) })   # a ticket holder polls only its OWN turn faster; the probe cadence is untouched
        while ($true) {
            $rq = @{ Venue = $Venue; RunDir = $RunDir; Purpose = "$Purpose leg $($leg.name)"; TtlSec = $ClaimTtlSec; ClaimDir = $ClaimDir }
            if (-not $NoTicket) { $rq['UseTicket'] = $true; $rq['TicketStaleAfterSec'] = $ticketPollStaleSec; if ($ticket) { $rq['Ticket'] = $ticket } }
            $req = Request-VenueClaim @rq
            if ($req.ticket -and ($null -eq $ticket -or $req.ticket.name -cne $ticket.name)) { L "TICKET_TAKEN leg=$($leg.name) ticket=$($req.ticket.name) runDir=$RunDir pid=$PID$(if ($ticket) { ' (the earlier ticket was lost: back of the line)' })" }
            if ($req.ticket) { if ($null -eq $ticket -or $req.ticket.name -cne $ticket.name) { $ticketKeeps = 0 }; $ticket = $req.ticket }   # a new place in the line starts a new keep count
            foreach ($st in $req.staleTickets) { L "TICKET_STALE_REMOVED venue=$Venue reason=$($st.reason) ownerPid=$($st.ticket.ownerPid) runDir=$($st.ticket.runDir) ticket=$($st.name)" }
            foreach ($b in $req.broken) { L "CLAIM_STALE_BROKEN venue=$Venue reason=$($b.reason) ownerPid=$($b.claim.ownerPid) ownerCreatedUtc=$($b.claim.ownerCreatedUtc) runDir=$($b.claim.runDir) expiresUtc=$($b.claim.expiresUtc)" }
            if ($req.granted) { $claim = $req.claim; break }
            $holder = $(if ($req.claim) { "pid=$($req.claim.ownerPid) runDir=$($req.claim.runDir) purpose=$($req.claim.purpose) expiresUtc=$($req.claim.expiresUtc)" } else { $req.state })
            $lineText = $(if ($req.ticket) { " position=$($req.position)/$(@($req.queue).Count) queue=[$(Format-VenueTicketQueue $req.queue)]" } else { '' })
            $lineKey = "$holder|$(@($req.queue | ForEach-Object { $_.name }) -join ',')"   # ages move every poll: log when the holder or the line changes, not every second
            if ($lineKey -cne $lastHolder) { L "CLAIM_HELD leg=$($leg.name) state=$($req.state) holder $holder$lineText -> wait (no claim held)"; $lastHolder = $lineKey }
            if ($suppEnabled -and ([DateTime]::UtcNow - $legWaitStart).TotalSeconds -ge $SupplementaryHeldSec) { Invoke-Supplementary $leg 'held' }   # held in the claim line: same offer as (a0)
            if ([DateTime]::UtcNow.AddSeconds($claimSleepSec) -ge $deadline) { break }
            Start-Sleep -Seconds $claimSleepSec
        }
        if ($null -eq $claim) { $stopReason = 'DEADLINE'; $exitCode = 3; Add-Result $leg 'NOT_RUN' @{ verdict = 'DEADLINE'; reason = 'claim-held' }; L "LEG $($leg.name) NOT_RUN DEADLINE reason=claim-held"; break }
        L "CLAIM_TAKEN leg=$($leg.name) nonce=$($claim.nonce) expiresUtc=$($claim.expiresUtc)"
        $released = $false
        try {
            # (d) ONE re-probe under the claim: the measurement nearest the leg is the one its line carries
            if ($ticket) { [void](Update-VenueTicket -Ticket $ticket -Phase 'probing' -StaleAfterSec $ticketProbeStaleSec) }
            $re = Invoke-QuietProbe $leg.name 'under-claim' 0
            L "QUIET leg=$($leg.name) phase=under-claim exit=$($re.code) verdict=$($re.verdict) mean=$(Format-Mean $re.mean)$(if ($re.reason) { " reason=$($re.reason)" })"
            if ($re.verdict -ceq 'GATE_BUSY') {
                L "CLAIM_RELEASED leg=$($leg.name) result=$(Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir) reason=gate-busy-under-claim (back to waiting)"; $released = $true
                if ($ticket) {
                    # the ONE allowed keep (-TicketKeepMax) holds the place for one more quiet probe; the next GATE_BUSY under the claim gives it up and rejoins at the back
                    if ($ticketKeeps -lt $TicketKeepMax) { $ticketKeeps++; L "TICKET_KEPT leg=$($leg.name) ticket=$($ticket.name) keeps=$ticketKeeps/$TicketKeepMax (keeps its place in the line for one more quiet probe)" }
                    else { Close-LegTicket $leg.name 'TICKET_DROPPED' 'gate-busy-under-claim-after-kept-cycles' }
                }
                continue
            }
            Close-LegTicket $leg.name 'TICKET_ENDED' 'leg-starting'
            if ($re.verdict -ceq 'USAGE') { $stopReason = 'USAGE'; $exitCode = 2; Add-Result $leg 'NOT_RUN' @{ verdict = 'USAGE'; reason = 'phase=under-claim' }; L "LEG $($leg.name) NOT_RUN USAGE phase=under-claim"; break }
            if ($re.verdict -in @('GATE_UNREADABLE', 'HOST_MISMATCH')) { $stopReason = $re.verdict; $exitCode = $(if ($re.verdict -ceq 'GATE_UNREADABLE') { 4 } else { 5 }); Add-Result $leg 'NOT_RUN' @{ verdict = $re.verdict; reason = 'phase=under-claim' }; L "LEG $($leg.name) NOT_RUN $($re.verdict) phase=under-claim"; break }
            if ($re.verdict -ceq 'VENUE_QUIET_UNKNOWN') {
                # (d1) the under-claim probe blocked in heavy-slot admission and ended not measured: same held offer as (a1), made AFTER Bachelor's claim is released (ultra-magnus is
                # offered while Bachelor is free, never while this chain holds it) and BEFORE -OnUnknown Stop sets stopReason. The leg never ran, so no after-leg offer follows.
                if ($suppEnabled -and ([DateTime]::UtcNow - $legWaitStart).TotalSeconds -ge $SupplementaryHeldSec) {
                    L "CLAIM_RELEASED leg=$($leg.name) result=$(Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir) reason=not-measured-under-claim (the held offer follows)"; $released = $true
                    Invoke-Supplementary $leg 'held'
                }
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
            $dl = $legLines | Where-Object { $_ -match '^DVE_DETAIL=' } | Select-Object -Last 1
            $detail = $(if ($dl) { ($dl -replace '^DVE_DETAIL=', '').Trim() } else { '' })
            if ($legCode -ne 0) {
                # Invoke-VenueLeg exits 0 whenever it wrote a receipt (any outcome, INVALID included); non-zero means it died without one. Whatever
                # DVE_OUTCOME it printed before dying is not evidence: the leg is LEG_FAILED / INVALID, never RAN, and the chain exits 7. It goes on to the next leg.
                Add-Result $leg 'LEG_FAILED' @{ verdict = $re.verdict; quietMean = $re.mean; outcome = 'INVALID'; printedOutcome = $outcome; legExit = $legCode; receipt = $null }
                L "LEG $($leg.name) LEG_FAILED exit=$legCode outcome=INVALID printedOutcome=$outcome verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) (the leg exited non-zero: no usable receipt)"
                if ($exitCode -eq 0) { $exitCode = 7 }
            } else {
                $preLoad = Get-LegPreLoadMean $receipt
                $class = $(if ($re.verdict -ceq 'COOLDOWN_UNMET') { 'COOLDOWN_UNMET' } elseif ($null -eq $preLoad) { 'QUIET_PRELOAD_UNKNOWN' } elseif ($preLoad -le 30.0) { 'QUIET_PRELOAD_OK' } else { 'QUIET_PRELOAD_HIGH' })
                $ranExtra = @{ verdict = $re.verdict; quietMean = $re.mean; preClaimVerdict = $pre.verdict; preClaimMean = $pre.mean; preLoadMean = $preLoad; class = $class; outcome = $outcome; legExit = $legCode; receipt = $receipt }
                if ($detail) { $ranExtra['outcomeDetail'] = $detail }
                Add-Result $leg 'RAN' $ranExtra
                L "LEG $($leg.name) RAN exit=$legCode outcome=$outcome verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) preLoadMean=$(Format-Mean $preLoad) class=$class receipt=$receipt$(if ($detail) { " detail=$detail" })"
                # VENUE-CHAIN-UNHEALTHY-STREAK-STOP-1: only a RAN leg counts or resets (LEG_FAILED / NOT_RUN never reach here). VENUE_UNHEALTHY with a detail extends the
                # streak when the detail KEY is the same (Get-UnhealthyDetailKey), otherwise starts it at 1; anything else (a different outcome, or no detail to compare) resets it.
                # -UnhealthyStreakStop 0 switches off the count only: the owner-only first occurrence stops the chain whatever N is.
                $rid = $(if ($receipt) { [IO.Path]::GetFileNameWithoutExtension($receipt) } else { "leg:$($leg.name)" })
                if ($outcome -ceq 'VENUE_UNHEALTHY' -and $detail) {
                    $key = Get-UnhealthyDetailKey $detail
                    if ($key -cne $streakKey) { $streakKey = $key; $streakIds.Clear() }
                    $streakRaw = $detail
                    $streakIds.Add($rid)
                } else { $streakKey = $null; $streakRaw = $null; $streakIds.Clear() }
                $ownerOnly = ($null -ne $streakKey) -and ($ownerOnlyDetails -ccontains $streakKey)
                if ($null -ne $streakKey -and ($ownerOnly -or ($UnhealthyStreakStop -gt 0 -and $streakIds.Count -ge $UnhealthyStreakStop))) {
                    $stopReason = 'VENUE_UNHEALTHY_STREAK'
                    if ($exitCode -eq 0 -or $exitCode -eq 7) { $exitCode = 8 }
                    $streakStop = [ordered]@{ detail = $streakRaw; key = $streakKey; n = $streakIds.Count; threshold = $UnhealthyStreakStop; ownerOnly = [bool]$ownerOnly; receipts = @($streakIds) }
                    L "CHAIN_STOP VENUE_UNHEALTHY_STREAK detail=$streakRaw n=$($streakIds.Count) threshold=$UnhealthyStreakStop receipts=$($streakIds -join ',')$(if ($ownerOnly) { ' ownerOnly=1 (a retry cannot clear it)' })$(if ($streakRaw -cne $streakKey) { " key=[$streakKey]" })"
                }
            }
            $ran = $true
        } finally {
            if (-not $released) { L "CLAIM_RELEASED leg=$($leg.name) result=$(Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir)" }
        }
        if (-not $ran) { break }
    }
    Close-LegTicket $leg.name 'TICKET_ENDED' 'leg-given-up'
    if ($ran) { Invoke-Supplementary $leg 'after-leg' }   # the Bachelor leg is done and its claim released; the cooldown below is unchanged and still runs in full
    if ($ran -and $legIndex -lt $plan.Count -and $null -eq $stopReason -and $CooldownSec -gt 0) {
        L "COOLDOWN ${CooldownSec} s (no claim held)"
        Start-Sleep -Seconds $CooldownSec
    }
}

$summary = [ordered]@{
    schema = 'mlv-app/venue-chain-result/v1'; venue = $Venue; runDir = $RunDir; startedUtc = $t0.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o')
    exitCode = $exitCode; stopReason = $stopReason; legs = @($results)
    ran = @($results | Where-Object { $_.status -ceq 'RAN' }).Count; failed = @($results | Where-Object { $_.status -ceq 'LEG_FAILED' }).Count; notRun = @($results | Where-Object { $_.status -ceq 'NOT_RUN' }).Count
    supplementary = @($suppResults)   # ultra-magnus runs and typed skips (header item 6): never counted in legs / ran / failed / notRun, never in exitCode
}
if ($streakStop) { $summary['unhealthyStop'] = $streakStop }
$resultLine = 'CHAIN_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
Add-Content -LiteralPath $ChainLog -Value $resultLine
Write-Output $resultLine
Complete-VenueProcess -ExitCode $exitCode   # a share read abandoned at its timeout would otherwise keep this process alive until it ends
exit $exitCode
