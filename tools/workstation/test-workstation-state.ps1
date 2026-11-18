# MINI3-CUTOVER-STATE-GAPS-1: tests for the Verify-phase share preflight and the cut-over state gaps in Setup-MlvWorkstation.ps1.
#
#   share preflight   Verify fails closed (exit 1, a typed SHARE_PREFLIGHT_FAILED line per missing heartbeat) and passes on two existing files.
#   gap (a) claims    Import quarantines a claim whose run dir is absent and which is under 24 h old; everything else stays.
#   gap (b) doctrine  Export carries .codex-state\doctrine; Import copies it and never overwrites a newer local file.
#   gap (c) meters    Verify runs -Install of MLV-DiskAutoReclaim and MLV-PwshAutoRepin; an SBP-launcher-absent throw reports
#                     METER_INSTALL_UNSUPPORTED_HOST and goes on; never on the source host without -Force, never under -WhatIf.
#
# Nothing here touches a real UNC path, the real C:\ProgramData\MlvWorkstation or a real home folder: every run gets a scratch -Workspace,
# -StateRoot and USERPROFILE, and the two heartbeat paths point at scratch files.
#
# Usage: pwsh -NoProfile -File tools\workstation\test-workstation-state.ps1 [-ScratchRoot <a writable folder>]
# Exit 0 = every check passed, 1 = at least one failed.
param([string]$ScratchRoot = '', [switch]$KeepGoing)

Set-StrictMode -Version 1.0
# -KeepGoing: run every check even when code under test lacks a function (a RED run against older code); each failing statement then shows as FAIL lines.
$ErrorActionPreference = $(if ($KeepGoing) { 'Continue' } else { 'Stop' })
$setup = Join-Path $PSScriptRoot 'Setup-MlvWorkstation.ps1'
if (-not $ScratchRoot) { $ScratchRoot = Join-Path ([IO.Path]::GetTempPath()) ('workstation-state-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
[void][IO.Directory]::CreateDirectory($ScratchRoot)
$pwshExe = (Get-Command pwsh).Source

$script:Fails = 0
function Assert-That([bool]$Condition, [string]$Name, [string]$Detail = '') {
    if ($Condition) { Write-Host "PASS $Name" } else { $script:Fails++; Write-Host "FAIL $Name $Detail" }
}

# True when the setup script defines the function; otherwise one FAIL line (an older script, or a regression) and the section is skipped.
function Test-Has([string]$Fn, [string]$Section) {
    if (Get-Command -Name $Fn -CommandType Function -ErrorAction SilentlyContinue) { return $true }
    Assert-That $false "$Section`: function $Fn is not defined"
    return $false
}

function New-Scratch([string]$Name) {
    $d = Join-Path $ScratchRoot $Name
    [void][IO.Directory]::CreateDirectory($d)
    return $d
}

# Runs Setup-MlvWorkstation.ps1 as a child process under a scratch USERPROFILE. Returns @{ Exit; Out }.
$script:AutoScratchLock = $true
function Invoke-Setup([string]$Home_, [string[]]$SetupArgs) {
    $saved = $env:USERPROFILE
    try {
        $env:USERPROFILE = $Home_
        # p5 A6: a non-default -LockRoot is refused unless the run passes the explicit test seam -AllowScratchLockRoot; the helper adds it
        # (tests use scratch lock folders) except where a test turns $script:AutoScratchLock off to prove the refusal.
        if ($script:AutoScratchLock -and ($SetupArgs -contains '-LockRoot') -and ($SetupArgs -notcontains '-AllowScratchLockRoot')) { $SetupArgs = @($SetupArgs) + '-AllowScratchLockRoot' }
        $out = @(& $pwshExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $setup @SetupArgs 2>&1 | ForEach-Object { "$_" })
        return @{ Exit = $LASTEXITCODE; Out = ($out -join "`n") }
    } finally { $env:USERPROFILE = $saved }
}

$ultra = Join-Path $ScratchRoot 'um-heartbeat.txt'
$bach = Join-Path $ScratchRoot 'bachelor-heartbeat.txt'
[IO.File]::WriteAllText($ultra, 'alive')
[IO.File]::WriteAllText($bach, 'alive')
$missingUltra = Join-Path $ScratchRoot 'absent\um-heartbeat.txt'
$missingBach = Join-Path $ScratchRoot 'absent\bachelor-heartbeat.txt'

# ---------------------------------------------------------------- share preflight: the function (dot-sourced, no phase runs)
$fnState = New-Scratch 'fn-state'
$fnWs = New-Scratch 'fn-ws'
$fnLoaded = $true
try { . $setup -Phase Verify -StateRoot $fnState -Workspace $fnWs -UltraMagnusHeartbeatPath $ultra -BachelorHeartbeatPath $bach }
catch { $fnLoaded = $false; Assert-That $false 'setup script loads by dot-sourcing (functions only, no phase runs)' $_.Exception.Message }
if ($fnLoaded) {
    $r = @(Test-ShareHeartbeatPreflight -UltraMagnusPath $ultra -BachelorPath $bach)
    Assert-That ($r.Count -eq 0) 'preflight fn: two existing scratch files pass' "failed=$($r -join ',')"
    $r = @(Test-ShareHeartbeatPreflight -UltraMagnusPath $missingUltra -BachelorPath $bach)
    Assert-That ($r.Count -eq 1 -and $r[0] -ceq $missingUltra) 'preflight fn: a missing Ultra-Magnus heartbeat fails, naming that path' "failed=$($r -join ',')"
    $r = @(Test-ShareHeartbeatPreflight -UltraMagnusPath $ultra -BachelorPath $missingBach)
    Assert-That ($r.Count -eq 1 -and $r[0] -ceq $missingBach) 'preflight fn: a missing bachelor heartbeat fails, naming that path' "failed=$($r -join ',')"
    $r = @(Test-ShareHeartbeatPreflight -UltraMagnusPath $missingUltra -BachelorPath $missingBach)
    Assert-That ($r.Count -eq 2) 'preflight fn: both missing = two failures' "failed=$($r -join ',')"
    $r = @(Test-ShareHeartbeatPreflight -UltraMagnusPath $ScratchRoot -BachelorPath $bach)
    Assert-That ($r.Count -eq 1) 'preflight fn: a folder where the heartbeat file should be is a failure (Leaf only)' "failed=$($r -join ',')"
}

# ---------------------------------------------------------------- share preflight: Verify end to end (exit code + typed line)
$home1 = New-Scratch 'home1'
$st1 = New-Scratch 'state1'
$ws1 = New-Scratch 'ws1'
$base = @('-Phase', 'Verify', '-StateRoot', $st1, '-Workspace', $ws1, '-SourceHost', 'NOT-THIS-HOST')
$v = Invoke-Setup $home1 ($base + @('-UltraMagnusHeartbeatPath', $ultra, '-BachelorHeartbeatPath', $bach))
Assert-That ($v.Exit -eq 0 -and $v.Out -notmatch 'SHARE_PREFLIGHT_FAILED') 'verify: both heartbeat files readable -> exit 0, no typed failure' "exit=$($v.Exit)`n$($v.Out)"
$v = Invoke-Setup $home1 ($base + @('-UltraMagnusHeartbeatPath', $missingUltra, '-BachelorHeartbeatPath', $bach))
Assert-That ($v.Exit -eq 1) 'verify: a missing Ultra-Magnus heartbeat fails closed (exit 1)' "exit=$($v.Exit)"
Assert-That ($v.Out.Contains("SHARE_PREFLIGHT_FAILED $missingUltra (Ultra-Magnus share; tools\workstation\README.md step 6b)")) 'verify: the typed line names the path, the share and the README step' $v.Out
$v = Invoke-Setup $home1 ($base + @('-UltraMagnusHeartbeatPath', $ultra, '-BachelorHeartbeatPath', $missingBach))
Assert-That ($v.Exit -eq 1 -and $v.Out.Contains("SHARE_PREFLIGHT_FAILED $missingBach (bachelor share; tools\workstation\README.md step 6b)")) 'verify: a missing bachelor heartbeat fails closed with its own typed line' "exit=$($v.Exit)`n$($v.Out)"
$v = Invoke-Setup $home1 ($base + @('-UltraMagnusHeartbeatPath', $missingUltra, '-BachelorHeartbeatPath', $missingBach))
Assert-That ($v.Exit -eq 1 -and ([regex]::Matches($v.Out, 'SHARE_PREFLIGHT_FAILED').Count -ge 2)) 'verify: both missing -> exit 1 and a typed line for each' "exit=$($v.Exit)"

# ---------------------------------------------------------------- gap (a): foreign claims quarantined (function)
$claimsDir = Join-Path (New-Scratch 'fn-ws2') 'claims'
[void][IO.Directory]::CreateDirectory($claimsDir)
$liveRunDir = New-Scratch 'live-run-dir'
function New-Claim([string]$Name, [string]$RunDir, [double]$AgeHours) {
    $utc = [DateTime]::UtcNow.AddHours(-$AgeHours).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
    $json = '{"utc":"' + $utc + '","pid":1,"runDir":"' + ($RunDir -replace '\\', '\\') + '","card":"X","lane":"sonnet","promptSha256":"00","subjectSha":""}'
    [IO.File]::WriteAllText((Join-Path $claimsDir $Name), $json)
}
New-Claim 'foreign-fresh.claim' (Join-Path $ScratchRoot 'absent-run-dir') 1
New-Claim 'foreign-old.claim' (Join-Path $ScratchRoot 'absent-run-dir') 30
New-Claim 'local-fresh.claim' $liveRunDir 1
[IO.File]::WriteAllText((Join-Path $claimsDir 'malformed.claim'), 'this is not json')
[IO.File]::WriteAllText((Join-Path $claimsDir 'no-rundir.claim'), '{"utc":"' + [DateTime]::UtcNow.ToString('o') + '"}')
if ($fnLoaded) {
    $moved = @(Move-ForeignClaims -ClaimsDir $claimsDir -Stamp 'TESTSTAMP')
    $dest = Join-Path $claimsDir 'migrated-foreign-TESTSTAMP'
    Assert-That ($moved.Count -eq 1 -and $moved[0] -ceq 'foreign-fresh.claim') 'claims fn: exactly the fresh claim with an absent run dir is quarantined' "moved=$($moved -join ',')"
    Assert-That ((Test-Path -LiteralPath (Join-Path $dest 'foreign-fresh.claim')) -and -not (Test-Path -LiteralPath (Join-Path $claimsDir 'foreign-fresh.claim'))) 'claims fn: it was moved (not copied, not deleted)'
    foreach ($keep in 'foreign-old.claim', 'local-fresh.claim', 'malformed.claim', 'no-rundir.claim') {
        Assert-That (Test-Path -LiteralPath (Join-Path $claimsDir $keep)) "claims fn: $keep stays in place"
    }
} else { Assert-That $false 'claims fn: not run, the setup script did not load' }

# ---------------------------------------------------------------- gap (a) + (b): Import end to end from a scratch bundle
$home2 = New-Scratch 'home2'
$st2 = New-Scratch 'state2'
$ws2 = New-Scratch 'ws2'
$repo2 = Join-Path $ws2 'MLV-App'
$bundle = New-Scratch 'bundle'
[IO.File]::WriteAllText((Join-Path $bundle 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
[void][IO.Directory]::CreateDirectory((Join-Path $bundle 'home-claude'))
[IO.File]::WriteAllText((Join-Path $bundle 'home-claude\settings.json'), '{}')
$bClaims = Join-Path $bundle 'project-claude-state\hub-autopilot\claims'
[void][IO.Directory]::CreateDirectory($bClaims)
$liveRun2 = New-Scratch 'live-run-dir2'
$utcNow = [DateTime]::UtcNow.AddHours(-1).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
[IO.File]::WriteAllText((Join-Path $bClaims 'foreign.claim'), '{"utc":"' + $utcNow + '","runDir":"' + ((Join-Path $ScratchRoot 'absent-run-dir2') -replace '\\', '\\') + '"}')
[IO.File]::WriteAllText((Join-Path $bClaims 'local.claim'), '{"utc":"' + $utcNow + '","runDir":"' + ($liveRun2 -replace '\\', '\\') + '"}')
$bDoctrine = Join-Path $bundle 'project-codex-state\doctrine'
[void][IO.Directory]::CreateDirectory($bDoctrine)
[IO.File]::WriteAllText((Join-Path $bDoctrine 'last-seen.json'), 'from-bundle')
[IO.File]::WriteAllText((Join-Path $bDoctrine 'only-in-bundle.json'), 'from-bundle')
(Get-Item -LiteralPath (Join-Path $bDoctrine 'last-seen.json')).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-2)
# The new host already advanced its own fold marker: that newer file must survive the import.
$localDoctrine = Join-Path $repo2 '.codex-state\doctrine'
[void][IO.Directory]::CreateDirectory($localDoctrine)
[IO.File]::WriteAllText((Join-Path $localDoctrine 'last-seen.json'), 'local-newer')
$imp = Invoke-Setup $home2 @('-Phase', 'Import', '-StateRoot', $st2, '-Workspace', $ws2, '-BundlePath', $bundle, '-SourceHost', 'NOT-THIS-HOST')
Assert-That ($imp.Exit -eq 0) 'import: the scratch bundle imports (exit 0)' "exit=$($imp.Exit)`n$($imp.Out)"
$liveClaims = Join-Path $repo2 '.claude-state\hub-autopilot\claims'
Assert-That ((Test-Path -LiteralPath (Join-Path $liveClaims 'local.claim')) -and -not (Test-Path -LiteralPath (Join-Path $liveClaims 'foreign.claim'))) 'import (a): the claim with an absent run dir left claims\, the one with a live run dir stayed'
$q = @(Get-ChildItem -LiteralPath $liveClaims -Directory -Filter 'migrated-foreign-*' -ErrorAction SilentlyContinue)
Assert-That ($q.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $q[0].FullName 'foreign.claim'))) 'import (a): the foreign claim is under claims\migrated-foreign-<stamp>' "dirs=$($q.Name -join ',')"
Assert-That ((Get-Content -LiteralPath (Join-Path $localDoctrine 'last-seen.json') -Raw).Trim() -ceq 'local-newer') 'import (b): a newer local last-seen.json is never overwritten'
Assert-That ((Test-Path -LiteralPath (Join-Path $localDoctrine 'only-in-bundle.json')) -and ((Get-Content -LiteralPath (Join-Path $localDoctrine 'only-in-bundle.json') -Raw).Trim() -ceq 'from-bundle')) 'import (b): a doctrine file the host lacks is carried'
# And on a host with no marker at all, the bundle's copy lands.
$ws3 = New-Scratch 'ws3'
$imp3 = Invoke-Setup (New-Scratch 'home3') @('-Phase', 'Import', '-StateRoot', (New-Scratch 'state3'), '-Workspace', $ws3, '-BundlePath', $bundle, '-SourceHost', 'NOT-THIS-HOST')
$fresh = Join-Path $ws3 'MLV-App\.codex-state\doctrine\last-seen.json'
Assert-That ($imp3.Exit -eq 0 -and (Test-Path -LiteralPath $fresh) -and ((Get-Content -LiteralPath $fresh -Raw).Trim() -ceq 'from-bundle')) 'import (b): a host with no fold marker gets the bundle copy' "exit=$($imp3.Exit)`n$($imp3.Out)"

# ---------------------------------------------------------------- gap (b): Export carries .codex-state\doctrine
$home4 = New-Scratch 'home4'
$ws4 = New-Scratch 'ws4'
$repo4 = Join-Path $ws4 'MLV-App'
[void][IO.Directory]::CreateDirectory((Join-Path $repo4 '.codex-state\doctrine'))
[IO.File]::WriteAllText((Join-Path $repo4 '.codex-state\doctrine\last-seen.json'), 'export-marker')
[IO.File]::WriteAllText((Join-Path $repo4 '.codex-state\session-state.json'), 'must-not-travel')
$exportDir = New-Scratch 'export-out'
& git -C $repo4 init --quiet 2>&1 | Out-Null
$exp = Invoke-Setup $home4 @('-Phase', 'Export', '-StateRoot', (New-Scratch 'state4'), '-Workspace', $ws4, '-ExportDir', $exportDir)
$stage = @(Get-ChildItem -LiteralPath $exportDir -Directory -Filter 'bundle-*' -ErrorAction SilentlyContinue | Select-Object -First 1)
$carried = if ($stage.Count) { Join-Path $stage[0].FullName 'project-codex-state\doctrine\last-seen.json' } else { '' }
Assert-That ($carried -and (Test-Path -LiteralPath $carried) -and ((Get-Content -LiteralPath $carried -Raw).Trim() -ceq 'export-marker')) 'export (b): the doctrine fold marker is staged in the bundle' "exit=$($exp.Exit)`n$($exp.Out)"
Assert-That ($stage.Count -gt 0 -and -not (Test-Path -LiteralPath (Join-Path $stage[0].FullName 'project-codex-state\session-state.json'))) 'export (b): only the doctrine folder of .codex-state travels'

# ---------------------------------------------------------------- gap (c): Verify runs the two meter -Install scripts
function New-MeterRepo([string]$Name, [string]$DiskBody, [string]$RepinBody) {
    $ws = New-Scratch $Name
    $cs = Join-Path $ws 'MLV-App\.claude-state'
    [void][IO.Directory]::CreateDirectory((Join-Path $cs 'disk-hygiene'))
    [void][IO.Directory]::CreateDirectory((Join-Path $cs 'hub-autopilot'))
    [IO.File]::WriteAllText((Join-Path $cs 'disk-hygiene\disk-reclaim-task.ps1'), $DiskBody)
    [IO.File]::WriteAllText((Join-Path $cs 'hub-autopilot\pwsh-autorepin.ps1'), $RepinBody)
    return $ws
}
$okBody = 'param([switch]$Install) if ($Install) { [IO.File]::WriteAllText((Join-Path $PSScriptRoot "installed.marker"), "ran -Install"); exit 0 } exit 5'
$sbpBody = 'param([switch]$Install) throw ''Approved SBP windowless launcher is absent or differs.'''
$failBody = 'param([switch]$Install) Write-Output "disk is full"; exit 3'
$okPaths = @('-UltraMagnusHeartbeatPath', $ultra, '-BachelorHeartbeatPath', $bach)

$wsA = New-MeterRepo 'ws-meter-a' $okBody $okBody
$mA = Invoke-Setup (New-Scratch 'home-a') (@('-Phase', 'Verify', '-StateRoot', (New-Scratch 'state-a'), '-Workspace', $wsA, '-SourceHost', 'NOT-THIS-HOST') + $okPaths)
Assert-That ($mA.Exit -eq 0 -and (Test-Path -LiteralPath (Join-Path $wsA 'MLV-App\.claude-state\disk-hygiene\installed.marker')) -and (Test-Path -LiteralPath (Join-Path $wsA 'MLV-App\.claude-state\hub-autopilot\installed.marker'))) 'meters (c): Verify ran -Install of BOTH meters' "exit=$($mA.Exit)`n$($mA.Out)"

$wsB = New-MeterRepo 'ws-meter-b' $sbpBody $okBody
$mB = Invoke-Setup (New-Scratch 'home-b') (@('-Phase', 'Verify', '-StateRoot', (New-Scratch 'state-b'), '-Workspace', $wsB, '-SourceHost', 'NOT-THIS-HOST') + $okPaths)
Assert-That ($mB.Out.Contains('METER_INSTALL_UNSUPPORTED_HOST MLV-DiskAutoReclaim')) 'meters (c): an SBP-launcher-absent throw reports METER_INSTALL_UNSUPPORTED_HOST' $mB.Out
Assert-That ($mB.Exit -eq 0 -and (Test-Path -LiteralPath (Join-Path $wsB 'MLV-App\.claude-state\hub-autopilot\installed.marker'))) 'meters (c): Verify goes on to the next meter and still exits 0' "exit=$($mB.Exit)"

$wsC = New-MeterRepo 'ws-meter-c' $failBody $okBody
$mC = Invoke-Setup (New-Scratch 'home-c') (@('-Phase', 'Verify', '-StateRoot', (New-Scratch 'state-c'), '-Workspace', $wsC, '-SourceHost', 'NOT-THIS-HOST') + $okPaths)
Assert-That ($mC.Exit -eq 1 -and $mC.Out.Contains('METER_INSTALL_FAILED MLV-DiskAutoReclaim')) 'meters (c): any other -Install failure is METER_INSTALL_FAILED and Verify exits 1' "exit=$($mC.Exit)`n$($mC.Out)"

$wsD = New-MeterRepo 'ws-meter-d' $okBody $okBody
$mD = Invoke-Setup (New-Scratch 'home-d') (@('-Phase', 'Verify', '-StateRoot', (New-Scratch 'state-d'), '-Workspace', $wsD, '-SourceHost', $env:COMPUTERNAME) + $okPaths)
Assert-That ($mD.Exit -eq 0 -and -not (Test-Path -LiteralPath (Join-Path $wsD 'MLV-App\.claude-state\disk-hygiene\installed.marker'))) 'meters (c): on the source host without -Force no -Install runs' "exit=$($mD.Exit)`n$($mD.Out)"
$mD2 = Invoke-Setup (New-Scratch 'home-d2') (@('-Phase', 'Verify', '-StateRoot', (New-Scratch 'state-d2'), '-Workspace', $wsD, '-SourceHost', $env:COMPUTERNAME, '-Force') + $okPaths)
Assert-That ($mD2.Exit -eq 0 -and (Test-Path -LiteralPath (Join-Path $wsD 'MLV-App\.claude-state\disk-hygiene\installed.marker'))) 'meters (c): -Force runs them on the source host' "exit=$($mD2.Exit)`n$($mD2.Out)"

$wsE = New-MeterRepo 'ws-meter-e' $okBody $okBody
$mE = Invoke-Setup (New-Scratch 'home-e') (@('-Phase', 'Verify', '-WhatIf', '-StateRoot', (New-Scratch 'state-e'), '-Workspace', $wsE, '-SourceHost', 'NOT-THIS-HOST') + $okPaths)
Assert-That ($mE.Exit -eq 0 -and -not (Test-Path -LiteralPath (Join-Path $wsE 'MLV-App\.claude-state\disk-hygiene\installed.marker'))) 'meters (c): under -WhatIf no -Install runs' "exit=$($mE.Exit)`n$($mE.Out)"

$wsF = New-Scratch 'ws-meter-f'
$mF = Invoke-Setup (New-Scratch 'home-f') (@('-Phase', 'Verify', '-StateRoot', (New-Scratch 'state-f'), '-Workspace', $wsF, '-SourceHost', 'NOT-THIS-HOST') + $okPaths)
Assert-That ($mF.Exit -eq 0 -and $mF.Out -match 'script absent') 'meters (c): an absent meter script is a warning, not a failure' "exit=$($mF.Exit)`n$($mF.Out)"

# ================================================================ adversary pass r2 (head 3f561245): one section per finding class
# ---------------------------------------------------------------- A1: the preflight opens the heartbeat for reading
if ($fnLoaded) {
    $denyFile = Join-Path $ScratchRoot 'um-heartbeat-denied.txt'
    [IO.File]::WriteAllText($denyFile, 'alive')
    & icacls.exe $denyFile /deny '*S-1-1-0:(RD)' 2>&1 | Out-Null
    try {
        $existsAsLeaf = Test-Path -LiteralPath $denyFile -PathType Leaf
        $r = @(Test-ShareHeartbeatPreflight -UltraMagnusPath $denyFile -BachelorPath $bach)
        Assert-That ($existsAsLeaf -and $r.Count -eq 1 -and $r[0] -ceq $denyFile) 'A1 preflight: a heartbeat that exists but cannot be read fails (Test-Path alone says it is fine)' "exists=$existsAsLeaf failed=$($r -join ',')"
    } finally { & icacls.exe $denyFile /remove:d '*S-1-1-0' 2>&1 | Out-Null }
}

# ---------------------------------------------------------------- A11: claims with a non-string or relative runDir are malformed
$claimsDir5 = Join-Path (New-Scratch 'fn-ws5') 'claims'
[void][IO.Directory]::CreateDirectory($claimsDir5)
$nowUtc = [DateTime]::UtcNow.AddHours(-1).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
[IO.File]::WriteAllText((Join-Path $claimsDir5 'obj-rundir.claim'), '{"utc":"' + $nowUtc + '","runDir":{"x":1}}')
[IO.File]::WriteAllText((Join-Path $claimsDir5 'rel-rundir.claim'), '{"utc":"' + $nowUtc + '","runDir":"relative\\path"}')
[IO.File]::WriteAllText((Join-Path $claimsDir5 'arr-rundir.claim'), '{"utc":"' + $nowUtc + '","runDir":["C:\\nowhere"]}')
if ($fnLoaded) {
    $moved5 = @(Move-ForeignClaims -ClaimsDir $claimsDir5 -Stamp 'T5')
    Assert-That ($moved5.Count -eq 0 -and (Test-Path -LiteralPath (Join-Path $claimsDir5 'obj-rundir.claim')) -and (Test-Path -LiteralPath (Join-Path $claimsDir5 'rel-rundir.claim')) -and (Test-Path -LiteralPath (Join-Path $claimsDir5 'arr-rundir.claim'))) 'A11 claims: an object, array or relative runDir is malformed and stays in place' "moved=$($moved5 -join ',')"
}

# ---------------------------------------------------------------- A2, A3: the secret scan covers every staged size and fails closed on an unreadable file
if ($fnLoaded -and (Test-Has 'Invoke-StagedSecretScan' 'A2/A3 scan')) {
    $scanStage = New-Scratch 'scan-stage'
    $token = 'gh' + 'p_' + ('A' * 40)
    $atBoundary = Join-Path $scanStage 'boundary.txt'
    [IO.File]::WriteAllText($atBoundary, ($token + ('x' * (5242880 - $token.Length))))
    $clean = Join-Path $scanStage 'clean.txt'
    [IO.File]::WriteAllText($clean, 'nothing secret here')
    $res = Invoke-StagedSecretScan -Stage $scanStage
    Assert-That ($null -eq (Get-Item -LiteralPath $atBoundary -ErrorAction SilentlyContinue) -and $res.Removed.Count -eq 1) 'A2 scan: a token inside a file of exactly 5 MiB is found and the file removed' "removed=$($res.Removed -join ',')"
    Assert-That ((Test-Path -LiteralPath $clean) -and $res.Uncleared.Count -eq 0) 'A2 scan: a clean file is kept and nothing is left uncleared'
    $bigStage = New-Scratch 'scan-stage-big'
    [IO.File]::WriteAllText((Join-Path $bigStage 'big.txt'), ('y' * 2000))
    $res = Invoke-StagedSecretScan -Stage $bigStage -MaxScanBytes 1000
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $bigStage 'big.txt')) -and $res.Removed.Count -eq 1) 'A2 scan: a file larger than the scan limit is removed, not archived unscanned' "removed=$($res.Removed -join ',')"
    $lockStage = New-Scratch 'scan-stage-lock'
    $lockedFile = Join-Path $lockStage 'locked.txt'
    [IO.File]::WriteAllText($lockedFile, $token)
    $holder = [IO.File]::Open($lockedFile, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { $res = Invoke-StagedSecretScan -Stage $lockStage } finally { $holder.Dispose() }
    Assert-That ($res.Uncleared.Count -eq 1 -and $res.Uncleared[0] -match 'locked.txt \(unreadable') 'A3 scan: a file that cannot be read is NOT scanned as empty text; it is reported uncleared (the export then writes no bundle)' "uncleared=$($res.Uncleared -join ',')"
}

# ---------------------------------------------------------------- A4: Import never overwrites credentials
$home6 = New-Scratch 'home6'
[void][IO.Directory]::CreateDirectory((Join-Path $home6 '.codex'))
[IO.File]::WriteAllText((Join-Path $home6 '.codex\auth.json'), 'dest-secret')
$bundle6 = New-Scratch 'bundle6'
[IO.File]::WriteAllText((Join-Path $bundle6 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
[void][IO.Directory]::CreateDirectory((Join-Path $bundle6 'home-claude'))
[IO.File]::WriteAllText((Join-Path $bundle6 'home-claude\settings.json'), '{}')
[IO.File]::WriteAllText((Join-Path $bundle6 'home-claude\.credentials.json'), '{"from":"bundle"}')
[void][IO.Directory]::CreateDirectory((Join-Path $bundle6 'home-codex'))
[IO.File]::WriteAllText((Join-Path $bundle6 'home-codex\auth.json'), 'bundle-secret')
[IO.File]::WriteAllText((Join-Path $bundle6 'home-codex\AGENTS.md'), 'agents')
$imp6 = Invoke-Setup $home6 @('-Phase', 'Import', '-StateRoot', (New-Scratch 'state6'), '-Workspace', (New-Scratch 'ws6'), '-BundlePath', $bundle6, '-SourceHost', 'NOT-THIS-HOST')
Assert-That ($imp6.Exit -eq 0 -and (Get-Content -LiteralPath (Join-Path $home6 '.codex\auth.json') -Raw).Trim() -ceq 'dest-secret') 'A4 import: an auth.json in the bundle never overwrites the host''s' "exit=$($imp6.Exit)`n$($imp6.Out)"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $home6 '.claude\.credentials.json'))) 'A4 import: a .credentials.json in the bundle is not copied'
Assert-That ((Test-Path -LiteralPath (Join-Path $home6 '.codex\AGENTS.md'))) 'A4 import: the non-credential files of the same folder still come across'

# ---------------------------------------------------------------- A5, A6: key-only sshd_config
if ($fnLoaded -and (Test-Has 'Set-SshdKeyOnlyConfig' 'A5/A6 sshd')) {
    $sshDir = New-Scratch 'sshd'
    $cfgBad = Join-Path $sshDir 'sshd_config_bad'
    $badText = "PasswordAuthentication yes`r`nPort 22`r`nMatch User obabalola`r`n    PasswordAuthentication yes`r`n"
    [IO.File]::WriteAllText($cfgBad, $badText)
    $bk = Join-Path $sshDir 'no-such-backup-dir\deeper'
    $threw = $false
    try { Set-SshdKeyOnlyConfig -ConfigPath $cfgBad -BackupDir $bk -Stamp 'S1' -Validate { 0 } } catch { $threw = ($_.Exception.Message -match 'Match block') }
    Assert-That ($threw -and ([IO.File]::ReadAllText($cfgBad) -ceq $badText) -and -not (Test-Path -LiteralPath $bk)) 'A5 sshd: a Match block that re-enables password auth is refused before anything is written'
    $cfgOk = Join-Path $sshDir 'sshd_config_ok'
    [IO.File]::WriteAllText($cfgOk, "#PasswordAuthentication yes`r`nPort 22`r`n")
    Set-SshdKeyOnlyConfig -ConfigPath $cfgOk -BackupDir $bk -Stamp 'S2' -Validate { 0 }
    $okText = [IO.File]::ReadAllText($cfgOk)
    Assert-That ($okText -match '(?m)^PasswordAuthentication no' -and $okText -match '(?m)^PubkeyAuthentication yes' -and (Test-Path -LiteralPath (Join-Path $bk 'sshd_config.S2'))) 'A6 sshd: the backup folder is created, the config goes key-only, the backup exists' $okText
    $cfgRej = Join-Path $sshDir 'sshd_config_rej'
    $rejText = "PasswordAuthentication yes`r`nPort 22`r`n"
    [IO.File]::WriteAllText($cfgRej, $rejText)
    $threw = $false
    try { Set-SshdKeyOnlyConfig -ConfigPath $cfgRej -BackupDir $bk -Stamp 'S3' -Validate { 1 } } catch { $threw = ($_.Exception.Message -match 'rejected') }
    Assert-That ($threw -and ([IO.File]::ReadAllText($cfgRej) -ceq $rejText)) 'A6 sshd: a validator rejection restores the original config and throws'
}

# ---------------------------------------------------------------- A7: scheduled tasks carry the path overrides they were registered with
if ($fnLoaded -and (Test-Has 'Get-GateTaskArgument' 'A7 tasks')) {
    $spec = @{ Name = 'T'; Script = 'C:\x\y.ps1'; ArgLine = 'Quiet'; Mode = 'Local' }
    $a = Get-GateTaskArgument -Pwsh 'C:\pwsh.exe' -Self "$fnState\Setup-MlvWorkstation.ps1" -Spec $spec
    Assert-That ($a.Contains("-StateRoot `"$fnState`"") -and $a.Contains("-Workspace `"$fnWs`"") -and -not $a.Contains('-MlvTmp')) 'A7 tasks: -StateRoot and -Workspace given to the registering run reach the task command line (and an unset -MlvTmp does not)' $a
    $saved = $script:SetupBound
    $script:SetupBound = @{}
    $a2 = Get-GateTaskArgument -Pwsh 'C:\pwsh.exe' -Self 'C:\s.ps1' -Spec $spec
    $script:SetupBound = $saved
    Assert-That (-not $a2.Contains('-StateRoot') -and -not $a2.Contains('-Workspace') -and $a2.Contains('-GateArgLine "Quiet"')) 'A7 tasks: with no overrides the command line is the one it always was'
}

# ---------------------------------------------------------------- A8, A15: the lease cannot be released under a running gate; forcing needs a reason
function New-LeaseWorkspace([string]$Name, [string]$Holder) {
    $ws = New-Scratch $Name
    $repo = Join-Path $ws 'MLV-App'
    $bare = Join-Path $ws 'fork.git'
    & git init --bare -q $bare 2>&1 | Out-Null
    & git init -q $repo 2>&1 | Out-Null
    [IO.File]::WriteAllText((Join-Path $repo 'HUB-LEASE.json'), ('{"schema":"mlv-app/hub-lease/v1","holder":"' + $Holder + '","setBy":"test","setUtc":"2026-01-01T00:00:00Z","reason":"seed"}'))
    & git -C $repo add HUB-LEASE.json 2>&1 | Out-Null
    & git -C $repo -c user.name=t -c user.email=t@t commit -q -m seed 2>&1 | Out-Null
    & git -C $repo remote add fork $bare 2>&1 | Out-Null
    & git -C $repo push -q fork 'HEAD:refs/heads/hub-lease/mlv-app' 2>&1 | Out-Null
    return $ws
}
function Get-LeaseHolder([string]$Ws) {
    $raw = (& git -C (Join-Path $Ws 'fork.git') cat-file -p 'refs/heads/hub-lease/mlv-app:HUB-LEASE.json') -join "`n"
    return [string](($raw | ConvertFrom-Json).holder)
}
$me = $env:COMPUTERNAME
$lockRoot = New-Scratch 'lock-root'
if ($fnLoaded -and (Test-Has 'Open-GateLock' 'A1/A8 lock')) {
    $s1 = Open-GateLock -Root $lockRoot -Mode Shared
    $s2 = Open-GateLock -Root $lockRoot -Mode Shared
    $x1 = Open-GateLock -Root $lockRoot -Mode Exclusive
    Assert-That ($s1 -and $s2 -and -not $x1) 'A1 lock: any number of Lease-gated runs may hold the lock shared, and an exclusive (release) request is refused while one does'
    $s1.Dispose(); $s2.Dispose()
    $x2 = Open-GateLock -Root $lockRoot -Mode Exclusive
    $s3 = Open-GateLock -Root $lockRoot -Mode Shared
    Assert-That ($x2 -and -not $s3) 'A1 lock: with no gate running the exclusive lock is granted, and while it is held a gate cannot take the shared one'
    $x2.Dispose()
    $s4 = Open-GateLock -Root $lockRoot -Mode Shared
    Assert-That ($null -ne $s4) 'A1 lock: released means free again (no stale lock)'
    $s4.Dispose()
}
$wsL1 = New-LeaseWorkspace 'ws-lease1' $me
$stL1 = New-Scratch 'state-lease1'
$stL1other = New-Scratch 'state-lease1-other'
$held = if ($fnLoaded -and (Test-Has 'Open-GateLock' 'A2 lock')) { Open-GateLock -Root $lockRoot -Mode Shared } else { $null }
try { $rel1 = Invoke-Setup (New-Scratch 'home-l1') @('-Phase', 'LeaseRelease', '-StateRoot', $stL1other, '-LockRoot', $lockRoot, '-Workspace', $wsL1) } finally { if ($held) { $held.Dispose() } }
Assert-That ($rel1.Exit -eq 4 -and (Get-LeaseHolder $wsL1) -ceq $me) 'A8/A2 release: with a Lease-gated task running (even under a DIFFERENT -StateRoot) the release is refused (exit 4) and the lease is unchanged' "exit=$($rel1.Exit) holder=$(Get-LeaseHolder $wsL1)`n$($rel1.Out)"
$rel2 = Invoke-Setup (New-Scratch 'home-l2') @('-Phase', 'LeaseRelease', '-StateRoot', $stL1, '-LockRoot', $lockRoot, '-Workspace', $wsL1)
Assert-That ($rel2.Exit -eq 0 -and (Get-LeaseHolder $wsL1) -ceq '') 'A8 release: with nothing running the release goes through' "exit=$($rel2.Exit) holder=$(Get-LeaseHolder $wsL1)`n$($rel2.Out)"
$wsL3 = New-LeaseWorkspace 'ws-lease3' 'SOME-OTHER-HOST'
$rel3 = Invoke-Setup (New-Scratch 'home-l3') @('-Phase', 'LeaseRelease', '-Force', '-StateRoot', (New-Scratch 'state-lease3'), '-LockRoot', $lockRoot, '-Workspace', $wsL3)
Assert-That ($rel3.Exit -eq 3 -and (Get-LeaseHolder $wsL3) -ceq 'SOME-OTHER-HOST') 'A15 release: -Force without -LeaseReason on another host''s lease is refused (exit 3), lease unchanged' "exit=$($rel3.Exit)`n$($rel3.Out)"
$rel4 = Invoke-Setup (New-Scratch 'home-l4') @('-Phase', 'LeaseRelease', '-Force', '-LeaseReason', 'host gone', '-StateRoot', (New-Scratch 'state-lease4'), '-LockRoot', $lockRoot, '-Workspace', $wsL3)
Assert-That ($rel4.Exit -eq 0 -and (Get-LeaseHolder $wsL3) -ceq '') 'A15 release: -Force with a reason releases it' "exit=$($rel4.Exit)`n$($rel4.Out)"

# ---------------------------------------------------------------- A8 + A9: Gate (lease-gated run, WhatIf, source of the marker)
$gateTarget = Join-Path $ScratchRoot 'gate-target.ps1'
$gateMarker = Join-Path $ScratchRoot 'gate-ran.marker'
[IO.File]::WriteAllText($gateTarget, "[IO.File]::WriteAllText('$($gateMarker -replace "'", "''")', 'ran')")
$stG = New-Scratch 'state-gate'
$gWhatIf = Invoke-Setup (New-Scratch 'home-g1') @('-Phase', 'Gate', '-GateMode', 'Local', '-GateScript', $gateTarget, '-WhatIf', '-StateRoot', $stG, '-Workspace', (New-Scratch 'ws-g1'))
Assert-That ($gWhatIf.Exit -eq 0 -and -not (Test-Path -LiteralPath $gateMarker)) 'A9 gate: -WhatIf never invokes the target' "exit=$($gWhatIf.Exit)"
$gLocal = Invoke-Setup (New-Scratch 'home-g2') @('-Phase', 'Gate', '-GateMode', 'Local', '-GateScript', $gateTarget, '-StateRoot', $stG, '-Workspace', (New-Scratch 'ws-g2'))
Assert-That ($gLocal.Exit -eq 0 -and (Test-Path -LiteralPath $gateMarker)) 'A9 gate: without -WhatIf the target runs' "exit=$($gLocal.Exit)"
[IO.File]::Delete($gateMarker)
$wsG3 = New-LeaseWorkspace 'ws-g3' $me
$gLease = Invoke-Setup (New-Scratch 'home-g3') @('-Phase', 'Gate', '-GateMode', 'Lease', '-GateScript', $gateTarget, '-StateRoot', $stG, '-LockRoot', $lockRoot, '-Workspace', $wsG3)
$freeAfter = if ($fnLoaded -and (Get-Command Open-GateLock -ErrorAction SilentlyContinue)) { $xa = Open-GateLock -Root $lockRoot -Mode Exclusive; $ok = [bool]$xa; if ($xa) { $xa.Dispose() }; $ok } else { $false }
Assert-That ($gLease.Exit -eq 0 -and (Test-Path -LiteralPath $gateMarker) -and $freeAfter) 'A8 gate: a Lease-gated run on the holder runs the target and leaves the gate lock free' "exit=$($gLease.Exit)`n$($gLease.Out)"
[IO.File]::Delete($gateMarker)
$wsG4 = New-LeaseWorkspace 'ws-g4' 'SOME-OTHER-HOST'
$gOther = Invoke-Setup (New-Scratch 'home-g4') @('-Phase', 'Gate', '-GateMode', 'Lease', '-GateScript', $gateTarget, '-StateRoot', $stG, '-LockRoot', $lockRoot, '-Workspace', $wsG4)
Assert-That ($gOther.Exit -eq 0 -and -not (Test-Path -LiteralPath $gateMarker)) 'A8 gate: a Lease-gated run on a host that does not hold the lease skips the target' "exit=$($gOther.Exit)`n$($gOther.Out)"

# ---------------------------------------------------------------- A9: Ssh -WhatIf creates nothing under the profile
$homeS = New-Scratch 'home-ssh'
[void](Invoke-Setup $homeS @('-Phase', 'Ssh', '-WhatIf', '-StateRoot', (New-Scratch 'state-ssh'), '-Workspace', (New-Scratch 'ws-ssh')))
Assert-That (-not (Test-Path -LiteralPath (Join-Path $homeS '.ssh'))) 'A9 ssh: -Phase Ssh -WhatIf does not create ~\.ssh'

# ---------------------------------------------------------------- A10, A16: argument guards
$pf = Invoke-Setup (New-Scratch 'home-pf') @('-Phase', 'Verify', '-PagefileMaxGB', '4194304', '-StateRoot', (New-Scratch 'state-pf'), '-Workspace', (New-Scratch 'ws-pf'))
Assert-That ($pf.Exit -ne 0 -and $pf.Out -match 'PagefileMaxGB') 'A10 pagefile: a -PagefileMaxGB whose MB value overflows uint32 is refused at parameter binding, before anything changes' "exit=$($pf.Exit)`n$($pf.Out)"
foreach ($combo in 'Host,Gate', 'All,Gate', 'Gate,Verify') {
    $gc = Invoke-Setup (New-Scratch 'home-combo') @('-Phase', $combo, '-WhatIf', '-StateRoot', (New-Scratch 'state-combo'), '-Workspace', (New-Scratch 'ws-combo'))
    Assert-That ($gc.Exit -eq 64 -and $gc.Out -match "must be run alone") "A16 phases: -Phase $combo is refused (exit 64)" "exit=$($gc.Exit)`n$($gc.Out)"
}

# ================================================================ adversary pass 2 (head b4824e4b): one section per finding class
# ---------------------------------------------------------------- A1: a release in flight stops a Lease-gated run from starting
if ($fnLoaded -and (Get-Command Open-GateLock -ErrorAction SilentlyContinue)) {
    $wsX = New-LeaseWorkspace 'ws-gx' $me
    [IO.File]::Delete($gateMarker) 2>$null
    $xh = Open-GateLock -Root $lockRoot -Mode Exclusive
    try { $gBusy = Invoke-Setup (New-Scratch 'home-gx') @('-Phase', 'Gate', '-GateMode', 'Lease', '-GateScript', $gateTarget, '-StateRoot', (New-Scratch 'state-gx'), '-LockRoot', $lockRoot, '-Workspace', $wsX) } finally { if ($xh) { $xh.Dispose() } }
    Assert-That ($gBusy.Exit -eq 0 -and -not (Test-Path -LiteralPath $gateMarker)) 'A1 gate: while a release holds the lock exclusively a Lease-gated run does not start its target' "exit=$($gBusy.Exit)`n$($gBusy.Out)"
} else { Assert-That $false 'A1 gate: Open-GateLock is not defined' }

# ---------------------------------------------------------------- A3: hidden files are scanned, listed and quarantined too
if ($fnLoaded -and (Get-Command Invoke-StagedSecretScan -ErrorAction SilentlyContinue)) {
    $hidStage = New-Scratch 'scan-stage-hidden'
    $hid = Join-Path $hidStage 'config.json'
    [IO.File]::WriteAllText($hid, ('gh' + 'p_' + ('A' * 40)))
    (Get-Item -LiteralPath $hid -Force).Attributes = [IO.FileAttributes]::Hidden
    $res = Invoke-StagedSecretScan -Stage $hidStage
    Assert-That (-not (Test-Path -LiteralPath $hid) -and $res.Removed.Count -eq 1) 'A3 scan: a Hidden staged file holding a token is found and removed' "removed=$($res.Removed -join ',')"
    $hidClaims = Join-Path (New-Scratch 'fn-ws-hid') 'claims'
    [void][IO.Directory]::CreateDirectory($hidClaims)
    $hc = Join-Path $hidClaims 'hidden-foreign.claim'
    [IO.File]::WriteAllText($hc, '{"utc":"' + [DateTime]::UtcNow.AddHours(-1).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ') + '","runDir":"' + ((Join-Path $ScratchRoot 'absent-hidden-run') -replace '\\', '\\') + '"}')
    (Get-Item -LiteralPath $hc -Force).Attributes = [IO.FileAttributes]::Hidden
    $mv = @(Move-ForeignClaims -ClaimsDir $hidClaims -Stamp 'TH')
    Assert-That ($mv.Count -eq 1) 'A3 claims: a Hidden foreign claim is quarantined too' "moved=$($mv -join ',')"
} else { Assert-That $false 'A3: Invoke-StagedSecretScan is not defined' }

# ---------------------------------------------------------------- A4: an uncleared file or an unwritable report means NOT clean, and never throws
if ($fnLoaded -and (Get-Command Test-StagedBundleClean -ErrorAction SilentlyContinue)) {
    $dirtyStage = New-Scratch 'scan-stage-dirty'
    $held4 = Join-Path $dirtyStage 'notes.txt'
    [IO.File]::WriteAllText($held4, ('gh' + 'p_' + ('A' * 40)))
    $report = Join-Path $dirtyStage 'SECRET-SCAN.txt'
    [IO.File]::WriteAllText($report, 'clean')
    (Get-Item -LiteralPath $report).Attributes = [IO.FileAttributes]::ReadOnly
    $rd = [IO.File]::Open($held4, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $threw = $false; $clean4 = $true
    try { $clean4 = Test-StagedBundleClean -Stage $dirtyStage } catch { $threw = $true } finally { $rd.Dispose(); (Get-Item -LiteralPath $report -Force).Attributes = [IO.FileAttributes]::Normal }
    Assert-That (-not $threw -and -not $clean4) 'A4 scan: a readable-but-undeletable secret file and a read-only report give "not clean" (no bundle), without throwing'
    $okStage = New-Scratch 'scan-stage-ok'
    [IO.File]::WriteAllText((Join-Path $okStage 'a.txt'), 'plain')
    Assert-That (Test-StagedBundleClean -Stage $okStage) 'A4 scan: a clean stage is clean'
} else { Assert-That $false 'A4: Test-StagedBundleClean is not defined' }

# ---------------------------------------------------------------- A5: the run stamp is unique per process
if ($fnLoaded) { Assert-That ($stamp -match 'Z-\d+$') 'A5 stamp: includes the process id, so two runs in one second do not share a stage folder or ZIP name' "stamp=$stamp" }

# ---------------------------------------------------------------- A6: Include directives are refused
if ($fnLoaded -and (Get-Command Set-SshdKeyOnlyConfig -ErrorAction SilentlyContinue)) {
    $incCfg = Join-Path (New-Scratch 'sshd-inc') 'sshd_config'
    $incText = "Include C:/ProgramData/ssh/site.conf`r`nPasswordAuthentication yes`r`n"
    [IO.File]::WriteAllText($incCfg, $incText)
    $threw = $false
    try { Set-SshdKeyOnlyConfig -ConfigPath $incCfg -BackupDir (Join-Path (Split-Path $incCfg) 'bk') -Stamp 'I' -Validate { 0 } } catch { $threw = ($_.Exception.Message -match 'includes other files') }
    Assert-That ($threw -and ([IO.File]::ReadAllText($incCfg) -ceq $incText)) 'A6 sshd: a config with an Include is refused and left unchanged'
} else { Assert-That $false 'A6: Set-SshdKeyOnlyConfig is not defined' }

# ---------------------------------------------------------------- A7: a pagefile maximum below the 8 GB initial size is refused at binding
$pf1 = Invoke-Setup (New-Scratch 'home-pf1') @('-Phase', 'LeaseShow', '-PagefileMaxGB', '1', '-StateRoot', (New-Scratch 'state-pf1'), '-Workspace', (New-Scratch 'ws-pf1'))
Assert-That ($pf1.Exit -ne 0 -and $pf1.Out -match 'PagefileMaxGB') 'A7 pagefile: -PagefileMaxGB 1 (below the 8 GB initial size) is refused at parameter binding' "exit=$($pf1.Exit)`n$($pf1.Out)"
$pf8 = Invoke-Setup (New-Scratch 'home-pf8') @('-Phase', 'LeaseShow', '-PagefileMaxGB', '8', '-StateRoot', (New-Scratch 'state-pf8'), '-Workspace', (New-Scratch 'ws-pf8'))
Assert-That ($pf8.Exit -eq 0) 'A7 pagefile: -PagefileMaxGB 8 is accepted' "exit=$($pf8.Exit)`n$($pf8.Out)"

# ---------------------------------------------------------------- A8, A9: claim runDir and utc edge cases
if ($fnLoaded) {
    $edgeDir = Join-Path (New-Scratch 'fn-ws-edge') 'claims'
    [void][IO.Directory]::CreateDirectory($edgeDir)
    $oneH = [DateTime]::UtcNow.AddHours(-1).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
    [IO.File]::WriteAllText((Join-Path $edgeDir 'drive-rel.claim'), '{"utc":"' + $oneH + '","runDir":"C:missing-run"}')
    [IO.File]::WriteAllText((Join-Path $edgeDir 'root-rel.claim'), '{"utc":"' + $oneH + '","runDir":"\\missing-run"}')
    [IO.File]::WriteAllText((Join-Path $edgeDir 'noz-old.claim'), '{"utc":"' + [DateTime]::UtcNow.AddHours(-25).ToString('yyyy-MM-ddTHH:mm:ss') + '","runDir":"' + ((Join-Path $ScratchRoot 'absent-edge') -replace '\\', '\\') + '"}')
    [IO.File]::WriteAllText((Join-Path $edgeDir 'noz-new.claim'), '{"utc":"' + [DateTime]::UtcNow.AddHours(-1).ToString('yyyy-MM-ddTHH:mm:ss') + '","runDir":"' + ((Join-Path $ScratchRoot 'absent-edge') -replace '\\', '\\') + '"}')
    $mvE = @(Move-ForeignClaims -ClaimsDir $edgeDir -Stamp 'TE')
    Assert-That ((Test-Path -LiteralPath (Join-Path $edgeDir 'drive-rel.claim')) -and (Test-Path -LiteralPath (Join-Path $edgeDir 'root-rel.claim'))) 'A8 claims: drive-relative (C:x) and root-relative (\x) runDirs are malformed and stay'
    Assert-That ((Test-Path -LiteralPath (Join-Path $edgeDir 'noz-old.claim')) -and $mvE.Count -eq 1 -and $mvE[0] -ceq 'noz-new.claim') 'A9 claims: an offset-free utc is read as UTC (a 25 h old claim stays whatever the host zone; a 1 h old one is quarantined)' "moved=$($mvE -join ',')"
}

# ---------------------------------------------------------------- A10: Import never replaces a claim the host already has
$home10 = New-Scratch 'home10'
$ws10 = New-Scratch 'ws10'
$liveRun10 = New-Scratch 'live-run10'
$localClaims10 = Join-Path $ws10 'MLV-App\.claude-state\hub-autopilot\claims'
[void][IO.Directory]::CreateDirectory($localClaims10)
$localClaim = Join-Path $localClaims10 'CARD--sonnet--r1.claim'
$localJson = '{"utc":"' + [DateTime]::UtcNow.AddMinutes(-5).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ') + '","runDir":"' + ($liveRun10 -replace '\\', '\\') + '","card":"LOCAL"}'
[IO.File]::WriteAllText($localClaim, $localJson)
$bundle10 = New-Scratch 'bundle10'
[IO.File]::WriteAllText((Join-Path $bundle10 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
[void][IO.Directory]::CreateDirectory((Join-Path $bundle10 'home-claude'))
[IO.File]::WriteAllText((Join-Path $bundle10 'home-claude\settings.json'), '{}')
$bc10 = Join-Path $bundle10 'project-claude-state\hub-autopilot\claims'
[void][IO.Directory]::CreateDirectory($bc10)
[IO.File]::WriteAllText((Join-Path $bc10 'CARD--sonnet--r1.claim'), '{"utc":"' + [DateTime]::UtcNow.AddMinutes(-1).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ') + '","runDir":"' + ((Join-Path $ScratchRoot 'absent-foreign10') -replace '\\', '\\') + '","card":"FOREIGN"}')
$imp10 = Invoke-Setup $home10 @('-Phase', 'Import', '-StateRoot', (New-Scratch 'state10'), '-Workspace', $ws10, '-BundlePath', $bundle10, '-SourceHost', 'NOT-THIS-HOST')
Assert-That ($imp10.Exit -eq 0 -and (Test-Path -LiteralPath $localClaim) -and ([IO.File]::ReadAllText($localClaim) -ceq $localJson)) 'A10 import: a same-named bundle claim never replaces a live local claim' "exit=$($imp10.Exit)`n$($imp10.Out)"

# ---------------------------------------------------------------- A11: PAUSED stops the meter installs
$wsP = New-MeterRepo 'ws-meter-paused' $okBody $okBody
$stP = New-Scratch 'state-paused'
[IO.File]::WriteAllText((Join-Path $stP 'PAUSED'), '')
$mP = Invoke-Setup (New-Scratch 'home-paused') (@('-Phase', 'Verify', '-StateRoot', $stP, '-Workspace', $wsP, '-SourceHost', 'NOT-THIS-HOST') + $okPaths)
Assert-That ($mP.Exit -eq 0 -and $mP.Out.Contains('METER_INSTALL_SKIPPED_PAUSED') -and -not (Test-Path -LiteralPath (Join-Path $wsP 'MLV-App\.claude-state\disk-hygiene\installed.marker'))) 'A11 meters: with PAUSED present no meter -Install runs' "exit=$($mP.Exit)`n$($mP.Out)"

# ---------------------------------------------------------------- A12: LeaseAcquire -Force needs a non-blank reason
$wsA12 = New-LeaseWorkspace 'ws-a12' 'SOME-OTHER-HOST'
$a12 = Invoke-Setup (New-Scratch 'home-a12') @('-Phase', 'LeaseAcquire', '-Force', '-LeaseReason', '   ', '-StateRoot', (New-Scratch 'state-a12'), '-LockRoot', $lockRoot, '-Workspace', $wsA12)
Assert-That ($a12.Exit -eq 3 -and (Get-LeaseHolder $wsA12) -ceq 'SOME-OTHER-HOST') 'A12 acquire: -Force with a blank -LeaseReason is refused (exit 3), lease unchanged' "exit=$($a12.Exit)`n$($a12.Out)"

# ---------------------------------------------------------------- A15: a secret VALUE under a secret-looking NAME is caught
if ($fnLoaded -and (Get-Command Invoke-StagedSecretScan -ErrorAction SilentlyContinue)) {
    $awsStage = New-Scratch 'scan-stage-aws'
    [IO.File]::WriteAllText((Join-Path $awsStage 'settings.json'), '{"env":{"AWS_SECRET_ACCESS_KEY":"' + ('A' * 40) + '"}}')
    [IO.File]::WriteAllText((Join-Path $awsStage 'benign.json'), '{"env":{"FOO":"bar","MODEL":"claude"}}')
    $res = Invoke-StagedSecretScan -Stage $awsStage
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $awsStage 'settings.json')) -and (Test-Path -LiteralPath (Join-Path $awsStage 'benign.json'))) 'A15 scan: an AWS secret access key under its name is removed; ordinary settings stay' "removed=$($res.Removed -join ',')"
} else { Assert-That $false 'A15: Invoke-StagedSecretScan is not defined' }

# ---------------------------------------------------------------- r3 A3: an unwritable SECRET-SCAN.txt alone stops the export zip
if ($fnLoaded -and (Get-Command Test-StagedBundleClean -ErrorAction SilentlyContinue)) {
    $r3a3 = New-Scratch 'r3-a3-stage'
    [IO.File]::WriteAllText((Join-Path $r3a3 'a.txt'), 'plain, nothing secret')
    $r3a3Report = Join-Path $r3a3 'SECRET-SCAN.txt'
    [IO.File]::WriteAllText($r3a3Report, 'clean')
    (Get-Item -LiteralPath $r3a3Report).Attributes = [IO.FileAttributes]::ReadOnly
    $r3a3Threw = $false; $r3a3Clean = $true
    try { $r3a3Clean = Test-StagedBundleClean -Stage $r3a3 } catch { $r3a3Threw = $true } finally { (Get-Item -LiteralPath $r3a3Report -Force).Attributes = [IO.FileAttributes]::Normal }
    Assert-That (-not $r3a3Threw -and -not $r3a3Clean) 'r3 A3 scan: a CLEAN stage whose SECRET-SCAN.txt cannot be written is NOT clean (isolated: no other dirty file)' "threw=$r3a3Threw clean=$r3a3Clean"
} else { Assert-That $false 'r3 A3: Test-StagedBundleClean is not defined' }

# ---------------------------------------------------------------- r3 A4: secret NAME list widened; single and double quotes; value minimum 4
if ($fnLoaded -and (Get-Command Invoke-StagedSecretScan -ErrorAction SilentlyContinue)) {
    $r3a4 = New-Scratch 'r3-a4-stage'
    $rows = [ordered]@{
        'dbpass.json'  = '{"env":{"DB_PASSWORD":"valid-pass-9"}}'
        'apikey.toml'  = 'api_key = "abcdefghijklmnopqrstuvwx1234567890"'
        'single.env2'  = "ACCESS_KEY='fake-access-9876'"
        'authkey.json' = '{"AUTH_KEY": "fake-auth-1234"}'
        'camel.json'   = '{"apiKey": "fake-camel-5555"}'
        'tok.json'     = '{"GITHUB_TOKEN": "fake-tok-4321"}'
        'cred.ini'     = 'DB_CREDENTIAL = "fake-cred-0001"'
    }
    foreach ($k in $rows.Keys) { [IO.File]::WriteAllText((Join-Path $r3a4 $k), $rows[$k]) }
    [IO.File]::WriteAllText((Join-Path $r3a4 'benign.json'), '{"env":{"FOO":"bar","MODEL":"claude","KEYBOARD":"qwerty-layout"}}')
    $res4 = Invoke-StagedSecretScan -Stage $r3a4
    foreach ($k in $rows.Keys) { Assert-That (-not (Test-Path -LiteralPath (Join-Path $r3a4 $k))) "r3 A4 scan: $k ($($rows[$k].Substring(0, [Math]::Min(14, $rows[$k].Length)))...) is removed" "removed=$($res4.Removed -join ',')" }
    Assert-That (Test-Path -LiteralPath (Join-Path $r3a4 'benign.json')) 'r3 A4 scan: ordinary settings with no secret-named key stay' "removed=$($res4.Removed -join ',')"
} else { Assert-That $false 'r3 A4: Invoke-StagedSecretScan is not defined' }

# ---------------------------------------------------------------- r3 A5: Import never replaces the host's own credential-bearing files (content and role, not file name only)
$homeA5 = New-Scratch 'r3-a5-home'
$localSettings = Join-Path $homeA5 '.claude\settings.json'
$localNotes = Join-Path $homeA5 '.claude\hooks-config.json'
$localToml = Join-Path $homeA5 '.codex\config.toml'
[void][IO.Directory]::CreateDirectory((Join-Path $homeA5 '.claude')); [void][IO.Directory]::CreateDirectory((Join-Path $homeA5 '.codex'))
[IO.File]::WriteAllText($localSettings, '{"env":{"DB_PASSWORD":"local-password"}}')
[IO.File]::WriteAllText($localNotes, '{"service":{"api_key":"local-api-1234"}}')
[IO.File]::WriteAllText($localToml, '[auth]' + "`n" + 'token = "local-token-1234"')
foreach ($p in $localSettings, $localNotes, $localToml) { (Get-Item -LiteralPath $p).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-3) }
$bundleA5 = New-Scratch 'r3-a5-bundle'
[IO.File]::WriteAllText((Join-Path $bundleA5 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
[void][IO.Directory]::CreateDirectory((Join-Path $bundleA5 'home-claude')); [void][IO.Directory]::CreateDirectory((Join-Path $bundleA5 'home-codex'))
[IO.File]::WriteAllText((Join-Path $bundleA5 'home-claude\settings.json'), '{"env":{"DB_PASSWORD":"foreign-password"}}')
[IO.File]::WriteAllText((Join-Path $bundleA5 'home-claude\hooks-config.json'), '{"service":{"api_key":"foreign-api-5678"}}')
[IO.File]::WriteAllText((Join-Path $bundleA5 'home-claude\CLAUDE.md'), 'owner instructions')
[IO.File]::WriteAllText((Join-Path $bundleA5 'home-codex\config.toml'), '[auth]' + "`n" + 'token = "foreign-token-5678"')
$impA5 = Invoke-Setup $homeA5 @('-Phase', 'Import', '-StateRoot', (New-Scratch 'r3-a5-state'), '-Workspace', (New-Scratch 'r3-a5-ws'), '-BundlePath', $bundleA5, '-SourceHost', 'NOT-THIS-HOST')
Assert-That ($impA5.Exit -eq 0) 'r3 A5 import: the bundle imports (exit 0)' "exit=$($impA5.Exit)`n$($impA5.Out)"
Assert-That (([IO.File]::ReadAllText($localSettings)) -ceq '{"env":{"DB_PASSWORD":"local-password"}}') 'r3 A5 import: the host''s settings.json (role file with a credential) is not replaced' ([IO.File]::ReadAllText($localSettings))
Assert-That (([IO.File]::ReadAllText($localNotes)) -ceq '{"service":{"api_key":"local-api-1234"}}') 'r3 A5 import: a host file with an arbitrary name but credential CONTENT is not replaced' ([IO.File]::ReadAllText($localNotes))
Assert-That (([IO.File]::ReadAllText($localToml)) -match 'local-token-1234') 'r3 A5 import: the host''s ~/.codex/config.toml with a token is not replaced' ([IO.File]::ReadAllText($localToml))
Assert-That ($impA5.Out.Contains('IMPORT_SKIPPED_CREDENTIAL_FILE')) 'r3 A5 import: each refusal is logged as IMPORT_SKIPPED_CREDENTIAL_FILE' $impA5.Out
Assert-That ((Test-Path -LiteralPath (Join-Path $homeA5 '.claude\CLAUDE.md')) -and ((Get-Content -LiteralPath (Join-Path $homeA5 '.claude\CLAUDE.md') -Raw).Trim() -ceq 'owner instructions')) 'r3 A5 import: a file with no credential on either side still comes across' $impA5.Out

# ---------------------------------------------------------------- r3 A7: Verify leaves no meter task enabled outside the gate
if ($fnLoaded -and (Get-Command Invoke-MeterInstalls -ErrorAction SilentlyContinue)) {
    $SourceHost = 'NOT-THIS-HOST'
    $markerOf = @{ 'MLV-DiskAutoReclaim' = 'disk-hygiene\installed.marker'; 'MLV-PwshAutoRepin' = 'hub-autopilot\installed.marker' }
    # Stubs shadow the ScheduledTasks cmdlets for this scope only: nothing here touches a real task. A -Install "enables" its task (the marker file stands for it).
    function Get-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath)
        # p5 A5: StubFailMode 'before' = the read fails until the install ran, 'after' = it fails once the install ran (a read error that is not "no such task").
        $installedNow = Test-Path -LiteralPath (Join-Path $script:StubRepo ('.claude-state\' + $markerOf[$TaskName]))
        if (($script:StubFailMode -ceq 'before' -and -not $installedNow) -or ($script:StubFailMode -ceq 'after' -and $installedNow -and -not $script:StubTouched.Contains($TaskName))) { throw 'Access is denied reading the scheduled task' }
        # r10 A5: the FIRST read after the install works, every later one (until the task is touched) fails.
        if ($script:StubSecondReadFails -and $installedNow -and -not $script:StubTouched.Contains($TaskName)) {
            $script:StubPostReads[$TaskName] = 1 + [int]$script:StubPostReads[$TaskName]
            if ($script:StubPostReads[$TaskName] -ge 2) { throw 'RPC server unavailable reading the scheduled task' }
        }
        $p = $script:StubPrior[$TaskName]
        if ($p -eq 'ABSENT' -and -not (Test-Path -LiteralPath (Join-Path $script:StubRepo ('.claude-state\' + $markerOf[$TaskName])))) { return $null }
        $touched = $script:StubTouched.Contains($TaskName)
        $installed = Test-Path -LiteralPath (Join-Path $script:StubRepo ('.claude-state\' + $markerOf[$TaskName]))
        $state = if ($touched) { 'Disabled' } elseif ($installed) { 'Ready' } else { $p }
        return [pscustomobject]@{ TaskName = $TaskName; State = $state }
    }
    function Export-ScheduledTask {
        [CmdletBinding()] param($TaskName, $TaskPath)
        if ($script:StubExportFail) { throw 'cannot export the task definition' }
        # r10 A4: once the install ran, the task definition differs (the install re-registered it with its own action).
        if ($script:StubXmlAfter -and (Test-Path -LiteralPath (Join-Path $script:StubRepo ('.claude-state\' + $markerOf[$TaskName])))) { return '<Task>install-registered</Task>' }
        return '<Task>gated</Task>'
    }
    function Register-ScheduledTask { [CmdletBinding()] param($TaskName, $TaskPath, $Xml, [switch]$Force) [void]$script:StubTouched.Add($TaskName); $script:StubCalls.Add("REGISTER $TaskName") }
    function Disable-ScheduledTask { [CmdletBinding()] param($TaskName, $TaskPath) [void]$script:StubTouched.Add($TaskName); $script:StubCalls.Add("DISABLE $TaskName") }
    function Enable-ScheduledTask { [CmdletBinding()] param($TaskName, $TaskPath) $script:StubCalls.Add("ENABLE $TaskName") }
    $script:StubFailMode = 'none'
    $script:StubXmlAfter = $false; $script:StubExportFail = $false; $script:StubSecondReadFails = $false; $script:StubPostReads = @{}
    function Invoke-A7Run([string]$Name, [hashtable]$Prior, [string]$FailMode = 'none', [string]$Body = '') {
        $mBody = if ($Body) { $Body } else { $okBody }
        $wsM = New-MeterRepo $Name $mBody $mBody
        $script:StubRepo = Join-Path $wsM 'MLV-App'
        $script:Repo = $script:StubRepo; $Repo = $script:StubRepo
        $script:StubPostReads = @{}
        $script:StubFailMode = $FailMode; $script:Failures.Clear()
        $script:StubPrior = $Prior; $script:StubTouched = [Collections.Generic.HashSet[string]]::new(); $script:StubCalls = [Collections.Generic.List[string]]::new()
        Invoke-MeterInstalls
        $script:StubFailMode = 'none'   # the post-run reads below are the test's own and must not hit the injected read failure
        return @{ Disk = (Get-ScheduledTask -TaskName 'MLV-DiskAutoReclaim'); Repin = (Get-ScheduledTask -TaskName 'MLV-PwshAutoRepin') }
    }
    $a7 = Invoke-A7Run 'r3-a7-a' @{ 'MLV-DiskAutoReclaim' = 'Disabled'; 'MLV-PwshAutoRepin' = 'ABSENT' }
    Assert-That ($a7.Disk -and $a7.Disk.State -ceq 'Disabled') 'r3 A7 meters: a task that was Disabled before -Install is Disabled after Verify' "state=$($a7.Disk.State) calls=$($script:StubCalls -join ',')"
    Assert-That ($a7.Repin -and $a7.Repin.State -ceq 'Disabled') 'r3 A7 meters: a task that was ABSENT before -Install is registered but Disabled after Verify' "state=$($a7.Repin.State) calls=$($script:StubCalls -join ',')"
    $a7b = Invoke-A7Run 'r3-a7-b' @{ 'MLV-DiskAutoReclaim' = 'Ready'; 'MLV-PwshAutoRepin' = 'Ready' }
    Assert-That ($script:StubCalls.Count -eq 0 -and $a7b.Disk.State -ceq 'Ready') 'r3 A7 meters: a task the owner had ENABLED is left enabled (no Disable or re-register call)' "calls=$($script:StubCalls -join ',')"
    # ---- p5 A5: an unreadable task is not an absent one, and the restore does not depend on the log write succeeding
    $p5a5a = Invoke-A7Run 'p5-a5-a' @{ 'MLV-DiskAutoReclaim' = 'Ready'; 'MLV-PwshAutoRepin' = 'Ready' } 'before'
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $script:StubRepo '.claude-state\disk-hygiene\installed.marker'))) 'p5 A5 meters: a task whose state cannot be read BEFORE the install is not installed over (it may be one the owner enabled)'
    Assert-That (($script:Failures | Where-Object { $_ -like 'METER_INSTALL_SKIPPED_STATE_UNREADABLE*' }).Count -ge 1) 'p5 A5 meters: the unreadable-before skip is a failure of the run (exit 1), not a silent skip' ($script:Failures -join ',')
    $null = Invoke-A7Run 'p5-a5-b' @{ 'MLV-DiskAutoReclaim' = 'Disabled'; 'MLV-PwshAutoRepin' = 'ABSENT' } 'after'
    Assert-That (($script:StubCalls | Where-Object { $_ -like 'DISABLE*' }).Count -ge 1) 'p5 A5 meters: a task that cannot be read after the install is Disabled anyway (not assumed absent)' ($script:StubCalls -join ',')
    $script:StubFailMode = 'none'
    $origWriteLog = ${function:Write-Log}
    function Write-Log { param([string]$Message, [string]$Level = 'INFO') if ($Message.StartsWith('    ')) { throw 'log file is locked' } }
    $p5a5thrown = $false
    $talkBody = 'param([switch]$Install) if ($Install) { [IO.File]::WriteAllText((Join-Path $PSScriptRoot "installed.marker"), "ran -Install"); Write-Output "installed"; exit 0 } exit 5'
    try { $null = Invoke-A7Run 'p5-a5-c' @{ 'MLV-DiskAutoReclaim' = 'Disabled'; 'MLV-PwshAutoRepin' = 'ABSENT' } 'none' $talkBody } catch { $p5a5thrown = $true }
    $p5a5calls = @($script:StubCalls)
    ${function:Write-Log} = $origWriteLog
    Assert-That ($p5a5thrown -and (@($p5a5calls | Where-Object { $_ -like 'DISABLE*' -or $_ -like 'REGISTER*' }).Count -ge 1)) 'p5 A5 meters: a log write that throws after the install still restores the task (restore is in finally)' ($p5a5calls -join ',')
} else { Assert-That $false 'r3 A7: Invoke-MeterInstalls is not defined' }

# r15 (hub ruling on adversary pass 12, DENY BY DEFAULT on the key name): the only value exemptions are the closed literal list ('', '""', "''", 'null', '~', '[]', '{}',
# '***', '${NAME}', '<words>', an empty XML element). The earlier rounds' "is not a credential" samples below that relied on a retired exemption (an unquoted boolean, a
# token count, a quoted placeholder, a sequence of exempt elements, a trailing comment or ';', an empty TOML triple quote, a comment-only XML element or block, a netrc
# '""') are now asserted to REFUSE (accepted over-refusal, forced by the ruling); every other sample must still pass. The retired samples are named here by label.
$script:R15RetiredOkLabels = @(
    'a numeric token limit', 'a boolean under a key name',
    'a YAML boolean under a password key', 'a boolean under a password key', 'a numeric token limit (still benign)', 'a YAML token count',
    'S-B1 control: an empty basic triple-quoted value', 'S-B1 control: an empty literal triple-quoted value',
    'A1 flow mapping, boolean value', 'A1 flow mapping, boolean flag key', 'A2 bare boolean', 'A2 boolean then a comment', 'A2 boolean then the next line', 'A2 boolean then a semicolon',
    'B1 cmd boolean then semicolon at the end', 'B1 yaml boolean with a spaced comment', 'B1 flow boolean with a spaced comment', 'B1 yaml token boolean',
    'a block key with only a comment after it', 'a ${...} placeholder', 'a netrc empty quoted password', 'a flow boolean on the next line',
    'p11 a sequence of exempt elements', 'p11 a block boolean followed by a sibling key', 'p11 an XML comment only'
)
function Assert-CredentialOkOrRetired {
    param([string]$Prefix, [string]$Label, [string]$Text)
    if ($script:R15RetiredOkLabels -ccontains $Label) { Assert-That (Test-CredentialText $Text) "$Prefix $Label is REFUSED under the r15 deny-by-default rule (its earlier exemption is retired)" $Text }
    else { Assert-That (-not (Test-CredentialText $Text)) "$Prefix $Label is not a credential" $Text }
}

# ================================================================ adversary pass 5 (head 2c812a47): one section per finding class
# ---------------------------------------------------------------- p5 A1: credential text is judged by what it says, not by one spelling of a value
if ($fnLoaded -and (Get-Command Test-CredentialText -ErrorAction SilentlyContinue) -and (Get-Command Invoke-StagedSecretScan -ErrorAction SilentlyContinue)) {
    $p5a1 = [ordered]@{
        'a value with a space inside the quotes'      = '{"DB_PASSWORD":"two words"}'
        'a three character quoted value'             = '{"DB_PASSWORD":"abc"}'
        'an unquoted cmd set line'                    = "@set DB_PASSWORD=valid-pass-9"
        'an unquoted dotenv style line'               = "API_KEY=abcd1234efgh"
        'a key spelled with a JSON unicode escape'    = '{"api\u005fkey":"valid-pass-9"}'
        'a value spelled with a JSON unicode escape'  = '{"DB_PASSWORD":"valid\u002dpass-9"}'
    }
    foreach ($k in $p5a1.Keys) { Assert-That (Test-CredentialText $p5a1[$k]) "p5 A1 Test-CredentialText: $k is a credential" $p5a1[$k] }
    $p5a1ok = [ordered]@{
        'ordinary env settings'       = '{"env":{"FOO":"bar","MODEL":"claude","KEYBOARD":"qwerty-layout"}}'
        'a numeric token limit'       = 'max_tokens = 4096'
        'a boolean under a key name'  = '{"passwordless": true, "token": null}'
        'an empty secret value'       = '{"DB_PASSWORD": ""}'
    }
    foreach ($k in $p5a1ok.Keys) { Assert-CredentialOkOrRetired 'p5 A1 Test-CredentialText:' $k $p5a1ok[$k] }
    $p5a1Stage = New-Scratch 'p5-a1-stage'
    [IO.File]::WriteAllText((Join-Path $p5a1Stage 'login.cmd'), "@set DB_PASSWORD=valid-pass-9`r`n")
    [IO.File]::WriteAllText((Join-Path $p5a1Stage 'hooks-config.json'), '{"api\u005fkey":"valid-pass-9"}')
    [IO.File]::WriteAllText((Join-Path $p5a1Stage 'notes.json'), '{"DB_PASSWORD":"two words"}')
    [void](Invoke-StagedSecretScan -Stage $p5a1Stage)
    foreach ($n in 'login.cmd', 'hooks-config.json', 'notes.json') { Assert-That (-not (Test-Path -LiteralPath (Join-Path $p5a1Stage $n))) "p5 A1 export scan: $n is removed from the staged bundle" }
} else { Assert-That $false 'p5 A1: Test-CredentialText / Invoke-StagedSecretScan are not defined' }

# ---------------------------------------------------------------- p5 A2, A3, A4: Import end to end (claims and doctrine copies, newer host files, a host settings.json that was never replaced)
$homeP5 = New-Scratch 'p5-home'
$wsP5 = New-Scratch 'p5-ws'
$repoP5 = Join-Path $wsP5 'MLV-App'
New-Item -ItemType Directory -Force -Path (Join-Path $homeP5 '.claude') | Out-Null
[IO.File]::WriteAllText((Join-Path $homeP5 '.claude\settings.json'), '{"env":{"DB_PASSWORD":"local-password"},"path":"C:/Users/obabalola/tool"}')
[IO.File]::WriteAllText((Join-Path $homeP5 '.claude\CLAUDE.md'), 'NEWER-LOCAL')
(Get-Item -LiteralPath (Join-Path $homeP5 '.claude\CLAUDE.md')).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-1)
$bundleP5 = New-Scratch 'p5-bundle'
[IO.File]::WriteAllText((Join-Path $bundleP5 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
New-Item -ItemType Directory -Force -Path (Join-Path $bundleP5 'home-claude') | Out-Null
[IO.File]::WriteAllText((Join-Path $bundleP5 'home-claude\CLAUDE.md'), 'OLD')
(Get-Item -LiteralPath (Join-Path $bundleP5 'home-claude\CLAUDE.md')).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-5)
New-Item -ItemType Directory -Force -Path (Join-Path $bundleP5 'project-claude-state\hub-autopilot\claims') | Out-Null
[IO.File]::WriteAllText((Join-Path $bundleP5 'project-claude-state\hub-autopilot\claims\new.claim'), '{"api_key":"foreign-key-1234"}')
New-Item -ItemType Directory -Force -Path (Join-Path $bundleP5 'project-codex-state\doctrine') | Out-Null
[IO.File]::WriteAllText((Join-Path $bundleP5 'project-codex-state\doctrine\last-seen.json'), '{"api_key":"foreign-key-1234"}')
[IO.File]::WriteAllText((Join-Path $bundleP5 'project-codex-state\doctrine\clean.json'), 'from-bundle')
$impP5 = Invoke-Setup $homeP5 @('-Phase', 'Import', '-StateRoot', (New-Scratch 'p5-state'), '-Workspace', $wsP5, '-BundlePath', $bundleP5, '-SourceHost', 'NOT-THIS-HOST')
Assert-That ($impP5.Exit -eq 0) 'p5 import: the bundle imports (exit 0)' "exit=$($impP5.Exit)`n$($impP5.Out)"
Assert-That (-not (Test-Path -LiteralPath (Join-Path $repoP5 '.claude-state\hub-autopilot\claims\new.claim'))) 'p5 A2 import: a claim file carrying a credential is not copied' $impP5.Out
Assert-That (-not (Test-Path -LiteralPath (Join-Path $repoP5 '.codex-state\doctrine\last-seen.json'))) 'p5 A2 import: a doctrine file carrying a credential is not copied' $impP5.Out
Assert-That ((Test-Path -LiteralPath (Join-Path $repoP5 '.codex-state\doctrine\clean.json'))) 'p5 A2 import: a clean doctrine file still comes across'
Assert-That (([IO.File]::ReadAllText((Join-Path $homeP5 '.claude\CLAUDE.md'))) -ceq 'NEWER-LOCAL') 'p5 A3 import: an OLDER bundle file never replaces a NEWER host file (README: never overwrites a newer local file)' ([IO.File]::ReadAllText((Join-Path $homeP5 '.claude\CLAUDE.md')))
Assert-That (([IO.File]::ReadAllText((Join-Path $homeP5 '.claude\settings.json'))) -ceq '{"env":{"DB_PASSWORD":"local-password"},"path":"C:/Users/obabalola/tool"}') 'p5 A4 import: with no bundle settings.json, the host''s credential-bearing settings.json is left byte-for-byte as it was' ([IO.File]::ReadAllText((Join-Path $homeP5 '.claude\settings.json')))

# ---------------------------------------------------------------- p5 A7: sshd_config keyword grammar (OpenSSH takes '=' as the delimiter and quoted keywords)
if ($fnLoaded -and (Get-Command Set-SshdKeyOnlyConfig -ErrorAction SilentlyContinue)) {
    $p5a7 = [ordered]@{
        'Match=User with a password-enabling rule'    = "PubkeyAuthentication yes`r`nPasswordAuthentication no`r`nMatch=User obabalola`r`n    PasswordAuthentication yes`r`n"
        'a quoted Match keyword'                       = "PasswordAuthentication no`r`n`"Match`" User obabalola`r`n    PasswordAuthentication yes`r`n"
        'a quoted Include keyword'                     = "PasswordAuthentication no`r`n`"Include`" C:/ProgramData/ssh/site.conf`r`n"
        'Include=path'                                 = "PasswordAuthentication no`r`nInclude=C:/ProgramData/ssh/site.conf`r`n"
        'a quoted password keyword inside a Match'     = "PasswordAuthentication no`r`nMatch User x`r`n    `"PasswordAuthentication`" = `"yes`"`r`n"
        'ChallengeResponseAuthentication inside Match' = "PasswordAuthentication no`r`nMatch User x`r`n    ChallengeResponseAuthentication yes`r`n"
    }
    foreach ($k in $p5a7.Keys) {
        $d = New-Scratch 'p5-sshd'
        $cfg = Join-Path $d 'sshd_config'
        [IO.File]::WriteAllText($cfg, $p5a7[$k])
        $threw = $false
        try { Set-SshdKeyOnlyConfig -ConfigPath $cfg -BackupDir (Join-Path $d 'bk') -Stamp 'P5' -Validate { 0 } } catch { $threw = $true }
        Assert-That ($threw -and ([IO.File]::ReadAllText($cfg) -ceq $p5a7[$k])) "p5 A7 sshd: $k is refused and the file is left unchanged"
    }
    $dOk = New-Scratch 'p5-sshd-ok'
    $cfgOk2 = Join-Path $dOk 'sshd_config'
    [IO.File]::WriteAllText($cfgOk2, "PasswordAuthentication=yes`r`nMatch User x`r`n    PasswordAuthentication no`r`n")
    Set-SshdKeyOnlyConfig -ConfigPath $cfgOk2 -BackupDir (Join-Path $dOk 'bk') -Stamp 'P5' -Validate { 0 }
    $okLines = @([IO.File]::ReadAllLines($cfgOk2))
    Assert-That (($okLines | Where-Object { $_ -match '^PasswordAuthentication=yes' }).Count -eq 0 -and $okLines -contains 'PasswordAuthentication no') 'p5 A7 sshd: a global PasswordAuthentication=yes (= delimiter) is replaced by key-only' ($okLines -join '|')
} else { Assert-That $false 'p5 A7: Set-SshdKeyOnlyConfig is not defined' }

# ---------------------------------------------------------------- p5 A9: scan and use see the same bytes (Import reads a private snapshot; Export re-checks the stage after tar)
if ($fnLoaded -and (Get-Command New-ImportSnapshot -ErrorAction SilentlyContinue) -and (Get-Command Get-StagedCredentialHits -ErrorAction SilentlyContinue)) {
    $a9Bundle = New-Scratch 'p5-a9-bundle'
    [IO.File]::WriteAllText((Join-Path $a9Bundle 'hooks-config.json'), '{}')
    $a9Snap = New-ImportSnapshot -From $a9Bundle -To (Join-Path (New-Scratch 'p5-a9-snaps') 'snap')
    [IO.File]::WriteAllText((Join-Path $a9Bundle 'hooks-config.json'), '{"api_key":"foreign-key-1234"}')
    Assert-That (([IO.File]::ReadAllText((Join-Path $a9Snap 'hooks-config.json'))) -ceq '{}') 'p5 A9 import: a bundle file replaced AFTER the snapshot does not change what the scan and the copy read'
    $a9Stage = New-Scratch 'p5-a9-stage'
    [IO.File]::WriteAllText((Join-Path $a9Stage 'a.json'), '{}')
    Assert-That (@(Get-StagedCredentialHits -Stage $a9Stage).Count -eq 0) 'p5 A9 export: a clean stage has no hits'
    [IO.File]::WriteAllText((Join-Path $a9Stage 'a.json'), '{"api_key":"foreign-key-1234"}')
    Assert-That (@(Get-StagedCredentialHits -Stage $a9Stage).Count -eq 1) 'p5 A9 export: a staged file that turned into a credential after the scan is a hit the archive step refuses'
    $a9Home = New-Scratch 'p5-a9-home'
    $a9State = New-Scratch 'p5-a9-state'
    $a9B = New-Scratch 'p5-a9-b'
    [IO.File]::WriteAllText((Join-Path $a9B 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
    New-Item -ItemType Directory -Force -Path (Join-Path $a9B 'home-claude') | Out-Null
    [IO.File]::WriteAllText((Join-Path $a9B 'home-claude\CLAUDE.md'), 'from-bundle')
    $a9Imp = Invoke-Setup $a9Home @('-Phase', 'Import', '-StateRoot', $a9State, '-Workspace', (New-Scratch 'p5-a9-ws'), '-BundlePath', $a9B, '-SourceHost', 'NOT-THIS-HOST')
    Assert-That ($a9Imp.Exit -eq 0 -and $a9Imp.Out -match 'Snapshot the bundle' -and (Test-Path -LiteralPath (Join-Path $a9Home '.claude\CLAUDE.md'))) 'p5 A9 import: Import copies from a snapshot and still lands the file' "exit=$($a9Imp.Exit)`n$($a9Imp.Out)"
    Assert-That (@(Get-ChildItem -LiteralPath $a9State -Directory -Filter 'import-snapshot-*' -ErrorAction SilentlyContinue).Count -eq 0) 'p5 A9 import: the snapshot is removed after the copy'
} else { Assert-That $false 'p5 A9: New-ImportSnapshot / Get-StagedCredentialHits are not defined' }

# ---------------------------------------------------------------- p5 A6: the gate lock is machine-wide, so a caller string cannot give Gate and LeaseRelease different locks
if ($fnLoaded -and (Test-Has 'Get-GateTaskArgument' 'p5 A6 tasks')) {
    $savedBound = $script:SetupBound
    $script:SetupBound = @{ StateRoot = $true; Workspace = $true; LockRoot = $true }
    $p5a6arg = Get-GateTaskArgument -Pwsh 'C:\pwsh.exe' -Self 'C:\s.ps1' -Spec @{ Name = 'T'; Script = 'C:\x\y.ps1'; ArgLine = ''; Mode = 'Lease' }
    $script:SetupBound = $savedBound
    Assert-That (-not $p5a6arg.Contains('-LockRoot')) 'p5 A6 tasks: a registered task never carries -LockRoot (every task and every release uses the one machine-wide lock)' $p5a6arg
}
$p5a6ws = New-Scratch 'p5-a6-ws'
$script:AutoScratchLock = $false   # these calls must NOT get the -AllowScratchLockRoot test seam
$p5a6 = Invoke-Setup (New-Scratch 'p5-a6-home') @('-Phase', 'LeaseRelease', '-LockRoot', (New-Scratch 'p5-a6-locks'), '-StateRoot', (New-Scratch 'p5-a6-state'), '-Workspace', $p5a6ws)
Assert-That ($p5a6.Exit -eq 4 -and $p5a6.Out -match 'LOCKROOT_NOT_MACHINE_WIDE') 'p5 A6 release: a non-default -LockRoot without the test seam is refused (exit 4, LOCKROOT_NOT_MACHINE_WIDE) before the lease is read' "exit=$($p5a6.Exit)`n$($p5a6.Out)"
$p5a6gState = New-Scratch 'p5-a6g-state'
$p5a6g = Invoke-Setup (New-Scratch 'p5-a6g-home') @('-Phase', 'Gate', '-GateMode', 'Lease', '-GateScript', $PSCommandPath, '-LockRoot', (New-Scratch 'p5-a6g-locks'), '-StateRoot', $p5a6gState, '-Workspace', $p5a6ws)
$script:AutoScratchLock = $true
# a Gate run logs to <StateRoot>\logs\gate.log only (no console), so the typed refusal is read from there
$p5a6gLog = Join-Path $p5a6gState 'logs\gate.log'
$p5a6g.Out = $p5a6g.Out + "`n" + $(if (Test-Path -LiteralPath $p5a6gLog) { [IO.File]::ReadAllText($p5a6gLog) } else { '' })
Assert-That ($p5a6g.Exit -eq 4 -and $p5a6g.Out -match 'LOCKROOT_NOT_MACHINE_WIDE') 'p5 A6 gate: the same refusal on a Lease-gated run (it would otherwise hold a lock no release can see)' "exit=$($p5a6g.Exit)`n$($p5a6g.Out)"

# ================================================================ adversary pass 6 (head da472094, r10): one section per finding class
# ---------------------------------------------------------------- r10 A1: more credential spellings (YAML colon, short and numeric values under a secret name)
if ($fnLoaded -and (Get-Command Test-CredentialText -ErrorAction SilentlyContinue)) {
    $r10a1 = [ordered]@{
        'a YAML password line'                   = "password: hunter2`n"
        'a YAML key under a list dash'           = "- api_key: abcd1234`n"
        'a three character unquoted password'    = "DB_PASSWORD=abc`n"
        'an all-digit unquoted password'         = "DB_PASSWORD=123456`n"
        'an ini password with spaces round ='    = "db_password = 7`n"
        'a multi-word YAML passphrase'           = "password: correct horse battery staple`n"
        'a cmd value that starts with a dollar'  = "@set DB_PASSWORD=`$hunter2`n"
        'a value that starts with an at sign'    = "DB_PASSWORD=@hunter2`n"
        'a value that only STARTS like false'    = "DB_PASSWORD=false-password`n"
        'a value that only STARTS like null'     = "DB_PASSWORD=null-secret`n"
    }
    foreach ($k in $r10a1.Keys) { Assert-That (Test-CredentialText $r10a1[$k]) "r10 A1 Test-CredentialText: $k is a credential" $r10a1[$k] }
    $r10a1ok = [ordered]@{
        'a YAML boolean under a password key'    = "password_required: true`n"
        'a boolean under a password key'         = "password_required = true`n"
        'a numeric token limit (still benign)'   = "max_tokens = 4096`n"
        'a YAML token count'                     = "token_count: 12`n"
    }
    foreach ($k in $r10a1ok.Keys) { Assert-CredentialOkOrRetired 'r10 A1 Test-CredentialText:' $k $r10a1ok[$k] }
} else { Assert-That $false 'r10 A1: Test-CredentialText is not defined' }

# ---------------------------------------------------------------- r10 A2: Import never copies a credential-bearing host file into its backup folder
# (the host settings.json in $homeP5 carries DB_PASSWORD; $impP5 above imported over it)
$r10a2Backups = Join-Path (New-Scratch 'p5-state') 'backups'
$r10a2Hits = @(Get-ChildItem -LiteralPath $r10a2Backups -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object { ([IO.File]::ReadAllText($_.FullName)) -match 'local-password' })
Assert-That ($r10a2Hits.Count -eq 0) 'r10 A2 import: the credential-bearing host settings.json is not copied into backups\' (($r10a2Hits | ForEach-Object FullName) -join ';')
Assert-That ($impP5.Out.Contains('IMPORT_BACKUP_SKIPPED_CREDENTIAL_FILE')) 'r10 A2 import: the skipped backup is logged as IMPORT_BACKUP_SKIPPED_CREDENTIAL_FILE' $impP5.Out
Assert-That (@(Get-ChildItem -LiteralPath $r10a2Backups -Recurse -File -Force -Filter 'CLAUDE.md' -ErrorAction SilentlyContinue).Count -ge 1) 'r10 A2 import: a clean file (CLAUDE.md) is still backed up'

# ---------------------------------------------------------------- r10 A3: the archive is judged by what it CONTAINS (the stage can change between scan, tar and re-check)
if ($fnLoaded -and (Test-Has 'Get-ArchiveCredentialHits' 'r10 A3 export')) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $a3Dir = New-Scratch 'r10-a3'
    $a3Clean = Join-Path $a3Dir 'clean.zip'; $a3Dirty = Join-Path $a3Dir 'dirty.zip'
    foreach ($z in @(@($a3Clean, '{}'), @($a3Dirty, '{"api_key":"foreign-key-1234"}'))) {
        $zf = [IO.Compression.ZipFile]::Open($z[0], 'Create')
        try { $e = $zf.CreateEntry('./hooks-config.json'); $sw = [IO.StreamWriter]::new($e.Open()); $sw.Write($z[1]); $sw.Dispose() } finally { $zf.Dispose() }
    }
    Assert-That (@(Get-ArchiveCredentialHits -Zip $a3Clean).Count -eq 0) 'r10 A3 export: an archive with only clean entries has no hits'
    Assert-That (@(Get-ArchiveCredentialHits -Zip $a3Dirty).Count -eq 1) 'r10 A3 export: an archive entry that carries a credential is a hit, whatever the stage holds now'
    Assert-That (@(Get-ArchiveCredentialHits -Zip (Join-Path $a3Dir 'absent.zip')).Count -ge 1) 'r10 A3 export: an archive that cannot be read is a hit (fail closed)'
}

# ---------------------------------------------------------------- r10 A4: a meter task the owner ENABLED gets its own definition back when the install replaced it
if ($fnLoaded -and (Get-Command Invoke-MeterInstalls -ErrorAction SilentlyContinue)) {
    $script:StubXmlAfter = $true
    $r10a4 = Invoke-A7Run 'r10-a4-a' @{ 'MLV-DiskAutoReclaim' = 'Ready'; 'MLV-PwshAutoRepin' = 'Ready' }
    Assert-That (@($script:StubCalls | Where-Object { $_ -like 'REGISTER MLV-DiskAutoReclaim*' }).Count -ge 1) 'r10 A4 meters: an enabled task whose definition the install replaced is re-registered from the saved XML' ($script:StubCalls -join ',')
    Assert-That (@($script:StubCalls | Where-Object { $_ -like 'DISABLE*' }).Count -eq 0) 'r10 A4 meters: restoring an enabled task does not disable it' ($script:StubCalls -join ',')
    $script:StubXmlAfter = $false
    $null = Invoke-A7Run 'r10-a4-b' @{ 'MLV-DiskAutoReclaim' = 'Ready'; 'MLV-PwshAutoRepin' = 'Ready' }
    Assert-That ($script:StubCalls.Count -eq 0) 'r10 A4 meters: an enabled task the install left unchanged is not touched' ($script:StubCalls -join ',')
    $script:StubExportFail = $true
    $null = Invoke-A7Run 'r10-a4-c' @{ 'MLV-DiskAutoReclaim' = 'Ready'; 'MLV-PwshAutoRepin' = 'Ready' }
    $script:StubExportFail = $false
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $script:StubRepo '.claude-state\disk-hygiene\installed.marker'))) 'r10 A4 meters: an enabled task whose definition cannot be saved is not installed over'
    # ---- r10 A5: a Disable that the second post-install read hides is still performed, and verified
    $script:StubSecondReadFails = $true
    try { $null = Invoke-A7Run 'r10-a5-a' @{ 'MLV-DiskAutoReclaim' = 'Disabled'; 'MLV-PwshAutoRepin' = 'ABSENT' } } catch { Write-Host "r10 A5 meters: the run threw: $($_.Exception.Message)" }
    $script:StubSecondReadFails = $false
    Assert-That (@($script:StubCalls | Where-Object { $_ -like 'DISABLE MLV-PwshAutoRepin*' }).Count -ge 1) 'r10 A5 meters: the task is Disabled even when the read after the register fails' ($script:StubCalls -join ',')
}

# ---------------------------------------------------------------- r10 A6: sshd keyword aliases (first value wins) and Match overrides that re-open another door
if ($fnLoaded -and (Get-Command Set-SshdKeyOnlyConfig -ErrorAction SilentlyContinue)) {
    $dA6 = New-Scratch 'r10-a6-a'
    $cfgA6 = Join-Path $dA6 'sshd_config'
    [IO.File]::WriteAllText($cfgA6, "DSAAuthentication no`r`nPubkeyAuthentication yes`r`nPasswordAuthentication yes`r`nChallengeResponseAuthentication yes`r`nKbdInteractiveAuthentication no`r`n")
    Set-SshdKeyOnlyConfig -ConfigPath $cfgA6 -BackupDir (Join-Path $dA6 'bk') -Stamp 'R10' -Validate { 0 }
    $a6Lines = @([IO.File]::ReadAllLines($cfgA6))
    $a6Active = @($a6Lines | Where-Object { $_ -notmatch '^\s*#' })
    $a6FirstPub = ($a6Active | Where-Object { $_ -match '^\s*(PubkeyAuthentication|DSAAuthentication)\s' } | Select-Object -First 1)
    $a6FirstKbd = ($a6Active | Where-Object { $_ -match '^\s*(KbdInteractiveAuthentication|ChallengeResponseAuthentication|SkeyAuthentication)\s' } | Select-Object -First 1)
    Assert-That ($a6FirstPub -ceq 'PubkeyAuthentication yes') 'r10 A6 sshd: the first effective public-key line (aliases included) is PubkeyAuthentication yes' ($a6Lines -join '|')
    Assert-That ($a6FirstKbd -ceq 'KbdInteractiveAuthentication no') 'r10 A6 sshd: the first effective keyboard-interactive line (aliases included) is KbdInteractiveAuthentication no' ($a6Lines -join '|')
    $a6Refuse = [ordered]@{
        'SkeyAuthentication yes inside a Match'          = "PasswordAuthentication no`r`nMatch User x`r`n    SkeyAuthentication yes`r`n"
        'PubkeyAuthentication no inside a Match'         = "PasswordAuthentication no`r`nMatch User x`r`n    PubkeyAuthentication no`r`n"
        'DSAAuthentication no inside a Match'            = "PasswordAuthentication no`r`nMatch User x`r`n    DSAAuthentication no`r`n"
        'KbdInteractiveAuthentication true inside Match' = "PasswordAuthentication no`r`nMatch User x`r`n    KbdInteractiveAuthentication true`r`n"
    }
    foreach ($k in $a6Refuse.Keys) {
        $d = New-Scratch 'r10-a6-refuse'
        $cfg = Join-Path $d 'sshd_config'
        [IO.File]::WriteAllText($cfg, $a6Refuse[$k])
        $threw = $false
        try { Set-SshdKeyOnlyConfig -ConfigPath $cfg -BackupDir (Join-Path $d 'bk') -Stamp 'R10' -Validate { 0 } } catch { $threw = $true }
        Assert-That ($threw -and ([IO.File]::ReadAllText($cfg) -ceq $a6Refuse[$k])) "r10 A6 sshd: $k is refused and the file is left unchanged"
    }
} else { Assert-That $false 'r10 A6: Set-SshdKeyOnlyConfig is not defined' }

# ================================================================ adversary pass 7 (head 9ef92a95, r10 second iteration)
# ---------------------------------------------------------------- r10 A2: the backup writes exactly the bytes that were judged (one read, then a write of those bytes)
if ($fnLoaded -and (Test-Has 'Copy-CleanBackupFile' 'r10 A2 backup')) {
    $a2Dir = New-Scratch 'r10-a2'
    $a2Dirty = Join-Path $a2Dir 'settings.json'; $a2Clean = Join-Path $a2Dir 'CLAUDE.md'
    [IO.File]::WriteAllText($a2Dirty, '{"env":{"DB_PASSWORD":"local-password"}}'); [IO.File]::WriteAllText($a2Clean, 'owner instructions')
    $a2Dest = New-Scratch 'r10-a2-bk'
    Assert-That ((Copy-CleanBackupFile -Path $a2Dirty -DestinationDir $a2Dest) -eq $false -and -not (Test-Path -LiteralPath (Join-Path $a2Dest 'settings.json'))) 'r10 A2 backup: a file that carries a credential is not written to the backup folder'
    Assert-That ((Copy-CleanBackupFile -Path $a2Clean -DestinationDir $a2Dest) -eq $true -and ([IO.File]::ReadAllText((Join-Path $a2Dest 'CLAUDE.md')) -ceq 'owner instructions')) 'r10 A2 backup: a clean file is written byte for byte'
    Assert-That ((Copy-CleanBackupFile -Path (Join-Path $a2Dir 'absent.json') -DestinationDir $a2Dest) -eq $false) 'r10 A2 backup: an unreadable (absent) file is not written'
}

# ---------------------------------------------------------------- r10 A3: -Phase Tasks never replaces a task whose state it could not read
if ($fnLoaded -and (Test-Has 'Invoke-PhaseTasks' 'r10 A3 tasks')) {
    $script:T3Calls = [Collections.Generic.List[string]]::new()
    function Get-ScheduledTask { [CmdletBinding()] param($TaskName, $TaskPath) Write-Error 'RPC server unavailable reading the scheduled task' }
    function New-ScheduledTaskPrincipal { param([Parameter(ValueFromRemainingArguments)]$a) 'principal' }
    function New-ScheduledTaskAction { param([Parameter(ValueFromRemainingArguments)]$a) 'action' }
    function New-ScheduledTaskTrigger { param([Parameter(ValueFromRemainingArguments)]$a) 'trigger' }
    function New-ScheduledTaskSettingsSet { param([Parameter(ValueFromRemainingArguments)]$a) 'settings' }
    function Register-ScheduledTask { param($TaskName, [Parameter(ValueFromRemainingArguments)]$a) $script:T3Calls.Add("REGISTER $TaskName") }
    function Enable-ScheduledTask { param($TaskName, [Parameter(ValueFromRemainingArguments)]$a) $script:T3Calls.Add("ENABLE $TaskName") }
    $script:Failures.Clear()
    Invoke-PhaseTasks
    Assert-That ($script:T3Calls.Count -eq 0) 'r10 A3 tasks: a task whose state cannot be read is not registered over' ($script:T3Calls -join ',')
    Assert-That (@($script:Failures | Where-Object { $_ -like 'TASK_STATE_UNREADABLE*' }).Count -ge 1) 'r10 A3 tasks: the unreadable task is a failure of the run (exit 1)' ($script:Failures -join ',')
    $script:Failures.Clear()
}

# ---------------------------------------------------------------- r10 A4: a dry-run LeaseRelease takes no gate lock and reads no lease
$a4Locks = New-Scratch 'r10-a4-locks'
$a4 = Invoke-Setup (New-Scratch 'r10-a4-home') @('-Phase', 'LeaseRelease', '-WhatIf', '-LockRoot', $a4Locks, '-StateRoot', (New-Scratch 'r10-a4-state'), '-Workspace', (New-Scratch 'r10-a4-ws'))
Assert-That (@(Get-ChildItem -LiteralPath $a4Locks -Recurse -Force -ErrorAction SilentlyContinue).Count -eq 0) 'r10 A4 release: -WhatIf creates nothing under the gate lock folder (no lock was taken)' "exit=$($a4.Exit)`n$($a4.Out)"
Assert-That ($a4.Exit -eq 0 -and $a4.Out -match 'SKIP \(WhatIf\) LeaseRelease') 'r10 A4 release: -WhatIf says it skipped the release (exit 0)' "exit=$($a4.Exit)`n$($a4.Out)"

# ---------------------------------------------------------------- r10 A5: native exit codes stay under Invoke-Native's control whatever the caller's preference
if ($fnLoaded -and (Test-Has 'Invoke-Native' 'r10 A5 native')) {
    $savedNative = $global:PSNativeCommandUseErrorActionPreference
    $global:PSNativeCommandUseErrorActionPreference = $true
    $a5threw = $false; $a5code = $null
    try { $a5code = Invoke-Native -Exe 'git' -Arguments @('rev-parse', '--verify', 'refs/heads/r10-no-such-branch-anywhere') -AllowFail } catch { $a5threw = $true }
    $a5threw2 = $false; $a5code2 = $null
    try { $a5code2 = Invoke-Native -Exe 'git' -Arguments @('rev-parse', '--verify', 'refs/heads/r10-no-such-branch-anywhere') -OkCodes @(0, 128) } catch { $a5threw2 = $true }
    $global:PSNativeCommandUseErrorActionPreference = $savedNative
    Assert-That (-not $a5threw -and $a5code -eq 128) 'r10 A5 native: -AllowFail returns the exit code even when $PSNativeCommandUseErrorActionPreference is $true' "threw=$a5threw code=$a5code"
    Assert-That (-not $a5threw2 -and $a5code2 -eq 128) 'r10 A5 native: -OkCodes is honoured even when $PSNativeCommandUseErrorActionPreference is $true' "threw=$a5threw2 code=$a5code2"
}

# ================================================================ r11 (head 0eb956f3): S-B1 triple-quoted TOML, F-B1 typed Export refusal, S-B2 locked Import replace
# ---------------------------------------------------------------- r11 S-B1: TOML basic and literal triple-quoted strings, single line and multiline
$q3 = '"' * 3
$s3 = "'" * 3
$r11Toml = [ordered]@{
    'S-B1 TOML basic triple-quoted, one line'       = 'api_key = ' + $q3 + 'synthetic-secret-1234' + $q3
    'S-B1 TOML literal triple-quoted, one line'     = 'api_key = ' + $s3 + 'synthetic-secret-1234' + $s3
    'S-B1 TOML basic triple-quoted, multiline'      = 'api_key = ' + $q3 + "`n" + 'synthetic-secret-1234' + "`n" + $q3
    'S-B1 TOML literal triple-quoted, multiline'    = 'api_key = ' + $s3 + "`n" + 'synthetic-secret-1234' + "`n" + $s3
    'S-B1 TOML multiline value that starts with a quote' = 'password = ' + $q3 + "`n" + '"synthetic-secret-1234"' + "`n" + $q3
    'S-B1 TOML quoted key, CRLF, token name'        = '"auth_token" = ' + $s3 + "`r`n" + 'synthetic-secret-1234' + "`r`n" + $s3
    'S-B1 TOML table entry, no spaces round ='      = "[auth]`ntoken=" + $q3 + 'synthetic-secret-1234' + $q3
}
$r11TomlOk = [ordered]@{
    'S-B1 control: an empty basic triple-quoted value'   = 'api_key = ' + $q3 + $q3
    'S-B1 control: an empty literal triple-quoted value' = 'api_key = ' + $s3 + $s3
    'S-B1 control: a triple-quoted value under a non-secret key' = 'description = ' + $q3 + 'synthetic text' + $q3
}
if ($fnLoaded -and (Get-Command Test-CredentialText -ErrorAction SilentlyContinue)) {
    foreach ($k in $r11Toml.Keys) { Assert-That (Test-CredentialText $r11Toml[$k]) "r11 $k is a credential (Test-CredentialText)" $r11Toml[$k] }
    foreach ($k in $r11TomlOk.Keys) { Assert-CredentialOkOrRetired 'r11' $k $r11TomlOk[$k] }
} else { Assert-That $false 'r11 S-B1: Test-CredentialText is not defined' }
# the same detector is behind every consumer: the Export scan, the archive re-check, the Import bundle/host guard and the backup copy
if ($fnLoaded -and (Get-Command Invoke-StagedSecretScan -ErrorAction SilentlyContinue)) {
    $sb1Stage = New-Scratch 'r11-sb1-stage'
    $sb1Names = @{}
    $i = 0
    foreach ($k in $r11Toml.Keys) { $i++; $n = "toml$i.toml"; $sb1Names[$k] = $n; [IO.File]::WriteAllText((Join-Path $sb1Stage $n), $r11Toml[$k]) }
    [void](Invoke-StagedSecretScan -Stage $sb1Stage)
    foreach ($k in $r11Toml.Keys) { Assert-That (-not (Test-Path -LiteralPath (Join-Path $sb1Stage $sb1Names[$k]))) "r11 $k`: removed by the Export scan" }
}
if ($fnLoaded -and (Get-Command Get-ArchiveCredentialHits -ErrorAction SilentlyContinue)) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $sb1Zip = Join-Path (New-Scratch 'r11-sb1-zip') 'toml.zip'
    $zf = [IO.Compression.ZipFile]::Open($sb1Zip, 'Create')
    try { $e = $zf.CreateEntry('./service.toml'); $sw = [IO.StreamWriter]::new($e.Open()); $sw.Write($r11Toml['S-B1 TOML basic triple-quoted, multiline']); $sw.Dispose() } finally { $zf.Dispose() }
    Assert-That (@(Get-ArchiveCredentialHits -Zip $sb1Zip).Count -eq 1) 'r11 S-B1: a triple-quoted TOML credential inside the archive is a hit (archive re-check)'
}
if ($fnLoaded -and (Get-Command Get-ImportCredentialSkips -ErrorAction SilentlyContinue)) {
    $sb1Src = New-Scratch 'r11-sb1-isrc'; $sb1Dst = New-Scratch 'r11-sb1-idst'
    [IO.File]::WriteAllText((Join-Path $sb1Src 'bundle.toml'), $r11Toml['S-B1 TOML literal triple-quoted, one line'])
    [IO.File]::WriteAllText((Join-Path $sb1Src 'host.toml'), '[x]')
    [IO.File]::WriteAllText((Join-Path $sb1Dst 'host.toml'), $r11Toml['S-B1 TOML basic triple-quoted, one line'])
    $sb1Skips = @(Get-ImportCredentialSkips -SourceDir $sb1Src -DestDir $sb1Dst)
    Assert-That (@($sb1Skips | Where-Object { $_.Source -like '*bundle.toml' -and $_.Reason -like 'bundle file carries a credential' }).Count -eq 1) 'r11 S-B1: Import refuses a bundle file with a triple-quoted TOML credential' (($sb1Skips | ForEach-Object Reason) -join ';')
    Assert-That (@($sb1Skips | Where-Object { $_.Source -like '*host.toml' -and $_.Reason -like 'host file carries a credential' }).Count -eq 1) 'r11 S-B1: Import keeps a host file with a triple-quoted TOML credential' (($sb1Skips | ForEach-Object Reason) -join ';')
}
if ($fnLoaded -and (Get-Command Copy-CleanBackupFile -ErrorAction SilentlyContinue)) {
    $sb1Bk = New-Scratch 'r11-sb1-bk'
    $sb1Settings = Join-Path (New-Scratch 'r11-sb1-bkhome') 'config.toml'
    [IO.File]::WriteAllText($sb1Settings, $r11Toml['S-B1 TOML literal triple-quoted, multiline'])
    Assert-That ((Copy-CleanBackupFile -Path $sb1Settings -DestinationDir $sb1Bk) -eq $false -and -not (Test-Path -LiteralPath (Join-Path $sb1Bk 'config.toml'))) 'r11 S-B1: the backup never copies a file with a triple-quoted TOML credential'
}

# ---------------------------------------------------------------- r11 F-B1: Export never exits 0 with a bundle that lacks a staged file; one typed line per dropped file, never the value
function New-ExportFixture([string]$Name, [bool]$WithHits) {
    $ws = New-Scratch "$Name-ws"
    $repo = Join-Path $ws 'MLV-App'
    $cs = Join-Path $repo '.claude-state'
    [void][IO.Directory]::CreateDirectory((Join-Path $cs 'hub-autopilot'))
    [void][IO.Directory]::CreateDirectory((Join-Path $cs 'kernel'))
    [IO.File]::WriteAllText((Join-Path $cs 'hub-autopilot\clean.ps1'), "Write-Output 'ok'`r`n")
    if ($WithHits) {
        $gate = @(1..21 | ForEach-Object { "# filler line $_" }) + @('$AhgToken = ''TYPED_REFUSAL_STRING_R11''', '# end')
        [IO.File]::WriteAllText((Join-Path $cs 'hub-autopilot\approved-head-gate.ps1'), (($gate -join "`r`n") + "`r`n"))
        $led = @(1..6 | ForEach-Object { "ledger row $_" }) + @('ruling quotes {"password":"demoValue42"} as a synthetic value', 'ledger row 8')
        [IO.File]::WriteAllText((Join-Path $cs 'kernel\subject-ledger.md'), (($led -join "`n") + "`n"))
    } else {
        [IO.File]::WriteAllText((Join-Path $cs 'kernel\subject-ledger.md'), "ledger row 1`n")
    }
    & git -C $repo init --quiet 2>&1 | Out-Null
    return $ws
}
$fb1Out = New-Scratch 'r11-fb1-out'
$fb1 = Invoke-Setup (New-Scratch 'r11-fb1-home') @('-Phase', 'Export', '-StateRoot', (New-Scratch 'r11-fb1-state'), '-Workspace', (New-ExportFixture 'r11-fb1' $true), '-ExportDir', $fb1Out)
Assert-That ($fb1.Exit -ne 0) 'r11 F-B1 export: a staged file that matches the credential scan ends Export NON-ZERO (never exit 0 with a bundle that lacks it)' "exit=$($fb1.Exit)`n$($fb1.Out)"
Assert-That ($fb1.Out -match 'EXPORT_CREDENTIAL_HIT project-claude-state\\hub-autopilot\\approved-head-gate\.ps1:22\b') 'r11 F-B1 export: a typed EXPORT_CREDENTIAL_HIT line names the first file and the line of the match' $fb1.Out
Assert-That ($fb1.Out -match 'EXPORT_CREDENTIAL_HIT project-claude-state\\kernel\\subject-ledger\.md:7\b') 'r11 F-B1 export: a typed EXPORT_CREDENTIAL_HIT line for the second file too (one line per file)' $fb1.Out
Assert-That (-not $fb1.Out.Contains('TYPED_REFUSAL_STRING_R11') -and -not $fb1.Out.Contains('demoValue42')) 'r11 F-B1 export: the matched value never reaches the output'
$fb1Stage = @(Get-ChildItem -LiteralPath $fb1Out -Directory -Filter 'bundle-*' -ErrorAction SilentlyContinue | Select-Object -First 1)
$fb1Report = if ($fb1Stage.Count) { Join-Path $fb1Stage[0].FullName 'SECRET-SCAN.txt' } else { '' }
$fb1ReportText = if ($fb1Report -and (Test-Path -LiteralPath $fb1Report)) { [IO.File]::ReadAllText($fb1Report) } else { '' }
Assert-That ($fb1ReportText -match 'EXPORT_CREDENTIAL_HIT project-claude-state\\hub-autopilot\\approved-head-gate\.ps1:22\b' -and $fb1ReportText -match 'EXPORT_CREDENTIAL_HIT project-claude-state\\kernel\\subject-ledger\.md:7\b') 'r11 F-B1 export: SECRET-SCAN.txt lists the same typed lines' $fb1ReportText
Assert-That (-not $fb1ReportText.Contains('TYPED_REFUSAL_STRING_R11') -and -not $fb1ReportText.Contains('demoValue42')) 'r11 F-B1 export: SECRET-SCAN.txt never carries the matched value'
$fb1c = Invoke-Setup (New-Scratch 'r11-fb1c-home') @('-Phase', 'Export', '-StateRoot', (New-Scratch 'r11-fb1c-state'), '-Workspace', (New-ExportFixture 'r11-fb1c' $false), '-ExportDir', (New-Scratch 'r11-fb1c-out'))
Assert-That ($fb1c.Exit -eq 0 -and $fb1c.Out -notmatch 'EXPORT_CREDENTIAL_HIT') 'r11 F-B1 export (control): a stage with no match still exits 0 with no typed hit' "exit=$($fb1c.Exit)`n$($fb1c.Out)"
if ($fnLoaded -and (Get-Command Test-StagedBundleClean -ErrorAction SilentlyContinue)) {
    $fb1Stage2 = New-Scratch 'r11-fb1-fn-stage'
    [IO.File]::WriteAllText((Join-Path $fb1Stage2 'one.ps1'), "# a`n`$AhgToken = 'TYPED_REFUSAL_STRING_R11'`n")
    $script:Failures.Clear()
    [void](Test-StagedBundleClean -Stage $fb1Stage2)
    Assert-That (@($script:Failures | Where-Object { $_ -ceq 'EXPORT_CREDENTIAL_HIT one.ps1:2' }).Count -eq 1) 'r11 F-B1 scan: Test-StagedBundleClean records EXPORT_CREDENTIAL_HIT <relative path>:<line> as a failure of the run' ($script:Failures -join ',')
    $script:Failures.Clear()
}

# ---------------------------------------------------------------- r11 S-B2: Import replaces a host file only through an exclusive handle that also judged its bytes
if ($fnLoaded -and (Test-Has 'Invoke-ImportRobocopy' 'r11 S-B2')) {
    $b2Src = New-Scratch 'r11-b2-src'; $b2Dst = New-Scratch 'r11-b2-dst'
    foreach ($d in 'hooks') { [void][IO.Directory]::CreateDirectory((Join-Path $b2Src $d)); [void][IO.Directory]::CreateDirectory((Join-Path $b2Dst $d)) }
    $newer = [DateTime]::UtcNow.AddMinutes(10); $older = [DateTime]::UtcNow.AddDays(-3)
    foreach ($f in @(@('hooks\service.json', '{"v":"bundle-service"}', '{}', $older), @('hooks\other.json', '{"v":"bundle-other"}', '{"v":"host-other"}', $older), @('hooks\keep.json', '{"v":"bundle-old"}', '{"v":"host-newer"}', [DateTime]::UtcNow.AddDays(1)), @('hooks\fresh.json', '{"v":"bundle-fresh"}', $null, $null))) {
        [IO.File]::WriteAllText((Join-Path $b2Src $f[0]), $f[1]); (Get-Item -LiteralPath (Join-Path $b2Src $f[0])).LastWriteTimeUtc = $(if ($f[0] -like '*keep*') { [DateTime]::UtcNow.AddDays(-5) } else { $newer })
        if ($null -ne $f[2]) { [IO.File]::WriteAllText((Join-Path $b2Dst $f[0]), $f[2]); (Get-Item -LiteralPath (Join-Path $b2Dst $f[0])).LastWriteTimeUtc = $f[3] }
    }
    $svc = Join-Path $b2Dst 'hooks\service.json'
    # the seam runs between the inspection pass and the replace: an editor saves a credential into the host file in that window
    $script:ImportBeforeReplaceHook = { param($From, $To) [IO.File]::WriteAllText((Join-Path $To 'hooks\service.json'), '{"api_key":"synthetic-local-key"}') }
    $script:Failures.Clear(); $script:ImportSkippedDest.Clear()
    $logBefore = if (Test-Path -LiteralPath $script:LogFile) { [IO.File]::ReadAllText($script:LogFile).Length } else { 0 }
    try { Invoke-ImportRobocopy -From $b2Src -To $b2Dst -Extra @('/E', '/XO', '/XF', '*.log') } finally { $script:ImportBeforeReplaceHook = $null }
    $b2Log = [IO.File]::ReadAllText($script:LogFile).Substring($logBefore)
    Assert-That (([IO.File]::ReadAllText($svc)) -ceq '{"api_key":"synthetic-local-key"}') 'r11 S-B2 import: a host file that gained a credential after inspection is NOT replaced by the newer bundle copy' ([IO.File]::ReadAllText($svc))
    Assert-That ($b2Log.Contains("IMPORT_DEST_CHANGED_SKIPPED $svc")) 'r11 S-B2 import: the skip is the typed line IMPORT_DEST_CHANGED_SKIPPED <path>' $b2Log
    Assert-That (@($script:Failures | Where-Object { $_ -like 'IMPORT_DEST_CHANGED_SKIPPED*' }).Count -eq 1) 'r11 S-B2 import: the skip is a failure of the run (non-zero step result)' ($script:Failures -join ',')
    Assert-That ($script:ImportSkippedDest -contains $svc) 'r11 S-B2 import: the skipped destination is remembered (a skipped settings.json is never read as applied)'
    Assert-That (([IO.File]::ReadAllText((Join-Path $b2Dst 'hooks\other.json'))) -ceq '{"v":"bundle-other"}') 'r11 S-B2 import: an unchanged clean host file is still replaced by the newer bundle copy' ([IO.File]::ReadAllText((Join-Path $b2Dst 'hooks\other.json')))
    Assert-That ([IO.File]::GetLastWriteTimeUtc((Join-Path $b2Dst 'hooks\other.json')) -eq [IO.File]::GetLastWriteTimeUtc((Join-Path $b2Src 'hooks\other.json'))) 'r11 S-B2 import: a replaced file keeps the bundle copy''s timestamp (the /XO rule needs it)'
    Assert-That (([IO.File]::ReadAllText((Join-Path $b2Dst 'hooks\keep.json'))) -ceq '{"v":"host-newer"}') 'r11 S-B2 import: an OLDER bundle copy never replaces a newer host file' ([IO.File]::ReadAllText((Join-Path $b2Dst 'hooks\keep.json')))
    Assert-That (([IO.File]::ReadAllText((Join-Path $b2Dst 'hooks\fresh.json'))) -ceq '{"v":"bundle-fresh"}') 'r11 S-B2 import: a file the host lacks is created' ([IO.File]::ReadAllText((Join-Path $b2Dst 'hooks\fresh.json')))
    $script:Failures.Clear(); $script:ImportSkippedDest.Clear()
}
# end to end: a host file another process holds open cannot be locked, so it is skipped with the typed line and the run fails (it used to end in a robocopy exit 8)
$b2eHome = New-Scratch 'r11-b2e-home'
$b2eSvc = Join-Path $b2eHome '.claude\hooks\service.json'
[void][IO.Directory]::CreateDirectory((Split-Path -Parent $b2eSvc))
[IO.File]::WriteAllText($b2eSvc, '{}'); (Get-Item -LiteralPath $b2eSvc).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-3)
$b2eBundle = New-Scratch 'r11-b2e-bundle'
[IO.File]::WriteAllText((Join-Path $b2eBundle 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
[void][IO.Directory]::CreateDirectory((Join-Path $b2eBundle 'home-claude\hooks'))
[IO.File]::WriteAllText((Join-Path $b2eBundle 'home-claude\hooks\service.json'), '{"v":"bundle-service"}')
[IO.File]::WriteAllText((Join-Path $b2eBundle 'home-claude\CLAUDE.md'), 'owner instructions')
$b2eHold = [IO.File]::Open($b2eSvc, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
try { $b2e = Invoke-Setup $b2eHome @('-Phase', 'Import', '-StateRoot', (New-Scratch 'r11-b2e-state'), '-Workspace', (New-Scratch 'r11-b2e-ws'), '-BundlePath', $b2eBundle, '-SourceHost', 'NOT-THIS-HOST') } finally { $b2eHold.Dispose() }
Assert-That ($b2e.Out.Contains("IMPORT_DEST_CHANGED_SKIPPED $b2eSvc")) 'r11 S-B2 import (end to end): a host file another process holds open is skipped with IMPORT_DEST_CHANGED_SKIPPED <path>' $b2e.Out
Assert-That ($b2e.Exit -eq 1 -and ([IO.File]::ReadAllText($b2eSvc)) -ceq '{}') 'r11 S-B2 import (end to end): the run fails (exit 1) and the held host file is untouched' "exit=$($b2e.Exit)"
Assert-That (Test-Path -LiteralPath (Join-Path $b2eHome '.claude\CLAUDE.md')) 'r11 S-B2 import (end to end): the other files still come across after one is skipped' $b2e.Out

# ================================================================ r12 (head 15c4ee5e): adversary pass 8, A1-A5
# ---------------------------------------------------------------- r12 A1: a YAML / JSON FLOW mapping ({password: hunter2}) is a credential line; A2: a boolean word is a whole value, not a prefix
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r12 A1')) {
    $r12Hit = [ordered]@{
        'A1 flow mapping {password: hunter2}'            = '{password: hunter2}'
        'A1 flow mapping, secret after another pair'     = '{name: svc, password: hunter2}'
        'A1 flow mapping nested in a block line'         = 'service: { api_key: abc123def }'
        'A1 flow mapping, TOKEN key'                     = '{token: abcd1234efgh}'
        'A2 cmd set DB_PASSWORD=false <words>'           = '@set DB_PASSWORD=false hunter2'
        'A2 cmd set DB_PASSWORD=null <words>'            = '@set DB_PASSWORD=null secretphrase'
        'A2 cmd set AUTH_TOKEN=null <words>'             = 'set AUTH_TOKEN=null secretphrase'
        'A2 dotenv DB_PASSWORD=true <words>'             = 'DB_PASSWORD=true hunter2'
    }
    $r12Ok = [ordered]@{
        'A1 flow mapping, boolean value'                 = '{password: false}'
        'A1 flow mapping, boolean flag key'              = '{name: svc, has_password: true, other: 1}'
        'A1 flow mapping, null value'                    = '{secret: null}'
        'A1 flow mapping, no secret key'                 = '{name: svc, port: 8080}'
        'A2 bare boolean'                                = 'DB_PASSWORD=false'
        'A2 boolean then a comment'                      = 'DB_PASSWORD=false # off'
        'A2 boolean then the next line'                  = "DB_PASSWORD=false`nOTHER=1"
        'A2 boolean then a semicolon'                    = 'DB_PASSWORD=null;'
    }
    foreach ($k in $r12Hit.Keys) { Assert-That (Test-CredentialText $r12Hit[$k]) "r12 $k is a credential (Test-CredentialText)" $r12Hit[$k] }
    foreach ($k in $r12Ok.Keys) { Assert-CredentialOkOrRetired 'r12' $k $r12Ok[$k] }
    # the same line through the Export stage scan, the Import bundle guard and the Import host guard
    $a1Stage = New-Scratch 'r12-a1-stage'
    [IO.File]::WriteAllText((Join-Path $a1Stage 'service.yaml'), '{password: hunter2}')
    Assert-That (@(Get-StagedCredentialHits -Stage $a1Stage).Count -eq 1) 'r12 A1 export: a staged service.yaml holding {password: hunter2} is a hit'
    $a1Src = New-Scratch 'r12-a1-src'; $a1Dst = New-Scratch 'r12-a1-dst'
    [IO.File]::WriteAllText((Join-Path $a1Src 'bundle.yaml'), '{password: hunter2}')
    [IO.File]::WriteAllText((Join-Path $a1Src 'host.yaml'), 'clean: 1')
    [IO.File]::WriteAllText((Join-Path $a1Dst 'host.yaml'), '{password: hunter2}')
    $a1Skips = @(Get-ImportCredentialSkips -SourceDir $a1Src -DestDir $a1Dst)
    Assert-That (@($a1Skips | Where-Object { $_.Source -like '*bundle.yaml' }).Count -eq 1 -and @($a1Skips | Where-Object { $_.Source -like '*host.yaml' -and $_.Reason -like 'host file carries a credential' }).Count -eq 1) 'r12 A1 import: a flow-mapping credential in the bundle file or in the host file is skipped' (($a1Skips | ForEach-Object Reason) -join ';')
}

# ---------------------------------------------------------------- r12 A3: the settings.json profile rewrite is one locked read-judge-write on the host file
if ($fnLoaded -and (Test-Has 'Update-ImportedSettingsLocked' 'r12 A3')) {
    $a3 = New-Scratch 'r12-a3'; $a3Settings = Join-Path $a3 'settings.json'
    [IO.File]::WriteAllText($a3Settings, '{"path":"C:/Users/obabalola/tool","p2":"C:\\Users\\obabalola\\x"}')
    # r14 A4: the rewrite only touches a file the Import copy wrote and nobody changed since; this direct call stands for that copy, so it records the identity first
    if (Get-Command Register-ImportAppliedIdentity -ErrorAction SilentlyContinue) { Register-ImportAppliedIdentity -Path $a3Settings }
    $script:A3HookWrote = $null
    # the seam runs after the file was read and before it is written: an editor saves a credential into the host file in that window
    $script:ImportSettingsRewriteHook = { param($Path) try { [IO.File]::WriteAllText($Path, '{"env":{"DB_PASSWORD":"owner-hunter2"}}'); $script:A3HookWrote = $true } catch { $script:A3HookWrote = $false } }
    $script:Failures.Clear()
    try { $a3r = Update-ImportedSettingsLocked -Path $a3Settings -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice' } finally { $script:ImportSettingsRewriteHook = $null }
    Assert-That ($script:A3HookWrote -eq $false) 'r12 A3 import: an editor save between the read and the write of the profile rewrite is refused (the host file is locked)' "hookWrote=$($script:A3HookWrote)"
    Assert-That (([IO.File]::ReadAllText($a3Settings)) -ceq '{"path":"C:/Users/alice/tool","p2":"C:\\Users\\alice\\x"}') 'r12 A3 import: the rewrite itself is applied' ([IO.File]::ReadAllText($a3Settings))
    Assert-That ($a3r.Result -ceq 'rewritten') 'r12 A3 import: the result is rewritten' "$($a3r.Result)"
    # a credential already there: never rewritten, typed line, failure
    $a3Cred = '{"env":{"DB_PASSWORD":"owner-hunter2"},"path":"C:/Users/obabalola/tool"}'
    [IO.File]::WriteAllText($a3Settings, $a3Cred); $script:Failures.Clear()
    $a3r = Update-ImportedSettingsLocked -Path $a3Settings -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice'
    Assert-That ($a3r.Result -ceq 'changed' -and ([IO.File]::ReadAllText($a3Settings)) -ceq $a3Cred) 'r12 A3 import: a settings.json that carries a credential is left as it is' "$($a3r.Result)"
    Assert-That (@($script:Failures | Where-Object { $_ -ceq "IMPORT_DEST_CHANGED_SKIPPED $a3Settings" }).Count -eq 1) 'r12 A3 import: the skip is the typed IMPORT_DEST_CHANGED_SKIPPED <path> failure' ($script:Failures -join ',')
    # held open by another process: cannot be locked, left as it is
    [IO.File]::WriteAllText($a3Settings, '{"path":"C:/Users/obabalola/tool"}'); $script:Failures.Clear()
    $a3Hold = [IO.File]::Open($a3Settings, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try { $a3r = Update-ImportedSettingsLocked -Path $a3Settings -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice' } finally { $a3Hold.Dispose() }
    Assert-That ($a3r.Result -ceq 'changed' -and ([IO.File]::ReadAllText($a3Settings)) -ceq '{"path":"C:/Users/obabalola/tool"}' -and $script:Failures.Count -eq 1) 'r12 A3 import: a settings.json another process holds open is skipped and fails the run' "$($a3r.Result) $($script:Failures -join ',')"
    $script:Failures.Clear()
}

# ---------------------------------------------------------------- r12 A4: a -StateRoot / bundle path with '..' or '.' parts maps every file to its OWN destination
if ($fnLoaded -and (Test-Has 'Invoke-ImportRobocopy' 'r12 A4')) {
    $a4 = New-Scratch 'r12-a4'; [void](New-Scratch 'r12-a4\st')
    $a4Src = Join-Path $a4 'src'; $a4Dst = Join-Path $a4 'dst'
    foreach ($d in 'hooks', 'claims') { [void][IO.Directory]::CreateDirectory((Join-Path $a4Src $d)) }
    [IO.File]::WriteAllText((Join-Path $a4Src 'hooks\service.json'), '{"v":"bundle-service"}')
    [IO.File]::WriteAllText((Join-Path $a4Src 'claims\c1.json'), '{"v":"claim"}')
    [IO.File]::WriteAllText((Join-Path $a4Src 'top.json'), '{"v":"top"}')
    $a4From = "$a4\st\..\src"; $a4To = "$a4\st\..\dst"
    $script:Failures.Clear()
    Invoke-ImportRobocopy -From $a4From -To $a4To -Extra @('/E', '/XO', '/XD', "$a4\st\..\src\claims")
    Assert-That (Test-Path -LiteralPath (Join-Path $a4Dst 'hooks\service.json')) 'r12 A4 import: hooks\service.json lands in dst\hooks (not dst) when the paths carry ..' ((Get-ChildItem -LiteralPath $a4Dst -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object FullName) -join ';')
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $a4Dst 'service.json'))) 'r12 A4 import: no file is flattened into the destination root'
    Assert-That (Test-Path -LiteralPath (Join-Path $a4Dst 'top.json')) 'r12 A4 import: a top-level file still lands in the destination root'
    Assert-That (@(Get-ChildItem -LiteralPath $a4Dst -Recurse -File -Filter 'c1.json' -ErrorAction SilentlyContinue).Count -eq 0) 'r12 A4 import: an /XD directory given with .. parts is still excluded' ((Get-ChildItem -LiteralPath $a4Dst -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object FullName) -join ';')
    # host guard: a host file that carries a credential is found under the right name
    [void][IO.Directory]::CreateDirectory((Join-Path $a4Dst 'hooks'))
    [IO.File]::WriteAllText((Join-Path $a4Dst 'hooks\service.json'), '{"api_key":"host-owned-key"}')
    $a4Skips = @(Get-ImportCredentialSkips -SourceDir $a4From -DestDir $a4To)
    Assert-That (@($a4Skips | Where-Object { $_.Dest -like '*\dst\hooks\service.json' -and $_.Reason -like 'host file carries a credential' }).Count -eq 1) 'r12 A4 import: the host credential guard maps hooks\service.json to dst\hooks\service.json' (($a4Skips | ForEach-Object { "$($_.Dest) $($_.Reason)" }) -join ';')
    # export side: the typed path is the path inside the bundle
    $a4Stage = Join-Path $a4 'stage'; [void][IO.Directory]::CreateDirectory((Join-Path $a4Stage 'hooks'))
    [IO.File]::WriteAllText((Join-Path $a4Stage 'hooks\a.yaml'), 'password: hunter2')
    $a4Hits = @(Get-StagedCredentialHits -Stage "$a4\st\..\stage")
    Assert-That ($a4Hits.Count -eq 1 -and $a4Hits[0] -like 'hooks\a.yaml *') 'r12 A4 export: Get-StagedCredentialHits names hooks\a.yaml when -Stage carries ..' ($a4Hits -join ';')
    $a4Scan = Invoke-StagedSecretScan -Stage "$a4\st\..\stage"
    Assert-That (@($a4Scan.Typed).Count -eq 1 -and $a4Scan.Typed[0] -ceq 'EXPORT_CREDENTIAL_HIT hooks\a.yaml:1') 'r12 A4 export: the typed EXPORT_CREDENTIAL_HIT line names hooks\a.yaml when -Stage carries ..' ($a4Scan.Typed -join ';')
    $script:Failures.Clear()
}

# ---------------------------------------------------------------- r12 A5: a copied file keeps the destination locked until its timestamps are set (no editor save is backdated)
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r12 A5')) {
    $a5 = New-Scratch 'r12-a5'
    $a5Src = Join-Path $a5 'src.txt'
    [IO.File]::WriteAllText($a5Src, 'BUNDLE-B'); (Get-Item -LiteralPath $a5Src).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-30)
    foreach ($mode in 'replace', 'create') {
        $a5Dst = Join-Path $a5 "dst-$mode.txt"
        if ($mode -eq 'replace') { [IO.File]::WriteAllText($a5Dst, 'old'); (Get-Item -LiteralPath $a5Dst).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-2) }
        $script:A5Blocked = $null
        # the seam stands for the editor: it saves into the destination as soon as the copy has written the bytes
        $script:ImportAfterWriteHook = { param($Dest) try { [IO.File]::WriteAllText($Dest, 'LOCAL-NEW'); $script:A5Blocked = $false } catch { $script:A5Blocked = $true } }
        try { $a5r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($a5Src)) -Dest $a5Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false } finally { $script:ImportAfterWriteHook = $null }
        Assert-That ($script:A5Blocked -eq $true) "r12 A5 import ($mode): an editor save right after the bytes are written is refused (the lock is held until the timestamps are set)" "blocked=$($script:A5Blocked)"
        Assert-That (([IO.File]::ReadAllText($a5Dst)) -ceq 'BUNDLE-B') "r12 A5 import ($mode): the destination holds the bundle bytes" ([IO.File]::ReadAllText($a5Dst))
        Assert-That ([IO.File]::GetLastWriteTimeUtc($a5Dst) -eq [IO.File]::GetLastWriteTimeUtc($a5Src)) "r12 A5 import ($mode): the destination carries the bundle timestamp" "$([IO.File]::GetLastWriteTimeUtc($a5Dst).ToString('o'))"
    }
}

# ================================================================ r12 iteration 2 (head e88c1414): adversary pass 9, B1-B3
# B1: a boolean word is a whole value only when a blank / line end / ' #' comment follows it; CMD ';' and a glued '#' are content.
# B2: netrc and npmrc record shapes. B3: Import rewrites settings.json only when the copy actually applied the bundle's file.
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r12 B1')) {
    $r12bHit = [ordered]@{
        'B1 cmd set DB_PASSWORD=false;hunter2'         = '@set DB_PASSWORD=false;hunter2'
        'B1 cmd set AUTH_TOKEN=false;hunter2'          = '@set AUTH_TOKEN=false;hunter2'
        'B1 yaml auth_token: false;hunter2'            = 'auth_token: false;hunter2'
        'B1 yaml password: false#hunter2'              = 'password: false#hunter2'
        'B1 flow {password: false#hunter2}'            = '{password: false#hunter2}'
        'B1 flow {token: false#hunter2}'               = '{token: false#hunter2}'
        'B2 netrc one line'                            = 'machine example.org login alice password hunter2'
        'B2 netrc default entry'                       = 'default login alice password hunter2'
        'B2 netrc several lines'                       = "machine example.org`n  login alice`n  password hunter2`n"
        'B2 npmrc _auth'                               = '_auth=YWxpY2U6aHVudGVyMg=='
        'B2 npmrc scoped _auth'                        = '//registry.example.org/:_auth=YWxpY2U6aHVudGVyMg=='
    }
    $r12bOk = [ordered]@{
        'B1 cmd boolean then semicolon at the end'     = 'DB_PASSWORD=null;'
        'B1 yaml boolean with a spaced comment'        = 'password: false # off'
        'B1 flow boolean with a spaced comment'        = '{password: false # off'
        'B1 yaml token boolean'                        = 'auth_token: false'
        'B2 netrc without a password'                  = 'machine example.org login alice'
        'B2 prose that mentions a password'            = 'Rotate the password every 90 days; see the runbook.'
        'B2 npmrc registry only'                       = 'registry=https://registry.example.org/'
        'B2 author key is not _auth'                   = 'author=Layi'
    }
    foreach ($k in $r12bHit.Keys) { Assert-That (Test-CredentialText $r12bHit[$k]) "r12 $k is a credential (Test-CredentialText)" $r12bHit[$k] }
    foreach ($k in $r12bOk.Keys) { Assert-CredentialOkOrRetired 'r12' $k $r12bOk[$k] }
    # the .netrc / .npmrc shapes through the Export stage scan and the Import guards (an older host .npmrc must not be replaced by a newer clean bundle copy)
    $b2Stage = New-Scratch 'r12-b2-stage'
    [IO.File]::WriteAllText((Join-Path $b2Stage '.netrc'), 'machine example.org login alice password hunter2')
    Assert-That (@(Get-StagedCredentialHits -Stage $b2Stage).Count -eq 1) 'r12 B2 export: a staged .netrc record is a hit'
    $b2Src = New-Scratch 'r12-b2-src'; $b2Dst = New-Scratch 'r12-b2-dst'
    [IO.File]::WriteAllText((Join-Path $b2Src '.npmrc'), 'registry=https://registry.example.org/')
    [IO.File]::WriteAllText((Join-Path $b2Dst '.npmrc'), '_auth=YWxpY2U6aHVudGVyMg==')
    $b2Skips = @(Get-ImportCredentialSkips -SourceDir $b2Src -DestDir $b2Dst)
    Assert-That (@($b2Skips | Where-Object { $_.Source -like '*.npmrc' -and $_.Reason -like 'host file carries a credential' }).Count -eq 1) 'r12 B2 import: an older host .npmrc holding _auth is not replaced' (($b2Skips | ForEach-Object Reason) -join ';')
}
# B3: end to end. The bundle's settings.json and the host's have the SAME timestamp and size but different bytes: the copy reports 'kept', so the bundle's file was not applied
# and the profile rewrite must leave the host's file alone. (The old predicate compared a timestamp sampled before the copy and rewrote the retained host file.)
$b3Home = New-Scratch 'r12-b3-home'
$b3Host = Join-Path $b3Home '.claude\settings.json'
[void][IO.Directory]::CreateDirectory((Split-Path -Parent $b3Host))
[IO.File]::WriteAllText($b3Host, '{"path":"C:/Users/obabalola/host"}')
$b3Bundle = New-Scratch 'r12-b3-bundle'
[IO.File]::WriteAllText((Join-Path $b3Bundle 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
[void][IO.Directory]::CreateDirectory((Join-Path $b3Bundle 'home-claude'))
$b3Bs = Join-Path $b3Bundle 'home-claude\settings.json'
[IO.File]::WriteAllText($b3Bs, '{"path":"C:/Users/obabalola/from"}')
$b3Time = [DateTime]::UtcNow.AddHours(-2)
(Get-Item -LiteralPath $b3Host).LastWriteTimeUtc = $b3Time; (Get-Item -LiteralPath $b3Bs).LastWriteTimeUtc = $b3Time
$b3 = Invoke-Setup $b3Home @('-Phase', 'Import', '-StateRoot', (New-Scratch 'r12-b3-state'), '-Workspace', (New-Scratch 'r12-b3-ws'), '-BundlePath', $b3Bundle, '-SourceHost', 'NOT-THIS-HOST')
Assert-That (([IO.File]::ReadAllText($b3Host)) -ceq '{"path":"C:/Users/obabalola/host"}') 'r12 B3 import (end to end): a settings.json the copy KEPT (same timestamp and size) is not rewritten by the profile rewrite' "$([IO.File]::ReadAllText($b3Host))`n$($b3.Out)"
# B3 control: a newer bundle settings.json IS applied and rewritten
$b3Newer = New-Scratch 'r12-b3n-home'
$b3nHost = Join-Path $b3Newer '.claude\settings.json'
[void][IO.Directory]::CreateDirectory((Split-Path -Parent $b3nHost))
[IO.File]::WriteAllText($b3nHost, '{"path":"C:/Users/obabalola/host"}'); (Get-Item -LiteralPath $b3nHost).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-3)
$b3nB = New-Scratch 'r12-b3n-bundle'
[IO.File]::WriteAllText((Join-Path $b3nB 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
[void][IO.Directory]::CreateDirectory((Join-Path $b3nB 'home-claude'))
[IO.File]::WriteAllText((Join-Path $b3nB 'home-claude\settings.json'), '{"path":"C:/Users/obabalola/from"}')
$b3n = Invoke-Setup $b3Newer @('-Phase', 'Import', '-StateRoot', (New-Scratch 'r12-b3n-state'), '-Workspace', (New-Scratch 'r12-b3n-ws'), '-BundlePath', $b3nB, '-SourceHost', 'NOT-THIS-HOST')
$b3nText = [IO.File]::ReadAllText($b3nHost)
Assert-That ($b3nText -ceq ('{"path":"' + ($b3Newer -replace '\\', '/') + '/from"}')) 'r12 B3 import (end to end, control): a newer bundle settings.json is applied and its profile rewritten' "$b3nText`n$($b3n.Out)"

# ================================================================ r14 (head 67ec50ca): adversary pass 10, A1-A6
# A1-A3 (one class): a secret-named key followed by ANY non-empty value is a credential, in a line, block (next non-blank, non-comment line) or flow form, and in a netrc
# record; quoting never ends a value; only an empty value, a whole-value placeholder, or an unquoted whole boolean / null word is exempt.
$r14Hit = [ordered]@{
    'A1 YAML block password (value on the next line)'      = "password:`n  hunter2`n"
    'A1 YAML block api_key'                                = "api_key:`n  abc123def`n"
    'A1 flow mapping across a line break'                  = "{password:`n hunter2}"
    'A1 YAML block with comment and blank lines between'   = "password:`n  # set by hand`n`n  hunter2`n"
    'A1 YAML block scalar indicator'                       = "password: |`n  hunter2`n"
    'A1 YAML block, CRLF'                                  = "api_key:`r`n  abc123def`r`n"
    'A2 netrc quoted value, one line'                      = 'machine example.org login alice password "hunter2"'
    'A2 netrc quoted value, several lines'                 = "machine example.org`nlogin alice`npassword `"hunter2`"`n"
    'A2 netrc, eight comment lines between the lines'      = "machine example.org`n" + ((1..8 | ForEach-Object { "# comment $_" }) -join "`n") + "`nlogin alice`n" + ((1..8 | ForEach-Object { "# note $_" }) -join "`n") + "`npassword hunter2`n"
    'A3 cmd DB_PASSWORD=false"hunter2'                     = '@set DB_PASSWORD=false"hunter2'
    'A3 cmd AUTH_TOKEN=false"hunter2'                      = '@set AUTH_TOKEN=false"hunter2'
    "A3 cmd DB_PASSWORD=none'hunter2"                      = "@set DB_PASSWORD=none'hunter2"
    'A3 cmd an empty quote pair then text'                 = '@set DB_PASSWORD=""hunter2'
    'A3 yaml password: true"hunter2'                       = 'password: true"hunter2'
    'class: a pwd key'                                     = 'pwd=hunter2'
    'class: an auth key'                                   = 'auth: hunter2'
    'class: an XML element'                                = '<configuration><DbPassword>hunter2</DbPassword></configuration>'
    'class: a URL with a user and a password'              = 'url = https://alice:hunter2@example.org/repo.git'
    'class: an all-digit value under a plain TOKEN key'    = 'GH_TOKEN=123456'
    'p11 A1 a sequence whose later element is a secret'    = '{"password":["","hunter2"]}'
    'p11 A1 a sequence that starts with false'             = 'password: [false, hunter2]'
    'p11 A1 a sequence that starts with a placeholder'     = '{"password":["${DB_PASSWORD}", "hunter2"]}'
    'p11 A2 a block scalar whose first line is false'      = "password: |`n  false`n  hunter2`n"
    'p11 A2 a plain scalar continued on the next line'     = "password:`n  false`n  hunter2`n"
    'p11 A2 a line value continued on the next line'       = "password: false`n  hunter2`n"
    'p11 A3 an XML CDATA value'                            = '<password><![CDATA[hunter2]]></password>'
    'p11 A3 an XML value inside a child element'           = '<password><value>hunter2</value></password>'
}
$r14Ok = [ordered]@{
    'a block key with nothing after it'                    = "password:`n"
    'a block key with only a comment after it'             = "password:`n# nothing here`n"
    'an empty quoted YAML value'                           = 'password: ""'
    'a ${...} placeholder'                                 = 'password: "${DB_PASSWORD}"'
    'a <...> placeholder'                                  = 'DB_PASSWORD=<your password>'
    'a *** placeholder'                                    = 'password: ***'
    'a netrc empty quoted password'                        = 'machine example.org login alice password ""'
    'a flow boolean on the next line'                      = "{password:`n false}"
    'author is not auth'                                   = 'author: Layi'
    'an empty XML element'                                 = '<password></password>'
    'a URL with a user only'                               = 'url = ssh://git@github.com/org/repo.git'
    'p11 an empty sequence'                                = 'password: []'
    'p11 a sequence of exempt elements'                    = '{"password":["", false]}'
    'p11 a block boolean followed by a sibling key'        = "password:`n  false`nother: 1`n"
    'p11 an XML comment only'                              = '<password><!-- set by hand --></password>'
}
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r14 A1-A3')) {
    foreach ($k in $r14Hit.Keys) { Assert-That (Test-CredentialText $r14Hit[$k]) "r14 $k is a credential (Test-CredentialText)" $r14Hit[$k] }
    foreach ($k in $r14Ok.Keys) { Assert-CredentialOkOrRetired 'r14' $k $r14Ok[$k] }
    Assert-That ((Get-CredentialHitLine "# a`n# b`npassword:`n  hunter2`n") -eq 3) 'r14 A1 Get-CredentialHitLine: a block credential is reported on the line of its key'
    # every guard site calls the one detector: Export stage scan + typed line, archive re-check, Import bundle guard, Import host guard, the locked copy, the settings rewrite, the backup
    $r14Samples = [ordered]@{ A1 = $r14Hit['A1 YAML block password (value on the next line)']; A2 = $r14Hit['A2 netrc quoted value, several lines']; A3 = $r14Hit['A3 cmd DB_PASSWORD=false"hunter2'] }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    foreach ($id in $r14Samples.Keys) {
        $t = $r14Samples[$id]
        $st = New-Scratch "r14-$id-stage"; [IO.File]::WriteAllText((Join-Path $st 'service.yaml'), $t)
        Assert-That (@(Get-StagedCredentialHits -Stage $st).Count -eq 1) "r14 $id export: the staged file is a hit (Get-StagedCredentialHits)"
        $sc = Invoke-StagedSecretScan -Stage $st
        Assert-That (-not (Test-Path -LiteralPath (Join-Path $st 'service.yaml')) -and @($sc.Typed | Where-Object { $_ -like 'EXPORT_CREDENTIAL_HIT service.yaml:*' }).Count -eq 1) "r14 $id export: the scan removes it with a typed EXPORT_CREDENTIAL_HIT line" (@($sc.Typed) -join ';')
        $zp = Join-Path (New-Scratch "r14-$id-zip") 'b.zip'
        $zf = [IO.Compression.ZipFile]::Open($zp, 'Create')
        try { $e = $zf.CreateEntry('home-claude/machine/service.yaml'); $sw = [IO.StreamWriter]::new($e.Open()); $sw.Write($t); $sw.Dispose() } finally { $zf.Dispose() }
        Assert-That (@(Get-ArchiveCredentialHits -Zip $zp).Count -eq 1) "r14 $id export: the archive re-check is a hit"
        $is = New-Scratch "r14-$id-isrc"; $id_ = New-Scratch "r14-$id-idst"
        [IO.File]::WriteAllText((Join-Path $is 'bundle.yaml'), $t); [IO.File]::WriteAllText((Join-Path $is 'host.yaml'), 'clean: 1'); [IO.File]::WriteAllText((Join-Path $id_ 'host.yaml'), $t)
        $sk = @(Get-ImportCredentialSkips -SourceDir $is -DestDir $id_)
        Assert-That (@($sk | Where-Object { $_.Source -like '*bundle.yaml' -and $_.Reason -eq 'bundle file carries a credential' }).Count -eq 1 -and @($sk | Where-Object { $_.Source -like '*host.yaml' -and $_.Reason -eq 'host file carries a credential' }).Count -eq 1) "r14 $id import: the bundle guard and the host guard both refuse it" (($sk | ForEach-Object Reason) -join ';')
        $cs = Join-Path $is 'locked-src.yaml'; [IO.File]::WriteAllText($cs, $t)
        $cr = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($cs)) -Dest (Join-Path $id_ 'locked-new.yaml') -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
        Assert-That ($cr.Result -ceq 'source-credential' -and -not (Test-Path -LiteralPath (Join-Path $id_ 'locked-new.yaml'))) "r14 $id import: the locked copy refuses the bundle file" "$($cr.Result)"
        $hs = Join-Path $is 'clean-src.yaml'; [IO.File]::WriteAllText($hs, 'clean: 2'); (Get-Item -LiteralPath $hs).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(5)
        $hd = Join-Path $id_ 'locked-host.yaml'; [IO.File]::WriteAllText($hd, $t); (Get-Item -LiteralPath $hd).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-2)
        $script:Failures.Clear()
        $cr2 = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($hs)) -Dest $hd -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
        Assert-That ($cr2.Result -ceq 'changed' -and ([IO.File]::ReadAllText($hd)) -ceq $t) "r14 $id import: the locked copy never replaces a host file that carries it" "$($cr2.Result)"
        $ss = Join-Path (New-Scratch "r14-$id-set") 'settings.json'; [IO.File]::WriteAllText($ss, $t)
        $ur = Update-ImportedSettingsLocked -Path $ss -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice'
        Assert-That ($ur.Result -ceq 'changed' -and ([IO.File]::ReadAllText($ss)) -ceq $t) "r14 $id import: the settings.json rewrite leaves a host file that carries it alone" "$($ur.Result)"
        $bk = New-Scratch "r14-$id-bk"
        Assert-That ((Copy-CleanBackupFile -Path $ss -DestinationDir $bk) -eq $false -and -not (Test-Path -LiteralPath (Join-Path $bk 'settings.json'))) "r14 $id import: the backup never copies it"
        $script:Failures.Clear()
    }
}

# ---------------------------------------------------------------- r14 A4: the settings.json rewrite refuses a host file that changed after the copy wrote it (an editor's newer save is kept)
if ($fnLoaded -and (Test-Has 'Update-ImportedSettingsLocked' 'r14 A4') -and (Test-Has 'Copy-ImportFileLocked' 'r14 A4')) {
    $a4d = New-Scratch 'r14-a4'
    $a4Src = Join-Path $a4d 'bundle-settings.json'; $a4Dst = Join-Path $a4d 'host\settings.json'
    [IO.File]::WriteAllText($a4Src, '{"path":"C:/Users/obabalola/from"}'); (Get-Item -LiteralPath $a4Src).LastWriteTimeUtc = [DateTime]::new(2026, 11, 15, 12, 0, 0, [DateTimeKind]::Utc)
    $a4r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($a4Src)) -Dest $a4Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
    Assert-That ($a4r.Result -ceq 'created') 'r14 A4 import: the bundle settings.json is applied (created)' "$($a4r.Result)"
    # after the copy released its lock and before the rewrite: an editor saves newer host settings
    [IO.File]::WriteAllText($a4Dst, '{"path":"C:/Users/obabalola/host"}')
    $script:Failures.Clear()
    $a4u = Update-ImportedSettingsLocked -Path $a4Dst -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice'
    Assert-That (([IO.File]::ReadAllText($a4Dst)) -ceq '{"path":"C:/Users/obabalola/host"}') 'r14 A4 import: an editor save newer than the copy is preserved byte for byte (not rewritten)' ([IO.File]::ReadAllText($a4Dst))
    Assert-That ($a4u.Result -ceq 'changed' -and @($script:Failures | Where-Object { $_ -ceq "IMPORT_DEST_CHANGED_SKIPPED $a4Dst" }).Count -eq 1) 'r14 A4 import: the refusal is the typed IMPORT_DEST_CHANGED_SKIPPED <path> failure' "$($a4u.Result) $($script:Failures -join ',')"
    # control: no save in between, the rewrite applies
    $a4Dst2 = Join-Path $a4d 'host2\settings.json'
    [void](Copy-ImportFileLocked -Source ([IO.FileInfo]::new($a4Src)) -Dest $a4Dst2 -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false)
    $a4u2 = Update-ImportedSettingsLocked -Path $a4Dst2 -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice'
    Assert-That ($a4u2.Result -ceq 'rewritten' -and ([IO.File]::ReadAllText($a4Dst2)) -ceq '{"path":"C:/Users/alice/from"}') 'r14 A4 import (control): the file the copy wrote, unchanged since, is rewritten' "$($a4u2.Result) $([IO.File]::ReadAllText($a4Dst2))"
    $script:Failures.Clear()
}

# ---------------------------------------------------------------- r14 A5: a replace whose write fails leaves the host file's bytes and timestamp as they were (never truncate first)
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r14 A5')) {
    $a5d = New-Scratch 'r14-a5'
    $a5Src = Join-Path $a5d 'bundle\service.ps1'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $a5Src))
    [IO.File]::WriteAllText($a5Src, ('#' * 8192)); (Get-Item -LiteralPath $a5Src).LastWriteTimeUtc = [DateTime]::new(2026, 11, 15, 12, 0, 0, [DateTimeKind]::Utc)
    $a5Dst = Join-Path $a5d 'host\service.ps1'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $a5Dst))
    $a5Time = [DateTime]::new(2026, 11, 14, 12, 0, 0, [DateTimeKind]::Utc)
    [IO.File]::WriteAllText($a5Dst, '# OWNER'); (Get-Item -LiteralPath $a5Dst).LastWriteTimeUtc = $a5Time
    # the seam stands for the disk: it throws while the replacement bytes are being written (insufficient space)
    $script:ImportWriteFaultHook = { param($Dest) throw 'simulated write failure: insufficient disk space' }
    $a5threw = $false
    try { [void](Copy-ImportFileLocked -Source ([IO.FileInfo]::new($a5Src)) -Dest $a5Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false) } catch { $a5threw = $true } finally { $script:ImportWriteFaultHook = $null }
    Assert-That $a5threw 'r14 A5 import: a replace whose write fails is an error (IMPORT_COPY_FAILED in the caller), not a success'
    Assert-That (([IO.File]::ReadAllText($a5Dst)) -ceq '# OWNER') 'r14 A5 import: the host file keeps its original bytes after a failed replace' ([IO.File]::ReadAllText($a5Dst)).Substring(0, [Math]::Min(20, ([IO.File]::ReadAllText($a5Dst)).Length))
    Assert-That ([IO.File]::GetLastWriteTimeUtc($a5Dst) -eq $a5Time) 'r14 A5 import: the host file keeps its original timestamp (a later /XO import still repairs it)' ([IO.File]::GetLastWriteTimeUtc($a5Dst).ToString('o'))
    Assert-That (@(Get-ChildItem -LiteralPath (Split-Path -Parent $a5Dst) -Force).Count -eq 1) 'r14 A5 import: no temp file is left beside the host file' ((Get-ChildItem -LiteralPath (Split-Path -Parent $a5Dst) -Force | ForEach-Object Name) -join ';')
    # control: the same replace without the fault lands the bundle bytes and the bundle timestamp
    $a5ok = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($a5Src)) -Dest $a5Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
    Assert-That ($a5ok.Result -ceq 'replaced' -and ([IO.File]::ReadAllText($a5Dst)) -ceq ('#' * 8192) -and [IO.File]::GetLastWriteTimeUtc($a5Dst) -eq [IO.File]::GetLastWriteTimeUtc($a5Src) -and @(Get-ChildItem -LiteralPath (Split-Path -Parent $a5Dst) -Force).Count -eq 1) 'r14 A5 import (control): an unfaulted replace lands the bundle bytes and timestamp and leaves no temp file' "$($a5ok.Result)"
    # the settings.json rewrite: same rule
    if (Test-Has 'Update-ImportedSettingsLocked' 'r14 A5 settings') {
        $a5sSrc = Join-Path $a5d 'bundle\settings.json'; [IO.File]::WriteAllText($a5sSrc, '{"path":"C:/Users/obabalola/tool"}')
        $a5s = Join-Path $a5d 'host\settings.json'
        [void](Copy-ImportFileLocked -Source ([IO.FileInfo]::new($a5sSrc)) -Dest $a5s -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false)
        $a5sTime = [IO.File]::GetLastWriteTimeUtc($a5s)
        $script:ImportWriteFaultHook = { param($Dest) throw 'simulated write failure: insufficient disk space' }
        $a5sThrew = $false
        try { $a5sr = Update-ImportedSettingsLocked -Path $a5s -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice' } catch { $a5sThrew = $true } finally { $script:ImportWriteFaultHook = $null }
        Assert-That (([IO.File]::ReadAllText($a5s)) -ceq '{"path":"C:/Users/obabalola/tool"}' -and [IO.File]::GetLastWriteTimeUtc($a5s) -eq $a5sTime) 'r14 A5 import: a settings.json rewrite whose write fails leaves the bytes and timestamp as they were' ([IO.File]::ReadAllText($a5s))
        Assert-That ($a5sThrew -or ($a5sr -and $a5sr.Result -cne 'rewritten')) 'r14 A5 import: the failed rewrite is not reported as rewritten'
        Assert-That (@(Get-ChildItem -LiteralPath (Split-Path -Parent $a5s) -Force).Count -eq 2) 'r14 A5 import: no temp or backup file is left beside settings.json' ((Get-ChildItem -LiteralPath (Split-Path -Parent $a5s) -Force | ForEach-Object Name) -join ';')
    }
    $script:Failures.Clear()
}

# ---------------------------------------------------------------- r14 pass 11 A4 / A5: a save between the lock release and the swap, or after the swap, is never lost
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r14 p11 A4')) {
    $p11 = New-Scratch 'r14-p11'
    $p11Src = Join-Path $p11 'bundle\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $p11Src))
    [IO.File]::WriteAllText($p11Src, '{"v":"bundle"}'); (Get-Item -LiteralPath $p11Src).LastWriteTimeUtc = [DateTime]::new(2026, 11, 15, 12, 0, 0, [DateTimeKind]::Utc)
    $p11Dst = Join-Path $p11 'host\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $p11Dst))
    $p11Newer = [DateTime]::new(2026, 11, 16, 12, 0, 0, [DateTimeKind]::Utc)
    # A4: an editor saves IDENTICAL bytes with a newer time in the instant between the lock release and the swap
    [IO.File]::WriteAllText($p11Dst, '{"v":"host"}'); (Get-Item -LiteralPath $p11Dst).LastWriteTimeUtc = [DateTime]::new(2026, 11, 14, 12, 0, 0, [DateTimeKind]::Utc)
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"v":"host"}'); [IO.File]::SetLastWriteTimeUtc($Dest, [DateTime]::new(2026, 11, 16, 12, 0, 0, [DateTimeKind]::Utc)) }
    try { $p11r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($p11Src)) -Dest $p11Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false } catch { $p11r = @{ Result = "threw: $($_.Exception.Message)" } } finally { $script:ImportBeforeSwapHook = $null }
    Assert-That (([IO.File]::ReadAllText($p11Dst)) -ceq '{"v":"host"}' -and [IO.File]::GetLastWriteTimeUtc($p11Dst) -eq $p11Newer -and $p11r.Result -ceq 'changed') 'r14 p11 A4 import: a newer save with identical bytes in the swap window is kept (result changed)' "$($p11r.Result) $([IO.File]::ReadAllText($p11Dst)) $([IO.File]::GetLastWriteTimeUtc($p11Dst).ToString('o'))"
    # A5: one save before the swap (owner-v1), a second after it (owner-v2): the newest save survives, nothing rolls it back
    [IO.File]::WriteAllText($p11Dst, '{"v":"host"}'); (Get-Item -LiteralPath $p11Dst).LastWriteTimeUtc = [DateTime]::new(2026, 11, 14, 12, 0, 0, [DateTimeKind]::Utc)
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"api_key":"owner-v1"}') }
    $script:ImportAfterSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"api_key":"owner-v2"}') }
    try { $p11r2 = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($p11Src)) -Dest $p11Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false } catch { $p11r2 = @{ Result = "threw: $($_.Exception.Message)" } } finally { $script:ImportBeforeSwapHook = $null; $script:ImportAfterSwapHook = $null }
    # r15 (pass 12 A4/A5 hub ruling): the rollback now swaps under its lock and finds the newer save in its aside, puts it back and refuses typed IMPORT_ROLLBACK_DEST_CHANGED
    # (forced companion: r14 returned 'changed' here without a typed refusal)
    Assert-That (([IO.File]::ReadAllText($p11Dst)) -ceq '{"api_key":"owner-v2"}' -and ($p11r2.Result -ceq 'changed' -or $p11r2.Result -like 'threw: IMPORT_ROLLBACK_DEST_CHANGED*')) 'r14 p11 A5 import: a save after the swap is the newest and is kept (no unconditional rollback)' "$($p11r2.Result) $([IO.File]::ReadAllText($p11Dst))"
    Assert-That (@(Get-ChildItem -LiteralPath (Split-Path -Parent $p11Dst) -Force).Count -eq 1) 'r14 p11 A5 import: no temp or backup file is left beside the host file' ((Get-ChildItem -LiteralPath (Split-Path -Parent $p11Dst) -Force | ForEach-Object Name) -join ';')
    # A5: a save before the swap only: it is put back
    [IO.File]::WriteAllText($p11Dst, '{"v":"host"}'); (Get-Item -LiteralPath $p11Dst).LastWriteTimeUtc = [DateTime]::new(2026, 11, 14, 12, 0, 0, [DateTimeKind]::Utc)
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"api_key":"owner-v1"}') }
    try { $p11r3 = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($p11Src)) -Dest $p11Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false } catch { $p11r3 = @{ Result = "threw: $($_.Exception.Message)" } } finally { $script:ImportBeforeSwapHook = $null }
    Assert-That (([IO.File]::ReadAllText($p11Dst)) -ceq '{"api_key":"owner-v1"}' -and $p11r3.Result -ceq 'changed') 'r14 p11 A5 import: a save in the swap window is put back (result changed)' "$($p11r3.Result) $([IO.File]::ReadAllText($p11Dst))"
}

# ---------------------------------------------------------------- r14 A6: SSH fails closed even when a log write throws after sshd was started and its firewall opened
if ($fnLoaded -and (Test-Has 'Invoke-PhaseSsh' 'r14 A6')) {
    function Invoke-R14A6 {
        $script:A6Calls = [System.Collections.Generic.List[string]]::new()
        # every cmdlet the phase calls is a local stub (no service, firewall, registry or ProgramData is touched)
        function Get-WindowsCapability { [CmdletBinding()] param($Online, $Name) [pscustomobject]@{ Name = 'OpenSSH.Server~~~~0.0.1.0'; State = 'Installed' } }
        function Set-Service { [CmdletBinding()] param($Name, $StartupType) $script:A6Calls.Add("SETSERVICE $Name $StartupType") }
        function Start-Service { [CmdletBinding()] param($Name) $script:A6Calls.Add("START $Name") }
        function Stop-Service { [CmdletBinding()] param($Name, [switch]$Force) $script:A6Calls.Add("STOP $Name") }
        function Restart-Service { [CmdletBinding()] param($Name) $script:A6Calls.Add("RESTART $Name") }
        function Get-NetFirewallRule { [CmdletBinding()] param($Name) [pscustomobject]@{ Name = $Name } }
        function New-NetFirewallRule { [CmdletBinding()] param([Parameter(ValueFromRemainingArguments)]$Rest) $script:A6Calls.Add('NEWFIREWALL') }
        function Set-NetFirewallRule { [CmdletBinding()] param($Name, $Enabled, $Profile, $RemoteAddress) $script:A6Calls.Add("FIREWALL $Name") }
        function Get-PwshPath { $null }
        function Set-SshdKeyOnlyConfig { [CmdletBinding()] param($ConfigPath, $BackupDir, $Stamp) throw 'r14 A6 test: the hardening step must not be reached' }
        function Invoke-Native { [CmdletBinding()] param($Exe, $Arguments, [switch]$AllowFail, $OkCodes) throw 'r14 A6 test: no native command runs' }
        # the setup log is held by another process from the step after the firewall on: every log append throws
        function Write-Log { param([string]$Message, [string]$Level = 'INFO') if ($script:A6Calls -contains 'FIREWALL OpenSSH-Server-In-TCP') { throw 'the setup log is locked by another process' } }
        $threw = $false
        try { Invoke-PhaseSsh } catch { $threw = $true }
        return @{ Threw = $threw; Calls = @($script:A6Calls) }
    }
    $a6 = Invoke-R14A6
    Assert-That ($a6.Calls -contains 'START sshd' -and $a6.Calls -contains 'FIREWALL OpenSSH-Server-In-TCP') 'r14 A6 ssh: the fixture reached the state of the finding (sshd started, firewall opened)' ($a6.Calls -join ',')
    $a6StartIdx = [array]::LastIndexOf([string[]]$a6.Calls, 'START sshd'); $a6StopIdx = [array]::LastIndexOf([string[]]$a6.Calls, 'STOP sshd')
    Assert-That ($a6StopIdx -gt $a6StartIdx) 'r14 A6 ssh: a Write-Log throw after the firewall step still stops sshd (password-enabled sshd does not stay up)' ($a6.Calls -join ',')
    Assert-That ($a6.Calls -contains 'SETSERVICE sshd Manual') 'r14 A6 ssh: the fail-closed path also takes sshd off automatic start (a reboot does not bring the password config back up)' ($a6.Calls -join ',')
}

# ================================================================ r15 (head 1ebadcaf): adversary pass 12, A1-A6
# A1-A3 by the hub ruling (DENY BY DEFAULT on the key NAME; doctrine bus 0fc7511 and d6d115b). A secret-named key with ANY value refuses; the value is the RAW text to
# the end of its record, nothing stripped; the only exemptions are the closed literal list ('', '""', "''", 'null', '~', '[]', '{}', '***', '${NAME}', '<words>', an
# XML element with no content). Over-refusal is accepted and asserted below.
# NOT COVERED by this section (and not by the rule): a key name built from pieces at run time ('$k = "pass" + "word"'), a value produced by run-time evaluation, and a
# secret under a key whose name says nothing (only the known token shapes catch those).
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r15 A1-A3') -and (Test-Has 'Get-CredentialHitLine' 'r15 A1-A3')) {
    $r15Hit = [ordered]@{
        'p12 A1 a brace inside a quoted sequence element'      = '{"password":["{hunter2}"]}'
        'p12 A1 text after an empty sequence'                  = 'DB_PASSWORD=[]hunter2'
        'p12 A1 text after an empty mapping'                   = 'DB_PASSWORD={}hunter2'
        'p12 A2 an XML comment inside CDATA'                   = '<password><![CDATA[<!--hunter2-->]]></password>'
        'p12 A3 netrc with a quoted multi-word login'          = 'machine h login "alice smith" password hunter2'
    }
    foreach ($k in $r15Hit.Keys) {
        Assert-That ((Test-CredentialText $r15Hit[$k]) -and (Get-CredentialHitLine $r15Hit[$k]) -gt 0) "r15 $k is a credential (Test-CredentialText and Get-CredentialHitLine)" $r15Hit[$k]
    }
    # the closed exemption list still passes (the rule is not 'refuse everything')
    $r15Ok = [ordered]@{
        'an empty line value'                                  = 'DB_PASSWORD='
        'an empty double-quoted value'                         = 'password: ""'
        'an empty single-quoted value'                         = "password: ''"
        'a JSON null'                                          = '{"token": null}'
        'a YAML ~'                                             = 'password: ~'
        'an empty sequence'                                    = 'password: []'
        'an empty mapping'                                     = '{"password": {}}'
        'three asterisks'                                      = 'password: ***'
        'an env reference'                                     = 'password: ${DB_PASSWORD}'
        'a placeholder'                                        = 'DB_PASSWORD=<your password>'
        'an XML element with no content'                       = '<password></password>'
        'a self-closing XML element'                           = '<password/>'
        'a block key with nothing after it'                    = "password:`n"
        'a key that is not secret-named'                       = 'author: Layi'
        'a password word with no netrc keyword in the text'    = 'reset the password here'
    }
    foreach ($k in $r15Ok.Keys) { Assert-That (-not (Test-CredentialText $r15Ok[$k])) "r15 $k is exempt (closed list)" $r15Ok[$k] }
    # the r14-and-earlier value exemptions that the closed list retires now REFUSE (accepted over-refusal)
    $r15Retired = [ordered]@{
        'a boolean'                                            = 'password_required: true'
        'a token count'                                        = 'max_tokens = 4096'
        'a mapping under an auth key'                          = 'auth: { type: x }'
        'a script variable assigned from code'                 = '$Token = Get-Token'
        'a quoted env reference'                               = 'password: "${DB_PASSWORD}"'
        'an XML comment only'                                  = '<password><!-- set by hand --></password>'
        'an empty value followed by a comment'                 = 'password: "" # set later'
    }
    foreach ($k in $r15Retired.Keys) { Assert-That (Test-CredentialText $r15Retired[$k]) "r15 retired exemption: $k now refuses" $r15Retired[$k] }

    # ---- the GENERATED must-refuse sweep (doctrine d6d115b): every wrapper composition (depth 1-3) of the value 'hunter2', in every form, with a comment line before,
    # after or not at all. The cases are generated here, never hand-listed; EVERY one must refuse through Test-CredentialText AND Get-CredentialHitLine.
    $r15Wrap = [ordered]@{
        'bare'      = { param($s) $s }
        '"..."'     = { param($s) '"' + $s + '"' }
        "'...'"     = { param($s) "'" + $s + "'" }
        '[..]'      = { param($s) '[' + $s + ']' }
        '{..}'      = { param($s) '{' + $s + '}' }
        '[] prefix' = { param($s) '[]' + $s }
        '{} prefix' = { param($s) '{}' + $s }
        '"" prefix' = { param($s) '""' + $s }
        '<!--..-->' = { param($s) '<!--' + $s + '-->' }
        'CDATA'     = { param($s) '<![CDATA[' + $s + ']]>' }
    }
    $r15Values = [System.Collections.Generic.List[object]]::new()
    $level = @(@{ Name = 'hunter2'; Value = 'hunter2'; Depth = 0 })
    foreach ($d in 1..3) {
        $next = @()
        foreach ($base in $level) {
            foreach ($wn in $r15Wrap.Keys) {
                $nv = @{ Name = "$wn($($base.Name))"; Value = (& $r15Wrap[$wn] $base.Value); Depth = $d }
                $next += $nv
                $r15Values.Add($nv)
            }
        }
        $level = $next
    }
    $r15Forms = [ordered]@{
        'line ='                  = { param($v) "DB_PASSWORD=$v" }
        'line :'                  = { param($v) "password: $v" }
        'block'                   = { param($v) "password:`n  $v" }
        'block, comment between'  = { param($v) "password:`n# note`n  $v" }
        'flow JSON'               = { param($v) "{`"password`": $v}" }
        'flow YAML'               = { param($v) "{password: $v, user: x}" }
        'sequence'                = { param($v) "{`"password`": [$v]}" }
        'XML element'             = { param($v) "<password>$v</password>" }
        'netrc, quoted login'     = { param($v) "machine h login `"alice smith`" password $v" }
    }
    $r15Comments = [ordered]@{ 'no comment' = { param($t) $t }; 'comment before' = { param($t) "# note`n" + $t }; 'comment after' = { param($t) $t + "`n# note" } }
    $r15Total = 0
    foreach ($fn in $r15Forms.Keys) {
        $miss = [System.Collections.Generic.List[string]]::new()
        $n = 0
        foreach ($val in $r15Values) {
            foreach ($cn in $r15Comments.Keys) {
                if ($val.Depth -eq 3 -and $cn -ne 'no comment') { continue }   # depth 3 without comment lines keeps the sweep inside a few seconds per form
                $text = & $r15Comments[$cn] (& $r15Forms[$fn] $val.Value)
                $n++
                if (-not (Test-CredentialText $text) -or (Get-CredentialHitLine $text) -le 0) { $miss.Add("$($val.Name) / $cn") }
            }
        }
        $r15Total += $n
        Assert-That ($miss.Count -eq 0) "r15 generated sweep, $fn form: all $n wrapped values refuse" ("misses=$($miss.Count): " + (($miss | Select-Object -First 12) -join '; '))
    }
    Assert-That ($r15Total -ge 9000) "r15 generated sweep: the sweep ran ($r15Total cases)" "$r15Total"
    # every guard site calls the one detector: one finding input per guard (export stage scan, archive re-check, Import bundle + host guard, locked copy, settings rewrite, backup)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    foreach ($id in 'p12 A1 a brace inside a quoted sequence element', 'p12 A2 an XML comment inside CDATA', 'p12 A3 netrc with a quoted multi-word login') {
        $t = $r15Hit[$id]; $tag = $id.Substring(4, 2)
        $st = New-Scratch "r15-$tag-stage"; [IO.File]::WriteAllText((Join-Path $st 'service.json'), $t)
        Assert-That (@(Get-StagedCredentialHits -Stage $st).Count -eq 1) "r15 $id export: the staged file is a hit"
        $zp = Join-Path (New-Scratch "r15-$tag-zip") 'b.zip'
        $zf = [IO.Compression.ZipFile]::Open($zp, 'Create')
        try { $e = $zf.CreateEntry('home-claude/machine/service.json'); $sw = [IO.StreamWriter]::new($e.Open()); $sw.Write($t); $sw.Dispose() } finally { $zf.Dispose() }
        Assert-That (@(Get-ArchiveCredentialHits -Zip $zp).Count -eq 1) "r15 $id export: the archive re-check is a hit"
        $is = New-Scratch "r15-$tag-isrc"; $idd = New-Scratch "r15-$tag-idst"
        $cs = Join-Path $is 'locked-src.json'; [IO.File]::WriteAllText($cs, $t)
        $cr = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($cs)) -Dest (Join-Path $idd 'locked-new.json') -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
        Assert-That ($cr.Result -ceq 'source-credential') "r15 $id import: the locked copy refuses the bundle file" "$($cr.Result)"
        $ss = Join-Path (New-Scratch "r15-$tag-set") 'settings.json'; [IO.File]::WriteAllText($ss, $t)
        $script:Failures.Clear()
        $ur = Update-ImportedSettingsLocked -Path $ss -FromProfile 'C:\Users\obabalola' -ToProfile 'C:\Users\alice'
        Assert-That ($ur.Result -ceq 'changed' -and ([IO.File]::ReadAllText($ss)) -ceq $t) "r15 $id import: the settings.json rewrite leaves it alone" "$($ur.Result)"
        Assert-That ((Copy-CleanBackupFile -Path $ss -DestinationDir (New-Scratch "r15-$tag-bk")) -eq $false) "r15 $id import: the backup never copies it"
        $script:Failures.Clear()
    }
}

# ---------------------------------------------------------------- r15 A4 / A5: the rollback runs under a lock held through its File.Replace and judges the displaced file by bytes AND time
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r15 A4')) {
    $r15d = New-Scratch 'r15-a4a5'
    $r15Src = Join-Path $r15d 'bundle\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $r15Src))
    $r15Dst = Join-Path $r15d 'host\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $r15Dst))
    function Reset-R15Fixture {
        [IO.File]::WriteAllText($r15Src, '{"v":"bundle"}'); (Get-Item -LiteralPath $r15Src).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 11, 0, 0, [DateTimeKind]::Utc)
        Get-ChildItem -LiteralPath (Split-Path -Parent $r15Dst) -Force | Remove-Item -Force
        [IO.File]::WriteAllText($r15Dst, '{"v":"old"}'); (Get-Item -LiteralPath $r15Dst).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 10, 0, 0, [DateTimeKind]::Utc)
    }
    $t12 = [DateTime]::new(2026, 11, 16, 12, 0, 0, [DateTimeKind]::Utc); $t13 = [DateTime]::new(2026, 11, 16, 13, 0, 0, [DateTimeKind]::Utc)
    # A4 (the finding's input): owner-v1 at 12:00 before the first swap; after the swap an IDENTICAL-bytes save of the bundle text at 13:00
    Reset-R15Fixture
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"v":"owner-v1"}'); [IO.File]::SetLastWriteTimeUtc($Dest, $t12) }
    $script:ImportAfterSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"v":"bundle"}'); [IO.File]::SetLastWriteTimeUtc($Dest, $t13) }
    $a4msg = ''
    try { $a4r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($r15Src)) -Dest $r15Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false; $a4msg = "result=$($a4r.Result)" } catch { $a4msg = $_.Exception.Message } finally { $script:ImportBeforeSwapHook = $null; $script:ImportAfterSwapHook = $null }
    Assert-That (([IO.File]::ReadAllText($r15Dst)) -ceq '{"v":"bundle"}' -and [IO.File]::GetLastWriteTimeUtc($r15Dst) -eq $t13) 'r15 A4 import: an identical-bytes save with a newer time after the swap is kept (never replaced by the older save)' "$a4msg | $([IO.File]::ReadAllText($r15Dst)) $([IO.File]::GetLastWriteTimeUtc($r15Dst).ToString('o'))"
    Assert-That ($a4msg -like 'IMPORT_ROLLBACK_DEST_CHANGED*') 'r15 A4 import: the rollback refuses typed IMPORT_ROLLBACK_DEST_CHANGED' $a4msg
    # A5 (the finding's input): owner-v1 before the swap; while the rollback holds its lock, another process saves owner-v2 by an in-place write (refused by the
    # lock) or by a rename-over save (lands, and must be put back)
    Reset-R15Fixture
    $script:R15Blocked = $null
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"v":"owner-v1"}'); [IO.File]::SetLastWriteTimeUtc($Dest, $t12) }
    $script:ImportBeforeRollbackHook = { param($Dest) try { [IO.File]::WriteAllText($Dest, '{"api_key":"owner-v2"}'); $script:R15Blocked = $false } catch { $script:R15Blocked = $true } }
    $a5msg = ''
    try { $a5r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($r15Src)) -Dest $r15Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false; $a5msg = "result=$($a5r.Result)" } catch { $a5msg = $_.Exception.Message } finally { $script:ImportBeforeSwapHook = $null; $script:ImportBeforeRollbackHook = $null }
    Assert-That ($script:R15Blocked -eq $true) 'r15 A5 import: an in-place save while the rollback runs is refused by the rollback''s lock (the lock is held through the Replace)' "blocked=$($script:R15Blocked) $a5msg"
    Assert-That (([IO.File]::ReadAllText($r15Dst)) -ceq '{"v":"owner-v1"}' -and $a5msg -ceq 'result=changed') 'r15 A5 import: with the in-place save refused, the interleaved save is put back (result changed)' "$a5msg | $([IO.File]::ReadAllText($r15Dst))"
    Reset-R15Fixture
    $script:R15Moved = $null
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"v":"owner-v1"}'); [IO.File]::SetLastWriteTimeUtc($Dest, $t12) }
    $script:ImportBeforeRollbackHook = { param($Dest) $tmpSave = "$Dest.editor-save"; [IO.File]::WriteAllText($tmpSave, '{"api_key":"owner-v2"}'); try { [IO.File]::Move($tmpSave, $Dest, $true); $script:R15Moved = $true } catch { $script:R15Moved = "move failed: $($_.Exception.Message)" } }
    $a5bmsg = ''
    try { $a5b = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($r15Src)) -Dest $r15Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false; $a5bmsg = "result=$($a5b.Result)" } catch { $a5bmsg = $_.Exception.Message } finally { $script:ImportBeforeSwapHook = $null; $script:ImportBeforeRollbackHook = $null }
    if ($script:R15Moved -eq $true) {
        Assert-That (([IO.File]::ReadAllText($r15Dst)) -ceq '{"api_key":"owner-v2"}') 'r15 A5 import: a rename-over save that lands during the rollback is kept (put back), never overwritten by the older save' "$a5bmsg | $([IO.File]::ReadAllText($r15Dst))"
        Assert-That ($a5bmsg -like 'IMPORT_ROLLBACK_DEST_CHANGED*') 'r15 A5 import: that rollback refuses typed IMPORT_ROLLBACK_DEST_CHANGED' $a5bmsg
    } else {
        Assert-That (([IO.File]::ReadAllText($r15Dst)) -ceq '{"v":"owner-v1"}') 'r15 A5 import: a rename-over save refused by the lock loses nothing (the interleaved save is put back)' "moved=$($script:R15Moved) $a5bmsg"
    }
    Assert-That (@(Get-ChildItem -LiteralPath (Split-Path -Parent $r15Dst) -Force | Where-Object { $_.Name -like '*.mlvimport-*' }).Count -eq 0) 'r15 A5 import: no rollback temp, backup or aside file is left beside the host file' ((Get-ChildItem -LiteralPath (Split-Path -Parent $r15Dst) -Force | ForEach-Object Name) -join ';')
}

# ---------------------------------------------------------------- r15 A6: a delete that is refused is never reported as done (export archive, rollback backup)
if ($fnLoaded -and (Test-Has 'Remove-RejectedArchive' 'r15 A6') -and (Test-Has 'Remove-FileVerified' 'r15 A6')) {
    $a6d = New-Scratch 'r15-a6'
    $a6zip = Join-Path $a6d 'mlv-migrate-bundle-r15.zip'; [IO.File]::WriteAllText($a6zip, 'password: hunter2')
    $a6h = [IO.File]::Open($a6zip, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)   # the finding's reader: FileShare.Read, kept open through cleanup
    $a6msg = ''
    $script:Failures.Clear()
    try { Remove-RejectedArchive -Zip $a6zip -Late @('home-claude/machine/service.yaml (credential pattern in the archive)') } catch { $a6msg = $_.Exception.Message } finally { $a6h.Dispose() }
    Assert-That ($a6msg -like 'EXPORT_CREDENTIAL_ZIP_NOT_DELETED *' -and $a6msg.Contains($a6zip) -and -not $a6msg.Contains('archive was deleted')) 'r15 A6 export: an archive whose delete is refused fails typed EXPORT_CREDENTIAL_ZIP_NOT_DELETED with its path (never "deleted")' $a6msg
    Assert-That (@($script:Failures | Where-Object { $_ -like 'EXPORT_CREDENTIAL_ZIP_NOT_DELETED*' }).Count -eq 1) 'r15 A6 export: the typed failure is recorded in the run failures (exit 1)' ($script:Failures -join ',')
    $a6zip2 = Join-Path $a6d 'mlv-migrate-bundle-r15b.zip'; [IO.File]::WriteAllText($a6zip2, 'password: hunter2')
    $a6msg2 = ''
    try { Remove-RejectedArchive -Zip $a6zip2 -Late @('x') } catch { $a6msg2 = $_.Exception.Message }
    Assert-That (-not (Test-Path -LiteralPath $a6zip2) -and $a6msg2 -like '*archive was deleted*') 'r15 A6 export (control): an archive that can be deleted is gone and reported deleted' $a6msg2
    $script:Failures.Clear()
    # the rollback backup cleanup: a backup another process holds open is a typed failure, never kept silently
    if (Test-Has 'Copy-ImportFileLocked' 'r15 A6 backup') {
        $b6 = New-Scratch 'r15-a6-bak'
        $b6Src = Join-Path $b6 'bundle\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $b6Src))
        $b6Dst = Join-Path $b6 'host\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $b6Dst))
        [IO.File]::WriteAllText($b6Src, '{"v":"bundle"}'); (Get-Item -LiteralPath $b6Src).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 11, 0, 0, [DateTimeKind]::Utc)
        [IO.File]::WriteAllText($b6Dst, '{"v":"old"}'); (Get-Item -LiteralPath $b6Dst).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 10, 0, 0, [DateTimeKind]::Utc)
        $script:R15BakHandle = $null
        $script:ImportAfterSwapHook = { param($Dest) $bk = @(Get-ChildItem -LiteralPath (Split-Path -Parent $Dest) -Force -Filter '*.mlvimport-*.bak')[0]; $script:R15BakHandle = [IO.File]::Open($bk.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) }
        $b6msg = ''
        try { $b6r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($b6Src)) -Dest $b6Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false; $b6msg = "result=$($b6r.Result)" } catch { $b6msg = $_.Exception.Message } finally { $script:ImportAfterSwapHook = $null; if ($script:R15BakHandle) { $script:R15BakHandle.Dispose() } }
        Assert-That ($b6msg -like 'IMPORT_ROLLBACK_BACKUP_NOT_DELETED *') 'r15 A6 import: a rollback backup that cannot be removed is a typed failure IMPORT_ROLLBACK_BACKUP_NOT_DELETED <path>, not a silent success' $b6msg
    }
}

# ================================================================ r15 iteration 2 (head 859af4c1): adversary pass 13, A1-A5
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r15 p13 A1-A2')) {
    $p13Hit = [ordered]@{
        'A1 a block value after a CR-only line break (JSON)'   = '{"password":' + "`r" + '"hunter2"}'
        'A1 a YAML block value after a CR-only line break'     = "password:`r  hunter2`r"
        'A1 a block value after a NEL line break'              = "password:$([char]0x85)  hunter2"
        'A1 a block value after an LS line break'              = "password:$([char]0x2028)  hunter2"
        'A2 a braced PowerShell env key'                       = '${env:DB_PASSWORD} = ''hunter2'''
        'A2 a subscripted script key'                          = '$config[''password''] = ''hunter2'''
        'A2 a double-quoted subscript'                         = '$c["token"]="hunter2"'
    }
    foreach ($k in $p13Hit.Keys) { Assert-That ((Test-CredentialText $p13Hit[$k]) -and (Get-CredentialHitLine $p13Hit[$k]) -gt 0) "r15 p13 $k is a credential" $p13Hit[$k] }
    Assert-That ((Get-CredentialHitLine ("# a`r# b`rpassword:`r  hunter2`r")) -eq 3) 'r15 p13 A1 Get-CredentialHitLine: a CR-only file reports the line of the key' "$(Get-CredentialHitLine ("# a`r# b`rpassword:`r  hunter2`r"))"
    # the generated sweep again, over every line-separator form (CR, CRLF, NEL, LS) for the forms that span lines; depth 1-2 wrappers
    $p13Breaks = [ordered]@{ 'CR' = "`r"; 'CRLF' = "`r`n"; 'NEL' = [string][char]0x85; 'LS' = [string][char]0x2028 }
    $p13Miss = [System.Collections.Generic.List[string]]::new(); $p13N = 0
    foreach ($bn in $p13Breaks.Keys) {
        foreach ($fn in 'block', 'block, comment between', 'flow JSON') {
            foreach ($val in @($r15Values | Where-Object { $_.Depth -le 2 })) {
                foreach ($cn in $r15Comments.Keys) {
                    $text = (& $r15Comments[$cn] (& $r15Forms[$fn] $val.Value)).Replace("`n", $p13Breaks[$bn])
                    if ($fn -eq 'flow JSON') { $text = $text.Replace('{"password": ', '{"password":' + $p13Breaks[$bn]) }
                    $p13N++
                    if (-not (Test-CredentialText $text) -or (Get-CredentialHitLine $text) -le 0) { $p13Miss.Add("$bn / $fn / $($val.Name) / $cn") }
                }
            }
        }
    }
    Assert-That ($p13Miss.Count -eq 0 -and $p13N -ge 3000) "r15 p13 A1 generated sweep over CR / CRLF / NEL / LS: all $p13N cases refuse" ("misses=$($p13Miss.Count): " + (($p13Miss | Select-Object -First 12) -join '; '))
}

# ---------------------------------------------------------------- r15 p13 A3: the credential-ROLE guard of Import is never narrower than the detector's name list
if ($fnLoaded -and (Test-Has 'Get-ImportCredentialSkips' 'r15 p13 A3') -and (Test-Has 'Test-CredentialNamePresent' 'r15 p13 A3')) {
    foreach ($nm in 'api.key', 'pass_word', 'auth', 'pwd', 'private.key', 'dbPassword') { Assert-That (Test-CredentialNamePresent ('{"' + $nm + '":""}')) "r15 p13 A3 Test-CredentialNamePresent: '$nm' with an exempt value is a credential name" }
    Assert-That (-not (Test-CredentialNamePresent '{"theme":"light","author":"x"}')) 'r15 p13 A3 Test-CredentialNamePresent (control): no secret-named word'
    $p13a3 = New-Scratch 'r15-p13-a3'
    $p13Src = Join-Path $p13a3 'bundle'; $p13Dst = Join-Path $p13a3 'host'
    [void][IO.Directory]::CreateDirectory($p13Src); [void][IO.Directory]::CreateDirectory($p13Dst)
    [IO.File]::WriteAllText((Join-Path $p13Src 'settings.json'), '{"theme":"light"}'); (Get-Item -LiteralPath (Join-Path $p13Src 'settings.json')).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 11, 0, 0, [DateTimeKind]::Utc)
    [IO.File]::WriteAllText((Join-Path $p13Dst 'settings.json'), '{"api.key":""}'); (Get-Item -LiteralPath (Join-Path $p13Dst 'settings.json')).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 10, 0, 0, [DateTimeKind]::Utc)
    $p13sk = @(Get-ImportCredentialSkips -SourceDir $p13Src -DestDir $p13Dst)
    Assert-That (@($p13sk | Where-Object { $_.Reason -eq 'host credential-role file names a credential key' }).Count -eq 1) 'r15 p13 A3 import: a host settings.json naming api.key (exempt value) is skipped by the role guard' (($p13sk | ForEach-Object Reason) -join ';')
    $p13cr = Copy-ImportFileLocked -Source ([IO.FileInfo]::new((Join-Path $p13Src 'settings.json'))) -Dest (Join-Path $p13Dst 'settings.json') -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
    Assert-That ($p13cr.Result -ceq 'changed' -and ([IO.File]::ReadAllText((Join-Path $p13Dst 'settings.json'))) -ceq '{"api.key":""}') 'r15 p13 A3 import: the locked copy never replaces it either' "$($p13cr.Result)"
    $script:Failures.Clear()
}

# ---------------------------------------------------------------- r15 p13 A4: a save that landed after the swap stays LIVE; the rollback never replaces it
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r15 p13 A4')) {
    Reset-R15Fixture
    $script:P13RollbackRan = $false
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"api_key":"owner-v1"}'); [IO.File]::SetLastWriteTimeUtc($Dest, $t12) }
    $script:ImportAfterSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"api_key":"owner-v2"}'); [IO.File]::SetLastWriteTimeUtc($Dest, $t13) }
    $script:ImportBeforeRollbackHook = { param($Dest) $script:P13RollbackRan = $true }
    $p13a4 = ''
    try { $p13r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($r15Src)) -Dest $r15Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false; $p13a4 = "result=$($p13r.Result)" } catch { $p13a4 = $_.Exception.Message } finally { $script:ImportBeforeSwapHook = $null; $script:ImportAfterSwapHook = $null; $script:ImportBeforeRollbackHook = $null }
    # the finding's owner-v3: a later save after the rollback decided; it must simply land and stay (nothing of this run touches the path again)
    [IO.File]::WriteAllText($r15Dst, '{"api_key":"owner-v3"}')
    Assert-That ($p13a4 -like 'IMPORT_ROLLBACK_DEST_CHANGED*' -and -not $script:P13RollbackRan) 'r15 p13 A4 import: a save after the swap is detected under the lock BEFORE any rollback Replace (typed IMPORT_ROLLBACK_DEST_CHANGED, no Replace)' "$a4msg | ran=$($script:P13RollbackRan) | $p13a4"
    Assert-That (([IO.File]::ReadAllText($r15Dst)) -ceq '{"api_key":"owner-v3"}' -and @(Get-ChildItem -LiteralPath (Split-Path -Parent $r15Dst) -Force | Where-Object { $_.Name -like '*.mlvimport-*' }).Count -eq 0) 'r15 p13 A4 import: the newest save is live at the host path and no rollback file is left' ((Get-ChildItem -LiteralPath (Split-Path -Parent $r15Dst) -Force | ForEach-Object Name) -join ';')
}

# ---------------------------------------------------------------- r15 p13 A5: Clone refuses an existing checkout whose remotes name other repositories
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r15 p13 A5') -and (Test-Has 'Test-GitRemoteUrlSame' 'r15 p13 A5')) {
    Assert-That (Test-GitRemoteUrlSame 'git@github.com:SuperDimensionalSpaceFortress/MLV-App.git' 'https://github.com/superdimensionalspacefortress/MLV-App/') 'r15 p13 A5 url: ssh and https forms of one repository are the same'
    Assert-That (-not (Test-GitRemoteUrlSame 'https://github.com/ilia3101/MLV-App.git' 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git')) 'r15 p13 A5 url: two repositories differ'
    $p13ws = New-Scratch 'r15-p13-a5'
    $p13repo = Join-Path $p13ws 'MLV-App'; $p13doc = Join-Path $p13ws 'softwarefactory-fleet-doctrine'
    foreach ($r in $p13repo, $p13doc) { [void][IO.Directory]::CreateDirectory($r); & git -C $r init -q 2>$null }
    & git -C $p13repo remote add fork 'https://github.com/ilia3101/MLV-App.git'
    & git -C $p13repo remote add origin 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git'
    & git -C $p13doc remote add origin 'https://github.com/example/unrelated.git'
    $savedRepo = $script:Repo; $savedDoc = $script:DoctrineRepo
    $script:Repo = $p13repo; $Repo = $p13repo; $script:DoctrineRepo = $p13doc; $DoctrineRepo = $p13doc
    $script:Failures.Clear()
    try { Invoke-PhaseClone } catch { }
    $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH*' }).Count -ge 1) 'r15 p13 A5 clone: swapped fork/origin remotes are refused typed CLONE_REMOTE_MISMATCH' ($script:Failures -join ',')
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_DOCTRINE_REMOTE_MISMATCH*' }).Count -eq 1) 'r15 p13 A5 clone: an unrelated checkout at the doctrine path is refused typed CLONE_DOCTRINE_REMOTE_MISMATCH' ($script:Failures -join ',')
    Assert-That (-not ((@(& git -C $p13repo branch -vv 2>$null) -join ' ') -match 'fork/master')) 'r15 p13 A5 clone: master is not set to track the wrong fork/master'
    $script:Failures.Clear()
}

# ================================================================ r15 fix iteration 2 (head af16ed54): adversary pass 14, A1 A2 A4 A5 (A3 is listed OPEN in the PR body)
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r15 p14 A1')) {
    $p14a1 = '{"password"' + "`n" + ':"hunter2"}'
    Assert-That ((Test-CredentialText $p14a1) -and (Get-CredentialHitLine $p14a1) -eq 1) 'r15 p14 A1 a JSON key whose separator is on the next line is a credential (reported on the key line)' $p14a1
    Assert-That (Test-CredentialText ("password`r`n  =  hunter2")) 'r15 p14 A1 an ini key whose = is on the next line (CRLF) is a credential'
}
if ($fnLoaded -and (Test-Has 'Get-RedactedRemoteUrl' 'r15 p14 A2') -and (Test-Has 'Invoke-PhaseClone' 'r15 p14 A2')) {
    Assert-That ((Get-RedactedRemoteUrl 'https://alice:hunter2@github.com/ilia3101/MLV-App.git') -ceq 'https://***@github.com/ilia3101/MLV-App.git') 'r15 p14 A2 url: the user:password part is redacted' (Get-RedactedRemoteUrl 'https://alice:hunter2@github.com/ilia3101/MLV-App.git')
    Assert-That (Test-GitRemoteUrlSame 'https://x-token:abc@github.com/SuperDimensionalSpaceFortress/MLV-App.git' 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git') 'r15 p14 A2 url: a token part does not change the repository'
    $p14ws = New-Scratch 'r15-p14-a2'
    $p14repo = Join-Path $p14ws 'MLV-App'; [void][IO.Directory]::CreateDirectory($p14repo); & git -C $p14repo init -q 2>$null
    & git -C $p14repo remote add fork 'https://alice:hunter2@github.com/ilia3101/MLV-App.git'
    $savedRepo = $script:Repo; $script:Repo = $p14repo; $Repo = $p14repo
    # the doctrine path holds a checkout of the right repository, so the phase never clones (no network in this test)
    $p14doc = Join-Path $p14ws 'doctrine'; [void][IO.Directory]::CreateDirectory($p14doc); & git -C $p14doc init -q 2>$null; & git -C $p14doc remote add origin $DoctrineUrl
    $savedDoc = $script:DoctrineRepo; $script:DoctrineRepo = $p14doc; $DoctrineRepo = $p14doc
    $savedLog = $script:LogFile; $script:LogFile = Join-Path $p14ws 'clone.log'
    $script:Failures.Clear()
    $p14out = ''
    try { $p14out = (Invoke-PhaseClone *>&1 | Out-String) } catch { $p14out += $_.Exception.Message }
    $p14log = $(if (Test-Path -LiteralPath $script:LogFile) { [IO.File]::ReadAllText($script:LogFile) } else { '' })
    $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH*' }).Count -ge 1 -and $p14log -match 'CLONE_REMOTE_MISMATCH') 'r15 p14 A2 clone: the wrong fork remote is refused (and logged)' ($script:Failures -join ',')
    Assert-That (-not ($p14log + $p14out).Contains('hunter2')) 'r15 p14 A2 clone: the refusal never prints or stores the remote URL''s password' ($p14log + $p14out)
    $script:Failures.Clear()
}
# A4: -Phase LeaseAcquire -WhatIf reads no lease (a lease read is a git fetch) and writes none
$p14a4 = Invoke-Setup (New-Scratch 'r15-p14-a4-home') @('-Phase', 'LeaseAcquire', '-WhatIf', '-StateRoot', (New-Scratch 'r15-p14-a4-state'), '-Workspace', (New-Scratch 'r15-p14-a4-ws'))
Assert-That ($p14a4.Out -match 'SKIP \(WhatIf\) LeaseAcquire' -and $p14a4.Out -notmatch 'cannot read the lease') 'r15 p14 A4 lease: -Phase LeaseAcquire -WhatIf skips the lease read and write (no git fetch)' "exit=$($p14a4.Exit)`n$($p14a4.Out)"
# A5: an Import whose bundle has no MANIFEST.txt is a FAILURE of the run (exit 1), not a quiet 'done'
$p14a5 = Invoke-Setup (New-Scratch 'r15-p14-a5-home') @('-Phase', 'Import', '-StateRoot', (New-Scratch 'r15-p14-a5-state'), '-Workspace', (New-Scratch 'r15-p14-a5-ws'), '-BundlePath', (New-Scratch 'r15-p14-a5-empty-bundle'), '-SourceHost', 'NOT-THIS-HOST')
Assert-That ($p14a5.Exit -eq 1 -and $p14a5.Out -match 'IMPORT_BUNDLE_INVALID') 'r15 p14 A5 import: a bundle without MANIFEST.txt fails the run (exit 1, IMPORT_BUNDLE_INVALID)' "exit=$($p14a5.Exit)`n$($p14a5.Out)"

# ================================================================ r16 fix iteration 1 (head 850ba3c1): adversary pass 15, A1-A4
# A1: an '=' whose value stands on the next line (PowerShell continues the assignment) is a credential, at every guard (all call the one detector)
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r16 p15 A1')) {
    $p15a1 = [ordered]@{
        'the adversary input ($password = then the quoted value on the next line)' = "`$password =`n'hunter2'`n"
        'the same with CRLF line ends'                                              = "`$password =`r`n'hunter2'"
        'an ini key whose value follows a comment line'                             = "DB_PASSWORD=`n# note`nhunter2"
    }
    foreach ($k in $p15a1.Keys) { Assert-That ((Test-CredentialText $p15a1[$k]) -and (Get-CredentialHitLine $p15a1[$k]) -eq 1) "r16 p15 A1 $k is a credential (reported on the key line)" $p15a1[$k] }
    Assert-That (-not (Test-CredentialText 'DB_PASSWORD=')) 'r16 p15 A1 control: k= at the very end of the text is still the empty value (exempt)'
    if (Test-Has 'Copy-ImportFileLocked' 'r16 p15 A1') {
        $p15a1Dir = New-Scratch 'r16-p15-a1'
        $p15a1Src = Join-Path $p15a1Dir 'connect.ps1'; [IO.File]::WriteAllText($p15a1Src, $p15a1['the adversary input ($password = then the quoted value on the next line)'])
        $p15a1Dst = Join-Path (New-Scratch 'r16-p15-a1-host') 'connect.ps1'
        $p15a1r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($p15a1Src)) -Dest $p15a1Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
        Assert-That ($p15a1r.Result -ceq 'source-credential' -and -not (Test-Path -LiteralPath $p15a1Dst)) 'r16 p15 A1 import: the locked copy refuses that bundle file and writes nothing' "$($p15a1r.Result)"
    }
}
# A2: the Import role guard sees a camelCase secret-named key even after its benign lowercase twin (the seen set is case-sensitive)
if ($fnLoaded -and (Test-Has 'Test-CredentialNamePresent' 'r16 p15 A2') -and (Test-Has 'Get-ImportCredentialSkips' 'r16 p15 A2') -and (Test-Has 'Copy-ImportFileLocked' 'r16 p15 A2')) {
    foreach ($pair in @(@('authheader', 'authHeader'), @('pwdheader', 'pwdHeader'), @('authorizationheader', 'authorizationHeader'))) {
        $txt = '{"' + $pair[0] + '":"","' + $pair[1] + '":""}'
        Assert-That (Test-CredentialNamePresent $txt) "r16 p15 A2 name guard: '$($pair[1])' after its lowercase twin '$($pair[0])' still names a credential key" $txt
        $tag = $pair[1].ToLowerInvariant()
        $sDir = New-Scratch "r16-p15-a2-$tag-bundle"; $dDir = New-Scratch "r16-p15-a2-$tag-host"
        $sFile = Join-Path $sDir 'settings.json'; $dFile = Join-Path $dDir 'settings.json'
        [IO.File]::WriteAllText($dFile, $txt); (Get-Item -LiteralPath $dFile).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-2)
        [IO.File]::WriteAllText($sFile, '{}')
        $sk = @(Get-ImportCredentialSkips -SourceDir $sDir -DestDir $dDir)
        Assert-That (@($sk | Where-Object { $_.Reason -ceq 'host credential-role file names a credential key' }).Count -eq 1) "r16 p15 A2 import skips: a host settings.json naming '$($pair[1])' is kept" (($sk | ForEach-Object Reason) -join ';')
        $cr = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($sFile)) -Dest $dFile -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false
        Assert-That ($cr.Result -ceq 'changed' -and ([IO.File]::ReadAllText($dFile)) -ceq $txt) "r16 p15 A2 locked copy: a host settings.json naming '$($pair[1])' is not replaced" "$($cr.Result)"
    }
}
# A3: an Import whose bundle path does not exist FAILS the run (exit 1, IMPORT_BUNDLE_MISSING); it used to end 'done' with exit 0
$p15a3 = Invoke-Setup (New-Scratch 'r16-p15-a3-home') @('-Phase', 'Import', '-StateRoot', (New-Scratch 'r16-p15-a3-state'), '-Workspace', (New-Scratch 'r16-p15-a3-ws'), '-BundlePath', (Join-Path (New-Scratch 'r16-p15-a3') 'absent-bundle.zip'), '-SourceHost', 'NOT-THIS-HOST')
Assert-That ($p15a3.Exit -eq 1 -and $p15a3.Out -match 'IMPORT_BUNDLE_MISSING') 'r16 p15 A3 import: a missing bundle fails the run (exit 1, IMPORT_BUNDLE_MISSING)' "exit=$($p15a3.Exit)`n$($p15a3.Out)"
# A4: a failed create never deletes a file another process saved at the destination after the handle was released. The seam: Remove-FileVerified is wrapped so that,
# when it is asked to delete the destination by PATHNAME, the owner's save lands there first (the window the old code had). The fixed code deletes through the held handle
# and never asks; either way the owner's save must survive, and with no owner save the half-written file must be gone.
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r16 p15 A4') -and (Test-Has 'Remove-FileVerified' 'r16 p15 A4')) {
    $p15a4Src = Join-Path (New-Scratch 'r16-p15-a4') 'service.ps1'; [IO.File]::WriteAllText($p15a4Src, ('x' * 8192))
    $script:P15A4Dest = Join-Path (New-Scratch 'r16-p15-a4-host') 'service.ps1'
    $script:P15A4SavedRv = ${function:Remove-FileVerified}
    $script:P15A4OwnerSaved = $false
    ${function:Remove-FileVerified} = { param([string]$Path) if ($Path -ceq $script:P15A4Dest) { [IO.File]::WriteAllText($Path, "`$password='owner-v2'"); $script:P15A4OwnerSaved = $true }; & $script:P15A4SavedRv $Path }
    $script:ImportAfterWriteHook = { param($Dest) throw 'r16 p15 A4 simulated I/O failure' }
    $p15a4Err = ''
    try { [void](Copy-ImportFileLocked -Source ([IO.FileInfo]::new($p15a4Src)) -Dest $script:P15A4Dest -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false) } catch { $p15a4Err = $_.Exception.Message }
    finally { $script:ImportAfterWriteHook = $null; ${function:Remove-FileVerified} = $script:P15A4SavedRv }
    $p15a4Ok = $(if ($script:P15A4OwnerSaved) { (Test-Path -LiteralPath $script:P15A4Dest) -and ([IO.File]::ReadAllText($script:P15A4Dest)) -ceq "`$password='owner-v2'" } else { -not (Test-Path -LiteralPath $script:P15A4Dest) })
    Assert-That ($p15a4Ok -and $p15a4Err -match 'simulated I/O failure') 'r16 p15 A4 import: a failed create removes only its own half-written file (an owner save at that path is never deleted)' "ownerSaved=$($script:P15A4OwnerSaved) exists=$(Test-Path -LiteralPath $script:P15A4Dest) err=$p15a4Err"
}
# A4 sibling: the backup copy deletes only what it wrote; an unreadable source leaves a file already standing at the backup name alone
if ($fnLoaded -and (Test-Has 'Copy-CleanBackupFile' 'r16 p15 A4 sibling')) {
    $p15bk = New-Scratch 'r16-p15-a4-bk'
    [IO.File]::WriteAllText((Join-Path $p15bk 'absent.json'), 'owner file')
    $p15bkR = Copy-CleanBackupFile -Path (Join-Path (New-Scratch 'r16-p15-a4-bksrc') 'absent.json') -DestinationDir $p15bk
    Assert-That ($p15bkR -eq $false -and (Test-Path -LiteralPath (Join-Path $p15bk 'absent.json')) -and ([IO.File]::ReadAllText((Join-Path $p15bk 'absent.json'))) -ceq 'owner file') 'r16 p15 A4 sibling backup: an unreadable source deletes nothing at the backup name' "result=$p15bkR"
    Assert-That ((Copy-CleanBackupFile -Path $p15a4Src -DestinationDir $p15bk) -eq $true -and ([IO.File]::ReadAllText((Join-Path $p15bk 'service.ps1'))) -ceq ('x' * 8192)) 'r16 p15 A4 sibling backup: a clean file is still written byte for byte through the owned handle'
}

# ================================================================ r16 fix iteration 2 (head 2912c48a): adversary pass 16, A1 A2 (A3 is listed OPEN in the PR comment)
# A1: a run of separators inside a name word ('api__key') is a secret-named key to the classifier, so every candidate regex offers it (key/value and XML forms)
# A2: any number of closing characters may stand between a name and its separator
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r16 p16 A1 A2') -and (Test-Has 'Get-CredentialKeyClass' 'r16 p16 A1 A2')) {
    $p16 = [ordered]@{
        'p16 A1 a JSON key with a doubled underscore (api__key)'        = '{"api__key":"hunter2"}'
        'p16 A1 a JSON key with a doubled dot (private..key)'           = '{"private..key":"hunter2"}'
        'p16 A1 an XML element with a doubled underscore (api__key)'    = '<api__key>hunter2</api__key>'
        'p16 A1 a separator run inside password (pass--word)'           = 'pass--word = hunter2'
        'p16 A2 a subscript with four closers before the ='             = '$c = @{}; $c[(("password"))] = "hunter2"'
        'p16 A2 a subscript with five closers before the = (and more)'  = '$c[((("token")))] = "hunter2"'
    }
    foreach ($k in $p16.Keys) { Assert-That ((Test-CredentialText $p16[$k]) -and (Get-CredentialHitLine $p16[$k]) -gt 0) "r16 $k is a credential" $p16[$k] }
    Assert-That ((Get-CredentialKeyClass 'api__key') -ceq 'strong' -and (Get-CredentialKeyClass 'private..key') -ceq 'strong') 'r16 p16 A1 control: the classifier calls api__key and private..key strong'
    Assert-That (-not (Test-CredentialText '{"author":"Layi","a_u_t_h":"x-is-not-a-key"}')) 'r16 p16 A1 control: a wider candidate set still leaves the classifier the decision (author, a_u_t_h are not secret-named)'
    if (Test-Has 'Get-StagedCredentialHits' 'r16 p16 A1') {
        $p16Stage = New-Scratch 'r16-p16-a1-stage'
        [IO.File]::WriteAllText((Join-Path $p16Stage 'service.json'), $p16['p16 A1 a JSON key with a doubled underscore (api__key)'])
        Assert-That (@(Get-StagedCredentialHits -Stage $p16Stage).Count -eq 1) 'r16 p16 A1 export: the stage guard refuses a staged service.json with api__key' ((Get-StagedCredentialHits -Stage $p16Stage) -join ';')
    }
}

# ================================================================ r17 (head 4e015891): the three key blockers all r16 seats agree on (opus key3 B1 B2, sol B3 B4 B5, fable B1 B2)
# F1: a host file Import must replace but cannot READ, because another process holds it with a share mode that denies readers, is the typed changed-destination failure
#     (IMPORT_DEST_CHANGED_SKIPPED <path>, recorded, exit 1, host bytes untouched). The r11 test above holds FileShare.Read only, which is why this escaped.
foreach ($f1 in @(@('readwrite-share-none', [IO.FileAccess]::ReadWrite, [IO.FileShare]::None), @('writer-denies-read', [IO.FileAccess]::Write, [IO.FileShare]::Write))) {
    $f1Home = New-Scratch "r17-f1-$($f1[0])-home"
    $f1Svc = Join-Path $f1Home '.claude\hooks\service.json'
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $f1Svc))
    [IO.File]::WriteAllText($f1Svc, '{"v":"host-old"}'); (Get-Item -LiteralPath $f1Svc).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-3)
    $f1Bundle = New-Scratch "r17-f1-$($f1[0])-bundle"
    [IO.File]::WriteAllText((Join-Path $f1Bundle 'MANIFEST.txt'), 'mlv-migrate-bundle v1')
    [void][IO.Directory]::CreateDirectory((Join-Path $f1Bundle 'home-claude\hooks'))
    [IO.File]::WriteAllText((Join-Path $f1Bundle 'home-claude\hooks\service.json'), '{"v":"bundle-service"}')
    [IO.File]::WriteAllText((Join-Path $f1Bundle 'home-claude\CLAUDE.md'), 'owner instructions')
    $f1Hold = [IO.File]::Open($f1Svc, [IO.FileMode]::Open, $f1[1], $f1[2])
    try { $f1r = Invoke-Setup $f1Home @('-Phase', 'Import', '-StateRoot', (New-Scratch "r17-f1-$($f1[0])-state"), '-Workspace', (New-Scratch "r17-f1-$($f1[0])-ws"), '-BundlePath', $f1Bundle, '-SourceHost', 'NOT-THIS-HOST') } finally { $f1Hold.Dispose() }
    Assert-That ($f1r.Out.Contains("IMPORT_DEST_CHANGED_SKIPPED $f1Svc")) "r17 F1 import (held $($f1[0])): the held host file is reported IMPORT_DEST_CHANGED_SKIPPED <path>" $f1r.Out
    Assert-That (-not ($f1r.Out -match "IMPORT_SKIPPED_CREDENTIAL_FILE [^\r\n]*hooks\\service\.json")) "r17 F1 import (held $($f1[0])): it is not mis-recorded as a credential skip" $f1r.Out
    Assert-That ($f1r.Exit -eq 1) "r17 F1 import (held $($f1[0])): the run fails (exit 1)" "exit=$($f1r.Exit)"
    Assert-That (([IO.File]::ReadAllText($f1Svc)) -ceq '{"v":"host-old"}') "r17 F1 import (held $($f1[0])): the held host file's bytes are unchanged" ([IO.File]::ReadAllText($f1Svc))
}
if ($fnLoaded -and (Test-Has 'Get-ImportCredentialSkips' 'r17 F1') -and (Test-Has 'Invoke-ImportRobocopy' 'r17 F1')) {
    # in-process: the inspection does not class a held host file as carrying a credential; a held file the copy rules would KEEP (host newer, /XO) is not a failure
    $f1uSrc = New-Scratch 'r17-f1u-bundle'; $f1uDst = New-Scratch 'r17-f1u-host'
    [IO.File]::WriteAllText((Join-Path $f1uSrc 'svc.json'), '{"v":"bundle"}'); [IO.File]::WriteAllText((Join-Path $f1uSrc 'keep.json'), '{"v":"bundle-older"}')
    (Get-Item -LiteralPath (Join-Path $f1uSrc 'keep.json')).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-5)
    [IO.File]::WriteAllText((Join-Path $f1uDst 'svc.json'), '{"v":"host"}'); (Get-Item -LiteralPath (Join-Path $f1uDst 'svc.json')).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-3)
    [IO.File]::WriteAllText((Join-Path $f1uDst 'keep.json'), '{"v":"host-newer"}')
    $h1 = [IO.File]::Open((Join-Path $f1uDst 'svc.json'), [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $h2 = [IO.File]::Open((Join-Path $f1uDst 'keep.json'), [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $script:Failures.Clear(); $script:ImportSkippedDest.Clear()
    $savedLog = $script:LogFile; $script:LogFile = Join-Path $f1uDst '..\r17-f1u.log'
    try {
        $f1uSk = @(Get-ImportCredentialSkips -SourceDir $f1uSrc -DestDir $f1uDst)
        Invoke-ImportRobocopy -From $f1uSrc -To $f1uDst -Extra @('/E', '/XO') *>&1 | Out-Null
    } finally { $h1.Dispose(); $h2.Dispose(); $script:LogFile = $savedLog }
    Assert-That ($f1uSk.Count -eq 0) 'r17 F1 inspection: a held host file is not classed as carrying a credential' (($f1uSk | ForEach-Object Reason) -join ';')
    Assert-That (@($script:Failures | Where-Object { $_ -ceq "IMPORT_DEST_CHANGED_SKIPPED $(Join-Path $f1uDst 'svc.json')" }).Count -eq 1) 'r17 F1 copy: the held host file the rules would replace is a recorded IMPORT_DEST_CHANGED_SKIPPED failure' ($script:Failures -join ',')
    Assert-That (@($script:Failures | Where-Object { $_ -like '*keep.json*' }).Count -eq 0) 'r17 F1 control: a held host file the rules would keep (host newer, /XO) is not a failure' ($script:Failures -join ',')
    Assert-That (([IO.File]::ReadAllText((Join-Path $f1uDst 'svc.json'))) -ceq '{"v":"host"}' -and ([IO.File]::ReadAllText((Join-Path $f1uDst 'keep.json'))) -ceq '{"v":"host-newer"}') 'r17 F1 copy: both held host files are untouched'
    # control: a host file that was READ and really carries a credential keeps today's skip
    $f1cSrc = New-Scratch 'r17-f1c-bundle'; $f1cDst = New-Scratch 'r17-f1c-host'
    [IO.File]::WriteAllText((Join-Path $f1cSrc 'svc.json'), '{"v":"bundle"}')
    [IO.File]::WriteAllText((Join-Path $f1cDst 'svc.json'), '{"api_key":"synthetic-local-key"}'); (Get-Item -LiteralPath (Join-Path $f1cDst 'svc.json')).LastWriteTimeUtc = [DateTime]::UtcNow.AddDays(-3)
    $f1cSk = @(Get-ImportCredentialSkips -SourceDir $f1cSrc -DestDir $f1cDst)
    Assert-That (@($f1cSk | Where-Object { $_.Reason -ceq 'host file carries a credential' }).Count -eq 1) 'r17 F1 control: a readable host file that carries a credential is still a credential skip' (($f1cSk | ForEach-Object Reason) -join ';')
    $script:Failures.Clear(); $script:ImportSkippedDest.Clear()
}

# F2: a remote URL is printed with userinfo redacted up to the LAST '@' of the authority and everything after '?' or '#' dropped ('?***')
if ($fnLoaded -and (Test-Has 'Get-RedactedRemoteUrl' 'r17 F2') -and (Test-Has 'Test-GitRemoteUrlSame' 'r17 F2')) {
    $f2 = [ordered]@{
        'https://github.com/other/repo.git?access_token=OWNER_HUNTER2'                       = 'https://github.com/other/repo.git?***'
        # r26 (fable key3 r25 B1, HUB RULING): a second '@' makes the authority uncertain, so the whole authority and the rest are redacted (no host is named)
        'https://alice:p@ssw0rd@github.com/SuperDimensionalSpaceFortress/MLV-App.git'         = 'https://***'
        'https://x-access-token:TOK@github.com/o/r.git#frag=TOK2'                            = 'https://***@github.com/o/r.git?***'
        'git@github.com:o/r.git'                                                             = 'git@github.com:o/r.git'
    }
    foreach ($k in $f2.Keys) { $got = Get-RedactedRemoteUrl $k; Assert-That ($got -ceq $f2[$k]) "r17 F2 url: $k prints as $($f2[$k])" $got }
    # r26 (fable key3 r25 B1, HUB RULING): an authority with a second '@' is not [userinfo@]host[:port], so it is keyed raw and is NOT the GitHub repository (fail closed)
    Assert-That (-not (Test-GitRemoteUrlSame 'https://alice:p@ssw0rd@github.com/SuperDimensionalSpaceFortress/MLV-App.git' 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git')) 'r26 url (was r17 F2): a password with an unencoded @ makes the authority uncertain; it is keyed raw and never equals the fork'
    Assert-That (-not (Test-GitRemoteUrlSame 'https://alice:p@ssw0rd@github.com/other/MLV-App.git' 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git')) 'r17 F2 url control: another owner is still another repository'
    # the setup log sink: no line it writes carries a query token or a password, whoever printed it
    $f2Log = Join-Path (New-Scratch 'r17-f2-sink') 'sink.log'
    $savedLog = $script:LogFile; $script:LogFile = $f2Log
    try { Write-Log 'x https://u:SINK_PW@h.example/r.git?access_token=SINK_TOK y' *>&1 | Out-Null } finally { $script:LogFile = $savedLog }
    $f2LogText = [IO.File]::ReadAllText($f2Log)
    Assert-That (-not ($f2LogText -match 'SINK_PW|SINK_TOK')) 'r17 F2 sink: Write-Log never stores a URL credential or query token' $f2LogText
}
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r17 F2')) {
    $f2Remotes = @('https://github.com/other/repo.git?access_token=OWNER_HUNTER2', 'https://alice:p@ssw0rd@github.com/other/MLV-App.git', 'https://x-access-token:TOK1SECRET@github.com/o/r.git#frag=TOK2SECRET')
    $f2i = 0
    foreach ($u in $f2Remotes) {
        $f2i++
        $f2ws = New-Scratch "r17-f2-clone-$f2i"
        $f2repo = Join-Path $f2ws 'MLV-App'; [void][IO.Directory]::CreateDirectory($f2repo); & git -C $f2repo init -q 2>$null
        & git -C $f2repo remote add fork $u
        $savedRepo = $script:Repo; $script:Repo = $f2repo; $Repo = $f2repo
        $f2doc = Join-Path $f2ws 'doctrine'; [void][IO.Directory]::CreateDirectory($f2doc); & git -C $f2doc init -q 2>$null; & git -C $f2doc remote add origin $DoctrineUrl
        $savedDoc = $script:DoctrineRepo; $script:DoctrineRepo = $f2doc; $DoctrineRepo = $f2doc
        $savedLog = $script:LogFile; $script:LogFile = Join-Path $f2ws 'clone.log'
        $script:Failures.Clear()
        $f2out = ''
        try { $f2out = (Invoke-PhaseClone *>&1 | Out-String) } catch { $f2out += $_.Exception.Message }
        $f2log = $(if (Test-Path -LiteralPath $script:LogFile) { [IO.File]::ReadAllText($script:LogFile) } else { '' })
        $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog
        Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH*' }).Count -ge 1) "r17 F2 clone ($f2i): the mismatching fork remote is refused typed" ($script:Failures -join ',')
        Assert-That (-not (($f2log + $f2out) -match 'OWNER_HUNTER2|ssw0rd|TOK1SECRET|TOK2SECRET|alice')) "r17 F2 clone ($f2i): the console and the setup log carry no credential text from the remote URL" ($f2log + $f2out)
        $script:Failures.Clear()
    }
}

# F3: every native git call runs with each GIT_TRACE* variable removed from the child environment (a trace path naming a tracked file is never appended to); the parent's
#     values are restored afterwards
if ($fnLoaded -and (Test-Has 'Invoke-Native' 'r17 F3')) {
    $f3repo = New-Scratch 'r17-f3-repo'
    & git -C $f3repo init -q 2>$null
    [IO.File]::WriteAllText((Join-Path $f3repo 'README.md'), "tracked readme`n"); [IO.File]::WriteAllText((Join-Path $f3repo 'NOTES.md'), "tracked notes`n")
    & git -C $f3repo add README.md NOTES.md 2>$null; & git -C $f3repo -c user.name=t -c user.email=t@example.invalid commit -q -m fixture 2>$null
    $f3a = Join-Path $f3repo 'README.md'; $f3b = Join-Path $f3repo 'NOTES.md'
    $f3before = [IO.File]::ReadAllText($f3a) + '|' + [IO.File]::ReadAllText($f3b)
    $f3saved = @{}; foreach ($n in 'GIT_TRACE', 'GIT_TRACE2_EVENT', 'GIT_TRACE_PERFORMANCE') { $f3saved[$n] = [Environment]::GetEnvironmentVariable($n) }
    $savedLog = $script:LogFile; $script:LogFile = Join-Path (New-Scratch 'r17-f3-log') 'f3.log'
    $f3env = ''
    try {
        [Environment]::SetEnvironmentVariable('GIT_TRACE', $f3a); [Environment]::SetEnvironmentVariable('GIT_TRACE2_EVENT', $f3b); [Environment]::SetEnvironmentVariable('GIT_TRACE_PERFORMANCE', $f3a)
        [void](Invoke-Native -Exe 'git' -Arguments @('-C', $f3repo, 'status', '--porcelain', '-z') *>&1)
        if (Test-Has 'Invoke-GitTraceScrubbed' 'r17 F3') {
            [void](Invoke-GitTraceScrubbed { & git -C $f3repo worktree list --porcelain } *>&1)
            [void](Invoke-GitTraceScrubbed { & git -C $f3repo ls-remote . 'refs/heads/*' 2>$null })
        }
        $f3env = "$([Environment]::GetEnvironmentVariable('GIT_TRACE'))|$([Environment]::GetEnvironmentVariable('GIT_TRACE2_EVENT'))"
    } finally {
        foreach ($n in $f3saved.Keys) { [Environment]::SetEnvironmentVariable($n, $f3saved[$n]) }
        $script:LogFile = $savedLog
    }
    $f3after = [IO.File]::ReadAllText($f3a) + '|' + [IO.File]::ReadAllText($f3b)
    Assert-That ($f3after -ceq $f3before) 'r17 F3 git: GIT_TRACE / GIT_TRACE2_EVENT naming tracked files are never appended to by the setup script''s native git calls' $f3after
    Assert-That ($f3env -ceq "$f3a|$f3b") 'r17 F3 git: the parent environment''s GIT_TRACE* values are restored after the call' $f3env
}

# ================================================================ r17 fix iteration 1 (head ea753a21): adversary pass 18, A1 A3 A4 (A2 is the hub-deferred detector class, DISTINGUISH in the PR)
# A1: a quote in a password or a query value never ends the redaction span (the single-URL form and the free-text log sink)
if ($fnLoaded -and (Test-Has 'Get-RedactedRemoteUrl' 'r17 p18 A1') -and (Test-Has 'ConvertTo-RedactedUrlText' 'r17 p18 A1')) {
    $p18a1 = [ordered]@{
        "https://alice:p'ass@github.com/other/repo.git"          = 'https://***@github.com/other/repo.git'
        "https://github.com/o/r.git?access_token=abc'def"        = 'https://github.com/o/r.git?***'
        'https://al ice:p ass@github.com/o/r.git'                = 'https://***@github.com/o/r.git'
    }
    foreach ($k in $p18a1.Keys) { $got = Get-RedactedRemoteUrl $k; Assert-That ($got -ceq $p18a1[$k]) "r17 p18 A1 url: $k prints as $($p18a1[$k])" $got }
    foreach ($t in @("remote 'fork' is https://alice:p'ass@github.com/other/repo.git, expected x", "fatal: 'https://github.com/o/r.git?access_token=abc'def' not found")) {
        $got = ConvertTo-RedactedUrlText $t
        Assert-That (-not ($got -match "p'ass|alice|abc|def'")) "r17 p18 A1 sink: no credential text survives in: $t" $got
    }
}
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r17 p18 A1')) {
    $p18ws = New-Scratch 'r17-p18-a1-clone'
    $p18repo = Join-Path $p18ws 'MLV-App'; [void][IO.Directory]::CreateDirectory($p18repo); & git -C $p18repo init -q 2>$null
    & git -C $p18repo remote add fork "https://alice:p'ass@github.com/other/repo.git"
    $savedRepo = $script:Repo; $script:Repo = $p18repo; $Repo = $p18repo
    $p18doc = Join-Path $p18ws 'doctrine'; [void][IO.Directory]::CreateDirectory($p18doc); & git -C $p18doc init -q 2>$null; & git -C $p18doc remote add origin $DoctrineUrl
    $savedDoc = $script:DoctrineRepo; $script:DoctrineRepo = $p18doc; $DoctrineRepo = $p18doc
    $savedLog = $script:LogFile; $script:LogFile = Join-Path $p18ws 'clone.log'
    $script:Failures.Clear(); $p18out = ''
    try { $p18out = (Invoke-PhaseClone *>&1 | Out-String) } catch { $p18out += $_.Exception.Message }
    $p18log = $(if (Test-Path -LiteralPath $script:LogFile) { [IO.File]::ReadAllText($script:LogFile) } else { '' })
    $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH*' }).Count -ge 1 -and -not (($p18log + $p18out) -match "p'ass|alice")) 'r17 p18 A1 clone: a quoted password in a mismatching remote is refused and never printed' ($p18log + $p18out)
    $script:Failures.Clear()
}
# A3: an AST census, so a native call site added later cannot escape the scrub: every '& git', '& $c[0]' (the version probes), '& $Exe', '& $pwsh' and '& $GateScript' in the
# setup script stands inside an Invoke-GitTraceScrubbed script block. (The Store-pwsh self re-run at the top re-runs this same script, which scrubs its own calls.)
$p18ast = [System.Management.Automation.Language.Parser]::ParseFile($setup, [ref]$null, [ref]$null)
$p18cmds = @($p18ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.InvocationOperator -eq 'Ampersand' -and ($n.CommandElements[0].Extent.Text -in @('git', '$c[0]', '$Exe', '$pwsh', '$GateScript')) }, $true))
$p18bare = @($p18cmds | Where-Object {
        $p = $_.Parent; $inside = $false
        while ($p) { if ($p -is [System.Management.Automation.Language.CommandAst] -and $p.GetCommandName() -ceq 'Invoke-GitTraceScrubbed') { $inside = $true; break }; $p = $p.Parent }
        -not $inside
    } | ForEach-Object { "line $($_.Extent.StartLineNumber): $($_.Extent.Text)" })
Assert-That ($p18cmds.Count -ge 15 -and $p18bare.Count -eq 0) "r17 p18 A3 census: every native git / git-capable child call ($($p18cmds.Count) found) runs inside Invoke-GitTraceScrubbed" ($p18bare -join ' | ')
# A4: a failed lease object (commit-tree refuses an empty -GitName) is typed LEASE_OBJECT_FAILED and NOTHING is pushed; it used to push ':<LeaseRef>', deleting the lease branch
if ($fnLoaded -and (Test-Has 'Set-HubLease' 'r17 p18 A4')) {
    $p18a4 = New-Scratch 'r17-p18-a4'
    $p18bare4 = Join-Path $p18a4 'remote.git'; & git init -q --bare $p18bare4 2>$null
    $p18r4 = Join-Path $p18a4 'repo'; [void][IO.Directory]::CreateDirectory($p18r4); & git -C $p18r4 init -q 2>$null
    [IO.File]::WriteAllText((Join-Path $p18r4 $LeaseFile), '{"holder":""}'); & git -C $p18r4 add $LeaseFile 2>$null; & git -C $p18r4 -c user.name=t -c user.email=t@example.invalid commit -q -m lease 2>$null
    & git -C $p18r4 remote add fork $p18bare4; & git -C $p18r4 push -q fork "HEAD:$LeaseRef" 2>$null
    $p18sha = ("$(& git -C $p18r4 rev-parse HEAD)").Trim()
    $savedRepo = $script:Repo; $savedName = $GitName; $savedTemp = $env:TEMP; $savedLog = $script:LogFile
    $script:Repo = $p18r4; $Repo = $p18r4; $GitName = ''; $script:GitName = ''; $env:TEMP = New-Scratch 'r17-p18-a4-temp'; $script:LogFile = Join-Path $p18a4 'lease.log'
    $savedIdent = @{}; foreach ($n in 'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME') { $savedIdent[$n] = [Environment]::GetEnvironmentVariable($n); [Environment]::SetEnvironmentVariable($n, $null) }
    $p18err = ''
    try { Set-HubLease -Holder 'TEST-HOST' -Current @{ Exists = $true; Sha = $p18sha; Holder = '' } -Reason 'r17 p18 A4' *>&1 | Out-Null } catch { $p18err = $_.Exception.Message }
    finally {
        $script:Repo = $savedRepo; $Repo = $savedRepo; $GitName = $savedName; $script:GitName = $savedName; $env:TEMP = $savedTemp; $script:LogFile = $savedLog
        foreach ($n in $savedIdent.Keys) { [Environment]::SetEnvironmentVariable($n, $savedIdent[$n]) }
    }
    $p18after = ("$(& git -C $p18bare4 rev-parse --verify --quiet $LeaseRef 2>$null)").Trim()
    Assert-That ($p18err -match 'LEASE_OBJECT_FAILED') 'r17 p18 A4 lease: a commit-tree that fails (empty -GitName) is typed LEASE_OBJECT_FAILED' $p18err
    Assert-That ($p18after -ceq $p18sha) 'r17 p18 A4 lease: nothing is pushed, the remote lease branch is not deleted' "after='$p18after' before='$p18sha'"
}

# ================================================================ r17 fix iteration 2 (head b9c22015): adversary pass 19, A3 A5 A6 (A1 A2 A4 A7 are hub-deferred classes, DISTINGUISH in the PR)
# A3: a space inside a query value or a userinfo part never ends the redaction in free text (git's error text quoting the URL)
if ($fnLoaded -and (Test-Has 'ConvertTo-RedactedUrlText' 'r17 p19 A3')) {
    foreach ($t in @("fatal: unable to access 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git?access_token=first SECOND_SECRET/': The requested URL returned error: 403",
            "remote is https://al ice:p ass@github.com/o/r.git, expected x", "a`nhttps://github.com/o/r.git#frag=ONE TWO_SECRET`nnext line stays")) {
        $got = ConvertTo-RedactedUrlText $t
        Assert-That (-not ($got -match 'first|SECOND_SECRET|ice:p|p ass|ONE|TWO_SECRET')) "r17 p19 A3 sink: no query or userinfo text survives a space in: $($t -replace "`n", ' / ')" $got
    }
    Assert-That ((ConvertTo-RedactedUrlText "a`nhttps://h/x?t=1`nnext line stays") -ceq "a`nhttps://h/x?***`nnext line stays") 'r17 p19 A3 sink control: only the rest of the URL''s own line is dropped'
}
# A5: a bundle file that grows past the scan limit after the inspection is skipped by the locked copy (judged on the bytes it read); nothing is written
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r17 p19 A5') -and (Test-Has 'Get-StagedFileVerdictDetail' 'r17 p19 A5')) {
    $p19src = Join-Path (New-Scratch 'r17-p19-a5') 'service.txt'; [IO.File]::WriteAllText($p19src, 'x')
    $p19fi = [IO.FileInfo]::new($p19src); [void]$p19fi.Length   # the inspection-time FileInfo, 1 byte
    [IO.File]::WriteAllText($p19src, ('x' * 4097))   # grows past a 4096-byte limit before the copy reads it
    $p19dst = Join-Path (New-Scratch 'r17-p19-a5-host') 'service.txt'
    $p19r = Copy-ImportFileLocked -Source $p19fi -Dest $p19dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false -MaxScanBytes 4096
    Assert-That ($p19r.Result -ceq 'source-credential' -and $p19r.Why -match 'larger than the scan limit' -and -not (Test-Path -LiteralPath $p19dst)) 'r17 p19 A5 import: a bundle file grown past the limit is skipped and nothing is written' "$($p19r.Result) $($p19r.Why)"
    $p19e = Join-Path (New-Scratch 'r17-p19-a5-stage') 'service.txt'; [IO.File]::WriteAllText($p19e, 'x'); $p19efi = [IO.FileInfo]::new($p19e); [void]$p19efi.Length
    [IO.File]::WriteAllText($p19e, ('x' * 4097))
    Assert-That ((Get-StagedFileVerdictDetail -File $p19efi -MaxScanBytes 4096).Why -ceq 'larger than the scan limit') 'r17 p19 A5 sibling export: a staged file grown past the limit before the read is still oversized'
}
# A6: a lease remote whose push url names another repository is refused (LEASE_PUSH_URL_DIFFERS) and nothing is pushed there; Clone refuses the same push url
if ($fnLoaded -and (Test-Has 'Set-HubLease' 'r17 p19 A6') -and (Test-Has 'Invoke-PhaseClone' 'r17 p19 A6')) {
    $p19a6 = New-Scratch 'r17-p19-a6'
    $p19main = Join-Path $p19a6 'main.git'; $p19other = Join-Path $p19a6 'other.git'
    & git init -q --bare $p19main 2>$null; & git init -q --bare $p19other 2>$null
    $p19r6 = Join-Path $p19a6 'repo'; [void][IO.Directory]::CreateDirectory($p19r6); & git -C $p19r6 init -q 2>$null
    & git -C $p19r6 remote add fork $p19main; & git -C $p19r6 remote set-url --push fork $p19other
    $savedRepo = $script:Repo; $savedTemp = $env:TEMP; $savedLog = $script:LogFile; $savedName = $GitName; $savedEmail = $GitEmail
    $script:Repo = $p19r6; $Repo = $p19r6; $env:TEMP = New-Scratch 'r17-p19-a6-temp'; $script:LogFile = Join-Path $p19a6 'lease.log'
    $GitName = 'r17 test'; $script:GitName = 'r17 test'; $GitEmail = 'r17@example.invalid'; $script:GitEmail = 'r17@example.invalid'   # a valid identity: the push url check is what must refuse
    $p19err = ''
    $p19ident = @{}; foreach ($n in 'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME') { $p19ident[$n] = [Environment]::GetEnvironmentVariable($n); [Environment]::SetEnvironmentVariable($n, 'r17 test') }   # identity pinned (env outranks -c user.name)
    try { Set-HubLease -Holder 'TEST-HOST' -Current @{ Exists = $false; Sha = ''; Holder = '' } -Reason 'r17 p19 A6' *>&1 | Out-Null } catch { $p19err = $_.Exception.Message }
    finally {
        $script:Repo = $savedRepo; $Repo = $savedRepo; $env:TEMP = $savedTemp; $script:LogFile = $savedLog; $GitName = $savedName; $script:GitName = $savedName; $GitEmail = $savedEmail; $script:GitEmail = $savedEmail
        foreach ($n in $p19ident.Keys) { [Environment]::SetEnvironmentVariable($n, $p19ident[$n]) }
    }
    $p19pushed = ("$(& git -C $p19other rev-parse --verify --quiet $LeaseRef 2>$null)").Trim()
    Assert-That ($p19err -match 'LEASE_PUSH_URL_DIFFERS' -and -not $p19pushed) 'r17 p19 A6 lease: a push url naming another repository is refused and nothing is pushed there' "err=$p19err pushed='$p19pushed'"
    # Clone: fork's fetch url is right, its push url names another repository
    $p19cws = New-Scratch 'r17-p19-a6-clone'
    $p19crepo = Join-Path $p19cws 'MLV-App'; [void][IO.Directory]::CreateDirectory($p19crepo); & git -C $p19crepo init -q 2>$null
    & git -C $p19crepo remote add fork $ForkUrl; & git -C $p19crepo remote set-url --push fork 'https://github.com/other/MLV-App.git'
    $p19cdoc = Join-Path $p19cws 'doctrine'; [void][IO.Directory]::CreateDirectory($p19cdoc); & git -C $p19cdoc init -q 2>$null; & git -C $p19cdoc remote add origin $DoctrineUrl
    $savedRepo = $script:Repo; $savedDoc = $script:DoctrineRepo; $savedLog = $script:LogFile
    $script:Repo = $p19crepo; $Repo = $p19crepo; $script:DoctrineRepo = $p19cdoc; $DoctrineRepo = $p19cdoc; $script:LogFile = Join-Path $p19cws 'clone.log'
    $script:Failures.Clear()
    # no network: should an older script go on to fetch, git refuses the https transport at once
    $savedProto = [Environment]::GetEnvironmentVariable('GIT_ALLOW_PROTOCOL'); [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', 'file')
    try { Invoke-PhaseClone *>&1 | Out-Null } catch { } finally { [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', $savedProto) }
    $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH* fork pushurl' }).Count -eq 1) 'r17 p19 A6 clone: a fork push url naming another repository is refused typed CLONE_REMOTE_MISMATCH (before any fetch)' ($script:Failures -join ',')
    $script:Failures.Clear()
}

# ================================================================ r19 fix iteration 1 (head f4ca8b44): adversary pass 21, A1 A2 A3 A4 A5
# A1: a secret-named key whose name carries a '/' ('password/primary') and a value under a secret-named TOML table ('[auth]' then 'key = "x"') are credentials
if ($fnLoaded -and (Test-Has 'Get-CredentialHitLine' 'r19 A1')) {
    Assert-That (Test-CredentialText '{"password/primary":"hunter2"}') 'r19 A1 detector: a secret-named key with a slash in its name is a credential'
    Assert-That (Test-CredentialText '{"vault\\secret":"hunter2"}') 'r19 A1 detector: a secret-named key with a backslash in its name is a credential'
    Assert-That (Test-CredentialText "[auth]`nkey = `"hunter2`"`n") 'r19 A1 detector: a value under a secret-named TOML table is a credential'
    Assert-That (Test-CredentialText "[[auth.primary]]`r`nuser = 'bob'`r`n") 'r19 A1 detector: a value under a secret-named TOML array-of-tables header is a credential'
    Assert-That (Test-CredentialText "[tool.token]`nvalue: abc`n") 'r19 A1 detector: a value under a dotted secret-named table is a credential'
    Assert-That (-not (Test-CredentialText "[auth]`n# nothing set yet`n`n[editor]`nkey = `"x`"`n")) 'r19 A1 control: an empty secret-named table and another table hold no credential'
    Assert-That (-not (Test-CredentialText "[author]`nname = `"x`"`n")) 'r19 A1 control: [author] is not a secret-named table'
}
# A2: two DISTINCT local paths ('C:/repos/lease' and 'C:/repos/lease.git', or a '#other' tail) are never the same repository; hosted URL forms keep their normalising
if ($fnLoaded -and (Test-Has 'Test-GitRemoteUrlSame' 'r19 A2')) {
    Assert-That (-not (Test-GitRemoteUrlSame 'C:/repos/lease' 'C:/repos/lease.git')) 'r19 A2 url: a local path and its .git sibling are two repositories'
    Assert-That (-not (Test-GitRemoteUrlSame 'C:/repos/lease#other' 'C:/repos/lease')) 'r19 A2 url: a local path with a # tail is another path'
    Assert-That (-not (Test-GitRemoteUrlSame '/srv/git/lease?x' '/srv/git/lease')) 'r19 A2 url: a local path with a ? tail is another path'
    Assert-That (Test-GitRemoteUrlSame 'C:\repos\lease' 'c:/repos/lease/') 'r19 A2 url control: separators, case and a trailing slash do not change a local path'
    Assert-That (Test-GitRemoteUrlSame 'https://github.com/o/r.git' 'https://github.com/o/r') 'r19 A2 url control: a hosted .git suffix is still dropped'
}
# A3: Clone refuses an existing doctrine checkout whose origin PUSH url names another repository
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r19 A3')) {
    $r19cws = New-Scratch 'r19-a3-clone'
    $r19crepo = Join-Path $r19cws 'MLV-App'; [void][IO.Directory]::CreateDirectory($r19crepo); & git -C $r19crepo init -q 2>$null
    & git -C $r19crepo remote add fork $ForkUrl; & git -C $r19crepo remote add origin $UpstreamUrl
    $r19cdoc = Join-Path $r19cws 'doctrine'; [void][IO.Directory]::CreateDirectory($r19cdoc); & git -C $r19cdoc init -q 2>$null
    & git -C $r19cdoc remote add origin $DoctrineUrl; & git -C $r19cdoc remote set-url --push origin 'https://github.com/example/unrelated.git'
    $savedRepo = $script:Repo; $savedDoc = $script:DoctrineRepo; $savedLog = $script:LogFile
    $script:Repo = $r19crepo; $Repo = $r19crepo; $script:DoctrineRepo = $r19cdoc; $DoctrineRepo = $r19cdoc; $script:LogFile = Join-Path $r19cws 'clone.log'
    $script:Failures.Clear()
    $savedProto = [Environment]::GetEnvironmentVariable('GIT_ALLOW_PROTOCOL'); [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', 'file')   # no network: an https fetch is refused at once
    try { Invoke-PhaseClone *>&1 | Out-Null } catch { } finally { [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', $savedProto) }
    $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_DOCTRINE_REMOTE_MISMATCH*' }).Count -eq 1) 'r19 A3 clone: a doctrine origin whose push url names another repository is refused typed CLONE_DOCTRINE_REMOTE_MISMATCH' ($script:Failures -join ',')
    $script:Failures.Clear()
}
# A4: a rename-over save that lands between the rollback's lock check and its second Replace stays the live file (the older save it displaced is put back aside)
if ($fnLoaded -and (Test-Has 'Set-ImportFileContentAtomic' 'r19 A4') -and (Test-Has 'Copy-ImportFileLocked' 'r19 A4')) {
    $r19d = New-Scratch 'r19-a4'
    $r19Src = Join-Path $r19d 'bundle\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $r19Src))
    $r19Dst = Join-Path $r19d 'host\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $r19Dst))
    [IO.File]::WriteAllText($r19Src, '{"v":"bundle"}'); (Get-Item -LiteralPath $r19Src).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 11, 0, 0, [DateTimeKind]::Utc)
    [IO.File]::WriteAllText($r19Dst, '{"v":"old"}'); (Get-Item -LiteralPath $r19Dst).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 10, 0, 0, [DateTimeKind]::Utc)
    $script:R19Moved = $null
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"v":"owner-v1"}'); [IO.File]::SetLastWriteTimeUtc($Dest, [DateTime]::new(2026, 11, 16, 12, 0, 0, [DateTimeKind]::Utc)) }
    $script:ImportBeforeRollbackHook = { param($Dest) $s2 = "$Dest.editor-save"; [IO.File]::WriteAllText($s2, '{"api_key":"owner-v2"}'); try { [IO.File]::Move($s2, $Dest, $true) } catch { } }
    $script:ImportBeforeAsideRestoreHook = { param($Dest) $s3 = "$Dest.editor-save3"; [IO.File]::WriteAllText($s3, '{"api_key":"owner-v3"}'); try { [IO.File]::Move($s3, $Dest, $true); $script:R19Moved = $true } catch { $script:R19Moved = "move failed: $($_.Exception.Message)" } }
    $r19msg = ''
    try { $r19r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($r19Src)) -Dest $r19Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false; $r19msg = "result=$($r19r.Result)" } catch { $r19msg = $_.Exception.Message } finally { $script:ImportBeforeSwapHook = $null; $script:ImportBeforeRollbackHook = $null; $script:ImportBeforeAsideRestoreHook = $null }
    Write-Host "INFO r19 A4 seam: moved=$($script:R19Moved) | $r19msg"
    if ($script:R19Moved -eq $true) {
        Assert-That (([IO.File]::ReadAllText($r19Dst)) -ceq '{"api_key":"owner-v3"}') 'r19 A4 import: the newest save (landed between the lock check and the second Replace) is the live file' "$r19msg | $([IO.File]::ReadAllText($r19Dst))"
        Assert-That ($r19msg -like 'IMPORT_ROLLBACK_DEST_CHANGED*') 'r19 A4 import: that rollback refuses typed IMPORT_ROLLBACK_DEST_CHANGED' $r19msg
    } else {
        Assert-That (([IO.File]::ReadAllText($r19Dst)) -notmatch 'bundle') 'r19 A4 import: a newest save refused by the lock loses nothing and the bundle copy is not live' "moved=$($script:R19Moved) $r19msg | $([IO.File]::ReadAllText($r19Dst))"
    }
}
# A5: a staged file is judged on the BYTES read, not the decoded character count (15 MB of a two-byte character is 30 MB)
if ($fnLoaded -and (Test-Has 'Get-StagedFileVerdictDetail' 'r19 A5')) {
    $r19e = Join-Path (New-Scratch 'r19-a5') 'service.txt'; [IO.File]::WriteAllText($r19e, 'x'); $r19efi = [IO.FileInfo]::new($r19e); [void]$r19efi.Length   # cached length 1
    [IO.File]::WriteAllText($r19e, ([string][char]0x00E9 * 3000))   # 3000 characters, 6000 UTF-8 bytes, no BOM
    Assert-That ((Get-StagedFileVerdictDetail -File $r19efi -MaxScanBytes 4096).Why -ceq 'larger than the scan limit') 'r19 A5 export: 3000 two-byte characters (6000 bytes) exceed a 4096-byte limit even though the cached length said 1'
    [IO.File]::WriteAllText($r19e, ([string][char]0x00E9 * 1000))
    Assert-That ((Get-StagedFileVerdictDetail -File ([IO.FileInfo]::new($r19e)) -MaxScanBytes 4096).Why -ceq '') 'r19 A5 export control: 2000 bytes under a 4096-byte limit is clean'
}

# ================================================================ r19 fix iteration 2 (head 72e24666): adversary pass 22, A1 A2 A3 A4 A6 (A5 DISTINGUISH, A7 advisory: see the PR)
if ($fnLoaded -and (Test-Has 'Get-CredentialHitLine' 'r19 p22')) {
    Assert-That (Test-CredentialText '{"api/key":"hunter2"}') 'r19 p22 A1 detector: api/key (a slash between the name words) is a credential key'
    Assert-That (Test-CredentialNamePresent '{"api/key":""}') 'r19 p22 A1 role guard: a host role file naming api/key counts as naming a credential key'
    Assert-That (Test-CredentialText "[`"secret[primary]`"]`nkey = `"hunter2`"`n") 'r19 p22 A2 detector: a quoted table name with brackets is still a secret-named table'
    Assert-That (Test-CredentialText 'endpoint = "https://alice:p''ass@example.org/repo"') 'r19 p22 A3 detector: an apostrophe in a URL password does not hide it'
    $r22 = Join-Path (New-Scratch 'r19-p22-a4') 'service.xml'
    [IO.File]::WriteAllBytes($r22, [Text.Encoding]::Unicode.GetBytes('<?xml version="1.0" encoding="UTF-16LE"?><password>hunter2</password>'))   # UTF-16LE, no BOM
    Assert-That ((Get-StagedFileVerdictDetail -File ([IO.FileInfo]::new($r22)) -MaxScanBytes 4096).Why -ceq 'credential pattern') 'r19 p22 A4 export: a BOM-less UTF-16LE file carrying a credential is refused'
    Assert-That ($null -ne (Get-Variable -Scope Script -Name ImportBeforeAsideRestoreHook -ErrorAction SilentlyContinue)) 'r19 p22 A6 import: the rollback seam variable is initialised (strict mode would throw on an unset one)'
}

# ================================================================ r20 (head 58e213a5): sol r19 INV-1 (lease remote comparator) and INV-12 (Clone set-upstream exit)
# INV-1: the repository PATH compares ordinally and a '.git' suffix is significant; only the scheme spelling (scp-like vs ssh://) and the host name (case-insensitive) are
# normalised, and a spelling that cannot be normalised with certainty compares UNEQUAL. github.com (documented case-insensitive, '.git' optional) keeps its own rule.
if ($fnLoaded -and (Test-Has 'Test-GitRemoteUrlSame' 'r20 INV-1')) {
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://git@nas.example/repos/Lease.git' 'ssh://git@nas.example/repos/lease.git')) 'r20 INV-1 url: ssh:// paths that differ only in case are two repositories'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:repos/Lease.git' 'git@nas.example:repos/lease.git')) 'r20 INV-1 url: scp-like paths that differ only in case are two repositories'
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://git@nas.example/repos/lease.git' 'ssh://git@nas.example/repos/lease')) 'r20 INV-1 url: a .git suffix is significant on a non-GitHub host'
    Assert-That (-not (Test-GitRemoteUrlSame 'nas.example:repos/Lease.git' 'nas.example:repos/lease.git')) 'r20 INV-1 url: a user-less scp-like host:path is compared ordinally too'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:/repos/lease.git' 'git@nas.example:repos/lease.git')) 'r20 INV-1 url: an absolute and a home-relative scp-like path are not the same repository'
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://alice@nas.example/repos/lease.git' 'ssh://bob@nas.example/repos/lease.git')) 'r20 INV-1 url: two ssh users are not normalised to one'
    Assert-That (-not (Test-GitRemoteUrlSame '/srv/git/Lease' '/srv/git/lease')) 'r20 INV-1 url: POSIX local paths that differ in case are two repositories'
    Assert-That (-not (Test-GitRemoteUrlSame 'https://nas.example/repos/Lease.git' 'https://nas.example/repos/lease.git')) 'r20 INV-1 url: https paths on a non-GitHub host compare ordinally'
    Assert-That (Test-GitRemoteUrlSame 'ssh://git@nas.example/repos/lease.git' 'ssh://git@nas.example/repos/lease.git') 'r20 INV-1 url control: an identical URL is the same repository'
    # r21 (sol r20 B1; hub ruling 20261117T0822Z): the r20 control here was WRONG and is flipped. Git sends git-upload-pack 'repos/lease.git' (home-relative) for the scp-like
    # form and git-upload-pack '/repos/lease.git' (absolute) for the ssh:// form: two repositories.
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:repos/lease.git' 'ssh://git@nas.example/repos/lease.git')) 'r21 B1 url (flips the r20 control): a home-relative scp-like path and an absolute ssh:// path are two repositories'
    Assert-That (Test-GitRemoteUrlSame 'ssh://git@NAS.Example/repos/lease.git' 'ssh://git@nas.example/repos/lease.git') 'r20 INV-1 url control: a host name differing only in case is the same repository'
    Assert-That (Test-GitRemoteUrlSame 'git@GitHub.com:Owner/Repo.git' 'https://github.com/owner/repo') 'r20 INV-1 url control: github.com keeps its case-insensitive, .git-optional rule'
}
if ($fnLoaded -and (Test-Has 'Set-HubLease' 'r20 INV-1')) {
    foreach ($r20pair in @(
            @('ssh://git@nas.example/repos/Lease.git', 'ssh://git@nas.example/repos/lease.git'),
            @('git@nas.example:repos/Lease.git', 'git@nas.example:repos/lease.git'),
            @('ssh://git@nas.example/repos/lease.git', 'ssh://git@nas.example/repos/lease'),
            @('git@nas.example:repos/lease.git', 'ssh://git@nas.example/repos/lease.git'),   # r21 B1: home-relative scp-like fetch, absolute ssh:// push
            @('nas.example:repos/lease.git', 'ssh://nas.example/repos/lease.git'),           # r21 B1: the user-less pair
            @('ssh://git@nas.example/repos/lease.git', 'git@nas.example:repos/lease.git'),   # r21 B1: the reverse direction
            @('ssh://git@nas.example:2222/repos/lease.git', 'ssh://git@nas.example/repos/lease.git'))) {   # r21 B1: a port is never normalised away
        $r20key = [Math]::Abs(($r20pair[0] + '|' + $r20pair[1]).GetHashCode())   # r21: keyed on BOTH urls (pairs that share a push url used to share one scratch repository)
        $r20ws = New-Scratch ('r20-inv1-' + $r20key)
        $r20repo = Join-Path $r20ws 'repo'; [void][IO.Directory]::CreateDirectory($r20repo); & git -C $r20repo init -q 2>$null
        & git -C $r20repo remote add fork $r20pair[0]; & git -C $r20repo remote set-url --push fork $r20pair[1]   # configuration only: no network is touched
        $savedRepo = $script:Repo; $savedTemp = $env:TEMP; $savedLog = $script:LogFile; $savedName = $GitName; $savedEmail = $GitEmail
        $script:Repo = $r20repo; $Repo = $r20repo; $env:TEMP = New-Scratch ('r20-inv1-temp-' + $r20key); $script:LogFile = Join-Path $r20ws 'lease.log'
        $GitName = 'r20 test'; $script:GitName = 'r20 test'; $GitEmail = 'r20@example.invalid'; $script:GitEmail = 'r20@example.invalid'
        $r20ident = @{}; foreach ($n in 'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME') { $r20ident[$n] = [Environment]::GetEnvironmentVariable($n); [Environment]::SetEnvironmentVariable($n, 'r20 test') }
        $r20err = ''
        try { Set-HubLease -Holder 'TEST-HOST' -Current @{ Exists = $false; Sha = ''; Holder = '' } -Reason 'r20 INV-1' *>&1 | Out-Null } catch { $r20err = $_.Exception.Message }
        finally {
            $script:Repo = $savedRepo; $Repo = $savedRepo; $env:TEMP = $savedTemp; $script:LogFile = $savedLog; $GitName = $savedName; $script:GitName = $savedName; $GitEmail = $savedEmail; $script:GitEmail = $savedEmail
            foreach ($n in $r20ident.Keys) { [Environment]::SetEnvironmentVariable($n, $r20ident[$n]) }
        }
        Assert-That ($r20err -match 'LEASE_PUSH_URL_DIFFERS') "r20 INV-1 lease: fetch $($r20pair[0]) / push $($r20pair[1]) is refused before any push" $r20err
    }
}
# r21 B1 (sol r20): normalise to (user, host, the server path as Git sends it to git-upload-pack). scp-like 'host:/p' is absolute '/p'; scp-like 'host:p' and 'host:~/p' are
# home-relative '~/p'; ssh://host/p is absolute '/p'; ssh://host/~/p is home-relative '~/p'. A port, a '~user' form or anything else not mapped with certainty is UNEQUAL
# unless the two strings are identical (the guard refuses).
if ($fnLoaded -and (Test-Has 'Test-GitRemoteUrlSame' 'r21 B1')) {
    Assert-That (-not (Test-GitRemoteUrlSame 'nas.example:repos/lease.git' 'ssh://nas.example/repos/lease.git')) 'r21 B1 url: the user-less home-relative scp-like / absolute ssh:// pair is two repositories'
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://git@nas.example/repos/lease.git' 'git@nas.example:repos/lease.git')) 'r21 B1 url: the pair is unequal in the other argument order too'
    Assert-That (Test-GitRemoteUrlSame 'git@nas.example:/repos/lease.git' 'ssh://git@nas.example/repos/lease.git') 'r21 B1 url control: an absolute scp-like path and the ssh:// form send the same absolute path to git-upload-pack'
    Assert-That (Test-GitRemoteUrlSame 'git@nas.example:repos/lease.git' 'ssh://git@nas.example/~/repos/lease.git') 'r21 B1 url control: a home-relative scp-like path equals ssh://host/~/path'
    Assert-That (Test-GitRemoteUrlSame 'git@nas.example:repos/lease.git' 'git@nas.example:~/repos/lease.git') 'r21 B1 url control: scp-like host:path equals host:~/path (both home-relative)'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:/repos/lease.git' 'ssh://git@nas.example/~/repos/lease.git')) 'r21 B1 url: an absolute path is not a home-relative one'
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://git@nas.example:2222/repos/lease.git' 'ssh://git@nas.example/repos/lease.git')) 'r21 B1 url: a port form is unequal to the portless form'
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://git@nas.example:22/repos/lease.git' 'git@nas.example:/repos/lease.git')) 'r21 B1 url: a port form is unequal to the scp-like form even for port 22'
    Assert-That (Test-GitRemoteUrlSame 'ssh://git@nas.example:2222/repos/lease.git' 'ssh://git@nas.example:2222/repos/lease.git') 'r21 B1 url control: an identical port form is the same'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:~alice/repos/lease.git' 'ssh://git@nas.example/~alice/repos/lease.git')) 'r21 B1 url: a ~user form compares unequal to another spelling'
    Assert-That (Test-GitRemoteUrlSame 'git@nas.example:~alice/repos/lease.git' 'git@nas.example:~alice/repos/lease.git') 'r21 B1 url control: an identical ~user form is the same'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:repos/lease.git' 'git@nas.example:~/repos/Lease.git')) 'r21 B1 url: a home-relative path still compares ordinally'
    Assert-That (Test-GitRemoteUrlSame 'git@github.com:Owner/Repo.git' 'ssh://git@github.com/owner/repo') 'r21 B1 url control: github.com ssh spellings still name one repository'
}
# r21 adversary pass 25 (its json was unparsable; the text is in sol-001.last.txt) A3 + A4, the same comparator class as B1:
# A3: Git percent-decodes the path of an ssh:// URL but sends a scp-like path literally ('/repos/lease%20copy.git' vs '/repos/lease copy.git'), so an ssh spelling with a '%' is
#     not mapped with certainty and is equal only to the identical string. A4: leading or trailing whitespace of a URL is part of what Git sends ('repos/lease.git ' is not
#     'repos/lease.git'), so the helper no longer trims and the readers strip only the line terminator.
if ($fnLoaded -and (Test-Has 'Test-GitRemoteUrlSame' 'r21 p25')) {
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:/repos/lease%20copy.git' 'ssh://git@nas.example/repos/lease%20copy.git')) 'r21 p25 A3 url: a percent-encoded path is decoded by Git for ssh:// only, so the two spellings are not the same'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:repos/lease%20copy.git' 'ssh://git@nas.example/~/repos/lease%20copy.git')) 'r21 p25 A3 url: the home-relative percent-encoded pair is not the same either'
    Assert-That (Test-GitRemoteUrlSame 'ssh://git@nas.example/repos/lease%20copy.git' 'ssh://git@nas.example/repos/lease%20copy.git') 'r21 p25 A3 url control: an identical percent-encoded URL is the same'
    Assert-That (-not (Test-GitRemoteUrlSame ('git@nas.example:repos/lease.git' + ' ') 'git@nas.example:repos/lease.git')) 'r21 p25 A4 url: a trailing space is part of the path Git sends'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:repos/lease.git' (' ' + 'git@nas.example:repos/lease.git'))) 'r21 p25 A4 url: a leading space is not trimmed away either'
    Assert-That (Test-GitRemoteUrlSame ('git@nas.example:repos/lease.git' + ' ') ('git@nas.example:repos/lease.git' + ' ')) 'r21 p25 A4 url control: two identical spellings (trailing space included) are the same'
    $r21wsRepo = Join-Path (New-Scratch 'r21-p25-a4') 'repo'; [void][IO.Directory]::CreateDirectory($r21wsRepo); & git -C $r21wsRepo init -q 2>$null
    & git -C $r21wsRepo remote add fork ('git@nas.example:repos/lease.git' + ' '); & git -C $r21wsRepo remote set-url --push fork 'git@nas.example:repos/lease.git'   # configuration only: no network is touched
    $r21err = ''
    $r21saved = @{ Repo = $script:Repo; Temp = $env:TEMP; Log = $script:LogFile; Name = $GitName; Email = $GitEmail }
    $script:Repo = $r21wsRepo; $Repo = $r21wsRepo; $env:TEMP = New-Scratch 'r21-p25-a4-temp'; $script:LogFile = Join-Path (Split-Path $r21wsRepo) 'lease.log'
    $GitName = 'r21 test'; $script:GitName = 'r21 test'; $GitEmail = 'r21@example.invalid'; $script:GitEmail = 'r21@example.invalid'
    $r21ident = @{}; foreach ($n in 'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME') { $r21ident[$n] = [Environment]::GetEnvironmentVariable($n); [Environment]::SetEnvironmentVariable($n, 'r21 test') }
    try { Set-HubLease -Holder 'TEST-HOST' -Current @{ Exists = $false; Sha = ''; Holder = '' } -Reason 'r21 p25 A4' *>&1 | Out-Null } catch { $r21err = $_.Exception.Message }
    finally {
        $script:Repo = $r21saved.Repo; $Repo = $r21saved.Repo; $env:TEMP = $r21saved.Temp; $script:LogFile = $r21saved.Log; $GitName = $r21saved.Name; $script:GitName = $r21saved.Name; $GitEmail = $r21saved.Email; $script:GitEmail = $r21saved.Email
        foreach ($n in $r21ident.Keys) { [Environment]::SetEnvironmentVariable($n, $r21ident[$n]) }
    }
    Assert-That ($r21err -match 'LEASE_PUSH_URL_DIFFERS') 'r21 p25 A4 lease: a fetch url with a trailing space and a push url without it is refused before any push (the reader no longer trims it away)' $r21err
}
# r21 adversary pass 26 A3: a line terminator INSIDE a configured url is part of what Git sends. 'git remote get-url' prints it as extra output lines, so the readers join the lines
# with LF (they used to join with '' and trim the end) and the helper treats any CR or LF as not mapped (equal only to the identical string).
if ($fnLoaded -and (Test-Has 'Test-GitRemoteUrlSame' 'r21 p26')) {
    Assert-That (-not (Test-GitRemoteUrlSame ('git@nas.example:repos/lease.git' + "`n") 'git@nas.example:repos/lease.git')) 'r21 p26 A3 url: a trailing line feed is part of the url'
    Assert-That (-not (Test-GitRemoteUrlSame ('git@nas.example:repos/lease' + "`n" + '.git') 'git@nas.example:repos/lease.git')) 'r21 p26 A3 url: an embedded line feed is part of the url'
    Assert-That (Test-GitRemoteUrlSame ('git@nas.example:repos/lease' + "`n" + '.git') ('git@nas.example:repos/lease' + "`n" + '.git')) 'r21 p26 A3 url control: an identical url with an embedded line feed is the same'
    $r26ws = New-Scratch 'r21-p26-a3'; $r26repo = Join-Path $r26ws 'repo'; [void][IO.Directory]::CreateDirectory($r26repo); & git -C $r26repo init -q 2>$null
    & git -C $r26repo remote add fork ('git@nas.example:repos/lease.git' + "`n"); & git -C $r26repo remote set-url --push fork 'git@nas.example:repos/lease.git'   # configuration only: no network is touched
    $r26err = ''
    $r26saved = @{ Repo = $script:Repo; Temp = $env:TEMP; Log = $script:LogFile; Name = $GitName; Email = $GitEmail }
    $script:Repo = $r26repo; $Repo = $r26repo; $env:TEMP = New-Scratch 'r21-p26-a3-temp'; $script:LogFile = Join-Path $r26ws 'lease.log'
    $GitName = 'r21 test'; $script:GitName = 'r21 test'; $GitEmail = 'r21@example.invalid'; $script:GitEmail = 'r21@example.invalid'
    $r26ident = @{}; foreach ($n in 'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME') { $r26ident[$n] = [Environment]::GetEnvironmentVariable($n); [Environment]::SetEnvironmentVariable($n, 'r21 test') }
    try { Set-HubLease -Holder 'TEST-HOST' -Current @{ Exists = $false; Sha = ''; Holder = '' } -Reason 'r21 p26 A3' *>&1 | Out-Null } catch { $r26err = $_.Exception.Message }
    finally {
        $script:Repo = $r26saved.Repo; $Repo = $r26saved.Repo; $env:TEMP = $r26saved.Temp; $script:LogFile = $r26saved.Log; $GitName = $r26saved.Name; $script:GitName = $r26saved.Name; $GitEmail = $r26saved.Email; $script:GitEmail = $r26saved.Email
        foreach ($n in $r26ident.Keys) { [Environment]::SetEnvironmentVariable($n, $r26ident[$n]) }
    }
    Assert-That ($r26err -match 'LEASE_PUSH_URL_DIFFERS') 'r21 p26 A3 lease: a fetch url ending in a line feed and a push url without it is refused before any push' $r26err
}
# r20 adversary pass 23 A2: an apostrophe in the USER part of 'https://user:password@host' does not hide the credential
if ($fnLoaded -and (Test-Has 'Get-CredentialHitLine' 'r20 p23 A2')) {
    Assert-That (Test-CredentialText 'endpoint = "https://o''brien:hunter2@example.org/repo"') 'r20 p23 A2 detector: an apostrophe in a URL user name does not hide the credential'
    Assert-That (Test-CredentialText 'endpoint = "https://o''brien:p''ass@example.org/repo"') 'r20 p23 A2 detector: an apostrophe in both the user name and the password does not hide it'
    Assert-That (-not (Test-CredentialText 'endpoint = "https://example.org/repo"')) 'r20 p23 A2 control: a URL with no user:password part holds no credential'
}
# INV-12: a non-zero exit of 'git branch --set-upstream-to=fork/master master' is a recorded typed failure and Clone exits non-zero (the exit decision is
# 'Failures.Count -gt 0'). Real git over local bare repositories gives the exit 128; a function stub gives the exit 1; a repository with a master gives the control.
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r20 INV-12')) {
    $r20ws2 = New-Scratch 'r20-inv12'
    $r20protoOuter = [Environment]::GetEnvironmentVariable('GIT_ALLOW_PROTOCOL'); [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', 'file')   # local bare repositories only; restored at the end of the section
    $r20fork = Join-Path $r20ws2 'fork.git'; $r20up = Join-Path $r20ws2 'up.git'; $r20seed = Join-Path $r20ws2 'seed'
    & git init -q --bare -b master $r20fork 2>$null; & git init -q --bare -b master $r20up 2>$null   # HEAD names master, so a clone of the fork checks out a local master
    [void][IO.Directory]::CreateDirectory($r20seed); & git -C $r20seed init -q -b master 2>$null
    [IO.File]::WriteAllText((Join-Path $r20seed 'a.txt'), 'a')
    & git -C $r20seed add a.txt 2>$null
    $r20idv = [ordered]@{ GIT_AUTHOR_NAME = 'r20 test'; GIT_AUTHOR_EMAIL = 'r20@example.invalid'; GIT_COMMITTER_NAME = 'r20 test'; GIT_COMMITTER_EMAIL = 'r20@example.invalid' }   # env outranks -c, and an earlier section may have left an empty one
    $r20idSaved = @{}; foreach ($n in $r20idv.Keys) { $r20idSaved[$n] = [Environment]::GetEnvironmentVariable($n); [Environment]::SetEnvironmentVariable($n, $r20idv[$n]) }
    try { $r20commitOut = (& git -C $r20seed -c commit.gpgsign=false commit -q --no-verify -m seed 2>&1 | Out-String) } finally { foreach ($n in $r20idSaved.Keys) { [Environment]::SetEnvironmentVariable($n, $r20idSaved[$n]) } }
    $r20pushOut = (& git -C $r20seed push -q --no-verify $r20fork master 2>&1 | Out-String)   # --no-verify: a global pre-push guard must not judge a scratch seed
    $r20commitOut = "$r20commitOut $r20pushOut"
    Assert-That ((("$(& git -C $r20fork rev-parse --verify --quiet refs/heads/master 2>$null)").Trim()) -match '^[0-9a-f]{40}') 'r20 INV-12 fixture: the fork has a master commit' $r20commitOut
    $r20doc = Join-Path $r20ws2 'doctrine'; [void][IO.Directory]::CreateDirectory($r20doc); & git -C $r20doc init -q 2>$null; & git -C $r20doc remote add origin $DoctrineUrl
    $savedRepo = $script:Repo; $savedDoc = $script:DoctrineRepo; $savedLog = $script:LogFile; $savedFork = $script:ForkUrl; $savedUp = $script:UpstreamUrl
    $script:ForkUrl = $r20fork; $ForkUrl = $r20fork; $script:UpstreamUrl = $r20up; $UpstreamUrl = $r20up
    $script:DoctrineRepo = $r20doc; $DoctrineRepo = $r20doc
    $r20gitExe = (Get-Command git -CommandType Application | Select-Object -First 1).Source
    foreach ($r20case in @(
            @{ Name = 'exit 128 (real git: no local master)'; Has = $false; Stub = $false; Expect = $true },
            @{ Name = 'exit 1 (stub)'; Has = $true; Stub = $true; Expect = $true },
            @{ Name = 'exit 0 control (a local master exists)'; Has = $true; Stub = $false; Expect = $false })) {
        $r20repo2 = Join-Path $r20ws2 ('repo-' + ($r20case.Name -replace '[^a-z0-9]', ''))
        if ($r20case.Has) { & git clone -q --origin fork $r20fork $r20repo2 2>$null }
        else { [void][IO.Directory]::CreateDirectory($r20repo2); & git -C $r20repo2 init -q -b other 2>$null; & git -C $r20repo2 remote add fork $r20fork }
        $script:Repo = $r20repo2; $Repo = $r20repo2; $script:LogFile = Join-Path $r20ws2 ('clone-' + ($r20case.Name -replace '[^a-z0-9]', '') + '.log')
        $script:Failures.Clear()
        if ($r20case.Stub) {
            $global:R20GitExe = $r20gitExe
            function global:git { if ($args -contains '--set-upstream-to=fork/master') { $global:LASTEXITCODE = 1; return }; & $global:R20GitExe @args }
        }
        $savedProto = [Environment]::GetEnvironmentVariable('GIT_ALLOW_PROTOCOL'); [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', 'file')
        try { Invoke-PhaseClone *>&1 | Out-Null } catch { } finally {
            [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', $savedProto)
            if ($r20case.Stub) { Remove-Item -Path 'Function:\git' -Force -ErrorAction SilentlyContinue }
            if (Get-Command -Name git -CommandType Function -ErrorAction SilentlyContinue) { Assert-That $false 'r20 INV-12 fixture: the git function stub was removed' }
        }
        $r20hit = @($script:Failures | Where-Object { "$_" -like 'CLONE_TRACKING_FAILED*' }).Count
        if ($r20case.Expect) { Assert-That ($r20hit -eq 1) "r20 INV-12 clone: set-upstream $($r20case.Name) is a recorded CLONE_TRACKING_FAILED failure (so setup exits non-zero)" ($script:Failures -join ',') }
        else { Assert-That ($r20hit -eq 0 -and $script:Failures.Count -eq 0) "r20 INV-12 clone: set-upstream $($r20case.Name) records no failure" ($script:Failures -join ',') }
        $script:Failures.Clear()
    }
    $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog; $script:ForkUrl = $savedFork; $ForkUrl = $savedFork; $script:UpstreamUrl = $savedUp; $UpstreamUrl = $savedUp
    [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', $r20protoOuter)
}

# ================================================================ r22 (sol r21 B1 B2, fable key3 r21 B1 B2; HUB RULING 20261117T1152Z)
# Runs Set-HubLease in this process against a scratch repository, identity pinned; every value it changes is restored in the finally. Returns the error text ('' = no throw).
function Invoke-R22Lease([string]$RepoDir, [hashtable]$Current, [string]$Tag) {
    $r22s = @{ Repo = $script:Repo; Temp = $env:TEMP; Log = $script:LogFile; Name = $script:GitName; Email = $script:GitEmail }
    $r22id = @{}; foreach ($n in 'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME') { $r22id[$n] = [Environment]::GetEnvironmentVariable($n) }
    $r22e = ''
    try {
        $script:Repo = $RepoDir; $env:TEMP = New-Scratch "r22-temp-$Tag"; $script:LogFile = Join-Path (Split-Path $RepoDir) "lease-$Tag.log"
        $script:GitName = 'r22 test'; $script:GitEmail = 'r22@example.invalid'
        foreach ($n in @($r22id.Keys)) { [Environment]::SetEnvironmentVariable($n, 'r22 test') }
        Set-HubLease -Holder 'TEST-HOST' -Current $Current -Reason "r22 $Tag" *>&1 | Out-Null
    } catch { $r22e = $_.Exception.Message }
    finally {
        $script:Repo = $r22s.Repo; $env:TEMP = $r22s.Temp; $script:LogFile = $r22s.Log; $script:GitName = $r22s.Name; $script:GitEmail = $r22s.Email
        foreach ($n in @($r22id.Keys)) { [Environment]::SetEnvironmentVariable($n, $r22id[$n]) }
    }
    return $r22e
}
function Get-R22Ref([string]$Bare) { return ("$(& git -C $Bare rev-parse --verify --quiet $LeaseRef 2>$null)").Trim() }
function Get-R22Holder([string]$Bare) { $raw = (& git -C $Bare cat-file -p "${LeaseRef}:$LeaseFile" 2>$null) -join "`n"; if (-not $raw) { return '<none>' }; return [string](($raw | ConvertFrom-Json).holder) }
# local bare repositories only (an earlier section may leave a narrower GIT_ALLOW_PROTOCOL or an empty identity); restored after the r22 sections
$r22envSaved = @{}; foreach ($n in 'GIT_ALLOW_PROTOCOL', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL') { $r22envSaved[$n] = [Environment]::GetEnvironmentVariable($n) }
[Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', 'file')
foreach ($n in 'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME') { [Environment]::SetEnvironmentVariable($n, 'r22 test') }
foreach ($n in 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_EMAIL') { [Environment]::SetEnvironmentVariable($n, 'r22@example.invalid') }
if ($fnLoaded -and (Test-Has 'Set-HubLease' 'r22 sol B1')) {
    # Two bare repositories hold the same released lease commit S; the lease repository reads and pushes 'read.git'.
    $r22a = New-Scratch 'r22-a'
    $r22seed = Join-Path $r22a 'seed'; [void][IO.Directory]::CreateDirectory($r22seed); & git -C $r22seed init -q 2>$null
    [IO.File]::WriteAllText((Join-Path $r22seed $LeaseFile), '{"holder":""}'); & git -C $r22seed add $LeaseFile 2>$null
    & git -C $r22seed -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false commit -q --no-verify -m lease 2>$null
    $r22S = ("$(& git -C $r22seed rev-parse HEAD)").Trim()
    # Each case gets its own read.git / other.git pair (both at S) and a lease repository holding S locally (commit-tree names it as the parent).
    function New-R22LeaseRepo([string]$Name) {
        $c = Join-Path $r22a $Name; [void][IO.Directory]::CreateDirectory($c)
        $script:r22read = Join-Path $c 'read.git'; $script:r22other = Join-Path $c 'other.git'
        foreach ($b in $script:r22read, $script:r22other) { & git init -q --bare $b 2>$null; & git -C $r22seed push -q --no-verify $b "HEAD:$LeaseRef" 2>$null }
        $d = Join-Path $c 'repo'; [void][IO.Directory]::CreateDirectory($d); & git -C $d init -q 2>$null
        & git -C $d fetch -q $script:r22read "+${LeaseRef}:refs/hub-lease-local/mlv-app" 2>$null
        return $d
    }
    # (a) check = use: the push url is changed AFTER the guard and BEFORE the push (through the Invoke-Native seam). The push must still go to the checked url.
    $r22ra = New-R22LeaseRepo 'repo-a'
    & git -C $r22ra remote add fork $r22read; & git -C $r22ra config remote.fork.pushurl $r22read
    $global:R22Swap = @{ Repo = $r22ra; Other = $r22other; Orig = ${function:Invoke-Native}; Pushes = 0 }
    function Invoke-Native { param([string]$Exe, [string[]]$Arguments, [switch]$AllowFail, [int[]]$OkCodes = @(0))
        if ($Arguments -contains 'push') { $global:R22Swap.Pushes++; & git -C $global:R22Swap.Repo config remote.fork.pushurl $global:R22Swap.Other 2>$null }
        & $global:R22Swap.Orig -Exe $Exe -Arguments $Arguments -AllowFail:$AllowFail -OkCodes $OkCodes
    }
    try { $r22aErr = Invoke-R22Lease $r22ra @{ Exists = $true; Sha = $r22S; Holder = '' } 'a' } finally { Set-Item -Path 'Function:\Invoke-Native' -Value $global:R22Swap.Orig }
    $r22aRead = Get-R22Holder $r22read; $r22aOther = Get-R22Ref $r22other
    Assert-That ($global:R22Swap.Pushes -ge 1 -and $r22aOther -ceq $r22S -and (($r22aErr -eq '' -and $r22aRead -ceq 'TEST-HOST') -or ($r22aErr -match 'LEASE_PUSH_URL_DIFFERS' -and $r22aRead -ceq ''))) 'r22 sol B1 lease: a push url changed between the check and the push never receives the push; the checked repository is the one written' "err=$r22aErr pushes=$($global:R22Swap.Pushes) readHolder=$r22aRead otherRef=$r22aOther S=$r22S"
    # (a2) the lease read names the url it read from, and a write whose fetch url moved since that read is refused
    $r22ra2 = New-R22LeaseRepo 'repo-a2'
    & git -C $r22ra2 remote add fork $r22read
    $r22gSaved = $script:Repo; $script:Repo = $r22ra2
    try { $r22gh = Get-HubLease } finally { $script:Repo = $r22gSaved }
    Assert-That ($null -eq $r22gh.Error -and $r22gh.Sha -ceq $r22S -and "$($r22gh.Url)" -ceq $r22read) 'r22 sol B1 lease read: Get-HubLease reads through the literal fetch url and returns it' "err=$($r22gh.Error) sha=$($r22gh.Sha) url=$($r22gh.Url)"
    $r22a2Err = Invoke-R22Lease $r22ra2 @{ Exists = $true; Sha = $r22S; Holder = ''; Url = $r22other } 'a2'
    Assert-That ($r22a2Err -match 'LEASE_PUSH_URL_DIFFERS' -and (Get-R22Ref $r22read) -ceq $r22S -and (Get-R22Ref $r22other) -ceq $r22S) 'r22 sol B1 lease: a fetch url that changed since the lease was read is refused and nothing is pushed' "err=$r22a2Err"
    # (b) control characters: a pushurl 'X<LF>X' with url X, and a url ending in a literal CR (a hand-edited quoted value) with pushurl X, are refused; nothing is pushed
    $r22rb = New-R22LeaseRepo 'repo-b'
    & git -C $r22rb remote add fork $r22read; & git -C $r22rb config remote.fork.pushurl ($r22read + "`n" + $r22read)
    $r22bErr = Invoke-R22Lease $r22rb @{ Exists = $true; Sha = $r22S; Holder = '' } 'b'
    Assert-That ($r22bErr -match 'LEASE_PUSH_URL_DIFFERS' -and $r22bErr -match 'REMOTE_URL_UNSAFE' -and (Get-R22Ref $r22read) -ceq $r22S) 'r22 fable B2 lease: a pushurl X<LF>X with url X is refused (REMOTE_URL_UNSAFE), nothing pushed' "err=$r22bErr"
    $r22rc = New-R22LeaseRepo 'repo-c'
    $r22fwd = $r22read -replace '\\', '/'
    [IO.File]::AppendAllText((Join-Path $r22rc '.git\config'), "[remote `"fork`"]`n`turl = `"$r22fwd`r`"`n`tpushurl = $r22fwd`n", [Text.Encoding]::ASCII)
    $r22cErr = Invoke-R22Lease $r22rc @{ Exists = $true; Sha = $r22S; Holder = '' } 'c'
    Assert-That ($r22cErr -match 'LEASE_PUSH_URL_DIFFERS' -and $r22cErr -match 'REMOTE_URL_UNSAFE' -and (Get-R22Ref $r22read) -ceq $r22S) 'r22 fable B2 lease: a url ending in a literal CR with a pushurl without it is refused (REMOTE_URL_UNSAFE), nothing pushed' "err=$r22cErr"
    # control: an ordinary url/pushurl pair still acquires the lease
    $r22rd = New-R22LeaseRepo 'repo-d'
    & git -C $r22rd remote add fork $r22read
    $r22dErr = Invoke-R22Lease $r22rd @{ Exists = $true; Sha = $r22S; Holder = ''; Url = $r22read } 'd'
    Assert-That ($r22dErr -eq '' -and (Get-R22Holder $r22read) -ceq 'TEST-HOST' -and (Get-R22Ref $r22other) -ceq $r22S) 'r22 lease control: a plain remote with no pushurl acquires the lease in the repository it read' "err=$r22dErr"
}
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r22 fable B2 clone')) {
    # Clone and doctrine siblings: a push url 'U<LF>U' (each line equal to the expected url) is a mismatch, not two matching urls
    $r22cws = New-Scratch 'r22-clone'
    $r22crepo = Join-Path $r22cws 'MLV-App'; [void][IO.Directory]::CreateDirectory($r22crepo); & git -C $r22crepo init -q 2>$null
    & git -C $r22crepo remote add fork $ForkUrl; & git -C $r22crepo config remote.fork.pushurl ($ForkUrl + "`n" + $ForkUrl)
    $r22cdoc = Join-Path $r22cws 'doctrine'; [void][IO.Directory]::CreateDirectory($r22cdoc); & git -C $r22cdoc init -q 2>$null
    & git -C $r22cdoc remote add origin $DoctrineUrl; & git -C $r22cdoc config remote.origin.pushurl ($DoctrineUrl + "`n" + $DoctrineUrl)
    $savedRepo = $script:Repo; $savedDoc = $script:DoctrineRepo; $savedLog = $script:LogFile
    $script:Repo = $r22crepo; $Repo = $r22crepo; $script:DoctrineRepo = $r22cdoc; $DoctrineRepo = $r22cdoc; $script:LogFile = Join-Path $r22cws 'clone.log'
    $script:Failures.Clear()
    $savedProto = [Environment]::GetEnvironmentVariable('GIT_ALLOW_PROTOCOL'); [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', 'file')   # no network: an older script that goes on to fetch is refused at once
    try { Invoke-PhaseClone *>&1 | Out-Null } catch { } finally { [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', $savedProto) }
    $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH* fork pushurl' }).Count -eq 1) 'r22 fable B2 clone: a fork pushurl U<LF>U is refused typed CLONE_REMOTE_MISMATCH' ($script:Failures -join ',')
    Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_DOCTRINE_REMOTE_MISMATCH*' }).Count -eq 1) 'r22 fable B2 doctrine: a doctrine origin pushurl U<LF>U is refused typed CLONE_DOCTRINE_REMOTE_MISMATCH' ($script:Failures -join ',')
    $script:Failures.Clear()
}
# r23 (adversary pass 26 on ae45ed76, A1): git reads a url argument as a REMOTE NAME first, so a checked url string that is also the name of a configured remote is resolved as
# that remote (its url / pushurl). The lease read and the lease push must refuse such a url (REMOTE_URL_UNSAFE): the push must never reach the alias's pushurl.
if ($fnLoaded -and (Test-Has 'Set-HubLease' 'r23 A1') -and (Get-Command New-R22LeaseRepo -ErrorAction SilentlyContinue)) {
    $r23ra = New-R22LeaseRepo 'repo-r23-a1'
    & git -C $r23ra remote add fork $r22read
    & git -C $r23ra config "remote.$r22read.url" $r22read; & git -C $r23ra config "remote.$r22read.pushurl" $r22other
    $r23gSaved = $script:Repo; $script:Repo = $r23ra
    try { $r23gh = Get-HubLease } finally { $script:Repo = $r23gSaved }
    Assert-That ("$($r23gh.Error)" -match 'REMOTE_URL_UNSAFE') 'r23 A1 lease read: a fetch url that is also the name of a configured remote is refused (REMOTE_URL_UNSAFE)' "err=$($r23gh.Error)"
    $r23aErr = Invoke-R22Lease $r23ra @{ Exists = $true; Sha = $r22S; Holder = ''; Url = $r22read } 'r23a1'
    Assert-That ($r23aErr -match 'LEASE_PUSH_URL_DIFFERS' -and $r23aErr -match 'REMOTE_URL_UNSAFE' -and (Get-R22Ref $r22other) -ceq $r22S -and (Get-R22Ref $r22read) -ceq $r22S) 'r23 A1 lease: a push url that is also a configured remote name (whose pushurl is other.git) is refused; other.git never receives the push' "err=$r23aErr otherRef=$(Get-R22Ref $r22other) S=$r22S"
    # control: the same repository without the alias remote still acquires the lease
    $r23rc = New-R22LeaseRepo 'repo-r23-a1c'
    & git -C $r23rc remote add fork $r22read
    $r23cErr = Invoke-R22Lease $r23rc @{ Exists = $true; Sha = $r22S; Holder = ''; Url = $r22read } 'r23a1c'
    Assert-That ($r23cErr -eq '' -and (Get-R22Holder $r22read) -ceq 'TEST-HOST') 'r23 A1 lease control: without an alias remote the lease is acquired in the repository it read' "err=$r23cErr"
}
# r23 (adversary pass 26 on ae45ed76, A2): a url Clone ADDS (absent fork/origin) and the url of an initial clone (MLV-App and the doctrine bus) are checked against the
# url.BASE.insteadOf rules before use, like a configured url; a rewritten url is refused typed and nothing is fetched or cloned from the rewrite target.
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r23 A2')) {
    $r23ws = New-Scratch 'r23-clone'
    $r23seed = Join-Path $r23ws 'seed'; [void][IO.Directory]::CreateDirectory($r23seed); & git -C $r23seed init -q 2>$null
    [IO.File]::WriteAllText((Join-Path $r23seed 'x.txt'), 'unrelated'); & git -C $r23seed add x.txt 2>$null
    & git -C $r23seed -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false commit -q --no-verify -m unrelated 2>$null
    $r23un = Join-Path $r23ws 'unrelated.git'; & git init -q --bare $r23un 2>$null; & git -C $r23seed push -q --no-verify $r23un 'HEAD:refs/heads/master' 2>$null
    $r23unFwd = $r23un -replace '\\', '/'
    $r23run = {
        param([string]$RepoDir, [string]$DocDir, [string]$Log)
        $sv = @{ Repo = $script:Repo; Doc = $script:DoctrineRepo; Log = $script:LogFile }
        $script:Repo = $RepoDir; $Repo = $RepoDir; $script:DoctrineRepo = $DocDir; $DoctrineRepo = $DocDir; $script:LogFile = $Log
        $script:Failures.Clear()
        try { Invoke-PhaseClone *>&1 | Out-Null } catch { } finally { $script:Repo = $sv.Repo; $script:DoctrineRepo = $sv.Doc; $script:LogFile = $sv.Log }
        return @($script:Failures)
    }
    # (a) an existing checkout with origin and no fork; a repository url rewrite rule maps the fork url to unrelated.git
    $r23repo = Join-Path $r23ws 'MLV-App'; [void][IO.Directory]::CreateDirectory($r23repo); & git -C $r23repo init -q 2>$null
    & git -C $r23repo remote add origin $UpstreamUrl; & git -C $r23repo config "url.$r23unFwd.insteadOf" $ForkUrl
    $r23doc = Join-Path $r23ws 'doctrine'; [void][IO.Directory]::CreateDirectory($r23doc); & git -C $r23doc init -q 2>$null; & git -C $r23doc remote add origin $DoctrineUrl
    $r23f = & $r23run $r23repo $r23doc (Join-Path $r23ws 'clone-a.log')
    $r23forkRef = ("$(& git -C $r23repo rev-parse --verify --quiet refs/remotes/fork/master 2>$null)").Trim()
    Assert-That (@($r23f | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH* fork*' }).Count -ge 1 -and -not $r23forkRef) 'r23 A2 clone: an added fork url that a url.insteadOf rule rewrites is refused typed CLONE_REMOTE_MISMATCH and nothing is fetched into refs/remotes/fork' "failures=$($r23f -join ',') forkRef=$r23forkRef"
    # (b) initial clones: neither checkout exists; command-scope rules (GIT_CONFIG_COUNT) rewrite the fork and doctrine urls to unrelated.git
    $r23fresh = Join-Path $r23ws 'fresh'; [void][IO.Directory]::CreateDirectory($r23fresh)
    $r23cfgNames = 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0', 'GIT_CONFIG_KEY_1', 'GIT_CONFIG_VALUE_1'
    $r23cfgSaved = @{}; foreach ($n in $r23cfgNames) { $r23cfgSaved[$n] = [Environment]::GetEnvironmentVariable($n) }
    try {
        [Environment]::SetEnvironmentVariable('GIT_CONFIG_COUNT', '2')
        [Environment]::SetEnvironmentVariable('GIT_CONFIG_KEY_0', "url.$r23unFwd.insteadOf"); [Environment]::SetEnvironmentVariable('GIT_CONFIG_VALUE_0', $ForkUrl)
        [Environment]::SetEnvironmentVariable('GIT_CONFIG_KEY_1', "url.$r23unFwd.insteadOf"); [Environment]::SetEnvironmentVariable('GIT_CONFIG_VALUE_1', $DoctrineUrl)
        $r23fb = & $r23run (Join-Path $r23fresh 'MLV-App') (Join-Path $r23fresh 'doctrine') (Join-Path $r23ws 'clone-b.log')
    } finally { foreach ($n in $r23cfgNames) { [Environment]::SetEnvironmentVariable($n, $r23cfgSaved[$n]) } }
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $r23fresh 'MLV-App\.git')) -and @($r23fb | Where-Object { $_ -like 'CLONE_REMOTE_MISMATCH*' }).Count -ge 1) 'r23 A2 clone: an initial MLV-App clone whose url a url.insteadOf rule rewrites is refused typed CLONE_REMOTE_MISMATCH and nothing is cloned' "failures=$($r23fb -join ',')"
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $r23fresh 'doctrine\.git')) -and @($r23fb | Where-Object { $_ -like 'CLONE_DOCTRINE_REMOTE_MISMATCH*' }).Count -ge 1) 'r23 A2 doctrine: an initial doctrine clone whose url a url.insteadOf rule rewrites is refused typed CLONE_DOCTRINE_REMOTE_MISMATCH and nothing is cloned' "failures=$($r23fb -join ',')"
    $script:Failures.Clear()
}
# r23 (adversary pass 27 A1/A2 on d7359c2c; pass 26 A3): the credential detector decodes the \U, \x and \/ escapes a TOML, YAML or JSON reader decodes, and an INI table header
# may end in a ';' comment
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r23 p27 detector')) {
    Assert-That (Test-CredentialText '"\U00000070assword" = "hunter2"') 'r23 p27 A1 detector: a TOML \U00000070assword key is a credential' ''
    Assert-That (Test-CredentialText ('["\U00000061uth"]' + "`n" + 'key = "hunter2"')) 'r23 p27 A1 detector: a TOML table ["\U00000061uth"] with a value is a credential' ''
    Assert-That (Test-CredentialText '"\x70assword": hunter2') 'r23 p27 A1 detector: a YAML \x70assword key is a credential' ''
    Assert-That (Test-CredentialText '{"endpoint":"https:\/\/alice:hunter2@example.org/repo"}') 'r23 p26 A3 detector: a JSON url with \/ escapes and user:password is a credential' ''
    Assert-That (Test-CredentialText ('[auth] ; connection settings' + "`n" + 'key = hunter2')) 'r23 p27 A2 detector: an INI [auth] table whose header ends in a ; comment is a credential' ''
    Assert-That (-not (Test-CredentialText '"\U00000070ath" = "C:/work"')) 'r23 p27 control detector: a \U escape that spells a plain key is not a credential' ''
    if (Test-Has 'Test-CredentialNamePresent' 'r23 p27 role name') { Assert-That (Test-CredentialNamePresent '\U00000070assword') 'r23 p27 A1 role name: a \U-escaped password name is a credential name' '' }
}
# r25 (adversary pass 29 on 39c1f9f2, A1/A2): a netrc reader accepts a quoted or backslashed keyword, and a YAML/TOML double-quoted scalar takes a backslash-newline as an escaped line break
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'r25 p29 detector')) {
    Assert-That (Test-CredentialText 'machine example.org login alice "password" hunter2') 'r25 p29 A1 detector: a netrc "password" (quoted keyword) is a credential' ''
    Assert-That (Test-CredentialText 'machine example.org login alice pass\word hunter2') 'r25 p29 A1 detector: a netrc pass\word (backslash inside the keyword) is a credential' ''
    Assert-That (Test-CredentialText 'machine example.org login alice ''password'' hunter2') 'r25 p29 A1 detector: a netrc single-quoted password keyword is a credential' ''
    Assert-That (Test-CredentialText '"machine" example.org "login" alice "passwd" hunter2') 'r25 p29 A1 detector: a netrc with every keyword quoted is a credential' ''
    Assert-That (Test-CredentialText ('? "pass\' + "`n" + '  word"' + "`n" + ': hunter2')) 'r25 p29 A2 detector: a YAML explicit key "pass\<newline>  word" (an escaped line break) is a credential' ''
    Assert-That (Test-CredentialText ('"pass\' + "`r`n" + '   word": hunter2')) 'r25 p29 A2 detector: the same split key with a CRLF is a credential' ''
    Assert-That (-not (Test-CredentialText 'machine example.org login alice')) 'r25 p29 control: a netrc record with no password is not a credential' ''
    Assert-That (-not (Test-CredentialText ('"pat\' + "`n" + '   h": C:/work'))) 'r25 p29 control: an escaped line break that spells a plain key is not a credential' ''
    # r25 (adversary pass 30 on b621d213, A2/A3): an escaped quote inside a quoted table-header name, and a quote inside a URL user or password
    Assert-That (Test-CredentialText ('["password\"primary"]' + "`n" + 'key = "hunter2"')) 'r25 p30 A2 detector: a TOML table ["password\"primary"] (escaped quote in the name) with a value is a credential' ''
    Assert-That (Test-CredentialText ('[["password\"primary"]]' + "`n" + 'key = "hunter2"')) 'r25 p30 A2 detector: the array-of-tables spelling [["password\"primary"]] is a credential' ''
    Assert-That (Test-CredentialText '{"endpoint":"https://alice:p\"ass@example.org/repo"}') 'r25 p30 A3 detector: a JSON url whose password holds an escaped quote is a credential' ''
    Assert-That (Test-CredentialText 'endpoint = https://alice:p"ass@example.org/repo') 'r25 p30 A3 detector: a raw url whose password holds a quote is a credential' ''
    Assert-That (Test-CredentialText 'endpoint = https://al"ice:hunter2@example.org/repo') 'r25 p30 A3 detector: a raw url whose user holds a quote is a credential' ''
    Assert-That (-not (Test-CredentialText '{"endpoint":"https://example.org/repo","contact":"a@example.org"}')) 'r25 p30 control: a url with no user and password next to an email is not a credential' ''
}
# r25 (adversary pass 29 A3): a save that lands in the instant before the newest-save restore Replace is the NEWEST; it must be live at the end, never left in the .undo file
if ($fnLoaded -and (Test-Has 'Copy-ImportFileLocked' 'r25 p29 A3')) {
    $r25d = New-Scratch 'r25-a3'
    $r25Src = Join-Path $r25d 'bundle\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $r25Src))
    $r25Dst = Join-Path $r25d 'host\service.json'; [void][IO.Directory]::CreateDirectory((Split-Path -Parent $r25Dst))
    [IO.File]::WriteAllText($r25Src, '{"v":"bundle"}'); (Get-Item -LiteralPath $r25Src).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 11, 0, 0, [DateTimeKind]::Utc)
    [IO.File]::WriteAllText($r25Dst, '{"v":"old"}'); (Get-Item -LiteralPath $r25Dst).LastWriteTimeUtc = [DateTime]::new(2026, 11, 16, 10, 0, 0, [DateTimeKind]::Utc)
    $script:R25Moved = $null
    $script:ImportBeforeSwapHook = { param($Dest) [IO.File]::WriteAllText($Dest, '{"v":"owner-v1"}'); [IO.File]::SetLastWriteTimeUtc($Dest, [DateTime]::new(2026, 11, 16, 12, 0, 0, [DateTimeKind]::Utc)) }
    $script:ImportBeforeRollbackHook = { param($Dest) $s2 = "$Dest.editor-save"; [IO.File]::WriteAllText($s2, '{"api_key":"owner-v2"}'); try { [IO.File]::Move($s2, $Dest, $true) } catch { } }
    $script:ImportBeforeAsideRestoreHook = { param($Dest) $s3 = "$Dest.editor-save3"; [IO.File]::WriteAllText($s3, '{"api_key":"owner-v3"}'); try { [IO.File]::Move($s3, $Dest, $true) } catch { } }
    $script:ImportBeforeUndoHook = { param($Dest) $s4 = "$Dest.editor-save4"; [IO.File]::WriteAllText($s4, '{"api_key":"owner-v4"}'); try { [IO.File]::Move($s4, $Dest, $true); $script:R25Moved = $true } catch { $script:R25Moved = "move failed: $($_.Exception.Message)" } }
    $r25msg = ''
    try { $r25r = Copy-ImportFileLocked -Source ([IO.FileInfo]::new($r25Src)) -Dest $r25Dst -ExcludeNewer $false -ExcludeOlder $true -ExcludeChanged $false; $r25msg = "result=$($r25r.Result)" } catch { $r25msg = $_.Exception.Message } finally { $script:ImportBeforeSwapHook = $null; $script:ImportBeforeRollbackHook = $null; $script:ImportBeforeAsideRestoreHook = $null; $script:ImportBeforeUndoHook = $null }
    Write-Host "INFO r25 A3 seam: moved=$($script:R25Moved) | $r25msg"
    if ($script:R25Moved -eq $true) {
        Assert-That (([IO.File]::ReadAllText($r25Dst)) -ceq '{"api_key":"owner-v4"}') 'r25 p29 A3 import: the newest save (landed in the instant before the .undo Replace) is the live file' "$r25msg | $([IO.File]::ReadAllText($r25Dst))"
        Assert-That ($r25msg -like 'IMPORT_ROLLBACK_DEST_CHANGED*' -and $r25msg -match 'again') 'r25 p29 A3 import: the refusal names that the newest save is live again' $r25msg
    } else {
        Assert-That (([IO.File]::ReadAllText($r25Dst)) -notmatch 'bundle') 'r25 p29 A3 import: a newest save refused by the lock loses nothing and the bundle copy is not live' "moved=$($script:R25Moved) $r25msg | $([IO.File]::ReadAllText($r25Dst))"
    }
    Assert-That ($null -ne (Get-Variable -Scope Script -Name ImportBeforeUndoHook -ErrorAction SilentlyContinue)) 'r25 p29 A3 import: the .undo seam variable is initialised'
}
# (c) fable key3 r21 B1 + H1, opus H2: deny by default. An ssh spelling is normalised only for a plain user, host and path; anything else is keyed by its raw string.
if ($fnLoaded -and (Test-Has 'Test-GitRemoteUrlSame' 'r22 fable B1')) {
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:a~b/lease.git' 'ssh://git@nas.example/~/a~b/lease.git')) 'r22 fable B1 R-a url: an scp path whose second character is ~ (Git sends ~b/lease.git) is not ssh://host/~/a~b/lease.git'
    Assert-That (-not (Test-GitRemoteUrlSame 'ab:cd@nas.example:repos/lease.git' 'ssh://ab:cd@nas.example/~/repos/lease.git')) 'r22 fable B1 R-b url: a colon in the scp user part (Git: host ab) is not the ssh:// user ab:cd'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:/~/r.git' 'ssh://git@nas.example/~/r.git')) 'r22 fable H1 url: scp host:/~/p is uncertain and fails closed'
    Assert-That (-not (Test-GitRemoteUrlSame 'git@nas.example:x~git/lease.git' 'git@nas.example:~/x~git/lease.git')) 'r22 opus H2 url: scp host:x~git/p (Git sends ~git/p) is not host:~/x~git/p'
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://git@nas.example/repos/lease.git?x' 'git@nas.example:/repos/lease.git?x')) 'r22 url: a query is outside the plain alphabet, so the two spellings are keyed raw'
    Assert-That (-not (Test-GitRemoteUrlSame 'git+ssh://git@nas.example/repos/lease.git' 'ssh://git@nas.example/repos/lease.git')) 'r22 fable r21 hardening url: another scheme is not normalised to ssh://'
    Assert-That (-not (Test-GitRemoteUrlSame 'ssh://git@nas.example/repos/lease.git/' 'ssh://git@nas.example/repos/lease.git')) 'r22 fable r21 hardening url: a trailing slash is significant'
    Assert-That (Test-GitRemoteUrlSame 'git@nas.example:repos/lease.git' 'ssh://git@nas.example/~/repos/lease.git') 'r22 url control: a home-relative scp path equals ssh://host/~/path'
    Assert-That (Test-GitRemoteUrlSame 'git@nas.example:/repos/lease.git' 'ssh://git@nas.example/repos/lease.git') 'r22 url control: an absolute scp path equals ssh://host/path'
    Assert-That (Test-GitRemoteUrlSame 'ab:cd@nas.example:repos/lease.git' 'ab:cd@nas.example:repos/lease.git') 'r22 url control: an identical uncertain spelling is the same'
    Assert-That (Test-GitRemoteUrlSame 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git' 'git@github.com:SuperDimensionalSpaceFortress/MLV-App.git') 'r22 url control: the live lease remote (https fetch, git@github.com push) still names one repository'
}
# (d) sol r21 B2 = opus H3: Open-GateLock returns null only for a sharing or lock violation. A lock file that vanishes before the open is created and the open retried once;
# a second failure, or any other error, is the typed GATE_LOCK_IO_FAILED, never 'a lease release is in progress' (Gate, exit 0) or 'a Lease-gated task is running' (exit 4).
if ($fnLoaded -and (Test-Has 'Open-GateLock' 'r22 sol B2')) {
    $r22lr = New-Scratch 'r22-lock-fnf'
    $global:R22Lock = @{ Calls = 0; FailTimes = 1; Orig = $(if (Get-Command Open-GateLockStream -CommandType Function -ErrorAction SilentlyContinue) { ${function:Open-GateLockStream} } else { $null }) }
    function Open-GateLockStream { param([string]$Path, [string]$Mode)
        $global:R22Lock.Calls++
        if ($global:R22Lock.Calls -le $global:R22Lock.FailTimes) { if ([IO.File]::Exists($Path)) { [IO.File]::Delete($Path) }; throw [IO.FileNotFoundException]::new('r22 test: the lock file vanished before the open', $Path) }
        & $global:R22Lock.Orig -Path $Path -Mode $Mode
    }
    $r22l1 = $null; $r22l1Err = ''
    try { $r22l1 = Open-GateLock -Root $r22lr -Mode Shared } catch { $r22l1Err = $_.Exception.Message }
    Assert-That ($null -ne $r22l1 -and $global:R22Lock.Calls -eq 2 -and (Test-Path -LiteralPath (Join-Path $r22lr 'gate.lock'))) 'r22 sol B2 lock: a lock file that vanishes before the open is created again and the lock is acquired (one retry)' "calls=$($global:R22Lock.Calls) err=$r22l1Err"
    if ($r22l1) { $r22l1.Dispose() }
    $global:R22Lock.Calls = 0; $global:R22Lock.FailTimes = 99
    $r22l2 = $null; $r22l2Err = ''
    try { $r22l2 = Open-GateLock -Root $r22lr -Mode Exclusive } catch { $r22l2Err = $_.Exception.Message }
    Assert-That ($null -eq $r22l2 -and $r22l2Err -match '^GATE_LOCK_IO_FAILED' -and $global:R22Lock.Calls -eq 2) 'r22 sol B2 lock: a second missing-file failure is the typed GATE_LOCK_IO_FAILED, not contention' "calls=$($global:R22Lock.Calls) err=$r22l2Err lock=$([bool]$r22l2)"
    if ($r22l2) { $r22l2.Dispose() }
    if ($global:R22Lock.Orig) { Set-Item -Path 'Function:\Open-GateLockStream' -Value $global:R22Lock.Orig } else { Remove-Item -Path 'Function:\Open-GateLockStream' -Force -ErrorAction SilentlyContinue }
    # end to end: a gate.lock that cannot be opened as a file (a folder stands there) is a typed failure in both consumers, exit 5
    $r22lrDir = New-Scratch 'r22-lock-dir'; [void][IO.Directory]::CreateDirectory((Join-Path $r22lrDir 'gate.lock'))
    [IO.File]::Delete($gateMarker) 2>$null
    $wsR22 = New-LeaseWorkspace 'ws-r22-gate' $me
    $r22gState = New-Scratch 'state-r22-g'
    $r22g = Invoke-Setup (New-Scratch 'home-r22-g') @('-Phase', 'Gate', '-GateMode', 'Lease', '-GateScript', $gateTarget, '-StateRoot', $r22gState, '-LockRoot', $r22lrDir, '-Workspace', $wsR22)
    $r22gLogPath = Join-Path $r22gState 'logs\gate.log'   # a gate run writes its log file, not the console
    $r22gText = $r22g.Out + "`n" + $(if (Test-Path -LiteralPath $r22gLogPath) { [IO.File]::ReadAllText($r22gLogPath) } else { '' })
    Assert-That ($r22g.Exit -eq 5 -and $r22gText -match 'GATE_LOCK_IO_FAILED' -and $r22gText -notmatch 'release is in progress' -and -not (Test-Path -LiteralPath $gateMarker)) 'r22 sol B2 gate: an unopenable gate lock is GATE_LOCK_IO_FAILED exit 5, not a release-in-progress skip, and the target does not run' "exit=$($r22g.Exit)`n$r22gText"
    $r22rel = Invoke-Setup (New-Scratch 'home-r22-r') @('-Phase', 'LeaseRelease', '-StateRoot', (New-Scratch 'state-r22-r'), '-LockRoot', $r22lrDir, '-Workspace', $wsR22)
    Assert-That ($r22rel.Exit -eq 5 -and $r22rel.Out -match 'GATE_LOCK_IO_FAILED' -and $r22rel.Out -notmatch 'task is running' -and (Get-LeaseHolder $wsR22) -ceq $me) 'r22 sol B2 release: an unopenable gate lock is GATE_LOCK_IO_FAILED exit 5, not a running-gate refusal, and the lease is unchanged' "exit=$($r22rel.Exit)`n$($r22rel.Out)"
}

# ================================================================ r26 (sol r25 B1, fable key3 r25 B1; HUB RULING fail closed, no more spelling chases)
# sol r25 B1: Git reads legacy remote aliases (remotes/<name>, branches/<name>) from the COMMON git dir, which in a linked worktree is not '$Repo\.git' (a file there).
# Any entry under remotes/ or branches/ of the common or the per-worktree git dir is refused typed LEASE_LEGACY_REMOTE_ALIAS_PRESENT; alias files are never parsed.
if ($fnLoaded -and (Test-Has 'Set-HubLease' 'r26 sol B1') -and (Get-Command New-R22LeaseRepo -ErrorAction SilentlyContinue)) {
    # (a) sol's input: a linked worktree, url 'lease.git', pushurl 'lease.git/', '<common>\remotes\lease.git' says 'URL: other.git', and a bare '<worktree>\lease.git'
    $r26main = New-R22LeaseRepo 'repo-r26-a'; $r26other = $script:r22other; $r26read = $script:r22read
    $r26wt = Join-Path (Split-Path $r26main) 'wt'
    & git -C $r26main worktree add -q --detach $r26wt 'refs/hub-lease-local/mlv-app' 2>$null
    $r26local = Join-Path $r26wt 'lease.git'; & git init -q --bare $r26local 2>$null; & git -C $r22seed push -q --no-verify $r26local "HEAD:$LeaseRef" 2>$null
    $r26common = ("$(& git -C $r26wt rev-parse --path-format=absolute --git-common-dir 2>$null)").Trim()
    [void][IO.Directory]::CreateDirectory((Join-Path $r26common 'remotes'))
    [IO.File]::WriteAllText((Join-Path $r26common 'remotes\lease.git'), "URL: $($r26other -replace '\\', '/')`n", [Text.Encoding]::ASCII)
    & git -C $r26wt remote add fork 'lease.git' 2>$null; & git -C $r26wt config remote.fork.pushurl 'lease.git/' 2>$null
    $r26gSaved = $script:Repo; $script:Repo = $r26wt
    try { $r26gh = Get-HubLease } finally { $script:Repo = $r26gSaved }
    Assert-That ("$($r26gh.Error)" -match 'LEASE_LEGACY_REMOTE_ALIAS_PRESENT') 'r26 sol B1 lease read: a linked worktree whose common git dir holds remotes/lease.git is refused (LEASE_LEGACY_REMOTE_ALIAS_PRESENT), the alias is never parsed' "err=$($r26gh.Error)"
    $r26aErr = Invoke-R22Lease $r26wt @{ Exists = $true; Sha = $r22S; Holder = ''; Url = 'lease.git' } 'r26a'
    Assert-That ($r26aErr -match 'LEASE_LEGACY_REMOTE_ALIAS_PRESENT' -and (Get-R22Ref $r26local) -ceq $r22S -and (Get-R22Ref $r26other) -ceq $r22S) 'r26 sol B1 lease: the push guard refuses a common-dir legacy alias; neither lease.git/ nor other.git receives a push' "err=$r26aErr localRef=$(Get-R22Ref $r26local) otherRef=$(Get-R22Ref $r26other) S=$r22S"
    # (b) the per-worktree git dir: a 'branches\x' entry there is refused too (plain urls, no alias name in use)
    $r26bmain = New-R22LeaseRepo 'repo-r26-b'; $r26bread = $script:r22read
    $r26bwt = Join-Path (Split-Path $r26bmain) 'wt'
    & git -C $r26bmain worktree add -q --detach $r26bwt 'refs/hub-lease-local/mlv-app' 2>$null
    $r26bgd = ("$(& git -C $r26bwt rev-parse --path-format=absolute --git-dir 2>$null)").Trim()
    [void][IO.Directory]::CreateDirectory((Join-Path $r26bgd 'branches')); [IO.File]::WriteAllText((Join-Path $r26bgd 'branches\x'), "unrelated`n", [Text.Encoding]::ASCII)
    & git -C $r26bwt remote add fork $r26bread 2>$null
    $r26bErr = Invoke-R22Lease $r26bwt @{ Exists = $true; Sha = $r22S; Holder = ''; Url = $r26bread } 'r26b'
    Assert-That ($r26bErr -match 'LEASE_LEGACY_REMOTE_ALIAS_PRESENT' -and (Get-R22Holder $r26bread) -ceq '') 'r26 sol B1 lease: an entry under the per-worktree git dir branches\ is refused; nothing is pushed' "err=$r26bErr holder=$(Get-R22Holder $r26bread)"
    # (c) a main worktree: any entry under .git\remotes (an unrelated name) refuses, not only one whose name equals a url
    $r26c = New-R22LeaseRepo 'repo-r26-c'; $r26cread = $script:r22read
    [void][IO.Directory]::CreateDirectory((Join-Path $r26c '.git\remotes')); [IO.File]::WriteAllText((Join-Path $r26c '.git\remotes\unrelated'), "URL: x`n", [Text.Encoding]::ASCII)
    & git -C $r26c remote add fork $r26cread 2>$null
    $r26cErr = Invoke-R22Lease $r26c @{ Exists = $true; Sha = $r22S; Holder = ''; Url = $r26cread } 'r26c'
    Assert-That ($r26cErr -match 'LEASE_LEGACY_REMOTE_ALIAS_PRESENT' -and (Get-R22Holder $r26cread) -ceq '') 'r26 sol B1 lease: any entry under .git\remotes refuses (fail closed, never parsed)' "err=$r26cErr holder=$(Get-R22Holder $r26cread)"
    # (d) control: a linked worktree with EMPTY remotes\ and branches\ folders still acquires the lease in the repository it read
    $r26dmain = New-R22LeaseRepo 'repo-r26-d'; $r26dread = $script:r22read
    $r26dwt = Join-Path (Split-Path $r26dmain) 'wt'
    & git -C $r26dmain worktree add -q --detach $r26dwt 'refs/hub-lease-local/mlv-app' 2>$null
    $r26dcommon = ("$(& git -C $r26dwt rev-parse --path-format=absolute --git-common-dir 2>$null)").Trim()
    foreach ($ld in 'remotes', 'branches') { [void][IO.Directory]::CreateDirectory((Join-Path $r26dcommon $ld)) }
    & git -C $r26dwt remote add fork $r26dread 2>$null
    $r26dErr = Invoke-R22Lease $r26dwt @{ Exists = $true; Sha = $r22S; Holder = ''; Url = $r26dread } 'r26d'
    Assert-That ($r26dErr -eq '' -and (Get-R22Holder $r26dread) -ceq 'TEST-HOST') 'r26 sol B1 lease control: a linked worktree with empty legacy folders acquires the lease in the repository it read' "err=$r26dErr holder=$(Get-R22Holder $r26dread)"
    # (e) a git dir that cannot be resolved refuses (the census never assumes an empty list)
    if (Test-Has 'Get-RemoteUrlContext' 'r26 sol B1 unresolved') {
        $r26eDir = New-Scratch 'r26-not-a-repo'; $r26eErr = ''
        $r26eSaved = [Environment]::GetEnvironmentVariable('GIT_CEILING_DIRECTORIES'); [Environment]::SetEnvironmentVariable('GIT_CEILING_DIRECTORIES', (Split-Path $r26eDir))
        try { [void](Get-RemoteUrlContext -RepoDir $r26eDir) } catch { $r26eErr = $_.Exception.Message } finally { [Environment]::SetEnvironmentVariable('GIT_CEILING_DIRECTORIES', $r26eSaved) }
        Assert-That ($r26eErr -match 'LEASE_GIT_DIR_UNRESOLVED') 'r26 sol B1 census: a directory whose git dir cannot be resolved is refused typed LEASE_GIT_DIR_UNRESOLVED' "err=$r26eErr"
    }
}
# fable key3 r25 B1: an https authority holding '#', '?', '\' or a second '@' before the host is not [userinfo@]host[:port]: Git and curl end it at the first '?' or '#', so the
# url is keyed by its raw string (byte-equal only) and the redactor never prints a host the url does not contact.
if ($fnLoaded -and (Test-Has 'Test-GitRemoteUrlSame' 'r26 fable B1') -and (Test-Has 'Get-RedactedRemoteUrl' 'r26 fable B1') -and (Test-Has 'ConvertTo-RedactedUrlText' 'r26 fable B1')) {
    $r26gh = 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git'
    foreach ($bad in @('https://evil.example#@github.com/SuperDimensionalSpaceFortress/MLV-App.git', 'https://evil.example?@github.com/SuperDimensionalSpaceFortress/MLV-App.git',
            'https://evil.example\@github.com/SuperDimensionalSpaceFortress/MLV-App.git', 'https://a@evil.example#@github.com/SuperDimensionalSpaceFortress/MLV-App.git')) {
        Assert-That (-not (Test-GitRemoteUrlSame $bad $r26gh)) "r26 fable B1 url: $bad is not the GitHub fork (Git connects to evil.example)"
        Assert-That (Test-GitRemoteUrlSame $bad $bad) "r26 fable B1 url: $bad is keyed raw (equal only to the identical string)"
        $red = Get-RedactedRemoteUrl $bad
        Assert-That ($red -notmatch 'github' -and $red -notmatch 'evil') "r26 fable B1 redact: $bad prints without naming a host ($red)" $red
        $txt = ConvertTo-RedactedUrlText "remote 'fork' pushes to $bad, expected x"
        Assert-That ($txt -notmatch '\*\*\*@github') "r26 fable B1 sink: free text quoting $bad never prints ***@github" $txt
    }
    Assert-That (-not (Test-GitRemoteUrlSame 'https://127.0.0.1:9#@github.com/o/r' 'https://github.com/o/r')) 'r26 fable B1 url: https://127.0.0.1:9#@github.com/o/r is not https://github.com/o/r'
    Assert-That (-not (Test-GitRemoteUrlSame 'https://evil.example?@nas.example/p' 'https://nas.example/p')) 'r26 fable B1 url: a non-GitHub https authority with ?@ is keyed raw'
    Assert-That (-not (Test-GitRemoteUrlSame 'https://alice:pa/ss@github.com/o/r' 'https://github.com/o/r')) 'r26 fable B1 url: an @ after the first / is path, not userinfo'
    Assert-That ((Get-RedactedRemoteUrl 'https://alice:pa/ss@github.com/o/r') -notmatch 'github|ss') 'r26 fable B1 redact: an @ after the first / is fully redacted (no host that is not contacted, no password tail)' (Get-RedactedRemoteUrl 'https://alice:pa/ss@github.com/o/r')
    Assert-That ((Format-RemoteUrlForLog 'https://127.0.0.1:9#@github.com/o/r') -notmatch 'github') 'r26 fable B1 redact: Format-RemoteUrlForLog never names github.com for 127.0.0.1:9#@github.com' (Format-RemoteUrlForLog 'https://127.0.0.1:9#@github.com/o/r')
    # controls: a plain userinfo, a port and a query after the path are unchanged
    Assert-That (Test-GitRemoteUrlSame 'https://x-access-token:TOK@github.com/o/r.git' 'https://github.com/o/r') 'r26 url control: a plain token userinfo still names the GitHub repository'
    Assert-That (Test-GitRemoteUrlSame 'https://nas.example:8443/p' 'https://NAS.example:8443/p') 'r26 url control: host:port is plain (host case-folded)'
    Assert-That (-not (Test-GitRemoteUrlSame 'https://nas.example:8443/p' 'https://nas.example/p')) 'r26 url control: a port is significant'
    Assert-That ((Get-RedactedRemoteUrl 'https://u:pw@nas.example:8443/p?t=1') -ceq 'https://***@nas.example:8443/p?***') 'r26 redact control: a plain authority with a port prints its host' (Get-RedactedRemoteUrl 'https://u:pw@nas.example:8443/p?t=1')
}
if ($fnLoaded -and (Test-Has 'Set-HubLease' 'r26 fable B1 lease') -and (Get-Command New-R22LeaseRepo -ErrorAction SilentlyContinue)) {
    # the lease guard: fetch https://github.com/o/r, push https://127.0.0.1:9#@github.com/o/r (GIT_ALLOW_PROTOCOL=file here, so nothing can connect)
    $r26f = New-R22LeaseRepo 'repo-r26-f'
    & git -C $r26f remote add fork 'https://github.com/o/r' 2>$null; & git -C $r26f config remote.fork.pushurl 'https://127.0.0.1:9#@github.com/o/r' 2>$null
    $r26fErr = Invoke-R22Lease $r26f @{ Exists = $true; Sha = $r22S; Holder = ''; Url = 'https://github.com/o/r' } 'r26f'
    $r26fLog = Join-Path (Split-Path $r26f) 'lease-r26f.log'
    $r26fLogText = $(if (Test-Path -LiteralPath $r26fLog) { [IO.File]::ReadAllText($r26fLog) } else { '' })
    Assert-That ($r26fErr -match 'LEASE_PUSH_URL_DIFFERS' -and $r26fErr -notmatch '\*\*\*@github' -and ($r26fLogText + $r26fErr) -notmatch '> git .*push') 'r26 fable B1 lease: a push url whose authority holds #@ is LEASE_PUSH_URL_DIFFERS, nothing is pushed, and no line names ***@github.com' "err=$r26fErr`nlog=$r26fLogText"
}
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'r26 clone')) {
    # Clone and doctrine siblings of both blockers: run 1 = a legacy branches\ entry in MLV-App + a doctrine origin pushurl with #@; run 2 = a fork pushurl with ?@ + a doctrine remotes\ entry
    foreach ($r26case in @(@{ N = 1; Legacy = 'mlv'; ForkPush = ''; DocPush = 'https://evil.example#@github.com/layibabalola/softwarefactory-fleet-doctrine.git' },
            @{ N = 2; Legacy = 'doc'; ForkPush = 'https://evil.example?@github.com/SuperDimensionalSpaceFortress/MLV-App.git'; DocPush = '' })) {
        $r26cws = New-Scratch "r26-clone-$($r26case.N)"
        $r26crepo = Join-Path $r26cws 'MLV-App'; [void][IO.Directory]::CreateDirectory($r26crepo); & git -C $r26crepo init -q 2>$null
        & git -C $r26crepo remote add fork $ForkUrl 2>$null; & git -C $r26crepo remote add origin $UpstreamUrl 2>$null
        if ($r26case.ForkPush) { & git -C $r26crepo config remote.fork.pushurl $r26case.ForkPush 2>$null }
        $r26cdoc = Join-Path $r26cws 'doctrine'; [void][IO.Directory]::CreateDirectory($r26cdoc); & git -C $r26cdoc init -q 2>$null; & git -C $r26cdoc remote add origin $DoctrineUrl 2>$null
        if ($r26case.DocPush) { & git -C $r26cdoc config remote.origin.pushurl $r26case.DocPush 2>$null }
        $r26lroot = $(if ($r26case.Legacy -eq 'mlv') { Join-Path $r26crepo '.git\branches' } else { Join-Path $r26cdoc '.git\remotes' })
        [void][IO.Directory]::CreateDirectory($r26lroot); [IO.File]::WriteAllText((Join-Path $r26lroot 'x'), "URL: x`n", [Text.Encoding]::ASCII)
        $savedRepo = $script:Repo; $savedDoc = $script:DoctrineRepo; $savedLog = $script:LogFile
        $script:Repo = $r26crepo; $Repo = $r26crepo; $script:DoctrineRepo = $r26cdoc; $DoctrineRepo = $r26cdoc; $script:LogFile = Join-Path $r26cws 'clone.log'
        $script:Failures.Clear()
        try { Invoke-PhaseClone *>&1 | Out-Null } catch { }
        $r26clog = $(if (Test-Path -LiteralPath $script:LogFile) { [IO.File]::ReadAllText($script:LogFile) } else { '' })
        $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog
        $r26forkWant = $(if ($r26case.N -eq 1) { 'CLONE_REMOTE_MISMATCH* fork' } else { 'CLONE_REMOTE_MISMATCH* fork pushurl' })
        Assert-That (@($script:Failures | Where-Object { $_ -like $r26forkWant }).Count -eq 1) "r26 clone ($($r26case.N)): the MLV-App fork is refused typed ($r26forkWant)" ($script:Failures -join ',')
        Assert-That (@($script:Failures | Where-Object { $_ -like 'CLONE_DOCTRINE_REMOTE_MISMATCH*' }).Count -eq 1) "r26 doctrine ($($r26case.N)): the doctrine checkout is refused typed CLONE_DOCTRINE_REMOTE_MISMATCH" ($script:Failures -join ',')
        Assert-That ($r26clog -notmatch '\*\*\*@github\.com/(SuperDimensionalSpaceFortress|layibabalola)' -and $r26clog -notmatch 'fetch --quiet') "r26 clone ($($r26case.N)): nothing is fetched and no log line names ***@github.com for a url that would contact evil.example" $r26clog
        $script:Failures.Clear()
    }
}
foreach ($n in @($r22envSaved.Keys)) { [Environment]::SetEnvironmentVariable($n, $r22envSaved[$n]) }

# ---------------------------------------------------------------- MINI3-CUTOVER-CREDENTIAL-DETECTOR-DENY-BY-DEFAULT-1 (filed from #829 r16 adversary pass 17 A1-A3)
# S1: a secret-named key written with ESCAPES a reader decodes (YAML/JSON/TOML \xNN \uNNNN \UNNNNNNNN, XML &#NNN; &#xNN;, PowerShell `u{NN} and `<letter>, C/Python
#     octal \NNN and \N{NAME}, URL %NN) is the same key. An escape that cannot be decoded in a key position counts as secret-named (deny by default).
# S2: a .NET config element that puts the secret NAME in a key= / name= attribute VALUE and the secret in another attribute ('<add key="password" value="x" />').
# S3: a Clone-phase message never carries a url's query token, also when git echoes the url WITHOUT its scheme ('fatal: ''/C:/r.git?token=x'' does not appear ...').
# Every case is GENERATED (doctrine d6d115b) over the same value wrappers as the r15 sweep; the fakes are 'hunter2' and 'fake-token-000N'.
if ($fnLoaded -and (Test-Has 'Test-CredentialText' 'DENY-BY-DEFAULT-1') -and (Test-Has 'Get-CredentialHitLine' 'DENY-BY-DEFAULT-1')) {
    $ddBt = [string][char]0x60
    # one encoder per escape form: the escaped spelling of ONE character, or $null when the form cannot spell it (it is then left as written)
    $ddEnc = [ordered]@{
        'yaml \xNN'        = { param($c) '\x{0:x2}' -f [int]$c }
        'json \uNNNN'      = { param($c) '\u{0:x4}' -f [int]$c }
        'toml \UNNNNNNNN'  = { param($c) '\U{0:x8}' -f [int]$c }
        'xml &#NNN;'       = { param($c) '&#{0};' -f [int]$c }
        'xml &#xNN;'       = { param($c) '&#x{0:x};' -f [int]$c }
        'ps `u{NN}'        = { param($c) $ddBt + ('u{{{0:x}}}' -f [int]$c) }
        'ps `letter'       = { param($c) if ('0abefnrtv'.Contains([string]$c)) { $null } else { $ddBt + [string]$c } }
        'octal \NNN'       = { param($c) '\' + [Convert]::ToString([int]$c, 8).PadLeft(3, '0') }
        'python \N{NAME}'  = { param($c) if ($c -cmatch '[a-z]') { '\N{LATIN SMALL LETTER ' + ([string]$c).ToUpperInvariant() + '}' } elseif ($c -eq [char]'_') { '\N{LOW LINE}' } else { $null } }
        'url %NN'          = { param($c) '%{0:X2}' -f [int]$c }
    }
    # the secret names, each with the index of the ONE letter the 'one letter' variant escapes
    $ddNames = @(@('password', 4), @('token', 2), @('api_key', 4), @('secret', 2))
    $ddKeyForms = [ordered]@{
        'YAML double-quoted' = { param($k, $v) '"' + $k + '": ' + $v }
        'JSON'               = { param($k, $v) '{"' + $k + '": ' + $v + '}' }
        'TOML'               = { param($k, $v) '"' + $k + '" = ' + $v }
        'ini line'           = { param($k, $v) $k + '=' + $v }
        'PS hashtable'       = { param($k, $v) '@{ "' + $k + '" = ' + $v + ' }' }
        'XML attribute pair' = { param($k, $v) $q = $(if ($v.Contains('"')) { "'" } else { '"' }); '<add key=' + $q + $k + $q + ' value=' + $q + $v + $q + ' />' }
        'query string'       = { param($k, $v) 'https://h.example/cb?' + $k + '=' + $v }
    }
    $ddValues = [System.Collections.Generic.List[object]]::new()
    $ddValues.Add(@{ Name = 'hunter2'; Value = 'hunter2' })
    foreach ($wn in $r15Wrap.Keys) { $ddValues.Add(@{ Name = "$wn(hunter2)"; Value = (& $r15Wrap[$wn] 'hunter2') }) }
    $ddTotal = 0
    foreach ($en in $ddEnc.Keys) {
        $ddMiss = [System.Collections.Generic.List[string]]::new(); $ddN = 0
        foreach ($nm in $ddNames) {
            $chars = $nm[0].ToCharArray()
            $one = (@(for ($i = 0; $i -lt $chars.Length; $i++) { if ($i -eq $nm[1]) { $e = & $ddEnc[$en] $chars[$i]; if ($null -eq $e) { [string]$chars[$i] } else { $e } } else { [string]$chars[$i] } }) -join '')
            $all = (@(foreach ($c in $chars) { $e = & $ddEnc[$en] $c; if ($null -eq $e) { [string]$c } else { $e } }) -join '')
            foreach ($key in @($one, $all)) {
                if ($key -ceq $nm[0]) { continue }
                foreach ($fn in $ddKeyForms.Keys) {
                    foreach ($val in $ddValues) {
                        $text = & $ddKeyForms[$fn] $key $val.Value
                        $ddN++
                        if (-not (Test-CredentialText $text) -or (Get-CredentialHitLine $text) -le 0) { $ddMiss.Add("$fn / $key / $($val.Name)") }
                    }
                }
            }
        }
        $ddTotal += $ddN
        Assert-That ($ddMiss.Count -eq 0) "DENY-BY-DEFAULT-1 S1 generated sweep, $en escapes: all $ddN escaped key spellings refuse" ("misses=$($ddMiss.Count): " + (($ddMiss | Select-Object -First 8) -join '; '))
    }
    Assert-That ($ddTotal -ge 5000) "DENY-BY-DEFAULT-1 S1 generated sweep: the sweep ran ($ddTotal cases)" "$ddTotal"
    # S1 deny by default: an escape that cannot be decoded, standing in a key, makes the key secret-named
    $ddResidual = [ordered]@{
        'an out-of-range \U escape'              = '"pass\U0011FFFFord": hunter2'
        'an unknown \N{NAME} escape'             = '"pass\N{NO SUCH CHARACTER}ord": hunter2'
        'an out-of-range XML character ref'      = '<add key="pass&#99999999;ord" value="hunter2" />'
        'an out-of-range PS `u{} escape'         = ('$c[' + "'pass" + $ddBt + "u{110FFFF}ord'] = 'hunter2'")
        'a \x escape with no hex digits'         = 'pass\xZZord=hunter2'
    }
    foreach ($k in $ddResidual.Keys) { Assert-That ((Test-CredentialText $ddResidual[$k]) -and (Get-CredentialHitLine $ddResidual[$k]) -gt 0) "DENY-BY-DEFAULT-1 S1 residual: $k in a key refuses" $ddResidual[$k] }
    # S2: the XML attribute pair, generated over the attribute names, both quote styles, the attribute order and both element shapes
    $ddS2Miss = [System.Collections.Generic.List[string]]::new(); $ddS2N = 0
    foreach ($nameAttr in @('key', 'name', 'Key', 'Name')) {
        foreach ($valAttr in @('value', 'Value', 'connectionString', 'serializedValue')) {
            foreach ($secret in @('password', 'DbPassword', 'ApiKey', 'GitHubToken', 'Pwd', 'client_secret', 'DB Password')) {
                foreach ($q in @('"', "'")) {
                    foreach ($order in @('name first', 'value first')) {
                        foreach ($shape in @('self-closing', 'open and close')) {
                            $na = $nameAttr + '=' + $q + $secret + $q; $va = $valAttr + '=' + $q + 'hunter2' + $q
                            $attrs = $(if ($order -eq 'name first') { "$na $va" } else { "$va $na" })
                            $el = $(if ($shape -eq 'self-closing') { "<add $attrs />" } else { "<add $attrs></add>" })
                            $text = "<configuration>`n  <appSettings>`n    $el`n  </appSettings>`n</configuration>"
                            $ddS2N++
                            if (-not (Test-CredentialText $text) -or (Get-CredentialHitLine $text) -le 0) { $ddS2Miss.Add($el) }
                        }
                    }
                }
            }
        }
    }
    Assert-That ($ddS2Miss.Count -eq 0 -and $ddS2N -ge 800) "DENY-BY-DEFAULT-1 S2 generated sweep: all $ddS2N XML key/value attribute pairs refuse" ("misses=$($ddS2Miss.Count): " + (($ddS2Miss | Select-Object -First 8) -join '; '))
    $ddS2Hit = [ordered]@{
        'a settings element whose value is a child element' = '<setting name="ApiToken" serializeAs="String"><value>hunter2</value></setting>'
        'a Java properties XML entry'                       = '<entry key="db.password">hunter2</entry>'
        'a connection string Password='                     = '<add name="Main" connectionString="Server=s;Database=d;User Id=u;Password=hunter2;" />'
        'a connection string Pwd='                          = '<add name="Main" connectionString="Server=s;Uid=u;Pwd=hunter2" />'
        'a connection string with spaces round ='           = '<add name="Main" connectionString="Server=s; Password = hunter2" />'
        'a connection string key spelled with a char ref'   = '<add name="Main" connectionString="Server=s;Pass&#119;ord=hunter2" />'
        'a key attribute spelled with a char ref'           = '<add key="pass&#119;ord" value="hunter2" />'
        'a key attribute spelled with a hex char ref'       = '<add key="pass&#x77;ord" value="hunter2" />'
    }
    foreach ($k in $ddS2Hit.Keys) { Assert-That ((Test-CredentialText $ddS2Hit[$k]) -and (Get-CredentialHitLine $ddS2Hit[$k]) -gt 0) "DENY-BY-DEFAULT-1 S2: $k refuses" $ddS2Hit[$k] }
    # must-pass: decodable escapes that name nothing secret, paths, and XML pairs whose name is not secret or whose value is on the closed exemption list
    $ddOk = [ordered]@{
        'a JSON key with a decoded accent'          = '{"caf\u00e9": "x"}'
        'a JSON value with a decoded accent'        = '{"name": "caf\u00e9"}'
        'a Windows path value'                      = 'path: C:\Users\me\x64\tools'
        'a JSON Windows path value'                 = '{"path": "C:\\Users\\x"}'
        'a percent-encoded query value'             = 'url: https://x.example/?q=a%20b'
        'a PowerShell string with escapes'          = ('$x = "line' + $ddBt + 'nnext' + $ddBt + 'tend"')
        'an appSettings pair that is not secret'    = '<add key="Theme" value="dark" />'
        'an appSettings secret with an empty value' = '<add key="password" value="" />'
        'a secret name with no other attribute'     = '<add name="password" />'
        'an appSettings secret with an env ref'     = '<add key="ApiKey" value="${API_KEY}" />'
        'a connection string with no password'      = '<add name="Main" connectionString="Server=s;Database=d;Integrated Security=true" />'
        'an appSettings block of plain settings'    = "<appSettings>`n  <add key=`"LogLevel`" value=`"Information`" />`n</appSettings>"
    }
    foreach ($k in $ddOk.Keys) { Assert-That (-not (Test-CredentialText $ddOk[$k])) "DENY-BY-DEFAULT-1 must-pass: $k is not a credential" $ddOk[$k] }
    # r1 adversary pass 1 (A1-A3, A6-A8): each finding's input, plus its siblings
    $ddE = [string][char]0xE9
    $ddA = [ordered]@{
        'A1 a char-ref key with a char-ref quote in the value'         = '<add key="pass&#119;ord" value="hunt&#34;er2" />'
        'A1 a hex char-ref key with a hex char-ref quote in the value' = '<add key="pass&#x77;ord" value="hunt&#x22;er2" />'
        'A1 single-quoted, with a char-ref apostrophe in the value'    = "<add key='pass&#119;ord' value='hunt&#39;er2' />"
        'A2 a second naming attribute carries the secret'              = '<add key="password" name="hunter2" />'
        'A2 name plus id'                                              = '<add name="token" id="hunter2" />'
        'A3 a percent-encoded slash in a query key'                    = 'https://h.example/cb?api%2Fkey=hunter2'
        'A3 a percent-encoded backslash in a query key'                = 'https://h.example/cb?api%5Ckey=hunter2'
        'A3 a percent-encoded slash in an XML key attribute'           = '<add key="api%2Fkey" value="hunter2" />'
        'A3 a percent escape left undecoded in a key'                  = 'pass%20word=hunter2'
        'A6 a non-ASCII attribute name beside the pair'                = ('<add key="password" value="hunter2" caf' + $ddE + '="x" />')
        'A6 a non-ASCII namespace prefix'                              = ('<' + $ddE + ':add xmlns:' + $ddE + '="urn:cfg" key="password" value="hunter2" />')
        'A6 sibling: a non-ASCII prefix on a secret element'           = ('<' + $ddE + ':password>hunter2</' + $ddE + ':password>')
        'A7 a table header with an undecodable escape'                 = "[`"pass\U0011FFFFord`"]`nvalue=`"hunter2`""
        # r1 adversary pass 2: a value is never decoded into an exempt literal (A1), and a DTD entity in a key is undecodable (A3)
        'p2 A1 an escaped key whose value decodes to null'             = '"password": \156ull'
        'p2 A1 sibling: a value written as a char-ref empty string'    = '{"password": "&#34;&#34;"}'
        'p2 A1 sibling: a table body value that decodes to null'       = "[`"password`"]`nx = \156ull"
        'p2 A3 a DTD entity inside an XML key attribute'               = '<!DOCTYPE add [<!ENTITY w "w">]><add key="pass&w;ord" value="hunter2" />'
    }
    foreach ($k in $ddA.Keys) { Assert-That ((Test-CredentialText $ddA[$k]) -and (Get-CredentialHitLine $ddA[$k]) -gt 0) "DENY-BY-DEFAULT-1 r1 adversary $k refuses" $ddA[$k] }
    $ddA8 = "`"pass\U0011FFFFord`": hunter2`n`"pass%77ord`": hunter2"
    Assert-That ((Get-CredentialHitLine $ddA8) -eq 1) 'DENY-BY-DEFAULT-1 r1 adversary A8: the report names the line of the EARLIEST key (a residual key on line 1)' "line=$(Get-CredentialHitLine $ddA8)"
    # r2 (adversary pass 3 A1-A4, one normal form): every finding's input and sibling, plus a generated sweep of an escaped key after ANY predecessor character
    $ddR2 = [ordered]@{
        'p3 A1 an escaped key after a colon inside a name'        = '{"db:password":"hunter2"}'
        'p3 A1 sibling: after an equals sign inside a name'       = '{"db=password":"hunter2"}'
        'p3 A1 sibling: a residual key after a colon'             = '{"db:pass\U0011FFFFord":"hunter2"}'
        'p3 A2 a nested same-name entry in an attribute pair'     = '<entry key="password"><entry></entry>hunter2</entry>'
        'p3 A2 twin: a nested same-name secret element'           = '<password><password></password>hunter2</password>'
        'p3 A2 sibling: an unclosed child as the pair content'    = '<entry key="password"><entry></entry>'
        'p3 A3 a DTD entity in a connection-string key'           = '<!DOCTYPE add [<!ENTITY w "w">]><add name="Main" connectionString="Pass&w;ord=hunter2" />'
        'p3 A4 a non-ASCII DTD entity name in a key attribute'    = ('<!DOCTYPE add [<!ENTITY ' + $ddE + ' "w">]><add key="pass&' + $ddE + ';ord" value="hunter2" />')
        'p3 A4 sibling: a non-ASCII entity in a JSON key'         = ('{"pass&' + $ddE + ';ord": "hunter2"}')
        'p3 nested: an escaped entity that decodes to a char ref' = '{"pass&amp;#119;ord": "hunter2"}'
        'p3 mask: a decoded key whose value decodes to null'      = '{"db:password": \156ull}'
        # r2 pass 4 A1: a decoded character that SHAPES the value (its terminator, a blank the reader skips, a '#' that makes a later line a comment)
        'p4 A1 a decoded comma ends the value'                    = '{"pass\x77ord": \x26#44;hunter2}'
        'p4 A1 sibling: a decoded blank before null'              = '{"pass\x77ord": \x26#32;null}'
        'p4 A1 sibling: a decoded line break ends the value'      = "pass\x77ord: \u000a"
        'p4 A1 sibling: a decoded # makes the value line a comment' = "`"password`":`n\x23 hunter2"
        'p4 A1 sibling: a decoded = in an ini-style value'        = "pass\x77ord=\x26#10;hunter2"
        # r2 pass 5 A1: a decode that DELETES (a decoded backslash joins the next line) still marks the value
        'p5 A1 a decoded backslash joins a null line'             = "`"password`": \`n  null"
        'p5 A1 sibling: the same in a table body'                 = "[`"password`"]`nx = \`n  null"
        'p5 A1 sibling: a raw escaped line break before null'     = "`"password`": \`n  null"
        # r2 pass 6 A1: a reader decodes ONCE; the deeper level ('password suffix') names nothing, the first level ('password suffix') does
        'p6 A1 an escaped backslash before a second escape'       = '{"password\u0020suffix":"hunter2"}'
    }
    # r3 pass 7 A2: an XML naming attribute VALUE is judged at every decode level too (its first level names a secret, the deeper fixed point does not)
    $ddR2['p7 A2 an XML key whose first decode level names a secret'] = '<add key="&#97;uth&#92;u0068eader" value="hunter2" />'
    $ddR2['p7 A2 sibling: the same key in a JSON pair'] = '{"\x61uth\x5cu0068eader": "hunter2"}'
    # r4 (sol r3 A1, defect class: a judge that drops the spelling AS WRITTEN once a decode exists): the raw naming value is a candidate too, beside every decode level
    # ($ddU is backslash + 'u0068', the escaped 'h', built here so no editor or encoder can decode it in the source)
    $ddU = [string][char]92 + 'u0068'
    $ddR2['r4 A1 a raw strong XML naming value that also decodes'] = ('<add key="auth' + $ddU + 'eader" value="hunter2" />')
    $ddR2['r4 A1 sibling: the same with single quotes'] = ("<add key='auth" + $ddU + "eader' value='hunter2' />")
    $ddR2['r4 A1 sibling: a raw strong spaced naming value that decodes'] = ('<add key="a u t h' + $ddU + 'eader" value="hunter2" />')
    # r4 (sol r3 hardening): raw benign, first decode level strong, fixed point benign, so only a judge of the intermediate level refuses it
    $ddR2['r4 hardening: a JSON key strong only at its first decode level'] = '{"&#97;uth&#92;u0068eader": "hunter2"}'
    # r4 adversary pass 9 A3: a quoted '/>' inside a secret-named element's start tag is not a self-closing tag
    $ddR2['r4 pass 9 A3 a quoted /> in a secret element start tag'] = '<password note="/>">hunter2</password>'
    $ddR2['r4 pass 9 A3 sibling: single quotes'] = "<password note='/>'>hunter2</password>"
    # r4 adversary pass 10 A1-A2: the escaped backslash and the one-letter escapes a JSON reader decodes ($ddBs is one backslash, built so no encoder can decode it)
    $ddBs = [string][char]92
    $ddR2['r4 pass 10 A1 an escaped backslash before an undecodable escape in a key'] = ('{"pass' + $ddBs + $ddBs + 'U0011FFFFord":"hunter2"}')
    $ddR2['r4 pass 10 A2 an escaped line break inside an XML naming value in JSON'] = ('{"xml":"<add key=''pass' + $ddBs + 'nword'' value=''hunter2'' />"}')
    $ddR2['r4 pass 10 A2 sibling: an escaped tab'] = ('{"xml":"<add key=''pass' + $ddBs + 'tword'' value=''hunter2'' />"}')
    # r5 (sol r4 A2, defect class: an escape a reader decodes that the normaliser left as written): the YAML whitespace escapes \L \P \v (and the control escapes \a \e) in an XML naming value
    $ddR2['r5 sol r4 A2 a YAML \L (U+2028) in an XML naming value'] = ('xml: "<add key=''pass' + $ddBs + 'Lword'' value=''hunter2'' />"')
    $ddR2['r5 sol r4 A2 sibling: a YAML \P (U+2029) in an element-form pair'] = ('xml: "<entry key=''pass' + $ddBs + 'Pword''>hunter2</entry>"')
    $ddR2['r5 sol r4 A2 sibling: a YAML \v (VT)'] = ('xml: "<add key=''pass' + $ddBs + 'vword'' value=''hunter2'' />"')
    $ddR2['r5 sol r4 A2 sibling: a YAML \e (ESC)'] = ('xml: "<add key=''pass' + $ddBs + 'eword'' value=''hunter2'' />"')
    $ddR2['r5 sol r4 A2 sibling: a YAML \a (BEL)'] = ('xml: "<add key=''pass' + $ddBs + 'aword'' value=''hunter2'' />"')
    $ddR2['r5 sol r4 A2 sibling: a YAML \_ (NBSP)'] = ('xml: "<add key=''pass' + $ddBs + '_word'' value=''hunter2'' />"')
    $ddR2['r5 sol r4 A2 sibling: a YAML \L in a JSON string'] = ('{"xml":"<add key=''pass' + $ddBs + 'Lword'' value=''hunter2'' />"}')
    # r5 adversary pass 12 A1 (defect class: an escape a reader decodes that the normaliser left as written, the PowerShell form): a backtick-escaped quote frames an XML pair inside
    # a PowerShell string, and the special backtick escapes (`n `t `r `a `b `e `f `v `0) are blanks or control characters inside a naming value ($ddBq is one backtick)
    $ddBq = [string][char]0x60
    $ddR2['r5 adv p12 A1 a backtick-escaped double quote frames the XML pair'] = ('$xml = "<add key=' + $ddBq + '"password' + $ddBq + '" value=' + $ddBq + '"hunter2' + $ddBq + '" />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-n inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'nword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-t inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'tword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-r inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'rword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-v inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'vword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-e inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'eword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-a inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'aword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-b inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'bword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-f inside the naming value'] = ('$xml = "<add key=''pass' + $ddBq + 'fword'' value=''hunter2'' />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-escaped quote and a backtick-n together'] = ('$xml = "<add key=' + $ddBq + '"pass' + $ddBq + 'nword' + $ddBq + '" value=' + $ddBq + '"hunter2' + $ddBq + '" />"')
    $ddR2['r5 adv p12 A1 sibling: a backtick-escaped single quote frames the pair'] = ('$xml = "<add key=' + $ddBq + '''password' + $ddBq + ''' value=' + $ddBq + '''hunter2' + $ddBq + ''' />"')
    # r6 (sol r5 A1 + opus r5 B1, defect class: an escape a reader decodes that the normaliser left as written; PowerShell reads a backtick before ANY character as that character,
    # so the punctuation, blank, tab and line-break forms the key rule allows are escapes too): a backtick before '/', a backslash, a blank, a tab, a line break, '>' or any other character
    $ddR2['r6 sol r5 A1 a backtick before a slash inside a key'] = ('$cfg = @{ "pass' + $ddBq + '/word" = ''hunter2'' }')
    $ddR2['r6 sol r5 A1 sibling: a backtick before a backslash inside a key'] = ('$cfg = @{ "pass' + $ddBq + '\word" = ''hunter2'' }')
    $ddR2['r6 sol r5 A1 sibling: a backtick before a slash in api/key'] = ('$cfg = @{ "api' + $ddBq + '/key" = ''hunter2'' }')
    $ddR2['r6 opus r5 B1 sibling: a backtick before a blank in an XML naming value'] = ('$xml = "<add key=''pass' + $ddBq + ' word'' value=''hunter2'' />"')
    $ddR2['r6 opus r5 B1 sibling: a backtick before a tab in an XML naming value'] = ('$xml = "<add key=''pass' + $ddBq + [string][char]9 + 'word'' value=''hunter2'' />"')
    $ddR2['r6 opus r5 B1 sibling: a backtick before a line break in an XML naming value'] = ('$xml = "<add key=''pass' + $ddBq + "`n" + 'word'' value=''hunter2'' />"')
    $ddR2['r6 sol r5 A1 XML sibling: a backtick before the > of the start tag'] = ('$xml = "<add key=''password'' value=''hunter2'' /' + $ddBq + '>"')
    # r6 (sol r5 A2 + opus r5 hardening, defect class: an escape a reader decodes that the normaliser left as written; the doubled quote): PowerShell and YAML read a doubled single
    # quote inside a single-quoted string as one quote, and PowerShell and CSV read a doubled double quote inside a double-quoted string as one
    $ddR2['r6 sol r5 A2 doubled single quotes frame an XML pair'] = ('$xml = ''<add key=''''password'''' value=''''hunter2'''' />''')
    $ddR2['r6 opus r5 hardening: doubled double quotes frame an XML pair'] = ('$xml = "<add key=""password"" value=""hunter2"" />"')
    $ddR2['r6 sol r5 A2 sibling: doubled single quotes around an element-form pair'] = ('$xml = ''<entry key=''''password''''>hunter2</entry>''')
    $ddR2['r6 sol r5 A2 sibling: doubled quotes around a slash-led key'] = ('$xml = ''<add key=''''/password'''' value=''''hunter2'''' />''')
    $ddR2['r6 sol r5 A2 sibling: a doubled quote pair with no enclosing run (adjacent to name characters)'] = ('x ''''password'''' value=''''hunter2'''' <add key=''''password'''' value=''''hunter2'''' />')
    # r6 adversary pass 13 A1 (defect class: decode order): the doubled quote belongs to the OUTER reader, so it collapses on a level of its own BEFORE a character reference inside the string is decoded
    $ddR2['r6 adv p13 A1 a char-ref apostrophe in a doubled-quote PowerShell string'] = ('$xml = ''<add key=''''pass&#119;ord'''' value=''''hunt&#39;er2'''' />''')
    $ddR2['r6 adv p13 A1 sibling: a char-ref quote in a doubled-double-quote string'] = ('$xml = "<add key=""pass&#119;ord"" value=""hunt&#34;er2"" />"')
    $ddR2['r6 adv p13 A1 sibling: a hex char-ref apostrophe in a YAML single-quoted string'] = ('xml: ''<add key=''''pass&#x77;ord'''' value=''''hunt&#x27;er2'''' />''')
    # r6 adversary pass 13 A2 (defect class: a residual escape whose framing a decode destroys on the SAME level): the residual key / table header stays visible whatever a same-level decode makes of its framing
    $ddBsA2 = [string][char]92
    foreach ($ddD in @('0020', '0009', '000A', '0022', '0027', '003A', '003D', '002C', '005D')) {
        foreach ($ddX in @(($ddBsA2 + $ddBsA2), $ddBsA2)) {
            $ddR2["r6 adv p13 A2 key: residual after '$ddX', then \u$ddD"] = ('{"ab' + $ddX + 'U0011FFFF' + $ddBsA2 + 'u' + $ddD + 'cd":"hunter2"}')
            $ddR2["r6 adv p13 A2 header: residual after '$ddX', then \u$ddD"] = ('["ab' + $ddX + 'U0011FFFF' + $ddBsA2 + 'u' + $ddD + 'cd"]' + "`n" + 'x="hunter2"')
        }
    }
    foreach ($k in $ddR2.Keys) { Assert-That ((Test-CredentialText $ddR2[$k]) -and (Get-CredentialHitLine $ddR2[$k]) -gt 0) "DENY-BY-DEFAULT-1 r2 adversary $k refuses" $ddR2[$k] }
    # r6 must-pass: the decode of a backtick before any character and of a doubled quote never turns an empty literal, a plain value or a Markdown code span into a credential
    foreach ($ddOkR6 in @('<add key="password" value="" />', '<add key="password" value=""/>', "<add key='password' value=''/>", "password: ''", 'password: ""', '{"a":"","b":"x"}', '$msg = ''it''''s a plain note''', '$msg = "say ""hi"" to the team"', ('a ' + $ddBq + '/ path note: plain'), ('the ' + $ddBq + 'theme' + $ddBq + ' setting'))) {
        Assert-That (-not (Test-CredentialText $ddOkR6)) "DENY-BY-DEFAULT-1 r6 must-pass: $ddOkR6 is not a credential" $ddOkR6
    }
    Assert-That (-not (Test-CredentialText ('{"caf' + [string][char]92 + 'u00e9": "x", "theme": "dark"}'))) 'DENY-BY-DEFAULT-1 r6 must-pass: a decodable accent escape in a key is not a credential (the residual view does not refuse it)'
    $ddR2Miss = [System.Collections.Generic.List[string]]::new(); $ddR2N = 0
    foreach ($pre in @('db:', 'db=', 'a.b:', 'x/', 'x?', 'x|', 'x#', 'x@')) {
        foreach ($esc in @('w', '\x77', '&#119;', '&#x77;', '\167', '%77', ('`u{77}'), '&amp;#119;', '\U0011FFFF', '&w;', ('&' + $ddE + ';'))) {
            foreach ($wrap in @('json', 'xml')) {
                $t = $(if ($wrap -eq 'json') { '{"' + $pre + 'pass' + $esc + 'ord":"hunter2"}' } else { '<add key="' + $pre + 'pass' + $esc + 'ord" value="hunter2" />' })
                $ddR2N++
                if (-not (Test-CredentialText $t)) { $ddR2Miss.Add($t) }
            }
        }
    }
    Assert-That ($ddR2Miss.Count -eq 0 -and $ddR2N -ge 170) "DENY-BY-DEFAULT-1 r2 generated sweep: all $ddR2N escaped keys after a predecessor character refuse" ("misses=$($ddR2Miss.Count): " + (($ddR2Miss | Select-Object -First 8) -join '; '))
    $ddA6 = "machine h login u password hunter2`n{`"password`":`"hunter3`"}"
    Assert-That ((Get-CredentialHitLine $ddA6) -eq 1) 'DENY-BY-DEFAULT-1 r2 advisory A6: the report names the netrc line (line 1), not the later key' "line=$(Get-CredentialHitLine $ddA6)"
    foreach ($ok in @('{"note": "a &amp; b"}', '<add key="Theme" value="a &amp; b" />', '{"name": "caf&#233;"}')) { Assert-That (-not (Test-CredentialText $ok)) "DENY-BY-DEFAULT-1 r2 must-pass: $ok is not a credential" $ok }
    if (Test-Has 'Test-CredentialNamePresent' 'DENY-BY-DEFAULT-1 r1 adversary role guard') {
        Assert-That (Test-CredentialNamePresent '{"api%2Fkey": ""}') 'DENY-BY-DEFAULT-1 r1 adversary A3 role guard: a percent-encoded slash spelling names a secret'
        Assert-That (Test-CredentialNamePresent "[`"pass\U0011FFFFord`"]`nvalue=`"`"") 'DENY-BY-DEFAULT-1 r1 adversary A7 role guard: a table header with an undecodable escape names a secret'
        # r2 pass 4 A4: a residual XML naming attribute names a secret whatever its value (an empty one included), also inside a config.toml string
        Assert-That (Test-CredentialNamePresent "<add key='pass&w;ord' value='' />") 'DENY-BY-DEFAULT-1 r2 adversary p4 A4 role guard: a residual XML naming attribute with an empty value names a secret'
        Assert-That (Test-CredentialNamePresent "xml = `"<add key='pass&w;ord' value='' />`"") 'DENY-BY-DEFAULT-1 r2 adversary p4 A4 role guard: the same inside a config.toml string'
        Assert-That (Test-CredentialNamePresent "xml = `"<add key='p a s s w o r d' value='' />`"") 'DENY-BY-DEFAULT-1 r2 adversary p6 A3 role guard: a spaced XML naming attribute with no escape names a secret'
        # r3 pass 7 A2: the role guard judges every decode level, not only the fixed point (hosts.yml '"\x61uth\x5cu0068eader": ""' is one decode away from a strong 'auth' name, two from the plain word 'authheader')
        Assert-That (Test-CredentialNamePresent '"\x61uth\x5cu0068eader": ""') 'DENY-BY-DEFAULT-1 r3 adversary p7 A2 role guard: a key whose first decode level names a secret (empty value)'
        Assert-That (Test-CredentialNamePresent '<add key="&#97;uth&#92;u0068eader" value="" />') 'DENY-BY-DEFAULT-1 r3 adversary p7 A2 role guard: the same as an XML naming attribute with an empty value'
        # r4 (sol r3 A1): the raw spelling of a naming value that also decodes still names a secret (spaced, so the word scan alone misses it)
        $ddU2 = [string][char]92 + 'u0068'
        Assert-That (Test-CredentialNamePresent ('<add key="a u t h' + $ddU2 + 'eader" value="" />')) 'DENY-BY-DEFAULT-1 r4 A1 role guard: a raw strong spaced XML naming value that decodes names a secret'
        Assert-That (Test-CredentialNamePresent ("xml = `"<add key='a u t h" + $ddU2 + "eader' value='' />`"")) 'DENY-BY-DEFAULT-1 r4 A1 role guard: the same inside a config.toml string'
        Assert-That (-not (Test-CredentialNamePresent ('<add key="t h' + $ddU2 + 'eme" value="" />'))) 'DENY-BY-DEFAULT-1 r4 A1 role guard must-pass: a raw benign naming value that decodes names nothing'
        # r4 adversary pass 9 A1: an XML naming attribute that exists only once the text is decoded (config.toml with an escaped '<' and '>') names a secret too
        $ddLt = [string][char]92 + 'u003C'; $ddGt = [string][char]92 + 'u003E'
        Assert-That (Test-CredentialNamePresent ("xml = `"" + $ddLt + "add key='p a s s w o r d' value='' /" + $ddGt + "`"")) 'DENY-BY-DEFAULT-1 r4 pass 9 A1 role guard: a spaced XML naming value that appears only after decoding names a secret'
        $ddBs2 = [string][char]92
        Assert-That (Test-CredentialNamePresent ('{"pass' + $ddBs2 + $ddBs2 + 'U0011FFFFord":""}')) 'DENY-BY-DEFAULT-1 r4 pass 10 A1 role guard: an escaped backslash before an undecodable escape names a secret'
        Assert-That (Test-CredentialNamePresent ('{"xml":"<add key=''pass' + $ddBs2 + 'nword'' value='''' />"}')) 'DENY-BY-DEFAULT-1 r4 pass 10 A2 role guard: an escaped line break inside an XML naming value names a secret'
        # r5 (sol r4 A1, defect class: a check that runs on the fixed point only): an intermediate residual key or table header that a deeper decode destroys keeps the role verdict true.
        # The generated sweep puts every framing-destroying decode (a blank, a tab, a line break, a double and a single quote, a colon, an equals sign) after the residual.
        $ddA1Miss = [System.Collections.Generic.List[string]]::new(); $ddA1N = 0
        foreach ($ddD in @('0020', '0009', '000A', '0022', '0027', '003A', '003D', '002C', '005D')) {
            $ddA1Keys = @(('{"ab' + $ddBs2 + $ddBs2 + 'U0011FFFF' + $ddBs2 + $ddBs2 + 'u' + $ddD + 'cd":""}'), ('["ab' + $ddBs2 + $ddBs2 + 'U0011FFFF' + $ddBs2 + $ddBs2 + 'u' + $ddD + 'cd"]' + "`n" + 'x=""'), ('<add key=''ab' + $ddBs2 + $ddBs2 + 'U0011FFFF' + $ddBs2 + $ddBs2 + 'u' + $ddD + 'cd'' value='''' />'))
            foreach ($ddT in $ddA1Keys) { $ddA1N++; if (-not (Test-CredentialNamePresent $ddT)) { $ddA1Miss.Add($ddT) } }
        }
        Assert-That ($ddA1Miss.Count -eq 0 -and $ddA1N -eq 27) "DENY-BY-DEFAULT-1 r5 sol r4 A1 role guard: all $ddA1N intermediate residual keys and headers keep a true verdict" ("misses=$($ddA1Miss.Count): " + (($ddA1Miss | Select-Object -First 4) -join '; '))
        # r5 (sol r4 A2): the YAML whitespace and control escapes in an XML naming value name a secret whatever the value (an empty one included)
        $ddA2Miss = [System.Collections.Generic.List[string]]::new(); $ddA2N = 0
        foreach ($ddL in @('L', 'P', 'v', 'e', 'a', '_', ' ')) {
            foreach ($ddT in @(('xml: "<add key=''pass' + $ddBs2 + $ddL + 'word'' value='''' />"'), ('xml: "<entry key=''pass' + $ddBs2 + $ddL + 'word''></entry>"'), ('{"xml":"<add name=''pass' + $ddBs2 + $ddL + 'word'' value='''' />"}'))) {
                $ddA2N++; if (-not (Test-CredentialNamePresent $ddT)) { $ddA2Miss.Add($ddT) }
            }
        }
        Assert-That ($ddA2Miss.Count -eq 0 -and $ddA2N -eq 21) "DENY-BY-DEFAULT-1 r5 sol r4 A2 role guard: all $ddA2N YAML-escape XML naming values name a secret" ("misses=$($ddA2Miss.Count): " + (($ddA2Miss | Select-Object -First 4) -join '; '))
        # r5 adversary pass 12 A1: the PowerShell backtick forms in an XML naming value or around its quotes name a secret whatever the value
        $ddBq2 = [string][char]0x60
        $ddA3Miss = [System.Collections.Generic.List[string]]::new(); $ddA3N = 0
        foreach ($ddL in @('n', 't', 'r', 'v', 'e', 'a', 'b', 'f')) {
            $ddA3N++; $ddT = ('$xml = "<add key=''pass' + $ddBq2 + $ddL + 'word'' value='''' />"'); if (-not (Test-CredentialNamePresent $ddT)) { $ddA3Miss.Add($ddT) }
        }
        $ddA3N++; $ddT = ('$xml = "<add key=' + $ddBq2 + '"password' + $ddBq2 + '" value=' + $ddBq2 + '"' + $ddBq2 + '" />"'); if (-not (Test-CredentialNamePresent $ddT)) { $ddA3Miss.Add($ddT) }
        Assert-That ($ddA3Miss.Count -eq 0 -and $ddA3N -eq 9) "DENY-BY-DEFAULT-1 r5 adv p12 A1 role guard: all $ddA3N PowerShell backtick forms in an XML naming value name a secret" ("misses=$($ddA3Miss.Count): " + (($ddA3Miss | Select-Object -First 4) -join '; '))
        # r6 (sol r5 A1-A2, opus r5 B1): the backtick before punctuation, a blank, a tab or a line break, and the doubled quote, name a secret in the role guard too (an empty value included)
        $ddA4Miss = [System.Collections.Generic.List[string]]::new(); $ddA4N = 0
        # a blank, tab or line break is dropped from an XML naming value only, so those three are checked in the XML form; '/' and '\' separate a key's parts in every form
        foreach ($ddCh in @('/', '\', ' ', [string][char]9, "`n")) {
            $ddForms = @(('$xml = "<add key=''pass' + $ddBq2 + $ddCh + 'word'' value='''' />"'))
            if ($ddCh -eq '/' -or $ddCh -eq '\') { $ddForms += ('$cfg = @{ "pass' + $ddBq2 + $ddCh + 'word" = '''' }') }
            foreach ($ddT in $ddForms) { $ddA4N++; if (-not (Test-CredentialNamePresent $ddT)) { $ddA4Miss.Add($ddT) } }
        }
        foreach ($ddT in @('$xml = ''<add key=''''password'''' value='''''''' />''', '$xml = "<add key=""password"" value="""" />"', '$xml = ''<entry key=''''password''''></entry>''')) {
            $ddA4N++; if (-not (Test-CredentialNamePresent $ddT)) { $ddA4Miss.Add($ddT) }
        }
        Assert-That ($ddA4Miss.Count -eq 0 -and $ddA4N -eq 10) "DENY-BY-DEFAULT-1 r6 sol r5 A1-A2 role guard: all $ddA4N backtick-punctuation and doubled-quote forms name a secret" ("misses=$($ddA4Miss.Count): " + (($ddA4Miss | Select-Object -First 6) -join '; '))
        Assert-That (-not (Test-CredentialNamePresent '{"theme": "dark", "note": "say ""hi"" and it''''s fine"}')) 'DENY-BY-DEFAULT-1 r6 role guard must-pass: doubled quotes in a plain note name no secret'
        # r6 adversary pass 13 A1-A2: a char-ref in a doubled-quote string, and a residual escape whose framing a same-level decode destroys, name a secret in the role guard too
        $ddA5Miss = [System.Collections.Generic.List[string]]::new(); $ddA5N = 0
        $ddBsR = [string][char]92
        foreach ($ddT in @(('xml: ''<add key=''''p a s s&#119;o r d'''' value=''''hunt&#39;er2'''' />'''), ('$xml = "<add key=""pass&#119;ord"" value="""" />"'))) { $ddA5N++; if (-not (Test-CredentialNamePresent $ddT)) { $ddA5Miss.Add($ddT) } }
        foreach ($ddD in @('0020', '0009', '000A', '0022', '0027', '003A', '003D', '002C', '005D')) {
            foreach ($ddX in @(($ddBsR + $ddBsR), $ddBsR)) {
                foreach ($ddT in @(('{"ab' + $ddX + 'U0011FFFF' + $ddBsR + 'u' + $ddD + 'cd":""}'), ('["ab' + $ddX + 'U0011FFFF' + $ddBsR + 'u' + $ddD + 'cd"]' + "`n" + 'x=""'))) { $ddA5N++; if (-not (Test-CredentialNamePresent $ddT)) { $ddA5Miss.Add($ddT) } }
            }
        }
        Assert-That ($ddA5Miss.Count -eq 0 -and $ddA5N -eq 38) "DENY-BY-DEFAULT-1 r6 adv p13 A1-A2 role guard: all $ddA5N forms name a secret" ("misses=$($ddA5Miss.Count): " + (($ddA5Miss | Select-Object -First 4) -join '; '))
        Assert-That (-not (Test-CredentialNamePresent ('{"caf' + $ddBsR + 'u00e9": "x", "theme": "dark"}'))) 'DENY-BY-DEFAULT-1 r6 role guard must-pass: a decodable accent escape in a key names no secret (the residual view does not refuse it)'
        # must-pass: the new escapes do not make a plain Windows path in a settings file name a secret
        Assert-That (-not (Test-CredentialNamePresent ('{"path": "C:' + $ddBs2 + $ddBs2 + 'Program Files' + $ddBs2 + $ddBs2 + 'Local' + $ddBs2 + $ddBs2 + 'Logs", "theme": "dark"}'))) 'DENY-BY-DEFAULT-1 r5 role guard must-pass: a JSON Windows path with \P \L \a \e \v \_ sequences names no secret'
        Assert-That (-not (Test-CredentialText ('{"path": "C:' + $ddBs2 + $ddBs2 + 'Program Files' + $ddBs2 + $ddBs2 + 'Local' + $ddBs2 + $ddBs2 + 'Logs", "theme": "dark"}'))) 'DENY-BY-DEFAULT-1 r5 must-pass: a JSON Windows path with \P \L sequences is not a credential'
    }
    # sibling: the Import credential-ROLE guard (Test-CredentialNamePresent) reads a key name with the same decoder and the same deny-by-default residual rule
    if (Test-Has 'Test-CredentialNamePresent' 'DENY-BY-DEFAULT-1 role guard') {
        $ddRole = [ordered]@{
            'a PowerShell backtick spelling'   = ('{"pass' + $ddBt + 'word": ""}')
            'an XML character reference'       = '<add key="pass&#119;ord" value="" />'
            'an octal spelling'                = '{"pass\167ord": ""}'
            'an undecodable \U escape'         = '{"pass\U0011FFFFord": ""}'
        }
        foreach ($k in $ddRole.Keys) { Assert-That (Test-CredentialNamePresent $ddRole[$k]) "DENY-BY-DEFAULT-1 role guard: $k names a secret" $ddRole[$k] }
        Assert-That (-not (Test-CredentialNamePresent '{"theme": "dark", "path": "C:\\Users\\x", "name": "caf\u00e9"}')) 'DENY-BY-DEFAULT-1 role guard must-pass: plain keys, a JSON path and an accent name no secret'
    }
    # every guard site calls the one detector: the export stage scan and the archive re-check refuse an S1 and an S2 file
    foreach ($ddG in @(@('s1', '{"pass\u0077ord": "hunter2"}'), @('s1x', '<add key="pass&#119;ord" value="hunter2" />'), @('s2', '<add key="password" value="hunter2" />'))) {
        $st = New-Scratch "dd-$($ddG[0])-stage"; [IO.File]::WriteAllText((Join-Path $st 'app.config'), $ddG[1])
        Assert-That (@(Get-StagedCredentialHits -Stage $st).Count -eq 1) "DENY-BY-DEFAULT-1 $($ddG[0]) export: the staged file is a hit" $ddG[1]
        $zp = Join-Path (New-Scratch "dd-$($ddG[0])-zip") 'b.zip'
        $zf = [IO.Compression.ZipFile]::Open($zp, 'Create')
        try { $e = $zf.CreateEntry('home-claude/machine/app.config'); $sw = [IO.StreamWriter]::new($e.Open()); $sw.Write($ddG[1]); $sw.Dispose() } finally { $zf.Dispose() }
        Assert-That (@(Get-ArchiveCredentialHits -Zip $zp).Count -eq 1) "DENY-BY-DEFAULT-1 $($ddG[0]) archive re-check: the zip entry is a hit" $ddG[1]
    }
    # r4 (sol r3 A3, defect class: unbounded input): nested entities decode one layer per pass. Past $script:CredentialMaxDecodeStages levels the text is REFUSED (typed
    # CREDENTIAL_DECODE_LIMIT, deny by default) by every guard, in bounded time and memory, and no level is retained as a full copy.
    $ddDeep = '{"note":"&' + ('amp;' * 40) + '"}'
    Assert-That (Test-CredentialText $ddDeep) 'DENY-BY-DEFAULT-1 r4 A3: a text that needs more decode levels than the bound refuses (deny by default)' $ddDeep
    Assert-That (Test-CredentialNamePresent $ddDeep) 'DENY-BY-DEFAULT-1 r4 A3 role guard: the same text counts as naming a secret (deny by default)'
    Assert-That (Test-CredentialNamePresent ('<add key="&' + ('amp;' * 40) + '" value="" />')) 'DENY-BY-DEFAULT-1 r4 A3 role guard: an XML naming value past the bound counts as naming a secret'
    Assert-That (-not (Test-CredentialText ('{"note":"&' + ('amp;' * 6) + '"}'))) 'DENY-BY-DEFAULT-1 r4 A3 must-pass: a shallow nesting within the bound is not a credential'
    if (Test-Has 'New-CredentialDecodeLimitError' 'DENY-BY-DEFAULT-1 r4 A3') {
        $ddDeepSt = New-Scratch 'dd-r4-a3-stage'; [IO.File]::WriteAllText((Join-Path $ddDeepSt 'note.json'), $ddDeep)
        $ddDeepWhy = (Get-StagedFileVerdictDetail -File ([IO.FileInfo]::new((Join-Path $ddDeepSt 'note.json')))).Why
        Assert-That ("$ddDeepWhy".Contains('CREDENTIAL_DECODE_LIMIT')) 'DENY-BY-DEFAULT-1 r4 A3 export: the staged verdict names the typed decode-limit refusal' "why=$ddDeepWhy"
        # the reviewer's input shape, a 4 MB text below the scan cap: it must finish (it used to need terabytes) and refuse
        $ddHuge = '{"note":"&' + ('amp;' * 1000000) + '"}'
        $ddSw = [Diagnostics.Stopwatch]::StartNew(); $ddHugeHit = Test-CredentialText $ddHuge; $ddSw.Stop()
        Assert-That ($ddHugeHit -and $ddSw.Elapsed.TotalSeconds -lt 120) 'DENY-BY-DEFAULT-1 r4 A3: a 4 MB nesting below the scan cap refuses in bounded time' "hit=$ddHugeHit seconds=$([int]$ddSw.Elapsed.TotalSeconds)"
    }
}
# S3: a Clone whose url carries a query token: neither the console nor the setup log ever holds the token, also where git echoes the url without its scheme
if ($fnLoaded -and (Test-Has 'Invoke-PhaseClone' 'DENY-BY-DEFAULT-1 S3')) {
    $ddCases = @(
        @{ N = 1; Url = ('file:///' + ((New-Scratch 'dd-s3-absent') -replace '\\', '/') + '/absent.git?token=fake-token-0001'); Tok = 'fake-token-0001' },
        @{ N = 2; Url = 'https://127.0.0.1:1/o/r.git?token=fake-token-0002'; Tok = 'fake-token-0002' },
        @{ N = 3; Url = 'https://127.0.0.1:1/o/r.git#token=fake-token-0003'; Tok = 'fake-token-0003' },
        # r1 adversary A4: git percent-decodes the url it echoes; A9: a short query is redacted too
        @{ N = 4; Url = ('file:///' + ((New-Scratch 'dd-s3-absent4') -replace '\\', '/') + '/absent.git?token=fake%2Dtoken%2D0004'); Tok = @('fake-token-0004', 'fake%2Dtoken%2D0004') },
        @{ N = 5; Url = ('file:///' + ((New-Scratch 'dd-s3-absent5') -replace '\\', '/') + '/absent.git?t=x'); Tok = @('?t=x') },
        # r1 adversary pass 2 A2: an encoded line break or a non-UTF-8 byte inside the token
        @{ N = 6; Url = ('file:///' + ((New-Scratch 'dd-s3-absent6') -replace '\\', '/') + '/absent.git?token=fake-token-0006%0Atail'); Tok = @('fake-token-0006') },
        @{ N = 7; Url = ('file:///' + ((New-Scratch 'dd-s3-absent7') -replace '\\', '/') + '/absent.git?token=fake-token-0007%FF'); Tok = @('fake-token-0007') },
        # r2 (adversary pass 3 A5): a SHORT query split at a decoded line break keeps its delimiter-bearing first piece
        @{ N = 8; Url = ('file:///' + ((New-Scratch 'dd-s3-absent8') -replace '\\', '/') + '/absent.git?t=x%0Ay'); Tok = @('?t=x') },
        # r2 pass 4 A2: the short piece AFTER the decoded line break (git prints it at the start of its own line); A3: an embedded url in the query
        @{ N = 9; Url = ('file:///' + ((New-Scratch 'dd-s3-absent9') -replace '\\', '/') + '/absent.git?t=%0Aqzv'); Tok = @('qzv') },
        @{ N = 10; Url = ('file:///' + ((New-Scratch 'dd-s3-absent10') -replace '\\', '/') + '/absent.git?token=fake-token-0010&redirect=https://h.example/x?q=1'); Tok = @('fake-token-0010') },
        # r2 pass 5 A2: a non-UTF-8 byte right after the delimiter ('?' and '#'); A3: the secret inside an embedded url's path
        @{ N = 11; Url = ('file:///' + ((New-Scratch 'dd-s3-absent11') -replace '\\', '/') + '/absent.git?%80qzv'); Tok = @('qzv') },
        @{ N = 12; Url = ('file:///' + ((New-Scratch 'dd-s3-absent12') -replace '\\', '/') + '/absent.git#%80qzw'); Tok = @('qzw') },
        @{ N = 13; Url = ('file:///' + ((New-Scratch 'dd-s3-absent13') -replace '\\', '/') + '/absent.git?token=https://h.example/SECRET0013?x=y'); Tok = @('SECRET0013') },
        # r2 pass 6 A2: a '%XX' in the PATH too (git prints 'absent80'), in the query and the fragment form
        @{ N = 14; Url = ('file:///' + ((New-Scratch 'dd-s3-absent14') -replace '\\', '/') + '/absent%80-14.git?%80qzx'); Tok = @('qzx') },
        @{ N = 15; Url = ('file:///' + ((New-Scratch 'dd-s3-absent15') -replace '\\', '/') + '/absent%80-15.git#%80qzy'); Tok = @('qzy') },
        # r3 pass 7 A1: git DECODES the path it echoes: a blank, a quote or a line break where the '%XX' stood (a decoded line break starts the rest on its own line)
        @{ N = 16; Url = ('file:///' + ((New-Scratch 'dd-s3-absent16') -replace '\\', '/') + '/absent%20x-16.git?%80qzr'); Tok = @('qzr') },
        @{ N = 17; Url = ('file:///' + ((New-Scratch 'dd-s3-absent17') -replace '\\', '/') + '/absent%27x-17.git?%80qzs'); Tok = @('qzs') },
        @{ N = 18; Url = ('file:///' + ((New-Scratch 'dd-s3-absent18') -replace '\\', '/') + '/absent%22x-18.git#%80qzt'); Tok = @('qzt') },
        @{ N = 19; Url = ('file:///' + ((New-Scratch 'dd-s3-absent19') -replace '\\', '/') + '/absent%0Ax-19.git?%80qzu'); Tok = @('qzu') },
        @{ N = 20; Url = ('file:///' + ((New-Scratch 'dd-s3-absent20') -replace '\\', '/') + '/absent%0D%0Ax-20.git?qzk'); Tok = @('qzk') },
        @{ N = 21; Url = ('file:///' + ((New-Scratch 'dd-s3-absent21') -replace '\\', '/') + '/absent-21%0A?%80qzj'); Tok = @('qzj') },
        # r4 (sol r3 A2, defect class: matching a secret by a spelling git may print differently): a line break then a high byte in the query or fragment; git prints a
        # continuation line '80qzh' that no registered spelling matches. The output of a native call whose url secret holds such a byte is withheld (fail closed).
        @{ N = 22; Url = ('file:///' + ((New-Scratch 'dd-s3-absent22') -replace '\\', '/') + '/absent-22.git?t=%0A%80qzh'); Tok = @('qzh') },
        @{ N = 23; Url = ('file:///' + ((New-Scratch 'dd-s3-absent23') -replace '\\', '/') + '/absent-23.git#t=%0A%80qzg'); Tok = @('qzg') },
        @{ N = 24; Url = ('file:///' + ((New-Scratch 'dd-s3-absent24') -replace '\\', '/') + '/absent-24.git?t=%0D%9Fqzf'); Tok = @('qzf') }
    )
    foreach ($ddc in $ddCases) {
        $ddws = New-Scratch "dd-s3-clone-$($ddc.N)"
        $ddrepo = Join-Path $ddws 'MLV-App'
        $dddoc = Join-Path $ddws 'doctrine'; [void][IO.Directory]::CreateDirectory($dddoc); & git -C $dddoc init -q 2>$null; & git -C $dddoc remote add origin $DoctrineUrl 2>$null
        $savedRepo = $script:Repo; $savedDoc = $script:DoctrineRepo; $savedLog = $script:LogFile; $savedFork = $ForkUrl
        $script:Repo = $ddrepo; $Repo = $ddrepo; $script:DoctrineRepo = $dddoc; $DoctrineRepo = $dddoc; $script:LogFile = Join-Path $ddws 'clone.log'
        $script:ForkUrl = $ddc.Url; $ForkUrl = $ddc.Url
        $script:Failures.Clear()
        $ddout = ''
        # file:// is allowed so git really opens case 1 and echoes its url without the scheme; https is not (no network)
        $savedProto = [Environment]::GetEnvironmentVariable('GIT_ALLOW_PROTOCOL'); [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', 'file')
        try { $ddout = (Invoke-PhaseClone *>&1 | Out-String) } catch { $ddout += $_.Exception.Message } finally { [Environment]::SetEnvironmentVariable('GIT_ALLOW_PROTOCOL', $savedProto) }
        $ddlog = $(if (Test-Path -LiteralPath $script:LogFile) { [IO.File]::ReadAllText($script:LogFile) } else { '' })
        $script:Repo = $savedRepo; $Repo = $savedRepo; $script:DoctrineRepo = $savedDoc; $DoctrineRepo = $savedDoc; $script:LogFile = $savedLog; $script:ForkUrl = $savedFork; $ForkUrl = $savedFork
        $ddLeakLog = @(@($ddc.Tok) | Where-Object { $ddlog.Contains($_) }); $ddLeakOut = @(@($ddc.Tok) | Where-Object { $ddout.Contains($_) })
        Assert-That ($ddlog.Length -gt 0 -and $ddLeakLog.Count -eq 0) "DENY-BY-DEFAULT-1 S3 clone ($($ddc.N)): the setup log never holds the url's query token" $ddlog
        Assert-That ($ddLeakOut.Count -eq 0) "DENY-BY-DEFAULT-1 S3 clone ($($ddc.N)): the console never shows the url's query token" $ddout
        $script:Failures.Clear()
    }
    # r4 (sol r3 A2): the fail-closed predicate shared by Invoke-Native and the lease read (Get-HubLease)
    if (Test-Has 'Test-UrlSecretUnprintable' 'DENY-BY-DEFAULT-1 r4 A2') {
        foreach ($ddW in @('file:///C:/x.git?t=%0A%80qzv', 'file:///C:/x.git#t=%0A%80qzv', 'https://h.example/r.git?t=%FF', 'https://u:p%0Aw@h.example/r.git', 'file:///C:/a%0Ab.git?t=x')) {
            Assert-That (Test-UrlSecretUnprintable $ddW) "DENY-BY-DEFAULT-1 r4 A2: output is withheld for $ddW"
        }
        foreach ($ddK in @('https://github.com/o/r.git', 'file:///C:/a%0Ab.git', 'https://h.example/r.git?t=plain-token', 'git@github.com:o/r.git')) {
            Assert-That (-not (Test-UrlSecretUnprintable $ddK)) "DENY-BY-DEFAULT-1 r4 A2 must-pass: output is shown for $ddK"
        }
    }
    # r4 adversary pass 9 A2: an anchor built from a path with many '%XX' is an ambiguous pattern; the sink finishes in bounded time and never prints the query
    if (-not (Test-Path -LiteralPath 'variable:script:UrlRedactTimeout')) { Assert-That $false 'DENY-BY-DEFAULT-1 r4 pass 9 A2: the log sink has no match bound ($script:UrlRedactTimeout missing; not run, it would not finish)' }
    else {
        $ddP9 = Join-Path (New-Scratch 'dd-p9a2-sink') 'sink.log'
        $savedLog = $script:LogFile; $script:LogFile = $ddP9
        $ddSw9 = [Diagnostics.Stopwatch]::StartNew()
        try { Register-UrlSecretParts ('file:///C:/r' + ('a%61' * 128) + '.git?xq9'); Write-Log ("fatal: '/C:/r" + ('aa' * 128) + ".git?xq9' does not appear to be a git repository") *>&1 | Out-Null } finally { $script:LogFile = $savedLog; $ddSw9.Stop() }
        $ddP9Text = [IO.File]::ReadAllText($ddP9)
        Assert-That ($ddSw9.Elapsed.TotalSeconds -lt 60 -and -not $ddP9Text.Contains('?xq9')) 'DENY-BY-DEFAULT-1 r4 pass 9 A2: an ambiguous anchor finishes in bounded time and the query never reaches the log' "seconds=$([int]$ddSw9.Elapsed.TotalSeconds) $ddP9Text"
    }
    # r1 adversary A5: a registered part that is also a scheme ('?https:') never breaks a url apart before the scheme rule redacts its short password
    if (Test-Has 'Register-UrlSecretParts' 'DENY-BY-DEFAULT-1 r1 adversary A5') {
        $ddA5 = Join-Path (New-Scratch 'dd-a5-sink') 'sink.log'
        $savedLog = $script:LogFile; $script:LogFile = $ddA5
        try { Register-UrlSecretParts 'https://u:abc@h.example/r.git?https:'; Write-Log 'x https://u:abc@h.example/r.git?https: y' *>&1 | Out-Null } finally { $script:LogFile = $savedLog }
        $ddA5Text = [IO.File]::ReadAllText($ddA5)
        Assert-That (-not $ddA5Text.Contains(':abc@') -and -not $ddA5Text.Contains('abc@h')) 'DENY-BY-DEFAULT-1 r1 adversary A5: the short password of a url whose query is a registered scheme is still redacted' $ddA5Text
    }
}

$gb = Invoke-Setup (New-Scratch 'home-bogus') @('-Phase', 'Bogus', '-StateRoot', (New-Scratch 'state-bogus'), '-Workspace', (New-Scratch 'ws-bogus'))
Assert-That ($gb.Exit -eq 64 -and $gb.Out -match "Unknown phase 'Bogus'") 'phases: an unknown phase exits 64 (it used to exit 1: Write-Error under ErrorActionPreference Stop threw before the exit)' "exit=$($gb.Exit)`n$($gb.Out)"

Write-Host ("RESULT fails={0}" -f $script:Fails)
if ($script:Fails -gt 0) { exit 1 }
exit 0
