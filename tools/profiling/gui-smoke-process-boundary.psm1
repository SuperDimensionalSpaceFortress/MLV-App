Set-StrictMode -Version Latest

function Get-GuiSmokeTaskText {
    param(
        [Parameter(Mandatory)]
        [object]$Task,
        [ValidateRange(1, 60000)]
        [int]$TimeoutMs,
        [Parameter(Mandatory)]
        [string]$Label
    )

    try {
        if (-not $Task.Wait($TimeoutMs)) {
            return [pscustomobject]@{
                completed = $false
                text = ""
                failure = "$Label did not drain within $TimeoutMs ms."
            }
        }
        return [pscustomobject]@{
            completed = $true
            text = [string]$Task.GetAwaiter().GetResult()
            failure = $null
        }
    }
    catch {
        # round 6 (sol MINOR, same class as round 5's OnSample sanitization): a faulted
        # stdout/stderr drain task's exception message can embed a path or host identifier (e.g. a
        # file-in-use error naming the exact file path) -- this string flowed unsanitized into
        # result.failures -> validation.failures -> the receipt/summary JSON. Record only the
        # exception's TYPE, never its message text.
        return [pscustomobject]@{
            completed = $false
            text = ""
            failure = "$Label drain failed: $($_.Exception.GetType().Name)"
        }
    }
}

# VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1: how long the app's log may stay silent before the leg is judged stuck. It comes from the
# LEG's own budget (play safety net + settle + screenshot allowances, the runner's $derivedProcessTimeoutMs) plus a margin, never from
# the caller's -ProcessTimeoutMs / the job timeout, which for an owner-clip leg also holds the input re-read and can be 3600 s.
# -OverrideMs replaces the derived value; -CeilingMs (the whole process budget) caps either, so the watchdog never outlasts the wait.
function Get-GuiSmokeNoProgressWindowMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(0, 3600000)]
        [int64]$LegBudgetMs,
        [ValidateRange(0, 3600000)]
        [int64]$MarginMs = 60000,
        [ValidateRange(0, 3600000)]
        [int64]$OverrideMs = 0,
        [ValidateRange(0, 3600000)]
        [int64]$CeilingMs = 0,
        # r10 (adversary A1): the accepted owner-input read allowance (the part of the hard deadline beyond the default 120 s setup/teardown)
        # also covers a synchronous load that writes no log line, so the silence window carries it too.
        [ValidateRange(0, 3600000)]
        [int64]$ReadAllowanceMs = 0
    )

    $window = if ($OverrideMs -gt 0) { $OverrideMs } else { $LegBudgetMs + $MarginMs + $ReadAllowanceMs }
    if ($CeilingMs -gt 0 -and $window -gt $CeilingMs) { $window = $CeilingMs }
    return [int64]$window
}

# VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1 r6 (sol r5 B1): the HARD deadline of the GUI process, derived from the LEG and not from the caller's job
# timeout. The leg budget (the runner's $derivedProcessTimeoutMs: play safety net + settle + screenshot allowances) plus a stated, bounded
# setup/teardown allowance (-AllowanceMs, 0 = 120000 ms: app start-up, clip open and teardown, which no play window covers), and NEVER above the
# job's -ProcessTimeoutMs (-JobTimeoutMs) nor the 3600000 ms safety ceiling. With no job timeout the runner's own derived leg budget is the deadline
# (floor 1000 ms), exactly as before. It bounds the wait on every branch -- no log yet, a log that keeps growing, and the finalize -- because the
# runner uses it AS its process budget; the silence watchdog only ever ends a leg sooner.
function Get-GuiSmokeLegHardLimitMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(0, 3600000)]
        [int64]$LegBudgetMs,
        [ValidateRange(0, 3600000)]
        [int64]$JobTimeoutMs = 0,
        [ValidateRange(0, 3600000)]
        [int64]$AllowanceMs = 0
    )

    $allowance = if ($AllowanceMs -gt 0) { $AllowanceMs } else { 120000 }
    $limit = if ($JobTimeoutMs -gt 0) { [Math]::Min($JobTimeoutMs, $LegBudgetMs + $allowance) } else { [Math]::Max(1000, $LegBudgetMs) }
    return [int64][Math]::Min(3600000, $limit)
}

# How often the runner's chunk loop looks at the log: a fraction of the window (a window of 1 s must not sit inside a 60 s chunked wait), never
# below 250 ms and never above the 4 s cadence the host-load samples already use. The wait overshoots a stall by at most one chunk + one probe.
function Get-GuiSmokeNoProgressChunkMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 3600000)]
        [int]$WindowMs
    )

    return [int][Math]::Max(250, [Math]::Min(4000, [Math]::Floor($WindowMs / 4)))
}

