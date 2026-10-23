[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = '',
    # The interpreter that runs the FAKE heavy-slot.py below (never the real one: it lives only on Bachelor).
    [string]$Python = $(if ($env:MLV_HEAVY_SLOT_PYTHON) { $env:MLV_HEAVY_SLOT_PYTHON } else { '' })
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
function Add-AgentLogLine([string]$Message) { $script:Log.Add($Message) }
function Write-AgentHeartbeat([string]$Activity = '') { $script:Beats.Add($Activity) }
function Get-QueueAgeSeconds { param($Job, $Metadata) return $script:Age }
function Invoke-ExemptJobsDuringWait { param([string]$WaitingJobId, [double]$WaitedSec) $script:Exempt++; return 0 }
function Read-HeavySlotFeedMetrics { return @{ metrics = $null; why = 'feed missing (unit)' } }

function Set-Fake([hashtable]$Control) { [IO.File]::WriteAllText((Join-Path $fx 'fake-control.json'), ($Control | ConvertTo-Json -Compress)) }
function Get-Calls { $p = Join-Path $fx 'calls.jsonl'; if (-not (Test-Path -LiteralPath $p)) { return @() }; return @(Get-Content -LiteralPath $p | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json }) }
function Reset-Case([hashtable]$Control) {
    Set-Fake $Control
    Remove-Item -LiteralPath (Join-Path $fx 'calls.jsonl'), (Join-Path $fx 'quiet-lease.json') -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $fx -Filter 'release-*' -File | Remove-Item -Force
    $script:Log.Clear(); $script:Beats.Clear(); $script:Age = 0; $script:Exempt = 0
    $script:QuietLease = $null; $script:QuietLeaseWaiting = $null; $script:Admission = $null
}
function Test-PidAlive([int]$ProcessId) { return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) }
function Wait-Until([scriptblock]$Cond, [double]$TimeoutSec) { $dl = (Get-Date).AddSeconds($TimeoutSec); while (-not (& $Cond) -and (Get-Date) -lt $dl) { Start-Sleep -Milliseconds 100 }; return [bool](& $Cond) }
$meta = [pscustomobject]@{ timeoutSec = 1800; maxQueueAgeSec = 600 }

try {
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
    Reset-Case @{ request = 'hang' }
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
finally {
    foreach ($l2 in @($script:QuietLease, $script:QuietLeaseWaiting)) { if ($null -ne $l2) { try { $null = Stop-QuietLease -Lease $l2 -Why 'test cleanup' } catch { } } }
    Remove-Item -LiteralPath $fx -Recurse -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
