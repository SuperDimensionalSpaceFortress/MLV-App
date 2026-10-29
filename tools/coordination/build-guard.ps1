# build-guard.ps1 (BUILD-GUARD-1) -- the tracked, host-global admission gate for LOCAL MinGW builds.
#
# WHY: on 2026-10-12 four MinGW builds overlapped on the hub VM, System Event 2004 fired, and three
# of the four died with cc1plus out-of-memory; every serial retry at -j2/-j3 passed. The board-side
# admission pieces (dispatch-lane HOST-MEMORY-PREFLIGHT, build-admission-lock.ps1) are untracked, so
# a lane worktree cannot reach them. invoke-mlv-exclusive.ps1 locks per checkout, not per host.
# This script is the tracked equivalent that every local build launcher routes through.
#
# WHAT IT DOES, in order:
#   1. Memory pre-flight: free physical >= 6 GB AND free commit >= 8 GB. Polls every 30 s up to a
#      bounded deadline (default 20 min), then prints  BUILD_MEM_REFUSED ...  and exits 3.
#   2. One host-global build slot: a lock file (default C:\mlvtmp\locks\build-slot.lock) created with
#      CreateNew, recording owner pid + start time + expiry (default 90 min). A lock is reclaimed when
#      its owner pid is dead (pid + start time must both match, so a reused pid does not hold it), or
#      when it has expired. A slot still held at the deadline prints  BUILD_SLOT_BUSY ...  and exits 3.
#      The slot is held ONLY around the build command, is released in finally, and must never be held
#      across a venue leg (Bachelor / Ultra-Magnus jobs are not gated by this script).
#   3. Job count: -j4 by default; -j2 when another live cc1plus or mingw32-make process exists on this
#      host. Peers are identified by pid + start time (reported as pid@startUtc).
#   4. -ExemptReason "<text>" (or env MLV_BUILD_GUARD_EXEMPT) skips ONLY the memory floor, for a hub
#      ruling, mirroring HOST_COMMIT_EXEMPT. The reason is written to stderr and appended to the
#      exemption log. The slot and the job count still apply. An empty, whitespace or false-y value
#      ('0', 'false', 'off', 'no') is not a reason and exempts nothing.
#
# MACHINE HEAVY SLOT (MLV-VM-HEAVY-SLOT-ADOPT-1): every build (Command and Library mode; -PrintJobs takes
# no slot and builds nothing, so it is not wrapped) first takes the machine-wide heavy slot shared with the
# other projects on this host, so it is the OUTERMOST lock. Steps 1-3 above then run nested inside it,
# unchanged. The CLI is the owner-installed heavy-slot (C:\ProgramData\HeavySlot\bin, never edited here),
# called through its heavy-slot.cmd shim, never a bare '--' to an in-process '&' (that is the 10-21 exit 77):
#      heavy-slot.cmd run --class shared --label mlv:<lane>:build --eta <n>m --wait 60m --exit-file <fresh>
#                         -- <pwsh> -EncodedCommand <holder>
#   with HEAVY_SLOT_EXPECT_SHA256=<pinned sha> and HEAVY_SLOT_EXPECT_KEEPER=1. The build runs in this
#   process, so the slot is held by a small holder child that heavy-slot starts on admission: it writes a
#   ready file, then waits for this guard to write a stop file (or to die), and exits. <lane> comes from
#   MLV_LANE_RUN_DIR (lane-<CARD>-rN-<stamp> gives <CARD>), else 'hub'.
#   - A tool-produced 75/76/77 counts ONLY when the --exit-file marker matches the run (source heavy-slot,
#     the same code, and the ticket + nonce of its HEAVY-SLOT TICKET stderr line): it prints
#     BUILD_HEAVY_SLOT_REQUEUE and exits 3, like BUILD_SLOT_BUSY. It is never a build failure.
#   - Any other exit before admission (a 75-77 without a matching marker included) prints
#     BUILD_HEAVY_SLOT_COULD_NOT_CHECK and exits with that code (0 becomes 1). It is never a pass.
#   - The ready file names its holder (pid@start ticks) and admits ONLY while that holder still runs, checked
#     when the file is seen and again just before "heavy-slot: admitted" (the cmd wrapper can outlive the
#     release, so it is not the test). A holder that wrote it and has already exited has given the slot back:
#     BUILD_HEAVY_SLOT_REQUEUE reason=holder-exited-after-ready code=<heavy-slot exit code>, exit 3, and the
#     build never runs without the slot on this path. A heavy-slot log that exists but cannot be read at
#     admission gives the slot back and prints BUILD_HEAVY_SLOT_COULD_NOT_CHECK code=1 (its lines are unchecked).
#     The ready file counts only once its closing 'end' line is on disk; a complete one whose holder or owner line
#     does not parse prints BUILD_HEAVY_SLOT_COULD_NOT_CHECK code=1. The slot owner counts only when its start time is
#     the HEAVY_SLOT_HELD_START heavy-slot exported, and the slot is checked once more after the admitted line, right
#     before the action. A start time that cannot be read means gone; a holder is only ever stopped through the handle
#     its identity was checked on. The hung-tool bound (wait + 15 min) is numeric and measured on a monotonic clock.
#   - The shim missing (not at MLV_HEAVY_SLOT_CMD / on PATH / at the install path) or not startable (it cannot
#     be started, or cmd exits 9009 before admission because the shim's own interpreter is not found), or a
#     shim, lock dir or pwsh path holding '%' (cmd would expand it), FAILS OPEN: one "HEAVY_SLOT_WARN" line
#     on stderr, and the build runs without the heavy slot. A slot mutex
#     that cannot be opened is heavy-slot's own fail-open (M6): it prints HEAVY-SLOT WARN and runs the
#     holder unguarded, which this guard relays to stderr.
#   - heavy-slot.ps1 beside the shim whose SHA-256 is not the pin prints BUILD_HEAVY_SLOT_REFUSED
#     reason=pin-mismatch and exits 3 before anything runs. If heavy-slot itself reports PIN MISMATCH by the
#     time it admits (the script changed after the hash), the slot is given back, BUILD_HEAVY_SLOT_REFUSED
#     reason=pin-mismatch-at-run is printed and the guard exits 3 before the build.
#   - Losing the slot DURING the build (heavy-slot ended before the guard wrote its stop file) does not kill
#     or fail the build: the slot is a machine-concurrency throttle, not a correctness lock. The guard prints
#     HEAVY_SLOT_LOST_DURING_ACTION label=<l> code=<heavy-slot exit> utc=<o> on stderr AND stdout, so a
#     receipt can classify the build as unprotected, and the build's own exit code is returned unchanged.
#     Only a loss after the action started is "during": one found by a release before it (a memory or path refusal, a
#     pin mismatch at run, a throw before the action) prints BUILD_HEAVY_SLOT_REQUEUE reason=holder-exited-before-action
#     instead, then that refusal's own typed line and exit code, unchanged (or the throw goes on).
#   - A heavy-slot call inside a build action does not pass through (the holder is a child of this guard,
#     not an ancestor of the action), so it would wait for its own guard: do not nest one there.
#   - heavy-slot's output goes to a log file beside the lock, never a pipe, so it can never block while it
#     holds the slot. Known limit: a guard killed ALONE while still queued (not a tree kill) leaves its
#     heavy-slot waiter queued; once admitted it may sample load (up to its 10 min admit cap) before the
#     holder starts, sees the guard gone and exits. A tree kill (taskkill /T) ends the waiter with it.
#   Lane FULL-SUITE runs take the same slot by hand, no new runner: from the lane worktree,
#      $env:HEAVY_SLOT_EXPECT_SHA256 = '<pin below>'; $env:HEAVY_SLOT_EXPECT_KEEPER = '1'
#      heavy-slot.cmd run --class shared --label mlv:<lane>:pytest --eta 45m --wait 60m --exit-file <fresh> -- py -3 -m pytest <args>
#   with HEAVY_SLOT_NAME, HEAVY_SLOT_COUNT, HEAVY_SLOT_DIR and HEAVY_SLOT_KEEPER_OWNER unset (the guard removes them from
#   its own heavy-slot call: they select other slots or more of them, r5b adversary A1), and judge 75-77 by the same marker rule (matched = REQUEUE, unmatched = COULD-NOT-CHECK); every other
#   code is pytest's own. The hub's fast-suites-for-diff.ps1 -Run wraps each selector the same way. Never
#   wrap an Invoke-Lane lane, a venue leg or a scheduled beat.
#
# THREE WAYS TO USE IT:
#   Command:   pwsh -NoProfile -File tools\coordination\build-guard.ps1 -Command "qmake X.pro; mingw32-make -j{JOBS}"
#              "{JOBS}" in the text is replaced with the chosen number BEFORE the fragment is parsed, so
#              "-j{JOBS}" reaches the child as the single token "-j4". The fragment runs in this process
#              (PowerShell 7.4+) and its verdict is its FIRST failure: a native command that exits non-zero
#              stops the fragment and its code is returned, so "qmake; make" cannot report a failed qmake
#              as 0. A command that cannot start, a throw or any error record (even one written with
#              -ErrorAction Continue or SilentlyContinue, or caught; only -ErrorAction Ignore is exempt)
#              prints BUILD_GUARD_COMMAND_FAILED on stderr and fails: exit 1, or the non-zero native or
#              'exit' code when there is one (that code is kept). An 'exit N' in the fragment does not skip
#              this: it ends the fragment, and N is judged like a native code. A native failure the fragment
#              caught (or ran in a child scope) is remembered: if the fragment then ends with code 0, the
#              guard exits with the FIRST failing native code (reason=native-failure-overwritten). If
#              nothing failed, the exit code is 0. (A "--" separator is not used: PowerShell binds it as a
#              parameter named '' and refuses.)
#   Print:     pwsh -NoProfile -File tools\coordination\build-guard.ps1 -PrintJobs
#              runs the memory pre-flight, prints "-jN" as the only stdout line, takes NO slot.
#   Library:   . tools\coordination\build-guard.ps1 -Library
#              Invoke-BuildGuard -Label x -Action { param($Jobs) ... }  (pre-flight + slot + jobs, then
#              the action, slot released in finally). Used by the PowerShell build launchers.
#              -NoSlot takes neither the heavy slot nor the build slot; it is for callers that build nothing
#              (-PrintJobs uses it) and always writes BUILD_GUARD_NO_SLOT on stderr.
#
# -RequestedJobs N (library/command) caps the choice: jobs = min(chosen, N). A caller can build
# smaller than the guard allows, never larger.
#
# Log lines go to stderr; typed lines (BUILD_MEM_REFUSED, BUILD_SLOT_BUSY) go to stdout, except
# BUILD_GUARD_COMMAND_FAILED, which goes to stderr beside the error it reports. Output can never hold a slot (r6 sol B1): a
# stderr line waits at most 2 s for its reader and then queues behind it (bounded, see Write-BuildGuardStderr), and every
# typed refusal is printed only after the machine heavy slot was given back (or its stop file written). No guard cmdlet that
# runs while a slot is held writes an error record, which the host renders to stderr synchronously, past that bound (r7,
# a34e2098-A1): the memory probe, the process census, the build-slot acquire and its release are Stop-scoped, and each failure
# is caught and logged, or refuses admission by a throw that the outer finally follows with the heavy-slot release.
# Typed lines are bounded the same way (r8, adversary 3e68ca85-A2), since a heavy slot an ancestor holds stays held until the
# guard ends: see Write-BuildGuardTyped and Complete-BuildGuardOutput. A stop file is deleted only once its holder is proven
# gone by pid + start ticks (r8, adversary 3e68ca85-A1): see Test-HeavySlotStopNeeded. Every caller or seam path is resolved
# by Resolve-BuildGuardPath and composed with [IO.Path] (r9, adversary 2ab4b1b6-A1): a -LockPath that names no file-system file
# with a parent directory (a missing drive, another provider, a drive root) or whose directory cannot be made prints
# BUILD_GUARD_PATH_REFUSED and exits 2 with nothing taken (or after the heavy slot was given back). In command mode any other
# throw out of the guard prints BUILD_GUARD_ERROR and exits 1, never an uncaught error the host renders; a Library caller
# still gets the throw.
# Test seams (used by tests/coordination/test_build_guard.py, no real probe needed):
#   -MemoryProbeFile   JSON array of {"freePhysGB":n,"freeCommitGB":n}, one sample per poll, last repeats
#   -ProcessTableFile  JSON array of {"ProcessId":n,"Name":"cc1plus.exe","CreationDate":"<iso>"}
#   -LockPath / -MutexName / -PollSeconds / -DeadlineMinutes / -LockExpiryMinutes / -ExemptLogPath
#   -HeavySlotCmd / env MLV_HEAVY_SLOT_CMD (a stub heavy-slot.cmd) and -HeavySlotSha256 (that stub's sha)
# Exit codes: 3 = refused (memory, slot, heavy-slot REQUEUE or pin mismatch), 2 = usage (BUILD_GUARD_PATH_REFUSED included), 1 = the
# guard itself failed (BUILD_GUARD_ERROR) or the command fragment failed without a non-zero
# code of its own, otherwise the fragment's non-zero code (its failing native command's or its own 'exit'),
# or, when the fragment ended with 0 after a native failure, the first failing native code (0 when nothing
# failed). Continuation policy: HEAVY_SLOT_LOST_DURING_ACTION never changes the exit code (see MACHINE HEAVY SLOT).
[CmdletBinding()]
param(
    [switch]$Library,
    [switch]$PrintJobs,
    [string]$ExemptReason = '',
    [string]$Label = 'build',
    [int]$RequestedJobs = 0,
    [double]$MinFreePhysGB = 6,
    [double]$MinFreeCommitGB = 8,
    [double]$PollSeconds = 30,
    [double]$DeadlineMinutes = 20,
    [double]$LockExpiryMinutes = 90,
    [string]$LockPath = 'C:\mlvtmp\locks\build-slot.lock',
    [string]$MutexName = 'Global\MLV-BuildGuardSlot',
    [string]$ExemptLogPath = 'C:\mlvtmp\locks\build-guard-exempt.log',
    [string]$MemoryProbeFile = '',
    [string]$ProcessTableFile = '',
    [string]$HeavySlotCmd = '',
    [string]$HeavySlotSha256 = '691efb4c2cc1b98cd5a7379e03a2bb5b9eebaa1174a03b11ee7a48741fc06534',
    [int]$HeavySlotEtaMinutes = 20,
    [int]$HeavySlotWaitMinutes = 60,
    [string]$Command = ''
)