# A watch over the app's own log files (mlvapp-*.log in the run's log directory). It is armed by the FIRST sign of a log (a log that is
# listed but cannot be opened still arms it), so the time before the app writes anything (loading the input, Qt start-up) stays bounded by
# the overall budget only. Progress is GROWTH of a file beyond its own high-water length (read through an opened handle: a directory listing
# can report a stale size for a file the app still holds) or the first sighting of a new log name. A log that vanishes, is held unreadable,
# shrinks, or returns to a length it already had is never progress, so toggling read access cannot manufacture it (adversary 57a7a5c9 A1).
# A probe that has not answered within its wait is left running and counts as no new information; it is waited on ONCE, never again, and
# never past the process budget the caller has left. Call Stop-GuiSmokeLogProgressWatch when done.
# The probe (the directory read and the file opens) runs as a background ForEach-Object -Parallel job so a stalled share cannot hold the caller
# (adversary 797947a7 A4). Its body is a LITERAL scriptblock below, not a string built at run time: nothing here is an executed string, and the
# module stays free of the runspace-object and script-string shapes the smoke-runner closure census (AttrCudaArtifacts.psm1) flags in every
# closure file -- the pinned dual-venue baseline generator carries that census without any new exclusion (test_dual_venue_evidence byte identity).
# -ProbeDelayMs and -ProbeOpenDelayMs are test seams (data only): the probe sleeps that long before it lists, or before each file open, so a
# probe that hangs on the share (or on one log) can be simulated.
function New-GuiSmokeLogProgressWatch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LogRoot,
        [Parameter(Mandatory)]
        [ValidateRange(1, 3600000)]
        [int]$WindowMs,
        [ValidateRange(0, 3600000)]
        [int]$ProbeDelayMs = 0,
        [ValidateRange(0, 3600000)]
        [int]$ProbeOpenDelayMs = 0,
        [ValidateRange(50, 5000)]
        [int]$ProbeWaitMs = 1000
    )

    [pscustomobject]@{
        LogRoot = $LogRoot
        WindowMs = $WindowMs
        ProbeDelayMs = $ProbeDelayMs
        ProbeOpenDelayMs = $ProbeOpenDelayMs
        ProbeWaitMs = $ProbeWaitMs
        Pending = $null
        Hung = $false
        Armed = $false
        Files = @{}
        Quiet = [System.Diagnostics.Stopwatch]::StartNew()
    }
}

function Stop-GuiSmokeLogProgressWatch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Watch
    )

    # a probe still hung on a stalled share is asked to stop and abandoned, never waited on
    try { if ($null -ne $Watch.Pending) { $Watch.Pending.StopJobAsync() } } catch { }
}

