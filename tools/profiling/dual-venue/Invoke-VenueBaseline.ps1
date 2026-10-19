# Invoke-VenueBaseline.ps1 -- the TRACKED regular playback baseline runner (UM-REGULAR-BASELINE-1). RUN THIS ON THE HUB VM (VIRTUAL-TEN).
#
# Owner directive 2026-10-18 (Layi, hub chat): "I'm curious how playback performs on ultramagnus so id like regular benchmarking there as well when possible".
# Ultra-Magnus is a REGULAR benchmark venue, not only a failover. Bachelor stays the ACCEPTANCE venue: the roles come from venues.json and are never set here, so
# every Ultra-Magnus row says "supplementary". The hub VM (VIRTUAL-TEN) is a guest on Ultra-Magnus, so each run records the hub-VM load at its start and its end.
#
# What it does, in order (a refusal at any step is a typed non-zero exit; see the table below):
#   1. reads venues.json and the two standard leg specs (legs\m16-1243-look.json, legs\m16-1243-display-fullscreen-s1.json), and takes each leg's ROLE from the
#      table (Get-DvVenueRole, the card named in the spec). -SourceCommit defaults to the local fork/master ref (-Fetch runs `git fetch fork master` first, as a child bounded
#      by -MaxTotalSec and killed past it: FETCH_TIMEOUT; -WhatIf never fetches);
#   2. -WhatIf stops here: it prints the plan (BASELINE_PLAN / BASELINE_LEG / BASELINE_CMD lines) and runs nothing, fetches nothing, reads no share and samples nothing;
#   3. samples the hub-VM load (commit GiB, CPU %), refuses when the venue claim is live or a ticket is queued (AGENT_BUSY), refuses when the agent share does not
#      answer inside -ProbeTimeoutSec (SHARE_UNREACHABLE), and finds the staged build manifest (BUILD_NOT_STAGED when it is not there);
#   4. runs the standard legs through the tracked Invoke-VenueChain.ps1 -PreferQuietWindows, once per backend (cuda, then cpu), as a CHILD process bounded by
#      -MaxTotalSec (+ -KillGraceSec): the chain's own -DeadlineUtc stops it gracefully, the kill is the backstop, so nothing here can wait unbounded;
#      The chain's exit code is read before any row is built (a pinned consumer of the evidence launchers: tools/repo_hygiene/test_playback_evidence_completeness.py).
#      A leg is recorded RAN, with figures, only when the chain ended on its own with exit 0 that agrees with its CHAIN_RESULT, and the leg's receipt is readable and
#      its validator verdict is ADVISORY / NO_SIGNAL / VERIFIED* (INVALID, INCOMPLETE and a validator error are refused); otherwise the row is status EVIDENCE_INCOMPLETE
#      (EVIDENCE_INVALID for an INVALID verdict) / outcome INVALID with a `failure` text and NO figures. An undocumented or disagreeing exit exits CHAIN_FAILED (27); a documented
#      non-zero exit (7, 3, gate) keeps its own typed exit but its legs carry no figures either.
#   5. appends ONE JSON line per leg that touched the venue to the trend file (default <main checkout>\.claude-state\dual-venue\baseline-trend.jsonl), then
#      samples the hub-VM load again; the same two samples sit in every row of the run.
#
# STAGING. The chain never builds or stages: Invoke-VenueLeg refuses (DEVICE_UNAVAILABLE BUILD_NOT_STAGED) a build that is not in the venue's cache. This script adds
# no staging logic. It finds the manifest the tracked staging path already published (<agentShare>\cache\playback-attr-3-cuda-<sha12>-build.json, the file
# Invoke-VenueLeg hashes) and passes its sha256 to the chain, or takes -BuildManifestSha256 from the caller. A commit that is not staged is exit 22; the hub stages it
# first (docs/dual-venue-evidence.md, "How to run a leg on a venue": playback-attr-3-cuda-assemble.ps1 on the board host, then playback-attr-3-cuda-stage-job.ps1).
#
#   pwsh -NoProfile -File "<repo>\tools\profiling\dual-venue\Invoke-VenueBaseline.ps1" [-Venue ultra-magnus] [-SourceCommit <40-hex>] [-TrendFile <path>] [-WhatIf]
#
# Exit codes (typed; the BASELINE_EXIT line and BASELINE_RESULT json carry the token): 0 every planned leg ran (the chain exited 0 for both backends); 2 usage;
# 20 AGENT_BUSY (a live claim or queued ticket, or the venue never went quiet before the deadline with no leg run); 21 SHARE_UNREACHABLE; 22 BUILD_NOT_STAGED;
# 23 GATE_REFUSED (the chain stopped on GATE_UNREADABLE / HOST_MISMATCH / VENUE_QUIET_UNKNOWN / an unhealthy streak: chain exits 4, 5, 6, 8);
# 24 BASELINE_CAP_EXCEEDED (-MaxTotalSec ran out; rows for the legs that did run are still appended); 25 LEG_FAILED (a leg process died: chain exit 7);
# 26 SOURCE_COMMIT_UNRESOLVED; 27 CHAIN_FAILED (the chain ended with no CHAIN_RESULT or an undocumented exit);
# 28 FETCH_TIMEOUT (-Fetch: git fetch outlived -MaxTotalSec and was killed). 17 is NOT used: it is the hub-VM playback refusal.
#
# NO LOCAL PLAYBACK: this script never starts MLVApp. Playback happens only on the venue, through the chain (hub-hosts.json, HUB-VM-NO-LOCAL-PLAYBACK-1).
# -ChainScript / -ValidatorScript / -GitExe / -HubLoadScript / -AgentShare / -ClaimDir / -VenueTablePath / -RepoRoot are seams for the offline tests (tools/repo_hygiene/test_venue_baseline.py);
# production passes none. Scheduling is NOT here: the hub wires one call into MLVHubAutopilot (see the PR body).
[CmdletBinding()]
param(
    [ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'ultra-magnus',
    [ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit = '',
    [ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256 = '',
    [string]$TrendFile = '',
    [string]$RunDir = '',
    # Total wall-clock cap for everything after the plan (probes, quiet waits, claim waits, legs, cooldowns). The default fits two 4-leg-minutes chains with their waits.
    [ValidateRange(10, 28800)][int]$MaxTotalSec = 7200,
    [ValidateRange(0, 600)][int]$KillGraceSec = 120,
    [ValidateRange(1, 300)][int]$ProbeTimeoutSec = 30,
    [ValidateRange(1, 3600)][int]$QuietMaxWaitSec = 600,
    [switch]$WhatIf,
    [switch]$Fetch,
    [string]$ChainScript = '',
    [string]$ValidatorScript = '',
    [string]$GitExe = 'git',
    [string]$HubLoadScript = '',
    [string]$AgentShare = '',
    [string]$ClaimDir = '',
    [string]$VenueTablePath = '',
    [string]$RepoRoot = ''
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force -DisableNameChecking

$StandardLegs = @('legs\m16-1243-look.json', 'legs\m16-1243-display-fullscreen-s1.json')
$StandardBackends = @('cuda', 'cpu')
$ExitCodes = [ordered]@{
    OK = 0; USAGE = 2; AGENT_BUSY = 20; SHARE_UNREACHABLE = 21; BUILD_NOT_STAGED = 22; GATE_REFUSED = 23
    BASELINE_CAP_EXCEEDED = 24; LEG_FAILED = 25; SOURCE_COMMIT_UNRESOLVED = 26; CHAIN_FAILED = 27; FETCH_TIMEOUT = 28
}
$startedUtc = [DateTime]::UtcNow
$pwshExe = [Environment]::ProcessPath

function Out-Line([string]$Text) { Write-Output $Text }

function Stop-Baseline([string]$Reason, [string]$Detail) {
    # A refusal before any leg ran: one typed line, one result line, the exit code of the table above.
    $code = [int]$ExitCodes[$Reason]
    Out-Line "BASELINE_EXIT code=$code reason=$Reason detail=$Detail"
    if ($script:RunDirResolved -and (Test-Path -LiteralPath $script:RunDirResolved -PathType Container)) {
        $r = [ordered]@{ schema = 'mlv-app/venue-baseline-result/v1'; venue = $Venue; exitCode = $code; reason = $Reason; detail = $Detail; startedUtc = $startedUtc.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o') }
        try { [IO.File]::WriteAllText((Join-Path $script:RunDirResolved 'baseline-result.json'), ($r | ConvertTo-Json -Compress -Depth 6)) } catch { }
    }
    exit $code
}

function Invoke-Bounded([scriptblock]$Script, [object[]]$ArgList, [int]$Sec) {
    # Run $Script on a worker thread and give up after $Sec seconds: a UNC path of a host that is down can block a Test-Path for minutes. A worker still stuck when we give
    # up is abandoned (BeginStop, no wait), never joined: the process exits soon after.
    $ps = [powershell]::Create()
    [void]$ps.AddScript($Script.ToString())
    foreach ($a in $ArgList) { [void]$ps.AddArgument($a) }
    $async = $ps.BeginInvoke()
    if (-not $async.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($Sec))) {
        try { [void]$ps.BeginStop($null, $null) } catch { }
        return [pscustomobject]@{ ok = $false; timedOut = $true; value = $null; error = "no answer in $Sec s" }
    }
    try { $v = @($ps.EndInvoke($async)); [pscustomobject]@{ ok = $true; timedOut = $false; value = $(if ($v.Count -gt 0) { $v[0] } else { $null }); error = $null } }
    catch { [pscustomobject]@{ ok = $false; timedOut = $false; value = $null; error = $_.Exception.Message } }
    finally { $ps.Dispose() }
}

function Get-HubLoad {
    # {utc, host, commitGiB, cpuPercent} of THIS host (the hub VM). A reading that cannot be taken is $null, never zero. -HubLoadScript (tests) prints one JSON object.
    $s = [ordered]@{ utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'); host = $env:COMPUTERNAME; commitGiB = $null; cpuPercent = $null }
    try {
        if ($HubLoadScript) {
            $o = (& $pwshExe -NoProfile -NonInteractive -File $HubLoadScript) -join "`n" | ConvertFrom-Json
            $s.commitGiB = $o.commitGiB; $s.cpuPercent = $o.cpuPercent
            return [pscustomobject]$s
        }
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        $s.commitGiB = [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / 1MB, 1)   # the same formula the venue health probe uses (commitUsedGiB)
        $c = (Get-Counter -Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 3 -ErrorAction Stop).CounterSamples | ForEach-Object { $_.CookedValue }
        $s.cpuPercent = [math]::Round((($c | Measure-Object -Average).Average), 1)
    } catch { }
    [pscustomobject]$s
}

# ------------------------------------------------------------------ 1. the plan --------------------------------------------------------------------------------
$repo = $(if ($RepoRoot) { $RepoRoot } else { (Resolve-Path (Join-Path $here '..\..\..')).Path })
$mainRoot = Get-VenueMainRoot -From $repo
$tablePath = $(if ($VenueTablePath) { $VenueTablePath } else { Join-Path $here 'venues.json' })
$chainPath = $(if ($ChainScript) { $ChainScript } else { Join-Path $here 'Invoke-VenueChain.ps1' })
if (-not $TrendFile) { $TrendFile = Join-Path $mainRoot '.claude-state\dual-venue\baseline-trend.jsonl' }
if (-not $RunDir) { $RunDir = Join-Path $mainRoot ('.claude-state\dual-venue\baseline-runs\' + $startedUtc.ToString('yyyyMMddTHHmmssZ') + '-' + $Venue) }
$script:RunDirResolved = $null

$table = try { Read-DvVenueTable $tablePath } catch { Stop-Baseline 'USAGE' $_.Exception.Message }
$venueEntry = $table.venues.PSObject.Properties[$Venue]
if ($null -eq $venueEntry) { Stop-Baseline 'USAGE' "venues.json has no venue '$Venue'" }
# The trend rows name receipt paths of owner-clip runs: they stay under a .claude-state directory, like the receipts themselves.
foreach ($p in @(@('-TrendFile', $TrendFile), @('-RunDir', $RunDir))) {
    if (-not (Test-DvUnderClaudeState -Path $p[1])) { Stop-Baseline 'USAGE' "$($p[0]) must be under a .claude-state directory: a row names the receipt of an owner-clip run and stays local" }
}

$legs = [System.Collections.Generic.List[object]]::new()
foreach ($rel in $StandardLegs) {
    $path = Join-Path $here $rel
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Stop-Baseline 'USAGE' "no standard leg spec at '$path'" }
    $spec = try { [IO.File]::ReadAllText($path) | ConvertFrom-Json } catch { Stop-Baseline 'USAGE' "leg spec '$path' is not valid JSON" }
    $legs.Add([pscustomobject]@{ legId = [string]$spec.legId; card = [string]$spec.card; spec = (Resolve-Path -LiteralPath $path).Path; backends = @($spec.backends); role = (Get-DvVenueRole -Table $table -Card ([string]$spec.card) -Venue $Venue) })
}

$deadline = $startedUtc.AddSeconds($MaxTotalSec)

function Invoke-BoundedFetch {
    # `git fetch fork master` as a CHILD bounded by the same -MaxTotalSec budget as everything else: past the deadline the child and its tree are killed. Returns $true when it timed out.
    $psi = [Diagnostics.ProcessStartInfo]::new($GitExe)
    foreach ($a in @('-C', $repo, 'fetch', '--quiet', 'fork', 'master')) { [void]$psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    [void]$p.StandardOutput.ReadToEndAsync(); [void]$p.StandardError.ReadToEndAsync()
    $done = $p.WaitForExit([int][Math]::Max(1000, ($deadline - [DateTime]::UtcNow).TotalMilliseconds))
    if (-not $done) { try { $p.Kill($true) } catch { }; [void]$p.WaitForExit(15000) }
    -not $done
}

$commit = $SourceCommit
if (-not $commit) {
    # -WhatIf never fetches (it runs nothing); a failed fetch is not an error (the local fork/master ref is used), a stalled one is FETCH_TIMEOUT.
    if ($Fetch -and -not $WhatIf) {
        $fetchTimedOut = try { Invoke-BoundedFetch } catch { $false }
        if ($fetchTimedOut) { Stop-Baseline 'FETCH_TIMEOUT' "git fetch fork master was still running at the $MaxTotalSec s cap and was killed" }
    }
    $ref = (& git -C $repo rev-parse --verify --quiet 'refs/remotes/fork/master^{commit}' 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]$ref -cnotmatch '^[0-9a-f]{40}$') { Stop-Baseline 'SOURCE_COMMIT_UNRESOLVED' 'refs/remotes/fork/master does not resolve; pass -SourceCommit <40-hex> or fetch fork' }
    $commit = [string]$ref
}

function Get-ChainArgs([string]$Backend, [string]$ChainRunDir, [string]$Manifest) {
    @('-NoProfile', '-NonInteractive', '-File', $chainPath, '-Venue', $Venue, '-LegSpec', (($legs | ForEach-Object { $_.spec }) -join ','), '-Backend', $Backend,
        '-RunDir', $ChainRunDir, '-SourceCommit', $commit, '-BuildManifestSha256', $Manifest, '-PreferQuietWindows', '-QuietMaxWaitSec', [string]$QuietMaxWaitSec,
        '-DeadlineUtc', $deadline.ToString('o'), '-Purpose', "UM-REGULAR-BASELINE-1 $Venue baseline $($commit.Substring(0, 12)) backend $Backend")
}

if ($WhatIf) {
    $total = $legs.Count * $StandardBackends.Count
    Out-Line "BASELINE_PLAN venue=$Venue sourceCommit=$commit legs=$total maxTotalSec=$MaxTotalSec trendFile=$TrendFile runDir=$RunDir (plan only: nothing is run, no share is read, nothing is sampled)"
    $n = 0
    foreach ($b in $StandardBackends) {
        foreach ($l in $legs) {
            $n++
            Out-Line ('BASELINE_LEG {0}/{1} leg={2} card={3} backend={4} role={5}' -f $n, $total, $l.legId, $l.card, $b, $l.role)
        }
    }
    foreach ($b in $StandardBackends) {
        $manifest = $(if ($BuildManifestSha256) { $BuildManifestSha256 } else { '<sha256 of the staged build.json, read from the venue cache at run time>' })
        $argv = Get-ChainArgs $b (Join-Path $RunDir "chain-$b") $manifest
        Out-Line ("BASELINE_CMD backend=$b pwsh " + (($argv | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '))
    }
    exit 0
}

# ------------------------------------------------------------------ 2. preflight --------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$RunDir = (Resolve-Path -LiteralPath $RunDir).Path
$script:RunDirResolved = $RunDir
$hubStart = Get-HubLoad
Out-Line "BASELINE_START venue=$Venue sourceCommit=$commit runDir=$RunDir deadlineUtc=$($deadline.ToString('o')) hubLoadStart=$($hubStart | ConvertTo-Json -Compress)"

$status = Get-VenueClaimStatus -Venue $Venue -ClaimDir $ClaimDir
$liveTickets = @($status.tickets | Where-Object { $_.state -cne 'STALE' })
if ($status.claim.state -cin @('LIVE', 'BUSY') -or $liveTickets.Count -gt 0) {
    $holder = $(if ($status.claim.claim) { "pid=$($status.claim.claim.ownerPid) runDir=$($status.claim.claim.runDir)" } else { "claimState=$($status.claim.state)" })
    Stop-Baseline 'AGENT_BUSY' "the $Venue agent is in use ($holder liveTickets=$($liveTickets.Count)); a baseline never queues behind another chain"
}

$share = $(if ($AgentShare) { $AgentShare } else { [string]$venueEntry.Value.agentShare })
$cache = $share.TrimEnd('\') + '\cache'
$reach = Invoke-Bounded { param($p) Test-Path -LiteralPath $p -PathType Container } @($cache) $ProbeTimeoutSec
if (-not $reach.ok -or $reach.value -ne $true) { Stop-Baseline 'SHARE_UNREACHABLE' "the agent share cache '$cache' did not answer as a directory ($(if ($reach.timedOut) { $reach.error } elseif ($reach.error) { $reach.error } else { 'not found' }))" }

$manifest = $BuildManifestSha256
if (-not $manifest) {
    $buildJson = "$cache\playback-attr-3-cuda-$($commit.Substring(0, 12))-build.json"
    $m = Invoke-Bounded { param($p) if (Test-Path -LiteralPath $p -PathType Leaf) { (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() } else { '' } } @($buildJson) $ProbeTimeoutSec
    if ($m.timedOut) { Stop-Baseline 'SHARE_UNREACHABLE' "reading '$buildJson' $($m.error)" }
    if (-not $m.ok -or -not $m.value) { Stop-Baseline 'BUILD_NOT_STAGED' "no staged build manifest for $($commit.Substring(0, 12)) at '$buildJson'; the hub stages it first (assemble + stage job), this script stages nothing" }
    $manifest = [string]$m.value
}
Out-Line "BASELINE_BUILD sourceCommit=$commit buildManifestSha256=$manifest"

# ------------------------------------------------------------------ 3. the chains (bounded children) --------------------------------------------------------------
function Invoke-BoundedChain([string]$Backend) {
    $chainRun = Join-Path $RunDir "chain-$Backend"
    New-Item -ItemType Directory -Force -Path $chainRun | Out-Null
    $psi = [Diagnostics.ProcessStartInfo]::new($pwshExe)
    foreach ($a in (Get-ChainArgs $Backend $chainRun $manifest)) { [void]$psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $outTask = $p.StandardOutput.ReadToEndAsync(); $errTask = $p.StandardError.ReadToEndAsync()
    $waitMs = [int][Math]::Max(1000, ($deadline - [DateTime]::UtcNow).TotalMilliseconds + ($KillGraceSec * 1000))
    $timedOut = -not $p.WaitForExit($waitMs)
    if ($timedOut) { try { $p.Kill($true) } catch { }; [void]$p.WaitForExit(15000) }
    [void]$outTask.Wait(15000); [void]$errTask.Wait(15000)
    $out = $(if ($outTask.IsCompleted) { $outTask.Result } else { '' }); $err = $(if ($errTask.IsCompleted) { $errTask.Result } else { '' })
    [IO.File]::WriteAllText((Join-Path $RunDir "chain-$Backend.out.txt"), $out + $(if ($err) { "`n--- stderr ---`n$err" } else { '' }))
    $resultLine = @($out -split "`r?`n" | Where-Object { $_ -like 'CHAIN_RESULT=*' }) | Select-Object -Last 1
    $result = $(if ($resultLine) { try { $resultLine.Substring('CHAIN_RESULT='.Length) | ConvertFrom-Json } catch { $null } } else { $null })
    [pscustomobject]@{ backend = $Backend; exit = $(if ($timedOut) { $null } else { $p.ExitCode }); timedOut = $timedOut; result = $result; stderr = $err }
}

function Get-RowFigures([string]$ReceiptPath, $Leg, [string]$Backend) {
    # The figures exactly as the receipt reports them (null when the receipt does not carry one; never a default). presentedFps / timelineFpsAfterFirstPresent come from
    # the receipt's own hashed run log through Get-DvMatrixRow, which refuses a log that no longer hashes to the receipt.
    $f = [ordered]@{ receipt = $null; outcome = $null; fps = $null; drops = $null; stall = $null; verification = $null; rejected = $null; noFigure = $null }
    if (-not $ReceiptPath -or -not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) { return $f }
    try { $rc = [IO.File]::ReadAllText($ReceiptPath) | ConvertFrom-Json } catch { return $f }
    # The evidence-bearing validator (-RepoRoot re-derives the admission from the committed blobs and the run from the hashed evidence). Its verdict is recorded in the row AND gates
    # the figures: a production receipt is ADVISORY at best (valid=false by design, so `valid` is not the gate), and only ADVISORY / NO_SIGNAL / VERIFIED / VERIFIED_OFFLINE_TEST
    # receipts have their figures copied. INVALID, INCOMPLETE, a validator error or any status not named here is `rejected`: no figure, the caller types the row. A figure needs
    # MORE than that: the receipt's own outcome must be PASS or FAIL (a measurement) AND the status ADVISORY / VERIFIED*. NO_SIGNAL is the validator's answer for every other
    # outcome, including the INVALID the tracked writer rewrites a refused PASS/FAIL to while keeping its metrics, so it carries no figure: `noFigure` names the typed reason.
    $v = try {
        if ($ValidatorScript) { $o = (& $pwshExe -NoProfile -NonInteractive -File $ValidatorScript -ReceiptPath $ReceiptPath) -join "`n"; if ($LASTEXITCODE -ne 0) { throw "validator exit $LASTEXITCODE" }; $o | ConvertFrom-Json }
        else { Test-DvReceiptValid -Receipt $rc -RepoRoot $repo }
    } catch { $null }
    $f.verification = $(if ($v -and [string]$v.status) { [ordered]@{ status = [string]$v.status; valid = [bool]$v.valid; reasons = @($v.reasons | Select-Object -First 3) } } else { [ordered]@{ status = 'VALIDATOR_ERROR'; valid = $false; reasons = @() } })
    if ($f.verification.status -cnotin @('ADVISORY', 'NO_SIGNAL', 'VERIFIED', 'VERIFIED_OFFLINE_TEST')) { $f.rejected = $f.verification.status; return $f }
    $outcomeText = [string](Get-DvProp $rc 'outcome')
    if (-not $outcomeText) { return $f }      # no outcome, no figure: the caller types a RAN leg whose receipt names none
    $f.outcome = $outcomeText
    if ($f.verification.status -ceq 'NO_SIGNAL') { $f.noFigure = 'RECEIPT_NO_SIGNAL'; return $f }
    if ($outcomeText -cnotin @('PASS', 'FAIL')) { $f.noFigure = "RECEIPT_OUTCOME_$outcomeText"; return $f }
    $m =Get-DvProp $rc 'metrics'; $pb = Get-DvProp $rc 'playback'; $pm = Get-DvProp $m 'presentMonStats'
    $entry = [pscustomobject]@{ seq = 1; repeat = 1; cellId = $Leg.legId; backend = $Backend; displayMode = [string](Get-DvProp (Get-DvProp $rc 'display') 'requestedMode'); scaleFactor = Get-DvProp (Get-DvProp $rc 'scale') 'requestedScale' }
    $row = try { Get-DvMatrixRow -PlanEntry $entry -Receipt $rc } catch { $null }
    $f.outcome = [string](Get-DvProp $rc 'outcome')
    $f.fps = [ordered]@{
        presentedFps = $(if ($row) { $row.presentedFps } else { $null }); timelineFpsAfterFirstPresent = $(if ($row) { $row.timelineFpsAfterFirstPresent } else { $null })
        cpuPaceTimelineFps = Get-DvProp $m 'cpuPaceTimelineFps'; presentMonFpsEquivalentMean = Get-DvProp $pm 'fpsEquivalentMean'
        nativeFps = Get-DvProp $pb 'nativeFps'; paceFps = Get-DvProp $pb 'paceFps'
    }
    $f.drops = [ordered]@{
        skippedOrUnpresentedRatio = Get-DvProp $m 'skippedOrUnpresentedRatio'; sourceAdvanced = Get-DvProp $pb 'sourceAdvanced'; requiredSourceFrames = Get-DvProp $pb 'requiredSourceFrames'
        gpuFramesTotal = Get-DvProp $m 'gpuFramesTotal'; cpuFrames = Get-DvProp $m 'cpuFrames'
    }
    $f.stall = [ordered]@{
        presentMonTemporalMaxGapMs = Get-DvProp $m 'presentMonTemporalMaxGapMs'; presentMonTemporalGapKind = Get-DvProp $m 'presentMonTemporalGapKind'
        presentMonStatus = Get-DvProp $m 'presentMonStatus'; presentMonP99Ms = Get-DvProp $pm 'p99Ms'
    }
    $f
}

$rows = [System.Collections.Generic.List[object]]::new()
$chains = [System.Collections.Generic.List[object]]::new()
$typed = $null     # the first non-OK typed outcome: @{ reason; detail }
foreach ($b in $StandardBackends) {
    if ([DateTime]::UtcNow -ge $deadline) { if ($null -eq $typed) { $typed = @{ reason = $(if ($rows.Count -gt 0) { 'BASELINE_CAP_EXCEEDED' } else { 'AGENT_BUSY' }); detail = "the $MaxTotalSec s cap was spent before the $b chain started" } }; break }
    Out-Line "BASELINE_CHAIN backend=$b start"
    $c = Invoke-BoundedChain $b
    $chains.Add($c)
    # The chain's exit code is read BEFORE any row is built. A leg is recorded RAN only when the chain ended on its own with a documented exit that agrees with the
    # exit its CHAIN_RESULT reports (the guard in test_playback_evidence_completeness.py pins this statement). Anything else is a typed EVIDENCE_INCOMPLETE row.
    $resultExit = $(if ($c.result -and $c.result.PSObject.Properties['exitCode']) { [int]$c.result.exitCode } else { $null })
    $chainComplete = (-not $c.timedOut) -and ($null -ne $c.exit) -and ($null -ne $resultExit) -and ($resultExit -eq [int]$c.exit) -and (([int]$c.exit) -in 0, 2, 3, 4, 5, 6, 7, 8)
    # A trend figure needs more than a documented, agreeing exit: the chain must also have EXITED 0. A chain that died (7), hit its deadline (3) or stopped on a gate has no clean
    # measurement, even for a leg that RAN before it.
    $chainClean = $chainComplete -and ([int]$c.exit -eq 0)
    $ranHere = 0
    if ($c.result) {
        foreach ($leg in @($c.result.legs)) {
            if ($leg.status -cnotin @('RAN', 'LEG_FAILED')) { continue }
            $ranHere++
            $planLeg = @($legs | Where-Object { $leg.spec -and ([IO.Path]::GetFullPath([string]$leg.spec) -ieq $_.spec) })[0]
            if ($null -eq $planLeg) { $planLeg = @($legs | Where-Object { [string]$leg.name -like "*$($_.legId)*" })[0] }
            if ($null -eq $planLeg) { continue }
            # An unclean chain's receipts are never read: no figure of such a leg reaches the trend file.
            $fig = Get-RowFigures $(if ($chainClean -and $leg.PSObject.Properties['receipt']) { [string]$leg.receipt } else { '' }) $planLeg $b
            $legStatus = [string]$leg.status
            $legOutcome = $(if ($fig.outcome) { $fig.outcome } else { [string]$leg.outcome })
            $failure = $null
            if (-not $chainComplete) { $failure = "the $b chain did not end on its own with a documented exit that agrees with its CHAIN_RESULT (exit=$($c.exit) timedOut=$($c.timedOut) resultExit=$resultExit)" }
            elseif (-not $chainClean) { $failure = "the $b chain ended on its own with a documented non-zero exit ($($c.exit), stopReason=$($c.result.stopReason)), so none of its legs is a clean measurement and no figure is recorded" }
            elseif ($fig.rejected) {
                $failure = "$(if ($fig.rejected -ceq 'INVALID') { 'EVIDENCE_INVALID' } else { 'EVIDENCE_INCOMPLETE' }): the leg's receipt validated $($fig.rejected), so no figure is recorded"
                if ($null -eq $typed) { $typed = @{ reason = 'LEG_FAILED'; detail = "a $b leg ($($planLeg.legId)) has a receipt the validator refused ($($fig.rejected)); no figure was recorded" } }
            }
            elseif ($fig.noFigure) { $failure = "$($fig.noFigure): the leg's receipt is not a PASS/FAIL measurement (outcome $($fig.outcome), validator $($fig.verification.status)), so no figure is recorded" }
            elseif ($legStatus -ceq 'RAN' -and -not $fig.outcome) {
                $failure = 'the leg is RAN but its receipt is missing or unreadable, so no figure can be recorded'
                if ($null -eq $typed) { $typed = @{ reason = 'LEG_FAILED'; detail = "a $b leg ($($planLeg.legId)) is RAN but its receipt is missing or unreadable (EVIDENCE_INCOMPLETE)" } }
            }
            if ($failure -and $legStatus -ceq 'RAN') { $legStatus = 'EVIDENCE_INCOMPLETE'; $legOutcome = 'INVALID' }
            if ($fig.noFigure) { $legOutcome = $fig.outcome }      # a noFigure row keeps the receipt's own outcome (the failure text names why it carries no figure)
            if ($failure) { $fig.fps = $null; $fig.drops = $null; $fig.stall = $null }      # a typed failure never carries figures, whatever the receipt held
            if ($fig.rejected -ceq 'INVALID' -and $legStatus -ceq 'EVIDENCE_INCOMPLETE') { $legStatus = 'EVIDENCE_INVALID' }
            $rows.Add([ordered]@{
                schema = 'mlv-app/venue-baseline-trend/v1'; utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'); venue = $Venue; role = $planLeg.role
                sourceCommit = $commit; leg = $planLeg.legId; card = $planLeg.card; backend = $b; status = $legStatus; failure = $failure
                outcome = $legOutcome; fps = $fig.fps; drops = $fig.drops; stall = $fig.stall; receiptVerification = $fig.verification
                receiptPath = $(if ($leg.PSObject.Properties['receipt'] -and $leg.receipt) { [string]$leg.receipt } else { $null }); buildManifestSha256 = $manifest; runDir = $RunDir; hubLoad = $null
            })
        }
    }
    $stop = $c.result.stopReason
    if ($c.timedOut) { if ($null -eq $typed) { $typed = @{ reason = 'BASELINE_CAP_EXCEEDED'; detail = "the $b chain was still running at the $MaxTotalSec s cap + $KillGraceSec s grace and was killed" } }; break }
    if (-not $chainComplete) { if ($null -eq $typed) { $typed = @{ reason = 'CHAIN_FAILED'; detail = "the $b chain exit ($($c.exit)) and its CHAIN_RESULT (exitCode $resultExit) are not a documented, agreeing pair; its legs are recorded as EVIDENCE_INCOMPLETE" } }; break }
    switch ($c.exit) {
        0 { }
        2 { if ($null -eq $typed) { $typed = @{ reason = 'USAGE'; detail = "the $b chain refused its arguments (CHAIN_USAGE)" } }; break }
        3 {
            if ($null -eq $typed) {
                if ($rows.Count -gt 0) { $typed = @{ reason = 'BASELINE_CAP_EXCEEDED'; detail = "the $b chain hit its deadline after $($rows.Count) leg(s) had run" } }
                else { $typed = @{ reason = 'AGENT_BUSY'; detail = "the $b chain reached its deadline with no leg run: the venue claim was held or the venue never went quiet" } }
            }
            break
        }
        { $_ -in 4, 5, 6, 8 } { if ($null -eq $typed) { $typed = @{ reason = 'GATE_REFUSED'; detail = "the $b chain stopped: exit $($c.exit) stopReason=$stop" } }; break }
        7 { if ($null -eq $typed) { $typed = @{ reason = 'LEG_FAILED'; detail = "a leg process of the $b chain exited non-zero (chain exit 7)" } } }
        default { if ($null -eq $typed) { $typed = @{ reason = 'CHAIN_FAILED'; detail = "the $b chain exited $($c.exit)$(if (-not $c.result) { ' with no CHAIN_RESULT' })" } }; break }
    }
    if ($null -ne $typed -and $typed.reason -cne 'LEG_FAILED') { break }
    if ($null -eq $c.result -and $null -eq $typed) { $typed = @{ reason = 'CHAIN_FAILED'; detail = "the $b chain printed no CHAIN_RESULT" }; break }
}

# ------------------------------------------------------------------ 4. the trend rows ---------------------------------------------------------------------------
$hubEnd = Get-HubLoad
$load = [ordered]@{ host = $hubStart.host; start = [ordered]@{ utc = $hubStart.utc; commitGiB = $hubStart.commitGiB; cpuPercent = $hubStart.cpuPercent }; end = [ordered]@{ utc = $hubEnd.utc; commitGiB = $hubEnd.commitGiB; cpuPercent = $hubEnd.cpuPercent } }
if ($rows.Count -gt 0) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TrendFile) | Out-Null
    $text = ($rows | ForEach-Object { $_['hubLoad'] = $load; ($_ | ConvertTo-Json -Compress -Depth 8) }) -join "`n"
    $fs = [IO.File]::Open($TrendFile, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $bytes = [Text.UTF8Encoding]::new($false).GetBytes($text + "`n"); $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
}
$reason = $(if ($typed) { $typed.reason } else { 'OK' })
$code = [int]$ExitCodes[$reason]
$summary = [ordered]@{
    schema = 'mlv-app/venue-baseline-result/v1'; venue = $Venue; sourceCommit = $commit; buildManifestSha256 = $manifest; runDir = $RunDir; trendFile = $TrendFile
    startedUtc = $startedUtc.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o'); maxTotalSec = $MaxTotalSec; exitCode = $code; reason = $reason
    detail = $(if ($typed) { $typed.detail } else { $null }); rowsAppended = $rows.Count; hubLoad = $load
    chains = @($chains | ForEach-Object { [ordered]@{ backend = $_.backend; exit = $_.exit; timedOut = $_.timedOut; stopReason = $_.result.stopReason; ran = $_.result.ran; failed = $_.result.failed; notRun = $_.result.notRun } })
}
[IO.File]::WriteAllText((Join-Path $RunDir 'baseline-result.json'), ($summary | ConvertTo-Json -Compress -Depth 8))
Out-Line "BASELINE_EXIT code=$code reason=$reason rows=$($rows.Count)$(if ($typed) { " detail=$($typed.detail)" })"
Out-Line ('BASELINE_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 8))
exit $code