$script:BuildGuardProbeIndex = 0
$script:BuildGuardDefaults = @{
    MinFreePhysGB     = 6
    MinFreeCommitGB   = 8
    PollSeconds       = 30
    DeadlineMinutes   = 20
    LockExpiryMinutes = 90
    LockPath          = 'C:\mlvtmp\locks\build-slot.lock'
    MutexName         = 'Global\MLV-BuildGuardSlot'
    ExemptLogPath     = 'C:\mlvtmp\locks\build-guard-exempt.log'
    MemoryProbeFile   = ''
    ProcessTableFile  = ''
    HeavySlotCmd         = ''
    HeavySlotSha256      = '691efb4c2cc1b98cd5a7379e03a2bb5b9eebaa1174a03b11ee7a48741fc06534'
    HeavySlotEtaMinutes  = 20
    HeavySlotWaitMinutes = 60
}
$script:BuildGuardCimTimeoutSec = 30
$script:HeavySlotInstallCmd = 'C:\ProgramData\HeavySlot\bin\heavy-slot.cmd'
# heavy-slot's held records in its default state dir (a test seam: a Library driver may set it after dot-sourcing).
$script:HeavySlotHeldDir = [IO.Path]::Combine([string]$env:ProgramData, 'HeavySlot', 'held')
# The largest --wait / --eta passed to heavy-slot (one week; heavy-slot's own --max-hold is 90 min), and the slack the
# hung-tool bound adds to the wait. The slack is a test seam: a Library driver may set it after dot-sourcing.
$script:HeavySlotMaxMinutes = 10080
$script:HeavySlotGiveUpSlackMinutes = 15

# r6 sol B1 (INV-13): a guard diagnostic can never block the guard. A full, open stderr pipe blocks not only a write: every
# synchronous call on that handle (even the redirect check inside [Console]::OpenStandardError, or the first [Console]::Error)
# waits behind the blocked writer, because an inherited pipe handle shares one file object. So this thread never touches the
# handle. The raw stderr stream is opened on the thread pool, through a .NET delegate, and each line is written with
# Stream.WriteAsync, which runs the write on the thread pool and keeps the lines in order; the guard waits at most
# $script:BuildGuardLogWaitMs per line, for the open and the write together. A stderr nobody drains costs one bounded wait; while a write is still pending, later
# lines queue without waiting, at most $script:BuildGuardLogMaxPending of them (the rest are dropped and counted in
# the channel's Dropped), so admission polling, the hung-tool bound and every release keep running. A drained stderr
# has each line written before the guard goes on, as before. Lines still queued when the guard ends go out in
# Complete-BuildGuardStderr, after every slot is given back (r7b); a line an open or a write failed on is counted as dropped.
# r8 (adversary 3e68ca85-A2): the typed lines go to stdout through the same bounded writer, on a channel of their own
# (Write-BuildGuardTyped). A heavy slot an ancestor holds (it exported HEAVY_SLOT_HELD) is held until this guard ends, so the
# guard never waits past the bound for a typed line there, not even at the end. With no ancestor slot every slot of this guard
# is given back before its typed line, so at the end it waits for that line however long its reader takes, and a late reader
# still gets it (Complete-BuildGuardOutput).
$script:BuildGuardLogWaitMs = 2000
$script:BuildGuardLogMaxPending = 256
# One channel per stream: the opener, the open task, the lines waiting for it, the writes still pending, the lines dropped.
$script:BuildGuardStderr = @{ Opener = 'New-BuildGuardStderrOpen'; Open = $null; Dropped = 0
    Bytes = [System.Collections.Generic.Queue[byte[]]]::new(); Pending = [System.Collections.Generic.Queue[System.Threading.Tasks.Task]]::new() }
$script:BuildGuardStdout = @{ Opener = 'New-BuildGuardStdoutOpen'; Open = $null; Dropped = 0
    Bytes = [System.Collections.Generic.Queue[byte[]]]::new(); Pending = [System.Collections.Generic.Queue[System.Threading.Tasks.Task]]::new() }

function New-BuildGuardStderrOpen {
    # The task that opens the raw stderr stream on the thread pool (a test seam: a test replaces it to open a slow stream).
    $open = [Delegate]::CreateDelegate([Func[System.IO.Stream]], [Console].GetMethod('OpenStandardError', [Type[]]@()))
    return [System.Threading.Tasks.Task]::Run($open)
}

function New-BuildGuardStdoutOpen {
    # The same for the raw stdout stream, which carries the typed lines.
    $open = [Delegate]::CreateDelegate([Func[System.IO.Stream]], [Console].GetMethod('OpenStandardOutput', [Type[]]@()))
    return [System.Threading.Tasks.Task]::Run($open)
}

function Test-BuildGuardAncestorSlot {
    # True when an ancestor holds a heavy slot (heavy-slot exports HEAVY_SLOT_HELD to the command it runs): it keeps that slot
    # until this guard ends, so no guard output may wait on its reader without a bound.
    return [bool]([string]$env:HEAVY_SLOT_HELD).Trim()
}

function Send-BuildGuardStderrQueue($Ch = $script:BuildGuardStderr) {
    # Once the stream is open, every queued line goes out (WriteAsync, in order); returns the last write task, or $null.
    $open = $Ch.Open
    if (-not $open -or -not $open.IsCompleted) { return $null }
    $waiting = $Ch.Bytes
    if ($open.Status -ne [System.Threading.Tasks.TaskStatus]::RanToCompletion -or $null -eq $open.Result) {
        # A stream that could not be opened takes no line: each one is counted as dropped, never lost silently (r7b, fable r6b note).
        $Ch.Dropped += $waiting.Count
        $waiting.Clear()
        return $null
    }
    $stream = $open.Result
    $task = $null
    while ($waiting.Count -gt 0) {
        $bytes = $waiting.Dequeue()
        try { $task = $stream.WriteAsync($bytes, 0, $bytes.Length) } catch { $Ch.Dropped++; continue }
        $Ch.Pending.Enqueue($task)
    }
    return $task
}

function Wait-BuildGuardStderrTask($Task, $Clock) {
    # Waits for one open or write task, at most what is left of $script:BuildGuardLogWaitMs on $Clock; a faulted task never throws.
    $left = [int][math]::Max(0, $script:BuildGuardLogWaitMs - $Clock.ElapsedMilliseconds)
    [void][System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]]@($Task), $left)
}

function Clear-BuildGuardStderrDone($Ch = $script:BuildGuardStderr) {
    # Finished writes leave the pending queue; one that failed lost its line, which is counted as dropped (r7b).
    $q = $Ch.Pending
    while ($q.Count -gt 0 -and $q.Peek().IsCompleted) {
        if ($q.Dequeue().Status -ne [System.Threading.Tasks.TaskStatus]::RanToCompletion) { $Ch.Dropped++ }
    }
}

function Write-BuildGuardStderr([string]$Line, $Ch = $script:BuildGuardStderr) {
    try {
        # One deadline for the whole line, the open and the write together (r6b adversary A2).
        $clock = [System.Diagnostics.Stopwatch]::StartNew()
        $q = $Ch.Pending
        $waiting = $Ch.Bytes
        # Lines queued while the open was still pending go out before the capacity check, so a stream that recovers gets
        # them and every later line instead of dropping all of them (r6b adversary A1).
        [void](Send-BuildGuardStderrQueue $Ch)
        Clear-BuildGuardStderrDone $Ch
        if ($q.Count + $waiting.Count -ge $script:BuildGuardLogMaxPending) { $Ch.Dropped++; return }
        $waiting.Enqueue([Console]::OutputEncoding.GetBytes($Line + [Environment]::NewLine))
        # Stalled: an earlier line (or the open) is still pending, so this one only queues behind it.
        $stalled = $q.Count -gt 0 -or ($Ch.Open -and -not $Ch.Open.IsCompleted)
        if (-not $Ch.Open) { $Ch.Open = & $Ch.Opener }
        $open = $Ch.Open
        if (-not $stalled -and -not $open.IsCompleted) { Wait-BuildGuardStderrTask $open $clock }
        $task = Send-BuildGuardStderrQueue $Ch
        if (-not $stalled -and $task) { Wait-BuildGuardStderrTask $task $clock }
    } catch { }
}

function Write-BuildGuardTyped([string]$Line) {
    # A typed line (BUILD_HEAVY_SLOT_*, BUILD_MEM_REFUSED, BUILD_SLOT_BUSY, HEAVY_SLOT_LOST_DURING_ACTION) to stdout, bounded
    # like a log line (r8, adversary 3e68ca85-A2); it never throws, so a closed stdout never fails a release (r6b adversary A2).
    Write-BuildGuardStderr $Line $script:BuildGuardStdout
}

function Complete-BuildGuardStderr($Ch = $script:BuildGuardStderr, [switch]$UntilWritten) {
    # r7b (adversary 8d0c73bd-A1): a line can still be queued when the guard is done, because its open completed after the
    # last line was logged and no later line sent it (a fail-open warning before a quiet action, say). Invoke-BuildGuard
    # calls this last, in its outermost finally, after both slots are given back, on every way out (an exit included): the
    # queue goes out and the guard waits for it, at most $script:BuildGuardLogWaitMs in all, the open and the writes together.
    # A stream still not open after that keeps its lines queued (a later line sends them); one that failed counts them dropped.
    # -UntilWritten waits with no bound (stdout with no ancestor slot: nothing is held any more, see Complete-BuildGuardOutput).
    try {
        $open = $Ch.Open
        if (-not $open) { return }
        $clock = [System.Diagnostics.Stopwatch]::StartNew()
        $wait = { param($t) if ($UntilWritten) { while (-not $t.IsCompleted) { [void][System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]]@($t), 500) } } else { Wait-BuildGuardStderrTask $t $clock } }
        if ($Ch.Bytes.Count -gt 0 -and -not $open.IsCompleted) { & $wait $open }
        [void](Send-BuildGuardStderrQueue $Ch)
        foreach ($t in @($Ch.Pending.ToArray())) { & $wait $t }
        Clear-BuildGuardStderrDone $Ch
    } catch { }
}

function Complete-BuildGuardOutput {
    # Invoke-BuildGuard's last step, once nothing of its own is held: the queued log lines (bounded), then the typed lines,
    # bounded too while an ancestor holds a heavy slot, else waited for until their reader takes them.
    Complete-BuildGuardStderr
    Complete-BuildGuardStderr $script:BuildGuardStdout -UntilWritten:(-not (Test-BuildGuardAncestorSlot))
}

function Write-BuildGuardLog([string]$Message) {
    Write-BuildGuardStderr "[build-guard] $Message"
}

function Get-BuildGuardJsonField($Object, [string]$Name) {
    # r6 sol B2: one field of a parsed JSON record, or $null when the record is not one object or has no such field. It reads
    # through PSObject.Properties, never a direct member access, so a strict-mode host gets $null, never a
    # PropertyNotFoundException.
    if ($Object -isnot [System.Management.Automation.PSCustomObject]) { return $null }
    $p = $Object.PSObject.Properties[$Name]
    if ($p) { return $p.Value }
    return $null
}

function Resolve-BuildGuardPath([string]$Path, [switch]$NeedParent) {
    # r9 (adversary 2ab4b1b6-A1 = 5334fd83-A2): the one resolver for a caller or seam path. Join-Path, Split-Path, Resolve-Path and
    # the provider resolver write an error record or throw on a drive that does not exist or a path of another provider, and the
    # host renders either to stderr synchronously, past the bounded writer, while an ancestor's heavy slot is held. This never
    # throws and writes no record: it returns @{ Full; Parent; Why }, the full file-system path and its parent directory, or why
    # there is none. Callers compose paths from it with [IO.Path] only.
    $r = @{ Full = ''; Parent = ''; Why = '' }
    try {
        if (-not ([string]$Path).Trim()) { throw 'it is empty' }
        $provider = $null
        $drive = $null
        $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path, [ref]$provider, [ref]$drive)
        if ($provider.Name -ne 'FileSystem') { throw ('it is a {0} path, not a file-system one' -f $provider.Name) }
        if ($full.IndexOfAny([IO.Path]::GetInvalidPathChars()) -ge 0) { throw 'it holds a character no file path may hold' }
        $full = [IO.Path]::GetFullPath($full)
        # A leaf no Windows file can have ('bad?.lock') made CreateNew fail as if another build held the lock (353c9a42-A3).
        if ([IO.Path]::GetFileName($full).IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) { throw 'its last part holds a character no file name may hold' }
        $parent = [string][IO.Path]::GetDirectoryName($full)
        if ($NeedParent -and -not $parent) { throw 'it has no parent directory' }
        $r.Full = $full
        $r.Parent = $parent
    } catch {
        $e = $_.Exception
        if ($e -is [System.Management.Automation.MethodInvocationException] -and $e.InnerException) { $e = $e.InnerException }
        $r.Why = $e.Message -replace '\s+', ' '
    }
    return $r
}

function New-BuildGuardDirectory([string]$Dir) {
    # r9: creates a directory of a resolved path, or returns why it is not one. New-Item -Force returns nothing and writes no
    # error for a directory under a file, so the test is [IO.Directory]::Exists afterwards. Never throws, writes no record.
    try {
        if (-not [IO.Directory]::Exists($Dir)) { [void][IO.Directory]::CreateDirectory($Dir) }
        if ([IO.Directory]::Exists($Dir)) { return '' }
        return "$Dir is not a directory"
    } catch {
        $e = $_.Exception
        if ($e -is [System.Management.Automation.MethodInvocationException] -and $e.InnerException) { $e = $e.InnerException }
        return ('{0} not created ({1})' -f $Dir, ($e.Message -replace '\s+', ' '))
    }
}

function Exit-BuildGuardPathRefused([string]$Label, [string]$Option, [string]$Value, [string]$Why) {
    # r9: a caller path the guard cannot use is a typed refusal (exit 2, usage), bounded like every typed line. The caller gives
    # back whatever it took first.
    Write-BuildGuardTyped ('BUILD_GUARD_PATH_REFUSED label={0} option={1} value={2} reason={3} utc={4}' -f $Label, $Option, ($Value -replace '[\x00-\x1f]', '?'), $Why, [DateTime]::UtcNow.ToString('o'))
    exit 2
}

