# test-um-ssh-stdin-bound.ps1
# Guard for tools/profiling/um-ssh.ps1 (UM-SSH-STDIN-BOUND-1). No host is contacted: the ssh
# executable is a local fake (-SshPath) that blocks on stdin or sleeps past the bound.
#
#   GREEN: the real script closes stdin, sends BatchMode/ConnectTimeout/ServerAlive options,
#          passes the exit code through, and at the wall-clock bound kills the whole process
#          tree and exits 124 with the reason on stderr and in a receipt; and a known_hosts path
#          with whitespace and '!' reaches ssh quoted, so ssh -G resolves it to ONE file.
#   RED:   each protection is removed in turn (lines tagged "# MUT:<name>" in um-ssh.ps1) and
#          the same scenarios must go RED. A tag that no longer matches exactly one line fails
#          the test, so the mutants cannot silently rot.
#
# Usage: pwsh -NoProfile -File tools\profiling\test-um-ssh-stdin-bound.ps1 [-SkipMutants]
#        [-ScriptUnderTest <path>]  (point at any um-ssh.ps1 variant to see it RED or GREEN)
[CmdletBinding()]
param(
    [string]$ScriptUnderTest = (Join-Path $PSScriptRoot 'um-ssh.ps1'),
    [switch]$SkipMutants
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$pwshExe = (Get-Process -Id $PID).Path
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("um-ssh-test-" + [Guid]::NewGuid().ToString('N').Substring(0, 12))
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

# ---- the fake remote -------------------------------------------------------------------
$fakePs1 = Join-Path $sandbox 'fake-ssh.ps1'
$fakeCmd = Join-Path $sandbox 'fake-ssh.cmd'
$keyFile = Join-Path $sandbox 'dummy_key'
Set-Content -Path $keyFile -Value 'not-a-key' -Encoding ascii
Set-Content -Path $fakePs1 -Encoding ascii -Value @'
$mode = $env:MLV_FAKE_SSH_MODE
if ($env:MLV_FAKE_ARGFILE) { Set-Content -Path $env:MLV_FAKE_ARGFILE -Value ($args -join "`n") -Encoding ascii }
if ($env:MLV_FAKE_PIDFILE) { Add-Content -Path $env:MLV_FAKE_PIDFILE -Value $PID -Encoding ascii }
switch ($mode) {
    'stdin' {
        # Blocks until stdin reaches EOF, exactly like ssh reading an inherited console/pipe.
        $text = [Console]::In.ReadToEnd()
        Write-Output "STDIN-EOF bytes=$($text.Length)"
        exit 0
    }
    'args' {
        Write-Output 'ARGS-OK'
        exit 7
    }
    'sleep' {
        # Ignores stdin and outlives the bound; the grandchild proves the whole tree is killed.
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = (Get-Process -Id $PID).Path
        $psi.Arguments = '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 600'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $grand = [System.Diagnostics.Process]::Start($psi)
        if ($env:MLV_FAKE_PIDFILE) { Add-Content -Path $env:MLV_FAKE_PIDFILE -Value $grand.Id -Encoding ascii }
        Write-Output 'SLEEPING'
        Start-Sleep -Seconds 600
    }
}
'@
Set-Content -Path $fakeCmd -Encoding ascii -Value ('@"{0}" -NoProfile -NonInteractive -File "%~dp0fake-ssh.ps1" %*' -f $pwshExe)

function Stop-TreeById([int]$ProcessId) {
    & taskkill.exe /T /F /PID $ProcessId *> $null
}

# Runs one um-ssh.ps1 variant against the fake. The harness keeps ITS OWN stdin pipe open and
# never closes it, which is the inherited-stdin situation the card is about.
function Invoke-UnderTest {
    param([string]$Script, [string]$Mode, [int]$Timeout, [string]$Command, [string]$Tag, [string]$TempDir = '')
    $argFile = Join-Path $sandbox "$Tag.args"
    $pidFile = Join-Path $sandbox "$Tag.pids"
    $receipt = Join-Path $sandbox "$Tag.receipt.json"
    foreach ($f in @($argFile, $pidFile, $receipt)) { if (Test-Path $f) { Remove-Item -LiteralPath $f -Force } }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $pwshExe
    $q = { param($s) '"' + $s + '"' }
    $psi.Arguments = "-NoProfile -NonInteractive -File $(& $q $Script) -Command $(& $q $Command)" +
        " -SshPath $(& $q $fakeCmd) -KeyPath $(& $q $keyFile) -ReceiptPath $(& $q $receipt) -TimeoutSeconds $Timeout"
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['MLV_FAKE_SSH_MODE'] = $Mode
    $psi.EnvironmentVariables['MLV_FAKE_ARGFILE'] = $argFile
    $psi.EnvironmentVariables['MLV_FAKE_PIDFILE'] = $pidFile
    if ($TempDir) { $psi.EnvironmentVariables['TMP'] = $TempDir; $psi.EnvironmentVariables['TEMP'] = $TempDir }
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $p = [System.Diagnostics.Process]::Start($psi)
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    $bound = $Timeout + 25
    $hung = -not $p.WaitForExit($bound * 1000)
    if ($hung) { Stop-TreeById $p.Id; [void]$p.WaitForExit(5000) }
    [void][System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]@($outTask, $errTask), 5000)
    $pids = @()
    if (Test-Path $pidFile) { $pids = @(Get-Content $pidFile | Where-Object { $_ } | ForEach-Object { [int]$_ }) }
    [pscustomobject]@{
        Exit    = $(if ($hung) { $null } else { $p.ExitCode })
        Hung    = $hung
        Seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 1)
        Stdout  = $(if ($outTask.IsCompleted) { $outTask.Result } else { '' })
        Stderr  = $(if ($errTask.IsCompleted) { $errTask.Result } else { '' })
        Args    = @(if (Test-Path $argFile) { Get-Content $argFile })
        Pids    = $pids
        Receipt = $(if (Test-Path $receipt) { Get-Content $receipt -Raw } else { $null })
    }
}

