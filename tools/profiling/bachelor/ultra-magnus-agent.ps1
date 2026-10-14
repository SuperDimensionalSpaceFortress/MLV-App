# ultra-magnus-agent.ps1
# Runs ON the GPU bench. A minimal file-drop job runner so the VM can execute
# work on the host over SMB alone (no SSH/WinRM/admin/open ports).
#
# Protocol (all under <agent root>, which is this script's folder):
#   inbox\<jobId>.job.ps1        - VM drops a PowerShell script here (atomic rename)
#   inbox\<jobId>.meta.json      - VM submit metadata written before the job is visible
#   running\<jobId>.started.json - agent-owned durable claim marker: phase=admission-wait (held in the
#                                  heavy-slot gate, childPid null, NOT launched), then phase=launching
#                                  (about to create the child, childPid null, NOT launched yet), then
#                                  phase=launched, written ONCE together with the child's identity;
#                                  no phase field = launched (agents before GATE-2)
#                                  r2d: every write is a UNIQUE tmp ('<marker>.<32 hex>.tmp') then ONE checked
#                                  replacing rename; a job with ANY unresolved marker artifact (a tmp, an
#                                  unreadable marker, a marker this agent has not reconciled) is QUARANTINED:
#                                  never admitted or launched until recovery reconciles it (Resolve-JobRecovery)
#   outbox\<jobId>.result.json   - agent writes {exitCode,stdout,stderr,timing}
#   processed\                   - consumed job scripts and metadata are moved here
#   orphaned\                    - abandoned job scripts are preserved here
#   cancel\<jobId>.cancel        - cooperative cancellation request
#   slot-holders\                - r1c: each gated job's heavy-slot.py holder (record, acquired, release files)
#   logs\                        - per-job stdout/stderr capture plus agent.log
#   heartbeat.txt                - rewritten every poll so the VM can prove liveness
#
# Child launch contract: every submitted PowerShell root is created with
# UseShellExecute=false, CreateNoWindow=true, and redirected stdout/stderr.
# Native grandchildren therefore inherit a windowless process context unless
# the submitted script explicitly requests a new console.
#
# Security: this intentionally executes scripts dropped into inbox\. It is a
# private automation channel on the user's own LAN/host/account. Stop it by
# ending the scheduled task / process (see install-ultra-magnus-agent.ps1).

[CmdletBinding()]
param(
    [string]$Root = $PSScriptRoot,
    [int]$PollSeconds = 2,
    [ValidateRange(1, 86400)]
    [int]$JobTimeoutSec = 1800,
    [string]$MutexName = "Local\MlvGpuProfileAgent",
    [switch]$RunOnce,
    # Heavy-slot admission (MLV-AGENT-HEAVY-SLOT-GATE-1, 2026-10-10). See the gate block above Invoke-QueuedJob.
    [string]$HeavySlotPython = "C:\Users\layib\AppData\Local\Python\bin\python.exe",
    [string]$HeavySlotScript = "C:\Users\layib\.claude\ops\heavy-slot.py",
    [string]$HeavySlotDir = $(if ($env:HEAVY_SLOT_DIR) { $env:HEAVY_SLOT_DIR } else { "C:\t\.heavy-slots" }),
    [ValidateRange(1, 8)]
    [int]$HeavySlotCount = 2,
    # r1c: SUSTAINED heat, read from the same feed heavy-slot.py reads (its HEAVY_SLOT_FEED override honoured):
    # blocked only when > GateMaxThrottledRows of the last GateSustainedRows rows are throttled, or their mean is
    # >= GateMeanMaxC. These defaults are heavy-slot.py's own first-run rule (thr60 <= 6 and mean60 < 88).
    [string]$HeavySlotFeed = $(if ($env:HEAVY_SLOT_FEED) { $env:HEAVY_SLOT_FEED } else { "C:\temp\hwinfo-log-adapter.csv" }),
    [ValidateRange(15, 150)]
    [int]$GateSustainedRows = 60,
    [ValidateRange(0, 150)]
    [int]$GateMaxThrottledRows = 6,
    [double]$GateMeanMaxC = 88,
    [ValidateRange(10, 3600)]
    [int]$GateFeedStaleSec = 120,
    [ValidateRange(2, 120)]
    [int]$GateProbeSec = 15,
    [ValidateRange(0, 3600)]
    [int]$GateIdleSampleSec = 60,
    # r1g/r1h: job ids the gate does not hold (light: ~1 s load probes, ~5 s venue health probes dve-*-health, agent maintenance). Anchored.
    [string]$GateExemptJobPattern = '^(venue-quiet-probe|agent-swap)-|^dve-[A-Za-z0-9._-]+-health$'
)

$ErrorActionPreference = "Continue"
$inbox     = Join-Path $Root "inbox"
$outbox    = Join-Path $Root "outbox"
$processed = Join-Path $Root "processed"
$logs      = Join-Path $Root "logs"
$running   = Join-Path $Root "running"
$orphaned  = Join-Path $Root "orphaned"
$cancel    = Join-Path $Root "cancel"
$slotHolders = Join-Path $Root "slot-holders"
$heartbeat = Join-Path $Root "heartbeat.txt"
foreach ($d in @($inbox, $outbox, $processed, $logs, $running, $orphaned, $cancel, $slotHolders)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# Prefer pwsh 7 for running jobs; fall back to Windows PowerShell if absent.
$psExe = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $psExe -and (Test-Path "$env:ProgramFiles\PowerShell\7\pwsh.exe")) { $psExe = "$env:ProgramFiles\PowerShell\7\pwsh.exe" }
if (-not $psExe) { $psExe = "powershell.exe" }

$script:LastHeartbeatWriteWarningUtc = [DateTime]::MinValue
$script:HeartbeatGeneration = 0
$script:AgentProcess = [System.Diagnostics.Process]::GetCurrentProcess()
$script:AgentProcessStartUtc = $script:AgentProcess.StartTime.ToUniversalTime().ToString("o")
$script:AgentImagePath = $script:AgentProcess.Path
if ([string]::IsNullOrWhiteSpace($script:AgentImagePath)) { $script:AgentImagePath = $psExe }
# r2d (MLV-AGENT-MARKER-CRASH-SAFE-1) recovery state. OwnMarkerWriteIds: every writeId THIS agent stamped (a final
# marker carrying one is this agent's own). OwnPendingMarkerTmp: marker path -> this agent's newest unpromoted tmp.
# RecoveryQuarantine: jobId -> why its recovery is unresolved (the inbox loop never admits or launches it).
$script:OwnMarkerWriteIds = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$script:OwnPendingMarkerTmp = @{}
$script:RecoveryQuarantine = @{}
$script:LastRecoveryWhy = $null

function Add-AgentLogLine {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        $line = "$((Get-Date).ToString('o')) $Message$([Environment]::NewLine)"
        [System.IO.File]::AppendAllText((Join-Path $logs "agent.log"), $line, [System.Text.Encoding]::ASCII)
    }
    catch {
        # Logging must never stop the agent.
    }
}

$script:SleepStateApiAvailable = $false
try {
    Add-Type -Namespace MlvApp.Agent -Name NativePower -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
"@ -ErrorAction Stop
    $script:SleepStateApiAvailable = $true
}
catch {
    Add-AgentLogLine "keep-awake api unavailable: $($_.Exception.Message)"
}

function Set-AgentKeepAwake {
    param([Parameter(Mandatory = $true)][bool]$Enable)

    if (-not $script:SleepStateApiAvailable) {
        return $false
    }

    $ES_CONTINUOUS = [Convert]::ToUInt32("80000000", 16)
    $ES_SYSTEM_REQUIRED = [Convert]::ToUInt32("00000001", 16)
    $flags = if ($Enable) { $ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED } else { $ES_CONTINUOUS }
    try {
        $result = [MlvApp.Agent.NativePower]::SetThreadExecutionState($flags)
        if ($result -eq 0) {
            Add-AgentLogLine "keep-awake SetThreadExecutionState failed enable=$Enable"
            return $false
        }
        return $true
    }
    catch {
        Add-AgentLogLine "keep-awake SetThreadExecutionState threw enable=$Enable error=$($_.Exception.Message)"
        return $false
    }
}

function Write-AsciiFileWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value,
        [int]$Attempts = 8,
        [int]$DelayMs = 125
    )

    $bytes = [System.Text.Encoding]::ASCII.GetBytes($Value + [Environment]::NewLine)
    $lastError = $null
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $stream = $null
        try {
            $stream = [System.IO.File]::Open($Path,
                [System.IO.FileMode]::Create,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.SetLength($bytes.Length)
            return $true
        }
        catch {
            $lastError = $_.Exception.Message
            if ($attempt -lt $Attempts) {
                Start-Sleep -Milliseconds $DelayMs
            }
        }
        finally {
            if ($null -ne $stream) {
                $stream.Dispose()
            }
        }
    }

    $now = Get-Date
    if (($now - $script:LastHeartbeatWriteWarningUtc).TotalSeconds -ge 30) {
        Add-AgentLogLine "heartbeat write skipped after ${Attempts} attempts: $lastError"
        $script:LastHeartbeatWriteWarningUtc = $now
    }
    return $false
}

function Write-AgentHeartbeat {
    param([string]$Activity = "")

    $script:HeartbeatGeneration++
    $line = "alive $((Get-Date).ToString('o')) pid=$PID host=$env:COMPUTERNAME generation=$script:HeartbeatGeneration processStartUtc=$script:AgentProcessStartUtc imagePath=""$script:AgentImagePath"" agentScript=""$PSCommandPath"""
    if (![string]::IsNullOrWhiteSpace($Activity)) {
        $line += " $Activity"
    }
    [void](Write-AsciiFileWithRetry -Path $heartbeat -Value $line)
}

function Move-FileChecked {
    # r2d: ONE rename, CHECKED. A failed [IO.File]::Move throws, and under this agent's Continue error policy an
    # unchecked failure ran straight on into the success path (sol r2c BLOCKER: "promoted" logged for a rename that
    # never happened). Every attempt is inside try/catch; the retry is BOUNDED: at most $Attempts attempts, $DelayMs
    # apart (defaults 10 x 200 ms, about 1.8 s of waiting). A transient reader -- um-run.ps1 and Read-JsonFile read
    # markers with Get-Content, which shares ReadWrite but not Delete -- denies a replacing rename only while its read
    # lasts (doctrine card mlv-app/atomic-replace-denied-while-a-reader-holds-the-destination).
    # Returns @{ ok; attempts; error }. ok=$true only when the rename completed; nothing else is ever success.
    param([Parameter(Mandatory = $true)][string]$Source, [Parameter(Mandatory = $true)][string]$Destination,
        [bool]$Overwrite = $false, [int]$Attempts = 10, [int]$DelayMs = 200)
    $lastError = $null
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            [System.IO.File]::Move($Source, $Destination, $Overwrite)
            return [pscustomobject]@{ ok = $true; attempts = $i; error = $null }
        }
        catch {
            $e = $_.Exception
            if ($null -ne $e.InnerException) { $e = $e.InnerException }
            $lastError = "$($e.GetType().Name): $($e.Message)"
        }
        if ($i -lt $Attempts) { Start-Sleep -Milliseconds $DelayMs }
    }
    return [pscustomobject]@{ ok = $false; attempts = $Attempts; error = $lastError }
}

function Write-JsonFileAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$InputObject,
        [int]$Depth = 8
    )

    # r2d: a UNIQUE tmp per write, so a later write can never overwrite an earlier tmp that is still unresolved (sol
    # r2c BLOCKER: the admission write reused '<marker>.tmp' and erased the only record of a live child).
    # Returns @{ ok; tmp; error }. ok=$true: the ONE replacing rename completed. ok=$false with tmp set: that tmp is
    # complete on disk and is LEFT IN PLACE as the record of this write (the caller or a later recovery owns it).
    $tmp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $InputObject | ConvertTo-Json -Depth $Depth | Set-Content -Encoding ASCII -LiteralPath $tmp -ErrorAction Stop
    }
    catch {
        # An incomplete tmp records nothing (the write never finished): best-effort removal.
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        return [pscustomobject]@{ ok = $false; tmp = $null; error = "tmp write failed: $($_.Exception.Message)" }
    }
    # r2c: ONE replacing rename (MoveFileEx MOVEFILE_REPLACE_EXISTING). Move-Item -Force deletes the destination first,
    # so a process killed between the two steps left no marker at all (sol r2b BLOCKER: a relaunch of a live job).
    # r2d: checked, with a short bounded retry (3 x 100 ms; Write-StartedMarkerVerified retries around it).
    $mv = Move-FileChecked -Source $tmp -Destination $Path -Overwrite $true -Attempts 3 -DelayMs 100
    return [pscustomobject]@{ ok = $mv.ok; tmp = $(if ($mv.ok) { $null } else { $tmp }); error = $mv.error }
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        Add-AgentLogLine "json read failed path=$Path error=$($_.Exception.Message)"
        return $null
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    }
    catch {
        return $null
    }
}

function Get-ProcessIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if (-not $process) {
            return [pscustomobject]@{ exists = $false; processId = $ProcessId; startUtc = $null; imagePath = $null; commandLine = $null }
        }
        $startUtc = $null
        if ($process.CreationDate) {
            $startUtc = ([DateTime]$process.CreationDate).ToUniversalTime().ToString("o")
        }
        $startEpochMs = $null
        if ($startUtc) {
            $startEpochMs = [DateTimeOffset]::Parse($startUtc).ToUnixTimeMilliseconds()
        }
        return [pscustomobject]@{
            exists      = $true
            processId   = [int]$process.ProcessId
            startUtc    = $startUtc
            startEpochMs = $startEpochMs
            imagePath   = $process.ExecutablePath
            commandLine = $process.CommandLine
        }
    }
    catch {
        return [pscustomobject]@{ exists = $false; processId = $ProcessId; startUtc = $null; startEpochMs = $null; imagePath = $null; commandLine = $null }
    }
}

function Test-EpochMillisClose {
    param(
        $ActualEpochMs,
        $ExpectedEpochMs,
        [int]$ToleranceSeconds = 2
    )

    try {
        if ($null -eq $ActualEpochMs -or $null -eq $ExpectedEpochMs) {
            return $false
        }
        $actual = [Int64]$ActualEpochMs
        $expected = [Int64]$ExpectedEpochMs
        return ([Math]::Abs($actual - $expected) -le ($ToleranceSeconds * 1000))
    }
    catch {
        return $false
    }
}

function Test-ProcessIdentityMatch {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [string]$ExpectedStartUtc,
        $ExpectedStartEpochMs,
        [string]$ExpectedImagePath
    )

    $identity = Get-ProcessIdentity -ProcessId $ProcessId
    if (-not $identity.exists) {
        return [pscustomobject]@{ matches = $false; identity = $identity; reason = "not-running" }
    }

    $startMatches = $true
    if ($null -ne $ExpectedStartEpochMs) {
        $startMatches = Test-EpochMillisClose -ActualEpochMs $identity.startEpochMs -ExpectedEpochMs $ExpectedStartEpochMs
    }
    elseif (-not [string]::IsNullOrWhiteSpace($ExpectedStartUtc)) {
        # Fallback for legacy markers created before childStartEpochMs existed.
        $startMatches = ([string]$identity.startUtc -eq [string]$ExpectedStartUtc)
    }
    $imageMatches = $true
    if (-not [string]::IsNullOrWhiteSpace($ExpectedImagePath)) {
        $imageMatches = ([string]::Equals($identity.imagePath, $ExpectedImagePath, [StringComparison]::OrdinalIgnoreCase))
    }

    $reason =
        if ($startMatches -and $imageMatches) { "matched" }
        elseif (-not $startMatches -and -not $imageMatches) { "start-and-image-mismatch" }
        elseif (-not $startMatches) { "start-mismatch" }
        else { "image-mismatch" }

    return [pscustomobject]@{
        matches  = ($startMatches -and $imageMatches)
        identity = $identity
        reason   = $reason
        startMatches = $startMatches
        imageMatches = $imageMatches
    }
}

function Get-DescendantProcessIdentities {
    param([Parameter(Mandatory = $true)][int]$ParentProcessId)

    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentProcessId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        $childProcessId = [int]$child.ProcessId
        Get-DescendantProcessIdentities -ParentProcessId $childProcessId
        Get-ProcessIdentity -ProcessId $childProcessId
    }
}