function Get-BuildGuardLockPath([hashtable]$Options, [string]$Label) {
    # r9: -LockPath through Resolve-BuildGuardPath; one that names no file-system file with a parent directory is refused before
    # anything is taken.
    $lock = Resolve-BuildGuardPath $Options.LockPath -NeedParent
    if ($lock.Why) { Exit-BuildGuardPathRefused $Label 'LockPath' $Options.LockPath $lock.Why }
    # A directory is never a lock (r12, adversary d4a586f2-A2): an old empty one read as an unreadable stale lock, was deleted
    # and replaced by the lock file, and the release then deleted that.
    if ([IO.Directory]::Exists($lock.Full)) { Exit-BuildGuardPathRefused $Label 'LockPath' $Options.LockPath 'it is a directory, not a lock file' }
    return $lock
}

function Add-BuildGuardLogLine([string]$Path, [string]$Line, [Text.Encoding]$Encoding) {
    # LEDGER-APPEND-ATOMIC-BUILD-LOGS-1: pwsh 7 Add-Content opens FileMode.Append, which records EOF as the
    # stream's own offset, so two concurrent writers overwrite each other's line (measured ~14 % lost across 6
    # writers). FILE_APPEND_DATA only (no WRITE_DATA) makes the OS append at the current end of file atomically.
    # Same bytes as before: the line, then the platform newline, no BOM.
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $bytes = $Encoding.GetBytes($Line + [Environment]::NewLine)
    $fs = [IO.FileSystemAclExtensions]::Create([IO.FileInfo]::new($full), [IO.FileMode]::OpenOrCreate, [Security.AccessControl.FileSystemRights]::AppendData, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete), 4096, [IO.FileOptions]::None, $null)
    try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush() } finally { $fs.Dispose() }
}

function ConvertTo-BuildGuardUtc($Value) {
    try { return ([datetime]$Value).ToUniversalTime().ToString('o') } catch { return [string]$Value }
}

function Get-BuildGuardMemory([hashtable]$Options) {
    # Returns @{ PhysGB; CommitGB }. A probe that cannot be read reports 0 (fail closed, like dispatch-lane).
    # r7 (a34e2098-A1): this runs while the machine heavy slot is held, so no cmdlet here may write an error record (the host
    # renders it to stderr synchronously, past the bounded writer): each one stops and is caught, or is silenced and reported.
    $ErrorActionPreference = 'Stop'
    if ($Options.MemoryProbeFile) {
        try { $raw = Get-Content -LiteralPath $Options.MemoryProbeFile -Raw } catch {
            Write-BuildGuardLog ('memory probe unreadable ({0}): reads 0 (fail closed)' -f ($_.Exception.Message -replace '\s+', ' '))
            return @{ PhysGB = 0.0; CommitGB = 0.0 }
        }
        $samples = @(if ($null -ne $raw) { $raw | ConvertFrom-Json })
        if ($samples.Count -eq 0) { return @{ PhysGB = 0.0; CommitGB = 0.0 } }
        $i = [math]::Min($script:BuildGuardProbeIndex, $samples.Count - 1)
        $script:BuildGuardProbeIndex++
        # A sample without a field reads 0 (fail closed), in a strict-mode host too (r6 sol B2 sweep).
        return @{ PhysGB = [double](Get-BuildGuardJsonField $samples[$i] 'freePhysGB'); CommitGB = [double](Get-BuildGuardJsonField $samples[$i] 'freeCommitGB') }
    }
    $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec $script:BuildGuardCimTimeoutSec -ErrorAction SilentlyContinue
    if (-not $os) {
        Write-BuildGuardLog 'memory probe unreadable (Win32_OperatingSystem returned nothing): reads 0 (fail closed)'
        return @{ PhysGB = 0.0; CommitGB = 0.0 }
    }
    return @{
        PhysGB   = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
        CommitGB = [math]::Round($os.FreeVirtualMemory / 1MB, 2)
    }
}

function Get-BuildGuardJobs([hashtable]$Options, [int]$SelfPid) {
    # 4 by default; 2 when another live cc1plus / mingw32-make exists. Peers are pid@startUtc.
    # It runs while both slots are held: Stop-scoped, and a table that cannot be read is logged (r7, a34e2098-A1).
    $ErrorActionPreference = 'Stop'
    if ($Options.ProcessTableFile) {
        try { $raw = Get-Content -LiteralPath $Options.ProcessTableFile -Raw } catch {
            Write-BuildGuardLog ('process table unreadable ({0}): no peers counted' -f ($_.Exception.Message -replace '\s+', ' '))
            $raw = $null
        }
        # Each row is read field by field, so a row without one reads $null in a strict-mode host too (r6 sol B2 sweep).
        # An empty file reads $null, which ConvertFrom-Json refuses: that is an empty table, as before.
        $table = @($(if ($null -ne $raw) { $raw | ConvertFrom-Json }) | ForEach-Object {
                $r = $_
                [pscustomobject]@{ ProcessId = Get-BuildGuardJsonField $r 'ProcessId'; ParentProcessId = Get-BuildGuardJsonField $r 'ParentProcessId'
                    Name = Get-BuildGuardJsonField $r 'Name'; CreationDate = Get-BuildGuardJsonField $r 'CreationDate' }
            })
    } else {
        $table = @(Get-CimInstance Win32_Process -OperationTimeoutSec $script:BuildGuardCimTimeoutSec -ErrorAction SilentlyContinue |
            Select-Object ProcessId, ParentProcessId, Name, CreationDate)
        if ($table.Count -eq 0) { Write-BuildGuardLog 'process table unreadable (Win32_Process returned nothing): no peers counted' }
    }
    # This process and its ancestors are not peers (a guard launched from make must not downgrade itself).
    $ancestors = New-Object 'System.Collections.Generic.HashSet[int]'
    [void]$ancestors.Add($SelfPid)
    $cursor = $SelfPid
    for ($depth = 0; $depth -lt 32; $depth++) {
        $row = $table | Where-Object { [int]$_.ProcessId -eq $cursor } | Select-Object -First 1
        if (-not $row -or -not $row.ParentProcessId) { break }
        $cursor = [int]$row.ParentProcessId
        if (-not $ancestors.Add($cursor)) { break }
    }
    $peers = @($table | Where-Object {
            $_.Name -match '^(cc1plus|mingw32-make)(\.exe)?$' -and -not $ancestors.Contains([int]$_.ProcessId)
        } | ForEach-Object { '{0}@{1}' -f [int]$_.ProcessId, (ConvertTo-BuildGuardUtc $_.CreationDate) })
    if ($peers.Count -gt 0) { return @{ Jobs = 2; Reason = 'peer-build'; Peers = $peers } }
    return @{ Jobs = 4; Reason = 'default'; Peers = @() }
}

function Test-BuildGuardOwnerAlive([int]$OwnerPid, [long]$StartTicks) {
    $p = Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    try { $actual = $p.StartTime.ToUniversalTime().Ticks } catch { return $true }   # cannot read start time: assume alive
    return ([math]::Abs($actual - $StartTicks) -le [TimeSpan]::FromSeconds(2).Ticks)
}

