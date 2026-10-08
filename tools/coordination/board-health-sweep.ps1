# Board lane-health sweep. Writes the health.log line every liveness claim on this board
# reads. Adapted from the AudioMile LANE-HEALTH FRAMEWORK. ASCII only.
#
# PROMOTED FROM `.claude-state/coordination/dual-lane/health/sweep-lane-health.ps1` under
# SWEEP-CLASS-1 (Layi authorised 2026-08-08). The original header declared the intent -
# "Tier-A lane-health sweep (untracked; Tier-B promotes a reviewed copy to tools/)" - but
# the promotion had never happened, so the board's most-read instrument lived in a
# gitignored path where `owner-of.ps1` returns `unknown`, `lane-commit` hard-blocks it, and
# no gated range could contain it. It could not be reviewed, only edited.
#
# It is deliberately NOT named `sweep-lane-health.ps1`: that name belongs to the
# machine-readable JSON sweep in this directory, which has its own consumers
# (`run-lane-heartbeat.ps1`, the AGENTS.md contract, `tests/coordination/test_lane_health_contract.py`)
# and a different output format. Two different instruments shared one filename in two
# directories, which is how the card's own line numbers came to point at a file the
# implementer would not have been editing. Both now read ONE class list.
param(
    [string]$HealthDir = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.claude-state\coordination\dual-lane\health'),
    [switch]$Quiet
)
$ErrorActionPreference = 'SilentlyContinue'
# --- staleness guard -------------------------------------------------------------------
# The board root is the ONLY checkout that carries .claude-state/, and it routinely sits on a
# peer branch -- so an absolute-path invocation of this script can silently run a months-old
# copy and print a WRONG board reading. Refuse that. See assert-script-currency.ps1.
$__guard = Join-Path $PSScriptRoot 'assert-script-currency.ps1'
if (Test-Path -LiteralPath $__guard) { & $__guard -ScriptPath $PSCommandPath }
# ---------------------------------------------------------------------------------------


# Enforce the log clause of COORDINATION-PRUNE-POLICY.md -- "rotate at 1 MB, keep 2 rotations,
# delete older" -- which names health.log explicitly. Measured 2026-09-04: NOTHING implemented that
# clause, no writer rotated and no .log.N existed, so health.log had reached 8.3 MB and
# codex-delivery-watcher.log 30.2 MB. This sweep is the writer of health.log, so it enforces its own
# budget rather than relying on someone noticing. The helper is idempotent and returns immediately
# below the cap.
#
# try/catch on purpose: a failed rotation is a log-size problem, a THROWN rotation is a lost health
# reading -- and this sweep is the board's most-read liveness instrument.
try {
    $rotator = Join-Path $PSScriptRoot 'rotate-logs.ps1'
    if (Test-Path -LiteralPath $rotator) {
        & $rotator -Root (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) '.claude-state') | Out-Null
    }
} catch { }

$dualLane = Split-Path $HealthDir -Parent
$now = [DateTime]::UtcNow
$registryPath = Join-Path $dualLane 'seat-registry.json'