function Stop-ProcessTree {
    param(
        [Parameter(Mandatory = $true)][int]$RootProcessId,
        [string]$RootStartUtc,
        $RootStartEpochMs,
        [string]$RootImagePath
    )

    $killed = [System.Collections.Generic.List[int]]::new()
    $rootMatch = Test-ProcessIdentityMatch -ProcessId $RootProcessId -ExpectedStartUtc $RootStartUtc -ExpectedStartEpochMs $RootStartEpochMs -ExpectedImagePath $RootImagePath
    if (-not $rootMatch.matches) {
        Add-AgentLogLine "refused process-tree stop pid=$RootProcessId reason=$($rootMatch.reason)"
        return @()
    }
    $identities = @(
        @(Get-DescendantProcessIdentities -ParentProcessId $RootProcessId)
        $rootMatch.identity
    )
    foreach ($expected in $identities) {
        if (-not $expected.exists) { continue }
        try {
            $match = Test-ProcessIdentityMatch -ProcessId ([int]$expected.processId) -ExpectedStartUtc $expected.startUtc -ExpectedStartEpochMs $expected.startEpochMs -ExpectedImagePath $expected.imagePath
            if (-not $match.matches) {
                Add-AgentLogLine "skipped changed process identity pid=$($expected.processId) reason=$($match.reason)"
                continue
            }
            Stop-Process -Id ([int]$expected.processId) -Force -ErrorAction Stop
            [void]$killed.Add([int]$expected.processId)
        }
        catch {
            # The child may already have exited while the tree was being walked.
        }
    }
    return @($killed)
}

function Stop-VerifiedProcessIdentities {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Identities)

    $killed = [System.Collections.Generic.List[int]]::new()
    foreach ($expected in $Identities) {
        if ($null -eq $expected -or -not $expected.exists) { continue }
        $match = Test-ProcessIdentityMatch -ProcessId ([int]$expected.processId) -ExpectedStartUtc $expected.startUtc -ExpectedStartEpochMs $expected.startEpochMs -ExpectedImagePath $expected.imagePath
        if (-not $match.matches) { continue }
        try {
            Stop-Process -Id ([int]$expected.processId) -Force -ErrorAction Stop
            [void]$killed.Add([int]$expected.processId)
        }
        catch { }
    }
    return @($killed)
}

function Start-WindowlessRedirectedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$OutFile,
        [Parameter(Mandatory = $true)][string]$ErrFile,
        [string]$WorkingDirectory = $Root,
        # r1c: per-child environment overrides; a $null value removes the variable from the child's environment.
        [hashtable]$Environment = @{}
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $ArgumentList) { [void]$startInfo.ArgumentList.Add($argument) }
    foreach ($k in $Environment.Keys) {
        if ($null -eq $Environment[$k]) { [void]$startInfo.Environment.Remove($k) } else { $startInfo.Environment[$k] = [string]$Environment[$k] }
    }

    $outStream = $null
    $errStream = $null
    $process = $null
    try {
        $outStream = [System.IO.File]::Open($OutFile, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
        $errStream = [System.IO.File]::Open($ErrFile, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw "Process.Start returned false for $FilePath" }
        return [pscustomobject]@{
            Process = $process
            OutStream = $outStream
            ErrStream = $errStream
            OutCopy = $process.StandardOutput.BaseStream.CopyToAsync($outStream)
            ErrCopy = $process.StandardError.BaseStream.CopyToAsync($errStream)
        }
    }
    catch {
        if ($null -ne $process) { $process.Dispose() }
        if ($null -ne $outStream) { $outStream.Dispose() }
        if ($null -ne $errStream) { $errStream.Dispose() }
        throw
    }
}

function Complete-WindowlessRedirectedProcess {
    param([Parameter(Mandatory = $true)]$Launch)

    try {
        [void]$Launch.OutCopy.GetAwaiter().GetResult()
        [void]$Launch.ErrCopy.GetAwaiter().GetResult()
        $Launch.OutStream.Flush()
        $Launch.ErrStream.Flush()
    }
    finally {
        $Launch.OutStream.Dispose()
        $Launch.ErrStream.Dispose()
    }
}

function Get-JobMetadata {
    param([Parameter(Mandatory = $true)][string]$JobId)

    $metaPath = Join-Path $inbox "$JobId.meta.json"
    $meta = Read-JsonFile -Path $metaPath
    if ($null -eq $meta) {
        return [pscustomobject]@{
            jobId          = $JobId
            submitUtc      = $null
            timeoutSec     = $JobTimeoutSec
            maxQueueAgeSec = 0
            metadataPath   = $metaPath
            missing        = $true
        }
    }
    $parsedTimeoutSec = 0
    if ($null -eq $meta.timeoutSec -or
        -not [int]::TryParse([string]$meta.timeoutSec, [ref]$parsedTimeoutSec) -or
        $parsedTimeoutSec -lt 1 -or
        $parsedTimeoutSec -gt 86400) {
        $meta | Add-Member -NotePropertyName timeoutSec -NotePropertyValue $JobTimeoutSec -Force
    }
    else {
        $meta | Add-Member -NotePropertyName timeoutSec -NotePropertyValue $parsedTimeoutSec -Force
    }
    if ($null -eq $meta.maxQueueAgeSec) {
        $meta | Add-Member -NotePropertyName maxQueueAgeSec -NotePropertyValue 0
    }
    $meta | Add-Member -NotePropertyName metadataPath -NotePropertyValue $metaPath -Force
    $meta | Add-Member -NotePropertyName missing -NotePropertyValue $false -Force
    return $meta
}

function Get-QueueAgeSeconds {
    param(
        [Parameter(Mandatory = $true)]$Job,
        [Parameter(Mandatory = $true)]$Metadata
    )

    $submitUtc = $null
    if ($Metadata.submitUtc -is [DateTime]) {
        $submitDateTime = [DateTime]$Metadata.submitUtc
        if ($submitDateTime.Kind -eq [DateTimeKind]::Local) {
            $submitUtc = $submitDateTime.ToUniversalTime()
        }
        else {
            $submitUtc = [DateTime]::SpecifyKind($submitDateTime, [DateTimeKind]::Utc)
        }
    }
    elseif (-not [string]::IsNullOrWhiteSpace($Metadata.submitUtc)) {
        try {
            $submitUtc = ([DateTimeOffset]::Parse([string]$Metadata.submitUtc)).UtcDateTime
        }
        catch {
            $submitUtc = $null
        }
    }
    if ($null -eq $submitUtc) {
        $submitUtc = $Job.CreationTimeUtc
    }
    return [Math]::Max(0, [int]((Get-Date).ToUniversalTime() - $submitUtc).TotalSeconds)
}

function Publish-JobResult {
    param(
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)]$Result
    )

    # r2d: a UNIQUE tmp per write; the ONE replacing rename is checked (Move-FileChecked, bounded retry). On a final
    # failure the result is kept at its tmp and PROMOTE FAILED is logged: never reported as published.
    $tmp = Join-Path $outbox "$JobId.result.$([guid]::NewGuid().ToString('N')).tmp"
    $fin = Join-Path $outbox "$JobId.result.json"
    $Result | ConvertTo-Json -Depth 8 | Set-Content -Encoding ASCII -LiteralPath $tmp
    $mv = Move-FileChecked -Source $tmp -Destination $fin -Overwrite $true   # r2c: one replacing rename (no missing-result window)
    if (-not $mv.ok) { Add-AgentLogLine "PROMOTE FAILED $JobId $tmp $($mv.error) (result kept at the tmp; $fin not written)" }
    return $fin
}

function Move-IfPresent {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    if (Test-Path -LiteralPath $Source) {
        Move-Item -Force -LiteralPath $Source -Destination $Destination
    }
}

function Move-JobArtifacts {
    param(
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)][string]$JobPath,
        [Parameter(Mandatory = $true)][string]$DestinationDirectory
    )

    New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null
    Move-IfPresent -Source $JobPath -Destination (Join-Path $DestinationDirectory (Split-Path $JobPath -Leaf))
    $metaPath = Join-Path $inbox "$JobId.meta.json"
    Move-IfPresent -Source $metaPath -Destination (Join-Path $DestinationDirectory "$JobId.meta.json")
    $cancelPath = Join-Path $cancel "$JobId.cancel"
    Move-IfPresent -Source $cancelPath -Destination (Join-Path $DestinationDirectory "$JobId.cancel")
}

function Complete-StartedMarker {
    param(
        [Parameter(Mandatory = $true)][string]$MarkerPath,
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$ResultPath = ""
    )

    $marker = Read-JsonFile -Path $MarkerPath
    if ($null -eq $marker) {
        return
    }
    # r2d: retire a marker only once its result is really on disk. A result whose ONE rename failed (PROMOTE FAILED,
    # kept at its unique tmp) leaves the marker in running\ and the job QUARANTINED, so a later pass promotes the
    # stranded result (Resolve-StrandedResult) -- never a retired marker with no result behind it.
    if (-not [string]::IsNullOrWhiteSpace($ResultPath) -and -not (Test-Path -LiteralPath $ResultPath -PathType Leaf)) {
        $jid = [string]$marker.jobId
        Add-AgentLogLine "marker kept job=${jid} status=${Status}: its result is not published ($ResultPath missing); quarantined until recovery promotes it"
        $script:RecoveryQuarantine[$jid] = "result not published: $ResultPath missing"
        return
    }
    $marker | Add-Member -NotePropertyName status -NotePropertyValue $Status -Force
    $marker | Add-Member -NotePropertyName completedUtc -NotePropertyValue ((Get-Date).ToUniversalTime().ToString("o")) -Force
    if (-not [string]::IsNullOrWhiteSpace($ResultPath)) {
        $marker | Add-Member -NotePropertyName resultPath -NotePropertyValue $ResultPath -Force
    }
    $w = Write-JsonFileAtomic -Path $MarkerPath -InputObject $marker
    # r2d: this write supersedes this agent's older pending tmp; a failed one stays on disk as the newest record.
    if ($w.ok) { Clear-OwnPendingMarkerTmp -Path $MarkerPath } elseif ($w.tmp) { Set-OwnPendingMarkerTmp -Path $MarkerPath -Tmp $w.tmp }
    $dest = Join-Path $processed ("{0}.started.{1}.json" -f $marker.jobId, $Status)
    Move-Item -Force -LiteralPath $MarkerPath -Destination $dest
}

function Set-OwnPendingMarkerTmp {
    # r2d: THIS agent's newest unpromoted tmp for a marker stays on disk, so an agent killed now leaves it for recovery.
    # The tmp it supersedes -- an OLDER complete record of the same marker by the same live agent -- is removed only
    # after the newer one is complete on disk: at most one own pending tmp per marker, and never none while a write is
    # pending. A tmp left by a DEAD agent is a recovery record and is never removed (Resolve-JobMarkerTemps).
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Tmp)
    $old = $script:OwnPendingMarkerTmp[$Path]
    $script:OwnPendingMarkerTmp[$Path] = $Tmp
    if ($old -and $old -ne $Tmp) { Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue }
}

function Clear-OwnPendingMarkerTmp {
    # r2d: the marker itself now holds this agent's newest write, so its older pending tmp is superseded.
    param([Parameter(Mandatory = $true)][string]$Path)
    $old = $script:OwnPendingMarkerTmp[$Path]
    if ($old) {
        Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue
        $script:OwnPendingMarkerTmp.Remove($Path)
    }
}

function Write-StartedMarkerVerified {
    # GATE-2: every marker rewrite replaces a file a VM client may be reading at that instant, and a reader's
    # open handle can make the atomic rename fail. Each attempt stamps a fresh writeId and reads it back, so
    # only THIS write can verify (a stale marker with the same phase never does). r2b: returns $true only
    # when verified; the caller decides what a failure forbids (a launch is never made on $false).
    # THREE OUTCOMES: $true = done (the marker on disk is this write); $false = not done OR cannot tell (the
    # write may have landed with its readback failing) -- callers treat $false as "the disk may hold the old
    # marker or this one", never as either for certain.
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Marker)
    $lastError = $null
    for ($i = 0; $i -lt 10; $i++) {
        $writeId = [guid]::NewGuid().ToString("N")
        $Marker | Add-Member -NotePropertyName writeId -NotePropertyValue $writeId -Force
        [void]$script:OwnMarkerWriteIds.Add($writeId)   # r2d: a marker carrying this id is THIS agent's own
        $w = $null
        try {
            $ErrorActionPreference = "Stop"
            $w = Write-JsonFileAtomic -Path $Path -InputObject $Marker
        }
        catch { $lastError = $_.Exception.Message }
        finally { $ErrorActionPreference = "Continue" }
        # r2d: a failed rename leaves this write's unique tmp on disk as the newest record of the marker.
        if ($null -ne $w -and -not $w.ok) {
            $lastError = $w.error
            if ($w.tmp) { Set-OwnPendingMarkerTmp -Path $Path -Tmp $w.tmp }
        }
        $back = Read-JsonFile -Path $Path
        if ($null -ne $back -and $back.writeId -eq $writeId) { Clear-OwnPendingMarkerTmp -Path $Path; return $true }
        Start-Sleep -Milliseconds 100
    }
    Add-AgentLogLine "started marker write not verified path=$Path phase=$($Marker.phase) after 10 attempts lastError=[$lastError]"
    return $false
}

function New-AdmissionWaitMarker {
    # MLV-AGENT-HEAVY-SLOT-GATE-2: the claim, written BEFORE the heavy-slot gate holds the job, so a VM client
    # never mistakes a held job for an unclaimed one and retracts it. childPid stays null: nothing has launched.
    param(
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)]$Job,
        [Parameter(Mandatory = $true)]$Metadata,
        [string]$GateReason = "waiting for heavy-slot admission (heavy-slot.py FIFO, an empty machine, sustained heat clear)"
    )

    $markerPath = Join-Path $running "$JobId.started.json"
    $marker = [ordered]@{
        schema                 = "mlvapp.agent.started.v1"
        jobId                  = $JobId
        phase                  = "admission-wait"
        status                 = "admission-wait"
        admissionWaitUtc       = (Get-Date).ToUniversalTime().ToString("o")
        submitUtc              = $Metadata.submitUtc
        maxQueueAgeSec         = $Metadata.maxQueueAgeSec
        agentPid               = $PID
        agentProcessStartUtc   = $script:AgentProcessStartUtc
        agentImagePath         = $script:AgentImagePath
        host                   = $env:COMPUTERNAME
        timeoutSec             = [int]$Metadata.timeoutSec
        heartbeatGeneration    = $script:HeartbeatGeneration
        sourceInboxPath        = $Job.FullName
        metadataPath           = $Metadata.metadataPath
        scriptSha256           = Get-FileSha256 -Path $Job.FullName
        # The paths the launch will use. Present so an agent older than GATE-2 (a rollback mid-hold), which
        # reads this as a launched marker with no child, publishes an honest orphan receipt instead of
        # failing on null paths at startup.
        outFile                = Join-Path $logs "$JobId.out.txt"
        errFile                = Join-Path $logs "$JobId.err.txt"
        childPid               = $null
        gate                   = $script:GateVersion
        gateReason             = $GateReason
    }
    # The claim only protects the job from a client's queue-ceiling retraction; it never gates a launch (the
    # launching marker below does). THREE OUTCOMES: verified = claimed; not verified = not claimed or cannot
    # tell -- the job waits in the gate either way (no launch has happened, so a restart that finds an
    # admission-wait marker, an old marker or none re-queues correctly), and a client that sees no marker may
    # retract it exactly as it could under r1i.
    $ok = Write-StartedMarkerVerified -Path $markerPath -Marker ([pscustomobject]$marker)
    if (-not $ok) { Add-AgentLogLine "heavy-slot-gate: admission-wait claim for job=$JobId not verified; the job is held unclaimed" }
    return $ok
}

function Complete-AdmissionWaitMarker {
    # A job the gate never launched (cancelled / queue-expired) retires its admission-wait claim AFTER its
    # result is published -- the same order as every other outcome, so a client never sees neither.
    # r2b: a launching marker reaching here is also unlaunched -- every caller runs before this agent's own
    # launching write, so such a marker is one a restart re-queued on proof that nothing launched (see
    # Resolve-LaunchingMarker) whose admission-wait rewrite did not verify.
    param([Parameter(Mandatory = $true)][string]$JobId, [Parameter(Mandatory = $true)][string]$Status)
    $markerPath = Join-Path $running "$JobId.started.json"
    $marker = Read-JsonFile -Path $markerPath
    if ($null -eq $marker -or $marker.phase -notin @("admission-wait", "launching")) { return }
    Complete-StartedMarker -MarkerPath $markerPath -Status $Status -ResultPath (Join-Path $outbox "$JobId.result.json")
}

