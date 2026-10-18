<#
.SYNOPSIS
Bounded newest-N listing of a venue agent mailbox directory (processed, outbox, inbox, ...).

.DESCRIPTION
HOST-CPU-SATURATION 2026-10-17: `ls -t //bachelor/mlv-agent/processed` (20.7k files) and `.../outbox`
(7k) from Git Bash opened and stat'ed every file over SMB; Defender scanned each one, so one listing ran
for over 10 minutes and cost about one host core. NEVER list those directories with ls/dir/find.

This script enumerates with [IO.DirectoryInfo]::EnumerateFileSystemInfos, which returns names and
timestamps from the directory listing without opening any file (20.7k entries in about 2.5 s over SMB),
then keeps only the newest -First entries. For one job, pass -JobId: that is an exact-name probe, not a
listing. Old entries are moved under <share>\archive\<dir>\<yyyy-MM>\ by Invoke-AgentShareRotation.ps1;
pass -IncludeArchive to search there too (bounded the same way).

.EXAMPLE
pwsh -NoProfile -File tools\profiling\agent-share\Get-AgentShareRecent.ps1 -Directory outbox -First 20
pwsh -NoProfile -File tools\profiling\agent-share\Get-AgentShareRecent.ps1 -Directory outbox -JobId venue-quiet-probe-20261018T000337475Z
#>
[CmdletBinding()]
param(
    [string]$AgentShare = '\\bachelor\mlv-agent',
    [ValidateSet('processed', 'outbox', 'inbox', 'running', 'orphaned', 'cancel', 'logs')]
    [string]$Directory = 'outbox',
    [ValidateRange(1, 500)]
    [int]$First = 20,
    # Name prefix filter applied during enumeration (passed to the directory search, so the server filters).
    [string]$NamePrefix = '',
    # Exact job id: probes the known artifact names directly; no directory listing at all.
    [string]$JobId = '',
    [switch]$IncludeArchive,
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'
if ($JobId -and $JobId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw "JOBID_INVALID '$JobId'" }
if ($NamePrefix -and $NamePrefix -notmatch '^[A-Za-z0-9._-]{1,128}$') { throw "NAME_PREFIX_INVALID '$NamePrefix'" }

$dir = Join-Path $AgentShare $Directory
$roots = @($dir)
if ($IncludeArchive) {
    $archiveRoot = Join-Path (Join-Path $AgentShare 'archive') $Directory
    if (Test-Path -LiteralPath $archiveRoot -PathType Container) {
        # Month folders only (yyyy-MM); never recurse into job artifact directories.
        $roots += @(([IO.DirectoryInfo]$archiveRoot).EnumerateDirectories('????-??') | ForEach-Object FullName)
    }
}

function ConvertTo-Row([IO.FileSystemInfo]$e) {
    [pscustomobject]@{
        name             = $e.Name
        kind             = if ($e -is [IO.DirectoryInfo]) { 'dir' } else { 'file' }
        lastWriteTimeUtc = $e.LastWriteTimeUtc.ToString('o')
        length           = if ($e -is [IO.FileInfo]) { $e.Length } else { $null }
        fullName         = $e.FullName
    }
}

$rows = @()
if ($JobId) {
    foreach ($r in $roots) {
        # Exact-name probe of the artifact shapes the agent writes; a pattern search scoped to this one id.
        $di = [IO.DirectoryInfo]$r
        if (-not $di.Exists) { continue }
        $rows += @($di.EnumerateFileSystemInfos("$JobId.*") | ForEach-Object { ConvertTo-Row $_ })
    }
} else {
    $pattern = if ($NamePrefix) { "$NamePrefix*" } else { '*' }
    $entries = foreach ($r in $roots) {
        $di = [IO.DirectoryInfo]$r
        if (-not $di.Exists) { if ($r -eq $dir) { throw "DIRECTORY_UNREADABLE $r (an unreachable share is not an empty one)" }; continue }
        $di.EnumerateFileSystemInfos($pattern)
    }
    $rows = @($entries | Sort-Object LastWriteTimeUtc -Descending -Top $First | ForEach-Object { ConvertTo-Row $_ })
}

if ($AsJson) { $rows | ConvertTo-Json -Depth 3 } else { $rows | Format-Table name, kind, lastWriteTimeUtc, length -AutoSize | Out-String -Width 220 }
