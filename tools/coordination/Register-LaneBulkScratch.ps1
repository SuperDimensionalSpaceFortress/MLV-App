<#
.SYNOPSIS
    Register one bulk scratch path in a lane run's scratch manifest, so Invoke-Lane.ps1 removes it at lane exit.

.DESCRIPTION
    Appends one absolute path to <RunDir>\scratch-paths.txt (the manifest Remove-LaneBulkScratch.ps1 reads).
    Dedupes (case-insensitive, trailing backslash ignored). Safe to call from several processes at once: the
    manifest is opened exclusively with a short retry.

    A lane gets the run dir from the environment: Invoke-Lane.ps1 exports MLV_LANE_RUN_DIR and
    MLV_LANE_SCRATCH_MANIFEST to the agent process, so `-RunDir $env:MLV_LANE_RUN_DIR` needs no hand-built path.
    A detached chain (a WMI-started script) does NOT inherit environment variables, so its launcher must
    pass the run dir on its command line and the chain calls this script with -RunDir explicitly.

    REFUSES (exit 2, one stdout line `REFUSED reason=<reason> path=<path>`, the manifest untouched):
      run-dir-missing   -RunDir does not exist
      relative-path     the path is not a plain absolute drive path
      dotdot            the path has a .. segment
      short-name-alias  the path has an 8.3 ~N segment
      outside-root      the path is not strictly inside -Root (default C:\mlvtmp; tests pass their own root)
    Success: exit 0 and `REGISTERED path=<path>` or `ALREADY-REGISTERED path=<path>`. Exit 3: the manifest
    could not be written (typed `FAILED reason=manifest-write-failed`).
#>
param(
    [Parameter(Mandatory)][string]$RunDir,
    [Parameter(Mandatory)][string]$Path,
    [string]$Root = 'C:\mlvtmp',
    [string]$ManifestName = 'scratch-paths.txt'
)
$ErrorActionPreference = 'Stop'

function Write-Refused([string]$Reason) {
    [Console]::Out.WriteLine("REFUSED reason=$Reason path=$Path")
    exit 2
}

if (-not (Test-Path -LiteralPath $RunDir -PathType Container)) { Write-Refused 'run-dir-missing' }
if ($Path -notmatch '^[A-Za-z]:\\') { Write-Refused 'relative-path' }
if ($Path -match '(^|\\)\.\.(\\|$)') { Write-Refused 'dotdot' }
if ($Path -match '~\d') { Write-Refused 'short-name-alias' }
$full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
$rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if (-not $full.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase)) { Write-Refused 'outside-root' }

$manifest = Join-Path (Resolve-Path -LiteralPath $RunDir).Path $ManifestName
$fs = $null
for ($try = 0; $try -lt 50 -and $null -eq $fs; $try++) {
    try { $fs = [IO.File]::Open($manifest, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { Start-Sleep -Milliseconds 100 }
}
if ($null -eq $fs) { [Console]::Out.WriteLine("FAILED reason=manifest-write-failed path=$Path"); exit 3 }
try {
    $enc = New-Object System.Text.UTF8Encoding($false)
    $sr = New-Object IO.StreamReader($fs, $enc, $true, 4096, $true)
    $text = $sr.ReadToEnd(); $sr.Dispose()
    $have = @($text -split "`r?`n" | ForEach-Object { $_.Trim().TrimEnd('\') } | Where-Object { $_ })
    if ($have | Where-Object { $_ -ieq $full }) {
        [Console]::Out.WriteLine("ALREADY-REGISTERED path=$full")
    } else {
        $prefix = if ($text.Length -and -not $text.EndsWith("`n")) { "`n" } else { '' }
        $bytes = $enc.GetBytes($prefix + $full + "`n")
        [void]$fs.Seek(0, [IO.SeekOrigin]::End)
        $fs.Write($bytes, 0, $bytes.Length)
        $fs.Flush()
        [Console]::Out.WriteLine("REGISTERED path=$full")
    }
} catch {
    [Console]::Out.WriteLine("FAILED reason=manifest-write-failed path=$Path")
    exit 3
} finally { $fs.Dispose() }
exit 0
