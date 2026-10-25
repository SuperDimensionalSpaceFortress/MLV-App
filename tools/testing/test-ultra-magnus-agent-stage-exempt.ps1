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
#   override-bounds - (hardening r1, Dot 04eedafd on #649) an override must stay inside the staging grammar: one that matches an id
#                     the default pattern cannot produce (a prefix no probe list names, a near-miss of a staging id) is refused
#   stage-ceiling   - (hardening r1) a stage-class job's effective timeout is bounded by -StageExemptMaxSec (default 300 s, ~66x the
#                     measured ~4.5 s step), with a typed STAGE_EXEMPT_CEILING_* reason; light and gated timeouts are unchanged
#   classify-and-override - (hardening r2, sol/opus/fable/Dot on b8faccf1) classify = the immutable default grammar AND the effective pattern, so an
#                     override that is somehow in force can only NARROW: an id the default grammar rejects is gated whatever the override says
#   stage-ceiling-kill - (hardening r2) the kill path driven by a REAL child: a stage job whose child outlives -StageExemptMaxSec is killed (exit 124)
#                     with the typed STAGE_EXEMPT_CEILING_EXCEEDED line and admission.stageCeiling.clamped; the controls (own shorter timeout, a child
#                     that finishes in time) carry no ceiling reason
#   stage-ceiling-adopt - (hardening r2) an ADOPTED stage job past its bound gets the same typed reason; a non-stage or unclamped adopted job does not
# Prints one JSON object with status PASS, or throws.

$ErrorActionPreference = 'Stop'
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }
$caseNames = @('classify', 'stage-ungated', 'stage-gated', 'light-ungated', 'stage-cancel', 'stage-notafter', 'stage-not-during-wait', 'override-bounds', 'stage-ceiling', 'classify-and-override', 'stage-ceiling-kill', 'stage-ceiling-adopt')
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
    'Get-JobNotAfterVerdict', 'Get-QuietLeaseAdmitTimeout', 'New-LeaseReleaseRecord', 'Invoke-ExemptJobsDuringWait', 'Invoke-QueuedJob', 'Read-JobNotAfterSidecar',
    # hardening r2: the real child launch, the process-tree kill and the adopt path, so the ceiling kill is driven against a real child
    'Start-WindowlessRedirectedProcess', 'Complete-WindowlessRedirectedProcess', 'Set-LaunchedMarkerFields', 'Stop-VerifiedProcessIdentities', 'Invoke-AdoptStartedJob', 'Get-RecoveredAdmission')
foreach ($name in $lift) { $fn = Find-Function $name; Assert-True ($null -ne $fn) "agent defines $name"; . ([scriptblock]::Create($fn.Extent.Text)) }
# The function under change: lifted when present, so a base agent runs every behavioural case and fails each on behaviour.
$classifyFn = Find-Function 'Get-GateExemptClass'
if ($null -ne $classifyFn) { . ([scriptblock]::Create($classifyFn.Extent.Text)) }
# BACHELOR-LEASE-BATCH-LEGS-1 r5: Invoke-QueuedJob and the lease wait call the lease-batch block (no batch is open here); lifted when present.
foreach ($name in @('Get-LeaseBatchToken', 'Test-LeaseBatchCloseJobId', 'Test-LeaseBatchClosePending', 'Get-QuietLeaseRemainingSec', 'Get-QuietLeaseRenewOkGapSec',
        'Get-LeaseBatchAdmission', 'Open-LeaseBatch', 'Use-LeaseBatchForJob', 'Get-LeaseBatchJobStop', 'Complete-LeaseBatchJob', 'Write-LeaseBatchReceipt', 'Close-LeaseBatch',
        'Close-LeaseBatchAfterRelease', 'Sync-LeaseBatchForJob')) {
    $fn = Find-Function $name
    if ($null -ne $fn) { . ([scriptblock]::Create($fn.Extent.Text)) }
}
$script:LeaseBatch = $null
$script:LeaseBatchKeep = $false
$paramDefaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) { if ($p.DefaultValue -is [System.Management.Automation.Language.ConstantExpressionAst]) { $paramDefaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value } }
$GateExemptJobPattern = [string]$paramDefaults['GateExemptJobPattern']
$GateStageJobPattern = $paramDefaults['GateStageJobPattern']
$StageExemptMaxSec = $paramDefaults['StageExemptMaxSec']
$agentText = [IO.File]::ReadAllText($agentScript)
# hardening r2: the immutable default grammar Get-GateExemptClass ANDs with the effective pattern (a base agent has none: its classifier ignores this).
$grammarAst = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$script:StageGrammarDefault' }, $true)
$script:StageGrammarDefault = if ($null -ne $grammarAst) { [string]$grammarAst.Right.Expression.Value } else { [string]$GateStageJobPattern }
$psExe = (Get-Process -Id $PID).Path
$Root = ''
$PollSeconds = 1

