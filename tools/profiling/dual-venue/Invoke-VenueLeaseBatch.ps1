# Invoke-VenueLeaseBatch.ps1 -- run Bachelor bench legs INSIDE one quiet lease per batch (BACHELOR-LEASE-BATCH-LEGS-1). RUN THIS ON THE VM.
#
# WHY. The Bachelor agent takes a heavy-slot.py quiet lease for every gated job. heavy-slot.py is Bachelor's, not ours (45 min max, 30 min pending, 30 min
# post-lease cooldown, <= 50 % of any 3 h), and it refuses a new lease for 30 min after the last one ends. With one lease per leg, and the venue quiet probe
# run as a light job OUTSIDE any lease (where it reads the open desktop), a leg that played about 1 min cost about 34 min (5 of 32 legs on 2026-10-23).
# WHAT. One lease per BATCH, requested FIRST. The probe and the legs then run under it:
#   1. CLAIM. The venue claim (VenueChain.psm1, ticketless) is taken once per batch and held until the batch ends. r5 (dot B1): its expiry is renewed every
#      -ClaimRenewSec while it is held (Start-VenueClaimKeeper), so no wait in the batch, however long, lets the claim of this live run read as expired.
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
#         QUIET_PRELOAD_UNKNOWN or LEASE_LOST (uncounted). r5 (V-01): a FOREIGN_FLOOR line from ANY probe of the leg (an earlier attempt or batch included)
#         sticks: the leg is COOLDOWN_UNMET (uncounted) whatever a later QUIET probe reads, as in Invoke-VenueChain. It never carries into the next leg.
#   4. END. A batch ends when its legs are done (ALL_LEGS_DONE), the claim is lost (CLAIM_LOST, 8b), the open's sidecar cannot be written (LIVENESS_UNAVAILABLE,
#      8a), the next leg would outlive the lease (LEASE_REMAINING_SHORT: the rest go to the next batch; also
#      a busy probe with less room left after the leg's budget than one -RecheckSec), the lease was lost (LEASE_LOST), demoted (LEASE_DEMOTED / LEASE_RENEW_UNKNOWN, 6e)
#      or ended under a leg (LEASE_EXPIRED, 7b), the deadline passed (DEADLINE), or a probe could not measure (PROBE_<verdict>: the run stops). An open still
#      unanswered is cancelled and reconciled first (7a). The light job
#      lease-batch-close-<stamp>-lb<token> then releases the lease at once (the agent's idle bound would, later). The agent's own batch receipt is read by
#      name (logs\lease-batch-<token>.jsonl), and the claim is released.
#   5. RECEIPTS. <RunDir>\batch-<nn>.json and one line per batch in <RunDir>\batches.jsonl (mlv-app/venue-lease-batch/v1): token, lease id, held, start and
#      end, the open's wait and refusals, legs attempted / ran / counted, retries (probe and leg re-check), the end reason, the per-leg rows and the agent's
#      receipt. <RunDir>\batch.log has one line per event, UTC stamp first; the last stdout line is LEASE_BATCH_RESULT=<json>.
#   6. CAPS (r2; Bachelor's heavy-slot.py, read from heavy-slot.lease.json, CAPS-ADDENDUM 2026-10-23; each one binding). The figures below (45 / 30 / 50 %) are the
#      defaults: at run start ONE exempt light job (venue-quiet-probe-caps-<stamp>, no lease) prints C:\Users\layib\.claude\ops\heavy-slot.lease.json and the run takes
#      cooldown = cooldown_minutes*60, budget = max_fraction_per_3h*10800 and lease cap = min(2700, max_minutes*60) from it (capsSource=live in the receipts and the CAPS
#      line of batch.log). Unreadable or invalid (heavy-slot.py's own rules) -> 1800 / 5400 / 2700 (capsSource=fallback). A caller can only tighten them (LEASE_CAP_RELAXED):
#      r2 (CoS/Dot on affa0d1a): the budget is always fraction * the FIXED 10800 s, whatever -LeaseBudgetWindowSec is (a longer window only counts more earlier leases
#      against it, a shorter one only shrinks it); a cooldown is kept exactly (2880 min = 172800 s), and one no int of seconds can hold is the fallback; the probe job id is
#      venue-quiet-probe-caps-<stamp>-<12 hex GUID> (New-VenueCapsProbeId), so two runs started in the same second never share an id.
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
#      f. max pending 30 min: the agent's --admit-timeout is LeasePendingMaxSec (<= 1800), or less when the open's notAfterUtc is nearer (7a); a request past it
#         is abandoned and asked again (d).
#      Foreign holders' leased time is not in the ledger: heavy-slot.py still refuses (75) on it, and (d) waits that out.
#   7. r3 (sol r2 blockers):
#      a. NO LEASE AFTER THE RUNNER LETS GO. An open with no result in hand at the END of its batch (um-run gave up, the deadline came while its result was
#         awaited, the batch threw) may still be in the agent's gate, re-asking for a lease. The runner writes its cancel file (Request-VenueJobCancel; the agent
#         reads it at the top of every gate loop, before any request), then reads its result BY NAME for up to -CancelReconcileSec, and only then closes the
#         lease and releases the claim: openCancel CANCELLED (no lease), RAN_BEFORE_CANCEL (its lease is this batch's: the close releases it), ENDED, or
#         UNRECONCILED (exit 11: the cancel file stays, and the ledger counts a full lease for it). The agent also ends a batch job's gate wait once that batch's
#         close job is queued (lease-batch r3). r4 (sol r3 B1): the open also carries the batch deadline as notAfterUtc (um-run.ps1 -NotAfterUtc; the batch
#         receipt's openNotAfterUtc), so even an agent that never answers the cancel takes no lease for it past the deadline: it requests none, its request's
#         --admit-timeout ends by then, and a lease that turns active later is released (LEASE-REQUEST-CHILD-NOT-AFTER-1). The close job carries none.
#         The bound holds to within the VM-to-Bachelor clock skew; a frozen agent process is not covered.
#      b. NO LEG PAST THE LEASE'S END. A leg runs as a bounded child (Invoke-VenueBoundedProcess) whose bound is the lease's end: still running then, it is
#         killed with its whole process tree, its agent job is asked to cancel, and it is LEASE_EXPIRED (outcome INVALID, uncounted, never re-run; exit 7).
#         The agent stops a batch job at the same bound, or on a demoted or lost lease (admission.leaseStop), which types it LEASE_EXPIRED / LEASE_DEMOTED /
#         LEASE_LOST. -LegBudgetSec only decides whether a leg STARTS; it never lets one run past the lease.
#      c. r5 (sol B1 on an early runner exit): while this run owns the open, it keeps the open's runner-liveness sidecar, <share>\notafter\<openJobId>.json (the
#         file the agent's LEASE-BATCH-OPEN-RUNNER-LIVENESS-1 reads), -LivenessWindowSec ahead, rewritten every -LivenessRenewSec by an in-process keeper. A run
#         killed early stops renewing it, so within -LivenessWindowSec (plus one agent gate loop) the agent ends the open's gate wait as expired-not-after and
#         releases a pending or just-active lease. With #644 (the open's notAfterUtc) and #663 (the agent reads the sidecar) this closes sol r3 B1. The sidecar
#         is removed once the open's result is in hand, or rewritten lapsed and left when the open is never reconciled. Not promised: the VM-to-Bachelor clock
#         skew (aliveUntilUtc is this host's clock), and a FROZEN (not dead) runner or agent.
#   8. r5b (the final narrow round, hub ACK 6389333885):
#      a. NO SIDECAR, NO OPEN (dot ef5805a4 residual B1). The open is submitted only once its FIRST sidecar write is WRITTEN. NO_NOTAFTER_DIR or UNKNOWN stops
#         the keeper, submits nothing (openOutcome NOT_SUBMITTED, no cancel, no close) and ends the batch LIVENESS_UNAVAILABLE (exit 9), uncounted.
#      b. NO PROBE OR LEG WITHOUT THE CLAIM (Grok G1). Before each in-lease probe and each leg the claim keeper's state is read (Get-VenueClaimKeeperLoss): found
#         NOT_OURS or ABSENT, or past its last good expiresUtc, the batch ends CLAIM_LOST (uncounted). The legs already run stay as measured; the rest go to the
#         next batch, which takes the claim again.
#      c. AN UNRESOLVED LEG IS WITHDRAWN (hub 13:21Z B2 clause). A leg whose job has no agent result by name (um-run said UNRESOLVED, or the read failed) gets
#         its cancel file at once (row withdraw, LEG_WITHDRAWN), so the agent never launches it later on a parked lease.
#   9. MULTI-BUILD LEG PLAN (LEASE-BATCH-MULTI-BUILD-1). The scalar form runs one build and one backend per call, so an A/B driver paid a lease (and its 30 min
#      cooldown) per build. -LegPlan <json> instead names an ordered list of legs, each with its own build: {"schema": "mlv-app/venue-lease-leg-plan/v1", "legs":
#      [{legSpec, backend, sourceCommit, buildManifestSha256, arm}, ...]} (a bare array of items is read too). legSpec is a path (as given, then beside the plan
#      file, then legs\<name>); backend is cuda or cpu; arm is a free label (A, B, base, cand...). It is a parameter set of its own: -LegSpec / -LegSet / -Repeats /
#      -Backend / -SourceCommit / -BuildManifestSha256 cannot be passed with it, and the scalar call is unchanged (same receipts, rows and exit codes).
#      a. EVERY item is validated BEFORE any claim or lease: the hex patterns of the scalar form, backend, arm, the leg spec found, and its build STAGED on the
#         venue with exactly that manifest (Get-VenueStagedBuildManifest, the file Invoke-VenueLeg hashes). One bad item refuses the whole plan on stderr,
#         BATCH_USAGE LEG_PLAN_REFUSED reason=<BAD_ITEM_SHAPE | BAD_SOURCE_COMMIT | BAD_MANIFEST_SHA256 | BAD_BACKEND | BAD_ARM | LEG_SPEC_MISSING | BUILD_NOT_STAGED |
#         BUILD_MANIFEST_MISMATCH | SHARE_UNREACHABLE | LEG_SPEC_INVALID> item=<n> (or LEG_PLAN_UNREADABLE | LEG_PLAN_INVALID), exit 2: nothing is submitted, so no lease ever fails
#         halfway on a build. A spec that is not a mlv-app/dual-venue-leg/v1 spec, does not list the item's backend, or has a field Invoke-VenueLeg casts or validates
#         (playSeconds, visualCapture and its opt-out reason, scaleFactor, acceptedEffectiveScale) it would throw on, is refused here (LEG_SPEC_INVALID, BAD_BACKEND)
#         because Invoke-VenueLeg would throw on it only after the lease is open.
#      b. The legs run in plan order (-PlanOrder AsGiven), or with the arms INTERLEAVED, A B A B (-PlanOrder Interleave, Get-VenueInterleavedLegPlan), so lease
#         position and venue heat fall on every arm alike. Per-leg budget, re-check, claim loss and deadline handling are the scalar form's, leg by leg.
#      c. Each leg process gets its own -SourceCommit / -BuildManifestSha256 / -Backend; each batch leg row and run result carries name, arm, backend, sourceCommit
#         and buildManifestSha256; the LEG ... RUN line names them; <RunDir>\leg-plan.json (written once the claim is held) keeps the validated plan in run order and each build's staged state.
# It changes nothing on Bachelor: not heavy-slot.py's caps, not its lease file, not the quiet threshold. A batch run needs the lease-batch agent deployed.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeaseBatch.ps1 -LegSpec a.json,b.json -Backend cpu -RunDir <rd> `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> [-DeadlineUtc <iso>]
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeaseBatch.ps1 -LegPlan ab-plan.json -PlanOrder Interleave -RunDir <rd> [-DeadlineUtc <iso>]
#
# Exit codes (a leg's OUTCOME is never the exit code: read LEASE_BATCH_RESULT and the receipts): 0 every leg ran; 2 usage (or a leg budget no lease can hold,
# or a refused -LegPlan: LEG_PLAN_REFUSED);
# 3 DEADLINE before every leg ran (a cooldown, budget or exit-75 wait that reached it included); 4 GATE_UNREADABLE and 5 HOST_MISMATCH from Wait-VenueQuiet; 6 a probe ended VENUE_QUIET_UNKNOWN; 7 a leg process exited
# non-zero (LEG_FAILED) or was killed at the lease's end (LEASE_EXPIRED; the run goes on); 9 a batch lease could not be opened (the open job was RETRACTED,
# UNRESOLVED or not admitted, or never submitted because its sidecar could not be written: LIVENESS_UNAVAILABLE); 10 the agent is not batch-capable; 11 an open left in the agent's gate was cancelled but never reconciled (OPEN_CANCEL_UNRECONCILED,
# 7a); 12 an unexpected throw inside a batch after its claim was taken (BATCH_ERROR: the claim keeper is stopped, the claim released and the batch receipt written
# first; a leg whose child had started is LEG_FAILED reason=BATCH_ERROR, the legs never started are NOT_RUN). A stopping code (2-6, 9, 10) replaces 0 or 7; 12 replaces every other code but 11, and 11 replaces every other code.
# -QuietScript / -LegScript / -UmRunScript / -AgentShare / -ClaimDir are seams for the offline tests (tools/repo_hygiene/test_venue_lease_batch.py).
[CmdletBinding(DefaultParameterSetName = 'Scalar')]
param(
    [ValidateSet('bachelor')][string]$Venue = 'bachelor',
    [Parameter(ParameterSetName = 'Scalar')][string[]]$LegSpec = @(),
    [Parameter(ParameterSetName = 'Scalar')][string]$LegSet = '',
    [Parameter(ParameterSetName = 'Scalar')][ValidateRange(1, 20)][int]$Repeats = 1,
    [Parameter(ParameterSetName = 'Scalar')][ValidateSet('cuda', 'cpu')][string]$Backend,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [Parameter(ParameterSetName = 'Scalar', Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [Parameter(ParameterSetName = 'Scalar', Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256,
    # LEASE-BATCH-MULTI-BUILD-1 (item 9): an ordered leg plan, each leg with its own build, backend and arm, run in ONE batched lease series
    [Parameter(ParameterSetName = 'Plan', Mandatory = $true)][string]$LegPlan,
    [Parameter(ParameterSetName = 'Plan')][ValidateSet('AsGiven', 'Interleave')][string]$PlanOrder = 'AsGiven',
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
    # Bachelor's heavy-slot.py caps, binding (see CAPS above), read LIVE at run start (the 1800 / 5400 / 2700 defaults are only the fallback). Each can only be
    # TIGHTENED: the ranges stop a longer lease, a bigger budget or a longer renew gap, and a cooldown shorter than the live one, a budget above it or a shorter window
    # is refused unless -UmRunScript is an offline stub (LEASE_CAP_RELAXED).
    [ValidateRange(60, 2700)][int]$LeaseCapSec = 2700,
    [ValidateRange(0, 86400)][int]$LeaseCooldownSec = 1800,
    [ValidateRange(60, 10800)][int]$LeaseBudgetSec = 5400,
    [ValidateRange(60, 86400)][int]$LeaseBudgetWindowSec = 10800,
    [ValidateRange(1, 120)][int]$RenewGapMaxSec = 120,
    [ValidateRange(60, 86400)][int]$ClaimTtlSec = 10800,
    # r5 (dot 242171b3 B1): the held claim's expiry is renewed this often (0 = a quarter of -ClaimTtlSec, at most 600 s) for as long as this run holds it
    [ValidateRange(0, 3600)][int]$ClaimRenewSec = 0,
    [ValidateRange(1, 3600)][int]$ClaimPollSec = 30,
    # r5 (sol B1, #663): while this run owns the open job, its runner-liveness sidecar is kept -LivenessWindowSec ahead, rewritten every -LivenessRenewSec
    [ValidateRange(5, 1800)][int]$LivenessWindowSec = 120,
    [ValidateRange(1, 600)][int]$LivenessRenewSec = 30,
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
if ($umRunIsReal -and $LeaseBudgetWindowSec -lt 10800) {
    Stop-Usage "LEASE_CAP_RELAXED -LeaseBudgetWindowSec $LeaseBudgetWindowSec is looser than Bachelor's 10800 s window"
}
$cooldownExplicit = $PSBoundParameters.ContainsKey('LeaseCooldownSec'); $budgetExplicit = $PSBoundParameters.ContainsKey('LeaseBudgetSec')
if ($ClaimRenewSec -eq 0) { $ClaimRenewSec = [int][Math]::Max(1, [Math]::Min(600, [Math]::Floor($ClaimTtlSec / 4))) }
if (2 * $ClaimRenewSec -gt $ClaimTtlSec) { Stop-Usage "-ClaimRenewSec $ClaimRenewSec is over half of -ClaimTtlSec ${ClaimTtlSec}: one late renewal would let the claim expire" }
if (2 * $LivenessRenewSec -gt $LivenessWindowSec) { Stop-Usage "-LivenessRenewSec $LivenessRenewSec is over half of -LivenessWindowSec ${LivenessWindowSec}: one late write would lapse a live runner" }
$legPlanMode = ($PSCmdlet.ParameterSetName -ceq 'Plan')
# -PlanOrder binds case-insensitively; every later test of it is case-sensitive, so name the canonical spelling once (sol r3 A1)
$PlanOrder = @('AsGiven', 'Interleave') | Where-Object { $_ -ieq $PlanOrder } | Select-Object -First 1
if (-not $legPlanMode -and (($LegSpec.Count -gt 0) -eq [bool]$LegSet)) { Stop-Usage 'name the legs with exactly one of -LegSpec <path>... or -LegSet <name>' }
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
    try { Add-Content -LiteralPath $batchLog -Value $line } catch { Register-LogFailure $_.Exception.Message; throw }   # r7 (adversary A1): every append failure is counted and said, whichever call site wrote the line
    Write-Output $line
}

# r6 (Dot r5 B5): the log is best effort wherever a mandatory step (keeper stop, claim release, receipt, summary, exit code) follows it. A batch.log another process holds
# exclusively must not skip them: the failure is swallowed, counted (logWriteFailures in the receipt and the summary), said once on stderr, and the line still reaches stdout.
$script:logWriteFailures = 0
function Register-LogFailure([string]$Why) {
    $script:logWriteFailures++
    if ($script:logWriteFailures -eq 1) { try { [Console]::Error.WriteLine("BATCH_LOG_FAILED the batch log cannot be written ($($Why -replace '\s+', ' ')); later failures are counted in the batch receipt as logWriteFailures") } catch { } }
}
function LogBest([string]$Message) {
    $failuresBefore = $script:logWriteFailures
    try { L $Message } catch {
        if ($script:logWriteFailures -eq $failuresBefore) { Register-LogFailure $_.Exception.Message }   # L counts a failed append itself
        try { Write-Output "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message" } catch { }
    }
}

function Stop-PlanRefused([string]$Reason, [int]$Item, [string]$Detail) {
    # LEASE-BATCH-MULTI-BUILD-1 (item 9a): a -LegPlan item that cannot run refuses the WHOLE plan before any claim, open or lease. Nothing was submitted.
    $line = "LEG_PLAN_REFUSED reason=$Reason$(if ($Item -gt 0) { " item=$Item" }) $Detail"
    # The log line is best effort (sol r3 A5): a batch.log another process holds must not turn the typed refusal into an unhandled exit 1 with no BATCH_USAGE line.
    try { Add-Content -LiteralPath $batchLog -Value "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $line" } catch { }
    [Console]::Error.WriteLine("BATCH_USAGE $line")
    try { Complete-VenueProcess -ExitCode 2 } catch { }
    exit 2
}

function Resolve-LegSpecPath([string]$Ref, [string]$BesideDir) {
    # a leg spec as given, then beside the plan file (-LegPlan only), then legs\<name>; $null when none is a file
    foreach ($c in @($Ref, $(if ($BesideDir -and -not [IO.Path]::IsPathRooted($Ref)) { Join-Path $BesideDir $Ref }), (Join-Path $here "legs\$Ref"))) {
        # a candidate the file system cannot probe (a NUL in the name, sol r3 second pass A2) is not a file
        try { if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return (Resolve-Path -LiteralPath $c).Path } } catch { }
    }
    $null
}

function Get-LegSpecId([string]$Path) {
    $legId = try { [string](([IO.File]::ReadAllText($Path) | ConvertFrom-Json).legId) } catch { '' }
    # \z, not $: '$' also matches before a final LF, which would ride into the leg name and then into file names (sol r3 A3)
    if ($legId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\z') { $legId = [IO.Path]::GetFileNameWithoutExtension($Path) }
    $legId
}

function Get-LegSpecRefusal([string]$Path, [string]$Backend) {
    # sol r3 A4: the refusal Invoke-VenueLeg would throw (DVE_LEG_SPEC_INVALID) AFTER the lease is open, found before it. '' when the leg can take this backend.
    # Same reads as Invoke-VenueLeg: the file's bytes as UTF-8, ConvertFrom-Json, the schema, then the backend against the spec's own list.
    # sol r3 second pass A1: then the spec fields Invoke-VenueLeg casts or validates BEFORE its terminal try (playSeconds, visualCapture and its opt-out reason,
    # scaleFactor and acceptedEffectiveScale, displayMode), by calling the functions it calls. r5 (key3 F2): and the card and legId it binds to New-DvReceipt's
    # Mandatory [string] parameters. NOT mirrored: the leg's pre-submit REFUSALS (LEG_SPEC_NOT_COMMITTED, the playSeconds floor, clipId admission; OPEN
    # LEASE-BATCH-PLAN-PREFLIGHT-ADMISSION-PARITY-1) and the generatorArgs casts, which sit inside the leg's try and end as a typed receipt.
    $spec = try { [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($Path)) | ConvertFrom-Json } catch { $null }
    if ($null -eq $spec -or $spec -isnot [System.Management.Automation.PSCustomObject]) { return 'LEG_SPEC_INVALID', "'$Path' is not a JSON object" }
    if ($spec.schema -ne 'mlv-app/dual-venue-leg/v1') { return 'LEG_SPEC_INVALID', "'$Path' schema is '$($spec.schema)', expected mlv-app/dual-venue-leg/v1" }
    if ($Backend -notin @($spec.backends)) { return 'BAD_BACKEND', "backend '$Backend' is not one of this leg's backends ($(@($spec.backends) -join ', ')) in '$Path'" }
    try {
        if ($null -ne $spec.PSObject.Properties['playSeconds']) { $null = [int]$spec.playSeconds }
        $null = Get-DvVisualCaptureRequest -Spec $spec
        $null = Get-DvScaleEvidence -Spec $spec -Backend $Backend -LogText $null
        $null = Get-DvDisplayEvidence -Spec $spec -LogText $null
        if ([string]::IsNullOrEmpty([string]$spec.card)) { throw 'the spec has no card (Invoke-VenueLeg binds it to New-DvReceipt, which refuses an empty one)' }
        if ([string]::IsNullOrEmpty([string]$spec.legId)) { throw 'the spec has no legId (Invoke-VenueLeg binds it to New-DvReceipt, which refuses an empty one)' }
    } catch { return 'LEG_SPEC_INVALID', "'$Path' cannot be run: $($_.Exception.Message -replace '\s+', ' ')" }
    return '', ''
}

# The planned legs: name, spec path, backend ('' = whatever the spec lists). Same naming as Invoke-VenueChain.
$plan = [System.Collections.Generic.List[object]]::new()
$planRecord = $null
if ($legPlanMode) {
    # LEASE-BATCH-MULTI-BUILD-1 (item 9): read the plan, validate EVERY item (shape first, then each build staged with that manifest), then order it.
    try {
        Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force   # the leg process's own spec predicates, run in preflight (Get-LegSpecRefusal)
        $planPath = $(if (Test-Path -LiteralPath $LegPlan -PathType Leaf) { (Resolve-Path -LiteralPath $LegPlan).Path } else { $null })
        if (-not $planPath) { Stop-PlanRefused 'LEG_PLAN_UNREADABLE' 0 "no plan file at '$LegPlan'" }
        $doc = try { [IO.File]::ReadAllText($planPath) | ConvertFrom-Json -NoEnumerate } catch { Stop-PlanRefused 'LEG_PLAN_UNREADABLE' 0 "'$planPath' is not JSON: $($_.Exception.Message)" }
        # Dot B1: the entries are kept exactly as written, nulls included, so each is shape-checked at its ORIGINAL position below. Nothing here filters, unrolls or wraps them.
        $rawItems = $null
        if ($doc -is [array]) { $rawItems = $doc }
        elseif ($doc -is [System.Management.Automation.PSCustomObject] -and $doc.PSObject.Properties['legs']) {
            if ($doc.PSObject.Properties['schema'] -and [string]$doc.schema -cne 'mlv-app/venue-lease-leg-plan/v1') { Stop-PlanRefused 'LEG_PLAN_UNREADABLE' 0 "schema '$($doc.schema)' is not mlv-app/venue-lease-leg-plan/v1" }
            if ($doc.legs -isnot [array]) { Stop-PlanRefused 'LEG_PLAN_UNREADABLE' 0 "'legs' in '$planPath' is not an array (a single leg is a one-item array)" }
            $rawItems = $doc.legs
        }
        if ($null -eq $rawItems -or $rawItems.Count -eq 0) { Stop-PlanRefused 'LEG_PLAN_UNREADABLE' 0 "'$planPath' names no legs (an array of items, or an object with a legs array)" }
        $planDir = Split-Path -Parent $planPath
        $items = [System.Collections.Generic.List[object]]::new()
        for ($i = 0; $i -lt $rawItems.Count; $i++) {
            $it = $rawItems[$i]; $n = $i + 1
            if ($it -isnot [System.Management.Automation.PSCustomObject]) { Stop-PlanRefused 'BAD_ITEM_SHAPE' $n "an item is an object {legSpec, backend, sourceCommit, buildManifestSha256, arm}, not $(if ($null -eq $it) { 'null' } else { $it.GetType().Name }); the whole plan is refused" }
            $sc = [string]$it.sourceCommit; $ms = [string]$it.buildManifestSha256; $be = [string]$it.backend; $arm = [string]$it.arm; $ref = [string]$it.legSpec
            # the scalar form's own patterns (ValidatePattern, case-insensitive) and backend set; \z, not $, so a final LF is not accepted (sol r3 A3)
            if ($sc -notmatch '^[0-9a-f]{40}\z') { Stop-PlanRefused 'BAD_SOURCE_COMMIT' $n "sourceCommit '$sc' is not 40 hex" }
            if ($ms -notmatch '^[0-9a-f]{64}\z') { Stop-PlanRefused 'BAD_MANIFEST_SHA256' $n "buildManifestSha256 '$ms' is not 64 hex" }
            if ($be -notin @('cuda', 'cpu')) { Stop-PlanRefused 'BAD_BACKEND' $n "backend '$be' is not cuda or cpu" }
            if ($arm -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,31}\z') { Stop-PlanRefused 'BAD_ARM' $n "arm '$arm' is not a label of 1-32 of [A-Za-z0-9._-]" }
            $path = $(if ($ref) { Resolve-LegSpecPath $ref $planDir } else { $null })
            if (-not $path) { Stop-PlanRefused 'LEG_SPEC_MISSING' $n "no leg spec at '$ref'" }
            $specRefusal = Get-LegSpecRefusal $path $be
            if ($specRefusal[0]) { Stop-PlanRefused $specRefusal[0] $n $specRefusal[1] }
            $items.Add([pscustomobject]@{ item = $n; legId = (Get-LegSpecId $path); spec = $path; backend = $be.ToLowerInvariant(); arm = $arm; sourceCommit = $sc.ToLowerInvariant(); buildManifestSha256 = $ms.ToLowerInvariant() })
        }
        # Each build once (the read Invoke-VenueChain's supplementary leg makes): staged on the venue, and with exactly the manifest the item names.
        $builds = [ordered]@{}
        foreach ($it in $items) {
            if (-not $builds.Contains($it.sourceCommit)) { $builds[$it.sourceCommit] = Get-VenueStagedBuildManifest -AgentShare $AgentShare -SourceCommit $it.sourceCommit -TimeoutSec 30 }
            $st = $builds[$it.sourceCommit]
            $short = $it.sourceCommit.Substring(0, 12)
            if ($st.state -ceq 'UNREACHABLE') { Stop-PlanRefused 'SHARE_UNREACHABLE' $it.item "the staged build manifest for $short could not be read at $($st.path): $($st.error)" }
            if ($st.state -cne 'STAGED') { Stop-PlanRefused 'BUILD_NOT_STAGED' $it.item "no staged build manifest for $short at $($st.path); the runner stages and builds nothing" }
            if ($st.sha256 -cne $it.buildManifestSha256) { Stop-PlanRefused 'BUILD_MANIFEST_MISMATCH' $it.item "the staged build manifest for $short is sha256 $($st.sha256), not $($it.buildManifestSha256) (a different build is never substituted)" }
            # r5 (adversary A2): and the manifest must be one the leg job's validator accepts (its commit, dll pair digest and symbol presence), not only the one named by hash
            if ($st.contentProblem) { Stop-PlanRefused 'BUILD_MANIFEST_MISMATCH' $it.item "the staged build manifest for $short at $($st.path) is not one the leg job accepts: $($st.contentProblem)" }
        }
        $ordered = $(if ($PlanOrder -ceq 'Interleave') { @(Get-VenueInterleavedLegPlan -Items @($items)) } else { @($items) })
        foreach ($it in $ordered) {
            $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}-{3}' -f ($plan.Count + 1), $it.legId, $it.backend, $it.arm); spec = $it.spec; backend = $it.backend
                    arm = $it.arm; sourceCommit = $it.sourceCommit; buildManifestSha256 = $it.buildManifestSha256; planItem = $it.item })
        }
        $planRecord = [ordered]@{ schema = 'mlv-app/venue-lease-leg-plan-run/v1'; legPlan = $planPath; order = $PlanOrder; arms = @($plan | ForEach-Object { $_.arm } | Select-Object -Unique)
            legs = @($plan | ForEach-Object { [ordered]@{ name = $_.name; planItem = $_.planItem; arm = $_.arm; backend = $_.backend; sourceCommit = $_.sourceCommit; buildManifestSha256 = $_.buildManifestSha256; spec = $_.spec } })
            builds = @($builds.Keys | ForEach-Object { [ordered]@{ sourceCommit = $_; state = $builds[$_].state; sha256 = $builds[$_].sha256; path = $builds[$_].path } }) }
    } catch {
        # sol r3 second pass A2: whatever else a plan item makes the validation throw on is still the typed refusal of the whole plan (exit 2, BATCH_USAGE), never an exit 1.
        Stop-PlanRefused 'LEG_PLAN_INVALID' 0 "the plan could not be validated: $($_.Exception.Message -replace '\s+', ' ')"
    }
} elseif ($LegSet) {
    Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force
    $sp = try { Get-DvLegSetPlan -LegSetPath (Join-Path $here "legsets\$LegSet.json") -Repeats $Repeats -Backend $(if ($Backend) { $Backend } else { '' }) } catch { Stop-Usage $_.Exception.Message }
    foreach ($e in $sp.plan) { $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f $e.seq, $e.legId, $e.backend); spec = $e.specPath; backend = $e.backend }) }
} else {
    foreach ($ref in $LegSpec) {
        $path = Resolve-LegSpecPath $ref ''
        if (-not $path) { Stop-Usage "no leg spec at '$ref'" }
        $legId = Get-LegSpecId $path
        for ($r = 1; $r -le $Repeats; $r++) {
            $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f ($plan.Count + 1), $legId, $(if ($Backend) { $Backend } else { 'spec' })); spec = $path; backend = $(if ($Backend) { $Backend } else { '' }) })
        }
    }
}
# The scalar form's one build rides on every leg, so each leg is launched with its own build in either form.
if (-not $legPlanMode) { foreach ($p in $plan) { $p | Add-Member -NotePropertyName sourceCommit -NotePropertyValue $SourceCommit; $p | Add-Member -NotePropertyName buildManifestSha256 -NotePropertyValue $BuildManifestSha256 } }

