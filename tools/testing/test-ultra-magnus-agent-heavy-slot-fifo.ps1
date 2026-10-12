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
    [string[]]$Only = @()
)

# AGENT-HEAVY-SLOT-WAIT-RELEASE-1 r1c: the Bachelor agent's heavy-slot gate
# (tools/profiling/bachelor/ultra-magnus-agent.ps1).
#   unit  - the SUSTAINED heat rule (spikes with 0 throttled rows do not block; > 6/60 throttled or a 60-row mean
#           >= 88 C does), occupancy with the job's own slot excluded, and the verdict precedence.
#   e2e   - the real agent and the real heavy-slot.py: a WAITING job holds no slot (only heavy-slot.py's FIFO
#           ticket); a heavy job runs only through `heavy-slot.py run --exclusive --label "mlv <jobId>"` and only when
#           no other slot is held (busy == 0); a live ticket ahead is served first; a dead one is reclaimed by
#           heavy-slot.py; an acquired slot the agent cannot use is released before the job re-queues; cancel and
#           agent restart leave no holder behind; light probes stay exempt.
# Prints one JSON object with status PASS, or throws. No playback, no venue: everything is local and private.

$ErrorActionPreference = 'Stop'
$agentScript = Join-Path $RepoRoot 'tools\profiling\bachelor\ultra-magnus-agent.ps1'
$results = [ordered]@{}

function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "FAIL: $Message" } }
# Under `pwsh -File`, -Only a,b arrives as ONE string "a,b": split it, and refuse a name that selects nothing, so a
# typo can never turn into a PASS with no case run.
$caseNames = @('waiting', 'fifo', 'heat', 'requeue', 'cancel', 'restart', 'exempt')
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($n in $Only) { if ($caseNames -notcontains $n) { throw "unknown case '$n' (known: $($caseNames -join ', '))" } }
function Want([string]$Name) { return ($Only.Count -eq 0 -or $Only -contains $Name) }

# ---------------------------------------------------------------- unit
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($agentScript, [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) "agent parses ($($parseErrors.Count) errors)"
foreach ($name in @('ConvertFrom-HeavySlotFeedRows', 'Get-SustainedHeatVerdict', 'Get-HeavySlotOccupancy', 'Get-HeavySlotVerdict')) {
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
$vOwn = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 1 -Label 'mlv j1'
Assert-True ($vOwn.state -eq 'clear' -and $vOwn.busy -eq 0) "the job's OWN slot is not counted as busy (r1k counted its own reservation): $($vOwn.state) busy=$($vOwn.busy)"
$vOther = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 0
Assert-True ($vOther.state -eq 'busy' -and $vOther.busy -eq 1) "another holder is busy: $($vOther.state)"
$vGone = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -OwnSlot 2
Assert-True ($vGone.state -eq 'status-unknown') "an own slot shown free is unknown, never clear: $($vGone.why)"
$vHot = Get-HeavySlotVerdict -Status $statusOwn -Feed $hotFeed -OwnSlot 1
Assert-True ($vHot.state -eq 'hot') "sustained heat blocks a job alone on the machine"
$vQ = Get-HeavySlotVerdict -Status $statusOwn -Feed $clearFeed -Label 'mlv j2'
Assert-True ($vQ.queuePos -eq 2 -and $vQ.queueLen -eq 2) "FIFO position read from status queue lines: $($vQ.queuePos)/$($vQ.queueLen)"
$vUnk = Get-HeavySlotVerdict -Status @{ ok = $false; lines = @(); error = 'status timed out after 30 s' } -Feed $clearFeed
Assert-True ($vUnk.state -eq 'status-unknown') "a failed status probe is unknown, never empty"
$vFeed = Get-HeavySlotVerdict -Status $statusOwn -Feed @{ metrics = $null; why = 'feed stale (300 s > 120 s)' } -OwnSlot 1
Assert-True ($vFeed.state -eq 'feed-unknown') "an unknown feed is never clear"
$unit.verdicts = [ordered]@{ ownSlotExcluded = $vOwn.state; otherHolder = $vOther.state; ownSlotFree = $vGone.state; hotAlone = $vHot.state; queuePos = "$($vQ.queuePos)/$($vQ.queueLen)"; statusFailed = $vUnk.state; feedUnknown = $vFeed.state }
$results.unit = $unit

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
            '-HeavySlotDir', $c.slots, '-HeavySlotFeed', $c.feed, '-GateProbeSec', '2', '-GateIdleSampleSec', '0')) { [void]$si.ArgumentList.Add($a) }
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

    [pscustomobject]@{ status = 'PASS'; agent = $agentScript; agentSha256 = (Get-FileHash -LiteralPath $agentScript -Algorithm SHA256).Hash; results = $results } | ConvertTo-Json -Depth 8
}
finally {
    foreach ($p in $agents) { try { if (-not $p.HasExited) { $p.Kill($true); [void]$p.WaitForExit(5000) } } catch { } }
    # Holders are children of the agents but survive Kill($false); stop any python still running from this run.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($base) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach ($k in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($k, $savedEnv[$k]) }
    if ($env:MLV_KEEP_TEST_ROOT -ne '1') { Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue }
}
