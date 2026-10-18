<#
.SYNOPSIS
Bounded newest-N listing of a venue agent mailbox directory (processed, outbox, inbox, ...).

.DESCRIPTION
HOST-CPU-SATURATION 2026-10-17: `ls -t //bachelor/mlv-agent/processed` (20.7k files) and `.../outbox`
(7k) from Git Bash opened and stat'ed every file over SMB; Defender scanned each one, so one listing ran
for over 10 minutes and cost about one host core. NEVER list those directories with ls/dir/find.

This script enumerates with [IO.DirectoryInfo]::EnumerateFileSystemInfos, which returns names and
timestamps from the directory listing without opening any file (20.7k entries in about 2.5 s over SMB),
then keeps only the newest -First entries. For one job, pass -JobId: the server-side pattern "<id>*" narrows
the listing and AgentShare.psm1's job-key rule keeps exactly that id (never "<id>.other.*", a different dotted
job id). Old entries are moved under <share>\archive\<dir>\<yyyy-MM>\ by Invoke-AgentShareRotation.ps1;
pass -IncludeArchive to search there too (bounded the same way). An unreachable share is an error, never an
empty answer; -AsJson always emits an array ([] when nothing matches).

.EXAMPLE
pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\profiling\agent-share\Get-AgentShareRecent.ps1" -Directory outbox -First 20
pwsh -NoProfile -File "C:\!Layi Wkspc\MLV-App\tools\profiling\agent-share\Get-AgentShareRecent.ps1" -Directory outbox -JobId venue-quiet-probe-20261018T000337475Z
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
    # Exact job id: a server-filtered "<id>*" enumeration, then AgentShare.psm1's job-key test keeps exactly this id.
    [string]$JobId = '',
    [switch]$IncludeArchive,
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'AgentShare.psm1') -Force
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

function Get-Entries([string]$root, [string]$pattern) {
    # An unreachable live directory is an error, never an empty answer; an absent archive month is just absent.
    $di = [IO.DirectoryInfo]$root
    if (-not $di.Exists) {
        if ($root -eq $dir) { throw "DIRECTORY_UNREADABLE $root (an unreachable share is not an empty one)" }
        return
    }
    $di.EnumerateFileSystemInfos($pattern)
}

if ($JobId) {
    # The server-side pattern narrows the listing; the job-key test then keeps exactly this id, so -JobId J never
    # returns J.other.result.json (job J.other) or Jx.result.json.
    $entries = foreach ($r in $roots) { Get-Entries $r "$JobId*" }
    $rows = @($entries | Where-Object { Test-AgentShareJobEntry -Name $_.Name -JobId $JobId } |
        Sort-Object LastWriteTimeUtc -Descending | ForEach-Object { ConvertTo-Row $_ })
} else {
    $pattern = if ($NamePrefix) { "$NamePrefix*" } else { '*' }
    $entries = foreach ($r in $roots) { Get-Entries $r $pattern }
    $rows = @($entries | Sort-Object LastWriteTimeUtc -Descending -Top $First | ForEach-Object { ConvertTo-Row $_ })
}

# JSON is always an array, [] included, so a consumer never has to special-case one row or none.
if ($AsJson) { ConvertTo-Json -InputObject @($rows) -Depth 3 } else { $rows | Format-Table name, kind, lastWriteTimeUtc, length -AutoSize | Out-String -Width 220 }