function Invoke-BuildSlotAcquire([hashtable]$Options, [string]$Label, [int]$MutexWaitMs = 15000) {
    # Returns @{ Acquired; Held; Ticket }. The whole read-check-reclaim-create runs under a named mutex so
    # two reclaimers cannot delete each other's fresh lock. The mutex wait is what is left of the admission deadline when
    # the caller has one (r12, sol r11 B2 sibling: a fixed 15 s wait held the machine slot past a 0.6 s deadline).
    # It runs while the machine heavy slot is held, so it is Stop-scoped (r7, a34e2098-A1): an error record would be rendered
    # to stderr past the bounded writer. A failure that should refuse admission throws to Invoke-BuildGuard, whose finally
    # gives the heavy slot back first; a stale lock that cannot be removed is a slot still busy, logged.
    $ErrorActionPreference = 'Stop'
    # A lock directory that cannot be made is a path refusal, never a throw (r9, 2ab4b1b6-A1 class): Invoke-BuildGuard gives the
    # heavy slot back and prints it typed.
    $dir = [IO.Path]::GetDirectoryName([string]$Options.LockPath)
    if ($dir) {
        $why = New-BuildGuardDirectory $dir
        if ($why) { return @{ Acquired = $false; Held = ''; Refused = "lock directory $why" } }
    }
    $mutex = New-Object System.Threading.Mutex($false, $Options.MutexName)
    $got = $false
    try {
        try { $got = $mutex.WaitOne([math]::Max(0, $MutexWaitMs)) } catch [System.Threading.AbandonedMutexException] { $got = $true }
        if (-not $got) { return @{ Acquired = $false; Held = 'acquire-mutex-busy' } }
        $path = $Options.LockPath
        # One made after the path was checked is refused here, under the mutex, never reclaimed (r12, d4a586f2-A2).
        if ([IO.Directory]::Exists($path)) { return @{ Acquired = $false; Held = ''; Refused = "$path is a directory, not a lock file" } }
        if (Test-Path -LiteralPath $path) {
            $info = $null
            # The record is read field by field and cast inside the catch (r6 sol B2 sweep): one that is not an object, lacks
            # its owner or holds a field that is not a number is unreadable (reclaimed once old), in a strict-mode host too,
            # never a throw.
            try {
                $rec = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
                $recOwner = Get-BuildGuardJsonField $rec 'ownerPid'
                if ($recOwner) {
                    $info = @{ ownerPid = [int]$recOwner; startTicks = [long](Get-BuildGuardJsonField $rec 'startTicks')
                        expiresTicks = [long](Get-BuildGuardJsonField $rec 'expiresTicks'); label = [string](Get-BuildGuardJsonField $rec 'label') }
                }
            } catch { $info = $null }
            $nowTicks = [DateTime]::UtcNow.Ticks
            $reclaim = ''
            if (-not $info -or -not $info.ownerPid) {
                # empty or half-written; a writer finishes in milliseconds, so only an old file is stale. One that vanished or
                # cannot be stat'ed now is not reclaimed on this poll (the next one sees it again).
                $age = 0
                try { $age = ([DateTime]::UtcNow - (Get-Item -LiteralPath $path).LastWriteTimeUtc).TotalSeconds } catch { }
                if ($age -gt 60) { $reclaim = 'unreadable' }
            } elseif ($nowTicks -ge [long]$info.expiresTicks) {
                $reclaim = 'expired'
            } elseif (-not (Test-BuildGuardOwnerAlive -OwnerPid ([int]$info.ownerPid) -StartTicks ([long]$info.startTicks))) {
                $reclaim = 'owner-dead'
            }
            if (-not $reclaim) {
                $held = if ($info -and $info.ownerPid) { 'pid={0} label={1} expiresUtc={2}' -f $info.ownerPid, $info.label, ([datetime]::new([long]$info.expiresTicks, 'Utc').ToString('o')) } else { 'lock-being-written' }
                return @{ Acquired = $false; Held = $held }
            }
            Write-BuildGuardLog "reclaiming stale build slot ($reclaim): $path"
            try { Remove-Item -LiteralPath $path -Force -WhatIf:$false -Confirm:$false } catch {
                $why = $_.Exception.Message -replace '\s+', ' '
                Write-BuildGuardLog "stale build slot not removed ($reclaim): $path ($why)"
                return @{ Acquired = $false; Held = "stale-lock-not-removed ($reclaim): $why" }
            }
        }
        $self = Get-Process -Id $PID
        $startTicks = $self.StartTime.ToUniversalTime().Ticks
        $record = [ordered]@{
            ownerPid     = $PID
            startTicks   = $startTicks
            startUtc     = $self.StartTime.ToUniversalTime().ToString('o')
            expiresTicks = [DateTime]::UtcNow.AddMinutes($Options.LockExpiryMinutes).Ticks
            label        = $Label
            host         = $env:COMPUTERNAME
        }
        $bytes = [System.Text.Encoding]::ASCII.GetBytes(($record | ConvertTo-Json -Compress))
        try {
            $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
            try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush() } finally { $fs.Dispose() }
        } catch [System.IO.IOException] {
            return @{ Acquired = $false; Held = 'lock-created-by-another-process' }
        } catch [System.UnauthorizedAccessException] {
            # A lock another guard is still deleting refuses CreateNew this way: busy for this poll, never a throw (r9).
            return @{ Acquired = $false; Held = 'lock-create-denied: ' + ($_.Exception.Message -replace '\s+', ' ') }
        }
        return @{ Acquired = $true; Held = ''; Ticket = @{ Path = $path; OwnerPid = $PID; StartTicks = $startTicks; MutexName = $Options.MutexName } }
    } finally {
        if ($got) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Remove-BuildSlot($Ticket) {
    # Release only a lock that is still ours (pid + start time), never a successor's. The read, compare and
    # delete run under the same mutex as acquire, so a contender cannot reclaim an expired lock and create its
    # own between our read and our delete. If the mutex stays busy, the lock is left: once this process exits
    # it is reclaimable as owner-dead.
    if (-not $Ticket) { return }
    # The mutex is opened inside the try too (r6b adversary A1): a name taken by another kernel object throws here, and a
    # release never throws past its caller, which still has the machine heavy slot to give back.
    # Stop-scoped (r7, a34e2098-A1): a delete that fails (the lock held open without a Delete share) used to write an error
    # record, which the host rendered to an undrained stderr before the heavy-slot stop. Now it is caught and logged through
    # the bounded writer, and the lock is left for owner-dead reclaim.
    # A release never throws on a preference an action locked (r11 adversary 6b8295aa-A2: an AllScope, ReadOnly
    # $ErrorActionPreference made this assignment throw before the try, so the lock was never deleted). Its own cmdlets say
    # -ErrorAction Stop, so they are caught below whatever the preference holds.
    try { $ErrorActionPreference = 'Stop' } catch { }
    $mutex = $null
    $got = $false
    try {
        $mutex = New-Object System.Threading.Mutex($false, $Ticket.MutexName)
        try { $got = $mutex.WaitOne(15000) } catch [System.Threading.AbandonedMutexException] { $got = $true }
        if (-not $got) { Write-BuildGuardLog "release-mutex-busy: build slot left for owner-dead reclaim: $($Ticket.Path)"; return }
        $info = Get-Content -LiteralPath $Ticket.Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ([int]$info.ownerPid -eq $Ticket.OwnerPid -and [long]$info.startTicks -eq $Ticket.StartTicks) {
            # A .NET delete, never a cmdlet (r13, adversary 16ea1ebb-A1): no default the caller or the action left in
            # $PSDefaultParameterValues can skip it, even in a dictionary the guard cannot switch off. It throws into the catch.
            [IO.File]::Delete($Ticket.Path)
        }
    } catch {
        Write-BuildGuardLog ('build slot not released: {0} ({1}); left for owner-dead reclaim' -f $Ticket.Path, ($_.Exception.Message -replace '\s+', ' '))
    } finally {
        if ($got) { $mutex.ReleaseMutex() }
        if ($mutex) { $mutex.Dispose() }
    }
}

function Get-BuildGuardExemptReason([string]$Reason) {
    # An empty, whitespace or false-y value ('0', 'false', 'off', 'no') is not an exemption (catalogue P-01).
    $text = ([string]$Reason).Trim()
    if ($text -match '^(0|false|off|no)$') { return '' }
    return $text
}

function Resolve-HeavySlotCmd([hashtable]$Options) {
    # The shim: -HeavySlotCmd, then env MLV_HEAVY_SLOT_CMD, then heavy-slot.cmd on PATH, then the install path.
    # A named path that does not exist is missing (fail open); it never falls through to the live install.
    foreach ($named in @($Options.HeavySlotCmd, $env:MLV_HEAVY_SLOT_CMD)) {
        if (-not ([string]$named).Trim()) { continue }
        # Through the one resolver (r9, 2ab4b1b6-A1 sibling :853): a path of another provider (Env:\ComSpec is a leaf there) or on
        # a missing drive is missing too, never an error record past the bounded writer (r8 adversary f99852cb-A1).
        $shim = Resolve-BuildGuardPath $named
        if (-not $shim.Why -and (Test-Path -LiteralPath $shim.Full -PathType Leaf -ErrorAction Ignore)) { return $shim.Full }
        return ''
    }
    $found = Get-Command -Name 'heavy-slot.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    if (Test-Path -LiteralPath $script:HeavySlotInstallCmd -PathType Leaf -ErrorAction Ignore) { return $script:HeavySlotInstallCmd }
    return ''
}

function Test-HeavySlotHeldCanonical([string]$Held) {
    # $null when the holder an inherited HEAVY_SLOT_HELD (<ticket>:<pid>) names holds the canonical machine slot: its held record
    # in heavy-slot's default state dir (the guard strips HEAVY_SLOT_DIR) says slot set Global\HeavySlot, one slot, slot 0.
    # Otherwise why not (r7b adversary 01aa4e7b-A2). The name is compared case-sensitively, as the kernel names its mutexes:
    # Global\HEAVYSLOT-0 is another slot (r7b adversary df03e61e-A1). heavy-slot still verifies the holder's identity and ancestry.
    if ($Held -notmatch '^([0-9A-Za-z-]+):(\d+)$') { return "malformed value '$Held'" }
    $ticket = $Matches[1]
    # The held dir goes through the one resolver (r9, 2ab4b1b6-A1 sibling :541): one on a missing drive is a reason, no record.
    $dir = Resolve-BuildGuardPath $script:HeavySlotHeldDir
    if ($dir.Why) { return "no readable held record: held dir $($script:HeavySlotHeldDir) unusable ($($dir.Why))" }
    $path = [IO.Path]::Combine($dir.Full, $ticket + '.json')
    try { $rec = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { return "no readable held record at $path" }
    $name = [string](Get-BuildGuardJsonField $rec 'slotName')
    $count = [string](Get-BuildGuardJsonField $rec 'slotCount')
    $slots = @(Get-BuildGuardJsonField $rec 'slots')
    if ($name -cne 'Global\HeavySlot' -or $count -cne '1' -or $slots.Count -ne 1 -or [string]$slots[0] -cne '0') {
        return "its held record names slot set '$name' x$count slots=[$($slots -join ',')], not Global\HeavySlot x1 slot 0"
    }
    return $null
}

function Get-HeavySlotLane {
    # <lane> of mlv:<lane>:build: lane-<CARD>-rN-<stamp> under MLV_LANE_RUN_DIR gives <CARD>; no run dir is the hub.
    $run = ([string]$env:MLV_LANE_RUN_DIR).TrimEnd('\', '/')
    if (-not $run) { return 'hub' }
    $leaf = [IO.Path]::GetFileName($run)
    if ($leaf -match '^lane-(.+)-r\d+-\d{8}T\d{4,6}Z$') { $leaf = $Matches[1] }
    return ($leaf -replace '[^A-Za-z0-9._-]', '-')
}

function New-HeavySlotHolder([string]$ReadyFile, [string]$StopFile) {
    # The child heavy-slot runs once it admits this unit: it marks the admission, then holds until the stop file
    # appears or this guard (pid + start time) is gone, so a killed guard never leaves the machine slot held.
    $quote = { param($s) "'" + ($s -replace "'", "''") + "'" }
    $startTicks = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
    # The ready file names the holder (pid@start ticks): heavy-slot holds the slot exactly while this child runs, so the
    # guard checks the holder itself, not the cmd wrapper, which can outlive the release.
    # It also names heavy-slot's own process (the slot mutex owner, HEAVY_SLOT_HELD = <ticket>:<pid>) by pid + start ticks,
    # read here while that owner is waiting on this child, so a reused pid can never pass for it later; and it ends itself
    # when that owner ends, so a holder never outlives the slot it stands for (r3b adversary A1/A2).
    # The owner counts only when the process under that pid started at HEAVY_SLOT_HELD_START, the start time heavy-slot
    # exports beside it; a missing or unreadable value, or another start time (a reused pid), is written as @-1, which no
    # process matches (r4 sol B2). Any error ends the holder (exit 1), so an unreadable identity is never one still held
    # (r4 sol B5). The ready file ends with an 'end' line, so a partly written one is never read as complete (r4 sol B1).
    $text = @(
        "`$ErrorActionPreference = 'Stop'; trap { exit 1 }"
        "`$me = Get-Process -Id `$PID"
        "`$ownerTicks = -1L; `$ownerLine = 'owner=none'"
        "if ([string]`$env:HEAVY_SLOT_HELD -match ':(\d+)`$') {"
        "    `$ownerPid = [int]`$Matches[1]"
        "    try {"
        "        `$want = [DateTime]::Parse([string]`$env:HEAVY_SLOT_HELD_START, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime().Ticks"
        "        `$owner = Get-Process -Id `$ownerPid -ErrorAction SilentlyContinue"
        "        if (`$owner -and `$owner.StartTime.ToUniversalTime().Ticks -eq `$want) { `$ownerTicks = `$want }"
        "    } catch { `$ownerTicks = -1L }"
        "    `$ownerLine = 'owner={0}@{1}' -f `$ownerPid, `$ownerTicks"
        "}"
        "Set-Content -LiteralPath $(& $quote $ReadyFile) -Value @([string]`$env:HEAVY_SLOT_HELD, ('holder={0}@{1}' -f `$PID, `$me.StartTime.ToUniversalTime().Ticks), `$ownerLine, 'end') -Encoding ascii"
        "while (-not (Test-Path -LiteralPath $(& $quote $StopFile))) {"
        "    `$p = Get-Process -Id $PID -ErrorAction SilentlyContinue"
        "    if (-not `$p -or `$p.StartTime.ToUniversalTime().Ticks -ne $startTicks) { exit 1 }"
        "    if (`$ownerLine -ne 'owner=none') {"
        "        `$o = Get-Process -Id `$ownerPid -ErrorAction SilentlyContinue"
        "        if (-not `$o -or `$o.StartTime.ToUniversalTime().Ticks -ne `$ownerTicks) { exit 1 }"
        "    }"
        "    Start-Sleep -Milliseconds 500"
        "}"
    ) -join "`n"
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($text))
}

function Send-HeavySlotLines($Lease) {
    # Relays the lines heavy-slot has written to its log file since the last call, and keeps them for the marker check.
    # A log that exists but cannot be read sets LogUnread, so admission can fail closed instead of seeing no lines.
    # While heavy-slot runs, only newline-terminated lines are taken: a last line still being written stays in $Lease.Tail
    # for a later call, so a half-written line is never kept in place of the whole one (r4 adversary A2). Once heavy-slot
    # has ended, its last line is complete with or without a newline.
    try { $raw = [string](Get-Content -LiteralPath $Lease.Log -Raw -ErrorAction Stop); $Lease.LogUnread = $false } catch {
        $raw = ''
        $Lease.LogUnread = [bool](Test-Path -LiteralPath $Lease.Log -ErrorAction Ignore)
    }
    $all = @($raw -split '\r?\n')
    $last = $all[-1]
    $all = @(if ($all.Count -gt 1) { $all[0..($all.Count - 2)] })
    $Lease.Tail = ''
    if ($last) { if ($Lease.Process -and $Lease.Process.HasExited) { $all += $last } else { $Lease.Tail = $last } }
    for ($i = $Lease.Lines.Count; $i -lt $all.Count; $i++) {
        $Lease.Lines.Add([string]$all[$i])
        Write-BuildGuardStderr ([string]$all[$i])
    }
}

function Test-HeavySlotMarker($Lease, [int]$Code) {
    # B1: a 75-77 is heavy-slot's own only when the --exit-file marker names THIS run: source heavy-slot, the same
    # code, and the ticket + nonce from its HEAVY-SLOT TICKET stderr line. Returns '' when it does, else why not.
    if ($Code -lt 75 -or $Code -gt 77) { return "exit $Code is not a heavy-slot code" }
    $ticket = ''
    $nonce = ''
    foreach ($l in $Lease.Lines) { if ($l -match '^HEAVY-SLOT TICKET (\S+) ([0-9a-f]{32})\s*$') { $ticket = $Matches[1]; $nonce = $Matches[2] } }
    if (-not $ticket) { return 'no HEAVY-SLOT TICKET line on stderr' }
    $Lease.Ticket = $ticket
    # r6 sol B2: only ONE JSON object counts (-NoEnumerate keeps '[{...}]' an array), and its fields are read through
    # Get-BuildGuardJsonField inside the second try. {}, [], null, a scalar or an object missing an identity field is a
    # reason string (so COULD_NOT_CHECK) in a strict-mode host too, never a PropertyNotFoundException; an absent reason is ''
    # and never changes the identity verdict.
    try { $m = Get-Content -LiteralPath $Lease.ExitFile -Raw -ErrorAction Stop | ConvertFrom-Json -NoEnumerate -ErrorAction Stop } catch { return 'no readable exit marker' }
    try {
        if ($m -isnot [System.Management.Automation.PSCustomObject]) { return 'the exit marker is not one JSON object' }
        $f = @{}
        foreach ($n in 'source', 'code', 'ticket', 'nonce', 'reason') { $f[$n] = [string](Get-BuildGuardJsonField $m $n) }
        if ($f['source'] -ne 'heavy-slot' -or $f['code'] -ne [string]$Code -or $f['ticket'] -ne $ticket -or $f['nonce'] -ne $nonce) {
            return ('the exit marker does not name this run (source={0} code={1} ticket={2})' -f $f['source'], $f['code'], $f['ticket'])
        }
        $Lease.Reason = $f['reason']
        return ''
    } catch { return ('the exit marker could not be checked ({0})' -f ($_.Exception.Message -replace '\s+', ' ')) }
}

function Remove-HeavySlotFiles($Lease) {
    # The one place a stop file is deleted (r8, adversary 3e68ca85-A1): only once Test-HeavySlotStopNeeded proves no holder
    # can still read it, decided before the ready file that names the holder goes. The other lease files go either way.
    $keepStop = Test-HeavySlotStopNeeded $Lease
    foreach ($f in @($Lease.Ready, $Lease.ExitFile, $Lease.Log)) { Remove-HeavySlotPath $f }
    if (-not $keepStop) { Remove-HeavySlotPath $Lease.Stop }
}

function Remove-HeavySlotPath([string]$Path) {
    # A release delete is a .NET call, never a cmdlet (r13, adversary 16ea1ebb-A1): an action can leave its own defaults
    # ('Remove-Item:Exclude'='*') in a read-only dictionary the guard can neither replace nor switch off, and a cmdlet would
    # obey them. As Remove-Item -Force without -Recurse did: a file or an empty directory goes; a failure is silent.
    if (-not $Path) { return }
    try { if ([IO.Directory]::Exists($Path)) { [IO.Directory]::Delete($Path) } else { [IO.File]::Delete($Path) } } catch { }
}

function Remove-HeavySlotLeaseFiles($Lease) {
    # After the stop file is written: a holder that outlived heavy-slot (its slot owner gone) polls for the stop file every
    # 500 ms, so it is given 10 s to see it and end (r3b adversary A2); then Remove-HeavySlotFiles keeps the stop file unless
    # that holder is proven gone. The wait is on the holder's own handle, checked against its pid + start ticks, never on
    # whatever process holds its pid later (r4 sol B4); a holder known only from a partial ready file is waited for too (r8).
    $known = Get-HeavySlotStopHolder $Lease
    $h = if ($known) { Get-HeavySlotProcess $known }
    if ($h) { try { [void]$h.WaitForExit(10000) } finally { $h.Dispose() } }
    Remove-HeavySlotFiles $Lease
}

function Get-HeavySlotProcess($Identity) {
    # The running process named by pid + start ticks, returned with its handle open (the caller disposes it), so that pid
    # cannot pass to another process while it is used; else $null. A property that cannot be read after the lookup (the
    # process ended in between) is $null, never a process still running (r4 sol B5).
    $p = Get-Process -Id $Identity.Pid -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { return $null }
    try {
        if ($null -ne $p.Handle -and -not $p.HasExited -and $p.StartTime.ToUniversalTime().Ticks -eq [long]$Identity.Ticks) { return $p }
    } catch { }
    if ($p -is [IDisposable]) { $p.Dispose() }
    return $null
}

function Test-HeavySlotIdentity($Identity) {
    # True only while the process named by pid + start ticks provably still runs.
    $p = Get-HeavySlotProcess $Identity
    if (-not $p) { return $false }
    if ($p -is [IDisposable]) { $p.Dispose() }
    return $true
}

function Get-HeavySlotHolderState($Lease) {
    # 'alive' or 'gone' for the machine slot behind the ready file; '' while the ready file is not complete yet (no 'end'
    # line); 'unreadable' for a complete one whose holder or owner line does not parse. 'alive' needs all three: the holder
    # named in the ready file (pid + start ticks), the heavy-slot process that owns the slot mutex (the pid on the ready
    # file's first line, HEAVY_SLOT_HELD = <ticket>:<pid>), and heavy-slot's cmd wrapper (r2 adversary A2: a holder whose
    # mutex owner has died holds nothing). $Lease.Gone names the first one found gone.
    # The first complete read is kept, so a ready file that later vanishes cannot hide a holder that has ended. A partial
    # read is never kept: its owner line may not be on disk yet, and owner validation must never be skipped (r4 sol B1).
    if (-not $Lease.Holder) {
        $text = [string](Get-Content -LiteralPath $Lease.Ready -Raw -ErrorAction SilentlyContinue)
        if (-not ($text -match '(?m)^end\s*$')) { return '' }
        # A pid or tick count too large for its type is unreadable too, never a throw (r4 adversary A3).
        [int]$n = 0; [long]$t = 0
        if (-not ($text -match '(?m)^holder=(\d+)@(\d+)\s*$' -and [int]::TryParse($Matches[1], [ref]$n) -and [long]::TryParse($Matches[2], [ref]$t))) { return 'unreadable' }
        $holder = @{ Pid = $n; Ticks = $t }
        # The owner's identity is the one the holder verified against HEAVY_SLOT_HELD_START (owner=<pid>@<ticks>; -1 when it
        # was gone or not verifiable), never a pid looked up now, which may have been reused (r3b adversary A1, r4 sol B2).
        # 'owner=none' (heavy-slot ran the holder without its slot) leaves only the holder to check; no owner line is unreadable.
        $owner = $null
        if ($text -match '(?m)^owner=(\d+)@(-?\d+)\s*$') {
            if (-not ([int]::TryParse($Matches[1], [ref]$n) -and [long]::TryParse($Matches[2], [ref]$t))) { return 'unreadable' }
            $owner = @{ Pid = $n; Ticks = $t }
        } elseif (-not ($text -match '(?m)^owner=none\s*$')) { return 'unreadable' }
        $Lease.Holder = $holder
        $Lease.Owner = $owner
    }
    if (-not (Test-HeavySlotIdentity $Lease.Holder)) { $Lease.Gone = 'holder-exited'; return 'gone' }
    if ($Lease.Owner -and -not (Test-HeavySlotIdentity $Lease.Owner)) { $Lease.Gone = 'slot-owner-exited'; return 'gone' }
    if ($Lease.Process.HasExited) { $Lease.Gone = 'heavy-slot-exited'; return 'gone' }
    return 'alive'
}

function Get-HeavySlotStopHolder($Lease) {
    # The holder that reads this run's stop file, by pid + start ticks: the verified one, else the holder line of the ready
    # file, complete or not, once that line ends in a newline (it was written whole; '8420@1' with no newline may be cut short
    # of '8420@1234...'). It is kept once read, so a ready file deleted later cannot hide it. $null while none was read.
    # This identity only ever decides the stop file's lifetime; admission still needs the complete record (r4 sol B1).
    # The lease is read through its indexer, so a hand-built one without these keys reads $null in a strict-mode host too.
    if ($Lease['Holder']) { return $Lease['Holder'] }
    if (-not $Lease['StopHolder'] -and $Lease['Ready']) {
        $text = [string](Get-Content -LiteralPath $Lease['Ready'] -Raw -ErrorAction SilentlyContinue)
        [int]$n = 0; [long]$t = 0
        if ($text -match '(?m)^holder=(\d+)@(\d+)\r?\n' -and [int]::TryParse($Matches[1], [ref]$n) -and [long]::TryParse($Matches[2], [ref]$t)) {
            $Lease['StopHolder'] = @{ Pid = $n; Ticks = $t }
        }
    }
    return $Lease['StopHolder']
}

function Test-HeavySlotStopNeeded($Lease) {
    # Whether a holder may still read the stop file, the one rule for every stop-file write and delete (r8, adversary
    # 3e68ca85-A1): yes while heavy-slot's cmd wrapper runs, and after it ended until this run's holder is PROVEN gone, named
    # by pid + start ticks (Get-HeavySlotStopHolder) and no longer running. No holder line read yet (no ready file, a partial
    # or unreadable one) proves nothing: the wrapper may have ended while the heavy-slot child it started outlived it, still
    # queued or descheduled mid-publication, and that holder would hold the machine slot while this host lives (r7b
    # 01aa4e7b-A1). So unreadable or partial means keep the stop file, never clean it up.
    if (-not $Lease.Process.HasExited) { return $true }
    $known = Get-HeavySlotStopHolder $Lease
    return (-not $known -or -not (Test-HeavySlotGone $known))
}

function Test-HeavySlotGone($Identity) {
    # True only when the process named by pid + start ticks provably no longer runs: no process has that pid, the one that has
    # it started at another time, or it has exited. One that cannot be inspected (its handle denied, it ended mid-read) proves
    # nothing (r8 adversary f99852cb-A2): Test-HeavySlotIdentity's $false means "not proven alive", not "gone".
    $p = Get-Process -Id $Identity.Pid -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { return $true }
    try {
        if ($p.StartTime.ToUniversalTime().Ticks -ne [long]$Identity.Ticks) { return $true }
        return [bool]$p.HasExited
    } catch { return $false } finally { if ($p -is [IDisposable]) { $p.Dispose() } }
}

function Send-HeavySlotStop($Lease) {
    # Writes the stop file. When it cannot be written, heavy-slot's process tree is ended instead (the OS then frees its
    # slot mutex), so a holder that would never see the file cannot keep the machine slot.
    $Lease['StopSent'] = $true
    # The same bytes Set-Content -Encoding ascii wrote, through .NET (r13, adversary 16ea1ebb-A1): a 'Set-Content:PassThru'
    # default the guard could not switch off put 'stop' into the action's output.
    try { [IO.File]::WriteAllText($Lease.Stop, "stop`r`n", [Text.Encoding]::ASCII); return } catch {
        $why = if ($_.Exception.InnerException) { $_.Exception.InnerException.Message } else { $_.Exception.Message }
    }
    Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} stop file not written ({1}); ending heavy-slot pid {2} and its children' -f $Lease.Label, $why, $Lease.Process.Id)
    # taskkill sets the global $LASTEXITCODE, which a Library caller may exit with after the release, so it is kept
    # (r5b adversary A3), and left unset when it was unset, which strict mode cannot read (r5b pass 2 A2). taskkill's own
    # failure (heavy-slot ended after the HasExited check) never throws, even under $PSNativeCommandUseErrorActionPreference
    # (r5b pass 2 A3): the release stays warning-only and the holder fallback below still runs.
    $ended = $Lease.Process.HasExited
    if (-not $ended) {
        $last = Get-Variable -Name LASTEXITCODE -Scope Global -ErrorAction Ignore
        $keep = if ($last) { $last.Value } else { $null }
        try { $PSNativeCommandUseErrorActionPreference = $false } catch { }  # locked by an action: the catch below still holds (r11, 6b8295aa-A2)
        try { $null = & ([IO.Path]::Combine([string]$env:SystemRoot, 'System32', 'taskkill.exe')) /PID $Lease.Process.Id /T /F 2>&1 }
        catch { Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} taskkill of heavy-slot pid {1} failed ({2})' -f $Lease.Label, $Lease.Process.Id, $_.Exception.Message) }
        finally {
            # r13 (sol r12 B3): an action that made the global $LASTEXITCODE ReadOnly made this restore throw past the release,
            # and the holder fallback below never ran. The value the action locked is the one it exits with, so a restore that
            # cannot be made is a warning only.
            try { if ($last) { $global:LASTEXITCODE = $keep } else { Remove-Variable -Name LASTEXITCODE -Scope Global -WhatIf:$false -Confirm:$false -ErrorAction Ignore } }
            catch { Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} $LASTEXITCODE not restored after taskkill ({1})' -f $Lease.Label, ($_.Exception.Message -replace '\s+', ' ')) }
        }
    }
    # (The wrapper's pid cannot be reused here: $Lease.Process has held its handle since Process.Start.)
    # A holder that outlived heavy-slot is no longer in that tree: it is ended by its own identity (r3b adversary A2), and only
    # through the handle that identity was verified on, so a process that took its pid later is never stopped (r4 sol B4).
    # A holder named only by a partial ready file is ended the same way (r8, 3e68ca85-A1 sibling).
    $known = Get-HeavySlotStopHolder $Lease
    if (-not $known) {
        if ($ended) {
            # Its tree is gone, so a heavy-slot child that outlived the wrapper cannot be found from here (r7b, 01aa4e7b-A1 sibling).
            Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} heavy-slot wrapper pid {1} already ended: a heavy-slot child that outlived it is not stopped' -f $Lease.Label, $Lease.Process.Id)
        }
        return
    }
    $h = Get-HeavySlotProcess $known
    if ($h) {
        try { $h.Kill() } catch { Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} holder {1}@{2} not stopped ({3})' -f $Lease.Label, $known.Pid, $known.Ticks, $_.Exception.Message) }
        finally { if ($h -is [IDisposable]) { $h.Dispose() } }
    } elseif (Get-Process -Id $known.Pid -ErrorAction SilentlyContinue) {
        Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} holder {1}@{2} not stopped: reason=identity-unverified (the process under that pid is not the verified holder)' -f $Lease.Label, $known.Pid, $known.Ticks)
    }
}

