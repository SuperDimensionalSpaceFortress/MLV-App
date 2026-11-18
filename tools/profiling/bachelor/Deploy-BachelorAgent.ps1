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
#   schtasks.exe /End /TN \MlvAgentBachelor, wait until the pid in heartbeat.txt is gone (start time checked, never a kill by
#   name), schtasks.exe /Run /TN \MlvAgentBachelor, wait for a heartbeat from a new LIVE agent process. It acts on THIS
#   machine's task list, so -RestartMode when-idle is refused (16) before anything changes unless ALL hold: heartbeat.txt
#   names this machine as host=; its agentScript= is <VenueRoot>\ultra-magnus-agent.ps1 (a copied heartbeat from another
#   root fails here); its pid is alive with the recorded start time; and the task's own XML has an executable action
#   `-File <that script path>`: the action's <Command> is pwsh/powershell, the FIRST -File (no -Command/-EncodedCommand before it,
#   no positional token before it) is that absolute path, and the agent's own arguments after it name no other root (-Root, or a
#   positional first argument, must be this folder; a path mentioned only in the description or a later argument does not count).
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
# -TaskXmlFile <xml> is read instead of `schtasks /Query /XML`; -TestCorruptAfterWrite appends a byte to the venue copy after
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
    [switch]$TestCorruptAfterWrite
)

$ErrorActionPreference = 'Stop'
$agentName = 'ultra-magnus-agent.ps1'
# ONE tolerance for "same process start": the heartbeat's processStartUtc against the live process (alive check) and the old
# heartbeat against the new one (replacement check). Same iff the gap is within it, in BOTH checks (VenueChain.psm1 uses 2 s too).
$script:StartToleranceSec = 2
# the longest a single read of the venue (a listing, the heartbeat) may take before it counts as unanswered
$script:ReadTimeoutSec = 20
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
    $iss.ExecutionPolicy = [Microsoft.PowerShell.ExecutionPolicy]::Bypass
    $rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($iss)
    $rs.Open()
    $rs.SessionStateProxy.SetVariable('__self', $script:SelfPath)
    $rs.SessionStateProxy.SetVariable('VenueRoot', $VenueRoot)
    $rs.SessionStateProxy.SetVariable('TaskName', $TaskName)
    $rs.SessionStateProxy.SetVariable('TaskXmlFile', $TaskXmlFile)
    foreach ($k in $Variables.Keys) { $rs.SessionStateProxy.SetVariable([string]$k, $Variables[$k]) }
    $ps = [System.Management.Automation.PowerShell]::Create($rs)
    [void]$ps.AddScript(('. $__self -VenueRoot $VenueRoot -TaskName $TaskName -TaskXmlFile $TaskXmlFile' + "`n" + $Script.ToString()))
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
    if (-not $p.WaitForExit([int][Math]::Min([long]$TimeoutSec * 1000, 2000000000L))) {
        try { $p.Kill($true) } catch { }
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
    # A drive path, a UNC path or a rooted POSIX path. A relative path is resolved against a working directory this script cannot
    # see for another process (a task's WorkingDirectory, the agent's own cwd), so it never counts as naming a place.
    param([string]$P)
    return ($P -match '^([A-Za-z]:[\\/]|\\\\|/)')
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
                Where-Object { $_ -match '\.(lease|holder|acquired)\.json$' } |
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

function Test-AgentProcessAlive {
    # Alive AND the same process the heartbeat recorded (pid plus start time, so a reused pid does not count).
    param($Beat, [switch]$RequireStart)
    if ($null -eq $Beat) { return $false }
    $proc = Get-Process -Id $Beat.pid -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return $false }
    if ([string]::IsNullOrWhiteSpace($Beat.start)) { return (-not $RequireStart) }
    try { return (Test-StartTimesMatch -A $proc.StartTime.ToUniversalTime() -B (ConvertTo-UtcStart -Text $Beat.start)) }
    catch { return (-not $RequireStart) }
}

