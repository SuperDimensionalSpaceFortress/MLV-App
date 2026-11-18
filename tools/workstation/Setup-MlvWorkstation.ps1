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
    # p5 A6: a -LockRoot other than the machine-wide default is a scratch/test seam; Gate (Lease) and LeaseRelease refuse it unless this switch says so.
    # A registered task never carries -LockRoot or this switch, so every task and every release on a host uses the same lock.
    [switch]$AllowScratchLockRoot,
    # The two venue heartbeat files -Phase Verify must be able to read (fail closed otherwise, README step 6b).
    # Overridable so tests can point them at scratch files; production never passes these.
    [string]$UltraMagnusHeartbeatPath = '\\Ultra-Magnus\g\Temp\mlv-gpu-profile\agent\heartbeat.txt',
    [string]$BachelorHeartbeatPath = '\\bachelor\mlv-agent\heartbeat.txt'
)

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Stop'
# r10 pass 7 A5: every direct native call below (sshd -t, git, schtasks) checks $LASTEXITCODE itself; a caller's $true here would throw first.
$PSNativeCommandUseErrorActionPreference = $false
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

# r17 F2: the URL redaction rule (Get-RedactedRemoteUrl calls it). In any text, a 'scheme://<userinfo>@' part is replaced by 'scheme://***@' with the userinfo running
# to the LAST '@' before the path (an unencoded '@' in a password leaks nothing), then everything after a '?' or '#' of a URL is dropped and printed '?***'.
# r17 pass 18 A1: no character but whitespace ends either span (a quote in a password, 'p''ass', or in a query value, 'abc''def', used to stop the match and print the rest);
# in free text a URL ends at whitespace, so the userinfo span is every non-space character up to the last '@' of that word (over-redaction is the safe side).
# r17 pass 19 A3: a space does not end a URL either (a query value 'first SECOND_SECRET' quoted in git's error text): the userinfo span is everything up to the last '@'
# before the first '/' on that line, and after a URL's first '?' or '#' the REST OF THE LINE is dropped ('?***'). Only a line break ends a span.
# r26 (fable key3 r25 B1; HUB RULING fail closed): the ONE authority rule. A '://' url's authority (the text before its first '/') is parsed only when it is exactly
# [userinfo@]host[:port]: no '?', '#', '\' or second '@' anywhere in it, a host without ':' and a port of digits. Git and curl end the authority at the first '/', '?' or '#'
# (RFC 3986 section 3.2), so 'https://evil.example#@github.com/o/r' contacts evil.example: splitting it at the last '@' named github.com. Test-GitRemoteUrlSame keys any other
# authority by its raw string (byte-equal only) and the redactors print it as 'scheme://***', so a log line never names a host the url does not contact.
function Test-UrlAuthorityPlain([string]$Authority) { return ("$Authority" -match '^(?:[^@?#\\/]*@)?[^@?#\\/:]*(?::[0-9]*)?$') }

function ConvertTo-RedactedUrlText {
    param([string]$Text)
    # r26: in free text the authority span runs to the first '/' or line end, and stops before the next 'scheme://' (two urls on one line are judged apart). A span whose
    # userinfo part (before its last '@') holds '@', '?', '#' or '\' is not the authority Git contacts: it is printed 'scheme://***', never '***@<a host after it>'.
    $s = [regex]::Replace("$Text", '(?im)([a-z][a-z0-9+.\-]*://)((?:(?![a-z][a-z0-9+.\-]*://)[^/\r\n])*)', {
            param($m)
            $span = $m.Groups[2].Value; $at = $span.LastIndexOf('@')
            if ($at -ge 0 -and $span.Substring(0, $at) -match '[@?#\\]') { return $m.Groups[1].Value + '***' }
            return $m.Value
        })
    $s = [regex]::Replace($s, '(?im)([a-z][a-z0-9+.\-]*://)[^/\r\n]*@', '$1***@')
    return ([regex]::Replace($s, '(?im)([a-z][a-z0-9+.\-]*://[^\r\n?#]*)[?#][^\r\n]*', '$1?***'))
}

# MINI3-CUTOVER-CREDENTIAL-DETECTOR-DENY-BY-DEFAULT-1 S3: the secret PARTS of every url this run hands to git or prints. Git can echo a url WITHOUT its scheme (a file://
# clone that fails prints "fatal: '/C:/r.git?token=x' does not appear to be a git repository"), and ConvertTo-RedactedUrlText keys on 'scheme://', so it saw no url at
# all and the token reached the log and the console. Register-UrlSecretParts records, for each 'scheme://' url in a text: the text after its first '?' or '#' (to the end
# of the line), and the userinfo before the authority's last '@' when it holds a ':' (user:password: the whole userinfo and the password) or the scheme is http(s) (a token
# used as the user). Write-Log replaces every recorded part with '***' in every line, whatever framing it stands in.
# r1 adversary A4: git percent-DECODES a url before it echoes it ('?token=fake%2Dtoken' is printed '?token=fake-token'), so every part is recorded as written AND decoded.
# r1 adversary A9: a part WITH its delimiter ('?t=x', '#f', 'u:pw@') is recorded at any length (the delimiter keeps it from matching an ordinary word); a bare part (the
# query without its '?', a password alone) only from 6 characters on, so a short one never blanks ordinary words in the log.
# r1 adversary A5: Write-Log applies the scheme rule (ConvertTo-RedactedUrlText) FIRST and the recorded parts after it, so a recorded part that is also a scheme or a host
# ('?https:') can never break the url apart before the scheme rule has redacted it.
$script:UrlSecretParts = [System.Collections.Generic.List[string]]::new()
# r2 pass 4 A2 (defect class: a short secret piece dropped by a length filter): a piece that FOLLOWS a split ('abc' in '?t=%0Aabc') starts the line git prints after the
# break, or follows the mid-line byte. It is recorded as a HEAD at any length; Write-Log redacts a head (and the rest of its word) only where it starts a line or follows
# a redacted part, so a short head never blanks ordinary words elsewhere.
$script:UrlSecretHeads = [System.Collections.Generic.List[string]]::new()
$script:UrlSecretAnchors = [System.Collections.Generic.List[string]]::new()
$script:UrlRedactTimeout = [TimeSpan]::FromSeconds(2)   # r4 (adversary pass 9 A2): the match bound of every pattern rule in Write-Log
# r1 adversary pass 2 A2: a decoded part can hold a line break ('%0A': git's output is split there) or a byte that is not UTF-8 ('%FF'), so the part is also decoded
# byte by byte (Latin-1) and every piece between two characters that are not printable ASCII is recorded on its own (from 6 characters on).
function Add-UrlSecretPart([string]$Part, [int]$MinLength = 6) {
    $latin = [regex]::Replace("$Part", '%([0-9A-Fa-f]{2})', { param($m) [string][char][Convert]::ToInt32($m.Groups[1].Value, 16) })
    $forms = @("$Part", $(try { [Uri]::UnescapeDataString("$Part") } catch { "$Part" }), $latin)
    # r2 (pass 3 A5): a piece that still carries the part's own delimiter ('?t=x' before a decoded line break, 'u:pw@' after one) keeps the delimiter's minimum length
    $forms += @($forms | ForEach-Object { $_ -split '[^\x20-\x7E]+' } | Where-Object { $_.Length -ge 6 -or ($MinLength -lt 6 -and $_.Length -ge [Math]::Max(1, $MinLength) -and $_ -match '^[?#].|.@$') })
    foreach ($f in @($forms)) {
        $pieces = @($f -split '[^\x20-\x7E]+')
        for ($k = 1; $k -lt $pieces.Count; $k++) { $h = $pieces[$k].Trim(); if ($h -and -not $script:UrlSecretHeads.Contains($h)) { $script:UrlSecretHeads.Add($h) } }
    }
    foreach ($p in $forms) {
        if ($p.Length -ge [Math]::Max(1, $MinLength) -and -not $script:UrlSecretParts.Contains($p)) { $script:UrlSecretParts.Add($p) }
    }
}
function Register-UrlSecretParts {
    param([string]$Text)
    foreach ($m in [regex]::Matches("$Text", '(?i)([a-z][a-z0-9+.\-]*)://([^\r\n]*)')) {
        $rest = $m.Groups[2].Value
        $q = $rest.IndexOfAny([char[]]@('?', '#'))
        if ($q -ge 0) {
            Add-UrlSecretPart $rest.Substring($q + 1).TrimEnd(); Add-UrlSecretPart $rest.Substring($q).TrimEnd() 2
            # r2 pass 5 A2/A3 (defect class: matching the secret by its own spelling, which git may print differently): the text BEFORE the '?' or '#' (the path git echoes
            # without the scheme) is an ANCHOR, as written and decoded; Write-Log drops everything after an anchor's '?' or '#' to the end of the line, exactly as the
            # scheme rule does for a 'scheme://' url, whatever bytes, embedded urls or rewritten text follow.
            $anc = $rest.Substring(0, $q)
            # git prints a file:// path with its leading '/' ('/C:/r.git?x') or without it ('C:/r.git#x', the fragment form), and prints a '%XX' in the path as written,
            # decoded, as a replacement character or as the bare hex digits ('ABSENT%80' is printed 'ABSENT80', r2 pass 6 A2). The anchor is therefore a PATTERN: the
            # leading '/' optional, and each '%XX' any run of up to four characters.
            # r3 pass 7 A1 (defect class: the anchor guessed which characters a decoded '%XX' can print): git prints a decoded '%20' as a blank, '%27' and '%22' as quotes,
            # so a '%XX' stands for ANY run of up to four characters but a line break. A '%XX' that decodes to a control byte ('%0A', '%0D') can BREAK the line git prints:
            # the path after it starts a later line, so that tail is an anchor of its own at the start of a line (blanks allowed before it, as Invoke-Native indents).
            $core = $anc.TrimStart('/')
            if ($core.Length -ge 2) {
                $wild = '[^\r\n]{0,4}?'
                $bits = @([regex]::Split($core, '(%[0-9A-Fa-f]{2})'))
                $pats = @('/?' + ((@(for ($k = 0; $k -lt $bits.Count; $k += 2) { [regex]::Escape($bits[$k]) })) -join $wild))
                for ($k = 1; $k -lt $bits.Count; $k += 2) {
                    $byte = [Convert]::ToInt32($bits[$k].Substring(1), 16)
                    if ($byte -lt 0x20 -or $byte -eq 0x7F) { $pats += '(?m)^\s*' + ((@(for ($t = $k + 1; $t -lt $bits.Count; $t += 2) { [regex]::Escape($bits[$t]) })) -join $wild) }
                }
                foreach ($pat in $pats) { if (-not $script:UrlSecretAnchors.Contains($pat)) { $script:UrlSecretAnchors.Add($pat) } }
            }
            # r2 pass 4 A3 (defect class: redaction-order dependence): the scheme rule runs first and rewrites an EMBEDDED url ('&redirect=https://h/x?q=1' becomes
            # '...https://h/x?***'), so the whole registered part no longer matches. Everything up to and including each embedded 'scheme://' survives that rule unchanged,
            # so each such prefix is recorded too.
            foreach ($em in [regex]::Matches($rest.Substring($q), '(?i)[a-z][a-z0-9+.\-]*://')) {
                $pre = $rest.Substring($q, $em.Index + $em.Length)
                Add-UrlSecretPart $pre 2; Add-UrlSecretPart $pre.Substring(1)
            }
        }
        $sl = $rest.IndexOfAny([char[]]@('/', '?', '#'))
        $auth = $(if ($sl -ge 0) { $rest.Substring(0, $sl) } else { $rest })
        $at = $auth.LastIndexOf('@')
        if ($at -le 0) { continue }
        $ui = $auth.Substring(0, $at)
        $colon = $ui.IndexOf(':')
        if ($colon -ge 0) { Add-UrlSecretPart ($ui + '@') 2; Add-UrlSecretPart $ui; Add-UrlSecretPart $ui.Substring($colon + 1) }
        elseif ($m.Groups[1].Value -match '^(?i)https?$') { Add-UrlSecretPart ($ui + '@') 2; Add-UrlSecretPart $ui }
    }
}

# r4 (sol r3 A2, defect class: matching a secret by a spelling git may print differently). Git prints a decoded control byte as a line break and a high byte as itself, a
# replacement character or its bare hex digits ('?t=%0A%80qzv' gave a continuation line '80qzv' that no registered spelling matched), so no finite list of spellings is
# known to be complete. FAIL CLOSED instead: true when a text holds a 'scheme://' url that carries a secret part (a query, a fragment or userinfo before an '@') and whose
# text after the scheme, as written or with every '%XX' decoded byte by byte, holds any character outside printable ASCII. Invoke-Native (and the lease read) then
# withholds the child's whole output from the log and the console, printing only how many lines were withheld.
function Test-UrlSecretUnprintable([string]$Text) {
    foreach ($m in [regex]::Matches("$Text", '(?i)[a-z][a-z0-9+.\-]*://([^\r\n]*)')) {
        $rest = $m.Groups[1].Value
        $sl = $rest.IndexOfAny([char[]]@('/', '?', '#'))
        $auth = $(if ($sl -ge 0) { $rest.Substring(0, $sl) } else { $rest })
        if ($rest.IndexOfAny([char[]]@('?', '#')) -lt 0 -and $auth.IndexOf('@') -lt 0) { continue }
        $latin = [regex]::Replace($rest, '%([0-9A-Fa-f]{2})', { param($x) [string][char][Convert]::ToInt32($x.Groups[1].Value, 16) })
        if ($rest -match '[^\x20-\x7E]' -or $latin -match '[^\x20-\x7E]') { return $true }
    }
    # a literal line break inside the argument itself (the url continues on the next line of the text)
    return ("$Text" -match '(?i)[a-z][a-z0-9+.\-]*://[^\r\n]*[\r\n]')
}

function Write-Log {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'ERROR', 'MANUAL', 'OK')][string]$Level = 'INFO')
    # r17 F2: the one sink for the console and the setup log; no line it writes carries a URL's userinfo or query string, whichever caller printed the URL
    # (a remote-mismatch refusal, a native command line, git's own error text quoting a remote)
    $Message = ConvertTo-RedactedUrlText $Message
    # r4 (adversary pass 9 A2, defect class: attacker-shaped regex work in the sink): an anchor is a PATTERN built from a url ('a%61' x 128 gives 128 ambiguous wildcards,
    # exponential backtracking on the path git echoes). Every pattern rule below runs under a match timeout; a timeout withholds the WHOLE line (fail closed), never a
    # partly redacted one.
    try {
        # r2 pass 5 A2/A3: after the scheme rule (r1 A5 order), an anchor's '?' or '#' drops the rest of the line
        foreach ($a in @($script:UrlSecretAnchors | Sort-Object Length -Descending)) { $Message = [regex]::Replace($Message, '(' + $a + ')[?#][^\r\n]*', '$1?***', [System.Text.RegularExpressions.RegexOptions]::None, $script:UrlRedactTimeout) }   # $a is a pattern (Register-UrlSecretParts)
        # DENY-BY-DEFAULT-1 S3: then every registered url secret part, wherever it stands (git's echo of a url without its scheme included); longest first (r1 A5: after the scheme rule)
        foreach ($p in @($script:UrlSecretParts | Sort-Object Length -Descending)) { $Message = $Message.Replace($p, '***') }
        # r2 pass 4 A2: a recorded HEAD (and the rest of its word) where it starts the line or follows a redacted part
        foreach ($h in @($script:UrlSecretHeads | Sort-Object Length -Descending)) { $Message = [regex]::Replace($Message, '(^\s*|\*\*\*[^\s''"]*?)' + [regex]::Escape($h) + '[^\s''"]*', '$1***', [System.Text.RegularExpressions.RegexOptions]::None, $script:UrlRedactTimeout) }
    } catch {
        $tx = $_.Exception; $timedOut = $false
        while ($tx) { if ($tx -is [System.Text.RegularExpressions.RegexMatchTimeoutException]) { $timedOut = $true }; $tx = $tx.InnerException }
        if (-not $timedOut) { throw }
        $Message = '(line withheld: its url-secret redaction exceeded the time bound, fail closed)'
    }
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

# r17 F3 (sol B5; catalogue T-01; card SETUP-MLVWS-GIT-TRACE-SCRUB-1 folded here): the ONE route for a native child that may run git. Every GIT_TRACE* variable
# (GIT_TRACE, GIT_TRACE2, GIT_TRACE2_EVENT, GIT_TRACE2_PERF, GIT_TRACE_PACKET, GIT_TRACE_PERFORMANCE, GIT_TRACE_SETUP, GIT_TRACE_CURL, GIT_TRACE_PACK_ACCESS,
# GIT_TRACE_SHALLOW, GIT_TRACE_REFS, GIT_TRACE_FSMONITOR and any other GIT_TRACE* name) is removed from the process environment the child inherits, so a trace
# destination that names a tracked file is never appended to (Export promises it never modifies a source file); the parent's values are restored in a finally.
# [Environment] is used, not Remove-Item Env:, so -WhatIf cannot leave the variables in place. The script block's output (and $LASTEXITCODE) pass through unchanged.
# Its locals carry a '__gitTrace' prefix: the script block runs in a child of THIS scope (PowerShell scoping is dynamic), so a plain name ($k, $saved) would shadow a
# caller's variable of the same name inside the block.
function Invoke-GitTraceScrubbed {
    param([scriptblock]$__gitTraceBlock)
    $__gitTraceSaved = @{}
    foreach ($__gitTraceKey in @([Environment]::GetEnvironmentVariables('Process').Keys)) { if ("$__gitTraceKey" -match '^(?i)GIT_TRACE') { $__gitTraceSaved["$__gitTraceKey"] = [Environment]::GetEnvironmentVariable("$__gitTraceKey", 'Process') } }
    try {
        foreach ($__gitTraceKey in @($__gitTraceSaved.Keys)) { [Environment]::SetEnvironmentVariable($__gitTraceKey, $null, 'Process') }
        & $__gitTraceBlock
    } finally {
        foreach ($__gitTraceKey in @($__gitTraceSaved.Keys)) { [Environment]::SetEnvironmentVariable($__gitTraceKey, $__gitTraceSaved[$__gitTraceKey], 'Process') }
    }
}