$results = [ordered]@{}
$fx = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-stage-exempt-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
$Root = $fx
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
# hardening r2: the launch-and-monitor tail of Invoke-QueuedJob, driven for real by the kill cases (a real child, the agent's own Stop-ProcessTree).
$script:RealLaunch = $false
$script:JobResults = @{}
function Set-AgentKeepAwake { param([bool]$Enable) return $true }
function Write-StartedMarkerVerified { param($Path, $Marker) return $true }
function Invoke-QuietLeaseRenewTick { }
function Publish-JobResult { param([string]$JobId, $Result) $script:JobResults[$JobId] = $Result; return "unit:$JobId" }
function Move-JobArtifacts { param([string]$JobId, [string]$JobPath, [string]$DestinationDirectory) }
function Complete-StartedMarker { param([string]$MarkerPath, [string]$Status, [string]$ResultPath) }
function New-LaunchingMarker {
    param([string]$JobId, $Job, $Metadata, [string]$OutFile, [string]$ErrFile, [string]$StartedUtc, [int]$TimeoutSec)
    # What WOULD launch, with the admission it would launch under; the job file leaves inbox\ as a run job's does.
    $a = $script:Admission
    $script:Launches.Add([pscustomobject]@{ jobId = $JobId; label = $a.label; why = $a.why; waitingFor = $a.waitingFor; leasePending = ($null -ne $script:QuietLeaseWaiting); leaseHeld = ($null -ne $script:QuietLease); timeoutSec = $TimeoutSec; stageCeiling = $a.stageCeiling })
    if ($script:RealLaunch) { return [pscustomobject]@{ jobId = $JobId; heartbeatGeneration = 0 } }   # the kill cases launch the child for real
    if (Test-Path -LiteralPath $Job.FullName -PathType Leaf) { Move-Item -LiteralPath $Job.FullName -Destination $processed }
    return $null
}