function Test-GuiSmokeLogProgressStalled {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Watch,
        # what is left of the caller's whole process budget: the probe is never waited on longer than this (adversary 57a7a5c9 A2)
        [ValidateRange(0, 3600000)]
        [int]$BudgetRemainingMs = 3600000
    )

    # r4 (adversary b0a33ba9 A2): the budget is an ABSOLUTE endpoint, the entry stopwatch plus -BudgetRemainingMs, so a scheduling delay between
    # here and the wait can never grant a wait past the deadline.
    $entryClock = [System.Diagnostics.Stopwatch]::StartNew()
    # no budget left: no probe is started (a pending one is only looked at, 0 ms)
    if ($null -eq $Watch.Pending -and $BudgetRemainingMs -gt 0) {
        $probeRoot = [string]$Watch.LogRoot
        $probeDelayMs = [int]$Watch.ProbeDelayMs
        $probeOpenDelayMs = [int]$Watch.ProbeOpenDelayMs
        $Watch.Pending = 1 | ForEach-Object -ThrottleLimit 1 -AsJob -Parallel {
            if ($using:probeDelayMs -gt 0) { Start-Sleep -Milliseconds $using:probeDelayMs }
            # every listed log is announced (length -2) AS THE LISTING YIELDS IT and BEFORE any open, so a log whose open then blocks, or a listing
            # that blocks after its first entry, still arms the watch (adversary 1b823896 A1)
            $listed = @()
            Get-ChildItem -LiteralPath $using:probeRoot -Filter 'mlvapp-*.log' -File -ErrorAction SilentlyContinue | ForEach-Object { '{0}|-2' -f $_.Name; $listed += $_ }
            foreach ($file in $listed) {
                if ($using:probeOpenDelayMs -gt 0) { Start-Sleep -Milliseconds $using:probeOpenDelayMs }
                $length = [int64]-1
                try {
                    $stream = [System.IO.File]::Open($file.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                        ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
                    try { $length = [int64]$stream.Length } finally { $stream.Dispose() }
                }
                catch { }
                '{0}|{1}' -f $file.Name, $length
            }
        }
        $Watch.Hung = $false
    }
    $entries = $null
    if ($null -ne $Watch.Pending) {
        # re-derived from the absolute end just before the wait; a probe that already outran its wait is only looked at (0 ms), never waited on again
        $leftMs = [int][Math]::Max([int64]0, [int64]$BudgetRemainingMs - [int64]$entryClock.ElapsedMilliseconds)
        $waitMs = if ($Watch.Hung) { 0 } else { [Math]::Min([int]$Watch.ProbeWaitMs, $leftMs) }
        $finished = $Watch.Pending.Finished.WaitOne($waitMs)
        # r5 (adversary 9fdf9e1a A1, A2): what the probe has emitted SO FAR is taken at every poll, finished or not, so a log whose open blocks
        # cannot hide the entries before it: a listed log arms the watch at once and a growth is credited at the poll that sees it, not when the
        # whole batch ends.
        $landed = @()
        try { $landed = @(Receive-Job -Job $Watch.Pending -ErrorAction SilentlyContinue) } catch { $landed = @() }
        if ($finished) {
            try { Remove-Job -Job $Watch.Pending -Force -ErrorAction SilentlyContinue } catch { }
            $Watch.Pending = $null
            $Watch.Hung = $false
        }
        else {
            $Watch.Hung = $true
        }
        # a result that lands once the budget is spent is ignored: the caller's loop is ending on the whole budget, not on this log
        if ([int64]$entryClock.ElapsedMilliseconds -lt [int64]$BudgetRemainingMs) { $entries = $landed }
    }
    if ($null -ne $entries -and $entries.Count -gt 0) {
        $progress = $false
        foreach ($entry in $entries) {
            $text = [string]$entry
            $cut = $text.LastIndexOf('|')
            if ($cut -lt 0) { continue }
            $name = $text.Substring(0, $cut)
            $parsed = [int64]0
            $length = if ([int64]::TryParse($text.Substring($cut + 1), [ref]$parsed)) { $parsed } else { [int64]-1 }
            if ($length -eq -2) {
                # listed, not yet read (the probe announces every listed log before it opens any); a name already known is left as it is. A NEW
                # name is a sign of life AT ITS ANNOUNCEMENT, and its first read is only a baseline (-2), in either state of the watch: before the
                # watch is armed the arming below IS the sign of life (adversary 1b823896 A2); once it is armed the fresh name is progress here
                # (adversary 2d33c39f A1: crediting the READ instead gave an announced-but-unread log no life, and an unchanged delayed read a
                # whole second window).
                if (-not $Watch.Files.ContainsKey($name)) {
                    $Watch.Files[$name] = [int64]-2
                    if ($Watch.Armed) { $progress = $true }
                }
            }
            elseif (-not $Watch.Files.ContainsKey($name)) {
                # the first sighting of a log arms the watch (below). r4 (adversary b0a33ba9 A1): a log unreadable at that first look is UNKNOWN
                # (-1), never length 0; a log readable at first sight is a sign of life.
                $Watch.Files[$name] = [Math]::Max([int64]-1, $length)
                if ($length -ge 0) { $progress = $true }
            }
            elseif ([int64]$Watch.Files[$name] -eq -2) {
                # the first read of an announced log: unreadable is UNKNOWN, readable is the BASELINE and is not growth (unchanged data must not
                # restart the quiet clock the announcement started or restarted)
                $Watch.Files[$name] = [Math]::Max([int64]-1, $length)
            }
            elseif ([int64]$Watch.Files[$name] -lt 0) {
                # unknown until the first readable length, which becomes the baseline and is NOT growth
                if ($length -ge 0) { $Watch.Files[$name] = $length }
            }
            elseif ($length -gt [int64]$Watch.Files[$name]) {
                $Watch.Files[$name] = $length
                $progress = $true
            }
            # unreadable (-1), shorter, or an already-seen length: not progress, and a known high-water mark is never lowered
        }
        if (-not $Watch.Armed) {
            $Watch.Armed = $true
            $Watch.Quiet.Restart()
            return $false
        }
        if ($progress) {
            $Watch.Quiet.Restart()
            return $false
        }
    }
    if (-not $Watch.Armed) { return $false }
    return ($Watch.Quiet.ElapsedMilliseconds -ge $Watch.WindowMs)
}