function Wait-PidsGone([int[]]$Pids, [int]$Seconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $alive = @($Pids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { return @() }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $alive
}

$cmdText = "Write-Output 'a b'; hostname"

# Each scenario returns the list of failures it observed (empty = GREEN).
function Test-StdinClosed([string]$Script, [string]$Tag) {
    $f = @()
    $r = Invoke-UnderTest -Script $Script -Mode 'stdin' -Timeout 12 -Command $cmdText -Tag $Tag
    if ($r.Hung) { $f += "stdin: the call hung past the harness bound ($($r.Seconds) s)" }
    elseif ($r.Exit -ne 0) { $f += "stdin: expected exit 0 (stdin at EOF), got $($r.Exit) after $($r.Seconds) s" }
    if ($r.Stdout -notmatch 'STDIN-EOF') { $f += 'stdin: the fake never saw EOF on stdin' }
    return $f
}

function Test-ArgsAndExit([string]$Script, [string]$Tag) {
    $f = @()
    $r = Invoke-UnderTest -Script $Script -Mode 'args' -Timeout 20 -Command $cmdText -Tag $Tag
    if ($r.Hung) { return @("args: the call hung past the harness bound ($($r.Seconds) s)") }
    if ($r.Exit -ne 7) { $f += "args: ssh exit code 7 must pass through, got $($r.Exit)" }
    if ($r.Stdout -notmatch 'ARGS-OK') { $f += 'args: remote stdout did not reach the caller' }
    foreach ($want in @('BatchMode=yes', 'ConnectTimeout=10', 'ServerAliveInterval=15', 'ServerAliveCountMax=3', 'StrictHostKeyChecking=accept-new')) {
        if ($r.Args -notcontains $want) { $f += "args: ssh was not given $want" }
    }
    if ($r.Args.Count -eq 0 -or $r.Args[-1] -ne $cmdText) { $f += "args: the command did not arrive as one argument (last arg: '$($r.Args | Select-Object -Last 1)')" }
    return $f
}

function Test-WallClockBound([string]$Script, [string]$Tag) {
    $f = @()
    $r = Invoke-UnderTest -Script $Script -Mode 'sleep' -Timeout 3 -Command $cmdText -Tag $Tag
    if ($r.Hung) { $f += "bound: no timeout at the bound; the call was still running after $($r.Seconds) s" }
    else {
        if ($r.Exit -ne 124) { $f += "bound: expected exit 124, got $($r.Exit)" }
        if ($r.Stderr -notmatch 'um-ssh TIMEOUT: wall-clock bound of 3 s') { $f += 'bound: the timeout reason is missing from stderr' }
        if (-not $r.Receipt -or $r.Receipt -notmatch '"outcome":\s*"timeout"') { $f += 'bound: no timeout receipt was written' }
    }
    if ($r.Pids.Count -lt 2) { $f += "bound: the fake did not record its process tree (pids: $($r.Pids -join ','))" }
    $alive = @(Wait-PidsGone $r.Pids 5)
    if ($alive.Count -gt 0) {
        $f += "bound: process tree survived the kill (pids still alive: $($alive -join ','))"
        foreach ($id in $alive) { Stop-TreeById $id }
    }
    return $f
}

# UM-SSH-KNOWN-HOSTS-BANG-PATH-1: ssh splits the VALUE of UserKnownHostsFile on whitespace, so a
# known_hosts path under 'C:\!Layi Wkspc\...' (TEMP inside the repo) must reach ssh quoted. The
# temp dir below carries a double space (a split collapses it to one) and a '!'. ssh -G only
# prints the resolved config and contacts no host; without ssh.exe the argv is checked directly.
function Test-KnownHostsPath([string]$Script, [string]$Tag) {
    $f = @()
    $tmp = Join-Path $sandbox 'kh  !dir'
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        $r = Invoke-UnderTest -Script $Script -Mode 'args' -Timeout 20 -Command $cmdText -Tag $Tag -TempDir $tmp
        if ($r.Hung) { return @("known_hosts: the call hung past the harness bound ($($r.Seconds) s)") }
        $want = Join-Path (Join-Path $tmp 'mlv-app-ultra-magnus-ssh') 'known_hosts'
        $opt = @($r.Args | Where-Object { $_ -like 'UserKnownHostsFile=*' })
        if ($opt.Count -ne 1) { return @("known_hosts: expected exactly one UserKnownHostsFile option, got $($opt.Count)") }
        $ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
        if ($ssh) {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $ssh.Source
            foreach ($a in @('-G', '-o', $opt[0], 'dummyhost')) { [void]$psi.ArgumentList.Add($a) }
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true
            $p = [System.Diagnostics.Process]::Start($psi)
            $text = $p.StandardOutput.ReadToEnd()
            [void]$p.WaitForExit(20000)
            $line = @($text -split "`r?`n" | Where-Object { $_ -match '^userknownhostsfile ' })
            # ssh -G joins a multi-file list with ONE space, so equality with the single path rules out a split.
            if ($line.Count -ne 1 -or $line[0] -ne "userknownhostsfile $want") {
                $f += "known_hosts: ssh -G resolved '$($line -join ' | ')' instead of the single file '$want' (option was: $($opt[0]))"
            }
        }
        elseif ($opt[0] -ne ('UserKnownHostsFile="' + $want + '"')) {
            $f += "known_hosts: no ssh.exe to resolve it; the option must carry the path in double quotes, got: $($opt[0])"
        }
    }
    finally {
        # um-ssh created <tmp>\mlv-app-ultra-magnus-ssh (empty: the fake ssh writes no known_hosts); remove both.
        $inner = Join-Path $tmp 'mlv-app-ultra-magnus-ssh'
        foreach ($d in @($inner, $tmp)) { if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Force -ErrorAction SilentlyContinue } }
    }
    return $f
}

