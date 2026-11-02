[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # The REAL heavy-slot.py (owned by the Bachelor heat-coordination session; read here, never edited). The test runs
    # a byte-identical copy against a private slot dir and a fake feed (HEAVY_SLOT_DIR / HEAVY_SLOT_FEED overrides).
    [string]$HeavySlotScript = $(if ($env:MLV_HEAVY_SLOT_PY) { $env:MLV_HEAVY_SLOT_PY } else { '\\bachelor\Users\layib\.claude\ops\heavy-slot.py' }),
    [string]$Python = $(if ($env:MLV_HEAVY_SLOT_PYTHON) { $env:MLV_HEAVY_SLOT_PYTHON } else { (Get-Command python.exe -ErrorAction SilentlyContinue).Source }),
    # 113D1F61 (2026-10-11 18:54 CDT) = 8A9FFAAA plus the docstring's "STABLE CONTRACT (MLV-App agent fix r1c ...)"
    # block, which pins exactly the surface this agent uses. A mismatch is reported in the output, not failed.
    [string]$ExpectHeavySlotSha256Prefix = '113D1F610C90F374',
    [switch]$UnitOnly,
    [string[]]$Only = @(),
    # Overrides the agent under test (the RED run points this at a copy of the base agent).
    [string]$AgentScript = '',
    # Wall-clock bounds of the two sub-suites -UnitOnly runs (measured 272 s and 48 s); a suite past its bound fails by name.
    # The range is refused at binding, before any child exists: -1 or 2147484 would otherwise throw in WaitForExit after the launch.
    [ValidateRange(1, 3600)][int]$QuietLeaseBoundSec = 400,
    [ValidateRange(1, 3600)][int]$StageExemptBoundSec = 120
)

# AGENT-HEAVY-SLOT-WAIT-RELEASE-1 r1c: the Bachelor agent's heavy-slot gate
# (tools/profiling/bachelor/ultra-magnus-agent.ps1).
#   unit  - the SUSTAINED heat rule (spikes with 0 throttled rows do not block; > 6/60 throttled or a 60-row mean
#           >= 88 C does), occupancy with the job's own slot excluded, and the verdict precedence.
#           r2: the own slot is excluded only when held by this job's label AND owner pid; a holder lost before,
#           during or after the status probe (sol's r1 repro) is holder-lost, never clear; and the pre-launch
#           revalidation refuses a launch whose slot is no longer proven this job's.
#   e2e   - the real agent and the real heavy-slot.py: a WAITING job holds no slot (only heavy-slot.py's FIFO
#           ticket); a heavy job runs only through `heavy-slot.py run --exclusive --label "mlv <jobId>"` and only when
#           no other slot is held (busy == 0); a live ticket ahead is served first; a dead one is reclaimed by
#           heavy-slot.py; an acquired slot the agent cannot use is released before the job re-queues; cancel and
#           agent restart leave no holder behind; light probes stay exempt.
# Prints one JSON object with status PASS, or throws. No playback, no venue: everything is local and private.

$ErrorActionPreference = 'Stop'
$agentScript = if ($AgentScript) { $AgentScript } else { Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1' }
$results = [ordered]@{}

function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }
# Under `pwsh -File`, -Only a,b arrives as ONE string "a,b": split it, and refuse a name that selects nothing, so a
# typo can never turn into a PASS with no case run.
$caseNames = @('waiting', 'fifo', 'heat', 'requeue', 'cancel', 'restart', 'exempt', 'maint')
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($n in $Only) { if ($caseNames -notcontains $n) { throw "unknown case '$n' (known: $($caseNames -join ', '))" } }
function Want([string]$Name) { return ($Only.Count -eq 0 -or $Only -contains $Name) }

# ---------------------------------------------------------------- unit
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) "agent parses ($($parseErrors.Count) errors)"
foreach ($name in @('ConvertFrom-HeavySlotFeedRows', 'ConvertFrom-HeavySlotHolderText', 'Get-SustainedHeatVerdict', 'Get-HeavySlotOccupancy', 'Get-HeavySlotVerdict')) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    Assert-True ($null -ne $fn) "agent defines $name"
    . ([scriptblock]::Create($fn.Extent.Text))
}
# The agent's defaults, read from its param block (not restated here).
$paramDefaults = @{}
foreach ($p in $ast.ParamBlock.Parameters) { if ($p.DefaultValue -is [System.Management.Automation.Language.ConstantExpressionAst]) { $paramDefaults[$p.Name.VariablePath.UserPath] = $p.DefaultValue.Value } }
$GateSustainedRows = [int]$paramDefaults['GateSustainedRows']
$GateMaxThrottledRows = [int]$paramDefaults['GateMaxThrottledRows']
$GateMeanMaxC = [double]$paramDefaults['GateMeanMaxC']
# Read by Get-HeavySlotLabel, which the wait calls; defined before the first Wait-HeavySlotAdmission case.
$GateLongJobSec = [int]$paramDefaults['GateLongJobSec']
$GateLongJobPattern = [string]$paramDefaults['GateLongJobPattern']
$JobTimeoutSec = [int]$paramDefaults['JobTimeoutSec']
Assert-True ($GateSustainedRows -eq 60 -and $GateMaxThrottledRows -eq 6 -and $GateMeanMaxC -eq 88) "defaults are heavy-slot.py's first-run rule (60 rows, > 6 throttled, mean >= 88); got $GateSustainedRows/$GateMaxThrottledRows/$GateMeanMaxC"

function New-FeedRows([double[]]$Temps, [bool[]]$Throttled, [double]$Watts = 40) {
    $rows = @('Date,Time,Total CPU Usage [%],CPU Package [C],CPU Package Power [W],Core Thermal Throttling (avg) [Yes/No],Package/Ring Thermal Throttling [Yes/No]')
    for ($i = 0; $i -lt $Temps.Count; $i++) {
        $t = if ($Throttled[$i]) { 'Yes' } else { 'No' }
        $rows += ('10.11.2026,3:{0:D2}:{1:D2}.000,50.0,{2},{3},{4},No' -f [int]($i / 60), ($i % 60), $Temps[$i].ToString([Globalization.CultureInfo]::InvariantCulture), $Watts, $t)
    }
    return $rows
}
function Get-Heat([string[]]$Rows) { return Get-SustainedHeatVerdict -Metrics (ConvertFrom-HeavySlotFeedRows -Lines $Rows) }

$unit = [ordered]@{}
# Turbo spikes: every other row touches 100 C (TjMax), none throttled, mean 85 C -> NOT blocked. (r1k read this as hot.)
$spikes = Get-Heat (New-FeedRows -Temps (1..60 | % { if ($_ % 2) { 100 } else { 70 } }) -Throttled (1..60 | % { $false }))
Assert-True ($spikes.state -eq 'clear') "turbo spikes (max 100, 0/60 throttled, mean 85) do not block: $($spikes.why)"
$unit.spikes = $spikes.why
$t6 = Get-Heat (New-FeedRows -Temps (1..60 | % { 70 }) -Throttled (1..60 | % { $_ -le 6 }))
Assert-True ($t6.state -eq 'clear') "6/60 throttled does not block: $($t6.why)"
$t7 = Get-Heat (New-FeedRows -Temps (1..60 | % { 70 }) -Throttled (1..60 | % { $_ -le 7 }))
Assert-True ($t7.state -eq 'hot') "7/60 throttled blocks: $($t7.why)"
$m88 = Get-Heat (New-FeedRows -Temps (1..60 | % { 88 }) -Throttled (1..60 | % { $false }))
Assert-True ($m88.state -eq 'hot') "mean 88.0 C blocks: $($m88.why)"
$m879 = Get-Heat (New-FeedRows -Temps (1..60 | % { 87.9 }) -Throttled (1..60 | % { $false }))
Assert-True ($m879.state -eq 'clear') "mean 87.9 C does not block: $($m879.why)"
# Only the LAST 60 rows count: 90 older throttled, hot rows then 60 clean rows -> clear.
$old = Get-Heat (New-FeedRows -Temps ((1..90 | % { 99 }) + (1..60 | % { 60 })) -Throttled ((1..90 | % { $true }) + (1..60 | % { $false })))
Assert-True ($old.state -eq 'clear') "only the last 60 rows count: $($old.why)"
$few = Get-Heat (New-FeedRows -Temps (1..14 | % { 60 }) -Throttled (1..14 | % { $false }))
Assert-True ($few.state -eq 'feed-unknown') "fewer than 15 rows is feed-unknown, never clear"
$bad = Get-Heat ((New-FeedRows -Temps (1..30 | % { 60 }) -Throttled (1..30 | % { $false })) + '10.11.2026,3:59:59.000,50.0,n/a,40,No,No')
Assert-True ($bad.state -eq 'feed-unknown') "an unparseable data row is feed-unknown (heavy-slot.py: ValueError -> None)"
$unit.thresholds = [ordered]@{ t6 = $t6.state; t7 = $t7.state; mean88 = $m88.state; mean879 = $m879.state; last60Only = $old.state; rows14 = $few.state; badRow = $bad.state }