function Wait-GuiSmokeProcessBounded {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)]
        [object]$StandardOutputTask,
        [Parameter(Mandatory)]
        [object]$StandardErrorTask,
        [ValidateRange(1, 3600000)]
        [int]$TimeoutMs,
        [ValidateRange(1, 60000)]
        [int]$TerminationGraceMs = 15000,
        [ValidateRange(1, 60000)]
        [int]$StreamDrainMs = 15000,
        # VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1: -TimeoutMs is only what is LEFT of the budget after the caller's own chunked waits (it can be the
        # 1 ms floor). The receipt and the failure text report the WHOLE budget the caller started with; 0 = the same as -TimeoutMs.
        [ValidateRange(0, 3600000)]
        [int]$ReportedTimeoutMs = 0,
        # The caller's chunk loop saw the app log stop advancing for -NoProgressWindowMs: end the wait at once (a 1 ms look for an exit that
        # raced the verdict, then the tree kill below) with the typed outcome process_no_progress.
        [switch]$NoProgressStalled,
        # r10 (adversary 412ce3e0 A1): the caller's chunked wait ran the whole leg-derived budget out with the process still running. An exit that
        # lands inside this call's 1 ms floor look is not a completion: the deadline was already missed, so the leg is a timeout, no look at all.
        [switch]$DeadlineReached,
        [ValidateRange(0, 3600000)]
        [int]$NoProgressWindowMs = 0
    )

    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 3-6: this used to chop the wait into chunks and
    # invoke an -OnSample callback between them, so host-load telemetry could be sampled DURING the
    # leg. round 5/6 kept finding new ways a slow or non-returning callback (running synchronously,
    # in this module's own scope, with no cooperative-cancellation point) could compound past
    # -TimeoutMs or block this function from ever returning at all.
    #
    # round 7 (sol + astra MAJOR -- "the new budget does not bound collection as a whole, and a
    # non-returning sampler prevents both timeout handling and receipt publication"): item 1's
    # replacement of per-process CPU accounting with a single GetSystemTimes syscall plus one
    # process-handle read means a host-load sample no longer needs an unbounded-scriptblock
    # indirection to be taken safely DURING the wait -- run-release-gui-smoke.ps1's own sampling
    # loop now calls Wait-GuiSmokeProcessBounded only for the FINAL, already-chunked-down remainder
    # of the timeout budget, sampling inline in its own scope between chunks. This function goes
    # back to being exactly what its round-3 default branch always was: ONE bounded wait, then the
    # stream-drain/kill-tree finalize below -- no callback, no per-callback overrun bookkeeping, and
    # therefore no way for a caller-supplied scriptblock to prevent this function from returning.
    $failures = [System.Collections.Generic.List[string]]::new()
    $timedOut = $false
    $treeKillAttempted = $false
    $treeKillSucceeded = $false
    $terminationConfirmed = $false
    $noProgress = $false
    # CORE-PARTITION-PREDECESSOR-CLOCK-STEP-1 r2 (Dot pr-780 S1): the wall-clock moment a WaitForExit call returned true, i.e. when termination was
    # confirmed, stamped BEFORE the stream drain below (which can run up to 2 x $StreamDrainMs past the exit while a child holds an inherited
    # handle). $null when termination was never confirmed. The caller's own end stamp, taken after this function returns, includes that drain.
    # r2 fix 2 (adversary 9a2372b7 A1): a wall-clock reading can be moved by an unsampled clock step, so the same moment is also taken on the
    # monotonic clock (exitObservedTimestamp, Stopwatch ticks); a consumer measures the drain as a monotonic duration from it, never from the wall.
    $exitObservedAtUtc = $null
    $exitObservedTimestamp = $null

    $reportedTimeoutMs = if ($ReportedTimeoutMs -gt 0) { $ReportedTimeoutMs } else { $TimeoutMs }
    $waitMs = if ($NoProgressStalled) { 1 } else { $TimeoutMs }

    try {
        if (-not $DeadlineReached) {
            $terminationConfirmed = $Process.WaitForExit($waitMs)
            if ($terminationConfirmed) { $exitObservedTimestamp = [System.Diagnostics.Stopwatch]::GetTimestamp(); $exitObservedAtUtc = [datetime]::UtcNow }
        }
    }
    catch {
        # round 7 (sol MINOR, same class as the two sanitizations already below): record only the
        # exception's TYPE, never its message text.
        $failures.Add("Process wait failed: $($_.Exception.GetType().Name)")
    }

    if (-not $terminationConfirmed) {
        $timedOut = $true
        $noProgress = [bool]$NoProgressStalled
        if ($noProgress) {
            # r6 (sol r5 H2, VENUE-SMOKE-KILL-PROSE-UNCONFIRMED-1): this line is written BEFORE the kill below, so it says termination was
            # REQUESTED; "termination was confirmed" is added only once the post-kill wait has seen the exit.
            $failures.Add("MLVApp made no log progress for $NoProgressWindowMs ms (process_no_progress); termination was requested; the whole process budget was $reportedTimeoutMs ms.")
        }
        else {
            $failures.Add("MLVApp exceeded the fail-closed process timeout of $reportedTimeoutMs ms.")
        }
        $treeKillAttempted = $true
        try {
            # Kill(entireProcessTree: true) is required: a child retaining an
            # inherited stdout/stderr handle can otherwise keep evidence
            # collection blocked after the direct process is terminated.
            $Process.Kill($true)
            $treeKillSucceeded = $true
        }
        catch {
            # round 7 (sol MINOR): $_.Exception.Message can embed a path or host identifier and
            # this string reaches the receipt via processBoundary.failures -> the summary JSON.
            # Record only the exception's TYPE, same class as round 5/6's OnSample/task-drain
            # sanitizations.
            $failures.Add("MLVApp process-tree termination failed: $($_.Exception.GetType().Name)")
        }

        try {
            $terminationConfirmed = $Process.WaitForExit($TerminationGraceMs)
            if ($terminationConfirmed) { $exitObservedTimestamp = [System.Diagnostics.Stopwatch]::GetTimestamp(); $exitObservedAtUtc = [datetime]::UtcNow }
        }
        catch {
            $failures.Add("MLVApp post-kill wait failed: $($_.Exception.GetType().Name)")
        }
        if (-not $terminationConfirmed) {
            $failures.Add("MLVApp process tree was not confirmed terminated within $TerminationGraceMs ms.")
        }
        elseif ($noProgress) {
            $failures.Add("MLVApp process_no_progress termination was confirmed.")
        }
    }

    $stdoutResult = Get-GuiSmokeTaskText `
        -Task $StandardOutputTask -TimeoutMs $StreamDrainMs -Label "stdout"
    $stderrResult = Get-GuiSmokeTaskText `
        -Task $StandardErrorTask -TimeoutMs $StreamDrainMs -Label "stderr"
    foreach ($failure in @($stdoutResult.failure, $stderrResult.failure)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$failure)) {
            $failures.Add([string]$failure)
        }
    }

    $nativeExitCode = $null
    if ($terminationConfirmed) {
        try {
            $nativeExitCode = $Process.ExitCode
        }
        catch {
            # round 7 (sol MINOR): same sanitization class as the catches above.
            $failures.Add("MLVApp exit code was unavailable: $($_.Exception.GetType().Name)")
        }
    }
    $effectiveExitCode = if ($timedOut -or -not $terminationConfirmed) {
        124
    }
    elseif ($null -ne $nativeExitCode) {
        [int]$nativeExitCode
    }
    else {
        125
    }

    [pscustomobject]@{
        timedOut = $timedOut
        # the WHOLE budget (VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-1); finalWaitMs is the remainder this call itself waited
        timeoutMs = $reportedTimeoutMs
        finalWaitMs = $TimeoutMs
        outcome = $(if ($noProgress) { 'process_no_progress' } elseif ($timedOut) { 'timeout' } else { 'exited' })
        noProgress = $noProgress
        noProgressWindowMs = $(if ($noProgress) { $NoProgressWindowMs } else { $null })
        treeKillAttempted = $treeKillAttempted
        treeKillSucceeded = $treeKillSucceeded
        terminationConfirmed = $terminationConfirmed
        exitObservedAtUtc = $exitObservedAtUtc
        exitObservedTimestamp = $exitObservedTimestamp
        stdoutDrained = [bool]$stdoutResult.completed
        stderrDrained = [bool]$stderrResult.completed
        stdout = [string]$stdoutResult.text
        stderr = [string]$stderrResult.text
        nativeExitCode = $nativeExitCode
        exitCode = $effectiveExitCode
        failures = @($failures)
    }
}

Export-ModuleMember -Function Wait-GuiSmokeProcessBounded, Get-GuiSmokeLegHardLimitMs, Get-GuiSmokeNoProgressWindowMs, Get-GuiSmokeNoProgressChunkMs, New-GuiSmokeLogProgressWatch, Stop-GuiSmokeLogProgressWatch, Test-GuiSmokeLogProgressStalled