function Invoke-Native {
    # Runs a native command, logs its output, throws on a non-zero exit unless -AllowFail.
    param([string]$Exe, [string[]]$Arguments, [switch]$AllowFail, [int[]]$OkCodes = @(0))
    # r10 pass 7 A5: a caller (or profile) with $PSNativeCommandUseErrorActionPreference = $true would make a non-zero exit throw before -AllowFail and
    # -OkCodes are consulted (robocopy's normal exit 1 included). This function owns exit-code handling, so it turns that off for its own scope.
    $PSNativeCommandUseErrorActionPreference = $false
    foreach ($a in @($Arguments)) { Register-UrlSecretParts $a }   # DENY-BY-DEFAULT-1 S3: before the command line and before anything the child echoes back
    Write-Log ("  > {0} {1}" -f $Exe, ($Arguments -join ' '))
    # r4 (sol r3 A2): an argument whose url secret holds a byte git may print in an unknown form: the child's output is withheld whole (fail closed)
    $withhold = @(@($Arguments) | Where-Object { Test-UrlSecretUnprintable $_ }).Count -gt 0
    $out = Invoke-GitTraceScrubbed { & $Exe @Arguments 2>&1 }   # r17 F3: no inherited GIT_TRACE* destination
    $code = $LASTEXITCODE
    if ($withhold) {
        $nOut = @(@($out) | Where-Object { "$_".Trim() }).Count
        if ($nOut -gt 0) { Write-Log ("    ({0} line(s) of output withheld: an argument carries a url secret with a byte outside printable ASCII, which the child may print in a form this log cannot redact)" -f $nOut) }
    } else {
        foreach ($o in @($out)) { if ("$o".Trim()) { Write-Log ("    {0}" -f "$o".TrimEnd()) } }
    }
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
# r22 (fable key3 r21 B2 = opus r21 H1; HUB RULING 20261117T1152Z rule 2): a configured value is read RAW. 'git config -z' ends each value with a NUL, and the bytes are taken
# off the child's stdout untouched: PowerShell's native-output line framing split a pushurl 'X<LF>X' into two urls and dropped a CR before the LF. Strict UTF-8 (a byte that is
# not UTF-8 is a failure, never a replacement character that two different values share). -Regexp reads 'git config -z --get-regexp' records ('<key><LF><value>').
# An unset key is an empty list; a failed read throws GIT_CONFIG_READ_FAILED (never an empty list).
function Get-GitConfigValuesRaw {
    param([string]$RepoDir, [string]$Key, [switch]$Regexp)
    $psi = [System.Diagnostics.ProcessStartInfo]::new('git')
    foreach ($a in @('-C', $RepoDir, 'config', '-z', $(if ($Regexp) { '--get-regexp' } else { '--get-all' }), $Key)) { $psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.CreateNoWindow = $true
    $proc = Invoke-GitTraceScrubbed { [System.Diagnostics.Process]::Start($psi) }   # r17 F3: the child inherits no GIT_TRACE* destination
    $ms = [IO.MemoryStream]::new()
    try {
        $errTask = $proc.StandardError.ReadToEndAsync()
        $proc.StandardOutput.BaseStream.CopyTo($ms)
        $proc.WaitForExit()
        $errText = "$($errTask.Result)".Trim(); $code = $proc.ExitCode
    } finally { $proc.Dispose() }
    if ($code -eq 1 -and $ms.Length -eq 0) { return [string[]]@() }   # the key is not set
    if ($code -ne 0) { throw "GIT_CONFIG_READ_FAILED git config $Key exited $code ($errText)" }
    $bytes = $ms.ToArray()
    if ($bytes.Length -eq 0 -or $bytes[$bytes.Length - 1] -ne 0) { throw "GIT_CONFIG_READ_FAILED git config -z $Key printed no NUL-terminated value" }
    try { $text = [Text.UTF8Encoding]::new($false, $true).GetString($bytes, 0, $bytes.Length - 1) } catch { throw "GIT_CONFIG_READ_FAILED git config $Key holds bytes that are not UTF-8" }
    return [string[]]$text.Split([char]0)
}

# r22: true when a remote url may not be compared or used at all: a control character (U+0000-U+001F or U+007F, CR and LF included) or a leading '-' (git would read it as an option)
function Test-RemoteUrlUnsafe([string]$Url) { return ("$Url" -match '[\x00-\x1F\x7F]' -or "$Url".StartsWith('-')) }

# r22: a remote url as a log line may show it: redacted (Get-RedactedRemoteUrl) and every control character printed as \xNN, so a CR or LF can never split or hide the line
function Format-RemoteUrlForLog([string]$Url) { return [regex]::Replace((Get-RedactedRemoteUrl $Url), '[\x00-\x1F\x7F]', { param($m) '\x{0:X2}' -f [int][char]$m.Value }) }

# r22 (HUB RULING 20261117T1152Z rules 1-2): remote <Name>'s urls, read raw ONCE. Fetch = the first remote.<name>.url (what git fetch uses); Push = every remote.<name>.pushurl, or
# every url when no pushurl is set (what git push uses). Rules = the url.<base>.insteadOf / pushInsteadOf prefixes: 'git remote get-url' used to apply them, a raw value does not,
# so a value one of them rewrites is refused by Get-RemoteUrlRefusal (Git would connect somewhere the check never saw).
function Get-RemoteUrlsRaw {
    param([string]$RepoDir, [string]$Name)
    $urls = @(Get-GitConfigValuesRaw -RepoDir $RepoDir -Key "remote.$Name.url")
    $push = @(Get-GitConfigValuesRaw -RepoDir $RepoDir -Key "remote.$Name.pushurl")
    if ($push.Count -eq 0) { $push = $urls }
    $ctx = Get-RemoteUrlContext -RepoDir $RepoDir
    return @{ Fetch = $(if ($urls.Count) { $urls[0] } else { '' }); Push = [string[]]$push; Rules = $ctx.Rules; Names = $ctx.Names }
}

# r23 (adversary pass 26 on ae45ed76, A1/A2): what Git applies to a url ARGUMENT before it connects, read raw. Rules = every url.<base>.insteadOf / pushInsteadOf prefix.
# Names = every configured remote name (the subsection of any remote.<name>.* key; r26: a legacy remotes/ or branches/ entry in the common or per-worktree git dir is
# no longer listed here but refused outright, LEASE_LEGACY_REMOTE_ALIAS_PRESENT): git fetch / ls-remote / push
# read their url argument as a remote NAME first, so a url string that is also a remote name would be resolved to that remote's url / pushurl, never the url that was checked.
# -RepoDir may be a directory outside any repository (an initial clone): the global, system and command-scope rules are read there and Names is then empty.
function Get-RemoteUrlContext {
    # r26: -RulesOnly is for an initial clone's probe directory (Get-CloneProbeDir), which need not be a repository: its callers use Rules only, so no Names census is taken
    param([string]$RepoDir, [switch]$RulesOnly)
    $rules = @(Get-GitConfigValuesRaw -RepoDir $RepoDir -Key '^url\..*\.(insteadof|pushinsteadof)$' -Regexp | ForEach-Object { $nl = $_.IndexOf("`n"); if ($nl -ge 0) { $_.Substring($nl + 1) } else { '' } })
    if ($RulesOnly) { return @{ Rules = [string[]]$rules; Names = [string[]]@() } }
    $names = [Collections.Generic.List[string]]::new()
    foreach ($rec in @(Get-GitConfigValuesRaw -RepoDir $RepoDir -Key '^remote\.' -Regexp)) {
        $k = "$rec"; $nl = $k.IndexOf("`n"); if ($nl -ge 0) { $k = $k.Substring(0, $nl) }
        $dot = $k.LastIndexOf('.'); if ($dot -gt 7) { $names.Add($k.Substring(7, $dot - 7)) }
    }
    # r26 (sol r25 B1; HUB RULING fail closed): Git reads legacy aliases from remotes/<name> and branches/<name> of the COMMON git dir, which in a linked worktree is not
    # '$RepoDir\.git' (a file there): the r23 census missed them and a url string could resolve as an alias while the guard compared the literal. Alias files are no longer
    # parsed: ANY entry under remotes\ or branches\ of the common or the per-worktree git dir refuses typed LEASE_LEGACY_REMOTE_ALIAS_PRESENT (move it aside to proceed).
    foreach ($gd in @(Get-GitDirsResolved -RepoDir $RepoDir)) {
        foreach ($legacy in 'remotes', 'branches') {
            $ld = Join-Path $gd $legacy
            if (-not (Test-Path -LiteralPath $ld)) { continue }
            $entry = $(if (Test-Path -LiteralPath $ld -PathType Container) { @(Get-ChildItem -LiteralPath $ld -Force -ErrorAction Stop) | Select-Object -First 1 } else { Get-Item -LiteralPath $ld -Force -ErrorAction Stop })
            if ($entry) { throw "LEASE_LEGACY_REMOTE_ALIAS_PRESENT $($entry.FullName) is a legacy remote alias entry (refused, never parsed: Git could resolve a url argument through it; move it aside and re-run)" }
        }
    }
    return @{ Rules = [string[]]$rules; Names = [string[]]$names.ToArray() }
}

# r26 (sol r25 B1): the per-worktree and the common git dir of -RepoDir, as Git resolves them ('git rev-parse --path-format=absolute --git-dir --git-common-dir').
# A failed resolution, an answer that is not exactly two lines, or a path that is not a directory throws LEASE_GIT_DIR_UNRESOLVED (never an empty list: the census refuses).
function Get-GitDirsResolved {
    param([string]$RepoDir)
    $out = @(Invoke-GitTraceScrubbed { & git -C $RepoDir rev-parse --path-format=absolute --git-dir --git-common-dir 2>$null })
    $code = $LASTEXITCODE
    $dirs = @($out | ForEach-Object { "$_".TrimEnd("`r") } | Where-Object { $_ -ne '' })
    if ($code -ne 0 -or $dirs.Count -ne 2) { throw "LEASE_GIT_DIR_UNRESOLVED git rev-parse --git-dir --git-common-dir in $RepoDir exited $code with $($dirs.Count) path(s) (the legacy remote alias census is refused)" }
    foreach ($d in $dirs) { if (-not (Test-Path -LiteralPath $d -PathType Container)) { throw "LEASE_GIT_DIR_UNRESOLVED git dir '$d' of $RepoDir is not a directory (the legacy remote alias census is refused)" } }
    return [string[]]$dirs
}

# r23 A2: the directory an initial clone's rules are read in: the nearest existing ancestor of the clone destination, else the user profile. A repository there only adds rules
# (and names), so the check can refuse more, never less, than the clone itself would rewrite.
function Get-CloneProbeDir([string]$Dest) {
    $d = Split-Path -Parent $Dest
    while ($d -and -not (Test-Path -LiteralPath $d -PathType Container)) { $d = Split-Path -Parent $d }
    return $(if ($d) { $d } else { $env:USERPROFILE })
}

# r22: '' when every url may be compared and used; otherwise the typed REMOTE_URL_UNSAFE reason (a control character, a leading '-', or a url rewrite rule that matches it)
# r23 A1: or a url string that is also the name of a configured remote (-Names; compared case-insensitively, so a case-folding file system can only refuse more)
function Get-RemoteUrlRefusal {
    param([string[]]$Urls, [string[]]$Rules, [string[]]$Names)
    foreach ($u in @($Urls)) {
        if (Test-RemoteUrlUnsafe $u) { return "REMOTE_URL_UNSAFE $(Format-RemoteUrlForLog $u) holds a control character or starts with '-' (refused, never compared)" }
        foreach ($r in @($Rules)) { if ("$u".StartsWith("$r", [StringComparison]::Ordinal)) { return "REMOTE_URL_UNSAFE $(Format-RemoteUrlForLog $u) is rewritten by a url.<base>.insteadOf/pushInsteadOf rule (refused: Git would not connect to the url that was checked)" } }
        foreach ($n in @($Names)) { if ([string]::Equals("$u", "$n", [StringComparison]::OrdinalIgnoreCase)) { return "REMOTE_URL_UNSAFE $(Format-RemoteUrlForLog $u) is also the name of a configured remote (refused: Git would resolve it as that remote, not as the url that was checked)" } }
    }
    return ''
}

function Get-HubLease {
    if (-not (Test-Path -LiteralPath (Join-Path $Repo '.git'))) { return @{ Error = "repo missing at $Repo" } }
    # r22 (sol r21 B1; HUB RULING 20261117T1152Z rule 1, check = use): the fetch url is read raw ONCE and the lease is read from that literal string, which is returned as Url;
    # no git call below resolves the remote NAME again (a configuration change in between can no longer move the read to another repository)
    try { $lr = Get-RemoteUrlsRaw -RepoDir $Repo -Name $LeaseRemote } catch { return @{ Error = "$($_.Exception.Message)" } }
    $url = $lr.Fetch
    if (-not $url) { return @{ Error = "remote '$LeaseRemote' has no url" } }
    $why = Get-RemoteUrlRefusal -Urls @($url) -Rules $lr.Rules -Names $lr.Names   # r23 A1: a url that is also a remote name is refused
    if ($why) { return @{ Error = $why } }
    Register-UrlSecretParts $url   # r4 (sol r3 A2 sibling): git's own error text may quote this url without its scheme; its secret parts are redacted in every later line
    $ls = Invoke-GitTraceScrubbed { & git -C $Repo ls-remote $url $LeaseRef 2>&1 }   # r17 F3: every native git call drops GIT_TRACE*
    if ($LASTEXITCODE -ne 0) {
        # r4 (sol r3 A2 sibling): the same fail-closed rule as Invoke-Native; the lines are joined here, so a continuation would not even start a line
        if (Test-UrlSecretUnprintable $url) { return @{ Error = "ls-remote failed (exit $LASTEXITCODE; its $(@($ls).Count) output line(s) withheld: the remote url carries a secret with a byte outside printable ASCII)" } }
        return @{ Error = "ls-remote failed: $($ls -join ' ')" }
    }
    $line = @($ls | Where-Object { "$_" -match '^[0-9a-f]{40}\s' }) | Select-Object -First 1
    if (-not $line) { return @{ Error = $null; Exists = $false; Sha = ''; Holder = ''; Data = $null; Url = $url } }
    $sha = ("$line" -split '\s+')[0]
    # r15 pass 14 A4 sibling: under -WhatIf the fetch (a write to the local lease ref and the object store) is not run; the lease is reported unread
    if ($WhatIfPreference) { return @{ Error = 'WhatIf: the lease content is not fetched (a dry run writes nothing)' } }
    Invoke-GitTraceScrubbed { & git -C $Repo fetch --quiet --no-tags $url "+${LeaseRef}:refs/hub-lease-local/mlv-app" 2>&1 } | Out-Null
    if ($LASTEXITCODE -ne 0) { return @{ Error = 'fetch of lease ref failed' } }
    $raw = Invoke-GitTraceScrubbed { & git -C $Repo show "${sha}:$LeaseFile" 2>&1 }
    if ($LASTEXITCODE -ne 0) { return @{ Error = "lease commit $sha has no $LeaseFile" } }
    try { $data = ($raw -join "`n") | ConvertFrom-Json } catch { return @{ Error = "lease JSON unreadable at $sha" } }
    return @{ Error = $null; Exists = $true; Sha = $sha; Holder = [string]$data.holder; Data = $data; Url = $url }
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
    $blob = ("$(Invoke-GitTraceScrubbed { & git -C $Repo hash-object -w --no-filters $tmp })").Trim()
    # A private temp index, not mktree on stdin: pwsh appends CRLF to piped native input, which would
    # land a carriage return in the tree entry name. The repo's real index and worktree are untouched.
    $idx = Join-Path $env:TEMP "hub-lease-index-$stamp"
    $oldIdx = $env:GIT_INDEX_FILE
    try {
        $env:GIT_INDEX_FILE = $idx
        Invoke-GitTraceScrubbed { & git -C $Repo update-index --add --cacheinfo "100644,$blob,$LeaseFile" }
        if ($LASTEXITCODE -ne 0) { throw 'update-index failed' }
        $tree = ("$(Invoke-GitTraceScrubbed { & git -C $Repo write-tree })").Trim()
    } finally {
        $env:GIT_INDEX_FILE = $oldIdx
        Remove-Item -LiteralPath $idx -ErrorAction SilentlyContinue
    }
    $msg = "hub-lease: holder=$(if ($Holder) { $Holder } else { '<released>' }) by $env:COMPUTERNAME"
    $commitArgs = @('-C', $Repo, '-c', "user.name=$GitName", '-c', "user.email=$GitEmail", 'commit-tree', $tree, '-m', $msg)
    if ($Current.Exists) { $commitArgs += @('-p', $Current.Sha) }
    $commitOut = @(Invoke-GitTraceScrubbed { & git @commitArgs 2>&1 })
    $commit = (@($commitOut | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join '').Trim()
    $commitErr = (@($commitOut | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" }) -join ' ').Trim()   # git's own reason, for the typed line
    # r17 pass 18 A4: every object the push publishes is proven first. A failed hash-object / write-tree / commit-tree (an empty -GitName, say: commit-tree exits 128
    # with empty stdout) used to leave '' here, and pushing ':<LeaseRef>' DELETES the lease branch while the log said the lease was acquired. Now it is typed and nothing is pushed.
    $objRx = '^[0-9a-f]{40}([0-9a-f]{24})?$'
    foreach ($o in @(@('hash-object', $blob), @('write-tree', $tree), @('commit-tree', $commit))) {
        if ("$($o[1])" -notmatch $objRx) { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue; throw "LEASE_OBJECT_FAILED git $($o[0]) gave no object id ('$($o[1])'$(if ($o[0] -eq 'commit-tree' -and $commitErr) { "; git: $commitErr" })); nothing was pushed, the lease ref is unchanged" }
    }
    if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue; throw "LEASE_OBJECT_FAILED git commit-tree exited $LASTEXITCODE; nothing was pushed, the lease ref is unchanged" }
    # r17 pass 19 A6: Get-HubLease reads the lease through the remote's FETCH url and a push goes to its PUSH url(s); a remote whose pushurl names another repository would
    # report the lease acquired while the fork stays unheld. Every push url must name the fetch url's repository, else LEASE_PUSH_URL_DIFFERS and nothing is pushed.
    # r22 (sol r21 B1, fable key3 r21 B2; HUB RULING 20261117T1152Z rules 1-2): the urls are read raw ONCE (Get-RemoteUrlsRaw). A value with a control character, a leading '-' or a
    # matching url rewrite rule is refused unread (REMOTE_URL_UNSAFE). The fetch url must be the very string the lease was read from ($Current.Url, set by Get-HubLease), and
    # every push url must name its repository. The push then goes to each CHECKED push url string, never to the remote name: git re-resolves a name, so a configuration change
    # after the check used to move the push to an unchecked repository while the lease was logged acquired.
    $refuse = { param($why) Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue; throw "LEASE_PUSH_URL_DIFFERS remote '$LeaseRemote' $why; nothing was pushed, the lease ref is unchanged" }
    $lr = $null
    try { $lr = Get-RemoteUrlsRaw -RepoDir $Repo -Name $LeaseRemote } catch { & $refuse "could not be read ($($_.Exception.Message))" }
    $leaseFetchUrl = $lr.Fetch; $leasePushUrls = @($lr.Push)
    $unsafeWhy = Get-RemoteUrlRefusal -Urls (@($leaseFetchUrl) + $leasePushUrls) -Rules $lr.Rules -Names $lr.Names   # r23 A1
    if ($unsafeWhy) { & $refuse "($unsafeWhy)" }
    if ($Current.ContainsKey('Url') -and "$($Current.Url)" -cne $leaseFetchUrl) { & $refuse "reads from $(Format-RemoteUrlForLog $leaseFetchUrl) now, but the lease was read from $(Format-RemoteUrlForLog $Current.Url)" }
    if (-not $leaseFetchUrl -or $leasePushUrls.Count -eq 0 -or @($leasePushUrls | Where-Object { -not (Test-GitRemoteUrlSame $_ $leaseFetchUrl) }).Count -gt 0) {
        & $refuse "reads from $(Format-RemoteUrlForLog $leaseFetchUrl) but pushes to $((@($leasePushUrls | ForEach-Object { Format-RemoteUrlForLog $_ })) -join ', ')"
    }
    $expect = if ($Current.Exists) { $Current.Sha } else { '' }
    foreach ($pushUrl in $leasePushUrls) {
        [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'push', '--porcelain', "--force-with-lease=${LeaseRef}:$expect", $pushUrl, "${commit}:$LeaseRef"))
    }
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    Write-Log "lease now holder='$Holder' commit=$commit at $((@($leasePushUrls | ForEach-Object { Format-RemoteUrlForLog $_ })) -join ', ')" 'OK'
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
        $have = (Invoke-GitTraceScrubbed { & py list 2>&1 }) -join "`n"
        if ($have -notmatch '3\.14') { Invoke-Step 'py install 3.14 (matches VIRTUAL-TEN python 3.14)' { [void](Invoke-Native -Exe 'py' -Arguments @('install', '3.14') -AllowFail) } }
    } else { Write-Log 'py launcher not on PATH yet; open a new shell and re-run -Phase Packages' 'WARN' }
    Update-SessionPath

    if (Get-Command npm -ErrorAction SilentlyContinue) {
        foreach ($pkg in '@anthropic-ai/claude-code', '@openai/codex') {
            $present = (Invoke-GitTraceScrubbed { & npm ls -g --depth 0 $pkg 2>&1 }) -join "`n"
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
# p5 A7: OpenSSH accepts '=' as well as whitespace between a keyword and its argument, and a quoted keyword. Every recognizer below uses these.
$SshdMatchKeywordRx = '^\s*"?Match"?(\s|=)'
$SshdIncludeKeywordRx = '^\s*"?Include"?(\s|=)'
# r10 A6: sshd treats these keywords as ONE option and the FIRST value wins: DSAAuthentication is PubkeyAuthentication, ChallengeResponseAuthentication and
# SkeyAuthentication are KbdInteractiveAuthentication. A rewrite of the canonical name alone leaves an earlier alias in force, and a Match block may
# re-open password / keyboard-interactive (yes or true) or close public-key (no or false) with any of the spellings.
$SshdAliases = @{
    'PubkeyAuthentication'         = @('DSAAuthentication')
    'KbdInteractiveAuthentication' = @('ChallengeResponseAuthentication', 'SkeyAuthentication')
}
$SshdPasswordYesRx = '^\s*"?(PasswordAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication|SkeyAuthentication)"?(\s+|\s*=\s*)"?(yes|true)\b'
$SshdPubkeyNoRx = '^\s*"?(PubkeyAuthentication|DSAAuthentication)"?(\s+|\s*=\s*)"?(no|false)\b'
function Set-SshdOption {
    param([string[]]$Lines, [string]$Name, [string]$Value)
    $matchIdx = [Array]::FindIndex($Lines, [Predicate[string]] { param($l) $l -match $SshdMatchKeywordRx })
    $end = if ($matchIdx -ge 0) { $matchIdx } else { $Lines.Count }
    # r10 A6: comment out every ACTIVE global alias line of this option, so the first effective value is the one written below.
    if ($SshdAliases.ContainsKey($Name)) {
        $aliasRx = '^\s*"?(' + ($SshdAliases[$Name] -join '|') + ')"?(\s|=)'
        for ($i = 0; $i -lt $end; $i++) {
            if ($Lines[$i] -match $aliasRx) { $Lines[$i] = '# replaced by Setup-MlvWorkstation (alias of ' + $Name + '): ' + $Lines[$i].Trim() }
        }
    }
    for ($i = 0; $i -lt $end; $i++) {
        if ($Lines[$i] -match "^\s*#?\s*`"?$Name`"?(\s|=)") { $Lines[$i] = "$Name $Value"; return , $Lines }
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
    $inc = [Array]::FindIndex($lines, [Predicate[string]] { param($l) $l -match $SshdIncludeKeywordRx })
    if ($inc -ge 0) { throw "sshd_config line $($inc + 1) ('$($lines[$inc].Trim())') includes other files, which this script does not inspect; inline them by hand and re-run -Phase Ssh. Nothing was changed." }
    $firstMatch = [Array]::FindIndex($lines, [Predicate[string]] { param($l) $l -match $SshdMatchKeywordRx })
    if ($firstMatch -ge 0) {
        for ($i = $firstMatch; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match $SshdPasswordYesRx) {
                throw "sshd_config line $($i + 1) ('$($lines[$i].Trim())') re-enables password authentication inside a Match block; key-only is the rule, so remove it by hand and re-run -Phase Ssh. Nothing was changed."
            }
            if ($lines[$i] -match $SshdPubkeyNoRx) {
                throw "sshd_config line $($i + 1) ('$($lines[$i].Trim())') disables public-key authentication inside a Match block; key-only is the rule, so remove it by hand and re-run -Phase Ssh. Nothing was changed."
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

    # r14 A6: SSH fails closed whatever throws (a Write-Log append included). From the moment this phase starts sshd until the key-only config has landed, a finally
    # that does not depend on logging stops sshd and takes it off automatic start; the hardening step sets the flag only after it succeeded.
    $script:SshdStartAttempted = $false
    $script:SshKeyOnlyLanded = $false
    try {
    Invoke-Step 'sshd and ssh-agent: automatic start, running' {
        $script:SshdStartAttempted = $true
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
                $script:SshKeyOnlyLanded = $true   # r14 A6: only now may sshd stay up
            } catch {
                # Fail closed (adversary A6): sshd was started and its firewall rule opened above with the stock config, which allows passwords.
                # If the hardening did not land, the service does not stay up on that config.
                try { Stop-Service sshd -Force -ErrorAction Stop; Write-Log 'sshd STOPPED because the key-only config did not land; fix the cause and re-run -Phase Ssh' 'ERROR' } catch { Write-Log "could not stop sshd after a failed hardening: $($_.Exception.Message)" 'ERROR' }
                throw
            }
        }
    } else { Write-Log "sshd_config not created yet ($cfg); start sshd once and re-run -Phase Ssh" 'WARN' }
    } finally {
        if ($script:SshdStartAttempted -and -not $script:SshKeyOnlyLanded) {
            # no logging before the stop: a log that cannot be written must not keep password-enabled sshd running (r14 A6)
            $stopErr = ''
            try { Stop-Service sshd -Force -ErrorAction Stop } catch { $stopErr = $_.Exception.Message }
            try { Set-Service sshd -StartupType Manual -ErrorAction Stop } catch { if (-not $stopErr) { $stopErr = $_.Exception.Message } }
            try {
                if ($stopErr) { Write-Log "could not fully stop sshd after the key-only config did not land: $stopErr" 'ERROR' }
                else { Write-Log 'sshd STOPPED and set to Manual start because the key-only config did not land; fix the cause and re-run -Phase Ssh' 'ERROR' }
            } catch { }
        }
    }
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
    foreach ($u in @($ForkUrl, $UpstreamUrl, $DoctrineUrl)) { Register-UrlSecretParts $u }   # DENY-BY-DEFAULT-1 S3: before any message names one of them
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Log 'CLONE_PREREQ_MISSING git missing: run -Phase Packages first' 'ERROR'; $script:Failures.Add('CLONE_PREREQ_MISSING git'); return }   # r15 pass 14 A5 sibling: a failure (exit 1)
    if (-not (Test-Path -LiteralPath (Join-Path $Repo '.git'))) {
        Invoke-Step "Clone MLV-App fork into $Repo (remote 'fork' = SuperDimensionalSpaceFortress, 'origin' = ilia3101, as on VIRTUAL-TEN)" {
            # r23 (adversary pass 26 A2 sibling): the clone url is checked against the url rewrite rules BEFORE the clone (a rewritten url would clone another repository)
            $cWhy = ''
            try { $cCtx = Get-RemoteUrlContext -RepoDir (Get-CloneProbeDir $Repo) -RulesOnly; $cWhy = Get-RemoteUrlRefusal -Urls @($ForkUrl) -Rules $cCtx.Rules } catch { $cWhy = "$($_.Exception.Message)" }
            if ($cWhy) { $script:Failures.Add("CLONE_REMOTE_MISMATCH $Repo fork"); throw "CLONE_REMOTE_MISMATCH $Repo clone url $ForkUrl is refused ($cWhy); nothing was cloned" }
            [void](Invoke-Native -Exe 'git' -Arguments @('-c', 'core.longpaths=true', 'clone', '--origin', 'fork', $ForkUrl, $Repo))
        }
    } else { Write-Log "present: $Repo" 'OK' }
    if (Test-Path -LiteralPath (Join-Path $Repo '.git')) {
        Invoke-Step 'MLV-App remotes, fetch, master tracks fork/master' {
            $remotes = @(Invoke-GitTraceScrubbed { & git -C $Repo remote })
            # r15 pass 13 A5: an EXISTING remote must name the intended repository (fork = $ForkUrl, origin = $UpstreamUrl). A remote that names another repository (the
            # two swapped, say) is refused typed CLONE_REMOTE_MISMATCH before anything is fetched or tracked; it is never silently accepted or rewritten.
            # r22 (sol r21 B1 siblings; HUB RULING 20261117T1152Z rules 1-2): each remote's urls are read raw once, a REMOTE_URL_UNSAFE value is a mismatch (never compared), and
            # the fetches below go to the CHECKED url string ($checked), never to the remote name that git would resolve again
            $checked = @{}
            foreach ($want in @(@('fork', $ForkUrl), @('origin', $UpstreamUrl))) {
                if ($remotes -notcontains $want[0]) { continue }
                $ru = $null; $readWhy = ''
                try { $ru = Get-RemoteUrlsRaw -RepoDir $Repo -Name $want[0] } catch { $readWhy = "$($_.Exception.Message)" }
                $have = $(if ($ru) { $ru.Fetch } else { '' })
                $haveWhy = $(if ($readWhy) { $readWhy } else { Get-RemoteUrlRefusal -Urls @($have) -Rules $ru.Rules -Names $ru.Names })
                if ($haveWhy -or -not (Test-GitRemoteUrlSame $have $want[1])) {
                    $script:Failures.Add("CLONE_REMOTE_MISMATCH $Repo $($want[0])")
                    # r15 pass 14 A2: the URL is printed with any user:password part redacted (the log and the console never carry a credential)
                    throw "CLONE_REMOTE_MISMATCH $Repo remote '$($want[0])' is $(Format-RemoteUrlForLog $have), expected $($want[1])$(if ($haveWhy) { " ($haveWhy)" }) (nothing was fetched; fix the remote by hand and re-run -Phase Clone)"
                }
                # r17 pass 19 A6 sibling: a push url (remote.<name>.pushurl) that names another repository is the same mismatch
                foreach ($pu in @($ru.Push)) {
                    $puWhy = Get-RemoteUrlRefusal -Urls @($pu) -Rules $ru.Rules -Names $ru.Names
                    if ($puWhy -or -not (Test-GitRemoteUrlSame $pu $want[1])) {
                        $script:Failures.Add("CLONE_REMOTE_MISMATCH $Repo $($want[0]) pushurl")
                        throw "CLONE_REMOTE_MISMATCH $Repo remote '$($want[0])' pushes to $(Format-RemoteUrlForLog $pu), expected $($want[1])$(if ($puWhy) { " ($puWhy)" }) (nothing was fetched; fix the push url by hand and re-run -Phase Clone)"
                    }
                }
                $checked[$want[0]] = $have
            }
            # r23 (adversary pass 26 A2): an ADDED remote's url gets the same full refusal as a configured one (rewrite rules and remote names, not only the character check),
            # BEFORE it is added; and every url about to be fetched is checked once more against the configuration as it is after the adds
            $ctx = $null
            try { $ctx = Get-RemoteUrlContext -RepoDir $Repo } catch { $script:Failures.Add("CLONE_REMOTE_MISMATCH $Repo fork"); throw "CLONE_REMOTE_MISMATCH $Repo url rewrite rules could not be read ($($_.Exception.Message)); nothing was fetched" }
            foreach ($add in @(@('fork', $ForkUrl), @('origin', $UpstreamUrl))) {
                if ($remotes -contains $add[0]) { continue }
                $addWhy = Get-RemoteUrlRefusal -Urls @($add[1]) -Rules $ctx.Rules -Names $ctx.Names
                if ($addWhy) { $script:Failures.Add("CLONE_REMOTE_MISMATCH $Repo $($add[0])"); throw "CLONE_REMOTE_MISMATCH $Repo remote '$($add[0])' url $($add[1]) is refused ($addWhy); it was not added and nothing was fetched" }
                [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'remote', 'add', $add[0], $add[1])); $checked[$add[0]] = $add[1]
            }
            try { $ctx = Get-RemoteUrlContext -RepoDir $Repo } catch { $script:Failures.Add("CLONE_REMOTE_MISMATCH $Repo fork"); throw "CLONE_REMOTE_MISMATCH $Repo url rewrite rules could not be read ($($_.Exception.Message)); nothing was fetched" }
            foreach ($k in @($checked.Keys)) {
                $useWhy = Get-RemoteUrlRefusal -Urls @($checked[$k]) -Rules $ctx.Rules -Names $ctx.Names
                if ($useWhy) { $script:Failures.Add("CLONE_REMOTE_MISMATCH $Repo $k"); throw "CLONE_REMOTE_MISMATCH $Repo remote '$k' url $(Format-RemoteUrlForLog $checked[$k]) is refused ($useWhy); nothing was fetched" }
            }
            [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'config', 'core.longpaths', 'true'))
            [void](Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'fetch', '--quiet', $checked['fork'], '+refs/heads/*:refs/remotes/fork/*'))
            # r20 sibling: the upstream (origin) fetch stays non-fatal by design (fork is the base), but its failure is now said, not discarded
            $originCode = Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'fetch', '--quiet', $checked['origin'], '+refs/heads/*:refs/remotes/origin/*') -AllowFail
            if ($originCode -ne 0) { Write-Log "CLONE_ORIGIN_FETCH_FAILED git fetch origin exited $originCode (non-fatal: fork is the base; re-run -Phase Clone when the upstream is reachable)" 'WARN' }
            # r20 (sol r19 INV-12; r18 OPEN A7): 'master tracks fork/master' is promised by this step, so a non-zero exit (1, or 128 'branch master does not exist') is a recorded
            # typed failure (the setup exits non-zero); it used to be discarded with -AllowFail and the run reported done / exit 0
            $trackCode = Invoke-Native -Exe 'git' -Arguments @('-C', $Repo, 'branch', '--set-upstream-to=fork/master', 'master') -AllowFail
            if ($trackCode -ne 0) {
                $script:Failures.Add("CLONE_TRACKING_FAILED $Repo")
                throw "CLONE_TRACKING_FAILED $Repo 'git branch --set-upstream-to=fork/master master' exited $trackCode (the local master does not track fork/master; create or check out master, then re-run -Phase Clone)"
            }
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $DoctrineRepo '.git'))) {
        Invoke-Step "Clone doctrine bus into $DoctrineRepo" {
            # r23 (adversary pass 26 A2 sibling): the doctrine clone url is checked against the url rewrite rules before the clone, like the MLV-App clone above
            $dcWhy = ''
            try { $dcCtx = Get-RemoteUrlContext -RepoDir (Get-CloneProbeDir $DoctrineRepo) -RulesOnly; $dcWhy = Get-RemoteUrlRefusal -Urls @($DoctrineUrl) -Rules $dcCtx.Rules } catch { $dcWhy = "$($_.Exception.Message)" }
            if ($dcWhy) { $script:Failures.Add("CLONE_DOCTRINE_REMOTE_MISMATCH $DoctrineRepo"); throw "CLONE_DOCTRINE_REMOTE_MISMATCH $DoctrineRepo clone url $DoctrineUrl is refused ($dcWhy); nothing was cloned" }
            $code = Invoke-Native -Exe 'git' -Arguments @('clone', $DoctrineUrl, $DoctrineRepo) -AllowFail
            if ($code -ne 0) { Write-Log 'doctrine bus clone failed (private repo?): run "gh auth login", then re-run -Phase Clone' 'MANUAL' }
        }
    } else {
        # r15 pass 13 A5: an existing doctrine checkout must BE the doctrine repository (its 'origin' names $DoctrineUrl); any other checkout is a typed failure
        # r22: read raw once (Get-RemoteUrlsRaw); a REMOTE_URL_UNSAFE fetch or push url is a mismatch, never compared
        $dr = $null; $dWhy = ''
        try { $dr = Get-RemoteUrlsRaw -RepoDir $DoctrineRepo -Name 'origin' } catch { $dWhy = "$($_.Exception.Message)" }
        $dHave = $(if ($dr) { $dr.Fetch } else { '' })
        if (-not $dWhy -and $dr) { $dWhy = Get-RemoteUrlRefusal -Urls @($dHave) -Rules $dr.Rules -Names $dr.Names }
        # r19 pass 21 A3: every PUSH url of that origin must name it too ('remote.origin.pushurl' naming another repository is the same mismatch)
        $dPush = $(if ($dr) { @($dr.Push) } else { @() })
        $dBadPush = @($dPush | Where-Object { (Get-RemoteUrlRefusal -Urls @($_) -Rules $dr.Rules -Names $dr.Names) -or -not (Test-GitRemoteUrlSame $_ $DoctrineUrl) })
        if (-not $dWhy -and (Test-GitRemoteUrlSame $dHave $DoctrineUrl) -and $dBadPush.Count -eq 0) { Write-Log "present: $DoctrineRepo" 'OK' }
        else {
            $dPushNote = $(if ($dBadPush.Count -gt 0) { " and pushes to $((@($dBadPush | ForEach-Object { Format-RemoteUrlForLog $_ })) -join ', ')" } else { '' })
            Write-Log "CLONE_DOCTRINE_REMOTE_MISMATCH $DoctrineRepo (origin is '$(Format-RemoteUrlForLog $dHave)'$dPushNote$(if ($dWhy) { "; $dWhy" }), expected $DoctrineUrl; it is left as it is: move it aside and re-run -Phase Clone)" 'ERROR'
            $script:Failures.Add("CLONE_DOCTRINE_REMOTE_MISMATCH $DoctrineRepo")
        }
    }
}

# r15 pass 14 A2: a remote URL as it may be printed: every 'scheme://<userinfo>@' part (a user, a user:password, a token) is replaced by 'scheme://***@'.
# r26 (fable key3 r25 B1): a second '@' (or a '?', '#' or '\') in the authority now prints the whole value as 'scheme://***' (Test-UrlAuthorityPlain); the r17 text below
# describes a plain authority.
# r17 F2 (opus key3 B2 = sol B3 = fable B2): the userinfo runs to the LAST '@' before the path ('https://alice:p@ssw0rd@github.com/...' prints 'https://***@github.com/...',
# not '***@ssw0rd@'), and everything after a '?' or '#' (a query-string token, a fragment) is dropped and printed '?***'. It works on any text (a log line, a
# git error that quotes the URL), so Write-Log applies it to every line it writes. An scp-like 'git@github.com:o/r.git' has no scheme and no secret; it is unchanged.
function Get-RedactedRemoteUrl {
    param([string]$Url)
    Register-UrlSecretParts $Url   # DENY-BY-DEFAULT-1 S3: a url printed once is redacted in every later line too (git may echo it without its scheme)
    # r26 (fable key3 r25 B1): the authority rule (Test-UrlAuthorityPlain). An authority that is not [userinfo@]host[:port], or an '@' anywhere after the authority (path,
    # query or fragment), prints as 'scheme://***' and nothing more: no host the url does not contact, and no tail of a password that held a '/'.
    $mA = [regex]::Match("$Url", '(?is)^(\s*[a-z][a-z0-9+.\-]*://)(.*)$')
    if ($mA.Success) {
        $rest = $mA.Groups[2].Value; $sl = $rest.IndexOf('/')
        $auth = $(if ($sl -ge 0) { $rest.Substring(0, $sl) } else { $rest })
        if (-not (Test-UrlAuthorityPlain $auth) -or ($sl -ge 0 -and $rest.Substring($sl).Contains('@'))) { return ($mA.Groups[1].Value + '***') }
    }
    # r17 pass 18 A1: the value is ONE URL, so a space in it ends nothing: userinfo runs to the last '@' of the whole value, and everything after its first '?' or '#' goes
    $u = [regex]::Replace("$Url", '(?is)^(\s*[a-z][a-z0-9+.\-]*://).*@', '$1***@')
    $u = [regex]::Replace($u, '(?s)^([^?#]*)[?#].*$', '$1?***')
    return (ConvertTo-RedactedUrlText $u)   # the one free-text rule, defined beside Write-Log (it runs before this point of the script is reached)
}

# r15 pass 13 A5: true when two git remote URLs name the same repository (r20: exactly; see below). An empty or unparseable URL never matches.
#
# r20 (sol r19 INV-1; hub ruling): the lease reads one repository and pushes to another when this says two URLs are the same, so equality is EXACT on the repository: the PATH
# compares ordinally (case-sensitive) and a '.git' suffix or a trailing slash is significant. The host name (case-insensitive) is normalised and the ssh user is kept.
# r21 (sol r20 B1; hub ruling 20261117T0822Z, which withdrew the r20 scp = ssh:// control): an ssh URL is normalised to (user, host, the server path AS GIT SENDS IT to
# git-upload-pack). scp-like 'host:/p' is the absolute '/p' and equals 'ssh://host/p'; scp-like 'host:p' and 'host:~/p' are home-relative '~/p' and equal 'ssh://host/~/p'
# but NOT 'ssh://host/p' (Git sends 'p' for the first and '/p' for the second: two repositories). A spelling that cannot be normalised with certainty (another scheme, a port,
# a '~user' path, a query) compares UNEQUAL unless the two strings are identical: the lease guard then refuses.
# r22: deny by default (see the ssh branches): only a plain user, host and path alphabet is normalised; scp 'host:/~/p' and 'host:a~b/p' (Git strips them to '~' forms) are raw.
# Two documented exceptions: github.com (case-insensitive owner/repo, '.git' optional, https and 'git@' ssh the same repository) and a Windows drive or UNC local path (NTFS
# is case-insensitive; separators and a trailing slash do not change it). Any other local path (a POSIX path, 'file://') is ordinal.
function Test-GitRemoteUrlSame {
    param([string]$A, [string]$B)
    $norm = {
        param($u)
        $s = "$u"
        # r21 pass 25 A4: whitespace around a URL is part of what Git sends ('repos/lease.git ' is not 'repos/lease.git'), so it is never trimmed away; compared only as the identical string
        if ($s -match '^\s|\s$|[\r\n]') { return "raw:$s" }   # r21 pass 26 A3: a CR or LF anywhere in it counts too
        $scheme = ''; $user = ''; $hostName = ''; $path = ''
        # r22 (fable key3 r21 B1 + H1, opus r21 H2; HUB RULING 20261117T1152Z rule 3): DENY BY DEFAULT in both ssh branches. A spelling is normalised only when the user is
        # [A-Za-z0-9._-]+ (no ':'), the host is [A-Za-z0-9.-]+ (no port, no IPv6 literal) and the path is [A-Za-z0-9._/-]+ after exactly one of: '/~/' (ssh://host/~/p), '~/' or
        # nothing (scp home-relative), or a '/' NOT followed by '~'. Anything else ('~user', a second-character '~' that Git strips to a '~user' form, '%', '?', a port, a user
        # with ':' that Git's first-colon scp split reads as the host...) is uncertain and keyed by its raw string: equal only to the identical spelling, so the guard refuses.
        $plainUser = '^[A-Za-z0-9._-]+$'; $plainHost = '^[A-Za-z0-9.-]+$'; $plainPath = '^[A-Za-z0-9._/-]+$'
        $mUrl = [regex]::Match($s, '^(?i)(?!file://)([a-z][a-z0-9+.\-]*)://([^/]*)(.*)$')
        # scp-like as Git splits it: no '://', the FIRST ':' at index 2 or later (index 1 is a Windows drive, 'C:/repos'), no '/' before it, and not 'file:'
        $colon = $s.IndexOf(':'); $slash = $s.IndexOf('/')
        $isScp = (-not $mUrl.Success) -and $colon -ge 2 -and ($slash -lt 0 -or $slash -gt $colon) -and -not $s.Contains('://') -and $s -notmatch '^(?i)file:'
        if ($mUrl.Success) {
            $scheme = $mUrl.Groups[1].Value.ToLowerInvariant(); $auth = $mUrl.Groups[2].Value; $path = $mUrl.Groups[3].Value
            # r26 (fable key3 r25 B1; HUB RULING fail closed): every scheme's authority must be exactly [userinfo@]host[:port] (Test-UrlAuthorityPlain: no '?', '#', '\' or
            # second '@'), else the url is keyed raw. 'https://evil.example#@github.com/o/r' used to split at the last '@' and key as the GitHub fork while Git contacts evil.example.
            if (-not (Test-UrlAuthorityPlain $auth)) { return "raw:$s" }
            $at = $auth.LastIndexOf('@')
            if ($at -ge 0) { $user = $auth.Substring(0, $at); $hostName = $auth.Substring($at + 1) } else { $hostName = $auth }
            if ($scheme -eq 'ssh') {
                if ($user -cnotmatch $plainUser -or $hostName -cnotmatch $plainHost) { return "raw:$s" }
                # ssh://host/~/p is home-relative '~/p' (Git strips the slash); ssh://host/p is absolute '/p'
                if ($path.StartsWith('/~/') -and $path.Substring(3) -cmatch $plainPath) { $path = $path.Substring(1) }
                elseif ($path.Length -gt 1 -and $path[0] -eq '/' -and $path[1] -ne '~' -and $path.Substring(1) -cmatch $plainPath) { }
                else { return "raw:$s" }
            }
        } elseif ($isScp) {
            # r21 B1 (sol r20): the server path is what Git sends to git-upload-pack. scp-like 'host:/p' is the absolute '/p'; 'host:p' and 'host:~/p' are both home-relative
            # and are kept as '~/p', NEVER given a leading '/' (that made 'host:p' equal to the absolute 'ssh://host/p').
            $scheme = 'ssh'; $before = $s.Substring(0, $colon); $sp = $s.Substring($colon + 1)
            $mB = [regex]::Match($before, '^([^@]*)@(.*)$')
            if (-not $mB.Success) { return "raw:$s" }
            $user = $mB.Groups[1].Value; $hostName = $mB.Groups[2].Value
            if ($user -cnotmatch $plainUser -or $hostName -cnotmatch $plainHost) { return "raw:$s" }
            if ($sp.Length -gt 1 -and $sp[0] -eq '/' -and $sp[1] -ne '~' -and $sp.Substring(1) -cmatch $plainPath) { $path = $sp }
            elseif ($sp.StartsWith('~/') -and $sp.Substring(2) -cmatch $plainPath) { $path = $sp }
            elseif ($sp -cmatch $plainPath -and $sp[0] -ne '/') { $path = '~/' + $sp }
            else { return "raw:$s" }
        } else {
            # r19 pass 21 A2: a LOCAL path names a directory exactly: '.git', '?x' and '#x' are part of it, so 'C:/repos/lease' and 'C:/repos/lease.git' are two repositories.
            if ($s -match '^(?:[a-zA-Z]:[\\/]|\\\\)') { return (($s -replace '\\', '/') -replace '/+$', '').ToLowerInvariant() }   # a drive or UNC path: separators, case, trailing slash
            return ($s -replace '/+$', '')   # r20: any other local path is ordinal
        }
        $hostName = $hostName.ToLowerInvariant()
        if (-not $hostName -or $hostName -match '[\s]') { return '' }
        # r21 B1 / pass 25 A3 (a '~user' path, a port, an IPv6 literal, a '%' that Git decodes for ssh:// only) are all outside the r22 plain alphabet above, so an ssh spelling
        # that reaches this point was mapped with certainty
        # github.com: documented case-insensitive and '.git'-optional; https (any userinfo is a token) and the 'git@' ssh forms name one repository
        if ($hostName -ceq 'github.com' -and (($scheme -in @('http', 'https')) -or ($scheme -in @('ssh') -and $user -ceq 'git'))) {
            $gp = (($path -replace '[?#].*$', '') -replace '^~/', '').Trim('/') -replace '(?i)\.git$', ''
            if (-not $gp) { return '' }
            return ('https://github.com/' + $gp).ToLowerInvariant()
        }
        if (-not $path -or $path -eq '/' -or $path -eq '~/') { return '' }
        # r21 B1: the key keeps the two path kinds apart: an absolute path starts with '/', a home-relative one with '~/' (they never compare equal)
        if ($scheme -eq 'ssh') { return "ssh://$user@$hostName|$path" }
        if ($scheme -in @('http', 'https')) { return "${scheme}://$hostName$path" }   # a user / token part does not change which repository an https URL names
        return "${scheme}://$user@$hostName$path"   # any other scheme: kept whole (user included); equal only when spelled identically
    }
    $x = & $norm $A; $y = & $norm $B
    return ($x.Length -gt 0 -and $x -ceq $y)
}

# ---------------------------------------------------------------------------------------------
# Phase: Qt (the repo pins Qt 6.10.2 win64_mingw + MinGW 13.1 via hash-locked aqtinstall)
# ---------------------------------------------------------------------------------------------
function Invoke-PhaseQt {
    $qmake = Join-Path $QtRoot "$QtVersion\mingw_64\bin\qmake.exe"
    $make = Join-Path $MingwDir 'bin\mingw32-make.exe'
    if ((Test-Path -LiteralPath $qmake) -and (Test-Path -LiteralPath $make)) { Write-Log "present: Qt $QtVersion and MinGW 13.1" 'OK'; return }
    $req = Join-Path $Repo '.github\requirements\aqtinstall.txt'
    if (-not (Test-Path -LiteralPath $req)) { Write-Log "QT_PREREQ_MISSING missing ${req}: run -Phase Clone first" 'ERROR'; $script:Failures.Add("QT_PREREQ_MISSING $req"); return }   # r15 pass 14 A5 sibling: a failure (exit 1)
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
# r16 (adversary pass 16 A1 sibling): a separator RUN ('[_.\-]*') may stand inside a compound name word, as the classifier allows (its role guards also call
# Test-CredentialNamePresent, which asks the classifier itself; this regex is never the narrower of the two)
$CredentialNameWords = 'SECRET|PASS[_.\-]*W(?:OR)?D|PRIVATE[_.\-]*KEY|API[_.\-]*KEY|ACCESS[_.\-]*KEY|AUTH[_.\-]*KEY|TOKEN|CREDENTIAL'
# r14 (hub ruling on adversary pass 10 A1-A3; replaces the r3..r12 per-format regex set: p5 A1, r10 A1, r11 S-B1, r12 A1/A2, r12 pass 9 B1/B2).
# Every pass since r10 found three more spellings of the same detector, so the per-format patterns (a quoted name/value pattern, an unquoted '=' pattern,
# two YAML patterns, two flow patterns, two netrc patterns, an npmrc pattern) are gone. There is ONE shape-agnostic rule, Get-CredentialKeyValueHitIndex:
#   a text is refused when a SECRET-NAMED KEY is followed by ANY NON-EMPTY VALUE, in
#     - a line form:  'k=v', 'k = v', 'k: v', 'set k=v', '"k": "v"', 'k = """v"""' (TOML triple quotes, across lines);
#     - a block form: 'k:' then the next non-blank, non-comment line, at any indent, with any number of comment and blank lines between ('password:\n  hunter2',
#                     'password: |\n  hunter2', 'password: # note\n  hunter2');
#     - a flow form:  '{k: v}', '{a: 1, k: v}', 'svc: { k: v }', and across line breaks ('{password:\n hunter2}');
#   plus a netrc record ('machine h login u password v', the same across lines, with any number of comment lines between; Get-CredentialNetrcHitIndex).
# A secret-named key is any key whose letters (case-insensitive, any separator: '_', '-', '.', camelCase) say password / passwd / pass_word, secret,
# credential, api_key / apikey, private_key, access_key, auth_key, token, or that has a part 'auth', 'authorization' or 'pwd' ('_auth', 'x-auth', 'AuthHeader';
# not 'author').
# r15 (hub ruling on adversary pass 12 A1-A3; doctrine bus 0fc7511 'spelling-chase-scanner-needs-a-deny-by-default-name' and d6d115b 'scanner-narrowing-needs-a-
# generated-must-refuse-sweep'): DENY BY DEFAULT on the key NAME. Five passes in a row drew three new spellings each, because r14 still STRIPPED the value
# (quotes, braces inside a sequence, comments, CDATA) before its exemption test and so judged a value that was not the one written. Now:
#   1. a text is refused when a secret-named key appears in it with ANY value. The value is the RAW text from the separator to the end of the record: the line form
#      to the end of the line; the block form ('k:' alone) through the next non-blank, non-comment line plus every following more-indented line; a flow
#      mapping / sequence element up to its ',' or the enclosing close; an XML element up to its matching close tag. A quoted run or a container is followed
#      across line breaks to its close. Nothing is stripped before the exemption test.
#   2. the ONLY exemptions (Test-CredentialValueExempt) are a closed literal list compared against that whole raw value after trimming outer whitespace only:
#      the empty string, '""', "''", 'null', '~', '[]', '{}', '***', an anchored '${NAME}' env reference and an anchored '<words>' placeholder; for XML an element
#      with no content at all. Anything else refuses: '[]hunter2', '""hunter2', '"x"', 'false', '4096' under 'max_tokens', '{ type: x }' under 'auth', and a
#      script variable assigned from code ('$Token = Get-Token'). Over-refusal is accepted (a dropped file is named in SECRET-SCAN.txt).
#   3. netrc: a netrc-shaped text in which 'password' / 'passwd' is followed by ANY further token refuses (no record tokenizer).
#   4. tools/workstation/test-workstation-state.ps1 carries a GENERATED must-refuse sweep over every wrapper combination and form.
#   5. MINI3-CUTOVER-CREDENTIAL-DETECTOR-DENY-BY-DEFAULT-1: a key NAME is judged as a reader decodes it. The second pass decodes every escape form a reader decodes in a
#      key (ConvertFrom-CredentialEscapes: YAML/JSON/TOML, XML character references, PowerShell backtick, octal, Python \N{}, URL %NN), and a key that still holds an
#      escape it could not decode is secret-named (deny by default). An XML element whose key= / name= attribute VALUE is secret-named and that carries any other
#      non-exempt attribute or content is a hit (Get-CredentialXmlAttrPairHitIndex). The generated sweep covers every escape form and the attribute pair.
#      r2: the second pass reads ONE normal form (Get-CredentialNormalForm: the whole text decoded to a fixed point, with a mask of decoded characters that keeps a
#      decoded value off the exemption list); an XML element's content runs to its depth-matched close tag.
#   NOT covered: a key name built from pieces at run time ('$k = "pass" + "word"'), a value produced by run-time evaluation, and a secret under a key whose name
#   says nothing (only the known token shapes in $SecretPatterns catch those).
# The r14 exemption list that r15 retires (kept here as the record of what changed): an unquoted boolean / null word (true, false, none, nil, undefined); a TOKEN
# key's all-digit count ('max_tokens = 4096'); a mapping value ('auth: { type: x }'); a script variable assigned from code ('$Token = Get-Token'); a quoted placeholder
# ('"${NAME}"'); three or more asterisks ('****'). Quoting never ends a value: text after a closing quote is part of the value ('""hunter2', '"x"y').
# A prose line such as 'Password: see the owner' is a hit by design (fail closed: a false positive costs one dropped file named in SECRET-SCAN.txt; a miss ships a
# secret). The detector is never narrowed to make a staged file pass (r11 F-B1). Every guard site (the Export stage scan and its typed EXPORT_CREDENTIAL_HIT line,
# the archive re-check, the Import bundle and host guards, the locked copy, the settings.json rewrite and the backup copy) calls Test-CredentialText /
# Get-CredentialHitLine, which call this rule and nothing else besides the known token shapes below.
# r15 pass 13 A2: between the name and its separator up to three CLOSING characters may stand (a quote, ']', ')', '}'), so a subscripted or braced script key is a key
# too: '${env:DB_PASSWORD} = ''x''', '$config[''password''] = ''x''', '$c["token"]="x"', '(password) = x'. (Blanks may stand before and after them.)
# r15 pass 14 A1: any whitespace, line breaks included, may stand between the name (and its closers) and the separator ('{"password"\n:"hunter2"}' is valid JSON).
# r16 (adversary pass 16 A1): the candidate name list is built from the classifier's own words with a RUN of separators ('[_.\-]*') allowed between ANY two letters,
# because Get-CredentialKeyClass strips every '_', '.' and '-' before it decides ('api__key', 'private..key', 'pass--word' are strong there). The old candidate
# regexes allowed at most one separator, and only at one place, so such a key was never even offered to the classifier. A wider candidate set is always safe: the
# classifier still decides every candidate ('a_u_t_h' is offered and rejected, as 'author' is).
# r16 (adversary pass 16 A2): any number of CLOSING characters may stand between the name and its separator ('$c[(("password"))] = "x"' has four); the r15 bound of
# three let a fourth closer hide the key. The run is linear (each step needs one closer), so it cannot backtrack catastrophically.
$CredentialNameCore = (@('password', 'passwd', 'secret', 'credential', 'apikey', 'privatekey', 'accesskey', 'auth', 'token', 'pwd') | ForEach-Object { (@($_.ToCharArray()) | ForEach-Object { [string]$_ }) -join '[_.\-/\\]*' }) -join '|'   # r19 pass 22 A1: '/' and '\' separate letters as '_' does ('api/key')
# r19 pass 21 A1: a '/' or '\' may stand inside a key name ('password/primary', 'vault\secret'): the name run is the classifier's own separators plus those two
$CredentialKeyPattern = '(?i)(?<![A-Za-z0-9_.\-/\\])([A-Za-z0-9_.\-/\\]*?(?:' + $CredentialNameCore + ')[A-Za-z0-9_.\-/\\]*)((?:\s*["''\]\)\}])*)\s*(:|=(?!=))'
$script:CredentialKeyRegex = [regex]::new($CredentialKeyPattern)
# r15 (hub ruling on adversary pass 12 A1-A3, DENY BY DEFAULT on the key name): the value span regexes, the quoted-run regexes and the netrc record tokenizer of
# r14 are gone (they stripped quotes, braces, comments and CDATA before the exemption test). What is left is the CLOSED exemption list, compared against the whole
# RAW value after trimming outer whitespace only (Test-CredentialValueExempt), and the two netrc regexes (Get-CredentialNetrcHitIndex).
$script:CredentialExemptLiterals = @('""', "''", 'null', '~', '[]', '{}', '***')
# r15 pass 13 A1: every line separator form, rewritten to one '\n' at the detector's entry (Get-CredentialHitLine)
$script:CredentialLineBreakRegex = [regex]::new("\r\n|\r|\u0085|\u2028|\u2029")
$script:CredentialPlaceholderRegex = [regex]::new('^(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|<[A-Za-z_ -]+>)$')
# a netrc-shaped text: it carries a whitespace-delimited netrc keyword that opens or names a record ('machine', 'default', 'login', 'macdef')
# r25 (adversary pass 29 A1): a netrc reader (Python's netrc, curl) accepts a double-quoted token and a backslash inside a token, so 'login alice "password" hunter2' and
# 'password' written 'pass\word' are the same keywords. $q is the run of quote and backslash characters allowed around and between the letters of a keyword.
$script:NetrcQ = '[\\"'']*'
function Get-NetrcKeywordPattern([string]$Word) { return (($Word.ToCharArray() | ForEach-Object { [regex]::Escape([string]$_) }) -join $script:NetrcQ) }
$script:CredentialNetrcShapeRegex = [regex]::new('(?i)(?<!\S)' + $script:NetrcQ + '(?:' + ((@('machine', 'default', 'login', 'macdef') | ForEach-Object { Get-NetrcKeywordPattern $_ }) -join '|') + ')' + $script:NetrcQ + '(?!\S)')
# in a netrc-shaped text, the token 'password' (or 'passwd') followed by ANY further token (any line, comments included) is a credential
$script:CredentialNetrcPasswordRegex = [regex]::new('(?i)(?<!\S)' + $script:NetrcQ + '(?:' + (Get-NetrcKeywordPattern 'password') + '|' + (Get-NetrcKeywordPattern 'passwd') + ')' + $script:NetrcQ + '(?!\S)\s+\S')
# Files that hold credentials by ROLE on the host that receives a bundle, whatever else they contain (Import never replaces one that carries a credential).
$CredentialRoleFiles = @('settings.json', 'settings.local.json', 'config.toml', '.mcp.json', 'mcp.json', 'mcp_settings.json', 'hosts.yml', '.npmrc', '.netrc', '.git-credentials', 'credentials')
# Known token SHAPES: a secret by its own form, whatever key (if any) it stands under. These are not spellings of the key/value rule above.
$SecretPatterns = @(
    'sk-ant-[A-Za-z0-9_\-]{20,}', 'sk-proj-[A-Za-z0-9_\-]{20,}', 'sk-[A-Za-z0-9]{40,}',
    'gh[pousr]_[A-Za-z0-9]{30,}', 'github_pat_[A-Za-z0-9_]{30,}', 'xox[abprs]-[A-Za-z0-9\-]{10,}',
    '-----BEGIN [A-Z ]*PRIVATE KEY-----', '"(access|refresh|id)_?[Tt]oken"\s*:\s*"[^"]{16,}"', 'AKIA[0-9A-Z]{16}',
    # r14: a URL that carries a user AND a password ('https://alice:hunter2@example.org', the .git-credentials shape)
    # r19 pass 22 A3: an apostrophe is a legal password character ('https://alice:p''ass@host/'), so only '"', '<', '>' and blanks end the password run
    # r20 pass 23 A2: the same holds for the user part ('https://o''brien:hunter2@host/')
    # r25 pass 30 A3: a double quote (raw, or '\"' inside a JSON string) is a legal user or password character too ('https://alice:p"ass@host/'): git's own parser takes it
    '(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@<>]+:[^\s/@<>]+@'
)
# r14: the XML element form of the key/value rule ('<password>hunter2</password>', '<DbPassword>x</DbPassword>'); the attribute form 'password="x"' is the line form.
# r15 (pass 12 A2): the element's RAW content, from just after its opening tag to its matching closing tag (or to the end of the text when it is never closed), is
# its value. Nothing is removed first: a comment, a CDATA section ('<![CDATA[<!--hunter2-->]]>') or a child element is content, so it is never empty. This regex
# finds the OPENING tag of a secret-named element (Get-CredentialXmlHitIndex finds its close); a self-closing tag ('<password/>') has no content at all.
# r16 (adversary pass 16 A1 sibling): the same separator-run name list ($CredentialNameCore) as the key/value rule ('<api__key>x</api__key>')
# r1 adversary A6 sibling: a namespace prefix (or any name part) may hold non-ASCII name characters ('<e:password>' with an accented 'e'); only what ends a name is excluded
# r4 (adversary pass 9 A3, defect class: a quote-unaware tag tail): the tail of a start tag skips QUOTED attribute values whole, so '<password note="/>">hunter2</password>'
# ends at its real '>' and is not read as self-closing. A quoted value never holds a '<' (XML forbids it; an unmatched quote is one character), and the loop is atomic, so the
# tail is linear in the tag's length.
$script:CredentialXmlTagTail = '(?>(?:[^<>"'']|"[^"<]*"|''[^''<]*''|["''])*)>'
$script:CredentialXmlOpenRegex = [regex]::new('(?i)<(?![!?/])([^\s<>/="'']*?(?:' + $CredentialNameCore + ')[^\s<>/="'']*)(?=[\s/>])' + $script:CredentialXmlTagTail)
$script:SecretShapeRegex = [regex]::new(($SecretPatterns -join '|'))

# '' = not a secret-named key; 'strong' = the key says its value is a secret; 'count' = a TOKEN key that names a count ('max_tokens').
# r15: both non-empty classes are secret-named and meet the SAME closed exemption list; a count key's number ('max_tokens = 4096') is no longer exempt (accepted
# over-refusal: the file is dropped and named in SECRET-SCAN.txt). The class is kept so the report and a later narrowing can still tell the two apart.
function Get-CredentialKeyClass {
    param([string]$Key)
    $flat = ($Key -replace '[_.\-/\\]', '').ToLowerInvariant()   # r19 pass 21 A1: '/' and '\' separate a name's parts too
    if ($flat -match 'password|passwd|secret|credential|apikey|privatekey|accesskey|authkey') { return 'strong' }
    $parts = @(($Key -creplace '(?<=[a-z0-9])(?=[A-Z])', '_').ToLowerInvariant() -split '[_.\-/\\]+' | Where-Object { $_ })
    foreach ($p in $parts) { if ($p -ceq 'auth' -or $p -ceq 'pwd' -or $p -ceq 'authorization') { return 'strong' } }
    if (-not $flat.Contains('token')) { return '' }
    # a TOKEN key that names a count ('max_tokens', 'token_count', 'token_limit', 'num_tokens', 'token_budget') may hold a number; any other TOKEN key ('GH_TOKEN') is strong
    if ($flat -match 'tokens|count|limit|max|min|budget|num|size|usage|used') { return 'count' }
    return 'strong'
}

# r15 (hub ruling on adversary pass 12 A1-A3: DENY BY DEFAULT on the key NAME). This replaces Get-CredentialPlainSpan and Read-CredentialValueAt, which judged a
# value they had rewritten first: they stripped the quotes ('"x"' judged as 'x'), the braces inside a sequence ('["{hunter2}"]' judged as '[""]', pass 12 A1), a
# trailing ' #' comment, a trailing ';' or ',', and they returned an empty container without looking at what followed it ('[]hunter2', '{}hunter2', pass 12 A1).
# Nothing is stripped now. The value of a secret-named key is the RAW text from just after its separator to the end of the record, and only that raw text (outer
# whitespace trimmed, nothing else) meets the closed exemption list in Test-CredentialValueExempt.
#
# Get-CredentialRawValueEnd: the index just past the raw value that starts at $At. The scan walks the text once and tracks:
#   - a double-quoted run (a backslash escapes the next character) and a single-quoted run. A quote opens a run wherever it stands (also after other text), and a
#     run may cross line breaks; an unterminated run takes the rest of the text, which is never exempt (fail closed).
#   - the depth of '[' and '{' outside a quoted run. A container may cross line breaks; an unclosed container takes the rest of the text (fail closed).
# At depth 0 and outside a quoted run the value ends at a line break. Inside a flow mapping or flow sequence ($Flow) it also ends at a ',' and at a ']' or '}'
# that closes the ENCLOSING container. Nothing else ends a value: text after a closing quote, after an empty container or after a comment marker ('""hunter2',
# '[]hunter2', '{}hunter2', '"" # note') is part of it, so such a value is never one of the exempt literals.
function Get-CredentialRawValueEnd {
    param([string]$Text, [int]$At, [bool]$Flow)
    $n = $Text.Length
    $i = $At
    $depth = 0
    $quote = [char]0
    while ($i -lt $n) {
        $c = $Text[$i]
        if ($quote -ne [char]0) {
            if ($quote -eq [char]'"' -and $c -eq [char]'\' -and $i + 1 -lt $n) { $i += 2; continue }
            if ($c -eq $quote) { $quote = [char]0 }
            $i++
            continue
        }
        if ($c -eq [char]'"' -or $c -eq [char]"'") { $quote = $c; $i++; continue }
        if ($c -eq [char]'[' -or $c -eq [char]'{') { $depth++; $i++; continue }
        if ($c -eq [char]']' -or $c -eq [char]'}') {
            if ($depth -gt 0) { $depth--; $i++; continue }
            if ($Flow) { return $i }   # the close of the enclosing flow container
            $i++
            continue
        }
        if ($depth -eq 0) {
            if ($c -eq [char]"`n" -or $c -eq [char]"`r") { return $i }
            if ($Flow -and $c -eq [char]',') { return $i }
        }
        $i++
    }
    return $n
}

# True when the raw text ends at a line break (or at the end of the text) from $At on: only blanks and tabs stand between.
function Test-CredentialAtLineEnd {
    param([string]$Text, [int]$At)
    $i = $At
    while ($i -lt $Text.Length -and ($Text[$i] -eq [char]' ' -or $Text[$i] -eq [char]"`t")) { $i++ }
    return ($i -ge $Text.Length -or $Text[$i] -eq [char]"`r" -or $Text[$i] -eq [char]"`n")
}

# The index of the first character of the next line (after $From, which is at a line break or the end) that is neither blank nor a '#' comment; -1 when none.
function Get-CredentialNextValueLine {
    param([string]$Text, [int]$From)
    $n = $Text.Length
    $p = $From
    while ($p -lt $n) {
        $nl = $Text.IndexOf("`n", $p)
        if ($nl -lt 0) { return -1 }
        $p = $nl + 1
        $q = $p
        while ($q -lt $n -and ($Text[$q] -eq ' ' -or $Text[$q] -eq "`t" -or $Text[$q] -eq "`r")) { $q++ }
        if ($q -ge $n) { return -1 }
        if ($Text[$q] -eq "`n") { $p = $q; continue }
        if ($Text[$q] -eq '#') { $p = $q; continue }
        return $q
    }
    return -1
}

# r14 pass 11 A2: the text of the lines after $From (a line break or the end) that are more indented than the key ($KeyIndent), up to the first non-blank line that is
# not; blank lines are skipped, and '#' comment lines too unless -KeepComments (a block scalar's '#' is content). Joined with blanks.
function Get-CredentialIndentedBlock {
    param([string]$Text, [int]$From, [int]$KeyIndent, [bool]$KeepComments)
    $n = $Text.Length
    $parts = [System.Collections.Generic.List[string]]::new()
    $p = $From
    while ($p -lt $n) {
        $nl = $Text.IndexOf("`n", $p)
        if ($nl -lt 0) { break }
        $s = $nl + 1
        $e = $Text.IndexOf("`n", $s); if ($e -lt 0) { $e = $n }
        $line = $Text.Substring($s, $e - $s).TrimEnd("`r")
        $p = $e
        if (-not $line.Trim()) { continue }
        $ind = $line.Length - $line.TrimStart(' ', "`t").Length
        # r15 (pass 12, deny by default): a '#' comment line never ends the block, at any indent ('password: ""\n# note\n  hunter2' keeps 'hunter2' in the value),
        # and with -KeepComments it is part of the raw value (the r15 caller always passes $true: nothing is stripped before the exemption test)
        $isComment = $line.TrimStart().StartsWith('#')
        if ($ind -le $KeyIndent -and -not $isComment) { break }
        if (-not $KeepComments -and $isComment) { continue }
        $parts.Add($line.Trim())
    }
    return ($parts -join ' ')
}

# True when the key at $KeyStart stands inside a flow mapping: the previous non-blank character (line breaks included) is '{', or a ',' (for '=' only when a '{'
# opened earlier on that line is still open: a TOML inline table, not a cmd / CSV ',KEY=value,more' line).
function Test-CredentialFlowContext {
    param([string]$Text, [int]$KeyStart, [string]$Sep)
    $i = $KeyStart - 1
    if ($i -ge 0 -and ($Text[$i] -eq '"' -or $Text[$i] -eq "'")) { $i-- }
    while ($i -ge 0 -and [char]::IsWhiteSpace($Text[$i])) { $i-- }
    if ($i -lt 0) { return $false }
    if ($Text[$i] -eq '{') { return $true }
    if ($Text[$i] -ne ',') { return $false }
    if ($Sep -eq ':') { return $true }
    $ls = $Text.LastIndexOf("`n", $i) + 1
    $seg = $Text.Substring($ls, $i - $ls)
    return (($seg.Split('{').Count - 1) -gt ($seg.Split('}').Count - 1))
}

# r15 (hub ruling, pass 12): the CLOSED exemption list, and nothing else. $Raw is the whole raw value as written (Get-CredentialRawValueEnd, the block lines, or an
# XML element's content); only its outer whitespace is trimmed. Exempt: the empty string, '""', "''", 'null', '~', '[]', '{}', '***' (each compared exactly,
# case-sensitive), an env reference '${NAME}' and a placeholder '<words>' (each anchored over the whole value). Everything else is a credential, including any text
# after an empty container ('[]hunter2'), a quoted literal ('"x"'), a boolean ('false'), a number ('4096') and a mapping ('{ type: x }'). Over-refusal is accepted.
# r2 (the one normalisation step, Get-CredentialNormalForm): in the DECODED pass, $script:CredentialDecodedMask marks every character a decode produced. A value whose
# span [$From, $To) holds one is NEVER exempt (deny by default: '"password": \156ull' reads as 'password: null' only because the value was decoded too).
# -Xml: an XML element's content is never the '<words>' placeholder ('<entry key="password"><entry></entry>hunter2</entry>': '<entry>' is a child element).
$script:CredentialDecodedMask = $null
function Test-CredentialValueExempt {
    param([string]$Raw, [int]$From = -1, [int]$To = -1, [switch]$Xml)
    if ($From -ge 0 -and $null -ne $script:CredentialDecodedMask) {
        $mk = $script:CredentialDecodedMask
        $a = [Math]::Min($From, $mk.Length); $b = [Math]::Min([Math]::Max($To, $a), $mk.Length)
        if ($b -gt $a -and $mk.IndexOf('1', $a, $b - $a) -ge 0) { return $false }
    }
    $s = $(if ($null -eq $Raw) { '' } else { $Raw.Trim() })
    if ($s.Length -eq 0) { return $true }
    if ($script:CredentialExemptLiterals -ccontains $s) { return $true }
    if ($Xml -and $s.StartsWith('<')) { return $false }
    return $script:CredentialPlaceholderRegex.IsMatch($s)
}

# r2 (pass 3 A2, and its pre-existing twin in Get-CredentialXmlHitIndex): the index of the close tag that MATCHES the element opened just before $Start (depth-aware: a
# nested '<name>' opens one more level, '<name/>' none), -1 when it is never closed.
function Get-CredentialXmlCloseIndex {
    param([string]$Text, [string]$Name, [int]$Start)
    $depth = 1
    foreach ($t in [regex]::new('(?i)<(/?)' + [regex]::Escape($Name) + '(?=[\s/>])' + $script:CredentialXmlTagTail).Matches($Text, $Start)) {   # r4 pass 9 A3 sibling: quote-aware tail
        if ($t.Groups[1].Value -eq '/') { $depth--; if ($depth -eq 0) { return $t.Index } }
        elseif (-not $t.Value.EndsWith('/>')) { $depth++ }
    }
    return -1
}

# DENY-BY-DEFAULT-1 S1 (deny by default): an escape that is still in the text after ConvertFrom-CredentialEscapes could not be decoded (an out-of-range '\U0011FFFF',
# '&#99999999;' or '`u{110FFFF}', an unknown '\N{NAME}', a '\x' or octal form with no valid digits). A key that holds one is secret-named whatever its letters say
# (Get-CredentialKeyValueHitIndex -Residual, Get-CredentialXmlAttrPairHitIndex -Residual), so no spelling a reader might still decode to 'password' can hide a key.
# Only the DECODED copy is judged this way (Get-CredentialHitLine): in the raw text every escape is still present and the rule would refuse an accented key.
# r1 adversary A3: a '%NN' still in a key after decoding (it decodes to a character that is not a name character, '%20') is undecodable too ('a%20b=c' refuses: accepted
# over-refusal). A backtick before a letter is NOT an introducer (a Markdown code span '`token`: x' would refuse everywhere); '`u{' is.
# r1 adversary pass 2 A3: an XML entity reference other than the five predefined ones ('&w;' declared in a DTD) is undecodable here too.
# r2 (pass 3 A4): an entity NAME is any run of XML name characters, non-ASCII included ('&' + U+00E9 + ';'); only what cannot stand in a name ends it.
# r2 (pass 3 A1, defect class "a key is found only after a listed predecessor"): a residual key starts after ANY character (no lookbehind), so 'db:pass&w;ord=' and
# 'db=pass\U0011FFFFord:' are keys too. Over-refusal is accepted.
$script:CredentialResidualIntro = '(?:\\N\{[^}\n]*\}|(?<!\\)\\[xuUN0-7]|&#|`u\{[0-9A-Fa-f]*\}?|%[0-9A-Fa-f]{2}|&(?!(?:amp|lt|gt|quot|apos);)[^\s&;<>"''=]+;)'
$script:CredentialResidualUnit = '(?:\\N\{[^}\n]*\}|`u\{[0-9A-Fa-f]*\}?|[^\s"''=:,{}\[\]()<>])'
$script:CredentialResidualIntroRegex = [regex]::new($script:CredentialResidualIntro)
# r4 (sol r3 A3 sibling, unbounded work): the lazy unit run before the introducer is quadratic on a long run of name characters, so this rule runs only when the text holds an
# introducer at all, and under a match timeout: a timeout is the typed CREDENTIAL_DECODE_LIMIT refusal (Test-CredentialDecodeLimitError), deny by default.
$script:CredentialResidualKeyRegex = [regex]::new('(' + $script:CredentialResidualUnit + '*?' + $script:CredentialResidualIntro + $script:CredentialResidualUnit + '*)((?:\s*["''\]\)\}])*)\s*(:|=(?!=))', [System.Text.RegularExpressions.RegexOptions]::None, [TimeSpan]::FromSeconds(20))

# The one key/value rule: the index of the first secret-named key that carries a value, -1 when there is none.
# DENY-BY-DEFAULT-1: with -Residual a key that holds an undecodable escape ($script:CredentialResidualKeyRegex) is a candidate too, and always secret-named.
function Get-CredentialKeyValueHitIndex {
    param([string]$Text, [bool]$Residual = $false)
    $n = $Text.Length
    $cands = [System.Collections.Generic.List[object]]::new()
    foreach ($m in $script:CredentialKeyRegex.Matches($Text)) { $cands.Add([pscustomobject]@{ M = $m; Residual = $false }) }
    if ($Residual -and $script:CredentialResidualIntroRegex.IsMatch($Text)) { foreach ($m in $script:CredentialResidualKeyRegex.Matches($Text)) { $cands.Add([pscustomobject]@{ M = $m; Residual = $true }) } }
    # r1 adversary A8: in text order, so the first hit (and the line the report names) is the earliest key of either kind
    foreach ($cand in @($cands | Sort-Object { $_.M.Groups[1].Index })) {
        $m = $cand.M
        $key = $m.Groups[1].Value
        $class = $(if ($cand.Residual) { 'strong' } else { Get-CredentialKeyClass $key })
        if (-not $class) { continue }
        $keyStart = $m.Groups[1].Index
        $sep = $m.Groups[3].Value
        # r15: a script variable ('$Token = Get-Token') is no longer exempt (deny by default: the ONLY exemptions are the closed literal list)
        $flow = Test-CredentialFlowContext -Text $Text -KeyStart $keyStart -Sep $sep
        $p = $m.Index + $m.Length
        while ($p -lt $n -and ($Text[$p] -eq ' ' -or $Text[$p] -eq "`t")) { $p++ }
        # r15 LINE form (and flow / sequence form): the raw text from the separator to the end of the record (a line break at depth 0; in a flow container also a ','
        # or the enclosing close); a quoted run or a container that crosses line breaks is followed to its close.
        $end = Get-CredentialRawValueEnd -Text $Text -At $p -Flow $flow
        $raw = $Text.Substring($p, $end - $p)
        # r2: the span the decoded-character mask is read over (Test-CredentialValueExempt -From -To). r2 pass 4 A1 (defect class: a decoded character that SHAPES the
        # value, not only one inside it): the span runs from the separator itself (a decoded blank before 'null' is skipped by the reader above), through the character that
        # ENDS the value (a decoded ',' or line break), and over every line a block-form lookup reads (a decoded '#' turns a line into a comment).
        $vFrom = $m.Groups[3].Index; $vTo = $end + 1
        if ($sep -eq ':') {
            $ls0 = $(if ($keyStart -eq 0) { 0 } else { $Text.LastIndexOf("`n", $keyStart - 1) + 1 })
            $keyIndent = 0
            while ($ls0 + $keyIndent -lt $n -and ($Text[$ls0 + $keyIndent] -eq ' ' -or $Text[$ls0 + $keyIndent] -eq "`t")) { $keyIndent++ }
            # r15 BLOCK form: nothing after 'k:' on its line. The value is the raw text from that line break through the first later line that is neither blank nor a
            # '#' comment (at ANY indent; the comment lines between are part of the raw value) and the record that line opens.
            if (-not $raw.Trim() -and (Test-CredentialAtLineEnd -Text $Text -At $end)) {
                $q = Get-CredentialNextValueLine $Text $end
                $vTo = $n
                if ($q -ge 0) {
                    $qEnd = Get-CredentialRawValueEnd -Text $Text -At $q -Flow $flow
                    $raw = $Text.Substring($end, $qEnd - $end)
                    $end = $qEnd
                }
            }
            # r15: outside a flow container every following line more indented than the key is part of the value too (a block scalar 'password: |', a plain
            # continuation 'password: ""\n  hunter2'), comment lines included and never ending it
            if (-not $flow) {
                $more = Get-CredentialIndentedBlock -Text $Text -From $end -KeyIndent $keyIndent -KeepComments $true
                if ($more.Trim()) { $raw = $raw + "`n" + $more; $end = $n }   # r2: the mask span runs to the end (over-refusal only)
            }
        }
        # r16 (adversary pass 15 A1, deny by default): 'k=' with nothing after it on its line takes the next line that is neither blank nor a '#' comment as its value,
        # exactly like the ':' block form. PowerShell continues an assignment across the line break ('$password =' then "'hunter2'" assigns hunter2), so the old reading
        # ('k=' then a later line is not a key/value record) let that through. 'k=' at the very end of the text (no later value line) is still the empty raw value
        # (exempt). Over-refusal is accepted: an ini 'password=' followed by an unrelated 'user=bob' line is now refused (the file is named in SECRET-SCAN.txt).
        if ($sep -ne ':' -and -not $raw.Trim() -and (Test-CredentialAtLineEnd -Text $Text -At $end)) {
            $q = Get-CredentialNextValueLine $Text $end
            $vTo = $n
            if ($q -ge 0) {
                $qEnd = Get-CredentialRawValueEnd -Text $Text -At $q -Flow $flow
                $raw = $Text.Substring($end, $qEnd - $end)
                $end = $qEnd
            }
        }
        if (Test-CredentialValueExempt $raw -From $vFrom -To ([Math]::Max($vTo, $end + 1))) { continue }
        return $keyStart
    }
    return -1
}

# r15 (hub ruling on pass 12 A3): netrc WITHOUT a record tokenizer. The r14 tokenizer split a quoted multi-word login ('login "alice smith"') on its blank, read
# 'smith"' as a word that ends the record and so ignored the 'password hunter2' after it. The decision now needs no record at all: a NETRC-SHAPED text (one that
# carries a whitespace-delimited 'machine', 'default', 'login' or 'macdef' token anywhere) in which the token 'password' or 'passwd' is followed by ANY further
# token (on any later line, a '#' comment or a quoted '""' included) is a credential. Over-refusal is accepted: prose that says 'login' and then 'password x' is a
# hit. Returns the index of that 'password' token, -1 when there is none.
function Get-CredentialNetrcHitIndex {
    param([string]$Text)
    if (-not $script:CredentialNetrcShapeRegex.IsMatch($Text)) { return -1 }
    $pw = $script:CredentialNetrcPasswordRegex.Match($Text)
    if ($pw.Success) { return $pw.Index }
    return -1
}

# r15 (pass 12 A2): the XML element form. For every OPENING tag of a secret-named element (namespace prefix ignored) that is not self-closing, the value is the RAW
# content from just after that tag to the first matching closing tag '</name>' (or to the end of the text when it is never closed). Nothing is removed first: a
# comment, a CDATA section or a child element is content. Exempt only when that content is empty after trimming outer whitespace (an element with no content at
# all) or one of the closed literals of Test-CredentialValueExempt. Returns the index of the opening tag, -1 when there is none.
function Get-CredentialXmlHitIndex {
    param([string]$Text)
    foreach ($xm in $script:CredentialXmlOpenRegex.Matches($Text)) {
        $name = $xm.Groups[1].Value
        if (-not (Get-CredentialKeyClass ($name -replace '^.*:', ''))) { continue }
        if ($xm.Value.EndsWith('/>')) { continue }   # '<password/>', '<password x="1" />': no content at all (its attributes are judged by the line form)
        $start = $xm.Index + $xm.Length
        $ci = Get-CredentialXmlCloseIndex -Text $Text -Name $name -Start $start   # r2 (pass 3 A2 pre-existing twin): depth-aware, '<password><password></password>x</password>'
        if ($ci -lt 0) { $ci = $Text.Length }
        if (Test-CredentialValueExempt $Text.Substring($start, $ci - $start) -From ($start - 1) -To ($ci + 1) -Xml) { continue }   # r2 pass 4 A1 sibling: the delimiters too
        return $xm.Index
    }
    return -1
}

# r19 pass 21 A1: a TOML / INI TABLE whose header names a secret ('[auth]', '[[auth.primary]]', '[tool.token]') holds a credential whenever any line of its body (up to the
# next table header) carries a value that is not on the closed exemption list, or has no key/value form at all (deny by default: 'key = "hunter2"' under '[auth]' names no
# secret by its own key). A body of blank and '#' comment lines holds nothing. Returns the index of the header, -1 when there is none.
# r19 pass 22 A2: a quoted name part may hold brackets ('["secret[primary]"]'), so a quoted run is one unit of the header name
# r23 (adversary pass 27 A2): an INI header may end in a ';' comment too ('[auth] ; connection settings'); only '#' was allowed, so that table was never seen
$script:CredentialTableHeaderRegex = [regex]::new('(?m)^[ \t]*\[{1,2}[ \t]*((?:"(?:[^"\\\r\n]|\\[^\r\n])*"|''[^''\r\n]*''|[^\[\]\r\n"''])+?)[ \t]*\]{1,2}[ \t]*(?:[#;][^\n]*)?$')
# r1 adversary A7: with -Residual (the decoded copy) a header name that still holds an undecodable escape ('["pass\U0011FFFFord"]') is secret-named too (deny by default).
function Get-CredentialTableHitIndex {
    param([string]$Text, [bool]$Residual = $false)
    $heads = @($script:CredentialTableHeaderRegex.Matches($Text))
    for ($h = 0; $h -lt $heads.Count; $h++) {
        $name = ($heads[$h].Groups[1].Value -replace '["'']', '')
        if (-not (Get-CredentialKeyClass $name) -and -not ($Residual -and $script:CredentialResidualIntroRegex.IsMatch($heads[$h].Groups[1].Value))) { continue }
        $from = $heads[$h].Index + $heads[$h].Length
        $to = $(if ($h + 1 -lt $heads.Count) { $heads[$h + 1].Index } else { $Text.Length })
        $lo = $from
        foreach ($line in @($Text.Substring($from, $to - $from) -split "`n")) {
            $lFrom = $lo; $lo += $line.Length + 1
            $l = $line.Trim()
            if (-not $l -or $l.StartsWith('#')) { continue }
            $kv = [regex]::Match($l, '^[^=:]*[=:]\s*(.*)$')
            if (-not $kv.Success -or -not (Test-CredentialValueExempt $kv.Groups[1].Value -From $lFrom -To ($lFrom + $line.Length + 1))) { return $heads[$h].Index }
        }
    }
    return -1
}

# DENY-BY-DEFAULT-1 S2: the XML ATTRIBUTE PAIR. A .NET config ('<add key="password" value="x" />', '<add name="Db" connectionString="..." />'), a settings file
# ('<setting name="ApiToken" serializeAs="String"><value>x</value></setting>') or a Java properties XML ('<entry key="db.password">x</entry>') puts the secret NAME in
# an attribute VALUE, where the key-name rule never looks. A start tag is a hit when one of its naming attributes ($CredentialXmlNameAttrs: key, name, id, property,
# variable, setting, param, parameter, field) has a value that is secret-named (Get-CredentialKeyClass, blanks removed first: 'DB Password'), or, with -Residual, holds an
# undecodable escape, AND either another attribute's value is not on the closed exemption list (deny by default: any other attribute, 'serializeAs="String"' included) or
# the element is not self-closing and its raw content up to its matching close tag is not exempt. Over-refusal is accepted ('<input type="password" name="pw">').
# A 'Password=' or 'Pwd=' inside any attribute value (a connection string) is the key/value rule's line form already.
# r1 adversary A6: an XML name is any run of name characters, not only ASCII ('<add key="password" value="x" caf&#233;="1" />' written with a literal accent, a namespace
# prefix '<e:add xmlns:e="urn:cfg" ...>' with one); the grammar only excludes what ends a name (blank, '<', '>', '/', '=', a quote). '<!' and '<?' are not start tags.
# r1 adversary A1: each attribute VALUE is decoded on its own (ConvertFrom-CredentialEscapes, so '&#119;' and '&#x77;' are read as an XML reader reads them) before its
# name test; the whole-document decoded copy can no longer hide the pair ('value="hunt&#34;er2"' decoded in place breaks the quoting). An undecodable escape left in a
# naming attribute's decoded value makes it secret-named (deny by default) in either pass.
# r1 adversary A2: every OTHER attribute is judged against the exemption list, the other naming attributes included ('<add key="password" name="hunter2" />').
$script:CredentialXmlStartTagRegex = [regex]::new('<(?![!?/])([^\s<>/="'']+)((?:\s+[^\s<>/="'']+\s*=\s*(?:"[^"]*"|''[^'']*''))+)\s*(/?)>')
$script:CredentialXmlAttrRegex = [regex]::new('([^\s<>/="'']+)\s*=\s*(?:"([^"]*)"|''([^'']*)'')')
$script:CredentialXmlNameAttrs = @('key', 'name', 'id', 'property', 'variable', 'setting', 'param', 'parameter', 'field')
# r2 pass 4 A4: -NameOnly returns the first start tag whose naming attribute is secret-named (or holds an undecodable escape) WHATEVER the other values are; the Import
# credential-ROLE guard (Test-CredentialNamePresent) asks whether a name is present, not whether a value is.
function Get-CredentialXmlAttrPairHitIndex {
    param([string]$Text, [bool]$Residual = $false, [switch]$NameOnly)
    foreach ($tm in $script:CredentialXmlStartTagRegex.Matches($Text)) {
        $attrs = @(foreach ($am in $script:CredentialXmlAttrRegex.Matches($tm.Groups[2].Value)) {
                $vg = $(if ($am.Groups[2].Success) { $am.Groups[2] } else { $am.Groups[3] })
                [pscustomobject]@{ N = ($am.Groups[1].Value -replace '^.*:', '').ToLowerInvariant(); V = $vg.Value; At = $tm.Groups[2].Index + $vg.Index }
            })
        $namer = -1
        for ($j = 0; $j -lt $attrs.Count; $j++) {
            if ($script:CredentialXmlNameAttrs -notcontains $attrs[$j].N) { continue }
            # r3 pass 7 A2: the value is judged at EVERY decode level, not only the fixed point: 'auth&#92;u0068eader' is 'authheader'
            # (a secret name) to a reader that decodes once, and only the deeper 'authheader' is a plain word
            # (an escape still standing at a shallower level is decodable by definition: the undecodable residual is judged on the fixed point only)
            # r4 (sol r3 A1, defect class: a judge that drops the spelling AS WRITTEN once a decode exists): the RAW value is the first candidate ('authheader' is
            # a strong name as written, a plain word decoded); then every level as it is produced (r4 A3: none retained; past the bound the caller refuses typed)
            $v = $attrs[$j].V
            if (Get-CredentialKeyClass ($v -replace '\s', '')) { $namer = $j; break }
            $vnf = Get-CredentialNormalForm $v -OnStage { param($lvText, $lvMask) if (Get-CredentialKeyClass ($lvText -replace '\s', '')) { 0 } else { -1 } }
            if ($vnf.Hit -ge 0 -or $script:CredentialResidualIntroRegex.IsMatch($vnf.Text)) { $namer = $j; break }
        }
        if ($namer -lt 0) { continue }
        if ($NameOnly) { return $tm.Index }
        for ($j = 0; $j -lt $attrs.Count; $j++) { if ($j -ne $namer -and -not (Test-CredentialValueExempt $attrs[$j].V -From ($attrs[$j].At - 1) -To ($attrs[$j].At + $attrs[$j].V.Length + 1))) { return $tm.Index } }
        if ($tm.Groups[3].Value -ne '/') {
            $start = $tm.Index + $tm.Length
            # r2 (pass 3 A2): depth-aware close, and XML content is never the '<words>' placeholder
            $ci = Get-CredentialXmlCloseIndex -Text $Text -Name $tm.Groups[1].Value -Start $start
            if ($ci -lt 0) { $ci = $Text.Length }
            if (-not (Test-CredentialValueExempt $Text.Substring($start, $ci - $start) -From ($start - 1) -To ($ci + 1) -Xml)) { return $tm.Index }
        }
    }
    return -1
}

# The index of the first credential in the text (a known token shape, the key/value rule, or a netrc record), -1 when there is none.
# DENY-BY-DEFAULT-1: -Residual (the decoded copy only) also treats a key holding an undecodable escape as secret-named; the XML attribute pair rule always runs.
function Get-CredentialHitIndex {
    param([string]$Text, [bool]$Residual = $false)
    $best = -1
    $m = $script:SecretShapeRegex.Match($Text)
    if ($m.Success) { $best = $m.Index }
    # r15: the XML element form (Get-CredentialXmlHitIndex) judges the element's RAW content; the comment, CDATA and markup strip of r14 (pass 12 A2) is gone
    $xml = Get-CredentialXmlHitIndex $Text
    foreach ($i in @((Get-CredentialKeyValueHitIndex -Text $Text -Residual $Residual), (Get-CredentialNetrcHitIndex $Text), $xml, (Get-CredentialTableHitIndex -Text $Text -Residual $Residual), (Get-CredentialXmlAttrPairHitIndex -Text $Text -Residual $Residual))) {
        if ($i -ge 0 -and ($best -lt 0 -or $i -lt $best)) { $best = $i }
    }
    return $best
}

# r23 (adversary pass 27 A1; pass 26 A3): the escapes a TOML, YAML or JSON reader decodes in a key or value: \uXXXX, \UXXXXXXXX (TOML), \xXX (YAML) and \/ (JSON).
# Only \uXXXX used to be decoded, so '"\U00000070assword" = "hunter2"' and '"\x70assword": hunter2' named no secret. A \U value that is not a Unicode scalar is left as written.
# r25 (adversary pass 29 A2): a YAML or TOML double-quoted scalar also takes a backslash at the end of a line as an escaped line break: the break and the leading blanks of the
# next line are dropped ('"pass\<newline>  word"' is 'password'). The text has one '\n' per separator by the time it gets here (Get-CredentialHitLine).
# MINI3-CUTOVER-CREDENTIAL-DETECTOR-DENY-BY-DEFAULT-1 S1 (doctrine bus 0fc7511 + d6d115b: fix the CLASS, not one more spelling): every escape form a reader of a file
# Export may ship decodes in a KEY is decoded on the COPY that the second pass judges: YAML/JSON/TOML '\xNN' '\uNNNN' '\UNNNNNNNN' '\/' and the escaped line break;
# XML/HTML character references '&#NNN;' and '&#xNN;' (an attribute value is decoded by every XML reader: '<add key="pass&#119;ord" .../>'); PowerShell '`u{NN}' and
# '`<letter>' (an unknown backtick escape is the letter itself: "pass`word" is 'password'; the special '`0 `a `b `e `f `n `r `t `v' stay as written); C / Python octal
# '\NNN' and Python '\N{LATIN SMALL LETTER W}' (also CAPITAL, DIGIT ZERO..NINE, LOW LINE, HYPHEN-MINUS, FULL STOP, SOLIDUS); URL '%NN'. The octal and '%NN' forms are
# decoded only to a name character ([A-Za-z0-9_.-/\]), so a path or a query value is not rewritten into line breaks. The decode repeats until nothing changes, so a
# nested spelling ('&#92;x77' is '\x77' is 'w') is decoded too. What is still an escape after that cannot be decoded and is judged deny by default
# ($script:CredentialResidualKeyRegex). Over-decoding is safe: the copy only adds hits, the raw text was judged first.
$script:CredentialPyNames = @{ 'LOW LINE' = '_'; 'HYPHEN-MINUS' = '-'; 'FULL STOP' = '.'; 'SOLIDUS' = '/' }
$credDigitNames = @('ZERO', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE')
for ($credDigit = 0; $credDigit -lt 10; $credDigit++) { $script:CredentialPyNames['DIGIT ' + $credDigitNames[$credDigit]] = [string]$credDigit }
# r4 (adversary pass 10 A1-A2, defect class: an escape a reader decodes that the normaliser left as written): JSON, YAML, TOML and C also decode the escaped backslash
# '\\' (so '"pass\\U0011FFFFord"' is the key 'pass\U0011FFFFord', whose undecodable escape is deny by default) and the one-letter escapes '\n' '\t' '\r' '\b' '\f' '\"'
# ('<add key=''pass\nword'' .../>' inside a JSON string is the naming value 'pass<LF>word', 'password' once its blanks are removed). Each is decoded on its own level.
$script:CredentialEscapeRegex = [regex]::new('\\(?:u(?<u4>[0-9a-fA-F]{4})|U(?<u8>[0-9a-fA-F]{8})|x(?<x>[0-9a-fA-F]{2})|(?<sl>/)|(?<nl>\n[ \t]*)|(?<bs>\\)|(?<one>[ntrbf"])|(?<oct>[0-7]{3})|N\{(?<nm>[A-Za-z0-9 \-]+)\})|&#(?<dec>[0-9]{1,8});|&#[xX](?<hex>[0-9a-fA-F]{1,8});|`u\{(?<psu>[0-9a-fA-F]{1,8})\}|`(?<psl>[A-Za-z0-9_.\-])|%(?<pct>[0-9A-Fa-f]{2})|&(?<ent>amp|lt|gt|quot|apos);')
$script:CredentialEntities = @{ 'amp' = '&'; 'lt' = '<'; 'gt' = '>'; 'quot' = '"'; 'apos' = "'" }
$script:CredentialOneLetterEscapes = [System.Collections.Generic.Dictionary[string, string]]::new([StringComparer]::Ordinal)
foreach ($credOne in @(@('n', "`n"), @('t', "`t"), @('r', "`r"), @('b', [string][char]8), @('f', [string][char]12), @('"', '"'))) { $script:CredentialOneLetterEscapes[$credOne[0]] = $credOne[1] }
# r2: true when the normaliser has anything to do in the text: an escape it decodes changes the text, or an undecodable one is left (deny by default). It is derived from the
# normaliser itself, so no escape form can be missing from a separate trigger list (pass 3 A3: '&w;' had no trigger and the residual rule never ran).
function Test-CredentialEscapesPresent([string]$Text) { return ($script:CredentialResidualIntroRegex.IsMatch("$Text") -or ((ConvertFrom-CredentialEscapes $Text) -cne "$Text")) }
function ConvertFrom-CredentialEscapes([string]$Text) { return (Get-CredentialNormalForm $Text).Text }
$script:CredentialEscapeEval = {
        param($m)
        $g = $m.Groups
        if ($g['ent'].Success) { return $script:CredentialEntities[$g['ent'].Value] }
        if ($g['nl'].Success) { return '' }
        if ($g['sl'].Success) { return '/' }
        if ($g['bs'].Success) { return '\' }   # r4 pass 10 A1
        if ($g['one'].Success) { return $script:CredentialOneLetterEscapes[$g['one'].Value] }   # r4 pass 10 A2
        if ($g['x'].Success) { return [string][char][Convert]::ToInt32($g['x'].Value, 16) }
        if ($g['psl'].Success) { if ('0abefnrtv'.Contains($g['psl'].Value)) { return $m.Value }; return $g['psl'].Value }
        if ($g['nm'].Success) {
            $nm = $g['nm'].Value.ToUpperInvariant()
            $lm = [regex]::Match($nm, '^LATIN (SMALL|CAPITAL) LETTER ([A-Z])$')
            if ($lm.Success) { if ($lm.Groups[1].Value -eq 'SMALL') { return $lm.Groups[2].Value.ToLowerInvariant() }; return $lm.Groups[2].Value }
            if ($script:CredentialPyNames.ContainsKey($nm)) { return $script:CredentialPyNames[$nm] }
            return $m.Value
        }
        if ($g['oct'].Success -or $g['pct'].Success) {
            $c = $(if ($g['oct'].Success) { [Convert]::ToInt32($g['oct'].Value, 8) } else { [Convert]::ToInt32($g['pct'].Value, 16) })
            $ch = [string][char]$c
            if ($ch -cmatch '^[A-Za-z0-9_.\-/\\]$') { return $ch }   # r1 adversary A3: '/' and '\' separate a name's parts ('api%2Fkey' is 'api/key')
            return $m.Value
        }
        $codePoint = $(if ($g['u4'].Success) { [Convert]::ToInt64($g['u4'].Value, 16) } elseif ($g['u8'].Success) { [Convert]::ToInt64($g['u8'].Value, 16) } elseif ($g['dec'].Success) { [Convert]::ToInt64($g['dec'].Value, 10) } elseif ($g['hex'].Success) { [Convert]::ToInt64($g['hex'].Value, 16) } else { [Convert]::ToInt64($g['psu'].Value, 16) })
        if ($g['u4'].Success) { return [string][char][int]$codePoint }   # r23: a lone \uD800 stays one UTF-16 unit, as before
        if ($codePoint -gt 0x10FFFF -or ($codePoint -ge 0xD800 -and $codePoint -le 0xDFFF)) { return $m.Value }
        return [char]::ConvertFromUtf32([int]$codePoint)
    }

# r2 DEFECT CLASS (r1 found 16 decoder gaps one at a time): the detector decoded escaped and entity forms by a LIST OF SPECIAL CASES (key names only, after a listed
# predecessor character, behind a separate trigger list). Get-CredentialNormalForm is the ONE normalisation step every check reads: the WHOLE text, every escape form
# ($script:CredentialEscapeRegex: \uXXXX \UXXXXXXXX \xXX \/ the escaped line break, octal, \N{NAME}, &#N; &#xN;, the five predefined &name; entities, `u{N}, `letter, %XX)
# decoded to a FIXED POINT (nesting included: '&amp;#119;' is '&#119;' is 'w'), line separators made one '\n' at every step. It also returns a MASK ('1' for each
# character a decode produced, '0' for each one copied as written), so the exemption list can never be met by a decoded value (Test-CredentialValueExempt -From -To):
# a value is decoded for the key and token rules, and judged as written for the exemption. What the step cannot decode stays as written and is deny by default
# ($script:CredentialResidualKeyRegex, $script:CredentialResidualIntroRegex). Each replacement is no longer than what it replaces and the loop stops when nothing changes.
function Invoke-CredentialMaskedReplace {
    param([string]$S, [string]$Mask, [regex]$Regex, [scriptblock]$Eval)
    $sb = [System.Text.StringBuilder]::new(); $mb = [System.Text.StringBuilder]::new(); $last = 0; $changed = $false; $pendingMark = $false; $pendingAt = 0
    foreach ($m in $Regex.Matches($S)) {
        $r = [string](& $Eval $m)
        if ($r -ceq $m.Value) { continue }
        [void]$sb.Append($S, $last, $m.Index - $last); [void]$mb.Append($Mask, $last, $m.Index - $last)
        if ($pendingMark -and $mb.Length -gt $pendingAt) { $mb[$pendingAt] = '1' }
        $pendingMark = $false
        [void]$sb.Append($r); [void]$mb.Append('1', $r.Length)
        # r2 pass 5 A1 (defect class: a decode that DELETES leaves no mark): an empty replacement (the escaped line break) marks the characters on both sides of where it
        # stood, so '"password": \<newline>  null' (a decoded backslash that joins the next line) never reads as an exempt 'null'.
        if ($r.Length -eq 0) { if ($mb.Length -gt 0) { $mb[$mb.Length - 1] = '1' }; $pendingMark = $true; $pendingAt = $mb.Length }
        $last = $m.Index + $m.Length; $changed = $true
    }
    if (-not $changed) { return $null }
    [void]$sb.Append($S, $last, $S.Length - $last); [void]$mb.Append($Mask, $last, $S.Length - $last)
    if ($pendingMark -and $mb.Length -gt $pendingAt) { $mb[$pendingAt] = '1' }
    return [pscustomobject]@{ Text = $sb.ToString(); Mask = $mb.ToString() }
}
$script:CredentialLineBreakEval = { param($m) "`n" }
# r2 pass 6 A1 (defect class: over-decoding can REMOVE a hit): a reader decodes ONCE ('password\u0020suffix' is the key 'password suffix'); the fixed
# point decodes on to 'password suffix', which no candidate matches. Every decode LEVEL is therefore kept (.Stages, one per pass of the loop, each with its mask) and the
# decoded pass judges each one: the first level a reader may stop at, every deeper level a nested reader may reach.
# r4 (sol r3 A3, defect class: unbounded input; nested entities decode one layer per pass, so '&' + 'amp;' x 1e6 (4 MB, below the scan cap) needed a million passes and
# kept every level's full text and mask: terabytes before a verdict). Two hard bounds, both REFUSING typed (CREDENTIAL_DECODE_LIMIT, deny by default), never truncating:
#   - at most $script:CredentialMaxDecodeStages decode levels; a text that still changes after them is refused (a reader may decode it further than this judge did);
#   - no level is RETAINED: each one is handed to the judge (-OnStage) as it is produced and dropped, so memory is the current text and mask only (two copies of the input).
# A regex match timeout (RegexMatchTimeoutException) in a credential rule is the same typed refusal (Test-CredentialDecodeLimitError).
$script:CredentialMaxDecodeStages = 16
$script:CredentialDecodeLimitTag = 'CREDENTIAL_DECODE_LIMIT'
function New-CredentialDecodeLimitError([string]$Why) { return [System.InvalidOperationException]::new("$($script:CredentialDecodeLimitTag) $Why (refused, deny by default)") }
function Test-CredentialDecodeLimitError($ErrorRecord) {
    $e = $(if ($ErrorRecord -is [System.Management.Automation.ErrorRecord]) { $ErrorRecord.Exception } else { $ErrorRecord })
    while ($e) {
        if ($e -is [System.Text.RegularExpressions.RegexMatchTimeoutException] -or "$($e.Message)".StartsWith($script:CredentialDecodeLimitTag)) { return $true }
        $e = $e.InnerException
    }
    return $false
}
# -OnStage { param($LevelText, $LevelMask) <index or -1> }: called on every decode level, shallowest first; the first level it returns an index >= 0 for stops the loop
# (.Hit is that index, .Text and .Mask that level). Without a hit, .Text and .Mask are the fixed point and .Levels the number of levels (0 when nothing decodes).
function Get-CredentialNormalForm([string]$Text, [scriptblock]$OnStage = $null) {
    $s = "$Text"; $mask = [string]::new([char]'0', $s.Length)
    $levels = 0; $hit = -1
    while ($true) {
        $r = Invoke-CredentialMaskedReplace -S $s -Mask $mask -Regex $script:CredentialEscapeRegex -Eval $script:CredentialEscapeEval
        if ($r) { $s = $r.Text; $mask = $r.Mask }
        $b = Invoke-CredentialMaskedReplace -S $s -Mask $mask -Regex $script:CredentialLineBreakRegex -Eval $script:CredentialLineBreakEval
        if ($b) { $s = $b.Text; $mask = $b.Mask }
        if (-not $r -and -not $b) { break }
        $levels++
        if ($levels -gt $script:CredentialMaxDecodeStages) { throw (New-CredentialDecodeLimitError "the text still decodes after $($script:CredentialMaxDecodeStages) levels") }
        if ($OnStage) {
            $got = @(& $OnStage $s $mask)
            if ($got.Count -gt 0 -and [int]$got[-1] -ge 0) { $hit = [int]$got[-1]; break }
        }
    }
    return [pscustomobject]@{ Text = $s; Mask = $mask; Levels = $levels; Hit = $hit }
}

# True when the text carries a credential: a known token shape, or a secret-named key with a value (the Export scan's own patterns). The text is
# judged as written AND with its JSON \uXXXX escapes decoded (p5 A1: '{"api_key":"x"}' says api_key). The Export scan and the Import host-side
# test both call this one function.
function Test-CredentialText {
    param([string]$Text)
    return ((Get-CredentialHitLine $Text) -gt 0)
}

# r11 F-B1: the 1-based line of the first credential match in the text (judged as written, then with JSON \uXXXX escapes decoded), 0 when there is none.
# Test-CredentialText is exactly "this is greater than 0", so the Export report and every guard always agree. Never returns the matched text.
function Get-CredentialHitLine {
    param([string]$Text)
    if (-not $Text) { return 0 }
    $Text = $Text.Replace([string][char]0, '')   # r19 pass 22 A4: a NUL is never part of a credential file's text; a BOM-less UTF-16 file decodes with one between every letter
    if (-not $Text) { return 0 }
    # r15 pass 13 A1: every line separator is ONE '\n' before any rule runs ('\r\n', a lone '\r' as in '{"password":\r"hunter2"}', NEL U+0085, LS U+2028, PS U+2029).
    # The rules below (block form, indented lines, next value line, the hit line) all walk '\n'; a CR-only file used to look like one line and its block value was
    # never found. This rewrites separators only, never a value's characters, so the raw-value rule still judges the text as written.
    $Text = $script:CredentialLineBreakRegex.Replace($Text, "`n")
    $script:CredentialDecodeLimitHit = $false
    try {
        # r14: the one shape-agnostic rule (Get-CredentialHitIndex: known token shapes, the key/value rule in its line, block and flow forms, netrc records).
        $idx = Get-CredentialHitIndex $Text
        if ($idx -lt 0) {
            # r2: the decoded pass reads the ONE normal form (Get-CredentialNormalForm: the whole text decoded to a fixed point). It runs whenever the normal form differs from
            # the text or an undecodable escape is left in it, so no trigger list can skip it (pass 3 A3). Every rule reads the decoded text; the mask keeps a decoded value
            # off the exemption list (r1 pass 2 A1: '"password": \156ull'); a key still holding an undecodable escape is secret-named (-Residual, deny by default).
            # r2 pass 6 A1: every decode level, shallowest first. r4 (sol r3 A3): each level is judged as it is produced and then dropped (-OnStage), never kept.
            $nf = Get-CredentialNormalForm $Text -OnStage { param($lvText, $lvMask) $script:CredentialDecodedMask = $lvMask; try { Get-CredentialHitIndex -Text $lvText -Residual $true } finally { $script:CredentialDecodedMask = $null } }
            if ($nf.Hit -ge 0) { $idx = $nf.Hit; $Text = $nf.Text }
            elseif ($nf.Levels -eq 0 -and $script:CredentialResidualIntroRegex.IsMatch($Text)) {
                # a text with only undecodable escapes has no level: it is judged as its own normal form
                $script:CredentialDecodedMask = $nf.Mask
                try { $idx = Get-CredentialHitIndex -Text $nf.Text -Residual $true } finally { $script:CredentialDecodedMask = $null }
            }
        }
    } catch {
        # r4 (sol r3 A3): past the decode bound (or a rule's match timeout) the text is REFUSED, typed, on line 1: deny by default, never a partial verdict
        if (Test-CredentialDecodeLimitError $_) { $script:CredentialDecodeLimitHit = $true; return 1 }
        throw
    }
    if ($idx -lt 0) { return 0 }
    # the line of the KEY (a block-form value sits on a later line; the report names where the secret-named key stands)
    return ($Text.Substring(0, $idx).Split("`n").Count)
}
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
# p5 A9: the verdict on ONE staged file, shared by the scan and by the post-archive re-check: '' = clean, else the reason it must not ship.
# r12 A4: the path of $Full below $Root. Both are normalised first (GetFullPath: '..' and '.' parts, doubled separators): Get-ChildItem returns normalised FullName values,
# so a Substring by the length of an un-normalised root (-StateRoot C:\x\..\x) cut the wrong number of characters and mapped a file to a different folder.
function Get-RelativeUnder {
    param([string]$Root, [string]$Full)
    $r = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $f = [IO.Path]::GetFullPath($Full)
    if (-not $f.StartsWith($r + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "path $Full is not below $Root" }
    return $f.Substring($r.Length).TrimStart('\')
}

function Get-StagedFileVerdict {
    param([System.IO.FileInfo]$File, [long]$MaxScanBytes = 26214400)
    return (Get-StagedFileVerdictDetail -File $File -MaxScanBytes $MaxScanBytes).Why
}

# r11 F-B1: the same verdict plus the line of the first credential match (0 when the reason is not a credential match). Never carries the matched text.
function Get-StagedFileVerdictDetail {
    param([System.IO.FileInfo]$File, [long]$MaxScanBytes = 26214400)
    if ($File.Length -gt $MaxScanBytes) { return @{ Why = 'larger than the scan limit'; Line = 0 } }
    $t = $null
    # r19 pass 21 A5: the size is judged on the BYTES actually read (never the cached FileInfo.Length, never the decoded character count: 15 MB of a two-byte character is 30 MB).
    # The read stops one byte past the limit, so a file that grew or was swapped after the length check is still oversized and is never decoded.
    $ms = [IO.MemoryStream]::new()
    try {
        $rs = [IO.File]::OpenRead($File.FullName)
        try {
            $chunk = [byte[]]::new(65536)
            # r20 (sol r19 advisory): each read asks for no more than the bytes still allowed plus one, so at most limit+1 bytes are ever read (no 65536-byte chunk past a small limit)
            while ($ms.Length -le $MaxScanBytes -and (($got = $rs.Read($chunk, 0, [int][Math]::Min([long]$chunk.Length, $MaxScanBytes + 1 - $ms.Length))) -gt 0)) { $ms.Write($chunk, 0, $got) }
        } finally { $rs.Dispose() }
    } catch { return @{ Why = 'unreadable'; Line = 0 } }
    if ($ms.Length -gt $MaxScanBytes) { return @{ Why = 'larger than the scan limit'; Line = 0 } }
    try { $t = ConvertFrom-ImportBytes $ms.ToArray() } catch { return @{ Why = 'unreadable'; Line = 0 } }
    $line = Get-CredentialHitLine $t
    # r4 (sol r3 A3): the typed decode-limit refusal is named as such (it is still a credential-pattern refusal: the file is removed and listed)
    if ($line -gt 0 -and $script:CredentialDecodeLimitHit) { return @{ Why = "credential pattern ($($script:CredentialDecodeLimitTag): more than $($script:CredentialMaxDecodeStages) decode levels or a rule's match timeout, deny by default)"; Line = $line } }
    if ($line -gt 0) { return @{ Why = 'credential pattern'; Line = $line } }
    return @{ Why = ''; Line = 0 }
}

# Read-only: the relative paths of staged files that are NOT clean right now (the archive step runs this again after tar, because a file can change
# between the scan and the moment tar reads it; adversary pass 5, A9).
function Get-StagedCredentialHits {
    param([string]$Stage, [long]$MaxScanBytes = 26214400)
    $hits = [System.Collections.Generic.List[string]]::new()
    foreach ($f in @(Get-ChildItem -LiteralPath $Stage -Recurse -File -Force)) {
        $why = Get-StagedFileVerdict -File $f -MaxScanBytes $MaxScanBytes
        if ($why) { $hits.Add("$(Get-RelativeUnder $Stage $f.FullName) ($why)") }
    }
    return $hits.ToArray()
}

# r10 A3: the verdict on what the ARCHIVE holds (read from the zip itself, nothing extracted to disk). The stage can change between the scan, the moment
# tar reads a file and a re-scan of the stage, so only the archive's own entries say what would be offered. Unreadable or oversized counts as a hit.
function Get-ArchiveCredentialHits {
    param([string]$Zip, [long]$MaxScanBytes = 26214400)
    $hits = [System.Collections.Generic.List[string]]::new()
    $arc = $null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $arc = [System.IO.Compression.ZipFile]::OpenRead($Zip)
        foreach ($e in $arc.Entries) {
            if ($e.FullName.EndsWith('/') -or $e.FullName.EndsWith('\')) { continue }
            if ($e.Length -gt $MaxScanBytes) { $hits.Add("$($e.FullName) (larger than the scan limit)"); continue }
            $t = $null
            try {
                $sr = [IO.StreamReader]::new($e.Open(), [Text.Encoding]::UTF8, $true)
                try { $t = $sr.ReadToEnd() } finally { $sr.Dispose() }
            } catch { $hits.Add("$($e.FullName) (unreadable)"); continue }
            if ($t -and (Test-CredentialText $t)) { $hits.Add("$($e.FullName) (credential pattern in the archive)") }
        }
    } catch { $hits.Add("$Zip (archive unreadable: $($_.Exception.Message))") }
    finally { if ($arc) { $arc.Dispose() } }
    return $hits.ToArray()
}

# p5 A9: a private copy of a bundle FOLDER. Import scans and then copies; between the two a writer could swap the file, so both work from this
# snapshot, which only this run can write to. Returns the snapshot path.
function New-ImportSnapshot {
    param([string]$From, [string]$To)
    [void][IO.Directory]::CreateDirectory($To)
    Invoke-Robocopy $From $To @() @('/E', '/COPY:DAT')
    return $To
}

function Invoke-StagedSecretScan {
    param([string]$Stage, [long]$MaxScanBytes = 26214400)
    $removed = [System.Collections.Generic.List[string]]::new()
    $uncleared = [System.Collections.Generic.List[string]]::new()
    # r11 F-B1: one typed line per file the scan took out of the stage. A credential match is EXPORT_CREDENTIAL_HIT <relative path>:<line> (the line of the
    # first match, never the matched text); a file that was too large or unreadable is EXPORT_SCAN_DROPPED <relative path> (<why>).
    $typed = [System.Collections.Generic.List[string]]::new()
    # -Force: robocopy copies Hidden and System files, and Get-ChildItem hides them by default (adversary pass 2, A3).
    foreach ($f in @(Get-ChildItem -LiteralPath $Stage -Recurse -File -Force)) {
        $rel = Get-RelativeUnder $Stage $f.FullName
        $v = Get-StagedFileVerdictDetail -File $f -MaxScanBytes $MaxScanBytes
        $why = $v.Why
        if (-not $why) { continue }
        try {
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop
            $removed.Add("$rel ($why)")
            $typed.Add($(if ($v.Line -gt 0) { "EXPORT_CREDENTIAL_HIT ${rel}:$($v.Line)" } else { "EXPORT_SCAN_DROPPED $rel ($why)" }))
        }
        catch { $uncleared.Add("$rel ($why; could not be removed)") }
    }
    return @{ Removed = $removed.ToArray(); Uncleared = $uncleared.ToArray(); Typed = $typed.ToArray() }
}

# Scan + report, returning $true only when every staged file is clean or was removed. Fail closed (adversary pass 2, A4): any throw, an uncleared file,
# or a report that cannot be written reads as NOT clean, so the caller never goes on to zip a stage that still holds a credential.
function Test-StagedBundleClean {
    param([string]$Stage)
    try {
        $res = Invoke-StagedSecretScan -Stage $Stage
        $clean = ($res.Uncleared.Count -eq 0)
        # r11 F-B1: SECRET-SCAN.txt carries the same typed lines the run prints (path and line, never the matched value).
        $lacks = $(if ($res.Typed.Count) { ' (this bundle LACKS them and Export ends non-zero)' } else { '' })
        try { Write-AsciiFile (Join-Path $Stage 'SECRET-SCAN.txt') ("removed $($res.Removed.Count) file(s) that matched a credential pattern or could not be scanned${lacks}:`r`n" + ($res.Typed -join "`r`n") + "`r`n") }
        # r3 A3: a bundle whose scan report cannot be written is NOT clean, even when every file scanned clean: the report is what the owner reviews before transfer.
        catch { $clean = $false; Write-Log "could not write SECRET-SCAN.txt (the bundle is NOT clean and will not be archived): $($_.Exception.Message)" 'ERROR' }
        Write-Log "secret scan removed $($res.Removed.Count) file(s) (see SECRET-SCAN.txt; review before transfer)" $(if ($res.Removed.Count) { 'WARN' } else { 'OK' })
        # r11 F-B1: a file the scan took out of the stage is a file the bundle lacks, so the run fails (exit 1) with one typed line per file. The bundle itself
        # is still written WITHOUT those files; nothing narrows the detector to make a match go away.
        foreach ($t in $res.Typed) { Write-Log $t 'ERROR'; $script:Failures.Add($t) }
        if (-not $clean) { Write-Log "secret scan could not clear $($res.Uncleared.Count) file(s): $($res.Uncleared -join '; ')" 'ERROR' }
        return $clean
    } catch {
        Write-Log "secret scan failed: $($_.Exception.Message)" 'ERROR'
        return $false
    }
}

# r15 (pass 12 A6): delete a file and PROVE it is gone. Remove-Item runs with -ErrorAction Stop. When the delete is refused (another process holds the file open),
# the file is renamed aside to '<name>.undeleted-<id>' so it no longer stands at its usable name (the transfer name of a bundle, the name of a backup), and the
# result says where it is. It is never reported as deleted unless Test-Path is false afterwards. Returns @{ Deleted; Path (where it is now, '' when deleted); Why }.
function Remove-FileVerified {
    param([string]$Path)
    $why = ''
    try { Remove-Item -LiteralPath $Path -Force -ErrorAction Stop } catch { $why = $_.Exception.Message }
    if (-not (Test-Path -LiteralPath $Path)) { return @{ Deleted = $true; Path = ''; Why = '' } }
    if (-not $why) { $why = 'the file is still present after Remove-Item' }
    $aside = $Path + '.undeleted-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    try {
        [IO.File]::Move($Path, $aside)
        if (-not (Test-Path -LiteralPath $Path) -and (Test-Path -LiteralPath $aside)) { return @{ Deleted = $false; Path = $aside; Why = "$why (renamed aside)" } }
    } catch { $why = "$why; the rename aside failed too: $($_.Exception.Message)" }
    return @{ Deleted = $false; Path = $Path; Why = $why }
}

# r16 (adversary pass 15 A4): a file this run CREATES and may have to take back is opened through a handle that also holds DELETE access (New-OwnedFileStream), and a
# failed write removes it THROUGH THAT HANDLE (Remove-OwnedFileByHandle: SetFileInformationByHandle FileDispositionInfo, deleted when the handle closes). The old
# cleanup released the handle and then deleted by PATHNAME, so a file another process saved at that path in the gap (an owner's credential-bearing save) was deleted
# instead and reported Deleted=true. The handle is never released before the delete is marked, and the share mode is None, so the deleted file is always ours.
function New-OwnedFileStream {
    param([string]$Path, [IO.FileMode]$Mode)
    $rights = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::Synchronize
    return [System.IO.FileSystemAclExtensions]::Create([IO.FileInfo]::new($Path), $Mode, $rights, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $null)
}

# Marks the file behind $Stream for deletion and closes the handle. Returns '' when the delete is marked (the OS removes the file as the handle closes; no other handle
# can exist under share mode None), else the reason it could not be marked (the file then stays, and the caller fails typed with its path).
function Remove-OwnedFileByHandle {
    param([IO.FileStream]$Stream)
    $why = ''
    try {
        if (-not ('MlvOwnedFileDisposition' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class MlvOwnedFileDisposition {
    [StructLayout(LayoutKind.Sequential)]
    private struct FILE_DISPOSITION_INFO { [MarshalAs(UnmanagedType.U1)] public bool DeleteFile; }
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle hFile, int fileInformationClass, ref FILE_DISPOSITION_INFO info, int bufferSize);
    public static void MarkForDelete(SafeFileHandle handle) {
        FILE_DISPOSITION_INFO info = new FILE_DISPOSITION_INFO();
        info.DeleteFile = true;
        if (!SetFileInformationByHandle(handle, 4, ref info, Marshal.SizeOf(typeof(FILE_DISPOSITION_INFO)))) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
    }
}
'@
        }
        [MlvOwnedFileDisposition]::MarkForDelete($Stream.SafeFileHandle)
    } catch { $why = $_.Exception.Message }
    finally { $Stream.Dispose() }
    return $why
}

# r15 (pass 12 A6): an archive that fails the post-archive secret scan is deleted, and the delete is proven (Remove-FileVerified). Always throws: the plain
# message when the archive is gone, or the typed EXPORT_CREDENTIAL_ZIP_NOT_DELETED <path> (recorded in the run's failures too) when it still exists anywhere.
function Remove-RejectedArchive {
    param([string]$Zip, [string[]]$Late)
    $r = Remove-FileVerified $Zip
    if ($r.Deleted) { throw "the archive (or a staged file it was made from) fails the secret scan, so the archive was deleted: $($Late -join '; ')" }
    $script:Failures.Add("EXPORT_CREDENTIAL_ZIP_NOT_DELETED $($r.Path)")
    throw "EXPORT_CREDENTIAL_ZIP_NOT_DELETED $($r.Path) (the archive fails the secret scan and could NOT be deleted: $($r.Why); delete it by hand and never transfer it): $($Late -join '; ')"
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
        # Project .claude-state: tooling, memory, checkpoints. Run dirs, evidence and worktrees stay behind. The secret scan below judges every staged file; one it
        # takes out (a script with `$Token = '<literal>'`, a ledger line that quotes a synthetic password) is a file the bundle LACKS, so Export ends non-zero with an
        # EXPORT_CREDENTIAL_HIT <path>:<line> line per file (r11 F-B1) rather than shipping the gap with exit 0.
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
            try { $o = (Invoke-GitTraceScrubbed { & $c[0] $c[1..($c.Count - 1)] 2>&1 } | Select-Object -First 1); $v.Add("$($c[0]): $o") } catch { $v.Add("$($c[0]): (missing)") }   # r17 F3
        }
        $v.Add("pwsh: $($PSVersionTable.PSVersion)")
        Write-AsciiFile (Join-Path $ref 'toolchain-versions.txt') (($v -join "`r`n") + "`r`n")
    }
    Invoke-Step 'Census: worktree branches whose tip is not on the fork (push these before cut-over)' {
        $forkRefs = @{}
        foreach ($l in @(Invoke-GitTraceScrubbed { & git -C $Repo ls-remote $LeaseRemote 'refs/heads/*' 2>$null })) {   # r17 F3: Export never lets git append to a trace file
            $p = "$l" -split '\s+'; if ($p.Count -ge 2) { $forkRefs[$p[1] -replace '^refs/heads/', ''] = $p[0] }
        }
        $rows = [System.Collections.Generic.List[string]]::new()
        $rows.Add("status`tbranch`tlocal_sha`tworktree")
        $wt = ''; $sha = ''
        foreach ($l in @(Invoke-GitTraceScrubbed { & git -C $Repo worktree list --porcelain })) {
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
        # p5 A9: a staged file can change between the scan and the moment tar reads it. Judge the stage again now the archive exists; a hit means
        # the archive may hold it, so the archive is deleted and the run fails (the bundle is never offered).
        # r10 A3: judge the ARCHIVE'S OWN entries (what would be offered), not the stage, which can change again between tar and this check.
        $late = @(Get-ArchiveCredentialHits -Zip $zip) + @(Get-StagedCredentialHits -Stage $stage)
        # r15 (pass 12 A6): the delete is PROVEN (Remove-RejectedArchive): a refused delete renames the archive aside and fails typed EXPORT_CREDENTIAL_ZIP_NOT_DELETED
        # with its path; it is never reported as deleted while it still exists.
        if ($late.Count) { Remove-RejectedArchive -Zip $zip -Late $late }
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
# r15 pass 13 A3: true when the text NAMES a secret-named key anywhere, whatever its value (an exempt '' included): any word the key classifier
# (Get-CredentialKeyClass, the same name list the detector uses: 'api.key', 'pass_word', 'auth', 'pwd', 'private.key', camelCase, any separator) calls secret-named,
# judged as written and with JSON \uXXXX escapes decoded. The credential-ROLE guard of Import (Get-ImportCredentialSkips and the locked copy) uses this together with
# $CredentialNameWords, so the role guard is never narrower than the detector's own name list.
function Test-CredentialNamePresent {
    param([string]$Text)
    if (-not $Text) { return $false }
    try {
        # r2 pass 6 A3: an XML naming attribute that names a secret ('p a s s w o r d': blanks removed first) counts with or without an escape in the text
        # (r2 pass 4 A4: a name, whatever the value; r4 sol r3 A1: its raw spelling and every decode level, Get-CredentialXmlAttrPairHitIndex)
        if ((Get-CredentialXmlAttrPairHitIndex -Text $Text -NameOnly) -ge 0) { return $true }
        if (Test-CredentialNameWordPresent $Text) { return $true }
        # r3 pass 7 A2: every decode level is searched for a secret name, not only the fixed point (a reader that decodes once sees the first). r23 (pass 27 A1 sibling):
        # the same decoder as the content guard. r4 (sol r3 A3): each level is judged as it is produced and dropped; past the bound the text names a secret (deny by default).
        # r4 (adversary pass 9 A1, defect class: a check that runs on the raw text only): EVERY name check runs at every level, the XML naming attribute included
        # (config.toml 'xml = "<add key=''p a s s w o r d'' value='''' />"' has no tag until it is decoded)
        $nf = Get-CredentialNormalForm $Text -OnStage { param($lvText, $lvMask) if ((Test-CredentialNameWordPresent $lvText) -or (Get-CredentialXmlAttrPairHitIndex -Text $lvText -NameOnly) -ge 0) { 0 } else { -1 } }
        if ($nf.Hit -ge 0) { return $true }
        $dec = $nf.Text
        if ($script:CredentialResidualIntroRegex.IsMatch($dec)) {
            # DENY-BY-DEFAULT-1 sibling: a key in the decoded copy that still holds an undecodable escape names a secret, as in the content guard (deny by default)
            if ($script:CredentialResidualKeyRegex.IsMatch($dec)) { return $true }
            # r1 adversary A7 sibling: a table header ('["pass\U0011FFFFord"]') holding one counts the same
            foreach ($hm in $script:CredentialTableHeaderRegex.Matches($dec)) { if ($script:CredentialResidualIntroRegex.IsMatch($hm.Groups[1].Value)) { return $true } }
        }
    } catch {
        if (Test-CredentialDecodeLimitError $_) { return $true }   # r4 (sol r3 A3): the typed decode-limit refusal, deny by default
        throw
    }
    return $false
}
# True when a word of the text is secret-named (Get-CredentialKeyClass).
function Test-CredentialNameWordPresent([string]$Text) {
    # r16 (adversary pass 15 A2): the seen set is CASE-SENSITIVE (ordinal). The classifier splits camelCase, so 'authHeader' (auth + header: secret-named) and
    # 'authheader' (one word: not) get different answers; a case-insensitive @{} table skipped the camelCase spelling after its benign lowercase twin.
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($w in [regex]::Matches("$Text", '[A-Za-z0-9_.\-/\\]+')) {   # r19 pass 22 A1: '/' and '\' stand inside a name word, as in the key pattern
        $v = $w.Value
        if (-not $seen.Add($v)) { continue }
        if (Get-CredentialKeyClass $v) { return $true }
    }
    return $false
}

# r3 A5: the full paths of bundle files an Import must NOT copy, judged by CONTENT and ROLE (the file name alone is only the first line of
# defence, $ExcludeFiles). A bundle file is skipped when
#   - it carries a credential itself (a handmade or tampered bundle), or
#   - the file it would replace on this host carries a credential (any name), or
#   - the file it would replace is a credential-ROLE file (settings.json, config.toml, ...) that names a credential-like key at all.
# Returns objects { Source; Dest; Reason }. Reads only; a file that cannot be read counts as carrying a credential (fail closed), except (r17 F1) a host file
# another process HOLDS (an IOException, a sharing or lock violation): that one record is IMPORT_DEST_CHANGED_SKIPPED <path> from the locked copy, a failure (exit 1).
function Get-ImportCredentialSkips {
    param([string]$SourceDir, [string]$DestDir, [long]$MaxScanBytes = 26214400)
    $skips = [System.Collections.Generic.List[object]]::new()
    $SourceDir = [IO.Path]::GetFullPath($SourceDir); $DestDir = [IO.Path]::GetFullPath($DestDir)   # r12 A4: '..' parts would shift every relative path
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) { return @() }
    $roleRx = [regex]::new('(?i)(' + $CredentialNameWords + ')')
    foreach ($f in @(Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $rel = Get-RelativeUnder $SourceDir $f.FullName
        $dest = Join-Path $DestDir $rel
        $reason = ''
        $srcText = $null
        # A file over the scan limit is not read at all and is not copied (fail closed, the same limit the Export scan uses).
        if ($f.Length -gt $MaxScanBytes) { $reason = 'bundle file larger than the scan limit' }
        else { try { $srcText = [IO.File]::ReadAllText($f.FullName) } catch { $reason = 'bundle file unreadable' } }
        if (-not $reason -and (Test-CredentialText $srcText)) { $reason = 'bundle file carries a credential' }
        if (-not $reason -and (Test-Path -LiteralPath $dest -PathType Leaf)) {
            $dstText = $null; $dstOk = $true; $dstHeld = $false
            if ((Get-Item -LiteralPath $dest -Force).Length -gt $MaxScanBytes) { $dstOk = $false }
            else {
                # r17 F1 (opus key3 B1 = sol B4 = fable B1): an IOException here is a host file another process HOLDS with a share mode that denies readers (a sharing or
                # lock violation; FileShare.None, or a writer that denies Read). It is NOT classed as carrying a credential: it is left to Copy-ImportFileLocked, which
                # opens it exclusively and, when the copy rules would replace it, returns 'changed' (IMPORT_DEST_CHANGED_SKIPPED <path>, a recorded failure, exit 1, the
                # host file untouched); a held file the rules would keep is never opened there. Every other read failure (access denied) still fails closed below.
                try { $dstText = [IO.File]::ReadAllText($dest) }
                catch [System.IO.IOException] { $dstHeld = $true }
                catch { $dstOk = $false }
            }
            if ($dstHeld) { }
            elseif (-not $dstOk) { $reason = 'host file unreadable or larger than the scan limit' }
            elseif (Test-CredentialText $dstText) { $reason = 'host file carries a credential' }
            elseif (($CredentialRoleFiles -contains $f.Name.ToLowerInvariant()) -and ($roleRx.IsMatch($dstText) -or (Test-CredentialNamePresent $dstText))) { $reason = 'host credential-role file names a credential key' }
        }
        if ($reason) { $skips.Add([pscustomobject]@{ Source = $f.FullName; Dest = $dest; Reason = $reason }) }
    }
    return $skips.ToArray()
}

# r11 S-B2: would robocopy's rules (the /XO /XN /XC switches the caller passed) replace Dest with Source? newer = Source is newer than Dest (/XN excludes it),
# older = Source is older (/XO excludes it), changed = the same timestamp with a different size (/XC excludes it). Equal time and size is the same file.
function Test-ImportReplaces {
    param([System.IO.FileInfo]$Source, [System.IO.FileInfo]$Dest, [bool]$ExcludeNewer, [bool]$ExcludeOlder, [bool]$ExcludeChanged)
    $s = $Source.LastWriteTimeUtc; $d = $Dest.LastWriteTimeUtc
    if ($s -gt $d) { return (-not $ExcludeNewer) }
    if ($s -lt $d) { return (-not $ExcludeOlder) }
    if ($Source.Length -ne $Dest.Length) { return (-not $ExcludeChanged) }
    return $false
}

# r11 S-B2: the ONE place an Import copy writes a host file. The credential check and the replace act on the SAME bytes, because the host file is opened
# exclusively (FileShare.None) and that handle is held across the re-read, the credential check and the write:
#   - destination absent: created with FileMode.CreateNew (it cannot overwrite a file that appeared a moment ago; that falls through to the existing-file path);
#   - destination present: replaced only when the copy rules say so, and only while locked and judged clean; a destination that cannot be locked, or that now
#     carries a credential (or is a credential-role file naming a credential key), is NOT replaced (Result 'changed').
# The bundle bytes are read once and judged too. Returns @{ Result = 'created'|'replaced'|'kept'|'changed'|'source-credential'; Why }.
function Copy-ImportFileLocked {
    param([System.IO.FileInfo]$Source, [string]$Dest, [bool]$ExcludeNewer, [bool]$ExcludeOlder, [bool]$ExcludeChanged, [long]$MaxScanBytes = 26214400)
    $bytes = [IO.File]::ReadAllBytes($Source.FullName)
    # r17 pass 19 A5: the size limit is judged on the bytes actually read (a bundle file that grew past it after the inspection is skipped, as the inspection would have)
    if ($bytes.Length -gt $MaxScanBytes) { return @{ Result = 'source-credential'; Why = 'bundle file larger than the scan limit' } }
    $sr = [IO.StreamReader]::new([IO.MemoryStream]::new($bytes), [Text.Encoding]::UTF8, $true)
    try { $srcText = $sr.ReadToEnd() } finally { $sr.Dispose() }
    if (Test-CredentialText $srcText) { return @{ Result = 'source-credential'; Why = 'bundle file carries a credential' } }
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $Dest))
    $fs = $null
    try {
        $created = $false
        # r16 A4: created through a handle that can also delete it (New-OwnedFileStream), so a failed write takes back exactly this file
        try { $fs = New-OwnedFileStream -Path $Dest -Mode ([IO.FileMode]::CreateNew); $created = $true }
        catch [System.IO.IOException] { if (-not [IO.File]::Exists($Dest)) { throw } }
        if ($created) {
            # r12 A5: the timestamps are set THROUGH the handle that is still held (nothing can save into the file between the write and its timestamp, and a later
            # import's /XO rule reads the file's real time). The old order released the lock first and set the time by path, which backdated an editor's save.
            try {
                $fs.Write($bytes, 0, $bytes.Length); $fs.Flush()
                [IO.File]::SetCreationTimeUtc($fs.SafeFileHandle, $Source.CreationTimeUtc)
                [IO.File]::SetLastWriteTimeUtc($fs.SafeFileHandle, $Source.LastWriteTimeUtc)
                if ($script:ImportAfterWriteHook) { & $script:ImportAfterWriteHook $Dest }
            }
            catch {
                $orig = $_
                # r15 (pass 12 A6 sibling): the half-written new file's removal is proven; one that cannot be removed fails typed with its path.
                # r16 (adversary pass 15 A4): removed THROUGH the held handle (Remove-OwnedFileByHandle), never by pathname after the handle is released, so a file another
                # process saves at $Dest can never be the one deleted.
                $held = $fs; $fs = $null
                $rmWhy = Remove-OwnedFileByHandle $held
                if ($rmWhy) { throw "IMPORT_PARTIAL_NOT_DELETED $Dest ($rmWhy) after: $($orig.Exception.Message)" }
                throw $orig
            }
            $fs.Dispose(); $fs = $null
            $keepAttr = $Source.Attributes -band ([IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System)
            if ($keepAttr) { [IO.File]::SetAttributes($Dest, ([IO.File]::GetAttributes($Dest) -bor $keepAttr)) }
            Register-ImportAppliedIdentity -Path $Dest -Bytes $bytes -WriteTimeUtc $Source.LastWriteTimeUtc   # r14 A4: what the copy wrote, for the settings rewrite
            return @{ Result = 'created' }
        }
        # Cheap pre-check without a lock: a destination the rules would not replace is never opened (a running hook may hold it).
        if (-not (Test-ImportReplaces -Source $Source -Dest ([IO.FileInfo]::new($Dest)) -ExcludeNewer $ExcludeNewer -ExcludeOlder $ExcludeOlder -ExcludeChanged $ExcludeChanged)) { return @{ Result = 'kept' } }
        try { $fs = [IO.File]::Open($Dest, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch { return @{ Result = 'changed'; Why = "host file cannot be locked for the replace ($($_.Exception.Message))" } }
        # From here the host file is ours alone: the timestamps, the content and the write below are one atomic view of it.
        if (-not (Test-ImportReplaces -Source $Source -Dest ([IO.FileInfo]::new($Dest)) -ExcludeNewer $ExcludeNewer -ExcludeOlder $ExcludeOlder -ExcludeChanged $ExcludeChanged)) { return @{ Result = 'kept' } }
        if ($fs.Length -gt $MaxScanBytes) { return @{ Result = 'changed'; Why = 'host file larger than the scan limit' } }
        $orig = Read-ImportHeldBytes $fs
        $dstText = ConvertFrom-ImportBytes $orig
        if (Test-CredentialText $dstText) { return @{ Result = 'changed'; Why = 'host file carries a credential' } }
        if (($CredentialRoleFiles -contains $Source.Name.ToLowerInvariant()) -and (([regex]::new('(?i)(' + $CredentialNameWords + ')')).IsMatch($dstText) -or (Test-CredentialNamePresent $dstText))) { return @{ Result = 'changed'; Why = 'host credential-role file names a credential key' } }
        # r14 A5: never truncate first. The bundle bytes go to a sibling temp file carrying the bundle timestamp (r12 A5: the timestamp is set before the file takes the host
        # name, so no editor save can be backdated), and File.Replace swaps it in; a write that fails leaves the host file's bytes and timestamp as they were.
        $swap = Set-ImportFileContentAtomic -Dest $Dest -Bytes $bytes -WriteTimeUtc $Source.LastWriteTimeUtc -Held $fs -JudgedBytes $orig
        $fs = $null
        if ($swap -ceq 'changed') { return @{ Result = 'changed'; Why = 'host file was saved while the replace ran (that save was put back)' } }
        Register-ImportAppliedIdentity -Path $Dest -Bytes $bytes -WriteTimeUtc $Source.LastWriteTimeUtc   # r14 A4
        return @{ Result = 'replaced' }
    } finally { if ($fs) { $fs.Dispose() } }
}

# r14 A5: all bytes of a host file this run holds open (from the start), without closing the handle.
function Read-ImportHeldBytes {
    param([IO.FileStream]$Stream)
    [void]$Stream.Seek(0, [IO.SeekOrigin]::Begin)
    $ms = [IO.MemoryStream]::new()
    $Stream.CopyTo($ms)
    return , $ms.ToArray()
}

# The text of a file's bytes as Import judges it (UTF-8, a byte order mark honoured).
function ConvertFrom-ImportBytes {
    param([byte[]]$Bytes)
    $sr = [IO.StreamReader]::new([IO.MemoryStream]::new($Bytes), [Text.Encoding]::UTF8, $true)
    # r19 pass 22 A4: a UTF-16 file WITHOUT a byte order mark decodes as NUL-separated text ('p<NUL>a<NUL>s...') that no rule matches; the NULs are removed so the same text is judged
    # (a NUL never stands in a real credential file, so removing them only ever widens what is refused).
    try { return $sr.ReadToEnd().Replace([string][char]0, '') } finally { $sr.Dispose() }
}

function Get-ImportBytesHash {
    param([byte[]]$Bytes)
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Bytes))
}

# r14 A4: the identity (SHA-256 and last-write time) of what an Import copy wrote to a host file, keyed by its normalised path. The settings.json rewrite only touches a
# file whose identity still matches (an editor's newer save is kept). Without -Bytes the file is read as it stands now.
$script:ImportAppliedIdentity = @{}
function Register-ImportAppliedIdentity {
    param([string]$Path, [byte[]]$Bytes = $null, [Nullable[DateTime]]$WriteTimeUtc = $null)
    $full = [IO.Path]::GetFullPath($Path)
    if ($null -eq $Bytes) { $Bytes = [IO.File]::ReadAllBytes($full); $WriteTimeUtc = [IO.File]::GetLastWriteTimeUtc($full) }
    $script:ImportAppliedIdentity[$full.ToLowerInvariant()] = @{ Hash = (Get-ImportBytesHash $Bytes); WriteTimeUtc = $WriteTimeUtc }
}

# r14 A5: replace the content of a host file this run holds locked (FileShare.None) WITHOUT truncating it first:
#   1. the new bytes are written in full to a sibling temp file (CreateNew) and flushed; its last-write time is set when one is given. A failure here (a full disk)
#      removes the temp file and throws; the host file was never written, so its bytes and timestamp are as they were (the next /XO import still repairs it).
#   2. the lock is released and File.Replace swaps the temp file in, keeping the old host file as a backup (one step; a failure throws with the host file intact).
#   3. the backup must be the bytes judged under the lock; when it is not, someone saved in the instant between 2's release and the swap, and their file is put back
#      ('changed'). Otherwise the backup is removed ('replaced').
# Test seams (always $null in a real run): ImportWriteFaultHook runs halfway through writing the temp file (a throw stands for the disk failing);
# ImportAfterWriteHook runs once the bytes and the timestamp are written, while the host file is still locked (it stands for an editor saving).
function Set-ImportFileContentAtomic {
    param([string]$Dest, [byte[]]$Bytes, [Nullable[DateTime]]$WriteTimeUtc, [IO.FileStream]$Held, [byte[]]$JudgedBytes)
    $dir = Split-Path -Parent $Dest
    $base = Join-Path $dir ('.' + (Split-Path -Leaf $Dest) + '.mlvimport-' + $PID + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $tmp = "$base.tmp"; $bak = "$base.bak"
    $ts = $null
    try {
        $ts = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $half = [int][Math]::Floor($Bytes.Length / 2)
        $ts.Write($Bytes, 0, $half)
        if ($script:ImportWriteFaultHook) { & $script:ImportWriteFaultHook $Dest }
        $ts.Write($Bytes, $half, $Bytes.Length - $half)
        $ts.Flush($true)
        # r15 (pass 12 A4): this run's file is identified by its SHA-256 AND its last-write time, so the time is always set explicitly through the handle (the bundle
        # time when one is given, else now) and recorded; the rollback below compares both.
        $ourTime = $(if ($null -ne $WriteTimeUtc) { [datetime]$WriteTimeUtc } else { [DateTime]::UtcNow })   # (PowerShell unwraps the Nullable: no .Value)
        [IO.File]::SetLastWriteTimeUtc($ts.SafeFileHandle, $ourTime)
        $ourHash = Get-ImportBytesHash $Bytes
        $ts.Dispose(); $ts = $null
        if ($script:ImportAfterWriteHook) { & $script:ImportAfterWriteHook $Dest }
        # r14 pass 11 A4: the judged identity is the bytes AND the last-write time, read under the lock (an identical save with a newer time is a newer file)
        $judgedTime = [IO.File]::GetLastWriteTimeUtc($Held.SafeFileHandle)
        $Held.Dispose()   # File.Replace cannot swap a file this run still holds open
        if ($script:ImportBeforeSwapHook) { & $script:ImportBeforeSwapHook $Dest }
        [IO.File]::Replace($tmp, $Dest, $bak, $true)
    } catch {
        $orig = $_
        if ($ts) { $ts.Dispose() }
        # r15 (pass 12 A6 sibling): the temp file's removal is proven; one that cannot be removed fails typed with its path
        if (Test-Path -LiteralPath $tmp) {
            $rm = Remove-FileVerified $tmp
            if (-not $rm.Deleted) { throw "IMPORT_TEMP_NOT_DELETED $($rm.Path) ($($rm.Why)) after: $($orig.Exception.Message)" }
        }
        throw $orig
    }
    if ($script:ImportAfterSwapHook) { & $script:ImportAfterSwapHook $Dest }
    # r14 pass 11 A5: every step after the swap is guarded; an error never deletes the backup (the host's previous file survives in it, named in the error).
    try {
        $was = [IO.File]::ReadAllBytes($bak)
        $wasTime = [IO.File]::GetLastWriteTimeUtc($bak)
    } catch { throw "IMPORT_REPLACE_UNVERIFIED $Dest (the previous host file is kept at $bak and could not be read back: $($_.Exception.Message))" }
    if ((Get-ImportBytesHash $was) -ceq (Get-ImportBytesHash $JudgedBytes) -and $wasTime -eq $judgedTime) {
        # r15 (pass 12 A6): the backup's removal is proven (Remove-FileVerified); one that cannot be removed fails typed with its path, never silently kept
        $rm = Remove-FileVerified $bak
        if (-not $rm.Deleted) { throw "IMPORT_ROLLBACK_BACKUP_NOT_DELETED $($rm.Path) (the previous host file of $Dest could not be removed: $($rm.Why))" }
        return 'replaced'
    }
    # Someone saved between the release and the swap; their file is in $bak. r15 (pass 12 A4 / A5): put it back with ONE File.Replace(backup, dest, aside) done
    # UNDER a lock that is never released before that Replace. The lock (FileShare Read|Delete) blocks every in-place writer and still lets File.Replace swap the
    # path. What stood at the host path at the instant of the Replace lands in the aside, so nothing can be overwritten unseen: the aside must be THIS run's file,
    # by its SHA-256 AND its last-write time ($ourHash / $ourTime). When either differs (an identical-bytes save with a newer time, or a rename-over save the lock
    # cannot block), that newer save is put back at once and the rollback refuses typed IMPORT_ROLLBACK_DEST_CHANGED; the older interleaved save is kept and named.
    $h = $null
    $aside = "$base.aside"
    try {
        try { $h = [IO.File]::Open($Dest, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::Read -bor [IO.FileShare]::Delete)) }
        catch { throw "IMPORT_REPLACE_UNVERIFIED $Dest (an interleaved save is kept at $bak; the host file could not be locked to restore it: $($_.Exception.Message))" }
        # r15 pass 13 A4: BEFORE any Replace, prove under the lock what it would displace. When the host path no longer holds this run's file (bytes AND time), a save
        # landed after the swap: it is the newest and stays live; the interleaved save in $bak is older and superseded (removed, proven). Nothing is replaced.
        $cur = Read-ImportHeldBytes $h
        $curTime = [IO.File]::GetLastWriteTimeUtc($h.SafeFileHandle)
        if ((Get-ImportBytesHash $cur) -cne $ourHash -or $curTime -ne $ourTime) {
            $h.Dispose(); $h = $null
            $rm = Remove-FileVerified $bak
            $bakNote = $(if ($rm.Deleted) { 'the older interleaved save it superseded was removed' } else { "the older interleaved save could NOT be removed and is at $($rm.Path) ($($rm.Why))" })
            throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (a save landed after the swap and is kept live; $bakNote)"
        }
        if ($script:ImportBeforeRollbackHook) { & $script:ImportBeforeRollbackHook $Dest }
        [IO.File]::Replace($bak, $Dest, $aside, $true)   # while $h is still held: nothing can have changed the host path since the check above
        $h.Dispose(); $h = $null
        try {
            $asideBytes = [IO.File]::ReadAllBytes($aside)
            $asideTime = [IO.File]::GetLastWriteTimeUtc($aside)
        } catch { throw "IMPORT_REPLACE_UNVERIFIED $Dest (the interleaved save was restored; the file it displaced is kept at $aside and could not be read back: $($_.Exception.Message))" }
        if ((Get-ImportBytesHash $asideBytes) -cne $ourHash -or $asideTime -ne $ourTime) {
            # belt and braces (the lock refuses in-place and rename-over saves, so this is not expected): a save displaced into the aside is put back, again only after
            # proving UNDER A LOCK that the host path still holds the interleaved save just restored; a newer save there stays live and the aside is kept and named.
            $older = "$base.older"
            $h2 = $null
            try {
                try { $h2 = [IO.File]::Open($Dest, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::Read -bor [IO.FileShare]::Delete)) }
                catch { throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (a save landed while the rollback ran and is kept at $aside; the host file could not be locked to put it back: $($_.Exception.Message))" }
                $cur2 = Read-ImportHeldBytes $h2
                if ((Get-ImportBytesHash $cur2) -cne (Get-ImportBytesHash $was) -or [IO.File]::GetLastWriteTimeUtc($h2.SafeFileHandle) -ne $wasTime) {
                    throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (two saves landed while the rollback ran: the newest stays live at $Dest and the other is kept at $aside)"
                }
                if ($script:ImportBeforeAsideRestoreHook) { & $script:ImportBeforeAsideRestoreHook $Dest }   # test seam: an editor's rename-over save in the instant before the Replace
                try { [IO.File]::Replace($aside, $Dest, $older, $true) }   # while $h2 is still held
                catch { throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (a save landed while the rollback ran and is kept at $aside; putting it back failed: $($_.Exception.Message); the older interleaved save is at $Dest)" }
                # r19 pass 21 A4: a rename-over save the lock cannot block may land between the check above and this Replace. What the Replace displaced (now at $older) must be the
                # interleaved save read above (bytes AND time); anything else is a NEWER save, and it goes back to the host path at once (the file the Replace just put there, the older
                # one, moves aside to $undo). The newest save is therefore always the live file; the older saves are kept and named, never deleted.
                # r25 (adversary pass 29 A3, deny by default): an unreadable $older is NOT proof that it is the interleaved save (it used to read as 'is the interleaved save', so a newer
                # save held open at that instant stayed unrestored while the message said it had been put back). Unreadable now takes the restore path below, whose Replace fails
                # typed when the file really is held and names both files.
                $olderIsWas = $false
                $olderHash = $null; $olderTime = $null   # r25 pass 30 A1: the identity of the file the restore below will put back (kept to verify what the terminal Replace displaces)
                try { $olderHash = Get-ImportBytesHash ([IO.File]::ReadAllBytes($older)); $olderTime = [IO.File]::GetLastWriteTimeUtc($older); $olderIsWas = ($olderHash -ceq (Get-ImportBytesHash $was) -and $olderTime -eq $wasTime) } catch { $olderIsWas = $false; $olderHash = $null }
                if (-not $olderIsWas) {
                    $undo = "$base.undo"
                    if ($script:ImportBeforeUndoHook) { & $script:ImportBeforeUndoHook $Dest }   # r25 test seam: a save in the instant before this Replace
                    try { [IO.File]::Replace($older, $Dest, $undo, $true) }
                    catch { throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (a newer save landed while the rollback ran and is kept at $older; putting it back failed: $($_.Exception.Message); an older save is at $Dest)" }
                    # r25 (adversary pass 29 A3, pass 30 A1): this Replace is itself a window. What it displaced (now at $undo) must be the file that stood at the host path just
                    # before it: the one the Replace above put there, which is the $aside content ($asideBytes / $asideTime, read above; NOT $was, which is a different file).
                    # Anything else is a save that landed after the check, i.e. the NEWEST, and it goes back to the host path at once. Nothing is deleted: the file the second
                    # Replace displaces is kept at $undo2 and named, and it is verified against the identity of what was restored ($olderHash / $olderTime): when it is not that
                    # file, a still newer save landed in the instant of the restore and the message says so instead of claiming the newest is live.
                    $undoIsPrev = $false
                    try { $undoIsPrev = ((Get-ImportBytesHash ([IO.File]::ReadAllBytes($undo))) -ceq (Get-ImportBytesHash $asideBytes) -and [IO.File]::GetLastWriteTimeUtc($undo) -eq $asideTime) } catch { $undoIsPrev = $false }
                    if (-not $undoIsPrev) {
                        $undo2 = "$base.undo2"
                        try { [IO.File]::Replace($undo, $Dest, $undo2, $true) }
                        catch { throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (a newest save landed in the instant of the restore and is kept at $undo; putting it back failed: $($_.Exception.Message); an older save is at $Dest)" }
                        $undo2IsRestored = $false
                        try { $undo2IsRestored = ($null -ne $olderHash -and (Get-ImportBytesHash ([IO.File]::ReadAllBytes($undo2))) -ceq $olderHash -and [IO.File]::GetLastWriteTimeUtc($undo2) -eq $olderTime) } catch { $undo2IsRestored = $false }
                        if ($undo2IsRestored) { throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (saves landed while the rollback ran: the newest is live at $Dest again; the older save it displaced is kept at $undo2)" }
                        throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (saves landed while the rollback ran: a further save landed in the instant of the restore; the file the restore displaced is kept at $undo2 and was NOT verified to be older than the live file at $Dest)"
                    }
                    throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (saves landed while the rollback ran: the newest is live at $Dest; the older save it displaced is kept at $undo)"
                }
            } finally { if ($h2) { $h2.Dispose() } }
            # the displaced file is the older interleaved save read above (bytes AND time): superseded by the newer save, it is removed (proven). Anything else is kept.
            $olderNote = "the older interleaved save is kept at $older"
            try {
                if ((Get-ImportBytesHash ([IO.File]::ReadAllBytes($older))) -ceq (Get-ImportBytesHash $was) -and [IO.File]::GetLastWriteTimeUtc($older) -eq $wasTime) {
                    $rm = Remove-FileVerified $older
                    $olderNote = $(if ($rm.Deleted) { 'the older interleaved save it superseded was removed' } else { "the older interleaved save could NOT be removed and is at $($rm.Path) ($($rm.Why))" })
                }
            } catch { $olderNote = "$olderNote (it could not be read back: $($_.Exception.Message))" }
            throw "IMPORT_ROLLBACK_DEST_CHANGED $Dest (a save landed while the rollback ran; it was put back at $Dest; $olderNote)"
        }
        $rm = Remove-FileVerified $aside
        if (-not $rm.Deleted) { throw "IMPORT_ROLLBACK_BACKUP_NOT_DELETED $($rm.Path) (this run's displaced copy of $Dest could not be removed: $($rm.Why))" }
        return 'changed'
    } catch {
        if ($_.Exception.Message -match '^IMPORT_[A-Z_]+ ') { throw }
        throw "IMPORT_REPLACE_UNVERIFIED $Dest (an interleaved save is kept at $bak; restoring it failed: $($_.Exception.Message))"
    } finally { if ($h) { $h.Dispose() } }
}

# One Import copy that refuses credential-bearing files (r3 A5) and replaces nothing it has not judged under a lock (r11 S-B2). Reads the same switches the
# robocopy calls used (/E /S /XF <names or full paths> /XD <full paths> /XO /XN /XC); robocopy itself no longer writes a host file here.
#   inspection (Get-ImportCredentialSkips): the bundle files and host files that already carry a credential are skipped up front, each logged as
#       IMPORT_SKIPPED_CREDENTIAL_FILE;
#   copy (Copy-ImportFileLocked): a host file that cannot be locked, or that gained a credential since the inspection, is skipped with
#       IMPORT_DEST_CHANGED_SKIPPED <path> and the run fails (exit 1).
function Invoke-ImportRobocopy {
    param([string]$From, [string]$To, [string[]]$Extra = @())
    # r12 A4: every path is normalised once, here: the tree walk below slices FullName values (always normalised) by the length of $From, and the /XF /XD paths are compared
    # with those same FullName values, so a '..' or '.' part in -StateRoot or the bundle path must not survive.
    $From = [IO.Path]::GetFullPath($From); $To = [IO.Path]::GetFullPath($To)
    $skips = @(Get-ImportCredentialSkips -SourceDir $From -DestDir $To)
    foreach ($s in $skips) {
        Write-Log "IMPORT_SKIPPED_CREDENTIAL_FILE $($s.Dest) ($($s.Reason); the bundle copy $($s.Source) was not applied)" 'WARN'
        $script:ImportSkippedDest.Add($s.Dest)
    }
    if (-not (Test-Path -LiteralPath $From)) { Write-Log "  (absent, skipped) $From" 'WARN'; return }
    $xfNames = [System.Collections.Generic.List[string]]::new(); $xfPaths = [System.Collections.Generic.List[string]]::new(); $xdPaths = [System.Collections.Generic.List[string]]::new()
    $recurse = $false; $exN = $false; $exO = $false; $exC = $false; $mode = ''
    foreach ($tok in $Extra) {
        if ($tok.StartsWith('/')) {
            $mode = ''
            switch -Regex ($tok.ToUpperInvariant()) { '^/(E|S)$' { $recurse = $true } '^/XN$' { $exN = $true } '^/XO$' { $exO = $true } '^/XC$' { $exC = $true } '^/XF$' { $mode = 'F' } '^/XD$' { $mode = 'D' } }
        }
        elseif ($mode -eq 'F') { if ($tok -match '[\\:]') { $xfPaths.Add([IO.Path]::GetFullPath($tok).TrimEnd('\')) } else { $xfNames.Add($tok) } }
        elseif ($mode -eq 'D') { $xdPaths.Add($(if ($tok -match '[\\:]') { [IO.Path]::GetFullPath($tok).TrimEnd('\') } else { $tok })) }
    }
    # Test seam: runs between the inspection above and the replace below (the window an editor can save a credential into). Null in every real run.
    if ($script:ImportBeforeReplaceHook) { & $script:ImportBeforeReplaceHook $From $To }
    $skipSet = @{}
    foreach ($s in $skips) { $skipSet[$s.Source.ToLowerInvariant()] = $true }
    $n = @{ created = 0; replaced = 0; kept = 0; skipped = 0; failed = 0 }
    $root = $From.TrimEnd('\')
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $stack.Push($root)
    while ($stack.Count) {
        $dir = $stack.Pop()
        $rel = $(if ($dir -ieq $root) { '' } else { Get-RelativeUnder $root $dir })
        $destDir = $(if ($rel) { Join-Path $To $rel } else { $To })
        try { [void][IO.Directory]::CreateDirectory($destDir) }
        catch { $n.failed++; Write-Log "IMPORT_COPY_FAILED $destDir :: $($_.Exception.Message)" 'ERROR'; $script:Failures.Add("IMPORT_COPY_FAILED $destDir"); continue }
        foreach ($e in @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)) {
            if ($e.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }   # /XJ
            if ($e.PSIsContainer) {
                $skipDir = $false
                foreach ($x in $xdPaths) { if ($x -match '[\\:]') { if ($e.FullName.TrimEnd('\') -ieq $x) { $skipDir = $true } } elseif ($e.Name -like $x) { $skipDir = $true } }
                if ($recurse -and -not $skipDir) { $stack.Push($e.FullName) }
                continue
            }
            $skipFile = $skipSet.ContainsKey($e.FullName.ToLowerInvariant())
            foreach ($x in $xfNames) { if ($e.Name -like $x) { $skipFile = $true } }
            foreach ($x in $xfPaths) { if ($e.FullName -ieq $x) { $skipFile = $true } }
            if ($skipFile) { continue }
            $dest = Join-Path $destDir $e.Name
            try { $r = Copy-ImportFileLocked -Source $e -Dest $dest -ExcludeNewer $exN -ExcludeOlder $exO -ExcludeChanged $exC }
            catch { $n.failed++; Write-Log "IMPORT_COPY_FAILED $dest :: $($_.Exception.Message)" 'ERROR'; $script:Failures.Add("IMPORT_COPY_FAILED $dest"); continue }
            switch ($r.Result) {
                'created' { $n.created++; $script:ImportAppliedDest.Add($dest) }
                'replaced' { $n.replaced++; $script:ImportAppliedDest.Add($dest) }
                'kept' { $n.kept++ }
                'source-credential' {
                    $n.skipped++
                    Write-Log "IMPORT_SKIPPED_CREDENTIAL_FILE $dest ($($r.Why); the bundle copy $($e.FullName) was not applied)" 'WARN'
                    $script:ImportSkippedDest.Add($dest)
                }
                'changed' {
                    $n.skipped++
                    Write-Log "IMPORT_DEST_CHANGED_SKIPPED $dest ($($r.Why); the host file was left as it is, the bundle copy $($e.FullName) was not applied)" 'ERROR'
                    $script:ImportSkippedDest.Add($dest)
                    $script:Failures.Add("IMPORT_DEST_CHANGED_SKIPPED $dest")
                }
            }
        }
    }
    Write-Log "  import copy $From -> $To : created $($n.created), replaced $($n.replaced), kept $($n.kept) (older or the same), skipped $($n.skipped), failed $($n.failed)"
}

$script:ImportSkippedDest = [System.Collections.Generic.List[string]]::new()
# r12 pass 9 B3: every destination a copy actually wrote ('created' or 'replaced' by Copy-ImportFileLocked), normalised paths. The settings.json profile rewrite reads this, not a
# timestamp sampled before the copy: a file the copy KEPT (a newer or an identical host file) is the host's own and is never rewritten.
$script:ImportAppliedDest = [System.Collections.Generic.List[string]]::new()
$script:ImportSettingsApplied = $false
# Test seam for r11 S-B2 (see Invoke-ImportRobocopy): a scriptblock called with (From, To) between the inspection and the replace. Always $null in a real run.
$script:ImportBeforeReplaceHook = $null
# Test seams for r12 A3 / A5: a scriptblock called with the destination path (a) after Copy-ImportFileLocked wrote the bytes and set the timestamps, while the host file is still
# locked; (b) after the settings.json profile rewrite read the file, before it writes. Both stand for an editor saving into the host file; always $null in a real run.
$script:ImportAfterWriteHook = $null
$script:ImportSettingsRewriteHook = $null
# Test seam for r14 A5 (Set-ImportFileContentAtomic): a scriptblock called with the destination path halfway through writing the replacement; a throw stands for the write
# failing (a full disk). Always $null in a real run.
$script:ImportWriteFaultHook = $null
# Test seams for r14 pass 11 A4 / A5: called with the destination path (a) after the host file's lock is released and before File.Replace, (b) right after File.Replace.
# Both stand for an editor saving in that instant. Always $null in a real run.
$script:ImportBeforeSwapHook = $null
$script:ImportAfterSwapHook = $null
# Test seam for r15 (pass 12 A5): called with the destination path while the rollback holds its lock on the host file, right before the rollback's File.Replace. It stands
# for an editor saving in that instant (an in-place write is refused by the lock; a rename-over save lands and must be put back). Always $null in a real run.
$script:ImportBeforeRollbackHook = $null
# r19 pass 22 A6: test seam, called right before the rollback's second Replace (an editor's rename-over save in that instant). It MUST be initialised here: the script runs under
# strict mode, and an unset script variable read in the rollback threw before the newest save could be put back. Always $null in a real run.
$script:ImportBeforeAsideRestoreHook = $null
$script:ImportBeforeUndoHook = $null   # r25 test seam (pass 29 A3): the instant before the newest-save restore Replace

# r12 A3: the settings.json profile rewrite (VIRTUAL-TEN profile -> this profile) as ONE locked read-judge-write on the host file, the same discipline as
# Copy-ImportFileLocked: the file is opened exclusively (FileShare.None), read, judged by Test-CredentialText, and written back through the SAME handle. It used to be
# ReadAllText then WriteAllText, so an editor saving a credential between the two had it overwritten with the older clean text.
# Returns @{ Result = 'rewritten'|'changed'; Why }. 'changed' (cannot be locked, larger than the scan limit, carries a credential) leaves the file as it is and records the
# typed IMPORT_DEST_CHANGED_SKIPPED <path> failure.
function Update-ImportedSettingsLocked {
    param([string]$Path, [string]$FromProfile, [string]$ToProfile, [long]$MaxScanBytes = 26214400)
    $fs = $null
    $why = ''
    # r14 A4: the identity of what the Import copy wrote to this file (Register-ImportAppliedIdentity); none = the copy did not write it, so it is not rewritten.
    $expect = $script:ImportAppliedIdentity[([IO.Path]::GetFullPath($Path)).ToLowerInvariant()]
    try {
        try { $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch { $why = "host file cannot be locked for the rewrite ($($_.Exception.Message))" }
        if (-not $why) {
            if ($fs.Length -gt $MaxScanBytes) { $why = 'host file larger than the scan limit' }
            else {
                $orig = Read-ImportHeldBytes $fs
                $text = ConvertFrom-ImportBytes $orig
                if (Test-CredentialText $text) { $why = 'host file carries a credential' }
                # r14 A4: re-read under the lock, right before the rewrite: the bytes and the last-write time must still be what the copy wrote. An editor's save after
                # the copy released its lock (a newer host file) is kept, and the refusal is typed.
                elseif (-not $expect) { $why = 'no record of the Import copy writing this file' }
                elseif ((Get-ImportBytesHash $orig) -cne $expect.Hash -or [IO.File]::GetLastWriteTimeUtc($fs.SafeFileHandle) -ne $expect.WriteTimeUtc) { $why = 'host file changed after the Import copy wrote it (the newer save is kept)' }
            }
        }
        if (-not $why) {
            if ($script:ImportSettingsRewriteHook) { & $script:ImportSettingsRewriteHook $Path }
            $new = $text.Replace(($FromProfile -replace '\\', '/'), ($ToProfile -replace '\\', '/')).Replace(($FromProfile -replace '\\', '\\'), ($ToProfile -replace '\\', '\\'))
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes($new)
            # r14 A5: never truncate first (Set-ImportFileContentAtomic): a write that fails leaves the bytes and the timestamp as they were, and throws.
            $swap = Set-ImportFileContentAtomic -Dest ([IO.Path]::GetFullPath($Path)) -Bytes $bytes -WriteTimeUtc $null -Held $fs -JudgedBytes $orig
            $fs = $null
            if ($swap -ceq 'replaced') { return @{ Result = 'rewritten' } }
            $why = 'host file was saved while the rewrite ran (that save was put back)'
        }
    } finally { if ($fs) { $fs.Dispose() } }
    Write-Log "IMPORT_DEST_CHANGED_SKIPPED $Path ($why; the settings.json profile rewrite was not applied, the host file was left as it is)" 'ERROR'
    $script:Failures.Add("IMPORT_DEST_CHANGED_SKIPPED $Path")
    return @{ Result = 'changed'; Why = $why }
}

# r10 pass 7 A2: read the file ONCE, judge those bytes, and write those same bytes (a host editor saving a credential between a check and a Copy-Item
# can no longer put it into the backup). $true = written; $false = carries a credential, unreadable, or could not be written (nothing is left behind).
function Copy-CleanBackupFile {
    param([string]$Path, [string]$DestinationDir)
    $dest = Join-Path $DestinationDir (Split-Path -Leaf $Path)
    $ws = $null
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
        $sr = [IO.StreamReader]::new([IO.MemoryStream]::new($bytes), [Text.Encoding]::UTF8, $true)
        try { $text = $sr.ReadToEnd() } finally { $sr.Dispose() }
        if (Test-CredentialText $text) { return $false }
        [void][IO.Directory]::CreateDirectory($DestinationDir)
        # r16 (adversary pass 15 A4 sibling): written through a handle that can also delete it (New-OwnedFileStream), so a failed write takes back exactly this file
        $ws = New-OwnedFileStream -Path $dest -Mode ([IO.FileMode]::Create)
        $ws.Write($bytes, 0, $bytes.Length); $ws.Flush()
        $ws.Dispose(); $ws = $null
        return $true
    } catch {
        # r15 (pass 12 A6 sibling): a partly written backup copy is removed and the removal is proven; one that cannot be removed is a typed failure of the run.
        # r16 (adversary pass 15 A4 sibling): removed THROUGH the held handle, never by pathname: a failure before this run opened $dest (an unreadable source) deletes
        # nothing, and a file another process saved at $dest after a release can never be the one deleted.
        if ($ws) {
            $held = $ws; $ws = $null
            $rmWhy = Remove-OwnedFileByHandle $held
            if ($rmWhy) {
                Write-Log "IMPORT_BACKUP_NOT_DELETED $dest ($rmWhy)" 'ERROR'
                $script:Failures.Add("IMPORT_BACKUP_NOT_DELETED $dest")
            }
        }
        return $false
    }
}

function Invoke-PhaseImport {
    if (-not (Test-Path -LiteralPath $BundlePath)) {
        Write-Log "no bundle at ${BundlePath}: run -Phase Export on VIRTUAL-TEN and copy the zip here (README step 4), then re-run -Phase Import" 'MANUAL'
        # r16 (adversary pass 15 A3): nothing was imported, so the run FAILS (exit 1, typed IMPORT_BUNDLE_MISSING), like the r15 IMPORT_BUNDLE_INVALID refusal below; it
        # used to return quietly and end 'done' with exit 0. A -WhatIf preview imports nothing in any case and stays a plain notice.
        if (-not $WhatIfPreference) { Write-Log "IMPORT_BUNDLE_MISSING $BundlePath" 'ERROR'; $script:Failures.Add("IMPORT_BUNDLE_MISSING $BundlePath") }
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
    # r15 pass 14 A5: a refused Import is a FAILURE of the run (exit 1), never a quiet return that ends 'done' with exit 0
    if (-not $WhatIfPreference -and -not (Test-Path -LiteralPath (Join-Path $src 'MANIFEST.txt'))) { Write-Log "IMPORT_BUNDLE_INVALID not a migrate bundle (no MANIFEST.txt): $src" 'ERROR'; $script:Failures.Add("IMPORT_BUNDLE_INVALID $src"); return }
    # p5 A9: from here on the bundle is read from a private snapshot (only this run writes it): the credential scan and the copy then see the same bytes,
    # whatever happens to the bundle folder in between. Removed again after the copy.
    $importSnapshot = $null
    if (-not $WhatIfPreference) {
        $importSnapshot = Join-Path $StateRoot "import-snapshot-$stamp"
        Invoke-Step "Snapshot the bundle to $importSnapshot (the scan and the copy read the same bytes)" { [void](New-ImportSnapshot -From $src -To $importSnapshot) }
        if (Test-Path -LiteralPath $importSnapshot -PathType Container) { $src = $importSnapshot } else { Write-Log 'IMPORT_SNAPSHOT_FAILED the bundle snapshot could not be made; nothing was imported' 'ERROR'; $script:Failures.Add('IMPORT_SNAPSHOT_FAILED'); return }
    }
    $h = $env:USERPROFILE
    Invoke-Step 'Back up existing ~/.claude CLAUDE.md and settings.json' {
        $b = Join-Path $StateRoot "backups\home-claude-$stamp"; [void][IO.Directory]::CreateDirectory($b)
        foreach ($f in 'CLAUDE.md', 'settings.json') {
            $p = Join-Path "$h\.claude" $f
            if (-not (Test-Path -LiteralPath $p)) { continue }
            # r10 A2: a host file that carries a credential is never copied (the backup would be a second, durable copy of it, and Import
            # never replaces such a file anyway, so there is nothing to roll back). One read: the bytes judged are the bytes written.
            if (-not (Copy-CleanBackupFile -Path $p -DestinationDir $b)) { Write-Log "IMPORT_BACKUP_SKIPPED_CREDENTIAL_FILE $p (it carries a credential or cannot be read; no copy was made)" 'WARN' }
        }
    }
    Invoke-Step 'Copy bundle into ~/.claude, ~/bin, ~/.codex, ProgramData\FleetDefender, MLV-App\.claude-state' {
        # Import never touches credentials (adversary A4): the export never stages the credential-shaped names, but a bundle is only a folder, so
        # the same exclusion list is applied on the way IN. A hand-made or tampered bundle cannot overwrite this host's auth.json or
        # .credentials.json, and its sign-in stays the owner's (README step 3).
        $inX = @('/XF') + $ExcludeFiles
        # r3 A5: the file name is only the first line of defence; these copies also refuse, by CONTENT and ROLE, any file that carries a credential
        # or would replace a host file that does (Invoke-ImportRobocopy). The claims and doctrine copies keep their own /XC /XN /XO rules.
        $script:ImportSkippedDest.Clear()
        # p5 A3: every Import copy carries /XO (README: an Import never replaces a NEWER file this host already has with an older bundle copy).
        # p5 A4 / r12 pass 9 B3: remember whether the bundle's settings.json was APPLIED, from what the copy actually did (it wrote the file: 'created' or 'replaced'), not from a
        # timestamp sampled before the copy (a newer or an identical host file the copy KEPT would then have been rewritten).
        $script:ImportAppliedDest.Clear()
        $script:ImportAppliedIdentity.Clear()   # r14 A4
        $hostSettings = [IO.Path]::GetFullPath("$h\.claude\settings.json")
        Invoke-ImportRobocopy "$src\home-claude" "$h\.claude" (@('/E', '/XO') + $inX)
        $script:ImportSettingsApplied = @($script:ImportAppliedDest | Where-Object { $_ -ieq $hostSettings }).Count -gt 0
        Invoke-ImportRobocopy "$src\home-bin" "$h\bin" (@('/E', '/XO') + $inX)
        Invoke-ImportRobocopy "$src\home-codex" "$h\.codex" (@('/E', '/XO') + $inX)
        Invoke-ImportRobocopy "$src\programdata-fleetdefender" 'C:\ProgramData\FleetDefender' (@('/E', '/XO') + $inX)
        # The claims folder is copied on its own below, never overwriting a claim this host already has (adversary pass 2, A10): a live local claim
        # replaced by a foreign one of the same name would be quarantined for its absent run dir and the running card would lose its claim.
        $claimsSrc = "$src\project-claude-state\hub-autopilot\claims"
        Invoke-ImportRobocopy "$src\project-claude-state" "$Repo\.claude-state" (@('/E', '/XO', '/XD', $claimsSrc) + $inX)
        # p5 A2: the claims and doctrine copies go through the same content guard as every other Import copy.
        Invoke-ImportRobocopy $claimsSrc "$Repo\.claude-state\hub-autopilot\claims" (@('/E', '/XC', '/XN', '/XO') + $inX)
        # MINI3-CUTOVER-STATE-GAPS-1 (b): /XO so a fold marker this host already advanced is never overwritten by the older bundle copy.
        Invoke-ImportRobocopy "$src\project-codex-state\doctrine" "$Repo\.codex-state\doctrine" (@('/E', '/XO') + $inX)
        Invoke-ImportRobocopy "$src\reference" "$StateRoot\reference" (@('/E', '/XO') + $inX)
    }
    if ($importSnapshot -and (Test-Path -LiteralPath $importSnapshot)) {
        try { Remove-Item -LiteralPath $importSnapshot -Recurse -Force -ErrorAction Stop } catch { Write-Log "could not remove the import snapshot $importSnapshot : $($_.Exception.Message)" 'WARN' }
    }
    # MINI3-CUTOVER-STATE-GAPS-1 (a): claims came across without their fleet-runs run dirs. A claim younger than 6 h with no run dir
    # reads 'receipt state none' and blocks its card, so those claims are quarantined, not carried.
    Invoke-Step 'Quarantine foreign hub claims (run dir absent on this host, under 24 h old)' {
        [void](Move-ForeignClaims -ClaimsDir (Join-Path $Repo '.claude-state\hub-autopilot\claims') -Stamp $stamp)
    }
    if ($h -ine $SourceUserProfile) {
        # r3 A5: when the bundle's settings.json was NOT applied (the host's own credential-bearing one stayed), there is nothing of the bundle to
        # rewrite, and rewriting the host's file would touch a credential-bearing file the Import just refused to replace.
        if (-not $script:ImportSettingsApplied) {
            Write-Log "skipped the settings.json profile rewrite: the bundle's settings.json was not applied (absent from the bundle, older than the host's, or IMPORT_SKIPPED_CREDENTIAL_FILE), the host's file stays as it is"
        } else {
        Invoke-Step "Rewrite $SourceUserProfile -> $h in ~/.claude/settings.json" {
            # r12 A3: one locked read-judge-write (Update-ImportedSettingsLocked); a file it cannot lock, or that gained a credential, stays as it is with a typed failure.
            [void](Update-ImportedSettingsLocked -Path "$h\.claude\settings.json" -FromProfile $SourceUserProfile -ToProfile $h)
        }
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
    # 'LockRoot' is deliberately NOT carried (p5 A6): the gate lock is the one machine-wide default for every task and every release.
    foreach ($k in 'StateRoot', 'Workspace', 'MlvTmp') {
        if ($script:SetupBound.ContainsKey($k)) { $arg += " -$k `"$(Get-Variable -Name $k -ValueOnly)`"" }
    }
    if ($Spec.ArgLine) { $arg += " -GateArgLine `"$($Spec.ArgLine)`"" }
    return $arg
}

function Invoke-PhaseTasks {
    $pwsh = Get-PwshPath
    if (-not $pwsh) { Write-Log 'TASKS_PREREQ_MISSING pwsh missing: run -Phase Packages first' 'ERROR'; $script:Failures.Add('TASKS_PREREQ_MISSING pwsh'); return }   # r15 pass 14 A5 sibling: a failure (exit 1)
    $self = Join-Path $StateRoot 'Setup-MlvWorkstation.ps1'
    if ($PSCommandPath -and ($PSCommandPath -ine $self)) {
        Invoke-Step "Install gate runner copy at $self" { [void][IO.Directory]::CreateDirectory($StateRoot); Copy-Item -LiteralPath $PSCommandPath -Destination $self -Force }
    }
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    foreach ($t in $TaskSpecs) {
        # r10 pass 7 A3: "no such task" and "could not read the task" are different answers; an unreadable task may be an enabled one, and
        # registering over it would leave it Disabled with no failure recorded, so it is skipped and the run fails.
        $existing = $null
        $unreadable = $false
        try { $existing = Get-ScheduledTask -TaskName $t.Name -TaskPath '\' -ErrorAction Stop }
        catch { $existing = $null; if (-not (Test-ScheduledTaskNotFound $_)) { $unreadable = $true } }
        if ($unreadable) {
            Write-Log "TASK_STATE_UNREADABLE $($t.Name) (could not read the task, so it was not registered or replaced)" 'ERROR'
            $script:Failures.Add("TASK_STATE_UNREADABLE $($t.Name)")
            continue
        }
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
# p5 A6: the lock a Lease-gated run holds and the one a release takes must be the same file. A -LockRoot that differs from the machine-wide default
# is allowed only with the explicit -AllowScratchLockRoot seam (tests); anything else would let a release run under a gate holding another lock.
function Test-LockRootIsMachineWide {
    $default = 'C:\ProgramData\MlvWorkstation-locks'
    if ($LockRoot.TrimEnd('\') -ieq $default) { return $true }
    return [bool]$AllowScratchLockRoot
}

function Invoke-PhaseGate {
    $tag = "gate[$GateMode] $(Split-Path -Leaf $GateScript)"
    if ($GateMode -eq 'Lease' -and -not (Test-LockRootIsMachineWide)) { Write-Log "$tag refused: LOCKROOT_NOT_MACHINE_WIDE ($LockRoot is not the machine-wide gate lock folder)" 'ERROR'; exit 4 }
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
            try { $lock = Open-GateLock -Root $LockRoot -Mode Shared }
            catch { Write-Log "$tag failed: $($_.Exception.Message) (the target was not run)" 'ERROR'; exit 5 }   # r22: an I/O failure is not contention, and never a successful skip
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
        Invoke-GitTraceScrubbed { & $GateScript @splat }   # r17 pass 18 A3 sibling: the gate target's git calls inherit no GIT_TRACE* destination
        $rc = [int]$global:LASTEXITCODE
        Write-Log "$tag exit=$rc"
    } finally { if ($lock) { $lock.Dispose() } }
    exit $rc
}

# The gate lock (see Invoke-PhaseGate). Shared = a Lease-gated run is inside its target; Exclusive = a lease release is in flight. Returns the open
# FileStream (the caller disposes it) or $null when the other kind holds it. Any error other than a sharing violation is thrown, not read as "free".
# r22 (sol r21 B2 = opus r21 H3; HUB RULING 20261117T1152Z rule 4): $null ONLY for a sharing or lock violation (HResult 0x80070020 / 0x80070021). FileNotFoundException and
# DirectoryNotFoundException derive from IOException and used to read as "busy" too (a gate skipped its target with exit 0, a release refused as if a gate ran): a lock file
# that vanished is now created again and the open retried ONCE; a second failure, or any other error, throws the typed GATE_LOCK_IO_FAILED (both callers exit 5).
function Open-GateLockStream {
    # the raw open, a seam of its own (the suite replaces it to make the lock file vanish between the create and the open)
    param([string]$Path, [string]$Mode)
    if ($Mode -eq 'Shared') { return [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) }
    return [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}

function Open-GateLock {
    param([string]$Root, [ValidateSet('Shared', 'Exclusive')][string]$Mode)
    $p = Join-Path $Root 'gate.lock'
    for ($attempt = 1; ; $attempt++) {
        try {
            [void][IO.Directory]::CreateDirectory($Root)
            if (-not (Test-Path -LiteralPath $p)) { try { [IO.File]::WriteAllText($p, '') } catch { } }   # a failed create shows up as the open's own error below
            return (Open-GateLockStream -Path $p -Mode $Mode)
        } catch {
            $e = $_.Exception
            while ($e -is [System.Management.Automation.MethodInvocationException] -and $e.InnerException) { $e = $e.InnerException }
            if ($e -is [System.IO.IOException] -and (('0x{0:X8}' -f $e.HResult) -in @('0x80070020', '0x80070021'))) { return $null }   # sharing / lock violation: the other kind holds it
            if ($attempt -eq 1 -and ($e -is [System.IO.FileNotFoundException] -or $e -is [System.IO.DirectoryNotFoundException])) { continue }   # vanished: create it again, open once more
            throw "GATE_LOCK_IO_FAILED $p could not be opened $Mode ($($e.GetType().Name): $($e.Message))"
        }
    }
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

# True for the error Get-ScheduledTask raises when the named task does not exist (and only that one).
function Test-ScheduledTaskNotFound {
    param($ErrorRecord)
    if (-not $ErrorRecord) { return $false }
    $id = "$($ErrorRecord.FullyQualifiedErrorId)"
    return ($ErrorRecord.CategoryInfo.Category -eq 'ObjectNotFound') -or ($id -match 'CmdletizationQuery_NotFound|ObjectNotFound|No MSFT_ScheduledTask objects found')
}

# r3 A7: what a meter task looked like before its -Install. Existed / WasEnabled (existed and not Disabled) / Xml (kept only for a task that
# existed Disabled, so its own gated action can be put back). A read that fails counts as "task absent" for Existed but leaves WasEnabled false,
# so the after-step still disables whatever the install created.
function Get-MeterTaskSnapshot {
    param([string]$Name)
    $t = $null
    # p5 A5: "no such task" and "could not read the task" are different answers. An unreadable task is NOT taken for an absent one (it may be one the
    # owner had enabled): Unreadable = $true, and the install is not run for it (Invoke-MeterInstalls).
    $unreadable = $false
    try { $t = Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop }
    catch {
        $t = $null
        if (-not (Test-ScheduledTaskNotFound $_)) { $unreadable = $true }
    }
    $snap = @{ Existed = [bool]$t; WasEnabled = $false; Xml = $null; Unreadable = $unreadable }
    if ($t) {
        $snap.WasEnabled = ("$($t.State)" -ne 'Disabled')
        # r10 A4: the definition is saved for EVERY existing task, enabled or not. The install re-registers the task with its own action, outside the
        # gate; without the saved XML an enabled task's gated action would be gone for good. A definition that cannot be saved makes the task
        # unreadable (the install is not run over it).
        try { $snap.Xml = [string](Export-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop) } catch { $snap.Xml = $null }
        if (-not $snap.Xml) { $snap.Unreadable = $true }
    }
    return $snap
}

# After the child -Install (any exit code): a task that was not enabled before and is enabled now goes back to Disabled, the rule Invoke-PhaseTasks
# applies (registered Disabled, enabled only when it was enabled before). A task the owner had enabled is left alone; an unchanged one is a no-op.
function Restore-MeterTaskState {
    param([string]$Name, [hashtable]$Before)
    if ($Before.WasEnabled) {
        # r10 A4: the owner had it enabled and it stays enabled, but the install may have replaced its definition (its own action, outside the gate):
        # when the definition differs from the one saved before, the saved one is registered again (it carries its own Enabled setting).
        $curXml = $null
        try { $curXml = [string](Export-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop) } catch { $curXml = $null }
        if ($curXml -and ($curXml -ceq $Before.Xml)) { return }
        try {
            [void](Register-ScheduledTask -TaskName $Name -TaskPath '\' -Xml $Before.Xml -Force -ErrorAction Stop)
            Write-Log "METER_TASK_DEFINITION_RESTORED $Name (the install had replaced its definition; the saved one is back)" 'WARN'
        } catch {
            Write-Log "METER_TASK_DEFINITION_REPLACED $Name (could not put its saved definition back: $($_.Exception.Message)); re-register it by hand from the gated XML" 'ERROR'
            $script:Failures.Add("METER_TASK_DEFINITION_REPLACED $Name")
        }
        return
    }
    $now = $null
    $readFailed = $false
    try { $now = Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop }
    catch { $now = $null; if (-not (Test-ScheduledTaskNotFound $_)) { $readFailed = $true } }
    if ($readFailed) {
        # p5 A5: the state after the install cannot be read, so it is not assumed absent: disable it (idempotent, and it was not enabled before) and
        # report; a disable that also fails is a failure of this run (exit 1).
        try {
            [void](Disable-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop)
            Write-Log "METER_TASK_STATE_UNREADABLE $Name (could not read it after -Install; it was Disabled to be safe)" 'WARN'
        } catch {
            Write-Log "METER_TASK_LEFT_ENABLED $Name (state unreadable after -Install and Disable failed: $($_.Exception.Message)); disable it by hand: Disable-ScheduledTask -TaskName $Name" 'ERROR'
            $script:Failures.Add("METER_TASK_LEFT_ENABLED $Name")
        }
        return
    }
    if (-not $now -or "$($now.State)" -eq 'Disabled') { return }
    try {
        if ($Before.Xml) { [void](Register-ScheduledTask -TaskName $Name -TaskPath '\' -Xml $Before.Xml -Force -ErrorAction Stop) }
        # r10 A5: Disable runs whatever a re-read says (idempotent; a read that fails is not "already Disabled"), and the result is then READ with
        # -ErrorAction Stop: a task that cannot be read back, or is not Disabled, is a failure of the run, not a success line.
        [void](Disable-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop)
        $again = Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop
        if ("$($again.State)" -ne 'Disabled') { throw "state is $($again.State) after Disable" }
        Write-Log "METER_TASK_RESTORED_DISABLED $Name (it was $(if ($Before.Existed) { 'Disabled' } else { 'absent' }) before -Install; the install had enabled it outside the gate)" 'WARN'
    } catch {
        Write-Log "METER_TASK_LEFT_ENABLED $Name (could not put it back Disabled: $($_.Exception.Message)); disable it by hand: Disable-ScheduledTask -TaskName $Name" 'ERROR'
        $script:Failures.Add("METER_TASK_LEFT_ENABLED $Name")
    }
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
        # r3 A7: snapshot the task BEFORE the child -Install (which registers it ENABLED, outside this script's gate); afterwards a task that was not
        # enabled before is put back Disabled (see Restore-MeterTaskState).
        $before = Get-MeterTaskSnapshot -Name $m.Name
        if ($before.Unreadable) {
            # p5 A5: the install registers its task ENABLED outside the gate; without a readable "before" its effect could not be undone correctly.
            Write-Log "METER_INSTALL_SKIPPED_STATE_UNREADABLE $($m.Name) (the task state could not be read before -Install, so nothing was installed)" 'ERROR'
            $script:Failures.Add("METER_INSTALL_SKIPPED_STATE_UNREADABLE $($m.Name)")
            continue
        }
        Write-Log "DO   meter -Install $($m.Name) ($($m.Script))"
        $out = @(); $code = 1
        # p5 A5: the restore runs in finally, so a failing log write (or any throw) between the install and here still puts the task back.
        try {
            $out = @(Invoke-GitTraceScrubbed { & $pwsh -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $m.Script -Install 2>&1 } | ForEach-Object { "$_" })   # r17 F3: the child inherits no GIT_TRACE*
            $code = $LASTEXITCODE
            foreach ($o in $out) { if ($o.Trim()) { Write-Log "    $($o.TrimEnd())" } }
        } finally { Restore-MeterTaskState -Name $m.Name -Before $before }
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
        $o = if ($cmd) { try { (Invoke-GitTraceScrubbed { & $c[0] ($c[1] -split ' ') 2>&1 } | Select-Object -First 1) } catch { 'error' } } else { 'MISSING' }   # r17 pass 18 A3: no GIT_TRACE* either
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
    Write-Log "LongPathsEnabled=$((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled)  git core.longpaths=$(Invoke-GitTraceScrubbed { & git config --get core.longpaths 2>$null })"
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
    if (Get-Command gh -ErrorAction SilentlyContinue) { Invoke-GitTraceScrubbed { & gh auth status *> $null }; Write-Log "gh auth: $(if ($LASTEXITCODE -eq 0) { 'signed in' } else { 'NOT signed in' })" }
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
            # r15 pass 14 A4: a dry run reads no lease (a lease read is a git fetch, which changes the local lease ref and the object store) and writes none
            if ($WhatIfPreference) { Write-Log 'SKIP (WhatIf) LeaseAcquire: the lease is neither read (that is a git fetch) nor written'; break }
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
            if (-not (Test-LockRootIsMachineWide)) { Write-Log "LOCKROOT_NOT_MACHINE_WIDE: $LockRoot is not the machine-wide gate lock folder, so this release could not exclude a running gate" 'ERROR'; exit 4 }
            # r10 pass 7 A4: a dry run takes no gate lock (it would make a scheduled Lease-gated task skip as busy) and reads no lease (that is a git fetch).
            if ($WhatIfPreference) { Write-Log 'SKIP (WhatIf) LeaseRelease: no gate lock was taken and no lease was read'; break }
            $xlock = $null
            try { $xlock = Open-GateLock -Root $LockRoot -Mode Exclusive }
            catch { Write-Log "$($_.Exception.Message) (nothing was released; this is not a running gate: fix the lock folder and re-run)" 'ERROR'; exit 5 }   # r22: typed, not 'a Lease-gated task is running'
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
