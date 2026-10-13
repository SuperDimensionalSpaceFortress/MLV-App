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
  4. write probe: the same sandbox asked to write (`git init <new dir>`) is DENIED. DENIED is
     positive evidence (PR #339 r2): the sandboxed run exits non-zero without timing out, creates
     nothing, and its stderr carries a write-denial signature, AND a positive control -- the
     identical `git init` WITHOUT the sandbox, same environment, in a scratch dir -- exits 0 and
     creates its target. A write that went through is exit 13; any other outcome is UNPROVEN
     (timed-out / control-failed / no-denial-signature), exit 19, never DENIED
  5. one real `codex exec` PER MODEL in the read-only sandbox, MCP servers disabled exactly as
     Invoke-Lane does for a review key, each classified EXEC_OK by the codex-exec-health rule (at
     least one shell exec ran). MODEL-POSTURE-R16-1: the models probed are the ones the resolver
     currently picks for EVERY lane tier (-Tiers, default sol, luna, astra), because a pinned CLI
     can silently exclude a newer model (the sol tier moved to gpt-6.1-sol while the pin had only
     ever run luna). Every probed model must pass or nothing is promoted. -Model probes that one
     explicit slug instead. This step spends tokens: one short call per model.
  6. write codex-pin.json (temp file + move) -- including `probedModels`, the slugs step 5 proved,
     which Invoke-Lane checks before running a lane on the pin -- and promotion.json evidence

Exit codes:
  0   promoted (pin written)
  2   bad arguments (version not plain semver, no version resolvable, WorkDir missing)
  10  npm install failed
  11  installed exe missing, or its --version is not <version>
  12  read-only sandbox probe failed in every candidate sandbox mode
  13  write probe NOT denied (the read-only sandbox let a write through)
  14  an exec probe not EXEC_OK (no shell exec ran: blind, timed out, or no exec attempted); the
      message names the model, and no pin is written even if earlier models passed
  15  a probe reported an auth/login failure -- stopped; this script never runs auth commands
  16  pin or evidence write failed
  17  `codex mcp list --json` unreadable, so MCP could not be disabled for the exec probe
  18  model resolution failed for the exec probe: some tier in -Tiers did not resolve (stale or
      missing models cache, no listed model) (pass -Model to skip it)
  19  write probe UNPROVEN: refused, but not provably by the sandbox (see step 4)
  20  a child's cleanup did not finish within its budget (exit-wait-expired / stream-drain-expired)

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
    # Exec probe model. Default: none -- every tier in -Tiers is resolved by resolve-codex-tier.py,
    # as a lane would, and each resolved model is probed. -Model probes this one slug only.
    [string]$Model = '',
    # MODEL-POSTURE-R16-1: the lane tiers whose current model the pin must have proven (Invoke-Lane's
    # codex lanes: sol, luna, astra).
    [string[]]$Tiers = @('sol', 'luna', 'astra'),
    # Models cache the resolver reads (default: ~/.codex/models_cache.json); tests point it at a fixture.
    [string]$ModelsCache = '',
    [int]$ExecTimeoutSec = 240,
    [int]$ProbeTimeoutSec = 60,
    [int]$InstallTimeoutSec = 600
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# ValidateSet matches case-insensitively; the pin carries the canonical lower-case spelling,
# which is the only one Invoke-Lane.ps1 accepts.
$WindowsSandbox = $WindowsSandbox.ToLowerInvariant()

# Same rule as Invoke-Lane.ps1 ($CODEX_PIN_EXE_RELATIVE / $CODEX_PIN_VERSION_PATTERN); the
# containment tests pin both literals in both files.
$CODEX_PIN_EXE_RELATIVE = 'node_modules\.bin\codex.cmd'
$CODEX_PIN_VERSION_PATTERN = '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'
# codex-exec-health.ps1 classification rule (hub tool, CODEX-KEY-EXEC-PREFLIGHT-1).
$EXEC_OK_PATTERN = '(?m)^[ \t]*(?:succeeded in \d+ms|exited (?!-1 in 0ms)-?\d+ in \d+ms)'
$EXEC_REJECTED_PATTERN = '(?m)^.*(?:Failed to create unified exec process|setup refresh had errors|CreateProcess \{ message: "Rejected).*$'
$AUTH_FAILURE_PATTERN = '(?i)(not logged in|codex login|401 Unauthorized|authentication (required|failed)|please (log|sign) in)'
# Write-denial signature for the sandboxed `git init` (step 4). Live on codex-cli 0.160.1, both
# sandbox modes: "fatal: cannot mkdir <name>: Permission denied"; the rest are the Windows, Rust
# and POSIX spellings of the same denial.
$WRITE_DENIAL_PATTERN = '(?i)(Permission denied|Access is denied|os error 5\b|EACCES)'
# CODEX-PIN-PROMOTION-BOUNDS-1: after a child exits or is killed, the exit wait and the
# stdout/stderr drains share this budget; a grandchild that escaped Kill(true) can hold the pipes
# open forever, so on expiry the step fails with a named reason instead of hanging.
$CLEANUP_BUDGET_SEC = 10

if (-not $PinFile) { $PinFile = Join-Path $PSScriptRoot 'codex-pin.json' }
if (-not $WorkDir) { $WorkDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path }

$evidence = [ordered]@{
    schema = 'mlv-app/codex-pin-promotion/v1'; startedUtc = [DateTime]::UtcNow.ToString('o')
    version = $null; pinRoot = $PinRoot; prefix = $null; exe = $null; workDir = $WorkDir
    windowsSandboxRequested = $WindowsSandbox; steps = [System.Collections.Generic.List[object]]::new()
    result = $null; exitCode = $null
}

function Invoke-Bounded {
    param([string]$Step, [string]$Exe, [string[]]$ArgList, [string]$Cwd, [int]$TimeoutSec, [string]$StdIn = '')
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
    $killError = $null
    if ($timedOut) { try { $p.Kill($true) } catch { $killError = $_.Exception.Message } }
    # Bounded cleanup: the post-kill exit wait has its own deadline, the stream drains get what is
    # left of the same budget, and neither is ever an unbounded wait.
    $budgetMs = $CLEANUP_BUDGET_SEC * 1000
    $cleanup = [System.Diagnostics.Stopwatch]::StartNew()
    $exited = $p.WaitForExit($budgetMs)
    $drained = $false
    if ($exited) {
        $leftMs = [int][math]::Max(0, $budgetMs - $cleanup.ElapsedMilliseconds)
        try { $drained = [System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]@($o, $e), $leftMs) } catch { $drained = $true }
    }
    $failure = if (-not $exited) { 'exit-wait-expired' } elseif (-not $drained) { 'stream-drain-expired' } else { $null }
    $run = [pscustomobject]@{
        exit = if ($timedOut -or -not $exited) { $null } else { $p.ExitCode }; timedOut = $timedOut
        out = if ($o.IsCompletedSuccessfully) { $o.Result } else { '' }
        err = if ($e.IsCompletedSuccessfully) { $e.Result } else { '' }
        seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); argv = @($ArgList)
        killError = $killError; cleanupFailure = $failure
    }
    if ($failure) {
        $after = if ($timedOut) { "a ${TimeoutSec}s timeout" } else { 'exit' }
        $killNote = if ($killError) { "; Kill(true) failed: $killError" } else { '' }
        Add-Step $Step "FAILED: cleanup $failure" $run
        Stop-Promotion 20 ("{0}: {1} after {2} (cleanup budget {3}s){4}" -f $Step, $failure, $after, $CLEANUP_BUDGET_SEC, $killNote)
    }
    $run
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
        $s.killError = $Run.killError; $s.cleanupFailure = $Run.cleanupFailure
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
        $gv = Invoke-Bounded -Step 'global-version' -Exe $globalExe -ArgList @('--version') -Cwd $WorkDir -TimeoutSec 30
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
    $r = Invoke-Bounded -Step 'version-check' -Exe $exe -ArgList @('--version') -Cwd $WorkDir -TimeoutSec 30
    if ($r.exit -eq 0 -and $r.out -match '(?m)^codex-cli\s+(\S+)\s*$') { return $Matches[1] }
    return $null
}