function New-LaunchingMarker {
    # r2b (sol blocker 1 + 2): the LAST write before the child is created. phase=launching, childPid null: a
    # client reads it as "claimed, not launched", never as launched. It carries what a restarted agent needs to
    # decide whether a launch could have happened: the image and the job script path it is about to run (to find
    # a live child by command line) and the out file Start-WindowlessRedirectedProcess creates immediately before
    # Process.Start (absent = Process.Start was never reached). Returns the marker object when it verified, or
    # $null. On $null the caller does NOT launch.
    param(
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)]$Job,
        [Parameter(Mandatory = $true)]$Metadata,
        [Parameter(Mandatory = $true)][string]$OutFile,
        [Parameter(Mandatory = $true)][string]$ErrFile,
        [Parameter(Mandatory = $true)][string]$StartedUtc,
        [Parameter(Mandatory = $true)][int]$TimeoutSec
    )

    $markerPath = Join-Path $running "$JobId.started.json"
    $marker = [ordered]@{
        schema                 = "mlvapp.agent.started.v1"
        jobId                  = $JobId
        phase                  = "launching"
        status                 = "launching"
        launchingUtc           = (Get-Date).ToUniversalTime().ToString("o")
        launchImagePath        = $psExe
        launchScriptPath       = $Job.FullName
        startedUtc             = $StartedUtc
        submitUtc              = $Metadata.submitUtc
        maxQueueAgeSec         = $Metadata.maxQueueAgeSec
        agentPid               = $PID
        agentProcessStartUtc   = $script:AgentProcessStartUtc
        agentImagePath         = $script:AgentImagePath
        host                   = $env:COMPUTERNAME
        timeoutSec             = $TimeoutSec
        heartbeatGeneration    = $script:HeartbeatGeneration
        sourceInboxPath        = $Job.FullName
        metadataPath           = $Metadata.metadataPath
        scriptSha256           = Get-FileSha256 -Path $Job.FullName
        outFile                = $OutFile
        errFile                = $ErrFile
        childPid               = $null
        childStartUtc          = $null
        childStartEpochMs      = $null
        childImagePath         = $null
        # Heavy-slot admission (label clear, or exempt-light), persisted so a result published by a
        # RESTARTED agent (adoption / orphan recovery) carries the same load label as a normal one.
        admission              = $script:Admission
    }
    $o = [pscustomobject]$marker
    if (Write-StartedMarkerVerified -Path $markerPath -Marker $o) { return $o }
    return $null
}

function Set-LaunchedMarkerFields {
    # phase=launched and the child identity, set on the in-memory marker together: the ONE write that follows
    # (Write-StartedMarkerVerified) is the first and only time a reader can see phase=launched, and it already
    # carries childPid. Never written without a child.
    param([Parameter(Mandatory = $true)]$Marker, [Parameter(Mandatory = $true)]$Identity)
    $Marker | Add-Member -NotePropertyName phase -NotePropertyValue "launched" -Force
    $Marker | Add-Member -NotePropertyName status -NotePropertyValue "started" -Force
    $Marker | Add-Member -NotePropertyName childPid -NotePropertyValue ([int]$Identity.processId) -Force
    $Marker | Add-Member -NotePropertyName childStartUtc -NotePropertyValue $Identity.startUtc -Force
    $Marker | Add-Member -NotePropertyName childStartEpochMs -NotePropertyValue $Identity.startEpochMs -Force
    $Marker | Add-Member -NotePropertyName childImagePath -NotePropertyValue $Identity.imagePath -Force
    $Marker | Add-Member -NotePropertyName childCommandLine -NotePropertyValue $Identity.commandLine -Force
}

function Find-LaunchedJobChild {
    # r2b restart evidence: is there a LIVE process that is this job's child? Matches the image the launching
    # marker named, a command line naming the job script, and a start no earlier than the launching write.
    # Returns @{ state = found | none | unknown; identity; count; why }. unknown = the process table could not be
    # read: CANNOT-TELL, never folded into none.
    param([Parameter(Mandatory = $true)]$Marker)
    $script = [string]$Marker.launchScriptPath
    if ([string]::IsNullOrWhiteSpace($script)) { return @{ state = "unknown"; why = "launching marker names no job script" } }
    # ConvertFrom-Json has already turned the ISO string into a DateTime; [string] on that drops its zone, so a
    # DateTime is used as-is (made UTC) and only a string is parsed.
    $notBefore = $null
    try {
        $at = $Marker.launchingUtc
        $notBefore = $(if ($at -is [DateTime]) { $at.ToUniversalTime() } else { ([DateTimeOffset]::Parse([string]$at)).UtcDateTime }).AddSeconds(-2)
    }
    catch { return @{ state = "unknown"; why = "launching marker has no readable launchingUtc" } }
    try { $all = @(Get-CimInstance Win32_Process -ErrorAction Stop) }
    catch { return @{ state = "unknown"; why = "process table unreadable: $($_.Exception.Message)" } }
    $hits = @($all | Where-Object {
        $_.CommandLine -and $_.CommandLine.IndexOf($script, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        ([string]::IsNullOrWhiteSpace([string]$Marker.launchImagePath) -or [string]::Equals([string]$_.ExecutablePath, [string]$Marker.launchImagePath, [StringComparison]::OrdinalIgnoreCase)) -and
        $_.CreationDate -and ([DateTime]$_.CreationDate).ToUniversalTime() -ge $notBefore
    })
    if ($hits.Count -eq 0) { return @{ state = "none"; count = 0; why = "no live process runs $script" } }
    if ($hits.Count -gt 1) { return @{ state = "unknown"; count = $hits.Count; why = "$($hits.Count) live processes run $script" } }
    $identity = Get-ProcessIdentity -ProcessId ([int]$hits[0].ProcessId)
    if (-not $identity.exists) { return @{ state = "unknown"; count = 1; why = "candidate pid $($hits[0].ProcessId) vanished while being read" } }
    return @{ state = "found"; count = 1; identity = $identity; why = "live child pid $($identity.processId) runs $script" }
}

function Test-LaunchOutFile {
    # absent | present | unknown. absent is the only answer that proves Process.Start was never reached: the out
    # file is removed before the launching write and created by Start-WindowlessRedirectedProcess right before
    # Process.Start, and nothing else in the agent deletes it.
    # Its folder must be readable first: an unreachable folder also says "not found" for the file.
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return "unknown" }
    try { if (-not (Get-Item -LiteralPath (Split-Path -Parent $Path) -Force -ErrorAction Stop).PSIsContainer) { return "unknown" } }
    catch { return "unknown" }
    try { [void](Get-Item -LiteralPath $Path -Force -ErrorAction Stop); return "present" }
    catch [System.Management.Automation.ItemNotFoundException] { return "absent" }
    catch { return "unknown" }
}

function Invoke-QueueExpiredJob {
    param(
        [Parameter(Mandatory = $true)]$Job,
        [Parameter(Mandatory = $true)]$Metadata,
        [Parameter(Mandatory = $true)][int]$QueueAgeSec
    )

    $jobId = $Job.BaseName -replace '\.job$', ''
    $ended = (Get-Date).ToUniversalTime().ToString("o")
    $result = [pscustomobject]@{
        jobId          = $jobId
        exitCode       = 125
        startedUtc     = $null
        endedUtc       = $ended
        host           = $env:COMPUTERNAME
        timeoutSec     = [int]$Metadata.timeoutSec
        timedOut       = $false
        cancelled      = $false
        queueExpired   = $true
        queueAgeSec    = $QueueAgeSec
        maxQueueAgeSec = [int]$Metadata.maxQueueAgeSec
        orphaned       = $false
        adopted        = $false
        killedProcessIds = @()
        stdout         = ""
        stderr         = "queue expired before launch; job was not executed"
    }
    $resultPath = Publish-JobResult -JobId $jobId -Result $result
    Move-JobArtifacts -JobId $jobId -JobPath $Job.FullName -DestinationDirectory $processed
    Add-AgentLogLine "queue expired job=$jobId age=${QueueAgeSec}s max=$($Metadata.maxQueueAgeSec) result=$resultPath"
}

function Invoke-OrphanResult {
    param(
        [Parameter(Mandatory = $true)]$Marker,
        [Parameter(Mandatory = $true)][string]$MarkerPath,
        [string]$Reason = "started marker found but child is not live and no result exists"
    )

    $jobId = [string]$Marker.jobId
    $result = [pscustomobject]@{
        jobId          = $jobId
        exitCode       = 127
        startedUtc     = $Marker.startedUtc
        endedUtc       = (Get-Date).ToUniversalTime().ToString("o")
        host           = $env:COMPUTERNAME
        timeoutSec     = $Marker.timeoutSec
        timedOut       = $false
        cancelled      = $false
        queueExpired   = $false
        orphaned       = $true
        adopted        = $false
        orphanReason   = $Reason
        childPid       = $Marker.childPid
        childStartUtc  = $Marker.childStartUtc
        childStartEpochMs = $Marker.childStartEpochMs
        childImagePath = $Marker.childImagePath
        killedProcessIds = @()
        admission      = Get-RecoveredAdmission -Admission $Marker.admission
        stdout         = if (Test-Path -LiteralPath $Marker.outFile) { Get-Content -LiteralPath $Marker.outFile -Raw } else { "" }
        stderr         = if (Test-Path -LiteralPath $Marker.errFile) { Get-Content -LiteralPath $Marker.errFile -Raw } else { $Reason }
    }
    $resultPath = Publish-JobResult -JobId $jobId -Result $result
    if (-not [string]::IsNullOrWhiteSpace($Marker.sourceInboxPath)) {
        Move-JobArtifacts -JobId $jobId -JobPath $Marker.sourceInboxPath -DestinationDirectory $orphaned
    }
    Complete-StartedMarker -MarkerPath $MarkerPath -Status "orphaned" -ResultPath $resultPath
    Add-AgentLogLine "orphaned job=$jobId result=$resultPath reason=$Reason"
}

function Invoke-AdoptStartedJob {
    param(
        [Parameter(Mandatory = $true)]$Marker,
        [Parameter(Mandatory = $true)][string]$MarkerPath,
        [Parameter(Mandatory = $true)]$Match
    )

    $jobId = [string]$Marker.jobId
    Add-AgentLogLine "adopting live child job=$jobId pid=$($Marker.childPid)"
    $keepAwakeBeginUtc = (Get-Date).ToUniversalTime().ToString("o")
    $keepAwakeBeginOk = Set-AgentKeepAwake -Enable $true
    $timedOut = $false
    $killedProcessIds = @()
    try {
        $startedUtc = ([DateTimeOffset]::Parse([string]$Marker.startedUtc)).UtcDateTime
        $deadline = $startedUtc.AddSeconds([int]$Marker.timeoutSec)
        $waitSliceMs = [Math]::Max(250, [Math]::Min(5000, $PollSeconds * 1000))
        while ((Get-Date).ToUniversalTime() -lt $deadline) {
            $currentMatch = Test-ProcessIdentityMatch -ProcessId ([int]$Marker.childPid) -ExpectedStartUtc ([string]$Marker.childStartUtc) -ExpectedStartEpochMs $Marker.childStartEpochMs -ExpectedImagePath $Marker.childImagePath
            if (-not $currentMatch.matches) {
                break
            }
            Write-AgentHeartbeat -Activity "adopt job=$jobId"
            Start-Sleep -Milliseconds $waitSliceMs
        }
        $finalMatch = Test-ProcessIdentityMatch -ProcessId ([int]$Marker.childPid) -ExpectedStartUtc ([string]$Marker.childStartUtc) -ExpectedStartEpochMs $Marker.childStartEpochMs -ExpectedImagePath $Marker.childImagePath
        if ($finalMatch.matches) {
            $timedOut = $true
            $killedProcessIds = @(Stop-ProcessTree -RootProcessId ([int]$Marker.childPid) -RootStartUtc ([string]$Marker.childStartUtc) -RootStartEpochMs $Marker.childStartEpochMs -RootImagePath $Marker.childImagePath)
            "adopted child timed out after $($Marker.timeoutSec)s; killed process tree pids=$($killedProcessIds -join ',')" |
                Add-Content -Encoding ASCII $Marker.errFile
        }
    }
    finally {
        $keepAwakeEndUtc = (Get-Date).ToUniversalTime().ToString("o")
        $keepAwakeEndOk = Set-AgentKeepAwake -Enable $false
    }

    $result = [pscustomobject]@{
        jobId             = $jobId
        exitCode          = if ($timedOut) { 124 } else { $null }
        startedUtc        = $Marker.startedUtc
        endedUtc          = (Get-Date).ToUniversalTime().ToString("o")
        host              = $env:COMPUTERNAME
        timeoutSec        = $Marker.timeoutSec
        timedOut          = $timedOut
        cancelled         = $false
        queueExpired      = $false
        orphaned          = $false
        adopted           = $true
        childPid          = $Marker.childPid
        childStartUtc     = $Marker.childStartUtc
        childStartEpochMs = $Marker.childStartEpochMs
        childImagePath    = $Marker.childImagePath
        keepAwakeBeginUtc = $keepAwakeBeginUtc
        keepAwakeBeginOk  = $keepAwakeBeginOk
        keepAwakeEndUtc   = $keepAwakeEndUtc
        keepAwakeEndOk    = $keepAwakeEndOk
        killedProcessIds  = @($killedProcessIds)
        admission         = Get-RecoveredAdmission -Admission $Marker.admission
        stdout            = if (Test-Path -LiteralPath $Marker.outFile) { Get-Content -LiteralPath $Marker.outFile -Raw } else { "" }
        stderr            = if (Test-Path -LiteralPath $Marker.errFile) { Get-Content -LiteralPath $Marker.errFile -Raw } else { "" }
    }
    $resultPath = Publish-JobResult -JobId $jobId -Result $result
    if (-not [string]::IsNullOrWhiteSpace($Marker.sourceInboxPath)) {
        Move-JobArtifacts -JobId $jobId -JobPath $Marker.sourceInboxPath -DestinationDirectory $processed
    }
    Complete-StartedMarker -MarkerPath $MarkerPath -Status "adopted" -ResultPath $resultPath
}

function Invoke-RequeueAdmissionWait {
    # The claim stays in running\ (rewritten under THIS agent) while the job waits its turn again, so a VM client
    # keeps reading "held, not launched" instead of "unclaimed" across the restart. No result is published.
    # Callers reach here ONLY when it is certain nothing launched (an admission-wait marker, or a launching
    # marker with no live child and no out file). If the rewrite does not verify, the job is still re-queued:
    # the disk keeps the old admission-wait / launching marker, which a later restart judges the same way.
    param([Parameter(Mandatory = $true)]$Marker, [Parameter(Mandatory = $true)][string]$MarkerPath,
        [string]$Why = "admission-wait, never launched; not orphaned")
    $jobId = [string]$Marker.jobId
    $jobPath = Join-Path $inbox "$jobId.job.ps1"
    if (Test-Path -LiteralPath $jobPath -PathType Leaf) {
        $o = [ordered]@{}
        foreach ($p in $Marker.PSObject.Properties) { $o[$p.Name] = $p.Value }
        $o.previousPhase = $Marker.phase
        $o.phase = "admission-wait"
        $o.status = "admission-wait"
        $o.previousAgentPid = $Marker.agentPid
        $o.agentPid = $PID
        $o.agentProcessStartUtc = $script:AgentProcessStartUtc
        $o.requeuedUtc = (Get-Date).ToUniversalTime().ToString("o")
        $o.gate = $script:GateVersion
        $o.gateReason = "requeued after agent restart (previous agent pid=$($Marker.agentPid) died before launching it: $Why)"
        $ok = Write-StartedMarkerVerified -Path $MarkerPath -Marker ([pscustomobject]$o)
        Add-AgentLogLine "heavy-slot-gate: requeued job=$jobId after restart ($Why) markerVerified=$ok"
        return
    }
    $dest = Join-Path $processed "$jobId.started.requeue-no-job.json"
    Move-Item -Force -LiteralPath $MarkerPath -Destination $dest
    Add-AgentLogLine "heavy-slot-gate: admission-wait job=$jobId has no job file after restart; claim retired to $dest; no result published"
}

