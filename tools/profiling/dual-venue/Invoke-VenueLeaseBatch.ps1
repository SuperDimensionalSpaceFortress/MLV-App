# Invoke-VenueLeaseBatch.ps1 -- run Bachelor bench legs INSIDE one quiet lease per batch (BACHELOR-LEASE-BATCH-LEGS-1). RUN THIS ON THE VM.
#
# WHY. The Bachelor agent takes a heavy-slot.py quiet lease for every gated job. heavy-slot.py is Bachelor's, not ours (45 min max, 30 min pending, 30 min
# post-lease cooldown, <= 50 % of any 3 h), and it refuses a new lease for 30 min after the last one ends. With one lease per leg, and the venue quiet probe
# run as a light job OUTSIDE any lease (where it reads the open desktop), a leg that played about 1 min cost about 34 min (5 of 32 legs on 2026-10-23).
# WHAT. One lease per BATCH, requested FIRST. The probe and the legs then run under it:
#   1. CLAIM. The venue claim (VenueChain.psm1, ticketless) is taken once per batch and held until the batch ends.
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
#         QUIET_PRELOAD_UNKNOWN or LEASE_LOST (uncounted).
#   4. END. A batch ends when its legs are done (ALL_LEGS_DONE), the next leg would outlive the lease (LEASE_REMAINING_SHORT: the rest go to the next batch),
#      the lease was lost (LEASE_LOST), the deadline passed (DEADLINE), or a probe could not measure (PROBE_<verdict>: the run stops). The light job
#      lease-batch-close-<stamp>-lb<token> then releases the lease at once (the agent's idle bound would, later). The agent's own batch receipt is read by
#      name (logs\lease-batch-<token>.jsonl), and the claim is released.
#   5. RECEIPTS. <RunDir>\batch-<nn>.json and one line per batch in <RunDir>\batches.jsonl (mlv-app/venue-lease-batch/v1): token, lease id, held, start and
#      end, the open's wait and refusals, legs attempted / ran / counted, retries (probe and leg re-check), the end reason, the per-leg rows and the agent's
#      receipt. <RunDir>\batch.log has one line per event, UTC stamp first; the last stdout line is LEASE_BATCH_RESULT=<json>.
# It changes nothing on Bachelor: not heavy-slot.py's caps, not its lease file, not the quiet threshold. A batch run needs the lease-batch agent deployed.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeaseBatch.ps1 -LegSpec a.json,b.json -Backend cpu -RunDir <rd> `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-hex> [-DeadlineUtc <iso>]
#
# Exit codes (a leg's OUTCOME is never the exit code: read LEASE_BATCH_RESULT and the receipts): 0 every leg ran; 2 usage (or a leg budget no lease can hold);
# 3 DEADLINE before every leg ran; 4 GATE_UNREADABLE and 5 HOST_MISMATCH from Wait-VenueQuiet; 6 a probe ended VENUE_QUIET_UNKNOWN; 7 a leg process exited
# non-zero (LEG_FAILED; the run goes on); 9 a batch lease could not be opened (the open job was RETRACTED, UNRESOLVED or not admitted); 10 the agent is not
# batch-capable. A stopping code (2-6, 9, 10) replaces 0 or 7.
# -QuietScript / -LegScript / -UmRunScript / -AgentShare / -ClaimDir are seams for the offline tests (tools/repo_hygiene/test_venue_lease_batch.py).
[CmdletBinding()]
param(
    [ValidateSet('bachelor')][string]$Venue = 'bachelor',
    [string[]]$LegSpec = @(),
    [string]$LegSet = '',
    [ValidateRange(1, 20)][int]$Repeats = 1,
    [ValidateSet('cuda', 'cpu')][string]$Backend,
    [Parameter(Mandatory = $true)][string]$RunDir,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256,
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
    [ValidateRange(60, 86400)][int]$ClaimTtlSec = 10800,
    [ValidateRange(1, 3600)][int]$ClaimPollSec = 30,
    [string]$ClaimDir = '',
    [string]$QuietScript = '',
    [string]$LegScript = '',
    [string]$UmRunScript = '',
    [string]$AgentShare = ''
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking

function Stop-Usage([string]$Message) { [Console]::Error.WriteLine("BATCH_USAGE $Message"); exit 2 }

# `pwsh -File` passes an array argument as ONE string, so -LegSpec a.json,b.json is split on commas (an entry that is itself an existing path is kept whole).
$LegSpec = @(foreach ($i in $LegSpec) {
        if ([string]::IsNullOrWhiteSpace($i)) { continue }
        if ((Test-Path -LiteralPath $i) -or $i -notlike '*,*') { $i; continue }
        $i -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    })
if (-not $QuietScript) { $QuietScript = Join-Path $here 'Wait-VenueQuiet.ps1' }
if (-not $LegScript) { $LegScript = Join-Path $here 'Invoke-VenueLeg.ps1' }
if (-not $UmRunScript) { $UmRunScript = Join-Path $here '..\um-run.ps1' }
foreach ($s in $QuietScript, $LegScript, $UmRunScript) { if (-not (Test-Path -LiteralPath $s -PathType Leaf)) { Stop-Usage "no script at '$s'" } }
if (($LegSpec.Count -gt 0) -eq [bool]$LegSet) { Stop-Usage 'name the legs with exactly one of -LegSpec <path>... or -LegSet <name>' }
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

function L([string]$Message) {
    $line = "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message"
    Add-Content -LiteralPath $batchLog -Value $line
    Write-Output $line
}

# The planned legs: name, spec path, backend ('' = whatever the spec lists). Same naming as Invoke-VenueChain.
$plan = [System.Collections.Generic.List[object]]::new()
if ($LegSet) {
    Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force
    $sp = try { Get-DvLegSetPlan -LegSetPath (Join-Path $here "legsets\$LegSet.json") -Repeats $Repeats -Backend $(if ($Backend) { $Backend } else { '' }) } catch { Stop-Usage $_.Exception.Message }
    foreach ($e in $sp.plan) { $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f $e.seq, $e.legId, $e.backend); spec = $e.specPath; backend = $e.backend }) }
} else {
    foreach ($ref in $LegSpec) {
        $path = $(if (Test-Path -LiteralPath $ref -PathType Leaf) { $ref } elseif (Test-Path -LiteralPath (Join-Path $here "legs\$ref") -PathType Leaf) { Join-Path $here "legs\$ref" } else { Stop-Usage "no leg spec at '$ref'" })
        $path = (Resolve-Path -LiteralPath $path).Path
        $legId = try { [string](([IO.File]::ReadAllText($path) | ConvertFrom-Json).legId) } catch { '' }
        if ($legId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$') { $legId = [IO.Path]::GetFileNameWithoutExtension($path) }
        for ($r = 1; $r -le $Repeats; $r++) {
            $plan.Add([pscustomobject]@{ name = ('{0:00}-{1}-{2}' -f ($plan.Count + 1), $legId, $(if ($Backend) { $Backend } else { 'spec' })); spec = $path; backend = $(if ($Backend) { $Backend } else { '' }) })
        }
    }
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
    [pscustomobject]@{ verdict = $verdict; code = $code; mean = $mean; reason = $reason; foreignFloor = [bool]$ff }
}

function Submit-AgentJob([string]$JobId, [string]$Body, [int]$TimeoutSec, [int]$WaitSec) {
    # One job through um-run.ps1 (the only writer to a venue share). RECEIPT | RETRACTED | UNRESOLVED | ERROR; a thrown RETRACTED / UNRESOLVED is data here.
    $f = Join-Path $jobDir "$JobId.job.ps1"
    [IO.File]::WriteAllText($f, $Body + "`r`n", [Text.Encoding]::ASCII)
    try {
        $o = @(& $UmRunScript -ScriptPath $f -JobId $JobId -AgentShare $AgentShare -TimeoutSec $TimeoutSec -MaxQueueWaitSec $WaitSec -MaxClaimedWaitSec 300 -MaxAdmissionWaitSec $WaitSec 6>$null)
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
        $ended = [DateTimeOffset]::Parse([string]$Result.endedUtc, [Globalization.CultureInfo]::InvariantCulture).ToUnixTimeMilliseconds() / 1000.0
        [DateTime]::UtcNow.AddSeconds([double]$lb.untilEpoch - $ended)
    } catch { $null }
}

$pending = [System.Collections.Generic.List[object]]::new()
foreach ($p in $plan) { $pending.Add($p) }
$results = [System.Collections.Generic.List[object]]::new()
$batches = [System.Collections.Generic.List[object]]::new()
$exitCode = 0
$stopReason = $null
$legBudget = $LegBudgetSec
$batchNo = 0
function Set-Stop([string]$Reason, [int]$Code) { $script:stopReason = $Reason; if ($script:exitCode -eq 0 -or $script:exitCode -eq 7) { $script:exitCode = $Code } }
function Add-Result([object]$Leg, [string]$Status, [hashtable]$Extra) {
    $r = [ordered]@{ name = $Leg.name; spec = $Leg.spec; backend = $Leg.backend; status = $Status }
    foreach ($k in $Extra.Keys) { $r[$k] = $Extra[$k] }
    $results.Add([pscustomobject]$r)
}

L "BATCH_RUN_START venue=$Venue legs=$($plan.Count) runDir=$RunDir agentShare=$AgentShare deadline=$($deadline.ToString('o')) threshold=$ThresholdPercent legBudgetSec=$LegBudgetSec leaseMarginSec=$LeaseMarginSec recheckSec=$RecheckSec cooldownSec=$CooldownSec pid=$PID quietScript=$QuietScript legScript=$LegScript umRunScript=$UmRunScript"

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
    }
    # (1) the claim, once per batch. A LIVE claim of another chain is waited out; stale claims are broken once each.
    $claim = $null; $lastHolder = ''
    while ($true) {
        $req = Request-VenueClaim -Venue $Venue -RunDir $RunDir -Purpose "$Purpose batch $batchNo" -TtlSec $ClaimTtlSec -ClaimDir $ClaimDir
        foreach ($bk in $req.broken) { L "CLAIM_STALE_BROKEN venue=$Venue reason=$($bk.reason) ownerPid=$($bk.claim.ownerPid) runDir=$($bk.claim.runDir) expiresUtc=$($bk.claim.expiresUtc)" }
        if ($req.granted) { $claim = $req.claim; break }
        $holder = $(if ($req.claim) { "pid=$($req.claim.ownerPid) runDir=$($req.claim.runDir) purpose=$($req.claim.purpose)" } else { $req.state })
        if ($holder -cne $lastHolder) { L "CLAIM_HELD batch=$batchNo state=$($req.state) holder $holder -> wait"; $lastHolder = $holder }
        if ([DateTime]::UtcNow.AddSeconds($ClaimPollSec) -ge $deadline) { break }
        Start-Sleep -Seconds $ClaimPollSec
    }
    if ($null -eq $claim) {
        $b.endReason = 'DEADLINE'; $b.endDetail = 'claim-held'; Set-Stop 'DEADLINE' 3
        $b.endedUtc = [DateTime]::UtcNow.ToString('o'); $batches.Add([pscustomobject]$b)
        L "BATCH_END batch=$batchNo token=$token reason=DEADLINE detail=claim-held"
        break
    }
    $b.claimNonce = $claim.nonce
    L "CLAIM_TAKEN batch=$batchNo nonce=$($claim.nonce) expiresUtc=$($claim.expiresUtc)"
    try {
        # (2) OPEN: the lease first. Nothing is probed or run before the agent reports this batch's lease active.
        $b.openJobId = "lease-batch-open-$stamp-lb$token"
        $wait = [int][Math]::Max(60, [Math]::Min($OpenAdmissionWaitSec, ($deadline - [DateTime]::UtcNow).TotalSeconds))
        L "BATCH_OPEN_SUBMIT batch=$batchNo token=$token job=$($b.openJobId) admissionWaitSec=$wait (the agent requests the lease; heavy-slot.py's cooldown is waited out there)"
        $op = Submit-AgentJob $b.openJobId "Write-Output ('LEASE_BATCH_OPEN token=$token held=' + [string]`$env:HEAVY_SLOT_HELD + ' admission=' + [string]`$env:MLV_AGENT_ADMISSION)" 120 $wait
        $adm = $(if ($null -ne $op.result) { $op.result.admission } else { $null })
        $b.openOutcome = $op.outcome
        if ($op.outcome -cne 'RECEIPT' -or $null -eq $op.result -or [int]$op.result.exitCode -ne 0 -or $null -eq $adm -or [string]$adm.label -cne 'clear') {
            $b.endReason = 'OPEN_FAILED'; $b.endDetail = "outcome=$($op.outcome) exit=$(if ($op.result) { $op.result.exitCode }) label=$(if ($adm) { $adm.label }) $($op.message)".Trim()
            Set-Stop 'OPEN_FAILED' 9
        } elseif ($null -eq $adm.leaseBatch -or [string]$adm.leaseBatch.token -cne $token -or -not $adm.leaseBatch.leaseId) {
            $b.endReason = 'AGENT_NOT_BATCH_CAPABLE'; $b.endDetail = "admission.mode=$($adm.mode) gate=$($adm.gate) (no admission.leaseBatch for token ${token}: the lease-batch agent is not deployed)"
            Set-Stop 'AGENT_NOT_BATCH_CAPABLE' 10
        } else {
            $b.leaseId = [string]$adm.leaseBatch.leaseId; $b.held = [string]$adm.heavySlotHeld; $b.slot = $adm.slot; $b.leaseMinutes = $adm.leaseMinutes
            $b.leaseUntilEpoch = $adm.leaseBatch.untilEpoch; $b.openWaitedSec = $adm.waitedSec; $b.openRequests = $adm.leaseRequests; $b.refusals = @($adm.leaseRefusals)
            $leaseEnd = Get-LeaseEndUtc $op.result
            if ($null -eq $leaseEnd) { $leaseEnd = [DateTime]::UtcNow }   # cannot place the lease's end: no leg fits
            $b.leaseEndUtc = $leaseEnd.ToString('o')
            L "BATCH_OPEN batch=$batchNo token=$token lease=$($b.leaseId) held=$($b.held) minutes=$($b.leaseMinutes) roomSec=$([int]($leaseEnd - [DateTime]::UtcNow).TotalSeconds) openWaitedSec=$($b.openWaitedSec) refusals=$(@($b.refusals).Count)"
            # (3) the legs, under the lease
            $attempt = 0
            while ($pending.Count -gt 0) {
                $leg = $pending[0]
                if ([DateTime]::UtcNow -ge $deadline) { $b.endReason = 'DEADLINE'; Set-Stop 'DEADLINE' 3; break }
                $room = [int](($leaseEnd - [DateTime]::UtcNow).TotalSeconds) - $LeaseMarginSec
                if ($room -lt $legBudget) {
                    $b.endReason = 'LEASE_REMAINING_SHORT'; $b.endDetail = "roomSec=$room < legBudgetSec=$legBudget (leaseMarginSec=$LeaseMarginSec): leg $($leg.name) goes to the next batch"
                    break
                }
                $attempt++
                $probeWait = [int][Math]::Max(0, [Math]::Min($ProbeMaxWaitSec, $room - $legBudget))
                $q = Invoke-QuietProbe $leg.name "b$('{0:00}' -f $batchNo)-a$attempt" $probeWait
                L "QUIET leg=$($leg.name) batch=$batchNo phase=in-lease exit=$($q.code) verdict=$($q.verdict) mean=$(Format-Mean $q.mean) maxWaitSec=$probeWait roomSec=$room$(if ($q.reason) { " reason=$($q.reason)" })$(if ($q.foreignFloor) { ' foreignFloor=1' })"
                if ($q.verdict -ceq 'USAGE') { $b.endReason = 'PROBE_USAGE'; Set-Stop 'USAGE' 2; break }
                if ($q.verdict -ceq 'GATE_UNREADABLE') { $b.endReason = 'PROBE_GATE_UNREADABLE'; Set-Stop 'GATE_UNREADABLE' 4; break }
                if ($q.verdict -ceq 'HOST_MISMATCH') { $b.endReason = 'PROBE_HOST_MISMATCH'; Set-Stop 'HOST_MISMATCH' 5; break }
                if ($q.verdict -ceq 'VENUE_QUIET_UNKNOWN') { $b.endReason = 'PROBE_VENUE_QUIET_UNKNOWN'; $b.endDetail = $q.reason; Set-Stop 'VENUE_QUIET_UNKNOWN' 6; break }
                if ($q.verdict -cne 'QUIET') {
                    # COOLDOWN_UNMET (a measured busy desktop, a foreign floor included) or GATE_BUSY: retry INSIDE this lease, never a release and a new request.
                    $b.retries++; $b.probeRetries++
                    L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=$($q.verdict) mean=$(Format-Mean $q.mean) roomSec=$room"
                    if ($q.verdict -ceq 'GATE_BUSY') { Start-Sleep -Seconds ([int][Math]::Max(1, [Math]::Min($ClaimPollSec, $room - $legBudget))) }
                    continue
                }
                # QUIET under the lease: the leg, on the same lease.
                $b.legsAttempted++
                $out = Join-Path $RunDir "leg-$($leg.name)-b$('{0:00}' -f $batchNo)-a$attempt.out.txt"
                $lArgs = @('-NoProfile', '-NonInteractive', '-File', $LegScript, '-Venue', $Venue, '-LegSpec', $leg.spec, '-SourceCommit', $SourceCommit, '-BuildManifestSha256', $BuildManifestSha256, '-LeaseBatch', $token)
                if ($leg.backend) { $lArgs += @('-Backend', $leg.backend) }
                if ($SheetCopyDir) { $lArgs += @('-SheetCopyDir', $SheetCopyDir) }
                L "LEG $($leg.name) RUN batch=$batchNo lease=$($b.leaseId) quietMean=$(Format-Mean $q.mean) roomSec=$room legBudgetSec=$legBudget spec=$($leg.spec) backend=$(if ($leg.backend) { $leg.backend } else { 'spec' })"
                $tLeg = [DateTime]::UtcNow
                & $pwshExe @lArgs *> $out
                $legCode = $LASTEXITCODE
                $legSec = [int]([DateTime]::UtcNow - $tLeg).TotalSeconds
                if ($legSec -gt $legBudget) { $legBudget = [Math]::Min(2700, $legSec) }
                $text = $(if (Test-Path -LiteralPath $out) { [IO.File]::ReadAllText($out) } else { '' })
                $legLines = @($text -split "`r?`n")
                $o = $legLines | Where-Object { $_ -match '^DVE_OUTCOME=(\S+)' } | Select-Object -Last 1
                $outcome = $(if ($o -and $o -match '^DVE_OUTCOME=(\S+)') { $Matches[1] } else { 'NONE' })
                $pl = $legLines | Where-Object { $_ -match '^DVE_RECEIPT_PATH=(.+)$' } | Select-Object -Last 1
                $receipt = $(if ($pl -and $pl -match '^DVE_RECEIPT_PATH=(.+)$') { $Matches[1].Trim() } else { $null })
                $dl = $legLines | Where-Object { $_ -match '^DVE_DETAIL=' } | Select-Object -Last 1
                $detail = $(if ($dl) { ($dl -replace '^DVE_DETAIL=', '').Trim() } else { '' })
                $legJobId = @(Get-VenueRemoteJobIds -Text $text | Where-Object { $_ -and $_ -cnotmatch '-health$' }) | Select-Object -Last 1
                if ($legCode -ne 0) {
                    # Invoke-VenueLeg exits 0 whenever it wrote a receipt; non-zero means it died without one. Not evidence: LEG_FAILED, the run goes on.
                    $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'LEG_FAILED'; outcome = 'INVALID'; printedOutcome = $outcome; legExit = $legCode; jobId = $legJobId; counted = $false; legSec = $legSec }
                    $b.legs.Add([pscustomobject]$row); Add-Result $leg 'LEG_FAILED' $row; $pending.RemoveAt(0)
                    L "LEG $($leg.name) LEG_FAILED batch=$batchNo exit=$legCode outcome=INVALID printedOutcome=$outcome (the leg exited non-zero: no usable receipt)"
                    if ($exitCode -eq 0) { $exitCode = 7 }
                    continue
                }
                # Did it run on THIS batch's lease? Read by name, bounded.
                $jr = Get-VenueJobResult -AgentShare $AgentShare -JobId $legJobId
                $ja = $(if ($jr.state -ceq 'FOUND') { $jr.result.admission } else { $null })
                $jobLease = $(if ($null -ne $ja -and $null -ne $ja.leaseBatch) { [string]$ja.leaseBatch.leaseId } else { $null })
                $onLease = ($null -ne $jobLease -and $jobLease -ceq $b.leaseId -and [string]$ja.slotContinuity -cne 'lost')
                if ($onLease) { $e2 = Get-LeaseEndUtc $jr.result; if ($null -ne $e2 -and $e2 -lt $leaseEnd) { $leaseEnd = $e2; $b.leaseEndUtc = $leaseEnd.ToString('o') } }
                $leaseWhy = "jobId=$legJobId result=$($jr.state) jobLease=$jobLease slotContinuity=$(if ($ja) { $ja.slotContinuity })$(if ($jr.error) { " error=$($jr.error)" })"
                if ($outcome -ceq 'VENUE_NOT_QUIESCENT') {
                    # The leg's own <= 20 % re-check at its start failed: it never played. Retry INSIDE the same lease (on a lost lease, in the next batch).
                    $b.retries++; $b.legRetries++
                    $b.legs.Add([pscustomobject][ordered]@{ batch = $batchNo; attempt = $attempt; status = 'RETRY'; outcome = $outcome; receipt = $receipt; jobId = $legJobId; jobLease = $jobLease; onLease = $onLease; counted = $false; legSec = $legSec })
                    L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=LEG_NOT_QUIESCENT receipt=$receipt onLease=$onLease $leaseWhy"
                    if (-not $onLease) { $b.endReason = 'LEASE_LOST'; $b.endDetail = $leaseWhy; break }
                    continue
                }
                $preLoad = Get-VenueLegPreLoadMean -ReceiptPath $receipt
                # Only a QUIET probe reaches here (a FOREIGN_FLOOR exit is a COOLDOWN_UNMET verdict, retried above), so the class is the lease and the pre-load.
                $class = $(if (-not $onLease) { 'LEASE_LOST' } elseif ($null -eq $preLoad) { 'QUIET_PRELOAD_UNKNOWN' } elseif ($preLoad -le 30.0) { 'QUIET_PRELOAD_OK' } else { 'QUIET_PRELOAD_HIGH' })
                $counted = ($class -ceq 'QUIET_PRELOAD_OK')
                $row = [ordered]@{ batch = $batchNo; attempt = $attempt; status = 'RAN'; outcome = $outcome; class = $class; counted = $counted; quietVerdict = $q.verdict; quietMean = $q.mean; preLoadMean = $preLoad
                    receipt = $receipt; jobId = $legJobId; jobLease = $jobLease; onLease = $onLease; legSec = $legSec; legExit = $legCode }
                if ($detail) { $row['outcomeDetail'] = $detail }
                $b.legs.Add([pscustomobject]$row); Add-Result $leg 'RAN' $row; $pending.RemoveAt(0)
                $b.legsRan++; if ($counted) { $b.legsCounted++ }
                L "LEG $($leg.name) RAN batch=$batchNo exit=$legCode outcome=$outcome class=$class counted=$counted quietMean=$(Format-Mean $q.mean) preLoadMean=$(Format-Mean $preLoad) legSec=$legSec receipt=$receipt $leaseWhy"
                if (-not $onLease) { $b.endReason = 'LEASE_LOST'; $b.endDetail = $leaseWhy; break }
                if ($pending.Count -gt 0 -and $CooldownSec -gt 0) { L "COOLDOWN ${CooldownSec} s (inside the lease)"; Start-Sleep -Seconds $CooldownSec }
            }
            if ($null -eq $b.endReason -and $pending.Count -eq 0) { $b.endReason = 'ALL_LEGS_DONE' }
            # A FRESH lease with no room for even one leg can never run it: stop rather than open lease after lease.
            if ($b.endReason -ceq 'LEASE_REMAINING_SHORT' -and $b.legsAttempted -eq 0 -and $b.probeRetries -eq 0) {
                $b.endReason = 'LEG_BUDGET_EXCEEDS_LEASE'; Set-Stop 'LEG_BUDGET_EXCEEDS_LEASE' 2
            }
        }
    } finally {
        # (4) END: release the lease now (a light close job), read the agent's receipt for it, release the claim, write the batch receipt.
        if ($b.openJobId) {
            $b.closeJobId = "lease-batch-close-$stamp-lb$token"
            $cl = Submit-AgentJob $b.closeJobId "Write-Output 'LEASE_BATCH_CLOSE token=$token'" 60 300
            $b.closeOutcome = $cl.outcome
            if ($b.leaseId) {
                $ar = Read-VenueLeaseBatchReceipt -AgentShare $AgentShare -Token $token -LeaseId $b.leaseId
                $b.agent = $(if ($ar.state -ceq 'FOUND') { $ar.record } else { [pscustomobject]@{ state = $ar.state; error = $ar.error } })
            }
        }
        L "CLAIM_RELEASED batch=$batchNo result=$(Release-VenueClaim -Venue $Venue -Nonce $claim.nonce -ClaimDir $ClaimDir)"
        $b.endedUtc = [DateTime]::UtcNow.ToString('o')
        $rec = [pscustomobject]$b
        [IO.File]::WriteAllText((Join-Path $RunDir ('batch-{0:00}.json' -f $batchNo)), ($rec | ConvertTo-Json -Depth 8) + "`n", [Text.UTF8Encoding]::new($false))
        Add-Content -LiteralPath (Join-Path $RunDir 'batches.jsonl') -Value ($rec | ConvertTo-Json -Compress -Depth 8)
        $batches.Add($rec)
        L "BATCH_END batch=$batchNo token=$token lease=$($b.leaseId) reason=$($b.endReason) attempted=$($b.legsAttempted) ran=$($b.legsRan) counted=$($b.legsCounted) retries=$($b.retries) probeRetries=$($b.probeRetries) legRetries=$($b.legRetries) refusals=$(@($b.refusals).Count) close=$($b.closeOutcome) agentEnd=$(if ($b.agent) { $b.agent.endReason })$(if ($b.endDetail) { " detail=$($b.endDetail)" })"
    }
}
foreach ($leg in $pending) { Add-Result $leg 'NOT_RUN' @{ verdict = $(if ($stopReason) { $stopReason } else { 'NOT_RUN' }); reason = 'run-stopped' }; L "LEG $($leg.name) NOT_RUN $stopReason reason=run-stopped" }