function Get-HeavySlotExitCode($Lease) {
    # heavy-slot's exit code once it ends (up to 60 s), else 'unknown'.
    if ($Lease.Process.WaitForExit(60000)) { return [string]$Lease.Process.ExitCode }
    return 'unknown'
}

function Exit-HeavySlotHolderGone($Lease, [string]$When = 'after-ready') {
    # A ready file whose slot is gone (holder, slot owner or heavy-slot ended) is no admission: the machine slot is not
    # held, so nothing is built on it (Dot advisory pr-702). REQUEUE, like a marked 75. The stop file goes first, so a
    # holder that outlived its slot owner ends too, and it stays while that holder still runs.
    # The REQUEUE names a holder, so it needs one read from a complete ready record. Every caller without one has seen
    # heavy-slot end, so the record is read once more: a ready file still partial or empty then (its publication failed and
    # the holder's trap ended it) never established admission, and is COULD_NOT_CHECK (r4 sol B1, r5).
    if (-not $Lease.Holder) {
        $state = Get-HeavySlotHolderState $Lease
        if ($state -eq '') { Exit-HeavySlotUnreadable $Lease $Lease.BuildLabel 'ready file incomplete' }
        if ($state -ne 'gone') { Exit-HeavySlotUnreadable $Lease $Lease.BuildLabel }
    }
    Send-HeavySlotStop $Lease
    $code = Get-HeavySlotExitCode $Lease
    Send-HeavySlotLines $Lease
    foreach ($l in $Lease.Lines) { if ($l -match '^HEAVY-SLOT TICKET (\S+) ') { $Lease.Ticket = $Matches[1] } }
    Remove-HeavySlotLeaseFiles $Lease
    $gone = if ($Lease.Gone) { $Lease.Gone } else { 'holder-exited' }
    Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_REQUEUE label={0} code={1} ticket={2} reason={3}-{4} utc={5}' -f $Lease.BuildLabel, $code, $Lease.Ticket, $gone, $When, [DateTime]::UtcNow.ToString('o'))
    exit 3
}

function Exit-HeavySlotUnreadable($Lease, [string]$Label, [string]$What = 'ready file unreadable') {
    # A complete ready file whose holder or owner line does not parse cannot be checked, so it is never an admission
    # (r4 sol B1), and neither can one heavy-slot left partial or empty ($What 'ready file incomplete', r5): the stop file
    # is written, heavy-slot is given up to 60 s to end, and nothing is built. The stop file then stays unless the holder is
    # proven gone (r8, 3e68ca85-A1): a partial record may belong to a holder that is still publishing it.
    Send-HeavySlotStop $Lease
    $code = Get-HeavySlotExitCode $Lease
    Send-HeavySlotLines $Lease
    Remove-HeavySlotLeaseFiles $Lease
    Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_COULD_NOT_CHECK label={0} code=1 reason={1} (heavy-slot exit {2}) utc={3}' -f $Label, $What, $code, [DateTime]::UtcNow.ToString('o'))
    exit 1
}

