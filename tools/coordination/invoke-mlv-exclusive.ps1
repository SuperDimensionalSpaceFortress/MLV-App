param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$Owner,
    [Parameter(Mandatory = $true)][string]$FilePath,
    [string[]]$ArgumentList = @(),
    [string]$ArgumentListBase64 = "",
    [string]$WorkingDirectory = "",
    [int]$TimeoutSeconds = 0
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $RepoRoot).Path
$working = if ($WorkingDirectory) { (Resolve-Path -LiteralPath $WorkingDirectory).Path } else { $root }
if ($ArgumentListBase64) {
    try {
        $argumentJson = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ArgumentListBase64))
        $ArgumentList = @($argumentJson | ConvertFrom-Json)
    }
    catch { throw "ArgumentListBase64 must encode a JSON array of strings: $($_.Exception.Message)" }
    if (@($ArgumentList | Where-Object { $_ -isnot [string] }).Count -gt 0) {
        throw "ArgumentListBase64 must encode only strings."
    }
}
$coordination = Join-Path $root '.claude-state\coordination\build-ownership.jsonl'
$lockDir = Join-Path $root '.claude-state\coordination\locks'
$lockPath = Join-Path $lockDir 'mlvapp-exclusive.lock'
New-Item -ItemType Directory -Force -Path $lockDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $coordination) | Out-Null