function Resolve-LaunchingMarker {
    # THREE OUTCOMES for a launching marker with no result (the previous agent died in its launch window):
    #   DONE (launched, child live)  - exactly one live process runs this job's script: publish phase=launched
    #                                  with its identity and ADOPT it (result adopted=true). Never re-queued.
    #   NOT DONE (certain)           - no live child AND the out file is absent, so Process.Start was never
    #                                  reached: re-queue (the job runs once, later).
    #   CANNOT TELL                  - the process table is unreadable, several candidates exist, or no live child
    #                                  but the out file exists (it may have run and exited): ORPHAN (exit 127,
    #                                  job script preserved under orphaned\). Never re-queued, never run twice.
    param([Parameter(Mandatory = $true)]$Marker, [Parameter(Mandatory = $true)][string]$MarkerPath)
    $jobId = [string]$Marker.jobId
    $child = Find-LaunchedJobChild -Marker $Marker
    if ($child.state -eq "found") {
        Set-LaunchedMarkerFields -Marker $Marker -Identity $child.identity
        $ok = Write-StartedMarkerVerified -Path $MarkerPath -Marker $Marker
        Add-AgentLogLine "launching marker job=${jobId}: $($child.why); adopting (launched marker verified=$ok)"
        $match = Test-ProcessIdentityMatch -ProcessId ([int]$Marker.childPid) -ExpectedStartUtc ([string]$Marker.childStartUtc) -ExpectedStartEpochMs $Marker.childStartEpochMs -ExpectedImagePath $Marker.childImagePath
        Invoke-AdoptStartedJob -Marker $Marker -MarkerPath $MarkerPath -Match $match
        return
    }
    $out = Test-LaunchOutFile -Path ([string]$Marker.outFile)
    if ($child.state -eq "none" -and $out -eq "absent") {
        Invoke-RequeueAdmissionWait -Marker $Marker -MarkerPath $MarkerPath -Why "launching marker, no live child and no out file: Process.Start was never reached"
        return
    }
    $reason = "launching marker with no launched write: the previous agent died in its launch window and a launch cannot be ruled out (child: $($child.state), $($child.why); out file: $out)"
    Add-AgentLogLine "launching marker job=$jobId cannot tell; orphaning, never re-queued: $reason"
    Invoke-OrphanResult -Marker $Marker -MarkerPath $MarkerPath -Reason $reason
}

function Get-MarkerTempFiles {
    # r2d: the unresolved marker tmps in running\, oldest first: '<id>.started.json.<32 hex>.tmp' (r2d, unique per
    # write) and the legacy shared '<id>.started.json.tmp' (r2c and earlier, e.g. after a rollback). A set-aside record
    # ('.stale-<ticks>', '.corrupt-<ticks>') is resolved and is not listed. Returns @{ ok; items; error }: ok=$false =
    # running\ could not be listed (CANNOT TELL, never "none").
    param([string]$JobId = "")
    try { $files = @(Get-ChildItem -LiteralPath $running -File -ErrorAction Stop) }
    catch { return [pscustomobject]@{ ok = $false; items = @(); error = $_.Exception.Message } }
    $items = @()
    foreach ($f in $files) {
        $mm = [regex]::Match($f.Name, '^(?<id>.+)\.started\.json(\.[0-9a-f]{32})?\.tmp$')
        if (-not $mm.Success) { continue }
        if ($JobId -and $mm.Groups['id'].Value -ne $JobId) { continue }
        $items += [pscustomobject]@{ JobId = $mm.Groups['id'].Value; File = $f }
    }
    return [pscustomobject]@{ ok = $true; items = @($items | Sort-Object { $_.File.LastWriteTimeUtc }, { $_.File.Name }); error = $null }
}

function Read-MarkerArtifact {
    # r2d: one read of a marker or marker tmp. FOUR outcomes, never merged:
    #   ok       parsed, with a jobId
    #   missing  not there
    #   locked   could not be opened (a reader holds it with no sharing, access denied): CANNOT TELL what it holds
    #   corrupt  opened but not a marker (a write killed mid-Set-Content leaves a short or empty file)
    # Opens with ReadWrite+Delete sharing, so this read never denies anyone's rename.
    param([Parameter(Mandatory = $true)][string]$Path)
    $text = $null
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]"ReadWrite, Delete")
        try { $text = ([System.IO.StreamReader]::new($fs)).ReadToEnd() }
        finally { $fs.Dispose() }
    }
    catch {
        $e = $_.Exception
        if ($null -ne $e.InnerException) { $e = $e.InnerException }
        if ($e -is [System.IO.FileNotFoundException] -or $e -is [System.IO.DirectoryNotFoundException]) {
            return [pscustomobject]@{ state = "missing"; marker = $null; why = "not found" }
        }
        return [pscustomobject]@{ state = "locked"; marker = $null; why = "$($e.GetType().Name): $($e.Message)" }
    }
    $m = $null
    try { $m = $text | ConvertFrom-Json -ErrorAction Stop } catch { }
    if ($null -eq $m -or [string]::IsNullOrWhiteSpace([string]$m.jobId)) {
        return [pscustomobject]@{ state = "corrupt"; marker = $null; why = "not a marker ($($text.Length) chars)" }
    }
    return [pscustomobject]@{ state = "ok"; marker = $m; why = $null }
}

function Move-MarkerTmpAside {
    # r2d: a settled tmp is PRESERVED under a name recovery no longer lists ('<tmp>.<kind>-<ticks>'); never deleted.
    param([Parameter(Mandatory = $true)]$File, [Parameter(Mandatory = $true)][string]$Kind, [Parameter(Mandatory = $true)][string]$JobId, [string]$Why = "")
    $aside = "$($File.FullName).$Kind-$([DateTime]::UtcNow.Ticks)"
    $mv = Move-FileChecked -Source $File.FullName -Destination $aside -Overwrite $false
    if ($mv.ok) { Add-AgentLogLine "orphan marker tmp set aside $($File.Name) -> $([IO.Path]::GetFileName($aside)) ($Why)"; return $true }
    Add-AgentLogLine "SET-ASIDE FAILED $JobId $($File.FullName) $($mv.error)"
    return $false
}

function Resolve-JobMarkerTemps {
    # r2d (sol r2c BLOCKER): settle ONE job's unresolved marker tmps before its final marker is judged. Returns $true
    # when none remain, $false when the job must stay QUARANTINED -- and then every record is left exactly as found:
    # nothing promoted over, set aside, overwritten or deleted. THREE OUTCOMES:
    #   promote   the NEWEST readable tmp carrying child identity (childPid) when the final marker is missing, cannot
    #             be read, or lacks childPid; with no child tmp and no final marker, the newest readable tmp. CHECKED
    #             (Move-FileChecked: try/catch, bounded retry). A failure logs PROMOTE FAILED, never "promoted".
    #   set aside every other tmp, ONLY after the promotion (if any) landed: '.stale-<ticks>', or '.corrupt-<ticks>'
    #             for one that opened but did not parse (its write never finished; an older record holds the state).
    #   wait      a tmp that cannot be opened, or a final marker that cannot be read while no tmp carries the child:
    #             CANNOT TELL, so nothing is touched.
    param([Parameter(Mandatory = $true)][string]$JobId)
    $final = Join-Path $running "$JobId.started.json"
    $list = Get-MarkerTempFiles -JobId $JobId
    if (-not $list.ok) { $script:LastRecoveryWhy = "running\ cannot be listed: $($list.error)"; return $false }
    $tmps = @($list.items)
    if ($tmps.Count -eq 0) { return $true }
    $readable = @()
    $corrupt = @()
    foreach ($t in $tmps) {
        $a = Read-MarkerArtifact -Path $t.File.FullName
        if ($a.state -eq "ok") { $readable += [pscustomobject]@{ file = $t.File; marker = $a.marker } }
        elseif ($a.state -eq "corrupt") { $corrupt += $t.File }
        elseif ($a.state -eq "locked") { $script:LastRecoveryWhy = "marker tmp $($t.File.Name) cannot be read: $($a.why)"; return $false }
    }
    $fa = Read-MarkerArtifact -Path $final
    $childTmp = @($readable | Where-Object { $null -ne $_.marker.childPid }) | Select-Object -Last 1
    $promote = $null
    if ($fa.state -eq "missing") { $promote = $(if ($childTmp) { $childTmp } else { $readable | Select-Object -Last 1 }) }
    elseif ($fa.state -eq "ok") { if ($childTmp -and $null -eq $fa.marker.childPid) { $promote = $childTmp } }
    elseif ($childTmp) { $promote = $childTmp }
    else { $script:LastRecoveryWhy = "final marker $($fa.state) ($($fa.why)) and no tmp carries a child"; return $false }
    if ($promote) {
        $mv = Move-FileChecked -Source $promote.file.FullName -Destination $final -Overwrite ($fa.state -ne "missing")
        if (-not $mv.ok) {
            Add-AgentLogLine "PROMOTE FAILED $JobId $($promote.file.FullName) $($mv.error)"
            $script:LastRecoveryWhy = "PROMOTE FAILED after $($mv.attempts) attempts: $($mv.error)"
            return $false
        }
        Add-AgentLogLine "orphan marker tmp promoted $($promote.file.Name) -> $([IO.Path]::GetFileName($final)) phase=$($promote.marker.phase) childPid=$($promote.marker.childPid) attempts=$($mv.attempts) (final marker was $($fa.state)$(if ($fa.state -eq 'ok') { ", childPid=$($fa.marker.childPid)" }))"
    }
    $settled = $true
    foreach ($r in $readable) {
        if ($promote -and $r.file.FullName -eq $promote.file.FullName) { continue }
        if (-not (Move-MarkerTmpAside -File $r.file -Kind "stale" -JobId $JobId -Why "the marker is authoritative")) { $settled = $false }
    }
    foreach ($c in $corrupt) {
        if (-not (Move-MarkerTmpAside -File $c -Kind "corrupt" -JobId $JobId -Why "unparseable: an unfinished write")) { $settled = $false }
    }
    if (-not $settled) { $script:LastRecoveryWhy = "a settled marker tmp could not be set aside" }
    return $settled
}

function Resolve-StrandedResult {
    # r2d: '<id>.result.<32 hex>.tmp' (or the legacy '<id>.result.tmp') with no '<id>.result.json' is a result whose ONE
    # replacing rename failed (PROMOTE FAILED) or whose agent died before it. It is a recovery record: promote the NEWEST
    # that parses as this job's result (CHECKED, Move-FileChecked); never one that does not parse. THREE OUTCOMES:
    # $true = nothing stranded, or promoted; $false = a candidate cannot be read, or its promotion failed (the job
    # stays quarantined and every tmp stays where it is).
    param([Parameter(Mandatory = $true)][string]$JobId)
    $fin = Join-Path $outbox "$JobId.result.json"
    if (Test-Path -LiteralPath $fin -PathType Leaf) { return $true }
    $rx = '^' + [regex]::Escape($JobId) + '\.result(\.[0-9a-f]{32})?\.tmp$'
    $cands = @(Get-ChildItem -LiteralPath $outbox -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match $rx } | Sort-Object LastWriteTimeUtc, Name)
    for ($i = $cands.Count - 1; $i -ge 0; $i--) {
        $a = Read-MarkerArtifact -Path $cands[$i].FullName
        if ($a.state -eq "locked") { $script:LastRecoveryWhy = "stranded result tmp $($cands[$i].Name) cannot be read: $($a.why)"; return $false }
        if ($a.state -ne "ok" -or [string]$a.marker.jobId -ne $JobId) { continue }
        $mv = Move-FileChecked -Source $cands[$i].FullName -Destination $fin -Overwrite $false
        if (-not $mv.ok) {
            Add-AgentLogLine "PROMOTE FAILED $JobId $($cands[$i].FullName) $($mv.error)"
            $script:LastRecoveryWhy = "stranded result PROMOTE FAILED after $($mv.attempts) attempts: $($mv.error)"
            return $false
        }
        Add-AgentLogLine "stranded result tmp promoted $($cands[$i].Name) -> $([IO.Path]::GetFileName($fin)) attempts=$($mv.attempts)"
        return $true
    }
    return $true
}

function Resolve-StartedMarkerFile {
    # The r2c per-marker recovery logic, unchanged, for ONE readable final marker. r2d: reached only once the job's
    # tmps are settled (Resolve-JobRecovery), and after any stranded result of the job is promoted.
    param([Parameter(Mandatory = $true)]$Marker, [Parameter(Mandatory = $true)][string]$MarkerPath)
    if (-not (Resolve-StrandedResult -JobId ([string]$marker.jobId))) { return }
    $resultPath = Join-Path $outbox "$($marker.jobId).result.json"
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        Complete-StartedMarker -MarkerPath $MarkerPath -Status "completed" -ResultPath $resultPath
        return
    }

    # GATE-2: a job the previous agent was HOLDING in the admission gate never launched (no child), so it is
    # neither adoptable nor orphaned. Its job file is still in inbox\, so it is re-queued: the main loop below
    # gates it again. r1c: a waiting job held no slot; its queued holder is stopped at start (Stop-StaleHeavySlotHolders).
    if ($marker.phase -eq "admission-wait" -and $null -eq $marker.childPid) {
        Invoke-RequeueAdmissionWait -Marker $marker -MarkerPath $MarkerPath
        return
    }

    # r2b: the previous agent died between its launching write and its launched write, so a child may or
    # may not exist. Never re-queued unless it is certain nothing launched.
    if ($marker.phase -eq "launching" -and $null -eq $marker.childPid) {
        Resolve-LaunchingMarker -Marker $marker -MarkerPath $MarkerPath
        return
    }

    if ($null -ne $marker.childPid) {
        $match = Test-ProcessIdentityMatch -ProcessId ([int]$marker.childPid) -ExpectedStartUtc ([string]$marker.childStartUtc) -ExpectedStartEpochMs $marker.childStartEpochMs -ExpectedImagePath $marker.childImagePath
        if ($match.matches) {
            Invoke-AdoptStartedJob -Marker $marker -MarkerPath $MarkerPath -Match $match
            return
        }
        Add-AgentLogLine "started marker child not adoptable job=$($marker.jobId) pid=$($marker.childPid) reason=$($match.reason) startMatches=$($match.startMatches) imageMatches=$($match.imageMatches) expectedStart=$($marker.childStartUtc) actualStart=$($match.identity.startUtc) expectedEpochMs=$($marker.childStartEpochMs) actualEpochMs=$($match.identity.startEpochMs) expectedImage=$($marker.childImagePath) actualImage=$($match.identity.imagePath)"
    }

    Invoke-OrphanResult -Marker $marker -MarkerPath $MarkerPath
}

function Get-JobRecoveryPending {
    # r2d: why the ordinary inbox loop must NOT admit or launch this job now, or $null when it may. It may only when
    # the job has NO marker artifact, or its one artifact is a final marker that is THIS agent's own unlaunched claim
    # (admission-wait / launching, childPid null, carrying a writeId this agent stamped: a launch this agent refused).
    param([Parameter(Mandatory = $true)][string]$JobId, [switch]$IgnoreQuarantine)
    if (-not $IgnoreQuarantine -and $script:RecoveryQuarantine.ContainsKey($JobId)) { return "quarantined: $($script:RecoveryQuarantine[$JobId])" }
    $list = Get-MarkerTempFiles -JobId $JobId
    if (-not $list.ok) { return "running\ cannot be listed: $($list.error)" }
    if (@($list.items).Count -gt 0) { return "unresolved marker tmp $(@($list.items)[0].File.Name)" }
    $fa = Read-MarkerArtifact -Path (Join-Path $running "$JobId.started.json")
    if ($fa.state -eq "missing") { return $null }
    if ($fa.state -ne "ok") { return "final marker $($fa.state): $($fa.why)" }
    $m = $fa.marker
    if ($m.writeId -and $script:OwnMarkerWriteIds.Contains([string]$m.writeId) -and $m.phase -in @("admission-wait", "launching") -and $null -eq $m.childPid) { return $null }
    return "marker phase=$($m.phase) childPid=$($m.childPid) agentPid=$($m.agentPid) not yet reconciled by this agent"
}

function Resolve-JobRecovery {
    # r2d: reconcile ONE job's marker artifacts: settle its tmps (Resolve-JobMarkerTemps), then run the r2c logic on
    # its readable final marker (Resolve-StartedMarkerFile). Returns $true when nothing unreconciled is left. Otherwise
    # the job is QUARANTINED (RecoveryQuarantine): the ordinary inbox loop never admits or launches it, its records stay
    # exactly as found, and Invoke-QuarantineReconcile retries it every poll (e.g. once a reader has let go).
    param([Parameter(Mandatory = $true)][string]$JobId)
    $script:LastRecoveryWhy = $null
    $ok = Resolve-JobMarkerTemps -JobId $JobId
    if ($ok) {
        $final = Join-Path $running "$JobId.started.json"
        $fa = Read-MarkerArtifact -Path $final
        if ($fa.state -eq "ok") {
            $null = Resolve-StartedMarkerFile -Marker $fa.marker -MarkerPath $final
            $left = Get-JobRecoveryPending -JobId $JobId -IgnoreQuarantine
            if ($left) { $ok = $false; $script:LastRecoveryWhy = "recovery ran but left: $left" }
        }
        elseif ($fa.state -ne "missing") { $ok = $false; $script:LastRecoveryWhy = "final marker $($fa.state): $($fa.why)" }
    }
    if ($ok) {
        if ($script:RecoveryQuarantine.ContainsKey($JobId)) {
            $script:RecoveryQuarantine.Remove($JobId)
            Add-AgentLogLine "recovery reconciled job=$JobId; quarantine released"
        }
        return $true
    }
    $why = [string]$script:LastRecoveryWhy
    if ($script:RecoveryQuarantine[$JobId] -ne $why) { Add-AgentLogLine "QUARANTINED job=$JobId (never admitted or launched until recovery reconciles it): $why" }
    $script:RecoveryQuarantine[$JobId] = $why
    return $false
}

