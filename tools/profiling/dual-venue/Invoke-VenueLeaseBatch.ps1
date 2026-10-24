# Invoke-VenueLeaseBatch.ps1 -- run Bachelor bench legs INSIDE one quiet lease per batch (BACHELOR-LEASE-BATCH-LEGS-1). RUN THIS ON THE VM.
#
# WHY. The Bachelor agent takes a heavy-slot.py quiet lease for every gated job. heavy-slot.py is Bachelor's, not ours (45 min max, 30 min pending, 30 min
# post-lease cooldown, <= 50 % of any 3 h), and it refuses a new lease for 30 min after the last one ends. With one lease per leg, and the venue quiet probe
# run as a light job OUTSIDE any lease (where it reads the open desktop), a leg that played about 1 min cost about 34 min (5 of 32 legs on 2026-10-23).
# WHAT. One lease per BATCH, requested FIRST. The probe and the legs then run under it:
#   1. CLAIM. The venue claim (VenueChain.psm1, ticketless) is taken once per batch and held until the batch ends.
#   2. OPEN (lease first). A trivial gated job lease-batch-open-<stamp>-lb<token> goes through um-run.ps1. The agent requests the lease for it (LeaseMaxMinutes)
#      and, when the job ends, PARKS the lease for the batch (ultra-magnus-agent.ps1, lease-batch block). Its result's admission.leaseBatch names the lease.
#      An agent that returns none is not batch-capable (exit 10), and nothing else is submitted.
#   3. PER LEG, while the lease has room for it (time left >= -LegBudgetSec + -LeaseMarginSec; the budget grows to the longest leg seen in the run):
#      a. the in-lease quiet probe: Wait-VenueQuiet.ps1, unchanged. Its probe job is light, so the agent runs it UNDER the parked lease. Threshold
#         -ThresholdPercent (20), re-probing every -RecheckSec for as long as the lease has room;
#      b. QUIET -> the leg: Invoke-VenueLeg.ps1 -LeaseBatch <token>. The agent runs its job on the parked lease. The leg's own <= 20 % check at its start is
#         unchanged. COOLDOWN_UNMET, GATE_BUSY, or a leg that ended VENUE_NOT_QUIESCENT (its own re-check failed) -> RETRY INSIDE THE SAME LEASE, back to (a).
#         The lease is never released and re-requested on a busy desktop. The lease's own end bounds the retries;
#      c. the leg's agent result is read BY NAME (outbox\<jobId>.result.json). The leg counts only if it ran on THIS batch's lease (admission.leaseBatch
#         .leaseId) and kept it (slotContinuity). Class: QUIET_PRELOAD_OK (counted) when its own pre-load mean is <= 30 %; otherwise QUIET_PRELOAD_HIGH,
#         QUIET_PRELOAD_UNKNOWN or LEASE_LOST (uncounted).
#   4. END. A batch ends when its legs are done (ALL_LEGS_DONE), the next leg would outlive the lease (LEASE_REMAINING_SHORT: the rest go to the next batch; also
#      a busy probe with less room left after the leg's budget than one -RecheckSec), the lease was lost (LEASE_LOST), demoted (LEASE_DEMOTED / LEASE_RENEW_UNKNOWN, 6e)
#      or ended under a leg (LEASE_EXPIRED, 7b), the deadline passed (DEADLINE), or a probe could not measure (PROBE_<verdict>: the run stops). An open still
#      unanswered is cancelled and reconciled first (7a). The light job
#      lease-batch-close-<stamp>-lb<token> then releases the lease at once (the agent's idle bound would, later). The agent's own batch receipt is read by
#      name (logs\lease-batch-<token>.jsonl), and the claim is released.
#   5. RECEIPTS. <RunDir>\batch-<nn>.json and one line per batch in <RunDir>\batches.jsonl (mlv-app/venue-lease-batch/v1): token, lease id, held, start and
#      end, the open's wait and refusals, legs attempted / ran / counted, retries (probe and leg re-check), the end reason, the per-leg rows and the agent's
#      receipt. <RunDir>\batch.log has one line per event, UTC stamp first; the last stdout line is LEASE_BATCH_RESULT=<json>.
#   6. CAPS (r2; Bachelor's heavy-slot.py, read from heavy-slot.lease.json, CAPS-ADDENDUM 2026-10-23; each one binding):
#      a. 45 min max lease: the agent asks for LeaseMaxMinutes (<= 45) and a leg is started only while the lease has room for it, and that room never runs past
#         -LeaseCapSec (2700) after the open was read, nor past the 3 h budget left (c).
#      b. 30 min cooldown: before each open the run waits until -LeaseCooldownSec (1800) after the end of the last lease it (or any earlier run on this host)
#         took, WITHOUT the venue claim (BATCH_WAIT reason=COOLDOWN, the batch's preOpenWait).
#      c. <= 50 % of any 3 h: every lease a lease-batch run takes is appended to <ClaimDir>\<venue>.lease-ledger.jsonl (VenueChain.psm1), which a run reads
#         ONCE, at its start (a lease another run takes meanwhile reaches this one only as heavy-slot.py's exit 75, d). An open waits until the 3 h window ending
#         when a full-length lease opened then would end holds at most -LeaseBudgetSec - -LeaseCapSec (Get-VenueLeaseBudgetWait; reason=BUDGET), so a day of
#         batches is paced to the budget instead of being refused. A wait that would pass the deadline ends the run DEADLINE (exit 3), nothing submitted.
#      d. exit 75 = wait and retry, NOT a failure: the agent re-asks a refused (75) request every LeaseRetrySec; the batch receipt types its refusals
#         (exit75Refusals, exit75Class WAIT_RETRY). An open that um-run gave up on while the agent still held it in its admission gate is NOT resubmitted
#         and NOT a failure: its result is read by name until the deadline (openOutcome RECEIPT_BY_NAME, openLeaseWait EXIT_75_WAIT).
#      e. renew about every 60 s; a gap over 120 s demotes the lease: the agent renews every LeaseRenewSec (<= 60), measures the longest gap between
#         SUCCESSFUL renews (okMaxGapSec) and releases a demoted parked lease. A leg counts only when its result's admission.leaseRenew.okMaxGapSec is
#         <= -RenewGapMaxSec (120): otherwise it is LEASE_DEMOTED (or LEASE_RENEW_UNKNOWN when absent), uncounted, and the batch ends; the rest go to the next.
#      f. max pending 30 min: the agent's --admit-timeout is LeasePendingMaxSec (<= 1800); a request past it is abandoned and asked again (d).
#      Foreign holders' leased time is not in the ledger: heavy-slot.py still refuses (75) on it, and (d) waits that out.
#   7. r3 (sol r2 blockers):
#      a. NO LEASE AFTER THE RUNNER LETS GO. An open with no result in hand at the END of its batch (um-run gave up, the deadline came while its result was
#         awaited, the batch threw) may still be in the agent's gate, re-asking for a lease. The runner writes its cancel file (Request-VenueJobCancel; the agent
#         reads it at the top of every gate loop, before any request), then reads its result BY NAME for up to -CancelReconcileSec, and only then closes the
#         lease and releases the claim: openCancel CANCELLED (no lease), RAN_BEFORE_CANCEL (its lease is this batch's: the close releases it), ENDED, or
#         UNRECONCILED (exit 11: the cancel file stays, and the ledger counts a full lease for it). The agent also ends a batch job's gate wait once that batch's
#         close job is queued (lease-batch r3).
#      b. NO LEG PAST THE LEASE'S END. A leg runs as a bounded child (Invoke-VenueBoundedProcess) whose bound is the lease's end: still running then, it is
#         killed with its whole process tree, its agent job is asked to cancel, and it is LEASE_EXPIRED (outcome INVALID, uncounted, never re-run; exit 7).
#         The agent stops a batch job at the same bound, or on a demoted or lost lease (admission.leaseStop), which types it LEASE_EXPIRED / LEASE_DEMOTED /
#         LEASE_LOST. -LegBudgetSec only decides whether a leg STARTS; it never lets one run past the lease.
# It changes nothing on Bachelor: not heavy-slot.py's caps, not its lease file, not the quiet threshold. A batch run needs the lease-batch agent deployed.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeaseBatch.ps1 -LegSpec a.json,b.json -Backend cpu -RunDir <rd> `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> [-DeadlineUtc <iso>]
#
# Exit codes (a leg's OUTCOME is never the exit code: read LEASE_BATCH_RESULT and the receipts): 0 every leg ran; 2 usage (or a leg budget no lease can hold);
# 3 DEADLINE before every leg ran (a cooldown, budget or exit-75 wait that reached it included); 4 GATE_UNREADABLE and 5 HOST_MISMATCH from Wait-VenueQuiet; 6 a probe ended VENUE_QUIET_UNKNOWN; 7 a leg process exited
# non-zero (LEG_FAILED) or was killed at the lease's end (LEASE_EXPIRED; the run goes on); 9 a batch lease could not be opened (the open job was RETRACTED,
# UNRESOLVED or not admitted); 10 the agent is not batch-capable; 11 an open left in the agent's gate was cancelled but never reconciled (OPEN_CANCEL_UNRECONCILED,
# 7a). A stopping code (2-6, 9, 10) replaces 0 or 7; 11 replaces every other code.
# -QuietScript / -LegScript / -UmRunScript / -AgentShare / -ClaimDir are seams for the offline tests (tools/repo_hygiene/test_venue_lease_batch.py).
[CmdletBinding()]
param(
    [ValidateSet('bachelor')][string]$Venue = 'bachelor',
    [string[]]$LegSpec = @(),
    [string]$LegSet = '',
    [ValidateRange(1, 20)][int]$Repeats = 1,
    [ValidateSet('cuda', 'cpu')][string]$Backend,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256,
    [string]$Purpose = '',
    [string]$SheetCopyDir = '',
    # passed through to Wait-VenueQuiet.ps1 (never relaxed here)
    [double]$ThresholdPercent = 20.0,
    [ValidateRange(1, 3600)][int]$RecheckSec = 90,
    # one in-lease probe waits at most this long (and never past the room the lease has left for the leg)
    [ValidateRange(0, 3600)][int]$ProbeMaxWaitSec = 600,
    [ValidateRange(60, 2700)][int]$LegBudgetSec = 360,
    [ValidateRange(0, 900)][int]$LeaseMarginSec = 120,
    [ValidateRange(0, 3600)][int]$CooldownSec = 0,
    [ValidateRange(60, 86400)][int]$OpenAdmissionWaitSec = 7200,
    [string]$DeadlineUtc = '',
    # Bachelor's heavy-slot.py caps (CAPS-ADDENDUM 2026-10-23), binding (see CAPS above). Each can only be TIGHTENED: the ranges stop a longer lease, a bigger budget or a
    # longer renew gap, and a shorter cooldown or window is refused unless -UmRunScript is an offline stub (LEASE_CAP_RELAXED).
    [ValidateRange(60, 2700)][int]$LeaseCapSec = 2700,
    [ValidateRange(0, 7200)][int]$LeaseCooldownSec = 1800,
    [ValidateRange(60, 5400)][int]$LeaseBudgetSec = 5400,
    [ValidateRange(60, 86400)][int]$LeaseBudgetWindowSec = 10800,
    [ValidateRange(1, 120)][int]$RenewGapMaxSec = 120,
    [ValidateRange(60, 86400)][int]$ClaimTtlSec = 10800,
    [ValidateRange(1, 3600)][int]$ClaimPollSec = 30,
    # r3: how long the end of a batch reads a cancelled open's result by name before it types it UNRECONCILED (exit 11)
    [ValidateRange(1, 3600)][int]$CancelReconcileSec = 300,
    [string]$ClaimDir = '',
    [string]$QuietScript = '',
    [string]$LegScript = '',
    [string]$UmRunScript = '',
    [string]$AgentShare = ''
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking

function Stop-Usage([string]$Message) { [Console]::Error.WriteLine("BATCH_USAGE $Message"); exit 2 }

# `pwsh -File` passes an array argument as ONE string, so -LegSpec a.json,b.json is split on commas (an entry that is itself an existing path is kept whole;
# PWSH-FILE-ARRAY-PASSTHRU-1, test_pwsh_string_array_file_params.py).
$LegSpec = @($LegSpec | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $QuietScript) { $QuietScript = Join-Path $here 'Wait-VenueQuiet.ps1' }
if (-not $LegScript) { $LegScript = Join-Path $here 'Invoke-VenueLeg.ps1' }
if (-not $UmRunScript) { $UmRunScript = Join-Path $here '..\um-run.ps1' }
foreach ($s in $QuietScript, $LegScript, $UmRunScript) { if (-not (Test-Path -LiteralPath $s -PathType Leaf)) { Stop-Usage "no script at '$s'" } }
# The caps hold against the real um-run.ps1 (by path or by content): only an offline stub may shorten the cooldown or the budget window.
$realUmRun = [IO.Path]::GetFullPath((Join-Path $here '..\um-run.ps1'))
$umRunIsReal = ([IO.Path]::GetFullPath($UmRunScript) -ieq $realUmRun) -or ((Test-Path -LiteralPath $realUmRun -PathType Leaf) -and (Get-FileHash -LiteralPath $UmRunScript).Hash -ceq (Get-FileHash -LiteralPath $realUmRun).Hash)
if ($umRunIsReal -and ($LeaseCooldownSec -lt 1800 -or $LeaseBudgetWindowSec -lt 10800)) {
    Stop-Usage "LEASE_CAP_RELAXED -LeaseCooldownSec $LeaseCooldownSec / -LeaseBudgetWindowSec $LeaseBudgetWindowSec are looser than Bachelor's caps (1800 s cooldown, 10800 s window)"
}
if ($LeaseBudgetSec -lt $LeaseCapSec) { Stop-Usage "-LeaseBudgetSec $LeaseBudgetSec is under -LeaseCapSec ${LeaseCapSec}: no lease could ever open" }
if (($LegSpec.Count -gt 0) -eq [bool]$LegSet) { Stop-Usage 'name the legs with exactly one of -LegSpec <path>... or -LegSet <name>' }
if (-not $AgentShare) { $AgentShare = try { [string](([IO.File]::ReadAllText((Join-Path $here 'venues.json')) | ConvertFrom-Json).venues.$Venue.agentShare) } catch { '' } }
if (-not $AgentShare) { Stop-Usage "venues.json names no agentShare for $Venue" }
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$RunDir = (Resolve-Path -LiteralPath $RunDir).Path
$jobDir = Join-Path $RunDir 'batch-jobs'
New-Item -ItemType Directory -Force -Path $jobDir | Out-Null
$batchLog = Join-Path $RunDir 'batch.log'
$t0 = [DateTime]::UtcNow
$deadline = $(if ($DeadlineUtc) { [DateTimeOffset]::Parse($DeadlineUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime } else { $t0.AddHours(6) })
$pwshExe = [Environment]::ProcessPath
if (-not $Purpose) { $Purpose = "Invoke-VenueLeaseBatch $(Split-Path -Leaf $RunDir)" }

function L([string]$Message) {
    $line = "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message"
    Add-Content -LiteralPath $batchLog -Value $line
    Write-Output $line
}

# The planned legs: name, spec path, backend ('' = whatever the spec lists). Same naming as Invoke-VenueChain.
$plan = [System.Collections.Generic.List[object]]::new()
if ($LegSet) {
    Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force
    $sp = try { Get-DvLegSetPlan -LegSetPath (Join-Path $here "legsets\$LegSet.json") -Repeats $Repeats -Backend $(if ($Backend) { $Backend } else { '' }) } catch { Stop-Usage $_.Exception.Message }
    foreach ($e in $sp.plan) { $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f $e.seq, $e.legId, $e.backend); spec = $e.specPath; backend = $e.backend }) }
} else {
    foreach ($ref in $LegSpec) {
        $path = $(if (Test-Path -LiteralPath $ref -PathType Leaf) { $ref } elseif (Test-Path -LiteralPath (Join-Path $here "legs\$ref") -PathType Leaf) { Join-Path $here "legs\$ref" } else { Stop-Usage "no leg spec at '$ref'" })
        $path = (Resolve-Path -LiteralPath $path).Path
        $legId = try { [string](([IO.File]::ReadAllText($path) | ConvertFrom-Json).legId) } catch { '' }
        if ($legId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$') { $legId = [IO.Path]::GetFileNameWithoutExtension($path) }
        for ($r = 1; $r -le $Repeats; $r++) {
            $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f ($plan.Count + 1), $legId, $(if ($Backend) { $Backend } else { 'spec' })); spec = $path; backend = $(if ($Backend) { $Backend } else { '' }) })
        }
    }
}

