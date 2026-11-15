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
param([string]$ScratchRoot = '', [switch]$KeepGoing)

Set-StrictMode -Version 1.0
# -KeepGoing: run every check even when code under test lacks a function (a RED run against older code); each failing statement then shows as an error line.
$ErrorActionPreference = $(if ($KeepGoing) { 'Continue' } else { 'Stop' })
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
# The real shape of System error 5 from a .NET file call: an IOException whose HResult is HRESULT_FROM_WIN32(5), 0x80070005. The message text is not what classifies it (r3 A8).
$sysErr5 = { param($r) throw [System.IO.IOException]::new('System error 5 has occurred. Access is denied.', [int]0x80070005) }
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

# A8 (r3): the classification comes from the exception type and the Win32 code, never from message text. A missing path whose NAME contains
# 'Access is denied' is a path-not-found (UNREACHABLE).
$tricky = Join-Path $ScratchRoot 'no-such-parent\Access is denied'
Assert-That ((Get-ShareRootAccess -Root $tricky).State -ceq 'UNREACHABLE') 'A8 module: the default probe on a missing path whose name contains "Access is denied" is UNREACHABLE' "state=$((Get-ShareRootAccess -Root $tricky).State)"
$trickyThrow = { param($r) throw [System.IO.DirectoryNotFoundException]::new("Could not find a part of the path '$r'.") }
Assert-That ((Get-ShareRootAccess -Root $tricky -Probe $trickyThrow).State -ceq 'UNREACHABLE') 'A8 module: a DirectoryNotFoundException naming "...\Access is denied" is UNREACHABLE'
$trickyIo = { param($r) throw [System.IO.IOException]::new("The network path '$r' (System error 5) was not found.") }
Assert-That ((Get-ShareRootAccess -Root '\\h\Logon failure' -Probe $trickyIo).State -ceq 'UNREACHABLE') 'A8 module: an untyped IOException whose text mentions System error 5 / a logon-failure path component is UNREACHABLE'
$plainText = { param($r) throw [System.Exception]::new('Access is denied.') }
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $plainText).State -ceq 'UNREACHABLE') 'A8 module: a plain Exception with only the words "Access is denied" and no code is not classified by its text'
$hres5 = { param($r) throw [System.Exception]::new('wrapped', [System.IO.IOException]::new('x', [int]0x80070005)) }
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $hres5).State -ceq 'ACCESS_DENIED') 'A8 module: an inner IOException with HRESULT 0x80070005 is still ACCESS_DENIED'
$secErr = { param($r) throw [System.Security.SecurityException]::new('nope') }
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $secErr).State -ceq 'ACCESS_DENIED') 'A8 module: a SecurityException is ACCESS_DENIED'

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

# ---------------------------------------------------------------- adversary pass r2 (head 3f561245)
# A12: an access-denied exception wrapped deeper than 8 levels is still recognised.
$inner = [UnauthorizedAccessException]::new('denied')
$chain = $inner
for ($i = 0; $i -lt 20; $i++) { $chain = [System.Exception]::new("wrapper $i", $chain) }
Assert-That (Test-ShareAccessDeniedException -Exception $chain) 'A12 module: an UnauthorizedAccessException 20 wrappers deep is ACCESS_DENIED'
$deepProbe = { param($r) throw $chain }
Assert-That ((Get-ShareRootAccess -Root $fakeRoot -Probe $deepProbe).State -ceq 'ACCESS_DENIED') 'A12 module: the probe classifies the deep chain as ACCESS_DENIED'

