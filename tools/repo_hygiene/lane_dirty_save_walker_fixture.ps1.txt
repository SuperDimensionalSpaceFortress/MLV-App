# lane-dirty-save.ps1 - LANE-DIRTY-SAVE-CORE-1 (core split out of parked PR #698).
# A dead lane's uncommitted work is the one thing a refusal or a clean-up destroys. When the lane process is dead, this writes into -RunDir:
#   dirty-save-<head8>.patch   git diff HEAD --binary, byte for byte; .staged.patch = git diff --cached --binary (staged work the tree no longer has)
#   dirty-save-<head8>.untracked.txt = git ls-files -z --others --exclude-standard: untracked file NAMES only, NUL-separated and never C-quoted (git quotes " \ TAB LF even with core.quotepath=false)
#   .receipt.json = one JSON line naming the files of THIS save, also printed; it says untrackedFormat=nul-separated and untrackedContents=not-saved
# Untracked file CONTENTS are NOT preserved by this saver (names only); copying them is LANE-DIRTY-SAVE-INVENTORY-ROWS-1 / LANE-DIRTY-SAVE-WORKSTREAM-WIRE-1 scope.
# The lane is named by -LanePid or by -ChildJson ({pid, createdUtc}; createdUtc is required and must be an ISO-8601 string, else exit 30 before any write; a process that started after it, at full precision, is a reused pid, so the lane is dead), never both.
# The whole worktree is collected: a -WorkDir below the top level is lifted to `git rev-parse --show-toplevel` and every diff gets --no-relative. A -RunDir inside that worktree (links followed) is refused (exit 30): evidence written there would be listed as untracked dirt.
# A -WorkDir, -RunDir or -ChildJson holding a `..` segment is refused with exit 30 reason=dotdot-path before any read (r12, sol r11 B1): path normalisation removes `link/..` before the OS follows the link, so the path judged would not be the path opened. Link-then-parent is not emulated.
# Paths are split on the platform's own separators only (a POSIX backslash is a filename character), and the RunDir that passes the containment test is the resolved path every artifact is then written through.
# Windows resolves both directories through an open handle (GetFinalPathNameByHandle: links, junctions, subst drives, 8.3 and \\?\ spellings in any case) and keeps the handle open, so the checked RunDir cannot be renamed or replaced before the write. POSIX walks symlinks by name, then also refuses a RunDir (or ancestor) whose st_dev:st_ino is the worktree root or any directory inside it, so a bind mount cannot alias it; if that cannot be proven (no find/stat, unreadable tree) it exits 30. A swap between check and write is not closed on POSIX.
# HEAD is resolved ONCE and both diffs run against that sha, so the receipt head is the patch base.
# Exit: 0 saved (or already saved with these exact bytes), 10 nothing to save, 20 lane alive (refused, nothing written), 30 bad arguments, 40 git failed, 50 writing into RunDir failed (artifacts already written stay), 60 INCOMPLETE: git exited 0 but said something on stderr (an unreadable directory, a config it could not read), so the collection may be missing evidence. 60 is never "clean": with nothing to save it prints DIRTY_SAVE_INCOMPLETE and the warning text; with something saved the receipt carries collection=incomplete and warnings, and what was readable is still written.
# Only git's line-ending conversion notices ("LF will be replaced by CRLF ...", printed for a file git DID compare) are not warnings, and only as WHOLE lines of git's known shape (r12, sol r11 B2). Everything else on stderr is (r11, sol r10 B2).
# The untracked names exclude the saver's own files by name (its GUID path component) AND by identity: on Windows a name below a directory link that resolves into the private directory is dropped (r12, sol r11 B3). Every name comparison is ordinal, never by culture (r12, sol r11 B4).
# Never stashes, resets, checks out, cleans or deletes (the one removal is its own private git dir, below). Each file is created CREATE_NEW; a name taken by different bytes is skipped (.2, .3 ... suffix), by the same bytes reused,
# a DIRECTORY at a name is skipped too, and a taken name whose bytes stay unreadable after 5 tries is skipped and listed in the receipt as comparisonUnreadable (never called "different").
# Every git call gets GIT_TRACE*=0 in its OWN environment (never the caller's): a trace variable or Trace2 config naming a tracked file would make git
# append to it (sol r20 B1 on #698), and removing a caller's disable is what exposed it. GIT_OPTIONAL_LOCKS=0 keeps git from refreshing the index.
# NO CALLBACK CAN RUN (r10, sol r9 B1+B2): every call that reads the tree runs against a private git dir in the OS temp directory, or the RunDir when temp is not provably outside the worktree (never under the worktree or a git dir beneath it; removed on exit) whose config holds no program; system/global config are off and config-injecting env is stripped, so a clean/smudge/process filter, textconv, fsmonitor or diff.external, added at any time or in any submodule, has nothing to be read from. Submodules are scanned by NOTHING (--ignore-submodules=dirty keeps a changed gitlink commit, drops their dirty contents); the receipt says submodules="skipped <n>".
# Every git call also gets --no-replace-objects: a refs/replace entry for HEAD would make the diffs read another commit's tree while the receipt names HEAD (adversary r9 A2). Path and name comparisons are ordinal, never by culture (U+00AD and case are ignored by culture compares).
param(
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$RunDir,
    [int]$LanePid = 0,
    [string]$ChildJson = ''
)
$ErrorActionPreference = 'Stop'
$script:unreadable = @()
$script:gitStderr = @()  # stderr of every git call that exited 0: a warning there is a collection that may be incomplete (r11 sol r10 B2)
$script:seal = $null; $script:sealDir = $null
$script:noCallbacks = @('-c', 'core.fsmonitor=false')  # the few read-only calls made BEFORE the seal (toplevel, HEAD, paths, config list) run nothing, and core.fsmonitor is blanked anyway (sol r7 B1)
function Invoke-GitBytes([string[]]$GitArgs, [switch]$KeepConfigEnv) {  # -KeepConfigEnv: the ONE call that reads the caller's effective config as data keeps the config-injecting environment (r11 sol r10 B1); every other call has it stripped
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new('git')
        foreach ($a in (@('-C', $WorkDir, '--no-replace-objects') + $(if ($script:seal) { $script:seal.Args } else { $script:noCallbacks }) + $GitArgs)) { $psi.ArgumentList.Add($a) }
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
        foreach ($k in 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_DIFF_OPTS', 'GIT_CONFIG', 'GIT_REDIRECT_STDOUT', 'GIT_REDIRECT_STDERR', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_ATTR_SOURCE', 'GIT_NAMESPACE') { [void]$psi.Environment.Remove($k) }  # GIT_REDIRECT_STDOUT/STDERR make Git for Windows write its output into a named file, a tracked one included (r8 A4)
        if (-not $KeepConfigEnv) {  # the config-injecting variables would hand a callback to the sealed git (r10). The effective-config read keeps them, so a core.autocrlf, core.eol or core.excludesFile the caller injects reaches the private config like any other (r11 sol r10 B1); the allowlist, not this stripping, is what keeps a callback out
            foreach ($k in 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_SYSTEM') { [void]$psi.Environment.Remove($k) }
            foreach ($k in @($psi.Environment.Keys | Where-Object { $_.StartsWith('GIT_CONFIG_KEY_', [System.StringComparison]::OrdinalIgnoreCase) -or $_.StartsWith('GIT_CONFIG_VALUE_', [System.StringComparison]::OrdinalIgnoreCase) })) { [void]$psi.Environment.Remove($k) }
        }
        if ($script:seal) { foreach ($k in $script:seal.Env.Keys) { $psi.Environment[$k] = $script:seal.Env[$k] } }
        $psi.Environment['GIT_OPTIONAL_LOCKS'] = '0'
        foreach ($k in (@('GIT_TRACE', 'GIT_TRACE2', 'GIT_TRACE2_EVENT', 'GIT_TRACE2_PERF') + @($psi.Environment.Keys | Where-Object { $_.StartsWith('GIT_TRACE', [System.StringComparison]::OrdinalIgnoreCase) }))) { $psi.Environment[$k] = '0' }
        foreach ($k in 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_SSH_VARIANT', 'GIT_ASKPASS', 'SSH_ASKPASS', 'GIT_PROXY_COMMAND', 'GIT_EXTERNAL_DIFF', 'GIT_PAGER', 'GIT_EXEC_PATH') { [void]$psi.Environment.Remove($k) }  # each names a program git may run; a diff over a partial clone's missing blob starts a promisor fetch that runs the ssh one, and exit 40 would be reported only after it rewrote a tracked file (adversary r9 A3)
        $psi.Environment['GIT_NO_LAZY_FETCH'] = '1'; $psi.Environment['GIT_TERMINAL_PROMPT'] = '0'  # git 2.44+ then never lazy-fetches at all
        $p = [System.Diagnostics.Process]::Start($psi)
        $err = $p.StandardError.ReadToEndAsync()
        $ms = [System.IO.MemoryStream]::new()
        $p.StandardOutput.BaseStream.CopyTo($ms)
        $p.WaitForExit()
        if ($p.ExitCode -eq 0 -and $err.Result.Trim()) { $script:gitStderr += $err.Result }
        return @{ Code = $p.ExitCode; Bytes = $ms.ToArray(); Err = $err.Result }
    } catch { return @{ Code = -1; Bytes = [byte[]]@(); Err = $_.Exception.Message } }
}
function Remove-GitTerminator([byte[]]$Bytes) {  # git ends a path line with exactly one LF (never CRLF, also on Windows): strip that LF and nothing else (r5 ADV-A4: a worktree named 'repo ' is not the sibling 'repo'; r6 A1: a name ending in CR keeps its CR)
    $s = [System.Text.UTF8Encoding]::new($false, $true).GetString($Bytes)  # strict: a lossy decode turns byte FF into U+FFFD and can name a different, valid sibling (adversary r8 A3)
    if ($s.EndsWith("`n", [System.StringComparison]::Ordinal)) { $s = $s.Substring(0, $s.Length - 1) }
    return $s
}
function New-SealedGit([string]$HeadFull) {  # r10 (sol r9 B1, B2): no git call that reads the tree may be able to run a configured callback. Chasing filter NAMES (r8, r9) always missed one: a filter added after the search, or one inside a submodule below the depth cap. So every collecting call runs against a PRIVATE git dir whose config holds no program at all: system and global config are off, the repo's own config is not read (only an allowlist of plain comparison settings and rule-file paths is copied, byte for byte, into the private config), and the objects, index and work tree are the real ones. A filter, textconv, fsmonitor, diff.external or hook defined anywhere then has nothing to be read from.
    $paths = @()  # one call per path: a POSIX path may hold an LF, so the five cannot share one LF-separated answer (adversary r10 A3)
    foreach ($pa in @(@('--absolute-git-dir'), @('--git-path', 'index'), @('--git-path', 'objects'), @('--git-path', 'info/attributes'), @('--git-path', 'info/exclude'))) {
        $q = Invoke-GitBytes (@('rev-parse', '--path-format=absolute') + $pa)
        if ($q.Code -ne 0) { throw $q.Err.Trim() }
        $paths += Remove-GitTerminator $q.Bytes
    }
    $gitDir, $index, $objects, $attr, $excl = $paths
    $c = Invoke-GitBytes @('config', '--null', '--list') -KeepConfigEnv  # the EFFECTIVE config (system, global, includes, local, worktree, AND the caller's GIT_CONFIG_COUNT/KEY/VALUE, GIT_CONFIG_PARAMETERS and GIT_CONFIG_SYSTEM), read as data: what git itself would compare and convert by (adversary r10 A1; sol r10 B1). It runs nothing. Git that cannot parse the injection exits non-zero here, which is a typed refusal (40) before anything is collected, never a collection under other settings
    if ($c.Code -ne 0) { throw $c.Err.Trim() }
    $allow = 'core.filemode', 'core.symlinks', 'core.ignorecase', 'core.autocrlf', 'core.eol', 'core.safecrlf', 'core.precomposeunicode', 'core.trustctime', 'core.checkstat', 'core.protectntfs', 'core.protecthfs', 'core.longpaths', 'core.bigfilethreshold', 'core.quotepath'  # how git compares and converts bytes; none names a program
    $allow += 'core.excludesfile', 'core.attributesfile'  # a path to a list of RULES (data): kept, so the sealed git reads the same ignore and attribute files; unset, git's own default applies identically (adversary r10 A2)
    $vals = [ordered]@{}; $sha256 = $false
    foreach ($e in ([System.Text.Encoding]::Latin1.GetString($c.Bytes) -split "`0" | Where-Object { $_ })) {  # Latin-1: one char per byte, so values reach the private config byte for byte (a path that is not UTF-8 included) and the keys wanted here are ASCII (any other entry, a non-UTF-8 filter name included, matches nothing)
        $i = $e.IndexOf([char]10); $k = if ($i -ge 0) { $e.Substring(0, $i) } else { $e }; $v = if ($i -ge 0) { $e.Substring($i + 1) } else { $null }  # char overloads and Equals(..., Ordinal) throughout: the string IndexOf and the linguistic equality operators ignore U+00AD (r12 sol r11 B4)
        if ([string]::Equals($k, 'extensions.objectformat', [System.StringComparison]::Ordinal)) { $sha256 = [string]::Equals($v, 'sha256', [System.StringComparison]::Ordinal) }
        elseif ([System.Array]::IndexOf($allow, $k) -ge 0) { $vals[$k] = $v }  # last value wins; the value is whatever git accepted (+1 is a boolean too), quoted below, never parsed here (adversary r10 A1 pass 2)
    }
    $cfg = "[core]`n`trepositoryformatversion = $(if ($sha256) { 1 } else { 0 })`n"
    foreach ($k in $vals.Keys) { $cfg += "`t" + $k.Substring(5) + $(if ($null -eq $vals[$k]) { '' } else { ' = "' + $vals[$k].Replace('\', '\\').Replace('"', '\"').Replace("`n", '\n').Replace("`t", '\t').Replace("`b", '\b') + '"' }) + "`n" }
    if ($sha256) { $cfg += "[extensions]`n`tobjectformat = sha256`n" }
    $sg = [System.IO.Path]::Combine((Get-PrivateBase), 'lane-dirty-save-' + [guid]::NewGuid().ToString('N')); $script:sealDir = $sg  # never under the worktree or a git dir inside it (a separate git dir can sit there): the untracked scan would list the saver's own files and the clean-up would then delete them (r11 sol r10 B3)
    foreach ($sub in @('objects', 'info'), @('refs'), @('info')) { [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::Combine([string[]](@($sg) + $sub))) }  # one segment per argument: a \\?\ path (the RunDir fall-back) does not translate a slash
    $enc = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText([System.IO.Path]::Combine($sg, 'HEAD'), "$HeadFull`n", $enc)
    [System.IO.File]::WriteAllBytes([System.IO.Path]::Combine($sg, 'config'), [System.Text.Encoding]::Latin1.GetBytes($cfg))
    $alt = if ($objects.IndexOfAny([char[]]@("`n", "`r", '"', '\')) -ge 0 -or $objects.StartsWith('#', [System.StringComparison]::Ordinal)) { '"' + $objects.Replace('\', '\\').Replace('"', '\"').Replace("`n", '\n').Replace("`r", '\r') + '"' } else { $objects }  # alternates is one path per LINE: a path holding an LF or a quote is written C-quoted (adversary r10 A3)
    [System.IO.File]::WriteAllText([System.IO.Path]::Combine($sg, 'objects', 'info', 'alternates'), "$alt`n", $enc)
    foreach ($pair in @(, @($attr, 'attributes')) + @(, @($excl, 'exclude'))) {  # attribute and ignore RULES are data; a filter attribute without a configured driver runs nothing
        $dest = [System.IO.Path]::Combine($sg, 'info', $pair[1])
        try { [System.IO.File]::Copy($pair[0], $dest) }
        catch [System.IO.FileNotFoundException], [System.IO.DirectoryNotFoundException] { continue }  # absent is the only silent case: File.Exists answers false for a file it may not look at, which dropped the rules and let the sealed git compare by other ones (adversary r11 A2). Any other failure ends in the typed private-git-dir refusal (40)
        [System.IO.File]::SetAttributes($dest, [System.IO.FileAttributes]::Normal)
    }
    $nullDev = if ($script:onWin) { 'NUL' } else { '/dev/null' }
    $script:seal = @{ Args = $script:noCallbacks; Env = @{ GIT_DIR = $sg; GIT_WORK_TREE = $WorkDir; GIT_INDEX_FILE = $index; GIT_CONFIG_NOSYSTEM = '1'; GIT_CONFIG_GLOBAL = $nullDev } }  # the REAL index file: copying it would reset its timestamp and hide racily-clean entries
}
$script:onWin = [System.IO.Path]::DirectorySeparatorChar -eq '\'  # a backslash separates path parts on Windows only; on POSIX it is a literal filename character (dot B1 on #722)
function Test-DirPresent([string]$Path) {  # $false ONLY when the directory is really gone. Directory.Exists answers false for one it may not look at (a parent it cannot search), which read as a clean removal and left the private directory unnamed (adversary r11 A2). Unknown is present
    try { [void][System.IO.Directory]::GetFileSystemEntries($Path); return $true }
    catch [System.IO.DirectoryNotFoundException] { return $false }
    catch { return $true }
}
function Remove-OwnNames([byte[]]$Bytes, [string]$Own) {  # the saver never lists itself: its private directory is named by a random GUID, so any NUL-separated name holding that whole path component is the saver's own file, however git reached it (an inward junction to the temp directory, a directory swapped for a link after the placement check; adversary r11 pass 2 A1, A2). Where the directory is cannot be proven unreachable; what git lists can be filtered
    if (-not $Own -or $Bytes.Length -eq 0) { return , $Bytes }
    $keep = @([System.Text.Encoding]::Latin1.GetString($Bytes) -split "`0" | Where-Object { $_ -and -not ([string]::Equals($_, $Own, [System.StringComparison]::Ordinal) -or $_.StartsWith($Own + '/', [System.StringComparison]::Ordinal) -or $_.EndsWith('/' + $Own, [System.StringComparison]::Ordinal) -or $_.Contains('/' + $Own + '/')) })
    if (-not $keep) { return , [byte[]]@() }
    return , [System.Text.Encoding]::Latin1.GetBytes(($keep -join "`0") + "`0")
}
function Test-ResolvesInto([string]$Work, [string]$RelDir, [string]$Priv) {  # Windows: $true when the directory $RelDir (a git path below $Work) is a link whose final target is $Priv or below it. Identity, not spelling: both sides are read back by the kernel (Get-FinalDir), so 8.3 names, other casing and nested links cannot hide an alias. Anything that cannot be examined is $false: a name is kept, never dropped, on a guess
    try {
        $abs = [System.IO.Path]::Combine($Work, [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::Latin1.GetBytes($RelDir)).Replace('/', '\'))
        if (-not ([System.IO.File]::GetAttributes($abs) -band [System.IO.FileAttributes]::ReparsePoint)) { return $false }
        $f = Get-FinalDir $abs; $f.Handle.Dispose()
        return (Test-Inside $Priv $f.Path)
    } catch { return $false }
}
function Remove-AliasedNames([byte[]]$Bytes, [string]$Work, [string]$Private) {  # r12 (sol r11 B3): the saver never observes itself, by IDENTITY. A junction made straight to the private directory has no GUID in its name, so git lists alias/HEAD, alias/config ... and the clean-up then deletes the files those names stand for. Every name below a directory link that resolves into the private directory is the saver's own file and is dropped. A link listed as a file itself (a Windows symbolic link, which git does not walk) survives clean-up and stays in the list. POSIX git does not walk symlinks, so there is nothing to resolve there. An unresolvable private directory throws (typed exit 40), never a quiet pass
    if (-not $script:onWin -or -not $Private -or $Bytes.Length -eq 0) { return , $Bytes }
    $f = Get-FinalDir $Private; $f.Handle.Dispose(); $priv = $f.Path  # the handle is released at once: it would block the clean-up delete
    $into = [System.Collections.Generic.Dictionary[string, bool]]::new([System.StringComparer]::Ordinal)
    $keep = [System.Collections.Generic.List[string]]::new()
    foreach ($name in ([System.Text.Encoding]::Latin1.GetString($Bytes) -split "`0" | Where-Object { $_ })) {
        $hit = $false; $at = -1
        while (-not $hit -and ($at = $name.IndexOf([char]47, $at + 1)) -ge 0) {
            $dir = $name.Substring(0, $at)
            if (-not $into.ContainsKey($dir)) { $into[$dir] = Test-ResolvesInto $Work $dir $priv }
            $hit = $into[$dir]
        }
        if (-not $hit) { $keep.Add($name) }
    }
    if ($keep.Count -eq 0) { return , [byte[]]@() }
    return , [System.Text.Encoding]::Latin1.GetBytes(($keep -join "`0") + "`0")
}
function Get-WarningLines([string[]]$Stderr) {  # what git said on stderr, minus its line-ending conversion notices (printed for a file git DID compare). Matched over the WHOLE text, not per line: a notice holds the raw file name, which may hold a line feed (adversary r11 A3). Anything else, or a notice in another language, stays a warning: unknown is never "complete"
    $all = (@($Stderr) -join "`n").Replace("`r`n", "`n")
    $nm = "(?:(?!\n(?:(?:warning|error|fatal|hint): |in the working copy of '|(?:LF|CRLF) will be replaced by )).)*"  # a file name: anything up to the line feed that starts the NEXT git message
    $notice = "(?<![^\n])(?:warning: )?in the working copy of '$nm', (?:LF|CRLF) will be replaced by (?:LF|CRLF) the next time Git touches it(?![^\n])|(?<![^\n])(?:warning: )?(?:LF|CRLF) will be replaced by (?:LF|CRLF) in $nm\.\nThe file will have its original line endings in your working directory(?![^\n])"  # r12 (sol r11 B2, adversary A1): a notice is recognised only as WHOLE line(s) of git's known shape, from line start to line end; the file name is everything between the fixed prefix and the LAST fixed suffix that ends a line before the next git message begins (greedy, bounded), so suffix-looking text inside the name (x', LF will be replaced ... it), even one that closes a line in the middle of a name holding a line feed, cannot end the match early and leave a fragment. A warning is never swallowed: the name stops where the next message starts. (?<![^\n]) is 'at a line start', (?![^\n]) 'at a line end' (no dollar sign: it lets a trailing newline through)
    $rest = [regex]::Replace($all, $notice, '', [System.Text.RegularExpressions.RegexOptions]::Singleline)
    return @($rest -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
function Test-HasDotDot([string]$Path) {  # $true when a path holds a parent-directory segment. GetFullPath removes `link/..` lexically BEFORE the link is followed, but the OS follows the link first and then goes up from its target, so the path the saver judged is not the path the OS opens (r12 sol r11 B1). Link-then-parent is not emulated: such a path is refused. Ordinal; on Windows a segment of only dots and spaces (other than a single dot) is also refused, because Win32 trims those
    foreach ($seg in $Path.Split([char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar))) {
        if ([string]::Equals($seg, '..', [System.StringComparison]::Ordinal)) { return $true }
        if ($script:onWin -and $seg.Length -gt 0 -and -not [string]::Equals($seg, '.', [System.StringComparison]::Ordinal) -and $seg.Trim([char[]]@([char]46, [char]32)).Length -eq 0) { return $true }
    }
    return $false
}
function Test-Inside([string]$Parent, [string]$Child) {  # $true when $Child is $Parent or below it; ordinal, both already resolved to real paths
    $rel = [System.IO.Path]::GetRelativePath($Parent, $Child)
    return -not ([string]::Equals($rel, '..', [System.StringComparison]::Ordinal) -or $rel.StartsWith('..' + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::Ordinal) -or [System.IO.Path]::IsPathRooted($rel))
}
function Get-ShareOf([string]$Path) {  # Windows: the \\server\share a final path lives on, '' for a drive path. A share can expose a local folder (\\localhost\C$) and the kernel keeps the two spellings apart; only an identical \\server\share can be compared by name (r7 fix-2 A2)
    if ($Path.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) { return [System.IO.Path]::GetPathRoot($Path).ToLowerInvariant() }
    return ''
}
function Get-PrivateBase {  # where the saver's own temporary directory may live: the OS temp directory, unless that is not provably outside the worktree (the lane's TEMP can be inside it, on the hub VM it is), then the RunDir, which is already proven to be. The temp directory gets the SAME proofs the RunDir got, not a name compare alone: a bind mount (POSIX) or a loopback share (Windows) aliases a worktree folder under an outside-looking name (adversary r11 A1)
    foreach ($cand in @([System.IO.Path]::GetTempPath(), $RunDir)) {
        try {
            if ($script:onWin) {
                $f = Get-FinalDir $cand; $f.Handle.Dispose(); $real = $f.Path
                if (-not [string]::Equals((Get-ShareOf $real), (Get-ShareOf $realWork), [System.StringComparison]::Ordinal)) { continue }
            }
            else { $real = Resolve-RealPath $cand; if (-not [string]::Equals($cand, $RunDir, [System.StringComparison]::Ordinal)) { Assert-RunDirNotAlias $real $realWork } }  # throws when the temp directory is, or sits in, the worktree by identity, or when that cannot be proven: not used
            if (Test-Inside $realWork $real) { continue }
            if ($script:onWin -and $cand.StartsWith('\\?\', [System.StringComparison]::Ordinal)) {  # git rejects a \\?\ GIT_DIR, so it gets the plain spelling, and only when Win32 would not re-read it as another directory (a component ending in a dot or a space is trimmed)
                $cand = if ($cand.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) { '\\' + $cand.Substring(8) } else { $cand.Substring(4) }
                if (@($cand.Split('\') | Where-Object { $_ -and ($_.EndsWith([char]46) -or $_.EndsWith([char]32)) }).Count) { continue }
            }
            return $cand
        } catch { continue }
    }
    throw 'no-private-dir-outside-worktree'
}
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
function Invoke-Capture([string]$Exe, [string[]]$CmdArgs) {  # one child's stdout lines; $null when it cannot start or exits non-zero (unknown is never "clear")
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new($Exe)
        foreach ($a in $CmdArgs) { $psi.ArgumentList.Add($a) }
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
        $p = [System.Diagnostics.Process]::Start($psi)
        $err = $p.StandardError.ReadToEndAsync(); $out = $p.StandardOutput.ReadToEnd(); $p.WaitForExit()
        if ($p.ExitCode -ne 0) { return $null }
        return , @($out -split "`n" | Where-Object { $_ })
    } catch { return $null }
}
function Get-DirIds([string[]]$Dirs, [bool]$Tree) {  # POSIX 'st_dev:st_ino' of each of $Dirs, or of every directory under $Dirs[0]. GNU stat (-c) and BSD stat (-f) are told apart first, because GNU's -f prints filesystem data shaped like an id. $null = unprovable
    $fmt = if ($null -ne (Invoke-Capture 'stat' @('--version'))) { '-c' } else { '-f' }
    $lines = if ($Tree) { Invoke-Capture 'find' @($Dirs[0], '-type', 'd', '-exec', 'stat', '-L', $fmt, '%d:%i', '{}', '+') } else { Invoke-Capture 'stat' (@('-L', $fmt, '%d:%i', '--') + $Dirs) }
    if ($null -eq $lines -or @($lines).Count -lt 1 -or @($lines | Where-Object { $_ -notmatch '\A[0-9]+:[0-9]+\z' }).Count) { return $null }
    return , @($lines)
}
function Assert-RunDirNotAlias([string]$Run, [string]$Work) {  # POSIX: a bind mount has no link target, so names cannot see it; compare file identity instead. The RunDir and each ancestor must not BE the worktree root or any directory inside it (sol r7 B2). Not provable = refused
    $chain = @(); for ($d = $Run; $d; $d = [System.IO.Path]::GetDirectoryName($d)) { $chain += $d }
    $mine = Get-DirIds $chain $false; $inside = Get-DirIds @($Work) $true
    if ($null -eq $mine -or $mine.Count -ne $chain.Count -or $null -eq $inside) { throw 'run-dir-identity-unprovable' }
    if (@($mine | Where-Object { $inside -contains $_ }).Count) { throw 'run-dir-inside-worktree' }
}
function ConvertTo-NativePath([string]$Path) {  # Windows: \\?\ and \\.\ (any case, either slash) are one spelling, rewritten to \\?\ and KEPT: stripping it would let Win32 trim a trailing dot or space and open the sibling directory (r7 fix-2 A1). A form with no drive or UNC meaning is refused, never guessed
    if (-not $script:onWin) { return $Path }
    $p = $Path.Replace('/', '\')
    if ($p -notmatch '^\\\\[?.]\\') { return $p }
    if ($p -match '^\\\\[?.]\\UNC\\(?<rest>.+)\z') { return '\\?\UNC\' + $Matches['rest'] }
    if ($p -match '^\\\\[?.]\\(?<rest>[A-Za-z]:\\.*)\z') { return '\\?\' + $Matches['rest'] }
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
        # System.Text.Json, not ConvertFrom-Json: PowerShell turns '/Date(0)/' into a DateTime, unwraps one-item lists and casts arrays to int, so every shape check was passed by something that is no ISO string (adversary r8 A2, A3)
        $root = [System.Text.Json.JsonDocument]::Parse([System.IO.File]::ReadAllText($ChildJson)).RootElement
        $pe = [System.Text.Json.JsonElement]::new(); $ce = [System.Text.Json.JsonElement]::new()
        if ($root.ValueKind -ne 'Object' -or -not $root.TryGetProperty('pid', [ref]$pe) -or $pe.ValueKind -ne 'Number' -or -not $pe.TryGetInt32([ref]$procId)) { throw 'bad-child-json' }
        if (-not $root.TryGetProperty('createdUtc', [ref]$ce) -or $ce.ValueKind -ne 'String') { throw 'bad-created-utc' }  # absent, null, a number (1 casts to year 0001), prose or '/Date(0)/' is no timestamp: without one a reused pid cannot be told from the lane (sol r4 B2, r7 B3)
        $stamp = $ce.GetString(); $when = [datetime]::MinValue
        # \A...\z, never ^...$ (which lets one trailing LF through), [0-9] not \d (which matches other scripts' digits), then an exact-format parse: a value with an embedded or trailing newline is no timestamp (sol r8b B2)
        if ($stamp -cnotmatch '\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,7})?(Z|[+-][0-9]{2}:[0-9]{2})\z' -or -not [datetime]::TryParseExact($stamp, [string[]]@('yyyy-MM-dd\THH:mm:ssK', 'yyyy-MM-dd\THH:mm:ss.FFFFFFFK'), [cultureinfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind, [ref]$when)) { throw 'bad-created-utc' }
        $born = $when.ToUniversalTime()
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
    foreach ($raw in $WorkDir, $RunDir, $ChildJson) { if ($raw -and (Test-HasDotDot $raw)) { throw 'dotdot-path' } }  # before any normalisation or read (r12 sol r11 B1); exit 30 with a reason of its own
    $here = (Get-Location).ProviderPath  # PowerShell's location, not .NET's (adversary A6)
    $WorkDir = [System.IO.Path]::GetFullPath($WorkDir, $here); $RunDir = [System.IO.Path]::GetFullPath($RunDir, $here)  # .NET only: PowerShell's path layer may re-spell a POSIX backslash as '/' (dot B1 on #722)
    if ($ChildJson) { $ChildJson = [System.IO.Path]::GetFullPath($ChildJson, $here) }  # every path the saver reads is resolved against PowerShell's location before any read: a relative record must never be opened through .NET's current directory, where another lane's record may sit (sol r8b B1)
    if (-not [System.IO.Directory]::Exists($WorkDir) -or -not [System.IO.Directory]::Exists($RunDir)) { throw 'no-such-directory' }
    if (Test-LaneAlive) { Write-Output 'DIRTY_SAVE_REFUSED reason=lane-alive'; exit 20 }
} catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)"; exit 30 }
$flags = @('--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--no-relative', '--submodule=short', '--ignore-submodules=dirty', '--unified=3', '--src-prefix=a/', '--dst-prefix=b/')
$top = Invoke-GitBytes @('rev-parse', '--show-toplevel')
if ($top.Code -eq 0) {
    try { $WorkDir = Remove-GitTerminator $top.Bytes } catch { Write-Output 'DIRTY_SAVE_BAD_ARGS reason=toplevel-not-utf8'; exit 30 }
}
try {
    if ($script:onWin) {
        $script:held = @((Get-FinalDir $RunDir), (Get-FinalDir $WorkDir)); $RunDir = $script:held[0].Path; $realWork = $script:held[1].Path
        if (-not [string]::Equals((Get-ShareOf $RunDir), (Get-ShareOf $realWork), [System.StringComparison]::Ordinal)) { throw 'run-dir-namespace-unprovable' }  # see Get-ShareOf (r7 fix-2 A2)
    }
    else { $RunDir = Resolve-RealPath $RunDir; $realWork = Resolve-RealPath $WorkDir; if ($top.Code -eq 0) { Assert-RunDirNotAlias $RunDir $realWork } }
    $runInside = Test-Inside $realWork $RunDir
} catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)"; exit 30 }  # the directory that passes the containment test below IS the one written to: no re-spelling between check and use (dot B1 on #722)
if ($top.Code -eq 0 -and $runInside) { Write-Output 'DIRTY_SAVE_BAD_ARGS reason=run-dir-inside-worktree'; exit 30 }
$git = [ordered]@{}; $subs = 0
try {
    $headR = if ($top.Code -eq 0) { Invoke-GitBytes @('rev-parse', 'HEAD') } else { $top }
    $headFull = [System.Text.Encoding]::ASCII.GetString($headR.Bytes).Trim()
    if ($headR.Code -eq 0 -and $headFull -notmatch '\A[0-9a-f]{40,64}\z') { $headR = @{ Code = -1; Err = "unexpected HEAD '$headFull'" } }
    $git.head = $headR
    if ($headR.Code -eq 0) {
        New-SealedGit $headFull
        $git.index = Invoke-GitBytes @('ls-files', '-s', '-z')  # only to count gitlinks (mode 160000): their CONTENTS are not scanned (--ignore-submodules=dirty), so no submodule config is ever read
        if ($git.index.Code -eq 0) { $subs = @([System.Text.Encoding]::Latin1.GetString($git.index.Bytes) -split "`0" | Where-Object { $_.StartsWith('160000 ', [System.StringComparison]::Ordinal) }).Count }
        $git.patch = Invoke-GitBytes (@('diff', $headFull) + $flags); $git.staged = Invoke-GitBytes (@('diff', '--cached', $headFull) + $flags)
        $git.untracked = Invoke-GitBytes @('ls-files', '-z', '--others', '--exclude-standard')
        if ($git.untracked.Code -eq 0) { $git.untracked.Bytes = Remove-AliasedNames (Remove-OwnNames $git.untracked.Bytes ([System.IO.Path]::GetFileName($script:sealDir))) $WorkDir $script:sealDir }
    }
}
catch { $git.seal = @{ Code = -1; Err = "private-git-dir: $($_.Exception.Message)" } }
finally {  # only the private dir this run made. A handle another process holds open (no delete sharing, Windows) can keep it: retried, then NAMED in the output and the receipt, never swallowed (adversary r10 A4)
    for ($i = 0; $i -lt 5 -and $script:sealDir -and (Test-DirPresent $script:sealDir); $i++) { try { [System.IO.Directory]::Delete($script:sealDir, $true) } catch { Start-Sleep -Milliseconds 200 } }
}
$left = if ($script:sealDir -and (Test-DirPresent $script:sealDir)) { $script:sealDir } else { $null }
$leftNote = if ($left) { " privateGitDirLeft=$left" } else { '' }
$bad = $git.Values | Where-Object { $_.Code -ne 0 } | Select-Object -First 1
if ($bad) { Write-Output "DIRTY_SAVE_GIT_FAILED reason=$($bad.Err.Trim())$leftNote"; exit 40 }
$todo = 'patch', 'staged', 'untracked' | Where-Object { $git[$_].Bytes.Length -gt 0 }
# r11 (sol r10 B2): exit 0 with words on stderr is not a complete collection (git walks past a directory it cannot open and says so only there). Line-ending conversion notices are the one exception: git printed them for a file it DID compare.
$warnLines = Get-WarningLines $script:gitStderr
$warnShown = @($warnLines | Select-Object -First 20 | ForEach-Object { if ($_.Length -gt 300) { $_.Substring(0, 300) + '...' } else { $_ } })
if ($warnLines.Count -gt 20) { $warnShown += "... and $($warnLines.Count - 20) more" }
if (-not $todo) {
    if ($warnLines) { Write-Output "DIRTY_SAVE_INCOMPLETE reason=git-warning count=$($warnLines.Count) warnings=$($warnShown -join ' | ')$leftNote"; exit 60 }
    Write-Output "DIRTY_SAVE_NOTHING reason=clean-tree$leftNote"; exit 10
}
$head8 = $headFull.Substring(0, 8)
$ext = @{ patch = 'patch'; staged = 'staged.patch'; untracked = 'untracked.txt' }
$r = [ordered]@{ schema = 'lane-dirty-save/v1'; head = $headFull; untrackedFormat = 'nul-separated'; untrackedContents = 'not-saved'; submodules = "skipped $subs" }
if ($left) { $r.privateGitDirLeft = $left }
if ($warnLines) { $r.collection = 'incomplete'; $r.warnings = @($warnShown) }
try {
    foreach ($k in $todo) { $r[$k] = Save-Bytes $ext[$k] $git[$k].Bytes }
    if ($r.Contains('patch')) { $r.patchSha256 = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData($git.patch.Bytes)).Replace('-', '').ToLowerInvariant() }
    $make = { $r.comparisonUnreadable = @($script:unreadable); [System.Text.UTF8Encoding]::new($false).GetBytes(($r | ConvertTo-Json -Compress) + "`n") }
    [void](Save-Bytes 'receipt.json' $null $make)
} catch { Write-Output "DIRTY_SAVE_WRITE_FAILED reason=$($_.Exception.Message)$leftNote"; exit 50 }  # r6 A4: a read-only or full RunDir ends in a documented status; artifacts already written stay
Write-Output ([System.Text.UTF8Encoding]::new($false).GetString($script:written).TrimEnd("`n"))
exit $(if ($warnLines) { 60 } else { 0 })