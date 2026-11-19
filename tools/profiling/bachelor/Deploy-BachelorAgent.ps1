# Deploy-BachelorAgent.ps1  (BACHELOR-AGENT-DEPLOY-TRACKED-1 r1)
#
# The ONE tracked command that puts a merged tools/profiling/bachelor/ultra-magnus-agent.ps1 on the Bachelor bench.
# It copies the agent into the agent root, verifies the sha256 of the copy on the venue, keeps the previous agent as a
# rollback copy, and (only with -RestartMode when-idle) restarts the agent when nothing is running.
#
# THE DEPLOY COMMAND (run it from a checkout of the merged master, so the default -Source is the merged agent; the paths
# are absolute and quoted because the owner's shell may sit in System32; run it ON Bachelor for -RestartMode when-idle):
#
#   pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\profiling\bachelor\Deploy-BachelorAgent.ps1" -VenueRoot "\\bachelor\mlv-agent" -DryRun
#   pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\profiling\bachelor\Deploy-BachelorAgent.ps1" -VenueRoot "\\bachelor\mlv-agent"
#   pwsh -NoProfile -File "<path to this script on Bachelor>" -VenueRoot "<the agent's LOCAL root on Bachelor>" -RestartMode when-idle
#
# Parameters
#   -VenueRoot    the agent root (the folder that holds ultra-magnus-agent.ps1, inbox\, running\, slot-holders\), as the
#                 agent sees it on Bachelor, or the share path the board uses. Required. For -RestartMode when-idle it must
#                 be the agent's own local root: the restart is bound to the agent whose script is <VenueRoot>\ultra-magnus-agent.ps1.
#   -Source       the agent to deploy. Default: ultra-magnus-agent.ps1 beside this script (the tracked file).
#   -DryRun       run every check and print what would happen; change nothing (it takes no lock). A refusal is still a refusal.
#   -RestartMode  none (default) | when-idle. when-idle restarts the agent after a verified deploy, and only when running\
#                 and inbox\ are still empty and no lease is held at that moment.
#   -RestartScript, -TaskXmlFile, -TestCorruptAfterWrite  test seams (see below); not for the venue.
#
# What it does, in order (every doubt refuses; nothing is changed before the last refusal check passes)
#   1. source sha256 (read once; the bytes hashed are the bytes written).
#   2. venue root readable: the root, inbox\, running\ and slot-holders\ must all list, even for a no-op. Else DEPLOY_REFUSED_UNREADABLE.
#   3. the deploy lock: <VenueRoot>\deploy.lock is created exclusively (FileShare.None, DeleteOnClose, so it vanishes with
#      this process even if killed). Two deployers on one root could each judge "no previous copy" and one would delete the
#      other's verified deploy; the second is refused (DEPLOY_REFUSED_LOCKED). A leftover deploy.lock is never removed by this script.
#   4. the venue copy already has that sha -> DEPLOY_NOOP, exit 0 (idempotent; nothing is touched, busy or not).
#   5. busy or leased -> typed refusal, nothing changed (table below).
#   6. keep the previous agent at <VenueRoot>\rollback-<utc>\ultra-magnus-agent.ps1 (verified), then write the new agent as
#      a temp file beside it and rename it over the agent (the old file is never half-written).
#   7. re-read the venue copy; its sha must equal the source's. Else DEPLOY_SHA_MISMATCH and the rollback copy is put back.
#   8. -RestartMode when-idle: re-check idle, then restart (below). A running agent has the old script in memory, so the
#      deploy itself never disturbs a job; only the restart does, which is why it is gated.
#   9. DEPLOY_OK sha=<s> rollback=<path|none> restarted=<yes|no>
#
# Typed lines (stdout) and exit codes - the hub parses these
#   DEPLOY_NOOP sha=<s>                                          0
#   DEPLOY_DRYRUN action=<deploy|noop> sha=<s> restart=<mode>   0
#   DEPLOY_OK sha=<s> rollback=<path|none> restarted=<yes|no>    0
#   DEPLOY_WARN_ARRIVED_DURING_RESTART running=<n> inbox=<n> ... 0  (printed before DEPLOY_OK; see "Residual race")
#   DEPLOY_SOURCE_MISSING <path>                                 2
#   DEPLOY_REFUSED_BUSY running=<n> inbox=<n>                    10  (running\ or inbox\ holds anything)
#   DEPLOY_REFUSED_LEASE <what>                                  11  (slot-holders\ holds a *.lease.json, *.holder.json or
#                                                                     *.acquired.json: a lease is held or a batch is parked)
#   DEPLOY_REFUSED_UNREADABLE <why>                              12
#   DEPLOY_SHA_MISMATCH expected=<s> actual=<s> restored=<..>    13
#   DEPLOY_RESTART_FAILED <why>                                  14  (the verified deploy stays in place)
#   DEPLOY_ERROR <why>                                           15  (a write failed before the swap finished; see restore=)
#   DEPLOY_REFUSED_RESTART_HOST <why>                            16  (-RestartMode when-idle not bound to this root's live agent; nothing changed)
#   DEPLOY_REFUSED_LOCKED <why>                                  17  (another deployer holds, or left, <VenueRoot>\deploy.lock)
#
# The restart (same mechanism the agent's existing restart path uses)
#   The agent has no self-restart. Its one start path is the Task Scheduler task \MlvAgentBachelor, named in
#   ultra-magnus-agent.ps1:259 and :393-404 (the task that starts it; its start also takes the single-instance mutex,
#   ultra-magnus-agent.ps1:4675-4681, so a start while the old agent lives exits at once). Restart is therefore:
#   stop the task's running INSTANCE whose pid the binding proved (Task Scheduler IRunningTask.Stop on that pid, found and stopped in
#   one loop: never `schtasks /End` by a name that can be re-pointed, never a kill by name), wait until that pid is gone (start time
#   checked), re-read the task definition, schtasks.exe /Run /TN \MlvAgentBachelor, wait for a heartbeat from a new LIVE agent
#   process. The host options before -File may only be -NoProfile/-NoLogo/-NonInteractive, -ExecutionPolicy Bypass|RemoteSigned|
#   Unrestricted (Restricted, AllSigned, Default and Undefined would keep the replacement from running) and -WindowStyle, and
#   -NoProfile is REQUIRED (a profile can set the agent's -Root default, so an argv that names no -Root would serve an unseen root)
#   and so is an explicit -ExecutionPolicy (without one the account's persistent policy applies and may block the replacement).
#   The stop is handed the start the OS reported for the pid at the binding (osStart) and compares within 100 ms; the task
#   definition is re-read AFTER the last wait, right before /Run. Residual (stated, OPEN): a trigger that starts the task between
#   the no-instance check and /Run, with /Run then ignored (IgnoreNew), is not distinguishable from this side. Right before /Run the task must show no running instance (IgnoreNew would make /Run a no-op and confirm an earlier instance), and a
#   wall clock stepped against a monotonic watch during the restart makes the "started at or after /Run" proof void. It acts on THIS
#   machine's task list, so -RestartMode when-idle is refused (16) before anything changes unless ALL hold: heartbeat.txt
#   names this machine as host=; its agentScript= is <VenueRoot>\ultra-magnus-agent.ps1 (a copied heartbeat from another
#   root fails here); its pid is alive with the recorded start time; and the task's own XML has an executable action
#   `-File <that script path>`: the action's <Command> is pwsh/powershell, the FIRST -File (no -Command/-EncodedCommand before it,
#   no positional token before it) is that absolute path, and the agent's own arguments after it name no other root (-Root, or a
#   positional first argument, must be this folder; a path mentioned only in the description or a later argument does not count).
#   The RUNNING instance must say the same: its own command line (Win32_Process.CommandLine of the heartbeat pid, read under a
#   deadline; an unreadable one, for example another user's process without elevation, is a refusal) is held to the same rule, so a
#   task definition edited after the instance started cannot bind while the live agent serves another root. The replacement that
#   /Run starts is held to it too, and must have STARTED at or after /Run (no tolerance: the old agent never counts as new).
#   The heartbeat pid must also BE the task's running instance: the Task Scheduler is asked (IRunningTask.EnginePID, the pid of the
#   Exec action's process) and must name exactly one instance, that pid; a process that only writes a matching heartbeat is not what
#   `schtasks /End` stops. The replacement must be the task's new instance by the same test. Idle is judged once more AFTER all of
#   this binding, and again INSIDE the stop routine with the instance object in hand and immediately before Stop() (the scheduler
#   lookup can take up to its deadline too). The stop acts on pid AND the recorded start (a reused pid is another instance). Its answer is
#   three-valued: stopped / not-stopped (Stop() never called) / unknown (no answer in time, or Stop() raised: it may have been applied,
#   so the pid is re-observed and the restart goes on only when the process is seen gone). -TaskPidsFile is the
#   seam for the scheduler's running pids. Residual (stated): the definition is re-read immediately before /Run, and the replacement
#   is attested after it, but an edit in the instant between the two cannot be excluded from here; a replacement that fails the
#   attestation is DEPLOY_RESTART_FAILED, never reported as restarted.
#   A path with no drive (/bench, \bench) never names a place on Windows: it resolves against another process's current drive.
#   The root itself must also list (a root whose listing is denied is DEPLOY_REFUSED_UNREADABLE, even for a no-op).
#   The idle gate keeps the End from stranding a lease: with running\, inbox\ and slot-holders\ empty, nothing is lost.
#   Waits use a monotonic Stopwatch, never the wall clock, and EVERY read of the venue is under a deadline: a listing, a heartbeat
#   read and each wait condition run in their own runspace (Invoke-Bounded) and a schtasks call runs as a child process
#   (Invoke-BoundedProcess), so a share that stops answering is a typed refusal or failure at the deadline, never a hang with
#   deploy.lock held. Not bounded (the deploy's own file I/O, the root's existence check, -RestartScript, the dot-source seam): a stall
#   there holds the process, and the lock is released when the process is killed (DeleteOnClose).
#   One start-time tolerance ($script:StartToleranceSec) decides both "is this the live agent" and "is this a different process".
#
# Residual race (stated, not hidden): the agent has no drain or admission barrier, so a job can land in inbox\ between the
#   last idle check and the End. The check is repeated immediately before the End and once more after the old agent has
#   stopped; work found by the second look is printed as DEPLOY_WARN_ARRIVED_DURING_RESTART and the agent is still started
#   (a stopped agent with queued work is worse). The agent's own startup recovery (Resolve-StartedMarkers, Stop-StaleQuietLeases,
#   ultra-magnus-agent.ps1:4688-4690) adopts, requeues or ends what a restart finds; the hub verifies after such a warning.
#
# Test seams: -RestartScript <ps1> replaces the schtasks steps with `& <ps1> -VenueRoot <root>` (non-zero exit = failed);
# -TaskXmlFile <xml> is read instead of `schtasks /Query /XML`; -TaskPidsFile <txt> instead of the scheduler's running pids; -TestCorruptAfterWrite appends a byte to the venue copy after
# the rename, to prove the sha-mismatch restore. Dot-sourcing the script (`. <script> -VenueRoot <x>`) loads only the functions.
# The tests (tools/repo_hygiene/test_bachelor_agent_deploy.py) run only on a temp dir. Never point this script at the real
# venue from a test, a lane or a producer: a deploy there is the owner's or the hub's call.
#
# ASCII only. PowerShell 7. Call it with -File; it takes no array parameters.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$VenueRoot,
    [string]$Source = (Join-Path $PSScriptRoot 'ultra-magnus-agent.ps1'),
    [switch]$DryRun,
    [ValidateSet('none', 'when-idle')][string]$RestartMode = 'none',
    [string]$RestartScript = '',
    [string]$TaskName = '\MlvAgentBachelor',
    [string]$TaskXmlFile = '',
    [string]$TaskPidsFile = '',
    [switch]$TestCorruptAfterWrite
)