function Enter-HeavySlot([hashtable]$Options, [string]$Label) {
    # Takes the machine heavy slot for one build unit (see the header). Returns the lease, or $null when it fails
    # open. A pin mismatch, a REQUEUE or a COULD-NOT-CHECK prints its typed line and exits here: nothing ran.
    $cmd = Resolve-HeavySlotCmd $Options
    if (-not $cmd) {
        Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=missing: heavy-slot.cmd not found; building WITHOUT the machine heavy slot (fail open)"
        return $null
    }
    # Every path below is resolved by Resolve-BuildGuardPath and composed with [IO.Path], which cannot throw or write an error
    # record on a missing drive or another provider (r9, adversary 2ab4b1b6-A1 and its siblings :853 :869 :887 :888).
    $shim = Resolve-BuildGuardPath $cmd -NeedParent
    if ($shim.Why) {
        Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=$cmd unusable: $($shim.Why); building WITHOUT the machine heavy slot (fail open)"
        return $null
    }
    $cli = [IO.Path]::Combine($shim.Parent, 'heavy-slot.ps1')
    # A CLI that cannot be hashed (held open without read sharing, say) is a pin mismatch, never an error record: the host would
    # write one to stderr synchronously, past the bounded writer, while an ancestor's slot is held (r8 adversary f99852cb-A1).
    $actual = 'missing'
    if (Test-Path -LiteralPath $cli -PathType Leaf -ErrorAction Ignore) {
        try { $actual = (Get-FileHash -LiteralPath $cli -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() }
        catch { $actual = 'unreadable(' + ($_.Exception.Message -replace '\s+', ' ') + ')' }
    }
    $expected = ([string]$Options.HeavySlotSha256).Trim().ToLowerInvariant()
    if ($actual -ne $expected) {
        # Nothing is taken yet, but an ancestor's slot may be held until this guard ends, so the line is bounded (r8, 3e68ca85-A2).
        Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_REFUSED label={0} reason=pin-mismatch expected={1} actual={2} cli={3} utc={4}' -f $Label, $expected, $actual, $cli, [DateTime]::UtcNow.ToString('o'))
        exit 3
    }
    # A relative -LockPath is resolved first, so its parent is never empty (adversary A1); one that cannot be is refused, with
    # nothing taken (r9). A lock file named heavy-slot would be the lease directory itself, read as a lock that is always busy
    # (r9 adversary 353c9a42-A1): the lease directory then takes another name.
    $lock = Get-BuildGuardLockPath $Options $Label
    $dir = [IO.Path]::Combine($lock.Parent, 'heavy-slot')
    if ([string]::Equals($dir, $lock.Full, [StringComparison]::OrdinalIgnoreCase)) { $dir = [IO.Path]::Combine($lock.Parent, 'heavy-slot.lease') }
    $exe = [Environment]::ProcessPath
    if (-not $exe) { $exe = (Get-Process -Id $PID).Path }
    # cmd expands %VAR% even inside quotes, so a path with '%' would not reach heavy-slot as given (r1 adversary A3).
    $pct = @($cmd, $dir, $exe) | Where-Object { ([string]$_).Contains('%') } | Select-Object -First 1
    if ($pct) {
        Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=$cmd path-has-percent: $pct; building WITHOUT the machine heavy slot (fail open)"
        return $null
    }
    # A lease directory that cannot be created is a shim that cannot be run: fail open like start-failed, never an error record
    # (r8 adversary f99852cb-A1 sibling). New-Item -Force made nothing and said nothing for a directory under a file, so cmd
    # failed its own redirect into the inherited stderr (r9): the directory must exist afterwards.
    $why = New-BuildGuardDirectory $dir
    if ($why) {
        Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} shim={1} lease-dir-failed: {2}; building WITHOUT the machine heavy slot (fail open)' -f $Label, $cmd, $why)
        return $null
    }
    $id = '{0}-{1}' -f $PID, [Guid]::NewGuid().ToString('N')
    $lease = @{
        Label = 'mlv:{0}:build' -f (Get-HeavySlotLane); BuildLabel = $Label
        Ready = [IO.Path]::Combine($dir, "$id.ready"); Stop = [IO.Path]::Combine($dir, "$id.stop"); ExitFile = [IO.Path]::Combine($dir, "$id.exit.json")
        Log = [IO.Path]::Combine($dir, "$id.log"); Lines = [System.Collections.Generic.List[string]]::new(); Process = $null; Ticket = ''; Reason = ''; LogUnread = $false; Holder = $null
        Owner = $null; Gone = ''; Tail = ''; StopHolder = $null; StopSent = $false
    }
    # heavy-slot ends its own wait (a marked 75) and its admit cap; this bound only catches a hung tool. It is numeric even
    # when a Library caller passes the minutes as a string, and monotonic, so a wall-clock step cannot move it. It is
    # computed BEFORE heavy-slot starts: a value it cannot hold (an overflow, NaN, text) is a typed refusal with nothing
    # launched, never a throw after the launch that skips the stop file (r2 adversary A3).
    # --wait and --eta are written in the invariant culture: heavy-slot reads only plain decimals ('0.5m'), never the
    # '0,5m' a de-DE culture writes. An eta that is not a finite number >= 0 is refused here too (r4 adversary A1).
    # heavy-slot reads both with ^\d+(\.\d+)?m$ into TimeSpan.FromMinutes, so a negative wait, or either one past
    # TimeSpan's range, fails its parser after the launch; both must be 0..$script:HeavySlotMaxMinutes (r5b adversary A2).
    $giveUp = $null
    $inv = [Globalization.CultureInfo]::InvariantCulture
    try {
        $waitMin = [double]$Options.HeavySlotWaitMinutes
        $etaMin = [double]$Options.HeavySlotEtaMinutes
        foreach ($d in @(@('HeavySlotWaitMinutes', $waitMin), @('HeavySlotEtaMinutes', $etaMin))) {
            if (-not ($d[1] -ge 0 -and $d[1] -le $script:HeavySlotMaxMinutes)) { throw ('{0} is not a number from 0 to {1}' -f $d[0], $script:HeavySlotMaxMinutes) }
        }
        # -0.0 is in range but formats as '-0' (r5b pass 2 A1); Abs makes it 0. Each value then goes out only in the form
        # heavy-slot's parser reads.
        $waitMin = [Math]::Abs($waitMin); $etaMin = [Math]::Abs($etaMin)
        $waitArg = $waitMin.ToString('0.##########', $inv) + 'm'
        $etaArg = $etaMin.ToString('0.##########', $inv) + 'm'
        foreach ($a in $waitArg, $etaArg) { if ($a -cnotmatch '^\d+(\.\d+)?m$') { throw "'$a' is not a duration heavy-slot reads" } }
        $giveUp = [TimeSpan]::FromMinutes($waitMin + $script:HeavySlotGiveUpSlackMinutes)
    } catch { $why = $_.Exception.Message }
    if ($null -eq $giveUp) {
        Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_REFUSED label={0} reason=invalid-option HeavySlotWaitMinutes={1} HeavySlotEtaMinutes={2} ({3}) utc={4}' -f $Label, $Options.HeavySlotWaitMinutes, $Options.HeavySlotEtaMinutes, ($why -replace '\s+', ' '), [DateTime]::UtcNow.ToString('o'))
        exit 2
    }
    # cmd opens the log for its own redirect and, when it cannot, writes that diagnostic to the stderr it inherited, past the
    # bounded writer (r9 adversary 353c9a42-A2: a lease directory that denies creating files). The guard makes the log first,
    # so a lease directory it cannot write fails open here with nothing launched.
    try { [IO.File]::Open($lease.Log, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite).Dispose() } catch {
        $e = $_.Exception
        if ($e -is [System.Management.Automation.MethodInvocationException] -and $e.InnerException) { $e = $e.InnerException }
        Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} shim={1} lease-log-failed: {2}; building WITHOUT the machine heavy slot (fail open)' -f $Label, $cmd, ($e.Message -replace '\s+', ' '))
        return $null
    }
    # cmd quoting, as heavy-slot itself starts a batch child: each argument double-quoted. Its stdout and stderr go to a
    # file, never a pipe, so nothing it writes while it holds the slot can block it (adversary A4).
    $quoted = @($cmd, 'run', '--class', 'shared', '--label', $lease.Label, '--eta', $etaArg,
        '--wait', $waitArg, '--exit-file', $lease.ExitFile,
        '--', $exe, '-NoProfile', '-NonInteractive', '-EncodedCommand', (New-HeavySlotHolder $lease.Ready $lease.Stop)) |
        ForEach-Object { '"' + ([string]$_ -replace '"', '""') + '"' }
    $psi = [System.Diagnostics.ProcessStartInfo]::new($env:ComSpec)
    $psi.UseShellExecute = $false
    # cmd's own streams are pipes whose read ends the guard closes at once (r9, 353c9a42-A2 class): a diagnostic of cmd itself
    # then fails at once, never waiting on a stdout or stderr the guard inherited. What heavy-slot writes goes to the log.
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.Arguments = '/d /v:off /s /c "' + ($quoted -join ' ') + ' >"' + $lease.Log + '" 2>&1"'
    $psi.Environment['HEAVY_SLOT_EXPECT_SHA256'] = $expected
    $psi.Environment['HEAVY_SLOT_EXPECT_KEEPER'] = '1'
    # A guarded build always takes the canonical machine slot: an inherited mutex name, slot count, state dir or keeper
    # owner would let it queue on other slots, or beside the canonical holder (r5b adversary A1).
    foreach ($v in 'HEAVY_SLOT_NAME', 'HEAVY_SLOT_COUNT', 'HEAVY_SLOT_DIR', 'HEAVY_SLOT_KEEPER_OWNER') { $null = $psi.Environment.Remove($v) }
    # heavy-slot passes an inherited HEAVY_SLOT_HELD through for a live ancestor that holds ANY slot of the set, whatever its
    # slot count, so an ancestor run with HEAVY_SLOT_COUNT=2 on slot 1 would admit this build beside the canonical holder. The
    # holder is kept only when its held record proves slot 0 of the one-slot set; otherwise heavy-slot takes slot 0 for this
    # build (r7b adversary 01aa4e7b-A2). The line is logged after the launch, so the hung-tool clock still starts first.
    $heldDropped = ''
    if ($env:HEAVY_SLOT_HELD) {
        $notCanonical = Test-HeavySlotHeldCanonical $env:HEAVY_SLOT_HELD
        if ($notCanonical) {
            foreach ($v in 'HEAVY_SLOT_HELD', 'HEAVY_SLOT_HELD_START', 'HEAVY_SLOT_HELD_NAME') { $null = $psi.Environment.Remove($v) }
            $heldDropped = 'heavy-slot: inherited HEAVY_SLOT_HELD={0} not passed through ({1}); taking the canonical slot' -f $env:HEAVY_SLOT_HELD, $notCanonical
        }
    }
    # The hung-tool clock starts before the launch and before any diagnostic line (r6 sol B1).
    $waitClock = [System.Diagnostics.Stopwatch]::StartNew()
    try { $lease.Process = [System.Diagnostics.Process]::Start($psi) } catch {
        Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=$cmd start-failed: $($_.Exception.Message); building WITHOUT the machine heavy slot (fail open)"
        return $null
    }
    $admitted = $false
    # The cleanup try begins at the first statement after the launch, so nothing between them can skip its finally.
    try {
        foreach ($s in @($lease.Process.StandardOutput, $lease.Process.StandardError)) { try { $s.Close() } catch { } }
        # The wait printed is the one heavy-slot was given, never the raw option (fable r6b note).
        Write-BuildGuardLog ('heavy-slot: waiting label={0} wait={1} exit-file={2} shim={3}' -f $lease.Label, $waitArg, $lease.ExitFile, $cmd)
        if ($heldDropped) { Write-BuildGuardLog $heldDropped }
        while ($true) {
            Send-HeavySlotLines $lease
            # The ready file admits only while the holder it names still runs (checked after the file is seen, and again below).
            if (Test-Path -LiteralPath $lease.Ready -ErrorAction Ignore) {
                $holder = Get-HeavySlotHolderState $lease
                if ($holder -eq 'alive') { break }
                if ($holder -eq 'unreadable') { Exit-HeavySlotUnreadable $lease $Label }
                if ($holder -eq 'gone' -or $lease.Process.HasExited) { Exit-HeavySlotHolderGone $lease }
            }
            $why = ''
            $code = 1
            if ($lease.Process.HasExited) {
                if (Test-Path -LiteralPath $lease.Ready -ErrorAction Ignore) { Exit-HeavySlotHolderGone $lease }
                Send-HeavySlotLines $lease
                $code = $lease.Process.ExitCode
                if ($code -eq 9009) {
                    # cmd's "is not recognized": the shim started but its own interpreter did not (r1 adversary A2).
                    Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=$cmd not-startable: exit 9009 before admission; building WITHOUT the machine heavy slot (fail open)"
                    Remove-HeavySlotFiles $lease
                    return $null
                }
                $why = Test-HeavySlotMarker $lease $code
                if (-not $why) {
                    Remove-HeavySlotFiles $lease
                    # The stop goes before the typed line here too (r8, 3e68ca85-A2 sibling).
                    if (Test-HeavySlotStopNeeded $lease) { Send-HeavySlotStop $lease }
                    Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_REQUEUE label={0} code={1} ticket={2} reason={3} utc={4}' -f $Label, $code, $lease.Ticket, ($lease.Reason -replace '\s+', ' '), [DateTime]::UtcNow.ToString('o'))
                    exit 3
                }
                if ($code -lt 75 -or $code -gt 77) { $why = 'heavy-slot ended before admitting the build' }
            } elseif ($waitClock.Elapsed -ge $giveUp) {
                $why = 'no admission and no exit within the wait plus 15 min'
            }
            if ($why) {
                if ($code -eq 0) { $code = 1 }
                # The stop goes BEFORE the typed line (r6 sol B1): a stdout nobody drains may block that line, never the slot.
                if (Test-HeavySlotStopNeeded $lease) { Send-HeavySlotStop $lease }
                Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_COULD_NOT_CHECK label={0} code={1} reason={2} utc={3}' -f $Label, $code, $why, [DateTime]::UtcNow.ToString('o'))
                exit $code
            }
            Start-Sleep -Milliseconds 250
        }
        Send-HeavySlotLines $lease
        # A line still unfinished at admission is given up to 2 s to end (r4 adversary A2).
        for ($i = 0; $lease.Tail -and $i -lt 20; $i++) { [Threading.Thread]::Sleep(100); Send-HeavySlotLines $lease }
        if ((Get-HeavySlotHolderState $lease) -ne 'alive') { Exit-HeavySlotHolderGone $lease }
        if ($lease.LogUnread -or $lease.Tail) {
            # Its lines (a PIN MISMATCH among them) cannot be checked, so the slot is given back and nothing is built.
            $what = if ($lease.LogUnread) { 'heavy-slot log unreadable at admission' } else { 'heavy-slot log line unfinished at admission' }
            Exit-HeavySlot $lease
            Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_COULD_NOT_CHECK label={0} code=1 reason={1} utc={2}' -f $Label, $what, [DateTime]::UtcNow.ToString('o'))
            exit 1
        }
        # heavy-slot only warns when the script it runs is not the pin (it may have changed after the hash above), so
        # its own PIN MISMATCH line refuses here too, after the slot is given back (r1 adversary A4).
        if (@($lease.Lines | Where-Object { $_ -match 'PIN MISMATCH' }).Count -gt 0) {
            Exit-HeavySlot $lease
            Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_REFUSED label={0} reason=pin-mismatch-at-run cli={1} utc={2}' -f $Label, $cli, [DateTime]::UtcNow.ToString('o'))
            exit 3
        }
        Write-BuildGuardLog ('heavy-slot: admitted label={0} held={1}' -f $lease.Label, ((Get-Content -LiteralPath $lease.Ready -Raw -ErrorAction SilentlyContinue) -replace '\s', ''))
        $admitted = $true
        return $lease
    } finally {
        # Every way out before admission (exit, throw, Ctrl+C) leaves the stop file while heavy-slot still runs, even when a
        # ready file appeared after the last check, so its holder ends at once instead of holding the machine slot
        # (adversary A2; r1 adversary A1). A holder that outlived heavy-slot (its slot owner killed) gets it too.
        # It is written last, after any lease-file cleanup above, unless a stop was already sent (that one stays until its
        # holder is proven gone, Remove-HeavySlotFiles), and nothing deletes it on this path.
        if (-not $admitted -and -not $lease['StopSent'] -and (Test-HeavySlotStopNeeded $lease)) { Send-HeavySlotStop $lease }
    }
}

