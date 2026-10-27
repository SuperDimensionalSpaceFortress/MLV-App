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
#     The hung-tool bound (wait + 15 min) is numeric and measured on a monotonic clock.
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
#   - A heavy-slot call inside a build action does not pass through (the holder is a child of this guard,
#     not an ancestor of the action), so it would wait for its own guard: do not nest one there.
#   - heavy-slot's output goes to a log file beside the lock, never a pipe, so it can never block while it
#     holds the slot. Known limit: a guard killed ALONE while still queued (not a tree kill) leaves its
#     heavy-slot waiter queued; once admitted it may sample load (up to its 10 min admit cap) before the
#     holder starts, sees the guard gone and exits. A tree kill (taskkill /T) ends the waiter with it.
#   Lane FULL-SUITE runs take the same slot by hand, no new runner: from the lane worktree,
#      $env:HEAVY_SLOT_EXPECT_SHA256 = '<pin below>'; $env:HEAVY_SLOT_EXPECT_KEEPER = '1'
#      heavy-slot.cmd run --class shared --label mlv:<lane>:pytest --eta 45m --wait 60m --exit-file <fresh> -- py -3 -m pytest <args>
#   and judge 75-77 by the same marker rule (matched = REQUEUE, unmatched = COULD-NOT-CHECK); every other
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
#
# -RequestedJobs N (library/command) caps the choice: jobs = min(chosen, N). A caller can build
# smaller than the guard allows, never larger.
#
# Log lines go to stderr; typed lines (BUILD_MEM_REFUSED, BUILD_SLOT_BUSY) go to stdout, except
# BUILD_GUARD_COMMAND_FAILED, which goes to stderr beside the error it reports.
# Test seams (used by tests/coordination/test_build_guard.py, no real probe needed):
#   -MemoryProbeFile   JSON array of {"freePhysGB":n,"freeCommitGB":n}, one sample per poll, last repeats
#   -ProcessTableFile  JSON array of {"ProcessId":n,"Name":"cc1plus.exe","CreationDate":"<iso>"}
#   -LockPath / -MutexName / -PollSeconds / -DeadlineMinutes / -LockExpiryMinutes / -ExemptLogPath
#   -HeavySlotCmd / env MLV_HEAVY_SLOT_CMD (a stub heavy-slot.cmd) and -HeavySlotSha256 (that stub's sha)
# Exit codes: 3 = refused (memory, slot, heavy-slot REQUEUE or pin mismatch), 2 = usage, 1 = the command fragment failed without a non-zero
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

function Write-BuildGuardLog([string]$Message) {
    [Console]::Error.WriteLine("[build-guard] $Message")
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
    if ($Options.MemoryProbeFile) {
        $samples = @(Get-Content -LiteralPath $Options.MemoryProbeFile -Raw | ConvertFrom-Json)
        if ($samples.Count -eq 0) { return @{ PhysGB = 0.0; CommitGB = 0.0 } }
        $i = [math]::Min($script:BuildGuardProbeIndex, $samples.Count - 1)
        $script:BuildGuardProbeIndex++
        return @{ PhysGB = [double]$samples[$i].freePhysGB; CommitGB = [double]$samples[$i].freeCommitGB }
    }
    $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec $script:BuildGuardCimTimeoutSec -ErrorAction SilentlyContinue
    if (-not $os) { return @{ PhysGB = 0.0; CommitGB = 0.0 } }
    return @{
        PhysGB   = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
        CommitGB = [math]::Round($os.FreeVirtualMemory / 1MB, 2)
    }
}