function Invoke-QuarantineReconcile {
    # r2d: every poll, retry each quarantined job's recovery.
    foreach ($id in @($script:RecoveryQuarantine.Keys)) { $null = Resolve-JobRecovery -JobId $id }
}

function Resolve-StartedMarkers {
    # r2d: every job with ANY marker artifact in running\ -- a final marker, a tmp, or one that cannot be read -- is
    # reconciled at startup; one that cannot be is QUARANTINED (Resolve-JobRecovery) and retried every poll.
    $ids = [System.Collections.Generic.SortedSet[string]]::new([StringComparer]::Ordinal)
    foreach ($f in @(Get-ChildItem -LiteralPath $running -Filter *.started.json -File -ErrorAction SilentlyContinue)) {
        if ($f.Name -like "*.started.json") { [void]$ids.Add($f.Name.Substring(0, $f.Name.Length - ".started.json".Length)) }
    }
    foreach ($t in @((Get-MarkerTempFiles).items)) { [void]$ids.Add($t.JobId) }
    foreach ($id in $ids) { $null = Resolve-JobRecovery -JobId $id }
}

# ---------------------------------------------------------------------------
# Heavy-slot admission gate (MLV-AGENT-HEAVY-SLOT-GATE-1, 2026-10-10, owner-directed;
# reworked by AGENT-HEAVY-SLOT-WAIT-RELEASE-1 r1c, 2026-10-11)
#
# WHY: this agent ran CUDA playback-smoke jobs outside the laptop's machine-wide heavy-slot cap
# (~\.claude\ops\heavy-slot.py, MACHINE.md s0.14) and throttled it at 100 C (2026-10-09 22:15).
# These jobs are playback-timing attribution, so a job beside another heavy run also measures that
# run: every gated job runs ALONE (no other slot held) and EXCLUSIVE (nothing joins it).
#
# r1c (design from the Bachelor heat-coordination session, owner-relayed; MACHINE s0.15-s0.28):
#   1. SUSTAINED HEAT, not spikes. The feed's PackageC is the hottest single core, and few-thread
#      P-core turbo touches TjMax at 40-65 W by design, so the old 15-row max read turbo spikes as
#      busy (rows at maxC 93-100 with throttled 0/15 held jobs). A job is blocked on heat only when
#      more than GateMaxThrottledRows of the last GateSustainedRows feed rows are throttled, OR their
#      mean is >= GateMeanMaxC: heavy-slot.py's own first-run rule (thr60 <= 6 and mean60 < 88).
#   2. FAIRNESS, and no wait cap. The long waits (9390 s on 2026-10-11) were starvation: an exclusive
#      job needs an EMPTY machine and other runs kept refilling it between this agent's polls. So the
#      agent no longer polls for a gap. It joins heavy-slot.py's own FIFO by running a slot HOLDER:
#          python heavy-slot.py run --exclusive --label "mlv <jobId>" --slots N -- <psExe> -EncodedCommand <sentinel>
#      heavy-slot.py files the ticket (<slot dir>\queue\<ns>-<pid>.json), serves the oldest live
#      ticket first, waits for an empty machine, acquires the slot exclusively, runs the sentinel and
#      releases the slot when the sentinel exits. Dead waiters' tickets and dead owners' slots are
#      reclaimed by heavy-slot.py. THIS AGENT NEVER WRITES A SLOT OR A TICKET ITSELF.
#      The sentinel publishes slot-holders\<id>.acquired.json (with HEAVY_SLOT_HELD="<slot>:<owner
#      pid>"), then holds until slot-holders\<id>.release exists or this agent (pid + start time) is
#      gone. The job itself is NOT wrapped: the launch handoff, timeout, adoption and child-identity
#      contracts are unchanged. The job inherits HEAVY_SLOT_HELD, so a heavy-slot.py run nested in it
#      passes straight through (heavy-slot.py NESTING). There is no wait cap and no admitted-after-cap
#      label: starting CUDA playback beside a running suite is the pairing this gate exists to prevent.
#   3. A WAITING JOB HOLDS NO SLOT. r1j-r1k reserved a slot while waiting and then counted that
#      reservation as busy ("all 2 slot(s) held" with no MLVApp running). Now only the holder holds a
#      slot, and only from acquisition to the end of the job.
#
# Once the holder has acquired, the agent re-checks with its own slot excluded: busy == 0 and
# sustained heat clear. If either fails, or the feed or status cannot be read, the holder is released
# and the job re-joins the FIFO: a job that is not running never keeps a slot. Light probes
# (GateExemptJobPattern) are not gated and take no slot.
# maint-dispatch r1: light jobs are also run inline from inside another job's gate-wait (Invoke-ExemptJobsDuringWait),
# once per GateProbeSec, in Name order, so a job waiting on an unknown feed cannot starve probes, dve health checks or
# agent-swap. No ordinary job is touched by that scan, so ordinary order is unchanged. A slot the waiting holder
# acquires during a light job is re-checked as before once the light job returns (bounded by its own timeoutSec).
#
# r2 (sol r1 BLOCKER, HOLDER LOSS): a holder can exit after acquiring, and heavy-slot.py then gives its slot to
# another run. So the agent's own slot is excluded from busy ONLY when `status` shows it held by this job's label
# AND the owner pid in HEAVY_SLOT_HELD, and the holder must still hold it after every status probe. Otherwise the
# verdict is holder-lost: never clear, the slot's real holder counts as busy, the holder is ended and the job
# re-joins the FIFO. Immediately before the child is created the same proof is re-run (Confirm-HeavySlotHeldForLaunch);
# a failure there launches nothing and leaves the job queued. Every such event is typed in agent.log and kept in
# the job's admission.holderLost.
#
# Every probe goes to logs\heavy-slot-gate.jsonl (also sampled every GateIdleSampleSec while idle), with an
# hourly summary in agent.log and logs\heavy-slot-gate-hourly.jsonl. The heartbeat carries
# "gate-wait job=<id> state=<s> waited=<n>s", which um-run.ps1 reads.
#
# CLAIM (MLV-AGENT-HEAVY-SLOT-GATE-2 r2): a held job is CLAIMED before it waits -- running\<id>.started.json with
# phase=admission-wait and childPid null. Without it the VM client (tools/profiling/um-run.ps1) saw no marker
# during a hold and RETRACTED the job at its queue ceiling. A restart never orphans an admission-wait claim:
# nothing launched, so the job is re-queued (Resolve-StartedMarkers).
# r2b LAUNCH HANDOFF (sol blockers on r2): admission-wait -> launching (verified, or NO launch: the job stays queued)
# -> Process.Start -> launched (one write, with childPid). phase=launched never exists without a child. A restart
# that finds launching adopts a live child found by command line, re-queues only on proof nothing launched (no
# live child and no out file), and otherwise orphans (Resolve-LaunchingMarker).
# ---------------------------------------------------------------------------
$script:GateVersion = "heavy-slot-gate-1 r1k + heavy-slot-gate-2 r2e + wait-release r1c fifo-holder sustained-heat + r2 holder-owner-check (2026-10-12) + maint-dispatch r1 (2026-10-14)"
$script:HeavySlotHolder = $null
$script:Admission = $null
$script:ExemptDuringWaitOf = $null
$script:HolderLossByJob = @{}
$script:GateHourKey = $null
$script:GateHour = $null
$script:GateLastIdleSampleUtc = [DateTime]::MinValue

function Resolve-HeavySlotPython {
    if (Test-Path -LiteralPath $HeavySlotPython -PathType Leaf) { return $HeavySlotPython }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Invoke-HeavySlotStatus {
    # Returns @{ ok; lines; error }. ok=$false is UNKNOWN occupancy, never an empty machine.
    param([string]$HeartbeatActivity = "")
    $py = Resolve-HeavySlotPython
    if (-not $py) { return @{ ok = $false; lines = @(); error = "python not found ($HeavySlotPython)" } }
    if (-not (Test-Path -LiteralPath $HeavySlotScript -PathType Leaf)) { return @{ ok = $false; lines = @(); error = "heavy-slot.py not found ($HeavySlotScript)" } }
    $p = $null
    try {
        $si = [System.Diagnostics.ProcessStartInfo]::new()
        $si.FileName = $py
        foreach ($a in @($HeavySlotScript, "status", "--slots", "$HeavySlotCount")) { [void]$si.ArgumentList.Add($a) }
        $si.UseShellExecute = $false
        $si.CreateNoWindow = $true
        $si.RedirectStandardOutput = $true
        $si.RedirectStandardError = $true
        $si.Environment["HEAVY_SLOT_DIR"] = $HeavySlotDir
        $si.Environment["HEAVY_SLOT_FEED"] = $HeavySlotFeed
        $p = [System.Diagnostics.Process]::Start($si)
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        # Wait in 5 s slices, heartbeating between them, so a stalled probe never opens a heartbeat gap
        # (the VM client reads a heartbeat older than 30 s as a possibly-dead agent).
        $deadline = (Get-Date).AddSeconds(30)
        while (-not $p.WaitForExit(5000)) {
            Write-AgentHeartbeat -Activity $HeartbeatActivity
            if ((Get-Date) -ge $deadline) {
                try { $p.Kill($true) } catch { }
                return @{ ok = $false; lines = @(); error = "status timed out after 30 s" }
            }
        }
        $p.WaitForExit()
        $lines = @($outTask.Result -split "`r?`n" | Where-Object { $_ -ne "" })
        if ($p.ExitCode -ne 0) { return @{ ok = $false; lines = $lines; error = "status exit $($p.ExitCode): $($errTask.Result.Trim())" } }
        return @{ ok = $true; lines = $lines; error = $null }
    }
    catch {
        return @{ ok = $false; lines = @(); error = "status failed: $($_.Exception.Message)" }
    }
    finally {
        if ($null -ne $p) { $p.Dispose() }
    }
}

function ConvertFrom-HeavySlotFeedRows {
    # Pure: heavy-slot.py's feed_metrics() over feed CSV rows (oldest first; the last 150 are used). Columns:
    # 3 = PackageC (the hottest core), 4 = package W, 5 / 6 = core / package-ring throttling ("Yes").
    # Returns $null (feed unknown) when a data row does not parse or fewer than 15 data rows remain.
    param([AllowEmptyCollection()][AllowNull()][string[]]$Lines)
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $num = [System.Globalization.NumberStyles]::Float
    $recs = [System.Collections.Generic.List[object]]::new()
    foreach ($r in @($Lines | Select-Object -Last 150)) {
        if ([string]::IsNullOrEmpty($r)) { continue }
        $c = $r.Split(",")
        if ($c.Count -lt 7 -or $c[0].Length -lt 1 -or -not [char]::IsDigit($c[0], 0)) { continue }
        $t = 0.0
        $w = 0.0
        if (-not [double]::TryParse($c[3], $num, $inv, [ref]$t)) { return $null }
        if (-not [double]::TryParse($c[4], $num, $inv, [ref]$w)) { return $null }
        $recs.Add([pscustomobject]@{ c = $t; w = $w; thr = ($c[5].Trim() -eq "Yes" -or $c[6].Trim() -eq "Yes") })
    }
    if ($recs.Count -lt 15) { return $null }
    $win = @($recs | Select-Object -Last $GateSustainedRows)
    $last15 = @($recs | Select-Object -Last 15)
    return [pscustomobject]@{
        rows      = $recs.Count
        window    = $win.Count
        throttled = @($win | Where-Object { $_.thr }).Count
        meanC     = ($win | Measure-Object -Property c -Average).Average
        max15C    = ($last15 | Measure-Object -Property c -Maximum).Maximum
        watts30   = [Math]::Round(($last15 | Measure-Object -Property w -Average).Average, 1)
    }
}

function Read-HeavySlotFeedMetrics {
    # Returns @{ metrics; why }. metrics=$null is FEED UNKNOWN: missing, older than GateFeedStaleSec, unreadable,
    # or fewer than 15 data rows (heavy-slot.py's own staleness and row rules).
    try {
        $fi = [System.IO.FileInfo]::new($HeavySlotFeed)
        if (-not $fi.Exists) { return @{ metrics = $null; why = "feed missing ($HeavySlotFeed)" } }
        $age = ([DateTime]::UtcNow - $fi.LastWriteTimeUtc).TotalSeconds
        if ($age -gt $GateFeedStaleSec) { return @{ metrics = $null; why = "feed stale ($([int]$age) s > $GateFeedStaleSec s)" } }
        $fs = [System.IO.File]::Open($HeavySlotFeed, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)
        $start = [Math]::Max([long]0, $fs.Length - (80 * 150 + 400))
        try {
            [void]$fs.Seek($start, [System.IO.SeekOrigin]::Begin)
            $sr = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8)
            $text = $sr.ReadToEnd()
        }
        finally { $fs.Dispose() }
        $lines = @($text -split "`r?`n")
        if ($start -gt 0) { $lines = @($lines | Select-Object -Skip 1) }   # the first line read mid-row is partial
        $m = ConvertFrom-HeavySlotFeedRows -Lines $lines
        if ($null -eq $m) { return @{ metrics = $null; why = "feed unreadable or fewer than 15 rows" } }
        return @{ metrics = $m; why = $null }
    }
    catch {
        return @{ metrics = $null; why = "feed read failed: $($_.Exception.Message)" }
    }
}

function Get-SustainedHeatVerdict {
    # Pure. BLOCKED (hot) only when more than GateMaxThrottledRows of the last GateSustainedRows rows are throttled,
    # or their mean is >= GateMeanMaxC -- heavy-slot.py's first-run rule. A turbo spike (a 100 C max with no
    # throttled rows and a cool mean) does NOT block. $Metrics=$null is feed-unknown.
    param($Metrics, [string]$UnknownWhy = "feed unknown")
    if ($null -eq $Metrics) { return [pscustomobject]@{ state = "feed-unknown"; why = $UnknownWhy } }
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $s = [string]::Format($inv, "throttled {0}/{1}, mean {2:F1} C, max15 {3:F0} C, {4:F0} W", $Metrics.throttled, $Metrics.window, $Metrics.meanC, $Metrics.max15C, $Metrics.watts30)
    if ($Metrics.throttled -gt $GateMaxThrottledRows -or $Metrics.meanC -ge $GateMeanMaxC) {
        return [pscustomobject]@{ state = "hot"; why = "sustained heat: $s (blocks at > $GateMaxThrottledRows throttled or mean >= $GateMeanMaxC C)" }
    }
    return [pscustomobject]@{ state = "clear"; why = $s }
}

function Get-HeavySlotOccupancy {
    # Pure: who holds a slot, from `heavy-slot.py status`. $OwnSlot (this job's holder slot) counts as neither busy
    # nor free, so busy is about everyone ELSE. Also reads this job's position in heavy-slot.py's FIFO (queue lines).
    # ok=$false is UNKNOWN, never empty.
    # r2 (sol r1 BLOCKER): $OwnSlot is excluded ONLY when status shows it held by this job's own holder: label $Label
    # AND owner pid $OwnPid (the pid in HEAVY_SLOT_HELD). Held by anyone else, the holder lost it (it exited and
    # heavy-slot.py gave the slot to another run): that holder counts as BUSY and ownLost is set -- never excluded.
    param($Status, [int]$OwnSlot = 0, [int]$OwnPid = 0, [string]$Label = "")
    $o = [ordered]@{ ok = $false; busy = 0; holders = @(); queuePos = 0; queueLen = 0; ownLost = $false; why = $null }
    if (-not $Status.ok) { $o.why = $Status.error; return [pscustomobject]$o }
    $slotLines = @($Status.lines | Where-Object { $_ -match '^slot \d+: ' })
    if ($slotLines.Count -lt 1) { $o.why = "status output unrecognised"; return [pscustomobject]$o }
    $ownSeen = $false
    $ownWho = $null
    foreach ($l in $slotLines) {
        if ($l -notmatch '^slot (\d+): (.*)$') { continue }
        $n = [int]$Matches[1]
        $who = $Matches[2]
        if ($OwnSlot -gt 0 -and $n -eq $OwnSlot -and $who -ne "free") {
            if ($Label -and $OwnPid -gt 0 -and $who -match '^(.*?) \(pid (\d+)[,)]' -and $Matches[1] -eq $Label -and [int]$Matches[2] -eq $OwnPid) { $ownSeen = $true; continue }
            $ownWho = $who
            $o.ownLost = $true
        }
        if ($who -ne "free") { $o.busy++; $o.holders += $who }
    }
    if ($o.ownLost) {
        $o.ok = $true
        $o.why = "own slot $OwnSlot is held by '$ownWho', not by '$Label' pid ${OwnPid}: this job's holder lost it"
        return [pscustomobject]$o
    }
    foreach ($l in @($Status.lines | Where-Object { $_ -match '^queue \d+: ' })) {
        if ($l -match '^queue (\d+): (.*) \(pid ') {
            $o.queueLen++
            if ($Label -and $o.queuePos -eq 0 -and $Matches[2] -eq $Label) { $o.queuePos = [int]$Matches[1] }
        }
    }
    if ($OwnSlot -gt 0 -and -not $ownSeen) { $o.why = "own slot $OwnSlot not shown as held"; return [pscustomobject]$o }
    $o.ok = $true
    return [pscustomobject]$o
}

