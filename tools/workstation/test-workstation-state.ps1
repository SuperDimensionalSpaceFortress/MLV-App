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
function Invoke-Setup([string]$Home_, [string[]]$SetupArgs) {
    $saved = $env:USERPROFILE
    try {
        $env:USERPROFILE = $Home_
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
    Assert-That ((Get-Item -LiteralPath $atBoundary -ErrorAction SilentlyContinue) -eq $null -and $res.Removed.Count -eq 1) 'A2 scan: a token inside a file of exactly 5 MiB is found and the file removed' "removed=$($res.Removed -join ',')"
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

$gb = Invoke-Setup (New-Scratch 'home-bogus') @('-Phase', 'Bogus', '-StateRoot', (New-Scratch 'state-bogus'), '-Workspace', (New-Scratch 'ws-bogus'))
Assert-That ($gb.Exit -eq 64 -and $gb.Out -match "Unknown phase 'Bogus'") 'phases: an unknown phase exits 64 (it used to exit 1: Write-Error under ErrorActionPreference Stop threw before the exit)' "exit=$($gb.Exit)`n$($gb.Out)"

Write-Host ("RESULT fails={0}" -f $script:Fails)
if ($script:Fails -gt 0) { exit 1 }
exit 0
