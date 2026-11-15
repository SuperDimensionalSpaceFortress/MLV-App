# MINI3-CUTOVER-STATE-GAPS-1: typed SHARE_ACCESS_DENIED versus 'No agent heartbeat'.
#
# A share root this host cannot read (System error 5: no SMB credential stored for it) used to surface as 'No agent heartbeat'. These tests
# drive every reader through its share-root probe seam, so no test touches a real UNC path:
#   1. ShareRootAccess.psm1: the three states (READABLE / ACCESS_DENIED / UNREACHABLE) and the typed line.
#   2. um-run.ps1, in process, with -TestShareRootProbe.
#   3. Test-AgentHeartbeat in invoke-ultramagnus-p3-evidence.ps1 and invoke-ultramagnus-cdng-export-evidence.ps1 (the function is lifted out of
#      each script with the PowerShell parser, because the scripts run their whole job when invoked).
#   4. Invoke-UmRun in dual-venue\Invoke-VenueStageNightly.ps1 (lifted the same way, its collaborators stubbed): the typed line is printed verbatim
#      first and keeps the SHARE_UNREACHABLE reason; 'No agent heartbeat' keeps its own detail.
#
# Usage: pwsh -NoProfile -File tools\profiling\test-um-run-share-root-access.ps1 [-ScratchRoot <a writable folder>]
# Exit 0 = every check passed, 1 = at least one failed.
param([string]$ScratchRoot = '')

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
if (-not $ScratchRoot) { $ScratchRoot = Join-Path ([IO.Path]::GetTempPath()) ('share-root-access-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
[void][IO.Directory]::CreateDirectory($ScratchRoot)

$script:Fails = 0
function Assert-That([bool]$Condition, [string]$Name, [string]$Detail = '') {
    if ($Condition) { Write-Host "PASS $Name" }
    else { $script:Fails++; Write-Host "FAIL $Name $Detail" }
}

function Get-ScriptFunctionText([string]$Path, [string]$Name) {
    $tokens = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errs)
    $fd = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true)
    if (-not $fd) { throw "function $Name not found in $Path" }
    return $fd.Extent.Text
}

$denied = { param($r) throw [UnauthorizedAccessException]::new('Access to the path is denied.') }
$sysErr5 = { param($r) throw [System.IO.IOException]::new('System error 5 has occurred. Access is denied.') }
$logonFail = { param($r) throw [System.ComponentModel.Win32Exception]::new(1326, 'The user name or password is incorrect.') }
$notFound = { param($r) throw [System.IO.DirectoryNotFoundException]::new('The network path was not found.') }
$readable = { param($r) }

Import-Module (Join-Path $here 'ShareRootAccess.psm1') -Force -DisableNameChecking

# ---------------------------------------------------------------- 1. the module
$fakeRoot = '\\scratch-host\scratch-share'
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $denied).State -ceq 'ACCESS_DENIED') 'module: UnauthorizedAccessException is ACCESS_DENIED'
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $sysErr5).State -ceq 'ACCESS_DENIED') 'module: "System error 5" text is ACCESS_DENIED'
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $logonFail).State -ceq 'ACCESS_DENIED') 'module: Win32 1326 (logon failure) is ACCESS_DENIED'
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $notFound).State -ceq 'UNREACHABLE') 'module: path-not-found is UNREACHABLE, not ACCESS_DENIED'
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $readable).State -ceq 'READABLE') 'module: a probe that returns is READABLE'
$realRoot = Join-Path $ScratchRoot 'real-root'
[void][IO.Directory]::CreateDirectory($realRoot)
Assert-That ((Get-ShareRootAccess -Root $realRoot).State -ceq 'READABLE') 'module: the default probe reads an existing scratch folder as READABLE'
Assert-That ((Get-ShareRootAccess -Root (Join-Path $ScratchRoot 'no-such-root')).State -ceq 'UNREACHABLE') 'module: the default probe reads a missing scratch folder as UNREACHABLE'
Assert-That ((Get-ShareAccessDeniedLine -Root $fakeRoot) -ceq "SHARE_ACCESS_DENIED $fakeRoot") 'module: typed line is "SHARE_ACCESS_DENIED <root>"'

# ---------------------------------------------------------------- 2. um-run.ps1 in process
function Invoke-UmRunCapture([scriptblock]$Probe, [string]$Root) {
    try { & (Join-Path $here 'um-run.ps1') -Command 'noop' -AgentShare $Root -TestShareRootProbe $Probe | Out-Null; return 'RETURNED' }
    catch { return [string]$_.Exception.Message }
}
$m = Invoke-UmRunCapture $denied $fakeRoot
Assert-That ($m -ceq "SHARE_ACCESS_DENIED $fakeRoot") 'um-run: a denied root throws exactly SHARE_ACCESS_DENIED <root>' "got: $m"
Assert-That ($m -notmatch 'No agent heartbeat') 'um-run: the denied case never says "No agent heartbeat"' "got: $m"
$m = Invoke-UmRunCapture $sysErr5 $fakeRoot
Assert-That ($m -ceq "SHARE_ACCESS_DENIED $fakeRoot") 'um-run: "System error 5" is typed too' "got: $m"
$m = Invoke-UmRunCapture $readable $realRoot
Assert-That ($m -match '^No agent heartbeat at ') 'um-run: a readable root with no heartbeat keeps "No agent heartbeat"' "got: $m"
Assert-That ($m -notmatch 'SHARE_ACCESS_DENIED') 'um-run: a readable root is never typed as denied' "got: $m"
$m = Invoke-UmRunCapture $notFound (Join-Path $ScratchRoot 'no-such-root')
Assert-That ($m -match '^No agent heartbeat at ') 'um-run: an unreachable (not denied) root keeps "No agent heartbeat"' "got: $m"

