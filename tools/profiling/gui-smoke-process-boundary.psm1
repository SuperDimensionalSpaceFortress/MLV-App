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
        [int]$StreamDrainMs = 15000
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

    try {
        $terminationConfirmed = $Process.WaitForExit($TimeoutMs)
    }
    catch {
        # round 7 (sol MINOR, same class as the two sanitizations already below): record only the
        # exception's TYPE, never its message text.
        $failures.Add("Process wait failed: $($_.Exception.GetType().Name)")
    }

    if (-not $terminationConfirmed) {
        $timedOut = $true
        $failures.Add("MLVApp exceeded the fail-closed process timeout of $TimeoutMs ms.")
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
        timeoutMs = $TimeoutMs
        treeKillAttempted = $treeKillAttempted
        treeKillSucceeded = $treeKillSucceeded
        terminationConfirmed = $terminationConfirmed
        stdoutDrained = [bool]$stdoutResult.completed
        stderrDrained = [bool]$stderrResult.completed
        stdout = [string]$stdoutResult.text
        stderr = [string]$stderrResult.text
        nativeExitCode = $nativeExitCode
        exitCode = $effectiveExitCode
        failures = @($failures)
    }
}

Export-ModuleMember -Function Wait-GuiSmokeProcessBounded
