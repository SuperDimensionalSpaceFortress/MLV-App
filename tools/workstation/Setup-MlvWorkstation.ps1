#Requires -Version 7.0
<#
.SYNOPSIS
    Bootstrap a dedicated MLV-App workstation that mirrors the VIRTUAL-TEN fleet hub host.

.DESCRIPTION
    One script, many phases. Idempotent and re-runnable: every phase checks before it acts,
    and every action is logged to C:\ProgramData\MlvWorkstation\logs\.

    NEW machine (elevated pwsh 7; on a bare box install it first with winget - README step 1):
        Host, Packages, Ssh, Layout, Clone, Qt, Import, Tasks, Verify, Checklist   ('All' runs these)
    SOURCE machine (VIRTUAL-TEN, not elevated, read-only towards its tasks and hooks):
        Export        builds a sanitized transfer bundle under C:\mlvtmp\migrate-out
    EITHER machine (one-host-active rule for the hub):
        LeaseShow, LeaseAcquire, LeaseRelease
    Scheduled tasks only:
        Gate          runs a board script only when this host holds the hub lease (or -GateMode Local)

    It never types, stores or prints a credential. Owner-only steps are printed by -Phase Checklist.
    Pass -WhatIf for a dry run: each action is announced and skipped.

.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File "C:\!Layi Wkspc\MLV-App\tools\workstation\Setup-MlvWorkstation.ps1" -WhatIf

.EXAMPLE
    pwsh -NoProfile -ExecutionPolicy Bypass -File "C:\!Layi Wkspc\MLV-App\tools\workstation\Setup-MlvWorkstation.ps1" -Phase Export
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # One or more of: All Host Layout Packages Ssh Clone Qt Import Tasks Verify Checklist Export Gate
    # LeaseShow LeaseAcquire LeaseRelease. "A,B" works under pwsh -File too (split below).
    [string[]]$Phase = @('All'),
    [string]$Workspace = 'C:\!Layi Wkspc',
    [string]$MlvTmp = 'C:\mlvtmp',
    # Optional: put the worktree root on another volume and make C:\mlvtmp a junction to it.
    # The repo hard-codes C:\mlvtmp (Invoke-Lane, build-guard), so the C: path must keep resolving.
    [string]$MlvTmpTarget = '',
    [string]$BundlePath = 'C:\mlvtmp\migrate-in\mlv-migrate-bundle.zip',
    [string]$ExportDir = 'C:\mlvtmp\migrate-out',
    # Public key line of the SOURCE machine (ssh-ed25519 AAAA... comment). Empty = placeholder only.
    [string]$AuthorizedKey = '',
    [string[]]$SshAllowFrom = @('LocalSubnet', '100.64.0.0/10'),
    [string]$GitName = 'Layi',
    [string]$GitEmail = 'kidfob@gmail.com',
    [string]$QtVersion = '6.10.2',
    [string]$CodexPinVersion = '0.160.1',
    # Capped at 16 TB (the Windows per-file limit): the MB value (GB * 1024) must fit the uint32 the pagefile setting takes, or the step would
    # fail AFTER it had turned automatic pagefile management off (adversary A10).
    # 0 = leave as is; otherwise 8..16384. The initial size is 8192 MB, so a maximum below 8 GB is an invalid initial/maximum pair that would be rejected AFTER
    # automatic management was switched off (adversary pass 2, A7); the upper bound keeps the MB value inside uint32.
    [ValidateScript({ if ($_ -eq 0 -or ($_ -ge 8 -and $_ -le 16384)) { $true } else { throw "PagefileMaxGB must be 0 (leave as is) or between 8 and 16384, got $_" } })][int]$PagefileMaxGB = 0,
    [switch]$NoPowerChanges,
    [switch]$NoDefenderExclusions,
    [switch]$ReplaceTasks,
    [string]$GateScript = '',
    # Gate arguments as "Name=Value;Switch" (a dash-free form, so pwsh -File cannot misbind them).
    [string]$GateArgLine = '',
    [ValidateSet('Lease', 'Local')]
    [string]$GateMode = 'Lease',
    [string]$LeaseReason = '',
    # The machine being migrated FROM. New-machine phases refuse to run there (they would rewrite its live
    # tasks, sshd and settings) unless -Force.
    [string]$SourceHost = 'VIRTUAL-TEN',
    [switch]$Force,
    # Where this script keeps its logs, backups and the gate copy. Overridable so tests stay in a scratch folder.
    [string]$StateRoot = 'C:\ProgramData\MlvWorkstation',
    # Where the gate/lease-release exclusion lock lives. A machine-wide place that does NOT follow -StateRoot, so two runs with different state roots
    # still exclude each other (adversary pass 2, A2). Tests point it at a scratch folder.
    [string]$LockRoot = 'C:\ProgramData\MlvWorkstation-locks',
    # The two venue heartbeat files -Phase Verify must be able to read (fail closed otherwise, README step 6b).
    # Overridable so tests can point them at scratch files; production never passes these.
    [string]$UltraMagnusHeartbeatPath = '\\Ultra-Magnus\g\Temp\mlv-gpu-profile\agent\heartbeat.txt',
    [string]$BachelorHeartbeatPath = '\\bachelor\mlv-agent\heartbeat.txt'
)

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Stop'
# Which parameters this run was given (Get-GateTaskArgument hands the path overrides on to the scheduled tasks).
$script:SetupBound = @{}
foreach ($k in $PSBoundParameters.Keys) { $script:SetupBound[$k] = $true }

$validPhases = @('All', 'Host', 'Packages', 'Ssh', 'Layout', 'Clone', 'Qt', 'Import', 'Tasks', 'Verify', 'Checklist',
    'Export', 'Gate', 'LeaseShow', 'LeaseAcquire', 'LeaseRelease')
$Phase = @($Phase | ForEach-Object { ($_ -split ',').Trim() } | Where-Object { $_ })
foreach ($ph in $Phase) { if ($validPhases -notcontains $ph) { [Console]::Error.WriteLine("Unknown phase '$ph'. Valid: $($validPhases -join ', ')"); exit 64 } }
# Gate is what the scheduled tasks run, alone. Combined with another phase it would switch off the source-host, elevation and failure-exit guards
# for that other phase (adversary A16), so the combination is refused outright.
if ($Phase -contains 'Gate' -and $Phase.Count -gt 1) { [Console]::Error.WriteLine("Phase 'Gate' must be run alone (got: $($Phase -join ', '))."); exit 64 }

# Store/MSIX pwsh (under WindowsApps) cannot set the LocalMachine execution policy and fails the Windows
# capability cmdlets with "Class not registered" (PC-MINI3 2026-11-13). Re-run under the MSI pwsh when present.
$msiPwsh = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
if ($PSHOME -like '*\WindowsApps\*' -and (Test-Path -LiteralPath $msiPwsh) -and $MyInvocation.InvocationName -ne '.') {
    $q = { param($v) "'" + ("$v" -replace "'", "''") + "'" }
    $argText = foreach ($k in $PSBoundParameters.Keys) {
        $v = $PSBoundParameters[$k]
        if ($v -is [System.Management.Automation.SwitchParameter]) { "-${k}:`$$([bool]$v)" }
        elseif ($k -eq 'Phase') { "-Phase " + ((@($Phase) | ForEach-Object { & $q $_ }) -join ',') }
        elseif ($v -is [array]) { "-$k " + ((@($v) | ForEach-Object { & $q $_ }) -join ',') }
        else { "-$k " + (& $q $v) }
    }
    $call = "& $(& $q $PSCommandPath) $($argText -join ' '); exit `$LASTEXITCODE"
    Write-Host "Store pwsh detected ($PSHOME); re-running under $msiPwsh"
    & $msiPwsh -NoProfile -ExecutionPolicy Bypass -EncodedCommand ([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($call)))
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------------------------
# Constants (survey of VIRTUAL-TEN, 2026-11-11; versions are a reference, not a pin, except Qt
# and MinGW, which the repo pins in .github/workflows/Windows.yml and tools/build-release.ps1)
# ---------------------------------------------------------------------------------------------
$Repo = Join-Path $Workspace 'MLV-App'
$DoctrineRepo = Join-Path $Workspace 'softwarefactory-fleet-doctrine'
$ForkUrl = 'https://github.com/SuperDimensionalSpaceFortress/MLV-App.git'
$UpstreamUrl = 'https://github.com/ilia3101/MLV-App.git'
$DoctrineUrl = 'https://github.com/layibabalola/softwarefactory-fleet-doctrine.git'
$QtRoot = 'C:\Qt'
$MingwDir = Join-Path $QtRoot 'Tools\mingw1310_64'
$LeaseRemote = 'fork'
$LeaseRef = 'refs/heads/hub-lease/mlv-app'
$LeaseFile = 'HUB-LEASE.json'
$SourceUserProfile = 'C:\Users\obabalola'
$ReferenceVersions = [ordered]@{
    git = '2.40.0'; gh = '2.97.0'; pwsh = '7.6.9'; python = '3.14.3'; node = '24.14.0'; npm = '8.17.0'
    claude = '2.1.322'; codex = '0.167.1 (lanes pinned to 0.160.1)'; qt = '6.10.2 mingw_64'; mingw = 'mingw1310_64'
    openssh = 'OpenSSH_for_Windows_9.5p1'; ram = '64 GiB'; cpus = '16 logical'; pagefile = '8192/32768 MB'
}

# Board-driving tasks run only on the lease holder; host-local tasks run anywhere.
# Limits and cadences copied from the VIRTUAL-TEN task definitions.
$TaskSpecs = @(
    @{ Name = 'MLV-HubAutopilot'; Script = "$Repo\.claude-state\hub-autopilot\hub-autopilot.ps1"; ArgLine = 'Quiet'; Every = 5; Limit = 14; Mode = 'Lease' }
    @{ Name = 'MLV-FleetHeartbeatPublish'; Script = "$Repo\.claude-state\heartbeat\publish-fleet-heartbeat.ps1"; ArgLine = ''; Every = 60; Limit = 20; Mode = 'Lease' }
    @{ Name = 'MLV-QualityLift'; Script = "$Repo\.claude-state\fleet-runs\hub-tools\Measure-FactoryQualityLift.ps1"; ArgLine = ''; Every = 60; Limit = 30; Mode = 'Lease' }
    @{ Name = 'Fleet-HealthDaily'; Script = "$env:USERPROFILE\bin\Invoke-FleetHealthDaily.ps1"; ArgLine = ''; Daily = '06:13'; Limit = 30; Mode = 'Lease' }
    @{ Name = 'MLV-BoardStateHeartbeat'; Script = "$Repo\.claude-state\heartbeat\board-heartbeat.ps1"; ArgLine = 'IntervalMinutes=10;Quiet'; Every = 10; Limit = 9; Mode = 'Local' }
    @{ Name = 'MLV-DiskAutoReclaim'; Script = "$Repo\.claude-state\disk-hygiene\disk-reclaim-task.ps1"; ArgLine = 'Quiet'; Every = 15; Limit = 14; Mode = 'Local' }
    @{ Name = 'MLV-ArchiveRetention'; Script = "$Repo\.claude-state\disk-hygiene\Invoke-ArchiveRetention.ps1"; ArgLine = 'Apply;Quiet'; Weekly = '02:30'; Limit = 480; Mode = 'Local' }
    @{ Name = 'Claude-UsageProbe'; Script = "$env:USERPROFILE\.claude\hooks\usage-probe.ps1"; ArgLine = ''; Every = 10; Limit = 9; Mode = 'Local' }
)

# ---------------------------------------------------------------------------------------------
# Logging and step plumbing
# ---------------------------------------------------------------------------------------------
$script:Cmdlet = $PSCmdlet
$script:Failures = [System.Collections.Generic.List[string]]::new()
$script:Manual = [System.Collections.Generic.List[string]]::new()
# The process id makes the stamp unique between two runs in the same second (they would share a stage folder, a ZIP name, lease temp files and backups;
# adversary pass 2, A5).
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + "-$PID"
$logDir = Join-Path $StateRoot 'logs'
try { [void][IO.Directory]::CreateDirectory($logDir) } catch { $logDir = Join-Path $env:TEMP 'MlvWorkstation-logs'; [void][IO.Directory]::CreateDirectory($logDir) }
$isGate = ($Phase -contains 'Gate')
$script:LogFile = if ($isGate) { Join-Path $logDir 'gate.log' } else { Join-Path $logDir "setup-$stamp.log" }

function Write-Log {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'ERROR', 'MANUAL', 'OK')][string]$Level = 'INFO')
    $line = '{0} [{1}] {2}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $Level, $Message
    if (-not $isGate) {
        $color = @{ INFO = 'Gray'; WARN = 'Yellow'; ERROR = 'Red'; MANUAL = 'Cyan'; OK = 'Green' }[$Level]
        Write-Host $line -ForegroundColor $color
    }
    # AppendAllText bypasses -WhatIf on purpose: a dry run still leaves its log.
    [IO.File]::AppendAllText($script:LogFile, $line + [Environment]::NewLine)
    if ($Level -eq 'MANUAL') { $script:Manual.Add($Message) }
}