function Get-HeavySlotVerdict {
    # Pure: one admission verdict from a status probe and a feed read. State, first match wins:
    #   status-unknown (occupancy unreadable) | holder-lost (own slot held by another run) | busy (another run holds
    #   a slot) | feed-unknown | hot | clear
    param($Status, $Feed, [int]$OwnSlot = 0, [int]$OwnPid = 0, [string]$Label = "")
    $occ = Get-HeavySlotOccupancy -Status $Status -OwnSlot $OwnSlot -OwnPid $OwnPid -Label $Label
    $heat = Get-SustainedHeatVerdict -Metrics $Feed.metrics -UnknownWhy ([string]$Feed.why)
    $m = $Feed.metrics
    $v = [ordered]@{
        state = $null; busy = $occ.busy; holders = @($occ.holders); queuePos = $occ.queuePos; queueLen = $occ.queueLen
        heat = $heat.state; throttled = $null; window = $null; meanC = $null; max15C = $null; watts = $null
        thermal = $heat.why; why = $null
    }
    if ($null -ne $m) {
        $v.throttled = $m.throttled; $v.window = $m.window; $v.meanC = [Math]::Round($m.meanC, 1); $v.max15C = $m.max15C; $v.watts = $m.watts30
    }
    if (-not $occ.ok) { $v.state = "status-unknown"; $v.why = $occ.why }
    elseif ($occ.ownLost) { $v.state = "holder-lost"; $v.why = $occ.why }
    elseif ($occ.busy -gt 0) { $v.state = "busy"; $v.why = "busy: $($occ.busy) other heavy run(s): $($occ.holders -join '; ')" }
    elseif ($heat.state -ne "clear") { $v.state = $heat.state; $v.why = $heat.why }
    else { $v.state = "clear"; $v.why = "clear: $($heat.why)" }
    return [pscustomobject]$v
}

function Add-GateProbeRecord {
    param([string]$JobId, $Verdict, [double]$WaitedSec, [string]$Phase, [string]$WaitState = "")
    $now = (Get-Date).ToUniversalTime()
    try {
        $rec = [ordered]@{ utc = $now.ToString("o"); phase = $Phase; job = $JobId; state = $Verdict.state; wait = $WaitState; busy = $Verdict.busy; queuePos = $Verdict.queuePos; queueLen = $Verdict.queueLen; throttled = $Verdict.throttled; rows = $Verdict.window; meanC = $Verdict.meanC; max15C = $Verdict.max15C; watts = $Verdict.watts; waitedSec = [int]$WaitedSec; why = $Verdict.why }
        [System.IO.File]::AppendAllText((Join-Path $logs "heavy-slot-gate.jsonl"), ($rec | ConvertTo-Json -Compress) + [Environment]::NewLine, [System.Text.Encoding]::ASCII)
    }
    catch { }
    $key = $now.ToString("yyyy-MM-ddTHH")
    if ($script:GateHourKey -ne $key) {
        Write-GateHourSummary
        $script:GateHourKey = $key
        $script:GateHour = [ordered]@{ hour = "$key`Z"; probes = 0; clear = 0; hot = 0; busy = 0; unknown = 0; admitted = 0; requeued = 0; waitSecTotal = 0 }
    }
    $script:GateHour.probes++
    switch ($Verdict.state) {
        "clear" { $script:GateHour.clear++ }
        "hot" { $script:GateHour.hot++ }
        "busy" { $script:GateHour.busy++ }
        default { $script:GateHour.unknown++ }
    }
}

function Write-GateHourSummary {
    if ($null -eq $script:GateHour -or $script:GateHour.probes -eq 0) { return }
    $h = $script:GateHour
    $rate = [Math]::Round($h.clear / $h.probes, 3)
    Add-AgentLogLine "heavy-slot-gate hour=$($h.hour) probes=$($h.probes) clear=$($h.clear) hot=$($h.hot) busy=$($h.busy) unknown=$($h.unknown) passRate=$rate admitted=$($h.admitted) requeued=$($h.requeued) waitSecTotal=$($h.waitSecTotal)"
    try {
        $o = [ordered]@{}; foreach ($k in $h.Keys) { $o[$k] = $h[$k] }; $o.passRate = $rate; $o.gate = $script:GateVersion
        [System.IO.File]::AppendAllText((Join-Path $logs "heavy-slot-gate-hourly.jsonl"), ($o | ConvertTo-Json -Compress) + [Environment]::NewLine, [System.Text.Encoding]::ASCII)
    }
    catch { }
}

function Invoke-GateIdleSample {
    if ($GateIdleSampleSec -le 0) { return }
    $now = (Get-Date).ToUniversalTime()
    if (($now - $script:GateLastIdleSampleUtc).TotalSeconds -lt $GateIdleSampleSec) { return }
    $script:GateLastIdleSampleUtc = $now
    $v = Get-HeavySlotVerdict -Status (Invoke-HeavySlotStatus) -Feed (Read-HeavySlotFeedMetrics)
    Add-GateProbeRecord -JobId "" -Verdict $v -WaitedSec 0 -Phase "idle"
}

