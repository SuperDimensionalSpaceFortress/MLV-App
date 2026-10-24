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
#   cpu       - the legs' <= 20 % CPU quiescence check is kept, and the job launches only after the lease is active
#   status    - -HeavySlotMode status / heavy-slot-mode.txt keep the r1c holder path (its own suite stays green)
#   not-after - LEASE-REQUEST-CHILD-NOT-AFTER-1 r1, meta.json notAfterUtc (cases notafter-*, cancel-active-race; -Only runs one):
#               past it no request is made, a request's --admit-timeout never outlives it, a pending request is stopped when
#               it passes, and a lease that turns active after it (or after a cancel file) is released before the job gets it
#   not-after hardening - LEASE-NOT-AFTER-HARDENING-1 r1 (cases notafter-queuedjob-*, notafter-prelaunch-*, notafter-renew-in-light-job,
#               notafter-meta-unreadable): Invoke-QueuedJob's not-after exits RUN (pre-gate, gate label, post-admission, and the
#               last check before the child is created), each refusing, releasing and retiring; a waiting lease is not renewed
#               past the deadline from inside a light job, and its true phase is recorded; a meta.json that may carry a deadline
#               but cannot be read fails closed. Invoke-QueuedJob is lifted and driven with its disk and launch steps stubbed.
#   not-after hardening 2 - LEASE-NOT-AFTER-HARDENING-2 r1 (cases queuedjob-cancel-post-admission, notafter-renew-crosses-deadline,
#               notafter-meta-sticky): the cancel exit after admission RUNS (released and recorded before Exit-HeavySlot); a
#               renew in flight as the deadline passes is still collected or killed; Get-JobMetadata's deadline flag holds over
#               its reads and matches the key in any case.
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
# not-after r1: lifted when present. A missing one is recorded, not thrown, so the cases above still run against a base agent
# and each not-after case fails on behaviour (or on "agent defines ..." in notafter-pure).
$liftNotAfter = @('Get-JobNotAfterVerdict', 'Get-QuietLeaseAdmitTimeout', 'New-LeaseReleaseRecord', 'Publish-NotAfterExpiredResult')
$liftMissing = [System.Collections.Generic.List[string]]::new()
foreach ($name in $liftNotAfter) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if ($null -eq $fn) { $liftMissing.Add($name) } else { . ([scriptblock]::Create($fn.Extent.Text)) }
}
$paramDefaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) { if ($p.DefaultValue -is [System.Management.Automation.Language.ConstantExpressionAst]) { $paramDefaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value } }

$results = [ordered]@{}
$fx = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-quiet-lease-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $fx, (Join-Path $fx 'cancel') | Out-Null

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
$heavySlotModeFile = Join-Path $fx 'heavy-slot-mode.txt'
$HeavySlotMode = [string]$paramDefaults['HeavySlotMode']
$LeaseMaxMinutes = [int]$paramDefaults['LeaseMaxMinutes']
$LeasePendingMaxSec = 1
$LeaseRenewSec = 1
$LeaseRenewTimeoutSec = 3
$LeaseRetrySec = 1
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
    Set-Fake $Control
    Remove-Item -LiteralPath (Join-Path $fx 'calls.jsonl'), (Join-Path $fx 'quiet-lease.json') -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $fx -Filter 'release-*' -File | Remove-Item -Force
    $script:Log.Clear(); $script:Beats.Clear(); $script:Age = 0; $script:Exempt = 0
    $script:QuietLease = $null; $script:QuietLeaseWaiting = $null; $script:Admission = $null
    $script:LogHook = $null; $script:HookFired = $false; $script:BeatHook = $null
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
        'notafter-absent-unchanged', 'notafter-pure', 'notafter-source-shape',
        'notafter-queuedjob-pregate', 'notafter-queuedjob-gate', 'notafter-queuedjob-post-admission', 'notafter-prelaunch-marker',
        'notafter-prelaunch-confirm', 'notafter-renew-in-light-job', 'notafter-meta-unreadable',
        'queuedjob-cancel-post-admission', 'notafter-renew-crosses-deadline', 'notafter-meta-sticky')
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
        # LEASE-NOT-AFTER-HARDENING-1: what each exit DOES (refuse, release, retire) is run by the notafter-queuedjob-* and
        # notafter-prelaunch-* cases; only where the last check sits is read from the source here.
        $iConfirm = $qj.IndexOf('$pre = Confirm-HeavySlotHeldForLaunch -JobId $jobId')
        $iLaunch = $qj.IndexOf('$launch = Start-WindowlessRedirectedProcess')
        $iLast = if ($iConfirm -ge 0) { $qj.IndexOf($probe, $iConfirm) } else { -1 }
        Assert-True ($iMarker -lt $iConfirm -and $iLast -gt $iConfirm -and $iLast -lt $iLaunch) "notafter-source-shape: the last re-check follows the launching marker and the slot re-confirm and precedes the child ($iMarker < $iConfirm < $iLast < $iLaunch)"
        $exitBody = (Get-FunctionBody 'Exit-HeavySlot') -replace '\s+', ' '
        $exitWant = @'
