Set-StrictMode -Version Latest

# VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1: ONE launch-anchored deadline model for the silence watchdog. r1-r16 of -1 built the wait from DURATIONS (a quiet
# stopwatch restarted at arming and at each growth, a remaining budget handed from call to call, a finalize remainder computed before the call), and each
# round fixed one clock instance. Every time below is ms since the runner's launch stamp (Stopwatch.GetTimestamp() read before Process.Start):
#   D = H   the hard deadline (the runner's $effectiveProcessTimeoutMs)
#   M       = ProbeWaitMs + 1000: room for one probe wait and the kill/1 ms look after the last silence check
#   L       = max(min(1000, H), H - M)                                          Get-GuiSmokeWatchdogLimitMs
#   W, WL   the leg window and the load window, both capped at L                Get-GuiSmokeNoProgressWindowMs -CeilingMs L
#   P       the last credited progress (arming, a growth, a new log name), read at the poll that credited it
#   phase   LOADING until a `gui_smoke.` record is first seen in any mlvapp-*.log (MainWindow logs interaction_trace event=gui_smoke.playback_policy
#           once openMlvSet returns, gui_smoke.launch_only for a launch-only leg); LOADED from then on (one-way)
#   E       unarmed: none (D alone); LOADING: max(WL, P + W); LOADED: P + W; a value past L is none too (-1: a window that cannot run out by L proves no
#           silence, so D decides the leg as a timeout)                                     Get-GuiSmokeSilenceExpiryMs
#   phase   the marker is a log RECORD whose message starts `gui_smoke.` or `interaction_trace event=gui_smoke.` ([ts] [level] [0x<thread>] message), never
#           those bytes inside another record (a clip path in the start-up run_metadata command line)
#   stalled iff armed, E >= 0 and now >= E; every chunk wait is min(chunk, D - now, max(1, E - now)) so the check lands at E (Get-GuiSmokeChunkWaitMs).
# INVARIANT: No silence expiration is later than L = D - M, whatever time the watch arms; while loading, E is never below min(L, WL).

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
        [int64]$ReadAllowanceMs = 0,
        # r16 (fable key3 r15 B1): a DERIVED window is capped this far below -CeilingMs, never below min(1000, -CeilingMs); an explicit -OverrideMs is
        # capped at -CeilingMs itself. -2 r1: the runner passes -CeilingMs L (Get-GuiSmokeWatchdogLimitMs) and no -BelowCeilingMs, so both windows and an
        # explicit override share the one cap L.
        [ValidateRange(0, 3600000)]
        [int64]$BelowCeilingMs = 0
    )

    $window = if ($OverrideMs -gt 0) { $OverrideMs } else { $LegBudgetMs + $MarginMs + $ReadAllowanceMs }
    if ($CeilingMs -gt 0) {
        $cap = if ($OverrideMs -gt 0) { $CeilingMs } else { [Math]::Max([Math]::Min([int64]1000, $CeilingMs), $CeilingMs - $BelowCeilingMs) }
        if ($window -gt $cap) { $window = $cap }
    }
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

# -2 r1: L, the latest time since launch any silence expiry may have. M = ProbeWaitMs + 1000 leaves one probe wait and the kill/look after the last check
# inside H. Never below min(1000, H).
function Get-GuiSmokeWatchdogLimitMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 3600000)]
        [int64]$HardMs,
        [ValidateRange(50, 5000)]
        [int64]$ProbeWaitMs = 1000
    )

    $margin = $ProbeWaitMs + 1000
    return [int64][Math]::Max([Math]::Min([int64]1000, $HardMs), $HardMs - $margin)
}

# -2 r1: E, the silence expiry in ms since launch, for an ARMED watch. LOADING: max(WL, P + W) -- the load allowance is anchored at the launch, so a
# late arming cannot push it past L; LOADED: P + W. Either one past L is no expiry at all (-1).
function Get-GuiSmokeSilenceExpiryMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 3600000)]
        [int64]$LimitMs,
        [Parameter(Mandatory)]
        [ValidateRange(1, 3600000)]
        [int64]$WindowMs,
        [ValidateRange(0, 3600000)]
        [int64]$LoadWindowMs = 0,
        [Parameter(Mandatory)]
        [double]$LastProgressMs,
        [switch]$Loaded
    )

    $expiry = $LastProgressMs + $WindowMs
    if (-not $Loaded) { $expiry = [Math]::Max([double]$LoadWindowMs, $expiry) }
    # adversary 81e7f8b2 A2: a window that cannot run out by L proves no silence by L, so there is no silence expiry (-1) and the hard deadline D decides
    # the leg (timeout). Capping it at L instead ended a log still growing at L as process_no_progress with less than one window of silence.
    if ($expiry -gt [double]$LimitMs) { return [double]-1 }
    return [double]$expiry
}