# ---------------------------------------------------------------- 3. the two sibling Test-AgentHeartbeat functions
$hbRoot = Join-Path $ScratchRoot 'hb-root'
[void][IO.Directory]::CreateDirectory($hbRoot)
$noHbRoot = Join-Path $ScratchRoot 'hb-root-empty'
[void][IO.Directory]::CreateDirectory($noHbRoot)
[IO.File]::WriteAllText((Join-Path $hbRoot 'heartbeat.txt'), 'alive')
foreach ($script in @('invoke-ultramagnus-p3-evidence.ps1', 'invoke-ultramagnus-cdng-export-evidence.ps1')) {
    $text = Get-ScriptFunctionText (Join-Path $here $script) 'Test-AgentHeartbeat'
    Invoke-Expression $text
    $TestShareRootProbe = $denied
    $r = Test-AgentHeartbeat -Root $fakeRoot
    Assert-That (-not $r.ok -and $r.reason -ceq "SHARE_ACCESS_DENIED $fakeRoot") "$script`: a denied root reports the typed line as the reason" "got: $($r.reason)"
    $TestShareRootProbe = $null
    $r = Test-AgentHeartbeat -Root $noHbRoot
    Assert-That (-not $r.ok -and $r.reason -ceq 'missing heartbeat') "$script`: a readable root without heartbeat.txt keeps 'missing heartbeat'" "got: $($r.reason)"
    $r = Test-AgentHeartbeat -Root $hbRoot
    Assert-That ($r.ok -and $r.reason -ceq 'ok') "$script`: a readable root with a fresh heartbeat is ok" "got: $($r.reason)"
}

# ---------------------------------------------------------------- 4. Invoke-VenueStageNightly: Invoke-UmRun maps the typed line
$nightly = Join-Path $here 'dual-venue\Invoke-VenueStageNightly.ps1'
Invoke-Expression (Get-ScriptFunctionText $nightly 'Invoke-UmRun')
Invoke-Expression (Get-ScriptFunctionText $nightly 'Get-LastLine')
function Assert-VenueFree { param($Text) }
function Assert-StagePackageSideFile { param($Side, $JobId, $FailToken) }
function Get-RemainingSec { 1000 }
function Get-DvJobNotAfter { param($TimeoutSec, $MaxQueueWaitSec, $MaxAdmissionWaitSec, $MaxClaimedWaitSec, $CallerCapSec, $CallerDeadlineUtc, [switch]$LaterHarvest) [pscustomobject]@{ budgetSec = $null; capUtc = ''; notAfterUtc = ''; source = 'stub' } }
function Say { param($Text) }
function Test-UmRunJobIdInUseMessage { param($Message, $JobId) $false }
function Stop-Nightly { param([string]$Reason, [string]$Detail = '', [string[]]$Verbatim = @(), [string]$Manifest = '') throw ('STOP|' + $Reason + '|' + $Detail + '|' + (@($Verbatim) -join ';')) }
$UmRun = 'um-run-stub.ps1'; $MaxTotalSec = 3600; $TestNotAfterSampleDelayMs = 0
$script:Deadline = [DateTime]::UtcNow.AddHours(1)
function Invoke-Child { param($Name, $A, $B, $CommandText) [pscustomobject]@{ timedOut = $false; exit = 1; out = $script:ChildOut; err = '' } }
function Invoke-StubbedUmRun([string]$ChildOut) {
    $script:ChildOut = $ChildOut
    try { [void](Invoke-UmRun -Step 'stage' -JobFile 'job.ps1' -Side @() -JobId 'J1' -Share $fakeRoot -JobTimeoutSec 60 -FailToken 'STAGE_FAILED'); return 'NOSTOP' }
    catch { return [string]$_.Exception.Message }
}
$s = Invoke-StubbedUmRun "UMRUN_ERROR SHARE_ACCESS_DENIED $fakeRoot"
Assert-That ($s.StartsWith('STOP|SHARE_UNREACHABLE|')) 'nightly: the typed line keeps the SHARE_UNREACHABLE reason (exit 41)' "got: $s"
Assert-That ($s.EndsWith("|SHARE_ACCESS_DENIED $fakeRoot")) 'nightly: the typed line is printed verbatim FIRST (Verbatim = SHARE_ACCESS_DENIED <root>)' "got: $s"
Assert-That ($s -notmatch 'No agent heartbeat') 'nightly: the denied case never carries "No agent heartbeat"' "got: $s"
$s = Invoke-StubbedUmRun "UMRUN_ERROR No agent heartbeat at $fakeRoot\heartbeat.txt - run install first."
Assert-That ($s.StartsWith('STOP|SHARE_UNREACHABLE|') -and $s -match 'No agent heartbeat' -and $s -notmatch 'SHARE_ACCESS_DENIED') 'nightly: a readable share with no heartbeat keeps "No agent heartbeat" and no typed line' "got: $s"

Write-Host ("RESULT fails={0}" -f $script:Fails)
if ($script:Fails -gt 0) { exit 1 }
exit 0
