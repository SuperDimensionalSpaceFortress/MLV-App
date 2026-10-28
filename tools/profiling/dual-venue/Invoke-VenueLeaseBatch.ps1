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
# UNRESOLVED or not admitted, or never submitted because its sidecar could not be written: LIVENESS_UNAVAILABLE, or its job file could not be written here:
# OPEN_JOB_WRITE_FAILED, no cancel, no close, no ledger charge); 10 the agent is not batch-capable; 11 an open left in the agent's gate was cancelled but never reconciled (OPEN_CANCEL_UNRECONCILED,
# 7a); 12 an unexpected throw inside a batch after its claim was taken (BATCH_ERROR: the claim keeper is stopped, the claim released and the batch receipt written
# first; a leg whose child had started is LEG_FAILED reason=BATCH_ERROR, the legs never started are NOT_RUN). A stopping code (2-6, 9, 10) replaces 0 or 7; 12 replaces every other code but 11, and 11 replaces every other code.
# 13 a receipt file could not be written (batch-NN.json or batches.jsonl, e.g. occupied by a directory): RECEIPT_PERSIST_FAILED file=<path> reason=<msg> is logged once per failure, the summary still prints with
# receiptPersistFailures, and every other code keeps its own meaning (13 replaces only a clean 0), so a lost receipt is never an exit 0. Each receipt is serialized with the live logWriteFailures (the diagnostic itself can fail to log).
# r5: a receipt write the input guard skipped (an accepted input is a runner output) is a receiptPersistFailures entry too (reason WRITE_SUPPRESSED <stop reason>), and a hit seen at a receipt
# write that no later claim guard consumed is the run's typed stop (exit 2) on the final batch. An input that can no longer be opened is INPUT_VANISHED / INPUT_UNREADABLE (exit 2), never an alias.
# Once a batch receipt exists, every end prints LEASE_BATCH_RESULT: a log line between batches is best effort, and an input refused at a later claim (exit 2) prints the summary first.
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

$script:summaryBeforeExit = $null   # r3: set once a refusal ends a run that already has a batch receipt; the typed refusal prints the summary just before its exit 2
function Stop-Usage([string]$Message) { [Console]::Error.WriteLine("BATCH_USAGE $Message"); if ($script:summaryBeforeExit) { & $script:summaryBeforeExit }; exit 2 }

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