function Format-Mean($m) { if ($null -eq $m) { 'UNKNOWN' } else { '{0:0.0}' -f $m } }

function Invoke-QuietProbe([string]$Name, [string]$Phase, [int]$MaxWaitSec) {
    # One Wait-VenueQuiet run (its probe job is light: the agent runs it under the parked batch lease). verdict: QUIET | COOLDOWN_UNMET | GATE_BUSY |
    # GATE_UNREADABLE | HOST_MISMATCH | USAGE | VENUE_QUIET_UNKNOWN (not measured). The same exit-code reading as Invoke-VenueChain's probe.
    $out = Join-Path $RunDir "quiet-$Name-$Phase.out.txt"
    if (-not (Test-Path -LiteralPath $QuietScript -PathType Leaf)) { return [pscustomobject]@{ verdict = 'VENUE_QUIET_UNKNOWN'; code = 'none'; mean = $null; reason = 'PROBE_SCRIPT_MISSING'; foreignFloor = $false } }
    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript, '-Venue', $Venue, '-WorkDir', (Join-Path $RunDir 'quiet'), '-GateLog', (Join-Path $RunDir 'gate.log'),
        '-ThresholdPercent', [string]$ThresholdPercent, '-MaxWaitSec', [string]$MaxWaitSec, '-RecheckSec', [string]$RecheckSec, '-MaxAdmissionWaitSec', '120')
    & $pwshExe @qArgs *> $out
    $code = $LASTEXITCODE
    $lines = @(if (Test-Path -LiteralPath $out) { Get-Content -LiteralPath $out })
    $v = $lines | Where-Object { $_ -match '^(QUIET|COOLDOWN_UNMET) mean=' } | Select-Object -Last 1
    $u = $lines | Where-Object { $_ -like 'VENUE_QUIET_UNKNOWN *' } | Select-Object -Last 1
    $ff = $lines | Where-Object { $_ -match '^FOREIGN_FLOOR ' } | Select-Object -Last 1
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
    [pscustomobject]@{ verdict = $verdict; code = $code; mean = $mean; reason = $reason; foreignFloor = [bool]$ff }
}

