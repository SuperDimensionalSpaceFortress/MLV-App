# um-ssh.ps1
# RUN ON THE VM. Executes a command on Ultra-Magnus over SSH using the dedicated
# key (passwordless). The remote default shell is PowerShell (set by
# enable-openssh-admin.ps1), so -Command runs as PowerShell on the host.
#
# Usage:
#   .\um-ssh.ps1 -Command 'hostname; (Get-CimInstance Win32_VideoController).Name'
#   .\um-ssh.ps1 -Command 'powershell -File G:\Temp\mlv-gpu-profile\run-ultra-magnus-profile.ps1'

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Command,
    [string]$HostName = 'Ultra-Magnus',
    [string]$User = 'obabalola',
    [int]$ConnectTimeout = 10,
    # Wall-clock bound for the whole call, including draining its output. At the bound the
    # ssh process tree is killed, the reason goes to stderr and a receipt, and the exit code
    # is 124. Default 3600 s: no tracked caller exists and the longest documented use is the
    # host profile bundle (duration not measured here); 1 h is a generous ceiling that still ends
    # a hang long before a 6 h task limit. A caller with a longer job passes a larger value.
    [ValidateRange(1, 86400)][int]$TimeoutSeconds = 3600,
    # Test seams: the ssh executable, the private key and the timeout receipt location.
    [string]$SshPath = 'ssh.exe',
    [string]$KeyPath = '',
    [string]$ReceiptPath = ''
)
# Exit codes: ssh's own exit code passes through (255 = ssh failure); 124 = wall-clock timeout.
# stdin is closed (EOF) before ssh reads it, so an inherited stdin from a hidden scheduled task
# or a headless lane can never park the call (bus TRAPS airmypc 2026-10-16).
$repo = Split-Path (Split-Path $PSScriptRoot)   # tools\profiling -> repo root
$key  = Join-Path $repo '.claude-state\ssh\ultra-magnus_id'
if ($KeyPath) { $key = $KeyPath }
$khDir = Join-Path ([System.IO.Path]::GetTempPath()) 'mlv-app-ultra-magnus-ssh'
New-Item -ItemType Directory -Force -Path $khDir | Out-Null
$kh   = Join-Path $khDir 'known_hosts'
if (-not (Test-Path $key)) { throw "VM private key not found: $key" }

$sshArgs = @(
    '-i', $key,
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', "UserKnownHostsFile=$kh",
    '-o', "ConnectTimeout=$ConnectTimeout",
    '-o', 'BatchMode=yes',
    '-o', 'ServerAliveInterval=15',   # MUT:alive-interval
    '-o', 'ServerAliveCountMax=3',    # MUT:alive-count
    "$User@$HostName", $Command
)

# Windows command-line quoting (CommandLineToArgvW rules) so $Command reaches ssh as ONE argv.
function ConvertTo-UmSshArg([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $bs = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq [char]92) { $bs++; continue }
        if ($ch -eq [char]34) { [void]$sb.Append([char]92, $bs * 2 + 1); [void]$sb.Append('"'); $bs = 0; continue }
        if ($bs -gt 0) { [void]$sb.Append([char]92, $bs); $bs = 0 }
        [void]$sb.Append($ch)
    }
    if ($bs -gt 0) { [void]$sb.Append([char]92, $bs * 2) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

# Kill the ssh process and every descendant. taskkill /T walks the parent links; the plain
# Kill() is only the fallback for the root process.
function Stop-UmSshTree([System.Diagnostics.Process]$Proc) {
    & taskkill.exe /T /F /PID $Proc.Id *> $null   # MUT:tree-kill
    try { if (-not $Proc.HasExited) { $Proc.Kill() } } catch { }
    try { [void]$Proc.WaitForExit(5000) } catch { }
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $SshPath
$psi.Arguments = (($sshArgs | ForEach-Object { ConvertTo-UmSshArg ([string]$_) }) -join ' ')
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

$clock = [System.Diagnostics.Stopwatch]::StartNew()
$proc = [System.Diagnostics.Process]::Start($psi)
$proc.StandardInput.Close()   # MUT:stdin-close
$outTask = $proc.StandardOutput.ReadLineAsync()
$errTask = $proc.StandardError.ReadLineAsync()
$outDone = $false
$errDone = $false
$timedOut = $false
while (-not ($outDone -and $errDone -and $proc.HasExited)) {
    if ($clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) { $timedOut = $true; break }   # MUT:wall-clock
    $pending = @()
    if (-not $outDone) { $pending += $outTask }
    if (-not $errDone) { $pending += $errTask }
    if ($pending.Count -gt 0) {
        [void][System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]]$pending, 200)
    } else {
        [void]$proc.WaitForExit(200)
    }
    if (-not $outDone -and $outTask.IsCompleted) {
        $line = $outTask.Result
        if ($null -eq $line) { $outDone = $true } else { Write-Output $line; $outTask = $proc.StandardOutput.ReadLineAsync() }
    }
    if (-not $errDone -and $errTask.IsCompleted) {
        $line = $errTask.Result
        if ($null -eq $line) { $errDone = $true } else { [Console]::Error.WriteLine($line); $errTask = $proc.StandardError.ReadLineAsync() }
    }
}

if ($timedOut) {
    $phase = if ($proc.HasExited) { 'draining output after ssh exited' } else { 'ssh still running' }
    $reason = "um-ssh TIMEOUT: wall-clock bound of $TimeoutSeconds s reached ($phase); ssh process tree killed (host=$HostName pid=$($proc.Id))"
    Stop-UmSshTree $proc
    [Console]::Error.WriteLine($reason)
    if (-not $ReceiptPath) { $ReceiptPath = Join-Path $khDir 'um-ssh-last-timeout.json' }
    $receipt = [ordered]@{
        outcome        = 'timeout'
        reason         = $reason
        host           = $HostName
        timeoutSeconds = $TimeoutSeconds
        pid            = $proc.Id
        atUtc          = [DateTime]::UtcNow.ToString('o')
    }
    try { ($receipt | ConvertTo-Json) | Set-Content -Path $ReceiptPath -Encoding ascii } catch { }
    exit 124
}
$proc.WaitForExit()
exit $proc.ExitCode
