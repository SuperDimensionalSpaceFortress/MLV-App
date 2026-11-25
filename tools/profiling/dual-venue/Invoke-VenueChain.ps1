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
#      = QUIET plus preLoad <= 30; PASS and the exe match stay lane-side). A FOREIGN_FLOOR line in EITHER the pre-claim or the under-claim probe keeps class=COOLDOWN_UNMET whatever the
#      under-claim verdict reads (the LEG line and the result carry preClaimForeignFloor= / underClaimForeignFloor=). The quiet gate itself is unchanged: threshold and waits pass straight through.
#   4. QUIET WINDOWS. -QuietRateTable prints the per-UTC-hour QUIET rate of the venue's PROBE lines in -GateLogPath (default every
#      <main checkout>\.claude-state\fleet-runs\*\gate.log) and exits. -PreferQuietWindows: when the current hour's observed rate is under -PreferRatePercent
#      (30) on at least -MinWindowProbes probes and a later hour within -WindowHorizonHours is at or above it, the chain waits (unclaimed) for that hour.
#   5. LOG. -ChainLog (default <RunDir>\chain.log): one line per event, UTC stamp first; the last line is CHAIN_RESULT=<json> (also printed on stdout).
#      The leg's own output goes to <RunDir>\leg-<name>.out.txt, each probe's to <RunDir>\quiet-<name>-<phase>.out.txt, probes append to <RunDir>\gate.log.
#      Both .out.txt files are written when their child ends, as evidence only: each child is classified from its own captured output, never from a file another chain on the
#      same -RunDir can also write (DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r3).
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
# and 5 HOST_MISMATCH from Wait-VenueQuiet (the chain stops); 6 at least one leg was NOT_RUN VENUE_QUIET_UNKNOWN, or NOT_RUN VENUE_DRIVER_SCRIPT_MISSING
# (LANE-RETIRE-LIVE-DETACHED-DRIVER-GUARD-1: the probe or leg script is gone, its worktree removed under the chain; one typed
# `VENUE_DRIVER_SCRIPT_MISSING path=<script>` line and the chain stops whatever -OnUnknown says); 7 (r2) at least one leg process exited
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
    [Parameter(ParameterSetName = 'Chain', Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}\z')][string]$SourceCommit,
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
# DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r2 (adversary A3): a caller's $PSNativeCommandUseErrorActionPreference = $true is inherited by `& script`, and with Stop it turns a
# leg's or quiet probe's documented non-zero exit (a spec refusal, exit 6) into a terminating error before the typed result and the next leg. The raw launches below read
# $LASTEXITCODE themselves, as Invoke-VenueLeg's matrix walk does.
$PSNativeCommandUseErrorActionPreference = $false
$here = $PSScriptRoot
# b3 r1 (adversary pass 5 A8 sibling on a73430b2): the VenueChain module object is bound once, here, as Invoke-VenueLeaseBatch.ps1 does; by name, `Get-Module VenueChain` answers
# every same-name module in the session (-Force keeps one imported from another path) and `& (Get-Module VenueChain) { }` throws. A harness that lifts one function and never
# runs this line falls back to the lookup by name (`??`).
Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking -PassThru -OutVariable VenueChainImported | Out-Null
$script:VenueChainModule = $VenueChainImported[0]

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
    # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-2 r2 (adversary pass 3 A4 on 372d1bfd): the chain-log append is BOUNDED (Add-ChainLogLine). A synchronous Add-Content on a ChainLog
    # share that stops answering without throwing held every path behind it, the NOT_RUN DEADLINE line before the claim release and CHAIN_RESULT included. An append that
    # fails still throws, as before (every caller's own try/catch keeps its meaning); one that does not answer in time is said once on stderr, bounded, and the line still goes out.
    $aw = Add-ChainLogLine $line
    if ($aw -clike 'FAILED *') { throw "the chain log append failed: $($aw.Substring(7))" }
    if ($aw -cne 'OK') { Write-ChainDiagnostic "CHAIN_LOG_APPEND_$aw path=$ChainLog (the line is on stdout; the chain goes on)" }
    Write-Output $line
}

$script:ChainLogAppendFailures = 0
function Write-ChainLineNoThrow([string]$Message) {
    # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-3 r3 (sol r2 B1): L for the lines of a leg's result path (LEG_FAILED, its spec-refusal label) and of the claim's release in the
    # finally. L throws on a FAILED append (as Add-Content did), and from there that throw ended the chain before CHAIN_RESULT. Here a failed append is said on stderr
    # (bounded), counted in CHAIN_RESULT (chainLogAppendFailures), and the line still goes to stdout; the chain's result, exit code and next leg are never decided by it.
    # (the catch is one line, as every chain-log fallback here is, pinned by test_planned_hash_bind_b2_r1_p1_a4: the bounded diagnostic first, then the count, then the line on stdout)
    try { L $Message }
    catch { Write-ChainDiagnostic "$Message (chain log write failed: $($_.Exception.Message -replace '\s+', ' '))"; $script:ChainLogAppendFailures++; Write-Output "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message" }
}