$ErrorActionPreference = 'Stop'
$agentName = 'ultra-magnus-agent.ps1'
# ONE tolerance for "same process start": the heartbeat's processStartUtc against the live process (alive check) and the old
# heartbeat against the new one (replacement check). Same iff the gap is within it, in BOTH checks (VenueChain.psm1 uses 2 s too).
$script:StartToleranceSec = 2
# Windows or not: the one platform switch. On Windows a path that starts with ONE separator and no drive (\bench, /bench) is
# resolved against the CURRENT DRIVE of whichever process reads it, so it never names a place (Test-AbsolutePathText); a runspace's
# ExecutionPolicy can be set only on Windows (Invoke-Bounded); a process's command line comes from Win32_Process there and from
# /proc elsewhere (Get-ProcessArgv). The tests flip it in a dot-sourced session to prove both readings on either host.
$script:OnWindows = ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT)
# the longest a single read of the venue (a listing, the heartbeat) may take before it counts as unanswered
$script:ReadTimeoutSec = 20
# two reads of the OS start time of ONE process agree within this (ms); the stop compares the process under a pid with the start the
# OS reported at the binding, so a pid reused by another instance (even started a fraction of a second later) is not the proved one
$script:OsStartToleranceMs = 100
# how long the restart waits for the stopped agent's process to go (the tests shorten it)
$script:StopWaitSec = 30
# how long the restart waits for the scheduler to show NO running instance of the task before /Run (the tests shorten it)
$script:TaskIdleWaitSec = 15
# the file a bounded runspace dot-sources to get this script's functions (empty only if the host gives no path)
$script:SelfPath = $PSCommandPath

function Out-Typed { param([string]$Line) Write-Output $Line }

function Get-BytesSha256 {
    param([byte[]]$Bytes)
    return [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($Bytes))
}

function Get-FileSha256Bytes {
    param([string]$Path)
    return Get-BytesSha256 -Bytes ([System.IO.File]::ReadAllBytes($Path))
}