function Invoke-Step {
    param([string]$Description, [scriptblock]$Action)
    if ($script:Cmdlet.ShouldProcess($env:COMPUTERNAME, $Description)) {
        Write-Log "DO   $Description"
        try { & $Action } catch {
            Write-Log "FAIL $Description :: $($_.Exception.Message)" 'ERROR'
            $script:Failures.Add($Description)
        }
    } else {
        Write-Log "SKIP (WhatIf) $Description"
    }
}

function Invoke-Native {
    # Runs a native command, logs its output, throws on a non-zero exit unless -AllowFail.
    param([string]$Exe, [string[]]$Arguments, [switch]$AllowFail, [int[]]$OkCodes = @(0))
    Write-Log ("  > {0} {1}" -f $Exe, ($Arguments -join ' '))
    $out = & $Exe @Arguments 2>&1
    $code = $LASTEXITCODE
    foreach ($o in @($out)) { if ("$o".Trim()) { Write-Log ("    {0}" -f "$o".TrimEnd()) } }
    if ($OkCodes -notcontains $code -and -not $AllowFail) { throw "$Exe exited $code" }
    return $code
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Update-SessionPath {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = ($m, $u, "$env:APPDATA\npm", "$env:LOCALAPPDATA\Python\bin" | Where-Object { $_ }) -join ';'
}

function Get-PwshPath {
    $p = 'C:\Program Files\PowerShell\7\pwsh.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    $c = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

function Invoke-Robocopy {
    # Robocopy exit codes 0-7 are success. Never /MIR or /PURGE: a re-run must not delete anything.
    param([string]$From, [string]$To, [string[]]$Files = @(), [string[]]$Extra = @())
    if (-not (Test-Path -LiteralPath $From)) { Write-Log "  (absent, skipped) $From" 'WARN'; return }
    $robo = Join-Path $env:SystemRoot 'System32\robocopy.exe'
    $robocopyArgs = @($From, $To) + $Files + @('/R:1', '/W:1', '/NP', '/NFL', '/NDL', '/NJH', '/NJS', '/XJ') + $Extra
    [void](Invoke-Native -Exe $robo -Arguments $robocopyArgs -OkCodes @(0, 1, 2, 3, 4, 5, 6, 7))
}

function Write-AsciiFile([string]$Path, [string]$Text) {
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    [IO.File]::WriteAllText($Path, $Text, [Text.Encoding]::ASCII)
}

# ---------------------------------------------------------------------------------------------
# Hub lease: one host drives the board. A git ref on the fork is the shared, atomic token.
# Acquire and release are deliberate (owner or hub), never automatic; staleness never transfers it.
# ---------------------------------------------------------------------------------------------
function Get-HubLease {
    if (-not (Test-Path -LiteralPath (Join-Path $Repo '.git'))) { return @{ Error = "repo missing at $Repo" } }
    $ls = & git -C $Repo ls-remote $LeaseRemote $LeaseRef 2>&1
    if ($LASTEXITCODE -ne 0) { return @{ Error = "ls-remote failed: $($ls -join ' ')" } }
    $line = @($ls | Where-Object { "$_" -match '^[0-9a-f]{40}\s' }) | Select-Object -First 1
    if (-not $line) { return @{ Error = $null; Exists = $false; Sha = ''; Holder = ''; Data = $null } }
    $sha = ("$line" -split '\s+')[0]
    & git -C $Repo fetch --quiet --no-tags $LeaseRemote "+${LeaseRef}:refs/hub-lease-local/mlv-app" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { return @{ Error = 'fetch of lease ref failed' } }
    $raw = & git -C $Repo show "${sha}:$LeaseFile" 2>&1
    if ($LASTEXITCODE -ne 0) { return @{ Error = "lease commit $sha has no $LeaseFile" } }
    try { $data = ($raw -join "`n") | ConvertFrom-Json } catch { return @{ Error = "lease JSON unreadable at $sha" } }
    return @{ Error = $null; Exists = $true; Sha = $sha; Holder = [string]$data.holder; Data = $data }
}

function Set-HubLease {
    param([string]$Holder, [hashtable]$Current, [string]$Reason)
    $doc = [ordered]@{
        schema = 'mlv-app/hub-lease/v1'
        holder = $Holder
        previousHolder = if ($Current.Exists) { $Current.Holder } else { '' }
        setBy = "$env:USERNAME@$env:COMPUTERNAME"
        setUtc = (Get-Date).ToUniversalTime().ToString('o')
        reason = $Reason
        rule = 'Only the holder runs board-driving tasks (MLV-HubAutopilot, FleetHeartbeatPublish, QualityLift, Fleet-HealthDaily). Release on the holder before acquiring elsewhere.'
    }
    $tmp = Join-Path $env:TEMP "hub-lease-$stamp.json"
    [IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json) + "`n", [Text.Encoding]::ASCII)
    $blob = (& git -C $Repo hash-object -w --no-filters $tmp).Trim()
    # A private temp index, not mktree on stdin: pwsh appends CRLF to piped native input, which would
    # land a carriage return in the tree entry name. The repo's real index and worktree are untouched.
    $idx = Join-Path $env:TEMP "hub-lease-index-$stamp"
    $oldIdx = $env:GIT_INDEX_FILE
    try {
        $env:GIT_INDEX_FILE = $idx
        & git -C $Repo update-index --add --cacheinfo "100644,$blob,$LeaseFile"
        if ($LASTEXITCODE -ne 0) { throw 'update-index failed' }
        $tree = (& git -C $Repo write-tree).Trim()
    } finally {
        $env:GIT_INDEX_FILE = $oldIdx
        Remove-Item -LiteralPath $idx -ErrorAction SilentlyContinue
    }
    $msg = "hub-lease: holder=$(if ($Holder) { $Holder } else { '<released>' }) by $env:COMPUTERNAME"
    $commitArgs = @('-C', $Repo, '-c', "user.name=$GitName", '-c', "user.email=$GitEmail", 'commit-tree', $tree, '-m', $msg)
    if ($Current.Exists) { $commitArgs += @('-p', $Current.Sha) }
    $commit = (& git @commitArgs).Trim()
    $expect = if ($Current.Exists) { $Current.Sha } else { '' }
    [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'push', '--porcelain', "--force-with-lease=${LeaseRef}:$expect", $LeaseRemote, "${commit}:$LeaseRef"))
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    Write-Log "lease now holder='$Holder' commit=$commit" 'OK'
}

function Show-HubLease {
    $l = Get-HubLease
    if ($l.Error) { Write-Log "lease: UNKNOWN ($($l.Error))" 'WARN'; return $l }
    if (-not $l.Exists) { Write-Log 'lease: no lease ref yet (nobody holds it; acquire deliberately on the active hub)' 'WARN'; return $l }
    $who = if ($l.Holder) { $l.Holder } else { '<released>' }
    Write-Log ("lease: holder={0} setBy={1} setUtc={2} reason={3} sha={4}" -f $who, $l.Data.setBy, $l.Data.setUtc, $l.Data.reason, $l.Sha.Substring(0, 10))
    return $l
}

# ---------------------------------------------------------------------------------------------
# Phase: Host (OS settings that every multi-agent dev box needs)
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseHost {
    Invoke-Step 'Enable Win32 long paths (LongPathsEnabled=1)' {
        Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -Type DWord
    }
    if (-not $NoPowerChanges) {
        Invoke-Step 'Power: High performance plan, never sleep or hibernate on AC' {
            [void](Invoke-Native -Exe 'powercfg' -Arguments @('/setactive', '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c') -AllowFail)
            foreach ($k in 'standby-timeout-ac', 'hibernate-timeout-ac', 'disk-timeout-ac') { [void](Invoke-Native -Exe 'powercfg' -Arguments @('/change', $k, '0')) }
        }
    } else { Write-Log 'power changes skipped (-NoPowerChanges)' }
    Invoke-Step 'Time sync: w32time automatic and resynced (lanes compare UTC stamps across hosts)' {
        Set-Service w32time -StartupType Automatic
        Start-Service w32time -ErrorAction SilentlyContinue
        [void](Invoke-Native -Exe 'w32tm' -Arguments @('/resync') -AllowFail)
    }
    Invoke-Step 'Execution policy RemoteSigned (LocalMachine)' {
        # Under -ExecutionPolicy Bypass the Process scope overrides the value just written, so Set-ExecutionPolicy
        # throws "Security error." even when the write landed (PC-MINI3 2026-11-13). Judge by read-back, not by the throw.
        try { Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine -Force -ErrorAction Stop } catch { Write-Log "  Set-ExecutionPolicy said: $($_.Exception.Message) (checking the value)" }
        $lm = Get-ExecutionPolicy -Scope LocalMachine
        if ($lm -notin 'RemoteSigned', 'Unrestricted', 'Bypass') { throw "LocalMachine execution policy is $lm after the set" }
        Write-Log "  LocalMachine execution policy = $lm" 'OK'
    }
    if ($PagefileMaxGB -gt 0) {
        Invoke-Step "Pagefile C:\pagefile.sys 8192 MB initial / $($PagefileMaxGB * 1024) MB max (reboot to apply)" {
            $cs = Get-CimInstance Win32_ComputerSystem
            if ($cs.AutomaticManagedPagefile) { Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $false } }
            $pf = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue | Where-Object { $_.Name -ieq 'C:\pagefile.sys' }
            if (-not $pf) { $pf = New-CimInstance -ClassName Win32_PageFileSetting -Property @{ Name = 'C:\pagefile.sys' } }
            Set-CimInstance -InputObject $pf -Property @{ InitialSize = [uint32]8192; MaximumSize = [uint32]($PagefileMaxGB * 1024) }
        }
    } else { Write-Log 'pagefile left as is (pass -PagefileMaxGB N to set; the commit gate reads RAM + pagefile MAX)' }
    $winPs = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not $NoDefenderExclusions) {
        $paths = @($MlvTmp, $QtRoot, $Repo, (Join-Path $env:LOCALAPPDATA 'Temp\claude'))
        if ($MlvTmpTarget) { $paths += $MlvTmpTarget }
        Invoke-Step ("Defender path exclusions (via Windows PowerShell, where the Defender module is native): " + ($paths -join ', ')) {
            $list = ($paths | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ','
            [void](Invoke-Native -Exe $winPs -Arguments @('-NoProfile', '-NonInteractive', '-Command', "Add-MpPreference -ExclusionPath @($list)"))
        }
        Write-Log 'VIRTUAL-TEN manages Defender roots with C:\ProgramData\FleetDefender (imported by -Phase Import); Apply-DefenderRoots.ps1 there is the long-term owner of this list'
    } else { Write-Log 'Defender exclusions skipped (-NoDefenderExclusions)' }
}