function Exit-HeavySlot($Lease, [switch]$ActionStarted) {
    # Gives the slot back: the holder sees the stop file and exits, so heavy-slot releases. The build's verdict
    # never depends on this; a release that does not end cleanly is a WARN only.
    if (-not $Lease) { return }
    # The holder (or heavy-slot) ended before the stop file: part of the build ran without the machine slot. The build
    # is not failed for it (the slot is a throttle, not a correctness lock), but the line lets a receipt call it unprotected.
    # Only a release after the action started says so (r10, sol r9 B1): every other one ran nothing, so a lost holder there
    # is the before-action REQUEUE line, and the caller's own refusal line and exit code follow it unchanged.
    $lost = $Lease.Process.HasExited -or (Get-HeavySlotHolderState $Lease) -ne 'alive'
    Send-HeavySlotStop $Lease
    if ($lost -and $ActionStarted) {
        $line = 'HEAVY_SLOT_LOST_DURING_ACTION label={0} code={1} utc={2}' -f $Lease.BuildLabel, (Get-HeavySlotExitCode $Lease), [DateTime]::UtcNow.ToString('o')
        Write-BuildGuardLog $line
        # Warning-only (r6b adversary A2) and bounded (r8, 3e68ca85-A2): a stdout that is closed, gone or undrained never turns a
        # finished action into a throw or a wait.
        Write-BuildGuardTyped $line
    }
    $code = 'unknown'
    if ($Lease.Process.WaitForExit(60000)) {
        $code = [string]$Lease.Process.ExitCode
        Send-HeavySlotLines $Lease
        if ($lost) { Write-BuildGuardLog ('heavy-slot: ended before release label={0}' -f $Lease.Label) }
        elseif ($Lease.Process.ExitCode -ne 0) { Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} heavy-slot ended with {1} after release' -f $Lease.Label, $Lease.Process.ExitCode) }
        else { Write-BuildGuardLog ('heavy-slot: released label={0}' -f $Lease.Label) }
        Remove-HeavySlotLeaseFiles $Lease
    } else {
        # The stop file stays: a holder that has not been scheduled yet must still see it (adversary A3).
        Send-HeavySlotLines $Lease
        Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} heavy-slot did not end within 60 s of release; the stop file stays at {1}' -f $Lease.Label, $Lease.Stop)
    }
    if ($lost -and -not $ActionStarted) {
        # The same line Exit-HeavySlotHolderGone prints before the action, written after the release like the refusals are.
        foreach ($l in $Lease.Lines) { if ($l -match '^HEAVY-SLOT TICKET (\S+) ') { $Lease.Ticket = $Matches[1] } }
        $gone = if ($Lease.Gone) { $Lease.Gone } else { 'holder-exited' }
        Write-BuildGuardTyped ('BUILD_HEAVY_SLOT_REQUEUE label={0} code={1} ticket={2} reason={3}-before-action utc={4}' -f $Lease.BuildLabel, $code, $Lease.Ticket, $gone, [DateTime]::UtcNow.ToString('o'))
    }
}

function Invoke-BuildGuard {
    param(
        [Parameter(Mandatory = $true)] [scriptblock]$Action,
        [string]$Label = 'build',
        [int]$RequestedJobs = 0,
        [string]$ExemptReason = '',
        [switch]$NoSlot,
        [hashtable]$Options = @{}
    )
    # r9 (adversary 46f83a19-A1, A2): the guard's own cmdlets run with no caller parameter defaults (a caller's
    # 'Get-Content:ErrorAction'='Continue' outranked every helper's Stop scope, so an error record reached the host) and no
    # verbose, debug, warning or information records (-Verbose on the command line made the CIM probes write verbose records the
    # host renders synchronously, past the bounded writers). Both are local to this call; the action gets the caller's back.
    # r10 (adversary a2bdda36-A1): nor WhatIf or a confirm prompt (an inherited $WhatIfPreference=$true made the stop file's
    # Set-Content a silent no-op, so the holder kept the slot after the release).
    # r11 (sol r10 B1): a caller's AllScope preference or defaults is ONE variable in every scope, so handing the action the
    # caller's value also handed it to every release after the action: the stop file's Set-Content and the lock and lease-file
    # deletes became successful WhatIf no-ops and the holder kept the machine slot. So the guard's own values are set again
    # after the action, before any release, and the caller's come back last, once both slots are given back. Both are
    # dot-sourced (they assign in the scope that runs them) and are plain assignments, never a cmdlet (r10, a2bdda36-A2).
    # Neither ever throws (r11 adversary 08736d55-A2): a variable the action made ReadOnly or Constant is skipped, never a
    # throw before a release; every release write and delete is a .NET call, never a cmdlet (r13). The guard's values are
    # set inside the try whose finally gives the caller's back, so no throw after them leaves them (08736d55-A1). Values the
    # guard cannot set on the way in (the caller made one ReadOnly) are a throw before anything is taken, never a run under
    # the caller's (6b8295aa-A1). $ErrorActionPreference comes back too: the guard's helpers set it to Stop, and an AllScope
    # one is the caller's own variable (6b8295aa-A3).
    $callerDefaults = $PSDefaultParameterValues
    $callerPrefs = @{ VerbosePreference = $VerbosePreference; DebugPreference = $DebugPreference; WarningPreference = $WarningPreference
        InformationPreference = $InformationPreference; WhatIfPreference = $WhatIfPreference; ConfirmPreference = $ConfirmPreference
        ErrorActionPreference = $ErrorActionPreference }
    # r13 (sol r12 B2): an action that locks the restored AllScope defaults (ReadOnly) makes the assignment below a no-op, and
    # every release cmdlet ran under them ('Set-Content:PassThru' wrote 'stop' into the action's output, 'Remove-Item:Exclude'
    # kept the lock and lease files). A ReadOnly variable still holds a dictionary that can be written, so its own 'Disabled'
    # switch turns every default off for the release, and the caller's value of that key comes back with the rest.
    $defaultsOff = $null
    $guardPrefs = {
        trap { continue }
        $PSDefaultParameterValues = @{}
        if (-not $defaultsOff -and $PSDefaultParameterValues -is [System.Collections.IDictionary] -and $PSDefaultParameterValues.Count) {
            $defaultsOff = @{ Map = $PSDefaultParameterValues; Had = $PSDefaultParameterValues.Contains('Disabled'); Value = $PSDefaultParameterValues['Disabled'] }
            $PSDefaultParameterValues['Disabled'] = $true
        }
        $VerbosePreference = 'SilentlyContinue'; $DebugPreference = 'SilentlyContinue'
        $WarningPreference = 'SilentlyContinue'; $InformationPreference = 'SilentlyContinue'
        $WhatIfPreference = $false; $ConfirmPreference = 'None'
    }
    $callerPrefsBack = {
        trap { continue }
        $VerbosePreference = $callerPrefs.VerbosePreference; $DebugPreference = $callerPrefs.DebugPreference
        $WarningPreference = $callerPrefs.WarningPreference; $InformationPreference = $callerPrefs.InformationPreference
        $WhatIfPreference = $callerPrefs.WhatIfPreference; $ConfirmPreference = $callerPrefs.ConfirmPreference
        $ErrorActionPreference = $callerPrefs.ErrorActionPreference
        $PSDefaultParameterValues = $callerDefaults
        if ($defaultsOff) {
            if ($defaultsOff.Had) { $defaultsOff.Map['Disabled'] = $defaultsOff.Value } else { $defaultsOff.Map.Remove('Disabled') }
            $defaultsOff = $null
        }
    }
    # The machine heavy slot is the OUTERMOST lock of a build: the memory floor and the build slot below nest inside it,
    # and the outer finally gives it back on every way out, a throw a Library caller catches included (adversary A2).
    $heavy = $null
    # Set only on the line before the action runs: a release reached before it never reports a loss "during" it (r10).
    $actionStarted = $false
    try {
        . $guardPrefs
        if ($PSDefaultParameterValues.Count -or $WhatIfPreference -or $ConfirmPreference -ne 'None' -or $VerbosePreference -ne 'SilentlyContinue' -or
            $DebugPreference -ne 'SilentlyContinue' -or $WarningPreference -ne 'SilentlyContinue' -or $InformationPreference -ne 'SilentlyContinue') {
            throw 'build-guard: a caller preference or $PSDefaultParameterValues is read-only, so the guard cannot run under its own values; nothing was taken'
        }
        $o = @{}
        foreach ($k in $script:BuildGuardDefaults.Keys) { $o[$k] = $script:BuildGuardDefaults[$k] }
        foreach ($k in $Options.Keys) { $o[$k] = $Options[$k] }
        $exempt = Get-BuildGuardExemptReason $ExemptReason
        if (-not $exempt) { $exempt = Get-BuildGuardExemptReason $env:MLV_BUILD_GUARD_EXEMPT }
        if ($exempt) {
            $line = 'BUILD_GUARD_EXEMPT label={0} reason={1} utc={2} pid={3}' -f $Label, $exempt, [DateTime]::UtcNow.ToString('o'), $PID
            Write-BuildGuardLog $line
            try {
                # Through the one resolver (r9): a log path that cannot be used is logged, never an error record.
                $log = Resolve-BuildGuardPath $o.ExemptLogPath -NeedParent
                if ($log.Why) { throw $log.Why }
                $why = New-BuildGuardDirectory $log.Parent
                if ($why) { throw $why }
                Add-BuildGuardLogLine -Path $log.Full -Line $line -Encoding ([Text.Encoding]::ASCII)
            } catch { Write-BuildGuardLog "exemption log not written: $($_.Exception.Message)" }
        }
        if ($NoSlot) {
            # -NoSlot is for a caller that builds nothing (-PrintJobs): it takes neither slot, so the bypass is a typed line a
            # receipt can classify, never a silent one (r4 adversary A1).
            Write-BuildGuardLog ('BUILD_GUARD_NO_SLOT label={0} reason=-NoSlot: no machine heavy slot and no build slot taken; for callers that build nothing' -f $Label)
        }
        if (-not $NoSlot) {
            # The lock path is resolved once, before anything is taken, and every later use is the full path (r9, 2ab4b1b6-A1):
            # one that cannot be is a typed refusal here, never a throw the host renders past the bounded writer.
            $o.LockPath = (Get-BuildGuardLockPath $o $Label).Full
            $heavy = Enter-HeavySlot -Options $o -Label $Label
        }
        $started = [DateTime]::UtcNow
        $deadline = $started.AddMinutes($o.DeadlineMinutes)
        # r12 (sol r11 B2): a positive deadline bounds the whole admission, the last look included. A deadline of 0 or less
        # is one look, admitted or refused, as before.
        $bounded = [double]$o.DeadlineMinutes -gt 0
        $ticket = $null
        $mem = @{ PhysGB = 0.0; CommitGB = 0.0 }
        while ($true) {
            $mem = Get-BuildGuardMemory -Options $o
            $memOk = [bool]$exempt -or ($mem.PhysGB -ge $o.MinFreePhysGB -and $mem.CommitGB -ge $o.MinFreeCommitGB)
            $held = ''
            if ($memOk) {
                if ($NoSlot) { break }
                # Never past the deadline (r12, sol r11 B2 sibling): the slot mutex waits at most what is left of it.
                $mutexWaitMs = 15000
                if ($bounded) { $mutexWaitMs = [int][math]::Max(0, [math]::Min(15000, [math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalMilliseconds))) }
                $slot = Invoke-BuildSlotAcquire -Options $o -Label $Label -MutexWaitMs $mutexWaitMs
                if ($slot.Acquired) { $ticket = $slot.Ticket; break }
                if ($slot['Refused']) {
                    # The heavy slot goes back before the typed refusal (r6 sol B1), as at the deadline below.
                    Exit-HeavySlot $heavy
                    $heavy = $null
                    Exit-BuildGuardPathRefused $Label 'LockPath' $o.LockPath $slot.Refused
                }
                $held = $slot.Held
            }
            if ([DateTime]::UtcNow -ge $deadline) {
                $waited = [int]([DateTime]::UtcNow - $started).TotalSeconds
                # The machine heavy slot is given back BEFORE the typed refusal (r6 sol B1): a stdout nobody drains may block
                # that line, never the slot. Nothing is built after it; $heavy is cleared only once the release has run.
                Exit-HeavySlot $heavy
                $heavy = $null
                if (-not $memOk) {
                    Write-BuildGuardTyped ('BUILD_MEM_REFUSED label={0} freePhysGB={1} freeCommitGB={2} need={3}/{4} waitedSec={5} utc={6}' -f $Label, $mem.PhysGB, $mem.CommitGB, $o.MinFreePhysGB, $o.MinFreeCommitGB, $waited, [DateTime]::UtcNow.ToString('o'))
                } else {
                    Write-BuildGuardTyped ('BUILD_SLOT_BUSY label={0} heldBy=[{1}] waitedSec={2} utc={3}' -f $Label, $held, $waited, [DateTime]::UtcNow.ToString('o'))
                }
                exit 3
            }
            # r11 (sol r10 B2): never past the deadline, on the memory wait and the local-slot-busy path alike: a -PollSeconds
            # longer than what is left (3600 against 0.6 s) slept that long with the machine heavy slot held. The next look is
            # at the deadline, which gives the slot back and refuses above. A NaN or negative poll is no sleep.
            $sleepMs = [math]::Min([double]$o.PollSeconds * 1000,[math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalMilliseconds))
            if (-not ($sleepMs -gt 0)) { $sleepMs = 0 }
            Start-Sleep -Milliseconds ([int][math]::Min($sleepMs, [int]::MaxValue))
        }
        try {
            $choice = Get-BuildGuardJobs -Options $o -SelfPid $PID
            $jobs = $choice.Jobs
            if ($RequestedJobs -gt 0) { $jobs = [math]::Min($jobs, $RequestedJobs) }
            # The action starts only under a live heavy slot (r2 adversary A1): a slot lost while waiting for memory, the
            # build slot or the process census is a REQUEUE with nothing built, not a build classified as unprotected
            # afterwards. The check is the last step before the action (r3b adversary A1: the census can take seconds).
            # Every caller (command mode, build-release, qscreen-probe, the attr-3 assembler) reaches its action through here.
            # The admitted line is written between two checks: a write to a stderr nobody drains can block for as long as it
            # likes, so the slot is checked again after it, with nothing left between that check and the action (r4 sol B3).
            for ($check = 1; $check -le 2; $check++) {
                if ($heavy -and (Get-HeavySlotHolderState $heavy) -ne 'alive') {
                    $lost = $heavy
                    $heavy = $null
                    # The stop is sent even when the build-slot release throws (r6b adversary A1): $heavy is already
                    # cleared, so the outer finally would not send it.
                    try { Remove-BuildSlot $ticket } finally { $ticket = $null; Exit-HeavySlotHolderGone $lost 'before-action' }
                }
                # r12 (sol r11 B2, adversary d4a586f2-A1): the one deadline check after every way in, at the gate itself.
                # The clamped sleep wakes at the deadline, so a look that then passed (memory back, a slot freed, a slow
                # acquisition) broke out of the wait and started the action after expiry, and so did a slow census. Past a
                # positive deadline the build slot goes back, then the machine slot, then the typed refusal; nothing is built.
                if ($bounded -and [DateTime]::UtcNow -ge $deadline) {
                    try { Remove-BuildSlot $ticket } finally { $ticket = $null }
                    Exit-HeavySlot $heavy
                    $heavy = $null
                    Write-BuildGuardTyped ('BUILD_SLOT_BUSY label={0} heldBy=[deadline-expired-before-admission] waitedSec={1} utc={2}' -f $Label, [int]([DateTime]::UtcNow - $started).TotalSeconds, [DateTime]::UtcNow.ToString('o'))
                    exit 3
                }
                if ($check -eq 1) {
                    Write-BuildGuardLog ('admitted label={0} jobs={1} reason={2} peers=[{3}] freePhysGB={4} freeCommitGB={5} slot={6}' -f $Label, $jobs, $choice.Reason, ($choice.Peers -join ','), $mem.PhysGB, $mem.CommitGB, $(if ($ticket) { 'held' } else { 'none' }))
                }
            }
            $actionStarted = $true
            & {
                param($Jobs)
                # Plain assignments, never a cmdlet: one run with the caller's defaults just restored ('Set-Variable:PassThru',
                # say) would write into the action's output (r10, adversary a2bdda36-A2).
                . $callerPrefsBack
                & $Action $Jobs
            } $jobs
        } finally {
            # The guard's own values again before the build-slot release: an AllScope caller value restored above is the one
            # this scope reads too, and so is anything the action set in one (r11).
            . $guardPrefs
            Remove-BuildSlot $ticket
        }
    } finally {
        # Queued guard lines go out last, once nothing is held (r7b, adversary 8d0c73bd-A1), the typed lines with them (r8).
        # The caller's preferences come back after both (r11): for an AllScope one this is the caller's own variable.
        . $guardPrefs
        try {
            try { Exit-HeavySlot $heavy -ActionStarted:$actionStarted } finally { Complete-BuildGuardOutput }
        } finally { . $callerPrefsBack }
    }
}