# SWEEP-CLASS-1 (a): one tracked authority for the class list and the DARK boundary.
#
# SWEEP-REG-1 -- THIS LOAD MUST FAIL LOUD, AND THE FIRST VERSION DID NOT.
# The script sets $ErrorActionPreference = 'SilentlyContinue' (inherited from the original,
# because the lane loop below is deliberately tolerant of unreadable individual files). That
# swallowed a failed import here: $classes became $null, both thresholds cast to 0, the
# boundary became min(cadence,0)+0 = 0, and EVERY lane with a positive lease age was
# classified DARK. Observed on the first live run: all five lanes DARK, including leases
# 0.4 minutes old.
#
# The failure direction is the expensive one in the opposite way from [B3]: [B3] silenced a
# true alarm, this raises a false one on every lane at once -- and a board-wide false DARK
# is what triggers reseats and succession. **An instrument that cannot load its own
# thresholds must say so, not invent them.** Silence would be bad; a confident wrong reading
# is worse; an explicit instrument failure is correct.
$classesPath = Join-Path $PSScriptRoot 'lane-health-classes.psd1'
$classes = $null
$classesError = ''
try {
    $classes = Import-PowerShellDataFile -LiteralPath $classesPath -ErrorAction Stop
} catch {
    $classesError = $_.Exception.Message
}
$darkCap = if ($null -ne $classes) { [double]$classes.DarkThresholdCapMinutes } else { 0 }
$darkGrace = if ($null -ne $classes) { [double]$classes.DarkThresholdGraceMinutes } else { 0 }
$hbFresh = if ($null -ne $classes) { [double]$classes.WatchHeartbeatFreshMinutes } else { 0 }
# Validate the VALUES, not merely that the file parsed: a psd1 that loads but carries a
# missing or zero threshold produces the identical board-wide false DARK.
# $hbFresh is validated here too, now that it is actually USED. A psd1 that loads without
# WatchHeartbeatFreshMinutes would otherwise set it to 0 and mark every inbound-watch
# heartbeat STALE -- the same fail-open shape as the DARK regression, one field over.
if ($null -eq $classes -or $darkCap -le 0 -or $darkGrace -le 0 -or $hbFresh -le 0) {
    $why = if ($classesError) { $classesError } else { "thresholds invalid (cap=$darkCap grace=$darkGrace hbFresh=$hbFresh)" }
    $failLine = "$($now.ToString('yyyy-MM-ddTHH:mm:ssZ')) OVERALL=INSTRUMENT-FAILED reason=lane-health-classes-unreadable path=$classesPath detail=$why"
    # Recorded in health.log so the outage is visible to anyone reading the board's history,
    # and deliberately NOT accompanied by any lane classification -- emitting lane states
    # here is exactly the defect being fixed.
    Add-Content -Path (Join-Path $HealthDir 'health.log') -Value $failLine -Encoding utf8
    if (-not $Quiet) { Write-Output $failLine }
    Write-Error "board-health-sweep: cannot load $classesPath - $why. Refusing to classify lanes; no health reading was produced."
    exit 3
}

$registryState = 'MISSING'
$registry = $null
if (Test-Path -LiteralPath $registryPath -PathType Leaf) {
    try {
        $registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
        if (-not $registry.seats) { throw 'seat-registry.json has no seats object' }
        $registryState = 'VALID'
    } catch {
        $registryState = 'UNPARSEABLE'
    }
}

# lane -> journal file (last-write age is the comms proxy)
$journals = [ordered]@{
    'fable'         = Join-Path $dualLane 'fable.md'
    'codex'         = Join-Path $dualLane 'codex.md'
    'opus'          = Join-Path $dualLane 'opus.md'
    'sol'           = Join-Path $dualLane 'sol.md'
    'claude-review' = Join-Path (Split-Path $dualLane -Parent) 'gpu-lane-impl-review-sync.md'
    'claude-impl'   = Join-Path $dualLane 'claude-impl.md'
}
$seatKeys = @{
    'fable' = 'fable'
    'codex' = 'codex'
    'opus' = 'opus'
    'sol' = 'sol'
    'claude-review' = 'claude'
    'claude-impl' = 'claude-impl'
}