# ---------------------------------------------------------------------------------------------
# Phase: Layout
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseLayout {
    if ($MlvTmpTarget -and -not (Test-Path -LiteralPath $MlvTmp)) {
        Invoke-Step "Worktree root on $MlvTmpTarget with junction $MlvTmp" {
            [void][IO.Directory]::CreateDirectory($MlvTmpTarget)
            New-Item -ItemType Junction -Path $MlvTmp -Target $MlvTmpTarget | Out-Null
        }
    }
    foreach ($d in @($Workspace, $MlvTmp, "$MlvTmp\locks", "$MlvTmp\migrate-in", "$MlvTmp\codex-pin", $StateRoot, "$StateRoot\backups")) {
        if (-not (Test-Path -LiteralPath $d)) { Invoke-Step "Create $d" { [void][IO.Directory]::CreateDirectory($d) } }
    }
}

# ---------------------------------------------------------------------------------------------
# Phase: Packages
# ---------------------------------------------------------------------------------------------
function Install-WingetPackage {
    param([string]$Id, [string]$Label, [switch]$Optional, [string]$ManualHint = '')
    $listed = & winget list --id $Id -e --accept-source-agreements --disable-interactivity 2>&1
    if ($LASTEXITCODE -eq 0 -and (($listed -join "`n") -match [regex]::Escape($Id))) { Write-Log "present: $Label ($Id)" 'OK'; return }
    Invoke-Step "winget install $Label ($Id)" {
        $code = Invoke-Native -Exe 'winget' -Arguments @('install', '--id', $Id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity') -AllowFail
        if ($code -ne 0) {
            $hint = if ($ManualHint) { $ManualHint } else { "install $Label by hand" }
            if ($Optional) { Write-Log "winget could not install $Label (exit $code): $hint" 'MANUAL' } else { throw "winget exit $code for $Id" }
        }
        # A fresh install edits the Machine/User PATH, not this process's; reload so later steps find the tool.
        Update-SessionPath
    }
}

function Invoke-PhasePackages {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Log 'winget missing: install "App Installer" from the Microsoft Store, then re-run' 'MANUAL'
        return
    }
    Install-WingetPackage 'Microsoft.PowerShell' 'PowerShell 7'
    Install-WingetPackage 'Git.Git' 'Git for Windows'
    Install-WingetPackage 'GitHub.cli' 'GitHub CLI'
    Install-WingetPackage 'Python.PythonInstallManager' 'Python install manager (py)' -Optional -ManualHint 'hooks expect %LOCALAPPDATA%\Python\bin\python.exe; install "Python Install Manager" from python.org'
    Install-WingetPackage 'OpenJS.NodeJS.LTS' 'Node.js LTS'
    Install-WingetPackage 'Microsoft.VisualStudioCode' 'VS Code'
    Install-WingetPackage 'Anthropic.Claude' 'Claude Desktop' -Optional -ManualHint 'download from https://claude.ai/download'
    Install-WingetPackage 'OpenAI.ChatGPT' 'ChatGPT desktop' -Optional -ManualHint 'install ChatGPT from the Microsoft Store'
    # NOT winget 'OpenAI.Codex': that id is the Codex CLI (0.168.0 on 2026-11-13), which lands on PATH ahead of the
    # npm codex and the lane pin (C:\mlvtmp\codex-pin\<CodexPinVersion>). Codex desktop comes from the Microsoft Store.
    Write-Log 'Codex desktop: install from the Microsoft Store (winget OpenAI.Codex is the CLI, not the desktop app; do not install it)' 'MANUAL'
    Install-WingetPackage 'Tailscale.Tailscale' 'Tailscale (reaches BACHELOR at bachelor.tail47abfa.ts.net)'
    Install-WingetPackage 'BurntSushi.ripgrep.MSVC' 'ripgrep'
    Install-WingetPackage '7zip.7zip' '7-Zip'
    Update-SessionPath

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $have = (& py list 2>&1) -join "`n"
        if ($have -notmatch '3\.14') { Invoke-Step 'py install 3.14 (matches VIRTUAL-TEN python 3.14)' { [void](Invoke-Native -Exe 'py' -Arguments @('install', '3.14') -AllowFail) } }
    } else { Write-Log 'py launcher not on PATH yet; open a new shell and re-run -Phase Packages' 'WARN' }
    Update-SessionPath

    if (Get-Command npm -ErrorAction SilentlyContinue) {
        foreach ($pkg in '@anthropic-ai/claude-code', '@openai/codex') {
            $present = (& npm ls -g --depth 0 $pkg 2>&1) -join "`n"
            if ($present -match [regex]::Escape($pkg) + '@') { Write-Log "present: npm -g $pkg" 'OK' }
            else { Invoke-Step "npm install -g $pkg" { [void](Invoke-Native -Exe 'npm' -Arguments @('install', '-g', $pkg)) } }
        }
        $pinDir = Join-Path $MlvTmp "codex-pin\$CodexPinVersion"
        if (-not (Test-Path -LiteralPath (Join-Path $pinDir 'node_modules\.bin\codex.cmd'))) {
            Invoke-Step "codex lane pin $CodexPinVersion into $pinDir (promotion probe stays owner-run after sign-in)" {
                [void][IO.Directory]::CreateDirectory($pinDir)
                [void](Invoke-Native -Exe 'npm' -Arguments @('install', '--prefix', $pinDir, "@openai/codex@$CodexPinVersion"))
            }
        } else { Write-Log "present: codex pin $CodexPinVersion" 'OK' }
    } else { Write-Log 'npm not on PATH yet; open a new shell and re-run -Phase Packages' 'WARN' }

    if (Get-Command git -ErrorAction SilentlyContinue) {
        Invoke-Step 'git: system core.longpaths=true, global identity, credential manager' {
            [void](Invoke-Native -Exe 'git' -Arguments @('config', '--system', 'core.longpaths', 'true'))
            [void](Invoke-Native -Exe 'git' -Arguments @('config', '--global', 'core.longpaths', 'true'))
            [void](Invoke-Native -Exe 'git' -Arguments @('config', '--global', 'user.name', $GitName))
            [void](Invoke-Native -Exe 'git' -Arguments @('config', '--global', 'user.email', $GitEmail))
            [void](Invoke-Native -Exe 'git' -Arguments @('config', '--global', 'credential.helper', 'manager'))
        }
    }
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        Invoke-Step 'gh: git_protocol https (remotes are https, matching VIRTUAL-TEN)' { [void](Invoke-Native -Exe 'gh' -Arguments @('config', 'set', 'git_protocol', 'https') -AllowFail) }
    }
}

# ---------------------------------------------------------------------------------------------
# Phase: Ssh (inbound key-only sshd with pwsh shell; outbound keys for GitHub and BACHELOR)
# ---------------------------------------------------------------------------------------------
function Set-SshdOption {
    param([string[]]$Lines, [string]$Name, [string]$Value)
    $matchIdx = [Array]::FindIndex($Lines, [Predicate[string]] { param($l) $l -match '^\s*Match\s' })
    $end = if ($matchIdx -ge 0) { $matchIdx } else { $Lines.Count }
    for ($i = 0; $i -lt $end; $i++) {
        if ($Lines[$i] -match "^\s*#?\s*$Name\s") { $Lines[$i] = "$Name $Value"; return , $Lines }
    }
    return , (@("$Name $Value") + $Lines)
}

# Key-only sshd_config (adversary A5, A6). Backs up first (creating the backup folder), sets the three options in the global section, REFUSES
# (before writing anything) a Match block that turns password or keyboard-interactive authentication back on, writes, validates with
# `sshd -t -f <config>` (or the -Validate seam: a scriptblock given the config path that returns the exit code), and restores the backup if
# the validator rejects the file. Throws on any failure.
function Set-SshdKeyOnlyConfig {
    param([string]$ConfigPath, [string]$BackupDir, [string]$Stamp, [scriptblock]$Validate = $null)
    [string[]]$lines = @(Get-Content -LiteralPath $ConfigPath)
    # An Include pulls in files this function does not read (a global PasswordAuthentication yes placed there, or a Match rule, still wins; adversary pass 2,
    # A6), so a config that includes anything is refused rather than judged by its main file alone.
    $inc = [Array]::FindIndex($lines, [Predicate[string]] { param($l) $l -match '^\s*Include(\s|=)' })
    if ($inc -ge 0) { throw "sshd_config line $($inc + 1) ('$($lines[$inc].Trim())') includes other files, which this script does not inspect; inline them by hand and re-run -Phase Ssh. Nothing was changed." }
    $firstMatch = [Array]::FindIndex($lines, [Predicate[string]] { param($l) $l -match '^\s*Match\s' })
    if ($firstMatch -ge 0) {
        for ($i = $firstMatch; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match '^\s*(PasswordAuthentication|KbdInteractiveAuthentication)(\s+|\s*=\s*)"?yes\b') {
                throw "sshd_config line $($i + 1) ('$($lines[$i].Trim())') re-enables password authentication inside a Match block; key-only is the rule, so remove it by hand and re-run -Phase Ssh. Nothing was changed."
            }
        }
    }
    [void][IO.Directory]::CreateDirectory($BackupDir)
    $bak = Join-Path $BackupDir "sshd_config.$Stamp"
    Copy-Item -LiteralPath $ConfigPath -Destination $bak -Force
    $lines = Set-SshdOption -Lines $lines -Name 'PubkeyAuthentication' -Value 'yes'
    $lines = Set-SshdOption -Lines $lines -Name 'PasswordAuthentication' -Value 'no'
    $lines = Set-SshdOption -Lines $lines -Name 'KbdInteractiveAuthentication' -Value 'no'
    [IO.File]::WriteAllLines($ConfigPath, $lines, [Text.Encoding]::ASCII)
    $rc = 0
    if ($Validate) { $rc = [int](& $Validate $ConfigPath) }
    else {
        $sshd = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
        & $sshd -t -f $ConfigPath 2>&1 | ForEach-Object { Write-Log "    $_" }
        $rc = $LASTEXITCODE
    }
    if ($rc -ne 0) { Copy-Item -LiteralPath $bak -Destination $ConfigPath -Force; throw 'sshd -t rejected the new config; restored the backup' }
}