function Get-BuildGuardJobs([hashtable]$Options, [int]$SelfPid) {
    # 4 by default; 2 when another live cc1plus / mingw32-make exists. Peers are pid@startUtc.
    if ($Options.ProcessTableFile) {
        $table = @(Get-Content -LiteralPath $Options.ProcessTableFile -Raw | ConvertFrom-Json)
    } else {
        $table = @(Get-CimInstance Win32_Process -OperationTimeoutSec $script:BuildGuardCimTimeoutSec -ErrorAction SilentlyContinue |
            Select-Object ProcessId, ParentProcessId, Name, CreationDate)
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

function Invoke-BuildSlotAcquire([hashtable]$Options, [string]$Label) {
    # Returns @{ Acquired; Held; Ticket }. The whole read-check-reclaim-create runs under a named mutex so
    # two reclaimers cannot delete each other's fresh lock.
    $dir = Split-Path -Parent $Options.LockPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $mutex = New-Object System.Threading.Mutex($false, $Options.MutexName)
    $got = $false
    try {
        try { $got = $mutex.WaitOne(15000) } catch [System.Threading.AbandonedMutexException] { $got = $true }
        if (-not $got) { return @{ Acquired = $false; Held = 'acquire-mutex-busy' } }
        $path = $Options.LockPath
        if (Test-Path -LiteralPath $path) {
            $info = $null
            try { $info = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { $info = $null }
            $nowTicks = [DateTime]::UtcNow.Ticks
            $reclaim = ''
            if (-not $info -or -not $info.ownerPid) {
                # empty or half-written; a writer finishes in milliseconds, so only an old file is stale
                $age = ([DateTime]::UtcNow - (Get-Item -LiteralPath $path).LastWriteTimeUtc).TotalSeconds
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
            Remove-Item -LiteralPath $path -Force
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
    $mutex = New-Object System.Threading.Mutex($false, $Ticket.MutexName)
    $got = $false
    try {
        try { $got = $mutex.WaitOne(15000) } catch [System.Threading.AbandonedMutexException] { $got = $true }
        if (-not $got) { Write-BuildGuardLog "release-mutex-busy: build slot left for owner-dead reclaim: $($Ticket.Path)"; return }
        $info = Get-Content -LiteralPath $Ticket.Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ([int]$info.ownerPid -eq $Ticket.OwnerPid -and [long]$info.startTicks -eq $Ticket.StartTicks) {
            Remove-Item -LiteralPath $Ticket.Path -Force
        }
    } catch {
    } finally {
        if ($got) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
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
        if (Test-Path -LiteralPath $named -PathType Leaf) { return (Resolve-Path -LiteralPath $named).ProviderPath }
        return ''
    }
    $found = Get-Command -Name 'heavy-slot.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    if (Test-Path -LiteralPath $script:HeavySlotInstallCmd -PathType Leaf) { return $script:HeavySlotInstallCmd }
    return ''
}

function Get-HeavySlotLane {
    # <lane> of mlv:<lane>:build: lane-<CARD>-rN-<stamp> under MLV_LANE_RUN_DIR gives <CARD>; no run dir is the hub.
    $run = ([string]$env:MLV_LANE_RUN_DIR).TrimEnd('\', '/')
    if (-not $run) { return 'hub' }
    $leaf = Split-Path -Leaf $run
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
    $text = @(
        "`$me = Get-Process -Id `$PID"
        "Set-Content -LiteralPath $(& $quote $ReadyFile) -Value @([string]`$env:HEAVY_SLOT_HELD, ('holder={0}@{1}' -f `$PID, `$me.StartTime.ToUniversalTime().Ticks)) -Encoding ascii"
        "while (-not (Test-Path -LiteralPath $(& $quote $StopFile))) {"
        "    `$p = Get-Process -Id $PID -ErrorAction SilentlyContinue"
        "    if (-not `$p -or `$p.StartTime.ToUniversalTime().Ticks -ne $startTicks) { exit 1 }"
        "    Start-Sleep -Milliseconds 500"
        "}"
    ) -join "`n"
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($text))
}

function Send-HeavySlotLines($Lease) {
    # Relays the lines heavy-slot has written to its log file since the last call, and keeps them for the marker check.
    # A log that exists but cannot be read sets LogUnread, so admission can fail closed instead of seeing no lines.
    try { $all = @(Get-Content -LiteralPath $Lease.Log -ErrorAction Stop); $Lease.LogUnread = $false } catch {
        $all = @()
        $Lease.LogUnread = [bool](Test-Path -LiteralPath $Lease.Log)
    }
    for ($i = $Lease.Lines.Count; $i -lt $all.Count; $i++) {
        $Lease.Lines.Add([string]$all[$i])
        [Console]::Error.WriteLine([string]$all[$i])
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
    try { $m = Get-Content -LiteralPath $Lease.ExitFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { return 'no readable exit marker' }
    if ([string]$m.source -ne 'heavy-slot' -or [string]$m.code -ne [string]$Code -or [string]$m.ticket -ne $ticket -or [string]$m.nonce -ne $nonce) {
        return ('the exit marker does not name this run (source={0} code={1} ticket={2})' -f $m.source, $m.code, $m.ticket)
    }
    $Lease.Reason = [string]$m.reason
    return ''
}

function Remove-HeavySlotFiles($Lease) {
    foreach ($f in @($Lease.Ready, $Lease.Stop, $Lease.ExitFile, $Lease.Log)) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
}

function Remove-HeavySlotLeaseFiles($Lease) {
    # After the stop file is written: a holder that outlived heavy-slot (its slot owner gone) polls for the stop file every
    # 500 ms, so it is given 10 s to see it and end. The stop file is deleted only once neither heavy-slot nor that holder
    # runs; otherwise it stays, so the holder still sees it (r3b adversary A2). The other lease files go either way.
    if ($Lease.Holder -and (Test-HeavySlotIdentity $Lease.Holder)) {
        $h = Get-Process -Id $Lease.Holder.Pid -ErrorAction SilentlyContinue
        if ($h) { [void]$h.WaitForExit(10000) }
    }
    if (-not $Lease.Process.HasExited -or ($Lease.Holder -and (Test-HeavySlotIdentity $Lease.Holder))) {
        foreach ($f in @($Lease.Ready, $Lease.ExitFile, $Lease.Log)) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
        return
    }
    Remove-HeavySlotFiles $Lease
}

function Test-HeavySlotIdentity($Identity) {
    # True while the process named by pid + start ticks still runs ($null ticks: the start time was not readable).
    $p = Get-Process -Id $Identity.Pid -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    if ($null -eq $Identity.Ticks) { return $true }
    try { return ($p.StartTime.ToUniversalTime().Ticks -eq $Identity.Ticks) } catch { return $true }
}

function Get-HeavySlotHolderState($Lease) {
    # 'alive' or 'gone' for the machine slot behind the ready file; '' while its holder line is not readable yet. 'alive'
    # needs all three: the holder named in the ready file (pid + start ticks), the heavy-slot process that owns the slot
    # mutex (the pid on the ready file's first line, HEAVY_SLOT_HELD = <ticket>:<pid>), and heavy-slot's cmd wrapper (r2
    # adversary A2: a holder whose mutex owner has died holds nothing). $Lease.Gone names the first one found gone.
    # The first identity read is kept, so a ready file that later vanishes cannot hide a holder that has ended.
    if (-not $Lease.Holder) {
        $text = Get-Content -LiteralPath $Lease.Ready -Raw -ErrorAction SilentlyContinue
        if (-not ($text -match '(?m)^holder=(\d+)@(\d+)\s*$')) { return '' }
        $Lease.Holder = @{ Pid = [int]$Matches[1]; Ticks = [long]$Matches[2] }
        # No owner pid on the first line (heavy-slot ran the holder without its slot) leaves only the holder to check.
        if ($text -match '\A[^\r\n]*:(\d+)[ \t]*(\r?\n|\z)') {
            $ownerPid = [int]$Matches[1]
            $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
            $ticks = -1L   # not running at the first read: never alive
            if ($owner) { try { $ticks = $owner.StartTime.ToUniversalTime().Ticks } catch { $ticks = $null } }
            $Lease.Owner = @{ Pid = $ownerPid; Ticks = $ticks }
        }
    }
    if (-not (Test-HeavySlotIdentity $Lease.Holder)) { $Lease.Gone = 'holder-exited'; return 'gone' }
    if ($Lease.Owner -and -not (Test-HeavySlotIdentity $Lease.Owner)) { $Lease.Gone = 'slot-owner-exited'; return 'gone' }
    if ($Lease.Process.HasExited) { $Lease.Gone = 'heavy-slot-exited'; return 'gone' }
    return 'alive'
}

function Send-HeavySlotStop($Lease) {
    # Writes the stop file. When it cannot be written, heavy-slot's process tree is ended instead (the OS then frees its
    # slot mutex), so a holder that would never see the file cannot keep the machine slot.
    try { Set-Content -LiteralPath $Lease.Stop -Value stop -Encoding ascii -ErrorAction Stop; return } catch { $why = $_.Exception.Message }
    Write-BuildGuardLog ('HEAVY_SLOT_WARN label={0} stop file not written ({1}); ending heavy-slot pid {2} and its children' -f $Lease.Label, $why, $Lease.Process.Id)
    if (-not $Lease.Process.HasExited) { $null = & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $Lease.Process.Id /T /F 2>&1 }
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
    Send-HeavySlotStop $Lease
    $code = Get-HeavySlotExitCode $Lease
    Send-HeavySlotLines $Lease
    foreach ($l in $Lease.Lines) { if ($l -match '^HEAVY-SLOT TICKET (\S+) ') { $Lease.Ticket = $Matches[1] } }
    Remove-HeavySlotLeaseFiles $Lease
    $gone = if ($Lease.Gone) { $Lease.Gone } else { 'holder-exited' }
    Write-Output ('BUILD_HEAVY_SLOT_REQUEUE label={0} code={1} ticket={2} reason={3}-{4} utc={5}' -f $Lease.BuildLabel, $code, $Lease.Ticket, $gone, $When, [DateTime]::UtcNow.ToString('o'))
    exit 3
}

function Enter-HeavySlot([hashtable]$Options, [string]$Label) {
    # Takes the machine heavy slot for one build unit (see the header). Returns the lease, or $null when it fails
    # open. A pin mismatch, a REQUEUE or a COULD-NOT-CHECK prints its typed line and exits here: nothing ran.
    $cmd = Resolve-HeavySlotCmd $Options
    if (-not $cmd) {
        Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=missing: heavy-slot.cmd not found; building WITHOUT the machine heavy slot (fail open)"
        return $null
    }
    $cli = Join-Path (Split-Path -Parent $cmd) 'heavy-slot.ps1'
    $actual = if (Test-Path -LiteralPath $cli -PathType Leaf) { (Get-FileHash -LiteralPath $cli -Algorithm SHA256).Hash.ToLowerInvariant() } else { 'missing' }
    $expected = ([string]$Options.HeavySlotSha256).Trim().ToLowerInvariant()
    if ($actual -ne $expected) {
        Write-Output ('BUILD_HEAVY_SLOT_REFUSED label={0} reason=pin-mismatch expected={1} actual={2} cli={3} utc={4}' -f $Label, $expected, $actual, $cli, [DateTime]::UtcNow.ToString('o'))
        exit 3
    }
    # A relative -LockPath is resolved first, so its parent is never empty (adversary A1).
    $lockFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Options.LockPath)
    $dir = Join-Path (Split-Path -Parent $lockFull) 'heavy-slot'
    $exe = [Environment]::ProcessPath
    if (-not $exe) { $exe = (Get-Process -Id $PID).Path }
    # cmd expands %VAR% even inside quotes, so a path with '%' would not reach heavy-slot as given (r1 adversary A3).
    $pct = @($cmd, $dir, $exe) | Where-Object { ([string]$_).Contains('%') } | Select-Object -First 1
    if ($pct) {
        Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=$cmd path-has-percent: $pct; building WITHOUT the machine heavy slot (fail open)"
        return $null
    }
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $id = '{0}-{1}' -f $PID, [Guid]::NewGuid().ToString('N')
    $lease = @{
        Label = 'mlv:{0}:build' -f (Get-HeavySlotLane); BuildLabel = $Label
        Ready = Join-Path $dir "$id.ready"; Stop = Join-Path $dir "$id.stop"; ExitFile = Join-Path $dir "$id.exit.json"
        Log = Join-Path $dir "$id.log"; Lines = [System.Collections.Generic.List[string]]::new(); Process = $null; Ticket = ''; Reason = ''; LogUnread = $false; Holder = $null
        Owner = $null; Gone = ''
    }
    # cmd quoting, as heavy-slot itself starts a batch child: each argument double-quoted. Its stdout and stderr go to a
    # file, never a pipe, so nothing it writes while it holds the slot can block it (adversary A4).
    $quoted = @($cmd, 'run', '--class', 'shared', '--label', $lease.Label, '--eta', ('{0}m' -f $Options.HeavySlotEtaMinutes),
        '--wait', ('{0}m' -f $Options.HeavySlotWaitMinutes), '--exit-file', $lease.ExitFile,
        '--', $exe, '-NoProfile', '-NonInteractive', '-EncodedCommand', (New-HeavySlotHolder $lease.Ready $lease.Stop)) |
        ForEach-Object { '"' + ([string]$_ -replace '"', '""') + '"' }
    $psi = [System.Diagnostics.ProcessStartInfo]::new($env:ComSpec)
    $psi.UseShellExecute = $false
    $psi.Arguments = '/d /v:off /s /c "' + ($quoted -join ' ') + ' >"' + $lease.Log + '" 2>&1"'
    $psi.Environment['HEAVY_SLOT_EXPECT_SHA256'] = $expected
    $psi.Environment['HEAVY_SLOT_EXPECT_KEEPER'] = '1'
    # heavy-slot ends its own wait (a marked 75) and its admit cap; this bound only catches a hung tool. It is numeric even
    # when a Library caller passes the minutes as a string, and monotonic, so a wall-clock step cannot move it. It is
    # computed BEFORE heavy-slot starts: a value it cannot hold (an overflow, NaN, text) is a typed refusal with nothing
    # launched, never a throw after the launch that skips the stop file (r2 adversary A3).
    $giveUp = $null
    try { $giveUp = [TimeSpan]::FromMinutes([double]$Options.HeavySlotWaitMinutes + 15) } catch { $why = $_.Exception.Message }
    if ($null -eq $giveUp) {
        Write-Output ('BUILD_HEAVY_SLOT_REFUSED label={0} reason=invalid-option HeavySlotWaitMinutes={1} ({2}) utc={3}' -f $Label, $Options.HeavySlotWaitMinutes, ($why -replace '\s+', ' '), [DateTime]::UtcNow.ToString('o'))
        exit 2
    }
    try { $lease.Process = [System.Diagnostics.Process]::Start($psi) } catch {
        Write-BuildGuardLog "HEAVY_SLOT_WARN label=$Label shim=$cmd start-failed: $($_.Exception.Message); building WITHOUT the machine heavy slot (fail open)"
        return $null
    }
    $admitted = $false
    # The cleanup try begins at the first statement after the launch, so nothing between them can skip its finally.
    try {
        Write-BuildGuardLog ('heavy-slot: waiting label={0} wait={1}m exit-file={2} shim={3}' -f $lease.Label, $Options.HeavySlotWaitMinutes, $lease.ExitFile, $cmd)
        $waitClock = [System.Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            Send-HeavySlotLines $lease
            # The ready file admits only while the holder it names still runs (checked after the file is seen, and again below).
            if (Test-Path -LiteralPath $lease.Ready) {
                $holder = Get-HeavySlotHolderState $lease
                if ($holder -eq 'alive') { break }
                if ($holder -eq 'gone' -or $lease.Process.HasExited) { Exit-HeavySlotHolderGone $lease }
            }
            $why = ''
            $code = 1
            if ($lease.Process.HasExited) {
                if (Test-Path -LiteralPath $lease.Ready) { Exit-HeavySlotHolderGone $lease }
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
                    Write-Output ('BUILD_HEAVY_SLOT_REQUEUE label={0} code={1} ticket={2} reason={3} utc={4}' -f $Label, $code, $lease.Ticket, ($lease.Reason -replace '\s+', ' '), [DateTime]::UtcNow.ToString('o'))
                    exit 3
                }
                if ($code -lt 75 -or $code -gt 77) { $why = 'heavy-slot ended before admitting the build' }
            } elseif ($waitClock.Elapsed -ge $giveUp) {
                $why = 'no admission and no exit within the wait plus 15 min'
            }
            if ($why) {
                if ($code -eq 0) { $code = 1 }
                Write-Output ('BUILD_HEAVY_SLOT_COULD_NOT_CHECK label={0} code={1} reason={2} utc={3}' -f $Label, $code, $why, [DateTime]::UtcNow.ToString('o'))
                exit $code
            }
            Start-Sleep -Milliseconds 250
        }
        Send-HeavySlotLines $lease
        if ((Get-HeavySlotHolderState $lease) -ne 'alive') { Exit-HeavySlotHolderGone $lease }
        if ($lease.LogUnread) {
            # Its lines (a PIN MISMATCH among them) cannot be checked, so the slot is given back and nothing is built.
            Exit-HeavySlot $lease
            Write-Output ('BUILD_HEAVY_SLOT_COULD_NOT_CHECK label={0} code=1 reason=heavy-slot log unreadable at admission utc={1}' -f $Label, [DateTime]::UtcNow.ToString('o'))
            exit 1
        }
        # heavy-slot only warns when the script it runs is not the pin (it may have changed after the hash above), so
        # its own PIN MISMATCH line refuses here too, after the slot is given back (r1 adversary A4).
        if (@($lease.Lines | Where-Object { $_ -match 'PIN MISMATCH' }).Count -gt 0) {
            Exit-HeavySlot $lease
            Write-Output ('BUILD_HEAVY_SLOT_REFUSED label={0} reason=pin-mismatch-at-run cli={1} utc={2}' -f $Label, $cli, [DateTime]::UtcNow.ToString('o'))
            exit 3
        }
        Write-BuildGuardLog ('heavy-slot: admitted label={0} held={1}' -f $lease.Label, ((Get-Content -LiteralPath $lease.Ready -Raw -ErrorAction SilentlyContinue) -replace '\s', ''))
        $admitted = $true
        return $lease
    } finally {
        # Every way out before admission (exit, throw, Ctrl+C) leaves the stop file while heavy-slot still runs, even when a
        # ready file appeared after the last check, so its holder ends at once instead of holding the machine slot
        # (adversary A2; r1 adversary A1). A holder that outlived heavy-slot (its slot owner killed) gets it too.
        if (-not $admitted -and (-not $lease.Process.HasExited -or ($lease.Holder -and (Test-HeavySlotIdentity $lease.Holder)))) { Send-HeavySlotStop $lease }
    }
}

function Exit-HeavySlot($Lease) {
    # Gives the slot back: the holder sees the stop file and exits, so heavy-slot releases. The build's verdict
    # never depends on this; a release that does not end cleanly is a WARN only.
    if (-not $Lease) { return }
    # The holder (or heavy-slot) ended before the stop file: part of the build ran without the machine slot. The build
    # is not failed for it (the slot is a throttle, not a correctness lock), but the line lets a receipt call it unprotected.
    $lost = $Lease.Process.HasExited -or (Get-HeavySlotHolderState $Lease) -ne 'alive'
    Send-HeavySlotStop $Lease
    if ($lost) {
        $line = 'HEAVY_SLOT_LOST_DURING_ACTION label={0} code={1} utc={2}' -f $Lease.BuildLabel, (Get-HeavySlotExitCode $Lease), [DateTime]::UtcNow.ToString('o')
        Write-BuildGuardLog $line
        [Console]::Out.WriteLine($line)
    }
    if ($Lease.Process.WaitForExit(60000)) {
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
    $o = @{}
    foreach ($k in $script:BuildGuardDefaults.Keys) { $o[$k] = $script:BuildGuardDefaults[$k] }
    foreach ($k in $Options.Keys) { $o[$k] = $Options[$k] }
    $exempt = Get-BuildGuardExemptReason $ExemptReason
    if (-not $exempt) { $exempt = Get-BuildGuardExemptReason $env:MLV_BUILD_GUARD_EXEMPT }
    if ($exempt) {
        $line = 'BUILD_GUARD_EXEMPT label={0} reason={1} utc={2} pid={3}' -f $Label, $exempt, [DateTime]::UtcNow.ToString('o'), $PID
        Write-BuildGuardLog $line
        try {
            $logDir = Split-Path -Parent $o.ExemptLogPath
            if ($logDir -and -not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
            Add-BuildGuardLogLine -Path $o.ExemptLogPath -Line $line -Encoding ([Text.Encoding]::ASCII)
        } catch { Write-BuildGuardLog "exemption log not written: $($_.Exception.Message)" }
    }
    # The machine heavy slot is the OUTERMOST lock of a build: the memory floor and the build slot below nest inside it,
    # and the outer finally gives it back on every way out, a throw a Library caller catches included (adversary A2).
    $heavy = $null
    try {
        if (-not $NoSlot) { $heavy = Enter-HeavySlot -Options $o -Label $Label }
        $started = [DateTime]::UtcNow
        $deadline = $started.AddMinutes($o.DeadlineMinutes)
        $ticket = $null
        $mem = @{ PhysGB = 0.0; CommitGB = 0.0 }
        while ($true) {
            $mem = Get-BuildGuardMemory -Options $o
            $memOk = [bool]$exempt -or ($mem.PhysGB -ge $o.MinFreePhysGB -and $mem.CommitGB -ge $o.MinFreeCommitGB)
            $held = ''
            if ($memOk) {
                if ($NoSlot) { break }
                $slot = Invoke-BuildSlotAcquire -Options $o -Label $Label
                if ($slot.Acquired) { $ticket = $slot.Ticket; break }
                $held = $slot.Held
            }
            if ([DateTime]::UtcNow -ge $deadline) {
                $waited = [int]([DateTime]::UtcNow - $started).TotalSeconds
                if (-not $memOk) {
                    Write-Output ('BUILD_MEM_REFUSED label={0} freePhysGB={1} freeCommitGB={2} need={3}/{4} waitedSec={5} utc={6}' -f $Label, $mem.PhysGB, $mem.CommitGB, $o.MinFreePhysGB, $o.MinFreeCommitGB, $waited, [DateTime]::UtcNow.ToString('o'))
                } else {
                    Write-Output ('BUILD_SLOT_BUSY label={0} heldBy=[{1}] waitedSec={2} utc={3}' -f $Label, $held, $waited, [DateTime]::UtcNow.ToString('o'))
                }
                exit 3
            }
            Start-Sleep -Milliseconds ([int]($o.PollSeconds * 1000))
        }
        try {
            $choice = Get-BuildGuardJobs -Options $o -SelfPid $PID
            $jobs = $choice.Jobs
            if ($RequestedJobs -gt 0) { $jobs = [math]::Min($jobs, $RequestedJobs) }
            # The action starts only under a live heavy slot (r2 adversary A1): a slot lost while waiting for memory, the
            # build slot or the process census is a REQUEUE with nothing built, not a build classified as unprotected
            # afterwards. The check is the last step before the action (r3b adversary A1: the census can take seconds).
            # Every caller (command mode, build-release, qscreen-probe, the attr-3 assembler) reaches its action through here.
            if ($heavy -and (Get-HeavySlotHolderState $heavy) -ne 'alive') {
                $lost = $heavy
                $heavy = $null
                Remove-BuildSlot $ticket
                $ticket = $null
                Exit-HeavySlotHolderGone $lost 'before-action'
            }
            Write-BuildGuardLog ('admitted label={0} jobs={1} reason={2} peers=[{3}] freePhysGB={4} freeCommitGB={5} slot={6}' -f $Label, $jobs, $choice.Reason, ($choice.Peers -join ','), $mem.PhysGB, $mem.CommitGB, $(if ($ticket) { 'held' } else { 'none' }))
            & $Action $jobs
        } finally {
            Remove-BuildSlot $ticket
        }
    } finally {
        Exit-HeavySlot $heavy
    }
}

if ($Library) { return }

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
    Invoke-BuildGuard -Label $Label -RequestedJobs $RequestedJobs -ExemptReason $ExemptReason -NoSlot -Options $guardOptions -Action {
        param($Jobs)
        Write-Output "-j$Jobs"
    }
    exit 0
}

if (-not $Command.Trim()) {
    [Console]::Error.WriteLine('build-guard: no build command given. Use -PrintJobs, or: build-guard.ps1 -Command "<fragment with {JOBS}>"')
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
    [Console]::Error.WriteLine(('BUILD_GUARD_COMMAND_FAILED label={0} lastNativeExit={1} errors={2} exit={3} via={4} reason={5}' -f $Label, [int]$global:LASTEXITCODE, ($records.Count + $unreported), $verdict, $Via, ($reason -replace '\s+', ' ')))
    return $verdict
}

if ($PSVersionTable.PSVersion -lt [version]'7.4') {
    [Console]::Error.WriteLine("build-guard: command mode needs PowerShell 7.4+ (native exit codes stop the fragment); this is $($PSVersionTable.PSVersion)")
    exit 2
}
$script:BuildGuardCommandExit = 0
Invoke-BuildGuard -Label $Label -RequestedJobs $RequestedJobs -ExemptReason $ExemptReason -Options $guardOptions -Action {
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
}
exit $script:BuildGuardCommandExit