# ------------------------------------------------------------------ 1. install
if ((Get-ExeVersion) -ceq $Version) {
    Add-Step 'install' "SKIPPED: $exe already reports $Version"
} else {
    New-Item -ItemType Directory -Force -Path $prefix | Out-Null
    $npm = (Get-Command -Name 'npm.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($null -eq $npm) { Add-Step 'install' 'FAILED: npm.cmd not found'; Stop-Promotion 10 'npm.cmd not found on PATH' }
    $ir = Invoke-Bounded -Step 'install' -Exe $npm.Source -Cwd $prefix -TimeoutSec $InstallTimeoutSec -ArgList @(
        'install', '--prefix', $prefix, '--no-save', '--no-fund', '--no-audit', '--loglevel=error', "@openai/codex@$Version")
    if ($ir.timedOut -or $ir.exit -ne 0) {
        Add-Step 'install' 'FAILED' $ir
        Stop-Promotion 10 "npm install exited $($ir.exit) (timedOut=$($ir.timedOut))"
    }
    Add-Step 'install' 'OK' $ir
}

# ------------------------------------------------------------------ 2. version
$actual = Get-ExeVersion
if ($actual -cne $Version) {
    Add-Step 'version' "FAILED: $exe reports '$actual'"
    Stop-Promotion 11 "installed exe reports '$actual', expected '$Version'"
}
Add-Step 'version' "OK: codex-cli $actual"

# ------------------------------------------------------------------ 3. read-only sandbox probe
$modes = if ($WindowsSandbox -eq 'auto') { @('elevated', 'unelevated') } else { @($WindowsSandbox) }
$chosen = $null
foreach ($m in $modes) {
    $sr = Invoke-Bounded -Step "sandbox-probe-$m" -Exe $exe -Cwd $WorkDir -TimeoutSec $ProbeTimeoutSec -ArgList @(
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
$wr = Invoke-Bounded -Step 'write-probe' -Exe $exe -Cwd $probeDir -TimeoutSec $ProbeTimeoutSec -ArgList @(
    'sandbox', '-c', ('windows.sandbox="{0}"' -f $chosen), '-P', ':read-only', '-C', $probeDir, '--',
    'git', 'init', '-q', $probeName)
Test-AuthFailure $wr 'write-probe'
$written = Test-Path -LiteralPath $probeFile
if ($written -or (-not $wr.timedOut -and $wr.exit -eq 0)) {
    Add-Step 'write-probe' "NOT DENIED (exit $($wr.exit), file written=$written)" $wr
    Stop-Promotion 13 "the $chosen read-only sandbox let a write through (exit $($wr.exit), file written=$written)"
}
# DENIED is positive evidence (PR #339 r2, sol blocker): a non-zero exit with nothing written also
# describes a git that never reached the write -- GIT_CONFIG_COUNT=bogus makes `git init` exit 128
# on "unable to parse command-line config" while `git --version` still passes. So DENIED also needs
# a write-denial signature in the probe's stderr, and a POSITIVE CONTROL: the identical `git init`
# run WITHOUT the sandbox, in the same environment, must exit 0 and create its target.
$controlDir = Join-Path $EvidenceDir 'write-probe-control'
New-Item -ItemType Directory -Force -Path $controlDir | Out-Null
$controlName = 'write-control-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$controlTarget = Join-Path $controlDir $controlName
$git = Get-Command -Name 'git.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$cr = $null
if ($null -ne $git) {
    $cr = Invoke-Bounded -Step 'write-probe-control' -Exe $git.Source -Cwd $controlDir -TimeoutSec $ProbeTimeoutSec -ArgList @(
        'init', '-q', $controlName)
}
$controlCreated = Test-Path -LiteralPath $controlTarget -PathType Container
$controlOk = $null -ne $cr -and -not $cr.timedOut -and $cr.exit -eq 0 -and $controlCreated
$signature = if ($wr.err -match $WRITE_DENIAL_PATTERN) { $Matches[0] } else { $null }
$control = [ordered]@{
    exe = if ($null -ne $git) { $git.Source } else { $null }; cwd = $controlDir
    argv = if ($null -ne $cr) { $cr.argv } else { $null }; exit = if ($null -ne $cr) { $cr.exit } else { $null }
    timedOut = if ($null -ne $cr) { $cr.timedOut } else { $null }; created = $controlCreated
    stderrTail = if ($null -ne $cr) { Get-Tail $cr.err } else { 'git.exe not found on PATH' }
}
$unproven = @()
if ($wr.timedOut) { $unproven += 'timed-out' }
if (-not $controlOk) { $unproven += 'control-failed' }
if (-not $signature) { $unproven += 'no-denial-signature' }
$writeExtra = @{ denialSignature = $signature; control = $control; unprovenReasons = $unproven }
if ($unproven.Count -gt 0) {
    Add-Step 'write-probe' "UNPROVEN: $($unproven -join ',') (exit $($wr.exit), file written=False)" $wr $writeExtra
    Stop-Promotion 19 "write probe UNPROVEN ($($unproven -join ', ')): the refusal is not proof that the $chosen sandbox enforces read-only"
}
Add-Step 'write-probe' "DENIED (exit $($wr.exit), signature '$signature', control exit 0)" $wr $writeExtra

# ------------------------------------------------------------------ 5. exec probe (spends tokens)
# MODEL-POSTURE-R16-1: the models to prove. Each tier resolves exactly as a lane would resolve it
# (resolve-codex-tier.py), so the pin covers what lanes will actually launch, not only one tier.
$probeModels = [System.Collections.Generic.List[string]]::new()
$probeTiersByModel = [ordered]@{}
$modelsFetchedAt = $null
if ($Model) {
    $probeModels.Add($Model)
    $probeTiersByModel[$Model] = @('(explicit -Model)')
} else {
    $resolver = Join-Path $PSScriptRoot 'resolve-codex-tier.py'
    $py = $null
    foreach ($n in @('python.exe', 'python3.exe', 'py.exe')) {
        $c = Get-Command -Name $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { $py = $c.Source; break }
    }
    if (-not $py) { Add-Step 'model' 'FAILED: no python'; Stop-Promotion 18 'no python interpreter to resolve the codex tiers (pass -Model)' }
    $pyArgs = @()
    if ((Split-Path -Leaf $py) -ieq 'py.exe') { $pyArgs += '-3' }
    # `pwsh -File ... -Tiers sol,luna` arrives as ONE string, so each entry is split on commas.
    $tierList = @($Tiers | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)
    if ($tierList.Count -eq 0) { Add-Step 'model' 'FAILED: no tiers'; Stop-Promotion 18 '-Tiers is empty and no -Model was given: nothing to probe' }
    foreach ($tierName in $tierList) {
        $resolverArgs = $pyArgs + @($resolver, '--tier', $tierName)
        if ($ModelsCache) { $resolverArgs += @('--cache', $ModelsCache) }
        $mr = Invoke-Bounded -Step "model-$tierName" -Exe $py -Cwd $WorkDir -TimeoutSec 30 -ArgList $resolverArgs
        try { $mj = $mr.out | ConvertFrom-Json } catch { $mj = $null }
        $okProp = if ($null -ne $mj) { $mj.PSObject.Properties['ok'] } else { $null }
        if ($null -eq $okProp -or -not [bool]$okProp.Value) {
            $errProp = if ($null -ne $mj) { $mj.PSObject.Properties['error'] } else { $null }
            $why = if ($null -ne $errProp) { [string]$errProp.Value } else { 'unparseable resolver output' }
            Add-Step "model-$tierName" "FAILED: $why" $mr
            Stop-Promotion 18 "tier '$tierName' did not resolve: $why"
        }
        $resolved = [string]$mj.resolvedModel
        $fetchedProp = $mj.PSObject.Properties['fetchedAt']
        if ($null -ne $fetchedProp) { $modelsFetchedAt = [string]$fetchedProp.Value }
        if (-not $probeTiersByModel.Contains($resolved)) { $probeModels.Add($resolved); $probeTiersByModel[$resolved] = @() }
        $probeTiersByModel[$resolved] += $tierName
        Add-Step "model-$tierName" "OK: $resolved"
    }
}
Add-Step 'model' ("OK: " + (($probeModels | ForEach-Object { '{0} ({1})' -f $_, ($probeTiersByModel[$_] -join '+') }) -join ', '))

$lr = Invoke-Bounded -Step 'mcp-list' -Exe $exe -Cwd $WorkDir -TimeoutSec 60 -ArgList @('mcp', 'list', '--json', '-c', 'features.plugins=false')
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


# One real exec per model, each in the pinned read-only sandbox with MCP disabled. ALL must be
# EXEC_OK: a pin that proved only some of the lane tiers must not claim the others.
$probeResults = [System.Collections.Generic.List[object]]::new()
foreach ($probeModel in $probeModels) {
    $safeName = $probeModel -replace '[^A-Za-z0-9._-]', '_'
    $stepName = "exec-probe:$probeModel"
    $nonce = 'PINPROBE' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $lastFile = Join-Path $EvidenceDir "exec-probe.$safeName.last.txt"
    # high, the effort every lane runs at (R16.3): a model proven only at another effort is not proven for a lane.
    $execArgs = @('exec', '-m', $probeModel, '-c', 'model_reasoning_effort="high"', '-s', 'read-only',
        '-c', ('windows.sandbox="{0}"' -f $chosen)) + $mcpArgs + @('-C', $WorkDir, '-o', $lastFile, '--skip-git-repo-check', '-')
    $er = Invoke-Bounded -Step $stepName -Exe $exe -Cwd $WorkDir -TimeoutSec $ExecTimeoutSec -ArgList $execArgs `
        -StdIn ("PIN PROMOTION PROBE. Run exactly one shell command: git --version`nThen reply with exactly this one line and nothing else: $nonce`n")
    try {
        [System.IO.File]::WriteAllText((Join-Path $EvidenceDir "exec-probe.$safeName.stdout.txt"), $er.out, [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::WriteAllText((Join-Path $EvidenceDir "exec-probe.$safeName.stderr.txt"), $er.err, [System.Text.UTF8Encoding]::new($false))
    } catch { }
    Test-AuthFailure $er $stepName
    $transcript = $er.err + "`n" + $er.out
    $okCount = [regex]::Matches($transcript, $EXEC_OK_PATTERN).Count
    $rejCount = [regex]::Matches($transcript, $EXEC_REJECTED_PATTERN).Count
    $execClass = if ($okCount -gt 0) { 'EXEC_OK' } elseif ($rejCount -gt 0) { 'EXEC_BLIND' } else { 'UNKNOWN' }
    $nonceEchoed = (Test-Path -LiteralPath $lastFile) -and ((Get-Content -LiteralPath $lastFile -Raw) -match [regex]::Escape($nonce))
    $execExtra = @{ model = $probeModel; tiers = @($probeTiersByModel[$probeModel]); execClass = $execClass
                    execOkCount = $okCount; execRejectedCount = $rejCount; nonceEchoed = $nonceEchoed }
    if ($execClass -ne 'EXEC_OK') {
        Add-Step $stepName "FAILED: $execClass" $er $execExtra
        Stop-Promotion 14 "exec probe of model '$probeModel' classified $execClass (exit $($er.exit), timedOut=$($er.timedOut), ok=$okCount, rejected=$rejCount); no pin written"
    }
    Add-Step $stepName "EXEC_OK (ok=$okCount rejected=$rejCount nonceEchoed=$nonceEchoed)" $er $execExtra
    $probeResults.Add([pscustomobject]@{ model = $probeModel; okCount = $okCount; rejCount = $rejCount })
}

# ------------------------------------------------------------------ 6. write the pin
$evidencePath = Join-Path $EvidenceDir 'promotion.json'
$execProbeText = (($probeResults | ForEach-Object { "EXEC_OK ok=$($_.okCount) rejected=$($_.rejCount) model=$($_.model)" }) -join '; ')
$pin = [ordered]@{
    schema            = 'mlv-app/codex-pin/v1'
    version           = $Version
    windowsSandbox    = $chosen
    installPrefix     = "<Invoke-Lane -CodexPinRoot, default $PinRoot>\$Version (exe $CODEX_PIN_EXE_RELATIVE); written by Promote-CodexPin.ps1"
    promotedAt        = [DateTime]::UtcNow.ToString('o')
    promotionEvidence = $evidencePath
    # MODEL-POSTURE-R16-1: the slugs exec-probed EXEC_OK on this version. Invoke-Lane runs a pinned
    # read-only lane on the pin only when its resolved model is listed here; any other model runs on
    # the global CLI (pinState PIN_MODEL_UNPROBED). probedTiers records which lane tier each slug was
    # resolved from, modelsFetchedAt the catalogue the tiers were resolved against.
    probedModels      = @($probeModels)
    probedTiers       = $probeTiersByModel
    modelsFetchedAt   = $modelsFetchedAt
    gate              = [ordered]@{
        sandboxProbe = "codex sandbox -P :read-only -- git --version rc 0 ($chosen)"
        writeProbe   = "DENIED (exit $($wr.exit), stderr '$signature'; unsandboxed control git init exit 0)"
        execProbe    = $execProbeText
    }
}
try {
    $tmp = "$PinFile.tmp$PID"
    [System.IO.File]::WriteAllText($tmp, (($pin | ConvertTo-Json -Depth 4) + "`n"), [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination $PinFile -Force
} catch {
    Stop-Promotion 16 "pin write failed: $($_.Exception.Message)"
}
$evidence.result = "PROMOTED codex $Version ($chosen sandbox, models: $(@($probeModels) -join ', ')) -> $PinFile"; $evidence.exitCode = 0
Write-Evidence
Write-Host "[promote-codex-pin] PROMOTED codex $Version ($chosen sandbox); models $(@($probeModels) -join ', '); pin $PinFile; evidence $evidencePath"
exit 0
