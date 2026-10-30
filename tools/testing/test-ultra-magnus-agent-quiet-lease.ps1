[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = '',
    # The interpreter that runs the FAKE heavy-slot.py below (never the real one: it lives only on Bachelor).
    [string]$Python = $(if ($env:MLV_HEAVY_SLOT_PYTHON) { $env:MLV_HEAVY_SLOT_PYTHON } else { '' }),
    # Runs only the named not-after cases (empty = every case, the original ones included), so each can be run alone for its RED.
    [string[]]$Only = @(),
    # lease-batch r5: the VM runner's module whose liveness writer batch-runner-killed-liveness runs (the RED run points this at a base copy).
    [string]$VenueModule = ''
)

# BACHELOR-AGENT-QUIET-LEASE-1 r1: the Bachelor agent's QUIET LEASE (tools/profiling/bachelor/ultra-magnus-agent.ps1,
# heavy-slot.py 2.4.1 `lease request|renew|release`). The agent's REAL functions are lifted from its AST and driven against
# a FAKE heavy-slot.py written into a private fixture dir: it prints heavy-slot.py's active line, honours renew and
# release, and can be told to refuse (exit 75) or hang pending. No agent loop, no share, no venue, no playback.
#   label     - the lease label is exactly "quiet-lease mlv" on request, renew and release (never "mlv " / "mlv-agent ")
#   parse     - the active line -> HEAVY_SLOT_HELD; anything else is not active
#   active    - a request that turns active admits the job with heavySlotHeld from that line, mode lease
#   renew     - renew cadence (injected 1 s interval) from the agent's own tick; a slow renew is killed, not fatal
#   prelaunch - the last check before launch renews to completion and must see this lease's active flag
#   release   - released on the normal end AND on a thrown leg; the wrapper is gone and its record removed
#   refused   - exit 75 -> typed line, never admitted, asked again (requeue); the queue ceiling ends the wait
#   abandoned - a request pending past the agent's bound is abandoned with a typed line, released and stopped
#   stale     - a successor agent ends a wrapper its predecessor left behind
#   batch     - BACHELOR-LEASE-BATCH-LEGS-1: the legs of one batch (job ids ending -lb<token>) share ONE lease: parked between
#               legs, renewed by the hold step, reused with no request (RED: one lease per leg pays the cooldown per leg), and
#               released with a typed receipt on close, idle, too little left, another job, the status rollback, a lost
#               wrapper, a job that never launched, or agent exit; a dead agent's parked lease is still ended by its successor.
#               r2: more than 120 s between SUCCESSFUL renews (heavy-slot.py's demotion) releases a parked lease (demoted)
#               and no later leg is ever admitted on it; the gap is carried in every batch admission and the receipt.
#               r3 (sol r2): a batch job pending in the gate leaves it with no lease once its batch's close job is queued (B1), and
#               a batch job is stopped at its lease's end, or on a demoted or lost lease (B2: 480 s left on a 900 s timeout)
#   cpu       - the legs' <= 20 % CPU quiescence check is kept, and the job launches only after the lease is active
#   status    - -HeavySlotMode status / heavy-slot-mode.txt keep the r1c holder path (its own suite stays green)
#   not-after - LEASE-REQUEST-CHILD-NOT-AFTER-1 r1, meta.json notAfterUtc (cases notafter-*, cancel-active-race; -Only runs one):
#               past it no request is made, a request's --admit-timeout never outlives it, a pending request is stopped when
#               it passes, and a lease that turns active after it (or after a cancel file) is released before the job gets it
#               lease-batch r4 (sol r3 B1 on #633; cases batch-*): a batch's open, which carries the batch deadline as notAfterUtc, holds no
#               lease past it; and a post-admission refusal of an open ends the batch it opened, with its receipt
#               lease-batch r5 (cases batch-close-before-reuse, batch-runner-killed-liveness): a queued close job wins over a leg that sorts ahead of
#               it (no reuse, no launch; dot B2), and a VM runner killed while its open is pending stops renewing the open's sidecar, so the open
#               expires and its request is released within the liveness window (sol B1; the writer is VenueChain.psm1's, -VenueModule for RED)
#   not-after hardening - LEASE-NOT-AFTER-HARDENING-1 r1 (cases notafter-queuedjob-*, notafter-prelaunch-*, notafter-renew-in-light-job,
#               notafter-meta-unreadable): Invoke-QueuedJob's not-after exits RUN (pre-gate, gate label, post-admission, and the
#               last check before the child is created), each refusing, releasing and retiring; a waiting lease is not renewed
#               past the deadline from inside a light job, and its true phase is recorded; a meta.json that may carry a deadline
#               but cannot be read fails closed. Invoke-QueuedJob is lifted and driven with its disk and launch steps stubbed.
#   not-after hardening 2 - LEASE-NOT-AFTER-HARDENING-2 r1 (cases queuedjob-cancel-post-admission, notafter-renew-crosses-deadline,
#               notafter-meta-sticky): the cancel exit after admission RUNS (released and recorded before Exit-HeavySlot); a
#               renew in flight as the deadline passes is still collected or killed; Get-JobMetadata's deadline flag holds over
#               its reads and matches the key in any case.
#   runner liveness - LEASE-BATCH-OPEN-RUNNER-LIVENESS-1 r1 (cases liveness-*): notafter\<jobId>.json, the runner-liveness sidecar.
#               A present sidecar that has passed or cannot be read ends the job like a passed notAfterUtc at every check (pre-gate,
#               each gate loop, post-active, post-admission, pre-launch); a renewed one keeps it alive across exit-75 retries; an
#               absent one changes nothing. It only shortens: remainingSec and --admit-timeout come from meta.json alone.
#   runner liveness r2 - (cases liveness-root-shape, liveness-lapse-latched): a sidecar whose JSON root is not an object (a
#               one-element array that pwsh 7 would unroll, an array of one, an empty array) fails closed, at the gate and in
#               Invoke-QueuedJob; a lapse the renew tick sees is latched, so a fresher sidecar written later revives no renew
#               and no admission.
#   exit release evidence - QUIET-LEASE-EXIT-RELEASE-EVIDENCE-AND-EVENT-OBSERVE-1 r1 (cases queuedjob-queue-expired-post-admission,
#               queuedjob-holder-lost-prelaunch, exit-release-pin): the queue-expired exit after admission and the pre-launch
#               holder-lost refusal release the lease and record that release (leaseRelease) before Exit-HeavySlot, and every
#               Exit-HeavySlot call site is either preceded by a recorded release or allowlisted with a reason.
#   snapshot bound hardening - AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r1 (cases lease-snapshot-bound-straddle, lease-probe-watchdog-*): a slot
#               snapshot probe is classified from one completion sample and the age sampled after it (sol r5 B1), and no probe starts
#               without an armed watchdog: an Add-Type failure, a type that did not load, a constructor throw, a failed attach, a
#               failed start and a failed registration each leave no unprotected probe (sol r5 B4).
#               r3 (case lease-start-post-start-cleanup-throws, sol r2b B1): a cleanup Dispose that throws after a post-start failure never
#               replaces the prepared exception, so the holder and job launch sites still get postStartStop and the child to stop again.
# Prints one JSON object with status PASS, or throws.

$ErrorActionPreference = 'Stop'
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }

if (-not $Python) {
    try { $Python = (& py -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim() } catch { $Python = '' }
}
if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1).Source }
Assert-True ($Python -and (Test-Path -LiteralPath $Python -PathType Leaf)) "a python interpreter for the fake heavy-slot.py (pass -Python or set MLV_HEAVY_SLOT_PYTHON); got '$Python'"

$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) "agent parses ($($parseErrors.Count) errors)"
$agentText = [IO.File]::ReadAllText($agentScript)
$code = (($agentText -split "`r?`n") | Where-Object { -not $_.TrimStart().StartsWith('#') }) -join "`n"
function Get-FunctionBody([string]$Name) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    Assert-True ($null -ne $fn) "agent defines $Name"
    return $fn.Extent.Text
}
$lift = @('Resolve-HeavySlotPython', 'Get-ProcessIdentity', 'Test-EpochMillisClose', 'Test-ProcessIdentityMatch', 'Get-DescendantProcessIdentities',
    'Stop-ProcessTree', 'Move-FileChecked', 'Write-JsonFileAtomic', 'Read-JsonFile', 'Get-SustainedHeatVerdict', 'Stop-HeavySlotHolder', 'Exit-HeavySlot',
    'Confirm-HeavySlotHeldForLaunch', 'Resolve-HeavySlotMode', 'Get-QuietLeaseMinutes', 'ConvertFrom-QuietLeaseActiveLine', 'Start-HeavySlotCli',
    'Receive-HeavySlotCli', 'Stop-HeavySlotCli', 'Complete-HeavySlotCli', 'Start-QuietLeaseRequest', 'Test-QuietLeaseOwnerPid', 'Update-QuietLeaseRequest',
    'Update-QuietLeaseRenew', 'Invoke-QuietLeaseRenewTick', 'Stop-QuietLease', 'Stop-StaleQuietLeases', 'Confirm-QuietLeaseHeldForLaunch', 'Wait-QuietLeaseAdmission')
foreach ($name in $lift) { . ([scriptblock]::Create((Get-FunctionBody $name))) }
# BACHELOR-LEASE-BATCH-LEGS-1: lifted when the agent has them, so the batch case below also runs (RED) against an agent
# that predates them; the batch case asserts they exist only after it has shown the behaviour.
$liftBatch = @('Get-LeaseBatchToken', 'Test-LeaseBatchCloseJobId', 'Get-QuietLeaseRemainingSec', 'Get-LeaseBatchAdmission', 'Open-LeaseBatch',
    'Use-LeaseBatchForJob', 'Complete-LeaseBatchJob', 'Write-LeaseBatchReceipt', 'Close-LeaseBatch', 'Sync-LeaseBatchForJob', 'Update-LeaseBatchHold')
# lease-batch r2 (the renew-gap demotion): the same, asserted by the demoted case after it has shown the behaviour.
$liftBatchR2 = @('Register-QuietLeaseRenewOk', 'Get-QuietLeaseRenewOkGapSec')
# lease-batch r3 (sol r2 B1, B2): the same, asserted by the r3 cases after they have shown the behaviour.
$liftBatchR3 = @('Test-LeaseBatchClosePending', 'Get-LeaseBatchJobStop')
$batchLifted = [System.Collections.Generic.List[string]]::new()
foreach ($name in $liftBatch + $liftBatchR2 + $liftBatchR3) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if ($null -ne $fn) { . ([scriptblock]::Create($fn.Extent.Text)); $batchLifted.Add($name) }
}
# not-after r1: lifted when present. A missing one is recorded, not thrown, so the cases above still run against a base agent
# and each not-after case fails on behaviour (or on "agent defines ..." in notafter-pure).
$liftNotAfter = @('Get-JobNotAfterVerdict', 'Get-QuietLeaseAdmitTimeout', 'New-LeaseReleaseRecord', 'Publish-NotAfterExpiredResult', 'Read-JobNotAfterSidecar')
$liftMissing = [System.Collections.Generic.List[string]]::new()
foreach ($name in $liftNotAfter) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if ($null -eq $fn) { $liftMissing.Add($name) } else { . ([scriptblock]::Create($fn.Extent.Text)) }
}
# lease-batch r4 (the merge of not-after r1): the same, so batch-refusal-ends-batch fails on behaviour against an agent without it.
foreach ($name in @('Close-LeaseBatchAfterRelease', 'ConvertTo-UtcDateTime', 'Get-LeaseWaitHeatField', 'Get-LeaseSlotAgeText', 'ConvertFrom-HeavySlotHolderText', 'Add-LeaseSlotSnapshot', 'Get-LeaseSnapshotHeadroomSec',
        'Write-LeaseSlotSnapshotLine', 'Update-LeaseSlotSnapshots', 'Set-LeaseCoverageAtLaunch', 'Test-LeaseLocalFixedPath', 'Get-LeaseSlotSnapshotWaitMs', 'Initialize-LeaseProbeWatchdog',
        'Stop-ProcessAfterStartFailure')) {   # AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r2: the post-start stop (sol r1 B4-post-start); absent on a base agent
   # r5: the last one (the probe's own watchdog; called below, as the agent does at start). r4: the two before it (the local-path rule, the bound-capped wait); from ConvertTo-UtcDateTime on: AGENT-LEASE-PENDING-HEAT-TRACE-1 (ConvertTo-UtcDateTime: the headroom helper reads the sidecar bound with it; the last three: r3, the asynchronous probe and the coverage at launch)
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if ($null -eq $fn) { $liftMissing.Add($name) } else { . ([scriptblock]::Create($fn.Extent.Text)) }
}
$paramDefaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) { if ($p.DefaultValue -is [System.Management.Automation.Language.ConstantExpressionAst]) { $paramDefaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value } }

$results = [ordered]@{}
$fx = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-quiet-lease-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $fx, (Join-Path $fx 'cancel'), (Join-Path $fx 'inbox'), (Join-Path $fx 'notafter') | Out-Null

# ---------------------------------------------------------------- the fake heavy-slot.py
$fake = @'
import json, os, sys, time
d = os.environ["HEAVY_SLOT_DIR"]
ctl = {}
try:
    with open(os.path.join(d, "fake-control.json"), encoding="utf-8") as f:
        ctl = json.load(f)
except OSError:
    pass
flag = os.path.join(d, "quiet-lease.json")
def log(**k):
    k.update(argv=sys.argv[1:], pid=os.getpid(), held=os.environ.get("HEAVY_SLOT_HELD"), ts=time.time())
    # AGENT-LEASE-PENDING-HEAT-TRACE-1 r5: the snapshot probes append here concurrently with the request wrapper. The C runtime's append is a seek to the
    # end then a write, so two writers could overwrite each other (a torn line in calls.jsonl, seen in r4 and r5 runs). One write per line, under a
    # byte-range lock past any content; the OS releases it if the writer is killed, and an unobtainable lock (2 s) degrades to the old unlocked write.
    data = (json.dumps(k) + "\n").encode("utf-8")
    fd = os.open(os.path.join(d, "calls.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0))
    try:
        locked = False
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 1 << 30, 0)
            for _ in range(200):
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.01)
        os.write(fd, data)
        if locked:
            os.lseek(fd, 1 << 30, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)
def read_flag():
    try:
        with open(flag, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
def write_flag(o):
    tmp = flag + "." + str(os.getpid()) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(o, f)
    os.replace(tmp, flag)
a = sys.argv[1:]
label = a[a.index("--label") + 1] if "--label" in a else None
if a[:2] == ["lease", "request"]:
    mode = ctl.get("request", "active")
    if mode == "refuse_then_active":
        # The first refuse_count requests are refused (exit 75); the next one turns active.
        n = 0
        try:
            with open(os.path.join(d, "calls.jsonl"), encoding="utf-8") as f:
                n = sum(1 for line in f if '"cmd": "request"' in line)
        except OSError:
            pass
        mode = "refuse" if n < int(ctl.get("refuse_count", 2)) else "active"
    log(cmd="request", mode=mode, label=label)
    # heavy-slot.py's post-lease cooldown (30 min on Bachelor; cooldown_s here): no lease within that long of the last one's end.
    cd = float(ctl.get("cooldown_s", 0))
    if cd > 0:
        try:
            with open(os.path.join(d, "last-lease-end.txt"), encoding="utf-8") as f:
                last = float(f.read())
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < cd:
            log(cmd="refused-cooldown", label=label)
            print("[heavy-slot] admission refused for %r: quiet lease cooldown: the last lease ended %.1f s ago (cooldown %.0f s)" % (label, time.time() - last, cd), file=sys.stderr, flush=True)
            sys.exit(75)
    if mode == "refuse":
        print("[heavy-slot] admission refused for %r: quiet lease cooldown: the last lease ended 12 s ago (cooldown 30 min)" % label, file=sys.stderr, flush=True)
        sys.exit(75)
    if mode == "fail":
        print("[heavy-slot] quiet lease label %r is not in the config labels" % label, file=sys.stderr, flush=True)
        sys.exit(2)
    me = os.getpid()
    rel = os.path.join(d, "release-%d" % me)
    write_flag({"label": label, "pid": me, "state": "pending", "renewed": time.time()})
    if mode == "admit_timeout":
        # heavy-slot.py's pending cap: still pending after --admit-timeout s -> a refusal line and exit 75 (a release ends it first).
        t_end = time.time() + float(a[a.index("--admit-timeout") + 1])
        while time.time() < t_end and not os.path.exists(rel):
            time.sleep(0.1)
        if not os.path.exists(rel):
            log(cmd="request-end", released=False, refused=True)
            o = read_flag()
            if o and o.get("pid") == me:
                os.remove(flag)
            print("[heavy-slot] admission refused for %r: still pending after --admit-timeout %s s" % (label, a[a.index("--admit-timeout") + 1]), file=sys.stderr, flush=True)
            sys.exit(75)
    if mode == "active":
        time.sleep(float(ctl.get("active_after", 0.3)))
        until = time.time() + 60 * float(a[a.index("--minutes") + 1]) + float(ctl.get("until_offset_s", 0))   # until_offset_s: a lease granted short (heat-trace r3)
        write_flag({"label": label, "pid": me, "state": "active", "slot": 1, "renewed": time.time()})
        print("[heavy-slot] 1 acquired for %r" % label, file=sys.stderr, flush=True)
        print("[heavy-slot] lease active slot=1 held=1:%d until=%.0f" % (me, until), flush=True)
    t_end = time.time() + 120
    while time.time() < t_end and not os.path.exists(rel):
        time.sleep(0.1)
    log(cmd="request-end", released=os.path.exists(rel))
    o = read_flag()
    if o and o.get("pid") == me:
        os.remove(flag)
    if mode == "active":
        with open(os.path.join(d, "last-lease-end.txt"), "w", encoding="utf-8") as f:
            f.write(repr(time.time()))
    sys.exit(0)
if a[:2] == ["lease", "renew"]:
    log(cmd="renew", label=label)
    if ctl.get("renew_hang"):
        time.sleep(30)
    if ctl.get("renew_sleep"):
        time.sleep(float(ctl["renew_sleep"]))
    o = read_flag()
    if o is None or o.get("label") != label:
        print(json.dumps({"state": "none"}), flush=True)
        sys.exit(1)
    o["renewed"] = time.time()
    write_flag(o)
    print(json.dumps(o), flush=True)
    sys.exit(0)
if a[:2] == ["lease", "release"]:
    log(cmd="release", label=label)
    o = read_flag()
    if o is None or o.get("label") != label:
        print(json.dumps({"state": "none"}), flush=True)
        sys.exit(0)
    with open(os.path.join(d, "release-%d" % int(o["pid"])), "w") as f:
        f.write("release")
    t_end = time.time() + 10
    while time.time() < t_end and read_flag() is not None:
        time.sleep(0.1)
    print(json.dumps({"state": "none"}), flush=True)
    sys.exit(0)
if a[:1] == ["status"]:
    # AGENT-LEASE-PENDING-HEAT-TRACE-1 r3: the slot snapshot's probe. fake-status.json (lines, rc, stderr, delay) is set per case.
    st = {}
    try:
        with open(os.path.join(d, "fake-status.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        pass
    log(cmd="status")
    if st.get("orphan"):
        # r5 (adversary r5-2ca9f68f A1): the root exits at once, leaving a 300 s child that holds its stdout and stderr.
        import subprocess
        c = subprocess.Popen([sys.executable, os.path.abspath(__file__), "sleep-child"], stdout=sys.stdout, stderr=sys.stderr)
        log(cmd="status-orphan", child=c.pid)
        sys.exit(0)
    time.sleep(float(st.get("delay", 0)))
    for line in st.get("lines", []):
        print(line, flush=True)
    if st.get("stderr"):
        print(st["stderr"], file=sys.stderr, flush=True)
    sys.exit(int(st.get("rc", 0)))
if a[:1] == ["sleep-child"]:
    time.sleep(300)
    sys.exit(0)
log(cmd="unexpected")
print("fake heavy-slot.py: unexpected argv %r" % (a,), file=sys.stderr)
sys.exit(9)
'@
$fakePath = Join-Path $fx 'heavy-slot-fake.py'
[IO.File]::WriteAllText($fakePath, $fake, [Text.UTF8Encoding]::new($false))

# The agent's own script variables, pointed at the fixture.
$HeavySlotPython = $Python
$HeavySlotScript = $fakePath
$HeavySlotDir = $fx
$HeavySlotFeed = Join-Path $fx 'feed-missing.csv'
$HeavySlotCount = 2
$slotHolders = $fx
$logs = $fx
$cancel = Join-Path $fx 'cancel'
$inbox = Join-Path $fx 'inbox'
$notAfterDir = Join-Path $fx 'notafter'
$heavySlotModeFile = Join-Path $fx 'heavy-slot-mode.txt'
$HeavySlotMode = [string]$paramDefaults['HeavySlotMode']
$LeaseMaxMinutes = [int]$paramDefaults['LeaseMaxMinutes']
$LeasePendingMaxSec = 1
$LeaseRenewSec = 1
$LeaseRenewTimeoutSec = 3
$LeaseRetrySec = 1
$LeaseBatchIdleSec = 60
$LeaseBatchMinRemainingSec = 30
$GateProbeSec = 15
$script:HeavySlotModeExplicit = $false
$script:LastHeavySlotModeKey = $null
$script:GateLoopSleepMs = 100
$script:LeasePendingGraceSec = 1
$script:GateVersion = 'unit'
$script:GateHour = @{ admitted = 0; waitSecTotal = 0; requeued = 0 }
$script:AgentProcessStartUtc = 'unit'
$script:HeavySlotHolder = $null
$script:QuietLease = $null
$script:QuietLeaseWaiting = $null
$script:LeaseBatch = $null
$script:LeaseBatchKeep = $false
# lease-batch r2: heavy-slot.py's renew_stale_s, as the agent assigns it (asserted in the demoted case).
$script:LeaseRenewStaleSec = 120
# lease-batch r3: a batch job is stopped this long before its lease's until= (asserted against the agent's assignment in the r3 case).
$script:LeaseBatchJobGuardSec = 10
$script:Admission = $null
# The agent's label constant, read from its own assignment (not restated here).
$labelAst = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$script:QuietLeaseLabel' }, $true)
Assert-True ($null -ne $labelAst) "agent assigns `$script:QuietLeaseLabel"
$script:QuietLeaseLabel = $labelAst.Right.Expression.Value
$script:Log = [System.Collections.Generic.List[string]]::new()
$script:Beats = [System.Collections.Generic.List[string]]::new()
$script:Age = 0
$script:Exempt = 0
# A case's hook runs inside the agent's own log call, so it can land an event at a known point of the gate loop.
$script:LogHook = $null
$script:LeaseNotAfterMinAdmitSec = 30
# AGENT-LEASE-PENDING-HEAT-TRACE-1 r3: the slot snapshots' probes in flight, and their bound (the agent assigns 30 s).
$script:LeaseSlotSnapshots = [System.Collections.Generic.List[object]]::new()
$script:LeaseSnapshotProbeSec = 30
function Add-AgentLogLine([string]$Message) { $script:Log.Add($Message); if ($null -ne $script:LogHook) { & $script:LogHook $Message } }
# r5: the agent compiles the snapshot probe watchdog once at start; so does the test (a base agent has none).
$script:ProbeWatchdogReady = if (Get-Command Initialize-LeaseProbeWatchdog -ErrorAction SilentlyContinue) { [bool](Initialize-LeaseProbeWatchdog) } else { $false }
function Write-AgentHeartbeat([string]$Activity = '') { $script:Beats.Add($Activity); if ($null -ne $script:BeatHook) { & $script:BeatHook $Activity } }
function Get-QueueAgeSeconds { param($Job, $Metadata) return $script:Age }
function Invoke-ExemptJobsDuringWait { param([string]$WaitingJobId, [double]$WaitedSec) $script:Exempt++; return 0 }
function Read-HeavySlotFeedMetrics { return @{ metrics = $null; why = 'feed missing (unit)' } }

function Set-Fake([hashtable]$Control) { [IO.File]::WriteAllText((Join-Path $fx 'fake-control.json'), ($Control | ConvertTo-Json -Compress)) }
function Get-Calls { $p = Join-Path $fx 'calls.jsonl'; if (-not (Test-Path -LiteralPath $p)) { return @() }; return @(Get-Content -LiteralPath $p | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json }) }
function Reset-Case([hashtable]$Control, [int]$GraceSec = 120) {
    # The agent abandons a request pending past LeasePendingMaxSec + LeasePendingGraceSec and asks again (designed: a second,
    # sequential request after the first was stopped). Python start-up on a loaded runner counts against that bound, so only the
    # case that EXERCISES the abandon keeps a tight grace; every other case gets a grace no start-up delay can cross.
    $script:LeasePendingGraceSec = $GraceSec
    # AGENT-LEASE-PENDING-HEAT-TRACE-1 r3: a slot-snapshot probe still in flight is killed before calls.jsonl is cleared (it never lands in the next case).
    if (Get-Command Update-LeaseSlotSnapshots -ErrorAction SilentlyContinue) { Update-LeaseSlotSnapshots -Abandon 'test reset' }
    if ($null -ne $script:LeaseBatch -and (Get-Command Close-LeaseBatch -ErrorAction SilentlyContinue)) { Close-LeaseBatch -Reason 'test-reset' }
    $script:LeaseBatch = $null; $script:LeaseBatchKeep = $false
    Set-Fake $Control
    Remove-Item -LiteralPath (Join-Path $fx 'calls.jsonl'), (Join-Path $fx 'quiet-lease.json'), (Join-Path $fx 'last-lease-end.txt'), (Join-Path $fx 'fake-status.json') -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $fx -Filter 'lease-batch-*.jsonl' -File | Remove-Item -Force
    Get-ChildItem -LiteralPath $fx -Filter 'release-*' -File | Remove-Item -Force
    Get-ChildItem -LiteralPath (Join-Path $fx 'notafter') -File -ErrorAction SilentlyContinue | Remove-Item -Force
    $script:Log.Clear(); $script:Beats.Clear(); $script:Age = 0; $script:Exempt = 0
    $script:QuietLease = $null; $script:QuietLeaseWaiting = $null; $script:Admission = $null
    $script:LogHook = $null; $script:HookFired = $false; $script:BeatHook = $null; $script:SidecarReadHook = $null
}
function Test-PidAlive([int]$ProcessId) { return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) }
function Wait-Until([scriptblock]$Cond, [double]$TimeoutSec) { $dl = (Get-Date).AddSeconds($TimeoutSec); while (-not (& $Cond) -and (Get-Date) -lt $dl) { Start-Sleep -Milliseconds 100 }; return [bool](& $Cond) }
$meta = [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 600 }

try {
  if ($Only.Count -eq 0) {   # the original cases run only in a full run
    # ------------------------------------------------------------ label + parse + minutes (pure)
    Assert-True ($script:QuietLeaseLabel -ceq 'quiet-lease mlv') "the lease label is exactly 'quiet-lease mlv': '$($script:QuietLeaseLabel)'"
    Assert-True (-not $script:QuietLeaseLabel.StartsWith('mlv ') -and -not $script:QuietLeaseLabel.StartsWith('mlv-agent ')) "the label never starts 'mlv ' or 'mlv-agent ' (heavy-slot.py LEASE_FORBIDDEN_PREFIXES)"
    Assert-True ($code -notmatch '"--label",\s*\$Label,\s*"--minutes"' -and $code -match '"lease", "request", "--label", \$script:QuietLeaseLabel') "the request passes the constant label, never a per-job one"
    $ok = ConvertFrom-QuietLeaseActiveLine '[heavy-slot] lease active slot=2 held=2:4242 until=1761200000'
    Assert-True ($null -ne $ok -and $ok.held -ceq '2:4242' -and $ok.slot -eq 2 -and $ok.ownerPid -eq 4242 -and $ok.until -eq 1761200000) "active line parses to HEAVY_SLOT_HELD 2:4242: $($ok | ConvertTo-Json -Compress)"
    Assert-True ($null -eq (ConvertFrom-QuietLeaseActiveLine '[heavy-slot] lease active slot=1 held=2:4242 until=1')) "held= naming another slot is not active"
    Assert-True ($null -eq (ConvertFrom-QuietLeaseActiveLine '[heavy-slot] 1 acquired for ''quiet-lease mlv''')) "the acquired stderr line is not the active line"
    Assert-True ($null -eq (ConvertFrom-QuietLeaseActiveLine '')) "an empty line is not active"
    $mins = [ordered]@{ t1800 = (Get-QuietLeaseMinutes 1800); t600 = (Get-QuietLeaseMinutes 600); t3600 = (Get-QuietLeaseMinutes 3600); t0 = (Get-QuietLeaseMinutes 0); t30 = (Get-QuietLeaseMinutes 30) }
    Assert-True ($mins.t1800 -eq 31 -and $mins.t600 -eq 11 -and $mins.t3600 -eq 45 -and $mins.t0 -eq 45 -and $mins.t30 -eq 2) "lease minutes = timeout + 1 min, capped at 45: $($mins | ConvertTo-Json -Compress)"
    $results.pure = [ordered]@{ label = $script:QuietLeaseLabel; held = $ok.held; minutes = $mins }

    # ------------------------------------------------------------ active -> admission, renew cadence, pre-launch, release on normal end
    Reset-Case @{ request = 'active'; active_after = 1.5 }     # pending long enough for the wait to heartbeat
    $adm = Wait-QuietLeaseAdmission -JobId 'jA' -Job ([pscustomobject]@{}) -Metadata $meta
    $l = $script:QuietLease
    Assert-True ($adm.label -eq 'clear' -and $adm.mode -eq 'lease' -and $null -ne $l) "active: admitted clear in lease mode: $($adm | ConvertTo-Json -Compress -Depth 4)"
    Assert-True ($adm.heavySlotHeld -ceq "1:$($l.active.ownerPid)" -and $adm.slot -eq (Join-Path $fx '1')) "active: HEAVY_SLOT_HELD comes from the active line: $($adm.heavySlotHeld)"
    Assert-True ($adm.leaseMinutes -eq 31 -and $adm.leaseCoversTimeout -and $adm.heavySlotLabel -ceq 'quiet-lease mlv' -and $null -eq $script:QuietLeaseWaiting) "active: 31 minutes cover a 1800 s job"
    Assert-True ($adm.heat -eq 'feed-unknown' -and $adm.thermal -match 'feed missing') "active: the heat verdict is recorded, not gated: $($adm.heat)"
    Assert-True (Test-Path -LiteralPath $l.recordFile) "active: the wrapper's record is on disk while the lease is held"
    $req = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
    Assert-True ($req.Count -eq 1 -and ($req[0].argv -join ' ') -ceq "lease request --label quiet-lease mlv --minutes 31 --admit-timeout 1 --slots 2" -and $null -eq $req[0].held) "active: one request with the exact CLI and no inherited HEAVY_SLOT_HELD: [$($req[0].argv -join ' ')] held=$($req[0].held)"
    Assert-True (@($script:Beats | Where-Object { $_ -match '^gate-wait job=jA state=lease-pending waited=\d+s$' }).Count -ge 1)"active: the wait heartbeats um-run.ps1's gate-wait shape"
    # renew cadence: the tick, as the job monitor loop calls it, at an injected 1 s interval, until 3 renews are done (bounded)
    $tStart = Get-Date
    $t = $tStart.AddSeconds(30)
    while ((Get-Date) -lt $t -and $l.renewsOk -lt 3) { Invoke-QuietLeaseRenewTick; Start-Sleep -Milliseconds 100 }
    $elapsed = ((Get-Date) - $tStart).TotalSeconds
    $renews = @(Get-Calls | Where-Object { $_.cmd -eq 'renew' })
    Assert-True ($renews.Count -ge 3 -and $renews.Count -le [Math]::Ceiling($elapsed) + 1) "renew: 3 renews done, never more than one per interval ($($renews.Count) in $([Math]::Round($elapsed, 1)) s)"
    Assert-True (@($renews | Where-Object { ($_.argv -join ' ') -cne 'lease renew --label quiet-lease mlv' }).Count -eq 0) "renew: every renew names the lease label"
    Assert-True ($l.renewsOk -ge 3 -and $l.renewFailures -eq 0)"renew: renews succeed (ok=$($l.renewsOk) fail=$($l.renewFailures))"
    $gaps = @(); for ($i = 1; $i -lt $renews.Count; $i++) { $gaps += [Math]::Round($renews[$i].ts - $renews[$i - 1].ts, 2) }
    # The gaps are measured at the fake's start, so they carry python start-up jitter: reported, not asserted (the count bound
    # above is the cadence check).
    $script:Admission = $adm
    $pre = Confirm-HeavySlotHeldForLaunch -JobId 'jA'
    Assert-True ($pre.ok) "prelaunch: lease mode's pre-launch proof passes on a live lease: $($pre.why)"
    $wrapperPid = $l.pid
    Exit-HeavySlot
    $rel = @(Get-Calls | Where-Object { $_.cmd -eq 'release' })
    Assert-True ($rel.Count -eq 1 -and ($rel[0].argv -join ' ') -ceq 'lease release --label quiet-lease mlv') "release (normal end): one release with the lease label"
    Assert-True ((Wait-Until { -not (Test-PidAlive $wrapperPid) } 5) -and $null -eq $script:QuietLease -and -not (Test-Path -LiteralPath $l.recordFile)) "release (normal end): wrapper gone, no admitted lease, record removed"
    Assert-True (@(Get-Calls | Where-Object { $_.cmd -eq 'request-end' -and $_.released }).Count -eq 1) "release (normal end): the wrapper ended on the release, not a kill"
    $pre2 = Confirm-HeavySlotHeldForLaunch -JobId 'jA'
    Assert-True (-not $pre2.ok -and $pre2.reason -eq 'holder-lost') "prelaunch: no lease -> refusal, nothing launches: $($pre2.why)"
    $results.active = [ordered]@{ admission = $adm.label; held = $adm.heavySlotHeld; renews = $renews.Count; gaps = $gaps; prelaunch = $pre.ok; releasedBy = 'lease release' }

    # ------------------------------------------------------------ release on a THROWN leg (the inbox loop's try/finally)
    Reset-Case @{ request = 'active' }
    $script:Admission = Wait-QuietLeaseAdmission -JobId 'jT' -Job ([pscustomobject]@{}) -Metadata $meta
    $tp = $script:QuietLease.pid
    $threw = $false
    try { try { throw 'leg threw' } finally { Exit-HeavySlot } } catch { $threw = $_.Exception.Message -eq 'leg threw' }
    Assert-True ($threw -and (Wait-Until { -not (Test-PidAlive $tp) } 5) -and @(Get-Calls | Where-Object { $_.cmd -eq 'release' }).Count -eq 1) "release (thrown leg): the lease is released and the wrapper gone"
    $loopFinally = $code.Substring($code.IndexOf('Invoke-QueuedJob -Job $job'))
    Assert-True ($loopFinally.Substring(0, [Math]::Min(400, $loopFinally.Length)) -match 'finally \{\s*Exit-HeavySlot') "release (thrown leg): the inbox loop's finally calls Exit-HeavySlot after Invoke-QueuedJob"
    Assert-True ($code -match 'finally \{\s*\n[^\n]*\n?\s*if \(\$agentMutexHeld\) \{ Exit-HeavySlot \}' -or $code -match 'if \(\$agentMutexHeld\) \{ Exit-HeavySlot \}') "release (agent exit): the agent's outer finally calls Exit-HeavySlot"
    $results.thrown = [ordered]@{ released = $true }

    # ------------------------------------------------------------ slow renew is killed, not fatal
    Reset-Case @{ request = 'active' }
    $script:Admission = Wait-QuietLeaseAdmission -JobId 'jS' -Job ([pscustomobject]@{}) -Metadata $meta
    $sl = $script:QuietLease
    Set-Fake @{ request = 'active'; renew_hang = $true }
    $t = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $t -and $sl.renewSlow -lt 1) { Invoke-QuietLeaseRenewTick; Start-Sleep -Milliseconds 100 }
    Assert-True ($sl.renewSlow -ge 1 -and -not $sl.proc.HasExited -and $null -ne $script:QuietLease) "slow renew: killed after the bound and counted ($($sl.renewSlow)); the lease is kept"
    Assert-True (@($script:Log | Where-Object { $_ -like 'quiet-lease: renew-slow job=jS*not fatal*' }).Count -ge 1) "slow renew: typed renew-slow line"
    Set-Fake @{ request = 'active' }
    Exit-HeavySlot
    $results.slowRenew = [ordered]@{ renewSlow = $sl.renewSlow; leaseKept = $true }

    # ------------------------------------------------------------ refused (exit 75) -> typed, never admitted, asked again
    Reset-Case @{ request = 'refuse' }
    # The queue ceiling is reached once the case has seen what it needs ($script:ExpireWhen), or after 60 s (a hang guard).
    $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=jR*' }).Count -ge 2 }
    $script:AgeAfter = (Get-Date).AddSeconds(60)
    function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter -or (& $script:ExpireWhen)) { return 9999 } return 0 }
    $adm = Wait-QuietLeaseAdmission -JobId 'jR' -Job ([pscustomobject]@{}) -Metadata $meta
    $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
    Assert-True ($adm.label -eq 'queue-expired' -and $null -eq $script:QuietLease) "refused: never admitted, the queue ceiling ends the wait: $($adm.label)"
    Assert-True ($reqs.Count -ge 2 -and $adm.requeues -ge 2) "refused: the request is asked again after LeaseRetrySec (requests=$($reqs.Count) requeues=$($adm.requeues))"
    $refs = @($adm.leaseRefusals)
    Assert-True ($refs.Count -ge 2 -and @($refs | Where-Object { $_.reason -ne 'refused' -or $_.rc -ne 75 }).Count -eq 0 -and $refs[0].why -match 'admission refused for') "refused: each refusal kept with rc 75 and heavy-slot.py's line: $($refs[0] | ConvertTo-Json -Compress)"
    Assert-True (@($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=jR rc=75 *NOT run unleased*' }).Count -ge 2) "refused: typed refused line per refusal"
    Assert-True (@($script:Beats | Where-Object { $_ -match '^gate-wait job=jR state=lease-refused waited=\d+s$' }).Count -ge 1) "refused: the heartbeat names the state for um-run.ps1"
    Assert-True ($script:Exempt -ge 1) "refused: light jobs are still dispatched from inside the wait"
    $results.refused = [ordered]@{ label = $adm.label; requests = $reqs.Count; requeues = $adm.requeues; refusal = $refs[0].why }

    # ------------------------------------------------------------ request failed (exit 2, e.g. label not in config) -> same, typed
    Reset-Case @{ request = 'fail' }
    $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: request-failed job=jF*' }).Count -ge 1 }
    $script:AgeAfter = (Get-Date).AddSeconds(60)
    $adm = Wait-QuietLeaseAdmission -JobId 'jF' -Job ([pscustomobject]@{}) -Metadata $meta
    Assert-True ($adm.label -eq 'queue-expired' -and @($adm.leaseRefusals)[0].reason -eq 'request-failed' -and @($adm.leaseRefusals)[0].rc -eq 2) "failed: rc 2 is request-failed, never admitted"
    $results.failed = [ordered]@{ reason = @($adm.leaseRefusals)[0].reason }

    # ------------------------------------------------------------ pending past the agent's bound -> abandoned
    Reset-Case @{ request = 'hang' } -GraceSec 1
    $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: ended job=jP*why=`[lease abandoned`]*' }).Count -ge 1 }
    $script:AgeAfter = (Get-Date).AddSeconds(60)
    $adm = Wait-QuietLeaseAdmission -JobId 'jP' -Job ([pscustomobject]@{}) -Metadata $meta
    $hung = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
    Assert-True ($adm.label -eq 'queue-expired' -and @($adm.leaseRefusals | Where-Object { $_.reason -eq 'abandoned' }).Count -ge 1) "abandoned: a request pending past LeasePendingMaxSec + grace is abandoned: $(@($adm.leaseRefusals) | ConvertTo-Json -Compress)"
    Assert-True (@($script:Log | Where-Object { $_ -like 'quiet-lease: abandoned job=jP pending=*bound=1s*NOT run*' }).Count -ge 1) "abandoned: typed abandoned line"
    Assert-True (@(Get-Calls | Where-Object { $_.cmd -eq 'release' }).Count -ge 1 -and (Wait-Until { @($hung | Where-Object { Test-PidAlive ([int]$_.pid) }).Count -eq 0 } 8)) "abandoned: released, and no request wrapper is left running"
    Assert-True (@(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0) "abandoned: no lease record is left"
    $results.abandoned = [ordered]@{ label = $adm.label; abandons = @($adm.leaseRefusals | Where-Object { $_.reason -eq 'abandoned' }).Count }

    # ------------------------------------------------------------ cancel ends the wait, no lease left
    Reset-Case @{ request = 'hang' }
    function Get-QueueAgeSeconds { param($Job, $Metadata) return 0 }
    [IO.File]::WriteAllText((Join-Path $cancel 'jC.cancel'), 'x')
    $adm = Wait-QuietLeaseAdmission -JobId 'jC' -Job ([pscustomobject]@{}) -Metadata $meta
    Remove-Item -LiteralPath (Join-Path $cancel 'jC.cancel') -Force
    Assert-True ($adm.label -eq 'cancelled-in-gate' -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "cancel: cancelled-in-gate, no lease"

    # ------------------------------------------------------------ stale wrapper from a dead agent is ended by the successor
    Reset-Case @{ request = 'active' }
    $script:Admission = Wait-QuietLeaseAdmission -JobId 'jD' -Job ([pscustomobject]@{}) -Metadata $meta
    $dead = $script:QuietLease
    $script:QuietLease = $null          # the agent "dies" holding the lease: no release, no more renews
    Stop-StaleQuietLeases
    Assert-True ((Wait-Until { -not (Test-PidAlive $dead.pid) } 8) -and -not (Test-Path -LiteralPath $dead.recordFile)) "stale: the successor ends the recorded wrapper and removes its record"
    Assert-True (@($script:Log | Where-Object { $_ -like "quiet-lease: stale lease id=$($dead.id) pid=$($dead.pid) *" }).Count -eq 1) "stale: typed stale-lease line"
    try { $dead.proc.Dispose() } catch { }
    $results.stale = [ordered]@{ stopped = $true }
    # Renewals are in-process only, so they stop with the agent (the lease then goes stale at heavy-slot.py's 120 s).
    foreach ($gone in @('Start-Job', 'Register-ObjectEvent', 'System.Timers.Timer', 'Start-ThreadJob', 'Register-EngineEvent')) { Assert-True ($code -notmatch [regex]::Escape($gone)) "kill-safe: the agent never renews from $gone" }
    Assert-True (([regex]::Matches($code, '"lease", "renew"')).Count -eq 2) "kill-safe: renew is started from the tick and the pre-launch proof only"

    # ------------------------------------------------------------ batch (BACHELOR-LEASE-BATCH-LEGS-1): several legs inside ONE lease
    # RED on an agent without the batch: each leg takes its own lease, so every leg after the first is refused by heavy-slot.py's
    # post-lease cooldown and waits it out (Bachelor 2026-10-23: about 34 min per leg that played about 1 min; 4 s here).
    Reset-Case @{ request = 'active'; cooldown_s = 4 }
    $tok = '0123456789ab'
    $legIds = @(1..3 | ForEach-Object { "dve-leg$_-bachelor-cpu-20261023000000-lb$tok" })
    $legAdm = @()
    $tB = Get-Date
    foreach ($jid in $legIds) {
        $script:Admission = Wait-QuietLeaseAdmission -JobId $jid -Job ([pscustomobject]@{}) -Metadata $meta
        Assert-True ($script:Admission.label -eq 'clear') "batch: $jid admitted clear: $($script:Admission.label)"
        $pre = Confirm-HeavySlotHeldForLaunch -JobId $jid
        Assert-True ($pre.ok) "batch: $jid passes the pre-launch proof: $($pre.why)"
        $legAdm += $script:Admission
        # The child ran and ended (Invoke-QueuedJob), then the inbox loop's finally and its next hold step.
        if (Get-Command Complete-LeaseBatchJob -ErrorAction SilentlyContinue) { Complete-LeaseBatchJob -JobId $jid -ExitCode 0 -StartedUtc ([DateTime]::UtcNow.ToString('o')) }
        Exit-HeavySlot
        if (Get-Command Update-LeaseBatchHold -ErrorAction SilentlyContinue) { Update-LeaseBatchHold }
    }
    $batchSec = [Math]::Round(((Get-Date) - $tB).TotalSeconds, 1)
    $bReq = @(Get-Calls | Where-Object { $_.cmd -eq 'request' }).Count
    $bCool = @(Get-Calls | Where-Object { $_.cmd -eq 'refused-cooldown' }).Count
    Assert-True ($bReq -eq 1 -and $bCool -eq 0) "batch: 3 legs of one batch take ONE lease and never wait out the cooldown (lease requests=$bReq, cooldown refusals=$bCool, $batchSec s for 3 legs); one lease per leg pays the cooldown once per leg"
    foreach ($name in $liftBatch) { Assert-True ($batchLifted -contains $name) "batch: the agent defines $name" }
    $b = $script:LeaseBatch
    Assert-True ($null -ne $b -and $b.token -ceq $tok -and $b.jobs.Count -eq 3 -and $null -eq $script:QuietLease) "batch: the lease is PARKED after the third leg (jobs=$($b.jobs.Count))"
    Assert-True (-not $b.lease.proc.HasExited -and (Test-Path -LiteralPath $b.lease.recordFile)) "batch: the parked wrapper is alive and its record stays on disk (a successor agent can end it)"
    Assert-True ($legAdm[0].leaseMinutes -eq $LeaseMaxMinutes -and $legAdm[0].leaseBatch.seq -eq 1 -and -not $legAdm[0].leaseBatch.reused -and $legAdm[0].leaseBatch.token -ceq $tok) "batch: the first leg's lease asks for LeaseMaxMinutes ($($legAdm[0].leaseMinutes)), not its own timeout, and opens the batch"
    Assert-True ($legAdm[1].leaseBatch.reused -and $legAdm[2].leaseBatch.reused -and $legAdm[2].leaseBatch.seq -eq 3 -and $legAdm[1].waitedSec -eq 0 -and $legAdm[1].leaseRequests -eq 0 -and $legAdm[1].mode -eq 'lease') "batch: legs 2 and 3 reuse it, with no request and no wait"
    Assert-True (@($legAdm | ForEach-Object { $_.heavySlotHeld } | Select-Object -Unique).Count -eq 1 -and @($legAdm | ForEach-Object { $_.leaseBatch.leaseId } | Select-Object -Unique).Count -eq 1) "batch: every leg holds the same HEAVY_SLOT_HELD and lease id"
    Assert-True ($legAdm[2].leaseBatch.remainingSec -gt 2600 -and $legAdm[2].leaseBatch.remainingSec -le 2700) "batch: remainingSec comes from the lease's until= ($($legAdm[2].leaseBatch.remainingSec))"
    $ok0 = $b.lease.renewsOk
    $t = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $t -and $b.lease.renewsOk -lt $ok0 + 2) { Update-LeaseBatchHold; Start-Sleep -Milliseconds 100 }
    Assert-True ($b.lease.renewsOk -ge $ok0 + 2 -and $null -ne $script:LeaseBatch) "batch: the inbox loop's hold step renews the parked lease on cadence ($ok0 -> $($b.lease.renewsOk))"
    Sync-LeaseBatchForJob -JobId 'venue-quiet-probe-20261023T000000Z' -Light $true
    Sync-LeaseBatchForJob -JobId "dve-leg1-bachelor-cpu-20261023000000-lb$tok-health" -Light $true
    Sync-LeaseBatchForJob -JobId 'lease-batch-close-x-lbffffffffffff' -Light $true
    Sync-LeaseBatchForJob -JobId "dve-leg4-bachelor-cpu-20261023000000-lb$tok" -Light $false -Mode 'lease'
    Assert-True ($null -ne $script:LeaseBatch -and @(Get-Calls | Where-Object { $_.cmd -eq 'release' }).Count -eq 0) "batch: light jobs, another batch's close job and the next leg of this batch leave it parked"
    $wp = $b.lease.pid
    Sync-LeaseBatchForJob -JobId "lease-batch-close-20261023-lb$tok" -Light $true
    Assert-True ($null -eq $script:LeaseBatch -and (Wait-Until { -not (Test-PidAlive $wp) } 8) -and @(Get-Calls | Where-Object { $_.cmd -eq 'release' }).Count -eq 1) "batch: its own close job releases it (one release, wrapper gone)"
    $rcpt = @(Get-Content -LiteralPath (Join-Path $fx "lease-batch-$tok.jsonl") | ForEach-Object { $_ | ConvertFrom-Json })
    Assert-True ($rcpt.Count -eq 1 -and $rcpt[0].schema -ceq 'mlv-agent-lease-batch.v1' -and $rcpt[0].endReason -ceq 'closed' -and $rcpt[0].jobsRun -eq 3 -and $rcpt[0].leaseId -ceq $legAdm[0].leaseBatch.leaseId -and $rcpt[0].held -ceq $legAdm[0].heavySlotHeld -and $rcpt[0].releaseRc -ceq '0' -and $rcpt[0].renews.ok -ge 3 -and $null -ne $rcpt[0].heldSec -and $rcpt[0].requests -eq 1) "batch: one typed receipt line for the lease: $($rcpt | ConvertTo-Json -Compress -Depth 5)"
    Assert-True ((@($rcpt[0].jobs | ForEach-Object { $_.seq }) -join ',') -ceq '1,2,3' -and (@($rcpt[0].jobs | ForEach-Object { $_.jobId }) -join ',') -ceq ($legIds -join ',')) "batch: the receipt lists the legs in order"
    Assert-True (@($script:Log | Where-Object { $_ -like "quiet-lease: batch-ended token=$tok * reason=closed jobs=3 *" }).Count -eq 1) "batch: typed batch-ended line"
    $results.batch = [ordered]@{ requests = $bReq; cooldownRefusals = $bCool; seconds = $batchSec; reused = @($legAdm | ForEach-Object { $_.leaseBatch.reused }); endReason = $rcpt[0].endReason; renewsOk = $rcpt[0].renews.ok }

    # ------------------------------------------------------------ batch: pure token rules
    Assert-True ((Get-LeaseBatchToken 'dve-x-bachelor-cpu-1-lb0123456789ab') -ceq '0123456789ab' -and $null -eq (Get-LeaseBatchToken 'dve-x-lb0123456789ab-health') -and $null -eq (Get-LeaseBatchToken 'dve-x-lb0123456789AB') -and $null -eq (Get-LeaseBatchToken 'dve-x-lb0123456789a') -and $null -eq (Get-LeaseBatchToken 'jA') -and $null -eq (Get-LeaseBatchToken '')) "batch: the token is exactly 12 lower-case hex at the END of the id (a health probe of a batch leg is not a batch job)"
    Assert-True ((Test-LeaseBatchCloseJobId 'lease-batch-close-20261023T1900Z-lb0123456789ab') -and -not (Test-LeaseBatchCloseJobId 'lease-batch-open-1-lb0123456789ab') -and -not (Test-LeaseBatchCloseJobId 'lease-batch-close-1') -and -not (Test-LeaseBatchCloseJobId 'x-lease-batch-close-1-lb0123456789ab')) "batch: only 'lease-batch-close-<x>-lb<token>' is a close job"

    # ------------------------------------------------------------ batch: every other way a parked lease ends is a release with a typed receipt
    function Start-ParkedBatch([string]$Token) {
        $jid = "dve-p-bachelor-cpu-1-lb$Token"
        $script:Admission = Wait-QuietLeaseAdmission -JobId $jid -Job ([pscustomobject]@{}) -Metadata $meta
        Complete-LeaseBatchJob -JobId $jid -ExitCode 0 -StartedUtc 'unit'
        Exit-HeavySlot
        Assert-True ($null -ne $script:LeaseBatch -and $script:LeaseBatch.token -ceq $Token) "batch: parked for $Token"
        return $script:LeaseBatch
    }
    function Get-BatchEnds([string]$Token) { return @(Get-Content -LiteralPath (Join-Path $fx "lease-batch-$Token.jsonl") -ErrorAction SilentlyContinue | ForEach-Object { ($_ | ConvertFrom-Json).endReason }) }
    $ends = [ordered]@{}
    Reset-Case @{ request = 'active' }
    $null = Start-ParkedBatch 'aaaaaaaaaaa1'
    $LeaseBatchIdleSec = 1; Start-Sleep -Milliseconds 1500; Update-LeaseBatchHold; $LeaseBatchIdleSec = 60
    $ends.idle = (Get-BatchEnds 'aaaaaaaaaaa1') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.idle -ceq 'idle') "batch: a parked lease with no job for LeaseBatchIdleSec is released (idle): '$($ends.idle)'"

    Reset-Case @{ request = 'active' }
    $null = Start-ParkedBatch 'aaaaaaaaaaa2'
    $LeaseBatchMinRemainingSec = 2760; Update-LeaseBatchHold; $LeaseBatchMinRemainingSec = 30
    $ends.remainingShort = (Get-BatchEnds 'aaaaaaaaaaa2') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.remainingShort -ceq 'remaining-short') "batch: a parked lease with less than LeaseBatchMinRemainingSec left is released: '$($ends.remainingShort)'"

    # At REUSE: a leg is never started on a lease about to end. It is released first, and the leg asks for a fresh lease (the next batch).
    Reset-Case @{ request = 'active' }
    $null = Start-ParkedBatch 'aaaaaaaaaaa3'
    $LeaseBatchMinRemainingSec = 2760
    $adm = Wait-QuietLeaseAdmission -JobId 'dve-q-bachelor-cpu-1-lbaaaaaaaaaaa3' -Job ([pscustomobject]@{}) -Metadata $meta
    $LeaseBatchMinRemainingSec = 30
    $script:Admission = $adm
    $ends.reuseShort = (Get-BatchEnds 'aaaaaaaaaaa3') -join ','
    Assert-True ($adm.label -eq 'clear' -and -not $adm.leaseBatch.reused -and $adm.leaseBatch.seq -eq 1 -and $ends.reuseShort -ceq 'remaining-short' -and @(Get-Calls | Where-Object { $_.cmd -eq 'request' }).Count -eq 2) "batch: a short parked lease is released BEFORE the next leg requests its own: '$($ends.reuseShort)'"
    Exit-HeavySlot      # no Complete-LeaseBatchJob: the job never launched (a pre-launch refusal, a cancel, a launch that threw)
    $ends.notCompleted = (Get-BatchEnds 'aaaaaaaaaaa3') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.notCompleted -ceq 'remaining-short,job-not-completed') "batch: a batch job that did not launch and end releases the lease: '$($ends.notCompleted)'"

    Reset-Case @{ request = 'active' }
    $null = Start-ParkedBatch 'aaaaaaaaaaa4'
    Sync-LeaseBatchForJob -JobId 'jOther' -Light $false -Mode 'lease'
    $ends.otherJob = (Get-BatchEnds 'aaaaaaaaaaa4') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.otherJob -ceq 'other-job') "batch: a gated job outside the batch never runs under its lease: '$($ends.otherJob)'"

    Reset-Case @{ request = 'active' }
    $null = Start-ParkedBatch 'aaaaaaaaaaa5'
    Sync-LeaseBatchForJob -JobId 'dve-r-bachelor-cpu-1-lbaaaaaaaaaaa5' -Light $false -Mode 'status'
    $ends.mode = (Get-BatchEnds 'aaaaaaaaaaa5') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.mode -ceq 'mode-status') "batch: the status rollback releases a parked lease before its gate: '$($ends.mode)'"

    Reset-Case @{ request = 'active' }
    $pb = Start-ParkedBatch 'aaaaaaaaaaa6'
    Stop-Process -Id $pb.lease.pid -Force
    Assert-True (Wait-Until { $pb.lease.proc.HasExited } 8) "batch: the fixture's wrapper is gone"
    Update-LeaseBatchHold
    $ends.leaseEnded = (Get-BatchEnds 'aaaaaaaaaaa6') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.leaseEnded -ceq 'lease-ended') "batch: a parked lease whose wrapper exited is dropped: '$($ends.leaseEnded)'"

    Reset-Case @{ request = 'active' }
    $pb = Start-ParkedBatch 'aaaaaaaaaaa7'
    Close-LeaseBatch -Reason 'agent-exit' -Why 'unit'
    $ends.agentExit = (Get-BatchEnds 'aaaaaaaaaaa7') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.agentExit -ceq 'agent-exit' -and (Wait-Until { -not (Test-PidAlive $pb.lease.pid) } 8)) "batch: the agent's exit releases a parked lease: '$($ends.agentExit)'"
    Assert-True ($code -match 'if \(\$agentMutexHeld\) \{ Close-LeaseBatch -Reason "agent-exit"') "batch: the agent's outer finally closes a parked batch"

    Reset-Case @{ request = 'active' }
    $pb = Start-ParkedBatch 'aaaaaaaaaaa8'
    $script:LeaseBatch = $null          # the agent "dies" with a parked lease: no release, no more renews
    Stop-StaleQuietLeases
    Assert-True ((Wait-Until { -not (Test-PidAlive $pb.lease.pid) } 8) -and -not (Test-Path -LiteralPath $pb.lease.recordFile)) "batch: a successor agent ends a dead agent's parked lease (Stop-StaleQuietLeases)"
    try { $pb.lease.proc.Dispose() } catch { }

    # ------------------------------------------------------------ batch r2: a renew gap over 120 s (heavy-slot.py's demotion) ends the lease for the batch
    # The caps: renew about every 60 s; after 120 s with no renew the lease is demoted and other heavy runs may join. A parked
    # lease that had such a gap is released (demoted), and the next leg of its batch is never admitted on it.
    Reset-Case @{ request = 'active' }
    $pb = Start-ParkedBatch 'aaaaaaaaaaa9'
    # 130 s since the last renew that succeeded (Add-Member, so the RED run on an agent without the field reaches the assertion)
    $pb.lease | Add-Member -NotePropertyName lastRenewOkStart -NotePropertyValue (Get-Date).AddSeconds(-130) -Force; $pb.lease.lastRenewStart = Get-Date
    Update-LeaseBatchHold
    $ends.demotedParked = (Get-BatchEnds 'aaaaaaaaaaa9') -join ','
    Assert-True ($null -eq $script:LeaseBatch -and $ends.demotedParked -ceq 'demoted') "batch r2: a parked lease with a renew gap over 120 s is released (demoted): '$($ends.demotedParked)'"

    Reset-Case @{ request = 'active' }
    $pb = Start-ParkedBatch 'aaaaaaaaaab0'
    $pb.lease | Add-Member -NotePropertyName lastRenewOkStart -NotePropertyValue (Get-Date).AddSeconds(-130) -Force; $pb.lease.lastRenewStart = Get-Date
    $adm = Wait-QuietLeaseAdmission -JobId 'dve-d-bachelor-cpu-1-lbaaaaaaaaaab0' -Job ([pscustomobject]@{}) -Metadata $meta
    $script:Admission = $adm
    $ends.demotedReuse = (Get-BatchEnds 'aaaaaaaaaab0') -join ','
    Assert-True ($adm.leaseBatch.leaseId -cne $pb.lease.id -and -not $adm.leaseBatch.reused -and $ends.demotedReuse -ceq 'demoted') "batch r2: the next leg is never admitted on a demoted lease (it was released first: '$($ends.demotedReuse)'; the leg's lease $($adm.leaseBatch.leaseId), the batch's $($pb.lease.id))"
    Assert-True ($null -ne $adm.leaseBatch.renewOkMaxGapSec -and $adm.leaseBatch.renewOkMaxGapSec -lt 60) "batch r2: a fresh batch admission carries its own renew-ok gap ($($adm.leaseBatch.renewOkMaxGapSec))"
    Exit-HeavySlot
    $rcptD = @(Get-Content -LiteralPath (Join-Path $fx 'lease-batch-aaaaaaaaaab0.jsonl') | ForEach-Object { $_ | ConvertFrom-Json })
    Assert-True ($rcptD[0].renews.okMaxGapSec -ge 130 -and $rcptD[0].renews.staleSec -eq 120) "batch r2: the receipt types the gap that demoted it: $($rcptD[0].renews | ConvertTo-Json -Compress)"

    foreach ($name in $liftBatchR2) { Assert-True ($batchLifted -contains $name) "batch r2: the agent defines $name" }
    # The gap is between SUCCESSFUL renews: a failed or slow renew does not reset it; activation counts as the first.
    $fake = [pscustomobject]@{ activeAt = (Get-Date).AddSeconds(-50); lastRenewOkStart = $null; renewOkMaxGapSec = 0 }
    Register-QuietLeaseRenewOk -Lease $fake -CallStarted (Get-Date)
    $g1 = Get-QuietLeaseRenewOkGapSec -Lease $fake
    $fake.lastRenewOkStart = (Get-Date).AddSeconds(-125)
    $g2 = Get-QuietLeaseRenewOkGapSec -Lease $fake
    Assert-True ($fake.renewOkMaxGapSec -ge 49 -and $fake.renewOkMaxGapSec -lt 55 -and $g1 -ge 49 -and $g2 -ge 125 -and $null -eq (Get-QuietLeaseRenewOkGapSec -Lease ([pscustomobject]@{ activeAt = $null }))) "batch r2: renew-ok gap: first success after activation $($fake.renewOkMaxGapSec) s, open gap counted ($g2 s)"
    Assert-True ($agentText -match '\$script:LeaseRenewStaleSec = 120\b') "batch r2: the agent's demotion bound is heavy-slot.py's 120 s"
    Assert-True ((Get-FunctionBody 'Update-QuietLeaseRenew').Contains('Register-QuietLeaseRenewOk -Lease $Lease -CallStarted $c.started') -and (Get-FunctionBody 'Confirm-QuietLeaseHeldForLaunch').Contains('Register-QuietLeaseRenewOk -Lease $l -CallStarted $preLaunchStart')) "batch r2: both successful renew paths (the tick and the pre-launch proof) record the gap"
    $results.batchEnds = $ends

    # ------------------------------------------------------------ batch r3 (sol r2 B1): a batch's job never takes a lease once its batch has ended
    # The VM ends a batch by queueing its close job (and cancelling an open it has no result for). A job of that batch still in the gate, with its lease
    # request PENDING behind a foreign holder, leaves the gate with no lease as soon as the close job is queued: the pending request is stopped, and no
    # request is made after it. RED: the close job is ignored, the request stays pending, and the wait runs on to the queue ceiling.
    $GateProbeSec = 1
    function Invoke-ExemptJobsDuringWait {
        param([string]$WaitingJobId, [double]$WaitedSec)
        $script:Exempt++
        # the VM's end of the batch, once the request is pending: another batch's close job first (it must not end this wait), then this batch's own
        if ($script:Exempt -eq 2) { [IO.File]::WriteAllText((Join-Path $inbox 'lease-batch-close-20261023T2200Z-lbdddddddddddd.job.ps1'), 'x') }
        if ($script:Exempt -eq 4) { [IO.File]::WriteAllText((Join-Path $inbox "lease-batch-close-20261023T2200Z-lb$script:TokC.job.ps1"), 'x'); $script:CloseQueuedAt = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }
        return 0
    }
    Reset-Case @{ request = 'hang' }
    $script:TokC = 'cccccccccccc'; $script:CloseQueuedAt = $null
    $script:AgeAfter = (Get-Date).AddSeconds(20)
    function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter) { return 9999 } return [int]$script:HtAge }   # HtAge: lease-pending-heat-trace sets a queue age (unset = 0)
    $admC = Wait-QuietLeaseAdmission -JobId "lease-batch-open-20261023T2200Z-lb$script:TokC" -Job ([pscustomobject]@{}) -Metadata $meta
    $reqC = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
    $lateC = @($reqC | Where-Object { $null -ne $script:CloseQueuedAt -and $_.ts -gt $script:CloseQueuedAt })
    Assert-True ($admC.label -eq 'cancelled-in-gate' -and $admC.why -match "close job of lease batch $script:TokC" -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "batch r3: a batch job pending in the gate leaves it with no lease once its batch's close job is queued: label=$($admC.label) why=[$($admC.why)]"
    Assert-True ($reqC.Count -eq 1 -and $lateC.Count -eq 0 -and $script:Exempt -ge 4) "batch r3: another batch's close job does not end the wait, and no lease is requested after this batch's close is queued (requests=$($reqC.Count), after the close=$($lateC.Count))"
    Assert-True ((Wait-Until { @($reqC | Where-Object { Test-PidAlive ([int]$_.pid) }).Count -eq 0 } 8) -and @(Get-Calls | Where-Object { $_.cmd -eq 'release' }).Count -ge 1) "batch r3: the pending request is released and its wrapper is gone"
    Get-ChildItem -LiteralPath $inbox -File | Remove-Item -Force
    function Invoke-ExemptJobsDuringWait { param([string]$WaitingJobId, [double]$WaitedSec) $script:Exempt++; return 0 }
    function Get-QueueAgeSeconds { param($Job, $Metadata) return 0 }
    $GateProbeSec = 15
    $results.batchClosed = [ordered]@{ label = $admC.label; requests = $reqC.Count; afterClose = $lateC.Count }

    # ------------------------------------------------------------ batch r3 (sol r2 B2): a batch job never runs past its lease's end, nor on a demoted or lost lease
    # Bachelor's case: a leg admitted on the parked lease with 480 s left (the VM's 360 s budget + 120 s margin) whose job timeout is 900 s. The lease's end
    # bounds it: the monitor loop's check says stop once less than LeaseBatchJobGuardSec is left, and on a demoted or a lost lease. An unbatched job keeps r1.
    Reset-Case @{ request = 'active' }
    $pb = Start-ParkedBatch 'eeeeeeeeeee1'
    $pb.lease.active.until = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 480
    $adm = Wait-QuietLeaseAdmission -JobId 'dve-h-bachelor-cpu-1-lbeeeeeeeeeee1' -Job ([pscustomobject]@{}) -Metadata ([pscustomobject]@{ timeoutSec = 900; maxQueueAgeSec = 600 })
    $script:Admission = $adm
    Assert-True ($adm.leaseBatch.reused -and -not $adm.leaseCoversTimeout -and $adm.leaseBatch.remainingSec -ge 470 -and $adm.leaseBatch.remainingSec -le 480) "batch r3: the leg is admitted on the parked lease with 480 s left for a 900 s timeout ($($adm.leaseBatch.remainingSec) s)"
    $hasStop = [bool](Get-Command Get-LeaseBatchJobStop -ErrorAction SilentlyContinue)
    $stops = [ordered]@{}
    $stops.at480 = if ($hasStop) { Get-LeaseBatchJobStop -Admission $adm -Lease $script:QuietLease } else { $null }
    $pb.lease.active.until = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 5
    $stops.atEnd = if ($hasStop) { Get-LeaseBatchJobStop -Admission $adm -Lease $script:QuietLease } else { $null }
    Assert-True ($null -eq $stops.at480 -and $null -ne $stops.atEnd -and $stops.atEnd.reason -ceq 'lease-end') "batch r3: a leg admitted with 480 s left on a 900 s timeout is stopped at the lease's end, never run past it (with 480 s left: '$($stops.at480.reason)'; at the end: '$($stops.atEnd.reason)')"
    $pb.lease.active.until = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 480
    $pb.lease | Add-Member -NotePropertyName lastRenewOkStart -NotePropertyValue (Get-Date).AddSeconds(-130) -Force
    $stops.demoted = Get-LeaseBatchJobStop -Admission $adm -Lease $script:QuietLease
    $pb.lease.lastRenewOkStart = Get-Date
    $pb.lease.active.until = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 - 60
    $stops.unbatched = Get-LeaseBatchJobStop -Admission ([pscustomobject]@{ mode = 'lease'; label = 'clear' }) -Lease $script:QuietLease
    $pb.lease.active.until = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 480
    Stop-Process -Id $pb.lease.pid -Force
    Assert-True (Wait-Until { $pb.lease.proc.HasExited } 8) "batch r3: the fixture's wrapper is gone"
    $stops.lost = Get-LeaseBatchJobStop -Admission $adm -Lease $script:QuietLease
    Assert-True ($stops.demoted.reason -ceq 'demoted' -and $stops.lost.reason -ceq 'lease-lost' -and $null -eq $stops.unbatched) "batch r3: a demoted or lost lease stops a batch job too, and an unbatched job is never stopped here: $($stops | ConvertTo-Json -Compress -Depth 3)"
    Exit-HeavySlot
    foreach ($name in $liftBatchR3) { Assert-True ($batchLifted -contains $name) "batch r3: the agent defines $name" }
    Assert-True ($agentText -match '\$script:LeaseBatchJobGuardSec = 10\b') "batch r3: the agent stops a batch job 10 s before until="
    $qjb3 = Get-FunctionBody 'Invoke-QueuedJob'
    $iCheck = $qjb3.IndexOf('$leaseStop = Get-LeaseBatchJobStop -Admission $script:Admission -Lease $script:QuietLease'); $iBreak = $qjb3.IndexOf('if ($null -ne $leaseStop) { break }')
    $iKill = $qjb3.IndexOf('elseif ($null -ne $leaseStop) {'); $iTree = $qjb3.IndexOf('Stop-ProcessTree -RootProcessId $jobProcess.Id', [Math]::Max(0, $iKill))
    $iTimeout = $qjb3.IndexOf('$timedOut = $true')
    $iClose = $qjb3.IndexOf('if ($null -ne $leaseStop) { Close-LeaseBatch -Reason "job-stopped-$($leaseStop.reason)"'); $iRes = $qjb3.IndexOf('$result = [pscustomobject]@{')
    Assert-True ($iCheck -ge 0 -and $iCheck -lt $iBreak -and $iBreak -lt $iKill -and $iKill -lt $iTree -and $iTree -lt $iTimeout) "batch r3: the monitor loop checks the bound every slice and a stopped job's whole tree is killed ($iCheck < $iBreak < $iKill < $iTree < $iTimeout)"
    Assert-True ($qjb3.Contains('-NotePropertyName leaseStop -NotePropertyValue') -and $iClose -ge 0 -and $iClose -lt $iRes) "batch r3: the stop is typed in the admission (leaseStop) and the lease is released before the result is published ($iClose < $iRes)"
    $results.batchJobStop = [ordered]@{ at480 = $stops.at480; atEnd = $stops.atEnd.reason; demoted = $stops.demoted.reason; lost = $stops.lost.reason }

    # ------------------------------------------------------------ batch: the wiring in Invoke-QueuedJob and the inbox loop
    $qjb = Get-FunctionBody 'Invoke-QueuedJob'
    $iSync = $qjb.IndexOf('Sync-LeaseBatchForJob -JobId $jobId -Light $isLight'); $iLight = $qjb.IndexOf('if ($isLight) {'); $iGate = $qjb.IndexOf('Wait-QuietLeaseAdmission -JobId $jobId')
    Assert-True ($iSync -ge 0 -and $iSync -lt $iLight -and $iLight -lt $iGate) "batch: a parked lease is synced before the light branch and the gate ($iSync < $iLight < $iGate)"
    Assert-True ($qjb.Contains('underLeaseBatch') -and $qjb.Contains('$script:LeaseBatch.lightJobs.Add($jobId)')) "batch: a light job run while a lease is parked says so in its admission and the receipt"
    $iDone = $qjb.IndexOf('if ($null -ne $launch) { Complete-LeaseBatchJob -JobId $jobId'); $iLaunchB = $qjb.IndexOf('$launch = Start-WindowlessRedirectedProcess'); $iResult = $qjb.IndexOf('$result = [pscustomobject]@{')
    Assert-True ($iLaunchB -ge 0 -and $iLaunchB -lt $iDone -and $iDone -lt $iResult) "batch: only a job that launched marks its lease for parking ($iLaunchB < $iDone < $iResult)"
    $loopB = $code.Substring($code.IndexOf('Invoke-QuarantineReconcile' + "`n"))
    Assert-True ($loopB.Substring(0, 300) -match 'Update-LeaseBatchHold') "batch: the inbox loop runs the hold step every poll"
    Assert-True (([regex]::Matches($code, 'lease", "release"')).Count -eq 2) "batch: no new release call site (Stop-QuietLease and the stale sweep only)"

    # ------------------------------------------------------------ cpu: the legs keep the <= 20 % check, after the lease
    $jobScript = [IO.File]::ReadAllText((Join-Path $RepoRoot 'tools\profiling\bachelor\playback-attr-3-cuda-job.ps1'))
    Assert-True ($jobScript -match '\[double\]\$CpuQuiescenceThresholdPercent = 20\.0\b') "cpu: the leg script's quiescence threshold stays 20 %"
    Assert-True ($jobScript.Contains('if ($cpuTimeUnknown -or -not ($avgTime -le $cpuThresholdPercent)) {') -and $jobScript.Contains("result='VENUE_NOT_QUIESCENT'")) "cpu: the leg refuses (VENUE_NOT_QUIESCENT) above the threshold"
    $qj = Get-FunctionBody 'Invoke-QueuedJob'
    $iWait = $qj.IndexOf('Wait-QuietLeaseAdmission -JobId $jobId'); $iPre = $qj.IndexOf('$pre = Confirm-HeavySlotHeldForLaunch -JobId $jobId'); $iLaunch = $qj.IndexOf('$launch = Start-WindowlessRedirectedProcess -FilePath $psExe')
    Assert-True ($iWait -ge 0 -and $iWait -lt $iPre -and $iPre -lt $iLaunch) "cpu: the job (and so each leg's CPU check) starts only after the lease is active and re-proven ($iWait < $iPre < $iLaunch)"
    Assert-True ($qj.Contains('if ($script:Admission.heavySlotHeld) { $env:HEAVY_SLOT_HELD = [string]$script:Admission.heavySlotHeld }')) "the legs inherit HEAVY_SLOT_HELD from the admission"
    $results.cpu = [ordered]@{ thresholdPercent = 20; order = 'lease-active < pre-launch-renew < launch' }

    # ------------------------------------------------------------ status mode is the rollback and keeps the r1c path
    Assert-True ($HeavySlotMode -ceq 'lease') "the default mode is lease"
    Remove-Item -LiteralPath $heavySlotModeFile -Force -ErrorAction SilentlyContinue
    $m0 = Resolve-HeavySlotMode
    [IO.File]::WriteAllText($heavySlotModeFile, "status`r`n"); $m1 = Resolve-HeavySlotMode
    [IO.File]::WriteAllText($heavySlotModeFile, "LEASE"); $m2 = Resolve-HeavySlotMode
    [IO.File]::WriteAllText($heavySlotModeFile, "statu"); $m3 = Resolve-HeavySlotMode
    $script:HeavySlotModeExplicit = $true; $HeavySlotMode = 'status'
    [IO.File]::WriteAllText($heavySlotModeFile, "lease"); $m4 = Resolve-HeavySlotMode
    $script:HeavySlotModeExplicit = $false; $HeavySlotMode = 'lease'
    Remove-Item -LiteralPath $heavySlotModeFile -Force
    Assert-True ($m0 -eq 'lease' -and $m1 -eq 'status' -and $m2 -eq 'lease' -and $m3 -eq 'lease' -and $m4 -eq 'status') "mode: none=lease file:status=status file:LEASE=lease bad=lease param-status-beats-file=status; got $m0 $m1 $m2 $m3 $m4"
    Assert-True (@($script:Log | Where-Object { $_ -like "heavy-slot-gate: mode=lease source=default (heavy-slot-mode.txt ignored: 'statu')" }).Count -eq 1) "mode: a bad file is logged and ignored"
    Assert-True ($qj -match '\(Resolve-HeavySlotMode\) -eq "lease"\) \{ \$script:Admission = Wait-QuietLeaseAdmission[^\n]*\}\s*\n\s*else \{ \$script:Admission = Wait-HeavySlotAdmission -JobId \$jobId -Job \$Job -Metadata \$metadata \}') "status: status mode routes to the unchanged Wait-HeavySlotAdmission"
    $confirm = Get-FunctionBody 'Confirm-HeavySlotHeldForLaunch'
    Assert-True ($confirm.Contains('if ($script:Admission.mode -eq "lease") { return Confirm-QuietLeaseHeldForLaunch -JobId $JobId }')) "status: only a lease admission takes the lease pre-launch proof"
    $idle = Get-FunctionBody 'Invoke-GateIdleSample'
    Assert-True ($idle.Contains('if ((Resolve-HeavySlotMode) -eq "lease") { return }')) "lease mode never polls heavy-slot.py status while idle"
    $results.status = [ordered]@{ modes = @($m0, $m1, $m2, $m3, $m4) }

    # ------------------------------------------------------------ item 1: the agent runs at Normal priority
    $prio = Get-FunctionBody 'Set-AgentPriority'
    Assert-True ($prio.Contains('[System.Diagnostics.ProcessPriorityClass]::Normal') -and [string]$paramDefaults['AgentPriority'] -ceq 'Normal') "priority: a BelowNormal agent raises itself to Normal by default"
    Assert-True ($code -match 'priority=\$priority') "priority: the agent start line names it (Deploy-AgentSwap's proof reads that line)"
    $results.priority = [ordered]@{ default = $paramDefaults['AgentPriority'] }
  }

    # ================================================================ not-after r1 (LEASE-REQUEST-CHILD-NOT-AFTER-1)
    $notAfterCases = @('notafter-before-request', 'notafter-while-pending', 'notafter-active-then-expired', 'cancel-active-race',
        'notafter-absent-unchanged', 'notafter-pure', 'notafter-source-shape', 'batch-open-notafter', 'batch-refusal-ends-batch',
        'notafter-queuedjob-pregate', 'notafter-queuedjob-gate', 'notafter-queuedjob-post-admission', 'notafter-prelaunch-marker',
        'notafter-prelaunch-confirm', 'notafter-renew-in-light-job', 'notafter-meta-unreadable',
        'queuedjob-cancel-post-admission', 'notafter-renew-crosses-deadline', 'notafter-meta-sticky',
        'liveness-dead-before-request', 'liveness-lapses-while-pending', 'liveness-lapses-at-active', 'liveness-renewed-across-exit75', 'liveness-renewed-across-stall', 'liveness-renewal-stops-lapses',
        'liveness-absent-unchanged', 'liveness-unparseable-expired', 'liveness-pure', 'liveness-reader', 'liveness-ast-guard', 'liveness-queuedjob',
        'liveness-root-shape', 'liveness-lapse-latched',
        'queuedjob-queue-expired-post-admission', 'queuedjob-holder-lost-prelaunch', 'exit-release-pin',
        'batch-close-before-reuse', 'batch-runner-killed-liveness', 'lease-pending-heat-trace',
        'lease-heat-trace-async-retry', 'lease-heat-trace-grant-coverage', 'queuedjob-lease-coverage-at-launch', 'lease-heat-trace-nonlocal-feed',
        'lease-heat-trace-checked-python', 'lease-heat-trace-probe-watchdog',
        'lease-snapshot-bound-straddle', 'lease-probe-watchdog-add-type-failure', 'lease-probe-watchdog-ctor-failure', 'lease-probe-watchdog-arm-paths',
        'lease-start-post-start-failure', 'lease-start-post-start-cleanup-throws')
    foreach ($o in $Only) { Assert-True ($notAfterCases -contains $o) "-Only names a known case: '$o' (known: $($notAfterCases -join ', '))" }
    function Test-Case([string]$Name) { return ($Only.Count -eq 0 -or $Only -contains $Name) }
    # Hang guard: an agent that ignores notAfterUtc would wait these cases out; the queue ceiling then ends them queue-expired
    # (a FAIL on the label assert), never a hang.
    function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter) { return 9999 } return [int]$script:HtAge }   # HtAge: lease-pending-heat-trace sets a queue age (unset = 0)
    function Get-FakeFlag { try { return ([IO.File]::ReadAllText((Join-Path $fx 'quiet-lease.json')) | ConvertFrom-Json) } catch { return $null } }
    function New-NotAfterMeta([double]$InSec) { return [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 600; notAfterUtc = [DateTime]::UtcNow.AddSeconds($InSec).ToString('o') } }
    function Get-RequestArg($Call, [string]$Flag) { $a = @($Call.argv); $i = [Array]::IndexOf($a, $Flag); if ($i -lt 0) { return $null }; return $a[$i + 1] }
    function Get-Released { return @(Get-Calls | Where-Object { $_.cmd -eq 'release' }) }
    # liveness r1: the runner's sidecar, written the way the runner (#633 r5) writes it: a tmp file, then one replacing rename.
    function Write-SidecarRaw([string]$JobId, [string]$Text) {
        $dst = Join-Path $notAfterDir "$JobId.json"
        $tmp = "$dst.$([guid]::NewGuid().ToString('N')).tmp"
        [IO.File]::WriteAllText($tmp, $Text, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($tmp, $dst, $true)
    }
    function New-SidecarJson([string]$JobId, $AliveUntil, [hashtable]$Over = @{}, [int]$Seq = 1) {
        $o = [ordered]@{ schema = 'mlv-app/job-notafter-sidecar/v1'; jobId = $JobId; aliveUntilUtc = $AliveUntil; writtenUtc = [DateTime]::UtcNow.ToString('o'); seq = $Seq; writerPid = $PID; writerHost = 'unit-vm' }
        foreach ($k in $Over.Keys) { if ($null -eq $Over[$k]) { $o.Remove($k) } else { $o[$k] = $Over[$k] } }
        return ($o | ConvertTo-Json -Compress)
    }
    # Returns the aliveUntilUtc it wrote, as the agent types it back ('o', UTC).
    function Write-Sidecar([string]$JobId, [DateTime]$AliveUntilUtc, [int]$Seq = 1) {
        $u = $AliveUntilUtc.ToUniversalTime().ToString('o')
        Write-SidecarRaw $JobId (New-SidecarJson $JobId $u -Seq $Seq)
        return $u
    }

    # CI-FLAKE-HEAVY-SLOT-GATE-QUIET-LEASE-1: renewal driven by the gate's own sidecar reads. Read-JobNotAfterSidecar is wrapped (the
    # real one is still what reads); with $script:SidecarReadHook set the hook runs first, like the LogHook and BeatHook above.
    if ($liftMissing -notcontains 'Read-JobNotAfterSidecar') {
        $script:RealReadSidecar = ${function:Read-JobNotAfterSidecar}
        function Read-JobNotAfterSidecar { param([Parameter(Mandatory = $true)][string]$JobId) if ($null -ne $script:SidecarReadHook) { & $script:SidecarReadHook $JobId }; return (& $script:RealReadSidecar -JobId $JobId) }
    }
    # The runner's renewal as an event: every gate read of <JobId>'s sidecar first rewrites it (aliveUntilUtc = now + ttl, seq++) for
    # the first $MaxRenews reads, then stops. $StallSec > 0 sleeps once in the gate's heartbeat after two renewals (a runner stall
    # between renewals, longer than the window). Reset-Case clears both hooks. Returns the state the case asserts on.
    function Start-SidecarRenewOnRead([string]$JobId, [int]$MaxRenews, [double]$StallSec) {
        $st = @{ jobId = $JobId; max = $MaxRenews; ttl = 3; renews = 0; reads = 0; seq = 1; lastRenewUtc = $null; stallSec = $StallSec; stalled = $false }
        $script:RenewSt = $st
        $null = Write-Sidecar $JobId ([DateTime]::UtcNow.AddSeconds($st.ttl)) 1
        $script:SidecarReadHook = {
            param($Id)
            $s = $script:RenewSt
            if ($Id -ne $s.jobId) { return }
            $s.reads++
            if ($s.renews -lt $s.max) { $s.renews++; $s.seq++; $s.lastRenewUtc = [DateTime]::UtcNow; $null = Write-Sidecar $s.jobId ($s.lastRenewUtc.AddSeconds($s.ttl)) $s.seq }
        }
        if ($StallSec -gt 0) {
            $script:BeatHook = {
                param($Activity)
                $s = $script:RenewSt
                if (-not $s.stalled -and $s.renews -ge 2 -and $Activity -like "gate-wait job=$($s.jobId) *") { $s.stalled = $true; Start-Sleep -Milliseconds ([int]($s.stallSec * 1000)) }
            }
        }
        return $st
    }
    # The wait of liveness-renewed-across-exit75 and -across-stall: two exit-75 refusals, then the third request turns active, with the
    # sidecar renewed on every gate read throughout (a wait of more than 3x the 3 s window).
    function Invoke-RenewedAcrossExit75([string]$Name, [string]$JobId, [double]$StallSec) {
        Reset-Case @{ request = 'refuse_then_active'; refuse_count = 2; active_after = 8 }
        $script:LeaseNotAfterMinAdmitSec = 1
        $script:AgeAfter = (Get-Date).AddSeconds(40)
        $mL = New-NotAfterMeta 3600
        $st = Start-SidecarRenewOnRead $JobId ([int]::MaxValue) $StallSec
        $tR = Get-Date
        $adm = Wait-QuietLeaseAdmission -JobId $JobId -Job ([pscustomobject]@{}) -Metadata $mL
        $elapsed = ((Get-Date) - $tR).TotalSeconds
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        $r75 = @($adm.leaseRefusals | Where-Object { $_.reason -eq 'refused' -and $_.rc -eq 75 })
        Assert-True ($adm.label -eq 'clear' -and $adm.livenessState -ceq 'live' -and $adm.notAfterSource -ceq 'meta') "${Name}: admitted clear under a live sidecar: label=$($adm.label) livenessState=$($adm.livenessState) source=$($adm.notAfterSource) why=$($adm.why)"
        Assert-True ($r75.Count -eq 2 -and $reqs.Count -eq 3) "${Name}: two exit-75 refusals, then the third request turned active: refusals=$($r75.Count) requests=$($reqs.Count)"
        Assert-True ($elapsed -gt 9 -and $st.renews -ge 8 -and $st.reads -ge $st.renews -and [long]$adm.livenessSeq -gt 3 -and [long]$adm.livenessSeq -eq $st.seq) "${Name}: the wait ($([Math]::Round($elapsed, 1)) s) outlasted 3x the $($st.ttl) s window and the gate admitted on the latest renewal, not the first write: renews=$($st.renews) reads=$($st.reads) seq=$($st.seq) admittedSeq=$($adm.livenessSeq)"
        if ($StallSec -gt 0) { Assert-True ($st.stalled -and $StallSec -gt $st.ttl) "${Name}: a stall longer than the window ($StallSec s) ran between renewals: stalled=$($st.stalled)" }
        Exit-HeavySlot
        return [ordered]@{ label = $adm.label; elapsedSec = [Math]::Round($elapsed, 1); renews = $st.renews; reads = $st.reads; admittedSeq = $adm.livenessSeq; stallSec = $StallSec }
    }


    # ------------------------------------------------------------ notafter-before-request: no request once it has passed / is too near
    if (Test-Case 'notafter-before-request') {
        Reset-Case @{ request = 'active' }
        $script:LeaseNotAfterMinAdmitSec = 30
        $script:AgeAfter = (Get-Date).AddSeconds(20)
        $mPast = New-NotAfterMeta -60
        $adm = Wait-QuietLeaseAdmission -JobId 'jNb' -Job ([pscustomobject]@{}) -Metadata $mPast
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -eq 'pre-request' -and $adm.notAfterState -eq 'passed' -and $reqs.Count -eq 0) "notafter-before-request: a passed notAfterUtc ends the wait before any request: label=$($adm.label) phase=$($adm.phase) state=$($adm.notAfterState) requests=$($reqs.Count)"
        Assert-True ($null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting -and $null -eq $adm.leaseRelease -and $adm.leaseRequests -eq 0 -and $adm.notAfterUtc -ceq $mPast.notAfterUtc) "notafter-before-request: no lease, nothing released, the bound recorded: $($adm | ConvertTo-Json -Compress -Depth 4)"
        Assert-True (@($script:Log | Where-Object { $_ -ceq "quiet-lease: expired-not-after job=jNb phase=pre-request notAfterUtc=$($mPast.notAfterUtc) requests=0 releaseRc=none; NOT run, no lease kept" }).Count -eq 1) "notafter-before-request: typed expired-not-after line"
        Reset-Case @{ request = 'active' }
        $script:LeaseNotAfterMinAdmitSec = 30
        $script:AgeAfter = (Get-Date).AddSeconds(20)
        $adm2 = Wait-QuietLeaseAdmission -JobId 'jNm' -Job ([pscustomobject]@{}) -Metadata (New-NotAfterMeta 10)
        $reqs2 = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($adm2.label -eq 'expired-not-after' -and $adm2.phase -eq 'pre-request' -and $adm2.notAfterState -eq 'open' -and $reqs2.Count -eq 0 -and $adm2.why -match 'under the 30 s minimum request window') "notafter-before-request: 10 s left is under the 30 s minimum: no request: label=$($adm2.label) requests=$($reqs2.Count) why=$($adm2.why)"
        $results.notAfterBeforeRequest = [ordered]@{ passed = $adm.label; near = $adm2.label; nearWhy = $adm2.why; requests = $reqs.Count + $reqs2.Count }
    }

    # ------------------------------------------------------------ notafter-while-pending: the request child is bounded by it
    if (Test-Case 'notafter-while-pending') {
        Reset-Case @{ request = 'admit_timeout' }
        $script:LeaseNotAfterMinAdmitSec = 1
        $savedPendingMax = $LeasePendingMaxSec
        $LeasePendingMaxSec = 1800
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $mPend = New-NotAfterMeta 4
        $tP = Get-Date
        try { $adm = Wait-QuietLeaseAdmission -JobId 'jNp' -Job ([pscustomobject]@{}) -Metadata $mPend }
        finally { $LeasePendingMaxSec = $savedPendingMax }
        $elapsed = ((Get-Date) - $tP).TotalSeconds
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        $at = if ($reqs.Count -ge 1) { Get-RequestArg $reqs[0] '--admit-timeout' } else { $null }
        Assert-True ($reqs.Count -eq 1 -and [int]$at -ge 1 -and [int]$at -le 4) "notafter-while-pending: one request, its --admit-timeout bounded by notAfterUtc (1..4, not 1800): requests=$($reqs.Count) admit-timeout=$at"
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -in @('pre-request', 'pending') -and $adm.leaseRequests -eq 1 -and $elapsed -lt 20) "notafter-while-pending: expired-not-after after one request, quickly: label=$($adm.label) phase=$($adm.phase) requests=$($adm.leaseRequests) elapsed=$([Math]::Round($elapsed, 1))s"
        # The phase depends on whether the fake's own clock (started after python start-up) reached its 75 before notAfterUtc.
        if ($adm.phase -eq 'pre-request') {
            $r75 = @($adm.leaseRefusals | Where-Object { $_.reason -eq 'refused' -and $_.rc -eq 75 })
            Assert-True ($r75.Count -eq 1 -and @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=jNp rc=75 *' }).Count -eq 1) "notafter-while-pending: the bounded request's exit 75 is typed and kept: $(@($adm.leaseRefusals) | ConvertTo-Json -Compress)"
        }
        else {
            Assert-True ($null -ne $adm.leaseRelease -and $adm.leaseRelease.phase -eq 'pending') "notafter-while-pending: the still-pending request is released when notAfterUtc passes: $($adm.leaseRelease | ConvertTo-Json -Compress)"
        }
        Assert-True (Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) "notafter-while-pending: no request wrapper is left running"
        Assert-True (@($script:Log | Where-Object { $_ -match "^quiet-lease: request started job=jNp pid=\d+ label='quiet-lease mlv' minutes=31 admitTimeout=${at}s admitTimeoutSource=not-after record=True$" }).Count -eq 1) "notafter-while-pending: the request line names the bound and its source"
        $results.notAfterWhilePending = [ordered]@{ admitTimeout = [int]$at; phase = $adm.phase; elapsedSec = [Math]::Round($elapsed, 1) }
    }

    # ------------------------------------------------------------ notafter-active-then-expired: active after the bound -> released
    if (Test-Case 'notafter-active-then-expired') {
        Reset-Case @{ request = 'active'; active_after = 3 }
        $script:LeaseNotAfterMinAdmitSec = 1
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $mAct = New-NotAfterMeta 2
        $script:NotAfterAt = [DateTimeOffset]::Parse($mAct.notAfterUtc).UtcDateTime
        # Lands the race: the request is filed under a live bound, then turns active AND notAfterUtc passes before the gate's
        # next read of the wrapper (the same iteration, right after the request-started line).
        $script:LogHook = {
            param($Message)
            if ($script:HookFired -or $Message -notlike 'quiet-lease: request started job=jNa *') { return }
            $script:HookFired = $true
            $null = Wait-Until { $f = Get-FakeFlag; $null -ne $f -and $f.state -eq 'active' -and [DateTime]::UtcNow -gt $script:NotAfterAt } 10
            Start-Sleep -Milliseconds 500
        }
        $adm = Wait-QuietLeaseAdmission -JobId 'jNa' -Job ([pscustomobject]@{}) -Metadata $mAct
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($script:HookFired -and $reqs.Count -eq 1) "notafter-active-then-expired: one request, the race landed"
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -eq 'post-active' -and $adm.notAfterState -eq 'passed') "notafter-active-then-expired: a lease that turns active after notAfterUtc is not handed to the job: label=$($adm.label) phase=$($adm.phase)"
        Assert-True ((Get-Released).Count -eq 1 -and $null -ne $adm.leaseRelease -and [string]$adm.leaseRelease.releaseRc -ceq '0' -and $adm.leaseRelease.phase -eq 'post-active') "notafter-active-then-expired: released once, the release recorded: $($adm.leaseRelease | ConvertTo-Json -Compress)"
        Assert-True ((Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0 -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "notafter-active-then-expired: wrapper gone, record removed, no lease kept"
        Assert-True (@($script:Log | Where-Object { $_ -ceq "quiet-lease: expired-not-after job=jNa phase=post-active notAfterUtc=$($mAct.notAfterUtc) requests=1 releaseRc=0; NOT run, no lease kept" }).Count -eq 1) "notafter-active-then-expired: typed line"
        $results.notAfterActiveThenExpired = [ordered]@{ phase = $adm.phase; releaseRc = $adm.leaseRelease.releaseRc }
    }

    # ------------------------------------------------------------ cancel-active-race: a cancel file that lands as the lease turns active
    if (Test-Case 'cancel-active-race') {
        Reset-Case @{ request = 'active' }
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $script:CancelFile = Join-Path $cancel 'jCa.cancel'
        $script:LogHook = {
            param($Message)
            if ($script:HookFired -or $Message -notlike 'quiet-lease: request started job=jCa *') { return }
            $script:HookFired = $true
            $null = Wait-Until { $f = Get-FakeFlag; $null -ne $f -and $f.state -eq 'active' } 10
            [IO.File]::WriteAllText($script:CancelFile, 'x')
            Start-Sleep -Milliseconds 500
        }
        try { $adm = Wait-QuietLeaseAdmission -JobId 'jCa' -Job ([pscustomobject]@{}) -Metadata $meta }
        finally { Remove-Item -LiteralPath $script:CancelFile -Force -ErrorAction SilentlyContinue }
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($script:HookFired -and $adm.label -eq 'cancelled-in-gate' -and $adm.releasedAfterActive -eq $true) "cancel-active-race: a cancel file present when the lease turns active ends the wait: label=$($adm.label) releasedAfterActive=$($adm.releasedAfterActive)"
        Assert-True ((Get-Released).Count -eq 1 -and $null -ne $adm.leaseRelease -and [string]$adm.leaseRelease.releaseRc -ceq '0') "cancel-active-race: released once, the release recorded: $($adm.leaseRelease | ConvertTo-Json -Compress)"
        Assert-True ((Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "cancel-active-race: wrapper gone, no lease kept"
        Assert-True (@($script:Log | Where-Object { $_ -like 'quiet-lease: cancelled-in-gate job=jCa phase=post-active releaseRc=0;*' }).Count -eq 1) "cancel-active-race: typed line"
        $results.cancelActiveRace = [ordered]@{ label = $adm.label; releaseRc = $adm.leaseRelease.releaseRc }
    }

    # ------------------------------------------------------------ notafter-absent-unchanged: no notAfterUtc = today's request
    # The unchanged half (argv) passes on a base agent BY DESIGN; this case's RED is the new typed fields.
    if (Test-Case 'notafter-absent-unchanged') {
        Reset-Case @{ request = 'active' }
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $adm = Wait-QuietLeaseAdmission -JobId 'jNn' -Job ([pscustomobject]@{}) -Metadata $meta
        $req = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($adm.label -eq 'clear' -and $req.Count -eq 1 -and ($req[0].argv -join ' ') -ceq 'lease request --label quiet-lease mlv --minutes 31 --admit-timeout 1 --slots 2') "notafter-absent-unchanged: the request argv is today's: [$($req[0].argv -join ' ')]"
        Assert-True ($null -eq $adm.notAfterUtc -and $adm.admitTimeoutSec -eq 1 -and $adm.admitTimeoutSource -ceq 'pending-max') "notafter-absent-unchanged: the admission types notAfterUtc=null admitTimeoutSec=1 admitTimeoutSource=pending-max: $($adm | Select-Object notAfterUtc, admitTimeoutSec, admitTimeoutSource | ConvertTo-Json -Compress)"
        $line = @($script:Log | Where-Object { $_ -like 'quiet-lease: request started job=jNn *' })
        Assert-True ($line.Count -eq 1 -and $line[0] -match "^quiet-lease: request started job=jNn pid=\d+ label='quiet-lease mlv' minutes=31 admitTimeout=1s admitTimeoutSource=pending-max record=True$") "notafter-absent-unchanged: the request line: $($line -join ' | ')"
        Exit-HeavySlot
        $results.notAfterAbsentUnchanged = [ordered]@{ argv = ($req[0].argv -join ' '); admitTimeoutSource = $adm.admitTimeoutSource }
    }

    # ------------------------------------------------------------ notafter-pure: the verdict, the request bound, the result shape
    if (Test-Case 'notafter-pure') {
        foreach ($name in $liftNotAfter) { Assert-True ($liftMissing -notcontains $name) "agent defines $name" }
        $minAst = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$script:LeaseNotAfterMinAdmitSec' }, $true)
        Assert-True ($null -ne $minAst -and $minAst.Right.Expression.Value -eq 30) "the agent's minimum request window is 30 s (heavy-slot.py's 30 min cooldown follows any lease, however short)"
        $now = [DateTime]::new(2026, 10, 24, 2, 0, 0, [DateTimeKind]::Utc)
        $at3 = [DateTime]::new(2026, 10, 24, 3, 0, 0, [DateTimeKind]::Utc)
        $z3 = '2026-10-24T03:00:00.0000000Z'
        $vt = @(
            @{ n = 'missing'; m = [pscustomobject]@{ timeoutSec = 1 }; s = 'none'; o = $null; r = $null }
            @{ n = 'null'; m = [pscustomobject]@{ notAfterUtc = $null }; s = 'none'; o = $null; r = $null }
            @{ n = 'blank'; m = [pscustomobject]@{ notAfterUtc = '  ' }; s = 'none'; o = $null; r = $null }
            @{ n = 'future-z'; m = [pscustomobject]@{ notAfterUtc = $z3 }; s = 'open'; o = $z3; r = 3600 }
            @{ n = 'future-offset'; m = [pscustomobject]@{ notAfterUtc = '2026-10-24T05:00:00+02:00' }; s = 'open'; o = $z3; r = 3600 }
            @{ n = 'past'; m = [pscustomobject]@{ notAfterUtc = '2026-10-24T01:00:00Z' }; s = 'passed'; o = '2026-10-24T01:00:00.0000000Z'; r = -3600 }
            @{ n = 'exactly-now'; m = [pscustomobject]@{ notAfterUtc = '2026-10-24T02:00:00Z' }; s = 'passed'; o = '2026-10-24T02:00:00.0000000Z'; r = 0 }
            @{ n = 'datetime-utc'; m = [pscustomobject]@{ notAfterUtc = $at3 }; s = 'open'; o = $z3; r = 3600 }
            @{ n = 'datetime-local'; m = [pscustomobject]@{ notAfterUtc = $at3.ToLocalTime() }; s = 'open'; o = $z3; r = 3600 }
            @{ n = 'datetime-unspecified'; m = [pscustomobject]@{ notAfterUtc = [DateTime]::SpecifyKind($at3, [DateTimeKind]::Unspecified) }; s = 'open'; o = $z3; r = 3600 }
            @{ n = 'datetimeoffset'; m = [pscustomobject]@{ notAfterUtc = [DateTimeOffset]::new(2026, 10, 24, 5, 0, 0, [TimeSpan]::FromHours(2)) }; s = 'open'; o = $z3; r = 3600 }
            @{ n = 'json-z'; m = ('{"notAfterUtc":"2026-10-24T03:00:00.0000000Z"}' | ConvertFrom-Json); s = 'open'; o = $z3; r = 3600 }
            @{ n = 'json-offset'; m = ('{"notAfterUtc":"2026-10-24T05:00:00+02:00"}' | ConvertFrom-Json); s = 'open'; o = $z3; r = 3600 }
            @{ n = 'garbage'; m = [pscustomobject]@{ notAfterUtc = 'not-a-date' }; s = 'unparseable'; o = $null; r = $null }
            @{ n = 'number'; m = [pscustomobject]@{ notAfterUtc = 12345 }; s = 'unparseable'; o = $null; r = $null }
        )
        $verdicts = [ordered]@{}
        foreach ($c in $vt) {
            $v = Get-JobNotAfterVerdict -Metadata $c.m -NowUtc $now
            $remOk = if ($null -eq $c.r) { $null -eq $v.remainingSec } else { $null -ne $v.remainingSec -and [Math]::Abs([double]$v.remainingSec - $c.r) -lt 0.001 }
            Assert-True ($v.state -ceq $c.s -and $v.notAfterUtc -ceq $c.o -and $remOk -and -not [string]::IsNullOrWhiteSpace($v.why)) "notafter-pure: verdict $($c.n) is $($c.s) $($c.o) $($c.r): $($v | ConvertTo-Json -Compress)"
            $verdicts[$c.n] = $v.state
        }
        $savedPendingMax = $LeasePendingMaxSec
        $LeasePendingMaxSec = 1800
        $script:LeaseNotAfterMinAdmitSec = 30
        try {
            $aNone = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'none'; remainingSec = $null })
            $aFar = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 7200.0 })
            $aHuge = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 2.5e11 })
            $aEdge = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 1800.5 })
            $aNear = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 600.7 })
            $aMin = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 29.9 })
            $aPassed = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'passed'; remainingSec = -1.0 })
            $aBad = Get-QuietLeaseAdmitTimeout -Verdict ([pscustomobject]@{ state = 'unparseable'; remainingSec = $null })
        }
        finally { $LeasePendingMaxSec = $savedPendingMax }
        Assert-True ($aNone.ok -and $aNone.sec -eq 1800 -and $aNone.source -ceq 'pending-max') "notafter-pure: no notAfterUtc asks for LeasePendingMaxSec: $($aNone | ConvertTo-Json -Compress)"
        Assert-True ($aFar.ok -and $aFar.sec -eq 1800 -and $aFar.source -ceq 'pending-max' -and $aHuge.ok -and $aHuge.sec -eq 1800 -and $aHuge.source -ceq 'pending-max') "notafter-pure: a far notAfterUtc keeps pending-max: $($aFar | ConvertTo-Json -Compress) $($aHuge | ConvertTo-Json -Compress)"
        Assert-True ($aEdge.ok -and $aEdge.sec -eq 1800 -and $aEdge.source -ceq 'pending-max') "notafter-pure: floor(1800.5) = 1800 is not below pending-max: $($aEdge | ConvertTo-Json -Compress)"
        Assert-True ($aNear.ok -and $aNear.sec -eq 600 -and $aNear.source -ceq 'not-after') "notafter-pure: a near notAfterUtc bounds the request to its whole seconds: $($aNear | ConvertTo-Json -Compress)"
        Assert-True (-not $aMin.ok -and $aMin.why -ceq 'only 29 s left before notAfterUtc, under the 30 s minimum request window') "notafter-pure: under the minimum, no request: $($aMin | ConvertTo-Json -Compress)"
        Assert-True (-not $aPassed.ok -and -not $aBad.ok) "notafter-pure: a passed or unparseable verdict never requests"
        $r0 = New-LeaseReleaseRecord -Phase 'pending' -Release ([pscustomobject]@{ releaseRc = '0'; rc = 0; killed = @() })
        Assert-True ($r0.phase -ceq 'pending' -and $r0.releaseRc -ceq '0' -and $r0.rc -eq 0 -and @($r0.killed).Count -eq 0 -and $null -eq (New-LeaseReleaseRecord -Phase 'pending' -Release $null)) "notafter-pure: leaseRelease is {phase, releaseRc, rc, killed}, or null when nothing was released"
        # Publish-NotAfterExpiredResult, with the publish and move stubbed.
        $script:Published = $null; $script:Moved = $null
        function Publish-JobResult { param([string]$JobId, $Result) $script:Published = $Result; return (Join-Path $fx "$JobId.result.json") }
        function Move-JobArtifacts { param([string]$JobId, [string]$JobPath, [string]$DestinationDirectory) $script:Moved = "$JobId|$DestinationDirectory" }
        $processed = Join-Path $fx 'processed'
        $pubJob = [pscustomobject]@{ BaseName = 'jPub.job'; FullName = (Join-Path $fx 'jPub.job.ps1') }
        $pubMeta = [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 0 }
        $vPast = [pscustomobject]@{ state = 'passed'; notAfterUtc = '2026-10-24T01:00:00.0000000Z'; remainingSec = -5.0; why = 'passed' }
        Publish-NotAfterExpiredResult -Job $pubJob -Metadata $pubMeta -Admission ([pscustomobject]@{ label = 'expired-not-after'; phase = 'pending' }) -QueueAgeSec 7 -Verdict $vPast
        $p = $script:Published
        Assert-True ($null -ne $p -and $p.jobId -ceq 'jPub' -and $p.exitCode -eq 125 -and $null -eq $p.startedUtc -and $p.cancelled -eq $false -and $p.timedOut -eq $false -and $p.queueExpired -eq $true -and $p.notAfterExpired -eq $true) "notafter-pure: result exitCode 125, queueExpired + notAfterExpired true, not cancelled, never started: $($p | ConvertTo-Json -Compress -Depth 4)"
        Assert-True ($p.notAfterUtc -ceq '2026-10-24T01:00:00.0000000Z' -and $p.notAfterState -ceq 'passed' -and @($p.killedProcessIds).Count -eq 0 -and $p.queueAgeSec -eq 7 -and $p.admission.phase -ceq 'pending' -and $p.stdout -ceq '') "notafter-pure: result carries the bound, the state and the admission"
        Assert-True ($p.stderr -ceq 'notAfterUtc 2026-10-24T01:00:00.0000000Z passed before launch (pending); job was not executed') "notafter-pure: result stderr: $($p.stderr)"
        Assert-True ($script:Moved -ceq "jPub|$processed" -and @($script:Log | Where-Object { $_ -ceq "not-after expired job=jPub notAfterUtc=2026-10-24T01:00:00.0000000Z phase=pending result=$(Join-Path $fx 'jPub.result.json')" }).Count -eq 1) "notafter-pure: artifacts moved to processed, typed line"
        $script:Published = $null
        Publish-NotAfterExpiredResult -Job $pubJob -Metadata $pubMeta -QueueAgeSec 0 -Verdict ([pscustomobject]@{ state = 'unparseable'; notAfterUtc = $null; remainingSec = $null; why = 'garbage' })
        Assert-True ($null -eq $script:Published.admission -and $script:Published.notAfterState -ceq 'unparseable' -and $script:Published.exitCode -eq 125 -and $script:Published.stderr -like '*(pre-gate); job was not executed') "notafter-pure: before the gate there is no admission; unparseable is typed: $($script:Published.stderr)"
        $results.notAfterPure = [ordered]@{ verdicts = $verdicts; near = $aNear.sec; min = $aMin.why }
    }

    # ------------------------------------------------------------ notafter-source-shape: where Invoke-QueuedJob checks it
    if (Test-Case 'notafter-source-shape') {
        $qj = Get-FunctionBody 'Invoke-QueuedJob'
        $probe = '$notAfter = Get-JobNotAfterVerdict -Metadata $metadata'
        $iPre = $qj.IndexOf($probe)
        $iGate = $qj.IndexOf('Wait-QuietLeaseAdmission -JobId $jobId')
        $iLabel = $qj.IndexOf('if ($script:Admission.label -eq "expired-not-after") {')
        $iRe = if ($iLabel -ge 0) { $qj.IndexOf($probe, $iLabel) } else { -1 }
        $iMarker = $qj.IndexOf('$marker = New-LaunchingMarker')
        Assert-True ($iPre -ge 0 -and $iPre -lt $iGate) "notafter-source-shape: Invoke-QueuedJob reads notAfterUtc before the gate ($iPre < $iGate)"
        Assert-True ($iLabel -gt $iGate) "notafter-source-shape: it handles label expired-not-after after the gate ($iGate < $iLabel)"
        Assert-True ($iRe -gt $iLabel -and $iRe -lt $iMarker) "notafter-source-shape: the post-admission re-check sits before New-LaunchingMarker ($iLabel < $iRe < $iMarker)"
        # LEASE-NOT-AFTER-HARDENING-1: what each exit DOES (refuse, release, retire) is run by the notafter-queuedjob-* and
        # notafter-prelaunch-* cases; only where the last check sits is read from the source here.
        $iConfirm = $qj.IndexOf('$pre = Confirm-HeavySlotHeldForLaunch -JobId $jobId')
        $iLaunch = $qj.IndexOf('$launch = Start-WindowlessRedirectedProcess')
        $iLast = if ($iConfirm -ge 0) { $qj.IndexOf($probe, $iConfirm) } else { -1 }
        Assert-True ($iMarker -lt $iConfirm -and $iLast -gt $iConfirm -and $iLast -lt $iLaunch) "notafter-source-shape: the last re-check follows the launching marker and the slot re-confirm and precedes the child ($iMarker < $iConfirm < $iLast < $iLaunch)"
        $exitBody = (Get-FunctionBody 'Exit-HeavySlot') -replace '\s+', ' '
        # The merged agent's body: #633's lease-batch Exit-HeavySlot, which not-after r1 leaves as it is.
        $exitWant = @'
function Exit-HeavySlot {
    # Ends the admitted job's holder (status mode) or quiet lease (lease mode), so heavy-slot.py releases the job's slot.
    # Idempotent. Called on every path out of a job: normal end, a thrown leg, a pre-launch refusal, and agent exit.
    # lease-batch r1: a batch job that launched and ended (Complete-LeaseBatchJob) PARKS its batch's lease instead; every
    # other way out of a batch job releases it (Close-LeaseBatch).
    $h = $script:HeavySlotHolder
    $script:HeavySlotHolder = $null
    if ($null -ne $h) { Stop-HeavySlotHolder -Holder $h -Why "job done" }
    $l = $script:QuietLease
    $script:QuietLease = $null
    $keep = $script:LeaseBatchKeep
    $script:LeaseBatchKeep = $false
    if ($null -eq $l) { return }
    $b = $script:LeaseBatch
    if ($null -ne $b -and [object]::ReferenceEquals($b.lease, $l)) {
        if ($keep -and -not $l.proc.HasExited) {
            $b.lastActivity = Get-Date
            Add-AgentLogLine "quiet-lease: batch-parked token=$($b.token) lease=$($l.id) jobs=$($b.jobs.Count) remaining=$(Get-QuietLeaseRemainingSec -Lease $l)s idleBound=${LeaseBatchIdleSec}s"
        }
        else { Close-LeaseBatch -Reason $(if ($keep) { "lease-ended" } else { "job-not-completed" }) -Why "the batch job left without a normal end, or the wrapper exited" }
        return
    }
    $null = Stop-QuietLease -Lease $l -Why "job done"
}
'@ -replace '\s+', ' '
        Assert-True ($exitBody.Trim() -ceq $exitWant.Trim()) "notafter-source-shape: Exit-HeavySlot is unchanged (it returns nothing, so its bare calls leak no output)"
        Assert-True (-not (Get-FunctionBody 'Wait-HeavySlotAdmission').Contains('NotAfter')) "notafter-source-shape: status mode's wait loop is unchanged (Invoke-QueuedJob's checks cover it)"
        $results.notAfterSourceShape = [ordered]@{ order = 'pre-gate < gate < expired-not-after < post-admission re-check < launching marker < slot re-confirm < last re-check < child' }
    }

    # ------------------------------------------------------------ exit-release-pin: every Exit-HeavySlot call site, from the agent's AST
    # QUIET-LEASE-EXIT-RELEASE-EVIDENCE-1: a call site is RECORDED when, in its own statement block, an earlier statement assigns
    # `$<v> = Stop-QuietLease -Lease $script:QuietLease` and the block passes `-Release $<v>` to New-LeaseReleaseRecord. Any other
    # site must be a row below (enclosing function + its trimmed source line) with a reason. A row that matches no site, or a site
    # that is recorded, is stale and fails, so the list cannot outlive the code it excuses.
    if (Test-Case 'exit-release-pin') {
        $exitAllow = @(
            [pscustomobject]@{ fn = '(script)'; line = 'Exit-HeavySlot'; why = "the inbox loop's finally: the normal end's release, made after its result is published (no open record to carry it; Stop-QuietLease types it as why=[job done]), and the backstop for a thrown leg; after an exit before launch it finds nothing to end, that exit released and recorded already" }
            [pscustomobject]@{ fn = '(script)'; line = 'if ($agentMutexHeld) { Exit-HeavySlot }'; why = 'agent-mutex teardown: the agent is leaving (exception or stop request), no job record is open to carry a release' }
        )
        $agentLines = $agentText -split "`r?`n"
        $exitSites = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -ceq 'Exit-HeavySlot' }, $true))
        $exitTable = [System.Collections.Generic.List[object]]::new()
        $exitBad = [System.Collections.Generic.List[string]]::new()
        foreach ($s in $exitSites) {
            $fnAst = $s.Parent; while ($null -ne $fnAst -and $fnAst -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) { $fnAst = $fnAst.Parent }
            $fnName = if ($null -ne $fnAst) { $fnAst.Name } else { '(script)' }
            $stmt = $s; $blk = $s.Parent
            while ($null -ne $blk -and $blk -isnot [System.Management.Automation.Language.StatementBlockAst] -and $blk -isnot [System.Management.Automation.Language.NamedBlockAst]) { $stmt = $blk; $blk = $blk.Parent }
            $before = @($blk.Statements | Where-Object { $_.Extent.EndOffset -le $stmt.Extent.StartOffset } | ForEach-Object { $_.Extent.Text })
            $recorded = $false
            foreach ($t in $before) {
                $m = [regex]::Match($t, '\$(\w+) = Stop-QuietLease -Lease \$script:QuietLease\b')
                if ($m.Success -and [regex]::IsMatch($blk.Extent.Text, "New-LeaseReleaseRecord -Phase [^\r\n]*-Release \`$$($m.Groups[1].Value)\b")) { $recorded = $true }
            }
            $lineText = $agentLines[$s.Extent.StartLineNumber - 1].Trim()
            $rows = @($exitAllow | Where-Object { $_.fn -ceq $fnName -and $_.line -ceq $lineText })
            $exitTable.Add([pscustomobject]@{ line = $s.Extent.StartLineNumber; fn = $fnName; source = $lineText; recorded = $recorded; allowlisted = ($rows.Count -gt 0) })
            if (-not $recorded -and $rows.Count -eq 0) { $exitBad.Add("line $($s.Extent.StartLineNumber) in ${fnName}: [$lineText] is neither preceded by a recorded release nor allowlisted") }
            if ($recorded -and $rows.Count -gt 0) { $exitBad.Add("line $($s.Extent.StartLineNumber) in ${fnName}: [$lineText] records its release, so its allowlist row is stale") }
        }
        foreach ($r in $exitAllow) {
            $hits = @($exitTable | Where-Object { $_.fn -ceq $r.fn -and $_.source -ceq $r.line }).Count
            if ($hits -ne 1) { $exitBad.Add("allowlist row [$($r.fn)] [$($r.line)] matches $hits call sites, not exactly 1") }
        }
        Assert-True ($exitSites.Count -ge 1 -and $exitBad.Count -eq 0) "exit-release-pin: every Exit-HeavySlot call site records its release or is allowlisted with a reason: $($exitBad -join ' ; ')"
        # Light jobs run from inside a gate-wait never call it: the waiting job's lease is the wait's own local.
        $ewAst = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-ExemptJobsDuringWait' }, $true)
        Assert-True ($null -ne $ewAst -and $null -eq $ewAst.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -ceq 'Exit-HeavySlot' }, $true)) "exit-release-pin: Invoke-ExemptJobsDuringWait never calls Exit-HeavySlot (its mention is a comment)"
        $results.exitReleasePin = [ordered]@{
            sites = @($exitTable | ForEach-Object { "$($_.line) $($_.fn) recorded=$($_.recorded) allowlisted=$($_.allowlisted)" })
            recorded = @($exitTable | Where-Object { $_.recorded }).Count; allowlisted = @($exitTable | Where-Object { $_.allowlisted }).Count
            allow = @($exitAllow | ForEach-Object { "$($_.fn) [$($_.line)]: $($_.why)" })
        }
    }

    # ================================================================ LEASE-NOT-AFTER-HARDENING-1 r1
    # Invoke-QueuedJob, lifted and RUN against the fake heavy-slot.py. Its disk and launch steps are stubbed: the claim and the
    # launching marker are one-field files under running\, the result and the artifact move are recorded, and the child launch
    # is recorded and throws (no child is ever created). The REAL Get-JobMetadata reads a meta.json written to inbox\; the
    # wrapper keeps the object it returned, so a case moves that job's notAfterUtc into the past at a chosen step, which is
    # what the verdict sees when the clock passes the deadline there.
    $qjCases = @('notafter-queuedjob-pregate', 'notafter-queuedjob-gate', 'notafter-queuedjob-post-admission', 'notafter-prelaunch-marker', 'notafter-prelaunch-confirm', 'notafter-meta-unreadable',
        'queuedjob-cancel-post-admission', 'notafter-meta-sticky', 'liveness-queuedjob', 'liveness-root-shape',
        'queuedjob-queue-expired-post-admission', 'queuedjob-holder-lost-prelaunch', 'batch-close-before-reuse', 'queuedjob-lease-coverage-at-launch',
        'lease-start-post-start-cleanup-throws')
    if (@($qjCases | Where-Object { Test-Case $_ }).Count -gt 0) {
        # STAGE-JOB-LEASE-EXEMPT-1 r2: Invoke-QueuedJob classifies the job through Get-GateExemptClass, which reads both patterns.
        foreach ($name in @('Invoke-QueuedJob', 'Complete-AdmissionWaitMarker', 'Publish-GateUnlaunchedResult', 'Get-GateExemptClass')) { . ([scriptblock]::Create((Get-FunctionBody $name))) }
        . ([scriptblock]::Create(((Get-FunctionBody 'Get-JobMetadata') -replace '^function Get-JobMetadata', 'function Get-JobMetadataReal')))
        # QUIET-LEASE-EXIT-RELEASE-EVIDENCE-1: the REAL queue-expired publisher and holder-loss recorder run under counting wrappers,
        # so a case reads the published result and the kept holder-lost event, not a stub's copy.
        foreach ($name in @('Invoke-QueueExpiredJob', 'Add-HeavySlotHolderLoss', 'Add-HeavySlotHolderLossToAdmission')) {
            . ([scriptblock]::Create(((Get-FunctionBody $name) -replace "^function $name", "function ${name}Real")))
        }
        $inbox = Join-Path $fx 'inbox'; $running = Join-Path $fx 'running'; $outbox = Join-Path $fx 'outbox'; $processed = Join-Path $fx 'processed'
        New-Item -ItemType Directory -Force -Path $inbox, $running, $outbox | Out-Null
        $JobTimeoutSec = 1800; $PollSeconds = 2; $Root = $fx; $psExe = 'unit-pwsh.exe'
        $GateExemptJobPattern = [string]$paramDefaults['GateExemptJobPattern']
        Assert-True ($GateExemptJobPattern -and 'jQp' -notmatch $GateExemptJobPattern) "the agent's GateExemptJobPattern is read and does not match the case job ids: '$GateExemptJobPattern'"
        # Unset, -cmatch $null matches every id as "stage" and every case below would skip the gate.
        $GateStageJobPattern = [string]$paramDefaults['GateStageJobPattern']
        Assert-True ($GateStageJobPattern -and 'jQp' -cnotmatch $GateStageJobPattern) "the agent's GateStageJobPattern is read and does not match the case job ids: '$GateStageJobPattern'"
        function Get-JobMetadata { param([string]$JobId) $m = Get-JobMetadataReal -JobId $JobId; $script:LastMeta = $m; return $m }
        function New-AdmissionWaitMarker { param([string]$JobId, $Job, $Metadata, [string]$GateReason) $script:QJ.claims.Add($JobId); [IO.File]::WriteAllText((Join-Path $running "$JobId.started.json"), '{"phase":"admission-wait"}'); return $true }
        function New-LaunchingMarker {
            param([string]$JobId, $Job, $Metadata, [string]$OutFile, [string]$ErrFile, [string]$StartedUtc, [int]$TimeoutSec)
            $script:QJ.launchingMarkers.Add($JobId)
            [IO.File]::WriteAllText((Join-Path $running "$JobId.started.json"), '{"phase":"launching"}')
            if ($null -ne $script:QJ.markerHook) { & $script:QJ.markerHook }
            return [pscustomobject]@{ phase = 'launching'; childPid = $null; childStartUtc = $null; childStartEpochMs = $null; childImagePath = $null; heartbeatGeneration = $null }
        }
        function Complete-StartedMarker {
            param([string]$MarkerPath, [string]$Status, [string]$ResultPath)
            $m = Read-JsonFile -Path $MarkerPath
            $script:QJ.retired.Add("$(Split-Path -Leaf $MarkerPath)|$($m.phase)|$Status")
            Remove-Item -LiteralPath $MarkerPath -Force -ErrorAction SilentlyContinue
        }
        function Invoke-QueueExpiredJob { param($Job, $Metadata, [int]$QueueAgeSec, $Admission) $script:QJ.queueExpired++; Invoke-QueueExpiredJobReal @PSBoundParameters }
        function Add-HeavySlotHolderLossToAdmission { param([string]$JobId, $Admission) if ($null -ne $script:QJ.admittedHook) { & $script:QJ.admittedHook } }
        function Add-HeavySlotHolderLoss { param([string]$JobId, [string]$Phase, [string]$Why, [string]$Held, [string]$Reason, $LeaseRelease) $script:QJ.holderLoss.Add("$Phase|$Reason|$Why"); Add-HeavySlotHolderLossReal @PSBoundParameters }
        function Set-AgentKeepAwake { param([bool]$Enable) return $true }
        function Start-WindowlessRedirectedProcess {
            param([string]$FilePath, [string[]]$ArgumentList, [string]$OutFile, [string]$ErrFile, [string]$WorkingDirectory)
            $script:QJ.launches.Add("$FilePath $($ArgumentList -join ' ')")
            if ($script:QJLaunchDelaySec -gt 0) { Start-Sleep -Milliseconds ([int](1000 * [double]$script:QJLaunchDelaySec)) }   # r5: the helper's out/err opens, slow on a share root
            throw 'unit: the child launch was reached'
        }
        function Publish-JobResult { param([string]$JobId, $Result) $script:QJ.published.Add($Result); return (Join-Path $outbox "$JobId.result.json") }
        function Move-JobArtifacts { param([string]$JobId, [string]$JobPath, [string]$DestinationDirectory) $script:QJ.moved.Add("$JobId|$DestinationDirectory") }
        function Reset-QueuedJobCase([hashtable]$Control, [string]$JobId, $MetaBody) {
            Reset-Case $Control
            $script:LeaseNotAfterMinAdmitSec = 1
            $script:AgeAfter = (Get-Date).AddSeconds(30)
            Get-ChildItem -LiteralPath $inbox, $running -File | Remove-Item -Force
            Remove-Item Env:\MLV_AGENT_ADMISSION, Env:\MLV_AGENT_ADMISSION_THERMAL, Env:\MLV_AGENT_ADMISSION_WAIT_SEC, Env:\HEAVY_SLOT_HELD -ErrorAction SilentlyContinue
            $script:LastMeta = $null
            $script:HolderLossByJob = @{}
            $script:QJ = [pscustomobject]@{
                claims = [System.Collections.Generic.List[string]]::new(); launchingMarkers = [System.Collections.Generic.List[string]]::new()
                retired = [System.Collections.Generic.List[string]]::new(); holderLoss = [System.Collections.Generic.List[string]]::new()
                launches = [System.Collections.Generic.List[string]]::new(); published = [System.Collections.Generic.List[object]]::new()
                moved = [System.Collections.Generic.List[string]]::new(); queueExpired = 0; markerHook = $null; admittedHook = $null
            }
            if ($null -ne $MetaBody) { [IO.File]::WriteAllText((Join-Path $inbox "$JobId.meta.json"), $MetaBody) }
            return [pscustomobject]@{ BaseName = "$JobId.job"; FullName = (Join-Path $inbox "$JobId.job.ps1") }
        }
        function New-NotAfterMetaJson([double]$InSec) { return ([ordered]@{ timeoutSec = 1800; maxQueueAgeSec = 600; notAfterUtc = [DateTime]::UtcNow.AddSeconds($InSec).ToString('o') } | ConvertTo-Json -Compress) }
        function Set-DeadlinePassed { $script:LastMeta | Add-Member -NotePropertyName notAfterUtc -NotePropertyValue ([DateTime]::UtcNow.AddSeconds(-1).ToString('o')) -Force }
        # One not-after exit, as run: refused (one expired result, never started, in the named phase), NO child launch, the
        # artifacts moved, the claim retired as listed (marker file|its phase|status), and, for a leased exit, the lease
        # released once with that release recorded, the wrapper gone and no lease kept.
        function Assert-NotAfterExit([string]$Case, [string]$JobId, [string]$Phase, [bool]$Leased, [string]$Retired) {
            $p = @($script:QJ.published)
            Assert-True ($p.Count -eq 1 -and $p[0].jobId -ceq $JobId -and $p[0].exitCode -eq 125 -and $p[0].notAfterExpired -eq $true -and $p[0].queueExpired -eq $true -and $null -eq $p[0].startedUtc) "${Case}: one expired result, never started: $($p | ConvertTo-Json -Compress -Depth 5)"
            $gotPhase = if ($null -ne $p[0].admission) { [string]$p[0].admission.phase } else { 'pre-gate' }
            Assert-True ($gotPhase -ceq $Phase -and $p[0].stderr -like "*before launch ($Phase); job was not executed") "${Case}: refused in phase ${Phase}: got $gotPhase / $($p[0].stderr)"
            Assert-True ($script:QJ.launches.Count -eq 0) "${Case}: NO child launch: $($script:QJ.launches -join ' | ')"
            Assert-True ($script:QJ.moved.Count -eq 1 -and $script:QJ.moved[0] -ceq "$JobId|$processed") "${Case}: the job's artifacts moved to processed: $($script:QJ.moved -join ' | ')"
            Assert-True (($script:QJ.retired -join ';') -ceq $Retired -and -not (Test-Path -LiteralPath (Join-Path $running "$JobId.started.json"))) "${Case}: the claim retired as [$Retired]: [$($script:QJ.retired -join ';')]"
            Assert-True ($script:QJ.queueExpired -eq 0) "${Case}: typed as not-after, not as a queue expiry"
            if ($Leased) {
                $a = $p[0].admission
                $lr = $a.leaseRelease
                Assert-True ($a.label -ceq 'expired-not-after' -and $a.state -ceq 'expired' -and $null -eq $a.slot) "${Case}: the admission reads expired-not-after: $($a | ConvertTo-Json -Compress -Depth 4)"
                Assert-True ($null -ne $lr -and $lr.phase -ceq $Phase -and [string]$lr.releaseRc -ceq '0' -and (Get-Released).Count -eq 1) "${Case}: the lease released once, the release recorded in phase ${Phase}: $($lr | ConvertTo-Json -Compress) releases=$((Get-Released).Count)"
                $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
                Assert-True ($reqs.Count -eq 1 -and (Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0) "${Case}: the wrapper is gone, its record removed, no lease kept"
            }
            else {
                Assert-True ($null -eq $p[0].admission -and @(Get-Calls | Where-Object { $_.cmd -eq 'request' }).Count -eq 0 -and $script:QJ.claims.Count -eq 0) "${Case}: refused before the gate: never claimed, no lease request"
            }
        }
    }

    # ------------------------------------------------------------ notafter-queuedjob-pregate: a passed deadline is refused before the gate
    if (Test-Case 'notafter-queuedjob-pregate') {
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jQp' (New-NotAfterMetaJson -60)
        [IO.File]::WriteAllText((Join-Path $running 'jQp.started.json'), '{"phase":"admission-wait"}')   # a claim a restart re-queued
        $null = Invoke-QueuedJob -Job $job
        Assert-NotAfterExit 'notafter-queuedjob-pregate' 'jQp' 'pre-gate' $false 'jQp.started.json|admission-wait|expired-not-after'
        Assert-True ($script:QJ.published[0].notAfterState -ceq 'passed' -and $script:QJ.launchingMarkers.Count -eq 0) "notafter-queuedjob-pregate: state passed, no launching marker"
        $results.notAfterQueuedJobPregate = [ordered]@{ retired = @($script:QJ.retired) }
    }

    # ------------------------------------------------------------ notafter-queuedjob-gate: the gate's expired-not-after label is published
    if (Test-Case 'notafter-queuedjob-gate') {
        $job = Reset-QueuedJobCase @{ request = 'hang' } 'jQg' (New-NotAfterMetaJson 600)
        $script:LogHook = { param($Message) if ($script:HookFired -or $Message -notlike 'quiet-lease: request started job=jQg *') { return }; $script:HookFired = $true; Set-DeadlinePassed }
        $null = Invoke-QueuedJob -Job $job
        Assert-True ($script:HookFired) "notafter-queuedjob-gate: the deadline passed while the request was pending"
        Assert-NotAfterExit 'notafter-queuedjob-gate' 'jQg' 'pending' $true 'jQg.started.json|admission-wait|expired-not-after'
        Assert-True ($script:QJ.claims.Count -eq 1 -and $script:QJ.launchingMarkers.Count -eq 0) "notafter-queuedjob-gate: claimed once, no launching marker"
        $results.notAfterQueuedJobGate = [ordered]@{ phase = $script:QJ.published[0].admission.phase; releaseRc = $script:QJ.published[0].admission.leaseRelease.releaseRc }
    }

    # ------------------------------------------------------------ notafter-queuedjob-post-admission: admitted, then the deadline passes
    # key3's repro (Stop-QuietLease moved after Exit-HeavySlot) leaves admission.leaseRelease null: this case fails on it.
    if (Test-Case 'notafter-queuedjob-post-admission') {
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jQa' (New-NotAfterMetaJson 600)
        $script:QJ.admittedHook = { Set-DeadlinePassed }
        $null = Invoke-QueuedJob -Job $job
        Assert-NotAfterExit 'notafter-queuedjob-post-admission' 'jQa' 'post-admission' $true 'jQa.started.json|admission-wait|expired-not-after'
        Assert-True ($script:QJ.launchingMarkers.Count -eq 0 -and $script:QJ.published[0].admission.why -like '*after admission, before launch') "notafter-queuedjob-post-admission: refused before the launching marker: $($script:QJ.published[0].admission.why)"
        $results.notAfterQueuedJobPostAdmission = [ordered]@{ releaseRc = $script:QJ.published[0].admission.leaseRelease.releaseRc }
    }

    # ------------------------------------------------------------ queuedjob-cancel-post-admission: a cancel file lands once the job is admitted
    # LEASE-NOT-AFTER-HARDENING-2 H1 (key3 on #648): the cancel exit after admission releases the lease, and records that release,
    # before Exit-HeavySlot. Stop-QuietLease moved after Exit-HeavySlot (which then ends the lease as "job done"), or the
    # leaseRelease record dropped, leaves admission.leaseRelease null: this case fails on either.
    if (Test-Case 'queuedjob-cancel-post-admission') {
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jQx' (New-NotAfterMetaJson 600)
        $script:CancelFile = Join-Path $cancel 'jQx.cancel'
        $script:QJ.admittedHook = { [IO.File]::WriteAllText($script:CancelFile, 'x') }
        try { $null = Invoke-QueuedJob -Job $job }
        finally { Remove-Item -LiteralPath $script:CancelFile -Force -ErrorAction SilentlyContinue }
        $p = @($script:QJ.published)
        Assert-True ($p.Count -eq 1 -and $p[0].jobId -ceq 'jQx' -and $p[0].exitCode -eq 130 -and $p[0].cancelled -eq $true -and $null -eq $p[0].startedUtc -and $p[0].notAfterExpired -ne $true) "queuedjob-cancel-post-admission: one cancelled result, never started: $($p | ConvertTo-Json -Compress -Depth 5)"
        $a = $p[0].admission
        Assert-True ($a.label -ceq 'cancelled-in-gate' -and $a.why -ceq 'cancel file present after admission, before launch' -and $null -eq $a.slot) "queuedjob-cancel-post-admission: the admission reads cancelled-in-gate after admission: $($a | ConvertTo-Json -Compress -Depth 4)"
        $lr = $a.leaseRelease
        Assert-True ($null -ne $lr -and $lr.phase -ceq 'post-admission' -and [string]$lr.releaseRc -ceq '0' -and (Get-Released).Count -eq 1) "queuedjob-cancel-post-admission: the lease released once, the release recorded in phase post-admission: $($lr | ConvertTo-Json -Compress) releases=$((Get-Released).Count)"
        $ended = @($script:Log | Where-Object { $_.StartsWith('quiet-lease: ended job=jQx ') })
        Assert-True ($ended.Count -eq 1 -and $ended[0].EndsWith('why=[cancel file present after admission, before launch]')) "queuedjob-cancel-post-admission: the lease is ended by the cancel exit, not by Exit-HeavySlot: $($ended -join ' | ')"
        Assert-True ($script:QJ.launches.Count -eq 0 -and $script:QJ.launchingMarkers.Count -eq 0 -and $script:QJ.claims.Count -eq 1) "queuedjob-cancel-post-admission: claimed once, NO launching marker, NO child launch: $($script:QJ.launches -join ' | ')"
        Assert-True ($script:QJ.moved.Count -eq 1 -and $script:QJ.moved[0] -ceq "jQx|$processed" -and ($script:QJ.retired -join ';') -ceq 'jQx.started.json|admission-wait|cancelled-in-gate' -and -not (Test-Path -LiteralPath (Join-Path $running 'jQx.started.json'))) "queuedjob-cancel-post-admission: artifacts moved, the claim retired as cancelled-in-gate: [$($script:QJ.retired -join ';')]"
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($reqs.Count -eq 1 -and (Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0) "queuedjob-cancel-post-admission: the wrapper is gone, its record removed, no lease kept"
        $results.queuedJobCancelPostAdmission = [ordered]@{ label = $a.label; releasePhase = $lr.phase; releaseRc = $lr.releaseRc }
    }

    # ------------------------------------------------------------ queuedjob-queue-expired-post-admission: the queue ceiling passes once admitted
    # QUIET-LEASE-EXIT-RELEASE-EVIDENCE-1 (Dot 456ccdf9 on #652, item a): the queue-expired exit after admission releases the lease,
    # and records that release, before Exit-HeavySlot, as the cancel and not-after exits do. A bare Exit-HeavySlot (which ends the
    # lease as "job done" and records nothing) leaves the published result without admission.leaseRelease: this case fails on it.
    if (Test-Case 'queuedjob-queue-expired-post-admission') {
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jQe' (New-NotAfterMetaJson 600)
        $script:QJ.admittedHook = { $script:AgeAfter = (Get-Date).AddSeconds(-1) }
        $null = Invoke-QueuedJob -Job $job
        $p = @($script:QJ.published)
        Assert-True ($p.Count -eq 1 -and $p[0].jobId -ceq 'jQe' -and $p[0].exitCode -eq 125 -and $p[0].queueExpired -eq $true -and $p[0].notAfterExpired -ne $true -and $null -eq $p[0].startedUtc -and $p[0].queueAgeSec -eq 9999) "queuedjob-queue-expired-post-admission: one queue-expired result, never started: $($p | ConvertTo-Json -Compress -Depth 5)"
        $a = $p[0].admission
        Assert-True ($null -ne $a -and $a.label -ceq 'queue-expired' -and $a.state -ceq 'expired' -and $null -eq $a.slot -and $a.why -ceq 'queue age 9999s > 600s after admission, before launch') "queuedjob-queue-expired-post-admission: the result carries the admission, read queue-expired after admission: $($a | ConvertTo-Json -Compress -Depth 4)"
        $lr = $a.leaseRelease
        Assert-True ($null -ne $lr -and $lr.phase -ceq 'post-admission' -and [string]$lr.releaseRc -ceq '0' -and $null -ne $lr.rc -and $null -ne $lr.killed -and (Get-Released).Count -eq 1) "queuedjob-queue-expired-post-admission: the lease released once, the release recorded in phase post-admission: $($lr | ConvertTo-Json -Compress) releases=$((Get-Released).Count)"
        $ended = @($script:Log | Where-Object { $_.StartsWith('quiet-lease: ended job=jQe ') })
        Assert-True ($ended.Count -eq 1 -and $ended[0].EndsWith('why=[queue expired after admission, before launch]')) "queuedjob-queue-expired-post-admission: the lease is ended by the queue-expired exit, not by Exit-HeavySlot: $($ended -join ' | ')"
        Assert-True ($script:QJ.queueExpired -eq 1 -and $script:QJ.launches.Count -eq 0 -and $script:QJ.launchingMarkers.Count -eq 0 -and $script:QJ.claims.Count -eq 1) "queuedjob-queue-expired-post-admission: typed queue-expired once, claimed once, NO launching marker, NO child launch"
        Assert-True ($script:QJ.moved.Count -eq 1 -and $script:QJ.moved[0] -ceq "jQe|$processed" -and ($script:QJ.retired -join ';') -ceq 'jQe.started.json|admission-wait|queue-expired') "queuedjob-queue-expired-post-admission: artifacts moved, the claim retired as queue-expired: [$($script:QJ.retired -join ';')]"
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($reqs.Count -eq 1 -and (Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0) "queuedjob-queue-expired-post-admission: the wrapper is gone, its record removed, no lease kept"
        $results.queuedJobQueueExpiredPostAdmission = [ordered]@{ label = $a.label; releasePhase = $lr.phase; releaseRc = $lr.releaseRc }
    }

    # ------------------------------------------------------------ queuedjob-holder-lost-prelaunch: the slot re-confirm fails before launch
    # QUIET-LEASE-EXIT-RELEASE-EVIDENCE-1 (Dot 456ccdf9 on #652, item a): the launching marker is written, then the re-confirm's renew
    # hangs past LeaseRenewTimeoutSec, so the lease is not proven: holder-lost, NOT launched, left queued. The lease is released and
    # that release recorded (phase pre-launch) before Exit-HeavySlot, on the admission and on the kept holder-lost event, which is
    # what the job's next admission (and so its result) carries. A bare Exit-HeavySlot records nothing: this case fails on it.
    if (Test-Case 'queuedjob-holder-lost-prelaunch') {
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jQh' (New-NotAfterMetaJson 600)
        $script:QJ.markerHook = { Set-Fake @{ request = 'active'; renew_hang = $true } }
        $null = Invoke-QueuedJob -Job $job
        Assert-True ($script:QJ.launchingMarkers.Count -eq 1 -and $script:QJ.launches.Count -eq 0 -and $script:QJ.published.Count -eq 0 -and $script:QJ.retired.Count -eq 0 -and $script:QJ.moved.Count -eq 0) "queuedjob-holder-lost-prelaunch: the launching marker written, NO child launch, nothing published, the job left queued"
        Assert-True ($script:QJ.holderLoss.Count -eq 1 -and $script:QJ.holderLoss[0].StartsWith('pre-launch|holder-lost|')) "queuedjob-holder-lost-prelaunch: one typed pre-launch holder-lost: $($script:QJ.holderLoss -join ' | ')"
        $ev = @($script:HolderLossByJob['jQh'])
        $evLr = if ($ev.Count -eq 1) { $ev[0].leaseRelease } else { $null }
        Assert-True ($null -ne $evLr -and $evLr.phase -ceq 'pre-launch' -and [string]$evLr.releaseRc -ceq '0' -and $null -ne $evLr.rc -and $null -ne $evLr.killed -and (Get-Released).Count -eq 1) "queuedjob-holder-lost-prelaunch: the lease released once, the release recorded on the holder-lost event in phase pre-launch: $($ev | ConvertTo-Json -Compress -Depth 4) releases=$((Get-Released).Count)"
        $admLr = $script:Admission.leaseRelease
        Assert-True ($null -ne $admLr -and $admLr.phase -ceq 'pre-launch' -and [string]$admLr.releaseRc -ceq '0') "queuedjob-holder-lost-prelaunch: the admission records the same release: $($admLr | ConvertTo-Json -Compress)"
        $next = [pscustomobject]@{ label = 'clear' }
        Add-HeavySlotHolderLossToAdmissionReal -JobId 'jQh' -Admission $next
        Assert-True (@($next.holderLost).Count -eq 1 -and $next.holderLost[0].leaseRelease.phase -ceq 'pre-launch') "queuedjob-holder-lost-prelaunch: the job's next admission (and so its result) carries the recorded release: $($next | ConvertTo-Json -Compress -Depth 4)"
        $ended = @($script:Log | Where-Object { $_.StartsWith('quiet-lease: ended job=jQh ') })
        Assert-True ($ended.Count -eq 1 -and $ended[0] -like '*why=`[pre-launch holder-lost: *') "queuedjob-holder-lost-prelaunch: the lease is ended by the refusal, not by Exit-HeavySlot: $($ended -join ' | ')"
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($reqs.Count -eq 1 -and (Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0) "queuedjob-holder-lost-prelaunch: the wrapper is gone, its record removed, no lease kept"
        $results.queuedJobHolderLostPrelaunch = [ordered]@{ releasePhase = $evLr.phase; releaseRc = $evLr.releaseRc; holderLoss = $script:QJ.holderLoss[0] }
    }

    # ------------------------------------------------------------ queuedjob-lease-coverage-at-launch (AGENT-LEASE-PENDING-HEAT-TRACE-1 r3, Dot B2 / adversary r4 A2)
    # admission.leaseCoversTimeout must be true AT LAUNCH. Time spent between the grant and the child's creation (here the launching marker's
    # write, modelled by moving until= to 1790 s from now, for an 1800 s timeout) is counted: the admission reads not covered, with the typed
    # line and the seconds left. The control run (nothing spent) keeps it covered and records what was left.
    if (Test-Case 'queuedjob-lease-coverage-at-launch') {
        $cov = [ordered]@{}
        # r5 (adversary r5b-cb19d346 A3): 'slow' leaves 1803 s at the marker and the launch call itself takes 5 s (its out/err file opens on a share
        # root): the child is created with ~1798 s left, so it is NOT covered; a reading taken before the launch call says covered.
        foreach ($k in @('control', 'short', 'slow')) {
            $job = Reset-QueuedJobCase @{ request = 'active' } "jQc$k" (New-NotAfterMetaJson 600)
            $script:QJLaunchDelaySec = 0
            if ($k -ceq 'short') { $script:QJ.markerHook = { $script:QuietLease.active.until = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 1790 } }
            if ($k -ceq 'slow') { $script:QJ.markerHook = { $script:QuietLease.active.until = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 1803 }; $script:QJLaunchDelaySec = 5 }
            try { $null = Invoke-QueuedJob -Job $job }
            finally {
                $a = $script:Admission
                $cov[$k] = [pscustomobject]@{ label = $a.label; covers = $a.leaseCoversTimeout; remaining = $a.leaseRemainingAtLaunchSec; launches = $script:QJ.launches.Count
                    lines = @($script:Log | Where-Object { $_ -like "quiet-lease: lease-shorter-than-timeout job=jQc$k *" }) }
                Exit-HeavySlot
            }
        }
        Assert-True ($cov.control.launches -eq 1 -and $cov.short.launches -eq 1 -and $cov.control.label -ceq 'clear' -and $cov.short.label -ceq 'clear') "queuedjob-lease-coverage-at-launch: both runs are admitted clear and reach the child launch: $($cov | ConvertTo-Json -Compress -Depth 4)"
        Assert-True ($cov.short.covers -eq $false -and $null -ne $cov.short.remaining -and $cov.short.remaining -ge 1780 -and $cov.short.remaining -le 1790) "queuedjob-lease-coverage-at-launch: 1790 s left at launch for an 1800 s timeout is NOT reported covered: $($cov.short | ConvertTo-Json -Compress)"
        Assert-True ($cov.control.covers -eq $true -and $null -ne $cov.control.remaining -and $cov.control.remaining -ge 1800 -and $cov.control.lines.Count -eq 0) "queuedjob-lease-coverage-at-launch: control: covered at launch, the seconds left recorded: $($cov.control | ConvertTo-Json -Compress)"
        Assert-True ($cov.short.lines.Count -eq 1 -and $cov.short.lines[0] -match ' phase=launch remaining=17[89]\ds timeoutSec=1800;') "queuedjob-lease-coverage-at-launch: one typed lease-shorter-than-timeout line at launch: $($cov.short.lines -join ' | ')"
        $script:QJLaunchDelaySec = 0
        Assert-True ($cov.slow.launches -eq 1 -and $cov.slow.covers -eq $false -and $null -ne $cov.slow.remaining -and $cov.slow.remaining -lt 1800 -and $cov.slow.lines.Count -eq 1) "queuedjob-lease-coverage-at-launch A3(r5): a 5 s launch call with 1803 s left for an 1800 s timeout is NOT reported covered (read as the call returns): $($cov.slow | ConvertTo-Json -Compress)"
        $results.queuedJobLeaseCoverageAtLaunch = [ordered]@{ control = $cov.control.remaining; short = $cov.short.remaining; shortCovers = $cov.short.covers; slow = $cov.slow.remaining; slowCovers = $cov.slow.covers }
    }

    # ------------------------------------------------------------ notafter-prelaunch-marker: the deadline passes while the launching marker is written
    if (Test-Case 'notafter-prelaunch-marker') {
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jQm' (New-NotAfterMetaJson 600)
        $script:QJ.markerHook = { Set-DeadlinePassed }
        $null = Invoke-QueuedJob -Job $job
        Assert-True ($script:QJ.launchingMarkers.Count -eq 1 -and $script:QJ.holderLoss.Count -eq 0) "notafter-prelaunch-marker: the launching marker was written and the slot re-confirm passed (holderLoss=$($script:QJ.holderLoss -join ' | '))"
        Assert-NotAfterExit 'notafter-prelaunch-marker' 'jQm' 'pre-launch' $true 'jQm.started.json|launching|expired-not-after'
        Assert-True ($script:QJ.published[0].admission.why -like '*at the last check before launch') "notafter-prelaunch-marker: why: $($script:QJ.published[0].admission.why)"
        $results.notAfterPrelaunchMarker = [ordered]@{ retired = @($script:QJ.retired); releaseRc = $script:QJ.published[0].admission.leaseRelease.releaseRc }
    }

    # ------------------------------------------------------------ notafter-prelaunch-confirm: the deadline passes during the slot re-confirm's renew
    if (Test-Case 'notafter-prelaunch-confirm') {
        $job = Reset-QueuedJobCase @{ request = 'active'; renew_sleep = 1.6 } 'jQc' (New-NotAfterMetaJson 600)
        # The re-confirm renews to completion and heartbeats each second it waits; the deadline passes at that heartbeat.
        $script:BeatHook = { param($Activity) if (-not $script:HookFired -and $Activity -ceq 'pre-launch job=jQc') { $script:HookFired = $true; Set-DeadlinePassed } }
        $savedRenewTimeout = $LeaseRenewTimeoutSec
        $LeaseRenewTimeoutSec = 15
        try { $null = Invoke-QueuedJob -Job $job }
        finally { $LeaseRenewTimeoutSec = $savedRenewTimeout }
        Assert-True ($script:HookFired -and $script:QJ.holderLoss.Count -eq 0 -and @(Get-Calls | Where-Object { $_.cmd -eq 'renew' }).Count -eq 1) "notafter-prelaunch-confirm: the deadline passed inside the re-confirm, which itself passed (hook=$($script:HookFired) holderLoss=$($script:QJ.holderLoss -join ' | '))"
        Assert-NotAfterExit 'notafter-prelaunch-confirm' 'jQc' 'pre-launch' $true 'jQc.started.json|launching|expired-not-after'
        $results.notAfterPrelaunchConfirm = [ordered]@{ retired = @($script:QJ.retired); releaseRc = $script:QJ.published[0].admission.leaseRelease.releaseRc }
    }

    # ------------------------------------------------------------ notafter-renew-in-light-job: no renew past the deadline from a light job
    # key3 hardening 2: a light job run from inside the wait ticks the renew; the waiting lease turns active there and, past
    # notAfterUtc, must not be renewed again. The wait then releases it typed with its true phase (post-active).
    if (Test-Case 'notafter-renew-in-light-job') {
        # Still pending at the wait's first exempt scan (the request's own start-up outlasts a short active_after).
        Reset-Case @{ request = 'active'; active_after = 2 }
        $script:LeaseNotAfterMinAdmitSec = 1
        $script:AgeAfter = (Get-Date).AddSeconds(40)
        $mR = New-NotAfterMeta 7
        $script:NotAfterAt = [DateTimeOffset]::Parse($mR.notAfterUtc).UtcDateTime
        $script:Light = @{ ran = 0; activeBefore = $false; renewsBefore = 0; renewsAfter = 0; ticksAfter = 0 }
        function Invoke-ExemptJobsDuringWait {
            param([string]$WaitingJobId, [double]$WaitedSec)
            $script:Light.ran++
            $w = $script:QuietLeaseWaiting
            if ($null -eq $w -or $script:Light.ran -gt 1) { return 0 }
            # The light job's monitor loop, as Invoke-QueuedJob runs it: one renew tick per slice, until 4 s past the deadline.
            while ([DateTime]::UtcNow -lt $script:NotAfterAt.AddSeconds(4)) {
                $pre = [DateTime]::UtcNow
                $before = $w.renewStarts
                Invoke-QuietLeaseRenewTick
                if ($pre -gt $script:NotAfterAt) { $script:Light.renewsAfter += $w.renewStarts - $before; $script:Light.ticksAfter++ }
                else { $script:Light.renewsBefore += $w.renewStarts - $before; if ($null -ne $w.active) { $script:Light.activeBefore = $true } }
                Start-Sleep -Milliseconds 100
            }
            return 1
        }
        try {
            $adm = Wait-QuietLeaseAdmission -JobId 'jNr' -Job ([pscustomobject]@{}) -Metadata $mR
            $lightA = $script:Light
            $logA = @($script:Log)
            $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
            $releasedA = (Get-Released).Count
            $goneA = Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5
            # B: the request turns active only AFTER the deadline, while the light job still runs (so the tick no longer reads
            # it). The wait's last read before the release still finds it active: phase post-active, not pending.
            Reset-Case @{ request = 'active'; active_after = 5 }
            $script:LeaseNotAfterMinAdmitSec = 1
            $script:AgeAfter = (Get-Date).AddSeconds(40)
            $mB = New-NotAfterMeta 3
            $script:NotAfterAt = [DateTimeOffset]::Parse($mB.notAfterUtc).UtcDateTime
            $script:Light = @{ ran = 0; activeBefore = $false; renewsBefore = 0; renewsAfter = 0; ticksAfter = 0 }
            $admB = Wait-QuietLeaseAdmission -JobId 'jNs' -Job ([pscustomobject]@{}) -Metadata $mB
            $lightB = $script:Light
            $flagB = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        }
        finally { function Invoke-ExemptJobsDuringWait { param([string]$WaitingJobId, [double]$WaitedSec) $script:Exempt++; return 0 } }
        $script:Light = $lightA
        Assert-True ($lightB.ran -ge 1 -and -not $lightB.activeBefore -and $lightB.ticksAfter -ge 5 -and $lightB.renewsAfter -eq 0) "notafter-renew-in-light-job (B): pending at the deadline, never renewed after it: $($lightB | ConvertTo-Json -Compress)"
        Assert-True ($admB.label -eq 'expired-not-after' -and $admB.phase -ceq 'post-active' -and $admB.leaseRelease.phase -ceq 'post-active') "notafter-renew-in-light-job (B): a request that turned active after the deadline is released as post-active: phase=$($admB.phase) release=$($admB.leaseRelease | ConvertTo-Json -Compress)"
        Assert-True ($flagB.Count -eq 1 -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "notafter-renew-in-light-job (B): one request, no lease kept"
        Assert-True ($script:Light.ran -ge 1 -and $script:Light.activeBefore -and $script:Light.renewsBefore -ge 1) "notafter-renew-in-light-job: the race landed: the waiting lease turned active inside the light job and was renewed before notAfterUtc: $($script:Light | ConvertTo-Json -Compress)"
        Assert-True ($script:Light.ticksAfter -ge 5 -and $script:Light.renewsAfter -eq 0) "notafter-renew-in-light-job: no renew started past notAfterUtc: $($script:Light | ConvertTo-Json -Compress)"
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'post-active' -and $null -ne $adm.leaseRelease -and $adm.leaseRelease.phase -ceq 'post-active' -and [string]$adm.leaseRelease.releaseRc -ceq '0') "notafter-renew-in-light-job: released with its true phase post-active: label=$($adm.label) phase=$($adm.phase) release=$($adm.leaseRelease | ConvertTo-Json -Compress)"
        Assert-True (@($logA | Where-Object { $_ -like "quiet-lease: not-after passed job=jNr notAfterUtc=$($mR.notAfterUtc) active=True while waiting; no more renews*" }).Count -eq 1) "notafter-renew-in-light-job: the stop is typed once"
        Assert-True ($reqs.Count -eq 1 -and $releasedA -eq 1 -and $goneA) "notafter-renew-in-light-job: released once, wrapper gone"
        $results.notAfterRenewInLightJob = [ordered]@{ renewsBefore = $lightA.renewsBefore; renewsAfter = $lightA.renewsAfter; ticksAfter = $lightA.ticksAfter; phase = $adm.phase; phaseB = $admB.phase }
    }

    # ------------------------------------------------------------ notafter-renew-crosses-deadline: a renew in flight as the deadline passes
    # LEASE-NOT-AFTER-HARDENING-2 H3 (Dot R18 on #648): past notAfterUtc the tick starts no renew for the waiting lease, but the one
    # already in flight is still collected (it returned) or killed at LeaseRenewTimeoutSec (it hangs) by the tick, inside the light
    # job, not left to the wait's release. hang: the renew never returns -> killed, renewSlow. slow: it returns late -> collected.
    if (Test-Case 'notafter-renew-crosses-deadline') {
        $savedRenewTimeout = $LeaseRenewTimeoutSec
        $crossRun = [ordered]@{}
        $crossAdm = [ordered]@{}
        $crossLog = [ordered]@{}
        $crossReleased = [ordered]@{}
        try {
            foreach ($v in @(
                    @{ n = 'hang'; fake = @{ request = 'active'; active_after = 2; renew_hang = $true }; timeout = 3 },
                    @{ n = 'slow'; fake = @{ request = 'active'; active_after = 2; renew_sleep = 1.5 }; timeout = 15 })) {
                Reset-Case $v.fake
                $LeaseRenewTimeoutSec = $v.timeout
                $script:LeaseNotAfterMinAdmitSec = 1
                $script:AgeAfter = (Get-Date).AddSeconds(60)
                $script:CrossMeta = New-NotAfterMeta 600
                # QUIET-LEASE-EXIT-RELEASE-EVIDENCE-AND-EVENT-OBSERVE-1 (Dot 456ccdf9 on #652, item b): the ticks run until the event the
                # case asserts (the renew in flight is collected or killed: renewCall cleared), then a dwell of 3 renew intervals in which
                # a renew started past the deadline would be seen. The ceiling is the case's own timeout plus a margin that covers the
                # dwell, never a fixed window shorter than that timeout (a cold host's slow renew outlasted the old 6 s one).
                $script:CrossDwellSec = 3 * $LeaseRenewSec
                $script:CrossCeilingSec = $v.timeout + $script:CrossDwellSec + 5
                $script:Cross = @{ ran = 0; inFlight = $false; renewPid = 0; startsAtDeadline = -1; ticksAfter = 0; renewCallLeft = $null; renewAlive = $null; startsAfter = $null; renewsOk = $null; renewSlow = $null; renewCalls = $null; eventSec = $null; ceilingSec = $script:CrossCeilingSec }
                function Invoke-ExemptJobsDuringWait {
                    param([string]$WaitingJobId, [double]$WaitedSec)
                    $c = $script:Cross
                    $c.ran++
                    $w = $script:QuietLeaseWaiting
                    if ($null -eq $w -or $c.ran -gt 1) { return 0 }
                    # The light job's monitor loop ticks the renew: the waiting lease turns active and its first renew starts.
                    $dl = (Get-Date).AddSeconds(20)
                    while ($null -eq $w.renewCall -and (Get-Date) -lt $dl) { Invoke-QuietLeaseRenewTick; Start-Sleep -Milliseconds 100 }
                    if ($null -eq $w.renewCall) { return 1 }
                    $c.inFlight = $true
                    $c.renewPid = $w.renewCall.proc.Id
                    $c.startsAtDeadline = $w.renewStarts
                    $renewStart = $w.renewCall.started
                    # The deadline passes with that renew in flight (the tick reads the lease's copy, the wait the metadata's).
                    $past = [DateTime]::UtcNow.AddSeconds(-1).ToString('o')
                    $w | Add-Member -NotePropertyName notAfterUtc -NotePropertyValue $past -Force
                    $script:CrossMeta | Add-Member -NotePropertyName notAfterUtc -NotePropertyValue $past -Force
                    $ceiling = (Get-Date).AddSeconds($script:CrossCeilingSec)
                    $eventAt = $null
                    while ((Get-Date) -lt $ceiling) {
                        Invoke-QuietLeaseRenewTick; $c.ticksAfter++
                        if ($null -eq $eventAt -and $null -eq $w.renewCall) { $eventAt = Get-Date; $c.eventSec = [Math]::Round(($eventAt - $renewStart).TotalSeconds, 2) }
                        if ($null -ne $eventAt -and ((Get-Date) - $eventAt).TotalSeconds -ge $script:CrossDwellSec) { break }
                        Start-Sleep -Milliseconds 100
                    }
                    # Read before the light job returns: the wait's release would end the call anyway.
                    $c.renewCallLeft = $null -ne $w.renewCall
                    $c.renewAlive = Test-PidAlive $c.renewPid
                    $c.startsAfter = $w.renewStarts - $c.startsAtDeadline
                    $c.renewsOk = $w.renewsOk
                    $c.renewSlow = $w.renewSlow
                    $c.renewCalls = @(Get-Calls | Where-Object { $_.cmd -eq 'renew' }).Count
                    return 1
                }
                $crossAdm[$v.n] = Wait-QuietLeaseAdmission -JobId "jX$($v.n)" -Job ([pscustomobject]@{}) -Metadata $script:CrossMeta
                $crossRun[$v.n] = $script:Cross
                $crossLog[$v.n] = @($script:Log)
                $crossReleased[$v.n] = (Get-Released).Count
                $reqsX = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
                $null = Wait-Until { -not (Test-PidAlive ([int]$reqsX[0].pid)) } 5
            }
        }
        finally {
            $LeaseRenewTimeoutSec = $savedRenewTimeout
            function Invoke-ExemptJobsDuringWait { param([string]$WaitingJobId, [double]$WaitedSec) $script:Exempt++; return 0 }
        }
        foreach ($n in @('hang', 'slow')) {
            $c = $crossRun[$n]; $a = $crossAdm[$n]
            Assert-True ($c.ran -ge 1 -and $c.inFlight -and $c.ticksAfter -ge 5) "notafter-renew-crosses-deadline (${n}): the race landed: a renew was in flight inside the light job as the deadline passed: $($c | ConvertTo-Json -Compress)"
            Assert-True ($c.startsAfter -eq 0 -and $c.renewCalls -eq 1) "notafter-renew-crosses-deadline (${n}): no renew started past the deadline: $($c | ConvertTo-Json -Compress)"
            Assert-True ($a.label -eq 'expired-not-after' -and $a.phase -ceq 'post-active' -and $null -ne $a.leaseRelease -and $a.leaseRelease.phase -ceq 'post-active' -and [string]$a.leaseRelease.releaseRc -ceq '0' -and $crossReleased[$n] -eq 1) "notafter-renew-crosses-deadline (${n}): the wait then releases the lease once, post-active: label=$($a.label) phase=$($a.phase) release=$($a.leaseRelease | ConvertTo-Json -Compress)"
        }
        $h = $crossRun['hang']; $s = $crossRun['slow']
        $crossBad = [System.Collections.Generic.List[string]]::new()
        if (-not (-not $h.renewCallLeft -and -not $h.renewAlive -and $h.renewSlow -eq 1)) { $crossBad.Add("hang: the hung renew is not killed at LeaseRenewTimeoutSec by the tick: $($h | ConvertTo-Json -Compress)") }
        if (-not (-not $s.renewCallLeft -and $s.renewsOk -eq 1 -and $s.renewSlow -eq 0)) { $crossBad.Add("slow: the late renew is not collected by the tick: $($s | ConvertTo-Json -Compress)") }
        Assert-True ($crossBad.Count -eq 0) "notafter-renew-crosses-deadline: the renew in flight is cleaned up past the deadline, not left to the release: $($crossBad -join ' ; ')"
        # The bounds, from each case's own timeout: hang ends AT its bound (killed once LeaseRenewTimeoutSec passed, within the
        # ceiling), slow ends BEFORE its bound (collected, never killed).
        Assert-True ($null -ne $h.eventSec -and $h.eventSec -ge 3 -and $h.eventSec -le $h.ceilingSec + 1) "notafter-renew-crosses-deadline (hang): the hung renew ends at its 3 s bound, within the ceiling $($h.ceilingSec) s: eventSec=$($h.eventSec)"
        Assert-True ($null -ne $s.eventSec -and $s.eventSec -lt 15) "notafter-renew-crosses-deadline (slow): the late renew is collected before its 15 s bound: eventSec=$($s.eventSec)"
        Assert-True (@($crossLog['hang'] | Where-Object { $_ -like 'quiet-lease: renew-slow job=jXhang killed after 3s*' }).Count -eq 1) "notafter-renew-crosses-deadline (hang): typed renew-slow once"
        Assert-True (@($crossLog['slow'] | Where-Object { $_ -like 'quiet-lease: not-after passed job=jXslow * active=True while waiting; no more renews*' }).Count -eq 1) "notafter-renew-crosses-deadline (slow): the stop is typed once"
        $results.notAfterRenewCrossesDeadline = [ordered]@{ hang = [ordered]@{ renewSlow = $h.renewSlow; renewCallLeft = $h.renewCallLeft; renewAlive = $h.renewAlive; eventSec = $h.eventSec; ceilingSec = $h.ceilingSec }; slow = [ordered]@{ renewsOk = $s.renewsOk; renewCallLeft = $s.renewCallLeft; eventSec = $s.eventSec; ceilingSec = $s.ceilingSec } }
    }

    # ------------------------------------------------------------ notafter-meta-unreadable: a deadline the agent cannot read fails closed
    # key3 hardening 3. The rule (Get-JobMetadata): no file = defaults, no deadline; bytes that read but do not parse = defaults,
    # unless they name notAfterUtc; bytes that cannot be read (3 tries) = cannot tell. The last two fail closed (refused).
    if (Test-Case 'notafter-meta-unreadable') {
        $null = Reset-QueuedJobCase @{ request = 'active' } 'jMx' $null
        $mAbs = Get-JobMetadata -JobId 'jMabsent'
        $vAbs = Get-JobNotAfterVerdict -Metadata $mAbs
        Assert-True ($mAbs.missing -eq $true -and $mAbs.metaUnreadable -ne $true -and $mAbs.timeoutSec -eq 1800 -and $vAbs.state -ceq 'none') "notafter-meta-unreadable: absent: the defaults, no deadline (unchanged): $($vAbs | ConvertTo-Json -Compress)"
        [IO.File]::WriteAllText((Join-Path $inbox 'jMtorn.meta.json'), '{not valid json')
        $mTorn = Get-JobMetadata -JobId 'jMtorn'
        $vTorn = Get-JobNotAfterVerdict -Metadata $mTorn
        Assert-True ($mTorn.missing -eq $true -and $mTorn.metaUnreadable -ne $true -and $mTorn.timeoutSec -eq 1800 -and $vTorn.state -ceq 'none') "notafter-meta-unreadable: torn with no notAfterUtc: the defaults, unchanged (the sibling UM agent's rule, test_ultra_magnus_agent_metadata.py):$($vTorn | ConvertTo-Json -Compress)"
        [IO.File]::WriteAllText((Join-Path $inbox 'jMcut.meta.json'), '{"timeoutSec":60,"notAfterUtc":"2026-10-24T0')
        $mCut = Get-JobMetadata -JobId 'jMcut'
        $vCut = Get-JobNotAfterVerdict -Metadata $mCut
        Assert-True ($mCut.metaUnreadable -eq $true -and $vCut.state -ceq 'unparseable' -and $vCut.why -like 'meta.json does not parse:*fail closed*') "notafter-meta-unreadable: torn bytes naming notAfterUtc fail closed: $($vCut | ConvertTo-Json -Compress)"
        $lockPath = Join-Path $inbox 'jMlock.meta.json'
        [IO.File]::WriteAllText($lockPath, '{"timeoutSec":60}')
        $fs = [IO.File]::Open($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        try { $mLock = Get-JobMetadata -JobId 'jMlock' } finally { $fs.Dispose() }
        $vLock = Get-JobNotAfterVerdict -Metadata $mLock
        Assert-True ($mLock.metaUnreadable -eq $true -and $mLock.metaReadError -like 'cannot be read:*' -and $vLock.state -ceq 'unparseable') "notafter-meta-unreadable: bytes that cannot be read fail closed (the agent cannot tell): $($vLock | ConvertTo-Json -Compress)"
        [IO.File]::WriteAllText((Join-Path $inbox 'jMok.meta.json'), (New-NotAfterMetaJson 600))
        $mOk = Get-JobMetadata -JobId 'jMok'
        Assert-True ($mOk.missing -eq $false -and $mOk.metaUnreadable -ne $true -and (Get-JobNotAfterVerdict -Metadata $mOk).state -ceq 'open') "notafter-meta-unreadable: a readable claim is read as before"
        # Run: the torn claim naming a deadline is refused before the gate, with a readable reason.
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jMq' '{"timeoutSec":60,"maxQueueAgeSec":600,"notAfterUtc":"2026-10-2'
        $null = Invoke-QueuedJob -Job $job
        Assert-NotAfterExit 'notafter-meta-unreadable' 'jMq' 'pre-gate' $false ''
        $p = $script:QJ.published[0]
        Assert-True ($p.notAfterState -ceq 'unparseable' -and $p.notAfterWhy -like 'meta.json does not parse:*' -and $p.stderr -ceq 'notAfterUtc could not be read (fail closed) before launch (pre-gate); job was not executed') "notafter-meta-unreadable: the result says why: state=$($p.notAfterState) why=[$($p.notAfterWhy)] stderr=[$($p.stderr)]"
        Assert-True (@($script:Log | Where-Object { $_ -like "json read failed path=$(Join-Path $inbox 'jMq.meta.json') error=does not parse:* deadlineUnknown=True" }).Count -eq 1) "notafter-meta-unreadable: the read failure is typed once"
        $results.notAfterMetaUnreadable = [ordered]@{ absent = $vAbs.state; tornNoDeadline = $vTorn.state; tornWithDeadline = $vCut.state; locked = $vLock.state; why = $p.notAfterWhy }
    }

    # ------------------------------------------------------------ notafter-meta-sticky: Get-JobMetadata's deadline flag over its 3 reads
    # LEASE-NOT-AFTER-HARDENING-2 H2 (key3 on #648). The bytes change between the agent's own reads, at its 200 ms wait (Start-Sleep
    # is shadowed for the case): once any read names notAfterUtc, in any case, or cannot be read, a later read that names no key and
    # still does not parse cannot clear that. A read that parses wins, as before. A step 'LOCKED:<bytes>' holds the file unshared.
    if (Test-Case 'notafter-meta-sticky') {
        $null = Reset-QueuedJobCase @{ request = 'active' } 'jMs' $null
        $script:MetaLock = $null
        $script:MetaSteps = $null
        function Set-MetaStep([string]$Path, [string]$Step) {
            if ($null -ne $script:MetaLock) { $script:MetaLock.Dispose(); $script:MetaLock = $null }
            $locked = $Step.StartsWith('LOCKED:')
            [IO.File]::WriteAllText($Path, $(if ($locked) { $Step.Substring(7) } else { $Step }))
            if ($locked) { $script:MetaLock = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        }
        function Start-Sleep {
            param([int]$Milliseconds)
            $st = $script:MetaSteps
            if ($null -eq $st) { Microsoft.PowerShell.Utility\Start-Sleep -Milliseconds $Milliseconds; return }
            $st.i++
            Set-MetaStep $st.path $st.steps[[Math]::Min($st.i, $st.steps.Count - 1)]
        }
        function Get-JobMetadataSteps([string]$JobId, [string[]]$Steps) {
            $script:MetaSteps = [pscustomobject]@{ path = (Join-Path $inbox "$JobId.meta.json"); steps = $Steps; i = 0 }
            try { Set-MetaStep $script:MetaSteps.path $Steps[0]; return (Get-JobMetadata -JobId $JobId) }
            finally {
                if ($null -ne $script:MetaLock) { $script:MetaLock.Dispose(); $script:MetaLock = $null }
                $script:MetaSteps = $null
            }
        }
        $future = [DateTime]::UtcNow.AddSeconds(600).ToString('o')
        $pastZ = [DateTime]::UtcNow.AddSeconds(-60).ToString('o')
        $sticky = @(
            @{ n = 'key-then-torn-prefix'; steps = @('{"timeoutSec":60,"notAfterUtc":"2026-10-2', '{"timeoutSec":60,"no', '{"timeoutSec":60,"no'); unknown = $true; state = 'unparseable' }
            @{ n = 'key-in-the-middle-read'; steps = @('{"timeo', '{"timeoutSec":60,"notAfterUtc":"20', '{"timeo'); unknown = $true; state = 'unparseable' }
            @{ n = 'locked-then-torn-prefix'; steps = @('LOCKED:{"timeoutSec":60}', '{"timeo', '{"timeo'); unknown = $true; state = 'unparseable' }
            @{ n = 'torn-mixed-NotAfterUtc'; steps = @('{"timeoutSec":60,"NotAfterUtc":"2026-10-2'); unknown = $true; state = 'unparseable' }
            @{ n = 'torn-upper-NOTAFTERUTC'; steps = @('{"NOTAFTERUTC":"2026-1'); unknown = $true; state = 'unparseable' }
            @{ n = 'parsed-mixed-NotAfterUtc-past'; steps = @("{`"timeoutSec`":60,`"NotAfterUtc`":`"$pastZ`"}"); unknown = $false; state = 'passed' }
            @{ n = 'parsed-upper-NOTAFTERUTC-future'; steps = @("{`"timeoutSec`":60,`"NOTAFTERUTC`":`"$future`"}"); unknown = $false; state = 'open' }
            @{ n = 'key-then-parsed'; steps = @('{"timeoutSec":60,"notAfterUtc":"2026-1', "{`"timeoutSec`":60,`"notAfterUtc`":`"$future`"}"); unknown = $false; state = 'open' }
            @{ n = 'torn-prefix-only'; steps = @('{"timeo'); unknown = $false; state = 'none' }
        )
        $stickyGot = [ordered]@{}
        $stickyBad = [System.Collections.Generic.List[string]]::new()
        try {
            foreach ($c in $sticky) {
                $script:Log.Clear()
                $m = Get-JobMetadataSteps "jMs-$($c.n)" $c.steps
                $vS = Get-JobNotAfterVerdict -Metadata $m
                $unknown = ($m.metaUnreadable -eq $true)
                $stickyGot[$c.n] = "$($vS.state)|unknown=$unknown"
                $ok = ($unknown -eq $c.unknown -and $vS.state -ceq $c.state)
                if ($c.unknown) { $ok = $ok -and @($script:Log | Where-Object { $_ -like '*json read failed path=* deadlineUnknown=True' }).Count -eq 1 }
                if (-not $ok) { $stickyBad.Add("$($c.n): want $($c.state)|unknown=$($c.unknown) got $($stickyGot[$c.n])") }
            }
        }
        finally { Remove-Item -LiteralPath Function:\Start-Sleep -ErrorAction SilentlyContinue }
        Assert-True ($stickyBad.Count -eq 0) "notafter-meta-sticky: a deadline once seen (any case) or unreadable stays unknown over the reads; a parsed read wins: $($stickyBad -join ' ; ')"
        $results.notAfterMetaSticky = $stickyGot
    }

    # ================================================================ runner liveness r1 (LEASE-BATCH-OPEN-RUNNER-LIVENESS-1)
    # notafter\<jobId>.json: a present sidecar whose aliveUntilUtc has passed, or that cannot be read, ends the job like a passed
    # notAfterUtc; absent = notAfterUtc alone. The earlier bound wins; remainingSec and --admit-timeout come from meta.json alone.
    # Each case keeps the queue-age hang guard, so an agent that ignores the sidecar ends queue-expired (a FAIL), never a hang.
    function Get-LivenessSuffix([string]$Until) { return " source=sidecar livenessUntilUtc=$Until; NOT run, no lease kept" }

    # ------------------------------------------------------------ liveness-dead-before-request: a lapsed runner gets no request
    if (Test-Case 'liveness-dead-before-request') {
        Reset-Case @{ request = 'active' }
        $script:LeaseNotAfterMinAdmitSec = 30
        $script:AgeAfter = (Get-Date).AddSeconds(20)
        $mL = New-NotAfterMeta 3600
        $until = Write-Sidecar 'jLd' ([DateTime]::UtcNow.AddSeconds(-1))
        $adm = Wait-QuietLeaseAdmission -JobId 'jLd' -Job ([pscustomobject]@{}) -Metadata $mL
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'pre-request' -and $reqs.Count -eq 0 -and $adm.leaseRequests -eq 0) "liveness-dead-before-request: a lapsed sidecar ends the wait before any request: label=$($adm.label) phase=$($adm.phase) requests=$($reqs.Count)"
        Assert-True ($adm.notAfterSource -ceq 'sidecar' -and $adm.notAfterState -ceq 'passed' -and $adm.livenessState -ceq 'lapsed' -and $adm.livenessUntilUtc -ceq $until -and $adm.notAfterUtc -ceq $mL.notAfterUtc -and $null -eq $adm.leaseRelease) "liveness-dead-before-request: typed source=sidecar, the meta bound still recorded: $($adm | ConvertTo-Json -Compress -Depth 4)"
        $want = "quiet-lease: expired-not-after job=jLd phase=pre-request notAfterUtc=$($mL.notAfterUtc) requests=0 releaseRc=none$(Get-LivenessSuffix $until)"
        Assert-True (@($script:Log | Where-Object { $_ -ceq $want }).Count -eq 1) "liveness-dead-before-request: the typed source=sidecar line [$want]: $($script:Log -join ' | ')"
        Assert-True ($adm.why -like 'runner liveness sidecar aliveUntilUtc * passed *source=sidecar*' -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "liveness-dead-before-request: why names the sidecar: $($adm.why)"
        $results.livenessDeadBeforeRequest = [ordered]@{ label = $adm.label; phase = $adm.phase; source = $adm.notAfterSource; requests = $reqs.Count }
    }

    # ------------------------------------------------------------ liveness-lapses-while-pending: the pending request is released
    if (Test-Case 'liveness-lapses-while-pending') {
        Reset-Case @{ request = 'hang' }
        $script:LeaseNotAfterMinAdmitSec = 1
        $savedPendingMax = $LeasePendingMaxSec
        $LeasePendingMaxSec = 1800
        $script:AgeAfter = (Get-Date).AddSeconds(20)
        $mL = New-NotAfterMeta 3600
        $aliveAt = [DateTime]::UtcNow.AddSeconds(3)
        $until = Write-Sidecar 'jLp' $aliveAt
        $script:LastWaitBeat = $null
        $script:BeatHook = { param($Activity) if ($Activity -like 'gate-wait job=jLp *') { $script:LastWaitBeat = [DateTime]::UtcNow } }
        try { $adm = Wait-QuietLeaseAdmission -JobId 'jLp' -Job ([pscustomobject]@{}) -Metadata $mL }
        finally { $LeasePendingMaxSec = $savedPendingMax }
        $tEnd = [DateTime]::UtcNow
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        $at = if ($reqs.Count -ge 1) { Get-RequestArg $reqs[0] '--admit-timeout' } else { $null }
        Assert-True ($reqs.Count -eq 1 -and [int]$at -eq 1800) "liveness-lapses-while-pending: one request filed while the sidecar was live, its --admit-timeout from meta (LeasePendingMaxSec 1800, meta has 3600 s), never the 3 s window: requests=$($reqs.Count) admit-timeout=$at"
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'pending' -and $null -ne $adm.leaseRelease -and $adm.leaseRelease.phase -ceq 'pending' -and [string]$adm.leaseRelease.releaseRc -ceq '0') "liveness-lapses-while-pending: released while pending: label=$($adm.label) phase=$($adm.phase) release=$($adm.leaseRelease | ConvertTo-Json -Compress)"
        Assert-True ($adm.notAfterSource -ceq 'sidecar' -and $adm.notAfterState -ceq 'passed' -and $adm.livenessUntilUtc -ceq $until) "liveness-lapses-while-pending: typed source=sidecar: $($adm | Select-Object notAfterSource, notAfterState, livenessUntilUtc | ConvertTo-Json -Compress)"
        $seen = if ($null -ne $script:LastWaitBeat) { ($script:LastWaitBeat - $aliveAt).TotalSeconds } else { $null }
        $ended = ($tEnd - $aliveAt).TotalSeconds
        Assert-True ($null -ne $seen -and $seen -lt 1.5 -and $ended -gt 0 -and $ended -lt 10) "liveness-lapses-while-pending: the lapse is seen within the loops after aliveUntilUtc (last gate-wait beat $([Math]::Round([double]$seen, 2)) s after it) and the wait ends $([Math]::Round($ended, 2)) s after it"
        Assert-True ((Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting -and (Get-Released).Count -eq 1) "liveness-lapses-while-pending: released once, wrapper gone, no lease kept"
        $want = "quiet-lease: expired-not-after job=jLp phase=pending notAfterUtc=$($mL.notAfterUtc) requests=1 releaseRc=0$(Get-LivenessSuffix $until)"
        Assert-True (@($script:Log | Where-Object { $_ -ceq $want }).Count -eq 1) "liveness-lapses-while-pending: typed line [$want]"
        $results.livenessLapsesWhilePending = [ordered]@{ admitTimeout = [int]$at; phase = $adm.phase; seenAfterSec = [Math]::Round([double]$seen, 2); endedAfterSec = [Math]::Round($ended, 2) }
    }

    # ------------------------------------------------------------ liveness-lapses-at-active: a lease that turns active after it is released
    if (Test-Case 'liveness-lapses-at-active') {
        Reset-Case @{ request = 'active'; active_after = 4 }
        $script:LeaseNotAfterMinAdmitSec = 1
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $mL = New-NotAfterMeta 3600
        $script:AliveAt = [DateTime]::UtcNow.AddSeconds(2.5)
        $until = Write-Sidecar 'jLa' $script:AliveAt
        # The request is filed under a live sidecar; it turns active only after aliveUntilUtc, before the gate's next read.
        $script:LogHook = {
            param($Message)
            if ($script:HookFired -or $Message -notlike 'quiet-lease: request started job=jLa *') { return }
            $script:HookFired = $true
            $null = Wait-Until { $f = Get-FakeFlag; $null -ne $f -and $f.state -eq 'active' -and [DateTime]::UtcNow -gt $script:AliveAt } 12
            Start-Sleep -Milliseconds 500
        }
        $adm = Wait-QuietLeaseAdmission -JobId 'jLa' -Job ([pscustomobject]@{}) -Metadata $mL
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($script:HookFired -and $reqs.Count -eq 1) "liveness-lapses-at-active: one request, filed under a live sidecar; the race landed"
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'post-active' -and $adm.notAfterSource -ceq 'sidecar' -and $adm.notAfterState -ceq 'passed') "liveness-lapses-at-active: not admitted, released post-active: label=$($adm.label) phase=$($adm.phase) source=$($adm.notAfterSource)"
        Assert-True ((Get-Released).Count -eq 1 -and $null -ne $adm.leaseRelease -and [string]$adm.leaseRelease.releaseRc -ceq '0' -and $adm.leaseRelease.phase -ceq 'post-active') "liveness-lapses-at-active: released once, releaseRc 0: $($adm.leaseRelease | ConvertTo-Json -Compress)"
        Assert-True ((Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0 -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "liveness-lapses-at-active: wrapper gone, record removed, no lease kept"
        $want = "quiet-lease: expired-not-after job=jLa phase=post-active notAfterUtc=$($mL.notAfterUtc) requests=1 releaseRc=0$(Get-LivenessSuffix $until)"
        Assert-True (@($script:Log | Where-Object { $_ -ceq $want }).Count -eq 1) "liveness-lapses-at-active: typed line [$want]"
        $results.livenessLapsesAtActive = [ordered]@{ phase = $adm.phase; releaseRc = $adm.leaseRelease.releaseRc; source = $adm.notAfterSource }
    }

    # ------------------------------------------------------------ liveness-renewed-across-exit75: renewals, not the first write, carry it
    # CI-FLAKE-HEAVY-SLOT-GATE-QUIET-LEASE-1: the renewals are EVENTS, not a wall clock. A runspace that rewrote the sidecar every 1 s
    # (3 s window) lapsed it whenever a loaded runner starved it for 3 s (hosted job 124164592779: label=expired-not-after
    # livenessState=lapsed). Now each of the gate's own sidecar reads first renews (Start-SidecarRenewOnRead), so what is asserted is
    # the count of renewals the gate observed and the seq it admitted on, never how many wall-clock seconds passed between writes.
    if (Test-Case 'liveness-renewed-across-exit75') {
        $results.livenessRenewedAcrossExit75 = Invoke-RenewedAcrossExit75 'liveness-renewed-across-exit75' 'jLr' ([double]$env:QL_TEST_RENEW_STALL_SEC)
    }

    # ------------------------------------------------------------ liveness-renewed-across-stall: a gap longer than the window between renewals
    # The stall (4 s > the 3 s window) lands in the gate loop's heartbeat, between two renewals. The sidecar is lapsed on disk
    # meanwhile; the next read renews it first, so the job is still admitted. The stall itself is asserted to have run.
    if (Test-Case 'liveness-renewed-across-stall') {
        $results.livenessRenewedAcrossStall = Invoke-RenewedAcrossExit75 'liveness-renewed-across-stall' 'jLs' 4
    }

    # ------------------------------------------------------------ liveness-renewal-stops-lapses: the negative case, renewal really stops
    if (Test-Case 'liveness-renewal-stops-lapses') {
        Reset-Case @{ request = 'hang' }
        $script:LeaseNotAfterMinAdmitSec = 1
        $savedPendingMax = $LeasePendingMaxSec
        $LeasePendingMaxSec = 1800
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $mL = New-NotAfterMeta 3600
        $st = Start-SidecarRenewOnRead 'jLz' 3 0
        try { $adm = Wait-QuietLeaseAdmission -JobId 'jLz' -Job ([pscustomobject]@{}) -Metadata $mL }
        finally { $LeasePendingMaxSec = $savedPendingMax }
        $tEnd = [DateTime]::UtcNow
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'pending' -and $adm.notAfterSource -ceq 'sidecar' -and $adm.livenessState -ceq 'lapsed') "liveness-renewal-stops-lapses: renewal stopped, so the sidecar lapsed and the pending request was released: label=$($adm.label) phase=$($adm.phase) source=$($adm.notAfterSource) liveness=$($adm.livenessState)"
        Assert-True ($st.renews -eq 3 -and $st.reads -gt 3 -and (Get-Released).Count -eq 1) "liveness-renewal-stops-lapses: exactly 3 renewals, more reads than renewals, one release: renews=$($st.renews) reads=$($st.reads) released=$((Get-Released).Count)"
        $sinceRenew = ($tEnd - $st.lastRenewUtc).TotalSeconds
        Assert-True ($sinceRenew -ge 2.9) "liveness-renewal-stops-lapses: the lapse is called only after the last renewal's $($st.ttl) s window ($([Math]::Round($sinceRenew, 2)) s after it)"
        $results.livenessRenewalStopsLapses = [ordered]@{ label = $adm.label; phase = $adm.phase; renews = $st.renews; reads = $st.reads; sinceLastRenewSec = [Math]::Round($sinceRenew, 1) }
    }

    # ------------------------------------------------------------ liveness-absent-unchanged: no sidecar = notAfterUtc alone, as before
    # The existing notafter-* and cancel-active-race cases write no sidecar and run with their assertions untouched.
    if (Test-Case 'liveness-absent-unchanged') {
        Reset-Case @{ request = 'active' }
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $adm = Wait-QuietLeaseAdmission -JobId 'jLn' -Job ([pscustomobject]@{}) -Metadata (New-NotAfterMeta 3600)
        $req = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($adm.label -eq 'clear' -and $req.Count -eq 1 -and ($req[0].argv -join ' ') -ceq 'lease request --label quiet-lease mlv --minutes 31 --admit-timeout 1 --slots 2') "liveness-absent-unchanged: the request argv is today's: [$($req[0].argv -join ' ')]"
        Assert-True ($adm.livenessState -ceq 'absent' -and $adm.notAfterSource -ceq 'meta' -and $null -eq $adm.livenessUntilUtc) "liveness-absent-unchanged: the admission records livenessState absent, source meta: $($adm | Select-Object livenessState, notAfterSource, livenessUntilUtc | ConvertTo-Json -Compress)"
        Exit-HeavySlot
        $results.livenessAbsentUnchanged = [ordered]@{ label = $adm.label; livenessState = $adm.livenessState }
    }

    # ------------------------------------------------------------ liveness-unparseable-expired: a sidecar that cannot be read fails closed
    if (Test-Case 'liveness-unparseable-expired') {
        $futureU = [DateTime]::UtcNow.AddSeconds(600).ToString('o')
        $vectors = [ordered]@{
            'not-json'     = '{not json'
            'wrong-schema' = (New-SidecarJson 'jLu-wrong-schema' $futureU @{ schema = 'mlv-app/job-notafter-sidecar/v2' })
            'wrong-jobid'  = (New-SidecarJson 'jLu-someone-else' $futureU)
            'garbage'      = (New-SidecarJson 'jLu-garbage' 'garbage')
            'oversize'     = (New-SidecarJson 'jLu-oversize' $futureU @{ pad = ('x' * 5000) })
        }
        $unp = [ordered]@{}
        foreach ($n in $vectors.Keys) {
            Reset-Case @{ request = 'active' }
            $script:LeaseNotAfterMinAdmitSec = 1
            $script:AgeAfter = (Get-Date).AddSeconds(15)
            $id = "jLu-$n"
            $mL = New-NotAfterMeta 3600
            Write-SidecarRaw $id $vectors[$n]
            $adm = Wait-QuietLeaseAdmission -JobId $id -Job ([pscustomobject]@{}) -Metadata $mL
            $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
            Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'pre-request' -and $adm.notAfterState -ceq 'unparseable' -and $adm.notAfterSource -ceq 'sidecar' -and $adm.livenessState -ceq 'unparseable' -and $reqs.Count -eq 0) "liveness-unparseable-expired (${n}): fail closed, no request: label=$($adm.label) state=$($adm.notAfterState) source=$($adm.notAfterSource) requests=$($reqs.Count) why=$($adm.why)"
            $want = "quiet-lease: expired-not-after job=$id phase=pre-request notAfterUtc=$($mL.notAfterUtc) requests=0 releaseRc=none$(Get-LivenessSuffix 'unparseable')"
            Assert-True (@($script:Log | Where-Object { $_ -ceq $want }).Count -eq 1) "liveness-unparseable-expired (${n}): typed line [$want]: $($script:Log -join ' | ')"
            Assert-True (Test-Path -LiteralPath (Join-Path $notAfterDir "$id.json") -PathType Leaf) "liveness-unparseable-expired (${n}): the agent never deletes a sidecar"
            $unp[$n] = $adm.why
        }
        $results.livenessUnparseableExpired = $unp
    }

    # ------------------------------------------------------------ liveness-pure: Get-JobNotAfterVerdict -Sidecar
    if (Test-Case 'liveness-pure') {
        Assert-True ($liftMissing -notcontains 'Read-JobNotAfterSidecar') "agent defines Read-JobNotAfterSidecar"
        $now = [DateTime]::new(2026, 10, 24, 2, 0, 0, [DateTimeKind]::Utc)
        $z3 = '2026-10-24T03:00:00.0000000Z'
        $mOpen = [pscustomobject]@{ notAfterUtc = $z3 }
        $mNone = [pscustomobject]@{ timeoutSec = 1 }
        $mPast = [pscustomobject]@{ notAfterUtc = '2026-10-24T01:00:00Z' }
        $mBad = [pscustomobject]@{ notAfterUtc = 'not-a-date' }
        $mNear = [pscustomobject]@{ notAfterUtc = '2026-10-24T02:10:00.7000000Z' }
        function New-SidecarRead([string]$Text, [string]$State = 'present', [string]$JobId = 'jP') { return [pscustomobject]@{ state = $State; text = $Text; error = $(if ($State -eq 'unreadable') { 'IOException: unit lock' } else { $null }); jobId = $JobId; path = "unit\$JobId.json" } }
        $live = New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:01:00Z' @{ writtenUtc = '2026-10-24T01:59:30Z' } 7)
        $sOk = '2026-10-24T02:01:00.0000000Z'
        $lt = @(
            # n; meta; sidecar; state; source; livenessState; remainingSec; notAfterUtc; livenessUntilUtc
            @{ n = 'absent-state'; m = $mOpen; s = [pscustomobject]@{ state = 'absent'; text = $null; error = $null; jobId = 'jP' }; st = 'open'; src = 'meta'; ls = 'absent'; r = 3600; o = $z3; u = $null }
            @{ n = 'absent-null'; m = $mOpen; s = $null; st = 'open'; src = 'meta'; ls = 'absent'; r = 3600; o = $z3; u = $null }
            @{ n = 'live-meta-open'; m = $mOpen; s = $live; st = 'open'; src = 'meta'; ls = 'live'; r = 3600; o = $z3; u = $sOk }
            @{ n = 'live-meta-none'; m = $mNone; s = $live; st = 'none'; src = 'meta'; ls = 'live'; r = $null; o = $null; u = $sOk }
            @{ n = 'live-offset'; m = $mOpen; s = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T04:01:00+02:00')); st = 'open'; src = 'meta'; ls = 'live'; r = 3600; o = $z3; u = $sOk }
            # liveness r2: the root-shape check reads past JSON whitespace, so a valid object after it is still live.
            @{ n = 'live-leading-whitespace'; m = $mOpen; s = (New-SidecarRead (" `r`n`t" + (New-SidecarJson 'jP' '2026-10-24T02:01:00Z'))); st = 'open'; src = 'meta'; ls = 'live'; r = 3600; o = $z3; u = $sOk }
            @{ n = 'far-future-never-extends'; m = $mOpen; s = (New-SidecarRead (New-SidecarJson 'jP' '2099-01-01T00:00:00Z')); st = 'open'; src = 'meta'; ls = 'live'; r = 3600; o = $z3; u = '2099-01-01T00:00:00.0000000Z' }
            @{ n = 'lapsed'; m = $mOpen; s = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T01:59:59Z')); st = 'passed'; src = 'sidecar'; ls = 'lapsed'; r = 3600; o = $z3; u = '2026-10-24T01:59:59.0000000Z' }
            @{ n = 'lapsed-exactly-now'; m = $mOpen; s = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:00:00Z')); st = 'passed'; src = 'sidecar'; ls = 'lapsed'; r = 3600; o = $z3; u = '2026-10-24T02:00:00.0000000Z' }
            @{ n = 'lapsed-meta-none'; m = $mNone; s = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T01:00:00Z')); st = 'passed'; src = 'sidecar'; ls = 'lapsed'; r = $null; o = $null; u = '2026-10-24T01:00:00.0000000Z' }
            @{ n = 'meta-passed-sidecar-live'; m = $mPast; s = $live; st = 'passed'; src = 'meta'; ls = 'live'; r = -3600; o = '2026-10-24T01:00:00.0000000Z'; u = $sOk }
            @{ n = 'meta-unparseable-sidecar-live'; m = $mBad; s = $live; st = 'unparseable'; src = 'meta'; ls = 'live'; r = $null; o = $null; u = $sOk }
        )
        $bad = [ordered]@{
            'unreadable'     = (New-SidecarRead $null 'unreadable')
            'not-json'       = (New-SidecarRead '{not json')
            'empty'          = (New-SidecarRead '')
            'json-array'     = (New-SidecarRead '[1,2]')
            # liveness r2 (Dot B1 on #663): pwsh 7's ConvertFrom-Json unrolls a one-element root array, so '[{...}]' would read
            # as the object inside it. The root must be an object; these three are each a valid sidecar wrapped in an array.
            'singleton-object-array' = (New-SidecarRead ('[' + (New-SidecarJson 'jP' '2026-10-24T02:01:00Z') + ']'))
            'singleton-array-array'  = (New-SidecarRead ('[[' + (New-SidecarJson 'jP' '2026-10-24T02:01:00Z') + ']]'))
            'empty-array'    = (New-SidecarRead '[]')
            'json-string'    = (New-SidecarRead '"x"')
            'wrong-schema'   = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:01:00Z' @{ schema = 'mlv-app/job-notafter-sidecar/v2' }))
            'schema-case'    = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:01:00Z' @{ schema = 'MLV-APP/job-notafter-sidecar/v1' }))
            'schema-missing' = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:01:00Z' @{ schema = $null }))
            'wrong-jobid'    = (New-SidecarRead (New-SidecarJson 'jOther' '2026-10-24T02:01:00Z'))
            'jobid-case'     = (New-SidecarRead (New-SidecarJson 'JP' '2026-10-24T02:01:00Z'))
            'jobid-missing'  = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:01:00Z' @{ jobId = $null }))
            'until-missing'  = (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:01:00Z' @{ aliveUntilUtc = $null }))
            'until-blank'    = (New-SidecarRead (New-SidecarJson 'jP' '   '))
            'until-garbage'  = (New-SidecarRead (New-SidecarJson 'jP' 'garbage'))
            'until-number'   = (New-SidecarRead (New-SidecarJson 'jP' 12345))
        }
        foreach ($k in $bad.Keys) { $lt += @{ n = "unparseable-$k"; m = $mOpen; s = $bad[$k]; st = 'unparseable'; src = 'sidecar'; ls = 'unparseable'; r = 3600; o = $z3; u = $null } }
        $plain = Get-JobNotAfterVerdict -Metadata $mOpen -NowUtc $now
        $got = [ordered]@{}
        $wantProps = @('state', 'notAfterUtc', 'remainingSec', 'why', 'livenessState', 'livenessUntilUtc', 'livenessWrittenUtc', 'livenessSeq', 'source')
        foreach ($c in $lt) {
            $v = Get-JobNotAfterVerdict -Metadata $c.m -NowUtc $now -Sidecar $c.s
            $props = @($v.PSObject.Properties.Name)
            $remOk = if ($null -eq $c.r) { $null -eq $v.remainingSec } else { $null -ne $v.remainingSec -and [Math]::Abs([double]$v.remainingSec - $c.r) -lt 0.001 }
            Assert-True (@($wantProps | Where-Object { $props -notcontains $_ }).Count -eq 0) "liveness-pure ($($c.n)): every verdict carries $($wantProps -join ', '): $($props -join ', ')"
            Assert-True ($v.state -ceq $c.st -and $v.source -ceq $c.src -and $v.livenessState -ceq $c.ls -and $remOk -and $v.notAfterUtc -ceq $c.o -and $v.livenessUntilUtc -ceq $c.u -and -not [string]::IsNullOrWhiteSpace($v.why)) "liveness-pure ($($c.n)): want $($c.st)/$($c.src)/$($c.ls) remaining=$($c.r) until=$($c.u): $($v | ConvertTo-Json -Compress)"
            if ($c.src -ceq 'sidecar') { Assert-True ($v.why -like 'runner liveness sidecar *source=sidecar*') "liveness-pure ($($c.n)): why says the sidecar decided: $($v.why)" }
            $got[$c.n] = "$($v.state)|$($v.source)|$($v.livenessState)"
        }
        foreach ($n in @('absent-state', 'absent-null')) {
            $v = Get-JobNotAfterVerdict -Metadata $mOpen -NowUtc $now -Sidecar ($lt | Where-Object { $_.n -eq $n }).s
            Assert-True ($v.state -ceq $plain.state -and $v.notAfterUtc -ceq $plain.notAfterUtc -and $v.remainingSec -eq $plain.remainingSec -and $v.why -ceq $plain.why -and $plain.livenessState -ceq 'absent' -and $plain.source -ceq 'meta') "liveness-pure (${n}): the meta verdict as before: $($v | ConvertTo-Json -Compress) vs $($plain | ConvertTo-Json -Compress)"
        }
        $vPassedMeta = Get-JobNotAfterVerdict -Metadata $mPast -NowUtc $now -Sidecar $live
        Assert-True ($vPassedMeta.why -ceq (Get-JobNotAfterVerdict -Metadata $mPast -NowUtc $now).why) "liveness-pure: a passed meta verdict stands unchanged: $($vPassedMeta.why)"
        $vLive = Get-JobNotAfterVerdict -Metadata $mOpen -NowUtc $now -Sidecar $live
        Assert-True ([long]$vLive.livenessSeq -eq 7 -and $vLive.livenessWrittenUtc -ceq '2026-10-24T01:59:30.0000000Z') "liveness-pure: seq and writtenUtc are recorded: seq=$($vLive.livenessSeq) written=$($vLive.livenessWrittenUtc)"
        # INVARIANT: the request bound never comes from the sidecar. A 5 s window over a meta bound 600.7 s away asks for 600 s.
        $savedPendingMax = $LeasePendingMaxSec
        $LeasePendingMaxSec = 1800
        $script:LeaseNotAfterMinAdmitSec = 30
        try {
            $short = New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T02:00:05Z')
            $aNear = Get-QuietLeaseAdmitTimeout -Verdict (Get-JobNotAfterVerdict -Metadata $mNear -NowUtc $now -Sidecar $short)
            $aNone = Get-QuietLeaseAdmitTimeout -Verdict (Get-JobNotAfterVerdict -Metadata $mNone -NowUtc $now -Sidecar $short)
        }
        finally { $LeasePendingMaxSec = $savedPendingMax }
        Assert-True ($aNear.ok -and $aNear.sec -eq 600 -and $aNear.source -ceq 'not-after' -and $aNone.ok -and $aNone.sec -eq 1800 -and $aNone.source -ceq 'pending-max') "liveness-pure: a 5 s liveness window never shrinks --admit-timeout: near=$($aNear | ConvertTo-Json -Compress) none=$($aNone | ConvertTo-Json -Compress)"
        $atBody = Get-FunctionBody 'Get-QuietLeaseAdmitTimeout'
        Assert-True ($atBody -notmatch '(?i)liveness|sidecar') "liveness-pure: Get-QuietLeaseAdmitTimeout reads no liveness field"
        # Publish-NotAfterExpiredResult types the source; the publish and move are stubbed for the call, then restored.
        $savedPub = ${function:Publish-JobResult}; $savedMove = ${function:Move-JobArtifacts}
        $script:Published = $null
        function Publish-JobResult { param([string]$JobId, $Result) $script:Published = $Result; return (Join-Path $fx "$JobId.result.json") }
        function Move-JobArtifacts { param([string]$JobId, [string]$JobPath, [string]$DestinationDirectory) }
        try {
            $pubJob = [pscustomobject]@{ BaseName = 'jLpub.job'; FullName = (Join-Path $fx 'jLpub.job.ps1') }
            $pubMeta = [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 0 }
            $vLapsed = Get-JobNotAfterVerdict -Metadata $mOpen -NowUtc $now -Sidecar (New-SidecarRead (New-SidecarJson 'jP' '2026-10-24T01:59:59Z'))
            Publish-NotAfterExpiredResult -Job $pubJob -Metadata $pubMeta -Admission ([pscustomobject]@{ label = 'expired-not-after'; phase = 'pending' }) -QueueAgeSec 3 -Verdict $vLapsed
            $p1 = $script:Published
            $vUnp = Get-JobNotAfterVerdict -Metadata $mOpen -NowUtc $now -Sidecar $bad['not-json']
            Publish-NotAfterExpiredResult -Job $pubJob -Metadata $pubMeta -QueueAgeSec 0 -Verdict $vUnp
            $p2 = $script:Published
            Publish-NotAfterExpiredResult -Job $pubJob -Metadata $pubMeta -QueueAgeSec 0 -Verdict (Get-JobNotAfterVerdict -Metadata $mPast -NowUtc $now)
            $p3 = $script:Published
        }
        finally {
            Set-Item -Path Function:\Publish-JobResult -Value $savedPub
            Set-Item -Path Function:\Move-JobArtifacts -Value $savedMove
        }
        Assert-True ($p1.exitCode -eq 125 -and $p1.notAfterExpired -eq $true -and $p1.notAfterSource -ceq 'sidecar' -and $p1.livenessUntilUtc -ceq '2026-10-24T01:59:59.0000000Z' -and $p1.notAfterState -ceq 'passed' -and $p1.stderr -ceq 'runner liveness lapsed (sidecar aliveUntilUtc 2026-10-24T01:59:59.0000000Z passed) before launch (pending); job was not executed') "liveness-pure: a lapsed sidecar's result: $($p1 | ConvertTo-Json -Compress -Depth 4)"
        Assert-True ($p2.exitCode -eq 125 -and $p2.notAfterSource -ceq 'sidecar' -and $p2.notAfterState -ceq 'unparseable' -and $null -eq $p2.livenessUntilUtc -and $p2.stderr -ceq 'runner liveness sidecar could not be read (fail closed) before launch (pre-gate); job was not executed') "liveness-pure: an unreadable sidecar's result: $($p2 | ConvertTo-Json -Compress -Depth 4)"
        Assert-True ($p3.notAfterSource -ceq 'meta' -and $p3.stderr -ceq 'notAfterUtc 2026-10-24T01:00:00.0000000Z passed before launch (pre-gate); job was not executed') "liveness-pure: a meta expiry's result text is unchanged: $($p3.stderr)"
        $results.livenessPure = $got
    }

    # ------------------------------------------------------------ liveness-reader: Read-JobNotAfterSidecar
    if (Test-Case 'liveness-reader') {
        Assert-True ($liftMissing -notcontains 'Read-JobNotAfterSidecar') "agent defines Read-JobNotAfterSidecar"
        Reset-Case @{ request = 'active' }
        $rAbsent = Read-JobNotAfterSidecar -JobId 'jRa'
        $savedDir = $notAfterDir
        $notAfterDir = Join-Path $fx 'notafter-missing'
        try { $rNoDir = Read-JobNotAfterSidecar -JobId 'jRa' } finally { $notAfterDir = $savedDir }
        Assert-True ($rAbsent.state -ceq 'absent' -and $rNoDir.state -ceq 'absent' -and $rAbsent.jobId -ceq 'jRa') "liveness-reader: no file and no folder are absent: $($rAbsent | ConvertTo-Json -Compress) $($rNoDir | ConvertTo-Json -Compress)"
        $text = New-SidecarJson 'jRp' ([DateTime]::UtcNow.AddSeconds(60).ToString('o'))
        Write-SidecarRaw 'jRp' $text
        $rP = Read-JobNotAfterSidecar -JobId 'jRp'
        Assert-True ($rP.state -ceq 'present' -and $rP.text -ceq $text -and $rP.jobId -ceq 'jRp' -and $null -eq $rP.error) "liveness-reader: a present sidecar is read whole: $($rP | ConvertTo-Json -Compress)"
        # Opened with FileShare ReadWrite|Delete: a writer holding the file open (sharing read only) does not block the read.
        $pathP = Join-Path $notAfterDir 'jRp.json'
        $hold = [IO.File]::Open($pathP, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Read)
        try { $rShared = Read-JobNotAfterSidecar -JobId 'jRp' } finally { $hold.Dispose() }
        Assert-True ($rShared.state -ceq 'present') "liveness-reader: a read beside an open writer handle succeeds: $($rShared | ConvertTo-Json -Compress)"
        # A locked file: IOException (sharing violation), retried 3 times 200 ms apart, then unreadable.
        $lock = [IO.File]::Open($pathP, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $tL = Get-Date
        try { $rLock = Read-JobNotAfterSidecar -JobId 'jRp' } finally { $lock.Dispose() }
        $lockSec = ((Get-Date) - $tL).TotalSeconds
        Assert-True ($rLock.state -ceq 'unreadable' -and $rLock.error -like '*IOException*' -and $lockSec -ge 0.55 -and $lockSec -lt 5) "liveness-reader: a locked sidecar is unreadable after the retries ($([Math]::Round($lockSec, 2)) s): $($rLock | ConvertTo-Json -Compress)"
        Write-SidecarRaw 'jRo' ('{' + (' ' * 4200) + '}')
        $rOver = Read-JobNotAfterSidecar -JobId 'jRo'
        Write-SidecarRaw 'jR4' ('{' + (' ' * 4094) + '}')
        $r4 = Read-JobNotAfterSidecar -JobId 'jR4'
        Assert-True ($rOver.state -ceq 'unreadable' -and $rOver.error -like '*4096*' -and $r4.state -ceq 'present' -and $r4.text.Length -eq 4096) "liveness-reader: over 4096 bytes is unreadable, 4096 is read: $($rOver.state)/$($rOver.error) $($r4.state)"
        Assert-True ([IO.File]::ReadAllText($pathP) -ceq $text -and @(Get-ChildItem -LiteralPath $notAfterDir -File).Count -eq 3) "liveness-reader: the reads leave every sidecar in place, unchanged"
        # Read only: the reader writes, moves and deletes nothing, and notafter\ is named only where the agent creates it and here.
        $rb = Get-FunctionBody 'Read-JobNotAfterSidecar'
        $rbCode = (($rb -split "`r?`n") | Where-Object { -not $_.TrimStart().StartsWith('#') }) -join "`n"
        Assert-True ($rbCode -notmatch '(?i)Remove-Item|Move-|Set-Content|Out-File|WriteAll|\.Delete\(|FileAccess\]::(Read)?Write|FileMode\]::(Create|Truncate|Append|OpenOrCreate)') "liveness-reader: the reader never writes, moves or deletes a sidecar"
        Assert-True ($code -match '\$notAfterDir\s*=\s*Join-Path \$Root "notafter"' -and $code -match 'foreach \(\$d in @\([^)]*\$notAfterDir[^)]*\)\)') "liveness-reader: the agent creates notafter\ at start-up with its other folders"
        Assert-True (([regex]::Matches($code, '\$notAfterDir\b')).Count -eq 3) "liveness-reader: `$notAfterDir is used only by its assignment, the start-up folder list and the reader ($(([regex]::Matches($code, '\$notAfterDir\b')).Count))"
        $results.livenessReader = [ordered]@{ absent = $rAbsent.state; noDir = $rNoDir.state; present = $rP.state; shared = $rShared.state; locked = $rLock.state; lockSec = [Math]::Round($lockSec, 2); oversize = $rOver.state; at4096 = $r4.state }
    }

    # ------------------------------------------------------------ liveness-ast-guard: every verdict call passes a fresh sidecar read
    if (Test-Case 'liveness-ast-guard') {
        $def = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-JobNotAfterVerdict' }, $true)
        Assert-True ($null -ne $def) "agent defines Get-JobNotAfterVerdict"
        $calls = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-JobNotAfterVerdict' }, $true) |
            Where-Object { $_.Extent.StartOffset -lt $def.Extent.StartOffset -or $_.Extent.StartOffset -gt $def.Extent.EndOffset })
        $fresh = @($calls | Where-Object {
                $els = @($_.CommandElements)
                $i = -1
                for ($k = 0; $k -lt $els.Count; $k++) { if ($els[$k] -is [System.Management.Automation.Language.CommandParameterAst] -and $els[$k].ParameterName -eq 'Sidecar') { $i = $k } }
                $i -ge 0 -and $i + 1 -lt $els.Count -and $els[$i + 1].Extent.Text -match '^\(Read-JobNotAfterSidecar -JobId \$(jobId|JobId|l\.jobId)\)$'
            })
        # Six sites on this base (#648 added two to the four of 40c70931): the waiting-lease renew tick, the gate loop top, post-active,
        # Invoke-QueuedJob's pre-gate, post-admission and pre-launch checks.
        $where = @($calls | ForEach-Object { "L$($_.Extent.StartLineNumber)" }) -join ','
        Assert-True ($calls.Count -eq 6 -and $fresh.Count -eq 6) "liveness-ast-guard: all 6 Get-JobNotAfterVerdict calls outside its definition pass -Sidecar (Read-JobNotAfterSidecar ...): calls=$($calls.Count) fresh=$($fresh.Count) at $where"
        $results.livenessAstGuard = [ordered]@{ calls = $calls.Count; fresh = $fresh.Count; lines = $where }
    }

    # ------------------------------------------------------------ liveness-queuedjob: Invoke-QueuedJob's checks read the sidecar, typed
    if (Test-Case 'liveness-queuedjob') {
        $lq = [ordered]@{}
        # pre-gate: lapsed before the job is picked up
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jLq' (New-NotAfterMetaJson 3600)
        $u = Write-Sidecar 'jLq' ([DateTime]::UtcNow.AddSeconds(-1))
        $null = Invoke-QueuedJob -Job $job
        Assert-NotAfterExit 'liveness-queuedjob (pre-gate)' 'jLq' 'pre-gate' $false ''
        $p = $script:QJ.published[0]
        Assert-True ($p.notAfterSource -ceq 'sidecar' -and $p.notAfterState -ceq 'passed' -and $p.livenessUntilUtc -ceq $u -and $p.stderr -ceq "runner liveness lapsed (sidecar aliveUntilUtc $u passed) before launch (pre-gate); job was not executed") "liveness-queuedjob (pre-gate): typed: $($p | Select-Object notAfterSource, notAfterState, livenessUntilUtc, stderr | ConvertTo-Json -Compress)"
        $lq.preGate = $p.stderr
        # pre-gate, unreadable
        $job = Reset-QueuedJobCase @{ request = 'active' } 'jLqu' (New-NotAfterMetaJson 3600)
        Write-SidecarRaw 'jLqu' '{not json'
        $null = Invoke-QueuedJob -Job $job
        Assert-NotAfterExit 'liveness-queuedjob (pre-gate unreadable)' 'jLqu' 'pre-gate' $false ''
        $p = $script:QJ.published[0]
        Assert-True ($p.notAfterSource -ceq 'sidecar' -and $p.notAfterState -ceq 'unparseable' -and $p.stderr -ceq 'runner liveness sidecar could not be read (fail closed) before launch (pre-gate); job was not executed') "liveness-queuedjob (pre-gate unreadable): typed: $($p.stderr)"
        $lq.preGateUnreadable = $p.stderr
        # the gate: lapses while the request is pending; the gate's label is published with the sidecar's typing
        $job = Reset-QueuedJobCase @{ request = 'hang' } 'jLqg' (New-NotAfterMetaJson 3600)
        $null = Write-Sidecar 'jLqg' ([DateTime]::UtcNow.AddSeconds(600))
        $script:LqUntil = $null
        $script:LogHook = { param($Message) if ($script:HookFired -or $Message -notlike 'quiet-lease: request started job=jLqg *') { return }; $script:HookFired = $true; $script:LqUntil = Write-Sidecar 'jLqg' ([DateTime]::UtcNow.AddSeconds(-1)) 2 }
        $null = Invoke-QueuedJob -Job $job
        Assert-True ($script:HookFired) "liveness-queuedjob (gate): the sidecar lapsed while the request was pending"
        Assert-NotAfterExit 'liveness-queuedjob (gate)' 'jLqg' 'pending' $true 'jLqg.started.json|admission-wait|expired-not-after'
        $p = $script:QJ.published[0]
        Assert-True ($p.notAfterSource -ceq 'sidecar' -and $p.admission.notAfterSource -ceq 'sidecar' -and $p.livenessUntilUtc -ceq $script:LqUntil -and $p.stderr -ceq "runner liveness lapsed (sidecar aliveUntilUtc $($script:LqUntil) passed) before launch (pending); job was not executed") "liveness-queuedjob (gate): typed: $($p | Select-Object notAfterSource, livenessUntilUtc, stderr | ConvertTo-Json -Compress)"
        $lq.gate = $p.stderr
        # post-admission and pre-launch: lapses once the job is admitted / while the launching marker is written
        foreach ($c in @(@{ id = 'jLqa'; phase = 'post-admission'; hook = 'admittedHook'; retired = 'jLqa.started.json|admission-wait|expired-not-after'; when = '*after admission, before launch' },
                @{ id = 'jLql'; phase = 'pre-launch'; hook = 'markerHook'; retired = 'jLql.started.json|launching|expired-not-after'; when = '*at the last check before launch' })) {
            $job = Reset-QueuedJobCase @{ request = 'active' } $c.id (New-NotAfterMetaJson 3600)
            $null = Write-Sidecar $c.id ([DateTime]::UtcNow.AddSeconds(600))
            $script:LqId = $c.id
            $script:LqUntil = $null
            $script:QJ.($c.hook) = { $script:LqUntil = Write-Sidecar $script:LqId ([DateTime]::UtcNow.AddSeconds(-1)) 2 }
            $null = Invoke-QueuedJob -Job $job
            Assert-NotAfterExit "liveness-queuedjob ($($c.phase))" $c.id $c.phase $true $c.retired
            $p = $script:QJ.published[0]
            Assert-True ($p.notAfterSource -ceq 'sidecar' -and $p.admission.notAfterSource -ceq 'sidecar' -and $p.admission.livenessUntilUtc -ceq $script:LqUntil -and $p.admission.why -like $c.when -and $p.stderr -ceq "runner liveness lapsed (sidecar aliveUntilUtc $($script:LqUntil) passed) before launch ($($c.phase)); job was not executed") "liveness-queuedjob ($($c.phase)): typed: $($p | Select-Object notAfterSource, stderr | ConvertTo-Json -Compress) why=$($p.admission.why)"
            $lq[$c.phase] = $p.stderr
        }
        $results.livenessQueuedJob = $lq
    }

    # ================================================================ runner liveness r2 (LEASE-BATCH-OPEN-RUNNER-LIVENESS-1 r2)
    # ------------------------------------------------------------ liveness-root-shape: a sidecar whose root is not an object fails closed
    # Dot B1 on #663: pwsh 7's ConvertFrom-Json unrolls a one-element root array, so '[{valid sidecar}]' read as that sidecar,
    # live, and the job was requested, admitted and launched. Each shape wraps a sidecar that is valid for its job.
    if (Test-Case 'liveness-root-shape') {
        $rs = [ordered]@{}
        $shapes = [ordered]@{
            'singleton-object-array' = { param($id) '[' + (New-SidecarJson $id ([DateTime]::UtcNow.AddSeconds(600).ToString('o'))) + ']' }
            'singleton-array-array'  = { param($id) '[[' + (New-SidecarJson $id ([DateTime]::UtcNow.AddSeconds(600).ToString('o'))) + ']]' }
            'empty-array'            = { param($id) '[]' }
        }
        $k = 0
        foreach ($n in $shapes.Keys) {
            $k++
            # The gate: no request, typed fail closed.
            Reset-Case @{ request = 'active' }
            $script:LeaseNotAfterMinAdmitSec = 1
            $script:AgeAfter = (Get-Date).AddSeconds(15)
            $id = "jLs$k"
            $mL = New-NotAfterMeta 3600
            Write-SidecarRaw $id (& $shapes[$n] $id)
            $adm = Wait-QuietLeaseAdmission -JobId $id -Job ([pscustomobject]@{}) -Metadata $mL
            $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
            Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'pre-request' -and $adm.notAfterState -ceq 'unparseable' -and $adm.notAfterSource -ceq 'sidecar' -and $adm.livenessState -ceq 'unparseable' -and $reqs.Count -eq 0 -and $null -eq $script:QuietLease) "liveness-root-shape (${n}): fail closed at the gate, no request, no admission: label=$($adm.label) state=$($adm.notAfterState) source=$($adm.notAfterSource) requests=$($reqs.Count) why=$($adm.why)"
            Assert-True ($adm.why -like '*does not hold a JSON object*') "liveness-root-shape (${n}): why names the root shape: $($adm.why)"
            $want = "quiet-lease: expired-not-after job=$id phase=pre-request notAfterUtc=$($mL.notAfterUtc) requests=0 releaseRc=none$(Get-LivenessSuffix 'unparseable')"
            Assert-True (@($script:Log | Where-Object { $_ -ceq $want }).Count -eq 1) "liveness-root-shape (${n}): typed line [$want]: $($script:Log -join ' | ')"
            if ($null -ne $script:QuietLease) { Exit-HeavySlot }
            # Invoke-QueuedJob: refused pre-gate, NO child launch.
            $qid = "jLsq$k"
            $job = Reset-QueuedJobCase @{ request = 'active' } $qid (New-NotAfterMetaJson 3600)
            Write-SidecarRaw $qid (& $shapes[$n] $qid)
            $null = Invoke-QueuedJob -Job $job
            Assert-NotAfterExit "liveness-root-shape (${n}, queued job)" $qid 'pre-gate' $false ''
            $p = $script:QJ.published[0]
            Assert-True ($p.notAfterSource -ceq 'sidecar' -and $p.notAfterState -ceq 'unparseable' -and $p.stderr -ceq 'runner liveness sidecar could not be read (fail closed) before launch (pre-gate); job was not executed') "liveness-root-shape (${n}, queued job): typed: $($p | Select-Object notAfterSource, notAfterState, stderr | ConvertTo-Json -Compress)"
            $rs[$n] = $adm.why
        }
        $results.livenessRootShape = $rs
    }

    # ------------------------------------------------------------ liveness-lapse-latched: a lapse the renew tick saw is not revived
    # Dot on #663 (improvement): the waiting lease's renew tick, run from inside a light job, sees the sidecar lapse and stops
    # renewing ("released at the wait's next loop top"). A fresher sidecar written after that must not resume the renewals, and
    # the wait's next loop top must still release the lease, typed with the lapsed sidecar, never admit it.
    if (Test-Case 'liveness-lapse-latched') {
        Reset-Case @{ request = 'active'; active_after = 2 }
        $script:LeaseNotAfterMinAdmitSec = 1
        $script:AgeAfter = (Get-Date).AddSeconds(40)
        $mL = New-NotAfterMeta 3600
        $script:AliveAt = [DateTime]::UtcNow.AddSeconds(5)
        $until = Write-Sidecar 'jLl' $script:AliveAt
        $script:Latch = @{ ran = 0; activeBefore = $false; renewsBefore = 0; stopSeen = $false; fresh = $null; ticksAfterRevive = 0; renewsAfterRevive = 0 }
        function Invoke-ExemptJobsDuringWait {
            param([string]$WaitingJobId, [double]$WaitedSec)
            $script:Latch.ran++
            $w = $script:QuietLeaseWaiting
            if ($null -eq $w -or $script:Latch.ran -gt 1) { return 0 }
            # The light job's monitor loop: one renew tick per slice. Once the tick has typed the stop, the sidecar is renewed
            # (seq 2, 600 s ahead), and the loop ticks on for 30 more slices, over three renew intervals (LeaseRenewSec 1).
            $dl = $script:AliveAt.AddSeconds(15)
            while ([DateTime]::UtcNow -lt $dl -and $script:Latch.ticksAfterRevive -lt 30) {
                $before = $w.renewStarts
                Invoke-QuietLeaseRenewTick
                $d = $w.renewStarts - $before
                if ($null -ne $script:Latch.fresh) { $script:Latch.renewsAfterRevive += $d; $script:Latch.ticksAfterRevive++ }
                elseif ($w.notAfterStopLogged -eq $true) {
                    $script:Latch.stopSeen = $true
                    $script:Latch.fresh = Write-Sidecar 'jLl' ([DateTime]::UtcNow.AddSeconds(600)) 2
                }
                else { $script:Latch.renewsBefore += $d; if ($null -ne $w.active) { $script:Latch.activeBefore = $true } }
                Start-Sleep -Milliseconds 100
            }
            return 1
        }
        try { $adm = Wait-QuietLeaseAdmission -JobId 'jLl' -Job ([pscustomobject]@{}) -Metadata $mL }
        finally { function Invoke-ExemptJobsDuringWait { param([string]$WaitingJobId, [double]$WaitedSec) $script:Exempt++; return 0 } }
        $lt2 = $script:Latch
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($lt2.ran -ge 1 -and $lt2.stopSeen -and $null -ne $lt2.fresh -and $lt2.ticksAfterRevive -ge 30) "liveness-lapse-latched: the race landed: the tick typed the lapse inside the light job, then a fresher sidecar was written and ticked over: $($lt2 | ConvertTo-Json -Compress)"
        Assert-True ($lt2.renewsAfterRevive -eq 0) "liveness-lapse-latched: no renew started after the lapse, the fresher sidecar notwithstanding: $($lt2 | ConvertTo-Json -Compress)"
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -ceq 'post-active' -and $adm.notAfterSource -ceq 'sidecar' -and $adm.notAfterState -ceq 'passed' -and $adm.livenessUntilUtc -ceq $until) "liveness-lapse-latched: the wait releases it, typed with the LAPSED sidecar ($until), never admits it: label=$($adm.label) phase=$($adm.phase) source=$($adm.notAfterSource) until=$($adm.livenessUntilUtc)"
        Assert-True ($adm.why -like '*latched*' -and $null -ne $adm.leaseRelease -and $adm.leaseRelease.phase -ceq 'post-active' -and [string]$adm.leaseRelease.releaseRc -ceq '0') "liveness-lapse-latched: why says latched, released post-active: why=$($adm.why) release=$($adm.leaseRelease | ConvertTo-Json -Compress)"
        Assert-True ($reqs.Count -eq 1 -and (Get-Released).Count -eq 1 -and (Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "liveness-lapse-latched: one request, released once, wrapper gone, no lease kept"
        Assert-True (@($script:Log | Where-Object { $_ -like "quiet-lease: not-after passed job=jLl notAfterUtc=$($mL.notAfterUtc) active=* source=sidecar livenessUntilUtc=$until while waiting;*" }).Count -eq 1) "liveness-lapse-latched: the stop is typed once"
        $want = "quiet-lease: expired-not-after job=jLl phase=post-active notAfterUtc=$($mL.notAfterUtc) requests=1 releaseRc=0$(Get-LivenessSuffix $until)"
        Assert-True (@($script:Log | Where-Object { $_ -ceq $want }).Count -eq 1) "liveness-lapse-latched: typed line [$want]: $($script:Log -join ' | ')"
        $results.livenessLapseLatched = [ordered]@{ activeBefore = $lt2.activeBefore; renewsBefore = $lt2.renewsBefore; ticksAfterRevive = $lt2.ticksAfterRevive; renewsAfterRevive = $lt2.renewsAfterRevive; phase = $adm.phase }
    }

    # ------------------------------------------------------------ batch-open-notafter: the batch's OPEN never holds a lease past the batch deadline
    # lease-batch r4 (sol r3 B1 on #633): the VM's lease-batch runner gives its open job the batch deadline as notAfterUtc. An agent that never answers the
    # VM's cancel still asks for no lease for the open past that bound, and a lease that turns active after it is released before the batch opens on it.
    # RED (an agent without not-after): the open is admitted clear and its batch opens on a lease taken after the deadline.
    if (Test-Case 'batch-open-notafter') {
        Reset-Case @{ request = 'active'; active_after = 3 }
        $script:LeaseNotAfterMinAdmitSec = 1
        $savedPendingMax = $LeasePendingMaxSec
        $LeasePendingMaxSec = 1800
        $script:AgeAfter = (Get-Date).AddSeconds(25)
        $tokO = 'ffffffffff01'
        $script:HookJob = "lease-batch-open-20261024T0430Z-lb$tokO"
        $mOpen = New-NotAfterMeta 2
        $script:NotAfterAt = [DateTimeOffset]::Parse($mOpen.notAfterUtc).UtcDateTime
        $script:LogHook = {
            param($Message)
            if ($script:HookFired -or $Message -notlike "quiet-lease: request started job=$($script:HookJob) *") { return }
            $script:HookFired = $true
            $null = Wait-Until { $f = Get-FakeFlag; $null -ne $f -and $f.state -eq 'active' -and [DateTime]::UtcNow -gt $script:NotAfterAt } 10
            Start-Sleep -Milliseconds 500
        }
        try { $adm = Wait-QuietLeaseAdmission -JobId $script:HookJob -Job ([pscustomobject]@{}) -Metadata $mOpen }
        finally { $LeasePendingMaxSec = $savedPendingMax }
        $reqs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        $at = if ($reqs.Count -ge 1) { Get-RequestArg $reqs[0] '--admit-timeout' } else { $null }
        Assert-True ($script:HookFired -and $reqs.Count -eq 1 -and [int]$at -ge 1 -and [int]$at -le 2) "batch-open-notafter: one request for the open, its --admit-timeout bounded by the batch deadline (1..2, not 1800): requests=$($reqs.Count) admit-timeout=$at"
        Assert-True ($adm.label -eq 'expired-not-after' -and $adm.phase -eq 'post-active' -and $adm.notAfterState -eq 'passed') "batch-open-notafter: a lease that turns active after the batch deadline is not handed to the open: label=$($adm.label) phase=$($adm.phase)"
        Assert-True ($null -eq $script:LeaseBatch -and $null -eq $adm.leaseBatch -and -not (Test-Path -LiteralPath (Join-Path $fx "lease-batch-$tokO.jsonl"))) "batch-open-notafter: no batch opens on it (no parked lease, no leaseBatch, no receipt)"
        Assert-True ((Get-Released).Count -eq 1 -and [string]$adm.leaseRelease.releaseRc -ceq '0' -and (Wait-Until { -not (Test-PidAlive ([int]$reqs[0].pid)) } 5) -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0 -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting) "batch-open-notafter: released once, wrapper gone, record removed, no lease kept: $($adm.leaseRelease | ConvertTo-Json -Compress)"
        $results.batchOpenNotAfter = [ordered]@{ admitTimeout = [int]$at; label = $adm.label; phase = $adm.phase; releaseRc = $adm.leaseRelease.releaseRc }
    }

    # ------------------------------------------------------------ batch-refusal-ends-batch: a post-admission refusal of a batch's open ends its batch
    # lease-batch r4 (the merge of not-after r1): Invoke-QueuedJob's post-admission cancel and not-after refusals release the admitted lease themselves (to
    # record leaseRelease), so Exit-HeavySlot no longer reaches Close-LeaseBatch for it. The open that has just opened its batch must end that batch with
    # its receipt (job-not-completed), never leave it parked on a released lease. The two refusal lines are run as the agent writes them in Invoke-QueuedJob.
    # RED (the merge without Close-LeaseBatchAfterRelease): the batch stays, on a released lease, with no receipt.
    if (Test-Case 'batch-refusal-ends-batch') {
        $qjR = Get-FunctionBody 'Invoke-QueuedJob'
        # lease-batch r5 (the merge of #662): every such line, the queue-expired and pre-launch holder-lost exits included; $When and $pre are the
        # variables two of them read in Invoke-QueuedJob.
        $refusalLines = @(($qjR -split "`r?`n") | Where-Object { $_ -match '^\s*if \(\$null -ne \$script:QuietLease\) \{ \$rel = Stop-QuietLease -Lease \$script:QuietLease -Why ' } | ForEach-Object { $_.Trim() })
        Assert-True ($refusalLines.Count -eq 4) "batch-refusal-ends-batch: Invoke-QueuedJob has the post-admission cancel, not-after, queue-expired and pre-launch release lines (found $($refusalLines.Count))"
        # lease-batch r5: the cancel line's why is a variable now (a cancel file, or the batch's queued close job).
        $cancelWhy = 'cancel file present after admission, before launch'
        $When = 'after admission, before launch'
        $pre = [pscustomobject]@{ reason = 'holder-lost'; why = 'unit' }
        $refused = [ordered]@{}
        $i = 0
        foreach ($line in $refusalLines) {
            $i++
            Reset-Case @{ request = 'active' }
            $script:AgeAfter = (Get-Date).AddSeconds(25)
            $tokR = "ffffffffff1$i"
            $script:Admission = Wait-QuietLeaseAdmission -JobId "lease-batch-open-20261024T0431Z-lb$tokR" -Job ([pscustomobject]@{}) -Metadata $meta
            Assert-True ($script:Admission.label -eq 'clear' -and $null -ne $script:LeaseBatch -and [object]::ReferenceEquals($script:LeaseBatch.lease, $script:QuietLease)) "batch-refusal-ends-batch: the open is admitted and opens its batch ($($script:Admission.label))"
            $wpR = $script:QuietLease.pid
            $rel = $null
            . ([scriptblock]::Create($line))
            Exit-HeavySlot
            $rcptR = @(Get-Content -LiteralPath (Join-Path $fx "lease-batch-$tokR.jsonl") -ErrorAction SilentlyContinue | ForEach-Object { $_ | ConvertFrom-Json })
            Assert-True ($null -eq $script:LeaseBatch -and $null -eq $script:QuietLease) "batch-refusal-ends-batch: [$line] ends the batch: no batch is left parked on the released lease"
            Assert-True ($rcptR.Count -eq 1 -and $rcptR[0].endReason -ceq 'job-not-completed' -and $rcptR[0].releaseRc -ceq '0' -and $null -ne $rel -and [string]$rel.releaseRc -ceq '0') "batch-refusal-ends-batch: one typed receipt with the recorded release: $($rcptR | ConvertTo-Json -Compress -Depth 4)"
            Assert-True ((Get-Released).Count -eq 1 -and (Wait-Until { -not (Test-PidAlive $wpR) } 8)) "batch-refusal-ends-batch: released once, wrapper gone"
            $refused["refusal$i"] = $rcptR[0].endReason
        }
        $results.batchRefusalEndsBatch = $refused
    }

    # ------------------------------------------------------------ batch-close-before-reuse: a queued CLOSE wins over a leg file that sorts ahead of it
    # lease-batch r5 (dot 242171b3 B2): the batch's lease is parked; the VM queues the batch's close job, and a leg of that batch sits in inbox\ AHEAD of it
    # (dve-... < lease-batch-close-...), so the inbox loop takes the leg first. Invoke-QueuedJob is RUN (disk and launch steps stubbed, as above).
    # RED (reuse before the close check): the leg is admitted on the parked lease and reaches the child launch.
    if (Test-Case 'batch-close-before-reuse') {
        $tokC = 'ffffffffff21'
        $openC = "lease-batch-open-20261024T1930Z-lb$tokC"
        $legC = "dve-fx-a-bachelor-cpu-20261024000001-lb$tokC"
        $closeC = "lease-batch-close-20261024T1930Z-lb$tokC"
        $job = Reset-QueuedJobCase @{ request = 'active' } $legC (New-NotAfterMetaJson 600)
        # The open: admitted on a fresh lease, ran, ended; the inbox loop parks the lease (Complete-LeaseBatchJob, then Exit-HeavySlot).
        $script:Admission = Wait-QuietLeaseAdmission -JobId $openC -Job ([pscustomobject]@{}) -Metadata $meta
        Assert-True ($script:Admission.label -eq 'clear' -and $null -ne $script:LeaseBatch -and $script:LeaseBatch.token -ceq $tokC) "batch-close-before-reuse: the open is admitted and opens its batch ($($script:Admission.label))"
        Complete-LeaseBatchJob -JobId $openC -ExitCode 0 -StartedUtc ([DateTime]::UtcNow.ToString('o'))
        Exit-HeavySlot
        $script:Admission = $null
        Assert-True ($null -ne $script:LeaseBatch -and $null -eq $script:QuietLease -and (Test-PidAlive $script:LeaseBatch.lease.pid)) "batch-close-before-reuse: the lease is parked, its wrapper alive"
        $wpC = $script:LeaseBatch.lease.pid
        [IO.File]::WriteAllText((Join-Path $inbox "$legC.job.ps1"), '# leg')
        [IO.File]::WriteAllText((Join-Path $inbox "$closeC.job.ps1"), '# close')
        $order = @(Get-ChildItem -LiteralPath $inbox -Filter '*.job.ps1' -File | Sort-Object Name | ForEach-Object Name)
        Assert-True ($order.Count -eq 2 -and $order[0] -ceq "$legC.job.ps1" -and $order[1] -ceq "$closeC.job.ps1") "batch-close-before-reuse: the leg sorts ahead of the queued close: $($order -join ', ')"
        $null = Invoke-QueuedJob -Job $job
        $pC = @($script:QJ.published)
        Assert-True ($script:QJ.launches.Count -eq 0 -and $script:QJ.launchingMarkers.Count -eq 0) "batch-close-before-reuse: NO leg launches (no launching marker, no child): launches=[$($script:QJ.launches -join ' | ')] markers=$($script:QJ.launchingMarkers.Count)"
        Assert-True ($pC.Count -eq 1 -and $pC[0].jobId -ceq $legC -and $pC[0].admission.label -ceq 'cancelled-in-gate' -and $pC[0].admission.batchClosed -eq $true -and $null -eq $pC[0].startedUtc) "batch-close-before-reuse: the leg's result is cancelled-in-gate, batchClosed, never started: $($pC | ConvertTo-Json -Compress -Depth 4)"
        $reqsC = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
        Assert-True ($reqsC.Count -eq 1 -and (Get-Released).Count -eq 1 -and (Wait-Until { -not (Test-PidAlive $wpC) } 8)) "batch-close-before-reuse: one request (the open's), the parked lease released once, its wrapper gone: requests=$($reqsC.Count) releases=$((Get-Released).Count)"
        $rcptC = @(Get-Content -LiteralPath (Join-Path $fx "lease-batch-$tokC.jsonl") -ErrorAction SilentlyContinue | ForEach-Object { $_ | ConvertFrom-Json })
        Assert-True ($null -eq $script:LeaseBatch -and $null -eq $script:QuietLease -and $rcptC.Count -eq 1 -and $rcptC[0].endReason -ceq 'close-queued' -and $rcptC[0].jobsRun -eq 1) "batch-close-before-reuse: the batch ends close-queued with its receipt (the open ran, no leg): $($rcptC | ConvertTo-Json -Compress -Depth 4)"
        Assert-True (Test-Path -LiteralPath (Join-Path $inbox "$closeC.job.ps1") -PathType Leaf) "batch-close-before-reuse: the close job is left for the inbox loop"
        $results.batchCloseBeforeReuse = [ordered]@{ order = $order; label = $pC[0].admission.label; batchClosed = $pC[0].admission.batchClosed; endReason = $rcptC[0].endReason; launches = $script:QJ.launches.Count }
    }

    # ------------------------------------------------------------ batch-runner-killed-liveness: the VM runner dies while its open waits in the gate
    # lease-batch r5 (sol B1 on an early runner exit; #644 + #663 + this): the open's runner-liveness sidecar is written and renewed by the REAL VM writer
    # (VenueChain.psm1 Start-VenueJobLiveness) in its own process; the open's request is pending (heavy-slot.py has not admitted it). The writer is killed: it
    # stops renewing, and the agent's wait ends the open expired-not-after (source=sidecar) and releases the pending request once the last aliveUntilUtc passes,
    # within the window plus one gate loop, never at the open's far notAfterUtc. RED (a runner without the writer): no sidecar is ever written.
    if (Test-Case 'batch-runner-killed-liveness') {
        Reset-Case @{ request = 'pending' }
        $script:LeaseNotAfterMinAdmitSec = 1
        $script:AgeAfter = (Get-Date).AddSeconds(60)
        $tokK = 'ffffffffff31'
        $openK = "lease-batch-open-20261024T1931Z-lb$tokK"
        $sideK = Join-Path $notAfterDir "$openK.json"
        $modK = if ($VenueModule) { $VenueModule } else { Join-Path $RepoRoot 'tools\profiling\dual-venue\VenueChain.psm1' }
        $winK = 5
        $psi = [Diagnostics.ProcessStartInfo]::new([Environment]::ProcessPath)
        foreach ($a in @('-NoProfile', '-NonInteractive', '-Command', "Import-Module '$modK' -DisableNameChecking; `$l = Start-VenueJobLiveness -AgentShare '$fx' -JobId '$openK' -WindowSec $winK -RenewSec 1; Start-Sleep -Seconds 300")) { [void]$psi.ArgumentList.Add($a) }
        $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardOutput = $true
        $writer = [Diagnostics.Process]::Start($psi)
        $writerErr = $writer.StandardError.ReadToEndAsync(); $null = $writer.StandardOutput.ReadToEndAsync()
        try {
            function Read-SideK { try { return ([IO.File]::ReadAllText($sideK) | ConvertFrom-Json) } catch { return $null } }
            $first = Wait-Until { $null -ne (Read-SideK) } 20
            Assert-True $first "batch-runner-killed-liveness: the runner's writer puts the open's sidecar at notafter\$openK.json (writer alive=$(-not $writer.HasExited) stderr=[$(if ($writer.HasExited) { $writerErr.Result })])"
            $s0 = Read-SideK
            Assert-True ($s0.schema -ceq 'mlv-app/job-notafter-sidecar/v1' -and $s0.jobId -ceq $openK -and $s0.writerPid -eq $writer.Id) "batch-runner-killed-liveness: the sidecar is the runner's, in the agent's schema: $($s0 | ConvertTo-Json -Compress)"
            $mK = New-NotAfterMeta 3600
            $script:KillK = $null
            $script:LogHook = {
                param($Message)
                if ($null -ne $script:KillK -or $Message -notlike "quiet-lease: request started job=$openK *") { return }
                # the open is pending in heavy-slot.py while the runner renews its sidecar; then the runner dies
                $null = Wait-Until { $f = Get-FakeFlag; $s = Read-SideK; $null -ne $f -and $f.state -eq 'pending' -and $null -ne $s -and [int]$s.seq -ge 3 } 15
                $sk = Read-SideK
                $fk = Get-FakeFlag
                Stop-Process -Id $writer.Id -Force
                $null = $writer.WaitForExit(10000)
                $script:KillK = [pscustomobject]@{ at = [DateTime]::UtcNow; seq = [int]$sk.seq; waitingLive = ($null -ne $fk -and $fk.state -eq 'pending') }
            }
            $admK = Wait-QuietLeaseAdmission -JobId $openK -Job ([pscustomobject]@{}) -Metadata $mK
            $retK = [DateTime]::UtcNow
            $script:LogHook = $null
            $sEnd = Read-SideK
            # pwsh 7's ConvertFrom-Json reads an ISO stamp as a [DateTime]; a string (Windows PowerShell) is parsed.
            $lastUntil = if ($sEnd.aliveUntilUtc -is [DateTime]) { if ($sEnd.aliveUntilUtc.Kind -eq [DateTimeKind]::Unspecified) { [DateTime]::SpecifyKind($sEnd.aliveUntilUtc, [DateTimeKind]::Utc) } else { $sEnd.aliveUntilUtc.ToUniversalTime() } } else { [DateTimeOffset]::Parse([string]$sEnd.aliveUntilUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime }
            Assert-True ($null -ne $script:KillK -and $script:KillK.seq -ge 3 -and $script:KillK.waitingLive -and $writer.HasExited) "batch-runner-killed-liveness: the runner renewed the sidecar (seq $($script:KillK.seq)) while the open was pending, then was killed"
            Assert-True ([int]$sEnd.seq -le $script:KillK.seq + 1 -and $lastUntil -le $script:KillK.at.AddSeconds($winK + 1)) "batch-runner-killed-liveness: no renewal after the kill: seq $($sEnd.seq) (kill saw $($script:KillK.seq)), last aliveUntilUtc $($sEnd.aliveUntilUtc) <= kill + ${winK}s"
            Assert-True ($admK.label -eq 'expired-not-after' -and $admK.phase -ceq 'pending' -and $admK.notAfterSource -ceq 'sidecar' -and $admK.livenessState -ceq 'lapsed') "batch-runner-killed-liveness: the open ends expired-not-after from the lapsed sidecar, still pending: label=$($admK.label) phase=$($admK.phase) source=$($admK.notAfterSource) liveness=$($admK.livenessState)"
            $lagK = ($retK - $lastUntil).TotalSeconds
            Assert-True ($retK -ge $lastUntil -and $lagK -le 5) "batch-runner-killed-liveness: it ends within the #663 bound: $([Math]::Round($lagK, 2)) s after the last aliveUntilUtc (kill + $([Math]::Round(($retK - $script:KillK.at).TotalSeconds, 2)) s)"
            $reqsK = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
            Assert-True ($reqsK.Count -eq 1 -and (Get-Released).Count -eq 1 -and [string]$admK.leaseRelease.releaseRc -ceq '0' -and (Wait-Until { -not (Test-PidAlive ([int]$reqsK[0].pid)) } 5) -and $null -eq $script:QuietLease -and $null -eq $script:QuietLeaseWaiting -and $null -eq $script:LeaseBatch -and @(Get-ChildItem -LiteralPath $fx -Filter '*.lease.json' -File).Count -eq 0) "batch-runner-killed-liveness: the pending request released once, its wrapper gone, no lease or batch kept: $($admK.leaseRelease | ConvertTo-Json -Compress)"
            $results.batchRunnerKilledLiveness = [ordered]@{ windowSec = $winK; seqAtKill = $script:KillK.seq; lastSeq = [int]$sEnd.seq; secAfterLastAliveUntil = [Math]::Round($lagK, 2); secAfterKill = [Math]::Round(($retK - $script:KillK.at).TotalSeconds, 2); label = $admK.label; phase = $admK.phase; source = $admK.notAfterSource }
        }
        finally {
            $script:LogHook = $null
            if (-not $writer.HasExited) { Stop-Process -Id $writer.Id -Force -ErrorAction SilentlyContinue; $null = $writer.WaitForExit(10000) }
            Remove-Item -LiteralPath $sideK -Force -ErrorAction SilentlyContinue
        }
    }
    # AGENT-LEASE-PENDING-HEAT-TRACE-1 r3: the heat-trace cases' status answer (fake-status.json, read by the fake's `status`) and the poll of the
    # snapshot probes the job monitor loop would make after the grant. Definitions only: each case sets its own answer.
    function Sync-FakeStatus {
        $st = $script:StatusStub
        $o = [ordered]@{ lines = @($st.lines); rc = $(if ($st.ok) { 0 } else { [int]$st.rc }); stderr = $(if ($st.ok) { '' } else { [string]$st.error }); delay = [double]$script:StatusDelaySec; orphan = [bool]$script:StatusOrphan }
        [IO.File]::WriteAllText((Join-Path $fx 'fake-status.json'), ($o | ConvertTo-Json -Compress -Depth 4))
    }
    function Complete-HtSnapshots {
        # What the job monitor loop does after the grant: poll the snapshot probes until none is in flight (bounded). Then the probes the fake saw.
        if (Get-Command Update-LeaseSlotSnapshots -ErrorAction SilentlyContinue) { $dl = (Get-Date).AddSeconds(20); while ($script:LeaseSlotSnapshots.Count -gt 0 -and (Get-Date) -lt $dl) { Update-LeaseSlotSnapshots; Start-Sleep -Milliseconds 50 } }
        $script:StatusCalls = @(Get-Calls | Where-Object { $_.cmd -eq 'status' }).Count
    }
    function Invoke-HtWait([string]$JobId, $Metadata) {
        $script:StatusCalls = 0
        Sync-FakeStatus
        $a = Wait-QuietLeaseAdmission -JobId $JobId -Job ([pscustomobject]@{}) -Metadata $Metadata
        Complete-HtSnapshots
        return $a
    }
    # ------------------------------------------------------------ lease-pending-heat-trace (AGENT-LEASE-PENDING-HEAT-TRACE-1 r1)
    # Logging only. Three long quiet-lease waits on Bachelor (541 s, 672 s, 832 s) could not be attributed afterwards: the waiting
    # line carried neither the heat reading nor who held the heavy slots. Every waiting line now carries the gate's heat fields
    # (throttled/window, window mean C, feed age, or heat=unknown(<reason>)), and ONE bounded slot snapshot is logged at request
    # and one at grant. Nothing it logs may throw, delay the grant past the status probe's own bound, or change a return value.
    if (Test-Case 'lease-pending-heat-trace') {
        $GateSustainedRows = [int]$paramDefaults['GateSustainedRows']; $GateMaxThrottledRows = [int]$paramDefaults['GateMaxThrottledRows']; $GateMeanMaxC = [double]$paramDefaults['GateMeanMaxC']
        $script:QuietLeaseWaitLogSec = 1      # the agent logs a waiting line every 60 s; 1 s here so a short wait carries several
        $script:FeedReads = 0; $script:StatusCalls = 0; $script:FeedStub = $null; $script:StatusStub = $null; $script:StatusThrows = $false; $script:FeedThrows = $false
        $hotMetrics = [pscustomobject]@{ rows = 150; window = 60; throttled = 6; meanC = 88.2; max15C = 97; watts30 = 41.5 }
        function Read-HeavySlotFeedMetrics { $script:FeedReads++; if ($script:FeedThrows -and $null -eq $script:QuietLease) { throw 'feed boom' }; return $script:FeedStub }   # throws only while waiting: the real reader never throws, and the grant's own read is not under test
        $script:StatusDelaySec = 0
        # r3: the probe is the REAL one -- the agent's own (asynchronous) status call, or a base agent's Invoke-HeavySlotStatus, lifted -- answered by the
        # fake heavy-slot.py's `status` from fake-status.json: $script:StatusStub (ok/lines, or rc + error as its stderr) after $script:StatusDelaySec.
        . ([scriptblock]::Create((Get-FunctionBody 'Invoke-HeavySlotStatus')))
        $script:StartCliReal = ${function:Start-HeavySlotCli}
        function Start-HeavySlotCli { param([string[]]$Arguments, [string]$Python = '', [string]$Script = '') if ($script:StatusThrows -and $Arguments[0] -ceq 'status') { throw 'status boom' }; return (& $script:StartCliReal @PSBoundParameters) }   # r5: the checked -Python/-Script pass through
        $since = (Get-Date).AddSeconds(-300).ToString('yyyy-MM-dd HH:mm:ss')
        $slotsOk = @{ ok = $true; error = $null; lines = @("slot 1: suite X (pid 77, since $since) [exclusive]", 'slot 2: free', 'thermal: max 99 C, throttled 6/15, 40 W over ~30 s', "queue 1: mlv j9 (pid 78, since $since)") }
        function Invoke-HeatTraceCase([string]$JobId, [hashtable]$Control) {
            Reset-Case $Control
            $script:AgeAfter = (Get-Date).AddSeconds(60)
            $script:FeedReads = 0; $script:StatusCalls = 0
            $adm = Invoke-HtWait $JobId $meta
            $script:Admission = $adm
            return [pscustomobject]@{
                adm = $adm; waits = @($script:Log | Where-Object { $_ -like "quiet-lease: waiting job=$JobId *" })
                snaps = @($script:Log | Where-Object { $_ -like "quiet-lease: slots-snapshot job=$JobId *" })
                active = @($script:Log | Where-Object { $_ -like "quiet-lease: active job=$JobId *" })
            }
        }
        try {
            # (a) a feed AT the pass line (throttled 6/60, mean 88.2 C): every waiting line carries the numbers the gate uses.
            $script:FeedStub = @{ metrics = $hotMetrics; why = $null; ageSec = 12 }
            $script:StatusStub = $slotsOk
            $r = Invoke-HeatTraceCase 'jHt' @{ request = 'active'; active_after = 3.5 }
            Assert-True ($r.adm.label -eq 'clear' -and $r.adm.mode -eq 'lease' -and $null -ne $script:QuietLease -and $r.active.Count -eq 1) "heat-trace: the job is still admitted clear in lease mode: $($r.adm.label)"
            Assert-True ($r.waits.Count -ge 1) "heat-trace: the wait logged a waiting line: $($r.waits.Count)"
            $bad = @($r.waits | Where-Object { $_ -notmatch ' heat=hot throttled=6/60 meanC=88\.2 feedAge=12s( |$)' })
            Assert-True ($bad.Count -eq 0) "heat-trace: EVERY waiting line carries heat=<state> throttled=<n>/<window> meanC=<C> feedAge=<s>; without it: $($bad -join ' || ')"
            Assert-True ($r.waits.Count -ge 2) "heat-trace: a 3.5 s wait at a 1 s cadence logs at least two waiting lines: $($r.waits.Count)"
            Assert-True ($r.waits[0] -match '^quiet-lease: waiting job=jHt state=lease-pending why=\[.*\] waited=\d+s requests=1 requeues=0 leasePid=\d+') "heat-trace: the existing waiting-line fields are unchanged: $($r.waits[0])"
            Assert-True ($script:FeedReads -eq $r.waits.Count + 1) "heat-trace: ONE feed read per waiting line, plus the grant's own (no extra read): reads=$($script:FeedReads) lines=$($r.waits.Count)"
            Exit-HeavySlot
            # the snapshots: exactly one at request, one at grant, each naming the holder slot, its owner and its age.
            Assert-True ($script:StatusCalls -eq 2) "heat-trace: exactly two status probes (request, grant): $($script:StatusCalls)"
            $sReq = @($r.snaps | Where-Object { $_ -match ' phase=request ' }); $sGrant = @($r.snaps | Where-Object { $_ -match ' phase=grant ' })
            Assert-True ($r.snaps.Count -eq 2 -and $sReq.Count -eq 1 -and $sGrant.Count -eq 1) "heat-trace: exactly one slots-snapshot line at request and one at grant: $($r.snaps -join ' || ')"
            foreach ($s in @($sReq[0], $sGrant[0])) {
                Assert-True ($s -match 'slot 1: suite X pid=77 age=(29[5-9]|3[0-2]\d)s' -and $s -match 'slot 2: free') "heat-trace: the snapshot names each slot, the holder, its pid and its age (~300 s): $s"
            }
            # r5 (adversary r4-e98a2780 A3): order-independent. The request probe's LOG line lands whenever the loop next polls it, which on a loaded
            # host can be after the activation; what is asserted is that its probe RAN while the request was pending (the fake's status call is
            # stamped before the request's 3.5 s activation), and that the grant snapshot follows the grant line.
            $iReq = $script:Log.IndexOf($sReq[0]); $iAct = $script:Log.IndexOf($r.active[0]); $iGrant = $script:Log.IndexOf($sGrant[0])
            $stTs = @(Get-Calls | Where-Object { $_.cmd -eq 'status' } | ForEach-Object { [double]$_.ts } | Sort-Object)
            $rqTs = @(Get-Calls | Where-Object { $_.cmd -eq 'request' } | ForEach-Object { [double]$_.ts })
            Assert-True ($iReq -ge 0 -and $iAct -ge 0 -and $iAct -lt $iGrant -and $stTs.Count -eq 2 -and $rqTs.Count -eq 1 -and $stTs[0] -lt $rqTs[0] + 3.5) "heat-trace: the request probe ran while the request was pending (status at +$(if ($stTs.Count -ge 1 -and $rqTs.Count -ge 1) { [Math]::Round($stTs[0] - $rqTs[0], 2) }) s of a 3.5 s activation) and the grant snapshot follows the grant line"

            # (b) feed unknown: stale, then a read that THROWS -- an explicit heat=unknown(<reason>), never a silent omission, never a throw.
            $script:FeedStub = @{ metrics = $null; why = 'feed stale (400 s > 120 s)'; ageSec = 400 }
            $r2 = Invoke-HeatTraceCase 'jHu' @{ request = 'active'; active_after = 2.5 }
            Assert-True ($r2.adm.label -eq 'clear' -and $r2.waits.Count -ge 1) "heat-trace: a stale feed still admits and logs: $($r2.adm.label)"
            Assert-True (@($r2.waits | Where-Object { $_ -notmatch ' heat=unknown\(feed stale \(400 s > 120 s\)\) feedAge=400s( |$)' }).Count -eq 0) "heat-trace: a stale feed is heat=unknown(<reason>) feedAge=<s> on every waiting line: $($r2.waits -join ' || ')"
            Exit-HeavySlot
            $script:FeedStub = @{ metrics = $null; why = 'feed missing (x)' }
            $r3 = Invoke-HeatTraceCase 'jHm' @{ request = 'active'; active_after = 2.5 }
            Assert-True (@($r3.waits | Where-Object { $_ -notmatch ' heat=unknown\(feed missing \(x\)\) feedAge=unknown( |$)' }).Count -eq 0 -and $r3.waits.Count -ge 1) "heat-trace: a missing feed is heat=unknown(<reason>) feedAge=unknown: $($r3.waits -join ' || ')"
            Exit-HeavySlot
            $script:FeedThrows = $true
            $r4 = Invoke-HeatTraceCase 'jHx' @{ request = 'active'; active_after = 2.5 }
            $script:FeedThrows = $false
            Assert-True ($r4.adm.label -eq 'clear' -and $r4.waits.Count -ge 1 -and @($r4.waits | Where-Object { $_ -notmatch ' heat=unknown\(.*feed boom.*\)( |$)' }).Count -eq 0) "heat-trace: a feed read that throws is logged as heat=unknown(...) and the wait goes on: $($r4.adm.label) $($r4.waits -join ' || ')"
            Exit-HeavySlot

            # (c) the status probe times out / fails / throws: slots=unknown(...) on both snapshots, the job is still admitted and the lease is the same.
            $script:FeedStub = @{ metrics = $hotMetrics; why = $null; ageSec = 12 }
            # r3: the bound is the agent's LeaseSnapshotProbeSec (30 s), 1 s here; the fake answers after 4 s, so both probes are killed at the bound.
            $script:StatusStub = $slotsOk; $script:StatusDelaySec = 4; $script:LeaseSnapshotProbeSec = 1
            $r5 = Invoke-HeatTraceCase 'jHt5' @{ request = 'active'; active_after = 1.5 }
            $script:StatusDelaySec = 0; $script:LeaseSnapshotProbeSec = 30
            Assert-True ($r5.adm.label -eq 'clear' -and $r5.snaps.Count -eq 2 -and @($r5.snaps | Where-Object { $_ -notmatch ' slots=unknown\(timeout 1 s; killed at \d+\.\d s\)$' }).Count -eq 0) "heat-trace: a timed-out probe is slots=unknown(timeout <bound> s; killed at <age> s) at request and at grant, and the job is admitted: $($r5.adm.label) $($r5.snaps -join ' || ')"
            Assert-True ($script:StatusCalls -eq 2) "heat-trace: both timed-out probes really ran: $($script:StatusCalls)"
            Exit-HeavySlot
            $script:StatusStub = @{ ok = $false; rc = 3; error = 'status boom'; lines = @() }
            $r5b = Invoke-HeatTraceCase 'jHt5b' @{ request = 'active'; active_after = 1.5 }
            Assert-True ($r5b.adm.label -eq 'clear' -and $r5b.snaps.Count -eq 2 -and @($r5b.snaps | Where-Object { $_ -notmatch ' slots=unknown\(status exit 3: status boom\)$' }).Count -eq 0) "heat-trace: a probe that exits non-zero is slots=unknown(status exit <rc>: <stderr>): $($r5b.adm.label) $($r5b.snaps -join ' || ')"
            Exit-HeavySlot
            $script:StatusStub = $slotsOk
            $script:StatusThrows = $true
            $r6 = Invoke-HeatTraceCase 'jHt6' @{ request = 'active'; active_after = 1.5 }
            $script:StatusThrows = $false
            Assert-True ($r6.adm.label -eq 'clear' -and $r6.snaps.Count -eq 2 -and @($r6.snaps | Where-Object { $_ -notmatch ' slots=unknown\(.*status boom.*\)$' }).Count -eq 0) "heat-trace: a probe that throws is slots=unknown(...) and never stops the grant: $($r6.adm.label) $($r6.snaps -join ' || ')"
            Exit-HeavySlot

            # (d) a refused request that is asked again still snapshots ONCE at request (one per job, not one per ask).
            $script:StatusStub = $slotsOk
            $r7 = Invoke-HeatTraceCase 'jHt7' @{ request = 'refuse_then_active'; refuse_count = 1 }
            Assert-True ($r7.adm.label -eq 'clear' -and @(Get-Calls | Where-Object { $_.cmd -eq 'request' }).Count -eq 2) "heat-trace: the refused request was asked again and then admitted: $($r7.adm.label)"
            Assert-True (@($r7.snaps | Where-Object { $_ -match ' phase=request ' }).Count -eq 1 -and @($r7.snaps | Where-Object { $_ -match ' phase=grant ' }).Count -eq 1 -and $script:StatusCalls -eq 2) "heat-trace: one request snapshot and one grant snapshot across a refuse-then-active job: $($r7.snaps -join ' || ') calls=$($script:StatusCalls)"
            Exit-HeavySlot
            # (e) adversary r1 A1: heavy-slot.py may add fields after the timestamp (", held 5 min"): the age is still read.
            $since = (Get-Date).AddSeconds(-300).ToString('yyyy-MM-dd HH:mm:ss')   # r3: re-stamped, as the cases above now run real probes and take longer than the age window
            $script:StatusStub = @{ ok = $true; error = $null; lines = @("slot 1: suite X (pid 77, since $since, held 5 min) [exclusive]", 'slot 2: free') }
            $r8 = Invoke-HeatTraceCase 'jHt8' @{ request = 'active'; active_after = 1.5 }
            Assert-True (@($r8.snaps | Where-Object { $_ -notmatch 'slot 1: suite X pid=77 age=(29[5-9]|3[0-2]\d)s' }).Count -eq 0 -and $r8.snaps.Count -eq 2) "heat-trace A1: a status line with a field after 'since <ts>' still yields the age: $($r8.snaps -join ' || ')"
            Exit-HeavySlot
            $script:StatusStub = $slotsOk

            # (f) adversary r1 A2: a probe (up to 30 s) is never made when a deadline, the queue ceiling or the lease end is within 70 s -- it
            # would turn an admission into expired-not-after / queue-expired. The skip is logged, the admission is unchanged.
            $r9 = $null
            Reset-Case @{ request = 'active'; active_after = 1.0 }
            $script:AgeAfter = (Get-Date).AddSeconds(60); $script:StatusCalls = 0
            $adm9 = Invoke-HtWait 'jHt9' (New-NotAfterMeta 50)
            $script:Admission = $adm9
            $sk9 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHt9 *' })
            Assert-True ($adm9.label -eq 'clear' -and $script:StatusCalls -eq 0 -and $sk9.Count -eq 2 -and @($sk9 | Where-Object { $_ -notmatch ' slots=skipped\(' }).Count -eq 0) "heat-trace A2: 50 s before notAfterUtc, no probe is made (calls=$($script:StatusCalls)) and the admission stays clear: $($adm9.label) $($sk9 -join ' || ')"
            Exit-HeavySlot
            Reset-Case @{ request = 'active'; active_after = 1.0 }
            $script:AgeAfter = (Get-Date).AddSeconds(60); $script:StatusCalls = 0
            $adm10 = Invoke-HtWait 'jHt10' ([pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 60 })
            $script:Admission = $adm10
            $sk10 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHt10 phase=request *' })
            Assert-True ($adm10.label -eq 'clear' -and $sk10.Count -eq 1 -and $sk10[0] -match ' slots=skipped\(') "heat-trace A2: a queue ceiling within 70 s skips the request probe: $($adm10.label) $($sk10 -join ' || ')"
            Exit-HeavySlot
            $script:LeaseSnapshotSkipBelowSec = 7200       # the 31 min lease (1860 s) is then within the skip window at grant
            Reset-Case @{ request = 'active'; active_after = 1.0 }
            $script:AgeAfter = (Get-Date).AddSeconds(60); $script:StatusCalls = 0
            $adm11 = Invoke-HtWait 'jHt11' $meta
            $script:Admission = $adm11
            $script:LeaseSnapshotSkipBelowSec = 70
            $sk11 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHt11 phase=grant *' })
            Assert-True ($adm11.label -eq 'clear' -and $script:StatusCalls -eq 0 -and $sk11.Count -eq 1 -and $sk11[0] -match ' slots=skipped\(') "heat-trace A2: a lease end within the skip window skips the grant probe: calls=$($script:StatusCalls) $($sk11 -join ' || ')"
            Exit-HeavySlot
            $pureHead = Get-LeaseSnapshotHeadroomSec -Verdict ([pscustomobject]@{ state = 'none'; remainingSec = $null; livenessState = 'absent'; livenessUntilUtc = $null })
            Assert-True ([double]::IsPositiveInfinity($pureHead) -and (Get-LeaseSnapshotHeadroomSec -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 12.5 })) -eq 12.5 -and (Get-LeaseSnapshotHeadroomSec -Verdict ([pscustomobject]@{ state = 'none'; livenessState = 'live'; livenessUntilUtc = [DateTime]::UtcNow.AddSeconds(8).ToString('o') })) -lt 9 -and (Get-LeaseSnapshotHeadroomSec -Verdict ([pscustomobject]@{ state = 'none'; livenessState = 'live'; livenessUntilUtc = 'not a date' })) -eq 0) "heat-trace A2: headroom is the least of the open notAfter, the live sidecar bound and the lease; an unreadable bound is no headroom"

            # (g) adversary r1 A3: a feed stamped far in the future (age negative, huge) keeps its readable heat; and a status age that overflows Int32 is per-slot, not a whole-snapshot failure.
            $script:FeedStub = @{ metrics = $hotMetrics; why = $null; ageSec = -2309511600.0 }
            $r12 = Invoke-HeatTraceCase 'jHt12' @{ request = 'active'; active_after = 1.5 }
            Assert-True ($r12.waits.Count -ge 1 -and @($r12.waits | Where-Object { $_ -notmatch ' heat=hot throttled=6/60 meanC=88\.2 feedAge=unknown( |$)' }).Count -eq 0) "heat-trace A3: a far-future feed stamp keeps the readable heat and reports feedAge=unknown: $($r12.waits -join ' || ')"
            Exit-HeavySlot
            $script:FeedStub = @{ metrics = $hotMetrics; why = $null; ageSec = 12 }
            $script:StatusStub = @{ ok = $true; error = $null; lines = @('slot 1: suite X (pid 77, since 0100-01-02 00:00:00) [exclusive]', 'slot 2: free') }
            $r13 = Invoke-HeatTraceCase 'jHt13' @{ request = 'active'; active_after = 1.5 }
            Assert-True ($r13.snaps.Count -eq 2 -and @($r13.snaps | Where-Object { $_ -notmatch 'slot 1: suite X pid=77 age=\d{10,}s; slot 2: free' }).Count -eq 0) "heat-trace A3: a status age beyond Int32 is logged, not a whole-snapshot failure: $($r13.snaps -join ' || ')"
            Exit-HeavySlot
            $script:StatusStub = $slotsOk

            # (h) adversary r2 A1: the GRANT probe also respects the queue ceiling (a job 570 s into a 600 s ceiling would be queue-expired by the probe's 30 s).
            $script:HtAge = 570
            Reset-Case @{ request = 'active'; active_after = 1.0 }
            $script:AgeAfter = (Get-Date).AddSeconds(60); $script:StatusCalls = 0
            $adm14 = Invoke-HtWait 'jHt14' $meta
            $script:HtAge = 0
            $script:Admission = $adm14
            $sk14 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHt14 *' })
            Assert-True ($adm14.label -eq 'clear' -and $script:StatusCalls -eq 0 -and $sk14.Count -eq 2 -and @($sk14 | Where-Object { $_ -notmatch ' slots=skipped\(' }).Count -eq 0) "heat-trace A1(r2): 30 s of queue headroom skips BOTH probes (request and grant): calls=$($script:StatusCalls) $($sk14 -join ' || ')"
            Exit-HeavySlot

            # (i) adversary r2 A2: the age is counted across a DST change (TimeZoneInfo, not local DateTime subtraction); an ambiguous or non-existent local time is unknown.
            $cst = [TimeZoneInfo]::FindSystemTimeZoneById('Central Standard Time')
            $spring = Get-LeaseSlotAgeText -Since '2026-03-08 01:55:00' -NowUtc ([DateTime]::new(2026, 3, 8, 8, 5, 0, [DateTimeKind]::Utc)) -Zone $cst
            $fall = Get-LeaseSlotAgeText -Since '2026-11-01 01:45:00' -NowUtc ([DateTime]::new(2026, 11, 1, 7, 15, 0, [DateTimeKind]::Utc)) -Zone $cst
            $gap = Get-LeaseSlotAgeText -Since '2026-03-08 02:30:00' -NowUtc ([DateTime]::new(2026, 3, 8, 9, 0, 0, [DateTimeKind]::Utc)) -Zone $cst
            $plain = Get-LeaseSlotAgeText -Since '2026-10-25 10:00:00' -NowUtc ([DateTime]::new(2026, 10, 25, 16, 5, 0, [DateTimeKind]::Utc)) -Zone $cst
            $future = Get-LeaseSlotAgeText -Since '2026-10-25 12:00:00' -NowUtc ([DateTime]::new(2026, 10, 25, 16, 5, 0, [DateTimeKind]::Utc)) -Zone $cst
            Assert-True ($spring -ceq '600s' -and $fall -ceq 'unknown' -and $gap -ceq 'unknown' -and $plain -ceq '3900s' -and $future -ceq 'unknown' -and (Get-LeaseSlotAgeText -Since 'x') -ceq 'unknown') "heat-trace A2(r2): spring-forward 01:55 CST -> 03:05 CDT is 600 s (not 4200), the ambiguous/non-existent hours are unknown, a plain age is exact: spring=$spring fall=$fall gap=$gap plain=$plain future=$future"
            # (j) adversary r3 A2: an owner label may contain '(pid N)' (job id 'suite (pid 12)' -> label 'mlv suite (pid 12)'): the metadata group is the LAST one before [mode].
            $script:StatusStub = @{ ok = $true; error = $null; lines = @("slot 1: mlv suite (pid 12) (pid 77, since $since, held 5 min) [exclusive]", 'slot 2: free') }
            $r15 = Invoke-HeatTraceCase 'jHt15' @{ request = 'active'; active_after = 1.5 }
            Assert-True ($r15.snaps.Count -eq 2 -and @($r15.snaps | Where-Object { $_ -notmatch 'slot 1: mlv suite \(pid 12\) pid=77 age=(29\d|3\d\d)s; slot 2: free' }).Count -eq 0) "heat-trace A2(r3): a label containing '(pid 12)' is named whole with the real owner pid 77 and its age: $($r15.snaps -join ' || ')"
            Exit-HeavySlot
            $script:StatusStub = $slotsOk

            # (k) adversary r3 A1: the request-time probe must not consume admission time. A refusal that lands while a slow probe runs is asked again
            # LeaseRetrySec after the refusal (as without the probe), not after the probe: otherwise the job crosses its queue ceiling and is queue-expired.
            # Scaled: retry 5 s, ceiling 9 s, probe 5 s; the fake refuses the first request at once. Without the probe the second request is made at ~5 s.
            $savedRetry = $LeaseRetrySec; $LeaseRetrySec = 5
            $script:LeaseSnapshotSkipBelowSec = 1
            $script:HtT0 = Get-Date
            function Get-QueueAgeSeconds { param($Job, $Metadata) return [int]((Get-Date) - $script:HtT0).TotalSeconds }
            try {
                $metaQ = [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 9 }
                $out = @{}
                foreach ($delay in @(0, 5)) {
                    $script:StatusDelaySec = $delay
                    Reset-Case @{ request = 'refuse_then_active'; refuse_count = 1 }
                    $script:AgeAfter = (Get-Date).AddSeconds(120); $script:StatusCalls = 0; $script:HtT0 = Get-Date
                    $admK = Invoke-HtWait "jHtK$delay" $metaQ
                    $script:Admission = $admK
                    $out[$delay] = [pscustomobject]@{ label = $admK.label; requests = $admK.leaseRequests; calls = $script:StatusCalls }
                    if ($null -ne $script:QuietLease) { Exit-HeavySlot }
                }
                Assert-True ($out[0].label -eq 'clear' -and $out[0].requests -eq 2) "heat-trace A1(r3) baseline: with an instant probe the refused job is asked again and admitted: $($out[0] | ConvertTo-Json -Compress)"
                Assert-True ($out[5].calls -ge 1) "heat-trace A1(r3): the slow probe really ran (calls=$($out[5].calls))"
                Assert-True ($out[5].label -eq $out[0].label -and $out[5].requests -eq $out[0].requests) "heat-trace A1(r3): a 5 s probe changes neither the admission outcome nor the number of requests: instant=$($out[0] | ConvertTo-Json -Compress) slow=$($out[5] | ConvertTo-Json -Compress)"
            }
            finally {
                $LeaseRetrySec = $savedRetry; $script:LeaseSnapshotSkipBelowSec = 70; $script:StatusDelaySec = 0
                function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter) { return 9999 } return [int]$script:HtAge }
            }
            $results.leasePendingHeatTrace = [ordered]@{ waitLines = $r.waits.Count; sample = $r.waits[0]; requestSnapshot = $sReq[0]; grantSnapshot = $sGrant[0]; staleSample = $r2.waits[0]; timeoutSample = $r5.snaps[0] }
        }
        finally {
            $script:FeedThrows = $false; $script:StatusThrows = $false; $script:StatusDelaySec = 0; $script:LeaseSnapshotProbeSec = 30
            if (Get-Command Update-LeaseSlotSnapshots -ErrorAction SilentlyContinue) { Update-LeaseSlotSnapshots -Abandon 'test case end' }
            Set-Item -LiteralPath Function:\Start-HeavySlotCli -Value $script:StartCliReal
            Remove-Item -LiteralPath Function:\Invoke-HeavySlotStatus -ErrorAction SilentlyContinue
            function Read-HeavySlotFeedMetrics { return @{ metrics = $null; why = 'feed missing (unit)' } }
        }
    }
    # ------------------------------------------------------------ lease-heat-trace-async-retry (AGENT-LEASE-PENDING-HEAT-TRACE-1 r3, Dot B1 / adversary r4 A1)
    # The request-time snapshot probe must not hold the wait loop. A refusal that lands while a slow probe runs is asked again on the base
    # schedule, so a second request that needs time to turn active is still admitted before the queue ceiling. Scaled from the adversary's
    # input (LeaseRetrySec 1, ceiling 75 s, probe 30 s, activation 50 s): retry 1 s, ceiling 16 s, probe 12 s, activation 7 s. A probe that
    # holds the loop asks again only at ~12 s and turns active at ~19 s (queue-expired); one that does not asks at ~1-2 s and is active at ~9 s.
    # r5: widened from ceiling 12 s / probe 6 s, which left ~3 s for python start-up and failed on a host at 100% CPU (the e98a2780 agent too).
    if (Test-Case 'lease-heat-trace-async-retry') {
        . ([scriptblock]::Create((Get-FunctionBody 'Invoke-HeavySlotStatus')))   # a base agent's synchronous probe runs for real, so its RED is the real one
        $savedRetry = $LeaseRetrySec; $LeaseRetrySec = 1
        $script:LeaseSnapshotSkipBelowSec = 1   # the 12 s ceiling is inside the default 70 s skip window; the probe must run
        $script:HtT0 = Get-Date
        function Get-QueueAgeSeconds { param($Job, $Metadata) return [int]((Get-Date) - $script:HtT0).TotalSeconds }
        try {
            Reset-Case @{ request = 'refuse_then_active'; refuse_count = 1; active_after = 7 }
            $sinceA = (Get-Date).AddSeconds(-300).ToString('yyyy-MM-dd HH:mm:ss')
            $script:StatusStub = @{ ok = $true; error = $null; lines = @("slot 1: suite X (pid 77, since $sinceA) [exclusive]", 'slot 2: free') }; $script:StatusDelaySec = 12
            $script:AgeAfter = (Get-Date).AddSeconds(120); $script:HtT0 = Get-Date
            $admA = Invoke-HtWait 'jHtA' ([pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 16 })
            $script:Admission = $admA
            $reqsA = @(Get-Calls | Where-Object { $_.cmd -eq 'request' })
            $gapA = if ($reqsA.Count -ge 2) { [double]$reqsA[1].ts - [double]$reqsA[0].ts } else { -1.0 }
            $snapA = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtA phase=request *' })
            Assert-True ($script:StatusCalls -ge 1) "heat-trace B1: the slow request probe really ran (status calls=$($script:StatusCalls))"
            Assert-True ($reqsA.Count -eq 2 -and $gapA -ge 0 -and $gapA -le ($LeaseRetrySec + 5)) "heat-trace B1: the refused request is asked again on the base schedule (within LeaseRetrySec + 5 s), not after the 12 s probe: requests=$($reqsA.Count) gap=$([Math]::Round($gapA, 2)) s"
            Assert-True ($admA.label -ceq 'clear') "heat-trace B1: admitted before the 16 s queue ceiling, not expired by the probe: label=$($admA.label) why=$($admA.why)"
            Assert-True ($snapA.Count -eq 1 -and $snapA[0] -match 'slot 1: suite X pid=77 age=\d+s; slot 2: free') "heat-trace B1: the request snapshot is still logged, from the loop's poll: $($snapA -join ' || ')"
            if ($null -ne $script:QuietLease) { Exit-HeavySlot }
            # adversary r3-a96d3fe5 A1: the probe's bound is checked FIRST (a probe first OBSERVED after it is classified a timeout, not a success), and a
            # caller about to sleep past the bound (HorizonSec, the inbox loop's poll) has a still-running probe stopped now. Bound 1 s here.
            # r4 (Dot): this is LATE-OBSERVATION classification. The fake answers at once; what is delayed past the bound is the poll that observes the
            # exited probe, not the probe's completion. A probe that really completes late is the timeout cases above (StatusDelaySec 4, bound 1 s).
            $script:LeaseSnapshotProbeSec = 1; $script:StatusDelaySec = 0; Sync-FakeStatus
            $cLate = Start-HeavySlotCli -Arguments @('status', '--slots', '2')
            $script:LeaseSlotSnapshots.Add([pscustomobject]@{ jobId = 'jHtL'; phase = 'grant'; call = $cLate; clock = [System.Diagnostics.Stopwatch]::StartNew() })
            $null = $cLate.proc.WaitForExit(10000); Start-Sleep -Milliseconds 1100   # it has exited (rc 0, slot lines); the first poll to observe it comes after its 1 s bound
            Update-LeaseSlotSnapshots
            $lateL = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtL *' })
            Assert-True ($lateL.Count -eq 1 -and $lateL[0] -match ' slots=unknown\(timeout 1 s; seen at (1\.[1-9]|[2-9]\.\d|\d{2,}\.\d) s\)$') "heat-trace A1(r3): a probe first observed only after its bound (late observation) is classified a timeout, never a success, with the age it was seen at (r5): $($lateL -join ' || ')"
            $script:LeaseSnapshotProbeSec = 30; $script:StatusDelaySec = 8; Sync-FakeStatus
            Add-LeaseSlotSnapshot -JobId 'jHtH' -Phase 'grant'
            Start-Sleep -Milliseconds 300
            $pidH = $script:LeaseSlotSnapshots[$script:LeaseSlotSnapshots.Count - 1].call.proc.Id
            Update-LeaseSlotSnapshots -HorizonSec 60
            $horL = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtH *' })
            Assert-True ($script:LeaseSlotSnapshots.Count -eq 0 -and $horL.Count -eq 1 -and $horL[0] -match " slots=unknown\(stopped at \d+ s: the agent's next poll is past the 30 s bound\)$" -and (Wait-Until { -not (Test-PidAlive $pidH) } 5)) "heat-trace A1(r3): a running probe whose bound falls inside the next 60 s sleep is stopped now, not left running: $($horL -join ' || ')"
            $results.leaseHeatTraceAsyncRetry = [ordered]@{ label = $admA.label; requests = $reqsA.Count; retryGapSec = [Math]::Round($gapA, 2); waitedSec = $admA.waitedSec; requestSnapshot = $snapA[0]; lateProbe = $lateL[0]; horizon = $horL[0] }
        }
        finally {
            $LeaseRetrySec = $savedRetry; $script:LeaseSnapshotSkipBelowSec = 70; $script:StatusDelaySec = 0; $script:LeaseSnapshotProbeSec = 30
            if (Get-Command Update-LeaseSlotSnapshots -ErrorAction SilentlyContinue) { Update-LeaseSlotSnapshots -Abandon 'test case end' }
            function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter) { return 9999 } return [int]$script:HtAge }
            Remove-Item -LiteralPath Function:\Invoke-HeavySlotStatus -ErrorAction SilentlyContinue
        }
    }
    # ------------------------------------------------------------ lease-heat-trace-grant-coverage (AGENT-LEASE-PENDING-HEAT-TRACE-1 r3, Dot B2 / adversary r4 A2)
    # A lease the wait reports as covering the job timeout must still cover it when the wait hands it over: no probe may eat its time first.
    # The fake grants the 31 min lease 58 s short (1802 s left at activation, for an 1800 s timeout); each status probe takes 3 s. A wait that
    # runs the request and grant probes synchronously hands it over with ~1796 s left while reporting leaseCoversTimeout=true.
    if (Test-Case 'lease-heat-trace-grant-coverage') {
        . ([scriptblock]::Create((Get-FunctionBody 'Invoke-HeavySlotStatus')))
        try {
            Reset-Case @{ request = 'active'; active_after = 0.3; until_offset_s = -58 }
            $sinceG = (Get-Date).AddSeconds(-300).ToString('yyyy-MM-dd HH:mm:ss')
            $script:StatusStub = @{ ok = $true; error = $null; lines = @("slot 1: suite X (pid 77, since $sinceG) [exclusive]", 'slot 2: free') }; $script:StatusDelaySec = 3
            $script:AgeAfter = (Get-Date).AddSeconds(120)
            Sync-FakeStatus
            $admG = Wait-QuietLeaseAdmission -JobId 'jHtG' -Job ([pscustomobject]@{}) -Metadata $meta
            $remG = [double]$admG.leaseUntil - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0   # read the moment the wait returns
            $script:Admission = $admG
            Complete-HtSnapshots
            $snapG = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtG *' })
            Assert-True ($admG.label -ceq 'clear' -and $admG.leaseMinutes -eq 31) "heat-trace B2: the job is admitted on its 31 min lease: $($admG.label) minutes=$($admG.leaseMinutes)"
            Assert-True ($script:StatusCalls -eq 2 -and $snapG.Count -eq 2) "heat-trace B2: both probes ran and were logged: calls=$($script:StatusCalls) $($snapG -join ' || ')"
            Assert-True ((-not $admG.leaseCoversTimeout) -or $remG -ge 1800) "heat-trace B2: a lease reported as covering the 1800 s timeout has at least 1800 s left when the wait hands it over: covers=$($admG.leaseCoversTimeout) remaining=$([Math]::Round($remG, 1)) s"
            $results.leaseHeatTraceGrantCoverage = [ordered]@{ covers = $admG.leaseCoversTimeout; remainingAtReturnSec = [Math]::Round($remG, 1); waitedSec = $admG.waitedSec }
            Exit-HeavySlot
        }
        finally {
            $script:StatusDelaySec = 0
            if (Get-Command Update-LeaseSlotSnapshots -ErrorAction SilentlyContinue) { Update-LeaseSlotSnapshots -Abandon 'test case end' }
            Remove-Item -LiteralPath Function:\Invoke-HeavySlotStatus -ErrorAction SilentlyContinue
        }
    }
    # ------------------------------------------------------------ lease-heat-trace-nonlocal-feed (AGENT-LEASE-PENDING-HEAT-TRACE-1 r4, Dot B4 / r3 adversary A2)
    # The waiting line's heat read must never hold the wait loop. Read-HeavySlotFeedMetrics is a synchronous stat/open/read with no timeout, and the
    # first pending iteration reads (lastLog starts at MinValue). Scaled from Dot's shape (a UNC feed stalled 30 s, ceiling 20 s, active at 5 s): the
    # stubbed read stalls 8 s while waiting, the ceiling is 6 s, the request turns active at 2 s. A loop that reads a UNC feed is queue-expired at ~8 s;
    # one that skips it (heat=skipped reason=non-local-feed) is admitted on the baseline schedule (a local feed, whose read does not stall).
    if (Test-Case 'lease-heat-trace-nonlocal-feed') {
        $savedFeed = $HeavySlotFeed
        $script:LeaseSnapshotSkipBelowSec = 70   # the 6 s ceiling is inside it: neither snapshot probe runs, so only the heat read is under test
        $script:HtT0 = Get-Date
        function Get-QueueAgeSeconds { param($Job, $Metadata) return [int]((Get-Date) - $script:HtT0).TotalSeconds }
        $script:FeedReads = 0; $script:FeedStallSec = 0
        function Read-HeavySlotFeedMetrics { $script:FeedReads++; if ($null -eq $script:QuietLease -and $script:FeedStallSec -gt 0) { Start-Sleep -Seconds $script:FeedStallSec }; return @{ metrics = $null; why = 'feed missing (stub)'; ageSec = $null } }   # stalls only while waiting: the grant's own read is not under test
        $jn = Join-Path $fx 'heat-jn'
        try {
            $metaN = [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 6 }
            $script:StatusStub = @{ ok = $true; error = $null; lines = @('slot 1: free', 'slot 2: free') }; $script:StatusDelaySec = 0
            $outN = [ordered]@{}
            foreach ($k in @('local', 'unc')) {
                $HeavySlotFeed = if ($k -eq 'local') { Join-Path $fx 'feed-local.csv' } else { '\\heat-trace-unc.invalid\share\hwinfo-log-adapter.csv' }
                $script:FeedStallSec = if ($k -eq 'local') { 0 } else { 8 }
                Reset-Case @{ request = 'active'; active_after = 2 }
                $script:AgeAfter = (Get-Date).AddSeconds(120); $script:FeedReads = 0; $script:HtT0 = Get-Date
                $admN = Invoke-HtWait "jHtN$k" $metaN
                $script:Admission = $admN
                $outN[$k] = [pscustomobject]@{ label = $admN.label; waitedSec = $admN.waitedSec; why = $admN.why; reads = $script:FeedReads; waits = @($script:Log | Where-Object { $_ -like "quiet-lease: waiting job=jHtN$k *" }) }
                if ($null -ne $script:QuietLease) { Exit-HeavySlot }
            }
            $script:FeedStallSec = 0
            Assert-True ($outN.local.label -ceq 'clear' -and $outN.local.waits.Count -ge 1 -and @($outN.local.waits | Where-Object { $_ -notmatch ' heat=unknown\(feed missing \(stub\)\) feedAge=unknown$' }).Count -eq 0) "heat-trace B4 baseline: a local feed is read on the waiting line and the job is admitted: label=$($outN.local.label) $($outN.local.waits -join ' || ')"
            Assert-True ($outN.unc.label -ceq 'clear') "heat-trace B4: a stalled UNC feed read never holds the wait past its 6 s queue ceiling: label=$($outN.unc.label) why=$($outN.unc.why)"
            # r5: +4 s, not +2: at 100% host CPU the python start-up alone moved either run by 2-3 s (local 2-5 s, unc 2-4 s, both agents); a read of
            # the stalled feed adds its 8 s, so the bound still tells the two apart (and the label above already fails a queue-expired run).
            Assert-True ($outN.unc.waitedSec -le $outN.local.waitedSec + 4) "heat-trace B4: the activation is processed on the baseline schedule: unc=$($outN.unc.waitedSec)s local=$($outN.local.waitedSec)s"
            Assert-True ($outN.unc.waits.Count -ge 1 -and @($outN.unc.waits | Where-Object { $_ -notmatch ' heat=skipped reason=non-local-feed$' }).Count -eq 0) "heat-trace B4: the heat field stays on every waiting line, as heat=skipped reason=non-local-feed: $($outN.unc.waits -join ' || ')"
            Assert-True ($outN.unc.reads -eq 1) "heat-trace B4: a UNC feed is never read while waiting (the one read is the grant's own): reads=$($outN.unc.reads)"
            # The path rule, called directly: only a fully-qualified drive-letter path on a fixed disk with no reparse point on the way is read.
            $jReal = Join-Path $fx 'heat-jn-target'
            New-Item -ItemType Directory -Force -Path $jReal | Out-Null
            New-Item -ItemType Junction -Path $jn -Target $jReal | Out-Null
            $script:FeedReads = 0
            $pathCases = [ordered]@{ unc = '\\srv\share\f.csv'; slashUnc = '//srv/share/f.csv'; device = '\\?\C:\temp\f.csv'; dotDevice = '\\.\C:\temp\f.csv'; relative = 'feed.csv'; driveRelative = 'C:feed.csv'; empty = ''; junction = (Join-Path $jn 'f.csv') }
            foreach ($pc in $pathCases.Keys) {
                $HeavySlotFeed = $pathCases[$pc]
                $f = Get-LeaseWaitHeatField
                Assert-True ($f -ceq 'heat=skipped reason=non-local-feed') "heat-trace B4: a $pc feed path is skipped, not read: '$($pathCases[$pc])' -> $f"
            }
            Assert-True ($script:FeedReads -eq 0) "heat-trace B4: none of the non-local paths was read: reads=$($script:FeedReads)"
            $HeavySlotFeed = Join-Path $fx 'feed-local.csv'
            $fLocal = Get-LeaseWaitHeatField
            Assert-True ($script:FeedReads -eq 1 -and $fLocal -match '^heat=unknown\(feed missing \(stub\)\)') "heat-trace B4: a local fixed-disk path is read: reads=$($script:FeedReads) $fLocal"
            # adversary r4-6abf870f A3: the snapshot's Start-HeavySlotCli checks heavy-slot.py and python synchronously, before the probe's bound
            # exists. A script or configured python off a local fixed disk is slots=skipped(...), and no probe is started.
            $savedScript = $HeavySlotScript; $savedPy = $HeavySlotPython
            $cliReal = ${function:Start-HeavySlotCli}
            $script:CliCalls = 0
            function Start-HeavySlotCli { param([string[]]$Arguments) $script:CliCalls++; return [pscustomobject]@{ ok = $false; why = 'stub: not started' } }
            try {
                $HeavySlotScript = '\\heat-trace-unc.invalid\share\heavy-slot.py'
                Add-LeaseSlotSnapshot -JobId 'jHtS1' -Phase 'grant'
                $HeavySlotScript = $savedScript; $HeavySlotPython = '\\heat-trace-unc.invalid\share\python.exe'
                Add-LeaseSlotSnapshot -JobId 'jHtS2' -Phase 'request'
            }
            finally { $HeavySlotScript = $savedScript; $HeavySlotPython = $savedPy; Set-Item -LiteralPath Function:\Start-HeavySlotCli -Value $cliReal }
            $sS = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtS*' })
            Assert-True ($script:CliCalls -eq 0 -and $sS.Count -eq 2 -and @($sS | Where-Object { $_ -notmatch ' slots=skipped\(heavy-slot\.py or its python is not on a local fixed disk\)$' }).Count -eq 0) "heat-trace A3(r4): a UNC heavy-slot.py or python starts no snapshot probe and logs slots=skipped(...): cli=$($script:CliCalls) $($sS -join ' || ')"
            # adversary r4-6abf870f A2: a caller about to wait (the job monitor loop's slice, the wait loop's sleep) wakes at the earliest probe's bound,
            # so a probe 29.5 s into its 30 s bound is killed at ~30 s, not a 5 s slice later. Pure, on a private probe list; then both callers use it.
            Assert-True ($null -ne (Get-Command Get-LeaseSlotSnapshotWaitMs -ErrorAction SilentlyContinue)) "heat-trace A2(r4): the agent defines Get-LeaseSlotSnapshotWaitMs"
            $savedSnaps = $script:LeaseSlotSnapshots
            try {
                $script:LeaseSlotSnapshots = [System.Collections.Generic.List[object]]::new()
                $wNone = Get-LeaseSlotSnapshotWaitMs -MaxMs 5000
                $script:LeaseSlotSnapshots.Add([pscustomobject]@{ jobId = 'jHtW'; phase = 'grant'; call = $null; clock = [pscustomobject]@{ Elapsed = [TimeSpan]::FromSeconds(29.5) } })
                $wNear = Get-LeaseSlotSnapshotWaitMs -MaxMs 5000
                $script:LeaseSlotSnapshots.Add([pscustomobject]@{ jobId = 'jHtW2'; phase = 'grant'; call = $null; clock = [pscustomobject]@{ Elapsed = [TimeSpan]::FromSeconds(31) } })
                $wPast = Get-LeaseSlotSnapshotWaitMs -MaxMs 5000
            }
            finally { $script:LeaseSlotSnapshots = $savedSnaps }
            Assert-True ($wNone -eq 5000 -and $wNear -ge 1 -and $wNear -le 500 -and $wPast -eq 1) "heat-trace A2(r4): the wait is capped at the earliest bound: none=$wNone near(29.5 s)=$wNear past=$wPast"
            Assert-True ($agentText.Contains('Start-Sleep -Milliseconds (Get-LeaseSlotSnapshotWaitMs -MaxMs $script:GateLoopSleepMs)') -and $agentText.Contains('$remainingMs = Get-LeaseSlotSnapshotWaitMs -MaxMs $remainingMs')) "heat-trace A2(r4): the wait loop's sleep and the job monitor loop's slice are both capped by it"
            # adversary r4-52d0e792 A1: a heavy-slot.py that is a leaf symlink (here to a missing UNC target) and a configured python that does not
            # exist (Resolve-HeavySlotPython would search PATH) start no probe either. The symlink half needs the right to create one; without it,
            # that half is recorded as not run.
            $cliReal = ${function:Start-HeavySlotCli}
            $script:CliCalls = 0
            function Start-HeavySlotCli { param([string[]]$Arguments) $script:CliCalls++; return [pscustomobject]@{ ok = $false; why = 'stub: not started' } }
            $lnk = Join-Path $fx 'heavy-slot-link.py'
            $symlinkRan = $false
            try {
                try { New-Item -ItemType SymbolicLink -Path $lnk -Target '\\heat-trace-unc.invalid\share\heavy-slot.py' -ErrorAction Stop | Out-Null; $symlinkRan = $true } catch { }
                if ($symlinkRan) { $HeavySlotScript = $lnk; Add-LeaseSlotSnapshot -JobId 'jHtS3' -Phase 'grant'; $HeavySlotScript = $savedScript }
                $HeavySlotPython = Join-Path $fx 'no-such-python.exe'
                Add-LeaseSlotSnapshot -JobId 'jHtS4' -Phase 'request'
            }
            finally {
                $HeavySlotScript = $savedScript; $HeavySlotPython = $savedPy; Set-Item -LiteralPath Function:\Start-HeavySlotCli -Value $cliReal
                if (Test-Path -LiteralPath $lnk) { [IO.File]::Delete($lnk) }   # the link only
            }
            $sS2 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtS3 *' -or $_ -like 'quiet-lease: slots-snapshot job=jHtS4 *' })
            Assert-True ($script:CliCalls -eq 0 -and $sS2.Count -eq $(if ($symlinkRan) { 2 } else { 1 }) -and @($sS2 | Where-Object { $_ -notmatch ' slots=skipped\(heavy-slot\.py or its python is not on a local fixed disk\)$' }).Count -eq 0) "heat-trace A1(r4b): a symlinked heavy-slot.py (ran=$symlinkRan) and a missing configured python start no probe: cli=$($script:CliCalls) $($sS2 -join ' || ')"
            # adversary r4-52d0e792 A3: the headroom reads the absolute notAfterUtc against the clock now, not only the remainingSec sampled with the verdict.
            $hStale = Get-LeaseSnapshotHeadroomSec -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 75; notAfterUtc = [DateTime]::UtcNow.AddSeconds(25).ToString('o') })
            $hBad = Get-LeaseSnapshotHeadroomSec -Verdict ([pscustomobject]@{ state = 'open'; remainingSec = 75; notAfterUtc = 'not a date' })
            Assert-True ($hStale -le 26 -and $hStale -gt 20 -and $hBad -eq 0) "heat-trace A3(r4b): a verdict sampled 50 s ago (remainingSec 75, notAfterUtc now+25 s) is ~25 s of headroom, an unreadable one none: stale=$hStale bad=$hBad"
            Assert-True ($agentText.Contains('[double]$maxAge - [double](Get-QueueAgeSeconds -Job $Job -Metadata $Metadata) } else { [double]::PositiveInfinity }))   # r4 (adversary A3)')) "heat-trace A3(r4b): the request snapshot's queue headroom is the age read after the request started"
            # adversary r4-6abf870f A4 (advisory, fixed in scope): a held field past Int32 is read, one past Int64 is unknown; neither throws.
            $hBig = $null; $hHuge = $null; $hErr = $null
            try { $hBig = ConvertFrom-HeavySlotHolderText -Text 'suite (pid 77, held 2147483648 min)'; $hHuge = ConvertFrom-HeavySlotHolderText -Text 'suite (pid 77, held 99999999999999999999 min)' } catch { $hErr = $_.Exception.Message }
            Assert-True ($null -eq $hErr -and $hBig.heldMin -eq 2147483648 -and $hBig.ownerPid -ceq '77' -and $null -ne $hHuge -and $null -eq $hHuge.heldMin) "heat-trace A4(r4): an Int32-overflowing held field never throws: err=$hErr big=$($hBig.heldMin) huge=$($hHuge.heldMin)"
            # adversary r4-6abf870f A5 (advisory, fixed in scope): with no job timeout, Set-LeaseCoverageAtLaunch changes nothing.
            $adZ = [pscustomobject]@{ mode = 'lease'; leaseCoversTimeout = $true }
            Set-LeaseCoverageAtLaunch -JobId 'jHtZ' -Admission $adZ -Lease ([pscustomobject]@{ active = [pscustomobject]@{ until = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + 100 } }) -TimeoutSec 0
            Assert-True ($null -eq $adZ.PSObject.Properties['leaseRemainingAtLaunchSec'] -and $adZ.leaseCoversTimeout -eq $true) "heat-trace A5(r4): TimeoutSec 0 adds no field and keeps leaseCoversTimeout: $($adZ | ConvertTo-Json -Compress)"
            $results.leaseHeatTraceNonlocalFeed = [ordered]@{ local = $outN.local.waitedSec; unc = $outN.unc.waitedSec; uncLabel = $outN.unc.label; uncSample = $outN.unc.waits[0]; scriptSkip = $sS[0]; waitNearMs = $wNear }
        }
        finally {
            $HeavySlotFeed = $savedFeed; $script:FeedStallSec = 0
            if (Test-Path -LiteralPath $jn) { [IO.Directory]::Delete($jn) }   # the junction only, never its target's contents
            function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter) { return 9999 } return [int]$script:HtAge }
            function Read-HeavySlotFeedMetrics { return @{ metrics = $null; why = 'feed missing (unit)' } }
            if (Get-Command Update-LeaseSlotSnapshots -ErrorAction SilentlyContinue) { Update-LeaseSlotSnapshots -Abandon 'test case end' }
        }
    }
    # ------------------------------------------------------------ lease-heat-trace-checked-python (AGENT-LEASE-PENDING-HEAT-TRACE-1 r5, sol r4 S2 = adversary r4-e98a2780 A2)
    # The interpreter the snapshot checked is the one it starts. Sol's shape: HeavySlotPython=C:\probe\python.exe passes the check, is renamed before
    # the probe starts, and PATH begins with \\review-server\tools. The rename is made as the probe starts (a wrapper around the agent's own
    # Start-HeavySlotCli); a python.exe PATH search is recorded (Get-Command shadowed for that name only), never made.
    if (Test-Case 'lease-heat-trace-checked-python') {
        $savedPy = $HeavySlotPython; $savedPath = $env:PATH
        $script:CliRealP = ${function:Start-HeavySlotCli}
        $pyDir = Join-Path $fx 'probe-py'
        try {
            New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
            $pyChecked = Join-Path $pyDir 'python.exe'
            [IO.File]::WriteAllText($pyChecked, 'not an interpreter')   # exists, so the snapshot's check passes; renamed before it could be started
            $HeavySlotPython = $pyChecked
            $env:PATH = '\\heat-trace-unc.invalid\tools;' + $savedPath
            $script:PathSearches = 0; $script:RenameAt = $pyChecked; $script:Renamed = $false
            function Get-Command { if (@($args) -contains 'python.exe') { $script:PathSearches++; return $null }; Microsoft.PowerShell.Core\Get-Command @args }
            function Start-HeavySlotCli { param([string[]]$Arguments, [string]$Python = '', [string]$Script = '') if (Test-Path -LiteralPath $script:RenameAt) { Move-Item -LiteralPath $script:RenameAt -Destination ($script:RenameAt + '.renamed'); $script:Renamed = $true }; return (& $script:CliRealP @PSBoundParameters) }
            $nBefore = $script:LeaseSlotSnapshots.Count
            Add-LeaseSlotSnapshot -JobId 'jHtP2' -Phase 'request'
            $nAfter = $script:LeaseSlotSnapshots.Count
            Remove-Item -LiteralPath Function:\Get-Command
            Set-Item -LiteralPath Function:\Start-HeavySlotCli -Value $script:CliRealP
            $env:PATH = $savedPath; $HeavySlotPython = $savedPy
            $lS2 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtP2 *' })
            Assert-True ($script:Renamed) "probe-watchdog S2: the checked python was renamed between the snapshot's check and the probe's start"
            Assert-True ($script:PathSearches -eq 0) "probe-watchdog S2: a checked python gone at launch is never re-resolved through PATH (which begins with a UNC entry): python.exe PATH searches=$($script:PathSearches)"
            Assert-True ($nAfter -eq $nBefore -and $lS2.Count -eq 1 -and $lS2[0] -match ([regex]::Escape(" slots=unknown(python not found at its checked path ($pyChecked); no PATH search)") + '$')) "probe-watchdog S2: no probe starts, and the line names the checked path: $($lS2 -join ' || ')"
            Assert-True ($agentText.Contains('-Python $HeavySlotPython -Script $HeavySlotScript')) "probe-watchdog S2: the snapshot starts the paths it checked"
            $results.leaseHeatTraceCheckedPython = [ordered]@{ line = $lS2[0]; pathSearches = $script:PathSearches }
        }
        finally {
            if (Test-Path -LiteralPath Function:\Get-Command) { Remove-Item -LiteralPath Function:\Get-Command }
            Set-Item -LiteralPath Function:\Start-HeavySlotCli -Value $script:CliRealP
            $env:PATH = $savedPath; $HeavySlotPython = $savedPy
        }
    }
    # ------------------------------------------------------------ lease-heat-trace-probe-watchdog (AGENT-LEASE-PENDING-HEAT-TRACE-1 r5, sol r4 S1 = adversary r4-e98a2780 A1)
    # A probe terminates within its bound whether or not the agent polls. Sol's shape (request active at 0.3 s, status delay 300 s, a 30 s renew that
    # does not poll, bound 30 s), scaled: bound 2 s, the fake's renew sleeps 6 s inside the agent's own Complete-HeavySlotCli. The probe must be dead
    # when the renew returns, before any poll, and its line gives the age it was killed at. A poll that finds a probe past its bound gives the real
    # age too (sol's 31 s: 31.4 s of a 30 s bound).
    if (Test-Case 'lease-heat-trace-probe-watchdog') {
        $script:CliRealP = ${function:Start-HeavySlotCli}
        $script:OrphanKids = [System.Collections.Generic.List[int]]::new()
        try {
            $script:LeaseSnapshotProbeSec = 2; $script:StatusDelaySec = 300
            $script:StatusStub = @{ ok = $true; error = $null; lines = @('slot 1: free', 'slot 2: free') }
            Reset-Case @{ request = 'active'; active_after = 0.3; renew_sleep = 6 }
            Sync-FakeStatus
            Add-LeaseSlotSnapshot -JobId 'jHtP1' -Phase 'grant'
            $e1 = @($script:LeaseSlotSnapshots | Where-Object { $_.jobId -eq 'jHtP1' })
            Assert-True ($e1.Count -eq 1) "probe-watchdog S1: the grant probe started: $($e1.Count)"
            $pidP = $e1[0].call.proc.Id
            $tB = [Diagnostics.Stopwatch]::StartNew()
            $rn = Complete-HeavySlotCli -Call (Start-HeavySlotCli -Arguments @('lease', 'renew', '--label', $script:QuietLeaseLabel)) -TimeoutSec 15 -HeartbeatActivity 'pre-launch job=jHtP1'
            $blockedSec = $tB.Elapsed.TotalSeconds
            $exitedAtRenewEnd = $e1[0].call.proc.HasExited
            $aliveAtRenewEnd = Test-PidAlive $pidP
            $ageAtRenewEnd = $e1[0].clock.Elapsed.TotalSeconds
            Update-LeaseSlotSnapshots
            $lP = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtP1 *' })
            Assert-True ($rn.done -and $blockedSec -ge 5.5) "probe-watchdog S1: the pre-launch renew really held the agent without polling: $([Math]::Round($blockedSec, 2)) s (done=$($rn.done))"
            Assert-True ($exitedAtRenewEnd -and -not $aliveAtRenewEnd) "probe-watchdog S1: the probe was killed at its 2 s bound while the renew held the agent and nothing polled (exited=$exitedAtRenewEnd alive=$aliveAtRenewEnd, age $([Math]::Round($ageAtRenewEnd, 1)) s)"
            Assert-True ($lP.Count -eq 1 -and $lP[0] -match ' slots=unknown\(timeout 2 s; killed at (2\.\d|3\.[0-4]) s\)$') "probe-watchdog S1: the line gives the age it was killed at (the bound plus timer slack), not the age it was polled at: $($lP -join ' || ')"
            # The poll's own bound check (a probe with no watchdog) gives the real age too: sol's 31 s, bound 30 s.
            $script:LeaseSnapshotProbeSec = 30
            $cL = & $script:CliRealP -Arguments @('status', '--slots', '2')
            Assert-True ($cL.ok) "probe-watchdog S1: a status probe started for the poll case: $($cL.why)"
            $pidL = $cL.proc.Id
            $script:LeaseSlotSnapshots.Add([pscustomobject]@{ jobId = 'jHtP3'; phase = 'grant'; call = $cL; clock = [pscustomobject]@{ Elapsed = [TimeSpan]::FromSeconds(31.4) }; dog = $null })
            Update-LeaseSlotSnapshots
            $lP3 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtP3 *' })
            Assert-True ($lP3.Count -eq 1 -and $lP3[0] -match ' slots=unknown\(timeout 30 s; killed at 31\.4 s\)$' -and (Wait-Until { -not (Test-PidAlive $pidL) } 5)) "probe-watchdog S1: a probe the poll kills past its bound is logged with its real age (31.4 s of a 30 s bound): $($lP3 -join ' || ')"
            # adversary r5-2ca9f68f A1: a status root that exits at once, leaving a 300 s child holding its stdout/stderr, is not done: the
            # watchdog (nothing polls) and the poll's own bound kill both end that child. Bound 5 s here so a slow python start-up still spawns it.
            $script:LeaseSnapshotProbeSec = 5; $script:StatusDelaySec = 0; $script:StatusOrphan = $true
            Reset-Case @{ request = 'active' }
            Sync-FakeStatus
            Add-LeaseSlotSnapshot -JobId 'jHtP4' -Phase 'grant'
            $null = Wait-Until { $script:Kc = @(Get-Calls | Where-Object { $_.cmd -eq 'status-orphan' }); $script:Kc.Count -ge 1 } 10
            Assert-True ($script:Kc.Count -eq 1) "probe-watchdog A1(r5): the status root spawned its orphan child: $($script:Kc.Count)"
            $kid4 = [int]$script:Kc[0].child; $script:OrphanKids.Add($kid4)
            $kid4Gone = Wait-Until { -not (Test-PidAlive $kid4) } 10   # nothing polls meanwhile
            $age4 = ($script:LeaseSlotSnapshots | Where-Object { $_.jobId -eq 'jHtP4' } | Select-Object -First 1).clock.Elapsed.TotalSeconds
            Update-LeaseSlotSnapshots
            $lP4 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtP4 *' })
            Assert-True ($kid4Gone) "probe-watchdog A1(r5): the child a finished root left holding the probe's pipes is killed at the bound with no poll (alive after $([Math]::Round([double]$age4, 1)) s of a 5 s bound)"
            Assert-True ($lP4.Count -eq 1 -and $lP4[0] -match ' slots=unknown\(timeout 5 s; killed at (5\.\d|6\.[0-4]) s\)$') "probe-watchdog A1(r5): the watchdog's kill is logged with its age: $($lP4 -join ' || ')"
            $script:LeaseSnapshotProbeSec = 30
            Reset-Case @{ request = 'active' }
            Sync-FakeStatus
            $cO = & $script:CliRealP -Arguments @('status', '--slots', '2')
            $null = Wait-Until { $script:Kc = @(Get-Calls | Where-Object { $_.cmd -eq 'status-orphan' }); $script:Kc.Count -ge 1 -and $cO.proc.HasExited } 10
            Assert-True ($script:Kc.Count -eq 1 -and $cO.proc.HasExited) "probe-watchdog A1(r5): the poll-case root spawned its child and exited: $($script:Kc.Count)"
            $kid5 = [int]$script:Kc[0].child; $script:OrphanKids.Add($kid5)
            $script:LeaseSlotSnapshots.Add([pscustomobject]@{ jobId = 'jHtP5'; phase = 'grant'; call = $cO; clock = [pscustomobject]@{ Elapsed = [TimeSpan]::FromSeconds(31.4) }; dog = $null })
            Update-LeaseSlotSnapshots
            $lP5 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jHtP5 *' })
            Assert-True ((Wait-Until { -not (Test-PidAlive $kid5) } 5) -and $lP5.Count -eq 1 -and $lP5[0] -match ' slots=unknown\(timeout 30 s; killed at 31\.4 s\)$') "probe-watchdog A1(r5): the poll's kill past the bound ends the child of an exited root too, logged as killed (not seen): $($lP5 -join ' || ')"
            $script:StatusOrphan = $false
            Assert-True ($script:ProbeWatchdogReady) "probe-watchdog S1: the agent defines Initialize-LeaseProbeWatchdog and it compiles"
            $results.leaseHeatTraceProbeWatchdog = [ordered]@{ renewBlockedSec = [Math]::Round($blockedSec, 2); probeAgeAtRenewEnd = [Math]::Round($ageAtRenewEnd, 2); line = $lP[0]; pollLine = $lP3[0]; orphanLine = $lP4[0]; orphanPollLine = $lP5[0] }
        }
        finally {
            $script:StatusOrphan = $false
            foreach ($k in @($script:OrphanKids)) { Stop-Process -Id $k -Force -ErrorAction SilentlyContinue }
            $script:LeaseSnapshotProbeSec = 30; $script:StatusDelaySec = 0
            if (Get-Command Update-LeaseSlotSnapshots -ErrorAction SilentlyContinue) { Update-LeaseSlotSnapshots -Abandon 'test case end' }
        }
    }
    # ------------------------------------------------------------ lease-snapshot-bound-straddle (AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r1, sol r5 B1)
    # A probe's outcome comes from ONE completion sample and the age sampled AFTER it, never a re-read. Sol's shape: bound 30 s, the poll samples
    # 29.990 s with the probe not done, the probe completes at 30.010 s, and a second completion read accepts a success past the bound. Designed:
    # done with an age under the bound is a success; done with an age AT or past it is a timeout (seen), since when it finished inside the last
    # poll interval is unknown; not done past it is a timeout (killed). Controlled dependencies: a fake probe whose completion lands at a chosen
    # read and a fake clock; the agent's own Update-LeaseSlotSnapshots classifies it (Stop-/Receive-HeavySlotCli stubbed and counted).
    if (Test-Case 'lease-snapshot-bound-straddle') {
        $script:BsStopReal = ${function:Stop-HeavySlotCli}; $script:BsRecvReal = ${function:Receive-HeavySlotCli}
        $bsFails = [System.Collections.Generic.List[string]]::new()
        try {
            $script:LeaseSnapshotProbeSec = 30
            Update-LeaseSlotSnapshots -Abandon 'test case start'
            function Stop-HeavySlotCli { param($Call) $script:BsStops++ }
            function Receive-HeavySlotCli { param($Call) $script:BsRecv++; return [pscustomobject]@{ done = $true; rc = 0; stdout = "slot 1: free`nslot 2: free"; stderr = ''; why = $null } }
            function Add-BsProbe([string]$JobId, [double[]]$Ages, [string]$Land) {
                # Land: 'start' (done before the poll) | 'age1' (lands right after the first age read) | 'done1' (right after the first completion read) | 'never'
                $st = [pscustomobject]@{ done = ($Land -eq 'start'); ages = [System.Collections.Generic.Queue[double]]::new([double[]]$Ages); last = 0.0; land = $Land; ageReads = 0; doneReads = 0 }
                $clock = [pscustomobject]@{ st = $st }
                $clock | Add-Member -MemberType ScriptProperty -Name Elapsed -Value { $s = $this.st; if ($s.ages.Count -gt 0) { $s.last = $s.ages.Dequeue() }; $s.ageReads++; if ($s.land -eq 'age1' -and $s.ageReads -eq 1) { $s.done = $true }; [TimeSpan]::FromSeconds($s.last) }
                $proc = [pscustomobject]@{ st = $st }
                $proc | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $s = $this.st; $d = $s.done; $s.doneReads++; if ($s.land -eq 'done1' -and $s.doneReads -eq 1) { $s.done = $true }; $d }
                $pipe = [pscustomobject]@{ st = $st }
                $pipe | Add-Member -MemberType ScriptProperty -Name IsCompleted -Value { $this.st.done }
                $script:LeaseSlotSnapshots.Add([pscustomobject]@{ jobId = $JobId; phase = 'grant'; call = [pscustomobject]@{ ok = $true; proc = $proc; out = $pipe; err = $pipe }; clock = $clock; dog = $null })
            }
            function Invoke-BsPoll([string]$JobId) {
                $script:BsStops = 0; $script:BsRecv = 0
                $n0 = $script:Log.Count
                Update-LeaseSlotSnapshots
                return [pscustomobject]@{ lines = @($script:Log | Select-Object -Skip $n0 | Where-Object { $_ -like "quiet-lease: slots-snapshot job=$JobId *" }); stops = $script:BsStops; recv = $script:BsRecv; inFlight = @($script:LeaseSlotSnapshots | Where-Object { $_.jobId -eq $JobId }).Count }
            }
            $bsOk = ' slots=[slot 1: free; slot 2: free] queued=0'
            $bsSeen = ' slots=unknown(timeout 30 s; seen at 30.0 s)'
            $bsRes = [ordered]@{}
            # (a) sol's replay: sampled not done at 29.990 s; the completion lands right after that completion sample (30.010 s); next poll 30.025 s.
            # (b) the completion lands between the age sample and the completion sample, so an age read before the completion would be stale.
            foreach ($land in @('done1', 'age1')) {
                $j = "jBs-$land"
                Add-BsProbe $j @(29.990, 30.025) $land
                $p1 = Invoke-BsPoll $j
                if ($p1.lines.Count -ne 0 -or $p1.recv -ne 0 -or $p1.inFlight -ne 1) { $bsFails.Add("($land) a completion that lands after the poll's 29.990 s sample is not a success at that poll: lines=[$($p1.lines -join ' || ')] received=$($p1.recv) inFlight=$($p1.inFlight)") }
                else {
                    $p2 = Invoke-BsPoll $j
                    if ($p2.lines.Count -ne 1 -or -not $p2.lines[0].EndsWith($bsSeen) -or $p2.recv -ne 0 -or $p2.inFlight -ne 0) { $bsFails.Add("($land) the next poll (30.025 s) sees it done past the bound, a timeout: lines=[$($p2.lines -join ' || ')] received=$($p2.recv)") }
                    $bsRes[$land] = @($p2.lines)
                }
                $script:LeaseSlotSnapshots.Clear()
            }
            # (c) done before the 29.990 s sample: a success.
            Add-BsProbe 'jBs-in' @(29.990) 'start'
            $pc = Invoke-BsPoll 'jBs-in'
            if ($pc.lines.Count -ne 1 -or -not $pc.lines[0].EndsWith($bsOk) -or $pc.recv -ne 1 -or $pc.stops -ne 0) { $bsFails.Add("(in) done with its age under the bound is a success: lines=[$($pc.lines -join ' || ')] received=$($pc.recv) stops=$($pc.stops)") }
            $script:LeaseSlotSnapshots.Clear()
            # (d) done, age sampled exactly at the bound: a timeout (seen), by design.
            Add-BsProbe 'jBs-at' @(30.000) 'start'
            $pd = Invoke-BsPoll 'jBs-at'
            if ($pd.lines.Count -ne 1 -or -not $pd.lines[0].EndsWith($bsSeen) -or $pd.recv -ne 0 -or $pd.stops -ne 1) { $bsFails.Add("(at) done with its age sampled at the bound is a timeout (seen): lines=[$($pd.lines -join ' || ')] received=$($pd.recv) stops=$($pd.stops)") }
            $script:LeaseSlotSnapshots.Clear()
            # (e) not done past the bound: a timeout (killed).
            Add-BsProbe 'jBs-past' @(30.010) 'never'
            $pe = Invoke-BsPoll 'jBs-past'
            if ($pe.lines.Count -ne 1 -or -not $pe.lines[0].EndsWith(' slots=unknown(timeout 30 s; killed at 30.0 s)') -or $pe.stops -ne 1) { $bsFails.Add("(past) not done past the bound is a timeout (killed): lines=[$($pe.lines -join ' || ')] stops=$($pe.stops)") }
            $script:LeaseSlotSnapshots.Clear()
            $bsRes.in = @($pc.lines); $bsRes.at = @($pd.lines); $bsRes.past = @($pe.lines)
            Assert-True ($bsFails.Count -eq 0) "bound-straddle (sol r5 B1): $($bsFails -join ' ;; ')"
            $results.leaseSnapshotBoundStraddle = $bsRes
        }
        finally {
            $script:LeaseSlotSnapshots.Clear()
            Set-Item -LiteralPath Function:\Stop-HeavySlotCli -Value $script:BsStopReal
            Set-Item -LiteralPath Function:\Receive-HeavySlotCli -Value $script:BsRecvReal
        }
    }
    # ------------------------------------------------------------ lease-probe-watchdog-add-type-failure (AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r1, sol r5 B4)
    # No watchdog, no probe. The watchdog type is compiled once per process, and this process already has it, so the Add-Type failure runs in a
    # fresh pwsh: the agent's own Initialize-LeaseProbeWatchdog and Add-LeaseSlotSnapshot, with Add-Type shadowed to throw (throw) or to return
    # without loading the type (noop: the null type). Start-HeavySlotCli is a counting stub: a call to it is a probe started.
    if (Test-Case 'lease-probe-watchdog-add-type-failure') {
        $atFails = [System.Collections.Generic.List[string]]::new()
        $atChild = @'
param([string]$Agent, [string]$Mode, [string]$Py)
$ErrorActionPreference = 'Stop'
$tk = $null; $pe = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Agent, [ref]$tk, [ref]$pe)
foreach ($n in @('Initialize-LeaseProbeWatchdog', 'Add-LeaseSlotSnapshot')) {
    $fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
$script:Log = [System.Collections.Generic.List[string]]::new()
function Add-AgentLogLine([string]$Message) { $script:Log.Add($Message) }
if ($Mode -eq 'throw') { function Add-Type { throw 'unit: Add-Type failed (no compiler)' } } else { function Add-Type { } }
function Test-LeaseLocalFixedPath { param([string]$Path, [switch]$LeafLinkOk) return $true }
$script:Starts = 0
function Start-HeavySlotCli { param([string[]]$Arguments, [string]$Python = '', [string]$Script = '') $script:Starts++; return [pscustomobject]@{ ok = $true; proc = $null; out = $null; err = $null; started = (Get-Date); why = $null } }
$HeavySlotScript = $Agent; $HeavySlotPython = $Py; $HeavySlotCount = 2
$script:LeaseSlotSnapshots = [System.Collections.Generic.List[object]]::new()
$script:LeaseSnapshotProbeSec = 30
$script:LeaseSnapshotSkipBelowSec = 70
$ready = Initialize-LeaseProbeWatchdog
$typeLoaded = $null -ne ('MlvApp.Agent.LeaseProbeWatchdog' -as [type])
$threw = $null; $out = @()
try { $out = @(Add-LeaseSlotSnapshot -JobId 'jB4a' -Phase 'request') } catch { $threw = [string]$_.Exception.Message }
[pscustomobject]@{ ready = [bool]$ready; typeLoaded = $typeLoaded; starts = $script:Starts; registered = $script:LeaseSlotSnapshots.Count; output = $out.Count; threw = $threw; log = @($script:Log) } | ConvertTo-Json -Compress -Depth 4
'@
        $atPath = Join-Path $fx 'add-type-failure-child.ps1'
        [IO.File]::WriteAllText($atPath, $atChild, [Text.UTF8Encoding]::new($false))
        $pwshExe = (Get-Process -Id $PID).Path
        $atRes = [ordered]@{}
        foreach ($mode in @('throw', 'noop')) {
            $raw = @(& { $ErrorActionPreference = 'Continue'; & $pwshExe -NoProfile -NonInteractive -File $atPath -Agent $agentScript -Mode $mode -Py $Python 2>&1 } | ForEach-Object { [string]$_ })
            $r = $null; try { $r = ($raw | Where-Object { $_.StartsWith('{') } | Select-Object -Last 1) | ConvertFrom-Json } catch { $r = $null }
            if ($null -eq $r) { $atFails.Add("($mode) the child reported: $($raw -join ' | ')"); continue }
            $u = @($r.log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jB4a *' })
            if ($r.typeLoaded) { $atFails.Add("($mode) setup: the watchdog type must not load here") }
            if ($r.ready) { $atFails.Add("($mode) Initialize-LeaseProbeWatchdog reports the watchdog unavailable: ready=$($r.ready)") }
            if ($r.starts -ne 0 -or $r.registered -ne 0) { $atFails.Add("($mode) no probe is started without a watchdog: Start-HeavySlotCli calls=$($r.starts) registered=$($r.registered)") }
            if ($u.Count -ne 1 -or $u[0] -notmatch ' phase=request slots=unknown\(watchdog unavailable\)$') { $atFails.Add("($mode) the snapshot line is slots=unknown(watchdog unavailable): [$($u -join ' || ')]") }
            if ($r.threw -or $r.output -ne 0) { $atFails.Add("($mode) the snapshot neither throws nor returns anything (the wait goes on unchanged): threw=$($r.threw) output=$($r.output)") }
            $atRes[$mode] = $r
        }
        Assert-True ($atFails.Count -eq 0) "watchdog Add-Type failure (sol r5 B4): $($atFails -join ' ;; ')"
        $results.leaseProbeWatchdogAddTypeFailure = $atRes
    }
    # ------------------------------------------------------------ lease-probe-watchdog-ctor-failure (AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r1, sol r5 B4)
    # The agent's own watchdog constructor throws: its Timer rejects a due time past 4294967294 ms, so a 5,000,000 s bound is a real throw of the
    # real type. No probe starts (the fake heavy-slot.py logs every status call it gets), the line is slots=unknown(watchdog unavailable), and a
    # whole wait with both snapshots failing closed admits the job exactly as before.
    if (Test-Case 'lease-probe-watchdog-ctor-failure') {
        $ctFails = [System.Collections.Generic.List[string]]::new()
        try {
            $script:StatusStub = @{ ok = $true; error = $null; lines = @('slot 1: free', 'slot 2: free') }; $script:StatusDelaySec = 20
            Reset-Case @{ request = 'active' }
            Sync-FakeStatus
            $script:LeaseSnapshotProbeSec = 5000000
            $n0 = $script:LeaseSlotSnapshots.Count
            $o1 = @(Add-LeaseSlotSnapshot -JobId 'jB4c' -Phase 'request')
            $reg = $script:LeaseSlotSnapshots.Count - $n0
            $null = Wait-Until { @(Get-Calls | Where-Object { $_.cmd -eq 'status' }).Count -gt 0 } 4
            $sc = @(Get-Calls | Where-Object { $_.cmd -eq 'status' }).Count
            $l1 = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jB4c *' })
            if ($reg -ne 0 -or $sc -ne 0) { $ctFails.Add("(probe) no probe is started when the watchdog's constructor throws: registered=$reg statusCalls=$sc") }
            if ($l1.Count -ne 1 -or $l1[0] -notmatch ' phase=request slots=unknown\(watchdog unavailable\)$') { $ctFails.Add("(probe) the line is slots=unknown(watchdog unavailable): [$($l1 -join ' || ')]") }
            if ($o1.Count -ne 0) { $ctFails.Add("(probe) the snapshot returns nothing: $($o1.Count)") }
            Update-LeaseSlotSnapshots -Abandon 'test step'
            $script:StatusDelaySec = 0
            Reset-Case @{ request = 'active'; active_after = 0.3 }
            $script:AgeAfter = (Get-Date).AddSeconds(120)
            Sync-FakeStatus
            $admC = Wait-QuietLeaseAdmission -JobId 'jB4w' -Job ([pscustomobject]@{}) -Metadata $meta
            $script:Admission = $admC
            $null = Wait-Until { @(Get-Calls | Where-Object { $_.cmd -eq 'status' }).Count -gt 0 } 3
            $scW = @(Get-Calls | Where-Object { $_.cmd -eq 'status' }).Count
            $lW = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jB4w *' })
            if ($admC.label -cne 'clear' -or $admC.mode -ne 'lease' -or $admC.leaseMinutes -ne 31 -or -not $admC.leaseCoversTimeout) { $ctFails.Add("(wait) the job is admitted on its lease as without the snapshots: $($admC | ConvertTo-Json -Compress -Depth 3)") }
            if ($scW -ne 0 -or $lW.Count -ne 2 -or @($lW | Where-Object { $_ -match ' phase=(request|grant) slots=unknown\(watchdog unavailable\)$' }).Count -ne 2) { $ctFails.Add("(wait) request and grant each log slots=unknown(watchdog unavailable) and start no probe: statusCalls=$scW [$($lW -join ' || ')]") }
            Exit-HeavySlot
            Assert-True ($ctFails.Count -eq 0) "watchdog constructor failure (sol r5 B4): $($ctFails -join ' ;; ')"
            $results.leaseProbeWatchdogCtorFailure = [ordered]@{ probeLine = $l1; waitLines = $lW; admitted = $admC.label }
        }
        finally {
            $script:LeaseSnapshotProbeSec = 30; $script:StatusDelaySec = 0
            Update-LeaseSlotSnapshots -Abandon 'test case end'
        }
    }
    # ------------------------------------------------------------ lease-probe-watchdog-arm-paths (AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r1, sol r5 B4)
    # The remedy's own failure paths. The watchdog is armed BEFORE the probe starts (a wrapper around the agent's own Start-HeavySlotCli reads the
    # snapshot's $dog at the moment of the start) and the started probe is then attached to it, so:
    #   attach - the started probe cannot be attached (its stderr task is null): it is stopped at once, never registered;
    #   start  - the probe fails to start: the armed watchdog is disarmed (a probe attached to it later past its bound is not killed);
    #   early  - the watchdog fires before the attach: the attach kills the probe at once and records the kill;
    #   list   - the registration throws after the attach: the probe is stopped at once (snapshot failed).
    if (Test-Case 'lease-probe-watchdog-arm-paths') {
        $apFails = [System.Collections.Generic.List[string]]::new()
        $script:ApCliReal = ${function:Start-HeavySlotCli}
        $apSavedList = $script:LeaseSlotSnapshots
        $script:ApProcs = [System.Collections.Generic.List[int]]::new()   # pids: the agent disposes the Process objects it stops
        try {
            $script:StatusStub = @{ ok = $true; error = $null; lines = @('slot 1: free', 'slot 2: free') }; $script:StatusDelaySec = 300
            Reset-Case @{ request = 'active' }
            Sync-FakeStatus
            $script:ApMode = 'attach'
            function Start-HeavySlotCli {
                param([string[]]$Arguments, [string]$Python = '', [string]$Script = '')
                $script:ApArmed = Get-Variable -Name dog -Scope 1 -ValueOnly -ErrorAction SilentlyContinue
                if ($script:ApMode -eq 'start') { return [pscustomobject]@{ ok = $false; why = 'unit: start failed' } }
                $c = & $script:ApCliReal @PSBoundParameters
                if ($c.ok) { $script:ApProcs.Add($c.proc.Id) }
                if ($script:ApMode -eq 'attach' -and $c.ok) { return [pscustomobject]@{ ok = $true; proc = $c.proc; out = $c.out; err = $null; started = $c.started; why = $null } }
                return $c
            }
            # attach
            $script:ApArmed = $null; $script:ApProcs.Clear()
            $n0 = $script:LeaseSlotSnapshots.Count
            Add-LeaseSlotSnapshot -JobId 'jB4at' -Phase 'grant'
            $regA = $script:LeaseSlotSnapshots.Count - $n0
            $lA = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jB4at *' })
            $pA = @($script:ApProcs)
            if ($null -eq $script:ApArmed) { $apFails.Add('(attach) a watchdog is armed when the probe starts') }
            if ($regA -ne 0 -or $pA.Count -ne 1 -or -not (Wait-Until { -not (Test-PidAlive $pA[0]) } 3)) { $apFails.Add("(attach) the probe that cannot be attached is stopped at once and not registered: registered=$regA started=$($pA.Count)") }
            if ($lA.Count -ne 1 -or $lA[0] -notmatch ' phase=grant slots=unknown\(watchdog unavailable\)$') { $apFails.Add("(attach) the line is slots=unknown(watchdog unavailable): [$($lA -join ' || ')]") }
            Update-LeaseSlotSnapshots -Abandon 'test step'
            # start
            $script:ApMode = 'start'; $script:ApArmed = $null; $script:LeaseSnapshotProbeSec = 1
            Add-LeaseSlotSnapshot -JobId 'jB4st' -Phase 'grant'
            $dS = $script:ApArmed
            $lS = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jB4st *' })
            if ($lS.Count -ne 1 -or $lS[0] -notmatch ' slots=unknown\(unit: start failed\)$') { $apFails.Add("(start) the start failure is logged as before: [$($lS -join ' || ')]") }
            if ($null -eq $dS) { $apFails.Add('(start) a watchdog is armed when the probe starts') }
            else {
                Start-Sleep -Milliseconds 1500
                $cS = & $script:ApCliReal -Arguments @('status', '--slots', '2')
                if ($cS.ok) { $script:ApProcs.Add($cS.proc.Id) }
                try { $dS.Attach($cS.proc, $cS.out, $cS.err) } catch { $apFails.Add("(start) a disarmed watchdog still takes an attach: $($_.Exception.Message)") }
                $killedS = $cS.proc.WaitForExit(1500)
                if ($killedS -or $dS.KilledAtMs -ge 0) { $apFails.Add("(start) the watchdog of a probe that failed to start is disarmed (it kills nothing past its bound): exited=$killedS killedAtMs=$($dS.KilledAtMs)") }
                if (-not $cS.proc.HasExited) { try { $cS.proc.Kill($true) } catch { } }
            }
            # early
            $dogT = 'MlvApp.Agent.LeaseProbeWatchdog' -as [type]
            $dE = $null
            try { $dE = $dogT::new([System.Diagnostics.Stopwatch]::StartNew(), [long]0) } catch { $apFails.Add("(early) the watchdog is constructed before its probe: $($_.Exception.Message)") }
            if ($null -ne $dE) {
                Start-Sleep -Milliseconds 300
                $cE = & $script:ApCliReal -Arguments @('status', '--slots', '2')
                if ($cE.ok) { $script:ApProcs.Add($cE.proc.Id) }
                try { $dE.Attach($cE.proc, $cE.out, $cE.err) } catch { $apFails.Add("(early) attach: $($_.Exception.Message)") }
                $killedE = $cE.proc.WaitForExit(3000)
                if (-not $killedE -or $dE.KilledAtMs -lt 0) { $apFails.Add("(early) a watchdog that fired before its probe was attached kills it at the attach: exited=$killedE killedAtMs=$($dE.KilledAtMs)") }
                $dE.Disarm()
            }
            # list
            $script:ApMode = 'list'; $script:ApProcs.Clear(); $script:LeaseSnapshotProbeSec = 30
            $script:LeaseSlotSnapshots = [pscustomobject]@{ Count = 0 }
            $script:LeaseSlotSnapshots | Add-Member -MemberType ScriptMethod -Name Add -Value { throw 'unit: registration failed' }
            Add-LeaseSlotSnapshot -JobId 'jB4ls' -Phase 'grant'
            $script:LeaseSlotSnapshots = $apSavedList
            $pL = @($script:ApProcs)
            $lL = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jB4ls *' })
            if ($pL.Count -ne 1 -or -not (Wait-Until { -not (Test-PidAlive $pL[0]) } 3)) { $apFails.Add("(list) a probe whose registration throws is stopped at once: started=$($pL.Count)") }
            if ($lL.Count -ne 1 -or $lL[0] -notmatch ' slots=unknown\(snapshot failed: .*unit: registration failed.*\)$') { $apFails.Add("(list) the line is snapshot failed: [$($lL -join ' || ')]") }
            Assert-True ($apFails.Count -eq 0) "watchdog arm paths (sol r5 B4 remedy): $($apFails -join ' ;; ')"
            $results.leaseProbeWatchdogArmPaths = [ordered]@{ attach = $lA; start = $lS; list = $lL }
        }
        finally {
            $script:LeaseSlotSnapshots = $apSavedList
            Set-Item -LiteralPath Function:\Start-HeavySlotCli -Value $script:ApCliReal
            foreach ($k in @($script:ApProcs)) { Stop-Process -Id $k -Force -ErrorAction SilentlyContinue }
            $script:LeaseSnapshotProbeSec = 30; $script:StatusDelaySec = 0
            Update-LeaseSlotSnapshots -Abandon 'test case end'
        }
    }
    # AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r2 (sol r1 B4-post-start): Process.Start returns a live child and a LATER statement in the launch
    # helper throws. Sol's seam: Get-Date's -Date defaults to 'not-a-date' only while the named helper is on the call stack, with WarningPreference
    # Stop (the failed default binding is a warning, so Stop makes it a throw); the seam records the helper's $p, the child it had started. Each
    # child must be stopped before the failure is reported (RED on 88c907eb: alive, stopCalls=0):
    #   snapshot - Add-LeaseSlotSnapshot (the B4 caller, HeadroomSec 600): not registered, the line names postStartStop=stopped;
    #   direct   - Start-HeavySlotCli: ok=$false, postStartStop=stopped, no proc handed back;
    #   complete - Complete-HeavySlotCli over that start (the release and pre-launch renew callers): done=$false;
    #   renew    - Update-QuietLeaseRenew (the renew caller; the fake's renew hangs 30 s): no renewCall, renewFailures 1;
    #   request  - Start-QuietLeaseRequest, its timestamp after the start (the wrapper waits for a release): ok=$false, its record removed;
    #   status   - Invoke-HeavySlotStatus, its deadline after the start: ok=$false naming postStartStop=stopped.
    if (Test-Case 'lease-start-post-start-failure') {
        $psFails = [System.Collections.Generic.List[string]]::new()
        $script:PsSeamFn = ''
        $script:PsSeamPids = [System.Collections.Generic.List[int]]::new()
        $script:PsStops = 0
        $script:PsStopCliReal = ${function:Stop-HeavySlotCli}
        $script:PsStopAfterReal = ${function:Stop-ProcessAfterStartFailure}   # $null on a base agent
        $psStatusReal = ${function:Invoke-HeavySlotStatus}
        $psSavedList = $script:LeaseSlotSnapshots
        $psSavedWarn = $WarningPreference
        function Stop-HeavySlotCli { param($Call) $script:PsStops++; & $script:PsStopCliReal @PSBoundParameters }
        if ($null -ne $script:PsStopAfterReal) { function Stop-ProcessAfterStartFailure { param($Process) $script:PsStops++; return (& $script:PsStopAfterReal @PSBoundParameters) } }
        function Invoke-PsSeam([string]$Fn, [scriptblock]$Body) {
            # Returns @{ out; threw; pids; alive; stops }: alive is the started children still running 3 s after the helper returned.
            $script:PsSeamFn = $Fn; $script:PsSeamPids.Clear(); $script:PsStops = 0
            $out = $null; $threw = $null
            $PSDefaultParameterValues['Get-Date:Date'] = {
                $f = @(Get-PSCallStack | Where-Object { $_.Command -eq $script:PsSeamFn }) | Select-Object -First 1
                if ($null -eq $f) { return [DateTime]::Now }
                $v = $null
                try { $v = $f.GetFrameVariables()['p'] } catch { }
                if ($null -ne $v -and $v.Value -is [System.Diagnostics.Process]) { try { if (-not $script:PsSeamPids.Contains($v.Value.Id)) { $script:PsSeamPids.Add($v.Value.Id) } } catch { } }
                'not-a-date'
            }
            $WarningPreference = 'Stop'
            try { $out = & $Body 3>$null } catch { $threw = $_.Exception.Message }   # 3>$null: the seam's warning still throws (Stop), unprinted (the -UnitOnly caller parses stdout)
            finally { $PSDefaultParameterValues.Remove('Get-Date:Date'); $WarningPreference = $psSavedWarn; $script:PsSeamFn = '' }
            $pids = @($script:PsSeamPids)
            $null = Wait-Until { @($pids | Where-Object { Test-PidAlive $_ }).Count -eq 0 } 3
            return [pscustomobject]@{ out = $out; threw = $threw; pids = $pids; alive = @($pids | Where-Object { Test-PidAlive $_ }); stops = $script:PsStops }
        }
        function Test-PsStopped([string]$Case, $R) {
            if ($R.pids.Count -ne 1) { $psFails.Add("($Case) the seam fired after Process.Start returned exactly one child: pids=[$($R.pids -join ',')] threw=[$($R.threw)]"); return }
            if ($R.alive.Count -ne 0 -or $R.stops -lt 1) { $psFails.Add("($Case) the started child is stopped before the failure is reported: alive=[$($R.alive -join ',')] stopCalls=$($R.stops)") }
        }
        try {
            $script:StatusStub = @{ ok = $true; error = $null; lines = @('slot 1: free', 'slot 2: free') }; $script:StatusDelaySec = 300
            Reset-Case @{ request = 'active' }
            Sync-FakeStatus
            # snapshot
            $n0 = $script:LeaseSlotSnapshots.Count
            $rS = Invoke-PsSeam 'Start-HeavySlotCli' { Add-LeaseSlotSnapshot -JobId 'jPsSn' -Phase 'grant' -HeadroomSec 600 }
            $regS = $script:LeaseSlotSnapshots.Count - $n0
            $lS = @($script:Log | Where-Object { $_ -like 'quiet-lease: slots-snapshot job=jPsSn *' })
            Test-PsStopped 'snapshot' $rS
            if ($regS -ne 0) { $psFails.Add("(snapshot) a probe whose start failed is not registered: registered=$regS") }
            if ($lS.Count -ne 1 -or $lS[0] -notmatch ' phase=grant slots=unknown\(heavy-slot\.py status --slots \d+ failed to start: .*postStartStop=stopped\)\)?$') { $psFails.Add("(snapshot) the line names postStartStop=stopped: [$($lS -join ' || ')]") }
            # direct
            $rD = Invoke-PsSeam 'Start-HeavySlotCli' { Start-HeavySlotCli -Arguments @('status', '--slots', '2') }
            Test-PsStopped 'direct' $rD
            if ($null -eq $rD.out -or $rD.out.ok -ne $false -or [string]$rD.out.postStartStop -cne 'stopped' -or $null -ne $rD.out.proc -or [string]$rD.out.why -notmatch 'postStartStop=stopped') { $psFails.Add("(direct) ok=False postStartStop=stopped and no proc: ok=$($rD.out.ok) postStartStop=$($rD.out.postStartStop) proc=$($null -ne $rD.out.proc) why=[$($rD.out.why)]") }
            # complete
            $rC = Invoke-PsSeam 'Start-HeavySlotCli' { Complete-HeavySlotCli -Call (Start-HeavySlotCli -Arguments @('status', '--slots', '2')) -TimeoutSec 5 }
            Test-PsStopped 'complete' $rC
            if ($null -eq $rC.out -or $rC.out.done -ne $false) { $psFails.Add("(complete) done=False: [$($rC.out | ConvertTo-Json -Compress)] threw=[$($rC.threw)]") }
            # renew
            Reset-Case @{ request = 'active'; renew_hang = $true }
            $me = [System.Diagnostics.Process]::GetCurrentProcess()
            $lR = [pscustomobject]@{ jobId = 'jPsRn'; label = 'quiet-lease mlv'; proc = $me; active = [pscustomobject]@{ slot = 1 }; renewCall = $null
                lastRenewStart = [DateTime]::Now.AddSeconds(-600); renewMaxGapSec = 0; renewStarts = 0; renewFailures = 0; renewSlow = 0; renewsOk = 0 }
            $rR = Invoke-PsSeam 'Start-HeavySlotCli' { Update-QuietLeaseRenew -Lease $lR }
            Test-PsStopped 'renew' $rR
            if ($null -ne $lR.renewCall -or $lR.renewFailures -ne 1) { $psFails.Add("(renew) a failed start is a renew failure, never a renew in flight: renewCall=$($null -ne $lR.renewCall) renewFailures=$($lR.renewFailures)") }
            if ($null -ne $lR.renewCall) { try { $lR.renewCall.proc.Kill($true) } catch { } }
            # request
            Reset-Case @{ request = 'active' }
            $recs0 = @(Get-ChildItem -LiteralPath $slotHolders -Filter '*.lease.json' -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
            $rQ = Invoke-PsSeam 'Start-QuietLeaseRequest' { Start-QuietLeaseRequest -JobId 'jPsRq' -Minutes 5 -AdmitTimeoutSec 60 }
            $recsNew = @(Get-ChildItem -LiteralPath $slotHolders -Filter '*.lease.json' -File -ErrorAction SilentlyContinue | Where-Object { $recs0 -notcontains $_.Name })
            Test-PsStopped 'request' $rQ
            if ($null -eq $rQ.out -or $rQ.out.ok -ne $false -or [string]$rQ.out.postStartStop -cne 'stopped' -or [string]$rQ.out.why -notmatch 'postStartStop=stopped') { $psFails.Add("(request) ok=False postStartStop=stopped: ok=$($rQ.out.ok) why=[$($rQ.out.why)] threw=[$($rQ.threw)]") }
            if ($recsNew.Count -ne 0) { $psFails.Add("(request) a stopped wrapper's record is removed: [$($recsNew.Name -join ',')]") }
            foreach ($f in $recsNew) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue }
            # status
            . ([scriptblock]::Create((Get-FunctionBody 'Invoke-HeavySlotStatus')))
            Reset-Case @{ request = 'active' }
            Sync-FakeStatus
            $rT = Invoke-PsSeam 'Invoke-HeavySlotStatus' { Invoke-HeavySlotStatus -HeartbeatActivity 'unit' }
            Test-PsStopped 'status' $rT
            if ($null -eq $rT.out -or $rT.out.ok -ne $false -or [string]$rT.out.error -notmatch 'postStartStop=stopped') { $psFails.Add("(status) ok=False naming postStartStop=stopped: ok=$($rT.out.ok) error=[$($rT.out.error)]") }
            # partial (adversary r2-7778595e A1): Kill(true) ends the root but throws AggregateException for a descendant it could not end;
            # that is stop-failed (the caller keeps the process), never stopped. A fake process: Kill throws, WaitForExit says the root exited.
            if ($null -eq $script:PsStopAfterReal) { $psFails.Add("(partial) Stop-ProcessAfterStartFailure exists") }
            else {
                $fake = [pscustomobject]@{ HasExited = $false }
                $fake | Add-Member -MemberType ScriptMethod -Name Kill -Value { param($t) throw [System.AggregateException]::new('unit: not all processes in the tree could be terminated') }
                $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) return $true }
                $pp = & $script:PsStopAfterReal -Process $fake
                if ([string]$pp -cne 'stop-failed') { $psFails.Add("(partial) a tree Kill that threw AggregateException is stop-failed: got [$pp]") }
            }
            Assert-True ($psFails.Count -eq 0) "post-start failure stops the started child (sol r1 B4-post-start): $($psFails -join ' ;; ')"
            $results.leaseStartPostStartFailure = [ordered]@{ snapshot = $lS; direct = $rD.out.why; request = $rQ.out.why; status = $rT.out.error }
        }
        finally {
            $PSDefaultParameterValues.Remove('Get-Date:Date')
            $WarningPreference = $psSavedWarn
            $script:LeaseSlotSnapshots = $psSavedList
            Set-Item -LiteralPath Function:\Stop-HeavySlotCli -Value $script:PsStopCliReal
            if ($null -ne $script:PsStopAfterReal) { Set-Item -LiteralPath Function:\Stop-ProcessAfterStartFailure -Value $script:PsStopAfterReal }
            if ($null -ne $psStatusReal) { Set-Item -LiteralPath Function:\Invoke-HeavySlotStatus -Value $psStatusReal }
            foreach ($k in @($script:PsSeamPids)) { Stop-Process -Id $k -Force -ErrorAction SilentlyContinue }
            $script:StatusDelaySec = 0
            Update-LeaseSlotSnapshots -Abandon 'test case end'
        }
    }
    # AGENT-LEASE-HEAT-TRACE-BOUND-HARDEN-1 r3 (sol r2b B1): a child that started, then a failed setup, then a cleanup Dispose that throws (a buffered
    # FileStream flushes on Dispose). Controlled objects, not a real child or a real disk fault: a fake Process whose stop is stopped or stop-failed
    # and fake out/err streams, one of whose Dispose throws IOException. One table, {out, err} Dispose throws x {stop-failed, stopped}; every cell:
    #   helper  - Start-WindowlessRedirectedProcess's catch body, replayed from the agent's AST: the exception delivered is the prepared one (never the
    #             Dispose's), postStartStop is present, Data.process is present on stop-failed, and the other disposal still ran;
    #   holder  - Start-HeavySlotHolder RUN over that throw (A:2643): on stop-failed it stops the handed-back holder again and writes its release file;
    #   post    - Start-HeavySlotHolder RUN with a launch whose record setup throws (A:2671): it stops the holder (deleting that call goes RED), keeps
    #             a stop-failed launch with its release file, and disposes both streams and the process of a stopped one, the throwing one included;
    #   job     - Invoke-QueuedJob RUN over that throw (A:4330): on stop-failed it stops the handed-back child again; its stderr names postStartStop.
    # RED on 3d793edb: the Dispose's exception replaced the prepared one (no postStartStop, no Data.process) and skipped the later disposal.
    if (Test-Case 'lease-start-post-start-cleanup-throws') {
        $pcFails = [System.Collections.Generic.List[string]]::new()
        $pcLaunchReal = ${function:Start-WindowlessRedirectedProcess}   # the queued-job cases' stub
        foreach ($name in @('Start-HeavySlotHolder', 'New-HeavySlotSentinelCommand')) { . ([scriptblock]::Create((Get-FunctionBody $name))) }
        $pcSavedAgentProcess = $script:AgentProcess
        $script:AgentProcess = [System.Diagnostics.Process]::GetCurrentProcess()   # the sentinel command reads the agent's start time (never run here)
        $pcFn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-WindowlessRedirectedProcess' }, $true)
        $pcTry = $pcFn.Body.Find({ param($n) $n -is [System.Management.Automation.Language.TryStatementAst] }, $true)
        Assert-True ($null -ne $pcTry -and $pcTry.CatchClauses.Count -eq 1) "Start-WindowlessRedirectedProcess has one try with one catch"
        $script:PcReplay = [scriptblock]::Create("param(`$process, `$outStream, `$errStream, [bool]`$started)`ntry { throw 'unit: the pipe copy failed after the start' }`ncatch $($pcTry.CatchClauses[0].Body.Extent.Text)")
        function New-PcFake([string]$Stop, [string]$Throws, [string]$Id = '') {
            $proc = [pscustomobject]@{ tag = "pc-$([guid]::NewGuid().ToString('N').Substring(0, 8))"; HasExited = $false; Id = $Id; kills = 0; disposed = 0; exits = ($Stop -ceq 'stopped') }
            $proc | Add-Member -MemberType ScriptMethod -Name Kill -Value { param($t) $this.kills++ }
            $proc | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) return [bool]$this.exits }
            $proc | Add-Member -MemberType ScriptMethod -Name Dispose -Value { $this.disposed++ }
            $streams = foreach ($n in @('out', 'err')) {
                $s = [pscustomobject]@{ name = $n; disposed = 0; throws = ($Throws -ceq $n) }
                $s | Add-Member -MemberType ScriptMethod -Name Dispose -Value { $this.disposed++; if ($this.throws) { throw [System.IO.IOException]::new('unit: flush failed') } }
                $s
            }
            return [pscustomobject]@{ proc = $proc; out = $streams[0]; err = $streams[1] }
        }
        function Get-PcDataValue($Exception, [string]$Key) { try { return $Exception.Data[$Key] } catch { return $null } }
        $pcRows = [ordered]@{}
        try {
            foreach ($cell in @(@('out', 'stop-failed'), @('err', 'stop-failed'), @('out', 'stopped'), @('err', 'stopped'))) {
                $throws = $cell[0]; $stop = $cell[1]; $c = "$throws/$stop"; $k = "$($throws.Substring(0, 1))$(if ($stop -ceq 'stopped') { 's' } else { 'f' })"
                # helper
                $f = New-PcFake $stop $throws
                $got = $null
                try { & $script:PcReplay $f.proc $f.out $f.err $true } catch { $got = $_.Exception }
                $gotProc = Get-PcDataValue $got 'process'
                if ($got -isnot [System.InvalidOperationException] -or [string](Get-PcDataValue $got 'postStartStop') -cne $stop) { $pcFails.Add("($c helper) the prepared exception is delivered with postStartStop=${stop}: got $(if ($null -ne $got) { $got.GetType().Name }) [$(if ($null -ne $got) { $got.Message })]") }
                if ($stop -ceq 'stop-failed' -and ($null -eq $gotProc -or [string]$gotProc.tag -cne $f.proc.tag)) { $pcFails.Add("($c helper) Data.process is the undisposed child: present=$($null -ne $gotProc)") }
                if ($stop -ceq 'stopped' -and ($null -ne $gotProc -or $f.proc.disposed -ne 1)) { $pcFails.Add("($c helper) a stopped child is disposed, not handed back: handed=$($null -ne $gotProc) disposed=$($f.proc.disposed)") }
                if ($f.out.disposed -ne 1 -or $f.err.disposed -ne 1) { $pcFails.Add("($c helper) both streams are disposed when one Dispose throws: out=$($f.out.disposed) err=$($f.err.disposed)") }
                if (@(Get-PcDataValue $got 'cleanupErrors') -join ' ' -notmatch 'flush failed') { $pcFails.Add("($c helper) the Dispose failure is appended as Data.cleanupErrors: [$(@(Get-PcDataValue $got 'cleanupErrors') -join ' | ')]") }
                # holder (A:2643)
                $script:PcF = New-PcFake $stop $throws
                function Start-WindowlessRedirectedProcess { param([string]$FilePath, [string[]]$ArgumentList, [string]$OutFile, [string]$ErrFile, [string]$WorkingDirectory, [hashtable]$Environment) & $script:PcReplay $script:PcF.proc $script:PcF.out $script:PcF.err $true }
                $h = Start-HeavySlotHolder -JobId "jPcH$k" -Label 'unit pc'
                $hRel = @(Get-ChildItem -LiteralPath $fx -Filter "jPcH$k.*.release" -File -ErrorAction SilentlyContinue)
                if ($null -eq $h -or $h.ok -ne $false) { $pcFails.Add("($c holder) ok=False: $($h | ConvertTo-Json -Compress -Depth 2)") }
                if ($stop -ceq 'stop-failed' -and ($script:PcF.proc.kills -ne 2 -or $hRel.Count -ne 1)) { $pcFails.Add("($c holder) the handed-back holder is stopped again and its release file written: kills=$($script:PcF.proc.kills) release=$($hRel.Count) why=[$($h.why)]") }
                if ($stop -ceq 'stopped' -and ($script:PcF.proc.kills -ne 1 -or [string]$h.why -notmatch 'postStartStop=stopped')) { $pcFails.Add("($c holder) a stopped holder is reported stopped: kills=$($script:PcF.proc.kills) why=[$($h.why)]") }
                # post (A:2671): the launch returned; Get-ProcessIdentity's [int] binding of a non-numeric pid throws inside the record setup.
                $script:PcF = New-PcFake $stop $throws 'unit-not-a-pid'
                function Start-WindowlessRedirectedProcess { param([string]$FilePath, [string[]]$ArgumentList, [string]$OutFile, [string]$ErrFile, [string]$WorkingDirectory, [hashtable]$Environment) return [pscustomobject]@{ Process = $script:PcF.proc; OutStream = $script:PcF.out; ErrStream = $script:PcF.err } }
                $hp = Start-HeavySlotHolder -JobId "jPcP$k" -Label 'unit pc'
                $pRel = @(Get-ChildItem -LiteralPath $fx -Filter "jPcP$k.*.release" -File -ErrorAction SilentlyContinue)
                $pp = $script:PcF
                if ($null -eq $hp -or $hp.ok -ne $false -or [string]$hp.postStartStop -cne $stop -or $pp.proc.kills -ne 1) { $pcFails.Add("($c post) the holder is stopped once and reported postStartStop=${stop}: ok=$($hp.ok) postStartStop=$($hp.postStartStop) kills=$($pp.proc.kills)") }
                if ($stop -ceq 'stop-failed' -and ($null -eq $hp.launch -or [string]$hp.launch.Process.tag -cne $pp.proc.tag -or $pRel.Count -ne 1 -or $pp.out.disposed + $pp.err.disposed + $pp.proc.disposed -ne 0)) { $pcFails.Add("($c post) a stop-failed holder is kept undisposed with its release file: launch=$($null -ne $hp.launch) release=$($pRel.Count) disposed=$($pp.out.disposed)/$($pp.err.disposed)/$($pp.proc.disposed)") }
                if ($stop -ceq 'stopped' -and ($null -ne $hp.launch -or $pp.out.disposed -ne 1 -or $pp.err.disposed -ne 1 -or $pp.proc.disposed -ne 1)) { $pcFails.Add("($c post) a stopped holder's streams and process are all disposed: launch=$($null -ne $hp.launch) disposed=$($pp.out.disposed)/$($pp.err.disposed)/$($pp.proc.disposed)") }
                # job (A:4330)
                $job = Reset-QueuedJobCase @{ request = 'active' } "jPcJ$k" (New-NotAfterMetaJson 600)
                $script:PcF = New-PcFake $stop $throws
                function Start-WindowlessRedirectedProcess { param([string]$FilePath, [string[]]$ArgumentList, [string]$OutFile, [string]$ErrFile, [string]$WorkingDirectory, [hashtable]$Environment) $script:QJ.launches.Add("$FilePath $($ArgumentList -join ' ')"); & $script:PcReplay $script:PcF.proc $script:PcF.out $script:PcF.err $true }
                try { $null = Invoke-QueuedJob -Job $job } finally { Exit-HeavySlot }
                $jp = @($script:QJ.published)
                if ($script:QJ.launches.Count -ne 1 -or $jp.Count -ne 1 -or [string]$jp[0].stderr -notmatch "postStartStop=$stop") { $pcFails.Add("($c job) the launch is reached once and its stderr names postStartStop=${stop}: launches=$($script:QJ.launches.Count) stderr=[$(if ($jp.Count -gt 0) { $jp[0].stderr })]") }
                if ($stop -ceq 'stop-failed' -and ($script:PcF.proc.kills -ne 2 -or $script:PcF.proc.disposed -ne 1)) { $pcFails.Add("($c job) the handed-back child is stopped again and then disposed: kills=$($script:PcF.proc.kills) disposed=$($script:PcF.proc.disposed)") }
                if ($stop -ceq 'stopped' -and $script:PcF.proc.kills -ne 1) { $pcFails.Add("($c job) a stopped child is not stopped again: kills=$($script:PcF.proc.kills)") }
                $pcRows[$c] = [ordered]@{ delivered = $(if ($null -ne $got) { $got.GetType().Name }); postStartStop = (Get-PcDataValue $got 'postStartStop'); holderWhy = $h.why; post = $hp.postStartStop }
            }
            Assert-True ($pcFails.Count -eq 0) "post-start handoff survives a throwing cleanup Dispose (sol r2b B1): $($pcFails -join ' ;; ')"
            $results.leaseStartPostStartCleanupThrows = $pcRows
        }
        finally {
            Set-Item -LiteralPath Function:\Start-WindowlessRedirectedProcess -Value $pcLaunchReal
            $script:AgentProcess = $pcSavedAgentProcess
            foreach ($f2 in @(Get-ChildItem -LiteralPath $fx -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'jPc[HP]*' })) { Remove-Item -LiteralPath $f2.FullName -Force -ErrorAction SilentlyContinue }
        }
    }
    if ($Only.Count -gt 0) { $results.only = @($Only) }
}
finally {
    $parkedL = if ($null -ne $script:LeaseBatch) { $script:LeaseBatch.lease } else { $null }
    foreach ($l2 in @($script:QuietLease, $script:QuietLeaseWaiting, $parkedL)) { if ($null -ne $l2) { try { $null = Stop-QuietLease -Lease $l2 -Why 'test cleanup' } catch { } } }
    Remove-Item -LiteralPath $fx -Recurse -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