function Write-BytesAtomic {
    # temp file beside the target, then one rename that replaces it: the target is never half-written.
    param([string]$Path, [byte[]]$Bytes)
    $tmp = '{0}.deploy-{1}.tmp' -f $Path, ([guid]::NewGuid().ToString('N'))
    try {
        [System.IO.File]::WriteAllBytes($tmp, $Bytes)
        [System.IO.File]::Move($tmp, $Path, $true)
    }
    catch {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Invoke-Bounded {
    # Runs $Script in its OWN runspace and stops waiting for it at the deadline, so a read that stalls (a share that stops
    # answering) cannot hold this script, and the deploy lock with it, past its budget. The runspace dot-sources this script
    # (functions only), so every function here is available, plus $VenueRoot, $TaskName and $TaskXmlFile; anything else the
    # script closes over goes in -Variables. On a timeout the runspace is ABANDONED (stopped if it can be, never disposed, since
    # disposing would wait for the stalled call); its thread is a background thread and never keeps the process alive.
    # Returns [pscustomobject]@{ timedOut; error; ok; value }: ok is the truth of the last output, value is the last output.
    param([scriptblock]$Script, [long]$TimeoutMs, [hashtable]$Variables = @{})
    $iss = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
    # ExecutionPolicy is a Windows-only setting: assigning it on Linux makes Open() throw "Operation is not supported on this platform"
    if ($script:OnWindows) { $iss.ExecutionPolicy = [Microsoft.PowerShell.ExecutionPolicy]::Bypass }
    $rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($iss)
    $rs.Open()
    $rs.SessionStateProxy.SetVariable('__self', $script:SelfPath)
    $rs.SessionStateProxy.SetVariable('VenueRoot', $VenueRoot)
    $rs.SessionStateProxy.SetVariable('TaskName', $TaskName)
    $rs.SessionStateProxy.SetVariable('TaskXmlFile', $TaskXmlFile)
    $rs.SessionStateProxy.SetVariable('TaskPidsFile', $TaskPidsFile)
    foreach ($k in $Variables.Keys) { $rs.SessionStateProxy.SetVariable([string]$k, $Variables[$k]) }
    $ps = [System.Management.Automation.PowerShell]::Create($rs)
    [void]$ps.AddScript(('. $__self -VenueRoot $VenueRoot -TaskName $TaskName -TaskXmlFile $TaskXmlFile -TaskPidsFile $TaskPidsFile' + "`n" + $Script.ToString()))
    $handle = $ps.BeginInvoke()
    if (-not $handle.AsyncWaitHandle.WaitOne([int][Math]::Min([Math]::Max($TimeoutMs, 0L), 2000000000L))) {
        try { [void]$ps.BeginStop($null, $null) } catch { }
        return [pscustomobject]@{ timedOut = $true; error = $null; ok = $false; value = $null }
    }
    $out = @(); $err = $null
    try { $out = @($ps.EndInvoke($handle)) }
    catch { $inner = $_.Exception.InnerException; $err = $(if ($null -ne $inner) { $inner.Message } else { $_.Exception.Message }) }
    try { $ps.Dispose(); $rs.Dispose() } catch { }
    $last = $(if ($out.Count -gt 0) { $out[$out.Count - 1] } else { $null })
    return [pscustomobject]@{ timedOut = $false; error = $err; ok = [bool]$last; value = $last }
}

function Invoke-BoundedProcess {
    # A child process with a hard deadline: killed (by the pid this script started, never by name) when it does not exit in time.
    param([string]$FilePath, [string[]]$Arguments = @(), [int]$TimeoutSec = 30)
    $psi = [System.Diagnostics.ProcessStartInfo]::new($FilePath)
    foreach ($a in $Arguments) { [void]$psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    # the deadline is clamped to 0..2e9 ms: a negative value is not a short deadline to WaitForExit (-1 is "forever", anything lower
    # throws), and a throw after the child was started would leave it running with no one holding its pid
    $waitMs = [int][Math]::Min([Math]::Max([long]$TimeoutSec * 1000, 0L), 2000000000L)
    $exited = $false
    try { $exited = $p.WaitForExit($waitMs) }
    finally { if (-not $exited) { try { $p.Kill($true) } catch { } } }
    if (-not $exited) {
        return [pscustomobject]@{ timedOut = $true; exitCode = $null; output = ''; errorText = '' }
    }
    [void]$outTask.Wait(5000)
    [void]$errTask.Wait(5000)
    return [pscustomobject]@{
        timedOut = $false; exitCode = $p.ExitCode
        output = $(if ($outTask.IsCompleted) { $outTask.Result } else { '' })
        errorText = $(if ($errTask.IsCompleted) { $errTask.Result } else { '' })
    }
}

function Wait-Until {
    # Bounded by a monotonic Stopwatch: a wall-clock step cannot stretch or cut the wait.
    # A condition counts only if it was OBSERVED true inside the window. It is evaluated by Invoke-Bounded, so a condition that
    # itself stalls (a synchronous read of a share) is a timeout at the deadline, not a wait of the stall's length. A condition that
    # throws throws here. The remaining time is kept in [long] milliseconds (no int overflow for a large -TimeoutSec).
    param([int]$TimeoutSec, [scriptblock]$Condition, [int]$PollMs = 500, [hashtable]$Variables = @{})
    $budgetMs = [long]$TimeoutSec * 1000
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $leftMs = $budgetMs - [long]$sw.Elapsed.TotalMilliseconds
        if ($leftMs -le 0) { return $false }
        $r = Invoke-Bounded -Script $Condition -TimeoutMs $leftMs -Variables $Variables
        if ($r.timedOut) { return $false }
        if ($null -ne $r.error) { throw $r.error }
        if ($sw.Elapsed.TotalMilliseconds -ge $budgetMs) { return $false }
        if ($r.ok) { return $true }
        $leftMs = $budgetMs - [long]$sw.Elapsed.TotalMilliseconds
        if ($leftMs -le 0) { return $false }
        Start-Sleep -Milliseconds ([int][Math]::Min([long]$PollMs, $leftMs))
    }
}

function Test-AbsolutePathText {
    # A drive path (C:\x), a UNC path (\\host\share) or, off Windows only, a rooted POSIX path (/x). A relative path is resolved
    # against a working directory this script cannot see for another process (a task's WorkingDirectory, the agent's own cwd), and on
    # Windows so is a rooted-without-drive path (/bench, \bench): it lands on the CURRENT DRIVE of the process that resolves it, so
    # the task may serve D:\bench while C:\bench was the one checked idle. Neither counts as naming a place.
    param([string]$P)
    if ($P -match '^([A-Za-z]:[\\/]|[\\/]{2}[^\\/])') { return $true }
    if ($script:OnWindows) { return $false }
    return ($P -match '^/')
}

function Test-SamePath {
    # Two ABSOLUTE paths that name the same place (case-insensitive, a trailing separator ignored). A relative path on either side is
    # never "the same": it would be resolved against this process's cwd, which is not where the other process resolves it.
    param([string]$A, [string]$B)
    if (-not (Test-AbsolutePathText -P $A) -or -not (Test-AbsolutePathText -P $B)) { return $false }
    try {
        $x = [System.IO.Path]::GetFullPath($A).TrimEnd('\', '/')
        $y = [System.IO.Path]::GetFullPath($B).TrimEnd('\', '/')
        return [string]::Equals($x, $y, [StringComparison]::OrdinalIgnoreCase)
    }
    catch { return $false }
}

function Get-VenueState {
    # The listing under a hard deadline: a share that stops answering is "unreadable" (a refusal), never a hang with the lock held.
    $r = Invoke-Bounded -Script { Get-VenueStateRaw } -TimeoutMs ($script:ReadTimeoutSec * 1000)
    $failed = [ordered]@{ unreadable = $null; running = 0; inbox = 0; leases = @() }
    if ($r.timedOut) { $failed.unreadable = "listing the venue did not answer within $($script:ReadTimeoutSec) s"; return [pscustomobject]$failed }
    if ($null -ne $r.error) { $failed.unreadable = "listing failed: $($r.error)"; return [pscustomobject]$failed }
    if ($null -eq $r.value) { $failed.unreadable = 'listing returned nothing'; return [pscustomobject]$failed }
    return $r.value
}

function Get-VenueStateRaw {
    # Reads the three folders the idle judgement rests on. Any failure to list one is "unreadable", never "empty".
    $state = [ordered]@{ unreadable = $null; running = 0; inbox = 0; leases = @() }
    # the root itself must list too: lock creation and file reads can succeed where enumeration is denied
    try { [void]@([System.IO.Directory]::EnumerateFileSystemEntries($VenueRoot) | Select-Object -First 1) }
    catch {
        $state.unreadable = "the venue root cannot be listed: $($_.Exception.Message)"
        return [pscustomobject]$state
    }
    foreach ($d in @('inbox', 'running', 'slot-holders')) {
        if (-not (Test-Path -LiteralPath (Join-Path $VenueRoot $d) -PathType Container)) {
            $state.unreadable = "folder $d\ is missing under $VenueRoot"
            return [pscustomobject]$state
        }
    }
    try {
        $state.running = @([System.IO.Directory]::EnumerateFileSystemEntries((Join-Path $VenueRoot 'running'))).Count
        $state.inbox = @([System.IO.Directory]::EnumerateFileSystemEntries((Join-Path $VenueRoot 'inbox'))).Count
        $holders = Join-Path $VenueRoot 'slot-holders'
        $state.leases = @(
            [System.IO.Directory]::EnumerateFileSystemEntries($holders) |
                ForEach-Object { [System.IO.Path]::GetFileName($_) } |
                Where-Object { $_ -match '\.(lease|holder|acquired)\.json\z' } |
                Sort-Object
        )
    }
    catch {
        $state.unreadable = "listing failed: $($_.Exception.Message)"
    }
    return [pscustomobject]$state
}

function Get-Refusals {
    # Returns @{ code; lines[] } or $null when the venue is idle. Priority: unreadable, busy, lease.
    $s = Get-VenueState
    if ($null -ne $s.unreadable) {
        return @{ code = 12; lines = @("DEPLOY_REFUSED_UNREADABLE $($s.unreadable)") }
    }
    $lines = @(); $code = 0
    if ($s.running -gt 0 -or $s.inbox -gt 0) {
        $lines += "DEPLOY_REFUSED_BUSY running=$($s.running) inbox=$($s.inbox)"
        $code = 10
    }
    if ($s.leases.Count -gt 0) {
        $lines += "DEPLOY_REFUSED_LEASE $($s.leases -join ',')"
        if ($code -eq 0) { $code = 11 }
    }
    if ($code -eq 0) { return $null }
    return @{ code = $code; lines = $lines }
}

function Get-ArrivalWarning {
    # Work (or a lease) that is in the venue now. $null = idle.
    $s = Get-VenueState
    if ($null -ne $s.unreadable) { return "DEPLOY_WARN_ARRIVED_DURING_RESTART unreadable=$($s.unreadable)" }
    if ($s.running -gt 0 -or $s.inbox -gt 0 -or $s.leases.Count -gt 0) {
        return "DEPLOY_WARN_ARRIVED_DURING_RESTART running=$($s.running) inbox=$($s.inbox) leases=$($s.leases -join ',')"
    }
    return $null
}

function Read-Heartbeat {
    # heartbeat.txt: "alive <iso> pid=<n> host=<name> generation=<n> processStartUtc=<iso> imagePath="..." agentScript="..."" (ultra-magnus-agent.ps1 Write-AgentHeartbeat)
    $p = Join-Path $VenueRoot 'heartbeat.txt'
    try {
        $t = [System.IO.File]::ReadAllText($p)
        $pidM = [regex]::Match($t, '\bpid=(\d+)')
        $hostM = [regex]::Match($t, '\bhost=(\S+)')
        $startM = [regex]::Match($t, '\bprocessStartUtc=(\S+)')
        $scriptM = [regex]::Match($t, 'agentScript="([^"]*)"')
        if (-not $pidM.Success -or -not $hostM.Success) { return $null }
        return [pscustomobject]@{
            pid = [int]$pidM.Groups[1].Value; host = $hostM.Groups[1].Value
            start = $(if ($startM.Success) { $startM.Groups[1].Value } else { '' })
            agentScript = $(if ($scriptM.Success) { $scriptM.Groups[1].Value } else { '' })
            ageSec = ((Get-Date) - (Get-Item -LiteralPath $p).LastWriteTime).TotalSeconds
        }
    }
    catch { return $null }
}

function Read-HeartbeatBounded {
    # Read-Heartbeat under the read deadline; no answer reads as no heartbeat (a refusal upstream, never a hang).
    $r = Invoke-Bounded -Script { Read-Heartbeat } -TimeoutMs ($script:ReadTimeoutSec * 1000)
    if ($r.timedOut -or $null -ne $r.error) { return $null }
    return $r.value
}

function Test-StartTimesMatch {
    # THE one start-time comparison: the same process iff the two starts are within $script:StartToleranceSec of each other.
    param([datetime]$A, [datetime]$B)
    return ([Math]::Abs(($A - $B).TotalSeconds) -le $script:StartToleranceSec)
}

function ConvertTo-UtcStart {
    param([string]$Text)
    return [DateTimeOffset]::Parse($Text, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime
}

function Test-ClockStepped {
    # The wall clock moved differently from a monotonic Stopwatch since both were read together ($Utc0 at the moment $Watch started):
    # the clock was stepped (NTP, a manual set, a VM resume) by more than the one start-time tolerance.
    param([datetime]$Utc0, [System.Diagnostics.Stopwatch]$Watch)
    $expected = $Utc0.ToUniversalTime() + $Watch.Elapsed
    return ([Math]::Abs(([DateTime]::UtcNow - $expected).TotalSeconds) -gt $script:StartToleranceSec)
}

function Get-LiveProcess {
    # Get-Process for a process that is RUNNING: $null when the pid does not exist, and also when it is a zombie. A killed child whose
    # parent has not yet collected its exit status stays listed by the OS (state Z in /proc/<pid>/stat on Linux) and Get-Process still
    # returns it, although it executes nothing; Windows drops a terminated process from the list, so the two hosts agree only if the
    # zombie reads as gone. Every "is that pid alive / does that process exist" probe below goes through here.
    param([int]$ProcessId)
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return $null }
    if (-not $script:OnWindows) {
        try {
            $stat = [System.IO.File]::ReadAllText("/proc/$ProcessId/stat")
            # "<pid> (<comm>) <state> ...": comm may hold spaces and parentheses, so the state follows the LAST ')'
            $close = $stat.LastIndexOf(')')
            if ($close -ge 0 -and ($close + 2) -lt $stat.Length -and $stat[$close + 2] -eq 'Z') { return $null }
        }
        catch { }   # no /proc (another Unix) or the process just exited: the Get-Process answer stands
    }
    return $proc
}

function Get-RunLowerBound {
    # The moment every "started at or after /Run" comparison measures from: the LATER of the wall clock now and where the monotonic watch
    # says the wall clock should be ($Utc0 + $Watch.Elapsed). If the wall clock was stepped BACK after an instance started (a trigger
    # started pid 4200 at 12:00:02.5, the clock went back 1 s, and runAt would read 12:00:02), the raw wall clock would put /Run
    # BEFORE that instance and /Run (ignored by IgnoreNew) would be credited with it; the monotonic expectation cannot go back. A step
    # FORWARD only makes the bound later, which refuses a genuine replacement (the safe side: "verify by hand"), never admits an old one.
    param([datetime]$Utc0, [System.Diagnostics.Stopwatch]$Watch)
    $wall = [DateTime]::UtcNow
    $expected = $Utc0.ToUniversalTime() + $Watch.Elapsed
    if ($expected -gt $wall) { return $expected }
    return $wall
}

function Test-AgentProcessAlive {
    # Alive AND the same process the heartbeat recorded (pid plus start time, so a reused pid does not count).
    param($Beat, [switch]$RequireStart)
    if ($null -eq $Beat) { return $false }
    $proc = Get-LiveProcess -ProcessId $Beat.pid
    if ($null -eq $proc) { return $false }
    if ([string]::IsNullOrWhiteSpace($Beat.start)) { return (-not $RequireStart) }
    try { return (Test-StartTimesMatch -A $proc.StartTime.ToUniversalTime() -B (ConvertTo-UtcStart -Text $Beat.start)) }
    catch { return (-not $RequireStart) }
}

function Test-NewHeartbeatIsLiveAgent {
    # A restart is confirmed only by a heartbeat from a process that EXISTS with the recorded start time, names THIS machine and this
    # root's script, is not this deployer, and (with -NotBeforeUtc) was STARTED at or after the moment the task was started: a
    # process that was already running when /Run was issued is not the replacement, whatever its heartbeat claims.
    # With -NotBeforeUtc the start is compared EXACTLY, with no tolerance, and against BOTH the start the heartbeat claims and the start
    # the OS reports for the pid: /Run cannot create a process before it is called, so a process that began even a moment earlier
    # (the old agent, or a stray one) is not the replacement. (The tolerance belongs to "same process" checks, never to "newer than".)
    # With -CheckCommandLine the process's OWN command line must run this script with this root (Test-HostArgvRunsScript).
    # With -TaskPids (the running instances of the task, Get-TaskRunningPids) the heartbeat's pid must BE one of them: a process that
    # merely writes a matching heartbeat is not the instance `schtasks /End` and /Run act on.
    param($Beat, [string]$AgentScript, $NotBeforeUtc = $null, [switch]$CheckCommandLine, $TaskPids = $null)
    if ($null -eq $Beat) { return $false }
    if ($null -ne $TaskPids -and @($TaskPids) -notcontains [int]$Beat.pid) { return $false }
    if ([string]::IsNullOrWhiteSpace($Beat.start)) { return $false }
    if (-not [string]::Equals($Beat.host, $env:COMPUTERNAME, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    if ($Beat.pid -eq $PID) { return $false }
    if (-not (Test-SamePath -A $Beat.agentScript -B $AgentScript)) { return $false }
    if ($null -ne $NotBeforeUtc) {
        try {
            $notBefore = ([datetime]$NotBeforeUtc).ToUniversalTime()
            if ((ConvertTo-UtcStart -Text $Beat.start) -lt $notBefore) { return $false }
            $proc = Get-LiveProcess -ProcessId $Beat.pid
            if ($null -eq $proc -or $proc.StartTime.ToUniversalTime() -lt $notBefore) { return $false }
        }
        catch { return $false }
    }
    if (-not (Test-AgentProcessAlive -Beat $Beat -RequireStart)) { return $false }
    if ($CheckCommandLine) {
        try { $argv = Get-ProcessArgv -ProcessId $Beat.pid } catch { return $false }
        if (-not (Test-ArgvBindsScript -Argv $argv -AgentScript $AgentScript)) { return $false }
    }
    return $true
}

function Get-ProcessArgv {
    # The process's OWN argument vector, read from the OS, never from a file the agent (or anyone) wrote: Win32_Process.CommandLine on
    # Windows (decoded by Split-TaskArguments), /proc/<pid>/cmdline elsewhere. Returns @{ ok; tokens } (executable first), ok=$false when
    # the process is gone or its command line cannot be read (another user's process without elevation reads as empty).
    param([int]$ProcessId)
    $none = [pscustomobject]@{ ok = $false; tokens = @() }
    if ($script:OnWindows) {
        $c = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if ($null -eq $c -or [string]::IsNullOrWhiteSpace($c.CommandLine)) { return $none }
        return (Split-TaskArguments -Text $c.CommandLine)
    }
    $raw = [System.IO.File]::ReadAllText("/proc/$ProcessId/cmdline")
    $parts = @($raw.Split([char]0) | Where-Object { $_ -ne '' })
    if ($parts.Count -lt 1) { return $none }
    return [pscustomobject]@{ ok = $true; tokens = $parts }
}

function Get-ProcessArgvBounded {
    # Get-ProcessArgv under the read deadline (WMI can stall); no answer reads as unreadable, never a hang with the lock held.
    param([int]$ProcessId)
    $r = Invoke-Bounded -Script { Get-ProcessArgv -ProcessId $ProcId } -TimeoutMs ($script:ReadTimeoutSec * 1000) -Variables @{ ProcId = $ProcessId }
    if ($r.timedOut -or $null -ne $r.error) { return $null }
    return $r.value
}

function Get-TaskRunningPids {
    # The pids of the RUNNING instances of the task, asked of the Task Scheduler itself (IRunningTask.EnginePID, which for an Exec
    # action is the pid of the process the action started). `schtasks /End` stops exactly these, so this is the identity the stop acts
    # on; a heartbeat file only CLAIMS a pid. Returns @{ ok; pids } ($ok false when the scheduler cannot be asked, and off Windows).
    # -TaskPidsFile is the test seam: the pids are read (digits, any separator) from that file instead.
    $no = [pscustomobject]@{ ok = $false; pids = @() }
    try {
        if ($TaskPidsFile -ne '') {
            $text = [System.IO.File]::ReadAllText($TaskPidsFile)
            return [pscustomobject]@{ ok = $true; pids = @([regex]::Matches($text, '\d+') | ForEach-Object { [int]$_.Value }) }
        }
        if (-not $script:OnWindows) { return $no }
        $svc = New-Object -ComObject 'Schedule.Service'
        $svc.Connect()
        $want = '\' + $TaskName.TrimStart('\')
        $found = @()
        foreach ($rt in $svc.GetRunningTasks(1)) {
            if ([string]::Equals([string]$rt.Path, $want, [StringComparison]::OrdinalIgnoreCase)) { $found += [int]$rt.EnginePID }
        }
        return [pscustomobject]@{ ok = $true; pids = $found }
    }
    catch { return $no }
}

function Get-TaskRunningPidsBounded {
    # Get-TaskRunningPids under the read deadline (the scheduler can stall); no answer reads as unreadable. Returns the pid array or $null.
    $r = Invoke-Bounded -Script { Get-TaskRunningPids } -TimeoutMs ($script:ReadTimeoutSec * 1000)
    if ($r.timedOut -or $null -ne $r.error -or $null -eq $r.value -or -not $r.value.ok) { return $null }
    return ,@($r.value.pids)   # the comma keeps an EMPTY array from unrolling to $null (which would read as "unreadable")
}

function Test-ArgvBindsScript {
    # A process argv (from Get-ProcessArgv) is a PowerShell host that runs <AgentScript> with this script's root.
    param($Argv, [string]$AgentScript)
    if ($null -eq $Argv -or -not $Argv.ok) { return $false }
    $all = @($Argv.tokens)
    if ($all.Count -lt 1) { return $false }
    $rest = @(); if ($all.Count -gt 1) { $rest = @($all[1..($all.Count - 1)]) }
    return (Test-HostArgvRunsScript -Exe $all[0] -Tokens $rest -AgentScript $AgentScript)
}

function Test-BeatIsDifferentProcess {
    # A restart yields a different PROCESS, not necessarily a different pid number: a reused pid with another start time counts.
    param($Old, $New)
    if ($Old.pid -ne $New.pid) { return $true }
    if ([string]::IsNullOrWhiteSpace($Old.start) -or [string]::IsNullOrWhiteSpace($New.start)) { return $false }
    try { return (-not (Test-StartTimesMatch -A (ConvertTo-UtcStart -Text $Old.start) -B (ConvertTo-UtcStart -Text $New.start))) }
    catch { return $false }
}

function Split-TaskArguments {
    # The argument string as the process receives it (the Windows rules of CommandLineToArgvW): whitespace outside double quotes
    # splits; 2n backslashes before a quote are n backslashes and the quote toggles; 2n+1 are n backslashes and a LITERAL quote,
    # so "C:\bench\" keeps its closing quote inside the argument (C:\bench"). A doubled quote inside a quoted run reads differently
    # between runtimes, so it is not guessed at: the whole string is unreadable and $null is returned (the caller refuses).
    param([string]$Text)
    $tokens = [System.Collections.Generic.List[string]]::new()
    $sb = [System.Text.StringBuilder]::new()
    $inQuote = $false; $has = $false
    $chars = $Text.ToCharArray()
    $i = 0
    while ($i -lt $chars.Length) {
        $ch = $chars[$i]
        if ($ch -eq [char]'\') {
            $n = 0
            while ($i -lt $chars.Length -and $chars[$i] -eq [char]'\') { $n++; $i++ }
            $has = $true
            if ($i -lt $chars.Length -and $chars[$i] -eq [char]'"') {
                [void]$sb.Append([char]'\', [int][Math]::Floor($n / 2))
                if ($n % 2 -eq 1) { [void]$sb.Append([char]'"'); $i++ }
            }
            else { [void]$sb.Append([char]'\', $n) }
            continue
        }
        if ($ch -eq [char]'"') {
            if ($inQuote -and $i + 1 -lt $chars.Length -and $chars[$i + 1] -eq [char]'"') { return [pscustomobject]@{ ok = $false; tokens = @() } }
            $inQuote = -not $inQuote; $has = $true; $i++
            continue
        }
        # Windows separates arguments at a space or a tab ONLY: a no-break space (U+00A0), a newline or any other Unicode space stays
        # inside the argument, so splitting on it would show a command line the process never receives
        if ((-not $inQuote) -and ($ch -eq [char]' ' -or $ch -eq [char]9)) {
            if ($has) { $tokens.Add($sb.ToString()); [void]$sb.Clear(); $has = $false }
            $i++
            continue
        }
        [void]$sb.Append($ch); $has = $true; $i++
    }
    if ($inQuote) { return [pscustomobject]@{ ok = $false; tokens = @() } }
    if ($has) { $tokens.Add($sb.ToString()) }
    return [pscustomobject]@{ ok = $true; tokens = $tokens.ToArray() }
}

function Test-ParamName {
    # "-Fi", "-file", "/FILE" are all spellings of File: a leading - or /, then a (case-insensitive) prefix of the full name.
    param([string]$Token, [string]$Full)
    if ($Token.Length -lt 2) { return $false }
    if ($Token[0] -ne [char]'-' -and $Token[0] -ne [char]'/') { return $false }
    $n = $Token.Substring(1)
    if ($n.Length -gt $Full.Length) { return $false }
    return $Full.StartsWith($n, [StringComparison]::OrdinalIgnoreCase)
}

function Get-TaskRootFromScriptArgs {
    # The arguments AFTER the script path are the agent's own. The agent serves -Root (its FIRST positional parameter too), which
    # defaults to its own folder. The only shapes accepted are: nothing, or ONE -Root <folder> / -Root:<folder> (spelled Root, Roo or
    # Ro; "-R" is ambiguous with RunOnce). Every other token is refused, not skipped: a positional value, a common parameter such
    # as -Verbose (a switch that would let a following path bind to -Root), another agent parameter, a -Root with no value.
    # Returns @{ ok; root }: root = the folder named by -Root, or $null when the task names none.
    param([string[]]$ScriptArgs)
    $root = $null
    $k = 0
    while ($k -lt $ScriptArgs.Count) {
        # \A ... \z, never ^ ... $: `$` also matches before a FINAL newline, and `.` stops at one, so "-Root:C:\bench<LF>" read as the
        # root C:\bench while the agent would receive C:\bench<LF>. A control character in an argument is refused outright (it can only
        # be an attempt to make the checked text differ from the received one); the anchors are the second line of defence.
        if ([string]$ScriptArgs[$k] -match '[\x00-\x1f\x7f]') { return @{ ok = $false; root = $null } }
        $m = [regex]::Match([string]$ScriptArgs[$k], '(?s)\A-([A-Za-z]+)(:(.*))?\z')
        if (-not $m.Success) { return @{ ok = $false; root = $null } }
        if ($m.Groups[1].Value.Length -lt 2 -or -not (Test-ParamName -Token ('-' + $m.Groups[1].Value) -Full 'Root')) { return @{ ok = $false; root = $null } }
        if ($null -ne $root) { return @{ ok = $false; root = $null } }
        if ($m.Groups[2].Success) { $val = $m.Groups[3].Value; $k++ }
        elseif ($k + 1 -lt $ScriptArgs.Count) { $val = $ScriptArgs[$k + 1]; $k += 2 }
        else { return @{ ok = $false; root = $null } }
        if ([string]::IsNullOrWhiteSpace($val) -or $val -match '[\x00-\x1f\x7f]') { return @{ ok = $false; root = $null } }
        $root = $val
    }
    return @{ ok = $true; root = $root }
}

function Test-TaskXmlRunsScript {
    # `schtasks /End` stops the WHOLE task, so the task must be exactly one action: a PowerShell host that runs <AgentScript> and
    # nothing else. The action's <Command> is pwsh/powershell; the host options before -File are an allowlist (-NoProfile, -NoLogo,
    # -NonInteractive, -ExecutionPolicy <v>, -WindowStyle <v>): anything else (-Command, -Version, -Help, a positional token) could
    # keep the script from running, so it is refused, not skipped. The FIRST -File is the absolute agent path, and the agent's own
    # arguments name no other root (Get-TaskRootFromScriptArgs). A second action of any kind refuses. A mention elsewhere
    # (Description, a later -File) is not the script the task runs. XML escapes (&amp;) are decoded by the parser, so a correct
    # task for an unusual path still binds.
    param([string]$Xml, [string]$AgentScript)
    try { $doc = [xml]$Xml } catch { return $false }
    $actions = @($doc.SelectNodes("//*[local-name()='Actions']/*"))
    if ($actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec') { return $false }
    $exec = $actions[0]
    $cmdNode = $exec.SelectSingleNode("*[local-name()='Command']")
    $argNode = $exec.SelectSingleNode("*[local-name()='Arguments']")
    if ($null -eq $cmdNode -or $null -eq $argNode) { return $false }
    $split = Split-TaskArguments -Text $argNode.InnerText
    if (-not $split.ok) { return $false }
    return (Test-HostArgvRunsScript -Exe $cmdNode.InnerText -Tokens @($split.tokens) -AgentScript $AgentScript)
}

function Test-HostArgvRunsScript {
    # THE one reading of "this command line runs <AgentScript> serving this root", shared by the task definition (what the task WILL
    # start) and the live process (what is RUNNING now: a definition edited after the instance started binds only the definition).
    # $Exe is the host executable, $Tokens the arguments after it, already decoded (Split-TaskArguments or /proc).
    param([string]$Exe, [string[]]$Tokens, [string]$AgentScript)
    # the script's folder. A script directly under a drive root (R:\agent.ps1) has the folder R:\, never "R:" (drive-relative: the current
    # directory of that drive); a script directly under the POSIX root has the folder /
    # every token is text that came from a task definition or a process: a control character in one (a newline most of all) makes the
    # text this check reads differ from the text the host receives, so it binds nothing (and `$` anchors below are \z for the same reason)
    foreach ($tk in @($Tokens)) { if ([string]$tk -match '[\x00-\x1f\x7f]') { return $false } }
    if ($Exe.Trim() -match '[\x00-\x1f\x7f]') { return $false }   # (the surrounding whitespace of a pretty-printed <Command> is trimmed, not refused)
    $expectedRoot = $AgentScript -replace '[\\/][^\\/]*\z', ''
    if ($expectedRoot -match '\A[A-Za-z]:\z') { $expectedRoot += '\' }
    elseif ($expectedRoot -eq '' -and $AgentScript -match '\A/') { $expectedRoot = '/' }
    $leaf = [regex]::Match($Exe.Trim().Trim('"'), '[^\\/]+\z').Value
    if ($leaf -notmatch '(?i)\A(pwsh|powershell)(\.exe)?\z') { return $false }
    $tokens = @($Tokens)
    $hostFlags = @('NoProfile', 'nop', 'NoLogo', 'nol', 'NonInteractive', 'noni')
    # -NoProfile is REQUIRED: without it the host loads a profile, which can set PSDefaultParameterValues for the agent's -Root and so
    # point the agent at a root this check never saw (the argv then names no -Root, and the default is whatever the profile says)
    $sawNoProfile = $false
    # an explicit -ExecutionPolicy is REQUIRED too: without one the host applies the account's persistent policy, which may be
    # Restricted today even though the running agent started under another one, so the replacement script could be blocked
    $sawPolicy = $false
    # only the policies under which a LOCAL, unsigned script runs: Restricted and AllSigned (and Default / Undefined, which resolve to
    # Restricted on a Windows client) would keep the replacement script from running, so the restart would stop the agent and not bring it back
    $policies = @('Bypass', 'RemoteSigned', 'Unrestricted')
    $styles = @('Hidden', 'Normal', 'Minimized', 'Maximized')
    $hostValued = @{ ExecutionPolicy = $policies; ep = $policies; WindowStyle = $styles; w = $styles }
    $scriptPath = $null; $i = 0
    while ($i -lt $tokens.Count) {
        $t = $tokens[$i]
        if (Test-ParamName -Token $t -Full 'File') {
            if ($i + 1 -lt $tokens.Count) { $scriptPath = $tokens[$i + 1]; $i += 2 }
            break
        }
        if ($t.Length -lt 2 -or ($t[0] -ne [char]'-' -and $t[0] -ne [char]'/')) { return $false }
        $name = $t.Substring(1)
        if ($hostFlags -contains $name) { if ($name -eq 'NoProfile' -or $name -eq 'nop') { $sawNoProfile = $true }; $i++; continue }
        # a valued option takes ONLY one of its known values (a value of -File, or a path, would shift what the host runs)
        if ($hostValued.ContainsKey($name) -and $i + 1 -lt $tokens.Count -and $hostValued[$name] -contains $tokens[$i + 1]) {
            if ($name -eq 'ExecutionPolicy' -or $name -eq 'ep') { $sawPolicy = $true }
            $i += 2; continue
        }
        return $false
    }
    if ($null -eq $scriptPath) { return $false }
    if (-not $sawNoProfile -or -not $sawPolicy) { return $false }
    if (-not (Test-AbsolutePathText -P $scriptPath)) { return $false }
    if (-not (Test-SamePath -A $scriptPath -B $AgentScript)) { return $false }
    $rest = @(); if ($i -lt $tokens.Count) { $rest = @($tokens[$i..($tokens.Count - 1)]) }
    $parsed = Get-TaskRootFromScriptArgs -ScriptArgs $rest
    if (-not $parsed.ok) { return $false }
    if ($null -ne $parsed.root -and -not (Test-SamePath -A $parsed.root -B $expectedRoot)) { return $false }
    return $true
}

function Get-TaskXml {
    # The task's CURRENT definition (schtasks /Query /XML, bounded; -TaskXmlFile is the test seam). Returns @{ ok; xml; why }.
    $no = { param($why) [pscustomobject]@{ ok = $false; xml = $null; why = $why } }
    try {
        if ($TaskXmlFile -ne '') { return [pscustomobject]@{ ok = $true; xml = [System.IO.File]::ReadAllText($TaskXmlFile); why = $null } }
        $q = Invoke-BoundedProcess -FilePath 'schtasks.exe' -Arguments @('/Query', '/TN', $TaskName, '/XML') -TimeoutSec 30
        if ($q.timedOut) { return (& $no "task $TaskName did not answer schtasks /Query within 30 s") }
        if ($q.exitCode -ne 0) { return (& $no "task $TaskName could not be queried (exit $($q.exitCode))") }
        return [pscustomobject]@{ ok = $true; xml = $q.output; why = $null }
    }
    catch { return (& $no "task $TaskName could not be read: $($_.Exception.Message)") }
}

function Get-RunningTaskInstances {
    # ALL running task instances, as the Task Scheduler's IRunningTask objects (Path, EnginePID, Stop()); the caller picks its own by
    # path, pid and start. Windows only: off Windows it throws, and the caller reads that as "nothing was stopped". This is the one
    # place the scheduler is asked, and the seam the tests replace with stand-in objects that have the same three members.
    if (-not $script:OnWindows) { throw 'the Task Scheduler API exists on Windows only' }
    $svc = New-Object -ComObject 'Schedule.Service'
    $svc.Connect()
    $all = @()
    foreach ($rt in $svc.GetRunningTasks(1)) { $all += $rt }
    return $all
}

function Stop-TaskInstanceByPid {
    # Stops ONE running instance of the task: the one whose process is $ProcessId AND whose OS start time is $StartText (the start the
    # binding proved: a pid alone can be reused by another instance of the same task after the proved one exited), found and stopped
    # through the Task Scheduler (IRunningTask.Stop). `schtasks /End /TN <name>` stops whatever the NAME resolves to when it runs;
    # this acts on the identity the binding proved. The venue is judged idle AGAIN here, with the instance object in hand and
    # immediately before Stop(): the lookup before it can take up to its deadline, and work can land meanwhile.
    # Returns @{ outcome; why; busy } where outcome is one of
    #   'stopped'      Stop() returned (the caller still waits for the process to go)
    #   'not-stopped'  Stop() was never called: no instance with that pid and start, the venue was busy (busy = the refusal lines), or the
    #                  scheduler failed BEFORE the call
    #   'unknown'      Stop() was called and raised: whether it was applied is not known, so the caller re-observes the pid
    param([int]$ProcessId, [string]$StartText)
    $notStopped = { param($why, $busy = $null) [pscustomobject]@{ outcome = 'not-stopped'; why = $why; busy = $busy } }
    try {
        if ([string]::IsNullOrWhiteSpace($StartText)) { return (& $notStopped 'the proved start time of the agent is missing, so no instance can be identified') }
        $mine = $null
        $want = '\' + $TaskName.TrimStart('\')
        foreach ($rt in @(Get-RunningTaskInstances)) {
            if (-not [string]::Equals([string]$rt.Path, $want, [StringComparison]::OrdinalIgnoreCase)) { continue }
            if ([int]$rt.EnginePID -ne $ProcessId) { continue }
            # the instance object names a pid; the process under that pid must be the one proved (pid plus start), not a reuse of it
            $proc = Get-LiveProcess -ProcessId $ProcessId
            if ($null -eq $proc) { continue }
            # $StartText is the start the OS reported for this pid at the binding, so the two reads of ONE process agree to well under
            # a second; a reuse of the pid starts a different process (a heartbeat's 2 s tolerance would admit a reuse 0.5 s later)
            if ([Math]::Abs(($proc.StartTime.ToUniversalTime() - (ConvertTo-UtcStart -Text $StartText)).TotalMilliseconds) -gt $script:OsStartToleranceMs) { continue }
            $mine = $rt
            break
        }
        if ($null -eq $mine) { return (& $notStopped "task $TaskName has no running instance with pid=$ProcessId and the proved start $StartText any more") }
        $busy = Get-Refusals
        if ($null -ne $busy) { return (& $notStopped 'the venue is no longer idle' @($busy.lines)) }
    }
    catch { return (& $notStopped "looking for the instance failed: $($_.Exception.Message)") }
    try { $mine.Stop() }
    catch { return [pscustomobject]@{ outcome = 'unknown'; why = "IRunningTask.Stop raised: $($_.Exception.Message)"; busy = $null } }
    return [pscustomobject]@{ outcome = 'stopped'; why = $null; busy = $null }
}

function Stop-TaskInstanceBounded {
    # Stop-TaskInstanceByPid under a deadline (the scheduler can stall). The deadline covers the lookup, the idle read and Stop(), so a
    # timeout cannot tell whether Stop() ran: it is 'unknown', never "nothing was stopped" (a COM call that outlives the deadline may
    # still have been applied). The caller re-observes the pid.
    param([int]$ProcessId, [string]$StartText)
    $r = Invoke-Bounded -Script { Stop-TaskInstanceByPid -ProcessId $ProcId -StartText $StartAt } -TimeoutMs 60000 -Variables @{ ProcId = $ProcessId; StartAt = $StartText }
    if ($r.timedOut) { return [pscustomobject]@{ outcome = 'unknown'; why = 'the Task Scheduler did not answer the stop within 60 s'; busy = $null } }
    if ($null -ne $r.error -or $null -eq $r.value) { return [pscustomobject]@{ outcome = 'unknown'; why = "the stop returned no answer ($($r.error))"; busy = $null } }
    return $r.value
}

function Get-RestartBindingRefusal {
    # -RestartMode when-idle without -RestartScript: the schtasks End/Run act on THIS machine's task, so the root that was checked
    # idle must be the root of the agent that task runs. Returns a reason string, or $null when bound.
    # On success the heartbeat that was validated (live pid WITH its recorded start, this host, this root's script) is left in
    # $script:BoundBeat: it is the identity the stop acts on, so the restart re-runs this check right before the stop and uses it.
    param([string]$AgentScript)
    $script:BoundBeat = $null
    $beat = Read-HeartbeatBounded
    if ($null -eq $beat) { return 'heartbeat.txt is missing, unreadable or did not answer in time, so the agent host cannot be confirmed' }
    if (-not [string]::Equals($beat.host, $env:COMPUTERNAME, [StringComparison]::OrdinalIgnoreCase)) {
        return "heartbeat host=$($beat.host) is not this machine ($env:COMPUTERNAME); run it on the agent host"
    }
    if (-not (Test-SamePath -A $beat.agentScript -B $AgentScript)) {
        return "heartbeat agentScript=$($beat.agentScript) is not $AgentScript; the live agent is not this root's agent (use the agent's local root)"
    }
    if (-not (Test-AgentProcessAlive -Beat $beat -RequireStart)) {
        return "heartbeat pid=$($beat.pid) is not a live agent process with the recorded start time; start the agent task by hand, then deploy"
    }
    # the start the OS itself reports for that pid, right now: the stop is later handed THIS (not the heartbeat's claim, which is only
    # held to the 2 s tolerance above) and compares the process under the pid with it
    try {
        $liveProc = Get-LiveProcess -ProcessId $beat.pid
        if ($null -eq $liveProc) { return "heartbeat pid=$($beat.pid) is no longer a running process" }
        $osStart = $liveProc.StartTime.ToUniversalTime()
        if (-not (Test-StartTimesMatch -A $osStart -B (ConvertTo-UtcStart -Text $beat.start))) { return "heartbeat pid=$($beat.pid) no longer has the recorded start time" }
        Add-Member -InputObject $beat -NotePropertyName osStart -NotePropertyValue $osStart.ToString('o') -Force
    }
    catch { return "the OS start time of heartbeat pid=$($beat.pid) could not be read: $($_.Exception.Message)" }
    # the RUNNING instance's own command line, read from the OS: a task definition edited after the instance started would bind the
    # definition while the live agent still serves another root. Both must say: a PowerShell host, -File <this script>, this root.
    $argv = Get-ProcessArgvBounded -ProcessId $beat.pid
    if ($null -eq $argv -or -not $argv.ok) {
        return "the command line of heartbeat pid=$($beat.pid) could not be read (the process is gone, WMI did not answer in $($script:ReadTimeoutSec) s, or it belongs to another user: run elevated), so the root the live agent serves is unconfirmed"
    }
    if (-not (Test-ArgvBindsScript -Argv $argv -AgentScript $AgentScript)) {
        return "heartbeat pid=$($beat.pid) is not running -File $AgentScript for this root (its own command line differs); ending the task would stop an agent serving another root"
    }
    $x = Get-TaskXml
    if (-not $x.ok) { return $x.why }
    if (-not (Test-TaskXmlRunsScript -Xml $x.xml -AgentScript $AgentScript)) {
        return "task $TaskName does not run -File $AgentScript in an executable action; ending it would stop a different agent"
    }
    # the pid the heartbeat names must be the pid of the task's RUNNING instance: End stops that instance and no other, so a process in
    # another session that writes a matching heartbeat (and a task instance serving a different root) must not satisfy this
    $running = Get-TaskRunningPidsBounded
    if ($null -eq $running) { return "task $TaskName running instances could not be read from the Task Scheduler, so the process it would end is unconfirmed" }
    if (@($running).Count -ne 1) { return "task $TaskName has $(@($running).Count) running instance(s) ($(@($running) -join ',')), not exactly one; the stop would not act on the agent that was checked" }
    if ([int]$running[0] -ne [int]$beat.pid) { return "task $TaskName is running pid=$($running[0]), not heartbeat pid=$($beat.pid): the live agent that was checked is not the task instance that ending the task would stop" }
    $script:BoundBeat = $beat
    return $null
}

function Invoke-AgentRestart {
    # Returns @{ why; warn }: why = $null on success, else a reason. See the header: the agent's one start path is the scheduled task.
    param([string]$AgentScript)
    $pre = Get-Refusals
    if ($null -ne $pre) { return @{ why = "the venue is no longer idle, so the agent was not restarted: $($pre.lines -join '; ')"; warn = $null } }
    if ($RestartScript -ne '') {
        $global:LASTEXITCODE = 0
        try { & $RestartScript -VenueRoot $VenueRoot | Out-Null }
        catch { return @{ why = "restart script threw: $($_.Exception.Message)"; warn = $null } }
        if ($LASTEXITCODE -ne 0) { return @{ why = "restart script exited $LASTEXITCODE"; warn = $null } }
        return @{ why = $null; warn = (Get-ArrivalWarning) }
    }
    # the binding was proved before the deploy; the heartbeat and the task can change since, so prove it again NOW and stop only the
    # process that proof identified (pid AND recorded start), never a heartbeat read separately that might carry no start
    $again = Get-RestartBindingRefusal -AgentScript $AgentScript
    if ($null -ne $again) { return @{ why = "the agent binding no longer holds, so nothing was stopped: $again"; warn = $null } }
    $before = $script:BoundBeat
    # the binding above reads the heartbeat, the process command line, the task definition and the scheduler, each up to its deadline:
    # work can land meanwhile, so idle is judged AGAIN as the last step before the End (the check before the binding is only the early out)
    $last = Get-Refusals
    if ($null -ne $last) { return @{ why = "the venue is no longer idle, so the agent was not stopped: $($last.lines -join '; ')"; warn = $null } }
    # stop the INSTANCE the binding proved (its pid AND its recorded start), not "the task" by a name that can be re-pointed or re-run in
    # the meantime. The stop judges idle once more itself, with the instance in hand and right before Stop(), so the last idle read
    # above is only an early out. Its answer is three-valued: 'unknown' (no answer in time, or Stop() raised) may still have stopped the agent.
    # a wall clock and a monotonic watch, read together: if they disagree later, the clock was stepped, and every "started at or after
    # /Run" comparison below (which reads the wall clock) can no longer be trusted
    $clockUtc0 = [DateTime]::UtcNow
    $clockWatch = [System.Diagnostics.Stopwatch]::StartNew()
    # the start handed to the stop is the one the OS reported for the pid at the binding (osStart), compared with a tight tolerance there
    $stop = Stop-TaskInstanceBounded -ProcessId $before.pid -StartText $(if (-not [string]::IsNullOrWhiteSpace([string]$before.osStart)) { [string]$before.osStart } else { [string]$before.start })
    if ($stop.outcome -eq 'not-stopped') {
        if ($null -ne $stop.busy) { return @{ why = "the venue is no longer idle, so the agent was not stopped: $(@($stop.busy) -join '; ')"; warn = $null } }
        return @{ why = "could not stop agent pid=$($before.pid); nothing was stopped: $($stop.why)"; warn = $null }
    }
    # 'stopped' or 'unknown': either way the pid is re-observed, and only an OBSERVED exit lets the restart go on to the start
    $gone = Wait-Until -TimeoutSec $script:StopWaitSec -Variables @{ before = $before } -Condition { -not (Test-AgentProcessAlive -Beat $before) }
    if (-not $gone) {
        if ($stop.outcome -ne 'stopped') { return @{ why = "the stop of agent pid=$($before.pid) has an UNKNOWN outcome ($($stop.why)) and the agent is still alive $($script:StopWaitSec) s later; the task was NOT started"; warn = $null } }
        return @{ why = "agent pid $($before.pid) still alive $($script:StopWaitSec) s after its task instance was stopped"; warn = $null }
    }
    $warn = Get-ArrivalWarning
    # /Run with the task's default IgnoreNew policy does NOT start a second instance: if a trigger (or anything else) already started one
    # after the old agent exited, /Run succeeds and that earlier instance would be attested as the replacement. So the task must have NO
    # running instance right before /Run (it may take a moment for the scheduler to drop the instance whose process just exited)
    $idleTask = Wait-Until -TimeoutSec $script:TaskIdleWaitSec -Condition { $tp = Get-TaskRunningPids; $tp.ok -and (@($tp.pids).Count -eq 0) }
    if (-not $idleTask) {
        $left = Get-TaskRunningPidsBounded
        $what = $(if ($null -eq $left) { 'its running instances could not be read from the Task Scheduler' } else { "task $TaskName already has a running instance (pid $(@($left) -join ',')) that this restart did not start, so /Run would not start a new one and the replacement could not be proved" })
        return @{ why = "the old agent is stopped but the task was NOT started: $what"; warn = $warn }
    }
    # the definition can be edited between the binding and now (and during the wait above), and /Run STARTS whatever it says: read it
    # again as the LAST step before the start, AFTER every wait, and refuse to start a definition that no longer runs this script and
    # root (the old agent is down: said so)
    $nowXml = Get-TaskXml
    if (-not $nowXml.ok) { return @{ why = "the old agent is stopped but the task was NOT started: $($nowXml.why)"; warn = $warn } }
    if (-not (Test-TaskXmlRunsScript -Xml $nowXml.xml -AgentScript $AgentScript)) {
        return @{ why = "the old agent is stopped but the task was NOT started: its definition changed and no longer runs -File $AgentScript for this root"; warn = $warn }
    }
    $runAt = Get-RunLowerBound -Utc0 $clockUtc0 -Watch $clockWatch
    $run = Invoke-BoundedProcess -FilePath 'schtasks.exe' -Arguments @('/Run', '/TN', $TaskName) -TimeoutSec 30
    if ($run.timedOut) { return @{ why = "schtasks /Run $TaskName did not return within 30 s"; warn = $warn } }
    if ($run.exitCode -ne 0) { return @{ why = "schtasks /Run $TaskName exited $($run.exitCode)"; warn = $warn } }
    $up = Wait-Until -TimeoutSec 60 -Variables @{ before = $before; AgentScript = $AgentScript; runAt = $runAt } -Condition {
        $now = Read-Heartbeat
        if (($null -eq $now) -or ($now.ageSec -ge 30) -or -not (Test-BeatIsDifferentProcess -Old $before -New $now)) { return $false }
        # the replacement is the task's own new instance (not just a process that wrote a heartbeat), and it runs this script and root
        $tp = Get-TaskRunningPids
        if (-not $tp.ok) { return $false }
        Test-NewHeartbeatIsLiveAgent -Beat $now -AgentScript $AgentScript -NotBeforeUtc $runAt -CheckCommandLine -TaskPids $tp.pids
    } -PollMs 1000
    if (-not $up) { return @{ why = 'no heartbeat from a new LIVE agent process within 60 s of schtasks /Run'; warn = $warn } }
    if (Test-ClockStepped -Utc0 $clockUtc0 -Watch $clockWatch) {
        return @{ why = 'the system clock was stepped during the restart, so "started at or after /Run" cannot be proved for the new agent; verify it by hand'; warn = $warn }
    }
    return @{ why = $null; warn = $warn }
}

# Dot-sourced (`. <script> -VenueRoot <x>`): the functions above only.
if ($MyInvocation.InvocationName -eq '.') { return }

$lock = $null
try {
    # 1. source
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        Out-Typed "DEPLOY_SOURCE_MISSING $Source"
        exit 2
    }
    $srcBytes = [System.IO.File]::ReadAllBytes($Source)
    $srcSha = Get-BytesSha256 -Bytes $srcBytes

    # 2. venue root, readable even for a no-op
    if (-not (Test-Path -LiteralPath $VenueRoot -PathType Container)) {
        Out-Typed "DEPLOY_REFUSED_UNREADABLE venue root is not a readable folder: $VenueRoot"
        exit 12
    }
    $readable = Get-VenueState
    if ($null -ne $readable.unreadable) {
        Out-Typed "DEPLOY_REFUSED_UNREADABLE $($readable.unreadable)"
        exit 12
    }
    $dest = Join-Path $VenueRoot $agentName

    # 3. the deploy lock (a dry run changes nothing, so it takes none)
    if (-not $DryRun) {
        try {
            $lock = [System.IO.FileStream]::new((Join-Path $VenueRoot 'deploy.lock'), [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::DeleteOnClose)
        }
        catch {
            Out-Typed "DEPLOY_REFUSED_LOCKED another deployer holds (or a crash left) $(Join-Path $VenueRoot 'deploy.lock'): $($_.Exception.Message)"
            exit 17
        }
    }

    # 4. idempotent
    $destExists = Test-Path -LiteralPath $dest -PathType Leaf
    $destSha = $null
    if ($destExists) {
        try { $destSha = Get-FileSha256Bytes -Path $dest }
        catch {
            Out-Typed "DEPLOY_REFUSED_UNREADABLE cannot read the venue copy: $($_.Exception.Message)"
            exit 12
        }
    }
    if ($destExists -and $destSha -ceq $srcSha) {
        Out-Typed "DEPLOY_NOOP sha=$srcSha"
        exit 0
    }

    # restart binding precheck: nothing may change if the restart could not be done safely afterwards
    if ($RestartMode -eq 'when-idle' -and $RestartScript -eq '') {
        $bind = Get-RestartBindingRefusal -AgentScript $dest
        if ($null -ne $bind) {
            Out-Typed "DEPLOY_REFUSED_RESTART_HOST $bind"
            exit 16
        }
    }

    # 5. busy / lease / unreadable
    $ref = Get-Refusals
    if ($null -ne $ref) {
        foreach ($l in $ref.lines) { Out-Typed $l }
        exit $ref.code
    }

    if ($DryRun) {
        Out-Typed "DEPLOY_DRYRUN action=deploy sha=$srcSha restart=$RestartMode"
        exit 0
    }

    # 6. rollback copy, then temp-and-rename
    $rollbackFile = $null
    if ($destExists) {
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmss') + 'Z'
        $rbDir = Join-Path $VenueRoot "rollback-$stamp"
        $n = 1
        while (Test-Path -LiteralPath $rbDir) { $n++; $rbDir = Join-Path $VenueRoot ("rollback-{0}-{1}" -f $stamp, $n) }
        [void][System.IO.Directory]::CreateDirectory($rbDir)
        $rollbackFile = Join-Path $rbDir $agentName
        $oldBytes = [System.IO.File]::ReadAllBytes($dest)
        Write-BytesAtomic -Path $rollbackFile -Bytes $oldBytes
        if ((Get-FileSha256Bytes -Path $rollbackFile) -cne $destSha) {
            Out-Typed "DEPLOY_ERROR the rollback copy does not match the venue copy; nothing was replaced (restore=not-needed)"
            exit 15
        }
    }
    try { Write-BytesAtomic -Path $dest -Bytes $srcBytes }
    catch {
        Out-Typed "DEPLOY_ERROR writing the venue copy failed: $($_.Exception.Message) (restore=not-needed, the rename is atomic)"
        exit 15
    }
    if ($TestCorruptAfterWrite) {
        $fs = [System.IO.File]::Open($dest, [System.IO.FileMode]::Append)
        try { $fs.WriteByte(0x20) } finally { $fs.Dispose() }
    }

    # 7. verify on the venue
    $after = $null
    try { $after = Get-FileSha256Bytes -Path $dest } catch { $after = "unreadable: $($_.Exception.Message)" }
    if ($after -cne $srcSha) {
        $restored = 'no'
        try {
            if ($null -ne $rollbackFile) {
                Write-BytesAtomic -Path $dest -Bytes ([System.IO.File]::ReadAllBytes($rollbackFile))
                if ((Get-FileSha256Bytes -Path $dest) -ceq $destSha) { $restored = 'yes' }
            }
            else {
                Remove-Item -LiteralPath $dest -Force
                $restored = 'removed-first-deploy'
            }
        }
        catch { $restored = "failed: $($_.Exception.Message)" }
        Out-Typed "DEPLOY_SHA_MISMATCH expected=$srcSha actual=$after restored=$restored"
        exit 13
    }

    # 8. restart, only when idle at this moment
    $restarted = 'no'
    if ($RestartMode -eq 'when-idle') {
        $res = Invoke-AgentRestart -AgentScript $dest
        if ($null -ne $res.warn) { Out-Typed $res.warn }
        if ($null -ne $res.why) {
            Out-Typed "DEPLOY_RESTART_FAILED $($res.why)"
            exit 14
        }
        $restarted = 'yes'
    }

    # 9. done
    $rbText = if ($null -ne $rollbackFile) { $rollbackFile } else { 'none' }
    Out-Typed "DEPLOY_OK sha=$srcSha rollback=$rbText restarted=$restarted"
    exit 0
}
catch {
    Out-Typed "DEPLOY_ERROR $($_.Exception.Message)"
    exit 15
}
finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
