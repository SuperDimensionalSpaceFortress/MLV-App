[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = '',
    # The interpreter that runs the FAKE heavy-slot.py below (never the real one: it lives only on Bachelor).
    [string]$Python = $(if ($env:MLV_HEAVY_SLOT_PYTHON) { $env:MLV_HEAVY_SLOT_PYTHON } else { '' }),
    # Runs only the named cases (empty = every case), so each can be run alone for its RED.
    [string[]]$Only = @()
)

# STAGE-JOB-LEASE-EXEMPT-1 r1: build-artifact STAGING jobs skip the quiet lease (tools/profiling/bachelor/ultra-magnus-agent.ps1,
# GateStageJobPattern). On 2026-10-24 playback-attr-3-cuda-stage-8ea38dff267b, a ~4.5 s copy-and-hash step, waited 6889 s over
# 4 lease requests for an exclusive quiet lease of up to 45 min. The agent's REAL Invoke-QueuedJob, gate wait and during-wait
# dispatcher are lifted from its AST and driven against a FAKE heavy-slot.py that records every call and refuses every lease
# request (exit 75). The claim, the launching marker and the result writers are stubbed: the stubbed marker records what WOULD
# launch and returns nothing, so no child is ever created. No agent loop, no share, no venue, no playback.
#   classify       - Get-GateExemptClass: the three staging families are "stage", the light ids stay "light", and every
#                    measured id (one carrying "stage" outside the anchored family included) is gated
#   stage-ungated  - each staging family reaches the launch with ZERO heavy-slot.py calls, no claim, admission exempt-light
#   stage-gated    - a measured id that carries "stage" elsewhere, a footage stage and a build still request a lease
#   light-ungated  - venue-quiet-probe-*, agent-swap-*, dve-*-health still reach the launch with zero heavy-slot.py calls
#   stage-cancel   - a staging job with a cancel file is published cancelled-in-gate and never launched
#   stage-notafter - a staging job past its notAfterUtc is published expired and never launched
#   stage-not-during-wait - while a measured job waits on its lease, the light job behind it runs and the staging job does not
# Prints one JSON object with status PASS, or throws.

$ErrorActionPreference = 'Stop'
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }
$caseNames = @('classify', 'stage-ungated', 'stage-gated', 'light-ungated', 'stage-cancel', 'stage-notafter', 'stage-not-during-wait')
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($n in $Only) { if ($caseNames -notcontains $n) { throw "unknown case '$n' (known: $($caseNames -join ', '))" } }
function Test-Case([string]$Name) { return ($Only.Count -eq 0 -or $Only -contains $Name) }

if (-not $Python) {
    try { $Python = (& py -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim() } catch { $Python = '' }
}
if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1).Source }
Assert-True ($Python -and (Test-Path -LiteralPath $Python -PathType Leaf)) "a python interpreter for the fake heavy-slot.py (pass -Python or set MLV_HEAVY_SLOT_PYTHON); got '$Python'"

$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) "agent parses ($($parseErrors.Count) errors)"
function Find-Function([string]$Name) { return $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true) }
$lift = @('Resolve-HeavySlotPython', 'Get-ProcessIdentity', 'Test-EpochMillisClose', 'Test-ProcessIdentityMatch', 'Get-DescendantProcessIdentities',
    'Stop-ProcessTree', 'Move-FileChecked', 'Write-JsonFileAtomic', 'Read-JsonFile', 'Get-SustainedHeatVerdict', 'Stop-HeavySlotHolder', 'Exit-HeavySlot',
    'Resolve-HeavySlotMode', 'Get-QuietLeaseMinutes', 'ConvertFrom-QuietLeaseActiveLine', 'Start-HeavySlotCli', 'Receive-HeavySlotCli', 'Stop-HeavySlotCli',
    'Complete-HeavySlotCli', 'Start-QuietLeaseRequest', 'Test-QuietLeaseOwnerPid', 'Update-QuietLeaseRequest', 'Stop-QuietLease', 'Wait-QuietLeaseAdmission',
    'Get-JobNotAfterVerdict', 'Get-QuietLeaseAdmitTimeout', 'New-LeaseReleaseRecord', 'Invoke-ExemptJobsDuringWait', 'Invoke-QueuedJob', 'Read-JobNotAfterSidecar')