$laneStates = @()
$journalAges = @{}
$laneGaps = @()          # SWEEP-CLASS-1 (b), cadence-keyed inter-renewal gaps
$leaseAges = @{}         # SWEEP-CLASS-1 (d), so watchhb can be read against lease age
foreach ($lane in $journals.Keys) {
    $leasePath = Join-Path $HealthDir "leases\$lane.json"
    $seatKey = $seatKeys[$lane]
    $seat = if ($registryState -eq 'VALID') { $registry.seats.$seatKey } else { $null }
    $registered = $null -ne $seat
    $expectedSession = if ($registered) { [string]$seat.session } else { $null }
    $state = if ($registered) { 'MISSING' } else { 'PENDING-ADOPTION' }
    $leaseAgeMin = $null
    $leaseSession = $null
    $declaredDormant = $false
    if (Test-Path $leasePath) {
        try {
            $lease = Get-Content $leasePath -Raw | ConvertFrom-Json
            if ([string]$lease.lane -ne $lane) { throw 'lane field mismatch' }
            $leaseSession = [string]$lease.session
            $renewed = [DateTime]::Parse($lease.renewed, $null, 'AdjustToUniversal')
            $leaseAgeMin = [math]::Round(($now - $renewed).TotalMinutes, 1)
            $cad = [double]$lease.leaseMinutes
            if ($cad -le 0) { throw 'leaseMinutes must be positive' }
            $note = [string]$lease.note
            # [B3], LANE-4 review of 66549181..c2caf6bc. A THIRD alternative used to sit
            # here: `\bdeclared\b.{0,100}\b(?:dormant|dormancy)\b` -- a 100-character
            # PROXIMITY match on lane-authored free text, with NO POLARITY. It matched
            # notes that assert the lane is NOT dormant:
            #
            #   'declared 20m cadence. Not dormant, not parked, actively reviewing'   -> MATCH
            #   'declared cadence 5; no dormancy claimed by this lane'                -> MATCH
            #
            # and because a match lands in the SEATED-UNLEASED arm below, which is tested
            # BEFORE DARK, every one of those would convert a genuinely dark seat into
            # SEATED-UNLEASED. The failure direction is the expensive one: it does not
            # raise a false alarm, it SILENCES a true one, and a dead seat reading
            # SEATED-UNLEASED is a seat nobody goes looking for.
            #
            # It also contradicted this change's own thesis. lane-health-classes.psd1 says
            # of the watch-heartbeat classes that "a green token that also means 'could not
            # tell' is worth less than no token at all" -- SEATED-UNLEASED inferred from
            # prose IS that failure, in the one class list this change makes canonical.
            # A DECLARED state must be declared with a TOKEN, not inferred from sentiment.
            $declaredDormant = $note -match '(?i)\bDORMANT-BY-BLOCKER\b|\bSEATED-UNLEASED\b'
            # SWEEP-CLASS-1 (a): ONE boundary, from the shared class file. EXPIRED is
            # deleted - it existed only to name the interval between two boundary
            # functions that agreed at exactly declared=20 and disagreed everywhere else.
            $darkThreshold = [math]::Min($cad, $darkCap) + $darkGrace
            if (-not $registered) { $state = 'UNREGISTERED-LEASE' }
            elseif ($leaseSession -ne $expectedSession) { $state = 'DISPLACED' }
            elseif ($leaseAgeMin -le $darkThreshold) { $state = 'LIVE' }
            elseif ($declaredDormant) { $state = 'SEATED-UNLEASED' }
            else { $state = 'DARK' }
            # SWEEP-CLASS-1 (b): CADENCE-keyed inter-renewal gap. Deleting EXPIRED removes
            # the board's only signal that a lane had missed its OWN declared renewal
            # cadence while still being LIVE by the board threshold - that visibility was
            # accidental (a side effect of the second boundary) and is restored here
            # deliberately, keyed to cadence rather than to a second liveness rule. This is
            # the H3 instrument: it measures cadence, and it is NEVER a liveness verdict.
            if ($state -eq 'LIVE' -and $leaseAgeMin -gt $cad) {
                $laneGaps += ('gap:{0}=OVER({1}m/{2}m)' -f $lane, $leaseAgeMin, $cad)
            }
        } catch { $state = 'LEASE-UNPARSEABLE' }
    }
    $jAgeMin = $null
    $jp = $journals[$lane]
    if (Test-Path $jp) {
        $jAgeMin = [math]::Round(($now - (Get-Item $jp).LastWriteTimeUtc).TotalMinutes, 0)
    }
    $journalAges[$lane] = $jAgeMin
    $leaseAges[$lane] = @{ ageMin = $leaseAgeMin; state = $state }
    $laneStates += [pscustomobject]@{
        lane = $lane
        state = $state
        leaseAgeMin = $leaseAgeMin
        journalAgeMin = $jAgeMin
        registered = $registered
        expectedSession = $expectedSession
        leaseSession = $leaseSession
        declaredDormant = $declaredDormant
    }
}

# lead watcher processes (fable's heartbeat-check instances, marker match)
$watchers = 0
try {
    $watchers = @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" |
        Where-Object { $_.CommandLine -match 'heartbeat-check\.ps1' -and $_.CommandLine -match '\.fable-' }).Count
} catch {}

