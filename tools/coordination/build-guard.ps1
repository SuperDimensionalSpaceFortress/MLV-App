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
#              this: it ends the fragment, and N is judged like a native code. If nothing failed, the exit
#              code is 0. (A "--" separator is not used: PowerShell binds it as a parameter named '' and
#              refuses.)
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
# Exit codes: 3 = refused (memory or slot), 2 = usage, 1 = the command fragment failed without a non-zero
# code of its own, otherwise the code of the first native command that failed or of the fragment's own
# 'exit' (0 when nothing failed).
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
}
$script:BuildGuardCimTimeoutSec = 30

function Write-BuildGuardLog([string]$Message) {
    [Console]::Error.WriteLine("[build-guard] $Message")
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
            Add-Content -LiteralPath $o.ExemptLogPath -Value $line -Encoding ascii
        } catch { Write-BuildGuardLog "exemption log not written: $($_.Exception.Message)" }
    }
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
        Write-BuildGuardLog ('admitted label={0} jobs={1} reason={2} peers=[{3}] freePhysGB={4} freeCommitGB={5} slot={6}' -f $Label, $jobs, $choice.Reason, ($choice.Peers -join ','), $mem.PhysGB, $mem.CommitGB, $(if ($ticket) { 'held' } else { 'none' }))
        & $Action $jobs
    } finally {
        Remove-BuildSlot $ticket
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
    # command's, the one its 'exit' gave, or 0. A non-zero code is kept (9 stays 9). Any collected error
    # record, or the error that stopped the fragment, prints BUILD_GUARD_COMMAND_FAILED and turns a zero code
    # into 1. The record of the native failure itself is not an error here (its code already reports it).
    param([string]$Label, [string]$Via, [int]$Code, $Collected, $Failure, [string]$Unrecorded = '')
    $native = [System.Management.Automation.NativeCommandExitException]
    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($item in @($Collected)) {
        if ($item -is [System.Management.Automation.ErrorRecord] -and $item.Exception -isnot $native) { $records.Add($item) }
    }
    if ($Failure) {
        $failureException = if ($Failure -is [System.Management.Automation.ErrorRecord]) { $Failure.Exception } else { $Failure }
        $seen = $false
        foreach ($r in $records) { if ([object]::ReferenceEquals($r.Exception, $failureException)) { $seen = $true } }
        if (-not $seen -and $failureException -isnot $native) { $records.Add($Failure) }
    }
    if ($records.Count -eq 0 -and -not $Unrecorded) { return $Code }
    $verdict = if ($Code -ne 0) { $Code } else { 1 }
    if ($records.Count -gt 0) {
        $first = $records[0]
        $exception = if ($first -is [System.Management.Automation.ErrorRecord]) { $first.Exception } else { $first }
        $reason = ($exception.GetType().Name + ': ' + $exception.Message) -replace '\s+', ' '
    } else {
        $reason = $Unrecorded
    }
    [Console]::Error.WriteLine(('BUILD_GUARD_COMMAND_FAILED label={0} lastNativeExit={1} errors={2} exit={3} via={4} reason={5}' -f $Label, [int]$global:LASTEXITCODE, $records.Count, $verdict, $Via, $reason))
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
