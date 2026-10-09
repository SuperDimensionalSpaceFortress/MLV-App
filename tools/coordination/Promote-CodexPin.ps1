<#
.SYNOPSIS
Promote-CodexPin.ps1 - install a PRIVATE codex at a pinned version, prove it can review, and only
then write the tracked pin (tools/coordination/codex-pin.json) that Invoke-Lane.ps1 reads.

.DESCRIPTION
CODEX-KEY-PRIVATE-PIN-1. Read-only codex lanes (review keys) used to run the shared global npm codex,
which CLI-Currency reinstalls every ~6 h and other projects hold open: a key could change binary
between rounds (the codex 0.162 EXEC_BLIND incident, 2026-10-09), npm could race EBUSY, and no
verdict named its binary. Invoke-Lane.ps1 (Resolve-CodexExe) now runs
    <PinRoot>\<version>\node_modules\.bin\codex.cmd
for a read-only lane when codex-pin.json names <version> and that exe reports it. This script is
the only writer of both the install and the pin. It never touches the global install
(%APPDATA%\npm); it only READS the global `codex --version` when -Version is not given.

Promotion gate, in order; the first failure refuses with its exit code and writes no pin:
  1. npm install --prefix <PinRoot>\<version> @openai/codex@<version>   (skipped when the exe is
     already there and reports <version>)
  2. the installed exe exists and `--version` reports <version>
  3. zero-token sandbox probe: `codex sandbox -P :read-only -C <WorkDir> -- git --version` rc 0,
     elevated first (with -WindowsSandbox auto), unelevated only if elevated fails; the mode that
     passed is the one recorded in the pin
  4. write probe: the same sandbox asked to write (`git init <new dir>`) is DENIED (non-zero rc
     AND nothing created)
  5. one real `codex exec` in the read-only sandbox, MCP servers disabled exactly as Invoke-Lane
     does for a review key, classified EXEC_OK by the codex-exec-health rule (at least one shell
     exec ran). This step spends tokens: one short call.
  6. write codex-pin.json (temp file + move) and promotion.json evidence

Exit codes:
  0   promoted (pin written)
  2   bad arguments (version not plain semver, no version resolvable, WorkDir missing)
  10  npm install failed
  11  installed exe missing, or its --version is not <version>
  12  read-only sandbox probe failed in every candidate sandbox mode
  13  write probe NOT denied (the read-only sandbox let a write through)
  14  exec probe not EXEC_OK (no shell exec ran: blind, timed out, or no exec attempted)
  15  a probe reported an auth/login failure -- stopped; this script never runs auth commands
  16  pin or evidence write failed
  17  `codex mcp list --json` unreadable, so MCP could not be disabled for the exec probe
  18  model resolution failed for the exec probe (pass -Model to skip it)

