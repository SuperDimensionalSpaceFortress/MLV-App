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
    foreach ($k in $p5a1ok.Keys) { Assert-That (-not (Test-CredentialText $p5a1ok[$k])) "p5 A1 Test-CredentialText: $k is not a credential" $p5a1ok[$k] }
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
    foreach ($k in $r10a1ok.Keys) { Assert-That (-not (Test-CredentialText $r10a1ok[$k])) "r10 A1 Test-CredentialText: $k is not a credential" $r10a1ok[$k] }
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
    foreach ($k in $r11TomlOk.Keys) { Assert-That (-not (Test-CredentialText $r11TomlOk[$k])) "r11 $k is not a credential" $r11TomlOk[$k] }
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
    foreach ($k in $r12Ok.Keys) { Assert-That (-not (Test-CredentialText $r12Ok[$k])) "r12 $k is not a credential" $r12Ok[$k] }
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
    foreach ($k in $r12bOk.Keys) { Assert-That (-not (Test-CredentialText $r12bOk[$k])) "r12 $k is not a credential" $r12bOk[$k] }
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

$gb = Invoke-Setup (New-Scratch 'home-bogus') @('-Phase', 'Bogus', '-StateRoot', (New-Scratch 'state-bogus'), '-Workspace', (New-Scratch 'ws-bogus'))
Assert-That ($gb.Exit -eq 64 -and $gb.Out -match "Unknown phase 'Bogus'") 'phases: an unknown phase exits 64 (it used to exit 1: Write-Error under ErrorActionPreference Stop threw before the exit)' "exit=$($gb.Exit)`n$($gb.Out)"

Write-Host ("RESULT fails={0}" -f $script:Fails)
if ($script:Fails -gt 0) { exit 1 }
exit 0