$script:RunOutputAliased = $false; $script:openAliasHit = $null; $script:suppressionHit = $null; $script:keeperStoppedEarly = $false; $script:keeperStoppedInTime = $null; $script:outputRedirect = $null
# r16 (sol r15 B1): the job ids um-run.ps1 was called for (Submit-AgentJob sets one just before the call), so a job whose file was never complete here is known never sent
$script:umRunCalled = @{}
function L([string]$Message) {
    $line = "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message"
    # r13 (adversary r13 pass 2 A1): once an accepted input was found to BE a runner output after the open, nothing more is written into -RunDir (batch.log may be that input); stderr carries the line
    if ($script:RunOutputAliased) { [Console]::Error.WriteLine($line); return }
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
    # r9: not for a refusal of an input that IS a runner output (it may be batch.log itself): the refusal must leave that file as it found it; stderr carries the line.
    # r16 (adversary r16 A1, the class): and not through a batch.log that is a link (more than one name, or a reparse point): its other name can be an input any refusal
    # names (a staged manifest refused for its digest), so only a plain one-name batch.log, or none yet, is appended to. One that cannot be read is not appended to either.
    $logPlain = $false
    try {
        if (-not (Test-Path -LiteralPath $batchLog)) { $logPlain = $true }
        elseif (-not ((Get-Item -LiteralPath $batchLog -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -and (Get-FileIdentity $batchLog).links -le 1) { $logPlain = $true }
    } catch { }
    if ($logPlain -and $Reason -cnotin @('LEG_SPEC_IS_RUN_OUTPUT', 'LEG_PLAN_IS_RUN_OUTPUT', 'BUILD_MANIFEST_IS_RUN_OUTPUT')) {
        # r3 pass 3 (adversary r3 A1): a failed append is counted when a summary follows (a refusal once a batch receipt exists), so the count that summary reports includes it
        try { Add-Content -LiteralPath $batchLog -Value "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $line" } catch { if ($script:summaryBeforeExit) { Register-LogFailure $_.Exception.Message } }
    }
    [Console]::Error.WriteLine("BATCH_USAGE $line")
    # r3 pass 3: the summary is the LAST thing this run writes, after the refusal line, so no append it reports on is still to come
    if ($script:summaryBeforeExit) { & $script:summaryBeforeExit }
    try { Complete-VenueProcess -ExitCode 2 } catch { }
    exit 2
}

# r9 (Sol r8 / Dot B7 / card LEASE-BATCH-RUNDIR-OUTPUT-COLLISION-GUARD-1): every file this runner writes under -RunDir, in ONE place. An input (a plan, a leg spec) that resolves to one
# of them would be overwritten or appended to after the lease opened, so preflight refuses it before any claim. A new output is added HERE and nowhere else: the files named
# directly in RunDir (leaf patterns, matched whole and case-insensitively) and the subdirectories the runner owns (anything under them).
$script:RunDirOutputLeafPatterns = @(
    '^quiet-.+\.out\.txt\z'     # Invoke-QuietProbe: each probe's output
    '^leg-.+\.out\.txt\z'       # the leg's output (leg-<name>-b<nn>-a<n>.out.txt)
    '^gate\.log\z'              # Wait-VenueQuiet -GateLog
    '^batch-\d+\.json\z'        # the per-batch receipt
    '^batches\.jsonl\z'         # one line per batch
    '^batch\.log\z'             # one line per event
    '^leg-plan\.json\z'         # the validated -LegPlan run record
)
$script:RunDirOutputDirs = @('batch-jobs', 'quiet')   # the agent job files; Wait-VenueQuiet -WorkDir

function Get-PhysicalPath([string]$Path) {
    # r10 (adversary r9 A1): the path the file system opens for $Path, every junction, symlink, subst and 8.3 spelling resolved (GetFinalPathNameByHandle), so two spellings
    # of one file compare equal. THE one resolver of every comparison below. Throws when the path cannot be opened (the caller refuses: an unknown location is not a safe one).
    if (-not ('VenueLeaseBatchFinalPath' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class VenueLeaseBatchFinalPath {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder buffer, uint length, uint flags);
    [StructLayout(LayoutKind.Sequential)]
    struct FileTime { public uint Low; public uint High; }
    [StructLayout(LayoutKind.Sequential)]
    struct ByHandleFileInformation {
        public uint Attributes; public FileTime Created; public FileTime Accessed; public FileTime Written;
        public uint VolumeSerial; public uint SizeHigh; public uint SizeLow; public uint Links; public uint IndexHigh; public uint IndexLow;
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle handle, out ByHandleFileInformation info);
    // volume serial + file index = the file's identity, whatever name or link reached it (a hard link is a second name for the same file); Links is how many names it has
    public static string Identity(string path, out uint links) {
        using (SafeFileHandle h = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            if (h.IsInvalid) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
            ByHandleFileInformation info;
            if (!GetFileInformationByHandle(h, out info)) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
            links = info.Links;
            return info.VolumeSerial.ToString("x8") + ":" + info.IndexHigh.ToString("x8") + info.IndexLow.ToString("x8");
        }
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool GetVolumeInformationW(string root, StringBuilder volumeName, uint volumeNameSize, out uint serial, out uint maxComponent, out uint flags, StringBuilder fsName, uint fsNameSize);
    // r11 (sol r10 blocker 2): the longest single path component the volume holding $root accepts (255 on NTFS)
    public static int MaxComponent(string root) {
        uint serial, maxComponent, flags;
        if (!GetVolumeInformationW(root, null, 0, out serial, out maxComponent, out flags, null, 0)) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
        return (int)maxComponent;
    }
    public static string Resolve(string path) {
        // access 0 (attributes only), share all, OPEN_EXISTING, BACKUP_SEMANTICS (a directory opens too); no OPEN_REPARSE_POINT, so a link is followed to its target
        using (SafeFileHandle h = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            if (h.IsInvalid) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
            uint size = 1024;
            while (true) {
                StringBuilder sb = new StringBuilder((int)size);
                uint n = GetFinalPathNameByHandleW(h, sb, size, 0);
                if (n == 0) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
                if (n < size) {
                    string s = sb.ToString();
                    if (s.StartsWith(@"\\?\UNC\", StringComparison.Ordinal)) { return @"\\" + s.Substring(8); }
                    if (s.StartsWith(@"\\?\", StringComparison.Ordinal)) { return s.Substring(4); }
                    return s;
                }
                size = n + 1;
            }
        }
    }
}
'@
    }
    [VenueLeaseBatchFinalPath]::Resolve([IO.Path]::GetFullPath($Path))
}

function Get-FileIdentity([string]$Path) {
    # r10 (adversary r10 A1): a hard link has its own final path but is the SAME file, so a path comparison cannot see it; the volume + file index can. links > 1 = the file has another name.
    [uint32]$links = 0
    $key = [VenueLeaseBatchFinalPath]::Identity([IO.Path]::GetFullPath($Path), [ref]$links)
    [pscustomobject]@{ key = $key; links = [int]$links }
}

# r11 (sol r10 blocker 1 / fable H1; card LEASE-BATCH-MULTI-BUILD-1): the runner's outputs are not only the files in -RunDir. VenueChain.psm1 also writes, under the claim dir, every
# file it names with Join-Path / Get-VenueClaimPath / Get-VenueTicketDir / Get-VenueLeaseLedgerPath: <venue>.claim.json (Invoke-VenueClaimAttempt creates it, Update-VenueClaim rewrites it,
# Remove-VenueClaimIfUnchanged deletes it, and deletes an unparseable one as a stale claim), <venue>.lease-ledger.jsonl (Add-VenueLeaseLedger appends) and <venue>.tickets\ (New-VenueTicket
# creates <ticks>-p<pid>-<start>.ticket.json files there). The claim dir is a second zone beside -RunDir (they may be the same folder), listed here and nowhere else, like the -RunDir one above.
$script:ClaimDirOutputLeafPatterns = @(
    ('^' + [regex]::Escape("$Venue.claim.json") + '\z')
    ('^' + [regex]::Escape("$Venue.lease-ledger.jsonl") + '\z')
)
$script:ClaimDirOutputDirs = @("$Venue.tickets")
$script:ClaimDirPath = $(try { [string](Split-Path -Parent (Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir)) } catch { '' })

function Remove-TrailingSeparator([string]$Path) {
    # r12 (adversary r12 A2): TrimEnd('\') turns the drive root C:\ into C:, which every file API reads as the drive's CURRENT directory, so a guard that listed a root zone looked at the
    # wrong folder and missed a hard link or a link directly under it. A drive root keeps its one backslash; every other path (UNC share roots too) loses its trailing ones.
    $t = $Path.TrimEnd('\')
    if ($t -match '^[A-Za-z]:\z') { return $t + '\' }
    $t
}

function Get-OutputZones {
    # The folders this run writes into: -RunDir, then the claim dir when it already exists (nothing can sit in one that does not). root = the PHYSICAL path.
    # r13: rootId / ownedIds are the zone's and each existing owned directory's file identity (Get-RunDirOutputRefusal fills them)
    $zones = @([pscustomobject]@{ label = '-RunDir'; root = (Remove-TrailingSeparator (Get-PhysicalPath $RunDir)); leaf = $script:RunDirOutputLeafPatterns; dirs = $script:RunDirOutputDirs; linked = @(); rootId = $null; ownedIds = @{} })
    if ($script:ClaimDirPath -and (Test-Path -LiteralPath $script:ClaimDirPath -PathType Container)) {
        $zones += [pscustomobject]@{ label = '-ClaimDir'; root = (Remove-TrailingSeparator (Get-PhysicalPath $script:ClaimDirPath)); leaf = $script:ClaimDirOutputLeafPatterns; dirs = $script:ClaimDirOutputDirs; linked = @(); rootId = $null; ownedIds = @{} }
    }
    $zones
}

function Get-RunDirOutputRefusal([string]$Path) {
    # '' when $Path is not a file this run writes; else the sentence for the refusal. r10 (adversary r9 A1): PHYSICAL paths on both sides (Get-PhysicalPath), so neither a spelling of -RunDir
    # (\.\, ..\, a trailing \) nor a junction or symlink that reaches it (the input, an ancestor of it, or -RunDir itself) slips past. Every caller goes through here, and so does the
    # re-check once the claim is held (Get-InputOutputRefusal). r11: and the same for each zone of Get-OutputZones (-RunDir, then the claim dir).
    # r5b (adversary r5b A2): $script:refusalInputUnopened says whether a refusal came from the input ITSELF not opening (Get-InputOutputRefusal never calls that an alias)
    $hardLinkOf = ''; $hardLinkZone = $null; $script:refusalInputUnopened = $false
    try {
        try { $full = Get-PhysicalPath $Path } catch { $script:refusalInputUnopened = $true; throw }
        $zones = @(Get-OutputZones)
        foreach ($z in $zones) {
            # a runner-owned name inside the zone that is itself a link: what the runner writes under that name lands at the link's target, so the target is the output
            $linked = @()
            foreach ($c in @(Get-ChildItem -LiteralPath $z.root -Force -ErrorAction Stop | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) {
                $owned = ($c.Name -iin $z.dirs)
                foreach ($p in $z.leaf) { if ($c.Name -imatch $p) { $owned = $true } }
                if ($owned) { $linked += [pscustomobject]@{ name = $c.Name; isDir = [bool]$c.PSIsContainer; physical = (Get-PhysicalPath $c.FullName) } }
            }
            # r12 pass 2 (adversary r12 A1'): and a link BELOW an owned directory (quiet\x.job.ps1 -> a spec, or a junction in it) writes through just the same, whatever the target's link count:
            # anything under an owned directory is a runner output, so every reparse point found there is listed with its physical target. A link that cannot be resolved points at nothing.
            foreach ($d in $z.dirs) {
                $dp = Join-Path $z.root $d
                $top = @($linked | Where-Object { $_.isDir -and $_.name -ieq $d })
                if ($top.Count -gt 0) { $dp = $top[0].physical }
                if (-not (Test-Path -LiteralPath $dp -PathType Container)) { continue }
                foreach ($c in @(Get-ChildItem -LiteralPath $dp -Force -Recurse -ErrorAction Stop | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) {
                    $target = $(try { Get-PhysicalPath $c.FullName } catch { $null })
                    if ($target) { $linked += [pscustomobject]@{ name = $c.Name; isDir = [bool]$c.PSIsContainer; physical = $target; nested = $true } }
                }
            }
            $z.linked = $linked
            # r13 (Dot A2''): a final path has more than one SPELLING (\\localhost\C$\run\gate.log and C:\run\gate.log are one file, one link, no reparse point), so every location
            # below is also compared by identity (volume serial + file index), which no spelling changes. One that cannot be read refuses the input (the catch).
            $z.rootId = (Get-FileIdentity $z.root).key
            foreach ($d in $z.dirs) { $dp = Join-Path $z.root $d; if (Test-Path -LiteralPath $dp -PathType Container) { $z.ownedIds[$d] = (Get-FileIdentity $dp).key } }
            foreach ($l in $z.linked) { $l | Add-Member -NotePropertyName id -NotePropertyValue (Get-FileIdentity $l.physical).key }
        }
        # r10 (adversary r10 A1): and a HARD link is a second name for the same file, with its own final path, so the paths differ while the file is the one the runner writes. Compare file
        # identity: only an input with more than one name can be one, and then only the outputs that already exist can be its other name.
        try { $id = Get-FileIdentity $full } catch { $script:refusalInputUnopened = $true; throw }
        # r13 (Dot A2''): the identity of every directory above the input, nearest first, for the zone and owned-directory comparisons below
        $aboveIds = [System.Collections.Generic.List[string]]::new()
        $a = [IO.Path]::GetDirectoryName($full)
        while ($a) { $aboveIds.Add((Get-FileIdentity $a).key); $a = [IO.Path]::GetDirectoryName($a) }
        if ($id.links -gt 1) {
            foreach ($z in $zones) {
                $scan = [System.Collections.Generic.List[object]]::new()
                foreach ($c in @(Get-ChildItem -LiteralPath $z.root -Force -File -ErrorAction Stop)) {
                    foreach ($p in $z.leaf) { if ($c.Name -imatch $p) { $scan.Add($c); break } }
                }
                foreach ($d in $z.dirs) {
                    $dp = Join-Path $z.root $d
                    $lk = @($z.linked | Where-Object { $_.isDir -and -not $_.nested -and $_.name -ieq $d })
                    if ($lk.Count -gt 0) { $dp = $lk[0].physical }
                    if (Test-Path -LiteralPath $dp -PathType Container) { foreach ($c in @(Get-ChildItem -LiteralPath $dp -Force -File -Recurse -ErrorAction Stop)) { $scan.Add($c) } }
                }
                foreach ($c in $scan) {
                    if (((Get-FileIdentity $c.FullName).key -ceq $id.key) -and ((Get-PhysicalPath $c.FullName) -ine $full)) { $hardLinkOf = $c.FullName; $hardLinkZone = $z; break }
                }
                if ($hardLinkOf) { break }
            }
        }
    } catch { return "'$Path' cannot be resolved to a physical path ($($_.Exception.Message -replace '\s+', ' ')); an input whose location is unknown is refused" }
    $shown = $(if ($full -ieq [IO.Path]::GetFullPath($Path)) { "'$full'" } else { "'$Path' (physical path '$full')" })
    if ($hardLinkOf) { return "$shown is a hard link to $hardLinkOf, a file this run writes under $($hardLinkZone.label)" }
    $parent = Remove-TrailingSeparator ([IO.Path]::GetDirectoryName($full))
    foreach ($z in $zones) {
        # each test by spelling OR by identity (r13, Dot A2''): the same file or directory reached by another spelling is the same output
        foreach ($l in $z.linked) {
            if (-not $l.isDir -and ($full -ieq $l.physical -or $id.key -ceq $l.id)) { return "$shown is a file this run writes into $($z.label) ($($l.name), a link to it)" }
            if ($l.isDir -and (($full + '\').StartsWith($l.physical.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or $aboveIds -ccontains $l.id)) { return "$shown is inside $($l.name)\, a link to a directory this run writes into under $($z.label)" }
        }
        if ($parent -ieq $z.root -or ($aboveIds.Count -gt 0 -and $aboveIds[0] -ceq $z.rootId)) {
            $leaf = [IO.Path]::GetFileName($full)
            foreach ($p in $z.leaf) { if ($leaf -imatch $p) { return "$shown is a file this run writes into $($z.label) ($leaf)" } }
        }
        foreach ($d in $z.dirs) {
            if (($parent.TrimEnd('\') + '\').StartsWith($z.root.TrimEnd('\') + '\' + $d + '\', [StringComparison]::OrdinalIgnoreCase) -or ($z.ownedIds.ContainsKey($d) -and $aboveIds -ccontains $z.ownedIds[$d])) { return "$shown is inside $d\, a directory this run writes into under $($z.label)" }
        }
    }
    ''
}

function Get-OutputNameRefusal([string]$LegName) {
    # r11 (sol r10 blocker 2): '' when every file name the runner generates from $LegName can be opened, else the sentence. The names are Invoke-QuietProbe's quiet-<leg>-<phase>.out.txt and the
    # leg's leg-<leg>-b<nn>-a<n>.out.txt (the two generated ones; the rest are fixed names). The batch and attempt numbers are not known yet, so the check reserves room for the widest
    # it will print (b9999-a999999). A name over the volume's longest component (GetVolumeInformationW; 255 when it cannot be read), or a full path over 32000 characters, is refused.
    if ($null -eq $script:MaxNameComponent) {
        $script:MaxNameComponent = 255
        try { $n = [VenueLeaseBatchFinalPath]::MaxComponent([IO.Path]::GetPathRoot((Get-PhysicalPath $RunDir))); if ($n -gt 0) { $script:MaxNameComponent = $n } } catch { }
    }
    $phase = 'b9999-a999999'
    foreach ($name in @("quiet-$LegName-$phase.out.txt", "leg-$LegName-$phase.out.txt")) {
        if ($name.Length -gt $script:MaxNameComponent) { return "the file name '$name' this run would write is $($name.Length) characters, over the $($script:MaxNameComponent) the volume allows in one name (reserving room for batch and attempt numbers)" }
        if ($RunDir.TrimEnd('\').Length + 1 + $name.Length -gt 32000) { return "the path to '$name' under -RunDir is over 32000 characters" }
    }
    ''
}

function Get-InputUnresolvedCode([string]$Path, [switch]$SkipRead) {
    # r5 (opus r4b B1, the second trigger): Get-RunDirOutputRefusal refuses an input it cannot open, which is right before any claim, but once the claim is held a spec deleted after
    # its leg ran, or a plan briefly gone in a save-as-rename, is not a runner output. '' when the input itself opens (its refusal stands); INPUT_VANISHED when nothing is left at its
    # name and the folder it was in still resolves; INPUT_UNREADABLE for an input that is there but will not open (a dangling link, a share not answering).
    # r5b (adversary r5b A1): or that is no file any more: a folder at its name opens like one (BACKUP_SEMANTICS) and reads as nothing, so it is INPUT_UNREADABLE too.
    # r5b pass 2 (adversary A2): the guard's opens ask for no access (0), so a file whose data cannot be read (FILE_READ_DATA denied) passed them: it is opened for reading here.
    # -SkipRead: a staged build manifest's bytes are the content guard's (SHARE_UNREACHABLE, r13), so only its location and kind are checked here.
    try {
        $null = Get-PhysicalPath $Path; $null = Get-FileIdentity $Path
        if ([IO.Directory]::Exists($Path)) { return 'INPUT_UNREADABLE' }
        if (-not $SkipRead) { $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)); $fs.Dispose() }
        return ''
    } catch { }
    $entry = $true
    try { $null = Get-Item -LiteralPath $Path -Force -ErrorAction Stop } catch { $entry = $false }
    if (-not $entry) { try { $null = Get-PhysicalPath ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))); return 'INPUT_VANISHED' } catch { } }
    'INPUT_UNREADABLE'
}

function Get-InputOutputRefusal {
    # r11 (sol r10 blocker 3): the first validated input (the plan file, then each leg spec) that is now a file this run writes; $null when none is. Defined once the plan is built;
    # the very guard the preflight uses (Get-RunDirOutputRefusal), run again once the claim is held, because an alias can appear while the batch waits for a foreign claim.
    # r5: an input that cannot be opened any more is its own hit (kind unresolved, Get-InputUnresolvedCode), never "an input is a runner output", and a real alias outranks it.
    $unresolved = $null
    foreach ($g in $script:guardedInputs) {
        # a staged build manifest that is gone has no location to compare; Get-InputContentRefusal types that (BUILD_NOT_STAGED). r5b (adversary r5b A1): a folder at its name is not gone.
        if ($g.kind -ceq 'build' -and -not (Test-Path -LiteralPath $g.path)) { continue }
        # r5b (adversary r5b A2): only a refusal reached with the input itself open is an alias. An input that did not open while the guard ran got no answer from it (a save-as-rename's
        # moment): the guard is asked again, at most three times, and an input that never stays open is unresolved, never a runner output.
        $code = ''
        for ($ask = 1; $ask -le 3; $ask++) {
            $why = Get-RunDirOutputRefusal $g.path
            if (-not $why -or -not $script:refusalInputUnopened) { break }
            $code = Get-InputUnresolvedCode $g.path
            if ($code) { break }
        }
        if ($why -and $script:refusalInputUnopened -and -not $code) { $code = 'INPUT_UNREADABLE' }
        # r5b (adversary r5b A1): no output, but the input may be a folder now (it opens, and reads as nothing)
        if (-not $why) { $code = Get-InputUnresolvedCode $g.path -SkipRead:($g.kind -ceq 'build') }
        if (-not $code) { if ($why) { return [pscustomobject]@{ kind = $g.kind; item = $g.item; why = $why } }; continue }
        if (-not $unresolved) {
            $label = $(if ($g.kind -ceq 'plan') { 'the plan file' } elseif ($g.kind -ceq 'build') { 'the staged build manifest' } else { 'the leg spec' })
            $unresolved = [pscustomobject]@{ kind = 'unresolved'; input = $g.kind; item = $g.item; code = $code; path = $g.path
                why = "$label '$($g.path)' $(if ($code -ceq 'INPUT_VANISHED') { 'is gone (nothing at that name any more)' } else { 'can no longer be opened as a file' }) since it was accepted" }
        }
    }
    if ($unresolved) { return $unresolved }
    Get-InputContentRefusal
}

function Test-AliasHit($Hit) {
    # r5: a guard hit that means an accepted input IS a runner output (the hits that suppress every write into -RunDir); a content or unresolved hit is not one
    [bool]($Hit -and $Hit.kind -cne 'content' -and $Hit.kind -cne 'unresolved')
}

function Get-HitRefusal($Hit) {
    # r5: the typed reason, plan item and sentence of a guard hit, in ONE place (the claim guard, the open, the receipt guard and the summary name a hit alike)
    if ($Hit.kind -ceq 'content' -or $Hit.kind -ceq 'unresolved') { return $Hit.code, $Hit.item, $Hit.why }
    if ($Hit.kind -ceq 'plan') { return 'LEG_PLAN_IS_RUN_OUTPUT', 0, "the plan file $($Hit.why)" }
    if ($Hit.kind -ceq 'build') { return 'BUILD_MANIFEST_IS_RUN_OUTPUT', $Hit.item, "the staged build manifest $($Hit.why)" }
    return 'LEG_SPEC_IS_RUN_OUTPUT', $Hit.item, "the leg spec $($Hit.why)"
}

function Test-OpenOutputAlias {
    # r14 (Dot 2e6eb21d; adversary r13 pass 3 A1, A2): the guard run once the open returns, WHATEVER it returned (a failed open, one with no lease batch, one still held in the gate, one
    # held to the deadline), before anything more is written. A location hit means a runner output IS an accepted input, so it turns the write suppression on by itself: the log and
    # the receipt go to stderr, the close job file to a fresh folder under TEMP, the ledger row is said, not appended. Which typed stop the run then takes is decided apart from it.
    # adversary r14 pass 2 A1: a location hit is sticky. The gate-wait guard can find it and a later call find the link gone, but the keeper is stopped and the writes are
    # suppressed by then, so the run must still end in the refusal: no probe or leg ever runs once an alias was found.
    if ($script:openAliasHit) { return $script:openAliasHit }
    $hit = Get-InputOutputRefusal
    if ((Test-AliasHit $hit) -and -not $script:RunOutputAliased) {
        $script:openAliasHit = $hit; $script:suppressionHit = $hit
        # Suppression first, so nothing below can leave it off: no batch.log at all until a redirect exists (r5: the result line is then on stdout only, and no log failure).
        $script:RunOutputAliased = $true; $script:batchLog = $null
        # adversary r14 A3: the claim keeper rewrites the claim file in place, and that file may be the input too: it is stopped now, not after the close. The claim can then lapse
        # at its TTL while the lease is closed; it is still released (by nonce, a delete that changes no linked input's bytes) in the finally.
        $script:keeperStoppedEarly = $true; $script:keeperStoppedInTime = Stop-VenueKeeper -Keeper $claimKeeper
        # adversary r14 A1, A2: the redirect is a NEW folder with an unguessable name, created without -Force (an existing one, prepared with links, is never reused), and its files are
        # created new (Submit-AgentJob). A redirect that cannot be made is no throw: the close job then cannot be written, its submit is typed JOB_WRITE_FAILED, and the finally goes on.
        # r16 (sol r15 B2): no job folder at all until the redirect is made, and the redirect's path is built inside the try: TEMP on a drive that does not exist threw from Join-Path
        # before it, so BATCH_ERROR replaced the open's typed stop and the close job was still written into <RunDir>\batch-jobs. The failure is the outputRedirect field instead.
        $script:jobDir = $null; $tempDir = [IO.Path]::GetTempPath(); $redirect = $null
        try {
            $redirect = [IO.Path]::Combine($tempDir, "venue-lease-batch-close-$token-$([guid]::NewGuid().ToString('N'))")
            # TEMP set inside -RunDir would put the redirect back in it: refused, by spelling and (adversary r14 pass 2 A2: a junction to -RunDir) by the physical path of both
            foreach ($pair in @(@($RunDir, $tempDir), @((Get-PhysicalPath $RunDir), (Get-PhysicalPath $tempDir)))) {
                $runDirPrefix = Remove-TrailingSeparator $pair[0]; if (-not $runDirPrefix.EndsWith('\')) { $runDirPrefix += '\' }
                $tp = Remove-TrailingSeparator ([IO.Path]::GetFullPath($pair[1])); if (-not $tp.EndsWith('\')) { $tp += '\' }
                if ($tp.StartsWith($runDirPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw "TEMP ($tempDir) is inside -RunDir (physical $($pair[1]))" }
            }
            # r16 (fable r15 H1): and by identity, as Get-RunDirOutputRefusal compares (volume serial + file index): a loopback UNC TEMP (\\localhost\C$\<RunDir>) has no
            # prefix in common with -RunDir by spelling or by final path, so every existing folder from TEMP up is compared with -RunDir itself
            $runDirId = (Get-FileIdentity $RunDir).key
            $up = Remove-TrailingSeparator ([IO.Path]::GetFullPath($tempDir))
            while ($up) {
                if ((Test-Path -LiteralPath $up -PathType Container) -and (Get-FileIdentity $up).key -ceq $runDirId) { throw "TEMP ($tempDir) is inside -RunDir ($up is -RunDir by file identity)" }
                $up = [IO.Path]::GetDirectoryName($up)
            }
            $null = New-Item -ItemType Directory -Path $redirect -ErrorAction Stop
            $script:jobDir = $redirect; $script:batchLog = Join-Path $redirect 'batch.log'
            $script:outputRedirect = [ordered]@{ state = 'MADE'; path = $redirect; temp = $tempDir; error = $null }
        }
        catch {
            $why = $_.Exception.Message -replace '\s+', ' '
            $script:outputRedirect = [ordered]@{ state = 'FAILED'; path = $redirect; temp = $tempDir; error = $why }
            [Console]::Error.WriteLine("BATCH_OUTPUT_REDIRECT_FAILED temp=$tempDir redirect=$redirect ($why): the close job cannot be written (nothing is written into -RunDir instead); the result line is on stdout only")
        }
        [Console]::Error.WriteLine("BATCH_OUTPUT_ALIASED kind=$($hit.kind) item=$($hit.item) $($hit.why) (found once the open returned: nothing more is written into -RunDir; the log and the receipt go to stderr; claim keeper stopped inTime=$($script:keeperStoppedInTime))")
    }
    $hit
}

function Get-InputContentRefusal {
    # r12 pass 2 (adversary r12 A2'): the CONTENT the preflight validated (each leg spec the leg would run, each staged build manifest's digest and fields) can change in the same wait as
    # a location can, and the leg job would then throw or refuse after the lease opened. The same reads and the same typed reasons as the preflight, run again with the location re-check.
    # $null when nothing changed. r13 (Dot A1''): a manifest whose bytes cannot be read now (write-locked, the share not answering) is a hit too, SHARE_UNREACHABLE as in the preflight:
    # its digest cannot be re-validated, and no lease is opened on a build nobody re-validated.
    if (-not $legPlanMode) { return $null }
    foreach ($g in $script:guardedInputs) {
        if ($g.kind -ceq 'spec') {
            $sr = @(Get-LegSpecRefusal $g.path $g.backend)
            if ($sr[0]) { return [pscustomobject]@{ kind = 'content'; item = $g.item; code = [string]$sr[0]; why = [string]$sr[1] } }
        } elseif ($g.kind -ceq 'build') {
            $st = Get-VenueStagedBuildManifest -AgentShare $AgentShare -SourceCommit $g.commit -TimeoutSec 30
            $short = $g.commit.Substring(0, 12)
            if ($st.state -ceq 'UNREACHABLE') { return [pscustomobject]@{ kind = 'content'; item = $g.item; code = 'SHARE_UNREACHABLE'; why = "the staged build manifest for $short could not be read after the claim wait at $($st.path): $($st.error -replace '\s+', ' '); its digest cannot be re-validated, so no lease is opened" } }
            if ($st.state -cne 'STAGED' -and $st.state -cne 'NOT_STAGED') { return [pscustomobject]@{ kind = 'content'; item = $g.item; code = 'SHARE_UNREACHABLE'; why = "the staged build manifest for $short read as '$($st.state)' after the claim wait; only STAGED with the validated digest opens a lease" } }
            if ($st.state -ceq 'NOT_STAGED') { return [pscustomobject]@{ kind = 'content'; item = $g.item; code = 'BUILD_NOT_STAGED'; why = "no staged build manifest for $short at $($st.path) any more; the runner stages and builds nothing" } }
            if ($st.state -ceq 'STAGED' -and $st.sha256 -cne $g.sha256) { return [pscustomobject]@{ kind = 'content'; item = $g.item; code = 'BUILD_MANIFEST_MISMATCH'; why = "the staged build manifest for $short changed while the batch waited: sha256 $($st.sha256), not $($g.sha256)" } }
            if ($st.state -ceq 'STAGED' -and $st.contentProblem) { return [pscustomobject]@{ kind = 'content'; item = $g.item; code = 'BUILD_MANIFEST_MISMATCH'; why = "the staged build manifest for $short at $($st.path) is no longer one the leg job accepts: $($st.contentProblem)" } }
        }
    }
    $null
}

function Stop-InputIsOutput($Hit) {
    # the typed refusal of Get-InputOutputRefusal's hit: the same reason words and exit 2 as the preflight's
    $hr = Get-HitRefusal $Hit
    if ($legPlanMode) { Stop-PlanRefused $hr[0] $hr[1] $hr[2] } else { Stop-Usage "$($hr[0]) $($hr[2])" }
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
        $outputRefusal = Get-RunDirOutputRefusal $planPath   # r9: the plan is an input too (leg-plan.json is written over it once the claim is held)
        if ($outputRefusal) { Stop-PlanRefused 'LEG_PLAN_IS_RUN_OUTPUT' 0 "the plan file $outputRefusal" }
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
            # adversary r7 pass 2 A1, r9: a spec that IS one of the files this run writes under -RunDir (the one list above) would be overwritten after the lease opened.
            $outputRefusal = Get-RunDirOutputRefusal $path
            if ($outputRefusal) { Stop-PlanRefused 'LEG_SPEC_IS_RUN_OUTPUT' $n "the leg spec $outputRefusal" }
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
            # r12 (adversary r12 A1): the manifest is an input the legs read, so it gets the guard the plan and the specs get: a hard link or a link that makes it one of the files this run writes
            # (leg-plan.json, a batch receipt, ...) would have it overwritten after the lease opened. r16 (adversary r16 A1): BEFORE its digest and content refusals, as the specs' guard
            # comes before theirs: a manifest that is batch.log was appended to by the BUILD_MANIFEST_MISMATCH refusal's own log line.
            $outputRefusal = Get-RunDirOutputRefusal $st.path
            if ($outputRefusal) { Stop-PlanRefused 'BUILD_MANIFEST_IS_RUN_OUTPUT' $it.item "the staged build manifest for $short $outputRefusal" }
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
        $outputRefusal = Get-RunDirOutputRefusal $path   # r9: the same guard as a -LegPlan item's spec
        if ($outputRefusal) { Stop-Usage "LEG_SPEC_IS_RUN_OUTPUT the leg spec $outputRefusal" }
        $legId = Get-LegSpecId $path
        for ($r = 1; $r -le $Repeats; $r++) {
            $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f ($plan.Count + 1), $legId, $(if ($Backend) { $Backend } else { 'spec' })); spec = $path; backend = $(if ($Backend) { $Backend } else { '' }) })
        }
    }
}
# r11 (sol r10 blocker 2): every generated output name is checked HERE, before any claim, for every form: a leg name built from an accepted spec's own basename (the legId fallback) and a
# 32-character arm can pass every earlier check and then name a file the volume cannot hold, which would fail after the lease opened.
foreach ($leg in $plan) {
    $nameRefusal = Get-OutputNameRefusal $leg.name
    if ($nameRefusal) {
        if ($legPlanMode) { Stop-PlanRefused 'OUTPUT_NAME_TOO_LONG' $leg.planItem "leg $($leg.name): $nameRefusal" }
        else { Stop-Usage "OUTPUT_NAME_TOO_LONG leg $($leg.name): $nameRefusal" }
    }
}
# r11 (sol r10 blocker 3): the validated inputs, kept for the re-check once the claim is held (Get-InputOutputRefusal). A -LegSet's specs are inputs too, so they are checked now as well.
$script:guardedInputs = [System.Collections.Generic.List[object]]::new()
if ($legPlanMode) { $script:guardedInputs.Add([pscustomobject]@{ path = $planPath; kind = 'plan'; item = 0 }) }
foreach ($p in $plan) { $script:guardedInputs.Add([pscustomobject]@{ path = $p.spec; kind = 'spec'; item = $(if ($legPlanMode) { $p.planItem } else { 0 }); backend = $p.backend }) }
# r12 (adversary r12 A1): and each staged build manifest the plan reads (-LegPlan only; the scalar form's one build is read inside the leg job, after the lease)
if ($legPlanMode) { foreach ($b in $builds.GetEnumerator()) { $first = @($items | Where-Object { $_.sourceCommit -ceq $b.Key })[0]; $script:guardedInputs.Add([pscustomobject]@{ path = $b.Value.path; kind = 'build'; item = $first.item; commit = $b.Key; sha256 = $first.buildManifestSha256 }) } }
if ($LegSet) { $hit = Get-InputOutputRefusal; if ($hit) { Stop-InputIsOutput $hit } }
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
    # r16 (sol r15 B1): JOB_WRITE_FAILED = the job file was never complete here (no job folder, the create or the write failed), so um-run.ps1 was never called and
    # nothing reached the agent. Every other outcome follows the call, which $script:umRunCalled records first.
    $bound = @{}
    if ($NotAfterUtc) { $bound.NotAfterUtc = $NotAfterUtc }
    try {
        if (-not $jobDir) { throw 'no job folder: the output redirect could not be made' }
        $f = Join-Path $jobDir "$JobId.job.ps1"
        # adversary r14 A1, A2: the job file is created NEW (a file already at its name, a link prepared there included, is refused, never truncated), and a write that fails is
        # this job's outcome, not a throw out of the finally's close (which would skip the keeper stop, the claim release and the receipt)
        $fs = [IO.File]::Open($f, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $jb = [Text.Encoding]::ASCII.GetBytes($Body + "`r`n"); $fs.Write($jb, 0, $jb.Length) } finally { $fs.Dispose() }
    } catch {
        return [pscustomobject]@{ outcome = 'JOB_WRITE_FAILED'; result = $null; message = "the job file for $JobId could not be written ($($_.Exception.Message -replace '\s+', ' ')): um-run was not called" }
    }
    try {
        $script:umRunCalled[$JobId] = $true
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
# LEASE-BATCH-RECEIPT-PERSIST-FAILURE-REPORTING-1 (Dot B6 on #684): a receipt file the run cannot write (batches.jsonl or batch-NN.json occupied by a directory, a held or full disk) is a
# RESULT, not a throw. Each write is tried on its own; a failure keeps the in-memory row ($batches) and its error here, is logged once as RECEIPT_PERSIST_FAILED file=<path> reason=<msg>,
# and reaches the summary (receiptPersistFailures) and the exit code (13 when nothing worse was selected), so the summary is never skipped and a lost receipt is never exit 0.
$script:receiptPersistFailures = [System.Collections.Generic.List[object]]::new()
# adversary r4 A1/A2 (the class of the rewrite alias): an accepted input (the next leg's spec, the plan) can become a link to a receipt file in the window between the last alias guard and ANY receipt
# write, most easily inside the diagnostic of the write before it. So every receipt write asks first (this is the one function they all go through), a location hit skips the write and turns the
# suppression on (Test-OpenOutputAlias's rule: nothing more goes into -RunDir), and the hit is STICKY ($script:receiptGuardHit): the next batch's claim guard re-reports it even if the link
# is gone by then, so no probe, leg or write follows an alias that was seen. A content hit is no alias: the write goes on. Writes to stderr only (never the output stream).
# r5 (opus r4b B1 = sol r4b B1; folds LEASE-BATCH-FINAL-RECEIPT-ALIAS-EXIT-1): a skipped write recorded nothing, and only the next batch's claim guard read the saved hit, so on the final
# batch (or a deadline path's) the run ended exit 0 with no receipt on disk. Every suppressed receipt write is now a typed receiptPersistFailures entry (Add-SuppressedReceipt), and a hit
# no claim guard consumed is the run's typed stop in Write-RunSummary. An input that cannot be opened (INPUT_VANISHED / INPUT_UNREADABLE) is no alias: the receipts are written, and the
# hit is still kept, so the run ends on it, never exit 0.
$script:receiptGuardHit = $null
function Add-SuppressedReceipt([int]$Batch, [string]$Path) {
    $hr = $(if ($script:suppressionHit) { Get-HitRefusal $script:suppressionHit } else { 'RUN_OUTPUT_ALIASED', 0, 'an accepted input is a runner output' })
    $reason = "WRITE_SUPPRESSED $($hr[0])$(if ($hr[1] -gt 0) { " item=$($hr[1])" }): $($hr[2] -replace '\s+', ' ') (nothing more is written into -RunDir)"
    $script:receiptPersistFailures.Add([ordered]@{ batch = $Batch; file = $Path; reason = $reason; suppressedBy = $hr[0] })
    [Console]::Error.WriteLine("BATCH_RECEIPT_WRITE_SKIPPED batch=$Batch file=$Path reason=$reason")
}
function Test-ReceiptWriteSuppressed([int]$Batch, [string]$Path) {
    if (-not $script:RunOutputAliased) {
        $guardHit = Get-InputOutputRefusal
        if (-not (Test-AliasHit $guardHit)) {
            # r5b pass 2 (adversary A1): a content hit is kept too (a staged manifest gone after the final leg is BUILD_NOT_STAGED there), so no hit seen here is dropped
            if ($guardHit -and -not $script:receiptGuardHit) {
                $script:receiptGuardHit = $guardHit
                [Console]::Error.WriteLine("BATCH_RECEIPT_INPUT_$(if ($guardHit.kind -ceq 'unresolved') { 'UNRESOLVED' } else { 'CHANGED' }) batch=$Batch reason=$($guardHit.code) item=$($guardHit.item) $($guardHit.why) (no alias: the receipt is still written; the run ends on it)")
            }
            return $false
        }
        $script:receiptGuardHit = $guardHit; $script:suppressionHit = $guardHit
        $script:RunOutputAliased = $true; $script:batchLog = $null
    }
    Add-SuppressedReceipt $Batch $Path
    $true
}
function Write-ReceiptFile([int]$Batch, [string]$Path, [scriptblock]$Write) {
    $script:receiptFileWritten = $false   # not a return value: L writes its line to the output stream, which a captured return would swallow
    if (Test-ReceiptWriteSuppressed $Batch $Path) { return }
    try { & $Write; $script:receiptFileWritten = $true } catch {
        $why = ($_.Exception.Message -replace '\s+', ' ')
        $script:receiptPersistFailures.Add([ordered]@{ batch = $Batch; file = $Path; reason = $why })
        LogBest "RECEIPT_PERSIST_FAILED file=$Path reason=$why"
    }
}
function Write-BatchReceipt([System.Collections.IDictionary]$B) {
    $B.endedUtc = [DateTime]::UtcNow.ToString('o')
    $rec = [pscustomobject]$B
    $perBatch = Join-Path $RunDir ('batch-{0:00}.json' -f $B.batch); $jsonl = Join-Path $RunDir 'batches.jsonl'
    if ($script:RunOutputAliased) {
        [Console]::Error.WriteLine("BATCH_RECEIPT_NOT_WRITTEN batch=$($B.batch) (an accepted input is a runner output; nothing more is written into -RunDir) $($rec | ConvertTo-Json -Compress -Depth 8)"); $batches.Add($rec)
        foreach ($f in $perBatch, $jsonl) { Add-SuppressedReceipt $B.batch $f }   # r5: both lost receipts are named in the summary
        return
    }
    $batches.Add($rec)   # the in-memory row first: the summary reports it whatever the files do
    # adversary r1 A1: the diagnostic of one failed write is itself a log line that can fail and bump logWriteFailures, so every serialization takes the live count (the callers snapshot it
    # before this function), and the overwritable batch-NN.json is written once more when a later diagnostic bumped it (the append-only jsonl line is the only one that cannot be).
    $snap = @{ perBatch = -1 }
    $putPerBatch = { $rec.logWriteFailures = $script:logWriteFailures; $snap.perBatch = $script:logWriteFailures; [IO.File]::WriteAllText($perBatch, ($rec | ConvertTo-Json -Depth 8) + "`n", [Text.UTF8Encoding]::new($false)) }
    Write-ReceiptFile $B.batch $perBatch $putPerBatch
    $perBatchOk = $script:receiptFileWritten
    Write-ReceiptFile $B.batch $jsonl { $rec.logWriteFailures = $script:logWriteFailures; Add-Content -LiteralPath $jsonl -Value ($rec | ConvertTo-Json -Compress -Depth 8) -ErrorAction Stop }
    if ($perBatchOk -and $snap.perBatch -ne $script:logWriteFailures) { Write-ReceiptFile $B.batch $perBatch $putPerBatch }   # guarded like every receipt write, right before the write
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
# r3 (opus r2b H3): the end of a run in ONE place, so no stop can skip a part of it: the legs never started are NOT_RUN, a receipt that could not be written is never an exit 0, and
# LEASE_BATCH_RESULT is printed (and tried on batch.log). The normal end and an input-is-output refusal once a batch receipt exists both call it.
function Write-RunSummary {
    # r5 (opus r4b B1): a hit the receipt guard saw that no later claim guard consumed (the final batch's, a deadline path's) is the run's typed stop, as that guard would have made it.
    # A stopping code already selected (3, 9, 12, ...) keeps its meaning; the suppressed receipts are in receiptPersistFailures either way.
    if ($script:receiptGuardHit -and ($script:exitCode -eq 0 -or $script:exitCode -eq 7)) {
        $hr = Get-HitRefusal $script:receiptGuardHit
        Set-Stop $hr[0] 2
        [Console]::Error.WriteLine("BATCH_RECEIPT_GUARD_STOP reason=$($hr[0])$(if ($hr[1] -gt 0) { " item=$($hr[1])" }) $($hr[2]) (seen while a batch receipt was written; no later guard consumed it)")
    }
    foreach ($leg in $pending) { Add-Result $leg 'NOT_RUN' @{ verdict = $(if ($script:stopReason) { $script:stopReason } else { 'NOT_RUN' }); reason = 'run-stopped' }; LogBest "LEG $($leg.name) NOT_RUN $($script:stopReason) reason=run-stopped" }
    $pending.Clear()
    # A receipt that could not be written is never an exit 0 (13 replaces only a clean 0: every code the outcome selected, 7 included, keeps its own meaning).
    if ($script:receiptPersistFailures.Count -gt 0 -and $script:exitCode -eq 0) { $script:exitCode = 13; $script:stopReason = 'RECEIPT_PERSIST_FAILED' }
    $summary = [ordered]@{
        schema = 'mlv-app/venue-lease-batch-result/v1'; venue = $Venue; runDir = $RunDir; startedUtc = $t0.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o')
        exitCode = $script:exitCode; stopReason = $script:stopReason; capsSource = $capsReceipt.capsSource; logWriteFailures = $script:logWriteFailures; receiptPersistFailures = @($script:receiptPersistFailures); legs = @($results)
        ran = @($results | Where-Object { $_.status -ceq 'RAN' }).Count; counted = @($results | Where-Object { $_.status -ceq 'RAN' -and $_.counted }).Count
        failed = @($results | Where-Object { $_.status -ceq 'LEG_FAILED' }).Count; notRun = @($results | Where-Object { $_.status -ceq 'NOT_RUN' }).Count
        leaseExpired = @($results | Where-Object { $_.status -ceq 'LEASE_EXPIRED' }).Count
        batches = @($batches | ForEach-Object { [ordered]@{ batch = $_.batch; token = $_.token; leaseId = $_.leaseId; endReason = $_.endReason; legsAttempted = $_.legsAttempted; legsRan = $_.legsRan; legsCounted = $_.legsCounted; retries = $_.retries } })
    }
    if ($script:outputRedirect) { $summary.outputRedirect = $script:outputRedirect }
    if ($legPlanMode) { $summary.legPlan =[ordered]@{ path = $planRecord.legPlan; order = $PlanOrder; arms = $planRecord.arms; builds = @($planRecord.builds | ForEach-Object { $_.sourceCommit }) } }
    $resultLine = 'LEASE_BATCH_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
    # r5 (opus r4b B1 sweep): with the writes suppressed and no redirect there is no batch.log to append to: the line is on stdout only, which is no log failure
    if ($script:RunOutputAliased -and -not $script:batchLog) { [Console]::Error.WriteLine('BATCH_RESULT_NOT_LOGGED (an accepted input is a runner output: nothing is appended into -RunDir; the result line is on stdout)') }
    else { try { Add-Content -LiteralPath $batchLog -Value $resultLine } catch {   # r6 (Dot r5 B5; adversary A3): the result line on stdout and the typed exit code never depend on batch.log, and the failure is still said on stderr
        # r7 (sol r6b): the count was serialized before this append, so the append that just failed is counted and the line for stdout is serialized again with it (stdout is the authority when batch.log is broken).
        Register-LogFailure $_.Exception.Message
        $summary.logWriteFailures = $script:logWriteFailures
        $resultLine = 'LEASE_BATCH_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
    } }
    Write-Output $resultLine
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
        # r3 (sol r2b B1): this line, CLAIM_LAPSED_IN_GUARD and CLAIM_HELD sit between batches, outside the batch try: a batch.log held after an earlier batch's receipt (or its
        # RECEIPT_PERSIST_FAILED) threw out of the script here, so the summary and the typed exit were lost. Best effort, like every line a mandatory step follows.
        LogBest "BATCH_WAIT batch=$batchNo reason=$($bw.reason) waitSec=$($bw.waitSec) usedSec=$($bw.usedSec) capSec=$LeaseCapSec budgetSec=$LeaseBudgetSec windowSec=$LeaseBudgetWindowSec cooldownSec=$LeaseCooldownSec (Bachelor's lease caps; not a failure, no claim held)"
        Start-Sleep -Seconds $bw.waitSec
    }
    # (1) the claim, once per batch. A LIVE claim of another chain is waited out; stale claims are broken once each.
    $claim = $null; $lastHolder = ''
    while ($true) {
        $req = Request-VenueClaim -Venue $Venue -RunDir $RunDir -Purpose "$Purpose batch $batchNo" -TtlSec $ClaimTtlSec -ClaimDir $ClaimDir
        $grantReadUtc = [DateTime]::UtcNow
        # r11 (sol r10 blocker 3): the preflight guard ran before this wait, which can be long, and an alias to an output can appear in it. The same guard runs again after every attempt,
        # before the first write (the CLAIM_* log lines, leg-plan.json): a hit releases a claim just taken, opens no lease and ends in the typed refusal (exit 2).
        $hit = Get-InputOutputRefusal
        # adversary r4 A2: an alias seen while a receipt was written stays seen, whatever the link is by now. r5: it outranks a live hit that is no alias; an unresolved one is kept too
        if ((Test-AliasHit $script:receiptGuardHit) -and -not (Test-AliasHit $hit)) { $hit = $script:receiptGuardHit } elseif (-not $hit -and $script:receiptGuardHit) { $hit = $script:receiptGuardHit }
        if ($hit) {
            if ($req.granted) { try { [void](Release-VenueClaim -Venue $Venue -Nonce $req.claim.nonce -ClaimDir $ClaimDir) } catch { } }
            # r3 (opus r2b H3): once a batch receipt exists, this refusal ends the run like any other stop: the summary (receiptPersistFailures included) is printed before the typed
            # exit 2. A location hit turns the write suppression on first (Test-OpenOutputAlias's rule): the refused input may be batch.log itself, so nothing more goes into -RunDir.
            # r3 pass 3 (adversary r3 A1): the summary runs inside the refusal, AFTER its own log line, so the logWriteFailures it reports counts that line too.
            if ($batches.Count -gt 0) {
                if (Test-AliasHit $hit) { $script:RunOutputAliased = $true; $script:batchLog = $null; $script:suppressionHit = $hit }
                Set-Stop (Get-HitRefusal $hit)[0] 2
                $script:summaryBeforeExit = { Write-RunSummary }
            }
            Stop-InputIsOutput $hit
        }
        foreach ($bk in $req.broken) { LogBest "CLAIM_STALE_BROKEN venue=$Venue reason=$($bk.reason) ownerPid=$($bk.claim.ownerPid) runDir=$($bk.claim.runDir) expiresUtc=$($bk.claim.expiresUtc)" }
        # r13 pass 2 (adversary A2): the keeper starts only after that guard, whose reads can take a while; a claim granted longer ago than its TTL less one renew interval may lapse
        # (and be taken by another claimant) before the keeper's first renewal, so it is let go and asked for again, never carried into an open.
        $guardSec = ([DateTime]::UtcNow - $grantReadUtc).TotalSeconds
        if ($req.granted -and $guardSec -ge ($ClaimTtlSec - $ClaimRenewSec)) {
            try { [void](Release-VenueClaim -Venue $Venue -Nonce $req.claim.nonce -ClaimDir $ClaimDir) } catch { }
            LogBest "CLAIM_LAPSED_IN_GUARD batch=$batchNo guardSec=$([int]$guardSec) ttlSec=$ClaimTtlSec renewSec=$ClaimRenewSec (the claim was let go before the keeper started; asked for again)"
            if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -ge $deadline) { break }
            continue
        }
        if ($req.granted) { $claim = $req.claim; break }
        $holder = $(if ($req.claim) { "pid=$($req.claim.ownerPid) runDir=$($req.claim.runDir) purpose=$($req.claim.purpose)" } else { $req.state })
        if ($holder -cne $lastHolder) { LogBest "CLAIM_HELD batch=$batchNo state=$($req.state) holder $holder -> wait"; $lastHolder = $holder }
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
    $script:keeperStoppedEarly = $false; $script:keeperStoppedInTime = $null
    # r3 (Dot B1): the finally block's cancel, close and ledger charge follow whether the open was SUBMITTED, never "$op is null": a null $op before the call (a throw while
    # the sidecar was checked) means nothing was sent. r16 (sol r15 B1; hub ruling 08:18:44Z): submitted = um-run.ps1 was called for it ($script:umRunCalled), not merely
    # Submit-AgentJob: a job file that could not be created or written here (JOB_WRITE_FAILED) reached no one.
    $op = $null; $leaseStartUtc = $null; $openSubmitUtc = $null; $openLive = $null; $cr = $null; $inFlightLeg = $null
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
            $op = Submit-AgentJob $b.openJobId "Write-Output ('LEASE_BATCH_OPEN token=$token held=' + [string]`$env:HEAVY_SLOT_HELD + ' admission=' + [string]`$env:MLV_AGENT_ADMISSION)" 120 $wait $b.openNotAfterUtc
        }
        if ($op.outcome -ceq 'UNRESOLVED' -and $op.message -match 'admission gate') {
            # CAPS d: um-run stopped while the agent still holds the open in its gate, re-asking a refused (exit 75) request. That is a wait, not a failure:
            # the open is never resubmitted (a second open would queue behind it); its result is read by name until the deadline.
            $b.openLeaseWait = 'EXIT_75_WAIT'
            $null = Test-OpenOutputAlias   # r14 (adversary r13 pass 3 A2): the open returned (still held), so the guard runs before this line is written; it runs again below
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
        # r16: and an open whose job file was never written was never anyone's (JOB_WRITE_FAILED)
        if ($op.outcome -ceq 'RECEIPT' -or $op.outcome -ceq 'RECEIPT_BY_NAME' -or $op.outcome -ceq 'RETRACTED' -or $op.outcome -ceq 'JOB_WRITE_FAILED') { $b.openLiveness = Stop-VenueJobLiveness $openLive -Remove }
        $b.openOutcome = $op.outcome
        $b.openExit75Refusals = $(if ($null -ne $adm) { @(@($adm.leaseRefusals) | Where-Object { $null -ne $_ -and [string]$_.rc -eq '75' }).Count } else { 0 })
        $b.exit75Refusals = $b.openExit75Refusals
        if ($b.exit75Refusals -gt 0 -or $b.openLeaseWait -cne 'NONE') { $b.exit75Class = 'WAIT_RETRY' }
        # r13 (adversary r13 A2): the open can wait long in the agent's admission gate (and by name after it), and an alias to an output, or a changed input, can appear in that
        # wait as in the claim wait. The same guard runs again once a lease is in hand, before the first probe or leg writes: a hit runs nothing, the finally closes the lease,
        # and the run ends in the same typed refusal (exit 2). A link made after this check is a race no re-check closes (an instant later), and is not claimed here.
        # r13 pass 2 (adversary A3): the lease's end is placed from the moment the result was read, BEFORE the guard's reads (which can take a while), never after them
        $openReadUtc = [DateTime]::UtcNow
        $leaseEnd = Get-LeaseEndUtc $op.result
        # r14 (Dot 2e6eb21d; adversary r13 pass 3 A1, A2): the guard runs for EVERY outcome, a gate hold that reached the deadline included, and a location hit suppresses the writes
        # whichever typed stop wins below (OPEN_FAILED, AGENT_NOT_BATCH_CAPABLE, DEADLINE, ...); only a clean open turns a hit into the refusal itself.
        $openHit = Test-OpenOutputAlias
        if ($op.outcome -ceq 'NOT_SUBMITTED' -and $crossed) {
            $b.endReason = 'DEADLINE'; $b.endDetail = "$($op.message) (the open was not submitted: nothing ran, nothing counted)"
            Set-Stop 'DEADLINE' 3
        } elseif ($op.outcome -ceq 'NOT_SUBMITTED') {
            $b.endReason = 'LIVENESS_UNAVAILABLE'; $b.endDetail = "$($op.message) (the open was not submitted: nothing ran, nothing counted)"
            Set-Stop 'LIVENESS_UNAVAILABLE' 9
        } elseif ($op.outcome -ceq 'JOB_WRITE_FAILED') {
            # r16 (sol r15 B1): the open's job file was never complete here, so um-run was never called: no cancel, no close, no lease-budget charge (the finally reads $script:umRunCalled)
            $b.endReason = 'OPEN_JOB_WRITE_FAILED'; $b.endDetail = "$($op.message) (the open was not submitted: nothing ran, nothing counted)"
            Set-Stop 'OPEN_JOB_WRITE_FAILED' 9
        } elseif ($op.outcome -ceq 'UNRESOLVED' -and $b.openLeaseWait -cne 'NONE') {
            $b.endReason = 'DEADLINE'; $b.endDetail = "lease-wait: the open was still held in the agent's admission gate (exit-75 refusals, not a failure) at the deadline"
            Set-Stop 'DEADLINE' 3
        } elseif (($op.outcome -cne 'RECEIPT' -and $op.outcome -cne 'RECEIPT_BY_NAME') -or $null -eq $op.result -or [int]$op.result.exitCode -ne 0 -or $null -eq $adm -or [string]$adm.label -cne 'clear') {
            $b.endReason = 'OPEN_FAILED'; $b.endDetail = "outcome=$($op.outcome) exit=$(if ($op.result) { $op.result.exitCode }) label=$(if ($adm) { $adm.label }) $($op.message)".Trim()
            Set-Stop 'OPEN_FAILED' 9
        } elseif ($null -eq $adm.leaseBatch -or [string]$adm.leaseBatch.token -cne $token -or -not $adm.leaseBatch.leaseId) {
            $b.endReason = 'AGENT_NOT_BATCH_CAPABLE'; $b.endDetail = "admission.mode=$($adm.mode) gate=$($adm.gate) (no admission.leaseBatch for token ${token}: the lease-batch agent is not deployed)"
            Set-Stop 'AGENT_NOT_BATCH_CAPABLE' 10
        } elseif ($openHit) {
            $hr = Get-HitRefusal $openHit
            # the lease is held: the finally closes it and charges the ledger from the open's submit (no earlier start is known without reading its end)
            $b.leaseId = [string]$adm.leaseBatch.leaseId; $leaseStartUtc = $openSubmitUtc; $b.endReason = $hr[0]
            # r13 pass 2 (adversary A1): a location hit means a runner output IS the input, so from here nothing is written into -RunDir or the claim dir's outputs (the ledger may be
            # that input). r14: Test-OpenOutputAlias above already turned that suppression on, for this branch and every other.
            $b.endDetail = "after the open, before any probe or leg: $($hr[2])"
            [Console]::Error.WriteLine("BATCH_USAGE $(if ($legPlanMode) { "LEG_PLAN_REFUSED reason=$($hr[0])$(if ($hr[1] -gt 0) { " item=$($hr[1])" })" } else { $hr[0] }) $($hr[2]) (found after the open: no probe or leg ran; the lease is closed)")
            Set-Stop $hr[0] 2
        } else {
            $b.leaseId = [string]$adm.leaseBatch.leaseId; $b.held = [string]$adm.heavySlotHeld; $b.slot = $adm.slot; $b.leaseMinutes = $adm.leaseMinutes
            $b.leaseUntilEpoch = $adm.leaseBatch.untilEpoch; $b.openWaitedSec = $adm.waitedSec; $b.openRequests = $adm.leaseRequests; $b.refusals = @($adm.leaseRefusals)
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
        # r5b: an open never submitted (NOT_SUBMITTED) has nothing to cancel and no lease to close. r3: "never submitted" is not "$op is null", so a throw before the
        # call (op still null) is also never sent, while a throw or timeout once um-run was called (op null or not) stays a maybe-sent open. r16 (sol r15 B1): submitted means
        # um-run.ps1 was called for the open ($script:umRunCalled), so a job file that could not be created or written here (JOB_WRITE_FAILED) is never sent either.
        $openSent = [bool]($b.openJobId -and $script:umRunCalled.ContainsKey([string]$b.openJobId))
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
                if ($script:RunOutputAliased) { LogBest "LEDGER_NOT_WRITTEN batch=$batchNo (an accepted input is a runner output) row=$($row | ConvertTo-Json -Compress)" }
                else { try { Add-VenueLeaseLedger -Venue $Venue -ClaimDir $ClaimDir -Row ([pscustomobject]$row) } catch { LogBest "LEDGER_WRITE_FAILED batch=$batchNo $($_.Exception.Message)" } }
                $leases.Add([pscustomobject]@{ startUtc = $row.startUtc; endUtc = $row.endUtc; leaseId = $row.leaseId })
            }
        }
        $ckInTime = $(if ($script:keeperStoppedEarly) { $script:keeperStoppedInTime } else { Stop-VenueKeeper -Keeper $claimKeeper })   # r14: stopped at the alias already
        $cks = $claimKeeper.state
        $b.claimRenew = [ordered]@{ renewSec = $ClaimRenewSec; ttlSec = $ClaimTtlSec; renews = $cks.renews; errors = $cks.errors; lastError = $cks.lastError; lost = $cks.lost; lastExpiresUtc = $cks.expiresUtc; stoppedInTime = $ckInTime }
        LogBest "CLAIM_RENEW batch=$batchNo renews=$($cks.renews) errors=$($cks.errors) lost=$(if ($cks.lost) { $cks.lost } else { 'no' }) lastExpiresUtc=$($cks.expiresUtc)"
        # r6 (Dot r5 B5): the release no longer follows a log call that can throw, and a release that throws is a result, not a skipped receipt.
        $relResult = try { Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir } catch { "ERROR: $($_.Exception.Message -replace '\s+', ' ')" }
        LogBest "CLAIM_RELEASED batch=$batchNo result=$relResult"
        # r7 (sol r6b): BATCH_END is logged BEFORE the receipt snapshots logWriteFailures, so a failure logging it is counted in the receipt written for that batch.
        LogBest "BATCH_END batch=$batchNo token=$token lease=$($b.leaseId) reason=$($b.endReason) attempted=$($b.legsAttempted) ran=$($b.legsRan) counted=$($b.legsCounted) retries=$($b.retries) probeRetries=$($b.probeRetries) legRetries=$($b.legRetries) refusals=$(@($b.refusals).Count) exit75=$($b.exit75Refusals)/$($b.exit75Class) renewOkMaxGapSec=$($b.renewOkMaxGapSec) preOpenWait=$($b.preOpenWait.reason):$($b.preOpenWait.waitSec)s close=$($b.closeOutcome) agentEnd=$(if ($b.agent) { $b.agent.endReason })$(if ($b.endDetail) { " detail=$($b.endDetail)" })"
        $b.logWriteFailures = $script:logWriteFailures
        if ($script:outputRedirect) { $b.outputRedirect = $script:outputRedirect }   # r16 (sol r15 B2): MADE, or FAILED with its error (the close job then had no folder)
        Write-BatchReceipt $b
    }
}
Write-RunSummary
Complete-VenueProcess -ExitCode $exitCode   # a share read abandoned at its timeout would otherwise keep this process alive until it ends
exit $exitCode