function Get-Calls { $p = Join-Path $fx 'calls.jsonl'; if (-not (Test-Path -LiteralPath $p)) { return @() }; return @(Get-Content -LiteralPath $p | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json }) }
function Reset-Case {
    Remove-Item -LiteralPath (Join-Path $fx 'calls.jsonl') -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $inbox, $processed, $cancel -File | Remove-Item -Force
    $script:JobResults = @{}; $script:RealLaunch = $false; $script:Log.Clear(); $script:Claims.Clear(); $script:Launches.Clear(); $script:Published.Clear(); $script:Meta = @{}
    $script:QuietLease = $null; $script:QuietLeaseWaiting = $null; $script:Admission = $null; $script:ExemptDuringWaitOf = $null
    $script:ExpireJob = $null; $script:ExpireWhen = { $false }; $script:AgeAfter = (Get-Date).AddSeconds(60)
}
function New-SleepJob([string]$JobId, [int]$Sec) { $p = Join-Path $inbox "$JobId.job.ps1"; [IO.File]::WriteAllText($p, "Start-Sleep -Seconds $Sec"); return (Get-Item -LiteralPath $p) }
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
    # ------------------------------------------------------------ override-bounds: an override stays inside the staging grammar
    # Hardening r1 (Dot 04eedafd on #649): the override used to be refused only when it matched one of seven measured probe ids, so a
    # pattern for any OTHER measured shape (dllpair, a new leg generator, a reference frame with another stem) was accepted and that
    # leg would run with no quiet machine. The bound is now positive: every alternative must open with a staging stem, and the
    # pattern must reject near-misses of the staging ids. Narrowing to a subset of the grammar stays allowed (more jobs gated).
    if (Test-Case 'override-bounds') {
        $p = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'GateStageJobPattern' }
        $vs = @($p.Attributes | Where-Object { $_.TypeName.Name -eq 'ValidateScript' })
        Assert-True ($vs.Count -eq 1) "override-bounds: GateStageJobPattern is validated at binding"
        $validator = [scriptblock]::Create($vs[0].PositionalArguments[0].ScriptBlock.EndBlock.Extent.Text)
        Assert-True ($vs[0].PositionalArguments[0].ScriptBlock.EndBlock.Extent.Text.Contains("`$stageGrammar = '$GateStageJobPattern'")) "override-bounds: the validator's copy of the grammar equals the parameter default"
        $mustRefuse = @(
            '^playback-attr-3-cuda-dllpair-[0-9a-f]{12}\z'                                        # a real generator id the probe list never named
            '^cpu-look-leg-[0-9a-f]{12}-[A-Za-z0-9-]+\z'                                          # a new leg generator's ids
            '^reference-frame-[0-9a-f]{12}-[a-z0-9-]+-[0-9a-f]{12}\z'                             # a reference frame with another stem
            '^lease-batch-open-[0-9T]+-lb[0-9a-f]+\z'
            '^playback-attr-3-cuda-stage-[0-9a-f]{12}-.*\z'                                       # widening inside the stem: admits near-misses
            '^attr3-stage-fixture-.*\z'                                                           # any fixture stem, not the two tracked clips
            '^(playback-attr-3-cuda-stage-[0-9a-f]{12}|lease-batch-open-.*)\z'                    # one good alternative, one not
            '^playback-attr-3-cuda-stage-[0-9a-f]{12}\z|^.*-leg-[0-9]+\z'                         # top-level alternation, second arm not a stem
            '^(playback-attr-3-cuda-stage-)?.*\z'                                                 # the stem made optional
            '^playback-attr-3-cuda-stage-[0-9a-f]{12}(-[A-Za-z0-9_-]{1,32})?\z'                  # allows an underscore the generator never emits
            # hardening r2 (sol BLOCKER on b8faccf1, opus/fable/Dot): a fixture stem the default grammar does not list, with a tail that
            # looks like the real one. attr3-stage-fixture-medium_dual_iso-0123456789ab is not a staging id, so these admit ids the default rejects.
            '^attr3-stage-fixture-medium_dual_iso-[0-9a-f]{12}\z'
            '^attr3-stage-fixture-unlisted_dual_iso-[0-9a-f]{12}\z'
            '^attr3-stage-fixture-(tiny|large|med)_dual_iso-[0-9a-f]{12}\z'
            '^attr3-stage-fixture-(tiny|large)_dual_iso-[0-9a-fA-F]{12}\z'                       # hex wider than the generator's lower case
            '^(attr3-stage-fixture-(tiny|large)_dual_iso-[0-9a-f]{12}|attr3-stage-fixture-small_dual_iso-[0-9a-f]{12})\z'   # one good arm, one unlisted stem
        )
        $mustAccept = @(
            $GateStageJobPattern                                                                  # the default
            '^playback-attr-3-cuda-stage-8ea38dff267b\z'                                          # narrowed to one sha
            '^attr3-stage-fixture-tiny_dual_iso-[0-9a-f]{12}\z'                                   # narrowed to one fixture
            '^(attr3-stage-smoke-runner-[0-9a-f]{12}-[0-9a-f]{12}|attr3-stage-fixture-large_dual_iso-[0-9a-f]{12})\z'
            '^attr3-stage-smoke-runner-[0-9a-f]{12}-[0-9a-f]{12}\z|^attr3-stage-fixture-tiny_dual_iso-[0-9a-f]{12}\z'
        )
        $refusedNow = [ordered]@{}; $acceptedNow = [ordered]@{}
        foreach ($bad in $mustRefuse) {
            $msg = $null
            try { $null = ForEach-Object -Process $validator -InputObject $bad } catch { $msg = $_.Exception.Message }
            Assert-True ($null -ne $msg -and $msg -match '^HEAVY_SLOT_OVERRIDE_OUT_OF_BOUNDS: -GateStageJobPattern ') "override-bounds: '$bad' is refused with the typed token (it admits ids the default grammar cannot produce): '$msg'"
            $refusedNow[$bad] = $msg
        }
        foreach ($good in $mustAccept) {
            $msg = $null; $ok = $null
            try { $ok = ForEach-Object -Process $validator -InputObject $good } catch { $msg = $_.Exception.Message }
            Assert-True ($ok -eq $true -and $null -eq $msg) "override-bounds: '$good' stays accepted (inside the grammar): '$msg'"
            $acceptedNow[$good] = $true
        }
        $results.overrideBounds = [ordered]@{ refused = @($refusedNow.Keys); accepted = @($acceptedNow.Keys) }
    }

    # ------------------------------------------------------------ stage-ceiling: a staging job's timeout is bounded, the others' are not
    # Evidence for the default: playback-attr-3-cuda-stage-8ea38dff267b, a copy-and-hash step, ran ~4.5 s on 2026-10-24; 300 s is
    # ~66x that, well under the 900 s heavy-slot long-label bound. The submit paths ask 1800 s (smoke-runner) and 3600 s (stage).
    if (Test-Case 'stage-ceiling') {
        # The behavioural checks run first (against 300 when the agent declares no ceiling, so a base agent fails each on behaviour);
        # the declaration checks close the case.
        $declaredCeiling = $StageExemptMaxSec
        if ($declaredCeiling -isnot [int]) { $StageExemptMaxSec = 300 }
        $per = [ordered]@{}
        $runStage = {
            param([string]$Id, $Timeout)
            Reset-Case
            if ($null -ne $Timeout) { $script:Meta[$Id] = [pscustomobject]@{ jobId = $Id; timeoutSec = $Timeout; maxQueueAgeSec = 600 } }
            $script:ExpireJob = $Id
            $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=*' }).Count -ge 1 }
            $null = Invoke-QueuedJob -Job (New-InboxJob $Id)
            return @($script:Launches)
        }
        foreach ($k in $stageIds.Keys) {
            $id = $stageIds[$k]
            $l = @(& $runStage $id 3600)
            Assert-True ($l.Count -eq 1 -and $l[0].timeoutSec -eq $StageExemptMaxSec) "stage-ceiling: $id asked for 3600 s is bounded at ${StageExemptMaxSec} s: $($l | ConvertTo-Json -Compress)"
            Assert-True ($l[0].stageCeiling.ceilingSec -eq $StageExemptMaxSec -and $l[0].stageCeiling.requestedSec -eq 3600 -and $l[0].stageCeiling.clamped -eq $true) "stage-ceiling: $id carries the typed ceiling record in its admission: $($l[0].stageCeiling | ConvertTo-Json -Compress)"
            Assert-True (@($script:Log | Where-Object { $_ -ceq "heavy-slot-gate: STAGE_EXEMPT_CEILING_APPLIED job=$id requested=3600s ceiling=${StageExemptMaxSec}s" }).Count -eq 1) "stage-ceiling: typed STAGE_EXEMPT_CEILING_APPLIED line for $id"
            $per[$k] = $l[0].timeoutSec
        }
        # A 1800 s ask (the agent's JobTimeoutSec default value, given here as the job's own metadata timeoutSec) is bounded too.
        $id = $stageIds.attrStage
        $l = @(& $runStage $id 1800)
        Assert-True ($l.Count -eq 1 -and $l[0].timeoutSec -eq $StageExemptMaxSec) "stage-ceiling: an 1800 s ask is bounded at ${StageExemptMaxSec} s: $($l | ConvertTo-Json -Compress)"
        # No regression: a normal ask (inside the ceiling, and exactly at it) is untouched, and says nothing about a ceiling.
        foreach ($ask in @(60, 120, $StageExemptMaxSec)) {
            $l = @(& $runStage $id $ask)
            Assert-True ($l.Count -eq 1 -and $l[0].label -ceq 'exempt-light' -and $l[0].timeoutSec -eq $ask) "stage-ceiling: a ${ask} s staging ask is unchanged and still exempt-light: $($l | ConvertTo-Json -Compress)"
            Assert-True ($l[0].stageCeiling.clamped -eq $false -and @($script:Log | Where-Object { $_ -like '*STAGE_EXEMPT_CEILING*' }).Count -eq 0) "stage-ceiling: a ${ask} s ask is not clamped and logs no ceiling line"
        }
        # The light class keeps its timeout; a gated job keeps its timeout (its quiet lease is sized from it: 1800 s -> 31 min, not the 6 min a 300 s clamp would give).
        $l = @(& $runStage $lightIds.quietProbe 3600)
        Assert-True ($l.Count -eq 1 -and $l[0].timeoutSec -eq 3600 -and $null -eq $l[0].stageCeiling) "stage-ceiling: a light job asked for 3600 s keeps 3600 s and has no ceiling record: $($l | ConvertTo-Json -Compress)"
        $null = & $runStage $gatedIds.measuredLeg 1800
        $reqs = @(Get-Requests)
        Assert-True ($reqs.Count -ge 1 -and ((@($reqs[0].argv) -join ' ') -match '--minutes 31 ')) "stage-ceiling: a gated job asked for 1800 s still requests a 31 min lease: $((@($reqs | ForEach-Object { $_.argv -join ' ' })) -join ' | ')"
        Assert-True (@($script:Log | Where-Object { $_ -like '*STAGE_EXEMPT_CEILING*' }).Count -eq 0) "stage-ceiling: a gated job logs no stage ceiling line"
        # The kill path itself is driven by a real child in stage-ceiling-kill and stage-ceiling-adopt; here only the typed text is pinned.
        Assert-True ($agentText -match 'STAGE_EXEMPT_CEILING_EXCEEDED: ') "stage-ceiling: the timed-out branch writes the typed STAGE_EXEMPT_CEILING_EXCEEDED reason"
        Assert-True ($declaredCeiling -is [int] -and $declaredCeiling -eq 300) "stage-ceiling: the agent declares -StageExemptMaxSec with default 300 (got '$declaredCeiling')"
        $p = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'StageExemptMaxSec' }
        $vr = @($p.Attributes | Where-Object { $_.TypeName.Name -eq 'ValidateRange' })
        Assert-True ($vr.Count -eq 1 -and $vr[0].PositionalArguments[0].Value -eq 1 -and $vr[0].PositionalArguments[1].Value -le 900) "stage-ceiling: -StageExemptMaxSec has a validated range, 1..900 at most"
        $results.stageCeiling = [ordered]@{ ceilingSec = $StageExemptMaxSec; boundedAt = $per }
    }

    # ------------------------------------------------------------ classify-and-override: an override can only NARROW the default grammar
    # Hardening r2: sol reproduced an override ('^attr3-stage-fixture-medium_dual_iso-[0-9a-f]{12}\z') the load-time guard accepted and
    # that made attr3-stage-fixture-medium_dual_iso-0123456789ab classify "stage" though the default grammar does not match it, because
    # Get-GateExemptClass used the override alone. The load-time guard is a sampled check; this is the real bound: stage = default AND override.
    if (Test-Case 'classify-and-override') {
        Assert-True ($null -ne $classifyFn) "agent defines Get-GateExemptClass"
        $savedPattern = $GateStageJobPattern
        $outside = 'attr3-stage-fixture-medium_dual_iso-0123456789ab'
        Assert-True ($null -ne $grammarAst -and $script:StageGrammarDefault -ceq $GateStageJobPattern) "classify-and-override: the agent declares `$script:StageGrammarDefault, equal to the GateStageJobPattern default"
        Assert-True ($outside -cnotmatch $script:StageGrammarDefault) "classify-and-override: $outside is outside the default grammar"
        $per = [ordered]@{}
        try {
            foreach ($ov in @('^attr3-stage-fixture-medium_dual_iso-[0-9a-f]{12}\z', '^attr3-stage-fixture-(tiny|large|med)_dual_iso-[0-9a-f]{12}\z', '^attr3-stage-fixture-.*\z', '.*')) {
                $GateStageJobPattern = $ov     # in force WITHOUT passing the load-time guard: the classifier must hold on its own
                $c = Get-GateExemptClass -JobId $outside
                Assert-True ($null -eq $c) "classify-and-override: with override '$ov' in force, $outside is NOT stage (gated): '$c'"
                $per[$ov] = $c
            }
            # r3 (opus r2 hardening): fail CLOSED. An empty or missing default grammar matches everything (-cmatch '' is true), so the override alone
            # would decide; the classifier must gate every id instead, not fall through to the override.
            $savedGrammar = $script:StageGrammarDefault
            try {
                $GateStageJobPattern = '.*'
                foreach ($emptyGrammar in @('', $null)) {
                    $script:StageGrammarDefault = $emptyGrammar
                    $c = Get-GateExemptClass -JobId $stageIds.fixtureTiny
                    Assert-True ($null -eq $c) "classify-and-override: with the default grammar '$emptyGrammar' (empty/null) and '.*' in force, the classifier fails closed (gated), not stage: '$c'"
                }
            }
            finally { $script:StageGrammarDefault = $savedGrammar }
            # An override still NARROWS: an id the default admits but the override does not match is gated; one both admit stays stage.
            $GateStageJobPattern = '^attr3-stage-fixture-tiny_dual_iso-[0-9a-f]{12}\z'
            $c = Get-GateExemptClass -JobId $stageIds.fixtureLarge
            Assert-True ($null -eq $c) "classify-and-override: an override narrowed to tiny gates the large fixture (an override narrows): '$c'"
            $c = Get-GateExemptClass -JobId $stageIds.fixtureTiny
            Assert-True ($c -ceq 'stage') "classify-and-override: an override narrowed to tiny keeps the tiny fixture stage: '$c'"
            # Behaviour, not just the classifier: the out-of-grammar id under a wide override requests a quiet lease like any gated job.
            $GateStageJobPattern = '^attr3-stage-fixture-.*\z'
            Reset-Case
            $script:ExpireJob = $outside
            $script:ExpireWhen = { @($script:Log | Where-Object { $_ -like 'quiet-lease: refused job=*' }).Count -ge 1 }
            $null = Invoke-QueuedJob -Job (New-InboxJob $outside)
            $reqs = @(Get-Requests)
            Assert-True ($reqs.Count -ge 1 -and $script:Claims -contains $outside -and $script:Launches.Count -eq 0) "classify-and-override: $outside under a wide override is claimed and requests a quiet lease, never launched unleased: claims=[$($script:Claims -join ',')] requests=$($reqs.Count) launches=$($script:Launches.Count)"
        }
        finally { $GateStageJobPattern = $savedPattern }
        # No regression with the default in force: every default stage id still classes stage, a light id stays light, a measured id stays gated.
        foreach ($k in $stageIds.Keys) { $c = Get-GateExemptClass -JobId $stageIds[$k]; Assert-True ($c -ceq 'stage') "classify-and-override: default in force, $($stageIds[$k]) is stage: '$c'" }
        Assert-True ((Get-GateExemptClass -JobId $lightIds.quietProbe) -ceq 'light') "classify-and-override: a light id is still light"
        Assert-True ($null -eq (Get-GateExemptClass -JobId $gatedIds.measuredLeg)) "classify-and-override: a measured id is still gated"
        $results.classifyAndOverride = [ordered]@{ outside = $outside; classUnderOverride = $per }
    }

    # ------------------------------------------------------------ stage-ceiling-kill: the kill path, driven by a REAL child
    # Invoke-QueuedJob's own launch, monitor loop and Stop-ProcessTree run for real (only the disk, result and keep-awake steps are
    # stubbed). The child is a pwsh that sleeps 40 s; the ceiling is 1 s, so the agent must kill it long before it ends (the monitor loop pays ~1 s per CIM process query,
    # so the kill lands a few seconds after the bound, never near 40 s).
    if (Test-Case 'stage-ceiling-kill') {
        $savedCeiling = $StageExemptMaxSec
        $per = [ordered]@{}
        try {
            $id = $stageIds.attrStage
            # (1) the ceiling is the bound: a 3600 s ask, ceiling 1 s, a child that outlives it.
            $StageExemptMaxSec = 1
            Reset-Case
            $script:RealLaunch = $true
            $script:Meta[$id] = [pscustomobject]@{ jobId = $id; timeoutSec = 3600; maxQueueAgeSec = 600 }
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $null = Invoke-QueuedJob -Job (New-SleepJob $id 40)
            $sw.Stop()
            $r = $script:JobResults[$id]
            Assert-True ($null -ne $r) "stage-ceiling-kill: the job published a result"
            Assert-True ($sw.Elapsed.TotalSeconds -lt 30) "stage-ceiling-kill: the 40 s child was cut off at the 1 s ceiling, well before its end: $([math]::Round($sw.Elapsed.TotalSeconds, 1)) s"
            Assert-True ($r.exitCode -eq 124 -and $r.timedOut -eq $true) "stage-ceiling-kill: exit 124, timedOut: exit=$($r.exitCode) timedOut=$($r.timedOut)"
            Assert-True ($r.timeoutSec -eq 1) "stage-ceiling-kill: the effective timeout in the result is the ceiling: $($r.timeoutSec)"
            Assert-True ($r.stderr -match 'STAGE_EXEMPT_CEILING_EXCEEDED: ') "stage-ceiling-kill: the result's stderr carries the typed ceiling reason: $($r.stderr)"
            Assert-True ($r.admission.stageCeiling.clamped -eq $true -and $r.admission.stageCeiling.ceilingSec -eq 1 -and $r.admission.stageCeiling.requestedSec -eq 3600) "stage-ceiling-kill: admission.stageCeiling records the clamp: $($r.admission.stageCeiling | ConvertTo-Json -Compress)"
            Assert-True (@($r.killedProcessIds).Count -ge 1) "stage-ceiling-kill: the agent killed the child tree: pids=$(@($r.killedProcessIds) -join (","))"
            Assert-True (@($script:Log | Where-Object { $_ -like "heavy-slot-gate: STAGE_EXEMPT_CEILING_EXCEEDED job=$id *" }).Count -eq 1) "stage-ceiling-kill: typed log line"
            Assert-True ($null -eq (Get-Process -Id ([int]$r.childPid) -ErrorAction SilentlyContinue)) "stage-ceiling-kill: the child pid $($r.childPid) is gone"
            Assert-True (@(Get-Calls).Count -eq 0) "stage-ceiling-kill: and no lease was requested"
            $per.ceiling = [ordered]@{ seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); exit = $r.exitCode }
            # (2) control: the job's OWN shorter timeout (1 s) under the default ceiling kills it with exit 124 but is NOT a ceiling kill.
            $StageExemptMaxSec = 300
            Reset-Case
            $script:RealLaunch = $true
            $script:Meta[$id] = [pscustomobject]@{ jobId = $id; timeoutSec = 1; maxQueueAgeSec = 600 }
            $null = Invoke-QueuedJob -Job (New-SleepJob $id 40)
            $r = $script:JobResults[$id]
            Assert-True ($r.exitCode -eq 124 -and $r.timedOut -eq $true -and $r.admission.stageCeiling.clamped -eq $false) "stage-ceiling-kill: the job's own 1 s timeout still ends it (124) unclamped: exit=$($r.exitCode) clamped=$($r.admission.stageCeiling.clamped)"
            Assert-True (([string]$r.stderr) -notmatch 'STAGE_EXEMPT_CEILING' -and @($script:Log | Where-Object { $_ -like '*STAGE_EXEMPT_CEILING*' }).Count -eq 0) "stage-ceiling-kill: and it carries NO ceiling reason: $($r.stderr)"
            $per.ownTimeout = $r.exitCode
            # (3) control: a child that finishes inside the ceiling exits 0 and says nothing about one (a 3600 s ask is clamped to 300 s, never reached).
            Reset-Case
            $script:RealLaunch = $true
            $script:Meta[$id] = [pscustomobject]@{ jobId = $id; timeoutSec = 3600; maxQueueAgeSec = 600 }
            $null = Invoke-QueuedJob -Job (New-SleepJob $id 0)
            $r = $script:JobResults[$id]
            Assert-True ($r.exitCode -eq 0 -and $r.timedOut -eq $false -and $r.admission.stageCeiling.clamped -eq $true -and ([string]$r.stderr) -notmatch 'STAGE_EXEMPT_CEILING_EXCEEDED') "stage-ceiling-kill: a quick stage child ends 0, not timed out, no EXCEEDED reason: exit=$($r.exitCode) timedOut=$($r.timedOut) clamped=$($r.admission.stageCeiling.clamped) stderr=[$($r.stderr)]"
            $per.quick = $r.exitCode
        }
        finally { $StageExemptMaxSec = $savedCeiling; $script:RealLaunch = $false }
        $results.stageCeilingKill = $per
    }

    # ------------------------------------------------------------ stage-ceiling-adopt: a restarted agent adopts a live stage child
    # Invoke-AdoptStartedJob (agent restart) kills an adopted child at the marker's timeoutSec. For a stage job the marker's admission
    # carries stageCeiling, and that bound is the ceiling's when clamped: the kill must be typed as the live path types it.
    if (Test-Case 'stage-ceiling-adopt') {
        # r3 (sol/dot/opus/fable r2 + hub ACK 2026-10-25T12:47Z): the case used to kill the child in its own finally BEFORE asserting, and
        # Invoke-AdoptStartedJob sets exit 124 from its own deadline, so a Stop-ProcessTree that killed nothing still passed. Each variant now
        # waits (bounded, NOT killing) for the adopted child to exit and requires the agent's own killedProcessIds to name its pid, both BEFORE
        # the fixture cleanup below runs.
        $savedCeiling = $StageExemptMaxSec
        $per = [ordered]@{}
        # name, marker timeoutSec, seconds the marker says the child already ran, StageExemptMaxSec for the variant, stageCeiling in the admission
        # (the legacy variants model a marker written by a pre-ceiling agent: exemptClass stage, NO stageCeiling), and whether a ceiling reason is expected.
        $variants = @(
            @{ name = 'stage-clamped';   timeoutSec = 1;    ranSec = 0;    ceilingSec = 300; admission = 'clamped';   typed = $true }
            @{ name = 'stage-unclamped'; timeoutSec = 1;    ranSec = 0;    ceilingSec = 300; admission = 'unclamped'; typed = $false }
            @{ name = 'gated';           timeoutSec = 1;    ranSec = 0;    ceilingSec = 300; admission = 'gated';     typed = $false }
            # transition edge (fable key3 r2): the old marker's timeoutSec (3600) exceeds the ceiling (1); it ran 3599 s, so the old bound ends in ~1 s
            # on an unfixed agent (no typed reason, RED) while the clamp ends it at once with the typed reason.
            @{ name = 'legacy-over-ceiling';   timeoutSec = 3600; ranSec = 3599; ceilingSec = 1;   admission = 'legacy'; typed = $true }
            @{ name = 'legacy-within-ceiling'; timeoutSec = 1;    ranSec = 0;    ceilingSec = 300; admission = 'legacy'; typed = $false }
        )
        try {
            foreach ($v in $variants) {
                $variant = $v.name
                Reset-Case
                $StageExemptMaxSec = $v.ceilingSec
                $id = $stageIds.attrStage
                $outF = Join-Path $fx "adopt-$variant.out.txt"; $errF = Join-Path $fx "adopt-$variant.err.txt"
                $sleepJob = New-SleepJob $id 40
                $launch = Start-WindowlessRedirectedProcess -FilePath $psExe -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $sleepJob.FullName) -OutFile $outF -ErrFile $errF -WorkingDirectory $fx
                $childPid = $launch.Process.Id
                $childGone = $false
                try {
                    $ident = Get-ProcessIdentity -ProcessId $childPid
                    $admission = switch ($v.admission) {
                        'clamped' { [pscustomobject]@{ label = 'exempt-light'; state = 'exempt'; exemptClass = 'stage'; stageCeiling = [pscustomobject]@{ ceilingSec = 1; requestedSec = 3600; clamped = $true }; why = 'unit' } }
                        'unclamped' { [pscustomobject]@{ label = 'exempt-light'; state = 'exempt'; exemptClass = 'stage'; stageCeiling = [pscustomobject]@{ ceilingSec = 300; requestedSec = 1; clamped = $false }; why = 'unit' } }
                        'legacy' { [pscustomobject]@{ label = 'exempt-light'; state = 'exempt'; exemptClass = 'stage'; why = 'unit' } }
                        default { [pscustomobject]@{ label = 'clear'; state = 'clear'; why = 'unit' } }
                    }
                    $marker = [pscustomobject]@{ jobId = $id; childPid = $childPid; childStartUtc = $ident.startUtc; childStartEpochMs = $ident.startEpochMs; childImagePath = $ident.imagePath
                        startedUtc = (Get-Date).ToUniversalTime().AddSeconds(-1 * $v.ranSec).ToString('o'); timeoutSec = $v.timeoutSec; outFile = $outF; errFile = $errF; sourceInboxPath = ''; admission = $admission }
                    $sw = [Diagnostics.Stopwatch]::StartNew()
                    Invoke-AdoptStartedJob -Marker $marker -MarkerPath (Join-Path $fx "adopt-$variant.started.json") -Match ([pscustomobject]@{})
                    $sw.Stop()
                    # Wait only (no kill) for the child to be gone: it is the AGENT's kill that must have ended it.
                    $childGone = $launch.Process.WaitForExit(5000)
                }
                finally { try { if (-not $launch.Process.HasExited) { $launch.Process.Kill($true) } } catch { }; Complete-WindowlessRedirectedProcess -Launch $launch; $launch.Process.Dispose() }
                $r = $script:JobResults[$id]
                Assert-True ($null -ne $r -and $r.adopted -eq $true -and $r.exitCode -eq 124 -and $r.timedOut -eq $true) "stage-ceiling-adopt ($variant): the adopted child past its bound is killed, 124: $($r | ConvertTo-Json -Compress -Depth 3)"
                Assert-True $childGone "stage-ceiling-adopt ($variant): the adopted child pid $childPid exited (by the agent's kill, before the fixture cleanup) within 5 s of the adopt call"
                Assert-True (@($r.killedProcessIds) -contains $childPid) "stage-ceiling-adopt ($variant): the agent's killedProcessIds names the adopted child ${childPid}: [$(@($r.killedProcessIds) -join ',')]"
                Assert-True ($sw.Elapsed.TotalSeconds -lt 30) "stage-ceiling-adopt ($variant): killed at the bound, not run to its 40 s end: $([math]::Round($sw.Elapsed.TotalSeconds, 1)) s"
                $typed = ($r.stderr -match 'STAGE_EXEMPT_CEILING_EXCEEDED: ')
                if ($v.typed) {
                    Assert-True $typed "stage-ceiling-adopt ($variant): an adopted stage child killed at the ceiling carries the typed reason: $($r.stderr)"
                    Assert-True (@($script:Log | Where-Object { $_ -like "heavy-slot-gate: STAGE_EXEMPT_CEILING_EXCEEDED job=$id *" }).Count -eq 1) "stage-ceiling-adopt ($variant): typed log line"
                }
                else { Assert-True (-not $typed) "stage-ceiling-adopt ($variant): no ceiling reason when the bound was not the ceiling's: $($r.stderr)" }
                if ($variant -eq 'stage-clamped') { Assert-True ($r.admission.stageCeiling.clamped -eq $true) "stage-ceiling-adopt ($variant): the recovered admission keeps stageCeiling" }
                if ($variant -eq 'legacy-over-ceiling') { Assert-True ($r.timeoutSec -eq 1) "stage-ceiling-adopt ($variant): the result reports the clamped bound, not the old marker's 3600 s: $($r.timeoutSec)" }
                $per[$variant] = [ordered]@{ exit = $r.exitCode; typed = $typed; killedPids = @($r.killedProcessIds); childGone = $childGone }
            }
        }
        finally { $StageExemptMaxSec = $savedCeiling }
        $results.stageCeilingAdopt = $per
    }
    if ($Only.Count -gt 0) { $results.only = @($Only) }
}
finally {
    foreach ($l2 in @($script:QuietLease, $script:QuietLeaseWaiting)) { if ($null -ne $l2) { try { $null = Stop-QuietLease -Lease $l2 -Why 'test cleanup' } catch { } } }
    Remove-Item -LiteralPath $fx -Recurse -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