function Submit-AgentJob([string]$JobId, [string]$Body, [int]$TimeoutSec, [int]$WaitSec) {
    # One job through um-run.ps1 (the only writer to a venue share). RECEIPT | RETRACTED | UNRESOLVED | ERROR; a thrown RETRACTED / UNRESOLVED is data here.
    $f = Join-Path $jobDir "$JobId.job.ps1"
    [IO.File]::WriteAllText($f, $Body + "`r`n", [Text.Encoding]::ASCII)
    try {
        $o = @(& $UmRunScript -ScriptPath $f -JobId $JobId -AgentShare $AgentShare -TimeoutSec $TimeoutSec -MaxQueueWaitSec $WaitSec -MaxClaimedWaitSec 300 -MaxAdmissionWaitSec $WaitSec 6>$null)
        [pscustomobject]@{ outcome = 'RECEIPT'; result = $(if ($o.Count -gt 0) { $o[-1] } else { $null }); message = $null }
    } catch {
        $m = [string]$_.Exception.Message
        [pscustomobject]@{ outcome = $(if ($m -match '^RETRACTED:') { 'RETRACTED' } elseif ($m -match '^UNRESOLVED:') { 'UNRESOLVED' } else { 'ERROR' }); result = $null; message = ($m -replace '\s+', ' ') }
    }
}

function Get-LeaseEndUtc($Result) {
    # When the batch lease ends, on THIS clock: the agent's own until= minus its own endedUtc (one clock, no skew), added to now (when the result was read,
    # which is no earlier than when the job ended). $null when the result carries no batch lease.
    try {
        $lb = $Result.admission.leaseBatch
        if ($null -eq $lb -or $null -eq $lb.untilEpoch -or -not $Result.endedUtc) { return $null }
        $ended = [DateTimeOffset]::Parse([string]$Result.endedUtc, [Globalization.CultureInfo]::InvariantCulture).ToUnixTimeMilliseconds() / 1000.0
        [DateTime]::UtcNow.AddSeconds([double]$lb.untilEpoch - $ended)
    } catch { $null }
}