foreach ($name in $lift) { $fn = Find-Function $name; Assert-True ($null -ne $fn) "agent defines $name"; . ([scriptblock]::Create($fn.Extent.Text)) }
# The function under change: lifted when present, so a base agent runs every behavioural case and fails each on behaviour.
$classifyFn = Find-Function 'Get-GateExemptClass'
if ($null -ne $classifyFn) { . ([scriptblock]::Create($classifyFn.Extent.Text)) }
$paramDefaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) { if ($p.DefaultValue -is [System.Management.Automation.Language.ConstantExpressionAst]) { $paramDefaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value } }
$GateExemptJobPattern = [string]$paramDefaults['GateExemptJobPattern']
$GateStageJobPattern = $paramDefaults['GateStageJobPattern']

$results = [ordered]@{}
$fx = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-stage-exempt-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $fx, (Join-Path $fx 'cancel'), (Join-Path $fx 'inbox'), (Join-Path $fx 'processed'), (Join-Path $fx 'running') | Out-Null

# ---------------------------------------------------------------- the fake heavy-slot.py: every call recorded, every request refused
$fake = @'
import json, os, sys, time
d = os.environ["HEAVY_SLOT_DIR"]
a = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": a, "pid": os.getpid(), "ts": time.time()}) + "\n")
if a[:2] == ["lease", "request"]:
    print("[heavy-slot] admission refused for 'quiet-lease mlv': another tenant holds the heavy slot (fake)", file=sys.stderr, flush=True)
    sys.exit(75)
if a[:2] in (["lease", "release"], ["lease", "renew"]):
    print(json.dumps({"state": "none"}), flush=True)
    sys.exit(0)
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
$inbox = Join-Path $fx 'inbox'
$processed = Join-Path $fx 'processed'
$running = Join-Path $fx 'running'
$cancel = Join-Path $fx 'cancel'
# LEASE-BATCH-OPEN-RUNNER-LIVENESS-1: Invoke-QueuedJob and the lease wait read notafter\<jobId>.json (none here: absent).
$notAfterDir = Join-Path $fx 'notafter'
$heavySlotModeFile = Join-Path $fx 'heavy-slot-mode.txt'
$HeavySlotMode = [string]$paramDefaults['HeavySlotMode']
$LeaseMaxMinutes = [int]$paramDefaults['LeaseMaxMinutes']
$LeasePendingMaxSec = 1
$LeaseRenewSec = 1
$LeaseRenewTimeoutSec = 3
$LeaseRetrySec = 1
$GateProbeSec = 1
$script:HeavySlotModeExplicit = $false
$script:LastHeavySlotModeKey = $null
$script:GateLoopSleepMs = 100
$script:LeasePendingGraceSec = 120
$script:LeaseNotAfterMinAdmitSec = 30
$script:GateVersion = 'unit'
$script:GateHour = @{ admitted = 0; waitSecTotal = 0; requeued = 0 }
$script:AgentProcessStartUtc = 'unit'
$script:HeavySlotHolder = $null
$script:QuietLease = $null
$script:QuietLeaseWaiting = $null
$script:Admission = $null
$script:ExemptDuringWaitOf = $null
$script:RecoveryQuarantine = @{}
$labelAst = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$script:QuietLeaseLabel' }, $true)
Assert-True ($null -ne $labelAst) "agent assigns `$script:QuietLeaseLabel"
$script:QuietLeaseLabel = $labelAst.Right.Expression.Value