$script:ChainLogAppendDelayMs = 0   # offline test seam only (the bounded append's pool thread waits this long first, as a stalled share does); production leaves it 0
function Add-ChainLogLine([string]$Line) {
    # b2 r2 (A4): one chain-log line appended on a pool thread (the compiled writer below) and waited for at most $script:EvidenceWriteBoundMs; without the compiled writer, on a
    # worker runspace given up on after 2 s (Invoke-VenueBoundedRead: an abandoned one is counted, so Complete-VenueProcess ends the process). Never throws.
    # 'OK' | 'FAILED <why>' | 'TIMEOUT' (the append may still land later)
    if ($script:EvidenceWriterReady) {
        $we = $null
        $rc = try { [VenueChainEvidenceWriteB2]::AppendWithin($ChainLog, $Line + [Environment]::NewLine, $script:ChainLogAppendDelayMs, $script:EvidenceWriteBoundMs, [ref]$we) } catch { $we = $_.Exception.Message; 1 }
        return @('OK', "FAILED $($we -replace '\s+', ' ')", 'TIMEOUT')[$rc]
    }
    # b2 r3 (adversary pass 4 A1 sibling on 8b4b6d8c): Invoke-VenueBoundedRead is VenueChain's own (not exported), so it is called in the module's scope, as the batch does;
    # called by name from here it never resolved, so every L threw and the unguarded CHAIN_START ended the chain before CHAIN_RESULT
    $ar = try { & ($script:VenueChainModule ?? (Get-Module VenueChain)) { param($t, $a, $s) Invoke-VenueBoundedRead -ScriptText $t -Argument $a -TimeoutSec $s } 'param($a) if ($a.d -gt 0) { [Threading.Thread]::Sleep($a.d) }; try { [IO.File]::AppendAllText($a.p, $a.t, [Text.UTF8Encoding]::new($false)); "OK" } catch { "FAILED " + $_.Exception.Message }' @{ p = $ChainLog; t = $Line + [Environment]::NewLine; d = $script:ChainLogAppendDelayMs } 2 } catch { [pscustomobject]@{ answered = $true; value = @("FAILED $($_.Exception.Message)"); error = $null } }
    if (-not $ar.answered) { return 'TIMEOUT' }
    $av = @($ar.value | ForEach-Object { [string]$_ })
    if ($av -ccontains 'OK') { return 'OK' }
    "FAILED $((($av -join ' ') + ' ' + [string]$ar.error).Trim() -replace '^FAILED ', '' -replace '\s+', ' ')"
}

# The planned legs: name, spec path, backend ('' = whatever the spec lists, as Invoke-VenueLeg does with no -Backend).
$plan = [System.Collections.Generic.List[object]]::new()
if ($LegSet) {
    Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force
    $setPath = Join-Path $here "legsets\$LegSet.json"
    $sp = try { Get-DvLegSetPlan -LegSetPath $setPath -Repeats $Repeats -Backend $(if ($Backend) { $Backend } else { '' }) } catch { Stop-Usage $_.Exception.Message }
    # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 (sol r6 INV-3 sibling): each entry keeps the sha256 of the spec bytes the set planner judged (Get-DvLegSetPlan), and every leg child
    # below is bound to it with -ExpectSpecSha256, so bytes swapped in after planning (a committed visual twin's) are refused by the leg, never run in the planned speed slot
    foreach ($e in $sp.plan) { $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f $e.seq, $e.legId, $e.backend); spec = $e.specPath; backend = $e.backend; specSha256 = [string]$e.specSha256 }) }
} else {
    $seq = 0
    foreach ($ref in $LegSpec) {
        $seq++
        $path = $(if (Test-Path -LiteralPath $ref -PathType Leaf) { $ref } elseif (Test-Path -LiteralPath (Join-Path $here "legs\$ref") -PathType Leaf) { Join-Path $here "legs\$ref" } else { Stop-Usage "no leg spec at '$ref'" })
        $path = (Resolve-Path -LiteralPath $path).Path
        # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1: ONE read of the spec's bytes names the leg AND gives the sha256 its child is bound to (RAW bytes, no line-ending normalisation:
        # the hash Invoke-VenueLeg's -ExpectSpecSha256 takes), decoded BOM-detected as File.ReadAllText does. Bytes that cannot be read here are a usage refusal before any claim:
        # an unread spec has no planned hash to bind the child to.
        # (r2, adversary A2: a direct assignment, so an empty file is a zero-length array; `$x = try { <empty array> }` enumerates it to $null and the hash then threw)
        try { [byte[]]$specBytes = [IO.File]::ReadAllBytes($path) } catch { Stop-Usage "LEG_SPEC_UNREADABLE '$path' could not be read to plan it ($($_.Exception.Message -replace '\s+', ' '))" }
        $specSha256 = ([BitConverter]::ToString([Security.Cryptography.SHA256]::HashData([byte[]]$specBytes)) -replace '-', '').ToLowerInvariant()
        $legId = try { $rdr = [IO.StreamReader]::new([IO.MemoryStream]::new([byte[]]$specBytes), [Text.Encoding]::UTF8, $true); try { [string](($rdr.ReadToEnd() | ConvertFrom-Json).legId) } finally { $rdr.Dispose() } } catch { '' }
        if ($legId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$') { $legId = [IO.Path]::GetFileNameWithoutExtension($path) }
        for ($r = 1; $r -le $Repeats; $r++) {
            $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f ($plan.Count + 1), $legId, $(if ($Backend) { $Backend } else { 'spec' })); spec = $path; backend = $(if ($Backend) { $Backend } else { '' }); specSha256 = $specSha256 })
        }
    }
}

function Get-LegSpecRefusalLine([string[]]$StdoutLines, [object]$Leg) {
    # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1: the leg child's own typed refusal of a spec that is not the planned bytes (Invoke-VenueLeg prints these anchored whole-line STDOUT records
    # before anything is generated or submitted), naming THIS leg's planned hash; '' when there is none. A line that merely quotes a token is not one (the #892 r7 A1 rule).
    foreach ($l in @($StdoutLines)) {
        if ($l -cmatch '^DVE_LEG_SPEC_REFUSED planned_sha256=(?<p>[0-9a-f]{64}) read_sha256=(?<r>[0-9a-f]{64})$' -and $Matches['p'] -ceq [string]$Leg.specSha256) { return "DVE_CHAIN_SPEC_CHANGED leg=$($Leg.name) planned_sha256=$($Matches['p']) read_sha256=$($Matches['r'])" }
        if ($l -cmatch '^DVE_LEG_SPEC_UNREADABLE planned_sha256=(?<p>[0-9a-f]{64})$' -and $Matches['p'] -ceq [string]$Leg.specSha256) { return "DVE_CHAIN_SPEC_UNREADABLE leg=$($Leg.name) planned_sha256=$($Matches['p'])" }
    }
    ''
}