$pending = [System.Collections.Generic.List[object]]::new()
foreach ($p in $plan) { $pending.Add($p) }
$results = [System.Collections.Generic.List[object]]::new()
$batches = [System.Collections.Generic.List[object]]::new()
$exitCode = 0
$stopReason = $null
$legBudget = $LegBudgetSec
$batchNo = 0
function Write-BatchReceipt([System.Collections.IDictionary]$B) {
    $B.endedUtc = [DateTime]::UtcNow.ToString('o')
    $rec = [pscustomobject]$B
    [IO.File]::WriteAllText((Join-Path $RunDir ('batch-{0:00}.json' -f $B.batch)), ($rec | ConvertTo-Json -Depth 8) + "`n", [Text.UTF8Encoding]::new($false))
    Add-Content -LiteralPath (Join-Path $RunDir 'batches.jsonl') -Value ($rec | ConvertTo-Json -Compress -Depth 8)
    $batches.Add($rec)
}
# Every lease a lease-batch run on this host took on this venue in the last budget window plus cooldown, read once at the run's start (CAPS b and c).
$leases = [System.Collections.Generic.List[object]]::new()
foreach ($l in @(Read-VenueLeaseLedger -Venue $Venue -ClaimDir $ClaimDir -SinceUtc $t0.AddSeconds(-($LeaseBudgetWindowSec + $LeaseCooldownSec)))) { $leases.Add($l) }
function Set-Stop([string]$Reason, [int]$Code) { $script:stopReason = $Reason; if ($script:exitCode -eq 0 -or $script:exitCode -eq 7) { $script:exitCode = $Code } }
function Add-Result([object]$Leg, [string]$Status, [hashtable]$Extra) {
    $r = [ordered]@{ name = $Leg.name; spec = $Leg.spec; backend = $Leg.backend; status = $Status }
    foreach ($k in $Extra.Keys) { $r[$k] = $Extra[$k] }
    $results.Add([pscustomobject]$r)
}

L "BATCH_RUN_START venue=$Venue legs=$($plan.Count) runDir=$RunDir agentShare=$AgentShare deadline=$($deadline.ToString('o')) threshold=$ThresholdPercent legBudgetSec=$LegBudgetSec leaseMarginSec=$LeaseMarginSec recheckSec=$RecheckSec cooldownSec=$CooldownSec pid=$PID quietScript=$QuietScript legScript=$LegScript umRunScript=$UmRunScript"