function Invoke-PhaseSsh {
    $cap = try { Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction Stop | Select-Object -First 1 } catch { Write-Log "cannot query Windows capabilities (elevation?): $($_.Exception.Message)" 'WARN'; $null }
    if ($cap -and $cap.State -ne 'Installed') { Invoke-Step "Add Windows capability $($cap.Name)" { Add-WindowsCapability -Online -Name $cap.Name | Out-Null } }
    elseif ($cap) { Write-Log 'present: OpenSSH Server capability' 'OK' }
    elseif (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
        # The capability cmdlets fail with "Class not registered" under some pwsh hosts; DISM does the same job.
        Invoke-Step 'Add OpenSSH Server via DISM (capability query unavailable)' {
            [void](Invoke-Native -Exe (Join-Path $env:SystemRoot 'System32\dism.exe') -Arguments @('/Online', '/Add-Capability', '/CapabilityName:OpenSSH.Server~~~~0.0.1.0', '/NoRestart') -OkCodes @(0, 3010))
        }
    }

    Invoke-Step 'sshd and ssh-agent: automatic start, running' {
        Set-Service sshd -StartupType Automatic; Start-Service sshd
        Set-Service ssh-agent -StartupType Automatic; Start-Service ssh-agent
    }
    $pwsh = Get-PwshPath
    if ($pwsh) {
        Invoke-Step "OpenSSH DefaultShell = $pwsh" {
            if (-not (Test-Path 'HKLM:\SOFTWARE\OpenSSH')) { New-Item 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null }
            New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value $pwsh -PropertyType String -Force | Out-Null
        }
    } else { Write-Log 'pwsh not installed yet: run -Phase Packages first, then -Phase Ssh again' 'WARN' }

    Invoke-Step ("Firewall: inbound TCP 22, profiles Domain+Private, from " + ($SshAllowFrom -join ',')) {
        $rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
        if (-not $rule) { New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow | Out-Null }
        Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled True -Profile Domain, Private -RemoteAddress $SshAllowFrom
    }

    $akPath = 'C:\ProgramData\ssh\administrators_authorized_keys'
    Invoke-Step "administrators_authorized_keys: $(if ($AuthorizedKey) { 'add the given key' } else { 'placeholder only' }), ACL Administrators+SYSTEM" {
        [void][IO.Directory]::CreateDirectory('C:\ProgramData\ssh')
        $cur = if (Test-Path -LiteralPath $akPath) { @(Get-Content -LiteralPath $akPath) } else { @() }
        $placeholder = '# PASTE the VIRTUAL-TEN public key line here (ssh-ed25519 AAAA... obabalola@VIRTUAL-TEN). See tools\workstation\README.md step 2.'
        if ($cur -notcontains $placeholder) { $cur = @($placeholder) + $cur }
        if ($AuthorizedKey -and ($cur -notcontains $AuthorizedKey.Trim())) { $cur += $AuthorizedKey.Trim() }
        [IO.File]::WriteAllLines($akPath, [string[]]$cur, [Text.Encoding]::ASCII)
        [void](Invoke-Native -Exe 'icacls.exe' -Arguments @($akPath, '/inheritance:r', '/grant', '*S-1-5-32-544:F', '/grant', '*S-1-5-18:F'))
    }

    $cfg = 'C:\ProgramData\ssh\sshd_config'
    if (Test-Path -LiteralPath $cfg) {
        Invoke-Step 'sshd_config: PubkeyAuthentication yes, PasswordAuthentication no, validate, restart' {
            try {
                Set-SshdKeyOnlyConfig -ConfigPath $cfg -BackupDir (Join-Path $StateRoot 'backups') -Stamp $stamp
                Restart-Service sshd
            } catch {
                # Fail closed (adversary A6): sshd was started and its firewall rule opened above with the stock config, which allows passwords.
                # If the hardening did not land, the service does not stay up on that config.
                try { Stop-Service sshd -Force -ErrorAction Stop; Write-Log 'sshd STOPPED because the key-only config did not land; fix the cause and re-run -Phase Ssh' 'ERROR' } catch { Write-Log "could not stop sshd after a failed hardening: $($_.Exception.Message)" 'ERROR' }
                throw
            }
        }
    } else { Write-Log "sshd_config not created yet ($cfg); start sshd once and re-run -Phase Ssh" 'WARN' }
    if (-not $AuthorizedKey) { Write-Log 'SSH is key-only and no source key is installed yet: paste the VIRTUAL-TEN public key (README step 3)' 'MANUAL' }

    $sshDir = Join-Path $env:USERPROFILE '.ssh'
    if (-not $WhatIfPreference) { [void][IO.Directory]::CreateDirectory($sshDir) }   # -WhatIf must not create it (adversary A9)
    foreach ($k in @(@{ File = 'id_ed25519_github'; Purpose = 'github' }, @{ File = 'id_ed25519_bachelor'; Purpose = 'bachelor' })) {
        $kp = Join-Path $sshDir $k.File
        if (Test-Path -LiteralPath $kp) { Write-Log "present: $kp" 'OK'; continue }
        Invoke-Step "Generate $kp (ed25519, no passphrase so unattended lanes can use it)" {
            [void](Invoke-Native -Exe 'ssh-keygen' -Arguments @('-q', '-t', 'ed25519', '-f', $kp, '-N', '', '-C', "$env:USERNAME@$env:COMPUTERNAME-$($k.Purpose)"))
        }
    }
    $sshCfg = Join-Path $sshDir 'config'
    $existing = if (Test-Path -LiteralPath $sshCfg) { Get-Content -LiteralPath $sshCfg -Raw } else { '' }
    $blocks = @()
    if ($existing -notmatch '(?m)^Host\s+github\.com\b') {
        $blocks += "Host github.com`n    User git`n    IdentityFile ~/.ssh/id_ed25519_github`n    IdentitiesOnly yes`n"
    }
    if ($existing -notmatch '(?m)^Host\s+bachelor\b') {
        $blocks += "# Bachelor (Windows GPU host, user layib) over Tailscale - mirrors VIRTUAL-TEN.`nHost bachelor`n    HostName bachelor.tail47abfa.ts.net`n    User layib`n    IdentityFile ~/.ssh/id_ed25519_bachelor`n    IdentitiesOnly yes`n    StrictHostKeyChecking accept-new`n    ConnectTimeout 8`n    ServerAliveInterval 30`n"
    }
    if ($blocks) { Invoke-Step "Append $($blocks.Count) Host block(s) to $sshCfg" { [IO.File]::AppendAllText($sshCfg, "`n" + ($blocks -join "`n"), [Text.Encoding]::ASCII) } }
}

# ---------------------------------------------------------------------------------------------
# Phase: Clone
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseClone {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Log 'git missing: run -Phase Packages first' 'ERROR'; return }
    if (-not (Test-Path -LiteralPath (Join-Path $Repo '.git'))) {
        Invoke-Step "Clone MLV-App fork into $Repo (remote 'fork' = SuperDimensionalSpaceFortress, 'origin' = ilia3101, as on VIRTUAL-TEN)" {
            [void](Invoke-Native -Exe 'git' -Arguments @('-c', 'core.longpaths=true', 'clone', '--origin', 'fork', $ForkUrl, $Repo))
        }
    } else { Write-Log "present: $Repo" 'OK' }
    if (Test-Path -LiteralPath (Join-Path $Repo '.git')) {
        Invoke-Step 'MLV-App remotes, fetch, master tracks fork/master' {
            $remotes = @(& git -C $Repo remote)
            if ($remotes -notcontains 'fork') { [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'remote', 'add', 'fork', $ForkUrl)) }
            if ($remotes -notcontains 'origin') { [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'remote', 'add', 'origin', $UpstreamUrl)) }
            [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'config', 'core.longpaths', 'true'))
            [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'fetch', '--quiet', 'fork'))
            [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'fetch', '--quiet', 'origin') -AllowFail)
            [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'branch', '--set-upstream-to=fork/master', 'master') -AllowFail)
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $DoctrineRepo '.git'))) {
        Invoke-Step "Clone doctrine bus into $DoctrineRepo" {
            $code = Invoke-Native -Exe 'git' -Arguments @('clone', $DoctrineUrl, $DoctrineRepo) -AllowFail
            if ($code -ne 0) { Write-Log 'doctrine bus clone failed (private repo?): run "gh auth login", then re-run -Phase Clone' 'MANUAL' }
        }
    } else { Write-Log "present: $DoctrineRepo" 'OK' }
}