function New-HeavySlotSentinelCommand {
    # The command heavy-slot.py runs once it has acquired the slot (base64 for -EncodedCommand, so no path ever meets
    # a command-line quoting rule). It publishes HEAVY_SLOT_HELD and its pid to AcquiredFile (unique tmp + rename),
    # then holds the slot until ReleaseFile exists or this agent (pid AND start time) is gone. Its exit ends
    # heavy-slot.py's child, and heavy-slot.py frees the slot itself.
    param([Parameter(Mandatory = $true)][string]$AcquiredFile, [Parameter(Mandatory = $true)][string]$ReleaseFile)
    $acq = "'" + $AcquiredFile.Replace("'", "''") + "'"
    $rel = "'" + $ReleaseFile.Replace("'", "''") + "'"
    $agentStartMs = [DateTimeOffset]::new($script:AgentProcess.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()
    $text = @"
`$ErrorActionPreference = 'Stop'
`$acq = $acq
`$rel = $rel
`$tmp = `$acq + '.' + `$PID + '.tmp'
`$rec = [ordered]@{ held = [string]`$env:HEAVY_SLOT_HELD; sentinelPid = `$PID; acquiredUtc = [DateTime]::UtcNow.ToString('o') }
[System.IO.File]::WriteAllText(`$tmp, (`$rec | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new(`$false))
Move-Item -LiteralPath `$tmp -Destination `$acq -Force
while (-not [System.IO.File]::Exists(`$rel)) {
    try {
        `$a = Get-Process -Id $PID -ErrorAction Stop
        `$ms = [DateTimeOffset]::new(`$a.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()
        if ([Math]::Abs(`$ms - $agentStartMs) -gt 1000) { exit 3 }
    }
    catch { exit 3 }
    Start-Sleep -Milliseconds 500
}
exit 0
"@
    return [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($text))
}

function Start-HeavySlotHolder {
    # Joins heavy-slot.py's FIFO for this job (see the gate block). Returns the holder (ok=$true), or ok=$false with
    # why when it cannot start: the job then keeps waiting -- a gated job never runs without a held slot.
    param([Parameter(Mandatory = $true)][string]$JobId)
    $py = Resolve-HeavySlotPython
    if (-not $py) { return [pscustomobject]@{ ok = $false; why = "python not found ($HeavySlotPython)" } }
    if (-not (Test-Path -LiteralPath $HeavySlotScript -PathType Leaf)) { return [pscustomobject]@{ ok = $false; why = "heavy-slot.py not found ($HeavySlotScript)" } }
    $id = "$JobId.$([guid]::NewGuid().ToString('N').Substring(0, 12))"
    $acq = Join-Path $slotHolders "$id.acquired.json"
    $rel = Join-Path $slotHolders "$id.release"
    $label = "mlv $JobId"
    $argv = @($HeavySlotScript, "run", "--exclusive", "--label", $label, "--slots", "$HeavySlotCount", "--",
        $psExe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", (New-HeavySlotSentinelCommand -AcquiredFile $acq -ReleaseFile $rel))
    try {
        # HEAVY_SLOT_HELD is REMOVED: a holder must take its own slot, never pass through as nested.
        $launch = Start-WindowlessRedirectedProcess -FilePath $py -ArgumentList $argv `
            -OutFile (Join-Path $logs "$id.heavy-slot.out.txt") -ErrFile (Join-Path $logs "$id.heavy-slot.err.txt") `
            -Environment @{ HEAVY_SLOT_DIR = $HeavySlotDir; HEAVY_SLOT_FEED = $HeavySlotFeed; HEAVY_SLOT_HELD = $null }
    }
    catch {
        return [pscustomobject]@{ ok = $false; why = "holder launch failed: $($_.Exception.Message)" }
    }
    # The record goes down FIRST, from the Process object (no CIM query), so an agent killed right after the start still
    # leaves a record its successor can stop (Stop-StaleHeavySlotHolders matches pid + start time). A holder orphaned
    # anyway resolves itself: when its ticket reaches the head, its sentinel finds this agent gone and exits at once.
    $recordFile = Join-Path $slotHolders "$id.holder.json"
    $startMs = $null
    try { $startMs = [DateTimeOffset]::new($launch.Process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() } catch { }
    $rec = [ordered]@{ id = $id; jobId = $JobId; label = $label; pid = $launch.Process.Id; startEpochMs = $startMs; acquiredFile = $acq; releaseFile = $rel; agentPid = $PID; agentProcessStartUtc = $script:AgentProcessStartUtc }
    $w = Write-JsonFileAtomic -Path $recordFile -InputObject $rec
    $identity = Get-ProcessIdentity -ProcessId $launch.Process.Id
    $h = [pscustomobject]@{
        ok = $true; id = $id; jobId = $JobId; label = $label; launch = $launch; pid = $launch.Process.Id; identity = $identity
        acquiredFile = $acq; releaseFile = $rel; recordFile = $recordFile; why = $null
    }
    Add-AgentLogLine "heavy-slot-gate: holder started job=$JobId pid=$($h.pid) label='$label' (heavy-slot.py run --exclusive; its FIFO ticket) record=$($w.ok)"
    return $h
}

function Get-HeavySlotHolderState {
    # acquired (the sentinel published HEAVY_SLOT_HELD) | waiting (queued in heavy-slot.py) | exited (heavy-slot.py
    # is gone, or the acquired record has no HEAVY_SLOT_HELD: heavy-slot.py's NESTING contract is broken, fail closed).
    param([Parameter(Mandatory = $true)]$Holder)
    $p = $Holder.launch.Process
    if (Test-Path -LiteralPath $Holder.acquiredFile -PathType Leaf) {
        $a = Read-JsonFile -Path $Holder.acquiredFile
        if ($null -ne $a) {
            $held = [string]$a.held
            if ($held -notmatch '^(\d+):(\d+)$') { return [pscustomobject]@{ state = "exited"; why = "acquired without HEAVY_SLOT_HELD (got '$held'); heavy-slot.py NESTING contract not met" } }
            $slot = [int]$Matches[1]
            $ownerPid = [int]$Matches[2]
            if ($p.HasExited) { return [pscustomobject]@{ state = "exited"; why = "heavy-slot.py exited rc=$($p.ExitCode) after acquiring slot $slot" } }
            return [pscustomobject]@{ state = "acquired"; slot = $slot; ownerPid = $ownerPid; held = $held; why = $null }
        }
    }
    if ($p.HasExited) { return [pscustomobject]@{ state = "exited"; why = "heavy-slot.py exited rc=$($p.ExitCode) before acquiring" } }
    return [pscustomobject]@{ state = "waiting"; why = $null }
}

function Test-HeavySlotHolderHeld {
    # r2 (sol r1 BLOCKER): is the holder that acquired $Acquired (a Get-HeavySlotHolderState result) STILL holding it?
    # Called AFTER each status probe, so a holder that exits before, during or after the probe is never read as
    # holding. Returns @{ ok; why }: the holder process is alive, it still reports the same HEAVY_SLOT_HELD, and the
    # owner pid in it is a live process.
    param([Parameter(Mandatory = $true)]$Holder, [Parameter(Mandatory = $true)]$Acquired)
    $now = Get-HeavySlotHolderState -Holder $Holder
    if ($now.state -ne "acquired") { return [pscustomobject]@{ ok = $false; why = "holder pid=$($Holder.pid) no longer holds slot $($Acquired.slot): $($now.state) ($($now.why))" } }
    if ($now.held -ne $Acquired.held) { return [pscustomobject]@{ ok = $false; why = "holder pid=$($Holder.pid) HEAVY_SLOT_HELD changed from $($Acquired.held) to $($now.held)" } }
    if ($null -eq (Get-Process -Id $now.ownerPid -ErrorAction SilentlyContinue)) { return [pscustomobject]@{ ok = $false; why = "slot $($now.slot) owner pid $($now.ownerPid) (HEAVY_SLOT_HELD=$($now.held)) is not running" } }
    return [pscustomobject]@{ ok = $true; why = $null }
}

function Add-HeavySlotHolderLoss {
    # r2: a typed 'holder-lost' event for this job, logged, and kept for its result (admission.holderLost) across a
    # pre-launch refusal that leaves the job queued for the next poll.
    param([Parameter(Mandatory = $true)][string]$JobId, [Parameter(Mandatory = $true)][string]$Phase, [string]$Why, [string]$Held, [string]$Reason = "holder-lost")
    if (-not $script:HolderLossByJob.ContainsKey($JobId)) { $script:HolderLossByJob[$JobId] = [System.Collections.Generic.List[object]]::new() }
    $script:HolderLossByJob[$JobId].Add([pscustomobject]@{ reason = $Reason; phase = $Phase; utc = (Get-Date).ToUniversalTime().ToString("o"); held = $Held; why = $Why })
    Add-AgentLogLine "heavy-slot-gate: $Reason job=$JobId phase=$Phase held=$Held why=[$Why]; the job is NOT launched on that slot"
}

function Add-HeavySlotHolderLossToAdmission {
    # r2: copies this job's holder-lost events into its admission record (and so into its result).
    param([Parameter(Mandatory = $true)][string]$JobId, $Admission)
    if ($null -eq $Admission -or -not $script:HolderLossByJob.ContainsKey($JobId)) { return }
    $Admission | Add-Member -NotePropertyName holderLost -NotePropertyValue @($script:HolderLossByJob[$JobId].ToArray()) -Force
}

function Confirm-HeavySlotHeldForLaunch {
    # r2 (sol r1 BLOCKER, TOCTOU): the LAST check before the child is created. Admission was decided a moment ago;
    # this re-proves it now: the admitted holder still holds the same slot, `heavy-slot.py status` shows that slot
    # held by this job's label and owner pid, no other slot is held, and the holder is still holding after that
    # probe. Returns @{ ok; reason (holder-lost | busy); why }. Anything not proven is a refusal: no launch.
    param([Parameter(Mandatory = $true)][string]$JobId)
    $h = $script:HeavySlotHolder
    if ($null -eq $h) { return [pscustomobject]@{ ok = $false; reason = "holder-lost"; why = "no admitted holder" } }
    $hs = Get-HeavySlotHolderState -Holder $h
    if ($hs.state -ne "acquired" -or $hs.held -ne [string]$script:Admission.heavySlotHeld) {
        return [pscustomobject]@{ ok = $false; reason = "holder-lost"; why = "holder pid=$($h.pid) $($hs.state) ($($hs.why)); admitted with HEAVY_SLOT_HELD=$($script:Admission.heavySlotHeld)" }
    }
    $occ = Get-HeavySlotOccupancy -Status (Invoke-HeavySlotStatus -HeartbeatActivity "pre-launch job=$JobId") -OwnSlot $hs.slot -OwnPid $hs.ownerPid -Label $h.label
    $live = Test-HeavySlotHolderHeld -Holder $h -Acquired $hs
    if (-not $live.ok) { return [pscustomobject]@{ ok = $false; reason = "holder-lost"; why = $live.why } }
    if (-not $occ.ok) { return [pscustomobject]@{ ok = $false; reason = "holder-lost"; why = "slot $($hs.slot) ownership not confirmed: $($occ.why)" } }
    if ($occ.ownLost) { return [pscustomobject]@{ ok = $false; reason = "holder-lost"; why = $occ.why } }
    if ($occ.busy -gt 0) { return [pscustomobject]@{ ok = $false; reason = "busy"; why = "busy: $($occ.busy) other heavy run(s): $($occ.holders -join '; ')" } }
    return [pscustomobject]@{ ok = $true; reason = $null; why = "slot $($hs.slot) held by '$($h.label)' pid $($hs.ownerPid), busy 0" }
}

function Stop-HeavySlotHolder {
    # Ends a holder. Acquired: write the release file; the sentinel exits and heavy-slot.py frees the slot itself
    # (its own release()). Still queued, or not gone within 15 s: stop the holder's process tree, and heavy-slot.py
    # reclaims the dead waiter's ticket (queue_ahead) and a dead owner's slot (slots_state). This agent touches
    # neither the ticket nor the slot.
    param($Holder, [string]$Why)
    if ($null -eq $Holder -or -not $Holder.ok) { return }
    $p = $Holder.launch.Process
    $acquired = Test-Path -LiteralPath $Holder.acquiredFile -PathType Leaf
    if ($acquired) {
        try { [System.IO.File]::WriteAllText($Holder.releaseFile, "$Why`r`n", [System.Text.Encoding]::ASCII) } catch { }
    }
    $exited = $p.HasExited
    if (-not $exited -and $acquired) { $exited = $p.WaitForExit(15000) }
    $killed = @()
    if (-not $exited) {
        $killed = @(Stop-ProcessTree -RootProcessId $Holder.pid -RootStartUtc $Holder.identity.startUtc -RootStartEpochMs $Holder.identity.startEpochMs -RootImagePath $Holder.identity.imagePath)
        $exited = $p.WaitForExit(5000)
    }
    $rc = if ($exited) { $p.ExitCode } else { "still-running" }
    try { [void][System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]@($Holder.launch.OutCopy, $Holder.launch.ErrCopy), 5000) } catch { }
    try { $Holder.launch.OutStream.Dispose(); $Holder.launch.ErrStream.Dispose() } catch { }
    $p.Dispose()
    Remove-Item -LiteralPath $Holder.acquiredFile, $Holder.releaseFile, $Holder.recordFile -Force -ErrorAction SilentlyContinue
    Add-AgentLogLine "heavy-slot-gate: holder ended job=$($Holder.jobId) pid=$($Holder.pid) acquired=$acquired rc=$rc killed=$($killed -join ',') why=[$Why]"
}

function Stop-StaleHeavySlotHolders {
    # At agent start. A holder outlives a dead agent only while it is still QUEUED (an acquired sentinel exits once
    # the agent is gone). Each holder record carries its process identity: a live match is stopped, so heavy-slot.py
    # reclaims its ticket. Records and side files are then removed.
    foreach ($f in @(Get-ChildItem -LiteralPath $slotHolders -Filter "*.holder.json" -File -ErrorAction SilentlyContinue)) {
        $r = Read-JsonFile -Path $f.FullName
        if ($null -ne $r -and $r.pid -and $null -ne $r.startEpochMs -and (Get-ProcessIdentity -ProcessId ([int]$r.pid)).exists) {
            $killed = @(Stop-ProcessTree -RootProcessId ([int]$r.pid) -RootStartEpochMs $r.startEpochMs)
            Add-AgentLogLine "heavy-slot-gate: stale holder id=$($r.id) pid=$($r.pid) from agent pid=$($r.agentPid) stopped=$($killed -join ',')"
        }
        Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -LiteralPath $slotHolders -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '\.(acquired\.json|release|tmp)$' } | Remove-Item -Force -ErrorAction SilentlyContinue
}

function Exit-HeavySlot {
    # Ends the admitted job's holder, so heavy-slot.py releases the job's slot. Idempotent.
    $h = $script:HeavySlotHolder
    $script:HeavySlotHolder = $null
    if ($null -ne $h) { Stop-HeavySlotHolder -Holder $h -Why "job done" }
}

function Get-RecoveredAdmission {
    # A result published by a RESTARTED agent (adoption or orphan recovery): the dead agent's holder exited with it
    # and heavy-slot.py freed the slot while the child may have kept running. Never report that run as clean: keep
    # the original record and downgrade the label.
    param($Admission)
    if ($null -eq $Admission) { return $null }
    $o = [ordered]@{}
    foreach ($p in $Admission.PSObject.Properties) { $o[$p.Name] = $p.Value }
    $o.originalLabel = $Admission.label
    if ($Admission.label -eq "clear") { $o.label = "loaded-comparable" }
    $o.slotContinuity = "lost"
    $o.why = "agent restarted mid-job; its heavy slot was released while the agent was down; original: $($Admission.why)"
    return [pscustomobject]$o
}

function Invoke-ExemptJobsDuringWait {
    # Runs the light (GateExemptJobPattern) jobs waiting in inbox\ while another job sits in its gate-wait. Recovery is
    # left to the inbox loop (never Resolve-JobRecovery here). Returns the number of jobs run.
    param([Parameter(Mandatory = $true)][string]$WaitingJobId, [double]$WaitedSec)
    $ran = 0
    $exemptJobs = Get-ChildItem $inbox -Filter *.job.ps1 -File -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($ej in $exemptJobs) {
        $eid = $ej.BaseName -replace '\.job$', ''
        if ($eid -eq $WaitingJobId) { continue }
        if ($eid -notmatch $GateExemptJobPattern) { continue }
        if ($script:RecoveryQuarantine.ContainsKey($eid)) { continue }
        if (Get-JobRecoveryPending -JobId $eid) { continue }
        Add-AgentLogLine "heavy-slot-gate: maint-dispatch exempt job=$eid during gate-wait of job=$WaitingJobId waited=$([int]$WaitedSec)s"
        $script:ExemptDuringWaitOf = $WaitingJobId
        try {
            $null = Invoke-QueuedJob -Job $ej
        }
        finally {
            # No Exit-HeavySlot: $script:HeavySlotHolder is always $null inside the wait (the waiting job's holder is the local $holder).
            $script:ExemptDuringWaitOf = $null
            $script:Admission = $null
            Remove-Item Env:\MLV_AGENT_ADMISSION, Env:\MLV_AGENT_ADMISSION_THERMAL, Env:\MLV_AGENT_ADMISSION_WAIT_SEC, Env:\HEAVY_SLOT_HELD -ErrorAction SilentlyContinue
        }
        $ran++
    }
    return $ran
}

function Wait-HeavySlotAdmission {
    # Blocks until the job may start and returns the admission record. Admitted (label clear): $script:HeavySlotHolder
    # holds this job's exclusive slot until Exit-HeavySlot. Not admitted (cancelled-in-gate | queue-expired): no holder
    # is left behind. While waiting the job holds NO slot, only heavy-slot.py's FIFO ticket. No wait cap.
    param([Parameter(Mandatory = $true)][string]$JobId, [Parameter(Mandatory = $true)]$Job, [Parameter(Mandatory = $true)]$Metadata)
    $t0 = Get-Date
    $label = "mlv $JobId"
    $probes = 0
    $requeues = 0
    $holderStarts = 0
    $holder = $null
    $nextHolderAt = $t0
    $nextProbeAt = $t0
    $nextExemptScanAt = $t0
    $lastLog = [DateTime]::MinValue
    $state = "queued"
    $why = "joining heavy-slot.py's FIFO"
    $maxAge = [int]$Metadata.maxQueueAgeSec
    try {
        while ($true) {
            $now = Get-Date
            $waited = ($now - $t0).TotalSeconds
            if (Test-Path -LiteralPath (Join-Path $cancel "$JobId.cancel") -PathType Leaf) {
                return [pscustomobject]@{ gate = $script:GateVersion; label = "cancelled-in-gate"; state = "cancelled"; waitedSec = [int]$waited; probes = $probes; requeues = $requeues; slot = $null; thermal = $null; why = "cancel file present before launch" }
            }
            $age = Get-QueueAgeSeconds -Job $Job -Metadata $Metadata
            if ($maxAge -gt 0 -and $age -gt $maxAge) {
                return [pscustomobject]@{ gate = $script:GateVersion; label = "queue-expired"; state = "expired"; waitedSec = [int]$waited; probes = $probes; requeues = $requeues; slot = $null; thermal = $null; why = "queue age ${age}s > ${maxAge}s while waiting"; queueAgeSec = $age }
            }
            if ($null -eq $holder -and $now -ge $nextHolderAt) {
                $h = Start-HeavySlotHolder -JobId $JobId
                if ($h.ok) { $holder = $h; $holderStarts++; $state = "queued"; $why = "ticket filed by heavy-slot.py run --exclusive" }
                else { $state = "holder-unavailable"; $why = $h.why; $nextHolderAt = $now.AddSeconds($GateProbeSec) }
            }
            $hs = if ($null -ne $holder) { Get-HeavySlotHolderState -Holder $holder } else { $null }
            if ($null -ne $hs -and $hs.state -eq "exited") {
                $state = "holder-exited"
                $why = $hs.why
                Stop-HeavySlotHolder -Holder $holder -Why $hs.why
                $holder = $null
                $nextHolderAt = $now.AddSeconds($GateProbeSec)
            }
            elseif ($null -ne $hs -and $hs.state -eq "acquired") {
                # heavy-slot.py served this job's ticket and holds an EXCLUSIVE slot for it. Re-check with that slot excluded.
                # r2 (sol r1 BLOCKER): the slot is excluded only when status shows it held by THIS holder (label and
                # owner pid), and the holder must still hold it AFTER the probe; otherwise holder-lost, never clear.
                $status = Invoke-HeavySlotStatus -HeartbeatActivity "gate-wait job=$JobId state=acquired waited=$([int]$waited)s"
                $v = Get-HeavySlotVerdict -Status $status -Feed (Read-HeavySlotFeedMetrics) -OwnSlot $hs.slot -OwnPid $hs.ownerPid -Label $label
                $live = Test-HeavySlotHolderHeld -Holder $holder -Acquired $hs
                if (-not $live.ok) { $v.state = "holder-lost"; $v.why = $live.why }
                if ($v.state -eq "holder-lost") { Add-HeavySlotHolderLoss -JobId $JobId -Phase "acquired" -Why $v.why -Held $hs.held }
                $probes++
                Add-GateProbeRecord -JobId $JobId -Verdict $v -WaitedSec $waited -Phase "acquired" -WaitState "acquired"
                if ($v.state -eq "clear") {
                    $script:HeavySlotHolder = $holder
                    $holder = $null     # owned by $script:HeavySlotHolder now: the finally below must not end it
                    $slotDir = Join-Path $HeavySlotDir "$($hs.slot)"
                    $script:GateHour.admitted++
                    $script:GateHour.waitSecTotal += [int]$waited
                    $adm = [pscustomobject]@{
                        gate = $script:GateVersion; label = "clear"; state = "clear"; waitedSec = [int]((Get-Date) - $t0).TotalSeconds
                        probes = $probes; requeues = $requeues; holderStarts = $holderStarts; slot = $slotDir; heavySlotHeld = $hs.held
                        heavySlotLabel = $label; holderPid = $script:HeavySlotHolder.pid; exclusive = $true; busy = $v.busy
                        thermal = $v.thermal; throttled = $v.throttled; rows = $v.window; meanC = $v.meanC; max15C = $v.max15C; why = $v.why
                    }
                    Add-AgentLogLine "heavy-slot-gate: admit job=$JobId label=clear slot=$slotDir held=$($hs.held) holderPid=$($adm.holderPid) exclusive=1 busy=0 waited=$($adm.waitedSec)s requeues=$requeues probes=$probes thermal=[$($v.thermal)]"
                    return $adm
                }
                # Not clear with the slot in hand: give it back and re-join the FIFO. A job that is not running never keeps a slot.
                $requeues++
                $script:GateHour.requeued++
                $state = "requeue-$($v.state)"
                $why = "acquired slot $($hs.slot), but $($v.state): $($v.why); released, re-joining the FIFO"
                Stop-HeavySlotHolder -Holder $holder -Why $why
                $holder = $null
                $nextHolderAt = $now.AddSeconds($GateProbeSec)
            }
            elseif ($now -ge $nextProbeAt) {
                # Queued in heavy-slot.py (or between holders): a probe for the log only. The decision is heavy-slot.py's
                # FIFO plus the re-check above.
                $v = Get-HeavySlotVerdict -Status (Invoke-HeavySlotStatus -HeartbeatActivity "gate-wait job=$JobId state=$state waited=$([int]$waited)s") -Feed (Read-HeavySlotFeedMetrics) -Label $label
                $probes++
                $nextProbeAt = $now.AddSeconds($GateProbeSec)
                if ($null -ne $holder) { $state = "queued"; $why = "heavy-slot.py FIFO position $($v.queuePos) of $($v.queueLen); $($v.why)" }
                Add-GateProbeRecord -JobId $JobId -Verdict $v -WaitedSec $waited -Phase "pre-job" -WaitState $state
            }
            # maint-dispatch r1: light jobs are not held behind this wait. Capture with $null = : any output here would join the admission record this function returns.
            if ((Get-Date) -ge $nextExemptScanAt) {
                $null = Invoke-ExemptJobsDuringWait -WaitingJobId $JobId -WaitedSec ((Get-Date) - $t0).TotalSeconds
                $nextExemptScanAt = (Get-Date).AddSeconds($GateProbeSec)
            }
            if (((Get-Date) - $lastLog).TotalSeconds -ge 60) {
                Add-AgentLogLine "heavy-slot-gate: waiting job=$JobId state=$state why=[$why] waited=$([int]$waited)s requeues=$requeues holderPid=$(if ($null -ne $holder) { $holder.pid } else { 'none' })"
                $lastLog = Get-Date
            }
            Write-AgentHeartbeat -Activity "gate-wait job=$JobId state=$state waited=$([int]((Get-Date) - $t0).TotalSeconds)s"
            Start-Sleep -Milliseconds 1000
        }
    }
    finally {
        if ($null -ne $holder) { Stop-HeavySlotHolder -Holder $holder -Why "job left the gate unadmitted" }
    }
}

function Publish-GateUnlaunchedResult {
    # A job the gate decided never to launch (cancelled while waiting). Same result shape as a cancelled
    # job, with no child fields: nothing ran.
    param([Parameter(Mandatory = $true)]$Job, [Parameter(Mandatory = $true)]$Metadata, [Parameter(Mandatory = $true)]$Admission, [int]$QueueAgeSec)
    $jobId = $Job.BaseName -replace '\.job$', ''
    $result = [pscustomobject]@{
        jobId            = $jobId
        exitCode         = 130
        startedUtc       = $null
        endedUtc         = (Get-Date).ToUniversalTime().ToString("o")
        host             = $env:COMPUTERNAME
        timeoutSec       = [int]$Metadata.timeoutSec
        timedOut         = $false
        cancelled        = $true
        queueExpired     = $false
        queueAgeSec      = $QueueAgeSec
        maxQueueAgeSec   = [int]$Metadata.maxQueueAgeSec
        orphaned         = $false
        adopted          = $false
        killedProcessIds = @()
        admission        = $Admission
        stdout           = ""
        stderr           = "cancelled by mailbox request while waiting for heavy-slot admission; job was not executed"
    }
    $resultPath = Publish-JobResult -JobId $jobId -Result $result
    Move-JobArtifacts -JobId $jobId -JobPath $Job.FullName -DestinationDirectory $processed
    Add-AgentLogLine "heavy-slot-gate: cancelled before launch job=$jobId result=$resultPath"
}

function Invoke-QueuedJob {
    param([Parameter(Mandatory = $true)]$Job)

    $jobId = $Job.BaseName -replace '\.job$', ''
    $metadata = Get-JobMetadata -JobId $jobId
    $effectiveTimeoutSec = [int]$metadata.timeoutSec
    $queueAgeSec = Get-QueueAgeSeconds -Job $Job -Metadata $metadata
    if ([int]$metadata.maxQueueAgeSec -gt 0 -and $queueAgeSec -gt [int]$metadata.maxQueueAgeSec) {
        Invoke-QueueExpiredJob -Job $Job -Metadata $metadata -QueueAgeSec $queueAgeSec
        Complete-AdmissionWaitMarker -JobId $jobId -Status "queue-expired"   # a claim re-queued by a restart
        return
    }

    # Heavy-slot admission: wait (heartbeating) for a slot and a clear thermal feed; see the gate block.
    # r1g: a LIGHT job class is not held (it takes no slot): the venue's own quiet/load probe and agent maintenance.
    if ($jobId -match $GateExemptJobPattern) {
        $script:Admission = [pscustomobject]@{ gate = $script:GateVersion; label = "exempt-light"; state = "exempt"; waitedSec = 0; probes = 0; requeues = 0; slot = $null; thermal = $null; why = "job id matches GateExemptJobPattern ($GateExemptJobPattern): light job, not gated, holds no slot" }
        if ($null -ne $script:ExemptDuringWaitOf) { $script:Admission | Add-Member -NotePropertyName waitingFor -NotePropertyValue $script:ExemptDuringWaitOf -Force }
        Add-AgentLogLine "heavy-slot-gate: exempt job=$jobId (light class, no slot)"
    }
    else {
        # GATE-2: claim the job (phase=admission-wait, no child) BEFORE the gate holds it.
        [void](New-AdmissionWaitMarker -JobId $jobId -Job $Job -Metadata $metadata)
        $script:Admission = Wait-HeavySlotAdmission -JobId $jobId -Job $Job -Metadata $metadata
        Add-HeavySlotHolderLossToAdmission -JobId $jobId -Admission $script:Admission
    }
    if ($script:Admission.label -eq "queue-expired") {
        Invoke-QueueExpiredJob -Job $Job -Metadata $metadata -QueueAgeSec ([int]$script:Admission.queueAgeSec)
        Complete-AdmissionWaitMarker -JobId $jobId -Status "queue-expired"
        return
    }
    $queueAgeSec = Get-QueueAgeSeconds -Job $Job -Metadata $metadata
    if ($script:Admission.label -eq "cancelled-in-gate") {
        Publish-GateUnlaunchedResult -Job $Job -Metadata $metadata -Admission $script:Admission -QueueAgeSec $queueAgeSec
        Complete-AdmissionWaitMarker -JobId $jobId -Status "cancelled-in-gate"
        return
    }
    if (Test-Path -LiteralPath (Join-Path $cancel "$jobId.cancel") -PathType Leaf) {
        Exit-HeavySlot
        $script:Admission | Add-Member -NotePropertyName label -NotePropertyValue "cancelled-in-gate" -Force
        $script:Admission | Add-Member -NotePropertyName why -NotePropertyValue "cancel file present after admission, before launch" -Force
        $script:Admission | Add-Member -NotePropertyName slot -NotePropertyValue $null -Force
        Publish-GateUnlaunchedResult -Job $Job -Metadata $metadata -Admission $script:Admission -QueueAgeSec $queueAgeSec
        Complete-AdmissionWaitMarker -JobId $jobId -Status "cancelled-in-gate"
        return
    }
    if ([int]$metadata.maxQueueAgeSec -gt 0 -and $queueAgeSec -gt [int]$metadata.maxQueueAgeSec) {
        Exit-HeavySlot
        Invoke-QueueExpiredJob -Job $Job -Metadata $metadata -QueueAgeSec $queueAgeSec
        Complete-AdmissionWaitMarker -JobId $jobId -Status "queue-expired"
        return
    }
    $env:MLV_AGENT_ADMISSION = $script:Admission.label
    $env:MLV_AGENT_ADMISSION_THERMAL = [string]$script:Admission.thermal
    $env:MLV_AGENT_ADMISSION_WAIT_SEC = [string]$script:Admission.waitedSec
    # r1c: the job runs inside its holder's slot. heavy-slot.py's NESTING contract: a heavy-slot.py run inside the job
    # sees HEAVY_SLOT_HELD="<slot>:<owner pid>" and passes straight through instead of queueing behind this job.
    if ($script:Admission.heavySlotHeld) { $env:HEAVY_SLOT_HELD = [string]$script:Admission.heavySlotHeld }
    $holderLossLogged = $false

    $outFile = Join-Path $logs "$jobId.out.txt"
    $errFile = Join-Path $logs "$jobId.err.txt"
    $started = (Get-Date).ToUniversalTime().ToString("o")
    $exit = $null
    $timedOut = $false
    $cancelled = $false
    $killedProcessIds = @()
    $jobProcess = $null
    $launch = $null
    $trackedDescendants = @{}
    $keepAwakeBeginUtc = $null
    $keepAwakeEndUtc = $null
    $keepAwakeBeginOk = $false
    $keepAwakeEndOk = $false
    Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue

    # r2b (sol blockers 1 and 2): the launch handoff. The launching marker is the last write before the child
    # exists and MUST verify, or nothing launches. THREE OUTCOMES of that write:
    #   done        - verified: launch. A restart from here on never re-queues unless it proves nothing launched.
    #   not done /
    #   cannot tell - not verified (the disk holds the admission-wait claim, or this launching marker with its
    #                 readback failing): DO NOT LAUNCH. The job is LEFT QUEUED in inbox\ for the next poll, which
    #                 gates and claims it again; nothing is published. A restart meanwhile finds admission-wait, or
    #                 launching with no child and no out file, and re-queues: correct, because nothing launched.
    $markerPath = Join-Path $running "$jobId.started.json"
    $marker = New-LaunchingMarker -JobId $jobId -Job $Job -Metadata $metadata -OutFile $outFile -ErrFile $errFile -StartedUtc $started -TimeoutSec $effectiveTimeoutSec
    if ($null -eq $marker) {
        Add-AgentLogLine "launch refused job=${jobId}: the launching marker did not verify, so the job was NOT launched; left queued in inbox\ for the next poll, no result published"
        Write-AgentHeartbeat -Activity "launch-refused job=$jobId marker-unverified"
        return
    }
    # r2 (sol r1 BLOCKER, TOCTOU): re-prove the admitted slot is still this job's, immediately before the child is
    # created. On failure NOTHING launches: the holder is ended, the refusal is typed and kept for the job's result
    # (admission.holderLost), and the job is LEFT QUEUED in inbox\ exactly as a marker-unverified refusal is (its
    # launching marker is this agent's own, childPid null, out file absent), so the next poll gates it again.
    # A holder lost after this point is caught mid-job (admission.slotContinuity=lost).
    if ($script:Admission.label -eq "clear") {
        $pre = Confirm-HeavySlotHeldForLaunch -JobId $jobId
        if (-not $pre.ok) {
            $held = [string]$script:Admission.heavySlotHeld
            Exit-HeavySlot
            Add-HeavySlotHolderLoss -JobId $jobId -Phase "pre-launch" -Why $pre.why -Held $held -Reason $pre.reason
            Add-AgentLogLine "launch refused job=${jobId} reason=$($pre.reason): NOT launched; left queued in inbox\ for the next poll, no result published"
            Write-AgentHeartbeat -Activity "launch-refused job=$jobId $($pre.reason)"
            return
        }
    }
    $launchedPersisted = $false
    try {
        $keepAwakeBeginUtc = (Get-Date).ToUniversalTime().ToString("o")
        $keepAwakeBeginOk = Set-AgentKeepAwake -Enable $true
        Add-AgentLogLine "keep-awake begin job=$jobId ok=$keepAwakeBeginOk"

        $launch = Start-WindowlessRedirectedProcess -FilePath $psExe `
            -ArgumentList @("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $Job.FullName) `
            -OutFile $outFile -ErrFile $errFile -WorkingDirectory $Root
        $jobProcess = $launch.Process
        # phase=launched is published ONCE, in the same write as the child identity. THREE OUTCOMES:
        #   done        - verified: clients start their claimed clock; a restart adopts or orphans by child identity.
        #   not done /
        #   cannot tell - the disk still says launching: the job keeps running (it HAS launched), the write is
        #                 retried every monitor slice, and a restart meanwhile adopts the live child found by its
        #                 command line, or orphans -- never re-queues (the out file exists from here on).
        Set-LaunchedMarkerFields -Marker $marker -Identity (Get-ProcessIdentity -ProcessId $jobProcess.Id)
        $marker | Add-Member -NotePropertyName childPid -NotePropertyValue $jobProcess.Id -Force
        $launchedPersisted = Write-StartedMarkerVerified -Path $markerPath -Marker $marker
        if (-not $launchedPersisted) { Add-AgentLogLine "launched marker job=$jobId pid=$($jobProcess.Id) not verified; the child runs, the marker still reads launching; retrying each slice" }

        $deadline = (Get-Date).AddSeconds($effectiveTimeoutSec)
        $waitSliceMs = [Math]::Max(250, [Math]::Min(5000, $PollSeconds * 1000))
        while (!$jobProcess.HasExited -and (Get-Date) -lt $deadline) {
            if (-not $launchedPersisted) {
                $launchedPersisted = Write-StartedMarkerVerified -Path $markerPath -Marker $marker
                if ($launchedPersisted) { Add-AgentLogLine "launched marker job=$jobId pid=$($jobProcess.Id) verified on retry" }
            }
            foreach ($descendant in @(Get-DescendantProcessIdentities -ParentProcessId $jobProcess.Id)) {
                if ($descendant.exists) {
                    $trackedDescendants["$($descendant.processId):$($descendant.startEpochMs)"] = $descendant
                }
            }
            if (Test-Path -LiteralPath (Join-Path $cancel "$jobId.cancel") -PathType Leaf) {
                $cancelled = $true
                break
            }
            # r1c: a holder that dies mid-job has lost the slot (heavy-slot.py frees it); say so in the result.
            if (-not $holderLossLogged -and $null -ne $script:HeavySlotHolder -and $script:HeavySlotHolder.launch.Process.HasExited) {
                $holderLossLogged = $true
                $script:Admission | Add-Member -NotePropertyName slotContinuity -NotePropertyValue "lost" -Force
                Add-AgentLogLine "heavy-slot-gate: holder pid=$($script:HeavySlotHolder.pid) for job=$jobId exited mid-job; its slot is free, the job keeps running (admission.slotContinuity=lost)"
            }
            Write-AgentHeartbeat -Activity "job=$jobId"
            $remainingMs = [Math]::Max(1, [int][Math]::Min($waitSliceMs, ($deadline - (Get-Date)).TotalMilliseconds))
            [void]$jobProcess.WaitForExit($remainingMs)
        }

        if ($cancelled) {
            $exit = 130
            $killedProcessIds = @(Stop-ProcessTree -RootProcessId $jobProcess.Id -RootStartUtc $marker.childStartUtc -RootStartEpochMs $marker.childStartEpochMs -RootImagePath $marker.childImagePath)
            "cancelled by mailbox request; killed process tree pids=$($killedProcessIds -join ',')" |
                Add-Content -Encoding ASCII $errFile
        }
        elseif ($jobProcess.HasExited) {
            $exit = $jobProcess.ExitCode
            foreach ($descendant in @(Get-DescendantProcessIdentities -ParentProcessId $jobProcess.Id)) {
                if ($descendant.exists) {
                    $trackedDescendants["$($descendant.processId):$($descendant.startEpochMs)"] = $descendant
                }
            }
            $killedProcessIds = @(Stop-VerifiedProcessIdentities -Identities @($trackedDescendants.Values))
        }
        else {
            $timedOut = $true
            $exit = 124
            $killedProcessIds = @(Stop-ProcessTree -RootProcessId $jobProcess.Id -RootStartUtc $marker.childStartUtc -RootStartEpochMs $marker.childStartEpochMs -RootImagePath $marker.childImagePath)
            "timed out after ${effectiveTimeoutSec}s; killed process tree pids=$($killedProcessIds -join ',')" |
                Add-Content -Encoding ASCII $errFile
        }
    }
    catch {
        $exit = 1
        "agent failed to launch or monitor job: $($_.Exception.Message)" |
            Add-Content -Encoding ASCII $errFile
    }
    finally {
        if ($null -ne $launch) {
            Complete-WindowlessRedirectedProcess -Launch $launch
        }
        $keepAwakeEndUtc = (Get-Date).ToUniversalTime().ToString("o")
        $keepAwakeEndOk = Set-AgentKeepAwake -Enable $false
        Add-AgentLogLine "keep-awake end job=$jobId ok=$keepAwakeEndOk"
        if ($null -ne $jobProcess) {
            $jobProcess.Dispose()
        }
    }

    $ended = (Get-Date).ToUniversalTime().ToString("o")
    $stdout = if (Test-Path $outFile) { Get-Content $outFile -Raw } else { "" }
    $stderr = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { "" }
    # r2b: the child fields come from this agent's own in-memory marker, which holds them whether or not the
    # launched write ever verified (a launch that threw leaves childPid null, as before).

    $result = [pscustomobject]@{
        jobId             = $jobId
        exitCode          = $exit
        startedUtc        = $started
        endedUtc          = $ended
        host              = $env:COMPUTERNAME
        timeoutSec        = $effectiveTimeoutSec
        timedOut          = $timedOut
        cancelled         = $cancelled
        queueExpired      = $false
        queueAgeSec       = $queueAgeSec
        maxQueueAgeSec    = [int]$metadata.maxQueueAgeSec
        orphaned          = $false
        adopted           = $false
        childPid          = if ($null -ne $marker) { $marker.childPid } else { $null }
        childStartUtc     = if ($null -ne $marker) { $marker.childStartUtc } else { $null }
        childStartEpochMs = if ($null -ne $marker) { $marker.childStartEpochMs } else { $null }
        childImagePath    = if ($null -ne $marker) { $marker.childImagePath } else { $null }
        keepAwakeBeginUtc = $keepAwakeBeginUtc
        keepAwakeBeginOk  = $keepAwakeBeginOk
        keepAwakeEndUtc   = $keepAwakeEndUtc
        keepAwakeEndOk    = $keepAwakeEndOk
        heartbeatGenerationAtStart = if ($null -ne $marker) { $marker.heartbeatGeneration } else { $null }
        killedProcessIds  = @($killedProcessIds)
        admission         = $script:Admission
        stdout            = $stdout
        stderr            = $stderr
    }
    $resultPath = Publish-JobResult -JobId $jobId -Result $result
    Complete-StartedMarker -MarkerPath $markerPath -Status "completed" -ResultPath $resultPath
    Move-JobArtifacts -JobId $jobId -JobPath $Job.FullName -DestinationDirectory $processed
}

$agentMutex = [System.Threading.Mutex]::new($false, $MutexName)
$agentMutexHeld = $false
try {
    $agentMutexHeld = $agentMutex.WaitOne(0)
    if (-not $agentMutexHeld) {
        Add-AgentLogLine "agent overlap suppressed mutex=$MutexName pid=$PID"
        exit 0
    }
    Add-AgentLogLine "agent start pid=$PID host=$env:COMPUTERNAME root=$Root shell=$psExe processStartUtc=$script:AgentProcessStartUtc imagePath=$script:AgentImagePath gate=$script:GateVersion slots=$HeavySlotCount slotDir=$HeavySlotDir feed=$HeavySlotFeed heat=[blocked when > $GateMaxThrottledRows/$GateSustainedRows throttled or mean >= $GateMeanMaxC C] waitCap=none"
    Stop-StaleHeavySlotHolders
    Resolve-StartedMarkers

    while ($true) {
        # Liveness heartbeat (VM checks this file's age before submitting).
        Write-AgentHeartbeat
        # r2d: retry every quarantined job's recovery (a reader that denied its promotion may be gone now).
        Invoke-QuarantineReconcile

        $jobs = Get-ChildItem $inbox -Filter *.job.ps1 -File -ErrorAction SilentlyContinue | Sort-Object Name
        if (@($jobs).Count -eq 0) { Invoke-GateIdleSample }
        foreach ($job in $jobs) {
            # r2d (sol r2c BLOCKER): a job with ANY unresolved marker artifact is never admitted or launched here.
            $qid = $job.BaseName -replace '\.job$', ''
            if (-not (Test-Path -LiteralPath $job.FullName -PathType Leaf)) { continue }   # run inline during an earlier job's gate-wait (Invoke-ExemptJobsDuringWait); this listing is stale
            if ($script:RecoveryQuarantine.ContainsKey($qid)) { continue }
            if (Get-JobRecoveryPending -JobId $qid) {
                $null = Resolve-JobRecovery -JobId $qid
                continue   # the next poll re-lists inbox\: reconciliation may have adopted, orphaned or re-queued it
            }
            $script:Admission = $null
            try {
                Invoke-QueuedJob -Job $job
            }
            finally {
                Exit-HeavySlot
                Remove-Item Env:\MLV_AGENT_ADMISSION, Env:\MLV_AGENT_ADMISSION_THERMAL, Env:\MLV_AGENT_ADMISSION_WAIT_SEC, Env:\HEAVY_SLOT_HELD -ErrorAction SilentlyContinue
                # r2: a job that left inbox\ is finished; one refused before launch keeps its holder-lost record.
                if (-not (Test-Path -LiteralPath $job.FullName)) { $script:HolderLossByJob.Remove($qid) }
            }
        }

        if ($RunOnce) {
            break
        }
        Start-Sleep -Seconds $PollSeconds
    }
}
finally {
    if ($agentMutexHeld) { $agentMutex.ReleaseMutex() }
    $agentMutex.Dispose()
}