while ($pending.Count -gt 0 -and $null -eq $stopReason) {
    if ([DateTime]::UtcNow -ge $deadline) { Set-Stop 'DEADLINE' 3; break }
    $batchNo++
    $token = [guid]::NewGuid().ToString('N').Substring(0, 12)
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $b = [ordered]@{
        schema = 'mlv-app/venue-lease-batch/v1'; batch = $batchNo; token = $token; venue = $Venue; runDir = $RunDir; startedUtc = [DateTime]::UtcNow.ToString('o')
        claimNonce = $null; openJobId = $null; openOutcome = $null; leaseId = $null; held = $null; slot = $null; leaseMinutes = $null; leaseUntilEpoch = $null
        openWaitedSec = $null; openRequests = $null; refusals = @(); leaseEndUtc = $null
        legsAttempted = 0; legsRan = 0; legsCounted = 0; retries = 0; probeRetries = 0; legRetries = 0
        endReason = $null; endDetail = $null; legs = [System.Collections.Generic.List[object]]::new(); closeJobId = $null; closeOutcome = $null; agent = $null; endedUtc = $null
        preOpenWait = $null; openLeaseWait = 'NONE'; exit75Refusals = 0; exit75Class = 'NONE'; renewOkMaxGapSec = $null; leaseEndClamp = $null; ledger = $null
        openExit75Refusals = 0; legExit75Refusals = 0; openCancel = $null
    }
    # (0) CAPS b and c: wait out the cooldown after the last lease and the 3 h budget BEFORE the claim, so the venue is never held through either.
    $bw = Get-VenueLeaseBudgetWait -Leases @($leases) -NowUtc ([DateTime]::UtcNow) -CapSec $LeaseCapSec -CooldownSec $LeaseCooldownSec -WindowSec $LeaseBudgetWindowSec -BudgetSec $LeaseBudgetSec
    $b.preOpenWait = [ordered]@{ reason = $bw.reason; waitSec = $bw.waitSec; usedSec = $bw.usedSec; budgetRoomSec = $bw.budgetRoomSec; openUtc = $bw.openUtc.ToString('o'); lastLeaseEndUtc = $(if ($bw.lastEndUtc) { $bw.lastEndUtc.ToString('o') }) }
    if ($bw.waitSec -gt 0) {
        if ($bw.openUtc -ge $deadline) {
            $b.endReason = 'DEADLINE'; $b.endDetail = "lease-$($bw.reason.ToLowerInvariant())-wait: the next lease may open at $($bw.openUtc.ToString('o')) (usedSec=$($bw.usedSec) in the 3 h window), after the deadline"
            Set-Stop 'DEADLINE' 3; Write-BatchReceipt $b
            L "BATCH_END batch=$batchNo token=$token reason=DEADLINE detail=$($b.endDetail)"
            break
        }
        L "BATCH_WAIT batch=$batchNo reason=$($bw.reason) waitSec=$($bw.waitSec) usedSec=$($bw.usedSec) capSec=$LeaseCapSec budgetSec=$LeaseBudgetSec windowSec=$LeaseBudgetWindowSec cooldownSec=$LeaseCooldownSec (Bachelor's lease caps; not a failure, no claim held)"
        Start-Sleep -Seconds $bw.waitSec
    }
    # (1) the claim, once per batch. A LIVE claim of another chain is waited out; stale claims are broken once each.
    $claim = $null; $lastHolder = ''
    while ($true) {
        $req = Request-VenueClaim -Venue $Venue -RunDir $RunDir -Purpose "$Purpose batch $batchNo" -TtlSec $ClaimTtlSec -ClaimDir $ClaimDir
        foreach ($bk in $req.broken) { L "CLAIM_STALE_BROKEN venue=$Venue reason=$($bk.reason) ownerPid=$($bk.claim.ownerPid) runDir=$($bk.claim.runDir) expiresUtc=$($bk.claim.expiresUtc)" }
        if ($req.granted) { $claim = $req.claim; break }
        $holder = $(if ($req.claim) { "pid=$($req.claim.ownerPid) runDir=$($req.claim.runDir) purpose=$($req.claim.purpose)" } else { $req.state })
        if ($holder -cne $lastHolder) { L "CLAIM_HELD batch=$batchNo state=$($req.state) holder $holder -> wait"; $lastHolder = $holder }
        if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -ge $deadline) { break }
        Start-Sleep -Seconds $ClaimPollSec
    }
    if ($null -eq $claim) {
        $b.endReason = 'DEADLINE'; $b.endDetail = 'claim-held'; Set-Stop 'DEADLINE' 3
        Write-BatchReceipt $b
        L "BATCH_END batch=$batchNo token=$token reason=DEADLINE detail=claim-held"
        break
    }
    $b.claimNonce = $claim.nonce
    L "CLAIM_TAKEN batch=$batchNo nonce=$($claim.nonce) expiresUtc=$($claim.expiresUtc)"
    $op = $null; $leaseStartUtc = $null; $openSubmitUtc = $null
    try {
        # (2) OPEN: the lease first. Nothing is probed or run before the agent reports this batch's lease active.
        $b.openJobId = "lease-batch-open-$stamp-lb$token"
        $wait = [int][Math]::Max(60, [Math]::Min($OpenAdmissionWaitSec, ($deadline - [DateTime]::UtcNow).TotalSeconds))
        L "BATCH_OPEN_SUBMIT batch=$batchNo token=$token job=$($b.openJobId) admissionWaitSec=$wait (the agent requests the lease; heavy-slot.py's cooldown is waited out there)"
        $openSubmitUtc = [DateTime]::UtcNow
        $op = Submit-AgentJob $b.openJobId "Write-Output ('LEASE_BATCH_OPEN token=$token held=' + [string]`$env:HEAVY_SLOT_HELD + ' admission=' + [string]`$env:MLV_AGENT_ADMISSION)" 120 $wait
        if ($op.outcome -ceq 'UNRESOLVED' -and $op.message -match 'admission gate') {
            # CAPS d: um-run stopped while the agent still holds the open in its gate, re-asking a refused (exit 75) request. That is a wait, not a failure:
            # the open is never resubmitted (a second open would queue behind it); its result is read by name until the deadline.
            $b.openLeaseWait = 'EXIT_75_WAIT'
            L "BATCH_OPEN_STILL_WAITING batch=$batchNo job=$($b.openJobId) (held in the agent's admission gate: exit 75 is wait-and-retry; reading its result by name until $($deadline.ToString('o')))"
            while ($true) {
                $jr = Get-VenueJobResult -AgentShare $AgentShare -JobId $b.openJobId
                if ($jr.state -ceq 'FOUND') { $op = [pscustomobject]@{ outcome = 'RECEIPT_BY_NAME'; result = $jr.result; message = $null }; break }
                if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -ge $deadline) { break }
                Start-Sleep -Seconds $ClaimPollSec
            }
        }
        $adm = $(if ($null -ne $op.result) { $op.result.admission } else { $null })
        $b.openOutcome = $op.outcome
        $b.openExit75Refusals = $(if ($null -ne $adm) { @(@($adm.leaseRefusals) | Where-Object { $null -ne $_ -and [string]$_.rc -eq '75' }).Count } else { 0 })
        $b.exit75Refusals = $b.openExit75Refusals
        if ($b.exit75Refusals -gt 0 -or $b.openLeaseWait -cne 'NONE') { $b.exit75Class = 'WAIT_RETRY' }
        if ($op.outcome -ceq 'UNRESOLVED' -and $b.openLeaseWait -cne 'NONE') {
            $b.endReason = 'DEADLINE'; $b.endDetail = "lease-wait: the open was still held in the agent's admission gate (exit-75 refusals, not a failure) at the deadline"
            Set-Stop 'DEADLINE' 3
        } elseif (($op.outcome -cne 'RECEIPT' -and $op.outcome -cne 'RECEIPT_BY_NAME') -or $null -eq $op.result -or [int]$op.result.exitCode -ne 0 -or $null -eq $adm -or [string]$adm.label -cne 'clear') {
            $b.endReason = 'OPEN_FAILED'; $b.endDetail = "outcome=$($op.outcome) exit=$(if ($op.result) { $op.result.exitCode }) label=$(if ($adm) { $adm.label }) $($op.message)".Trim()
            Set-Stop 'OPEN_FAILED' 9
        } elseif ($null -eq $adm.leaseBatch -or [string]$adm.leaseBatch.token -cne $token -or -not $adm.leaseBatch.leaseId) {
            $b.endReason = 'AGENT_NOT_BATCH_CAPABLE'; $b.endDetail = "admission.mode=$($adm.mode) gate=$($adm.gate) (no admission.leaseBatch for token ${token}: the lease-batch agent is not deployed)"
            Set-Stop 'AGENT_NOT_BATCH_CAPABLE' 10
        } else {
            $b.leaseId = [string]$adm.leaseBatch.leaseId; $b.held = [string]$adm.heavySlotHeld; $b.slot = $adm.slot; $b.leaseMinutes = $adm.leaseMinutes
            $b.leaseUntilEpoch = $adm.leaseBatch.untilEpoch; $b.openWaitedSec = $adm.waitedSec; $b.openRequests = $adm.leaseRequests; $b.refusals = @($adm.leaseRefusals)
            $openReadUtc = [DateTime]::UtcNow
            $leaseEnd = Get-LeaseEndUtc $op.result
            if ($null -eq $leaseEnd) { $leaseEnd = $openReadUtc }   # cannot place the lease's end: no leg fits
            # Where the lease began, for the ledger: its end less the minutes it asked for, never before the open was submitted.
            $leaseStartUtc = $(if ($b.leaseMinutes) { $leaseEnd.AddMinutes(-[double]$b.leaseMinutes) } else { $openSubmitUtc })
            if ($leaseStartUtc -lt $openSubmitUtc) { $leaseStartUtc = $openSubmitUtc }
            if ($leaseStartUtc -gt $openReadUtc) { $leaseStartUtc = $openReadUtc }
            # CAPS a and c: the legs never plan past -LeaseCapSec after the open was read, nor past the budget the 3 h window had left.
            $capEnd = $openReadUtc.AddSeconds([Math]::Min($LeaseCapSec, [Math]::Max(0, $bw.budgetRoomSec)))
            if ($leaseEnd -gt $capEnd) { $b.leaseEndClamp = "until=$($leaseEnd.ToString('o')) clamped to $($capEnd.ToString('o')) (capSec=$LeaseCapSec budgetRoomSec=$($bw.budgetRoomSec))"; $leaseEnd = $capEnd }
            $b.leaseEndUtc = $leaseEnd.ToString('o')
            L "BATCH_OPEN batch=$batchNo token=$token lease=$($b.leaseId) held=$($b.held) minutes=$($b.leaseMinutes) roomSec=$([int]($leaseEnd - [DateTime]::UtcNow).TotalSeconds) openWaitedSec=$($b.openWaitedSec) refusals=$(@($b.refusals).Count)"
            # (3) the legs, under the lease
            $attempt = 0
            while ($pending.Count -gt 0) {
                $leg = $pending[0]
                if ([DateTime]::UtcNow -ge $deadline) { $b.endReason = 'DEADLINE'; Set-Stop 'DEADLINE' 3; break }
                $room = [int](($leaseEnd - [DateTime]::UtcNow).TotalSeconds) - $LeaseMarginSec
                if ($room -lt $legBudget) {
                    $b.endReason = 'LEASE_REMAINING_SHORT'; $b.endDetail = "roomSec=$room < legBudgetSec=$legBudget (leaseMarginSec=$LeaseMarginSec): leg $($leg.name) goes to the next batch"
                    break
                }
                $attempt++
                $probeWait = [int][Math]::Max(0, [Math]::Min($ProbeMaxWaitSec, $room - $legBudget))
                $q = Invoke-QuietProbe $leg.name "b$('{0:00}' -f $batchNo)-a$attempt" $probeWait
                L "QUIET leg=$($leg.name) batch=$batchNo phase=in-lease exit=$($q.code) verdict=$($q.verdict) mean=$(Format-Mean $q.mean) maxWaitSec=$probeWait roomSec=$room$(if ($q.reason) { " reason=$($q.reason)" })$(if ($q.foreignFloor) { ' foreignFloor=1' })"
                if ($q.verdict -ceq 'USAGE') { $b.endReason = 'PROBE_USAGE'; Set-Stop 'USAGE' 2; break }
                if ($q.verdict -ceq 'GATE_UNREADABLE') { $b.endReason = 'PROBE_GATE_UNREADABLE'; Set-Stop 'GATE_UNREADABLE' 4; break }
                if ($q.verdict -ceq 'HOST_MISMATCH') { $b.endReason = 'PROBE_HOST_MISMATCH'; Set-Stop 'HOST_MISMATCH' 5; break }
                if ($q.verdict -ceq 'VENUE_QUIET_UNKNOWN') { $b.endReason = 'PROBE_VENUE_QUIET_UNKNOWN'; $b.endDetail = $q.reason; Set-Stop 'VENUE_QUIET_UNKNOWN' 6; break }
                if ($q.verdict -cne 'QUIET') {
                    # COOLDOWN_UNMET (a measured busy desktop, a foreign floor included) or GATE_BUSY: retry INSIDE this lease, never a release and a new request.
                    $b.retries++; $b.probeRetries++
                    # r3 (fable r2): with less room left after the leg's budget than one -RecheckSec, a busy probe ends the batch (the leg goes to the next one)
                    # instead of re-probing back to back, one probe and one light agent job per spin, until the room runs out.
                    $roomNow = [int](($leaseEnd - [DateTime]::UtcNow).TotalSeconds) - $LeaseMarginSec
                    if (($roomNow - $legBudget) -lt $RecheckSec) {
                        $b.endReason = 'LEASE_REMAINING_SHORT'; $b.endDetail = "after a busy probe ($($q.verdict)) roomSec=$roomNow - legBudgetSec=$legBudget < recheckSec=${RecheckSec}: leg $($leg.name) goes to the next batch"
                        break
                    }
                    L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=$($q.verdict) mean=$(Format-Mean $q.mean) roomSec=$room"
                    if ($q.verdict -ceq 'GATE_BUSY') { Start-Sleep -Seconds ([int][Math]::Max(1, [Math]::Min($ClaimPollSec, $room - $legBudget))) }
                    continue
                }
                # QUIET under the lease: the leg, on the same lease.
                $b.legsAttempted++
                $out = Join-Path $RunDir "leg-$($leg.name)-b$('{0:00}' -f $batchNo)-a$attempt.out.txt"
                $lArgs = @('-NoProfile', '-NonInteractive', '-File', $LegScript, '-Venue', $Venue, '-LegSpec', $leg.spec, '-SourceCommit', $SourceCommit, '-BuildManifestSha256', $BuildManifestSha256, '-LeaseBatch', $token)
                if ($leg.backend) { $lArgs += @('-Backend', $leg.backend) }
                if ($SheetCopyDir) { $lArgs += @('-SheetCopyDir', $SheetCopyDir) }
                L "LEG $($leg.name) RUN batch=$batchNo lease=$($b.leaseId) quietMean=$(Format-Mean $q.mean) roomSec=$room legBudgetSec=$legBudget spec=$($leg.spec) backend=$(if ($leg.backend) { $leg.backend } else { 'spec' })"
                $tLeg = [DateTime]::UtcNow
                # r3 (sol r2 B2): the leg runs only while the lease does. It is a bounded child: still running at the lease's end, it is killed with its whole
                # process tree (Kill($true), Invoke-VenueBoundedProcess) and typed LEASE_EXPIRED below. The agent stops its job at the same bound (lease-batch r3).
                $legBoundSec = [int][Math]::Max(1, [Math]::Floor(($leaseEnd - [DateTime]::UtcNow).TotalSeconds))
                $lp = Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $lArgs -TimeoutSec $legBoundSec
                [IO.File]::WriteAllText($out, [string]$lp.stdout + [string]$lp.stderr, [Text.UTF8Encoding]::new($false))
                $legCode = $lp.exit
                $legSec = [int]([DateTime]::UtcNow - $tLeg).TotalSeconds
                # a leg killed at the lease's end says nothing about how long a leg takes
                if (-not $lp.timedOut -and $legSec -gt $legBudget) { $legBudget = [Math]::Min(2700, $legSec) }
                $text = $(if (Test-Path -LiteralPath $out) { [IO.File]::ReadAllText($out) } else { '' })
                $legLines = @($text -split "`r?`n")
                $o = $legLines | Where-Object { $_ -match '^DVE_OUTCOME=(\S+)' } | Select-Object -Last 1
                $outcome = $(if ($o -and $o -match '^DVE_OUTCOME=(\S+)') { $Matches[1] } else { 'NONE' })
                $pl = $legLines | Where-Object { $_ -match '^DVE_RECEIPT_PATH=(.+)$' } | Select-Object -Last 1
                $receipt = $(if ($pl -and $pl -match '^DVE_RECEIPT_PATH=(.+)$') { $Matches[1].Trim() } else { $null })
                $dl = $legLines | Where-Object { $_ -match '^DVE_DETAIL=' } | Select-Object -Last 1
                $detail = $(if ($dl) { ($dl -replace '^DVE_DETAIL=', '').Trim() } else { '' })
                $legJobId = @(Get-VenueRemoteJobIds -Text $text | Where-Object { $_ -and $_ -cnotmatch '-health$' }) | Select-Object -Last 1
                if ($lp.timedOut) {
                    # Still running at the lease's end: killed with its tree. Never counted and never re-run (a leg that outlives a whole lease is not retried lease
                    # after lease). Its agent job is asked to cancel too, in case the agent's own bound has not stopped it yet. The batch ends: its lease is over.
                    $cx = $(if ($legJobId) { Request-VenueJobCancel -AgentShare $AgentShare -JobId $legJobId } else { [pscustomobject]@{ state = 'UNKNOWN'; error = 'the leg printed no job id' } })
                    $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'LEASE_EXPIRED'; outcome = 'INVALID'; class = 'LEASE_EXPIRED'; counted = $false; printedOutcome = $outcome; jobId = $legJobId; legSec = $legSec; legBoundSec = $legBoundSec; cancel = $cx.state }
                    $b.legs.Add([pscustomobject]$row); Add-Result $leg 'LEASE_EXPIRED' $row; $pending.RemoveAt(0)
                    $b.endReason = 'LEASE_EXPIRED'; $b.endDetail = "leg $($leg.name) was still running at the lease's end ($legSec s, bound $legBoundSec s): killed with its process tree; job $legJobId cancel=$($cx.state)$(if ($cx.error) { " ($($cx.error))" })"
                    L "LEG $($leg.name) LEASE_EXPIRED batch=$batchNo legSec=$legSec boundSec=$legBoundSec outcome=INVALID counted=False jobId=$legJobId cancel=$($cx.state) (killed with its process tree at the lease's end)"
                    if ($exitCode -eq 0) { $exitCode = 7 }
                    break
                }
                if ($legCode -ne 0) {
                    # Invoke-VenueLeg exits 0 whenever it wrote a receipt; non-zero means it died without one. Not evidence: LEG_FAILED, the run goes on.
                    $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'LEG_FAILED'; outcome = 'INVALID'; printedOutcome = $outcome; legExit = $legCode; jobId = $legJobId; counted = $false; legSec = $legSec }
                    $b.legs.Add([pscustomobject]$row); Add-Result $leg 'LEG_FAILED' $row; $pending.RemoveAt(0)
                    L "LEG $($leg.name) LEG_FAILED batch=$batchNo exit=$legCode outcome=INVALID printedOutcome=$outcome (the leg exited non-zero: no usable receipt)"
                    if ($exitCode -eq 0) { $exitCode = 7 }
                    continue
                }
                # Did it run on THIS batch's lease? Read by name, bounded.
                $jr = Get-VenueJobResult -AgentShare $AgentShare -JobId $legJobId
                $ja = $(if ($jr.state -ceq 'FOUND') { $jr.result.admission } else { $null })
                $jobLease = $(if ($null -ne $ja -and $null -ne $ja.leaseBatch) { [string]$ja.leaseBatch.leaseId } else { $null })
                $onLease = ($null -ne $jobLease -and $jobLease -ceq $b.leaseId -and [string]$ja.slotContinuity -cne 'lost')
                if ($onLease) { $e2 = Get-LeaseEndUtc $jr.result; if ($null -ne $e2 -and $e2 -lt $leaseEnd) { $leaseEnd = $e2; $b.leaseEndUtc = $leaseEnd.ToString('o') } }
                # CAPS e: the longest gap between the lease's successful renews up to this leg's end (the agent's own measure). Over -RenewGapMaxSec heavy-slot.py
                # demoted the lease, so this leg and every later one did not have it exclusively: never counted, and the batch ends.
                $gap = $(if ($null -ne $ja -and $null -ne $ja.leaseRenew -and $null -ne $ja.leaseRenew.okMaxGapSec) { [double]$ja.leaseRenew.okMaxGapSec } else { $null })
                if ($null -ne $gap -and ($null -eq $b.renewOkMaxGapSec -or $gap -gt $b.renewOkMaxGapSec)) { $b.renewOkMaxGapSec = $gap }
                # r3: the agent's own bound (lease-batch r3) stopped the job at its lease's end, on a demoted lease or a lost one: typed as such, never counted.
                $agentStop = $(if ($null -ne $ja -and $null -ne $ja.leaseStop) { [string]$ja.leaseStop.reason } else { $null })
                $leaseClass = $(if ($agentStop) { switch ($agentStop) { 'lease-end' { 'LEASE_EXPIRED' } 'demoted' { 'LEASE_DEMOTED' } default { 'LEASE_LOST' } } }
                    elseif (-not $onLease) { 'LEASE_LOST' } elseif ($null -eq $gap) { 'LEASE_RENEW_UNKNOWN' } elseif ($gap -gt $RenewGapMaxSec) { 'LEASE_DEMOTED' } else { $null })
                $leaseWhy = "jobId=$legJobId result=$($jr.state) jobLease=$jobLease slotContinuity=$(if ($ja) { $ja.slotContinuity }) renewOkMaxGapSec=$(if ($null -ne $gap) { $gap } else { 'UNKNOWN' })$(if ($agentStop) { " leaseStop=$agentStop" })$(if ($jr.error) { " error=$($jr.error)" })"
                # r3 (fable r2): a leg whose job took a fresh lease under this token carries that request's exit-75 refusals; the batch receipt counts them too.
                $legRef = $(if ($null -ne $ja) { @(@($ja.leaseRefusals) | Where-Object { $null -ne $_ -and [string]$_.rc -eq '75' }).Count } else { 0 })
                if ($legRef -gt 0) { $b.legExit75Refusals += $legRef; $b.exit75Refusals += $legRef; $b.exit75Class = 'WAIT_RETRY' }
                if ($outcome -ceq 'VENUE_NOT_QUIESCENT') {
                    # The leg's own <= 20 % re-check at its start failed: it never played. Retry INSIDE the same lease (on a lost or demoted lease, in the next batch).
                    $b.retries++; $b.legRetries++
                    $b.legs.Add([pscustomobject][ordered]@{ batch = $batchNo; attempt = $attempt; status = 'RETRY'; outcome = $outcome; receipt = $receipt; jobId = $legJobId; jobLease = $jobLease; onLease = $onLease; renewOkMaxGapSec = $gap; counted = $false; legSec = $legSec; exit75Refusals = $legRef })
                    L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=LEG_NOT_QUIESCENT receipt=$receipt onLease=$onLease $leaseWhy"
                    if ($leaseClass) { $b.endReason = $leaseClass; $b.endDetail = $leaseWhy; break }
                    continue
                }
                $preLoad = Get-VenueLegPreLoadMean -ReceiptPath $receipt
                # Only a QUIET probe reaches here (a FOREIGN_FLOOR exit is a COOLDOWN_UNMET verdict, retried above), so the class is the lease and the pre-load.
                $class = $(if ($leaseClass) { $leaseClass } elseif ($null -eq $preLoad) { 'QUIET_PRELOAD_UNKNOWN' } elseif ($preLoad -le 30.0) { 'QUIET_PRELOAD_OK' } else { 'QUIET_PRELOAD_HIGH' })
                $counted = ($class -ceq 'QUIET_PRELOAD_OK')
                $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'RAN'; outcome = $outcome; class = $class; counted = $counted; quietVerdict = $q.verdict; quietMean = $q.mean; preLoadMean = $preLoad
                    receipt = $receipt; jobId = $legJobId; jobLease = $jobLease; onLease = $onLease; renewOkMaxGapSec = $gap; legSec = $legSec; legExit = $legCode; exit75Refusals = $legRef }
                if ($detail) { $row['outcomeDetail'] = $detail }
                if ($agentStop) { $row['leaseStop'] = $agentStop }
                $b.legs.Add([pscustomobject]$row); Add-Result $leg 'RAN' $row; $pending.RemoveAt(0)
                $b.legsRan++; if ($counted) { $b.legsCounted++ }
                L "LEG $($leg.name) RAN batch=$batchNo exit=$legCode outcome=$outcome class=$class counted=$counted quietMean=$(Format-Mean $q.mean) preLoadMean=$(Format-Mean $preLoad) legSec=$legSec receipt=$receipt $leaseWhy"
                if ($leaseClass) { $b.endReason = $leaseClass; $b.endDetail = $leaseWhy; break }
                if ($pending.Count -gt 0 -and $CooldownSec -gt 0) { L "COOLDOWN ${CooldownSec} s (inside the lease)"; Start-Sleep -Seconds $CooldownSec }
            }
            if ($null -eq $b.endReason -and $pending.Count -eq 0) { $b.endReason = 'ALL_LEGS_DONE' }
            # A FRESH lease with no room for even one leg can never run it: stop rather than open lease after lease.
            if ($b.endReason -ceq 'LEASE_REMAINING_SHORT' -and $b.legsAttempted -eq 0 -and $b.probeRetries -eq 0) {
                $b.endReason = 'LEG_BUDGET_EXCEEDS_LEASE'; Set-Stop 'LEG_BUDGET_EXCEEDS_LEASE' 2
            }
        }
    } finally {
        # (4) END: release the lease now (a light close job), read the agent's receipt for it, release the claim, write the batch receipt.
        # r3 (sol r2 B1): an open with no result in hand (um-run gave up on it, the deadline came while its result was awaited, or this batch threw) may still
        # be in the agent's admission gate, re-asking for a lease. It is CANCELLED and reconciled BY NAME first, so it can never take a lease once this runner
        # lets go: the agent reads the cancel file at the top of every gate loop, before any request. A RETRACTED open never reached the agent.
        if ($b.openJobId -and ($null -eq $op -or ($op.outcome -cne 'RECEIPT' -and $op.outcome -cne 'RECEIPT_BY_NAME' -and $op.outcome -cne 'RETRACTED'))) {
            $cx = Request-VenueJobCancel -AgentShare $AgentShare -JobId $b.openJobId
            L "BATCH_OPEN_CANCEL batch=$batchNo job=$($b.openJobId) requested=$($cx.state)$(if ($cx.error) { " error=$($cx.error)" }) (read by name for up to ${CancelReconcileSec}s before the lease is closed and the claim released)"
            $tc = [DateTime]::UtcNow; $cPoll = [Math]::Min($ClaimPollSec, 5); $cr = $null
            while ($true) {
                $cr = Get-VenueJobResult -AgentShare $AgentShare -JobId $b.openJobId
                if ($cr.state -ceq 'FOUND' -or [DateTime]::UtcNow.AddSeconds($cPoll) -ge $tc.AddSeconds($CancelReconcileSec)) { break }
                Start-Sleep -Seconds $cPoll
            }
            $ca = $(if ($cr.state -ceq 'FOUND') { $cr.result.admission } else { $null })
            # CANCELLED: it left the gate with no lease. RAN_BEFORE_CANCEL: its lease turned active first, so it is this batch's lease and the close below releases it.
            # ENDED: it ended some other way, with no lease. UNRECONCILED: no answer in time (exit 11): its cancel file stays, and the ledger counts a full lease.
            $cState = $(if ($cr.state -cne 'FOUND') { 'UNRECONCILED' } elseif ($null -ne $ca -and [string]$ca.label -ceq 'cancelled-in-gate') { 'CANCELLED' }
                elseif ($null -ne $ca -and $null -ne $ca.leaseBatch -and $ca.leaseBatch.leaseId) { 'RAN_BEFORE_CANCEL' } else { 'ENDED' })
            if ($cState -ceq 'RAN_BEFORE_CANCEL' -and -not $b.leaseId) { $b.leaseId = [string]$ca.leaseBatch.leaseId; $leaseStartUtc = $openSubmitUtc }
            $b.openCancel = [ordered]@{ requested = $cx.state; state = $cState; resultState = $cr.state; resultLabel = $(if ($null -ne $ca) { [string]$ca.label } else { $null }); waitedSec = [int]([DateTime]::UtcNow - $tc).TotalSeconds; error = $(if ($cx.error) { $cx.error } else { $cr.error }) }
            L "BATCH_OPEN_RECONCILED batch=$batchNo state=$cState result=$($cr.state) label=$($b.openCancel.resultLabel) requested=$($cx.state) waitedSec=$($b.openCancel.waitedSec)"
            if ($cState -ceq 'UNRECONCILED') { $stopReason = 'OPEN_CANCEL_UNRECONCILED'; $exitCode = 11 }
        }
        if ($b.openJobId) {
            $b.closeJobId = "lease-batch-close-$stamp-lb$token"
            $cl = Submit-AgentJob $b.closeJobId "Write-Output 'LEASE_BATCH_CLOSE token=$token'" 60 300
            $b.closeOutcome = $cl.outcome
            if ($b.leaseId) {
                $ar = Read-VenueLeaseBatchReceipt -AgentShare $AgentShare -Token $token -LeaseId $b.leaseId
                $b.agent = $(if ($ar.state -ceq 'FOUND') { $ar.record } else { [pscustomobject]@{ state = $ar.state; error = $ar.error } })
            }
            # CAPS b and c: the lease this batch held, into the venue's ledger, conservatively wide: from its start to the close's return (a leg that took a
            # fresh lease under this token is inside that span too). An open whose cancel was never reconciled may yet take one: counted as a full lease from now.
            $row = $null
            if ($b.leaseId) { $row = [ordered]@{ venue = $Venue; token = $token; leaseId = $b.leaseId; startUtc = $leaseStartUtc.ToString('o'); endUtc = [DateTime]::UtcNow.ToString('o'); runDir = $RunDir; basis = 'open-to-close' } }
            elseif ($null -ne $b.openCancel -and $b.openCancel.state -ceq 'UNRECONCILED') { $n0 = [DateTime]::UtcNow; $row = [ordered]@{ venue = $Venue; token = $token; leaseId = $null; startUtc = $n0.ToString('o'); endUtc = $n0.AddSeconds($LeaseCapSec).ToString('o'); runDir = $RunDir; basis = 'open-unreconciled' } }
            if ($null -ne $row) {
                $b.ledger = $row
                try { Add-VenueLeaseLedger -Venue $Venue -ClaimDir $ClaimDir -Row ([pscustomobject]$row) } catch { L "LEDGER_WRITE_FAILED batch=$batchNo $($_.Exception.Message)" }
                $leases.Add([pscustomobject]@{ startUtc = $row.startUtc; endUtc = $row.endUtc; leaseId = $row.leaseId })
            }
        }
        L "CLAIM_RELEASED batch=$batchNo result=$(Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir)"
        Write-BatchReceipt $b
        L "BATCH_END batch=$batchNo token=$token lease=$($b.leaseId) reason=$($b.endReason) attempted=$($b.legsAttempted) ran=$($b.legsRan) counted=$($b.legsCounted) retries=$($b.retries) probeRetries=$($b.probeRetries) legRetries=$($b.legRetries) refusals=$(@($b.refusals).Count) exit75=$($b.exit75Refusals)/$($b.exit75Class) renewOkMaxGapSec=$($b.renewOkMaxGapSec) preOpenWait=$($b.preOpenWait.reason):$($b.preOpenWait.waitSec)s close=$($b.closeOutcome) agentEnd=$(if ($b.agent) { $b.agent.endReason })$(if ($b.endDetail) { " detail=$($b.endDetail)" })"
    }
}
foreach ($leg in $pending) { Add-Result $leg 'NOT_RUN' @{ verdict = $(if ($stopReason) { $stopReason } else { 'NOT_RUN' }); reason = 'run-stopped' }; L "LEG $($leg.name) NOT_RUN $stopReason reason=run-stopped" }