$summary = [ordered]@{
    schema = 'mlv-app/venue-lease-batch-result/v1'; venue = $Venue; runDir = $RunDir; startedUtc = $t0.ToString('o'); finishedUtc = [DateTime]::UtcNow.ToString('o')
    exitCode = $exitCode; stopReason = $stopReason; legs = @($results)
    ran = @($results | Where-Object { $_.status -ceq 'RAN' }).Count; counted = @($results | Where-Object { $_.status -ceq 'RAN' -and $_.counted }).Count
    failed = @($results | Where-Object { $_.status -ceq 'LEG_FAILED' }).Count; notRun = @($results | Where-Object { $_.status -ceq 'NOT_RUN' }).Count
    batches = @($batches | ForEach-Object { [ordered]@{ batch = $_.batch; token = $_.token; leaseId = $_.leaseId; endReason = $_.endReason; legsAttempted = $_.legsAttempted; legsRan = $_.legsRan; legsCounted = $_.legsCounted; retries = $_.retries } })
}
$resultLine = 'LEASE_BATCH_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 6)
Add-Content -LiteralPath $batchLog -Value $resultLine
Write-Output $resultLine
Complete-VenueProcess -ExitCode $exitCode   # a share read abandoned at its timeout would otherwise keep this process alive until it ends
exit $exitCode