$clearFeed = @{ metrics = (ConvertFrom-HeavySlotFeedRows -Lines (New-FeedRows -Temps (1..60 | % { 60 }) -Throttled (1..60 | % { $false }))); why = $null }
$hotFeed = @{ metrics = (ConvertFrom-HeavySlotFeedRows -Lines (New-FeedRows -Temps (1..60 | % { 70 }) -Throttled (1..60 | % { $_ -le 7 }))); why = $null }
$statusOwn = @{ ok = $true; error = $null; lines = @('slot 1: mlv j1 (pid 4242, since 2026-10-11 18:00:00) [exclusive]', 'slot 2: free', 'thermal: max 99 C, throttled 0/15, 40 W over ~30 s', 'queue 1: suite x (pid 77, since 2026-10-11 18:00:01)', 'queue 2: mlv j2 (pid 78, since 2026-10-11 18:00:02)') }
$vOwn = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 1 -OwnPid 4242 -Label 'mlv j1'
Assert-True ($vOwn.state -eq 'clear' -and $vOwn.busy -eq 0) "the job's OWN slot is not counted as busy (r1k counted its own reservation): $($vOwn.state) busy=$($vOwn.busy)"
$vOther = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 0
Assert-True ($vOther.state -eq 'busy' -and $vOther.busy -eq 1) "another holder is busy: $($vOther.state)"
$vGone = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 2
Assert-True ($vGone.state -eq 'status-unknown') "an own slot shown free is unknown, never clear: $($vGone.why)"
$vHot = Get-HeavySlotVerdict -Status $statusOwn -Feed $hotFeed -OwnSlot 1 -OwnPid 4242 -Label 'mlv j1'
Assert-True ($vHot.state -eq 'hot') "sustained heat blocks a job alone on the machine"
$vQ = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -Label 'mlv j2'
Assert-True ($vQ.queuePos -eq 2 -and $vQ.queueLen -eq 2) "FIFO position read from status queue lines: $($vQ.queuePos)/$($vQ.queueLen)"
$vUnk = Get-HeavySlotVerdict -Status @{ ok = $false; lines = @(); error = 'status timed out after 30 s' } -Feed $clearFeed
Assert-True ($vUnk.state -eq 'status-unknown') "a failed status probe is unknown, never empty"
$vFeed = Get-HeavySlotVerdict -Status $statusOwn -Feed @{ metrics = $null; why = 'feed stale (300 s > 120 s)' } -OwnSlot 1 -OwnPid 4242 -Label 'mlv j1'
Assert-True ($vFeed.state -eq 'feed-unknown') "an unknown feed is never clear"
$unit.verdicts = [ordered]@{ ownSlotExcluded = $vOwn.state; otherHolder = $vOther.state; ownSlotFree = $vGone.state; hotAlone = $vHot.state; queuePos = "$($vQ.queuePos)/$($vQ.queueLen)"; statusFailed = $vUnk.state; feedUnknown = $vFeed.state }

# r2 (sol r1 BLOCKER): the own slot is excluded ONLY when status shows it held by this job's label AND owner pid.
# Anyone else in it means the holder lost it: holder-lost, that run counts as busy, never clear.
$statusTaken = @{ ok = $true; error = $null; lines = @('slot 1: suite X (pid 77, since 2026-10-11 18:00:00) [exclusive]', 'slot 2: free', 'thermal: max 60 C, throttled 0/15, 40 W over ~30 s') }
$vTaken = Get-HeavySlotVerdict -Status $statusTaken -Feed $clearFeed -OwnSlot 1 -OwnPid 4242 -Label 'mlv j1'
Assert-True ($vTaken.state -eq 'holder-lost' -and $vTaken.busy -eq 1) "owner mismatch (suite X pid 77 in the own slot) is holder-lost with busy 1, never excluded: $($vTaken.state) busy=$($vTaken.busy) $($vTaken.why)"
$statusPid = @{ ok = $true; error = $null; lines = @('slot 1: mlv j1 (pid 99, since 2026-10-11 18:00:00) [exclusive]', 'slot 2: free') }
$vPid = Get-HeavySlotVerdict -Status $statusPid -Feed $clearFeed -OwnSlot 1 -OwnPid 4242 -Label 'mlv j1'
Assert-True ($vPid.state -eq 'holder-lost' -and $vPid.busy -eq 1) "same label, other owner pid is holder-lost: $($vPid.state)"
$vLabel = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 1 -OwnPid 4242 -Label 'mlv j9'
Assert-True ($vLabel.state -eq 'holder-lost') "same owner pid, other label is holder-lost: $($vLabel.state)"
$vBare = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 1
Assert-True ($vBare.state -eq 'holder-lost' -and $vBare.busy -eq 1) "an own slot with no owner identity to verify is never excluded: $($vBare.state)"
$unit.ownerCheck = [ordered]@{ foreignOwner = $vTaken.state; otherPid = $vPid.state; otherLabel = $vLabel.state; noIdentity = $vBare.state }
# AGENT-LEASE-PENDING-HEAT-TRACE-1 (adversary r3 A2): an owner label may itself contain "(pid N)" (job id 'suite (pid 12)' -> label 'mlv suite (pid 12)'); the
# metadata group is the LAST "(pid N, since ..., held M min)" before the optional [mode] tag, so the own slot is still recognised as this holder's.
$statusParen = @{ ok = $true; error = $null; lines = @('slot 1: mlv suite (pid 12) (pid 77, since 2026-10-25 10:00:00, held 5 min) [exclusive]', 'slot 2: free') }
$oParen = Get-HeavySlotOccupancy -Status $statusParen -OwnSlot 1 -OwnPid 77 -Label 'mlv suite (pid 12)'
Assert-True ($oParen.ok -and -not $oParen.ownLost -and $oParen.busy -eq 0) "label containing '(pid 12)': the own slot is still this holder's (ownLost=$($oParen.ownLost) busy=$($oParen.busy) why=$($oParen.why))"
$oParenOther = Get-HeavySlotOccupancy -Status $statusParen -OwnSlot 1 -OwnPid 78 -Label 'mlv suite (pid 12)'
Assert-True ($oParenOther.ownLost -and $oParenOther.busy -eq 1) "label containing '(pid 12)', other owner pid 78: still holder-lost (ownLost=$($oParenOther.ownLost))"
$unit.ownerParenLabel = [ordered]@{ own = $oParen.ownLost; otherPid = $oParenOther.ownLost }