if ($Library) { return }

function Exit-BuildGuardError($ErrorRecord, [string]$Label) {
    # r9 backstop (adversary 2ab4b1b6-A1 class): in command mode nothing caught a throw out of Invoke-BuildGuard, and the host
    # rendered it to stderr synchronously, past the bounded writer, while an ancestor's heavy slot may be held. A Library caller
    # still gets the throw. Invoke-BuildGuard's finally has given back every slot of its own by now, so the throw becomes one
    # typed line, bounded like the others, and exit 1.
    if ($ErrorRecord.Exception -is [System.Management.Automation.PipelineStoppedException]) { throw $ErrorRecord }
    $e = $ErrorRecord.Exception
    if ($e -is [System.Management.Automation.MethodInvocationException] -and $e.InnerException) { $e = $e.InnerException }
    Write-BuildGuardTyped ('BUILD_GUARD_ERROR label={0} code=1 reason={1}: {2} utc={3}' -f $Label, $e.GetType().Name, ($e.Message -replace '\s+', ' '), [DateTime]::UtcNow.ToString('o'))
    Complete-BuildGuardOutput
    exit 1
}

$guardOptions = @{
    MinFreePhysGB     = $MinFreePhysGB
    MinFreeCommitGB   = $MinFreeCommitGB
    PollSeconds       = $PollSeconds
    DeadlineMinutes   = $DeadlineMinutes
    LockExpiryMinutes = $LockExpiryMinutes
    LockPath          = $LockPath
    MutexName         = $MutexName
    ExemptLogPath     = $ExemptLogPath
    MemoryProbeFile   = $MemoryProbeFile
    ProcessTableFile  = $ProcessTableFile
    HeavySlotCmd         = $HeavySlotCmd
    HeavySlotSha256      = $HeavySlotSha256
    HeavySlotEtaMinutes  = $HeavySlotEtaMinutes
    HeavySlotWaitMinutes = $HeavySlotWaitMinutes
}
if ($PrintJobs) {
    try { Invoke-BuildGuard -Label $Label -RequestedJobs $RequestedJobs -ExemptReason $ExemptReason -NoSlot -Options $guardOptions -Action {
        param($Jobs)
        Write-Output "-j$Jobs"
    } } catch { Exit-BuildGuardError $_ $Label }
    exit 0
}

# The two usage errors are bounded like every guard line (r8, 3e68ca85-A2 sibling): an ancestor's heavy slot may be held until
# this guard ends.
if (-not $Command.Trim()) {
    Write-BuildGuardStderr 'build-guard: no build command given. Use -PrintJobs, or: build-guard.ps1 -Command "<fragment with {JOBS}>"'
    Complete-BuildGuardStderr
    exit 2
}

# The verdict is the FIRST failure in the fragment, which is a sequence (BUILD-GUARD BG-01, BG-02, BG-03):
#   - a native command that exits non-zero stops the fragment, and its own code is returned (9 stays 9), so
#     a failed qmake cannot be hidden by a later make that exits 0 (PSNativeCommandUseErrorActionPreference);
#   - a command that cannot start, an error record or a throw stops the fragment and exits 1 (these never
#     touch LASTEXITCODE, so the guard used to exit 0);
#   - an error record written with an explicit -ErrorAction Continue does not stop the fragment, but it is
#     collected by -ErrorVariable and still fails the verdict. This fails closed: a record that was
#     silenced (SilentlyContinue) or caught by a try/catch in the fragment is collected too. Only
#     -ErrorAction Ignore drops a record.
#   - a native failure the fragment caught, or ran in a child scope or under SilentlyContinue, is collected
#     the same way, so a later command that exits 0 cannot overwrite it (BG-04): the verdict is the first
#     failing native code. A fragment that turns native errors off ($PSNativeCommandUseErrorActionPreference
#     = $false, or Ignore) leaves no record, and only its final code is judged.
# Every way out of the fragment reaches Resolve-BuildGuardFragmentVerdict: falling off the end, a failing
# native command, a terminating error, and an 'exit', 'break' or 'continue' that leaves the fragment. That
# last group cannot be caught, so the verdict is taken in a finally and replaces the fragment's own exit
# (the slot is still released by Invoke-BuildGuard's finally). Each 'exit' statement in the fragment is
# rewritten to record its code first; a fragment that leaves any other way records nothing and fails.
# Native stderr (compiler warnings, with or without 2>&1) is not an error record here and fails nothing.
function Set-BuildGuardFragmentExit($Code) {
    # Records the code of an 'exit' in the fragment, then hands it back to that exit unchanged.
    $script:BuildGuardFragmentExit = [int]$Code
    return $script:BuildGuardFragmentExit
}

function ConvertTo-BuildGuardFragment([string]$Text) {
    # 'exit X' becomes 'exit (Set-BuildGuardFragmentExit (X))' and a bare 'exit' records 0, which is what
    # pwsh exits with. Text that does not parse, before or after, is returned as given: it then fails as a
    # parse error, or leaves unrecorded, and both are failures.
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$parseErrors)
    if (@($parseErrors).Count -gt 0) { return $Text }
    $edits = @(foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ExitStatementAst] }, $true)) {
            if ($node.Pipeline) {
                [pscustomobject]@{ At = $node.Pipeline.Extent.EndOffset; Order = 0; Text = '))' }
                [pscustomobject]@{ At = $node.Pipeline.Extent.StartOffset; Order = 1; Text = '(Set-BuildGuardFragmentExit (' }
            } else {
                [pscustomobject]@{ At = $node.Extent.EndOffset; Order = 0; Text = ' (Set-BuildGuardFragmentExit 0)' }
            }
        })
    if ($edits.Count -eq 0) { return $Text }
    $rewritten = $Text
    foreach ($edit in ($edits | Sort-Object -Property @{ Expression = 'At'; Descending = $true }, Order)) {
        $rewritten = $rewritten.Insert($edit.At, $edit.Text)
    }
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput($rewritten, [ref]$tokens, [ref]$parseErrors)
    if (@($parseErrors).Count -gt 0) { return $Text }
    return $rewritten
}

function Resolve-BuildGuardFragmentVerdict {
    # The one verdict for every way out of the fragment. $Code is the fragment's own code: the failing native
    # command's, the one its 'exit' gave, or 0. Every native failure is observed from its record, even one the
    # fragment caught or ran in a child scope (BG-04), and the first one is remembered:
    #   - a non-zero code is kept (9 stays 9, 'exit 5' stays 5);
    #   - a zero code with a native failure observed becomes that FIRST native code (a later success, a catch,
    #     'exit $LASTEXITCODE' or 'return' cannot overwrite it), reason native-failure-overwritten;
    #   - a zero code with only other error records becomes 1.
    # BUILD_GUARD_COMMAND_FAILED is printed for any record the code does not already report. The code reports
    # one record only: the last native failure, when its exit code is the code.
    param([string]$Label, [string]$Via, [int]$Code, $Collected, $Failure, [string]$Unrecorded = '')
    $native = [System.Management.Automation.NativeCommandExitException]
    $records = [System.Collections.Generic.List[object]]::new()
    $natives = [System.Collections.Generic.List[object]]::new()
    $all = @(@($Collected) | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })
    if ($Failure) { $all += $Failure }
    foreach ($item in $all) {
        $exception = if ($item -is [System.Management.Automation.ErrorRecord]) { $item.Exception } else { $item }
        $list = $records
        if ($exception -is $native) { $list = $natives }
        $seen = $false
        foreach ($r in $list) { if ([object]::ReferenceEquals($r, $exception)) { $seen = $true } }
        if (-not $seen) { $list.Add($exception) }
    }
    $unreported = $natives.Count
    if ($Code -ne 0 -and $natives.Count -gt 0 -and $natives[$natives.Count - 1].ExitCode -eq $Code) { $unreported-- }
    if ($records.Count -eq 0 -and $unreported -eq 0 -and -not $Unrecorded) { return $Code }
    $overwritten = $Code -eq 0 -and $natives.Count -gt 0
    $verdict = if ($Code -ne 0) { $Code } elseif ($overwritten) { [int]$natives[0].ExitCode } else { 1 }
    if ($overwritten) {
        $reason = 'native-failure-overwritten: ' + $natives[0].GetType().Name + ': ' + $natives[0].Message
    } elseif ($records.Count -gt 0) {
        $reason = $records[0].GetType().Name + ': ' + $records[0].Message
    } elseif ($unreported -gt 0) {
        $reason = 'earlier-native-failure: ' + $natives[0].GetType().Name + ': ' + $natives[0].Message
    } else {
        $reason = $Unrecorded
    }
    Write-BuildGuardStderr (('BUILD_GUARD_COMMAND_FAILED label={0} lastNativeExit={1} errors={2} exit={3} via={4} reason={5}' -f $Label, [int]$global:LASTEXITCODE, ($records.Count + $unreported), $verdict, $Via, ($reason -replace '\s+', ' ')))
    return $verdict
}

if ($PSVersionTable.PSVersion -lt [version]'7.4') {
    Write-BuildGuardStderr "build-guard: command mode needs PowerShell 7.4+ (native exit codes stop the fragment); this is $($PSVersionTable.PSVersion)"
    Complete-BuildGuardStderr
    exit 2
}
$script:BuildGuardCommandExit = 0
# The action catches every way out of the fragment, so the backstop sees only an admission throw (r9).
try { Invoke-BuildGuard -Label $Label -RequestedJobs $RequestedJobs -ExemptReason $ExemptReason -Options $guardOptions -Action {
    param($Jobs)
    $fragment = ConvertTo-BuildGuardFragment ($Command -replace '\{JOBS\}', [string]$Jobs)
    $script:BuildGuardFragmentExit = $null
    $global:LASTEXITCODE = 0
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $true
    $fragmentErrors = $null
    $failure = $null
    $code = 0
    $via = ''   # stays empty only when the fragment left by exit, break or continue
    try {
        & ([scriptblock]::Create("[CmdletBinding()] param()`n" + $fragment)) -ErrorVariable fragmentErrors
        $via = 'end'
        $code = [int]$global:LASTEXITCODE
    } catch [System.Management.Automation.NativeCommandExitException] {
        $via = 'native-exit'
        $code = [int]$_.Exception.ExitCode
        $failure = $_
        Write-BuildGuardLog ('fragment stopped at its first failing native command: exit={0} {1}' -f $_.Exception.ExitCode, $_.Exception.Message)
    } catch {
        $via = 'error'
        $failure = $_
    } finally {
        $left = -not $via
        $unrecorded = ''
        if ($left -and $null -ne $script:BuildGuardFragmentExit) {
            $via = 'exit'
            $code = $script:BuildGuardFragmentExit
        } elseif ($left) {
            $via = 'left'
            $unrecorded = 'the fragment left by an exit, break or continue that recorded no code'
        }
        $script:BuildGuardCommandExit = Resolve-BuildGuardFragmentVerdict -Label $Label -Via $via -Code $code -Collected $fragmentErrors -Failure $failure -Unrecorded $unrecorded
        # The fragment's own exit is still unwinding: replace it with the verdict. Invoke-BuildGuard's
        # finally runs after this and releases the slot.
        if ($left) { exit $script:BuildGuardCommandExit }
    }
} } catch { Exit-BuildGuardError $_ $Label }
exit $script:BuildGuardCommandExit