# ---------------------------------------------------------------------------------------------
# Phase: Qt (the repo pins Qt 6.10.2 win64_mingw + MinGW 13.1 via hash-locked aqtinstall)
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseQt {
    $qmake = Join-Path $QtRoot "$QtVersion\mingw_64\bin\qmake.exe"
    $make = Join-Path $MingwDir 'bin\mingw32-make.exe'
    if ((Test-Path -LiteralPath $qmake) -and (Test-Path -LiteralPath $make)) { Write-Log "present: Qt $QtVersion and MinGW 13.1" 'OK'; return }
    $req = Join-Path $Repo '.github\requirements\aqtinstall.txt'
    if (-not (Test-Path -LiteralPath $req)) { Write-Log "missing ${req}: run -Phase Clone first" 'ERROR'; return }
    Update-SessionPath
    $venv = Join-Path $MlvTmp 'venv-aqt'
    $vpy = Join-Path $venv 'Scripts\python.exe'
    Invoke-Step "aqtinstall venv at $venv (hash-locked requirements from the repo)" {
        $ok = $false
        foreach ($sel in @('-3', '-V:3.12')) {
            if (Test-Path -LiteralPath $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
            if ($sel -eq '-V:3.12') { [void](Invoke-Native -Exe 'py' -Arguments @('install', '3.12') -AllowFail) }
            if ((Invoke-Native -Exe 'py' -Arguments @($sel, '-m', 'venv', $venv) -AllowFail) -ne 0) { continue }
            if ((Invoke-Native -Exe $vpy -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--only-binary=:all:', '--require-hashes', '-r', $req) -AllowFail) -eq 0) { $ok = $true; break }
            Write-Log "pip install under py $sel failed; trying the next interpreter" 'WARN'
        }
        if (-not $ok) { throw 'no interpreter could install the hash-locked aqtinstall' }
    }
    if (-not (Test-Path -LiteralPath $qmake)) {
        Invoke-Step "aqt install-qt $QtVersion win64_mingw (+qtmultimedia) into $QtRoot" {
            [void](Invoke-Native -Exe $vpy -Arguments @('-m', 'aqt', 'install-qt', '--outputdir', $QtRoot, 'windows', 'desktop', $QtVersion, 'win64_mingw', '-m', 'qtmultimedia'))
        }
    }
    if (-not (Test-Path -LiteralPath $make)) {
        Invoke-Step "aqt install-tool tools_mingw1310 into $QtRoot" {
            [void](Invoke-Native -Exe $vpy -Arguments @('-m', 'aqt', 'install-tool', '--outputdir', $QtRoot, 'windows', 'desktop', 'tools_mingw1310', 'qt.tools.win64_mingw1310'))
        }
    }
    if (-not ((Test-Path -LiteralPath $qmake) -and (Test-Path -LiteralPath $make)) -and -not $WhatIfPreference) {
        Write-Log "Qt fallback: run the Qt Online Installer (needs your Qt account), install to $QtRoot : Qt $QtVersion > MinGW 64-bit (+ Qt Multimedia) and Developer Tools > MinGW 13.1.0 64-bit" 'MANUAL'
    }
}

# ---------------------------------------------------------------------------------------------
# Phase: Export (SOURCE machine). Read-only towards tasks, hooks and the repo; writes only under
# $ExportDir. Secrets, credentials, auth tokens, ssh keys and session transcripts never enter it.
# ---------------------------------------------------------------------------------------------
$SecretPatterns = @(
    'sk-ant-[A-Za-z0-9_\-]{20,}', 'sk-proj-[A-Za-z0-9_\-]{20,}', 'sk-[A-Za-z0-9]{40,}',
    'gh[pousr]_[A-Za-z0-9]{30,}', 'github_pat_[A-Za-z0-9_]{30,}', 'xox[abprs]-[A-Za-z0-9\-]{10,}',
    '-----BEGIN [A-Z ]*PRIVATE KEY-----', '"(access|refresh|id)_?[Tt]oken"\s*:\s*"[^"]{16,}"', 'AKIA[0-9A-Z]{16}',
    # A secret VALUE under a name that says what it is (adversary pass 2, A15): an AWS secret access key has no recognisable shape of its own.
    '(?i)"?[A-Z0-9_]*(SECRET|PASSWORD|PASSWD|PRIVATE_?KEY)[A-Z0-9_]*"?\s*[:=]\s*"[^"\s]{16,}"'
)
$ExcludeFiles = @('*.log', '*.err', '*.bak*', '*.tmp', '*.lock', '*.pid', '*.sqlite*', '*.jsonl', '.credentials.json', '.claude.json',
    'auth.json', '*.pem', '*.pfx', '*.p12', '*.key', '*.ppk', 'id_rsa*', 'id_ed25519*', '*_id', '*_id.pub', '*.kdbx', '.env', '*.env',
    '*token*', '*secret*', '*credential*', '*.sbp-*', 'context-window-pct.tmp',
    '*.dll', '*.exe', '*.zip', '*.7z', '*.tar', '*.dng', '*.mp4', '*.mov')
$ExcludeDirs = @('.git', 'node_modules', '__pycache__', 'worktrees', 'ssh', 'openssh', '.ssh', 'sessions', 'archived_sessions',
    'identity', 'shell-snapshots', 'session-env', 'file-history', 'telemetry', 'usage', 'cache', 'logs', 'log', 'retired', 'diag',
    'archive', 'handoff-archive', 'quarantine', 'preservation')

# Secret scan of the staged bundle (adversary A2, A3). Every staged file up to the copy limit (-MaxScanBytes, equal to the robocopy /MAX
# above) is read and matched; a file that matches, that is larger than the limit, or that cannot be read is removed (fail closed: an
# unreadable file used to be scanned as empty text and then archived). A file that cannot be removed is reported as Uncleared, and the
# caller refuses to write the bundle.
function Invoke-StagedSecretScan {
    param([string]$Stage, [long]$MaxScanBytes = 26214400)
    $rx = [regex]::new(($SecretPatterns -join '|'))
    $removed = [System.Collections.Generic.List[string]]::new()
    $uncleared = [System.Collections.Generic.List[string]]::new()
    # -Force: robocopy copies Hidden and System files, and Get-ChildItem hides them by default (adversary pass 2, A3).
    foreach ($f in @(Get-ChildItem -LiteralPath $Stage -Recurse -File -Force)) {
        $rel = $f.FullName.Substring($Stage.Length).TrimStart('\')
        $why = ''
        if ($f.Length -gt $MaxScanBytes) { $why = 'larger than the scan limit' }
        else {
            $t = $null
            try { $t = [IO.File]::ReadAllText($f.FullName) } catch { $why = 'unreadable' }
            if (-not $why -and $t -and $rx.IsMatch($t)) { $why = 'credential pattern' }
        }
        if (-not $why) { continue }
        try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop; $removed.Add("$rel ($why)") }
        catch { $uncleared.Add("$rel ($why; could not be removed)") }
    }
    return @{ Removed = $removed.ToArray(); Uncleared = $uncleared.ToArray() }
}

# Scan + report, returning $true only when every staged file is clean or was removed. Fail closed (adversary pass 2, A4): any throw, an uncleared file,
# or a report that cannot be written reads as NOT clean, so the caller never goes on to zip a stage that still holds a credential.
function Test-StagedBundleClean {
    param([string]$Stage)
    try {
        $res = Invoke-StagedSecretScan -Stage $Stage
        $clean = ($res.Uncleared.Count -eq 0)
        try { Write-AsciiFile (Join-Path $Stage 'SECRET-SCAN.txt') ("removed $($res.Removed.Count) file(s) that matched a credential pattern or could not be scanned:`r`n" + ($res.Removed -join "`r`n") + "`r`n") }
        catch { Write-Log "could not write SECRET-SCAN.txt: $($_.Exception.Message)" 'WARN' }
        Write-Log "secret scan removed $($res.Removed.Count) file(s) (see SECRET-SCAN.txt; review before transfer)" $(if ($res.Removed.Count) { 'WARN' } else { 'OK' })
        if (-not $clean) { Write-Log "secret scan could not clear $($res.Uncleared.Count) file(s): $($res.Uncleared -join '; ')" 'ERROR' }
        return $clean
    } catch {
        Write-Log "secret scan failed: $($_.Exception.Message)" 'ERROR'
        return $false
    }
}

function Invoke-PhaseExport {
    $stage = Join-Path $ExportDir "bundle-$stamp"
    $ref = Join-Path $stage 'reference'
    $common = @('/XF') + $ExcludeFiles + @('/XD') + $ExcludeDirs + @('/MAX:26214400')
    # Hooks: code only. Their dot-files (.foo.log, .foo.state, .foo.json) are per-host receipts and state.
    $hookCommon = @('/XF') + $ExcludeFiles + @('.*') + @('/XD') + $ExcludeDirs + @('/MAX:26214400')
    $h = $env:USERPROFILE
    $cs = Join-Path $Repo '.claude-state'
    Invoke-Step "Stage sanitized copies under $stage" {
        [void][IO.Directory]::CreateDirectory($ref)
        # ~/.claude: instructions, settings, rotation docs, hooks (code, not their state dot-files), skills, machine runbooks.
        Invoke-Robocopy "$h\.claude" "$stage\home-claude" @('CLAUDE.md', 'settings.json', 'ROTATION.md', 'ROTATION-install-prompt.md', 'machine-authority-precedence.json', 'known-projects.json') (@('/LEV:1') + $common)
        Invoke-Robocopy "$h\.claude\hooks" "$stage\home-claude\hooks" @() (@('/E') + $hookCommon)
        Invoke-Robocopy "$h\.claude\skills" "$stage\home-claude\skills" @() (@('/E') + $common)
        Invoke-Robocopy "$h\.claude\machine" "$stage\home-claude\machine" @() (@('/E') + $common)
        Invoke-Robocopy "$h\.claude\workflows" "$stage\home-claude\workflows" @() (@('/E') + $common)
        Invoke-Robocopy "$h\.claude\projects\C---Layi-Wkspc-MLV-App\memory" "$stage\home-claude\projects\C---Layi-Wkspc-MLV-App\memory" @() (@('/E') + $common)
        Invoke-Robocopy "$h\bin" "$stage\home-bin" @('*.ps1', '*.py', '*.vbs', '*.cmd') (@('/LEV:1') + $common)
        Invoke-Robocopy "$h\.codex" "$stage\home-codex" @('AGENTS.md') (@('/LEV:1') + $common)
        Invoke-Robocopy "$h\.codex\rules" "$stage\home-codex\rules" @() (@('/E') + $common)
        Invoke-Robocopy 'C:\ProgramData\FleetDefender' "$stage\programdata-fleetdefender" @('*.ps1', 'policy.json') (@('/LEV:1') + $common)
        # Project .claude-state: tooling, memory, checkpoints. Run dirs, evidence and worktrees stay behind.
        Invoke-Robocopy $cs "$stage\project-claude-state" @('*.md') (@('/LEV:1') + $common)
        # disk-hygiene: its scripts only; its retire-* subfolders are archives of old worktrees (~0.9 GB).
        Invoke-Robocopy "$cs\disk-hygiene" "$stage\project-claude-state\disk-hygiene" @('*.ps1', '*.json', '*.md', '*.vbs') (@('/LEV:1') + $common)
        foreach ($d in 'heartbeat', 'hub-autopilot', 'project-memory', 'coordination', 'continuity', 'kernel', 'doctrine',
            'doctrine-outbox', 'scripts', 'tools', 'bin', 'machine-notes', 'planning') {
            Invoke-Robocopy "$cs\$d" "$stage\project-claude-state\$d" @() (@('/E') + $common)
        }
        Invoke-Robocopy "$cs\fleet-runs\hub-tools" "$stage\project-claude-state\fleet-runs\hub-tools" @('*.ps1', '*.py', '*.json', '*.md', '*.vbs') (@('/LEV:1') + $common)
        # MINI3-CUTOVER-STATE-GAPS-1 (b): the doctrine fold marker (.codex-state\doctrine\last-seen.json). Without it the new host's fold
        # meter reads reason=last-seen-missing. Only that folder: the rest of .codex-state is per-host session state.
        Invoke-Robocopy "$Repo\.codex-state\doctrine" "$stage\project-codex-state\doctrine" @() (@('/E') + $common)
        # Reference only (never installed blindly): ssh client config, codex config, task XML, inventory.
        Invoke-Robocopy "$h\.ssh" $ref @('config') @('/LEV:1')
        Invoke-Robocopy "$h\.codex" $ref @('config.toml') @('/LEV:1')
        Invoke-Robocopy "$h\.claude" $ref @('machine-inventory.yaml') @('/LEV:1')
    }
    Invoke-Step 'Export scheduled-task XML (reference) and toolchain versions' {
        $tx = Join-Path $ref 'task-xml'; [void][IO.Directory]::CreateDirectory($tx)
        Get-ScheduledTask | Where-Object { $_.TaskPath -eq '\' -and $_.TaskName -match '^(MLV-|Claude-|Fleet-|fleet-|Codex)' } | ForEach-Object {
            Export-ScheduledTask -TaskName $_.TaskName -TaskPath '\' | Set-Content -LiteralPath (Join-Path $tx "$($_.TaskName).xml") -Encoding utf8
        }
        $v = [System.Collections.Generic.List[string]]::new()
        $v.Add("host=$env:COMPUTERNAME exported=$stamp")
        foreach ($c in @(@('git', '--version'), @('gh', '--version'), @('node', '--version'), @('npm', '--version'), @('py', '-3', '--version'), @('claude', '--version'), @('codex', '--version'))) {
            try { $o = (& $c[0] $c[1..($c.Count - 1)] 2>&1 | Select-Object -First 1); $v.Add("$($c[0]): $o") } catch { $v.Add("$($c[0]): (missing)") }
        }
        $v.Add("pwsh: $($PSVersionTable.PSVersion)")
        Write-AsciiFile (Join-Path $ref 'toolchain-versions.txt') (($v -join "`r`n") + "`r`n")
    }
    Invoke-Step 'Census: worktree branches whose tip is not on the fork (push these before cut-over)' {
        $forkRefs = @{}
        foreach ($l in @(& git -C $Repo ls-remote $LeaseRemote 'refs/heads/*' 2>$null)) {
            $p = "$l" -split '\s+'; if ($p.Count -ge 2) { $forkRefs[$p[1] -replace '^refs/heads/', ''] = $p[0] }
        }
        $rows = [System.Collections.Generic.List[string]]::new()
        $rows.Add("status`tbranch`tlocal_sha`tworktree")
        $wt = ''; $sha = ''
        foreach ($l in @(& git -C $Repo worktree list --porcelain)) {
            if ($l -like 'worktree *') { $wt = $l.Substring(9) }
            elseif ($l -like 'HEAD *') { $sha = $l.Substring(5) }
            elseif ($l -like 'branch *') {
                $b = $l.Substring(7) -replace '^refs/heads/', ''
                $st = if (-not $forkRefs.ContainsKey($b)) { 'NOT_ON_FORK' } elseif ($forkRefs[$b] -ne $sha) { 'FORK_DIFFERS' } else { 'pushed' }
                if ($st -ne 'pushed') { $rows.Add("$st`t$b`t$($sha.Substring(0, 10))`t$wt") }
            }
        }
        Write-AsciiFile (Join-Path $ref 'unpushed-worktree-branches.tsv') (($rows -join "`r`n") + "`r`n")
        Write-Log "unpushed or diverged worktree branches: $($rows.Count - 1) (reference\unpushed-worktree-branches.tsv)" $(if ($rows.Count -gt 1) { 'WARN' } else { 'OK' })
    }
    # Fail closed: the flag starts TRUE and only a scan that returns clean clears it, so a throw anywhere in the step can never reach the zip.
    $script:ScanFailed = $true
    Invoke-Step 'Secret scan: drop any staged file that looks like it holds a credential (or cannot be scanned)' {
        $script:ScanFailed = -not (Test-StagedBundleClean -Stage $stage)
        if ($script:ScanFailed) { throw 'secret scan could not clear every staged file, so no bundle is written' }
    }
    $zip = Join-Path $ExportDir "mlv-migrate-bundle-$stamp.zip"
    if ($script:ScanFailed -and -not $WhatIfPreference) { Write-Log 'bundle NOT written: the secret scan could not clear every staged file (fail closed)' 'ERROR'; return }
    Invoke-Step "Write MANIFEST.txt and zip to $zip" {
        $files = Get-ChildItem -LiteralPath $stage -Recurse -File -Force
        $sum = ($files | Measure-Object Length -Sum).Sum
        Write-AsciiFile (Join-Path $stage 'MANIFEST.txt') ("mlv-migrate-bundle v1`r`nsource=$env:COMPUTERNAME user=$env:USERNAME profile=$env:USERPROFILE created=$stamp`r`nfiles=$($files.Count) bytes=$sum`r`n")
        $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
        [void](Invoke-Native -Exe $tar -Arguments @('-a', '-c', '-f', $zip, '-C', $stage, '.'))
        $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
        Write-AsciiFile "$zip.sha256" "$hash  $(Split-Path -Leaf $zip)`r`n"
        Write-Log "bundle $zip sha256=$hash files=$($files.Count) bytes=$sum" 'OK'
        Write-Log ("transfer: scp `"$zip`" <newbox>:C:/mlvtmp/migrate-in/mlv-migrate-bundle.zip   (or copy it by USB/share to $BundlePath)") 'MANUAL'
    }
}

# ---------------------------------------------------------------------------------------------
# Foreign hub claims (MINI3-CUTOVER-STATE-GAPS-1 gap a). A claim is a one-line JSON file {utc, runDir, ...} in
# hub-autopilot\claims. The export carries the claims but not the fleet-runs run dirs they name, so on the new host
# every claim younger than the dispatch preflight's 6 h window reads 'receipt state none' and blocks its card.
# Rule: a claim whose runDir does not exist here AND whose utc is under 24 h old moves to claims\migrated-foreign-<stamp>
# (moved, never deleted: the file is the evidence). A malformed claim, a claim older than 24 h and a claim whose
# runDir exists all stay where they are.
# ---------------------------------------------------------------------------------------------
function Move-ForeignClaims {
    param([string]$ClaimsDir, [string]$Stamp, [int]$MaxAgeHours = 24)
    $moved = [System.Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $ClaimsDir -PathType Container)) { Write-Log "no claims folder at $ClaimsDir (nothing to quarantine)"; return }
    $dest = Join-Path $ClaimsDir "migrated-foreign-$Stamp"
    $keptMalformed = 0
    foreach ($f in @(Get-ChildItem -LiteralPath $ClaimsDir -Filter '*.claim' -File -Force)) {
        $o = $null
        try { $o = [IO.File]::ReadAllText($f.FullName) | ConvertFrom-Json -ErrorAction Stop } catch { $o = $null }
        $pRun = if ($o -is [pscustomobject]) { $o.PSObject.Properties['runDir'] } else { $null }
        $pUtc = if ($o -is [pscustomobject]) { $o.PSObject.Properties['utc'] } else { $null }
        # Malformed = no runDir/utc, a runDir that is not a string (an object would stringify to a path that never exists, adversary A11) or is
        # not a FULLY QUALIFIED path (a relative one, and the drive-relative C:x and root-relative \x forms that IsPathRooted accepts, would be judged against
        # this process's working directory; adversary pass 2, A8).
        if (-not $pRun -or -not $pUtc -or $pRun.Value -isnot [string] -or -not "$($pRun.Value)".Trim() -or -not [IO.Path]::IsPathFullyQualified("$($pRun.Value)")) { $keptMalformed++; continue }
        $when = $null
        # ConvertFrom-Json turns a UTC string into a DateTime; an offset-free one comes back Kind=Unspecified, which ToUniversalTime() would shift by the host's
        # zone offset. The claim writer's utc is UTC by contract, so Unspecified is read as UTC (adversary pass 2, A9).
        if ($pUtc.Value -is [datetime]) { $dtv = [datetime]$pUtc.Value; $when = $(if ($dtv.Kind -eq [DateTimeKind]::Unspecified) { [DateTime]::SpecifyKind($dtv, [DateTimeKind]::Utc) } else { $dtv.ToUniversalTime() }) }
        else {
            $dto = [DateTimeOffset]::MinValue
            if ([DateTimeOffset]::TryParse("$($pUtc.Value)", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$dto)) { $when = $dto.UtcDateTime }
        }
        if ($null -eq $when) { $keptMalformed++; continue }
        $ageH = ((Get-Date).ToUniversalTime() - $when).TotalHours
        if ($ageH -ge $MaxAgeHours) { continue }
        if (Test-Path -LiteralPath "$($pRun.Value)") { continue }
        [void][IO.Directory]::CreateDirectory($dest)
        Move-Item -LiteralPath $f.FullName -Destination (Join-Path $dest $f.Name)
        $moved.Add($f.Name)
    }
    Write-Log "foreign claims quarantined: $($moved.Count) -> $dest ; malformed claims left in place: $keptMalformed" $(if ($moved.Count) { 'WARN' } else { 'OK' })
    return $moved.ToArray()
}

# ---------------------------------------------------------------------------------------------
# Phase: Import (NEW machine). Copies the bundle into place; never touches credentials.
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseImport {
    if (-not (Test-Path -LiteralPath $BundlePath)) {
        Write-Log "no bundle at ${BundlePath}: run -Phase Export on VIRTUAL-TEN and copy the zip here (README step 4), then re-run -Phase Import" 'MANUAL'
        return
    }
    $src = $BundlePath
    if ((Get-Item -LiteralPath $BundlePath).PSIsContainer -eq $false) {
        $src = Join-Path $MlvTmp "migrate-in\bundle-$stamp"
        Invoke-Step "Expand $BundlePath to $src" {
            [void][IO.Directory]::CreateDirectory($src)
            [void](Invoke-Native -Exe (Join-Path $env:SystemRoot 'System32\tar.exe') -Arguments @('-x', '-f', $BundlePath, '-C', $src))
        }
    }
    if (-not $WhatIfPreference -and -not (Test-Path -LiteralPath (Join-Path $src 'MANIFEST.txt'))) { Write-Log "not a migrate bundle (no MANIFEST.txt): $src" 'ERROR'; return }
    $h = $env:USERPROFILE
    Invoke-Step 'Back up existing ~/.claude CLAUDE.md and settings.json' {
        $b = Join-Path $StateRoot "backups\home-claude-$stamp"; [void][IO.Directory]::CreateDirectory($b)
        foreach ($f in 'CLAUDE.md', 'settings.json') { $p = Join-Path "$h\.claude" $f; if (Test-Path -LiteralPath $p) { Copy-Item -LiteralPath $p -Destination $b } }
    }
    Invoke-Step 'Copy bundle into ~/.claude, ~/bin, ~/.codex, ProgramData\FleetDefender, MLV-App\.claude-state' {
        # Import never touches credentials (adversary A4): the export never stages the credential-shaped names, but a bundle is only a folder, so
        # the same exclusion list is applied on the way IN. A hand-made or tampered bundle cannot overwrite this host's auth.json or
        # .credentials.json, and its sign-in stays the owner's (README step 3).
        $inX = @('/XF') + $ExcludeFiles
        Invoke-Robocopy "$src\home-claude" "$h\.claude" @() (@('/E') + $inX)
        Invoke-Robocopy "$src\home-bin" "$h\bin" @() (@('/E') + $inX)
        Invoke-Robocopy "$src\home-codex" "$h\.codex" @() (@('/E') + $inX)
        Invoke-Robocopy "$src\programdata-fleetdefender" 'C:\ProgramData\FleetDefender' @() (@('/E') + $inX)
        # The claims folder is copied on its own below, never overwriting a claim this host already has (adversary pass 2, A10): a live local claim
        # replaced by a foreign one of the same name would be quarantined for its absent run dir and the running card would lose its claim.
        $claimsSrc = "$src\project-claude-state\hub-autopilot\claims"
        Invoke-Robocopy "$src\project-claude-state" "$Repo\.claude-state" @() (@('/E', '/XD', $claimsSrc) + $inX)
        Invoke-Robocopy $claimsSrc "$Repo\.claude-state\hub-autopilot\claims" @() (@('/E', '/XC', '/XN', '/XO') + $inX)
        # MINI3-CUTOVER-STATE-GAPS-1 (b): /XO so a fold marker this host already advanced is never overwritten by the older bundle copy.
        Invoke-Robocopy "$src\project-codex-state\doctrine" "$Repo\.codex-state\doctrine" @() (@('/E', '/XO') + $inX)
        Invoke-Robocopy "$src\reference" "$StateRoot\reference" @() (@('/E') + $inX)
    }
    # MINI3-CUTOVER-STATE-GAPS-1 (a): claims came across without their fleet-runs run dirs. A claim younger than 6 h with no run dir
    # reads 'receipt state none' and blocks its card, so those claims are quarantined, not carried.
    Invoke-Step 'Quarantine foreign hub claims (run dir absent on this host, under 24 h old)' {
        [void](Move-ForeignClaims -ClaimsDir (Join-Path $Repo '.claude-state\hub-autopilot\claims') -Stamp $stamp)
    }
    if ($h -ine $SourceUserProfile) {
        Invoke-Step "Rewrite $SourceUserProfile -> $h in ~/.claude/settings.json" {
            $p = "$h\.claude\settings.json"; $t = [IO.File]::ReadAllText($p)
            $t = $t.Replace('C:/Users/obabalola', ($h -replace '\\', '/')).Replace('C:\\Users\\obabalola', ($h -replace '\\', '\\'))
            [IO.File]::WriteAllText($p, $t, [Text.UTF8Encoding]::new($false))
        }
        Write-Log "profile differs from VIRTUAL-TEN ($SourceUserProfile). The TRACKED $Repo\.claude\settings.json hard-codes C:/Users/obabalola/AppData/Local/Python/bin/python.exe, so project hooks will fail here. Prefer an OS user named obabalola; else fix the project file in a PR." 'MANUAL'
    }
    Write-Log 'imported hub state is a SNAPSHOT: re-export and re-import at cut-over (README phase C) before this host takes the lease' 'WARN'
}

# ---------------------------------------------------------------------------------------------
# Phase: Tasks (registered DISABLED; board-driving ones are lease-gated even once enabled)
# ---------------------------------------------------------------------------------------------
# The command line a scheduled task runs. -StateRoot, -Workspace and -MlvTmp are passed on when THIS run was given them (adversary A7): the gate
# copy starts with the defaults otherwise, and would read PAUSED, the lease and the board scripts from the wrong place.
function Get-GateTaskArgument {
    param([string]$Pwsh, [string]$Self, [hashtable]$Spec)
    $arg = "--headless `"$Pwsh`" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Self`" -Phase Gate -GateMode $($Spec.Mode) -GateScript `"$($Spec.Script)`""
    foreach ($k in 'StateRoot', 'Workspace', 'MlvTmp', 'LockRoot') {
        if ($script:SetupBound.ContainsKey($k)) { $arg += " -$k `"$(Get-Variable -Name $k -ValueOnly)`"" }
    }
    if ($Spec.ArgLine) { $arg += " -GateArgLine `"$($Spec.ArgLine)`"" }
    return $arg
}

function Invoke-PhaseTasks {
    $pwsh = Get-PwshPath
    if (-not $pwsh) { Write-Log 'pwsh missing: run -Phase Packages first' 'ERROR'; return }
    $self = Join-Path $StateRoot 'Setup-MlvWorkstation.ps1'
    if ($PSCommandPath -and ($PSCommandPath -ine $self)) {
        Invoke-Step "Install gate runner copy at $self" { [void][IO.Directory]::CreateDirectory($StateRoot); Copy-Item -LiteralPath $PSCommandPath -Destination $self -Force }
    }
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    foreach ($t in $TaskSpecs) {
        $existing = Get-ScheduledTask -TaskName $t.Name -TaskPath '\' -ErrorAction SilentlyContinue
        if ($existing -and -not $ReplaceTasks) { Write-Log "task exists, left as is: $($t.Name) state=$($existing.State) (pass -ReplaceTasks to rewrite; its enabled state is kept)" 'OK'; continue }
        $wasEnabled = [bool]($existing -and $existing.State -ne 'Disabled')
        $arg = Get-GateTaskArgument -Pwsh $pwsh -Self $self -Spec $t
        # Named $taskAction, not $action: Invoke-Step's own -Action parameter would shadow it inside the step.
        $taskAction = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\conhost.exe') -Argument $arg -WorkingDirectory $Repo
        $trigger = if ($t.ContainsKey('Every')) { New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $t.Every) }
        elseif ($t.ContainsKey('Daily')) { New-ScheduledTaskTrigger -Daily -At $t.Daily }
        else { New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $t.Weekly }
        $settings = New-ScheduledTaskSettingsSet -Disable -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes $t.Limit)
        Invoke-Step "Register task $($t.Name) [$($t.Mode)] DISABLED -> $($t.Script)" {
            Register-ScheduledTask -TaskName $t.Name -TaskPath '\' -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal -Description "MLV workstation ($($t.Mode)-gated). Registered by Setup-MlvWorkstation.ps1 $stamp. Enable per tools\workstation\README.md." -Force | Out-Null
            if ($wasEnabled) { Enable-ScheduledTask -TaskName $t.Name -TaskPath '\' | Out-Null; Write-Log "  kept enabled: $($t.Name)" }
        }
    }
    Write-Log "kill switch for every gated task on this host: create $StateRoot\PAUSED (delete it to resume)"
}

# ---------------------------------------------------------------------------------------------
# Phase: Gate (what the scheduled tasks run)
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseGate {
    $tag = "gate[$GateMode] $(Split-Path -Leaf $GateScript)"
    if (Test-Path -LiteralPath (Join-Path $StateRoot 'PAUSED')) { Write-Log "$tag skipped: PAUSED file present"; exit 0 }
    if (-not $GateScript -or -not (Test-Path -LiteralPath $GateScript)) { Write-Log "$tag skipped: target missing ($GateScript)" 'WARN'; exit 0 }
    # -WhatIf is a dry run: the target is never invoked (adversary A9).
    if ($WhatIfPreference) { Write-Log "SKIP (WhatIf) $tag would run $GateScript"; exit 0 }
    # One-host-active (adversary A8, pass 2 A1/A2): a Lease-gated run holds the gate lock SHARED for its whole life; -Phase LeaseRelease on the holder
    # takes it EXCLUSIVE around its check AND its push. File sharing makes the two mutually exclusive with nothing in between: a release cannot slip in
    # between a gate's lease read and its launch, and a gate cannot start while a release is in flight (it skips). The lock lives under -LockRoot, a fixed
    # machine-wide folder that does not follow -StateRoot. A crashed run leaves nothing: the OS drops the handle.
    $lock = $null
    try {
        if ($GateMode -eq 'Lease') {
            $lock = Open-GateLock -Root $LockRoot -Mode Shared
            if (-not $lock) { Write-Log "$tag skipped: a lease release is in progress on this host (gate lock busy)" 'WARN'; exit 0 }
            $l = Get-HubLease
            if ($l.Error) { Write-Log "$tag skipped: lease unreadable, failing closed ($($l.Error))" 'WARN'; exit 0 }
            if ($l.Holder -ine $env:COMPUTERNAME) { Write-Log "$tag skipped: lease holder is '$($l.Holder)', not $env:COMPUTERNAME"; exit 0 }
        }
        $splat = @{}
        foreach ($kv in @($GateArgLine -split ';' | Where-Object { $_ })) {
            $p = $kv -split '=', 2
            if ($p.Count -eq 2) { $splat[$p[0]] = $p[1] } else { $splat[$p[0]] = $true }
        }
        Write-Log "$tag running"
        # Reset first: under StrictMode reading an unset $LASTEXITCODE throws, which made a target with no native call (and no exit) end the gate with
        # exit 1; and a stale code left by the lease's git calls must not be taken for the target's.
        $global:LASTEXITCODE = 0
        & $GateScript @splat
        $rc = [int]$global:LASTEXITCODE
        Write-Log "$tag exit=$rc"
    } finally { if ($lock) { $lock.Dispose() } }
    exit $rc
}

# The gate lock (see Invoke-PhaseGate). Shared = a Lease-gated run is inside its target; Exclusive = a lease release is in flight. Returns the open
# FileStream (the caller disposes it) or $null when the other kind holds it. Any error other than a sharing violation is thrown, not read as "free".
function Open-GateLock {
    param([string]$Root, [ValidateSet('Shared', 'Exclusive')][string]$Mode)
    [void][IO.Directory]::CreateDirectory($Root)
    $p = Join-Path $Root 'gate.lock'
    if (-not (Test-Path -LiteralPath $p)) { try { [IO.File]::WriteAllText($p, '') } catch { } }
    try {
        if ($Mode -eq 'Shared') { return [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) }
        return [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [System.IO.IOException] { return $null }
}

# ---------------------------------------------------------------------------------------------
# Share preflight (MINI3-CUTOVER-STATE-GAPS-1). Every venue job, staged build and Bachelor heartbeat read goes through two SMB
# shares, and a host with no stored credential reads both as 'not there' (System error 5). Verify therefore fails closed unless
# both heartbeat files can be read. Inline Test-Path on purpose: this script is copied to C:\MlvSetup and run before the repo is
# cloned, so it cannot import a repo module. It names each heartbeat file by path and never lists a share.
# ---------------------------------------------------------------------------------------------
function Test-ShareHeartbeatPreflight {
    param([string]$UltraMagnusPath, [string]$BachelorPath)
    $failed = [System.Collections.Generic.List[string]]::new()
    foreach ($row in @(@{ Path = $UltraMagnusPath; HostName = 'Ultra-Magnus' }, @{ Path = $BachelorPath; HostName = 'bachelor' })) {
        $ok = $false
        # Test-Path only proves the name exists; a heartbeat this host may not read (a deny on FILE_READ_DATA, a stale credential) must fail too
        # (adversary A1), so the file is opened for reading. A folder, a missing file and an access-denied open all throw.
        try {
            $fs = [IO.File]::Open($row.Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $fs.Dispose()
            $ok = $true
        } catch { $ok = $false }
        if ($ok) { Write-Log "share preflight OK: $($row.Path)" 'OK'; continue }
        Write-Log "SHARE_PREFLIGHT_FAILED $($row.Path) ($($row.HostName) share; tools\workstation\README.md step 6b)" 'ERROR'
        $failed.Add($row.Path)
    }
    return $failed.ToArray()
}

# ---------------------------------------------------------------------------------------------
# Meter installs (MINI3-CUTOVER-STATE-GAPS-1 gap c). The import copies the meter scripts but not the scheduled-task registration
# or the pins their -Install writes, so on the new host disk-auto-reclaim reads 'no sweep pin' and pwsh-pin reads 'registered none'.
# Only these two meters: the board-driving tasks are registered by -Phase Tasks, lease-gated.
# Both scripts live in the board-root .claude-state (gitignored, not part of this repo). On a host without the approved SBP
# windowless launcher their -Install throws before writing anything; that is reported as METER_INSTALL_UNSUPPORTED_HOST and
# Verify goes on. Making -Install work without the launcher is a board-root edit and is OPEN, not part of this change.
# ---------------------------------------------------------------------------------------------
function Get-MeterInstallSpecs {
    return @(
        @{ Name = 'MLV-DiskAutoReclaim'; Script = (Join-Path $Repo '.claude-state\disk-hygiene\disk-reclaim-task.ps1') }
        @{ Name = 'MLV-PwshAutoRepin'; Script = (Join-Path $Repo '.claude-state\hub-autopilot\pwsh-autorepin.ps1') }
    )
}

function Invoke-MeterInstalls {
    if ($env:COMPUTERNAME -ieq $SourceHost -and -not $Force) { Write-Log "meter -Install skipped: $SourceHost is the source host (its tasks are live; pass -Force to run it there)"; return }
    # Those -Install scripts register their task ENABLED and not behind this script's gate (they are board-root scripts, outside this PR), so the PAUSED kill
    # switch is the only control: while it is present nothing is installed (adversary pass 2, A11).
    if (Test-Path -LiteralPath (Join-Path $StateRoot 'PAUSED')) { Write-Log "METER_INSTALL_SKIPPED_PAUSED the PAUSED kill switch is present in $StateRoot; delete it and re-run -Phase Verify to install the meters" 'WARN'; return }
    $pwsh = Get-PwshPath
    foreach ($m in (Get-MeterInstallSpecs)) {
        if (-not (Test-Path -LiteralPath $m.Script -PathType Leaf)) { Write-Log "meter -Install skipped: $($m.Name) script absent ($($m.Script)); run -Phase Import first" 'WARN'; continue }
        if ($WhatIfPreference) { Write-Log "SKIP (WhatIf) meter -Install $($m.Name)"; continue }
        if (-not $pwsh) { Write-Log "meter -Install skipped: pwsh missing ($($m.Name))" 'WARN'; continue }
        Write-Log "DO   meter -Install $($m.Name) ($($m.Script))"
        $out = @(& $pwsh -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $m.Script -Install 2>&1 | ForEach-Object { "$_" })
        $code = $LASTEXITCODE
        foreach ($o in $out) { if ($o.Trim()) { Write-Log "    $($o.TrimEnd())" } }
        if ($code -eq 0) { Write-Log "meter -Install ok: $($m.Name)" 'OK'; continue }
        if (($out -join "`n") -match 'SBP windowless launcher is absent') {
            Write-Log "METER_INSTALL_UNSUPPORTED_HOST $($m.Name) (its -Install needs the approved SBP windowless launcher, absent on $env:COMPUTERNAME; nothing was written; OPEN: make the board-root -Install work without it)" 'WARN'
            continue
        }
        Write-Log "METER_INSTALL_FAILED $($m.Name) exit=$code" 'ERROR'
        $script:Failures.Add("METER_INSTALL_FAILED $($m.Name)")
    }
}

# ---------------------------------------------------------------------------------------------
# Phase: Verify (read-only report, plus the share preflight and the two meter installs)
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseVerify {
    Update-SessionPath
    Write-Log '--- toolchain (VIRTUAL-TEN reference in brackets)'
    foreach ($c in @(@('git', '--version', 'git'), @('gh', '--version', 'gh'), @('node', '--version', 'node'), @('npm', '--version', 'npm'),
            @('py', '-3 --version', 'python'), @('claude', '--version', 'claude'), @('codex', '--version', 'codex'))) {
        $cmd = Get-Command $c[0] -ErrorAction SilentlyContinue
        $o = if ($cmd) { try { (& $c[0] ($c[1] -split ' ') 2>&1 | Select-Object -First 1) } catch { 'error' } } else { 'MISSING' }
        Write-Log ("{0,-7} {1}   [{2}]" -f $c[0], $o, $ReferenceVersions[$c[2]]) $(if ($o -eq 'MISSING') { 'WARN' } else { 'INFO' })
    }
    Write-Log ("pwsh    {0}   [{1}]" -f $(if (Get-PwshPath) { ((Get-Item (Get-PwshPath)).VersionInfo.ProductVersion -split ' ')[0] } else { 'MISSING' }), $ReferenceVersions.pwsh)
    $hookPy = Join-Path $env:LOCALAPPDATA 'Python\bin\python.exe'
    Write-Log "hook python $hookPy : $(if (Test-Path -LiteralPath $hookPy) { 'present' } else { 'MISSING (user + project hooks call this exact path)' })" $(if (Test-Path -LiteralPath $hookPy) { 'OK' } else { 'WARN' })
    Write-Log "qmake  $(Test-Path (Join-Path $QtRoot "$QtVersion\mingw_64\bin\qmake.exe"))   mingw32-make $(Test-Path (Join-Path $MingwDir 'bin\mingw32-make.exe'))"
    Write-Log "codex lane pin $CodexPinVersion : $(Test-Path (Join-Path $MlvTmp "codex-pin\$CodexPinVersion\node_modules\.bin\codex.cmd"))"
    Write-Log '--- host'
    $sshd = Get-Service sshd -ErrorAction SilentlyContinue
    Write-Log "sshd: $(if ($sshd) { "$($sshd.Status)/$($sshd.StartType)" } else { 'not installed' })  DefaultShell: $((Get-ItemProperty 'HKLM:\SOFTWARE\OpenSSH' -ErrorAction SilentlyContinue).DefaultShell)"
    $ak = 'C:\ProgramData\ssh\administrators_authorized_keys'
    $keys = try { if (Test-Path -LiteralPath $ak) { @(Get-Content -LiteralPath $ak -ErrorAction Stop | Where-Object { $_ -match '^\s*ssh-' }).Count } else { 0 } } catch { 'unreadable (not elevated)' }
    Write-Log "administrators_authorized_keys: $keys real key(s)" $(if ($keys -is [int] -and $keys -gt 0) { 'OK' } else { 'WARN' })
    Write-Log "LongPathsEnabled=$((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled)  git core.longpaths=$(& git config --get core.longpaths 2>$null)"
    $os = Get-CimInstance Win32_OperatingSystem
    $ram = [double](Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB
    $pfMax = [double](@(Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue | Measure-Object -Property MaximumSize -Sum).Sum) / 1024
    $limit = [Math]::Max([double]$os.TotalVirtualMemorySize / 1MB, $ram + $pfMax)
    Write-Log ("memory: RAM {0:N1} GiB, pagefile max {1:N1} GiB -> commit limit {2:N1} GiB; host-commit-gate ceiling = limit - 12 = {3:N1} GiB (VIRTUAL-TEN: 64 + 32 - 12 = 84). Also avail-phys >= 8 GiB, build-guard 6/8 floors." -f $ram, $pfMax, $limit, ($limit - 12))
    Write-Log ("cpus: {0} logical [{1}]; producer cap lives in .claude-state\hub-autopilot\producer-cap.json (1..4; the hub clamps to 2 under pressure)" -f [Environment]::ProcessorCount, $ReferenceVersions.cpus)
    Write-Log '--- tasks'
    foreach ($t in $TaskSpecs) {
        $x = Get-ScheduledTask -TaskName $t.Name -TaskPath '\' -ErrorAction SilentlyContinue
        Write-Log ("{0,-26} {1,-9} {2}" -f $t.Name, $(if ($x) { $x.State } else { 'ABSENT' }), $t.Mode)
    }
    Write-Log "PAUSED kill switch: $(Test-Path (Join-Path $StateRoot 'PAUSED'))"
    [void](Show-HubLease)
    if (Get-Command gh -ErrorAction SilentlyContinue) { & gh auth status *> $null; Write-Log "gh auth: $(if ($LASTEXITCODE -eq 0) { 'signed in' } else { 'NOT signed in' })" }
    Write-Log '--- venue shares (fail closed: README step 6b)'
    foreach ($bad in @(Test-ShareHeartbeatPreflight -UltraMagnusPath $UltraMagnusHeartbeatPath -BachelorPath $BachelorHeartbeatPath)) { $script:Failures.Add("SHARE_PREFLIGHT_FAILED $bad") }
    Write-Log '--- meter installs (disk-auto-reclaim pin, pwsh auto-repin)'
    Invoke-MeterInstalls
}

# ---------------------------------------------------------------------------------------------
# Phase: Checklist (owner-only steps; printed, never scripted)
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseChecklist {
    $readme = Join-Path $Repo 'tools\workstation\README.md'
    $lines = @(
        '', '================ OWNER-ONLY CHECKLIST (never scripted) ================',
        ' 1. gh auth login                      (GitHub.com, HTTPS, browser) - then re-run -Phase Clone if the doctrine bus failed',
        ' 2. Upload the GitHub key:             gh ssh-key add "$env:USERPROFILE\.ssh\id_ed25519_github.pub" --title "<NEWBOX> mlv"',
        ' 3. claude auth login --claudeai       (this host''s own account; it need NOT match VIRTUAL-TEN - owner 2026-11-13: PC-MINI hosts get their own accounts)',
        '    verify: claude auth status --json -> loggedIn, firstParty, a plan that can run lanes, and orgId equal to THIS host''s Claude Desktop org.',
        '    Sign in to Claude Desktop with the SAME account as this host''s CLI (the account-drift hook compares the two on this host).',
        ' 4. codex login                        (and sign in to Codex desktop / ChatGPT desktop)',
        '    then: pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\coordination\Promote-CodexPin.ps1"   (lane codex pin probe)',
        ' 5. Tailscale: sign in, confirm  tailscale ping bachelor.tail47abfa.ts.net',
        ' 6. BACHELOR: append  %USERPROFILE%\.ssh\id_ed25519_bachelor.pub  to BACHELOR (user layib) authorized keys, then  ssh bachelor hostname',
        '    SMB share \\bachelor\mlv-agent: if it asks for credentials, store them yourself (cmdkey or the Explorer prompt). Never list processed\ or outbox\ unbounded.',
        ' 7. Paste the VIRTUAL-TEN public key into C:\ProgramData\ssh\administrators_authorized_keys (README step 3), then from VIRTUAL-TEN: ssh <newbox> hostname',
        ' 8. Windows: stay signed in (tasks are Interactive logon), Windows Update active hours, BitLocker recovery key saved, reboot once after -PagefileMaxGB',
        " 9. Enable tasks only per the cut-over plan in $readme  (host-local ones first; lease-gated ones after LeaseAcquire)",
        '======================================================================='
    )
    foreach ($l in $lines) { Write-Host $l -ForegroundColor Cyan; [IO.File]::AppendAllText($script:LogFile, $l + [Environment]::NewLine) }
    if ($script:Manual.Count) {
        Write-Host 'Manual follow-ups raised during this run:' -ForegroundColor Cyan
        $script:Manual | ForEach-Object { Write-Host "  - $_" -ForegroundColor Cyan }
    }
}

# Dot-sourcing (". Setup-MlvWorkstation.ps1 -StateRoot <scratch>") loads the functions and stops here, so tests can call them.
if ($MyInvocation.InvocationName -eq '.') { return }

# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------
$order = @('Host', 'Layout', 'Packages', 'Ssh', 'Clone', 'Qt', 'Import', 'Tasks', 'Verify', 'Checklist')
$run = if ($Phase -contains 'All') { $order } else { $Phase }
$needAdmin = @('Host', 'Packages', 'Ssh', 'Tasks', 'Layout')
if (-not $isGate) {
    Write-Log "Setup-MlvWorkstation phases=$($run -join ',') host=$env:COMPUTERNAME user=$env:USERNAME whatif=$([bool]$WhatIfPreference) log=$script:LogFile"
    if ($run -contains 'Export' -and $run.Count -gt 1) { Write-Log 'Export runs on the SOURCE machine; run it on its own.' 'ERROR'; exit 1 }
    $mutating = @('Host', 'Layout', 'Packages', 'Ssh', 'Clone', 'Qt', 'Import', 'Tasks')
    if ($env:COMPUTERNAME -ieq $SourceHost -and @($run | Where-Object { $mutating -contains $_ }).Count -and -not $Force -and -not $WhatIfPreference) {
        Write-Log "This is the source host $SourceHost. New-machine phases would change its live setup; refusing. (Export, Verify, Checklist and Lease* are fine here.)" 'ERROR'
        exit 1
    }
    if (@($run | Where-Object { $needAdmin -contains $_ }).Count -and -not (Test-IsAdmin) -and -not $WhatIfPreference) {
        Write-Log 'These phases need an elevated PowerShell. Re-open as Administrator and re-run.' 'ERROR'
        exit 1
    }
}

foreach ($p in $run) {
    switch ($p) {
        'Host' { Invoke-PhaseHost }
        'Layout' { Invoke-PhaseLayout }
        'Packages' { Invoke-PhasePackages }
        'Ssh' { Invoke-PhaseSsh }
        'Clone' { Invoke-PhaseClone }
        'Qt' { Invoke-PhaseQt }
        'Import' { Invoke-PhaseImport }
        'Tasks' { Invoke-PhaseTasks }
        'Verify' { Invoke-PhaseVerify }
        'Checklist' { Invoke-PhaseChecklist }
        'Export' { Invoke-PhaseExport }
        'Gate' { Invoke-PhaseGate }
        'LeaseShow' { [void](Show-HubLease) }
        'LeaseAcquire' {
            $cur = Show-HubLease
            if ($cur.Error) { Write-Log 'cannot read the lease; refusing to acquire blind' 'ERROR'; exit 2 }
            if ($cur.Holder -ieq $env:COMPUTERNAME) { Write-Log "already held by $env:COMPUTERNAME" 'OK'; break }
            if ($cur.Holder -and -not $Force) {
                Write-Log "held by $($cur.Holder). Release it there first (-Phase LeaseRelease on $($cur.Holder)), or pass -Force -LeaseReason '<why>' if that host is gone." 'ERROR'; exit 3
            }
            if ($cur.Holder -and -not "$LeaseReason".Trim()) { Write-Log '-Force needs a non-blank -LeaseReason' 'ERROR'; exit 3 }
            Invoke-Step "Acquire hub lease for $env:COMPUTERNAME" { Set-HubLease -Holder $env:COMPUTERNAME -Current $cur -Reason $(if ($LeaseReason) { $LeaseReason } else { 'acquire' }) }
        }
        'LeaseRelease' {
            # The gate lock is held EXCLUSIVE across the holder check and the push (adversary pass 2, A1/A2): no Lease-gated run can be mid-flight, or
            # start, until the lease has moved. Busy = a Lease-gated task is inside its target on this host.
            $xlock = Open-GateLock -Root $LockRoot -Mode Exclusive
            if (-not $xlock) { Write-Log 'a Lease-gated task is running on this host (gate lock busy); releasing now would hand the lease over under it. Wait for it to end, then re-run.' 'ERROR'; exit 4 }
            try {
                $cur = Show-HubLease
                if ($cur.Error) { Write-Log 'cannot read the lease' 'ERROR'; exit 2 }
                if (-not $cur.Holder) { Write-Log 'already released' 'OK'; break }
                if ($cur.Holder -ine $env:COMPUTERNAME -and -not $Force) { Write-Log "held by $($cur.Holder), not this host; pass -Force -LeaseReason to override" 'ERROR'; exit 3 }
                if ($cur.Holder -ine $env:COMPUTERNAME -and -not "$LeaseReason".Trim()) { Write-Log '-Force needs a non-blank -LeaseReason (releasing another host''s lease)' 'ERROR'; exit 3 }
                Invoke-Step "Release hub lease (was $($cur.Holder))" { Set-HubLease -Holder '' -Current $cur -Reason $(if ($LeaseReason) { $LeaseReason } else { 'release' }) }
            } finally { $xlock.Dispose() }
        }
    }
}

if (-not $isGate) {
    if ($script:Failures.Count) {
        Write-Log "$($script:Failures.Count) step(s) failed; fix and re-run (idempotent): $($script:Failures -join ' | ')" 'ERROR'
        exit 1
    }
    Write-Log "done. log: $script:LogFile" 'OK'
}
