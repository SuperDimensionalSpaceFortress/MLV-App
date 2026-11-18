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
#   root fails here); its pid is alive with the recorded start time; and the task's own XML names that same script path.
#   The idle gate keeps the End from stranding a lease: with running\, inbox\ and slot-holders\ empty, nothing is lost.
#   Waits use a monotonic Stopwatch, never the wall clock.
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

function Wait-Until {
    # Bounded by a monotonic Stopwatch: a wall-clock step cannot stretch or cut the wait.
    param([int]$TimeoutSec, [scriptblock]$Condition, [int]$PollMs = 500)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        if (& $Condition) { return $true }
        if ($sw.Elapsed.TotalSeconds -ge $TimeoutSec) { return $false }
        Start-Sleep -Milliseconds $PollMs
    }
}

function Test-SamePath {
    param([string]$A, [string]$B)
    try {
        $x = [System.IO.Path]::GetFullPath($A).TrimEnd('\', '/')
        $y = [System.IO.Path]::GetFullPath($B).TrimEnd('\', '/')
        return [string]::Equals($x, $y, [StringComparison]::OrdinalIgnoreCase)
    }
    catch { return $false }
}

function Get-VenueState {
    # Reads the three folders the idle judgement rests on. Any failure to list one is "unreadable", never "empty".
    $state = [ordered]@{ unreadable = $null; running = 0; inbox = 0; leases = @() }
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

function Test-AgentProcessAlive {
    # Alive AND the same process the heartbeat recorded (pid plus start time, so a reused pid does not count).
    param($Beat, [switch]$RequireStart)
    if ($null -eq $Beat) { return $false }
    $proc = Get-Process -Id $Beat.pid -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return $false }
    if ([string]::IsNullOrWhiteSpace($Beat.start)) { return (-not $RequireStart) }
    try {
        $want = [DateTimeOffset]::Parse($Beat.start, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal)
        return ([Math]::Abs(($proc.StartTime.ToUniversalTime() - $want.UtcDateTime).TotalSeconds) -lt 2)
    }
    catch { return (-not $RequireStart) }
}

function Test-NewHeartbeatIsLiveAgent {
    # A restart is confirmed only by a heartbeat from a process that EXISTS with the recorded start time and runs this root's script.
    param($Beat, [string]$AgentScript)
    if ($null -eq $Beat) { return $false }
    if ([string]::IsNullOrWhiteSpace($Beat.start)) { return $false }
    if (-not (Test-SamePath -A $Beat.agentScript -B $AgentScript)) { return $false }
    return (Test-AgentProcessAlive -Beat $Beat -RequireStart)
}

function Get-RestartBindingRefusal {
    # -RestartMode when-idle without -RestartScript: the schtasks End/Run act on THIS machine's task, so the root that was checked
    # idle must be the root of the agent that task runs. Returns a reason string, or $null when bound.
    param([string]$AgentScript)
    $beat = Read-Heartbeat
    if ($null -eq $beat) { return 'heartbeat.txt is missing or unreadable, so the agent host cannot be confirmed' }
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
            $out = & schtasks.exe /Query /TN $TaskName /XML 2>&1
            if ($LASTEXITCODE -ne 0) { return "task $TaskName could not be queried (exit $LASTEXITCODE)" }
            $xml = ($out | Out-String)
        }
    }
    catch { return "task $TaskName could not be read: $($_.Exception.Message)" }
    if ($xml.IndexOf($AgentScript, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return "task $TaskName does not run $AgentScript; ending it would stop a different agent"
    }
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
    $before = Read-Heartbeat
    & schtasks.exe /End /TN $TaskName | Out-Null
    $gone = Wait-Until -TimeoutSec 30 -Condition { -not (Test-AgentProcessAlive -Beat $before) }
    if (-not $gone) { return @{ why = "agent pid $($before.pid) still alive 30 s after schtasks /End"; warn = $null } }
    $warn = Get-ArrivalWarning
    & schtasks.exe /Run /TN $TaskName | Out-Null
    if ($LASTEXITCODE -ne 0) { return @{ why = "schtasks /Run $TaskName exited $LASTEXITCODE"; warn = $warn } }
    $up = Wait-Until -TimeoutSec 60 -Condition {
        $now = Read-Heartbeat
        ($null -ne $now) -and ($now.ageSec -lt 30) -and ($now.pid -ne $before.pid) -and (Test-NewHeartbeatIsLiveAgent -Beat $now -AgentScript $AgentScript)
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