function Add-PlanIdentity([object]$Row, [object]$Leg) {
    # LEASE-BATCH-MULTI-BUILD-1 (item 9c): a -LegPlan leg's batch row names its own leg, arm, backend and build. A scalar run's rows are left as they were.
    if (-not $legPlanMode) { return }
    foreach ($k in 'name', 'arm', 'backend', 'sourceCommit', 'buildManifestSha256') { $Row | Add-Member -NotePropertyName $k -NotePropertyValue $Leg.$k -Force }
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
    [pscustomobject]@{ verdict = $verdict; code = $code; mean = $mean; reason = $reason; foreignFloor = [bool]$ff; foreignFloorLine = [string]$ff }
}

function Submit-AgentJob([string]$JobId, [string]$Body, [int]$TimeoutSec, [int]$WaitSec, [string]$NotAfterUtc = '') {
    # One job through um-run.ps1 (the only writer to a venue share). RECEIPT | RETRACTED | UNRESOLVED | ERROR; a thrown RETRACTED / UNRESOLVED is data here.
    # r4: -NotAfterUtc (the open only) goes to um-run.ps1 -NotAfterUtc, meta.json notAfterUtc: the agent takes no lease for the job past it.
    $f = Join-Path $jobDir "$JobId.job.ps1"
    [IO.File]::WriteAllText($f, $Body + "`r`n", [Text.Encoding]::ASCII)
    $bound = @{}
    if ($NotAfterUtc) { $bound.NotAfterUtc = $NotAfterUtc }
    try {
        $o = @(& $UmRunScript -ScriptPath $f -JobId $JobId -AgentShare $AgentShare -TimeoutSec $TimeoutSec -MaxQueueWaitSec $WaitSec -MaxClaimedWaitSec 300 -MaxAdmissionWaitSec $WaitSec @bound 6>$null)
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
        # PowerShell 7 ConvertFrom-Json hands a "...Z" string back as a DateTime of Kind Utc; [string] drops the Kind and Parse then assumes THIS host's offset
        # (a Central host put every lease end 5 h early). A DateTime converts by its Kind (Unspecified is read as UTC, like the string with no zone); a string
        # is read as UTC unless it names an offset.
        $er = $Result.endedUtc
        $edt = $null
        if ($er -is [DateTime]) { $edt = $(if ($er.Kind -eq [DateTimeKind]::Unspecified) { [DateTime]::SpecifyKind($er, [DateTimeKind]::Utc) } else { $er.ToUniversalTime() }) }
        elseif ($er -is [DateTimeOffset]) { $edt = $er.UtcDateTime }
        else {
            $o = [DateTimeOffset]::MinValue
            if ([DateTimeOffset]::TryParse([string]$er, [Globalization.CultureInfo]::InvariantCulture, ([Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal), [ref]$o)) { $edt = $o.UtcDateTime }
        }
        if ($null -eq $edt) { return $null }
        $ended = [DateTimeOffset]::new($edt).ToUnixTimeMilliseconds() / 1000.0
        [DateTime]::UtcNow.AddSeconds([double]$lb.untilEpoch - $ended)
    } catch { $null }
}

$pending = [System.Collections.Generic.List[object]]::new()
foreach ($p in $plan) { $pending.Add($p) }
# r5 (V-01, Invoke-VenueChain's r4 rule): leg name -> the first FOREIGN_FLOOR line any probe of that leg printed, in any attempt or batch. Sticky: a leg
# once seen contended is COOLDOWN_UNMET (uncounted) whatever a later QUIET probe reads; it never carries into the next leg.
$legForeign = @{}
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
# BACHELOR-DEDICATED-CAPS-RUNNER-1: the caps are Bachelor's heavy-slot.lease.json (the owner moves them), so they are READ, not mirrored: one light exempt job
# (GateExemptJobPattern ^venue-quiet-probe-: no lease, no slot, no venue claim) prints the file; anything unreadable or invalid is the old constants (the tightest ever
# set), typed capsSource=fallback.
$capsProbeId = New-VenueCapsProbeId
$capsProbe = Submit-AgentJob $capsProbeId ("`$p = 'C:\Users\layib\.claude\ops\heavy-slot.lease.json'`r`nif (Test-Path -LiteralPath `$p -PathType Leaf) { [IO.File]::ReadAllText(`$p) } else { 'HEAVY_SLOT_LEASE_JSON_MISSING' }") 60 180
$capsText = ''; $capsWhy = $null
if ($capsProbe.outcome -cne 'RECEIPT' -or $null -eq $capsProbe.result) { $capsWhy = "probe-$($capsProbe.outcome)" }
elseif ($capsProbe.result.exitCode -ne 0) { $capsWhy = "probe-exit-$($capsProbe.result.exitCode)" }
else { $capsText = (@($capsProbe.result.stdout) -join "`n").Trim() }
$caps = Get-VenueLeaseCaps -Text $capsText -WindowSec $LeaseBudgetWindowSec
if ($capsWhy) { $caps = [pscustomobject]@{ source = 'fallback'; reason = $capsWhy; cooldownSec = 1800; budgetSec = 5400; capSec = 2700 } }
$eff = Resolve-VenueLeaseCaps -Caps $caps -CooldownSec $LeaseCooldownSec -CooldownExplicit $cooldownExplicit -BudgetSec $LeaseBudgetSec -BudgetExplicit $budgetExplicit -CapSec $LeaseCapSec -AllowRelax (-not $umRunIsReal)
if (-not $eff.ok) { Stop-Usage $eff.error }
if ($eff.capSec -lt 60) { Stop-Usage "the lease cap is $($eff.capSec) s (caps source=$($caps.source)): no leg fits in a lease" }
if ($eff.budgetSec -lt $eff.capSec) { Stop-Usage "the lease budget $($eff.budgetSec) s is under the lease cap $($eff.capSec) s: no lease could ever open (caps source=$($caps.source))" }
$LeaseCooldownSec = $eff.cooldownSec; $LeaseBudgetSec = $eff.budgetSec; $LeaseCapSec = $eff.capSec
$capsReceipt = [ordered]@{ capsSource = $caps.source; capsCooldownSec = $LeaseCooldownSec; capsBudgetSec = $LeaseBudgetSec; capsCapSec = $LeaseCapSec; capsFallbackReason = $caps.reason; capsProbeJobId = $capsProbeId }
L "CAPS source=$($caps.source) cooldownSec=$LeaseCooldownSec budgetSec=$LeaseBudgetSec capSec=$LeaseCapSec windowSec=$LeaseBudgetWindowSec probeJob=$capsProbeId$(if ($caps.reason) { " fallbackReason=$($caps.reason)" }) (Bachelor's heavy-slot.lease.json$(if ($caps.source -ceq 'fallback') { ' unread: the old constants apply' }))"
# Every lease a lease-batch run on this host took on this venue in the last budget window plus cooldown, read once at the run's start (CAPS b and c).
$leases = [System.Collections.Generic.List[object]]::new()
foreach ($l in @(Read-VenueLeaseLedger -Venue $Venue -ClaimDir $ClaimDir -SinceUtc $t0.AddSeconds(-($LeaseBudgetWindowSec + $LeaseCooldownSec)))) { $leases.Add($l) }
function Set-Stop([string]$Reason, [int]$Code) { $script:stopReason = $Reason; if ($script:exitCode -eq 0 -or $script:exitCode -eq 7) { $script:exitCode = $Code } }
function Add-Result([object]$Leg, [string]$Status, [hashtable]$Extra) {
    $r = [ordered]@{ name = $Leg.name; spec = $Leg.spec; backend = $Leg.backend; status = $Status }
    if ($legPlanMode) { $r.arm = $Leg.arm; $r.sourceCommit = $Leg.sourceCommit; $r.buildManifestSha256 = $Leg.buildManifestSha256 }
    foreach ($k in $Extra.Keys) { $r[$k] = $Extra[$k] }
    $results.Add([pscustomobject]$r)
}

L "BATCH_RUN_START venue=$Venue legs=$($plan.Count) runDir=$RunDir agentShare=$AgentShare deadline=$($deadline.ToString('o')) threshold=$ThresholdPercent legBudgetSec=$LegBudgetSec leaseMarginSec=$LeaseMarginSec recheckSec=$RecheckSec cooldownSec=$CooldownSec pid=$PID quietScript=$QuietScript legScript=$LegScript umRunScript=$UmRunScript$(if ($legPlanMode) { " legPlan=$($planRecord.legPlan) planOrder=$PlanOrder arms=$($planRecord.arms -join ',') builds=$(@($planRecord.builds).Count)" })"

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
        openExit75Refusals = 0; legExit75Refusals = 0; openCancel = $null; openNotAfterUtc = $null; openLiveness = $null; openLivenessReadback = $null; claimRenew = $null
    }
    foreach ($ck in $capsReceipt.Keys) { $b[$ck] = $capsReceipt[$ck] }
    # (0) CAPS b and c: wait out the cooldown after the last lease and the 3 h budget BEFORE the claim, so the venue is never held through either.
    $bw = Get-VenueLeaseBudgetWait -Leases @($leases) -NowUtc ([DateTime]::UtcNow) -CapSec $LeaseCapSec -CooldownSec $LeaseCooldownSec -WindowSec $LeaseBudgetWindowSec -BudgetSec $LeaseBudgetSec
    $b.preOpenWait = [ordered]@{ reason = $bw.reason; waitSec = $bw.waitSec; usedSec = $bw.usedSec; budgetRoomSec = $bw.budgetRoomSec; openUtc = $bw.openUtc.ToString('o'); lastLeaseEndUtc = $(if ($bw.lastEndUtc) { $bw.lastEndUtc.ToString('o') }) }
    if ($bw.waitSec -gt 0) {
        if ($bw.openUtc -ge $deadline) {
            $b.endReason = 'DEADLINE'; $b.endDetail = "lease-$($bw.reason.ToLowerInvariant())-wait: the next lease may open at $($bw.openUtc.ToString('o')) (usedSec=$($bw.usedSec) in the 3 h window), after the deadline"
            Set-Stop 'DEADLINE' 3
            LogBest "BATCH_END batch=$batchNo token=$token reason=DEADLINE detail=$($b.endDetail)"   # r7 (sol r6b): the line is tried before the receipt snapshots logWriteFailures
            $b.logWriteFailures = $script:logWriteFailures; Write-BatchReceipt $b
            break
        }
        L "BATCH_WAIT batch=$batchNo reason=$($bw.reason) waitSec=$($bw.waitSec) usedSec=$($bw.usedSec) capSec=$LeaseCapSec budgetSec=$LeaseBudgetSec windowSec=$LeaseBudgetWindowSec cooldownSec=$LeaseCooldownSec (Bachelor's lease caps; not a failure, no claim held)"
        Start-Sleep -Seconds $bw.waitSec
    }
    # (1) the claim, once per batch. A LIVE claim of another chain is waited out; stale claims are broken once each.
    $claim = $null; $lastHolder = ''
    while ($true) {
        $req = Request-VenueClaim -Venue $Venue -RunDir $RunDir -Purpose "$Purpose batch $batchNo" -TtlSec $ClaimTtlSec -ClaimDir $ClaimDir
        foreach ($bk in $req.broken) { LogBest "CLAIM_STALE_BROKEN venue=$Venue reason=$($bk.reason) ownerPid=$($bk.claim.ownerPid) runDir=$($bk.claim.runDir) expiresUtc=$($bk.claim.expiresUtc)" }
        if ($req.granted) { $claim = $req.claim; break }
        $holder = $(if ($req.claim) { "pid=$($req.claim.ownerPid) runDir=$($req.claim.runDir) purpose=$($req.claim.purpose)" } else { $req.state })
        if ($holder -cne $lastHolder) { L "CLAIM_HELD batch=$batchNo state=$($req.state) holder $holder -> wait"; $lastHolder = $holder }
        if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -ge $deadline) { break }
        Start-Sleep -Seconds $ClaimPollSec
    }
    if ($null -eq $claim) {
        $b.endReason = 'DEADLINE'; $b.endDetail = 'claim-held'; Set-Stop 'DEADLINE' 3
        LogBest "BATCH_END batch=$batchNo token=$token reason=DEADLINE detail=claim-held"   # r7 (sol r6b): the line is tried before the receipt snapshots logWriteFailures
        $b.logWriteFailures = $script:logWriteFailures; Write-BatchReceipt $b
        break
    }
    $b.claimNonce = $claim.nonce
    # r5 (dot 242171b3 B1): the claim's expiry is renewed for as long as the batch holds it (the open's wait for the lease, the by-name wait and the legs can all
    # outlast -ClaimTtlSec), so Get-VenueClaimStaleReason never finds a live owner's claim expired. The keeper dies with this process.
    $claimKeeper = Start-VenueClaimKeeper -Venue $Venue -Nonce $claim.nonce -TtlSec $ClaimTtlSec -RenewSec $ClaimRenewSec -ClaimDir $ClaimDir
    # r3 (Dot B1): $openSubmitted is true from the moment Submit-AgentJob is called for the open. The finally block's cancel, close and ledger charge follow
    # that flag, never "$op is null": a null $op before the call (a throw while the sidecar was checked) means nothing was sent.
    $op = $null; $leaseStartUtc = $null; $openSubmitUtc = $null; $openLive = $null; $cr = $null; $openSubmitted = $false; $inFlightLeg = $null
    # r5 (key3 F1): the try opens IMMEDIATELY after the keeper starts, so every throw from here on ends with the keeper stopped, the claim released and the batch
    # receipt written (the finally), and as a typed exit 12 (the catch). A throw outside it left the keeper runspace renewing the claim of a process that had died.
    try {
        L "CLAIM_TAKEN batch=$batchNo nonce=$($claim.nonce) expiresUtc=$($claim.expiresUtc) renewSec=$ClaimRenewSec ttlSec=$ClaimTtlSec"
        # sol r3 second pass A3: the validated plan is kept only while this run holds the venue claim, so a second invocation on the same -RunDir that is still waiting
        # for the claim cannot replace the plan a running one preserved. Rewritten after each claim taken: it is always the holder's own plan.
        if ($legPlanMode) { [IO.File]::WriteAllText((Join-Path $RunDir 'leg-plan.json'), ($planRecord | ConvertTo-Json -Depth 6) + "`n", [Text.UTF8Encoding]::new($false)) }
        # (2) OPEN: the lease first. Nothing is probed or run before the agent reports this batch's lease active.
        $b.openJobId = "lease-batch-open-$stamp-lb$token"
        $wait = [int][Math]::Max(60, [Math]::Min($OpenAdmissionWaitSec, ($deadline - [DateTime]::UtcNow).TotalSeconds))
        L "BATCH_OPEN_SUBMIT batch=$batchNo token=$token job=$($b.openJobId) admissionWaitSec=$wait (the agent requests the lease; heavy-slot.py's cooldown is waited out there)"
        $openSubmitUtc = [DateTime]::UtcNow
        # r4 (sol r3 B1): the open carries the batch deadline as notAfterUtc, so no lease is taken for it after the deadline, whether or not the agent answers
        # the cancel below. The close job carries none: it must still run after the deadline.
        $b.openNotAfterUtc = $deadline.ToString('o')
        # r5 (sol B1 on early runner exit, #663): while this run owns the open, its runner-liveness sidecar (<share>\notafter\<openJobId>.json) is kept
        # -LivenessWindowSec ahead. Killed, this run stops renewing it, and the agent ends the open's gate wait and releases its lease within that window.
        $openLive = Start-VenueJobLiveness -AgentShare $AgentShare -JobId $b.openJobId -WindowSec $LivenessWindowSec -RenewSec $LivenessRenewSec
        L "BATCH_OPEN_LIVENESS batch=$batchNo job=$($b.openJobId) first=$($openLive.first.state) aliveUntilUtc=$($openLive.first.aliveUntilUtc) windowSec=$LivenessWindowSec renewSec=$LivenessRenewSec$(if ($openLive.first.error) { " error=$($openLive.first.error)" })"
        # r6 (Grok G2 on #633): WRITTEN only says the write returned. The file is read back with the agent's own reader and verdict; one the agent would not read as live is no sidecar.
        $rb = $null; $crossed = $false
        if ($openLive.first.state -ceq 'WRITTEN') {
            $rb = Test-VenueJobLivenessReadback -AgentShare $AgentShare -JobId $b.openJobId -NotAfterUtc $b.openNotAfterUtc
            $b.openLivenessReadback = $rb
            L "BATCH_OPEN_LIVENESS_READBACK batch=$batchNo job=$($b.openJobId) live=$($rb.live) livenessState=$($rb.livenessState) why=$($rb.why)"
            # r2 (opus H1): a LIVE sidecar under a job whose own notAfterUtc (the batch deadline) has passed reads state=passed. The sidecar is fine; the deadline crossed between
            # the check above and this read-back, so the batch ends DEADLINE (exit 3) like every other deadline stop, not as a liveness failure.
            $crossed = [string]$rb.livenessState -ceq 'live' -and [string]$rb.state -ceq 'passed'
        }
        if ($openLive.first.state -cne 'WRITTEN' -or -not $rb.live) {
            # r5b (dot ef5805a4 residual B1): no sidecar, no open. Submitted without one, an open this run dies under stays admissible until the far batch
            # deadline, not -LivenessWindowSec. The keeper is stopped and the open is never submitted: the batch ends LIVENESS_UNAVAILABLE (exit 9, below).
            $b.openLiveness = Stop-VenueJobLiveness $openLive -Remove
            $op = [pscustomobject]@{ outcome = 'NOT_SUBMITTED'; result = $null; message = "first liveness write $($openLive.first.state)$(if ($openLive.first.error) { ": $($openLive.first.error)" })$(if ($crossed) { ", but the batch deadline passed first (the sidecar is live; read-back: $($rb.why))" } elseif ($null -ne $rb -and -not $rb.live) { ", but the agent's own parser does not read it as live (read-back $($rb.livenessState)): $($rb.why)" })" }
        } else {
            $openSubmitted = $true
            $op = Submit-AgentJob $b.openJobId "Write-Output ('LEASE_BATCH_OPEN token=$token held=' + [string]`$env:HEAVY_SLOT_HELD + ' admission=' + [string]`$env:MLV_AGENT_ADMISSION)" 120 $wait $b.openNotAfterUtc
        }
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
        # r5: the open's result is in hand (or it was retracted before the agent saw it): this run no longer owns it, so its sidecar goes. Otherwise it is kept
        # renewed until the end of the batch cancels and reconciles the open.
        if ($op.outcome -ceq 'RECEIPT' -or $op.outcome -ceq 'RECEIPT_BY_NAME' -or $op.outcome -ceq 'RETRACTED') { $b.openLiveness = Stop-VenueJobLiveness $openLive -Remove }
        $b.openOutcome = $op.outcome
        $b.openExit75Refusals = $(if ($null -ne $adm) { @(@($adm.leaseRefusals) | Where-Object { $null -ne $_ -and [string]$_.rc -eq '75' }).Count } else { 0 })
        $b.exit75Refusals = $b.openExit75Refusals
        if ($b.exit75Refusals -gt 0 -or $b.openLeaseWait -cne 'NONE') { $b.exit75Class = 'WAIT_RETRY' }
        if ($op.outcome -ceq 'NOT_SUBMITTED' -and $crossed) {
            $b.endReason = 'DEADLINE'; $b.endDetail = "$($op.message) (the open was not submitted: nothing ran, nothing counted)"
            Set-Stop 'DEADLINE' 3
        } elseif ($op.outcome -ceq 'NOT_SUBMITTED') {
            $b.endReason = 'LIVENESS_UNAVAILABLE'; $b.endDetail = "$($op.message) (the open was not submitted: nothing ran, nothing counted)"
            Set-Stop 'LIVENESS_UNAVAILABLE' 9
        } elseif ($op.outcome -ceq 'UNRESOLVED' -and $b.openLeaseWait -cne 'NONE') {
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
                $leg = $pending[0]; $inFlightLeg = $null
                if ([DateTime]::UtcNow -ge $deadline) { $b.endReason = 'DEADLINE'; Set-Stop 'DEADLINE' 3; break }
                # r5b (Grok G1): no probe and no leg without the venue claim. Lost (NOT_OURS / ABSENT) or past its last good expiry, the batch ends CLAIM_LOST
                # (uncounted); the legs already run stay as measured and the rest go to the next batch, which takes the claim again.
                $cl = Get-VenueClaimKeeperLoss -Keeper $claimKeeper -ClaimExpiresUtc $claim.expiresUtc
                if ($cl) { $b.endReason = 'CLAIM_LOST'; $b.endDetail = "before the probe of leg $($leg.name): $($cl.detail)"; L "CLAIM_LOST batch=$batchNo leg=$($leg.name) phase=probe $($cl.detail)"; break }
                $room = [int](($leaseEnd - [DateTime]::UtcNow).TotalSeconds) - $LeaseMarginSec
                if ($room -lt $legBudget) {
                    $b.endReason = 'LEASE_REMAINING_SHORT'; $b.endDetail = "roomSec=$room < legBudgetSec=$legBudget (leaseMarginSec=$LeaseMarginSec): leg $($leg.name) goes to the next batch"
                    break
                }
                $attempt++
                $probeWait = [int][Math]::Max(0, [Math]::Min($ProbeMaxWaitSec, $room - $legBudget))
                $q = Invoke-QuietProbe $leg.name "b$('{0:00}' -f $batchNo)-a$attempt" $probeWait
                if ($q.foreignFloor -and -not $legForeign.ContainsKey($leg.name)) { $legForeign[$leg.name] = $q.foreignFloorLine }
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
                # QUIET under the lease: the leg, on the same lease (r5b, Grok G1: while the claim is still this run's).
                $cl = Get-VenueClaimKeeperLoss -Keeper $claimKeeper -ClaimExpiresUtc $claim.expiresUtc
                if ($cl) { $b.endReason = 'CLAIM_LOST'; $b.endDetail = "before leg $($leg.name): $($cl.detail)"; L "CLAIM_LOST batch=$batchNo leg=$($leg.name) phase=leg $($cl.detail)"; break }
                $b.legsAttempted++
                $out = Join-Path $RunDir "leg-$($leg.name)-b$('{0:00}' -f $batchNo)-a$attempt.out.txt"
                $lArgs = @('-NoProfile', '-NonInteractive', '-File', $LegScript, '-Venue', $Venue, '-LegSpec', $leg.spec, '-SourceCommit', $leg.sourceCommit, '-BuildManifestSha256', $leg.buildManifestSha256, '-LeaseBatch', $token)
                if ($leg.backend) { $lArgs += @('-Backend', $leg.backend) }
                if ($SheetCopyDir) { $lArgs += @('-SheetCopyDir', $SheetCopyDir) }
                L "LEG $($leg.name) RUN batch=$batchNo lease=$($b.leaseId) quietMean=$(Format-Mean $q.mean) roomSec=$room legBudgetSec=$legBudget spec=$($leg.spec) backend=$(if ($leg.backend) { $leg.backend } else { 'spec' })$(if ($legPlanMode) { " arm=$($leg.arm) sourceCommit=$($leg.sourceCommit) buildManifestSha256=$($leg.buildManifestSha256)" })"
                $tLeg = [DateTime]::UtcNow
                # r3 (sol r2 B2): the leg runs only while the lease does. It is a bounded child: still running at the lease's end, it is killed with its whole
                # process tree (Kill($true), Invoke-VenueBoundedProcess) and typed LEASE_EXPIRED below. The agent stops its job at the same bound (lease-batch r3).
                $legBoundSec = [int][Math]::Max(1, [Math]::Floor(($leaseEnd - [DateTime]::UtcNow).TotalSeconds))
                # adversary r7 A2: Process.Start failing (a command line past 32767 characters, a missing exe) throws a Win32Exception before any child exists: the leg stays NOT_RUN.
                $inFlightLeg = $leg   # adversary r5 pass 2 A2: marked at the launch itself, so a throw while the RUN line was logged leaves a leg that never started NOT_RUN
                try { $lp = Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $lArgs -TimeoutSec $legBoundSec } catch {
                    if ($_.Exception -is [ComponentModel.Win32Exception] -or $_.Exception.InnerException -is [ComponentModel.Win32Exception]) { $inFlightLeg = $null }
                    throw
                }
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
                    Add-PlanIdentity $b.legs[$b.legs.Count - 1] $leg
                    $b.endReason = 'LEASE_EXPIRED'; $b.endDetail = "leg $($leg.name) was still running at the lease's end ($legSec s, bound $legBoundSec s): killed with its process tree; job $legJobId cancel=$($cx.state)$(if ($cx.error) { " ($($cx.error))" })"
                    L "LEG $($leg.name) LEASE_EXPIRED batch=$batchNo legSec=$legSec boundSec=$legBoundSec outcome=INVALID counted=False jobId=$legJobId cancel=$($cx.state) (killed with its process tree at the lease's end)"
                    if ($exitCode -eq 0) { $exitCode = 7 }
                    break
                }
                if ($legCode -ne 0) {
                    # Invoke-VenueLeg exits 0 whenever it wrote a receipt; non-zero means it died without one. Not evidence: LEG_FAILED, the run goes on.
                    $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'LEG_FAILED'; outcome = 'INVALID'; printedOutcome = $outcome; legExit = $legCode; jobId = $legJobId; counted = $false; legSec = $legSec }
                    $b.legs.Add([pscustomobject]$row); Add-Result $leg 'LEG_FAILED' $row; $pending.RemoveAt(0)
                    Add-PlanIdentity $b.legs[$b.legs.Count - 1] $leg
                    L "LEG $($leg.name) LEG_FAILED batch=$batchNo exit=$legCode outcome=INVALID printedOutcome=$outcome (the leg exited non-zero: no usable receipt)"
                    if ($exitCode -eq 0) { $exitCode = 7 }
                    continue
                }
                # Did it run on THIS batch's lease? Read by name, bounded.
                $jr = Get-VenueJobResult -AgentShare $AgentShare -JobId $legJobId
                # r5b (hub 13:21Z B2 clause, dot 242171b3): no agent result for the leg's job (um-run said UNRESOLVED, um-run.ps1:480-501, or the read failed) means
                # the agent may still hold it. It is WITHDRAWN (its cancel file) before the batch goes on or closes, never left in place for a parked lease to launch.
                $withdraw = $null
                if ($jr.state -cne 'FOUND') {
                    $wx = $(if ($legJobId) { Request-VenueJobCancel -AgentShare $AgentShare -JobId $legJobId } else { [pscustomobject]@{ state = 'UNKNOWN'; error = 'the leg printed no job id' } })
                    $withdraw = $wx.state
                    L "LEG_WITHDRAWN leg=$($leg.name) batch=$batchNo jobId=$legJobId result=$($jr.state) printedOutcome=$outcome cancel=$($wx.state)$(if ($wx.error) { " error=$($wx.error)" })"
                }
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
                $leaseWhy = "jobId=$legJobId result=$($jr.state)$(if ($withdraw) { " withdraw=$withdraw" }) jobLease=$jobLease slotContinuity=$(if ($ja) { $ja.slotContinuity }) renewOkMaxGapSec=$(if ($null -ne $gap) { $gap } else { 'UNKNOWN' })$(if ($agentStop) { " leaseStop=$agentStop" })$(if ($jr.error) { " error=$($jr.error)" })"
                # r3 (fable r2): a leg whose job took a fresh lease under this token carries that request's exit-75 refusals; the batch receipt counts them too.
                $legRef = $(if ($null -ne $ja) { @(@($ja.leaseRefusals) | Where-Object { $null -ne $_ -and [string]$_.rc -eq '75' }).Count } else { 0 })
                if ($legRef -gt 0) { $b.legExit75Refusals += $legRef; $b.exit75Refusals += $legRef; $b.exit75Class = 'WAIT_RETRY' }
                if ($outcome -ceq 'VENUE_NOT_QUIESCENT') {
                    # The leg's own <= 20 % re-check at its start failed: it never played. Retry INSIDE the same lease (on a lost or demoted lease, in the next batch).
                    $b.retries++; $b.legRetries++
                    $b.legs.Add([pscustomobject][ordered]@{ batch = $batchNo; attempt = $attempt; status = 'RETRY'; outcome = $outcome; receipt = $receipt; jobId = $legJobId; jobLease = $jobLease; onLease = $onLease; renewOkMaxGapSec = $gap; counted = $false; legSec = $legSec; exit75Refusals = $legRef; withdraw = $withdraw })
                    Add-PlanIdentity $b.legs[$b.legs.Count - 1] $leg
                    L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=LEG_NOT_QUIESCENT receipt=$receipt onLease=$onLease $leaseWhy"
                    if ($leaseClass) { $b.endReason = $leaseClass; $b.endDetail = $leaseWhy; break }
                    continue
                }
                $preLoad = Get-VenueLegPreLoadMean -ReceiptPath $receipt
                # Only a QUIET probe reaches here, so the class is the lease, then the leg's sticky foreign floor (r5, V-01: a FOREIGN_FLOOR in ANY probe of this
                # leg, an earlier attempt or batch included, leaves it contended: COOLDOWN_UNMET, never counted), then the pre-load.
                $legFF = $legForeign.ContainsKey($leg.name)
                $class = $(if ($leaseClass) { $leaseClass } elseif ($legFF) { 'COOLDOWN_UNMET' } elseif ($null -eq $preLoad) { 'QUIET_PRELOAD_UNKNOWN' } elseif ($preLoad -le 30.0) { 'QUIET_PRELOAD_OK' } else { 'QUIET_PRELOAD_HIGH' })
                $counted = ($class -ceq 'QUIET_PRELOAD_OK')
                $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'RAN'; outcome = $outcome; class = $class; counted = $counted; quietVerdict = $q.verdict; quietMean = $q.mean; preLoadMean = $preLoad
                    receipt = $receipt; jobId = $legJobId; jobLease = $jobLease; onLease = $onLease; renewOkMaxGapSec = $gap; legSec = $legSec; legExit = $legCode; exit75Refusals = $legRef; foreignFloor = $legFF }
                if ($legFF) { $row['foreignFloorLine'] = $legForeign[$leg.name] }
                if ($detail) { $row['outcomeDetail'] = $detail }
                if ($agentStop) { $row['leaseStop'] = $agentStop }
                if ($withdraw) { $row['withdraw'] = $withdraw }
                $b.legs.Add([pscustomobject]$row); Add-Result $leg 'RAN' $row; $pending.RemoveAt(0)
                Add-PlanIdentity $b.legs[$b.legs.Count - 1] $leg
                $b.legsRan++; if ($counted) { $b.legsCounted++ }
                L "LEG $($leg.name) RAN batch=$batchNo exit=$legCode outcome=$outcome class=$class counted=$counted quietMean=$(Format-Mean $q.mean) preLoadMean=$(Format-Mean $preLoad) legSec=$legSec receipt=$receipt $leaseWhy$(if ($legFF) { ' foreignFloor=1' })"
                if ($leaseClass) { $b.endReason = $leaseClass; $b.endDetail = $leaseWhy; break }
                if ($pending.Count -gt 0 -and $CooldownSec -gt 0) { L "COOLDOWN ${CooldownSec} s (inside the lease)"; Start-Sleep -Seconds $CooldownSec }
            }
            if ($null -eq $b.endReason -and $pending.Count -eq 0) { $b.endReason = 'ALL_LEGS_DONE' }
            # A FRESH lease with no room for even one leg can never run it: stop rather than open lease after lease.
            if ($b.endReason -ceq 'LEASE_REMAINING_SHORT' -and $b.legsAttempted -eq 0 -and $b.probeRetries -eq 0) {
                $b.endReason = 'LEG_BUDGET_EXCEEDS_LEASE'; Set-Stop 'LEG_BUDGET_EXCEEDS_LEASE' 2
            }
        }
    } catch {
        # r5 (key3 F1): an unexpected throw anywhere in the batch is a typed stop, not an unhandled exit 1 that skips the summary. The finally below runs first; 12 replaces every code.
        $b.endReason = 'BATCH_ERROR'; $b.endDetail = ($_.Exception.Message -replace '\s+', ' ')
        $script:stopReason = 'BATCH_ERROR'; $script:exitCode = 12
        # adversary r5 A1: a leg whose child had started but whose result was not recorded yet RAN (or may have): LEG_FAILED / reason BATCH_ERROR, outcome unknown,
        # uncounted, never NOT_RUN. Only the legs never started stay pending for the NOT_RUN loop below.
        try {
            if ($null -ne $inFlightLeg -and $pending.Contains($inFlightLeg)) {
                $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'LEG_FAILED'; outcome = 'INVALID'; reason = 'BATCH_ERROR'; detail = $b.endDetail; counted = $false }
                $b.legs.Add([pscustomobject]$row); Add-Result $inFlightLeg 'LEG_FAILED' $row; $null = $pending.Remove($inFlightLeg)
                Add-PlanIdentity $b.legs[$b.legs.Count - 1] $inFlightLeg
            }
        } catch { }
        LogBest "BATCH_ERROR batch=$batchNo $($b.endDetail)"   # a batch.log another process holds must not turn the typed stop back into an unhandled throw
    } finally {
        # (4) END: release the lease now (a light close job), read the agent's receipt for it, release the claim, write the batch receipt.
        # r3 (sol r2 B1): an open with no result in hand (um-run gave up on it, the deadline came while its result was awaited, or this batch threw) may still
        # be in the agent's admission gate, re-asking for a lease. It is CANCELLED and reconciled BY NAME first, so it can never take a lease once this runner
        # lets go: the agent reads the cancel file at the top of every gate loop, before any request. A RETRACTED open never reached the agent.
        # r5b: an open never submitted (NOT_SUBMITTED) has nothing to cancel and no lease to close. r3: "never submitted" is the $openSubmitted flag, so a throw before
        # the call (op still null) is also never sent, while a throw or timeout inside the call (flag set, op null or not) stays a maybe-sent open.
        $openSent = ($b.openJobId -and $openSubmitted)
        if ($openSent -and ($null -eq $op -or ($op.outcome -cne 'RECEIPT' -and $op.outcome -cne 'RECEIPT_BY_NAME' -and $op.outcome -cne 'RETRACTED'))) {
            $cx = Request-VenueJobCancel -AgentShare $AgentShare -JobId $b.openJobId
            LogBest "BATCH_OPEN_CANCEL batch=$batchNo job=$($b.openJobId) requested=$($cx.state)$(if ($cx.error) { " error=$($cx.error)" }) (read by name for up to ${CancelReconcileSec}s before the lease is closed and the claim released)"
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
            LogBest "BATCH_OPEN_RECONCILED batch=$batchNo state=$cState result=$($cr.state) label=$($b.openCancel.resultLabel) requested=$($cx.state) waitedSec=$($b.openCancel.waitedSec)"
            if ($cState -ceq 'UNRECONCILED') { $stopReason = 'OPEN_CANCEL_UNRECONCILED'; $exitCode = 11 }
        }
        # r5: this run lets go of the open here. Reconciled: its sidecar goes. Unreconciled (or the batch threw first): it is rewritten lapsed and left, so an agent
        # still holding the open ends it at its next check. Idempotent when the open's result was in hand already.
        if ($null -ne $openLive) {
            $b.openLiveness = Stop-VenueJobLiveness $openLive -Remove:($null -ne $cr -and $cr.state -ceq 'FOUND')
            LogBest "BATCH_OPEN_LIVENESS_END batch=$batchNo job=$($b.openJobId) end=$($b.openLiveness.end) writes=$($b.openLiveness.writes) writeFailures=$($b.openLiveness.writeFailures) lastAliveUntilUtc=$($b.openLiveness.lastAliveUntilUtc)"
        }
        if ($openSent) {
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
                try { Add-VenueLeaseLedger -Venue $Venue -ClaimDir $ClaimDir -Row ([pscustomobject]$row) } catch { LogBest "LEDGER_WRITE_FAILED batch=$batchNo $($_.Exception.Message)" }
                $leases.Add([pscustomobject]@{ startUtc = $row.startUtc; endUtc = $row.endUtc; leaseId = $row.leaseId })
            }
        }
        $ckInTime = Stop-VenueKeeper -Keeper $claimKeeper
        $cks = $claimKeeper.state
        $b.claimRenew = [ordered]@{ renewSec = $ClaimRenewSec; ttlSec = $ClaimTtlSec; renews = $cks.renews; errors = $cks.errors; lastError = $cks.lastError; lost = $cks.lost; lastExpiresUtc = $cks.expiresUtc; stoppedInTime = $ckInTime }
        LogBest "CLAIM_RENEW batch=$batchNo renews=$($cks.renews) errors=$($cks.errors) lost=$(if ($cks.lost) { $cks.lost } else { 'no' }) lastExpiresUtc=$($cks.expiresUtc)"
        # r6 (Dot r5 B5): the release no longer follows a log call that can throw, and a release that throws is a result, not a skipped receipt.
        $relResult = try { Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir } catch { "ERROR: $($_.Exception.Message -replace '\s+', ' ')" }
        LogBest "CLAIM_RELEASED batch=$batchNo result=$relResult"
        # r7 (sol r6b): BATCH_END is logged BEFORE the receipt snapshots logWriteFailures, so a failure logging it is counted in the receipt written for that batch.
        LogBest "BATCH_END batch=$batchNo token=$token lease=$($b.leaseId) reason=$($b.endReason) attempted=$($b.legsAttempted) ran=$($b.legsRan) counted=$($b.legsCounted) retries=$($b.retries) probeRetries=$($b.probeRetries) legRetries=$($b.legRetries) refusals=$(@($b.refusals).Count) exit75=$($b.exit75Refusals)/$($b.exit75Class) renewOkMaxGapSec=$($b.renewOkMaxGapSec) preOpenWait=$($b.preOpenWait.reason):$($b.preOpenWait.waitSec)s close=$($b.closeOutcome) agentEnd=$(if ($b.agent) { $b.agent.endReason })$(if ($b.endDetail) { " detail=$($b.endDetail)" })"
        $b.logWriteFailures = $script:logWriteFailures
        Write-BatchReceipt $b
    }
}
foreach ($leg in $pending) { Add-Result $leg 'NOT_RUN' @{ verdict = $(if ($stopReason) { $stopReason } else { 'NOT_RUN' }); reason = 'run-stopped' }; LogBest "LEG $($leg.name) NOT_RUN $stopReason reason=run-stopped" }

