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
param([string]$ScratchRoot = '')

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Stop'
$setup = Join-Path $PSScriptRoot 'Setup-MlvWorkstation.ps1'
if (-not $ScratchRoot) { $ScratchRoot = Join-Path ([IO.Path]::GetTempPath()) ('workstation-state-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
[void][IO.Directory]::CreateDirectory($ScratchRoot)
$pwshExe = (Get-Command pwsh).Source

$script:Fails = 0
function Assert-That([bool]$Condition, [string]$Name, [string]$Detail = '') {
    if ($Condition) { Write-Host "PASS $Name" } else { $script:Fails++; Write-Host "FAIL $Name $Detail" }
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

Write-Host ("RESULT fails={0}" -f $script:Fails)
if ($script:Fails -gt 0) { exit 1 }
exit 0