# Stubs for the disk, result and launch steps Invoke-QueuedJob takes. Each records what it was asked to do.
$script:Log = [System.Collections.Generic.List[string]]::new()
$script:Claims = [System.Collections.Generic.List[string]]::new()
$script:Launches = [System.Collections.Generic.List[object]]::new()
$script:Published = [System.Collections.Generic.List[string]]::new()
$script:Meta = @{}
$script:ExpireJob = $null
$script:ExpireWhen = { $false }
$script:AgeAfter = (Get-Date).AddSeconds(60)
function Add-AgentLogLine([string]$Message) { $script:Log.Add($Message) }
function Write-AgentHeartbeat([string]$Activity = '') { }
function Read-HeavySlotFeedMetrics { return @{ metrics = $null; why = 'feed missing (unit)' } }
function Get-JobRecoveryPending { param($JobId) return $null }
function Get-JobMetadata { param([string]$JobId) if ($script:Meta.ContainsKey($JobId)) { return $script:Meta[$JobId] } return [pscustomobject]@{ jobId = $JobId; timeoutSec = 60; maxQueueAgeSec = 600 } }
# The queue ceiling ends a gated case once it has seen what it needs ($script:ExpireWhen), or after 60 s (a hang guard).
function Get-QueueAgeSeconds { param($Job, $Metadata) if ([string]$Metadata.jobId -eq $script:ExpireJob -and ((Get-Date) -ge $script:AgeAfter -or (& $script:ExpireWhen))) { return 9999 } return 0 }
function New-AdmissionWaitMarker { param([string]$JobId, $Job, $Metadata) $script:Claims.Add($JobId); return $true }
function Complete-AdmissionWaitMarker { param([string]$JobId, [string]$Status) }
function Add-HeavySlotHolderLossToAdmission { param([string]$JobId, $Admission) }
function Invoke-QueueExpiredJob { param($Job, $Metadata, [int]$QueueAgeSec) $script:Published.Add("queue-expired|$($Job.BaseName -replace '\.job$', '')") }
function Publish-GateUnlaunchedResult { param($Job, $Metadata, $Admission, [int]$QueueAgeSec) $script:Published.Add("$($Admission.label)|$($Job.BaseName -replace '\.job$', '')") }
function Publish-NotAfterExpiredResult { param($Job, $Metadata, $Admission, [int]$QueueAgeSec, $Verdict) $script:Published.Add("expired-not-after|$($Job.BaseName -replace '\.job$', '')") }
function New-LaunchingMarker {
    param([string]$JobId, $Job, $Metadata, [string]$OutFile, [string]$ErrFile, [string]$StartedUtc, [int]$TimeoutSec)
    # What WOULD launch, with the admission it would launch under; the job file leaves inbox\ as a run job's does.
    $a = $script:Admission
    $script:Launches.Add([pscustomobject]@{ jobId = $JobId; label = $a.label; why = $a.why; waitingFor = $a.waitingFor; leasePending = ($null -ne $script:QuietLeaseWaiting); leaseHeld = ($null -ne $script:QuietLease) })
    if (Test-Path -LiteralPath $Job.FullName -PathType Leaf) { Move-Item -LiteralPath $Job.FullName -Destination $processed }
    return $null
}

function Get-Calls { $p = Join-Path $fx 'calls.jsonl'; if (-not (Test-Path -LiteralPath $p)) { return @() }; return @(Get-Content -LiteralPath $p | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json }) }
function Reset-Case {
    Remove-Item -LiteralPath (Join-Path $fx 'calls.jsonl') -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $inbox, $processed, $cancel -File | Remove-Item -Force
    $script:Log.Clear(); $script:Claims.Clear(); $script:Launches.Clear(); $script:Published.Clear(); $script:Meta = @{}
    $script:QuietLease = $null; $script:QuietLeaseWaiting = $null; $script:Admission = $null; $script:ExemptDuringWaitOf = $null
    $script:ExpireJob = $null; $script:ExpireWhen = { $false }; $script:AgeAfter = (Get-Date).AddSeconds(60)
}
function New-InboxJob([string]$JobId) { $p = Join-Path $inbox "$JobId.job.ps1"; [IO.File]::WriteAllText($p, '# mock: never executed'); return (Get-Item -LiteralPath $p) }
function Get-Requests { return @(Get-Calls | Where-Object { ($_.argv | Select-Object -First 2) -join ' ' -eq 'lease request' }) }