function Test-NewHeartbeatIsLiveAgent {
    # A restart is confirmed only by a heartbeat from a process that EXISTS with the recorded start time, names THIS machine and this
    # root's script, is not this deployer, and (with -NotBeforeUtc) was STARTED at or after the moment the task was started: a
    # process that was already running when /Run was issued is not the replacement, whatever its heartbeat claims.
    param($Beat, [string]$AgentScript, $NotBeforeUtc = $null)
    if ($null -eq $Beat) { return $false }
    if ([string]::IsNullOrWhiteSpace($Beat.start)) { return $false }
    if (-not [string]::Equals($Beat.host, $env:COMPUTERNAME, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    if ($Beat.pid -eq $PID) { return $false }
    if (-not (Test-SamePath -A $Beat.agentScript -B $AgentScript)) { return $false }
    if ($null -ne $NotBeforeUtc) {
        try { if ((ConvertTo-UtcStart -Text $Beat.start) -lt ([datetime]$NotBeforeUtc).AddSeconds(-$script:StartToleranceSec)) { return $false } }
        catch { return $false }
    }
    return (Test-AgentProcessAlive -Beat $Beat -RequireStart)
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
        $m = [regex]::Match($ScriptArgs[$k], '^-([A-Za-z]+)(:(.*))?$')
        if (-not $m.Success) { return @{ ok = $false; root = $null } }
        if ($m.Groups[1].Value.Length -lt 2 -or -not (Test-ParamName -Token ('-' + $m.Groups[1].Value) -Full 'Root')) { return @{ ok = $false; root = $null } }
        if ($null -ne $root) { return @{ ok = $false; root = $null } }
        if ($m.Groups[2].Success) { $val = $m.Groups[3].Value; $k++ }
        elseif ($k + 1 -lt $ScriptArgs.Count) { $val = $ScriptArgs[$k + 1]; $k += 2 }
        else { return @{ ok = $false; root = $null } }
        if ([string]::IsNullOrWhiteSpace($val)) { return @{ ok = $false; root = $null } }
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
    $expectedRoot = $AgentScript -replace '[\\/][^\\/]*$', ''
    $actions = @($doc.SelectNodes("//*[local-name()='Actions']/*"))
    if ($actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec') { return $false }
    $exec = $actions[0]
    $cmdNode = $exec.SelectSingleNode("*[local-name()='Command']")
    $argNode = $exec.SelectSingleNode("*[local-name()='Arguments']")
    if ($null -eq $cmdNode -or $null -eq $argNode) { return $false }
    $leaf = [regex]::Match($cmdNode.InnerText.Trim().Trim('"'), '[^\\/]+$').Value
    if ($leaf -notmatch '(?i)^(pwsh|powershell)(\.exe)?$') { return $false }
    $split = Split-TaskArguments -Text $argNode.InnerText
    if (-not $split.ok) { return $false }
    $tokens = @($split.tokens)
    $hostFlags = @('NoProfile', 'nop', 'NoLogo', 'nol', 'NonInteractive', 'noni')
    $policies = @('Bypass', 'RemoteSigned', 'Unrestricted', 'AllSigned', 'Restricted', 'Default', 'Undefined')
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
        if ($hostFlags -contains $name) { $i++; continue }
        # a valued option takes ONLY one of its known values (a value of -File, or a path, would shift what the host runs)
        if ($hostValued.ContainsKey($name) -and $i + 1 -lt $tokens.Count -and $hostValued[$name] -contains $tokens[$i + 1]) { $i += 2; continue }
        return $false
    }
    if ($null -eq $scriptPath) { return $false }
    if ($scriptPath -notmatch '^([A-Za-z]:[\\/]|\\\\|/)') { return $false }
    if (-not (Test-SamePath -A $scriptPath -B $AgentScript)) { return $false }
    $rest = @(); if ($i -lt $tokens.Count) { $rest = @($tokens[$i..($tokens.Count - 1)]) }
    $parsed = Get-TaskRootFromScriptArgs -ScriptArgs $rest
    if (-not $parsed.ok) { return $false }
    if ($null -ne $parsed.root -and -not (Test-SamePath -A $parsed.root -B $expectedRoot)) { return $false }
    return $true
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
    $xml = $null
    try {
        if ($TaskXmlFile -ne '') { $xml = [System.IO.File]::ReadAllText($TaskXmlFile) }
        else {
            $q = Invoke-BoundedProcess -FilePath 'schtasks.exe' -Arguments @('/Query', '/TN', $TaskName, '/XML') -TimeoutSec 30
            if ($q.timedOut) { return "task $TaskName did not answer schtasks /Query within 30 s" }
            if ($q.exitCode -ne 0) { return "task $TaskName could not be queried (exit $($q.exitCode))" }
            $xml = $q.output
        }
    }
    catch { return "task $TaskName could not be read: $($_.Exception.Message)" }
    if (-not (Test-TaskXmlRunsScript -Xml $xml -AgentScript $AgentScript)) {
        return "task $TaskName does not run -File $AgentScript in an executable action; ending it would stop a different agent"
    }
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
    $end =Invoke-BoundedProcess -FilePath 'schtasks.exe' -Arguments @('/End', '/TN', $TaskName) -TimeoutSec 30
    if ($end.timedOut) { return @{ why = "schtasks /End $TaskName did not return within 30 s"; warn = $null } }
    $gone = Wait-Until -TimeoutSec 30 -Variables @{ before = $before } -Condition { -not (Test-AgentProcessAlive -Beat $before) }
    if (-not $gone) { return @{ why = "agent pid $($before.pid) still alive 30 s after schtasks /End"; warn = $null } }
    $warn = Get-ArrivalWarning
    $runAt = [DateTime]::UtcNow
    $run = Invoke-BoundedProcess -FilePath 'schtasks.exe' -Arguments @('/Run', '/TN', $TaskName) -TimeoutSec 30
    if ($run.timedOut) { return @{ why = "schtasks /Run $TaskName did not return within 30 s"; warn = $warn } }
    if ($run.exitCode -ne 0) { return @{ why = "schtasks /Run $TaskName exited $($run.exitCode)"; warn = $warn } }
    $up = Wait-Until -TimeoutSec 60 -Variables @{ before = $before; AgentScript = $AgentScript; runAt = $runAt } -Condition {
        $now = Read-Heartbeat
        ($null -ne $now) -and ($now.ageSec -lt 30) -and (Test-BeatIsDifferentProcess -Old $before -New $now) -and (Test-NewHeartbeatIsLiveAgent -Beat $now -AgentScript $AgentScript -NotBeforeUtc $runAt)
    } -PollMs 1000
    if (-not $up) { return @{ why = 'no heartbeat from a new LIVE agent process within 60 s of schtasks /Run'; warn = $warn } }
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
