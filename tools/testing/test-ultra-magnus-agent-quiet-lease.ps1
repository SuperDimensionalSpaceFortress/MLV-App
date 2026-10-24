[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = '',
    # The interpreter that runs the FAKE heavy-slot.py below (never the real one: it lives only on Bachelor).
    [string]$Python = $(if ($env:MLV_HEAVY_SLOT_PYTHON) { $env:MLV_HEAVY_SLOT_PYTHON } else { '' }),
    # Runs only the named not-after cases (empty = every case, the original ones included), so each can be run alone for its RED.
    [string[]]$Only = @()
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
$liftNotAfter = @('Get-JobNotAfterVerdict', 'Get-QuietLeaseAdmitTimeout', 'New-LeaseReleaseRecord', 'Publish-NotAfterExpiredResult')
$liftMissing = [System.Collections.Generic.List[string]]::new()
foreach ($name in $liftNotAfter) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if ($null -eq $fn) { $liftMissing.Add($name) } else { . ([scriptblock]::Create($fn.Extent.Text)) }
}
# lease-batch r4 (the merge of not-after r1): the same, so batch-refusal-ends-batch fails on behaviour against an agent without it.
foreach ($name in @('Close-LeaseBatchAfterRelease')) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if ($null -eq $fn) { $liftMissing.Add($name) } else { . ([scriptblock]::Create($fn.Extent.Text)) }
}
$paramDefaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) { if ($p.DefaultValue -is [System.Management.Automation.Language.ConstantExpressionAst]) { $paramDefaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value } }

$results = [ordered]@{}
$fx = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-quiet-lease-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $fx, (Join-Path $fx 'cancel'), (Join-Path $fx 'inbox') | Out-Null

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
    with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(k) + "\n")
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
        until = time.time() + 60 * float(a[a.index("--minutes") + 1])
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
function Add-AgentLogLine([string]$Message) { $script:Log.Add($Message); if ($null -ne $script:LogHook) { & $script:LogHook $Message } }
function Write-AgentHeartbeat([string]$Activity = '') { $script:Beats.Add($Activity) }
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
    if ($null -ne $script:LeaseBatch -and (Get-Command Close-LeaseBatch -ErrorAction SilentlyContinue)) { Close-LeaseBatch -Reason 'test-reset' }
    $script:LeaseBatch = $null; $script:LeaseBatchKeep = $false
    Set-Fake $Control
    Remove-Item -LiteralPath (Join-Path $fx 'calls.jsonl'), (Join-Path $fx 'quiet-lease.json'), (Join-Path $fx 'last-lease-end.txt') -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $fx -Filter 'lease-batch-*.jsonl' -File | Remove-Item -Force
    Get-ChildItem -LiteralPath $fx -Filter 'release-*' -File | Remove-Item -Force
    $script:Log.Clear(); $script:Beats.Clear(); $script:Age = 0; $script:Exempt = 0
    $script:QuietLease = $null; $script:QuietLeaseWaiting = $null; $script:Admission = $null
    $script:LogHook = $null; $script:HookFired = $false
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
    function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter) { return 9999 } return 0 }
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
        'notafter-absent-unchanged', 'notafter-pure', 'notafter-source-shape', 'batch-open-notafter', 'batch-refusal-ends-batch')
    foreach ($o in $Only) { Assert-True ($notAfterCases -contains $o) "-Only names a known case: '$o' (known: $($notAfterCases -join ', '))" }
    function Test-Case([string]$Name) { return ($Only.Count -eq 0 -or $Only -contains $Name) }
    # Hang guard: an agent that ignores notAfterUtc would wait these cases out; the queue ceiling then ends them queue-expired
    # (a FAIL on the label assert), never a hang.
    function Get-QueueAgeSeconds { param($Job, $Metadata) if ((Get-Date) -ge $script:AgeAfter) { return 9999 } return 0 }
    function Get-FakeFlag { try { return ([IO.File]::ReadAllText((Join-Path $fx 'quiet-lease.json')) | ConvertFrom-Json) } catch { return $null } }
    function New-NotAfterMeta([double]$InSec) { return [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 600; notAfterUtc = [DateTime]::UtcNow.AddSeconds($InSec).ToString('o') } }
    function Get-RequestArg($Call, [string]$Flag) { $a = @($Call.argv); $i = [Array]::IndexOf($a, $Flag); if ($i -lt 0) { return $null }; return $a[$i + 1] }
    function Get-Released { return @(Get-Calls | Where-Object { $_.cmd -eq 'release' }) }

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
        $post = $qj.Substring($iGate, $iMarker - $iGate)
        Assert-True (([regex]::Matches($post, [regex]::Escape('$rel = Stop-QuietLease -Lease $script:QuietLease'))).Count -eq 2 -and ([regex]::Matches($post, [regex]::Escape('-Phase "post-admission" -Release $rel'))).Count -eq 2) "notafter-source-shape: the post-admission cancel and not-after refusals each release the lease and record it"
        Assert-True (([regex]::Matches($qj, [regex]::Escape('Complete-AdmissionWaitMarker -JobId $jobId -Status "expired-not-after"'))).Count -eq 3) "notafter-source-shape: all three not-after exits retire the claim as expired-not-after"
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
        $results.notAfterSourceShape = [ordered]@{ order = 'pre-gate < gate < expired-not-after < post-admission re-check < launching marker' }
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
        $refusalLines = @(($qjR -split "`r?`n") | Where-Object { $_ -match '^\s*if \(\$null -ne \$script:QuietLease\) \{ \$rel = Stop-QuietLease -Lease \$script:QuietLease -Why "(cancel file present|not-after passed) after admission, before launch"' } | ForEach-Object { $_.Trim() })
        Assert-True ($refusalLines.Count -eq 2) "batch-refusal-ends-batch: Invoke-QueuedJob has the post-admission cancel and not-after release lines (found $($refusalLines.Count))"
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
    if ($Only.Count -gt 0) { $results.only = @($Only) }
}
finally {
    $parkedL = if ($null -ne $script:LeaseBatch) { $script:LeaseBatch.lease } else { $null }
    foreach ($l2 in @($script:QuietLease, $script:QuietLeaseWaiting, $parkedL)) { if ($null -ne $l2) { try { $null = Stop-QuietLease -Lease $l2 -Why 'test cleanup' } catch { } } }
    Remove-Item -LiteralPath $fx -Recurse -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