# A13: every SHARE_UNREACHABLE exit of the stage driver asks the share root first (Stop-ShareUnreachable), including the cache probe and Read-Share.
$s = ''
try { Invoke-Expression (Get-ScriptFunctionText $nightly 'Stop-ShareUnreachable') } catch { Assert-That $false 'A13 nightly: Stop-ShareUnreachable is not defined' $_.Exception.Message }
Invoke-Expression (Get-ScriptFunctionText $nightly 'Read-Share')
$share = $fakeRoot
$ProbeTimeoutSec = 5
function Invoke-Bounded { param($Script, $ArgList, $Sec) [pscustomobject]@{ ok = $false; timedOut = $false; value = $null; error = 'stub: not found' } }
$TestShareRootProbe = $denied
try { Stop-ShareUnreachable 'the agent share cache did not answer' } catch { $s = [string]$_.Exception.Message }
Assert-That ($s.StartsWith('STOP|SHARE_UNREACHABLE|the agent share cache did not answer') -and $s.EndsWith("|SHARE_ACCESS_DENIED $fakeRoot")) 'A13 nightly: a denied root at the cache probe prints SHARE_ACCESS_DENIED <root> verbatim and keeps SHARE_UNREACHABLE' "got: $s"
$TestShareRootProbe = $readable
try { Stop-ShareUnreachable 'the agent share cache did not answer' } catch { $s = [string]$_.Exception.Message }
Assert-That ($s -ceq 'STOP|SHARE_UNREACHABLE|the agent share cache did not answer|') 'A13 nightly: a readable root keeps the plain SHARE_UNREACHABLE detail, no typed line' "got: $s"
$TestShareRootProbe = $notFound
try { Stop-ShareUnreachable 'x' } catch { $s = [string]$_.Exception.Message }
Assert-That ($s -ceq 'STOP|SHARE_UNREACHABLE|x|') 'A13 nightly: an unreachable (not denied) root keeps the plain detail' "got: $s"
$TestShareRootProbe = $denied
try { [void](Read-Share 'cache\build.json' { } @()) } catch { $s = [string]$_.Exception.Message }
Assert-That ($s.StartsWith('STOP|SHARE_UNREACHABLE|reading cache\build.json failed') -and $s.EndsWith("|SHARE_ACCESS_DENIED $fakeRoot")) 'A13 nightly: Read-Share (a by-name read on a denied share) prints the typed line too' "got: $s"
# A16 (pass 2): the production probe is bounded by what is left of -MaxTotalSec, not only by -ProbeTimeoutSec, and still prints the typed line.
$TestShareRootProbe = $null
$shareRootModule = Join-Path $here 'ShareRootAccess.psm1'
$ProbeTimeoutSec = 300
function Get-RemainingSec { 3 }
$script:SecSeen = -1
function Invoke-Bounded { param($Script, $ArgList, $Sec) $script:SecSeen = $Sec; [pscustomobject]@{ ok = $true; timedOut = $false; value = 'ACCESS_DENIED'; error = $null } }
$s = ''
try { Stop-ShareUnreachable 'the agent share cache did not answer' } catch { $s = [string]$_.Exception.Message }
Assert-That ($script:SecSeen -eq 3) 'A16 nightly: the root probe is bounded by the 3 s left of the total cap, not the 300 s -ProbeTimeoutSec' "seen=$($script:SecSeen)"
Assert-That ($s.EndsWith("|SHARE_ACCESS_DENIED $fakeRoot")) 'A16 nightly: the bounded production path still prints the typed line' "got: $s"
# A9 (r3): with the total cap already spent (0 s left) NO probe is started; the plain SHARE_UNREACHABLE detail is reported. The old Max(1, ...) floor
# re-opened a 1 s probe after the deadline.
$TestShareRootProbe = $null
function Get-RemainingSec { 0 }
$script:SecSeen = -1
$s = ''
try { Stop-ShareUnreachable 'the agent share cache did not answer' } catch { $s = [string]$_.Exception.Message }
Assert-That ($script:SecSeen -eq -1) 'A9 nightly: with 0 s left of the total cap the root probe is not started at all' "seen=$($script:SecSeen)"
Assert-That ($s -ceq 'STOP|SHARE_UNREACHABLE|the agent share cache did not answer|') 'A9 nightly: with the cap spent the plain SHARE_UNREACHABLE detail is reported' "got: $s"
function Get-RemainingSec { -5 }
$script:SecSeen = -1
try { Stop-ShareUnreachable 'x' } catch { $s = [string]$_.Exception.Message }
Assert-That ($script:SecSeen -eq -1) 'A9 nightly: a NEGATIVE remainder does not become a fresh 1 s probe' "seen=$($script:SecSeen)"
$nightlyText = [IO.File]::ReadAllText($nightly)
Assert-That (-not ($nightlyText -match "Stop-Nightly 'SHARE_UNREACHABLE' `"the agent share cache")) 'A13 nightly: the cache-directory probe no longer calls Stop-Nightly SHARE_UNREACHABLE directly'

Write-Host ("RESULT fails={0}" -f $script:Fails)
if ($script:Fails -gt 0) { exit 1 }
exit 0