$summary = [ordered]@{
    schema = 'mlv-app/venue-lease-batch-result/v1'; venue = $Venue; runDir = $RunDir; startedUtc = $t0.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o')
    exitCode = $exitCode; stopReason = $stopReason; capsSource = $capsReceipt.capsSource; logWriteFailures = $script:logWriteFailures; legs = @($results)
    ran = @($results | Where-Object { $_.status -ceq 'RAN' }).Count; counted = @($results | Where-Object { $_.status -ceq 'RAN' -and $_.counted }).Count
    failed = @($results | Where-Object { $_.status -ceq 'LEG_FAILED' }).Count; notRun = @($results | Where-Object { $_.status -ceq 'NOT_RUN' }).Count
    leaseExpired = @($results | Where-Object { $_.status -ceq 'LEASE_EXPIRED' }).Count
    batches = @($batches | ForEach-Object { [ordered]@{ batch = $_.batch; token = $_.token; leaseId = $_.leaseId; endReason = $_.endReason; legsAttempted = $_.legsAttempted; legsRan = $_.legsRan; legsCounted = $_.legsCounted; retries = $_.retries } })
}
if ($legPlanMode) { $summary.legPlan = [ordered]@{ path = $planRecord.legPlan; order = $PlanOrder; arms = $planRecord.arms; builds = @($planRecord.builds | ForEach-Object { $_.sourceCommit }) } }
$resultLine = 'LEASE_BATCH_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
try { Add-Content -LiteralPath $batchLog -Value $resultLine } catch {   # r6 (Dot r5 B5; adversary A3): the result line on stdout and the typed exit code never depend on batch.log, and the failure is still said on stderr
    # r7 (sol r6b): the count was serialized before this append, so the append that just failed is counted and the line for stdout is serialized again with it (stdout is the authority when batch.log is broken).
    Register-LogFailure $_.Exception.Message
    $summary.logWriteFailures = $script:logWriteFailures
    $resultLine = 'LEASE_BATCH_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
}
Write-Output $resultLine
Complete-VenueProcess -ExitCode $exitCode   # a share read abandoned at its timeout would otherwise keep this process alive until it ends
exit $exitCode