ASCII only. PowerShell 7+.
#>
[CmdletBinding()]
param(
    # Version to pin. Default: whatever the global codex reports (read only, never changed).
    [string]$Version = '',
    [string]$PinRoot = 'C:\mlvtmp\codex-pin',
    [string]$PinFile = '',
    # Repo the probes run against (read only). Default: the checkout this script lives in.
    [string]$WorkDir = '',
    [ValidateSet('auto', 'elevated', 'unelevated')]
    [string]$WindowsSandbox = 'auto',
    # Where promotion.json and probe transcripts go. Default: <PinRoot>\<version>-promotion-<utc>.
    [string]$EvidenceDir = '',
    # Exec probe model. Default: the luna tier resolved by resolve-codex-tier.py, as a lane would.
    [string]$Model = '',
    [string]$Tier = 'luna',
    [int]$ExecTimeoutSec = 240,
    [int]$ProbeTimeoutSec = 60,
    [int]$InstallTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Same rule as Invoke-Lane.ps1 ($CODEX_PIN_EXE_RELATIVE / $CODEX_PIN_VERSION_PATTERN); the
# containment tests pin both literals in both files.
$CODEX_PIN_EXE_RELATIVE = 'node_modules\.bin\codex.cmd'
$CODEX_PIN_VERSION_PATTERN = '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'
# codex-exec-health.ps1 classification rule (hub tool, CODEX-KEY-EXEC-PREFLIGHT-1).
$EXEC_OK_PATTERN = '(?m)^[ \t]*(?:succeeded in \d+ms|exited (?!-1 in 0ms)-?\d+ in \d+ms)'
$EXEC_REJECTED_PATTERN = '(?m)^.*(?:Failed to create unified exec process|setup refresh had errors|CreateProcess \{ message: "Rejected).*$'
$AUTH_FAILURE_PATTERN = '(?i)(not logged in|codex login|401 Unauthorized|authentication (required|failed)|please (log|sign) in)'

if (-not $PinFile) { $PinFile = Join-Path $PSScriptRoot 'codex-pin.json' }
if (-not $WorkDir) { $WorkDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path }

$evidence = [ordered]@{
    schema = 'mlv-app/codex-pin-promotion/v1'; startedUtc = [DateTime]::UtcNow.ToString('o')
    version = $null; pinRoot = $PinRoot; prefix = $null; exe = $null; workDir = $WorkDir
    windowsSandboxRequested = $WindowsSandbox; steps = [System.Collections.Generic.List[object]]::new()
    result = $null; exitCode = $null
}

function Invoke-Bounded {
    param([string]$Exe, [string[]]$ArgList, [string]$Cwd, [int]$TimeoutSec, [string]$StdIn = '')
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $Exe
    foreach ($a in $ArgList) { [void]$psi.ArgumentList.Add($a) }
    $psi.WorkingDirectory = $Cwd
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $p = [System.Diagnostics.Process]::Start($psi)
    $o = $p.StandardOutput.ReadToEndAsync()
    $e = $p.StandardError.ReadToEndAsync()
    if ($StdIn) { $p.StandardInput.Write($StdIn) }
    $p.StandardInput.Close()
    $timedOut = -not $p.WaitForExit([math]::Max(1, $TimeoutSec) * 1000)
    if ($timedOut) { try { $p.Kill($true) } catch { } }
    $p.WaitForExit()
    [pscustomobject]@{
        exit = if ($timedOut) { $null } else { $p.ExitCode }; timedOut = $timedOut
        out = $o.Result; err = $e.Result; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        argv = @($ArgList)
    }
}

function Get-Tail([string]$Text, [int]$Max = 1500) {
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    if ($Text.Length -le $Max) { return $Text }
    return $Text.Substring($Text.Length - $Max)
}

function Add-Step([string]$Name, [string]$Outcome, $Run = $null, [hashtable]$Extra = @{}) {
    $s = [ordered]@{ name = $Name; outcome = $Outcome; utc = [DateTime]::UtcNow.ToString('o') }
    if ($null -ne $Run) {
        $s.argv = $Run.argv; $s.exit = $Run.exit; $s.timedOut = $Run.timedOut; $s.seconds = $Run.seconds
        $s.stdoutTail = Get-Tail $Run.out; $s.stderrTail = Get-Tail $Run.err
    }
    foreach ($k in $Extra.Keys) { $s[$k] = $Extra[$k] }
    $evidence.steps.Add($s)
    Write-Host ("[promote-codex-pin] {0}: {1}" -f $Name, $Outcome)
}

function Write-Evidence {
    if (-not $EvidenceDir) { return }
    try {
        $evidence.endedUtc = [DateTime]::UtcNow.ToString('o')
        [System.IO.File]::WriteAllText((Join-Path $EvidenceDir 'promotion.json'),
            ($evidence | ConvertTo-Json -Depth 8), [System.Text.UTF8Encoding]::new($false))
    } catch {
        Write-Host "[promote-codex-pin] evidence write failed: $($_.Exception.Message)"
    }
}

function Stop-Promotion([int]$Code, [string]$Why) {
    $evidence.result = "REFUSED: $Why"; $evidence.exitCode = $Code
    Write-Evidence
    Write-Host "[promote-codex-pin] REFUSED exit $Code -- $Why"
    exit $Code
}

function Test-AuthFailure($Run, [string]$Step) {
    if ($null -ne $Run -and (($Run.out + "`n" + $Run.err) -match $AUTH_FAILURE_PATTERN)) {
        Add-Step $Step 'AUTH_FAILURE' $Run
        Stop-Promotion 15 "auth/login failure reported during $Step ('$($Matches[0])'); not attempting any auth command"
    }
}

# ------------------------------------------------------------------ 0. arguments
if (-not (Test-Path -LiteralPath $WorkDir -PathType Container)) {
    Write-Host "[promote-codex-pin] REFUSED exit 2 -- WorkDir not found: $WorkDir"; exit 2
}
if (-not $Version) {
    $globalExe = Join-Path $env:APPDATA 'npm\codex.cmd'
    if (Test-Path -LiteralPath $globalExe -PathType Leaf) {
        $gv = Invoke-Bounded -Exe $globalExe -ArgList @('--version') -Cwd $WorkDir -TimeoutSec 30
        if ($gv.exit -eq 0 -and $gv.out -match '(?m)^codex-cli\s+(\S+)\s*$') { $Version = $Matches[1] }
    }
    if (-not $Version) { Write-Host '[promote-codex-pin] REFUSED exit 2 -- no -Version and the global codex version is unreadable'; exit 2 }
}
if ($Version -notmatch $CODEX_PIN_VERSION_PATTERN) {
    Write-Host "[promote-codex-pin] REFUSED exit 2 -- version '$Version' is not plain semver"; exit 2
}
$prefix = [System.IO.Path]::Combine($PinRoot, $Version)
$exe = [System.IO.Path]::Combine($prefix, $CODEX_PIN_EXE_RELATIVE)
$evidence.version = $Version; $evidence.prefix = $prefix; $evidence.exe = $exe
if (-not $EvidenceDir) {
    $EvidenceDir = [System.IO.Path]::Combine($PinRoot, ('{0}-promotion-{1}' -f $Version, [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')))
}
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
$EvidenceDir = (Resolve-Path -LiteralPath $EvidenceDir).Path
$evidence.evidenceDir = $EvidenceDir

function Get-ExeVersion {
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { return $null }
    $r = Invoke-Bounded -Exe $exe -ArgList @('--version') -Cwd $WorkDir -TimeoutSec 30
    if ($r.exit -eq 0 -and $r.out -match '(?m)^codex-cli\s+(\S+)\s*$') { return $Matches[1] }
    return $null
}

# ------------------------------------------------------------------ 1. install
if ((Get-ExeVersion) -eq $Version) {
    Add-Step 'install' "SKIPPED: $exe already reports $Version"
} else {
    New-Item -ItemType Directory -Force -Path $prefix | Out-Null
    $npm = (Get-Command -Name 'npm.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($null -eq $npm) { Add-Step 'install' 'FAILED: npm.cmd not found'; Stop-Promotion 10 'npm.cmd not found on PATH' }
    $ir = Invoke-Bounded -Exe $npm.Source -Cwd $prefix -TimeoutSec $InstallTimeoutSec -ArgList @(
        'install', '--prefix', $prefix, '--no-save', '--no-fund', '--no-audit', '--loglevel=error', "@openai/codex@$Version")
    if ($ir.timedOut -or $ir.exit -ne 0) {
        Add-Step 'install' 'FAILED' $ir
        Stop-Promotion 10 "npm install exited $($ir.exit) (timedOut=$($ir.timedOut))"
    }
    Add-Step 'install' 'OK' $ir
}

# ------------------------------------------------------------------ 2. version
$actual = Get-ExeVersion
if ($actual -ne $Version) {
    Add-Step 'version' "FAILED: $exe reports '$actual'"
    Stop-Promotion 11 "installed exe reports '$actual', expected '$Version'"
}
Add-Step 'version' "OK: codex-cli $actual"

# ------------------------------------------------------------------ 3. read-only sandbox probe
$modes = if ($WindowsSandbox -eq 'auto') { @('elevated', 'unelevated') } else { @($WindowsSandbox) }
$chosen = $null
foreach ($m in $modes) {
    $sr = Invoke-Bounded -Exe $exe -Cwd $WorkDir -TimeoutSec $ProbeTimeoutSec -ArgList @(
        'sandbox', '-c', ('windows.sandbox="{0}"' -f $m), '-P', ':read-only', '-C', $WorkDir, '--', 'git', '--version')
    Test-AuthFailure $sr "sandbox-probe-$m"
    if (-not $sr.timedOut -and $sr.exit -eq 0 -and $sr.out -match '(?m)^git version ') {
        Add-Step "sandbox-probe-$m" 'OK' $sr
        $chosen = $m
        break
    }
    Add-Step "sandbox-probe-$m" 'FAILED' $sr
}
if (-not $chosen) { Stop-Promotion 12 "read-only sandbox probe failed in mode(s): $($modes -join ', ')" }
$evidence.windowsSandbox = $chosen

# ------------------------------------------------------------------ 4. write probe
$probeDir = Join-Path $EvidenceDir 'write-probe'
New-Item -ItemType Directory -Force -Path $probeDir | Out-Null
# `git init <name>` is the write: it creates a directory and needs no shell redirection. A bare
# '>' argument would be read as a redirect by the cmd.exe that runs the codex.cmd shim itself,
# OUTSIDE the sandbox, and fake a "write allowed" result.
$probeName = 'write-probe-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$probeFile = Join-Path $probeDir $probeName
$wr = Invoke-Bounded -Exe $exe -Cwd $probeDir -TimeoutSec $ProbeTimeoutSec -ArgList @(
    'sandbox', '-c', ('windows.sandbox="{0}"' -f $chosen), '-P', ':read-only', '-C', $probeDir, '--',
    'git', 'init', '-q', $probeName)
Test-AuthFailure $wr 'write-probe'
$written = Test-Path -LiteralPath $probeFile
# DENIED needs both a non-zero exit and no write; a timeout proves neither.
if ($written -or $wr.timedOut -or $wr.exit -eq 0) {
    Add-Step 'write-probe' "NOT DENIED (exit $($wr.exit), file written=$written)" $wr
    Stop-Promotion 13 "the $chosen read-only sandbox let a write through (exit $($wr.exit), file written=$written)"
}
Add-Step 'write-probe' "DENIED (exit $($wr.exit), file written=False)" $wr

# ------------------------------------------------------------------ 5. exec probe (spends tokens)
if (-not $Model) {
    $resolver = Join-Path $PSScriptRoot 'resolve-codex-tier.py'
    $py = $null
    foreach ($n in @('python.exe', 'python3.exe', 'py.exe')) {
        $c = Get-Command -Name $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { $py = $c.Source; break }
    }
    if (-not $py) { Add-Step 'model' 'FAILED: no python'; Stop-Promotion 18 'no python interpreter to resolve the codex tier (pass -Model)' }
    $pyArgs = @()
    if ((Split-Path -Leaf $py) -ieq 'py.exe') { $pyArgs += '-3' }
    $mr = Invoke-Bounded -Exe $py -Cwd $WorkDir -TimeoutSec 30 -ArgList ($pyArgs + @($resolver, '--tier', $Tier))
    try { $mj = $mr.out | ConvertFrom-Json } catch { $mj = $null }
    $okProp = if ($null -ne $mj) { $mj.PSObject.Properties['ok'] } else { $null }
    if ($null -eq $okProp -or -not [bool]$okProp.Value) {
        Add-Step 'model' 'FAILED' $mr
        Stop-Promotion 18 "tier '$Tier' did not resolve"
    }
    $Model = [string]$mj.resolvedModel
}
Add-Step 'model' "OK: $Model"

$lr = Invoke-Bounded -Exe $exe -Cwd $WorkDir -TimeoutSec 60 -ArgList @('mcp', 'list', '--json', '-c', 'features.plugins=false')
Test-AuthFailure $lr 'mcp-list'
$mcpArgs = @('-c', 'features.plugins=false', '-c', 'features.apps=false')
try {
    if ($lr.timedOut -or $lr.exit -ne 0) { throw "exit $($lr.exit)" }
    $listed = ConvertFrom-Json -InputObject $lr.out -NoEnumerate
    if ($listed -isnot [array]) { throw 'not a JSON array' }
    foreach ($server in $listed) {
        $np = if ($null -ne $server) { $server.PSObject.Properties['name'] } else { $null }
        $name = if ($null -ne $np) { [string]$np.Value } else { '' }
        if ($name -notmatch '^[A-Za-z0-9_-]+$') { throw "unusable server name '$name'" }
        $mcpArgs += @('-c', "mcp_servers.$name.enabled=false")
    }
} catch {
    Add-Step 'mcp-list' "FAILED: $($_.Exception.Message)" $lr
    Stop-Promotion 17 "codex mcp list --json unreadable: $($_.Exception.Message)"
}
Add-Step 'mcp-list' 'OK' $lr

$nonce = 'PINPROBE' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$lastFile = Join-Path $EvidenceDir 'exec-probe.last.txt'
$execArgs = @('exec', '-m', $Model, '-c', 'model_reasoning_effort="low"', '-s', 'read-only',
    '-c', ('windows.sandbox="{0}"' -f $chosen)) + $mcpArgs + @('-C', $WorkDir, '-o', $lastFile, '--skip-git-repo-check', '-')
$er = Invoke-Bounded -Exe $exe -Cwd $WorkDir -TimeoutSec $ExecTimeoutSec -ArgList $execArgs `
    -StdIn ("PIN PROMOTION PROBE. Run exactly one shell command: git --version`nThen reply with exactly this one line and nothing else: $nonce`n")
try {
    [System.IO.File]::WriteAllText((Join-Path $EvidenceDir 'exec-probe.stdout.txt'), $er.out, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText((Join-Path $EvidenceDir 'exec-probe.stderr.txt'), $er.err, [System.Text.UTF8Encoding]::new($false))
} catch { }
Test-AuthFailure $er 'exec-probe'
$transcript = $er.err + "`n" + $er.out
$okCount = [regex]::Matches($transcript, $EXEC_OK_PATTERN).Count
$rejCount = [regex]::Matches($transcript, $EXEC_REJECTED_PATTERN).Count
$execClass = if ($okCount -gt 0) { 'EXEC_OK' } elseif ($rejCount -gt 0) { 'EXEC_BLIND' } else { 'UNKNOWN' }
$nonceEchoed = (Test-Path -LiteralPath $lastFile) -and ((Get-Content -LiteralPath $lastFile -Raw) -match [regex]::Escape($nonce))
$execExtra = @{ execClass = $execClass; execOkCount = $okCount; execRejectedCount = $rejCount; nonceEchoed = $nonceEchoed }
if ($execClass -ne 'EXEC_OK') {
    Add-Step 'exec-probe' "FAILED: $execClass" $er $execExtra
    Stop-Promotion 14 "exec probe classified $execClass (exit $($er.exit), timedOut=$($er.timedOut), ok=$okCount, rejected=$rejCount)"
}
Add-Step 'exec-probe' "EXEC_OK (ok=$okCount rejected=$rejCount nonceEchoed=$nonceEchoed)" $er $execExtra

# ------------------------------------------------------------------ 6. write the pin
$evidencePath = Join-Path $EvidenceDir 'promotion.json'
$pin = [ordered]@{
    schema            = 'mlv-app/codex-pin/v1'
    version           = $Version
    windowsSandbox    = $chosen
    installPrefix     = "<Invoke-Lane -CodexPinRoot, default $PinRoot>\$Version (exe $CODEX_PIN_EXE_RELATIVE); written by Promote-CodexPin.ps1"
    promotedAt        = [DateTime]::UtcNow.ToString('o')
    promotionEvidence = $evidencePath
    gate              = [ordered]@{
        sandboxProbe = "codex sandbox -P :read-only -- git --version rc 0 ($chosen)"
        writeProbe   = "DENIED (exit $($wr.exit))"
        execProbe    = "$execClass ok=$okCount rejected=$rejCount model=$Model"
    }
}
try {
    $tmp = "$PinFile.tmp$PID"
    [System.IO.File]::WriteAllText($tmp, (($pin | ConvertTo-Json -Depth 4) + "`n"), [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination $PinFile -Force
} catch {
    Stop-Promotion 16 "pin write failed: $($_.Exception.Message)"
}
$evidence.result = "PROMOTED codex $Version ($chosen sandbox) -> $PinFile"; $evidence.exitCode = 0
Write-Evidence
Write-Host "[promote-codex-pin] PROMOTED codex $Version ($chosen sandbox); pin $PinFile; evidence $evidencePath"
exit 0