$record = [ordered]@{
    owner = $Owner
    startTimeUtc = (Get-Date).ToUniversalTime().ToString('o')
    pid = $null
    pidTree = @()
    command = @($FilePath) + @($ArgumentList)
    outcome = 'not-started'
    exitCode = $null
    cleanupState = 'not-started'
}
$stream = $null
$process = $null
$stdoutTask = $null
$stderrTask = $null
$outputFlushed = $false
function Write-CapturedOutput($OutTask, $ErrorTask) {
    $stdout = if ($OutTask) { $OutTask.GetAwaiter().GetResult() } else { "" }
    $stderr = if ($ErrorTask) { $ErrorTask.GetAwaiter().GetResult() } else { "" }
    if ($stdout) { [Console]::Out.Write($stdout) }
    if ($stderr) { [Console]::Error.Write($stderr) }
}
function Get-Snapshot {
    @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CreationDate, CommandLine)
}
function Get-Descendants([int]$ParentId, [datetime]$ParentCreation, [object[]]$Snapshot) {
    $children = @($Snapshot | Where-Object {
        if ([int]$_.ParentProcessId -ne $ParentId) { return $false }
        try { return ([datetime]$_.CreationDate -ge $ParentCreation) } catch { return $false }
    })
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($child in $children) {
        $result.Add($child)
        foreach ($nested in (Get-Descendants -ParentId ([int]$child.ProcessId) -ParentCreation ([datetime]$child.CreationDate) -Snapshot $Snapshot)) { $result.Add($nested) }
    }
    return $result.ToArray()
}
function Stop-OwnedTree([int]$RootPid) {
    # Exactly one cached process snapshot is used for this cleanup pass.
    $snapshot = Get-Snapshot
    $rootProcess = $snapshot | Where-Object { [int]$_.ProcessId -eq $RootPid } | Select-Object -First 1
    if (-not $rootProcess) { return @() }
    $descendants = @(Get-Descendants -ParentId $RootPid -ParentCreation ([datetime]$rootProcess.CreationDate) -Snapshot $snapshot | Sort-Object @{Expression={($_.CommandLine -split '\s+').Count};Descending=$true})
    foreach ($child in $descendants) {
        try { Stop-Process -Id ([int]$child.ProcessId) -Force -ErrorAction Stop } catch { }
    }
    return @($descendants | ForEach-Object { [int]$_.ProcessId })
}
try {
    try {
        $stream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
        throw "MLV-App exclusive runner is busy: $lockPath"
    }
    $lockText = ([System.Text.Encoding]::UTF8.GetBytes(($record | ConvertTo-Json -Compress)))
    $stream.SetLength(0); $stream.Write($lockText, 0, $lockText.Length); $stream.Flush()
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $working
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    # ProcessStartInfo does not reliably populate the child environment on every
    # PowerShell/.NET combination used by the Windows lanes.  In particular, a
    # fresh MinGW build could start mingw32-make by absolute path but windres
    # could not resolve its nested gcc preprocessor because PATH disappeared at
    # this process boundary.  Copy the current environment explicitly before
    # adding arguments so nested compiler/runtime tools see the same bounded
    # build environment as the exclusive runner itself.
    foreach ($entry in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $startInfo.Environment[[string]$entry.Key] = [string]$entry.Value
    }
    foreach ($argument in $ArgumentList) { [void]$startInfo.ArgumentList.Add($argument) }
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $process) { throw "Failed to start MLV-App exclusive command: $FilePath" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $record.pid = $process.Id
    $record.outcome = 'running'
    # Cache one ownership snapshot immediately after spawn; never recursively poll CIM.
    $spawnSnapshot = Get-Snapshot
    $spawnRoot = $spawnSnapshot | Where-Object { [int]$_.ProcessId -eq $process.Id } | Select-Object -First 1
    $spawnCreation = if ($spawnRoot) { [datetime]$spawnRoot.CreationDate } else { $process.StartTime }
    $record.pidTree = @($process.Id) + @(Get-Descendants -ParentId $process.Id -ParentCreation $spawnCreation -Snapshot $spawnSnapshot | ForEach-Object { [int]$_.ProcessId })
    if ($TimeoutSeconds -gt 0 -and -not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $record.outcome = 'timeout'
        $record.cleanupState = 'terminating-owned-descendants'
        [void](Stop-OwnedTree -RootPid $process.Id)
        try { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } catch { }
        $process.WaitForExit()
        Write-CapturedOutput -OutTask $stdoutTask -ErrorTask $stderrTask
        $outputFlushed = $true
        throw "MLV-App exclusive command timed out after ${TimeoutSeconds}s (pid=$($process.Id))"
    }
    $process.WaitForExit()
    Write-CapturedOutput -OutTask $stdoutTask -ErrorTask $stderrTask
    $outputFlushed = $true
    $record.exitCode = $process.ExitCode
    $record.outcome = if ($process.ExitCode -eq 0) { 'succeeded' } else { 'failed' }
    $record.cleanupState = 'awaited; no owned descendants expected'
    exit $process.ExitCode
} finally {
    if ($process -and $process.HasExited -eq $false) {
        $record.cleanupState = 'terminating-owned-descendants'
        [void](Stop-OwnedTree -RootPid $process.Id)
        try { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
    if ($process -and -not $outputFlushed) {
        try { $process.WaitForExit(); Write-CapturedOutput -OutTask $stdoutTask -ErrorTask $stderrTask } catch { }
    }
    if ($stream) { $stream.Dispose() }
    $record.endTimeUtc = (Get-Date).ToUniversalTime().ToString('o')
    # LEDGER-APPEND-ATOMIC-BUILD-LOGS-1: pwsh 7 Add-Content (FileMode.Append) loses lines under concurrent writers
    # (~14 % across 6 writers). FILE_APPEND_DATA only makes the OS append at the current end of file atomically.
    # Same bytes as before: the JSON line, then the platform newline, UTF-8 without a BOM.
    $ownershipBytes = [System.Text.UTF8Encoding]::new($false).GetBytes(($record | ConvertTo-Json -Compress) + [Environment]::NewLine)
    $ownershipLog = [System.IO.FileSystemAclExtensions]::Create([System.IO.FileInfo]::new($coordination), [System.IO.FileMode]::OpenOrCreate, [System.Security.AccessControl.FileSystemRights]::AppendData, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete), 4096, [System.IO.FileOptions]::None, $null)
    try { $ownershipLog.Write($ownershipBytes, 0, $ownershipBytes.Length); $ownershipLog.Flush() } finally { $ownershipLog.Dispose() }
}