# Job ids, each in the exact grammar its generator emits (the sha is the 2026-10-24 incident's).
$stageIds = [ordered]@{
    attrStage       = 'playback-attr-3-cuda-stage-8ea38dff267b'                         # playback-attr-3-cuda-stage-job.ps1
    attrStageSuffix = 'playback-attr-3-cuda-stage-8ea38dff267b-n202610240519'           # its -JobIdSuffix re-stage
    smokeRunner     = 'attr3-stage-smoke-runner-8ea38dff267b-0123456789ab'              # attr3-stage-smoke-runner-job.ps1
    fixtureTiny     = 'attr3-stage-fixture-tiny_dual_iso-0123456789ab'                  # attr3-stage-fixture-job.ps1
    fixtureLarge    = 'attr3-stage-fixture-large_dual_iso-0123456789ab'
}
$lightIds = [ordered]@{
    quietProbe = 'venue-quiet-probe-20261024T051948123Z'
    agentSwap  = 'agent-swap-20261024T0519Z'
    dveHealth  = 'dve-bachelor-health'
}
$gatedIds = [ordered]@{
    measuredLeg       = 'playback-attr-3-cuda-8ea38dff267b-M16-1243-20261024T051948Z'
    stageElsewhere    = 'playback-attr-3-cuda-8ea38dff267b-stage-20261024T051948Z'      # "stage" outside the anchored family
    stageClip         = 'playback-attr-3-cuda-8ea38dff267b-stage-M16-1243-20261024T0519Z'
    footageStage      = 'attr3-footage-stage-M16-1243-0123456789ab-n1'                  # owner footage: GBs, 1800 s, stays gated
    build             = 'playback-attr-3-cuda-build-8ea38dff267b'
    referenceFrame    = 'reference-frame-8ea38dff267b-stage-0123456789ab'
    prefixed          = 'x-playback-attr-3-cuda-stage-8ea38dff267b'
    shortSha          = 'playback-attr-3-cuda-stage-8ea38dff267'
    longSha           = 'playback-attr-3-cuda-stage-8ea38dff267bab'
    upperSha          = 'playback-attr-3-cuda-stage-8EA38DFF267B'
    suffixTooLong     = 'playback-attr-3-cuda-stage-8ea38dff267b-' + ('a' * 33)
    suffixBadChar     = 'playback-attr-3-cuda-stage-8ea38dff267b-n_1'
    trailingNewline   = "playback-attr-3-cuda-stage-8ea38dff267b`n"
    smokeOneDigest    = 'attr3-stage-smoke-runner-8ea38dff267b'
    smokeTrailing     = 'attr3-stage-smoke-runner-8ea38dff267b-0123456789ab-x'
    fixtureOtherClip  = 'attr3-stage-fixture-M16-1243-0123456789ab'
    stageSoak         = 'playback-attr-3-cuda-stage-soak-8ea38dff267b'
    plainJob          = 'job-1'
}