# overall: declared seated-unleased is intentional; mismatched or undeclared stale seats degrade.
$adopted = $laneStates | Where-Object { $_.registered }
$anyDark = [bool]($adopted | Where-Object { $_.state -in @('MISSING','DARK','DISPLACED','LEASE-UNPARSEABLE') })
$freshest = ($journalAges.Values | Where-Object { $null -ne $_ } | Measure-Object -Minimum).Minimum
$silentWhileOthersMove = [bool]($laneStates | Where-Object {
    $_.journalAgeMin -gt 90 -and
    $freshest -lt 30 -and
    $_.state -notin @('LIVE','PENDING-ADOPTION','SEATED-UNLEASED')
})
# EXPIRED is gone, so ATTENTION is now driven by the CADENCE signal that replaced it
# (SWEEP-CLASS-1 b). This keeps OVERALL a three-state reading rather than collapsing it to
# HEALTHY/DEGRADED: a lane still LIVE by the board threshold but past its own declared
# renewal cadence is exactly the condition EXPIRED used to surface, and it is worth
# attention without being a liveness failure. LEASE-UNPARSEABLE already degrades via
# $anyDark, so it is not double-counted here.
$anyCadenceGap = [bool]($laneGaps.Count -gt 0)

$overall = 'HEALTHY'
if ($registryState -ne 'VALID' -or $anyDark -or $silentWhileOthersMove) { $overall = 'DEGRADED' }
elseif ($anyCadenceGap) { $overall = 'ATTENTION' }

