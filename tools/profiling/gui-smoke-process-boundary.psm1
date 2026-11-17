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
        [int64]$CeilingMs = 0
    )

    $window = if ($OverrideMs -gt 0) { $OverrideMs } else { $LegBudgetMs + $MarginMs }
    if ($CeilingMs -gt 0 -and $window -gt $CeilingMs) { $window = $CeilingMs }
    return [int64]$window
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

# The directory read and the file opens, run on a worker runspace so a stalled share cannot hold the caller (adversary 797947a7 A4).
$script:GuiSmokeLogLengthProbe = @'
param($LogRoot)
$total = [int64]0
$seen = $false
foreach ($file in @(Get-ChildItem -LiteralPath $LogRoot -Filter 'mlvapp-*.log' -File -ErrorAction SilentlyContinue)) {
    try {
        $stream = [System.IO.File]::Open($file.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        try { $total += [int64]$stream.Length; $seen = $true } finally { $stream.Dispose() }
    }
    catch { }
}
if ($seen) { $total }
'@

# A watch over the app's own log files (mlvapp-*.log in the run's log directory). It is armed by the FIRST sign of a log, so the time
# before the app writes anything (loading the input, Qt start-up) stays bounded by the overall budget only. Progress is GROWTH of the
# summed file length, read through an opened handle (a directory listing can report a stale size for a file the app still holds).
# A log that vanishes, is held unreadable, or makes the total shrink is not progress. A probe that has not answered within -ProbeWaitMs is
# left running and counts as no new information; the caller never waits on it again. Call Stop-GuiSmokeLogProgressWatch when done.
function New-GuiSmokeLogProgressWatch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$LogRoot,
        [Parameter(Mandatory)]
        [ValidateRange(1, 3600000)]
        [int]$WindowMs,
        [string]$ProbeScript = $script:GuiSmokeLogLengthProbe,
        [ValidateRange(50, 5000)]
        [int]$ProbeWaitMs = 1000
    )

    $runspace = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace()
    $runspace.Open()
    [pscustomobject]@{
        LogRoot = $LogRoot
        WindowMs = $WindowMs
        ProbeScript = $ProbeScript
        ProbeWaitMs = $ProbeWaitMs
        Runspace = $runspace
        Pending = $null
        Handle = $null
        Armed = $false
        LastLength = [int64]-1
        Quiet = [System.Diagnostics.Stopwatch]::StartNew()
    }
}

function Stop-GuiSmokeLogProgressWatch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Watch
    )

    try { if ($null -ne $Watch.Pending) { [void]$Watch.Pending.BeginStop($null, $null) } } catch { }
    try { [void]$Watch.Runspace.CloseAsync() } catch { }
}

function Test-GuiSmokeLogProgressStalled {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Watch
    )

    if ($null -eq $Watch.Pending) {
        $shell = [System.Management.Automation.PowerShell]::Create()
        $shell.Runspace = $Watch.Runspace
        [void]$shell.AddScript($Watch.ProbeScript).AddArgument($Watch.LogRoot)
        $Watch.Pending = $shell
        $Watch.Handle = $shell.BeginInvoke()
    }
    $length = $null
    if ($Watch.Handle.AsyncWaitHandle.WaitOne($Watch.ProbeWaitMs)) {
        try {
            $answer = @($Watch.Pending.EndInvoke($Watch.Handle))
            if ($answer.Count -gt 0 -and $null -ne $answer[0]) { $length = [int64]$answer[0] }
        }
        catch { }
        try { $Watch.Pending.Dispose() } catch { }
        $Watch.Pending = $null
        $Watch.Handle = $null
    }
    if ($null -ne $length) {
        if (-not $Watch.Armed) {
            $Watch.Armed = $true
            $Watch.LastLength = $length
            $Watch.Quiet.Restart()
            return $false
        }
        if ($length -gt $Watch.LastLength) {
            $Watch.LastLength = $length
            $Watch.Quiet.Restart()
            return $false
        }
        # shrinkage (a log removed) is never progress; the baseline follows it so a later real growth still counts
        $Watch.LastLength = $length
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
        $terminationConfirmed = $Process.WaitForExit($waitMs)
        if ($terminationConfirmed) { $exitObservedTimestamp = [System.Diagnostics.Stopwatch]::GetTimestamp(); $exitObservedAtUtc = [datetime]::UtcNow }
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
            $failures.Add("MLVApp made no log progress for $NoProgressWindowMs ms (process_no_progress) and was terminated; the whole process budget was $reportedTimeoutMs ms.")
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

Export-ModuleMember -Function Wait-GuiSmokeProcessBounded, Get-GuiSmokeNoProgressWindowMs, Get-GuiSmokeNoProgressChunkMs, New-GuiSmokeLogProgressWatch, Stop-GuiSmokeLogProgressWatch, Test-GuiSmokeLogProgressStalled