$script:EvidenceWriteBoundMs = 2000
function Write-ChainEvidence([string]$Path, [string[]]$Lines) {
    # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r3 fix 2 (adversary pass 3 A2): a child's transcript is evidence only, so its write never holds the chain: it runs on a pool thread
    # and is waited for at most $script:EvidenceWriteBoundMs (a -RunDir share that stops answering delays neither a probe's answer, the leg's launch nor the claim's release by more).
    # 'OK' | 'FAILED <why>' | 'TIMEOUT' (the write may still land later; nothing reads it back) | 'SKIPPED <why>' (the bounded writer could not be compiled at start: no transcript).
    # r4 (adversary pass 5 A5): the writer is compiled ONCE at script start (below), never here, so nothing in this helper can throw past its caller.
    if (-not $script:EvidenceWriterReady) { return "SKIPPED $($script:EvidenceWriterError -replace '\s+', ' ')" }
    $we = $null
    $rc = try { [VenueChainEvidenceWriteB2]::WriteWithin($Path, [string[]]@($Lines), $script:EvidenceWriteBoundMs, [ref]$we) } catch { $we = $_.Exception.Message; 1 }
    @('OK', "FAILED $($we -replace '\s+', ' ')", 'TIMEOUT')[$rc]
}

function Write-ChainDiagnostic([string]$Line) {
    # r4 fix 2 (adversary pass 6 A6): a transcript-write diagnostic said on stderr, bounded like the write itself ($script:EvidenceWriteBoundMs), so it holds neither the child's
    # classification nor the claim's release. With no compiled writer it is skipped (that was said once at start). Never throws.
    if (-not $script:EvidenceWriterReady) { return }
    try { $null = [VenueChainEvidenceWriteB2]::ErrorLineWithin($Line, $script:EvidenceWriteBoundMs) } catch { $null = $_ }
}
# DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r4 (adversary pass 5 A5): Write-ChainEvidence's Add-Type used to run on the first probe's transcript, outside any try, under this script's
# ErrorActionPreference Stop and with the CALLER's $PSDefaultParameterValues (an Add-Type:OutputAssembly default naming an existing file made it throw: no probe verdict, no
# CHAIN_RESULT). It is compiled here, once, before any child, inside a try and a child scope whose $PSDefaultParameterValues is empty. A compile that still fails leaves the chain
# running with no transcripts (Write-ChainEvidence answers SKIPPED; every transcript is evidence only and nothing reads one back), said once on stderr.
# b2 r2 (adversary pass 3 A4): the type carries the r2 shape (AppendWithin) in its name, so a same-named type an older copy of this script loaded in this process is never used
$script:EvidenceWriterReady = [bool]('VenueChainEvidenceWriteB2' -as [type])
$script:EvidenceWriterError = ''
if (-not $script:EvidenceWriterReady) {
    try {
        & {
            $PSDefaultParameterValues = @{}
            Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
public static class VenueChainEvidenceWriteB2 {
    // b2 r2 (adversary pass 3 A4 on 372d1bfd): one chain-log line appended on a pool thread and waited for at most timeoutMs (a ChainLog share that stops answering without
    // throwing cannot hold the caller); delayMs is the offline test seam (0 in production). 0 OK, 1 FAILED (error says why), 2 TIMEOUT (the append may still land later).
    public static int AppendWithin(string path, string text, int delayMs, int timeoutMs, out string error) {
        error = null;
        System.Threading.Tasks.Task t = System.Threading.Tasks.Task.Run(() => { if (delayMs > 0) { System.Threading.Thread.Sleep(delayMs); } System.IO.File.AppendAllText(path, text); });
        try { if (!t.Wait(timeoutMs)) { return 2; } } catch (AggregateException e) { error = e.InnerException != null ? e.InnerException.Message : e.Message; return 1; }
        return 0;
    }
    public static int WriteWithin(string path, string[] lines, int timeoutMs, out string error) {
        error = null;
        System.Threading.Tasks.Task t = System.Threading.Tasks.Task.Run(() => System.IO.File.WriteAllLines(path, lines));
        try { if (!t.Wait(timeoutMs)) { return 2; } } catch (AggregateException e) { error = e.InnerException != null ? e.InnerException.Message : e.Message; return 1; }
        return 0;
    }
    // r4 fix 2 (adversary pass 6 A6): a diagnostic line on stderr, written on a pool thread and waited for at most timeoutMs (a full stderr pipe nobody reads cannot hold the caller)
    public static bool ErrorLineWithin(string line, int timeoutMs) {
        System.Threading.Tasks.Task t = System.Threading.Tasks.Task.Run(() => Console.Error.WriteLine(line));
        try { return t.Wait(timeoutMs); } catch (AggregateException) { return false; }
    }
}
'@
        }
        $script:EvidenceWriterReady = [bool]('VenueChainEvidenceWriteB2' -as [type])
        if (-not $script:EvidenceWriterReady) { $script:EvidenceWriterError = 'the bounded transcript writer did not load' }
    } catch { $script:EvidenceWriterError = "the bounded transcript writer could not be compiled: $($_.Exception.Message)" }
    # b2 r1 (adversary pass 2 A4 on bf5ebeb2): said in the chain log, never synchronously on stderr (a full unread pipe held the start before the first probe); best effort
    if (-not $script:EvidenceWriterReady) { try { L "CHAIN_EVIDENCE_WRITER_UNAVAILABLE $($script:EvidenceWriterError -replace '\s+', ' ') (child transcripts are skipped; every child is still classified from its own captured output)" } catch { $null = $_ } }
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
    # LANE-RETIRE-LIVE-DETACHED-DRIVER-GUARD-1: typed VENUE_DRIVER_SCRIPT_MISSING (reason path=<script>), never VENUE_QUIET_UNKNOWN: the chain stops on it.
    $missing = Get-VenueDriverScriptMissing -Path @($QuietScript)
    if ($missing) {
        return [pscustomobject]@{ verdict = 'VENUE_DRIVER_SCRIPT_MISSING'; code = 'none'; mean = $null; reason = "path=$QuietScript"; waitedSec = $null; out = $out }
    }
    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript, '-Venue', $pv, '-WorkDir', (Join-Path $RunDir 'quiet'), '-GateLog', (Join-Path $RunDir 'gate.log'),
        '-ThresholdPercent', [string]$ThresholdPercent, '-MaxWaitSec', [string]$MaxWaitSec, '-RecheckSec', [string]$RecheckSec, '-MaxAdmissionWaitSec', [string]$ProbeAdmissionWaitSec)
    # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r3 (adversary pass 2 A1 sibling): the probe's output is CAPTURED from its own child and read from that capture. The RunDir-named
    # file is shared by every chain on this -RunDir (two venues' chains name a leg alike), so it is written after the probe as evidence only and never read back.
    $qRaw = @(& $pwshExe @qArgs 2>&1)
    $code = $LASTEXITCODE
    # r3 fix 2 (adversary pass 3 A6): the verdict is the probe's STDOUT (Wait-VenueQuiet's Write-Gate); a stderr line is in the transcript and decides nothing
    $lines = @($qRaw | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { [string]$_ })
    $qw = Write-ChainEvidence $out @($qRaw | ForEach-Object { [string]$_ })   # evidence only, bounded (r3 fix 2, A2): a file that cannot be written, or a share that stalls, never changes or delays the verdict
    if ($qw -cne 'OK') { Write-ChainDiagnostic "QUIET_OUT_WRITE_$qw probe=$Name phase=$Phase path=$out (evidence only: the probe is classified from its own captured output)" }   # r4 (pass 5 A6): said on stderr, as the leg's is
    $v = $lines | Where-Object { $_ -match '^(QUIET|COOLDOWN_UNMET) mean=' } | Select-Object -Last 1
    $u = $lines | Where-Object { $_ -like 'VENUE_QUIET_UNKNOWN *' } | Select-Object -Last 1
    $ff = $lines | Where-Object { $_ -match '^FOREIGN_FLOOR ' } | Select-Object -Last 1   # BACHELOR-VENUE-FOREIGN-FLOOR-FAST-FAIL-1 r3: the early-exit marker survives into the leg class
    $mean = $null; $reason = $null
    $verdict = switch ($code) {
        0 { if ($v) { ($v -split ' ')[0] } else { $reason = 'exit0-without-verdict'; 'VENUE_QUIET_UNKNOWN' } }
        2 { 'USAGE' }
        3 { 'GATE_BUSY' }
        4 { 'GATE_UNREADABLE' }
        5 { 'HOST_MISMATCH' }
        6 { $reason = $(if ($u -and $u -match '\breason=(\S+)') { $Matches[1] } else { 'exit6' }); 'VENUE_QUIET_UNKNOWN' }
        default { if ($code -eq 64 -and (Get-VenueDriverScriptMissing -Path @($QuietScript))) { $reason = "path=$QuietScript"; 'VENUE_DRIVER_SCRIPT_MISSING' } else { $reason = "undocumented-exit-$code"; 'VENUE_QUIET_UNKNOWN' } }
    }
    if ($v -and $v -match '\bmean=([0-9.]+)%') { $mean = [double]$Matches[1] }
    $waited = $(if (($v, $u | Where-Object { $_ }) -join ' ' -match '\bwaitedSec=(\d+)') { [int]$Matches[1] } else { $null })
    [pscustomobject]@{ verdict = $verdict; code = $code; mean = $mean; reason = $reason; waitedSec = $waited; out = $out; foreignFloor = [bool]$ff; foreignFloorLine = [string]$ff }
}