try {
    # ------------------------------------------------------------ classify: the families, by the agent's own pattern
    if (Test-Case 'classify') {
        Assert-True ($null -ne $classifyFn) "agent defines Get-GateExemptClass"
        Assert-True ($GateStageJobPattern -is [string] -and $GateStageJobPattern.Length -gt 0) "agent declares a GateStageJobPattern default"
        Assert-True ($GateExemptJobPattern -ceq '^(venue-quiet-probe|agent-swap)-|^dve-[A-Za-z0-9._-]+-health$') "GateExemptJobPattern is unchanged: '$GateExemptJobPattern'"
        $got = [ordered]@{}
        foreach ($k in $stageIds.Keys) { $c = Get-GateExemptClass -JobId $stageIds[$k]; $got[$k] = $c; Assert-True ($c -ceq 'stage') "classify: $($stageIds[$k]) is stage: '$c'" }
        foreach ($k in $lightIds.Keys) { $c = Get-GateExemptClass -JobId $lightIds[$k]; $got[$k] = $c; Assert-True ($c -ceq 'light') "classify: $($lightIds[$k]) is light: '$c'" }
        foreach ($k in $gatedIds.Keys) { $c = Get-GateExemptClass -JobId $gatedIds[$k]; $got[$k] = $c; Assert-True ($null -eq $c) "classify: $($gatedIds[$k] -replace "`n", '\n') is gated: '$c'" }
        # Override bounds: a pattern that would exempt a measured id (or every id) is refused at binding with a typed token.
        $p = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'GateStageJobPattern' }
        $vs = @($p.Attributes | Where-Object { $_.TypeName.Name -eq 'ValidateScript' })
        Assert-True ($vs.Count -eq 1) "GateStageJobPattern is validated at binding"
        $validator = [scriptblock]::Create($vs[0].PositionalArguments[0].ScriptBlock.EndBlock.Extent.Text)
        $refused = [ordered]@{}
        foreach ($bad in @('', ' ', '.*', 'stage', '^playback-attr-3-cuda-', '(', '^attr3-footage-stage-')) {
            $msg = $null
            try { $null = ForEach-Object -Process $validator -InputObject $bad } catch { $msg = $_.Exception.Message }
            Assert-True ($msg -match '^HEAVY_SLOT_OVERRIDE_OUT_OF_BOUNDS: -GateStageJobPattern ') "override: '$bad' is refused with the typed token: $msg"
            $refused[$bad] = $true
        }
        Assert-True ((ForEach-Object -Process $validator -InputObject $GateStageJobPattern) -eq $true) "override: the default passes its own validator"
        $results.classify = [ordered]@{ pattern = $GateStageJobPattern; classes = $got; refusedOverrides = @($refused.Keys) }
    }

    # ------------------------------------------------------------ stage-ungated: zero heavy-slot.py calls, straight to the launch
    if (Test-Case 'stage-ungated') {
        $per = [ordered]@{}
        foreach ($k in $stageIds.Keys) {
            Reset-Case
            $id = $stageIds[$k]
            $script:ExpireJob = $id      # a gated (base) agent would otherwise wait the hang guard out
            $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=*' }).Count -ge 1 }
            $null = Invoke-QueuedJob -Job (New-InboxJob $id)
            $calls = @(Get-Calls)
            $l = @($script:Launches)
            Assert-True ($calls.Count -eq 0) "stage-ungated: $id makes ZERO heavy-slot.py calls (no lease requested): $($calls.Count) call(s): $(@($calls | ForEach-Object { $_.argv -join ' ' }) -join ' | ')"
            Assert-True ($script:Claims.Count -eq 0) "stage-ungated: $id is not claimed for the gate (no admission-wait marker)"
            Assert-True ($l.Count -eq 1 -and $l[0].jobId -ceq $id -and $l[0].label -ceq 'exempt-light' -and -not $l[0].leasePending -and -not $l[0].leaseHeld) "stage-ungated: $id reaches the launch, exempt-light, with no lease pending or held: $($l | ConvertTo-Json -Compress)"
            Assert-True ($l[0].why -like '*GateStageJobPattern*') "stage-ungated: the admission names the stage pattern: $($l[0].why)"
            Assert-True (@($script:Log | Where-Object { $_ -ceq "heavy-slot-gate: exempt job=$id (staging step, no slot, no lease)" }).Count -eq 1) "stage-ungated: typed exempt line for $id"
            Assert-True ($script:Published.Count -eq 0) "stage-ungated: nothing is published for $id before it runs"
            $per[$k] = [ordered]@{ calls = $calls.Count; label = $l[0].label }
        }
        $results.stageUngated = $per
    }

    # ------------------------------------------------------------ stage-gated: measured ids that carry "stage" elsewhere still lease
    if (Test-Case 'stage-gated') {
        $per = [ordered]@{}
        foreach ($k in @('stageElsewhere', 'stageClip', 'footageStage', 'build', 'measuredLeg')) {
            Reset-Case
            $id = $gatedIds[$k]
            $script:ExpireJob = $id
            $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=*' }).Count -ge 1 }
            $null = Invoke-QueuedJob -Job (New-InboxJob $id)
            $reqs = @(Get-Requests)
            Assert-True ($reqs.Count -ge 1 -and $script:Claims -contains $id) "stage-gated: $id is claimed and requests a quiet lease: claims=[$($script:Claims -join ',')] requests=$($reqs.Count)"
            Assert-True ($script:Launches.Count -eq 0 -and $script:Published -contains "queue-expired|$id") "stage-gated: $id is never launched unleased (the queue ceiling ends it): launches=$($script:Launches.Count) published=[$($script:Published -join ',')]"
            $per[$k] = [ordered]@{ requests = $reqs.Count; published = @($script:Published) }
        }
        $results.stageGated = $per
    }

    # ------------------------------------------------------------ light-ungated: the existing exempt ids are unchanged
    if (Test-Case 'light-ungated') {
        $per = [ordered]@{}
        foreach ($k in $lightIds.Keys) {
            Reset-Case
            $id = $lightIds[$k]
            $script:ExpireJob = $id
            $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=*' }).Count -ge 1 }
            $null = Invoke-QueuedJob -Job (New-InboxJob $id)
            $l = @($script:Launches)
            Assert-True (@(Get-Calls).Count -eq 0 -and $script:Claims.Count -eq 0) "light-ungated: $id makes zero heavy-slot.py calls and no claim"
            Assert-True ($l.Count -eq 1 -and $l[0].label -ceq 'exempt-light' -and $l[0].why -like '*GateExemptJobPattern*') "light-ungated: $id reaches the launch as before: $($l | ConvertTo-Json -Compress)"
            Assert-True (@($script:Log | Where-Object { $_ -ceq "heavy-slot-gate: exempt job=$id (light class, no slot)" }).Count -eq 1) "light-ungated: the light line is unchanged for $id"
            $per[$k] = $l[0].label
        }
        $results.lightUngated = $per
    }

    # ------------------------------------------------------------ stage-cancel: the cancel file still stops a staging job
    if (Test-Case 'stage-cancel') {
        Reset-Case
        $id = $stageIds.attrStage
        $script:ExpireJob = $id
        $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=*' }).Count -ge 1 }
        [IO.File]::WriteAllText((Join-Path $cancel "$id.cancel"), 'x')
        $null = Invoke-QueuedJob -Job (New-InboxJob $id)
        Assert-True ($script:Launches.Count -eq 0 -and $script:Published -contains "cancelled-in-gate|$id") "stage-cancel: a cancelled staging job is published cancelled-in-gate and never launched: published=[$($script:Published -join ',')] launches=$($script:Launches.Count)"
        Assert-True (@(Get-Calls).Count -eq 0) "stage-cancel: and no lease was requested for it"
        $results.stageCancel = @($script:Published)
    }

    # ------------------------------------------------------------ stage-notafter: notAfterUtc still bounds a staging job
    if (Test-Case 'stage-notafter') {
        $per = [ordered]@{}
        foreach ($when in @('past', 'unparseable')) {
            Reset-Case
            $id = $stageIds.smokeRunner
            $na = if ($when -eq 'past') { [DateTime]::UtcNow.AddSeconds(-60).ToString('o') } else { 'not-a-date' }
            $script:Meta[$id] = [pscustomobject]@{ jobId = $id; timeoutSec = 60; maxQueueAgeSec = 600; notAfterUtc = $na }
            $script:ExpireJob = $id
            $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=*' }).Count -ge 1 }
            $null = Invoke-QueuedJob -Job (New-InboxJob $id)
            Assert-True ($script:Launches.Count -eq 0 -and $script:Published -contains "expired-not-after|$id" -and @(Get-Calls).Count -eq 0) "stage-notafter ($when): a staging job past notAfterUtc is published expired, never launched, no lease: published=[$($script:Published -join ',')] launches=$($script:Launches.Count)"
            $per[$when] = @($script:Published)
        }
        $results.stageNotAfter = $per
    }

    # ------------------------------------------------------------ stage-not-during-wait: serial with the agent's own measured leg
    # The during-wait dispatcher runs light jobs only. A staging job behind a measured job's lease wait stays in inbox\: it
    # cannot run while that job's request is pending, nor when it turns active, so it never shares the machine with the leg.
    if (Test-Case 'stage-not-during-wait') {
        Reset-Case
        $outer = $gatedIds.measuredLeg
        $script:ExpireJob = $outer
        $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like "quiet-lease: refused job=$outer *" }).Count -ge 2 }
        $stageJob = New-InboxJob $stageIds.attrStage
        $null = New-InboxJob $lightIds.agentSwap
        $outerJob = New-InboxJob $outer
        $null = Invoke-QueuedJob -Job $outerJob
        $l = @($script:Launches)
        $scans = @($script:Log | Where-Object { $_ -like "quiet-lease: refused job=$outer *" }).Count
        Assert-True ($scans -ge 2 -and $script:Published -contains "queue-expired|$outer") "stage-not-during-wait: the measured job waited through $scans refusals and ended queue-expired"
        Assert-True (@($l | Where-Object { $_.jobId -ceq $lightIds.agentSwap -and $_.waitingFor -ceq $outer -and $_.label -ceq 'exempt-light' }).Count -eq 1) "stage-not-during-wait: the light job behind the wait still runs from inside it: $($l | ConvertTo-Json -Compress)"
        Assert-True (@($l | Where-Object { $_.jobId -ceq $stageIds.attrStage }).Count -eq 0 -and (Test-Path -LiteralPath $stageJob.FullName -PathType Leaf)) "stage-not-during-wait: the staging job is NOT run during the measured job's lease wait and stays in inbox\"
        Assert-True (@($script:Log | Where-Object { $_ -like "heavy-slot-gate: maint-dispatch exempt job=$($stageIds.attrStage) *" }).Count -eq 0) "stage-not-during-wait: no maint-dispatch line for the staging job"
        $results.stageNotDuringWait = [ordered]@{ refusals = $scans; ranDuringWait = @($l | ForEach-Object { $_.jobId }); stageLeftQueued = $true }
    }
    if ($Only.Count -gt 0) { $results.only = @($Only) }
}
finally {
    foreach ($l2 in @($script:QuietLease, $script:QuietLeaseWaiting)) { if ($null -ne $l2) { try { $null = Stop-QuietLease -Lease $l2 -Why 'test cleanup' } catch { } } }
    Remove-Item -LiteralPath $fx -Recurse -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