# -2 r1: one chunk wait of the runner's loops, read from the launch stamp at the call: min(-ChunkMs, D - now, max(1, E - now)). 0 = the deadline D has been
# reached (no wait may start). -ExpiryMs < 0 = the watch is not armed or no window can run out by L (no silence verdict: D alone bounds the wait).
function Get-GuiSmokeChunkWaitMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 3600000)]
        [int]$ChunkMs,
        [Parameter(Mandatory)]
        [int64]$LaunchTimestamp,
        [Parameter(Mandatory)]
        [ValidateRange(0, 3600000)]
        [int64]$DeadlineMs,
        [double]$ExpiryMs = -1
    )

    $now = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $LaunchTimestamp
    $left = [double]$DeadlineMs - $now
    if ($left -le 0) { return 0 }
    $wait = [Math]::Min([double]$ChunkMs, [Math]::Ceiling($left))
    if ($ExpiryMs -ge 0) { $wait = [Math]::Min($wait, [Math]::Max(1.0, [Math]::Ceiling($ExpiryMs - $now))) }
    return [int]$wait
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
# -2 r1: the watch carries the launch-anchored model's inputs (LaunchTimestamp, P = LastProgressMs, Phase, W, WL, L) and exposes E as ExpiryMs (-1 while
# unarmed or when no window can run out by L); there is no quiet stopwatch. The probe also reports whether the bytes it read since the previous high-water
# mark hold a `gui_smoke.` record (the phase).
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
        [int]$ProbeWaitMs = 1000,
        # WL: while the app is LOADING (no `gui_smoke.` line yet) no silence expiry is earlier than WL ms after the LAUNCH (an owner-input leg's
        # synchronous load writes no log line and carries its read allowance here). 0 = the same as -WindowMs.
        [ValidateRange(0, 3600000)]
        [int]$LoadWindowMs = 0,
        # L (Get-GuiSmokeWatchdogLimitMs): no expiry is later than this many ms after the launch. 3600000 = no limit given (the safety ceiling).
        [ValidateRange(1, 3600000)]
        [int64]$LimitMs = 3600000,
        # the runner's launch stamp (Stopwatch.GetTimestamp() read before Process.Start); omitted ($null) = this call's own moment (a watch made without a
        # launch). -2 r3: every int64 is a moment on the counter, 0 and negatives included (a stamp taken before the counter's origin is still a stamp).
        [Nullable[int64]]$LaunchTimestamp = $null
    )

    $load = [Math]::Max($WindowMs, $LoadWindowMs)
    $launch = if ($null -ne $LaunchTimestamp) { [int64]$LaunchTimestamp } else { [System.Diagnostics.Stopwatch]::GetTimestamp() }
    [pscustomobject]@{
        LogRoot = $LogRoot
        WindowMs = $WindowMs
        LoadWindowMs = $load
        LimitMs = $LimitMs
        LaunchTimestamp = [int64]$launch
        LastProgressMs = [double]-1
        Phase = 'LOADING'
        ExpiryMs = [double]-1
        ActiveWindowMs = $load
        ProbeDelayMs = $ProbeDelayMs
        ProbeOpenDelayMs = $ProbeOpenDelayMs
        ProbeWaitMs = $ProbeWaitMs
        Pending = $null
        Hung = $false
        Armed = $false
        Files = @{}
    }
}