function Invoke-AllScenarios([string]$Script, [string]$Tag) {
    return @(Test-StdinClosed $Script "$Tag-stdin") + @(Test-ArgsAndExit $Script "$Tag-args") + @(Test-WallClockBound $Script "$Tag-bound") + @(Test-KnownHostsPath $Script "$Tag-kh")
}

$exitCode = 0
try {
    Write-Host "== GREEN: $ScriptUnderTest"
    $greenFailures = @(Invoke-AllScenarios $ScriptUnderTest 'green')
    foreach ($m in $greenFailures) { Write-Host "  FAIL $m" }
    if ($greenFailures.Count -eq 0) { Write-Host '  PASS stdin closed, options sent, exit code passed through, wall-clock bound kills the tree, known_hosts path with space and ! stays one file (4/4)' }
    else { $exitCode = 1 }

    if (-not $SkipMutants) {
        # marker -> scenario that must go RED when the protection is removed
        $mutants = [ordered]@{
            'stdin-close'    = 'Test-StdinClosed'
            'wall-clock'     = 'Test-WallClockBound'
            'tree-kill'      = 'Test-WallClockBound'
            'alive-interval' = 'Test-ArgsAndExit'
            'alive-count'    = 'Test-ArgsAndExit'
        }
        $original = @(Get-Content -LiteralPath $ScriptUnderTest)
        foreach ($name in $mutants.Keys) {
            $tag = "# MUT:$name"
            $hits = @($original | Where-Object { $_.Contains($tag) })
            if ($hits.Count -ne 1) {
                Write-Host "  FAIL mutant '$name': expected exactly one line tagged '$tag' in the script, found $($hits.Count)"
                $exitCode = 1
                continue
            }
            $mutPath = Join-Path $sandbox "um-ssh-mut-$name.ps1"
            Set-Content -LiteralPath $mutPath -Encoding ascii -Value @($original | Where-Object { -not $_.Contains($tag) })
            Write-Host "== RED expected: mutant '$name' (line removed: $($hits[0].Trim()))"
            $found = @(& $mutants[$name] $mutPath "mut-$name")
            if ($found.Count -gt 0) { foreach ($m in $found) { Write-Host "  RED  $m" } }
            else { Write-Host "  FAIL mutant '$name' stayed GREEN: the guard does not detect it"; $exitCode = 1 }
        }
    }
}
finally {
    # Never leave a fake behind.
    foreach ($pf in (Get-ChildItem -LiteralPath $sandbox -Filter '*.pids' -ErrorAction SilentlyContinue)) {
        foreach ($id in @(Get-Content $pf.FullName | Where-Object { $_ })) {
            if (Get-Process -Id ([int]$id) -ErrorAction SilentlyContinue) { Stop-TreeById ([int]$id) }
        }
    }
    # The sandbox is flat (files only), so delete file by file and then the empty directory.
    try {
        foreach ($leftover in (Get-ChildItem -LiteralPath $sandbox -File -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $leftover.FullName -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $sandbox -Force -ErrorAction Stop
    } catch { Write-Host "  note: sandbox left at $sandbox" }
}
if ($exitCode -eq 0) { Write-Host 'OVERALL PASS' } else { Write-Host 'OVERALL FAIL' }
exit $exitCode
