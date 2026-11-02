# lane-dirty-save.ps1 - LANE-DIRTY-SAVE-CORE-1 (core split out of parked PR #698).
# A dead lane's uncommitted work is the one thing a refusal or a clean-up destroys. When the lane process is dead, this writes into -RunDir:
#   dirty-save-<head8>.patch   git diff HEAD --binary, byte for byte; .staged.patch = git diff --cached --binary (staged work the tree no longer has)
#   dirty-save-<head8>.untracked.txt = git ls-files -z --others --exclude-standard: untracked file NAMES only, NUL-separated and never C-quoted (git quotes " \ TAB LF even with core.quotepath=false)
#   .receipt.json = one JSON line naming the files of THIS save, also printed; it says untrackedFormat=nul-separated and untrackedContents=not-saved
# Untracked file CONTENTS are NOT preserved by this saver (names only); copying them is LANE-DIRTY-SAVE-INVENTORY-ROWS-1 / LANE-DIRTY-SAVE-WORKSTREAM-WIRE-1 scope.
# The lane is named by -LanePid or by -ChildJson ({pid, createdUtc}; createdUtc must be an ISO-8601 string, else exit 30; a process that started after it, at full precision, is a reused pid, so the lane is dead), never both.
# The whole worktree is collected: a -WorkDir below the top level is lifted to `git rev-parse --show-toplevel` and every diff gets --no-relative. A -RunDir inside that worktree (links followed) is refused (exit 30): evidence written there would be listed as untracked dirt.
# Paths are split on the platform's own separators only (a POSIX backslash is a filename character), and the RunDir that passes the containment test is the resolved path every artifact is then written through.
# Windows resolves both directories through an open handle (GetFinalPathNameByHandle: links, junctions, subst drives, 8.3 and \\?\ spellings in any case) and keeps the handle open, so the checked RunDir cannot be renamed or replaced before the write. POSIX walks symlinks by name; a swap in that window is not closed there.
# HEAD is resolved ONCE and both diffs run against that sha, so the receipt head is the patch base.
# Exit: 0 saved (or already saved with these exact bytes), 10 nothing to save, 20 lane alive (refused, nothing written), 30 bad arguments, 40 git failed, 50 writing into RunDir failed (artifacts already written stay).
# Never stashes, resets, checks out, cleans or deletes. Each file is created CREATE_NEW; a name taken by different bytes is skipped (.2, .3 ... suffix), by the same bytes reused,
# a DIRECTORY at a name is skipped too, and a taken name whose bytes stay unreadable after 5 tries is skipped and listed in the receipt as comparisonUnreadable (never called "different").
# Every git call gets GIT_TRACE*=0 in its OWN environment (never the caller's): a trace variable or Trace2 config naming a tracked file would make git
# append to it (sol r20 B1 on #698), and removing a caller's disable is what exposed it. GIT_OPTIONAL_LOCKS=0 keeps git from refreshing the index.
param(
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$RunDir,
    [int]$LanePid = 0,
    [string]$ChildJson = ''
)
$ErrorActionPreference = 'Stop'
$script:unreadable = @()
function Invoke-GitBytes([string[]]$GitArgs) {
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new('git')
        foreach ($a in (@('-C', $WorkDir) + $GitArgs)) { $psi.ArgumentList.Add($a) }
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
        foreach ($k in 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_DIFF_OPTS') { [void]$psi.Environment.Remove($k) }
        foreach ($k in (@('GIT_TRACE', 'GIT_TRACE2', 'GIT_TRACE2_EVENT', 'GIT_TRACE2_PERF') + @($psi.Environment.Keys | Where-Object { $_ -like 'GIT_TRACE*' }))) { $psi.Environment[$k] = '0' }
        $psi.Environment['GIT_OPTIONAL_LOCKS'] = '0'
        $p = [System.Diagnostics.Process]::Start($psi)
        $err = $p.StandardError.ReadToEndAsync()
        $ms = [System.IO.MemoryStream]::new()
        $p.StandardOutput.BaseStream.CopyTo($ms)
        $p.WaitForExit()
        return @{ Code = $p.ExitCode; Bytes = $ms.ToArray(); Err = $err.Result }
    } catch { return @{ Code = -1; Bytes = [byte[]]@(); Err = $_.Exception.Message } }
}
function Remove-GitTerminator([byte[]]$Bytes) {  # git ends a path line with exactly one LF (never CRLF, also on Windows): strip that LF and nothing else (r5 ADV-A4: a worktree named 'repo ' is not the sibling 'repo'; r6 A1: a name ending in CR keeps its CR)
    $s = [System.Text.Encoding]::UTF8.GetString($Bytes)
    if ($s.EndsWith("`n")) { $s = $s.Substring(0, $s.Length - 1) }
    return $s
}
$script:onWin = [System.IO.Path]::DirectorySeparatorChar -eq '\'  # a backslash separates path parts on Windows only; on POSIX it is a literal filename character (dot B1 on #722)
function Resolve-RealPath([string]$Path, [int]$Depth = 0) {  # POSIX: every symlink on the way is followed, so the inside-worktree test sees where bytes would land (r6 A2). Windows asks the kernel instead (Get-FinalDir)
    if ($Depth -gt 16) { throw 'too-many-links' }
    $full = [System.IO.Path]::GetFullPath($Path); $root = [System.IO.Path]::GetPathRoot($full); $cur = $root
    foreach ($seg in $full.Substring($root.Length).Split([char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar), [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $cur = [System.IO.Path]::Combine($cur, $seg)
        $di = [System.IO.DirectoryInfo]::new($cur)
        if ($di.LinkTarget) { $t = $di.ResolveLinkTarget($true); if ($t) { $cur = Resolve-RealPath $t.FullName ($Depth + 1) } }
    }
    return $cur
}
function ConvertTo-NativePath([string]$Path) {  # Windows: \\?\ and \\.\ (any case, either slash) are one spelling, rewritten to \\?\ and KEPT: stripping it would let Win32 trim a trailing dot or space and open the sibling directory (r7 fix-2 A1). A form with no drive or UNC meaning is refused, never guessed
    if (-not $script:onWin) { return $Path }
    $p = $Path.Replace('/', '\')
    if ($p -notmatch '^\\\\[?.]\\') { return $p }
    if ($p -match '^\\\\[?.]\\UNC\\(?<rest>.+)$') { return '\\?\UNC\' + $Matches['rest'] }
    if ($p -match '^\\\\[?.]\\(?<rest>[A-Za-z]:\\.*)$') { return '\\?\' + $Matches['rest'] }
    throw 'unsupported-extended-path'
}
function Get-FinalDir([string]$Path) {  # Windows: open the directory and read its true path back, so links, junctions, subst drives, 8.3 and \\?\ spellings are all resolved by the kernel (r7 A1, A2). The handle stays open (no share-delete), so the directory and its parents cannot be renamed or replaced before the artifacts are written (r7 A3)
    if (-not ('LdsNative.Win' -as [type])) {
        Add-Type -Namespace LdsNative -Name Win -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFileW(string name, uint access, uint share, System.IntPtr security, uint disposition, uint flags, System.IntPtr template);
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern uint GetFinalPathNameByHandleW(Microsoft.Win32.SafeHandles.SafeFileHandle handle, System.Text.StringBuilder buffer, uint length, uint flags);
'@
    }
    $h = [LdsNative.Win]::CreateFileW((ConvertTo-NativePath $Path), 0x81, 3, [System.IntPtr]::Zero, 3, 0x02000000, [System.IntPtr]::Zero)  # LIST_DIRECTORY+READ_ATTRIBUTES (an attributes-only open is exempt from sharing checks, so it would hold nothing); share read+write, not delete; OPEN_EXISTING; BACKUP_SEMANTICS (a directory)
    if ($h.IsInvalid) { throw 'cannot-open-directory' }
    $sb = [System.Text.StringBuilder]::new(32768)
    $n = [LdsNative.Win]::GetFinalPathNameByHandleW($h, $sb, 32768, 0)
    if ($n -eq 0 -or $n -ge 32768) { $h.Dispose(); throw 'cannot-resolve-directory' }
    return @{ Path = $sb.ToString(); Handle = $h }  # the kernel's own spelling, \\?\C:\... or \\?\UNC\..., which Win32 and .NET then use without trimming
}
function Test-LaneAlive {
    if ($LanePid -and $ChildJson) { throw 'mixed-selectors' }  # one pid with another lane's createdUtc would judge a live lane reused (dot B1 on #722)
    $procId = $LanePid; $born = $null
    if ($ChildJson) {
        $j = Get-Content -LiteralPath $ChildJson -Raw | ConvertFrom-Json
        $procId = [int]$j.pid
        $c = $j.PSObject.Properties['createdUtc']
        if ($c) {
            $v = $c.Value
            if ($v -is [datetime]) { $born = $v.ToUniversalTime() }
            elseif ($v -is [string] -and $v -match '^\d{4}-\d\d-\d\dT\d\d:\d\d') { $born = [datetime]::Parse($v, [cultureinfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() }
            else { throw 'bad-created-utc' }  # a number (1 casts to year 0001) or prose is no timestamp (sol r4 B2)
        }
    }
    if ($procId -le 0) { throw 'no-lane-pid' }
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    return -not ($born -and $proc.StartTime.ToUniversalTime() -gt $born)  # Invoke-Lane writes StartTime itself, so equal is the lane and anything later is a reuse
}
function Save-Bytes([string]$Ext, [byte[]]$Bytes, [scriptblock]$Make = $null) {  # $Make rebuilds the bytes for every attempt, so the receipt lists names found unreadable by its OWN earlier attempts (r6 A3)
    for ($n = 1; ; $n++) {
        if ($Make) { $Bytes = [byte[]](& $Make) }
        $script:written = $Bytes
        $name = if ($n -eq 1) { "dirty-save-$head8.$Ext" } else { "dirty-save-$head8.$n.$Ext" }
        $f = [System.IO.Path]::Combine($RunDir, $name)
        try { $fs = [System.IO.File]::Open($f, 'CreateNew', 'Write', 'None') }
        catch [System.IO.IOException], [System.UnauthorizedAccessException] {  # a directory at the name raises UnauthorizedAccess (sol r4 B3)
            if (-not ([System.IO.File]::Exists($f) -or [System.IO.Directory]::Exists($f))) { throw }
            if ([System.IO.Directory]::Exists($f)) { continue }
            $same = $null
            for ($i = 0; $i -lt 5 -and $null -eq $same; $i++) { try { $same = [System.Linq.Enumerable]::SequenceEqual([byte[]][System.IO.File]::ReadAllBytes($f), $Bytes) } catch { Start-Sleep -Milliseconds 200 } }
            if ($same) { return $name }
            if ($null -eq $same) { $script:unreadable += $name }  # unknown is not "different" (sol r4 B4): the new bytes are still saved, and the receipt says the comparison failed
            continue
        }
        try { $fs.Write($Bytes, 0, $Bytes.Length) } finally { $fs.Dispose() }
        return $name
    }
}
try {
    $here = (Get-Location).ProviderPath  # PowerShell's location, not .NET's (adversary A6)
    $WorkDir = [System.IO.Path]::GetFullPath($WorkDir, $here); $RunDir = [System.IO.Path]::GetFullPath($RunDir, $here)  # .NET only: PowerShell's path layer may re-spell a POSIX backslash as '/' (dot B1 on #722)
    if (-not [System.IO.Directory]::Exists($WorkDir) -or -not [System.IO.Directory]::Exists($RunDir)) { throw 'no-such-directory' }
    if (Test-LaneAlive) { Write-Output 'DIRTY_SAVE_REFUSED reason=lane-alive'; exit 20 }
} catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)"; exit 30 }
$flags = @('--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--no-relative', '--submodule=short', '--ignore-submodules=none', '--unified=3', '--src-prefix=a/', '--dst-prefix=b/')
$top = Invoke-GitBytes @('rev-parse', '--show-toplevel')
if ($top.Code -eq 0) { $WorkDir = Remove-GitTerminator $top.Bytes }
try {
    if ($script:onWin) {
        $script:held = @((Get-FinalDir $RunDir), (Get-FinalDir $WorkDir)); $RunDir = $script:held[0].Path; $realWork = $script:held[1].Path
        $shareOf = { param($p) if ($p.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) { [System.IO.Path]::GetPathRoot($p).ToLowerInvariant() } else { '' } }
        if ((& $shareOf $RunDir) -ne (& $shareOf $realWork)) { throw 'run-dir-namespace-unprovable' }  # a share can expose a local folder (\\localhost\C$), and the kernel keeps the two spellings apart; only an identical \\server\share can be compared by name (r7 fix-2 A2)
    }
    else { $RunDir = Resolve-RealPath $RunDir; $realWork = Resolve-RealPath $WorkDir }
    $rel = [System.IO.Path]::GetRelativePath($realWork, $RunDir)
} catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)"; exit 30 }  # the directory that passes the containment test below IS the one written to: no re-spelling between check and use (dot B1 on #722)
if ($top.Code -eq 0 -and -not ($rel -eq '..' -or $rel.StartsWith('..' + [System.IO.Path]::DirectorySeparatorChar) -or [System.IO.Path]::IsPathRooted($rel))) { Write-Output 'DIRTY_SAVE_BAD_ARGS reason=run-dir-inside-worktree'; exit 30 }
$headR = if ($top.Code -eq 0) { Invoke-GitBytes @('rev-parse', 'HEAD') } else { $top }
$headFull = [System.Text.Encoding]::ASCII.GetString($headR.Bytes).Trim()
if ($headR.Code -eq 0 -and $headFull -notmatch '^[0-9a-f]{40,64}$') { $headR = @{ Code = -1; Err = "unexpected HEAD '$headFull'" } }
$git = [ordered]@{ head = $headR }
if ($headR.Code -eq 0) {
    $git.patch = Invoke-GitBytes (@('diff', $headFull) + $flags); $git.staged = Invoke-GitBytes (@('diff', '--cached', $headFull) + $flags)
    $git.untracked = Invoke-GitBytes @('ls-files', '-z', '--others', '--exclude-standard')
}
$bad = $git.Values | Where-Object { $_.Code -ne 0 } | Select-Object -First 1
if ($bad) { Write-Output "DIRTY_SAVE_GIT_FAILED reason=$($bad.Err.Trim())"; exit 40 }
$todo = 'patch', 'staged', 'untracked' | Where-Object { $git[$_].Bytes.Length -gt 0 }
if (-not $todo) { Write-Output 'DIRTY_SAVE_NOTHING reason=clean-tree'; exit 10 }
$head8 = $headFull.Substring(0, 8)
$ext = @{ patch = 'patch'; staged = 'staged.patch'; untracked = 'untracked.txt' }
$r = [ordered]@{ schema = 'lane-dirty-save/v1'; head = $headFull; untrackedFormat = 'nul-separated'; untrackedContents = 'not-saved' }
try {
    foreach ($k in $todo) { $r[$k] = Save-Bytes $ext[$k] $git[$k].Bytes }
    if ($r.Contains('patch')) { $r.patchSha256 = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData($git.patch.Bytes)).Replace('-', '').ToLowerInvariant() }
    $make = { $r.comparisonUnreadable = @($script:unreadable); [System.Text.UTF8Encoding]::new($false).GetBytes(($r | ConvertTo-Json -Compress) + "`n") }
    [void](Save-Bytes 'receipt.json' $null $make)
} catch { Write-Output "DIRTY_SAVE_WRITE_FAILED reason=$($_.Exception.Message)"; exit 50 }  # r6 A4: a read-only or full RunDir ends in a documented status; artifacts already written stay
Write-Output ([System.Text.UTF8Encoding]::new($false).GetString($script:written).TrimEnd("`n"))
exit 0