# -2 r1: E of a watch as it stands (-1 while unarmed), from its own fields through the one helper
function Get-GuiSmokeLogProgressExpiryMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object]$Watch
    )

    if (-not $Watch.Armed) { return [double]-1 }
    return (Get-GuiSmokeSilenceExpiryMs -LimitMs ([int64]$Watch.LimitMs) -WindowMs ([int64]$Watch.WindowMs) -LoadWindowMs ([int64]$Watch.LoadWindowMs) `
            -LastProgressMs ([double]$Watch.LastProgressMs) -Loaded:([string]$Watch.Phase -eq 'LOADED'))
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
        # -2 r1: D, the hard deadline in ms since the watch's launch stamp (0 = none given). The probe is never waited on past it (adversary 57a7a5c9 A2),
        # no probe starts once it is reached, a result that lands at or after it is ignored, and no stall verdict is passed at or after it (the whole
        # budget decides then, adversary d872def7 A2). Every reading is the time since the launch, so no entry stopwatch or remaining budget is carried.
        [ValidateRange(0, 3600000)]
        [int64]$DeadlineMs = 0
    )

    $launch = [int64]$Watch.LaunchTimestamp
    # no budget left: no probe is started (a pending one is only looked at, 0 ms)
    if ($null -eq $Watch.Pending -and ($DeadlineMs -le 0 -or (Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch) -lt $DeadlineMs)) {
        $probeRoot = [string]$Watch.LogRoot
        $probeDelayMs = [int]$Watch.ProbeDelayMs
        $probeOpenDelayMs = [int]$Watch.ProbeOpenDelayMs
        # -2 r1: the previous high-water mark of each known log, copied for this probe; the probe reads the bytes from 257 before it (a marker record -- its
        # [ts] [level] [0x<thread>] prefix is about 50 bytes -- split across two reads is still seen whole, with the newline before it) to the current length, at most the last 1 MiB (a skipped middle only keeps the lenient LOADING bound)
        $probeMarks = @{}
        foreach ($known in @($Watch.Files.Keys)) { $probeMarks[$known] = [int64]$Watch.Files[$known] }
        $Watch.Pending = 1 | ForEach-Object -ThrottleLimit 1 -AsJob -Parallel {
            if ($using:probeDelayMs -gt 0) { Start-Sleep -Milliseconds $using:probeDelayMs }
            # every listed log is announced (length -2) AS THE LISTING YIELDS IT and BEFORE any open, so a log whose open then blocks, or a listing
            # that blocks after its first entry, still arms the watch (adversary 1b823896 A1)
            $marks = $using:probeMarks
            $listed = @()
            Get-ChildItem -LiteralPath $using:probeRoot -Filter 'mlvapp-*.log' -File -ErrorAction SilentlyContinue | ForEach-Object { '{0}|-2' -f $_.Name; $listed += $_ }
            foreach ($file in $listed) {
                if ($using:probeOpenDelayMs -gt 0) { Start-Sleep -Milliseconds $using:probeOpenDelayMs }
                $length = [int64]-1
                $mark = 0
                try {
                    $stream = [System.IO.File]::Open($file.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
                        ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
                    try {
                        $length = [int64]$stream.Length
                        $from = [int64]0
                        $seen = $marks[$file.Name]
                        if ($null -ne $seen -and [int64]$seen -gt 256) { $from = [int64]$seen - 256 }
                        if ($length - $from -gt 1048576) { $from = $length - 1048576 }
                        # one byte before the window too, so a record that starts AT the window still has the newline before it; a window at the
                        # file's start lets the file's start stand for that newline
                        if ($from -gt 0) { $from = $from - 1 }
                        $head = if ($from -eq 0) { '(?:\A|\n)' } else { '\n' }
                        if ($length -gt $from) {
                            $buffer = [byte[]]::new($length - $from)
                            $null = $stream.Seek($from, [System.IO.SeekOrigin]::Begin)
                            $got = 0
                            $more = $true
                            while ($more -and $got -lt $buffer.Length) {
                                $step = $stream.Read($buffer, $got, $buffer.Length - $got)
                                if ($step -le 0) { $more = $false } else { $got += $step }
                            }
                            # the marker is a RECORD whose message starts `gui_smoke.` (LogLineField recordLine: [ts] [level] [0x<thread>] message), never
                            # the bytes anywhere: a clip path in the start-up run_metadata command line can hold them (adversary 81e7f8b2 A1)
                            if ([System.Text.Encoding]::Latin1.GetString($buffer, 0, $got) -cmatch ($head + '\[[^\]\r\n]*\] \[[^\]\r\n]*\] \[0x\w+\] (?:interaction_trace event=)?gui_smoke[.]')) { $mark = 1 }
                        }
                    }
                    finally { $stream.Dispose() }
                }
                catch { }
                '{0}|{1}|{2}' -f $file.Name, $length, $mark
            }
        }
        $Watch.Hung = $false
    }
    $entries = $null
    if ($null -ne $Watch.Pending) {
        # the probe wait is min(ProbeWaitMs, D - now), read from the launch stamp just before the wait; a probe that already outran its wait is only
        # looked at (0 ms), never waited on again
        $leftMs = if ($DeadlineMs -gt 0) { [int][Math]::Max(0, [Math]::Floor([double]$DeadlineMs - (Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch))) } else { [int]$Watch.ProbeWaitMs }
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
        # a result that lands once the deadline is reached is ignored: the caller's loop is ending on the whole budget, not on this log
        if ($DeadlineMs -le 0 -or (Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch) -lt $DeadlineMs) { $entries = $landed }
    }
    # the poll's one reading of the launch clock: P for whatever this poll credits, and the time the verdict compares with E
    $nowMs = Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $launch
    $progress = $false
    $marked = $false
    if ($null -ne $entries -and $entries.Count -gt 0) {
        foreach ($entry in $entries) {
            $text = [string]$entry
            # name|length (an announcement, length -2) or name|length|m (a read; m = 1 when the bytes read hold `gui_smoke.`)
            $found = [regex]::Match($text, '^(?<name>.+?)\|(?<length>-?\d+)(?:\|(?<mark>[01]))?$')
            if (-not $found.Success) { continue }
            $name = $found.Groups['name'].Value
            $parsed = [int64]0
            $length = if ([int64]::TryParse($found.Groups['length'].Value, [ref]$parsed)) { $parsed } else { [int64]-1 }
            if ($found.Groups['mark'].Success -and $found.Groups['mark'].Value -eq '1') { $marked = $true }
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
                # move P, which the announcement already set)
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
            # the first sign of a log arms the watch; P = the arming time
            $Watch.Armed = $true
            $Watch.LastProgressMs = $nowMs
        }
        elseif ($progress) {
            $Watch.LastProgressMs = $nowMs
        }
        # one-way: the first `gui_smoke.` the app logs ends the load (its clip is open); the leg window alone applies from then on
        if ($marked) { $Watch.Phase = 'LOADED' }
    }
    $Watch.ExpiryMs = Get-GuiSmokeLogProgressExpiryMs -Watch $Watch
    # the window in force is the SILENCE that runs out at E, E - P (adversary 0a7510a1 A1): while LOADING with E = WL (the allowance from the launch) it is
    # WL - P, never WL itself, so a stall never claims more silence than the log had. With no expiry it is the phase's own window.
    $Watch.ActiveWindowMs = if ($Watch.Armed -and [double]$Watch.ExpiryMs -ge 0) { [int64][Math]::Round([double]$Watch.ExpiryMs - [double]$Watch.LastProgressMs) }
        elseif ([string]$Watch.Phase -eq 'LOADED') { $Watch.WindowMs } else { $Watch.LoadWindowMs }
    if (-not $Watch.Armed) { return $false }
    # r15 (adversary d872def7 A2): at or past the deadline no stall verdict is passed: the caller's loop is ending on the whole budget, which then decides
    # the leg (timeout, not process_no_progress)
    if ($DeadlineMs -gt 0 -and $nowMs -ge $DeadlineMs) { return $false }
    # E < 0: no window can run out by L (adversary 81e7f8b2 A2), so no silence verdict: D decides
    if ([double]$Watch.ExpiryMs -lt 0) { return $false }
    return ($nowMs -ge [double]$Watch.ExpiryMs)
}

# r10 (adversary 3534e113 A1): did the leg-derived budget run out UNDER a still-running app? Elapsed time alone cannot say: a log probe that stalls
# on a share can spend the rest of the budget after the app already exited cleanly. The process's own lifetime decides. A process still running is
# exhausted; one that exited is exhausted only if it lived longer than the budget; a lifetime that cannot be read for an exited process is NOT
# exhausted (the finalize look then reports the exit it sees, as before).
# r11 (adversary f24a2aed A1): Process.StartTime and ExitTime are LOCAL times, and the difference of two local readings moves by the whole offset change when
# a daylight-saving step falls inside the process's life (+3600000 ms across spring-forward, -3598000 ms for a 2 s life across fall-back). The lifetime is
# therefore the difference of the two instants taken as UTC.
function Get-GuiSmokeProcessLifetimeMs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [datetime]$StartTime,
        [Parameter(Mandatory)]
        [datetime]$ExitTime
    )

    return ($ExitTime.ToUniversalTime() - $StartTime.ToUniversalTime()).TotalMilliseconds
}

# r16 (sol r15 B2, fable VENUE-SMOKE-EXITTIME-UNREADABLE-FAILS-OPEN-1): an exit is judged on ONE clock, the monotonic one the runner's launch stamp
# (Stopwatch.GetTimestamp() read just before Process.Start) is on. No wall-clock reading (Process.StartTime / ExitTime, a UTC launch instant) decides
# anything: even in UTC a wall lifetime moves with a clock step (NTP, a manual set, a VM resume), and r11-r15's mix of the two clocks still turned a real
# 2000 ms life across a -1500 ms step into a clean exit and a real 700 ms life across a +600 ms step into a timeout. Two monotonic facts bracket the exit:
#   -LastAliveMs  : a time since launch at which the app was KNOWN to be running (0 = only its launch is known);
#   -ExitSeenByMs : a time since launch by which it was known to have exited (-1 = not known).
#   seen by a time inside the budget       -> 'in_budget' (it exited inside it: native exit kept);
#   known running at or past the budget    -> 'late'      (it outlived the budget: a timeout);
#   anything else                          -> 'uncertain' (the exit cannot be placed on either side of the budget; never reported as clean or late).
function Get-GuiSmokeExitBudgetVerdict {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [double]$BudgetMs,
        [double]$LastAliveMs = 0,
        [double]$ExitSeenByMs = -1
    )

    if ($ExitSeenByMs -ge 0 -and $ExitSeenByMs -le $BudgetMs) { return 'in_budget' }
    if ($LastAliveMs -ge $BudgetMs) { return 'late' }
    return 'uncertain'
}

# The monotonic time since the runner's launch stamp (Stopwatch.GetTimestamp() read before Process.Start), in ms; -1 when no stamp was given.
# -2 r3 (hosted run 44531859108): "no stamp" is an OMITTED (or $null) stamp, never a value: the old `-le 0` sentinel read a stamp taken a fixed time before
# GetTimestamp() as "no launch" on a runner booted less than that time ago (the counter starts near boot). Every int64, 0 and negatives included, is a moment.
function Get-GuiSmokeSinceLaunchMs {
    [CmdletBinding()]
    param(
        [Nullable[int64]]$LaunchTimestamp = $null
    )

    if ($null -eq $LaunchTimestamp) { return -1.0 }
    return [double]([System.Diagnostics.Stopwatch]::GetTimestamp() - [int64]$LaunchTimestamp) * 1000.0 / [System.Diagnostics.Stopwatch]::Frequency
}

function Test-GuiSmokeWaitBudgetExhausted {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)]
        [ValidateRange(0, 3600000)]
        [int64]$BudgetMs,
        # Stopwatch.GetTimestamp() read before the process was started; omitted ($null) = not known (-2 r3: every int64 is a moment, 0 included)
        [Nullable[int64]]$LaunchTimestamp = $null,
        # r16 (sol r15 B2): the caller's last monotonic sighting of the app running, in ms since launch (0 = only the launch is known)
        [double]$LastAliveMs = 0,
        # -2 r1 (adversary r16 A2): the caller's FIRST sighting of the exit, ms since launch, carried by reference ($null = none carried). When it already
        # holds a value >= 0 that value judges the exit; when it holds a value < 0 and this call sees the exit, the moment read here is stored in it. A value
        # once set is never replaced, so a later look can never date the exit later than it was first seen.
        # (typed [ref] with no default: a [ref] default of $null fails binding, and an [object] parameter unwraps the reference; unbound = nothing carried)
        [ref]$ExitSeenByMs
    )

    # r16: is the app KNOWN to have been running at or past the budget? Still running now (the callers ask only once the budget is spent), or seen running
    # at or past it. An exit the monotonic clock cannot place ('uncertain') is not a deadline hit HERE: the finalize look judges it with the same facts and
    # reports the uncertainty. Nothing here reads a wall clock (r15: Process.StartTime of an exited child throws on Linux; r16: no wall reading decides).
    # r16 (fable VENUE-SMOKE-EXITTIME-UNREADABLE-FAILS-OPEN-1): any throw reads as exhausted, never as a clean exit.
    try {
        if (-not $Process.HasExited) { return $true }
        $carrier = $PSBoundParameters.ContainsKey('ExitSeenByMs') -and $null -ne $ExitSeenByMs
        $carried = if ($carrier -and $null -ne $ExitSeenByMs.Value) { [double]$ExitSeenByMs.Value } else { [double]-1 }
        $seenByMs = if ($carried -ge 0) { $carried } else { Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $LaunchTimestamp }
        if ($carrier -and $carried -lt 0 -and $seenByMs -ge 0) { $ExitSeenByMs.Value = [double]$seenByMs }
        return ((Get-GuiSmokeExitBudgetVerdict -BudgetMs $BudgetMs -LastAliveMs $LastAliveMs -ExitSeenByMs $seenByMs) -eq 'late')
    }
    catch {
        return $true
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
        [int]$NoProgressWindowMs = 0,
        # r11 fix 6 (adversary 34a08338 A1): the whole leg-derived budget (0 = do not judge) and the monotonic launch stamp (0 = not known). Every exit this
        # call confirms in its own look is judged against them, whichever path of the caller's chunked wait led here (a stall verdict, an exit the wait itself
        # saw, the expired deadline): an app that outlived the budget before exiting is a timeout, one that exited inside it keeps its native exit.
        [ValidateRange(0, 3600000)]
        [int64]$LifetimeBudgetMs = 0,
        # -2 r3: omitted ($null) = not known; every int64 is a moment, 0 and negatives included (Get-GuiSmokeSinceLaunchMs)
        [Nullable[int64]]$LaunchTimestamp = $null,
        # r16 (sol r15 B2): the caller's last monotonic sighting of the app running, ms since launch (0 = only the launch is known). With the monotonic
        # moment this call sees the exit, it brackets the exit (Get-GuiSmokeExitBudgetVerdict); no wall clock is read.
        [double]$LastAliveMs = 0,
        # -2 r1 (adversary r16 A2): the caller's first sighting of the exit, ms since launch (-1 = none). When it is >= 0 it judges the exit this call
        # confirms, and the exit is never dated again here (a look after a descheduled caller would date it past the budget).
        [double]$ExitSeenByMs = -1
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
    $overrunRecognized = $false
    $exitTimeUncertain = $false
    # (PowerShell names are case-insensitive: this local is not the -ExitSeenByMs parameter)
    $exitBoundMs = [double]-1
    # CORE-PARTITION-PREDECESSOR-CLOCK-STEP-1 r2 (Dot pr-780 S1): the wall-clock moment a WaitForExit call returned true, i.e. when termination was
    # confirmed, stamped BEFORE the stream drain below (which can run up to 2 x $StreamDrainMs past the exit while a child holds an inherited
    # handle). $null when termination was never confirmed. The caller's own end stamp, taken after this function returns, includes that drain.
    # r2 fix 2 (adversary 9a2372b7 A1): a wall-clock reading can be moved by an unsampled clock step, so the same moment is also taken on the
    # monotonic clock (exitObservedTimestamp, Stopwatch ticks); a consumer measures the drain as a monotonic duration from it, never from the wall.
    $exitObservedAtUtc = $null
    $exitObservedTimestamp = $null

    $reportedTimeoutMs = if ($ReportedTimeoutMs -gt 0) { $ReportedTimeoutMs } else { $TimeoutMs }
    # -2 r1: with a launch stamp and the whole budget the wait is what is left of the budget at THIS moment, max(1, budget - time since launch), read here;
    # -TimeoutMs (a remainder the caller computed earlier) is only the fallback for a caller without them
    $budgetWaitMs = if ($null -ne $LaunchTimestamp -and $LifetimeBudgetMs -gt 0) {
        [int][Math]::Max(1, [Math]::Ceiling([double]$LifetimeBudgetMs - (Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $LaunchTimestamp)))
    }
    else { $TimeoutMs }
    $waitMs = if ($NoProgressStalled) { 1 } else { $budgetWaitMs }

    try {
        if (-not $DeadlineReached) {
            $terminationConfirmed = $Process.WaitForExit($waitMs)
            if ($terminationConfirmed) {
                $exitObservedTimestamp = [System.Diagnostics.Stopwatch]::GetTimestamp(); $exitObservedAtUtc = [datetime]::UtcNow
                if ($LifetimeBudgetMs -gt 0) {
                    # r16 (sol r15 B2): the exit is placed on the monotonic clock only: it happened after -LastAliveMs and no later than the stamp just read.
                    # No launch stamp, or a throw, leaves it 'uncertain' (never clean).
                    $exitVerdict = 'uncertain'
                    try {
                        # -2 r1 (adversary r16 A2): the caller's first sighting of the exit, when it carries one, is the bound; this look never dates it again
                        $exitBoundMs = if ($ExitSeenByMs -ge 0) { $ExitSeenByMs }
                        elseif ($null -ne $LaunchTimestamp) { [double]($exitObservedTimestamp - [int64]$LaunchTimestamp) * 1000.0 / [System.Diagnostics.Stopwatch]::Frequency }
                        else { [double]-1 }
                        $exitVerdict = Get-GuiSmokeExitBudgetVerdict -BudgetMs $LifetimeBudgetMs -LastAliveMs $LastAliveMs -ExitSeenByMs $exitBoundMs
                    }
                    catch { $exitVerdict = 'uncertain' }
                    if ($exitVerdict -eq 'late') {
                        # the exit is real but came after the whole budget was spent: it is not a completion. The timeout path below ends the (already
                        # exited) tree and reports the whole budget, exactly as for an app still running at the deadline.
                        $terminationConfirmed = $false
                        $overrunRecognized = $true
                        $exitObservedTimestamp = $null; $exitObservedAtUtc = $null
                    }
                    elseif ($exitVerdict -eq 'uncertain') {
                        $exitTimeUncertain = $true
                    }
                }
            }
            elseif ($NoProgressStalled -and $LifetimeBudgetMs -gt 0 -and (Get-GuiSmokeSinceLaunchMs -LaunchTimestamp $LaunchTimestamp) -ge $LifetimeBudgetMs) {
                # r15 (adversary d872def7 A2): the stall verdict came at or past the whole budget (a log probe that blocked on a share spent the rest of
                # it) and the 1 ms look still sees the app running: it was alive AT the deadline, so the leg is a timeout, not a stall that ended it
                # early. The monotonic time since launch is the clock the budget was anchored on (r11 fix 6).
                $overrunRecognized = $true
            }
        }
    }
    catch {
        # round 7 (sol MINOR, same class as the two sanitizations already below): record only the
        # exception's TYPE, never its message text.
        $failures.Add("Process wait failed: $($_.Exception.GetType().Name)")
    }

    if (-not $terminationConfirmed) {
        $timedOut = $true
        # r14 (adversary 9af35de9 A2): the caller's stall flag is a verdict about the moment it broke its wait. An overrun this call recognized afterwards
        # (a late exit past the whole budget, or the caller's own reached deadline) makes the leg a timeout: the stall reason would claim the leg ended
        # before its budget, so it is read only while neither overrun fact is true.
        $noProgress = [bool]$NoProgressStalled -and -not $overrunRecognized -and -not [bool]$DeadlineReached
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
    if ($exitTimeUncertain -and -not $timedOut) {
        # r16 (sol r15 B2): reported as what it is: neither a clean exit nor a timeout
        $seenText = if ($exitBoundMs -ge 0) { "first seen $([long][Math]::Ceiling($exitBoundMs)) ms after launch" } else { "seen with no monotonic launch stamp" }
        $failures.Add("MLVApp exited, but its exit was $seenText and it was last known running $([long][Math]::Floor($LastAliveMs)) ms after launch, so the monotonic clock cannot place the exit inside or past the whole process budget of $reportedTimeoutMs ms (exit_time_uncertain); its native exit is not a clean completion.")
    }
    $effectiveExitCode = if ($timedOut -or -not $terminationConfirmed) {
        124
    }
    elseif ($exitTimeUncertain) {
        125
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
        finalWaitMs = $budgetWaitMs
        outcome = $(if ($noProgress) { 'process_no_progress' } elseif ($timedOut) { 'timeout' } elseif ($exitTimeUncertain) { 'exit_time_uncertain' } else { 'exited' })
        exitTimeUncertain = [bool]($exitTimeUncertain -and -not $timedOut)
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

Export-ModuleMember -Function Wait-GuiSmokeProcessBounded, Get-GuiSmokeLegHardLimitMs, Get-GuiSmokeNoProgressWindowMs, Get-GuiSmokeNoProgressChunkMs, Get-GuiSmokeWatchdogLimitMs, Get-GuiSmokeSilenceExpiryMs, Get-GuiSmokeChunkWaitMs, Get-GuiSmokeLogProgressExpiryMs, Test-GuiSmokeWaitBudgetExhausted, Get-GuiSmokeExitBudgetVerdict, Get-GuiSmokeSinceLaunchMs, Get-GuiSmokeProcessLifetimeMs, New-GuiSmokeLogProgressWatch, Stop-GuiSmokeLogProgressWatch, Test-GuiSmokeLogProgressStalled