function Format-Mean($m) { if ($null -eq $m) { 'UNKNOWN' } else { '{0:0.0}' -f $m } }

function Stop-DriverScriptMissing([object]$Leg, [string]$PathReason, [string]$Phase) {
    # LANE-RETIRE-LIVE-DETACHED-DRIVER-GUARD-1: a script the chain launches is gone (its worktree was removed under it). One typed line, the leg
    # NOT_RUN, and the chain STOPS whatever -OnUnknown says (exit 6, like any leg that was not measured): re-probing cannot bring the script back.
    Add-Result $Leg 'NOT_RUN' @{ verdict = 'VENUE_DRIVER_SCRIPT_MISSING'; reason = $PathReason; phase = $Phase }
    L "VENUE_DRIVER_SCRIPT_MISSING $PathReason leg=$($Leg.name) phase=$Phase (the chain stops whatever -OnUnknown says)"
    L "LEG $($Leg.name) NOT_RUN VENUE_DRIVER_SCRIPT_MISSING $PathReason phase=$Phase"
    $script:stopReason = 'VENUE_DRIVER_SCRIPT_MISSING'
    $script:exitCode = 6
}

function Get-LegPreLoadMean([string]$ReceiptPath) {
    # The leg's own pre-load mean (cpuQuiescence.timeMeanPercent of the run's summary, never the receipt file): one reader, VenueChain.psm1's, which knows both the
    # playback and the export evidence layouts. EXPORT-VENUE-LEG-1 r3: this script's own copy of it had the playback path only. $null when absent.
    Get-VenueLegPreLoadMean -ReceiptPath $ReceiptPath
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
    $built = Get-VenueStagedBuildManifest -AgentShare $share -SourceCommit $SourceCommit -TimeoutSec 30 -VerifyArtifacts
    if ($built.state -ceq 'UNREACHABLE') { Write-SuppSkip $Leg $Trigger 'UM_SHARE_UNREACHABLE' "$($built.error)" $false; return }
    if ($built.state -ceq 'NOT_STAGED') { Write-SuppSkip $Leg $Trigger 'BUILD_NOT_STAGED' "no staged build manifest for $($SourceCommit.Substring(0, 12)) at $($built.path); the chain stages and builds nothing" $true; return }
    # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1: a staged manifest whose cached exe/dll/zip is absent or differs from its digest would fail in the leg job after the claim and the probe
    if ($built.artifactProblem) { $ap = $built.artifactProblem -split ' ', 2; Write-SuppSkip $Leg $Trigger $ap[0] "$($ap[1])" $true; return }
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
        if ($script:ticket) { $pf = [string](Update-VenueTicket -Ticket $script:ticket -Phase 'supplementary' -StaleAfterSec ($suppProbeStaleSec + $SupplementaryMaxSec)); if ($pf -cne 'OK') { try { L "TICKET_PHASE_$pf ticket=$($script:ticket.name) phase=supplementary (the phase and its longer lifetime did not land; the probe goes on)" } catch { $null = $_ } } }   # r7 A15: a log line that cannot be written never stops the probe
        $sq = Invoke-QuietProbe $Leg.name "um-suppl-$Trigger" 0 $suppVenue $suppProbeAdmissionSec
        L "SUPPLEMENTARY_PROBE leg=$($Leg.name) venue=$suppVenue exit=$($sq.code) verdict=$($sq.verdict) mean=$(Format-Mean $sq.mean)$(if ($sq.reason) { " reason=$($sq.reason)" })"
        if ($sq.verdict -cnotin @('QUIET', 'COOLDOWN_UNMET')) { Write-SuppSkip $Leg $Trigger 'UM_NOT_QUIET' "verdict=$($sq.verdict) mean=$(Format-Mean $sq.mean)$(if ($sq.reason) { " reason=$($sq.reason)" })" $false; return }
        $capSec = [Math]::Min($SupplementaryMaxSec, [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds))
        if ($TestSupplementaryCapSec -gt 0) { $capSec = $TestSupplementaryCapSec }   # offline test seam only (header): production passes none
        if ($capSec -lt 60 -and $TestSupplementaryCapSec -eq 0) { Write-SuppSkip $Leg $Trigger 'DEADLINE_NEAR' "remainingSec=$capSec after the probe" $false; return }
        $sOut = Join-Path $RunDir "leg-$($Leg.name)-ultra-magnus-supplementary.out.txt"
        $sArgs = @('-NoProfile', '-NonInteractive', '-File', $LegScript, '-Venue', $suppVenue, '-LegSpec', $Leg.spec, '-SourceCommit', $SourceCommit, '-BuildManifestSha256', $built.sha256)
        if ($Leg.backend) { $sArgs += @('-Backend', $Leg.backend) }
        $sArgs += @('-ExpectSpecSha256', [string]$Leg.specSha256)   # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1: the supplementary child runs the planned bytes or nothing
        if ($SheetCopyDir) { $sArgs += @('-SheetCopyDir', $SheetCopyDir) }
        L "LEG $($Leg.name) SUPPLEMENTARY_RUN venue=$suppVenue role=supplementary trigger=$Trigger quietVerdict=$($sq.verdict) quietMean=$(Format-Mean $sq.mean) capSec=$capSec umManifestSha256=$($built.sha256) spec=$($Leg.spec) backend=$(if ($Leg.backend) { $Leg.backend } else { 'spec' })"
        # r3 fix 3 (adversary pass 4 A1 sibling): the append above can stall; the cap is cut to the room left NOW, the last step before the child starts (production only: the
        # offline -TestSupplementaryCapSec seam keeps its own bound), and no room left is no launch
        # r4 (adversary pass 5 A2): the 60 s floor holds after this late cut too (it admitted 1-59 s), and the bounded wait is also given the ABSOLUTE deadline: it starts no child at
        # or past it and kills one still running at it, whatever its relative bound (a parent descheduled between this cut and the child's start used to get the full relative wait)
        $suppDeadline = $null
        if ($TestSupplementaryCapSec -eq 0) {
            $capSec = [Math]::Min($capSec, [int][Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds))
            if ($capSec -lt 60) { Write-SuppSkip $Leg $Trigger 'DEADLINE_NEAR' "remainingSec=$capSec at the launch" $false; return }
            $suppDeadline = $deadline
        }
        # r4 fix 2 (adversary pass 6 A1): and the 60 s floor itself is asked again inside the helper, at its last clock read before the start (-MinRoomSec)
        $sr = $(if ($null -ne $suppDeadline) { Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $sArgs -TimeoutSec $capSec -DeadlineUtc $suppDeadline -MinRoomSec 60 } else { Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $sArgs -TimeoutSec $capSec })
        if ($sr.notStarted) { Write-SuppSkip $Leg $Trigger 'DEADLINE_NEAR' "remainingSec<60 at the child's start (less than the 60 s floor was left before -DeadlineUtc; nothing started)" $false; return }
        if ($sr.timedOut) {
            # Arm the unresolved-job guard at once, BEFORE any fallible step (the output write below, the receipt read, the share reads): the client was killed, the job it submitted was not,
            # and an exception from here on must leave the venue read as busy-or-unknown, never idle. Armed with no ids first, so even a failure reading the ids fails closed.
            $script:suppRemote = @{ leg = $Leg.name; jobIds = @(); state = 'UNKNOWN' }
            $script:suppRemote.jobIds = @(Get-VenueRemoteJobIds -Text $sr.stdout)
        }
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
        # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1: a supplementary child that refused other bytes than the planned ones says so on its own typed line (classified from its stdout only)
        # BATCH-3 r5 (sol r4 B1): read on EVERY status but RAN, as the primary leg's branch reads it: a child that printed its record, wrote no receipt and was observed past its
        # bound (TIMEOUT, its stdout complete) lost the refusal when only FAILED was read
        $sSpecRefusal = $(if ($sStatus -cne 'RAN' -and -not $sReceipt) { Get-LegSpecRefusalLine $sLines $Leg } else { '' })
        # (r2, adversary A5: the label's log write may fail; it never replaces the observed child failure recorded by Add-SuppResult below)
        # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r4 (adversary pass 5 A4): the child is classified above from its own captured stdout BEFORE its transcript is written, and the write is
        # the bounded, non-throwing Write-ChainEvidence (a read-only or stalled file used to throw here and turn the typed refusal into SUPPLEMENTARY_ERROR, or hold the claim release)
        $sw = Write-ChainEvidence $sOut @($sr.stdout + $(if ($sr.stderr) { "`n--- stderr ---`n$($sr.stderr)" } else { '' }))
        if ($sw -cne 'OK') { Write-ChainDiagnostic "SUPPLEMENTARY_OUT_WRITE_$sw leg=$($Leg.name) path=$sOut (evidence only: the supplementary child is classified from its own captured output)" }
        # BATCH-3 r4 (sol r3 B1): the spec-refusal label is written AFTER the result below records specRefusal, through Write-ChainLineNoThrow, as the primary leg's label is. Its
        # old fallback was the stderr-only Write-ChainDiagnostic, which is silent when the compiled writer is unavailable: a FAILED append of the label then lost the typed line,
        # and the result never carried the refusal. r5 (sol r4 B1): a TIMEOUT child's refusal is read too, so the TIMEOUT block below only reads the remote job's state and keeps
        # its SUPPLEMENTARY_REMOTE_JOB line for AFTER the result: no log append runs before the result records specRefusal, on either status.
        # Whatever the leg printed, a leg that died or timed out is INVALID evidence, never a measurement (the Bachelor rule); and nothing here is read as a timing verdict.
        $shownOutcome = $(if ($sStatus -ceq 'RAN') { $sOutcome } else { 'INVALID' })
        $extra = @{ quietVerdict = $sq.verdict; quietMean = $sq.mean; outcome = $shownOutcome; printedOutcome = $sOutcome; legExit = $sr.exit; timedOut = $sr.timedOut; receipt = $sReceipt; receiptRole = $sRole
            umManifestSha256 = $built.sha256; elapsedSec = [int]([DateTime]::UtcNow - $t0s).TotalSeconds; outcomeDetail = $sDetail }
        if ($sSpecRefusal) { $extra.specRefusal = ($sSpecRefusal -split ' ', 2)[0] }   # the typed code, as the primary result's specRefusal
        $rjLine = ''
        if ($sr.timedOut) {
            # The client was killed, the job it submitted was not: keep the job's identity (what the client printed before it went silent) and reconcile it BEFORE the venue is read as idle.
            # No id printed = nothing to look up = the venue is busy-or-unknown, never open. The claim is still released below; it is not the proof.
            $rjIds = $script:suppRemote.jobIds   # kept above, where the guard was armed at the timeout
            $rjs = Get-VenueRemoteJobState -AgentShare $share -JobId $rjIds -TimeoutSec 30
            $extra.remoteJobIds = $rjIds; $extra.remoteJobState = $rjs.state; $extra.venueIdle = ($rjs.state -ceq 'FINISHED')
            $rjLine = "SUPPLEMENTARY_REMOTE_JOB leg=$($Leg.name) jobId=$(Format-RemoteJobIds $rjIds) state=$($rjs.state) venueIdle=$($extra.venueIdle)$(if ($rjs.error) { " reason=$($rjs.error)" }) venue=$suppVenue role=supplementary (a released claim is not proof of idleness; later offers reconcile this first)"
            if ($rjs.state -ceq 'FINISHED') { $script:suppRemote = $null } else { $script:suppRemote.state = $rjs.state }
        }
        Add-SuppResult $Leg $Trigger $sStatus $extra
        if ($sSpecRefusal) { Write-ChainLineNoThrow "$sSpecRefusal venue=$suppVenue role=supplementary (the spec bytes are not the planned ones; the leg was refused before anything ran and is no measurement)" }   # r4: stdout, counted
        if ($rjLine) { L $rjLine }   # r5 (sol r4 B1): after the result and the typed refusal, never before them
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
    $legPreFF = $false; $legPreFFLine = ''; $legReFF = $false; $legReFFLine = ''   # r4: a FOREIGN_FLOOR on ANY probe of this leg sticks across GATE_BUSY retries of the same leg, never into the next
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
                while ([DateTime]::UtcNow -lt $until -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Seconds ([int][Math]::Max(1.0, [Math]::Min(60.0, ($until - [DateTime]::UtcNow).TotalSeconds))) }
                continue
            }
        }
        # (b) the quiet wait, WITHOUT the claim
        if ($ticket) { $pf = [string](Update-VenueTicket -Ticket $ticket -Phase 'probing' -StaleAfterSec $ticketProbeStaleSec); if ($pf -cne 'OK') { try { L "TICKET_PHASE_$pf ticket=$($ticket.name) phase=probing leg=$($leg.name) (the phase and its longer lifetime did not land; the probe goes on)" } catch { $null = $_ } } }   # r7 A15: a log line that cannot be written never stops the probe   # a ticket kept across a GATE_BUSY: held for this ONE probe
        $pre = Invoke-QuietProbe $leg.name 'pre-claim' $QuietMaxWaitSec
        if ($pre.foreignFloor) { $legPreFF = $true; $legPreFFLine = $pre.foreignFloorLine }
        L "QUIET leg=$($leg.name) phase=pre-claim exit=$($pre.code) verdict=$($pre.verdict) mean=$(Format-Mean $pre.mean) waitedSec=$($pre.waitedSec)$(if ($pre.reason) { " reason=$($pre.reason)" }) (no claim held)"
        if ($pre.verdict -ceq 'USAGE') { $stopReason = 'USAGE'; $exitCode = 2; Add-Result $leg 'NOT_RUN' @{ verdict = 'USAGE'; reason = 'wait-venuequiet-usage' }; L "LEG $($leg.name) NOT_RUN USAGE reason=wait-venuequiet-usage"; break }
        if ($pre.verdict -in @('GATE_UNREADABLE', 'HOST_MISMATCH')) { $stopReason = $pre.verdict; $exitCode = $(if ($pre.verdict -ceq 'GATE_UNREADABLE') { 4 } else { 5 }); Add-Result $leg 'NOT_RUN' @{ verdict = $pre.verdict; reason = 'phase=pre-claim' }; L "LEG $($leg.name) NOT_RUN $($pre.verdict) phase=pre-claim"; break }
        if ($pre.verdict -ceq 'VENUE_DRIVER_SCRIPT_MISSING') { Stop-DriverScriptMissing $leg $pre.reason 'pre-claim'; break }
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
            if ($req.note) { try { L "$($req.note) leg=$($leg.name)" } catch { $null = $_ } }   # r7 A15: best effort. Grok CoS G7: the flip to waiting stayed BUSY, the request came back QUEUED (tried again at the next poll)
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
            if ($ticket) { $pf = [string](Update-VenueTicket -Ticket $ticket -Phase 'probing' -StaleAfterSec $ticketProbeStaleSec); if ($pf -cne 'OK') { try { L "TICKET_PHASE_$pf ticket=$($ticket.name) phase=probing leg=$($leg.name) (the phase and its longer lifetime did not land; the probe goes on)" } catch { $null = $_ } } }   # r7 A15: a log line that cannot be written never stops the probe
            $re = Invoke-QuietProbe $leg.name 'under-claim' 0
            if ($re.foreignFloor) { $legReFF = $true; $legReFFLine = $re.foreignFloorLine }
            L "QUIET leg=$($leg.name) phase=under-claim exit=$($re.code) verdict=$($re.verdict) mean=$(Format-Mean $re.mean)$(if ($re.reason) { " reason=$($re.reason)" })"
            if ($re.verdict -ceq 'GATE_BUSY') {
                # BATCH-3 r3 (sol r2 B1 sibling): released once, marked released, then the line through Write-ChainLineNoThrow (a FAILED append here ended the chain before CHAIN_RESULT)
                $releaseResult = Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir; $released = $true; Write-ChainLineNoThrow "CLAIM_RELEASED leg=$($leg.name) result=$releaseResult reason=gate-busy-under-claim (back to waiting)"
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
            if ($re.verdict -ceq 'VENUE_DRIVER_SCRIPT_MISSING') { Stop-DriverScriptMissing $leg $re.reason 'under-claim'; break }
            $legMissing = Get-VenueDriverScriptMissing -Path @($LegScript)
            if ($legMissing) { Stop-DriverScriptMissing $leg ($legMissing -replace '^VENUE_DRIVER_SCRIPT_MISSING ', '') 'leg-launch'; break }
            if ($re.verdict -ceq 'VENUE_QUIET_UNKNOWN') {
                # (d1) the under-claim probe blocked in heavy-slot admission and ended not measured: same held offer as (a1), made AFTER Bachelor's claim is released (ultra-magnus is
                # offered while Bachelor is free, never while this chain holds it) and BEFORE -OnUnknown Stop sets stopReason. The leg never ran, so no after-leg offer follows.
                if ($suppEnabled -and ([DateTime]::UtcNow - $legWaitStart).TotalSeconds -ge $SupplementaryHeldSec) {
                    # BATCH-3 r3 (sol r2 B1 sibling): released once, marked released, then the line through Write-ChainLineNoThrow
                    $releaseResult = Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir; $released = $true; Write-ChainLineNoThrow "CLAIM_RELEASED leg=$($leg.name) result=$releaseResult reason=not-measured-under-claim (the held offer follows)"
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
            # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 (sol r6 INV-3 sibling): the leg runs the bytes the planner judged or nothing (Invoke-VenueLeg refuses DVE_LEG_SPEC_CHANGED before it
            # generates or submits anything); a path whose bytes changed between the plan and the leg's read is never measured in this leg's slot
            $lArgs += @('-ExpectSpecSha256', [string]$leg.specSha256)
            if ($SheetCopyDir) { $lArgs += @('-SheetCopyDir', $SheetCopyDir) }
            L "LEG $($leg.name) RUN verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) spec=$($leg.spec) backend=$(if ($leg.backend) { $leg.backend } else { 'spec' })"
            # r3 fix 2 / fix 3 (adversary pass 3 A2, pass 4 A1): the under-claim probe, its transcript write or the RUN line's append above can end after -DeadlineUtc (each can
            # stall on a share), so the deadline is asked again as the LAST step before the child starts: no leg starts at or past it.
            if ([DateTime]::UtcNow -ge $deadline) { $stopReason = 'DEADLINE'; $exitCode = 3; Add-Result $leg 'NOT_RUN' @{ verdict = 'DEADLINE'; reason = 'deadline-at-leg-launch' }; try { L "LEG $($leg.name) NOT_RUN DEADLINE reason=deadline-at-leg-launch (-DeadlineUtc passed before the child started)" } catch { Write-ChainDiagnostic "LEG $($leg.name) NOT_RUN DEADLINE reason=deadline-at-leg-launch" }; break }
            # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1 r3 (adversary pass 2 A1, A6): the leg child's output is CAPTURED (2>&1: its stdout lines are strings, its stderr lines
            # ErrorRecords), and every reading below (outcome, receipt path, detail, the spec refusal) is made from THIS child's own STDOUT. The RunDir-named file is shared by
            # every chain on this -RunDir (two venues' chains name a leg alike), so it is written after the leg as evidence only and never read back; a stderr line classifies nothing.
            # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-2 r1 (adversary pass 8 A3): the child is started by Invoke-VenueBoundedProcess with -StartNotAfterUtc $deadline, so a pause between
            # the check above and the start can no longer start it late: the helper asks the deadline at its own last clock read (notStarted) and against the child's OS StartTime
            # (lateStart: killed at once). The bound is on the START only (-TimeoutSec 86400): a leg that started in time is never killed at the deadline, as before. A pause inside
            # the helper between its check and Process.Start is the residual CARRIED to VENUE-LEG-CHILD-START-NOT-AFTER-GATE-1 (Guard: tools/profiling/dual-venue/Invoke-VenueLeg.ps1).
            $lp = Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $lArgs -TimeoutSec 86400 -StartNotAfterUtc $deadline
            if ($lp.notStarted -or $lp.lateStart) {
                $why = $(if ($lp.notStarted) { 'deadline-at-leg-launch' } else { 'deadline-at-child-start' })
                $killedIds = @(Get-VenueRemoteJobIds -Text ([string]$lp.stdout)) -join ','
                $stopReason = 'DEADLINE'; $exitCode = 3; Add-Result $leg 'NOT_RUN' @{ verdict = 'DEADLINE'; reason = $why }
                $dLine = "LEG $($leg.name) NOT_RUN DEADLINE reason=$why ($(if ($lp.notStarted) { '-DeadlineUtc passed before the child started' } else { "the OS started the child at or past -DeadlineUtc: it $(Format-VenueBoundedChildEnd $lp 86400)" }))$(if ($killedIds) { " jobIds=$killedIds" })"
                try { L $dLine } catch { Write-ChainDiagnostic $dLine }   # bounded, never throws: a failing log append costs neither the typed result nor exit 3
                break
            }
            # stdout alone classifies (r3); the transcript is stdout plus stderr, as the supplementary child's is
            $legTimedOut = [bool]$lp.timedOut
            $legCode = $(if ($legTimedOut) { $null } else { $lp.exit })
            $legLines = @(([string]$lp.stdout) -split "`r?`n")
            $ow = Write-ChainEvidence $out @([string]$lp.stdout + "`n--- stderr ---`n" + [string]$lp.stderr)   # bounded (r3 fix 2, A2): never holds the claim's release
            # r3 fix 3 (adversary pass 4 A2): the write's failure is said on STDERR only: a chain-log append could stall on the very share that stalled the write, before the
            # leg is classified and its claim released
            if ($ow -cne 'OK') { Write-ChainDiagnostic "LEG_OUT_WRITE_$ow leg=$($leg.name) path=$out (evidence only: the leg is classified from its own captured output)" }   # r4 fix 2 (pass 6 A6): bounded
            $o = $legLines | Where-Object { $_ -match '^DVE_OUTCOME=(\S+)' } | Select-Object -Last 1
            $outcome = $(if ($o -and $o -match '^DVE_OUTCOME=(\S+)') { $Matches[1] } else { 'NONE' })
            $p = $legLines | Where-Object { $_ -match '^DVE_RECEIPT_PATH=(.+)$' } | Select-Object -Last 1
            $receipt = $(if ($p -and $p -match '^DVE_RECEIPT_PATH=(.+)$') { $Matches[1].Trim() } else { $null })
            $dl = $legLines | Where-Object { $_ -match '^DVE_DETAIL=' } | Select-Object -Last 1
            $detail = $(if ($dl) { ($dl -replace '^DVE_DETAIL=', '').Trim() } else { '' })
            if ($legTimedOut -or $legCode -ne 0) {
                # Invoke-VenueLeg exits 0 whenever it wrote a receipt (any outcome, INVALID included); non-zero means it died without one. Whatever
                # DVE_OUTCOME it printed before dying is not evidence: the leg is LEG_FAILED / INVALID, never RAN, and the chain exits 7. It goes on to the next leg.
                Add-Result $leg 'LEG_FAILED' @{ verdict = $re.verdict; quietMean = $re.mean; outcome = 'INVALID'; printedOutcome = $outcome; legExit = $legCode; receipt = $null }
                # DV-PLANNED-SPEC-HASH-BIND-CHAIN-BATCH-1: a leg that refused other bytes than the planned ones (or could not read the planned spec) is named on its own typed line and in its
                # result (specRefusal). It stays LEG_FAILED / INVALID, exit 7, so the label never turns a leg into a measurement either way. The record is read from this child's
                # own STDOUT only (r3), from a leg that wrote no receipt line, as a whole anchored line naming this leg's planned hash.
                # (r2, adversary A5: the exit code is set BEFORE the label, and the label's log write may fail without costing the result, CHAIN_RESULT or the next leg)
                # BATCH-3 r3 (sol r2 B1): and BEFORE the LEG_FAILED line too, with specRefusal: that append ran first and threw on a FAILED write, so exit stayed 0, specRefusal was
                # never recorded and CHAIN_RESULT never went out. Both lines go through Write-ChainLineNoThrow (said on stderr, counted, still on stdout), so neither decides the result.
                if ($exitCode -eq 0) { $exitCode = 7 }
                $specRefusal = $(if (-not $receipt) { Get-LegSpecRefusalLine $legLines $leg } else { '' })
                if ($specRefusal) { $results[$results.Count - 1] | Add-Member -NotePropertyName specRefusal -NotePropertyValue (($specRefusal -split ' ', 2)[0]) -Force }
                if ($legTimedOut) { Write-ChainLineNoThrow "LEG $($leg.name) LEG_FAILED TIMEOUT outcome=INVALID printedOutcome=$outcome verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) (the leg $(Format-VenueBoundedChildEnd $lp 86400): no usable receipt)" }
                else { Write-ChainLineNoThrow "LEG $($leg.name) LEG_FAILED exit=$legCode outcome=INVALID printedOutcome=$outcome verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) (the leg exited non-zero: no usable receipt)" }
                if ($specRefusal) { Write-ChainLineNoThrow "$specRefusal (the spec bytes are not the planned ones; the leg was refused before anything ran and is no measurement)" }   # b2 r1 (pass 1 A4): bounded
            } else {
                $preLoad = Get-LegPreLoadMean $receipt
                # a FOREIGN_FLOOR early exit in ANY probe of the leg (either phase, any attempt, GATE_BUSY retries included) leaves it contended: a later QUIET reading never promotes it to a counted class
                $class = $(if ($re.verdict -ceq 'COOLDOWN_UNMET' -or $legPreFF -or $legReFF) { 'COOLDOWN_UNMET' } elseif ($null -eq $preLoad) { 'QUIET_PRELOAD_UNKNOWN' } elseif ($preLoad -le 30.0) { 'QUIET_PRELOAD_OK' } else { 'QUIET_PRELOAD_HIGH' })
                $ranExtra = @{ verdict = $re.verdict; quietMean = $re.mean; preClaimVerdict = $pre.verdict; preClaimMean = $pre.mean; preLoadMean = $preLoad; class = $class; preClaimForeignFloor = $legPreFF; underClaimForeignFloor = $legReFF; outcome = $outcome; legExit = $legCode; receipt = $receipt }
                if ($legPreFF) { $ranExtra['preClaimForeignFloorLine'] = $legPreFFLine }
                if ($legReFF) { $ranExtra['underClaimForeignFloorLine'] = $legReFFLine }
                if ($detail) { $ranExtra['outcomeDetail'] = $detail }
                Add-Result $leg 'RAN' $ranExtra
                L "LEG $($leg.name) RAN exit=$legCode outcome=$outcome verdict=$($re.verdict) quietMean=$(Format-Mean $re.mean) preLoadMean=$(Format-Mean $preLoad) class=$class receipt=$receipt$(if ($legPreFF) { ' preClaimForeignFloor=1' })$(if ($legReFF) { ' underClaimForeignFloor=1' })$(if ($detail) { " detail=$detail" })"
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
            # BATCH-3 r3 (sol r2 B1): the claim is released ONCE, then its line goes through Write-ChainLineNoThrow: a FAILED append here threw out of the finally and ended the chain before CHAIN_RESULT
            if (-not $released) { $releaseResult = Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir; Write-ChainLineNoThrow "CLAIM_RELEASED leg=$($leg.name) result=$releaseResult" }
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
if ($script:ChainLogAppendFailures -gt 0) { $summary['chainLogAppendFailures'] = $script:ChainLogAppendFailures }   # BATCH-3 r3 (sol r2 B1): lines whose chain-log append failed (each also on stdout and stderr)
$resultLine = 'CHAIN_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
# b2 r2 (adversary pass 3 A4 sibling): the typed result goes out FIRST, and its chain-log copy is the bounded append (a stalled ChainLog never holds CHAIN_RESULT)
Write-Output $resultLine
$rw = Add-ChainLogLine $resultLine
if ($rw -cne 'OK') { Write-ChainDiagnostic "CHAIN_LOG_APPEND_$($rw -replace '\s+', ' ') path=$ChainLog line=CHAIN_RESULT (the result is on stdout)" }
Complete-VenueProcess -ExitCode $exitCode   # a share read abandoned at its timeout would otherwise keep this process alive until it ends
exit $exitCode