# r2: sol's exact repro, through the agent's REAL Wait-HeavySlotAdmission / Get-HeavySlotHolderState /
# Test-HeavySlotHolderHeld / Confirm-HeavySlotHeldForLaunch, with the holder process, status and feed mocked.
foreach ($name in @('Read-JsonFile', 'Get-HeavySlotHolderState', 'Test-HeavySlotHolderHeld', 'Add-HeavySlotHolderLoss', 'Add-HeavySlotHolderLossToAdmission', 'Wait-HeavySlotAdmission', 'Confirm-HeavySlotHeldForLaunch', 'Invoke-ExemptJobsDuringWait', 'Get-HeavySlotLabel', 'ConvertFrom-HeavySlotHolderText', 'Format-HeavySlotHolderNote')) {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    Assert-True ($null -ne $fn) "agent defines $name"
    . ([scriptblock]::Create($fn.Extent.Text))
}
$mockDir = Join-Path ([IO.Path]::GetTempPath()) ("um-agent-holder-lost-{0}-{1}" -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $mockDir | Out-Null
$cancel = $mockDir
$HeavySlotDir = $mockDir
$GateProbeSec = 15
$script:GateVersion = 'unit'
$script:GateHour = @{ admitted = 0; waitSecTotal = 0; requeued = 0 }
$script:HolderLossByJob = @{}
$script:Mock = @{}
function Add-AgentLogLine([string]$Line) { $script:Mock.log += @($Line) }
function Write-AgentHeartbeat([string]$Activity) { }
function Add-GateProbeRecord { param([string]$JobId, $Verdict, [double]$WaitedSec, [string]$Phase, [string]$WaitState) $script:Mock.probes += @($Verdict.state) }
function Get-QueueAgeSeconds { param($Job, $Metadata) if ($script:Mock.expire) { 999 } else { 0 } }
function Read-HeavySlotFeedMetrics { return $clearFeed }
function New-MockHolder([string]$Held) {
    $acq = Join-Path $mockDir ("{0}.acquired.json" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($acq, (@{ held = $Held; sentinelPid = 1 } | ConvertTo-Json -Compress))
    return [pscustomobject]@{ ok = $true; id = 'j1.x'; jobId = 'j1'; label = 'mlv j1'; pid = [int]($Held -split ':')[1]; acquiredFile = $acq; releaseFile = "$acq.release"; recordFile = "$acq.holder.json"
        launch = [pscustomobject]@{ Process = [pscustomobject]@{ HasExited = $false; ExitCode = 0 } } }
}
function Start-HeavySlotHolder { param([string]$JobId, [string]$Label) $script:Mock.starts++; return $script:Mock.holder }
function Stop-HeavySlotHolder { param($Holder, [string]$Why) $script:Mock.stops += @($Why); $script:Mock.expire = $true }
function Exit-HeavySlot { $h = $script:HeavySlotHolder; $script:HeavySlotHolder = $null; if ($null -ne $h) { Stop-HeavySlotHolder -Holder $h -Why 'job done' } }
function Invoke-HeavySlotStatus {
    param([string]$HeartbeatActivity = '')
    if ($script:Mock.exitDuringStatus) { $script:Mock.holder.launch.Process.HasExited = $true; $script:Mock.holder.launch.Process.ExitCode = 1 }
    return $script:Mock.status
}
function Reset-Mock([string]$Held, $StatusLines, [bool]$ExitDuringStatus) {
    $script:Mock = @{ log = @(); probes = @(); stops = @(); starts = 0; expire = $false; exitDuringStatus = $ExitDuringStatus
        holder = (New-MockHolder $Held); status = @{ ok = $true; error = $null; lines = @($StatusLines) } }
    $script:HolderLossByJob = @{}
}
$takenLines = @('slot 1: suite X (pid 77, since 2026-10-11 18:00:00) [exclusive]', 'slot 2: free')
try {
    # 1. sol's repro: acquired HEAVY_SLOT_HELD=1:4242, the holder exits DURING the status probe, suite X (pid 77) now
    #    holds slot 1, 60 cool unthrottled feed rows. r1c returned clear / busy=0 / exclusive. Now: holder-lost, never clear.
    Reset-Mock '1:4242' $takenLines $true
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata ([pscustomobject]@{ maxQueueAgeSec = 1 })
    Assert-True ($adm.label -ne 'clear' -and $adm.label -eq 'queue-expired') "sol repro: never admitted (label $($adm.label))"
    Assert-True ($null -eq $script:HeavySlotHolder) "sol repro: no admitted holder is left"
    Assert-True (@($script:Mock.probes) -contains 'holder-lost' -and @($script:Mock.probes) -notcontains 'clear') "sol repro: the acquired probe is holder-lost, never clear: $($script:Mock.probes -join ',')"
    $loss = @($script:HolderLossByJob['j1'])
    Assert-True ($loss.Count -eq 1 -and $loss[0].reason -eq 'holder-lost' -and $loss[0].phase -eq 'acquired' -and $loss[0].held -eq '1:4242') "sol repro: one typed holder-lost event recorded: $($loss | ConvertTo-Json -Compress)"
    Assert-True (@($script:Mock.stops).Count -ge 1 -and $script:Mock.stops[0] -match 'holder-lost') "sol repro: the holder is ended with the typed reason: $($script:Mock.stops -join ' | ')"
    Assert-True (@($script:Mock.log | Where-Object { $_ -match '^heavy-slot-gate: holder-lost job=j1 phase=acquired' }).Count -eq 1) "sol repro: holder-lost is logged"
    Add-HeavySlotHolderLossToAdmission -JobId 'j1' -Admission $adm
    Assert-True (@($adm.holderLost).Count -eq 1 -and $adm.holderLost[0].reason -eq 'holder-lost') "sol repro: the event reaches the job's admission record (and so its result)"
    $unit.holderLost = [ordered]@{ solRepro = $script:Mock.probes[0]; label = $adm.label; event = $loss[0].why }

    # 2. owner mismatch with the holder still alive: suite X sits in the slot HEAVY_SLOT_HELD names -> holder-lost.
    Reset-Mock "1:$PID" $takenLines $false
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata ([pscustomobject]@{ maxQueueAgeSec = 1 })
    Assert-True ($adm.label -eq 'queue-expired' -and $script:Mock.probes[0] -eq 'holder-lost') "owner mismatch: holder-lost, not admitted: $($script:Mock.probes -join ',')"
    $unit.holderLost.ownerMismatch = $script:Mock.probes[0]

    # Positive control: the own slot shown held by 'mlv j1' with the live owner pid is admitted (clear, busy 0).
    Reset-Mock "1:$PID" @("slot 1: mlv j1 (pid $PID, since 2026-10-11 18:00:00) [exclusive]", 'slot 2: free') $false
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata ([pscustomobject]@{ maxQueueAgeSec = 1 })
    Assert-True ($adm.label -eq 'clear' -and [int]$adm.busy -eq 0 -and $null -ne $script:HeavySlotHolder) "control: a verified own slot admits: $($adm.label)"
    $script:Admission = $adm

    # 3. pre-launch revalidation (TOCTOU). Verified slot -> ok; holder exits during the pre-launch probe and suite X
    #    takes the slot -> refused, holder-lost; holder alive but another owner pid in the slot -> refused.
    $pre = Confirm-HeavySlotHeldForLaunch -JobId 'j1'
    Assert-True ($pre.ok) "pre-launch: a still-held, verified slot passes: $($pre.why)"
    $script:Mock.status = @{ ok = $true; error = $null; lines = $takenLines }
    $script:Mock.exitDuringStatus = $true
    $pre = Confirm-HeavySlotHeldForLaunch -JobId 'j1'
    Assert-True (-not $pre.ok -and $pre.reason -eq 'holder-lost') "pre-launch: holder exits during the probe -> refused holder-lost: $($pre.reason) $($pre.why)"
    $script:Mock.holder.launch.Process.HasExited = $false
    $script:Mock.exitDuringStatus = $false
    $script:Mock.status = @{ ok = $true; error = $null; lines = @('slot 1: mlv j1 (pid 99, since 2026-10-11 18:00:00) [exclusive]', 'slot 2: free') }
    $preMismatch = Confirm-HeavySlotHeldForLaunch -JobId 'j1'
    Assert-True (-not $preMismatch.ok -and $preMismatch.reason -eq 'holder-lost') "pre-launch: another owner pid in the slot -> refused holder-lost: $($preMismatch.why)"
    $script:Mock.status = @{ ok = $false; error = 'status timed out after 30 s'; lines = @() }
    $preUnknown = Confirm-HeavySlotHeldForLaunch -JobId 'j1'
    Assert-True (-not $preUnknown.ok -and $preUnknown.reason -eq 'holder-lost') "pre-launch: ownership that cannot be confirmed is refused: $($preUnknown.why)"
    $script:HeavySlotHolder = $null
    $preNone = Confirm-HeavySlotHeldForLaunch -JobId 'j1'
    Assert-True (-not $preNone.ok -and $preNone.reason -eq 'holder-lost') "pre-launch: no admitted holder is refused"
    $unit.holderLost.preLaunch = [ordered]@{ exitDuringProbe = $pre.reason; ownerMismatch = $preMismatch.reason; statusUnknown = $preUnknown.reason }

    # 4. maint-dispatch r1: a job in its gate-wait runs the exempt (light) jobs waiting in inbox\ -- and only those -- in
    #    Name order, once per scan. The waiting job, an ordinary job, a quarantined job and a recovery-pending job are
    #    untouched, and the stray output of the dispatched job must not join the admission record the wait returns.
    $inbox = Join-Path $mockDir 'inbox'
    $mdProcessed = Join-Path $mockDir 'processed'
    New-Item -ItemType Directory -Force -Path $inbox, $mdProcessed | Out-Null
    $GateExemptJobPattern = [string]$paramDefaults['GateExemptJobPattern']
    $script:RecoveryQuarantine = @{ 'agent-swap-Q' = 'q' }
    $script:ExemptDuringWaitOf = $null
    function Get-JobRecoveryPending { param($JobId) if ($JobId -eq 'dve-p-health') { 'pending' } else { $null } }
    function Invoke-QueuedJob {
        param($Job)
        $script:Mock.dispatched += @($Job.BaseName -replace '\.job$', '')
        Move-Item -LiteralPath $Job.FullName -Destination $mdProcessed
        Write-Output 'stray'
    }
    # STAGE-JOB-LEASE-EXEMPT-1: a staging job skips the gate when it is next, but is never run from inside another job's wait.
    foreach ($n in @('j1', 'jO', 'agent-swap-U1', 'venue-quiet-probe-U2', 'agent-swap-Q', 'dve-p-health', 'playback-attr-3-cuda-stage-8ea38dff267b')) { [IO.File]::WriteAllText((Join-Path $inbox "$n.job.ps1"), '# mock') }
    Reset-Mock "1:$PID" @("slot 1: mlv j1 (pid $PID, since 2026-10-11 18:00:00) [exclusive]", 'slot 2: suite X (pid 77, since 2026-10-11 18:00:01) [exclusive]') $false
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata ([pscustomobject]@{ maxQueueAgeSec = 1 })
    $dispatched = @($script:Mock.dispatched)
    Assert-True (($dispatched -join ',') -eq 'agent-swap-U1,venue-quiet-probe-U2') "maint-dispatch: exactly the two eligible light jobs ran, in Name order: [$($dispatched -join ',')]"
    foreach ($n in @('j1', 'jO', 'agent-swap-Q', 'dve-p-health', 'playback-attr-3-cuda-stage-8ea38dff267b')) { Assert-True (Test-Path -LiteralPath (Join-Path $inbox "$n.job.ps1") -PathType Leaf) "maint-dispatch: $n was left in inbox\" }
    Assert-True ($adm -isnot [array] -and $adm.label -eq 'queue-expired') "maint-dispatch: the wait returns ONE admission object (the dispatched job's output is not part of it): $(@($adm).Count) item(s), label $($adm.label)"
    Assert-True (@($script:Mock.log | Where-Object { $_ -match '^heavy-slot-gate: maint-dispatch exempt job=' }).Count -eq 2) "maint-dispatch: two maint-dispatch log lines"
    Assert-True ($null -eq $script:ExemptDuringWaitOf) "maint-dispatch: the during-wait marker is cleared"
    $unit.maintDispatch = [ordered]@{ dispatched = $dispatched; singleObject = ($adm -isnot [array]); label = $adm.label }

    # 5. AGENT-HEAVY-SLOT-2.3-ADAPT-1. heavy-slot.py 2.3.1 turns "short exclusive first" off for every tenant when a
    #    holder whose label starts with the literal "mlv " (trailing space) runs past 15 min. A gated job that can
    #    legitimately run longer is therefore labelled "mlv-long <jobId>", which that prefix test does not match.
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-JobMetadata' }, $true)
    Assert-True ($null -ne $fn) "agent defines Get-JobMetadata"
    . ([scriptblock]::Create($fn.Extent.Text))
    Assert-True ($GateLongJobSec -eq 900) "GateLongJobSec defaults to 900 (15 min is the 2.3.1 cap); got $GateLongJobSec"
    Assert-True ($JobTimeoutSec -eq 1800) "the agent's default job timeout is 1800 s; got $JobTimeoutSec"
    $labelCases = [ordered]@{}
    foreach ($c in @(
            @{ k = 'at900'; id = 'jobA'; t = 900; want = 'mlv jobA' }
            @{ k = 'at901'; id = 'jobA'; t = 901; want = 'mlv-long jobA' }
            @{ k = 'at1'; id = 'jobA'; t = 1; want = 'mlv jobA' }
            @{ k = 'stage'; id = 'playback-attr-3-cuda-stage-1a2b3c'; t = 60; want = 'mlv-long playback-attr-3-cuda-stage-1a2b3c' }
            @{ k = 'build'; id = 'um-build-7'; t = 60; want = 'mlv-long um-build-7' }
            @{ k = 'soak'; id = 'soak.run1'; t = 60; want = 'mlv-long soak.run1' }
            @{ k = 'kindUpper'; id = 'ATTR3-STAGE'; t = 60; want = 'mlv-long ATTR3-STAGE' }
            @{ k = 'notAToken'; id = 'upstage-1'; t = 60; want = 'mlv upstage-1' }
            @{ k = 'notAToken2'; id = 'rebuilder-2'; t = 60; want = 'mlv rebuilder-2' }
            @{ k = 'ordinary'; id = 'playback-smoke-0001'; t = 300; want = 'mlv playback-smoke-0001' })) {
        $got = Get-HeavySlotLabel -JobId $c.id -TimeoutSec $c.t
        Assert-True ($got -ceq $c.want) "label $($c.k): job '$($c.id)' timeoutSec $($c.t) -> '$($c.want)', got '$got'"
        $labelCases[$c.k] = $got
    }
    # The effective timeout is the one the agent applies: Get-JobMetadata. No meta.json, or an unusable timeoutSec, is the
    # 1800 s default -- so an unset timeout is LONG; only an explicit 1..900 stays short.
    $metaCases = [ordered]@{}
    foreach ($c in @(
            @{ k = 'noMeta'; id = 'mNone'; body = $null; want = 'mlv-long mNone' }
            @{ k = 'metaNoTimeout'; id = 'mNoT'; body = '{"jobId":"mNoT"}'; want = 'mlv-long mNoT' }
            @{ k = 'zero'; id = 'mZero'; body = '{"jobId":"mZero","timeoutSec":0}'; want = 'mlv-long mZero' }
            @{ k = 'overRange'; id = 'mBig'; body = '{"jobId":"mBig","timeoutSec":90000}'; want = 'mlv-long mBig' }
            @{ k = 'garbage'; id = 'mGar'; body = '{"jobId":"mGar","timeoutSec":"soon"}'; want = 'mlv-long mGar' }
            @{ k = 'explicit600'; id = 'm600'; body = '{"jobId":"m600","timeoutSec":600}'; want = 'mlv m600' }
            @{ k = 'explicit900'; id = 'm900'; body = '{"jobId":"m900","timeoutSec":900}'; want = 'mlv m900' }
            @{ k = 'explicit901'; id = 'm901'; body = '{"jobId":"m901","timeoutSec":901}'; want = 'mlv-long m901' })) {
        if ($null -ne $c.body) { [IO.File]::WriteAllText((Join-Path $inbox "$($c.id).meta.json"), $c.body) }
        $md = Get-JobMetadata -JobId $c.id
        $got = Get-HeavySlotLabel -JobId $c.id -TimeoutSec $md.timeoutSec
        Assert-True ($got -ceq $c.want) "metadata label $($c.k): '$($c.want)' expected, got '$got' (effective timeoutSec $($md.timeoutSec))"
        $metaCases[$c.k] = "$($md.timeoutSec) -> $got"
    }
    # The label the wait matches its own slot on is the new one: a slot shown under the old label is NOT this job's.
    $longMeta = [pscustomobject]@{ maxQueueAgeSec = 1; timeoutSec = 1800 }
    Reset-Mock "1:$PID" @("slot 1: mlv-long j1 (pid $PID, since 2026-10-11 18:00:00, held 1 min) [exclusive]", 'slot 2: free') $false
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata $longMeta
    Assert-True ($adm.label -eq 'clear' -and $adm.heavySlotLabel -ceq 'mlv-long j1') "wait: a long job admits on its own 'mlv-long j1' slot: $($adm.label) / $($adm.heavySlotLabel)"
    $script:HeavySlotHolder = $null
    Reset-Mock "1:$PID" @("slot 1: mlv j1 (pid $PID, since 2026-10-11 18:00:00, held 1 min) [exclusive]", 'slot 2: free') $false
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata $longMeta
    Assert-True ($adm.label -eq 'queue-expired' -and $script:Mock.probes[0] -eq 'holder-lost') "wait: a long job does not take a slot labelled 'mlv j1' as its own: $($adm.label) $($script:Mock.probes -join ',')"
    $unit.longLabel = [ordered]@{ cases = $labelCases; metadata = $metaCases; waitAdmitsOnLongLabel = 'clear'; waitRejectsShortLabel = $script:Mock.probes[0] }

    # 5a. UM-AGENT-HEAVY-SLOT-OVERRIDE-BOUNDS-VALIDATE-1. The bounds the agent documents for -GateLongJobSec and
    #     -GateLongJobPattern are enforced at parameter binding: an out-of-bounds override is refused with ONE typed token
    #     (HEAVY_SLOT_OVERRIDE_OUT_OF_BOUNDS) in the message, and an in-bounds one still binds. The binding is exercised
    #     through a scriptblock built from those two parameters' own AST text (attributes included), so the agent's main
    #     loop never runs and no share is touched.
    $overrideToken = 'HEAVY_SLOT_OVERRIDE_OUT_OF_BOUNDS'
    $boundParams = @($ast.ParamBlock.Parameters | Where-Object { @('GateLongJobSec', 'GateLongJobPattern') -contains $_.Name.VariablePath.UserPath })
    Assert-True ($boundParams.Count -eq 2) "agent param block declares GateLongJobSec and GateLongJobPattern (found $($boundParams.Count))"
    $bindSb = [scriptblock]::Create("param(`n" + (($boundParams | ForEach-Object { $_.Extent.Text }) -join ",`n") + "`n)`n[pscustomobject]@{ sec = `$GateLongJobSec; pattern = `$GateLongJobPattern }")
    function Test-OverrideBinding([hashtable]$Splat) {
        try { $r = & $bindSb @Splat; return [pscustomobject]@{ ok = $true; sec = $r.sec; pattern = $r.pattern; message = '' } }
        catch { return [pscustomobject]@{ ok = $false; sec = $null; pattern = $null; message = [string]$_.Exception.Message } }
    }
    $overrideRefused = [ordered]@{}
    $overrideMisses = @()
    foreach ($c in @(
            @{ k = 'sec0'; a = @{ GateLongJobSec = 0 } }
            @{ k = 'secMinus1'; a = @{ GateLongJobSec = -1 } }
            @{ k = 'sec901'; a = @{ GateLongJobSec = 901 } }
            @{ k = 'sec86400'; a = @{ GateLongJobSec = 86400 } }
            @{ k = 'patBadRegex'; a = @{ GateLongJobPattern = '(' } }
            @{ k = 'patBadClass'; a = @{ GateLongJobPattern = '[a-' } }
            @{ k = 'patEmpty'; a = @{ GateLongJobPattern = '' } }
            @{ k = 'patBlank'; a = @{ GateLongJobPattern = '   ' } }
            @{ k = 'patMatchAll'; a = @{ GateLongJobPattern = '.*' } }
            @{ k = 'patMatchAllAnchor'; a = @{ GateLongJobPattern = '^' } }
            @{ k = 'patMatchAllOptional'; a = @{ GateLongJobPattern = '(stage|build|soak)?' } }
            @{ k = 'patNoStage'; a = @{ GateLongJobPattern = '(^|[-_.])(build|soak)([-_.]|$)' } }
            @{ k = 'patNoBuild'; a = @{ GateLongJobPattern = '(^|[-_.])(stage|soak)([-_.]|$)' } }
            @{ k = 'patNoSoak'; a = @{ GateLongJobPattern = '(^|[-_.])(stage|build)([-_.]|$)' } }
            @{ k = 'patUnrelated'; a = @{ GateLongJobPattern = 'zzz-only' } })) {
        $r = Test-OverrideBinding $c.a
        # Misses are collected and asserted once below, so a RED run names every case that still binds.
        if ($r.ok) { $overrideMisses += "$($c.k): $(@($c.a.Keys)[0]) = '$(@($c.a.Values)[0])' bound (sec=$($r.sec) pattern=$($r.pattern))" }
        elseif (-not $r.message.Contains($overrideToken)) { $overrideMisses += "$($c.k): refused without ${overrideToken}: $($r.message)" }
        else { $overrideRefused[$c.k] = $overrideToken }
    }
    Assert-True ($overrideMisses.Count -eq 0) "$($overrideMisses.Count) out-of-bounds override(s) not refused with $overrideToken -- $($overrideMisses -join ' || ')"
    $overrideBound = [ordered]@{}
    foreach ($c in @(
            @{ k = 'sec1'; a = @{ GateLongJobSec = 1 }; sec = 1 }
            @{ k = 'sec600'; a = @{ GateLongJobSec = 600 }; sec = 600 }
            @{ k = 'sec900'; a = @{ GateLongJobSec = 900 }; sec = 900 }
            @{ k = 'patDefault'; a = @{ GateLongJobPattern = $GateLongJobPattern }; pattern = $GateLongJobPattern }
            @{ k = 'patWidened'; a = @{ GateLongJobPattern = '(^|[-_.])(stage|build|soak|trace)([-_.]|$)' }; pattern = '(^|[-_.])(stage|build|soak|trace)([-_.]|$)' }
            @{ k = 'patWidenedUnanchored'; a = @{ GateLongJobPattern = 'stage|build|soak|trace' }; pattern = 'stage|build|soak|trace' }
            @{ k = 'patUpperCase'; a = @{ GateLongJobPattern = '(^|[-_.])(STAGE|BUILD|SOAK)([-_.]|$)' }; pattern = '(^|[-_.])(STAGE|BUILD|SOAK)([-_.]|$)' }
            @{ k = 'bothInBounds'; a = @{ GateLongJobSec = 900; GateLongJobPattern = $GateLongJobPattern }; sec = 900; pattern = $GateLongJobPattern })) {
        $r = Test-OverrideBinding $c.a
        Assert-True $r.ok "override accepted $($c.k): an in-bounds override must bind, got: $($r.message)"
        if ($c.ContainsKey('sec')) { Assert-True ($r.sec -eq $c.sec) "override accepted $($c.k): sec bound as $($r.sec), wanted $($c.sec)" }
        if ($c.ContainsKey('pattern')) { Assert-True ($r.pattern -ceq $c.pattern) "override accepted $($c.k): pattern bound as '$($r.pattern)', wanted '$($c.pattern)'" }
        $overrideBound[$c.k] = 'bound'
    }
    # No override: both parameters keep the defaults the label cases above run on (validation does not touch a default).
    $noOverride = Test-OverrideBinding @{}
    Assert-True ($noOverride.ok -and $noOverride.sec -eq $GateLongJobSec -and $noOverride.pattern -ceq $GateLongJobPattern) "no override binds the defaults ($GateLongJobSec / '$GateLongJobPattern'), got $($noOverride.sec) / '$($noOverride.pattern)' $($noOverride.message)"
    $unit.overrideBounds = [ordered]@{ refused = $overrideRefused; bound = $overrideBound; token = $overrideToken }

    # 5b. AGENT-HEAVY-SLOT-LABEL-SINGLE-READ-1. The wait resolves the label ONCE, from the metadata the job was queued
    #     with, and the holder it creates is filed under that same label. Through the REAL Start-HeavySlotHolder (only its
    #     process, record and path helpers mocked) and the REAL Wait-HeavySlotAdmission, the job's meta.json is rewritten
    #     after the wait's metadata was read: 600 s then 1200 s with GateLongJobSec 900 must stay 'mlv', and 1200 s
    #     then 600 s must stay 'mlv-long'. A second read inside the holder start used to file the ticket under the other
    #     label, which Get-HeavySlotVerdict then reads as holder-lost.
    $startFn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-HeavySlotHolder' }, $true)
    Assert-True ($null -ne $startFn) "agent defines Start-HeavySlotHolder"
    $mockStartHolder = ${function:Start-HeavySlotHolder}
    # The names Start-HeavySlotHolder reads are the agent's own script variables; $HeavySlotScript is also this script's
    # -HeavySlotScript parameter, so every one is put back after the case.
    $savedAgentVars = @{}
    foreach ($n in @('HeavySlotPython', 'HeavySlotScript', 'HeavySlotFeed', 'HeavySlotCount', 'slotHolders', 'logs', 'psExe')) {
        $savedAgentVars[$n] = Get-Variable -Name $n -Scope Script -ValueOnly -ErrorAction SilentlyContinue
    }
    . ([scriptblock]::Create($startFn.Extent.Text))
    $HeavySlotPython = 'python-mock'
    $HeavySlotScript = Join-Path $mockDir 'heavy-slot-mock.py'
    [IO.File]::WriteAllText($HeavySlotScript, '# mock')
    $HeavySlotFeed = Join-Path $mockDir 'feed-mock.csv'
    $HeavySlotCount = 2
    $slotHolders = $mockDir
    $logs = $mockDir
    $psExe = 'pwsh-mock'
    $script:AgentProcessStartUtc = 'unit'
    function Resolve-HeavySlotPython { return $HeavySlotPython }
    function New-HeavySlotSentinelCommand { param($AcquiredFile, $ReleaseFile) return 'sentinel-mock' }
    function Get-ProcessIdentity { param($ProcessId) return $null }
    function Write-JsonFileAtomic { param($Path, $InputObject) $script:Mock.record = $InputObject; return [pscustomobject]@{ ok = $true } }
    function Start-WindowlessRedirectedProcess {
        param($FilePath, $ArgumentList, $OutFile, $ErrFile, $Environment)
        $script:Mock.launchArgs = @($ArgumentList)
        $script:Mock.expire = $true     # the next loop turn leaves the wait, so the case is one holder start
        return [pscustomobject]@{ Process = [pscustomobject]@{ Id = 4242; StartTime = (Get-Date); HasExited = $false; ExitCode = 0 } }
    }
    $singleRead = [ordered]@{}
    foreach ($c in @(
            @{ k = 'shortThenLong'; queuedSec = 600; laterSec = 1200; want = 'mlv jslr' }
            @{ k = 'longThenShort'; queuedSec = 1200; laterSec = 600; want = 'mlv-long jslr' })) {
        Assert-True ((($c.queuedSec -le $GateLongJobSec) -ne ($c.laterSec -le $GateLongJobSec))) "single-read $($c.k): the two timeouts straddle GateLongJobSec ($GateLongJobSec)"
        Reset-Mock "1:$PID" @('slot 1: free', 'slot 2: free') $false
        [IO.File]::WriteAllText((Join-Path $inbox 'jslr.meta.json'), ('{{"jobId":"jslr","timeoutSec":{0}}}' -f $c.laterSec))
        $queuedMeta = [pscustomobject]@{ maxQueueAgeSec = 1; timeoutSec = $c.queuedSec }
        $adm = Wait-HeavySlotAdmission -JobId 'jslr' -Job ([pscustomobject]@{}) -Metadata $queuedMeta
        Assert-True ($adm.label -eq 'queue-expired') "single-read $($c.k): the wait ended on the expiry, label $($adm.label)"
        $argv = @($script:Mock.launchArgs)
        $at = [array]::IndexOf($argv, '--label')
        Assert-True ($at -ge 0 -and $at + 1 -lt $argv.Count) "single-read $($c.k): the holder was launched with a --label: [$($argv -join ' ')]"
        $ticketLabel = [string]$argv[$at + 1]
        Assert-True ($ticketLabel -ceq $c.want) "single-read $($c.k): the ticket is filed under the label resolved from the metadata the wait read ('$($c.want)'), got '$ticketLabel' (meta.json now says $($c.laterSec) s)"
        Assert-True ([string]$script:Mock.record.label -ceq $c.want) "single-read $($c.k): the holder record carries the same label: '$($script:Mock.record.label)'"
        Assert-True (@($script:Mock.probes) -notcontains 'holder-lost') "single-read $($c.k): no probe reads the holder as lost: $($script:Mock.probes -join ',')"
        $singleRead[$c.k] = $ticketLabel
    }
    ${function:Start-HeavySlotHolder} = $mockStartHolder
    Remove-Item Function:\Resolve-HeavySlotPython, Function:\New-HeavySlotSentinelCommand, Function:\Get-ProcessIdentity, Function:\Write-JsonFileAtomic, Function:\Start-WindowlessRedirectedProcess -ErrorAction SilentlyContinue
    foreach ($n in $savedAgentVars.Keys) { Set-Variable -Name $n -Scope Script -Value $savedAgentVars[$n] }
    $unit.singleRead = $singleRead

    # 6. The gate-wait heartbeat names the holder it is waiting behind. The prefix stays byte-identical:
    #    um-run.ps1 reads "gate-wait job=<id> state=<s> waited=<n>s" unanchored at the end, and the fifo e2e needs
    #    "state=queued " with a space.
    $ctl = "bad" + [char]9 + "la" + [char]10 + [char]13 + [char]1 + "bel" + ("y" * 100)
    $hn = Format-HeavySlotHolderNote -Holders @("$ctl (pid 5, since 2026-10-11 18:00:00, held 9 min) [exclusive]")
    Assert-True ($hn -match '^ holder=([\x21-\x7E]+(?: [\x21-\x7E]+)*) held=9m$') "holder note is printable ASCII with single spaces only: [$hn]"
    $lab = ($hn -replace '^ holder=', '') -replace ' held=9m$', ''
    Assert-True ($lab.Length -le 64) "holder label is capped at 64 chars: $($lab.Length)"
    Assert-True ($lab.StartsWith('bad la bel') -and $lab -notmatch '\s{2}') "control characters and whitespace collapse to one space: [$lab]"
    Assert-True ((Format-HeavySlotHolderNote -Holders @("suite X (pid 77, since 2026-10-11 18:00:00, held 62 min) [exclusive]")) -ceq ' holder=suite X held=62m') "holder note: plain holder"
    Assert-True ((Format-HeavySlotHolderNote -Holders @("a (pid 1, since S, held 3 min)", "b (pid 2, since S, held 70 min) [exclusive]", "c (pid 3, since S, held 9 min)")) -ceq ' holder=b held=70m') "holder note: the longest-held holder is named"
    Assert-True ((Format-HeavySlotHolderNote -Holders @("old (pid 4, since 2026-10-11 18:00:00) [exclusive]")) -ceq ' holder=old held=?m') "holder note: a status line without a held field gives held=?m"
    Assert-True ((Format-HeavySlotHolderNote -Holders @('mlv suite (pid 12) (pid 77, since 2026-10-25 10:00:00, held 5 min) [exclusive]')) -ceq ' holder=mlv suite (pid 12) held=5m') "holder note (adversary r3 A2): a label containing '(pid 12)' is kept whole and the held minutes are read from the LAST group"
    Assert-True ((Format-HeavySlotHolderNote -Holders @()) -ceq '') "holder note: no holder, no field"
    Assert-True ((Format-HeavySlotHolderNote -Holders $null) -ceq '') "holder note: null holders, no field"
    $label64 = 'x' * 64
    Assert-True ((Format-HeavySlotHolderNote -Holders @(("x" * 70) + " (pid 6, since S, held 1 min)")) -ceq " holder=$label64 held=1m") "holder note: exactly 64 chars kept"
    function Write-AgentHeartbeat([string]$Activity) { $script:Mock.hb += @($Activity); $script:Mock.expire = $true }
    Reset-Mock "1:$PID" @('slot 1: suite X (pid 77, since 2026-10-11 18:00:00, held 62 min) [exclusive]', 'slot 2: free') $false
    Remove-Item -LiteralPath $script:Mock.holder.acquiredFile -Force
    $script:Mock.hb = @()
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata ([pscustomobject]@{ maxQueueAgeSec = 1 })
    Assert-True (@($script:Mock.hb).Count -ge 1) "heartbeat: the wait heartbeats"
    $busyBeat = [string]$script:Mock.hb[0]
    Assert-True ($busyBeat -cmatch '^gate-wait job=j1 state=queued waited=\d+s holder=suite X held=62m$') "heartbeat: busy line carries holder= and held=: [$busyBeat]"
    Assert-True ($busyBeat.StartsWith('gate-wait job=j1 state=queued ')) "heartbeat: the prefix keeps 'state=queued ' with a space"
    $script:Mock.hb = @()
    Reset-Mock "1:$PID" @('slot 1: free', 'slot 2: free') $false
    Remove-Item -LiteralPath $script:Mock.holder.acquiredFile -Force
    $script:Mock.hb = @()
    $adm = Wait-HeavySlotAdmission -JobId 'j1' -Job ([pscustomobject]@{}) -Metadata ([pscustomobject]@{ maxQueueAgeSec = 1 })
    $freeBeat = [string]$script:Mock.hb[0]
    Assert-True ($freeBeat -cmatch '^gate-wait job=j1 state=queued waited=\d+s$') "heartbeat: a free machine adds no holder field, line byte-identical to today's: [$freeBeat]"
    # um-run.ps1's own reader (its function, parsed from the tracked file) still reads the new line.
    $umRunPath = Join-Path $RepoRoot 'tools\profiling\um-run.ps1'
    $umAst = [System.Management.Automation.Language.Parser]::ParseFile($umRunPath, [ref]$null, [ref]$null)
    $umFn = $umAst.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-UmRunGateWaitState' }, $true)
    Assert-True ($null -ne $umFn) "um-run.ps1 defines Get-UmRunGateWaitState"
    . ([scriptblock]::Create($umFn.Extent.Text))
    $hbFile = Join-Path $mockDir 'um-run-heartbeat.txt'
    [IO.File]::WriteAllText($hbFile, "alive 2026-10-19T00:00:00.0000000-05:00 pid=1 host=H generation=9 imagePath=""x"" agentScript=""y"" $busyBeat")
    $umState = Get-UmRunGateWaitState -HeartbeatPath $hbFile -JobId 'j1'
    Assert-True ($umState.State -eq 'queued' -and $umState.AgentWaited -match '^\d+s$') "um-run.ps1 still parses the busy line: state=$($umState.State) waited=$($umState.AgentWaited)"
    $unit.heartbeat = [ordered]@{ busy = ($busyBeat -replace 'waited=\d+s', 'waited=Ns'); free = ($freeBeat -replace 'waited=\d+s', 'waited=Ns'); umRunState = $umState.State; capped = $lab.Length; collapsed = $lab.Substring(0, 10) }
}
finally {
    Remove-Item Function:\Invoke-QueuedJob, Function:\Get-JobRecoveryPending -ErrorAction SilentlyContinue
    $script:HeavySlotHolder = $null
    $script:Admission = $null
    Remove-Item -LiteralPath $mockDir -Recurse -Force -ErrorAction SilentlyContinue
}
$results.unit = $unit

# BACHELOR-AGENT-QUIET-LEASE-1: the quiet-lease cases (lease mode, the default) run in their own process against a fake
# heavy-slot.py, so this script's status-mode mocks never meet them. A failure there fails this run.
# CI-FLAKE-HEAVY-SLOT-GATE-UNIT-HOSTED-HANG-1: each sub-suite runs under a wall-clock bound, so a hung one fails by name (and its
# whole process tree is killed) instead of running to the CI job cap. Measured 272 s (quiet-lease) and 48 s (stage-exempt) locally.
function ConvertTo-CommandLineArg([string]$Value) {
    # Start-Process joins -ArgumentList with spaces and quotes nothing, so each value is quoted by the Windows rules: backslashes
    # before a quote (and before the closing quote) are doubled, or a RepoRoot ending in "\" escapes its closing quote (adversary r4 A1).
    '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}
function Invoke-BoundedSuite([string]$Name, [string]$Script, [int]$TimeoutSec) {
    $outF = [IO.Path]::GetTempFileName(); $errF = [IO.Path]::GetTempFileName()
    try {
        $suiteArgs = @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $Script, '-RepoRoot', $RepoRoot, '-AgentScript', $agentScript) | ForEach-Object { ConvertTo-CommandLineArg $_ }
        $p = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $suiteArgs -NoNewWindow -PassThru -RedirectStandardOutput $outF -RedirectStandardError $errF
        $null = $p.Handle   # keeps ExitCode readable after the exit
        $clock = [Diagnostics.Stopwatch]::StartNew()   # monotonic: a local-time subtraction goes negative across a DST fall-back
        $finished = $p.WaitForExit($TimeoutSec * 1000)
        $killNote = 'its process tree was killed'
        if (-not $finished) {
            # The kill itself is bounded (sol adversary A2): a hung taskkill.exe must not hold the suite-named failure back.
            try {
                $k = Start-Process -FilePath 'taskkill.exe' -ArgumentList @('/F', '/T', '/PID', [string]$p.Id) -NoNewWindow -PassThru
                if (-not $k.WaitForExit(60000)) { $k.Kill(); $killNote = 'its process tree kill is NOT confirmed (taskkill.exe still running after 60 s)' }
                elseif ($k.ExitCode -ne 0) { $killNote = "its process tree kill is NOT confirmed (taskkill.exe exited $($k.ExitCode))" }
            }
            catch { $killNote = "its process tree kill is NOT confirmed (taskkill.exe failed: $($_.Exception.Message))" }
            try { $p.Kill() } catch { }
        }
        $text = ((Get-Content -LiteralPath $outF -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath $errF -ErrorAction SilentlyContinue)) -join ' | '
        Assert-True $finished "$Name ($Script) exceeded its $TimeoutSec s bound (ran $([int]$clock.Elapsed.TotalSeconds) s) and $killNote; output so far: $text"
        # A finished suite's JSON is the result: a read error here must surface, not aggregate as an empty result.
        $json = (Get-Content -LiteralPath $outF -ErrorAction Stop) -join "`n"
        return [pscustomobject]@{ ExitCode = $p.ExitCode; Json = $json; Text = $text }
    }
    finally { Remove-Item -LiteralPath $outF, $errF -Force -ErrorAction SilentlyContinue }
}
$leaseTest = Join-Path $PSScriptRoot 'test-ultra-magnus-agent-quiet-lease.ps1'
$lease = Invoke-BoundedSuite 'quiet-lease cases' $leaseTest $QuietLeaseBoundSec
Assert-True ($lease.ExitCode -eq 0) "quiet-lease cases ($leaseTest) pass: $($lease.Text)"
Assert-True ($null -ne $lease.Json -and $lease.Json.Trim().Length -gt 0) "quiet-lease cases ($leaseTest) produced no JSON result: $($lease.Text)"
$results.quietLease = $lease.Json | ConvertFrom-Json
# STAGE-JOB-LEASE-EXEMPT-1: the staging-job exemption runs the same way (its own process, its own fake heavy-slot.py).
$stageTest = Join-Path $PSScriptRoot 'test-ultra-magnus-agent-stage-exempt.ps1'
$stage = Invoke-BoundedSuite 'stage-exempt cases' $stageTest $StageExemptBoundSec
Assert-True ($stage.ExitCode -eq 0) "stage-exempt cases ($stageTest) pass: $($stage.Text)"
Assert-True ($null -ne $stage.Json -and $stage.Json.Trim().Length -gt 0) "stage-exempt cases ($stageTest) produced no JSON result: $($stage.Text)"
$results.stageExempt = $stage.Json | ConvertFrom-Json

if ($UnitOnly) {
    [pscustomobject]@{ status = 'PASS'; mode = 'unit-only'; results = $results } | ConvertTo-Json -Depth 8
    return
}

# ---------------------------------------------------------------- e2e
if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "python not found (pass -Python or set MLV_HEAVY_SLOT_PYTHON); use -UnitOnly to run the unit cases alone" }
if (-not (Test-Path -LiteralPath $HeavySlotScript -PathType Leaf)) { throw "heavy-slot.py not found at $HeavySlotScript (pass -HeavySlotScript or set MLV_HEAVY_SLOT_PY); use -UnitOnly to run the unit cases alone" }
$hsSha = (Get-FileHash -LiteralPath $HeavySlotScript -Algorithm SHA256).Hash
$runId = 'agent-heavy-slot-fifo-{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID
$base = Join-Path $RepoRoot ".claude-state\testing\$runId"
New-Item -ItemType Directory -Force -Path $base | Out-Null
$hs = Join-Path $base 'heavy-slot.py'
Copy-Item -LiteralPath $HeavySlotScript -Destination $hs
$results.heavySlotPy = [ordered]@{ source = $HeavySlotScript; sha256 = $hsSha; expectedPrefix = $ExpectHeavySlotSha256Prefix; prefixMatches = $hsSha.StartsWith($ExpectHeavySlotSha256Prefix) }
$savedEnv = @{}
foreach ($k in @('HEAVY_SLOT_DIR', 'HEAVY_SLOT_FEED', 'HEAVY_SLOT_SHADOW_LOG', 'HEAVY_SLOT_GPU_W', 'HEAVY_SLOT_HELD')) { $savedEnv[$k] = [Environment]::GetEnvironmentVariable($k) }
$agents = [System.Collections.Generic.List[object]]::new()

function Write-Feed([string]$Path, [double[]]$Temps, [bool[]]$Throttled) {
    $tmp = "$Path.tmp"
    [IO.File]::WriteAllText($tmp, ((New-FeedRows -Temps $Temps -Throttled $Throttled) -join "`r`n") + "`r`n")
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}
function Write-CleanFeed([string]$Path) { Write-Feed $Path (1..60 | % { 60 }) (1..60 | % { $false }) }

function New-Case([string]$Name) {
    $root = Join-Path $base $Name
    New-Item -ItemType Directory -Force -Path "$root\inbox", "$root\slots" | Out-Null
    $env:HEAVY_SLOT_DIR = "$root\slots"
    $env:HEAVY_SLOT_FEED = "$root\feed.csv"
    $env:HEAVY_SLOT_SHADOW_LOG = "$root\shadow.jsonl"
    $env:HEAVY_SLOT_GPU_W = '0'
    Remove-Item Env:\HEAVY_SLOT_HELD -ErrorAction SilentlyContinue
    Write-CleanFeed "$root\feed.csv"
    return [pscustomobject]@{ name = $Name; root = $root; slots = "$root\slots"; queue = "$root\slots\queue"; feed = "$root\feed.csv"; agent = $null }
}
function Start-Agent($c) {
    $si = [Diagnostics.ProcessStartInfo]::new((Get-Command pwsh.exe).Source)
    foreach ($a in @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $agentScript, '-Root', $c.root, '-PollSeconds', '1',
            '-MutexName', "Local\MlvHeavySlotFifoTest-$PID-$($c.name)-$($agents.Count)", '-HeavySlotPython', $Python, '-HeavySlotScript', $hs,
            '-HeavySlotDir', $c.slots, '-HeavySlotFeed', $c.feed, '-GateProbeSec', '2', '-GateIdleSampleSec', '0',
            # AGENT-HEAVY-SLOT-2.3-ADAPT-1: an unset timeout is the 1800 s default, which labels a job "mlv-long <id>".
            # These test jobs are short, so they keep the "mlv <id>" label the cases below match on.
            '-JobTimeoutSec', '600',
            # BACHELOR-AGENT-QUIET-LEASE-1: these cases prove the r1c holder path, which is now the -HeavySlotMode status rollback.
            '-HeavySlotMode', 'status')) { [void]$si.ArgumentList.Add($a) }
    $si.UseShellExecute = $false; $si.CreateNoWindow = $true
    $si.RedirectStandardOutput = $true; $si.RedirectStandardError = $true
    $p = [Diagnostics.Process]::Start($si)
    $agents.Add($p)
    $c.agent = $p
    $hb = Join-Path $c.root 'heartbeat.txt'
    $dl = (Get-Date).AddSeconds(20)
    while (-not (Test-Path -LiteralPath $hb) -and (Get-Date) -lt $dl) { Start-Sleep -Milliseconds 200 }
    Assert-True (Test-Path -LiteralPath $hb) "$($c.name): agent heartbeat appeared"
}
function Submit-Job($c, [string]$JobId, [string]$Body) {
    $p = Join-Path $c.root "inbox\$JobId.job.ps1"
    [IO.File]::WriteAllText("$p.tmp", $Body)
    Move-Item -LiteralPath "$p.tmp" -Destination $p
}
function Wait-Until([scriptblock]$Cond, [int]$TimeoutSec) {
    $dl = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $dl) { if (& $Cond) { return $true }; Start-Sleep -Milliseconds 250 }
    return [bool](& $Cond)
}
function Get-Result($c, [string]$JobId) {
    $f = Join-Path $c.root "outbox\$JobId.result.json"
    if (-not (Test-Path -LiteralPath $f)) { return $null }
    try { return Get-Content -LiteralPath $f -Raw | ConvertFrom-Json } catch { return $null }
}
function Wait-Result($c, [string]$JobId, [int]$TimeoutSec) {
    $null = Wait-Until { $null -ne (Get-Result $c $JobId) } $TimeoutSec
    return Get-Result $c $JobId
}
function Get-SlotDirs($c) { return @(Get-ChildItem -LiteralPath $c.slots -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^\d+$' } | ForEach-Object Name) }
function Get-Tickets($c) {
    return @(Get-ChildItem -LiteralPath $c.queue -Filter '*.json' -File -ErrorAction SilentlyContinue | ForEach-Object {
            $o = $null; try { $o = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json } catch { }
            [pscustomobject]@{ name = $_.Name; label = $o.label; pid = $o.pid }
        })
}
function Get-Heartbeat($c) { try { return (Get-Content -LiteralPath (Join-Path $c.root 'heartbeat.txt') -Raw).Trim() } catch { return '' } }
function Test-PidAlive([int]$ProcessId) { return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) }
function Hold-FakeSlot($c, [int]$N, [string]$Label, [int]$OwnerPid) {
    New-Item -ItemType Directory -Force -Path (Join-Path $c.slots "$N") | Out-Null
    [IO.File]::WriteAllText((Join-Path $c.slots "$N\owner.json"), (@{ label = $Label; pid = $OwnerPid; start = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') } | ConvertTo-Json -Compress))
}
function Stop-Agent($c) {
    if ($null -ne $c.agent -and -not $c.agent.HasExited) { try { $c.agent.Kill($true) } catch { }; [void]$c.agent.WaitForExit(10000) }
}
function Get-DeadPid {
    $p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'exit 0' -WindowStyle Hidden -PassThru
    $p.WaitForExit()
    return $p.Id
}

# The job body: proves where it ran. It prints HEAVY_SLOT_HELD and heavy-slot.py's own status from INSIDE the job.
$probeBody = @"
`$st = & '$Python' '$hs' status 2>&1 | Out-String
"HELD=`$env:HEAVY_SLOT_HELD"
"ADMISSION=`$env:MLV_AGENT_ADMISSION"
"STATUS<<`$st>>"
"@

try {
    # A. A waiting job holds NO slot; heavy/CUDA runs only when busy == 0, and only through heavy-slot.py run --exclusive.
    if (Want 'waiting') {
        $c = New-Case 'waiting'
        Hold-FakeSlot $c 1 'fake suite' $PID
        Start-Agent $c
        Submit-Job $c 'jA' $probeBody
        $queued = Wait-Until { @(Get-Tickets $c | Where-Object { $_.label -eq 'mlv jA' }).Count -eq 1 -and (Get-Heartbeat $c) -match 'gate-wait job=jA state=queued ' } 30
        Assert-True $queued "A: the job filed exactly one heavy-slot.py ticket (label 'mlv jA') and heartbeats state=queued"
        Start-Sleep -Seconds 6
        $slotsWhileWaiting = Get-SlotDirs $c
        $ticket = @(Get-Tickets $c | Where-Object { $_.label -eq 'mlv jA' })[0]
        $marker = Get-Content -LiteralPath (Join-Path $c.root 'running\jA.started.json') -Raw | ConvertFrom-Json
        Assert-True ($null -eq (Get-Result $c 'jA')) "A: no result while another run holds a slot (busy == 0 rule)"
        Assert-True (($slotsWhileWaiting -join ',') -eq '1') "A: while waiting, the ONLY slot held is the other run's (agent holds none): [$($slotsWhileWaiting -join ',')]"
        Assert-True ($marker.phase -eq 'admission-wait' -and $null -eq $marker.childPid) "A: the job is claimed (admission-wait, no child)"
        $holderCmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($ticket.pid)").CommandLine
        Assert-True ($holderCmd -match 'heavy-slot\.py"?\s+run\s+--exclusive\s+--label\s+"mlv jA"') "A: the ticket's owner is 'heavy-slot.py run --exclusive --label `"mlv jA`"': $holderCmd"
        Remove-Item -LiteralPath (Join-Path $c.slots '1') -Recurse -Force
        $r = Wait-Result $c 'jA' 60
        Assert-True ($null -ne $r) "A: the job ran once the machine emptied"
        $held = if ($r.stdout -match 'HELD=(\d+):(\d+)') { @([int]$Matches[1], [int]$Matches[2]) } else { $null }
        Assert-True ($null -ne $held) "A: the job saw HEAVY_SLOT_HELD=<slot>:<pid>: $($r.stdout)"
        $statusLine = [regex]::Match($r.stdout, "slot $($held[0]): mlv jA \(pid $($held[1]), since [^)]+\) \[exclusive\]").Value
        Assert-True ([bool]$statusLine) "A: from inside the job, heavy-slot.py status shows slot $($held[0]) held EXCLUSIVE by 'mlv jA' (pid $($held[1])): $($r.stdout)"
        Assert-True ($r.exitCode -eq 0 -and $r.admission.label -eq 'clear' -and [bool]$r.admission.exclusive -and [int]$r.admission.busy -eq 0) "A: admission clear, exclusive, busy 0: $($r.admission | ConvertTo-Json -Compress)"
        Assert-True ($r.stdout -match 'ADMISSION=clear') "A: MLV_AGENT_ADMISSION=clear in the job"
        $released = Wait-Until { (Get-SlotDirs $c).Count -eq 0 -and (Get-Tickets $c).Count -eq 0 -and -not (Test-PidAlive $held[1]) } 20
        Assert-True $released "A: after the job, heavy-slot.py released the slot, the ticket is gone and the holder exited"
        $results.waiting = [ordered]@{ pass = $true; slotsWhileWaiting = $slotsWhileWaiting; holderCommandLine = $holderCmd; heldInJob = "$($held[0]):$($held[1])"; statusInJob = $statusLine; admission = $r.admission }
        Stop-Agent $c
    }

    # B. FIFO: a LIVE ticket ahead is served first; a DEAD ticket ahead is reclaimed by heavy-slot.py (not by the agent).
    if (Want 'fifo') {
        $c = New-Case 'fifo'
        New-Item -ItemType Directory -Force -Path $c.queue | Out-Null
        $deadPid = Get-DeadPid
        $ns = [long](([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - 60000) * 1000000)
        $deadTicket = Join-Path $c.queue ("{0}-{1}.json" -f $ns, $deadPid)
        $liveTicket = Join-Path $c.queue ("{0}-{1}.json" -f ($ns + 1000), $PID)
        [IO.File]::WriteAllText($deadTicket, (@{ label = 'dead waiter'; pid = $deadPid; since = '2026-10-11 18:00:00' } | ConvertTo-Json -Compress))
        [IO.File]::WriteAllText($liveTicket, (@{ label = 'live waiter'; pid = $PID; since = '2026-10-11 18:00:01' } | ConvertTo-Json -Compress))
        Start-Agent $c
        Submit-Job $c 'jB' $probeBody
        $deadGone = Wait-Until { -not (Test-Path -LiteralPath $deadTicket) } 30
        Assert-True $deadGone "B: heavy-slot.py reclaimed the dead waiter's ticket"
        Start-Sleep -Seconds 6
        Assert-True ($null -eq (Get-Result $c 'jB')) "B: the job waits behind the live ticket ahead of it (FIFO), on an empty, cool machine"
        Assert-True ((Test-Path -LiteralPath $liveTicket) -and (Get-SlotDirs $c).Count -eq 0) "B: the live ticket is untouched and no slot is held while waiting"
        $agentLog = Get-Content -LiteralPath (Join-Path $c.root 'logs\agent.log') -Raw
        Assert-True ($agentLog -notmatch [regex]::Escape($deadTicket) -and $agentLog -notmatch 'queue\\') "B: the agent never named or touched a queue file"
        Remove-Item -LiteralPath $liveTicket -Force
        $r = Wait-Result $c 'jB' 60
        Assert-True ($null -ne $r -and $r.admission.label -eq 'clear') "B: served once the live ticket ahead left"
        $results.fifo = [ordered]@{ pass = $true; deadTicketReclaimedBy = 'heavy-slot.py queue_ahead'; admission = $r.admission }
        Stop-Agent $c
    }

    # C. Sustained heat: spikes do not block; > 6/60 throttled does (heavy-slot.py and the agent read the same rule).
    if (Want 'heat') {
        $c = New-Case 'heat'
        Write-Feed $c.feed (1..60 | % { if ($_ % 2) { 100 } else { 70 } }) (1..60 | % { $false })
        Start-Agent $c
        Submit-Job $c 'jC1' $probeBody
        $r1 = Wait-Result $c 'jC1' 45
        Assert-True ($null -ne $r1 -and $r1.admission.label -eq 'clear' -and [double]$r1.admission.max15C -ge 100) "C: turbo spikes (max 100 C, 0 throttled, mean 85) admit: $($r1.admission | ConvertTo-Json -Compress)"
        Write-Feed $c.feed (1..60 | % { 70 }) (1..60 | % { $_ -le 8 })
        Submit-Job $c 'jC2' $probeBody
        Start-Sleep -Seconds 10
        Assert-True ($null -eq (Get-Result $c 'jC2')) "C: 8/60 throttled holds the job"
        Assert-True ((Get-SlotDirs $c).Count -eq 0) "C: the held job holds no slot"
        Write-CleanFeed $c.feed
        $r2 = Wait-Result $c 'jC2' 60
        Assert-True ($null -ne $r2 -and $r2.admission.label -eq 'clear') "C: runs once heat is no longer sustained"
        $results.heat = [ordered]@{ pass = $true; spikes = $r1.admission.thermal; afterThrottle = $r2.admission.thermal }
        Stop-Agent $c
    }

    # D. heavy-slot.py admits on an unknown feed ("not gating on it"); the agent does not: it RELEASES the acquired
    #    slot and re-joins the FIFO, so a job that is not running never keeps a slot.
    if (Want 'requeue') {
        $c = New-Case 'requeue'
        Write-Feed $c.feed (1..10 | % { 60 }) (1..10 | % { $false })
        Start-Agent $c
        Submit-Job $c 'jD' $probeBody
        $requeued = Wait-Until { (Get-Content -LiteralPath (Join-Path $c.root 'logs\agent.log') -Raw -ErrorAction SilentlyContinue) -match 'holder ended job=jD .*acquired=True .*but feed-unknown' } 45
        Assert-True $requeued "D: an acquired slot under an unknown feed is released (agent.log)"
        $samples = @(); for ($i = 0; $i -lt 8; $i++) { $samples += (Get-SlotDirs $c).Count; Start-Sleep -Milliseconds 500 }
        Assert-True ($null -eq (Get-Result $c 'jD')) "D: the job did not run on an unknown feed"
        Write-CleanFeed $c.feed
        $r = Wait-Result $c 'jD' 60
        Assert-True ($null -ne $r -and $r.admission.label -eq 'clear' -and [int]$r.admission.requeues -ge 1) "D: runs once the feed reads, after >= 1 requeue: $($r.admission | ConvertTo-Json -Compress)"
        $results.requeue = [ordered]@{ pass = $true; slotCountSamplesWhileRequeueing = $samples; requeues = $r.admission.requeues }
        Stop-Agent $c
    }

    # E. Cancel while queued: an unlaunched result, no holder left; its dead ticket is reclaimed by heavy-slot.py.
    if (Want 'cancel') {
        $c = New-Case 'cancel'
        Hold-FakeSlot $c 1 'fake suite' $PID
        Start-Agent $c
        Submit-Job $c 'jE' $probeBody
        $null = Wait-Until { @(Get-Tickets $c | Where-Object { $_.label -eq 'mlv jE' }).Count -eq 1 } 30
        $holderPid = [int]@(Get-Tickets $c | Where-Object { $_.label -eq 'mlv jE' })[0].pid
        New-Item -ItemType Directory -Force -Path (Join-Path $c.root 'cancel') | Out-Null
        [IO.File]::WriteAllText((Join-Path $c.root 'cancel\jE.cancel'), 'cancel')
        $r = Wait-Result $c 'jE' 30
        Assert-True ($null -ne $r -and $r.exitCode -eq 130 -and $r.admission.label -eq 'cancelled-in-gate') "E: cancelled in the gate, never launched"
        Assert-True (Wait-Until { -not (Test-PidAlive $holderPid) } 10) "E: the queued holder was stopped"
        Remove-Item -LiteralPath (Join-Path $c.slots '1') -Recurse -Force
        $probe = & $Python $hs run --label 'reclaim probe' -- cmd.exe /c exit 0 2>&1
        Assert-True ($LASTEXITCODE -eq 0 -and @(Get-Tickets $c).Count -eq 0) "E: the next heavy-slot.py waiter reclaimed the stopped holder's ticket: $($probe -join ' | ')"
        $results.cancel = [ordered]@{ pass = $true; holderPid = $holderPid; ticketReclaimedBy = 'heavy-slot.py queue_ahead (next waiter)' }
        Stop-Agent $c
    }

    # F. Agent restart while a job is queued: the new agent stops the old queued holder, re-queues the claim, and runs it.
    if (Want 'restart') {
        $c = New-Case 'restart'
        Hold-FakeSlot $c 1 'fake suite' $PID
        Start-Agent $c
        Submit-Job $c 'jF' $probeBody
        $null = Wait-Until { @(Get-Tickets $c | Where-Object { $_.label -eq 'mlv jF' }).Count -eq 1 -and @(Get-ChildItem -LiteralPath (Join-Path $c.root 'slot-holders') -Filter 'jF.*.holder.json' -ErrorAction SilentlyContinue).Count -eq 1 } 30
        $oldHolder = [int]@(Get-Tickets $c | Where-Object { $_.label -eq 'mlv jF' })[0].pid
        $c.agent.Kill($false); [void]$c.agent.WaitForExit(10000)
        Assert-True (Test-PidAlive $oldHolder) "F: the queued holder outlives a killed agent (precondition)"
        Start-Agent $c
        Assert-True (Wait-Until { -not (Test-PidAlive $oldHolder) } 20) "F: the restarted agent stopped the previous agent's queued holder"
        Remove-Item -LiteralPath (Join-Path $c.slots '1') -Recurse -Force
        $r = Wait-Result $c 'jF' 60
        Assert-True ($null -ne $r -and $r.admission.label -eq 'clear' -and $r.stdout -match 'HELD=\d+:\d+') "F: the re-queued job ran under a new holder"
        $results.restart = [ordered]@{ pass = $true; oldHolderPid = $oldHolder; admission = $r.admission }
        Stop-Agent $c
    }

    # G. Light probes stay exempt: they run beside a held slot, take no slot and file no ticket.
    if (Want 'exempt') {
        $c = New-Case 'exempt'
        Hold-FakeSlot $c 1 'fake suite' $PID
        Hold-FakeSlot $c 2 'fake suite 2' $PID
        Start-Agent $c
        Submit-Job $c 'venue-quiet-probe-1' $probeBody
        $r = Wait-Result $c 'venue-quiet-probe-1' 30
        Assert-True ($null -ne $r -and $r.admission.label -eq 'exempt-light' -and $r.stdout -match 'HELD=\r?\n' -and (Get-Tickets $c).Count -eq 0) "G: a light probe runs unheld, with no ticket"
        $results.exempt = [ordered]@{ pass = $true; admission = $r.admission.label }
        Stop-Agent $c
    }

    # H. maint-dispatch: while an ordinary job waits (unknown feed: it re-queues forever), the light jobs behind it are
    #    run from inside its gate-wait; the ordinary jobs keep their order; a stale inbox listing never re-runs a job.
    if (Want 'maint') {
        $c = New-Case 'maint'
        Write-Feed $c.feed (1..10 | % { 60 }) (1..10 | % { $false })
        Start-Agent $c
        Submit-Job $c 'jW1' $probeBody
        $waiting = Wait-Until { (Get-Heartbeat $c) -match 'gate-wait job=jW1 ' } 30
        Assert-True $waiting "H: jW1 is in its gate-wait (heartbeat)"
        Submit-Job $c 'jW2' $probeBody
        Submit-Job $c 'agent-swap-M1' $probeBody
        Submit-Job $c 'venue-quiet-probe-M2' $probeBody
        $m1 = Wait-Result $c 'agent-swap-M1' 20
        Assert-True ($null -ne $m1 -and $m1.admission.label -eq 'exempt-light' -and $m1.admission.waitingFor -eq 'jW1' -and $m1.stdout -match 'HELD=\r?\n') "H1: agent-swap-M1 ran during jW1's gate-wait: $($m1.admission | ConvertTo-Json -Compress)"
        $m2 = Wait-Result $c 'venue-quiet-probe-M2' 20
        Assert-True ($null -ne $m2 -and $m2.admission.label -eq 'exempt-light' -and $m2.admission.waitingFor -eq 'jW1' -and $m2.stdout -match 'HELD=\r?\n') "H2: venue-quiet-probe-M2 ran during jW1's gate-wait: $($m2.admission | ConvertTo-Json -Compress)"
        $m1Sha = (Get-FileHash -LiteralPath (Join-Path $c.root 'outbox\agent-swap-M1.result.json') -Algorithm SHA256).Hash
        $w1Marker = Get-Content -LiteralPath (Join-Path $c.root 'running\jW1.started.json') -Raw | ConvertFrom-Json
        Assert-True ($null -eq (Get-Result $c 'jW1') -and $null -eq (Get-Result $c 'jW2')) "H3: neither ordinary job has a result"
        Assert-True ($w1Marker.phase -eq 'admission-wait' -and $null -eq $w1Marker.childPid) "H3: jW1 is still claimed (admission-wait, no child)"
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $c.root 'running\jW2.started.json'))) "H3: jW2 was not claimed (ordinary order kept)"
        Assert-True (@(Get-Tickets $c | Where-Object { $_.label -eq 'mlv jW2' }).Count -eq 0) "H3: jW2 holds no heavy-slot.py ticket"
        Write-CleanFeed $c.feed
        $w1 = Wait-Result $c 'jW1' 60
        $w2 = Wait-Result $c 'jW2' 60
        Assert-True ($null -ne $w1 -and $null -ne $w2 -and $w1.admission.label -eq 'clear' -and $w2.admission.label -eq 'clear') "H4: jW1 and jW2 both ran, admission clear"
        Assert-True ([DateTimeOffset]$w1.endedUtc -le [DateTimeOffset]$w2.startedUtc) "H4: jW1 finished before jW2 started (ordinary FIFO order kept): $($w1.endedUtc) vs $($w2.startedUtc)"
        $m1ShaAfter = (Get-FileHash -LiteralPath (Join-Path $c.root 'outbox\agent-swap-M1.result.json') -Algorithm SHA256).Hash
        $exemptLines = @(Get-Content -LiteralPath (Join-Path $c.root 'logs\agent.log') | Where-Object { $_ -match 'heavy-slot-gate: exempt job=agent-swap-M1' })
        Assert-True ($m1Sha -eq $m1ShaAfter -and $exemptLines.Count -eq 1) "H5: agent-swap-M1 ran once and its result was not republished (stale listing guard): sha same=$($m1Sha -eq $m1ShaAfter), exempt lines=$($exemptLines.Count)"
        $results.maint = [ordered]@{ pass = $true; swapAdmission = $m1.admission; probeAdmission = $m2.admission; jW1Ended = $w1.endedUtc; jW2Started = $w2.startedUtc; swapResultSha256 = $m1Sha; exemptLogLines = $exemptLines.Count }
        Stop-Agent $c
    }

    [pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
}
finally {
    foreach ($p in $agents) { try { if (-not $p.HasExited) { $p.Kill($true); [void]$p.WaitForExit(5000) } } catch { } }
    # Holders are children of the agents but survive Kill($false); stop any python still running from this run.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($base) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach ($k in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($k, $savedEnv[$k]) }
    if ($env:MLV_KEEP_TEST_ROOT -ne '1') { Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue }
}