$summary = [ordered]@{
    schema = 'mlv-app/venue-lease-batch-result/v1'; venue = $Venue; runDir = $RunDir; startedUtc = $t0.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o')
    exitCode = $exitCode; stopReason = $stopReason; legs = @($results)
    ran = @($results | Where-Object { $_.status -ceq 'RAN' }).Count; counted = @($results | Where-Object { $_.status -ceq 'RAN' -and $_.counted }).Count
    failed = @($results | Where-Object { $_.status -ceq 'LEG_FAILED' }).Count; notRun = @($results | Where-Object { $_.status -ceq 'NOT_RUN' }).Count
    leaseExpired = @($results | Where-Object { $_.status -ceq 'LEASE_EXPIRED' }).Count
    batches = @($batches | ForEach-Object { [ordered]@{ batch = $_.batch; token = $_.token; leaseId = $_.leaseId; endReason = $_.endReason; legsAttempted = $_.legsAttempted; legsRan = $_.legsRan; legsCounted = $_.legsCounted; retries = $_.retries } })
}
$resultLine = 'LEASE_BATCH_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
Add-Content -LiteralPath $batchLog -Value $resultLine
Write-Output $resultLine
Complete-VenueProcess -ExitCode $exitCode   # a share read abandoned at its timeout would otherwise keep this process alive until it ends
exit $exitCode