$parts = $laneStates | ForEach-Object {
    $la = if ($null -ne $_.leaseAgeMin) { "$($_.leaseAgeMin)m" } else { '-' }
    $ja = if ($null -ne $_.journalAgeMin) { "$($_.journalAgeMin)m" } else { '-' }
    "$($_.lane)=$($_.state)(lease:$la,journal:$ja)"
}
# CONSULT-1 consumer arm (fable SEQ 975): inbound-watch heartbeats are telemetry until the
# sole liveness authority CONSUMES them (sol SEQ 212). Additive tokens only; OK<=5m else STALE.
# Identity check (claude SEQ 333 / fable SEQ 976): the body's session= must match the registry
# seat for the lane, else FOREIGN - a filename is an identity CLAIM, not identity (SEQ 812 shape).
# STALE + lapsed lease + dormant transcript = coherent DEAD-SEAT signature, not a watch incident.
$watchHb = @()
try {
    Get-ChildItem -LiteralPath $HealthDir -Force -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '.*-inbound-watch-heartbeat' } | ForEach-Object {
            $hbAge = [math]::Round(($now - $_.LastWriteTimeUtc).TotalMinutes, 1)
            $hbId = $_.Name -replace '^\.', '' -replace '-inbound-watch-heartbeat$', ''
            # SWEEP-CLASS-1 residual, LANE-4 review of 1f1e2264..5b3d9d2c: this comparison
            # used a hardcoded 5 while $hbFresh was read from WatchHeartbeatFreshMinutes and
            # never used, so that value had TWO authorities that happened to agree. Editing
            # the psd1 would have changed nothing -- silently, which is the exact disease
            # this card set exists to cure, sitting in the file that argues for the cure.
            $hbState = if ($hbAge -le $hbFresh) { 'OK' } else { 'STALE' }
            $hbBody = ''
            try { $hbBody = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue } catch {}
            $hbSession = [regex]::Match([string]$hbBody, 'session=([0-9a-fA-F-]{36})').Groups[1].Value
            $hbSeatKey = if ($seatKeys.ContainsKey($hbId)) { $seatKeys[$hbId] } else { $hbId }
            $hbRegistered = if ($registryState -eq 'VALID') { [string]$registry.seats.$hbSeatKey.session } else { $null }
            # Three-state identity (claude SEQ 334 / fable SEQ 977): UNKNOWN and UNATTRIBUTED are
            # distinct from OK - a green token that also means "could not tell" is worth less than
            # no token. UNKNOWN first: an unresolvable lane id makes the comparison meaningless.
            if (-not $hbRegistered)            { $hbState = 'UNKNOWN' }
            elseif (-not $hbSession)           { $hbState = 'UNATTRIBUTED' }
            elseif ($hbSession -ne $hbRegistered) { $hbState = 'FOREIGN' }
            # SWEEP-CLASS-1 (d): watchhb and lease age are read as a PAIR, not as two
            # independent tokens a human has to correlate by eye. A STALE watch beside a
            # DARK lease is a coherent DEAD-SEAT signature; a STALE watch beside a LIVE
            # lease is a watch incident on a living seat. Those demand opposite responses,
            # and reading either token alone cannot tell them apart - which is exactly how
            # a dead watch stayed indistinguishable from "nothing happened".
            $pairSuffix = ''
            if ($leaseAges.ContainsKey($hbId)) {
                $pairLease = $leaseAges[$hbId]
                if ($hbState -in @('STALE','UNKNOWN','UNATTRIBUTED','FOREIGN')) {
                    if ($pairLease.state -eq 'DARK') { $pairSuffix = ',pair=DEAD-SEAT' }
                    elseif ($pairLease.state -eq 'LIVE') { $pairSuffix = ',pair=WATCH-ONLY' }
                    else { $pairSuffix = (',pair={0}' -f $pairLease.state) }
                } elseif ($pairLease.state -eq 'DARK') {
                    # Fresh watch, dark lease: the seat is armed but not renewing. That is
                    # a seat that must TAKE A TURN, not a watch that must be re-armed.
                    $pairSuffix = ',pair=UNRENEWED'
                }
            }
            $watchHb += ('watchhb:{0}={1}({2}m{3})' -f $hbId, $hbState, $hbAge, $pairSuffix)
        }
} catch {}
# Codex delivery-watcher pair-link check (claude SEQ 342 / fable SEQ 993): the delivery watcher
# is codex's sole automated wake path since the poll retirement (01:02:45Z), and a process CENSUS
# cannot tell "no watcher" from "a watcher I failed to see" (fable SEQ 983's ~48h false-OK).
# This checks the exact proven failure shape: engine alive + wrapper parent DEAD = ORPHANED
# (wakes go nowhere while the lock blocks replacements). Fail-closed: errors emit UNKNOWN.
# Multi-instance: evaluate ALL matching engines and report the WORST state (claude SEQ 343 /
# fable SEQ 994) - a restart that leaves an orphan beside a healthy pair must read ORPHANED,
# because the orphan still holds .codex-marker.lock and wakes still go nowhere.
try {
    $cdEngines = @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match 'heartbeat-check\.ps1' -and $_.CommandLine -match '\.codex-marker' })
    if ($cdEngines.Count -eq 0) { $watchHb += 'watchhb:codex-delivery=DEAD' }
    else {
        $cdOrphans = 0
        foreach ($cdEngine in $cdEngines) {
            $cdWrap = Get-CimInstance Win32_Process -Filter "ProcessId=$($cdEngine.ParentProcessId)" -ErrorAction SilentlyContinue
            if (-not ($cdWrap -and $cdWrap.CommandLine -match 'codex-delivery-watcher\.ps1')) { $cdOrphans++ }
        }
        $cdState = if ($cdOrphans -gt 0) { 'ORPHANED' } else { 'OK' }
        $cdSuffix = if ($cdEngines.Count -gt 1) { "($($cdEngines.Count)inst,$cdOrphans orph)" } else { '' }
        $watchHb += ('watchhb:codex-delivery={0}{1}' -f $cdState, $cdSuffix)
    }
} catch { $watchHb += 'watchhb:codex-delivery=UNKNOWN' }
# Internal nudge consumer (Layi directive 2026-07-29 / fable SEQ 997): rotation nudgers write
# health/.<lane>-nudge instead of cross-session UI messages (retired after a cross-project
# misdelivery). An unanswered nudge is surfaced here until the lane deletes the file.
try {
    Get-ChildItem -LiteralPath $HealthDir -Force -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\.([a-z0-9-]+)-nudge$' } | ForEach-Object {
            $ndAge = [math]::Round(($now - $_.LastWriteTimeUtc).TotalMinutes, 1)
            $ndId = $_.Name -replace '^\.', '' -replace '-nudge$', ''
            $watchHb += ('nudge:{0}=PENDING({1}m)' -f $ndId, $ndAge)
        }
} catch {}
# Dispatch-intent reconciliation (DISPATCH-ATOMIC-1). A ledger sentence cannot wake a lane;
# queue.json can. The canonical submitter therefore persists a structured intent BEFORE it
# writes the queue, and every sweep retries the second half. This closes the crash window that
# left FACTORY-MATURITY-1 and PROVIDER-SHADOW-FAILOVER-2 "dispatched" only in Fable prose.
#
# The reconciler validates the cited Fable SEQ and artifact hash, refuses conflicting cards,
# and composes write-verified-json.ps1 for the actual queue write. Its stdout is captured so the
# health surface remains one line. ERROR/DRIFT/PROSE_ONLY are ATTENTION, never silently green.
$dispatchIntentAttention = $false
try {
    $dispatchTool = Join-Path $PSScriptRoot 'dispatch-queue.py'
    $dispatchStateExists = (Test-Path -LiteralPath (Join-Path $dualLane 'dispatch-intents.json')) -or
        (Test-Path -LiteralPath (Join-Path $dualLane 'dispatch-intents'))
    if (-not (Test-Path -LiteralPath $dispatchTool)) {
        if ($dispatchStateExists) {
            $watchHb += 'dispatch-intents=UNAVAILABLE'
            $dispatchIntentAttention = $true
        }
    } else {
        # Prefer the interpreter named `python` only after proving it is Python 3; this host
        # still carries C:\Python27\python.exe. Use the Windows launcher as the fallback.
        # The hosted workflow already demonstrated why installing with one interpreter and
        # executing with an assumed `py -3` is not a valid availability check.
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        $pythonIsThree = $false
        if ($null -ne $python) {
            $pythonMajor = @(& $python.Source -c 'import sys; print(sys.version_info[0])' 2>$null)
            $pythonIsThree = ($LASTEXITCODE -eq 0 -and $pythonMajor.Count -eq 1 -and $pythonMajor[0] -eq '3')
        }
        $pyLauncher = if (-not $pythonIsThree) { Get-Command py.exe -ErrorAction SilentlyContinue } else { $null }
        if (-not $pythonIsThree -and $null -eq $pyLauncher) {
            throw 'no Python interpreter is available for dispatch reconciliation'
        }
        if ($pythonIsThree) {
            $dispatchRaw = @(& $python.Source $dispatchTool reconcile `
                --dual-lane-dir $dualLane `
                --writer (Join-Path $PSScriptRoot 'write-verified-json.ps1') `
                --apply --json 2>&1)
        } else {
            $dispatchRaw = @(& $pyLauncher.Source -3 $dispatchTool reconcile `
                --dual-lane-dir $dualLane `
                --writer (Join-Path $PSScriptRoot 'write-verified-json.ps1') `
                --apply --json 2>&1)
        }
        $dispatchExit = $LASTEXITCODE
        $dispatchReport = $null
        if ($dispatchRaw.Count -gt 0) {
            try { $dispatchReport = $dispatchRaw[-1] | ConvertFrom-Json -ErrorAction Stop } catch {}
        }
        if ($null -eq $dispatchReport) {
            $watchHb += ('dispatch-intents=ERROR(exit={0})' -f $dispatchExit)
            $dispatchIntentAttention = $true
        } else {
            $intentState = [string]$dispatchReport.status
            $intentCount = [int]$dispatchReport.intentCount
            $healedCount = @($dispatchReport.healed).Count
            $proseCount = @($dispatchReport.proseOnly).Count
            if ($intentState -eq 'HEALED') {
                $watchHb += ('dispatch-intents=HEALED({0}/{1})' -f $healedCount, $intentCount)
            } elseif ($intentState -eq 'OK') {
                $watchHb += ('dispatch-intents=OK({0})' -f $intentCount)
            } elseif ($intentState -eq 'PROSE_ONLY') {
                $watchHb += ('dispatch-intents=PROSE-ONLY({0})' -f $proseCount)
                $dispatchIntentAttention = $true
            } else {
                $watchHb += ('dispatch-intents={0}' -f $intentState)
                $dispatchIntentAttention = $true
            }
            if ($dispatchExit -ne 0) { $dispatchIntentAttention = $true }
        }
    }
} catch {
    $watchHb += 'dispatch-intents=ERROR(exception)'
    $dispatchIntentAttention = $true
}
# Queue-vs-dispatch check (claude SEQ 345/346 / fable SEQ 997/998): "ALIVE BUT NOT DISPATCHING
# WHILE WORK IS QUEUED" was folded into the benign state for ~12.5h on 2026-07-29. The queue is
# now MACHINE-READABLE (../queue.json, hub-maintained, single authority - the prose queue in
# RESUME could not be counted by any instrument). Items with state "queued" are undispatched;
# any such item while fable.md has not advanced in >120m emits dispatch:fable=STALLED. A dead
# hub leaves the last state standing, so the alarm fails toward firing.
try {
    $qPath = Join-Path $dualLane 'queue.json'
    if (Test-Path -LiteralPath $qPath) {
        $qRaw = Get-Content -LiteralPath $qPath -Raw -ErrorAction SilentlyContinue
        $qOpen = ([regex]::Matches([string]$qRaw, '"state"\s*:\s*"queued')).Count
        $fableAge = $journalAges['fable']
        if (($qOpen -gt 0) -and ($null -ne $fableAge) -and ($fableAge -gt 120)) {
            $watchHb += ('dispatch:fable=STALLED(open={0},penAge={1}m)' -f $qOpen, $fableAge)
        }
    }
} catch {}
# SWEEP-CLASS-1 (c): CHECKPOINT-IDENTITY-1 detector. Each lane refreshes its own
# `<lane>-resume-CURRENT.md` in the same tool-call block as its lease renewal, so a fresh
# lease beside a checkpoint naming a DIFFERENT session means the checkpoint belongs to a
# predecessor - a successor would resume from another seat's state believing it was its
# own. The failure is silent by construction: both files exist and both look current.
# Emits OK / DRIFT / UNATTRIBUTED / MISSING; fail-closed, never a liveness verdict.
try {
    foreach ($ckLane in @('fable', 'codex')) {
        $ckPath = Join-Path $dualLane ("{0}-resume-CURRENT.md" -f $ckLane)
        if (-not (Test-Path -LiteralPath $ckPath)) {
            $watchHb += ('ckid:{0}=MISSING' -f $ckLane)
            continue
        }
        $ckAge = [math]::Round(($now - (Get-Item -LiteralPath $ckPath).LastWriteTimeUtc).TotalMinutes, 0)
        $ckSeatKey = if ($seatKeys.ContainsKey($ckLane)) { $seatKeys[$ckLane] } else { $ckLane }
        $ckExpected = if ($registryState -eq 'VALID') { [string]$registry.seats.$ckSeatKey.session } else { '' }
        $ckBody = ''
        try { $ckBody = Get-Content -LiteralPath $ckPath -Raw -ErrorAction SilentlyContinue } catch {}
        # CORRECTED after a live FALSE POSITIVE on its first day (my defect). The first
        # version took the FIRST GUID anywhere in the document and compared it to the seat.
        # These checkpoints are prose and legitimately name OTHER identifiers first --
        # codex's names a Codex TASK id (`019fe03e-...`) at line 306 and its actual session
        # credential (`3dd9331b-...`) at 307 -- so "first GUID wins" reported
        # ckid:codex=DRIFT while the checkpoint was correctly attributed.
        #
        # The test is now PRESENCE of the registered seat id anywhere in the checkpoint.
        # That is deliberately the weaker claim, and it is the honest one: it cannot
        # false-positive on prose that discusses other ids, which is the actual shape of
        # these files. DRIFT now means the registered seat is NOWHERE in the checkpoint
        # while some other identity is -- which is the condition worth waking someone for.
        $ckGuids = [regex]::Matches([string]$ckBody, '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
        $ckMentionsSeat = $false
        if ($ckExpected) {
            foreach ($g in $ckGuids) { if ($g.Value -ieq $ckExpected) { $ckMentionsSeat = $true; break } }
        }
        if (-not $ckExpected) { $ckState = 'UNKNOWN' }
        elseif ($ckGuids.Count -eq 0) { $ckState = 'UNATTRIBUTED' }
        elseif ($ckMentionsSeat) { $ckState = 'OK' }
        else { $ckState = 'DRIFT' }
        $watchHb += ('ckid:{0}={1}({2}m)' -f $ckLane, $ckState, $ckAge)
    }
} catch {}
if ($laneGaps.Count -gt 0) { $watchHb += $laneGaps }
if ($dispatchIntentAttention -and $overall -eq 'HEALTHY') { $overall = 'ATTENTION' }
$hbSuffix = if ($watchHb.Count -gt 0) { ' ' + ($watchHb -join ' ') } else { '' }
$line = "$($now.ToString('yyyy-MM-ddTHH:mm:ssZ')) OVERALL=$overall registry=$registryState watchers=$watchers " + ($parts -join ' ') + $hbSuffix
Add-Content -Path (Join-Path $HealthDir 'health.log') -Value $line -Encoding utf8
if (-not $Quiet) { Write-Output $line }
exit 0