function Exit-HeavySlot {
    # Ends the admitted job's holder (status mode) or quiet lease (lease mode), so heavy-slot.py releases the job's slot.
    # Idempotent. Called on every path out of a job: normal end, a thrown leg, a pre-launch refusal, and agent exit.
    $h = $script:HeavySlotHolder
    $script:HeavySlotHolder = $null
    if ($null -ne $h) { Stop-HeavySlotHolder -Holder $h -Why "job done" }
    $l = $script:QuietLease
    $script:QuietLease = $null
    if ($null -ne $l) { $null = Stop-QuietLease -Lease $l -Why "job done" }
}
'@ -replace '\s+', ' '
        Assert-True ($exitBody.Trim() -ceq $exitWant.Trim()) "notafter-source-shape: Exit-HeavySlot is unchanged (it returns nothing, so its bare calls leak no output)"
        Assert-True (-not (Get-FunctionBody 'Wait-HeavySlotAdmission').Contains('NotAfter')) "notafter-source-shape: status mode's wait loop is unchanged (Invoke-QueuedJob's checks cover it)"
        $results.notAfterSourceShape = [ordered]@{ order = 'pre-gate < gate < expired-not-after < post-admission re-check < launching marker < slot re-confirm < last re-check < child' }
    }

    # ================================================================ LEASE-NOT-AFTER-HARDENING-1 r1
    # Invoke-QueuedJob, lifted and RUN against the fake heavy-slot.py. Its disk and launch steps are stubbed: the claim and the
    # launching marker are one-field files under running\, the result and the artifact move are recorded, and the child launch
    # is recorded and throws (no child is ever created). The REAL Get-JobMetadata reads a meta.json written to inbox\; the
    # wrapper keeps the object it returned, so a case moves that job's notAfterUtc into the past at a chosen step, which is
    # what the verdict sees when the clock passes the deadline there.
    $qjCases = @('notafter-queuedjob-pregate', 'notafter-queuedjob-gate', 'notafter-queuedjob-post-admission', 'notafter-prelaunch-marker', 'notafter-prelaunch-confirm', 'notafter-meta-unreadable',
        'queuedjob-cancel-post-admission', 'notafter-meta-sticky')
    if (@($qjCases | Where-Object { Test-Case $_ }).Count -gt 0) {
        # STAGE-JOB-LEASE-EXEMPT-1 r2: Invoke-QueuedJob classifies the job through Get-GateExemptClass, which reads both patterns.
        foreach ($name in @('Invoke-QueuedJob', 'Complete-AdmissionWaitMarker', 'Publish-GateUnlaunchedResult', 'Get-GateExemptClass')) { . ([scriptblock]::Create((Get-FunctionBody $name))) }
        . ([scriptblock]::Create(((Get-FunctionBody 'Get-JobMetadata') -replace '^function Get-JobMetadata', 'function Get-JobMetadataReal')))
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
        function Invoke-QueueExpiredJob { param($Job, $Metadata, [int]$QueueAgeSec) $script:QJ.queueExpired++ }
        function Add-HeavySlotHolderLossToAdmission { param([string]$JobId, $Admission) if ($null -ne $script:QJ.admittedHook) { & $script:QJ.admittedHook } }
        function Add-HeavySlotHolderLoss { param([string]$JobId, [string]$Phase, [string]$Why, [string]$Held, [string]$Reason) $script:QJ.holderLoss.Add("$Phase|$Reason|$Why") }
        function Set-AgentKeepAwake { param([bool]$Enable) return $true }
        function Start-WindowlessRedirectedProcess {
            param([string]$FilePath, [string[]]$ArgumentList, [string]$OutFile, [string]$ErrFile, [string]$WorkingDirectory)
            $script:QJ.launches.Add("$FilePath $($ArgumentList -join ' ')")
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
                    @{ n = 'hang'; fake = @{ request = 'active'; active_after = 2; renew_hang = $true }; timeout = 3; window = 6 },
                    @{ n = 'slow'; fake = @{ request = 'active'; active_after = 2; renew_sleep = 1.5 }; timeout = 15; window = 6 })) {
                Reset-Case $v.fake
                $LeaseRenewTimeoutSec = $v.timeout
                $script:LeaseNotAfterMinAdmitSec = 1
                $script:AgeAfter = (Get-Date).AddSeconds(60)
                $script:CrossMeta = New-NotAfterMeta 600
                $script:CrossWindow = $v.window
                $script:Cross = @{ ran = 0; inFlight = $false; renewPid = 0; startsAtDeadline = -1; ticksAfter = 0; renewCallLeft = $null; renewAlive = $null; startsAfter = $null; renewsOk = $null; renewSlow = $null; renewCalls = $null }
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
                    # The deadline passes with that renew in flight (the tick reads the lease's copy, the wait the metadata's).
                    $past = [DateTime]::UtcNow.AddSeconds(-1).ToString('o')
                    $w | Add-Member -NotePropertyName notAfterUtc -NotePropertyValue $past -Force
                    $script:CrossMeta | Add-Member -NotePropertyName notAfterUtc -NotePropertyValue $past -Force
                    $end = (Get-Date).AddSeconds($script:CrossWindow)
                    while ((Get-Date) -lt $end) { Invoke-QuietLeaseRenewTick; $c.ticksAfter++; Start-Sleep -Milliseconds 100 }
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
        Assert-True (@($crossLog['hang'] | Where-Object { $_ -like 'quiet-lease: renew-slow job=jXhang killed after 3s*' }).Count -eq 1) "notafter-renew-crosses-deadline (hang): typed renew-slow once"
        Assert-True (@($crossLog['slow'] | Where-Object { $_ -like 'quiet-lease: not-after passed job=jXslow * active=True while waiting; no more renews*' }).Count -eq 1) "notafter-renew-crosses-deadline (slow): the stop is typed once"
        $results.notAfterRenewCrossesDeadline = [ordered]@{ hang = [ordered]@{ renewSlow = $h.renewSlow; renewCallLeft = $h.renewCallLeft; renewAlive = $h.renewAlive }; slow = [ordered]@{ renewsOk = $s.renewsOk; renewCallLeft = $s.renewCallLeft } }
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
    if ($Only.Count -gt 0) { $results.only = @($Only) }
}
finally {
    foreach ($l2 in @($script:QuietLease, $script:QuietLeaseWaiting)) { if ($null -ne $l2) { try { $null = Stop-QuietLease -Lease $l2 -Why 'test cleanup' } catch { } } }
    Remove-Item -LiteralPath $fx -Recurse -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
