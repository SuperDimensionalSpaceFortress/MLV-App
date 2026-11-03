# lane-dirty-save.ps1 - LANE-DIRTY-SAVE-CORE-1 (core split out of parked PR #698).
# A dead lane's uncommitted work is the one thing a refusal or a clean-up destroys. When the lane process is dead, this writes into -RunDir:
#   dirty-save-<head8>.patch   git diff HEAD --binary, byte for byte; .staged.patch = git diff --cached --binary (staged work the tree no longer has)
#   dirty-save-<head8>.untracked.txt = git ls-files -z --others --exclude-standard: untracked file NAMES only, NUL-separated and never C-quoted (git quotes " \ TAB LF even with core.quotepath=false)
#   .receipt.json = one JSON line naming the files of THIS save, also printed; it says untrackedFormat=nul-separated and untrackedContents=not-saved
# Untracked file CONTENTS are NOT preserved by this saver (names only); copying them is LANE-DIRTY-SAVE-INVENTORY-ROWS-1 / LANE-DIRTY-SAVE-WORKSTREAM-WIRE-1 scope.
# The lane is named by -LanePid or by -ChildJson ({pid, createdUtc}; createdUtc is required and must be an ISO-8601 string, else exit 30 before any write; a process that started after it, at full precision, is a reused pid, so the lane is dead), never both.
# The whole worktree is collected: a -WorkDir below the top level is lifted to `git rev-parse --show-toplevel` and every diff gets --no-relative. A -RunDir inside that worktree is refused (exit 30; r13: no link is ever followed, so "inside" is judged on a link-free path or by the kernel's own answer): evidence written there would be listed as untracked dirt.
# r13 (sol r12 F1-F3) ends the class "the saver computes a path's physical identity and the computation can be fooled" by REFUSING instead of computing. Exit 30 reasons, all before any read or write:
#   dotdot-path           a -WorkDir, -RunDir or -ChildJson holding a `..` segment (r12, sol r11 B1): normalisation removes `link/..` before the OS follows the link
#   not-fully-qualified   an argument that is not a fully qualified path ([System.IO.Path]::IsPathFullyQualified on the raw text): a relative path, a root-relative `\x` and a drive-relative `C:x` / `C:..\x` all depend on a current directory the saver cannot name (r13, sol r12 F1)
#   link-in-path          any component of -WorkDir, -RunDir, -ChildJson (the file itself included), the git top level or the private base is a symbolic link, junction or other reparse point (r13, sol r12 F2): no link is ever resolved or followed to decide identity
#   path-unreadable       the attributes of a component cannot be read: unknown is a refusal, never a pass
# An unreadable private-base candidate (the OS temp directory) is skipped and the RunDir is used instead; with none left the run ends in exit 40 (private-git-dir: no-private-dir-outside-worktree).
# An alias probe that cannot be read is not "unrelated" (r13, sol r12 F3): the names below that directory stay in the list, the warning "alias-inspection-failed dir=..." is added, and the run is collection=incomplete (exit 60).
# Paths are split on the platform's own separators only (a POSIX backslash is a filename character), and the RunDir that passes the containment test is the resolved path every artifact is then written through.
# Windows, with no link in either chain, reads both directories back through an open handle (GetFinalPathNameByHandle: subst drives, 8.3 and \\?\ spellings in any case, loopback shares) and keeps the handle open, so the checked RunDir cannot be renamed or replaced before the write. POSIX, with no symlink in either chain, then also refuses a RunDir (or ancestor) whose st_dev:st_ino is the worktree root or any directory inside it, so a bind mount cannot alias it; if that cannot be proven (no find/stat, unreadable tree) it exits 30. A swap between check and write is not closed on POSIX. The same limit holds for the repository and the private base: Windows holds the captured git dir and the accepted private base open for the whole run; POSIX re-reads the git dir and work tree identities before and after EVERY git call and once more after collection (r15, sol r14 B1 = adversary r14 pass 2 A1: a git dir swapped away and RESTORED during the run is seen at the call it affected and ends in repository-changed, never in clean-tree). The one window no re-read can see is a swap that begins and ends inside a single git process's lifetime.
# HEAD is resolved ONCE and both diffs run against that sha, so the receipt head is the patch base.
# An unborn HEAD (a repository with no commit) is a git failure, exit 40 DIRTY_SAVE_GIT_FAILED with the git message, and nothing is written (r17, Dot/CoS G3): there is no commit to diff against, so what is staged or untracked cannot be stated relative to anything. It is never a clean tree (10) and never a save; the lane's work is then reachable only through the repository itself (git ls-files --stage). A first commit makes the saver usable.
# Exit: 0 saved (a run never reuses an earlier run's file, even one with the same bytes: r16), 10 nothing to save, 20 lane alive (refused, nothing written), 30 bad arguments, 40 git failed, 50 writing into RunDir failed (artifacts already written stay), 60 INCOMPLETE: git exited 0 but said something on stderr (an unreadable directory, a config it could not read), so the collection may be missing evidence. 60 is never "clean": with nothing to save it prints DIRTY_SAVE_INCOMPLETE and the warning text; with something saved the receipt carries collection=incomplete and warnings, and what was readable is still written.
# Only git's line-ending conversion notices ("LF will be replaced by CRLF ...", printed for a file git DID compare) are not warnings, and only as WHOLE lines of git's known shape (r12, sol r11 B2). The private git dir also sets core.safecrlf=false (never the caller's value), so git prints no such notice in the first place (r12, adversary pass 2 A1). Everything else on stderr is (r11, sol r10 B2).
# r14 (sol r13 B1-B3) keeps the same rule: decide by PROVEN IDENTITY or refuse, never by a name or a string.
#   worktree-redirect     (exit 30, before any collection, B1) the selected repository is not provably ONE repository: GIT_DIR, GIT_WORK_TREE or GIT_COMMON_DIR is set in the caller's environment; core.worktree is set at any scope (stored config, -c, GIT_CONFIG_*, includes: `git config --list` is the judge) so the work tree is another directory than the one git was started in; or git, asked again from the lifted top level, does not name the same work tree and git dir (by file identity). The git dir and work tree are then captured ONCE and handed to every later git call as --git-dir/--work-tree, so no call rediscovers a repository. A submodule checkout (core.worktree is set in it) is refused by this rule.
#   lossy-argument        (exit 30, first check) a -WorkDir, -RunDir or -ChildJson holding U+FFFD: a POSIX argument with an invalid UTF-8 byte arrives as U+FFFD, indistinguishable from a real sibling directory of that name (adversary r14 pass 2 A3)
#   repository-changed    (exit 30, nothing written) the git dir or work tree captured at the start was not the same directory (by file identity) before or after some git call, or at the end: a peer swapped it, even if it put it back (r15, sol r14 B1). An identity that cannot be read at that point is run-dir-identity-unprovable, same exit. Windows also holds the git dir open for the whole run, so the swap is refused there; POSIX detects it at the next read (adversary r14 pass 1 A2)
#   B2: the saver's private directory and every artifact are proven OUTSIDE the worktree by identity (the private base is skipped, else exit 40, when it is not; a RunDir inside is exit 30), so no saver-owned path can be listed and no name is dropped by its spelling: an unrelated file under a directory that merely carries the private directory's name stays in the list. A directory reached through a link (a Windows junction from inside the worktree) is resolved by the kernel and its names are dropped only when it IS the private directory or below it.
#   B3: "inside" and "the same directory" are decided by file identity (volume serial + file index on Windows, st_dev:st_ino on POSIX) along the parent chain, never by comparing path strings: Path.GetRelativePath compares case-insensitively on Windows, so distinct case-sensitive siblings `wt` and `WT` read as one. An identity that cannot be read is exit 30 (run-dir-identity-unprovable), never a pass.
# The untracked names exclude names below a directory link that resolves into the private directory, by identity (r12, sol r11 B3; r14 B2). Every name comparison is ordinal, never by culture (r12, sol r11 B4).
# Never stashes, resets, checks out, cleans or deletes ANYTHING (r16, sol r15 B1: not even its own private git dir; that is left in place and named as privateGitDirLeft). Each file is created CREATE_NEW; a name that is taken (by any file, directory or link, with any bytes, the same ones included) is skipped for the next suffix (.2, .3 ...) and is never opened or read (r16, sol r15 B2: no existing artifact is ever compared or reused).
# Every git call gets GIT_TRACE*=0 in its OWN environment (never the caller's): a trace variable or Trace2 config naming a tracked file would make git
# append to it (sol r20 B1 on #698), and removing a caller's disable is what exposed it. GIT_OPTIONAL_LOCKS=0 keeps git from refreshing the index.
# NO CALLBACK CAN RUN (r10, sol r9 B1+B2): every call that reads the tree runs against a private git dir in the OS temp directory, or the RunDir when temp is not provably outside the worktree (never under the worktree or a git dir beneath it; left in place and named on exit, r16) whose config holds no program; system/global config are off and config-injecting env is stripped, so a clean/smudge/process filter, textconv, fsmonitor or diff.external, added at any time or in any submodule, has nothing to be read from. Submodules are scanned by NOTHING (--ignore-submodules=dirty keeps a changed gitlink commit, drops their dirty contents); the receipt says submodules="skipped <n>".
# Every git call also gets --no-replace-objects: a refs/replace entry for HEAD would make the diffs read another commit's tree while the receipt names HEAD (adversary r9 A2). Path and name comparisons are ordinal, never by culture (U+00AD and case are ignored by culture compares).
# r15 (sol r14 B1; adversary pass on 79641666 A1, A3, A4): the repository identity (git dir, work tree, and for a linked worktree the bytes of its `commondir` file) is re-read before and after EVERY git call; the private base is returned by its real path, never a re-mappable drive alias; the -ChildJson record is opened first, proved link-free again with the file held, and must equal what its name then holds; the private directory was deleted only while its name still held the directory this run made; an existing artifact name that is a link was skipped, never compared. r16 (sol r15 B1, B2) REMOVES both classes: nothing is deleted, an existing artifact is never read. OPEN, carried past r16: adversary A2 (a git dir replaced by another repository BEFORE the first identity read is indistinguishable from that repository having been selected; no re-read can tell) and any other repository metadata file a peer may rewrite and restore mid-run (alternates, packed-refs): the threat model excludes a peer with write access to the repository's own metadata during the run.
param(
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$RunDir,
    [int]$LanePid = 0,
    [string]$ChildJson = ''
)
$ErrorActionPreference = 'Stop'
$script:gitStderr = @()  # stderr of every git call that exited 0: a warning there is a collection that may be incomplete (r11 sol r10 B2)
$script:seal = $null; $script:sealDir = $null; $script:sealId = $null; $script:heldSeal = $null
$script:probeWarnings = @()  # identity probes that could not be read (r13 sol r12 F3): each is a warning, so the collection is incomplete
$script:repoChanged = $null  # r15 (sol r14 B1): set by Test-RepoSame the first time the captured repository is found to be another directory (or unreadable) before or after a git call; the run then ends in exit 30 whatever the git calls returned
$script:pin = @()  # r14 (sol r13 B1): --git-dir/--work-tree captured ONCE at the start; every pre-seal git call gets them, so none rediscovers a repository (a stored core.worktree can name another one)
$script:noCallbacks = @('-c', 'core.fsmonitor=false')  # the few read-only calls made BEFORE the seal (toplevel, HEAD, paths, config list) run nothing, and core.fsmonitor is blanked anyway (sol r7 B1)
function Test-RepoSame {  # r15 (sol r14 B1): $true while the git dir and work tree captured by Assert-SameRepo are still the same directories (by file identity). Called before AND after every git child, so a swap that is back in place at the end of the run (POSIX has no handle to refuse it) was still seen at the call it affected. Not yet captured = nothing to compare. The first mismatch, or an identity that cannot be read, is remembered in $script:repoChanged (exit 30) and every later call is refused
    if (-not $script:repoIds) { return $true }
    if ($script:repoChanged) { return $false }
    try { Assert-RepoUnchanged $gitDir $WorkDir $script:repoIds; return $true } catch { $script:repoChanged = $_.Exception.Message; return $false }
}
function Invoke-GitBytes([string[]]$GitArgs, [switch]$KeepConfigEnv) {  # -KeepConfigEnv: the ONE call that reads the caller's effective config as data keeps the config-injecting environment (r11 sol r10 B1); every other call has it stripped
    try {
        if (-not (Test-RepoSame)) { return @{ Code = -1; Bytes = [byte[]]@(); Err = 'repository-changed' } }
        $psi = [System.Diagnostics.ProcessStartInfo]::new('git')
        foreach ($a in (@('-C', $WorkDir, '--no-replace-objects') + $(if ($script:seal) { $script:seal.Args } else { $script:pin + $script:noCallbacks }) + $GitArgs)) { $psi.ArgumentList.Add($a) }
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
        if (-not (Test-RepoSame)) { return @{ Code = -1; Bytes = [byte[]]@(); Err = 'repository-changed' } }  # whatever this child read may have come from another repository: its output is dropped
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
    $allow = 'core.filemode', 'core.symlinks', 'core.ignorecase', 'core.autocrlf', 'core.eol', 'core.precomposeunicode', 'core.trustctime', 'core.checkstat', 'core.protectntfs', 'core.protecthfs', 'core.longpaths', 'core.bigfilethreshold', 'core.quotepath'  # how git compares and converts bytes; none names a program
    $allow += 'core.excludesfile', 'core.attributesfile'  # a path to a list of RULES (data): kept, so the sealed git reads the same ignore and attribute files; unset, git's own default applies identically (adversary r10 A2)
    $vals = [ordered]@{}; $sha256 = $false
    foreach ($e in ([System.Text.Encoding]::Latin1.GetString($c.Bytes) -split "`0" | Where-Object { $_ })) {  # Latin-1: one char per byte, so values reach the private config byte for byte (a path that is not UTF-8 included) and the keys wanted here are ASCII (any other entry, a non-UTF-8 filter name included, matches nothing)
        $i = $e.IndexOf([char]10); $k = if ($i -ge 0) { $e.Substring(0, $i) } else { $e }; $v = if ($i -ge 0) { $e.Substring($i + 1) } else { $null }  # char overloads and Equals(..., Ordinal) throughout: the string IndexOf and the linguistic equality operators ignore U+00AD (r12 sol r11 B4)
        if ([string]::Equals($k, 'extensions.objectformat', [System.StringComparison]::Ordinal)) { $sha256 = [string]::Equals($v, 'sha256', [System.StringComparison]::Ordinal) }
        elseif ([System.Array]::IndexOf($allow, $k) -ge 0) { $vals[$k] = $v }  # last value wins; the value is whatever git accepted (+1 is a boolean too), quoted below, never parsed here (adversary r10 A1 pass 2)
    }
    $cfg = "[core]`n`trepositoryformatversion = $(if ($sha256) { 1 } else { 0 })`n`tsafecrlf = false`n"  # r12 (adversary pass 2 A1): core.safecrlf only decides whether git SAYS something about a line-ending conversion (or dies on an irreversible one); it never changes the bytes compared. Forced off, git prints no conversion notice at all, so no stderr text has to be told apart from a file name that imitates one (a name may hold a line feed and any message text). Not copied from the caller for that reason
    foreach ($k in $vals.Keys) { $cfg += "`t" + $k.Substring(5) + $(if ($null -eq $vals[$k]) { '' } else { ' = "' + $vals[$k].Replace('\', '\\').Replace('"', '\"').Replace("`n", '\n').Replace("`t", '\t').Replace("`b", '\b') + '"' }) + "`n" }
    if ($sha256) { $cfg += "[extensions]`n`tobjectformat = sha256`n" }
    $sg = [System.IO.Path]::Combine((Get-PrivateBase), 'lane-dirty-save-' + [guid]::NewGuid().ToString('N')); $script:sealDir = $sg  # never under the worktree or a git dir inside it (a separate git dir can sit there): the untracked scan would list the saver's own files and the clean-up would then delete them (r11 sol r10 B3)
    [void][System.IO.Directory]::CreateDirectory($sg)  # r16 (adversary pass 1 A1): the directory is made, then HELD and proved to be at its own name BEFORE anything is written into it. Windows: opened without share-delete, so it cannot be moved away and replaced by a junction into the worktree, and its kernel final path must equal its own long path (a name that already resolves elsewhere is refused before a byte is written). POSIX cannot hold it: every private file is CREATE_NEW (an existing file is never truncated) and the directory is proved link-free and outside the worktree once written
    if ($script:onWin) { $script:heldSeal = Get-FinalDir $sg; Assert-HandleAtPath $script:heldSeal.Handle $sg }
    $script:sealId = $null; try { $script:sealId = Get-DirId $sg } catch { }  # r16 (adversary A2): the identity of the directory this run made; the leftover is reported verified only while the name still holds it
    $script:heldSub = [System.Collections.Generic.List[object]]::new()  # r17 (adversary A2): each subdirectory is created PARENT FIRST, then held and proved at its own path exactly like the root. Only the root was held before, so `objects` could be moved away and a junction to a worktree folder put at its name before objects/info/alternates was created
    foreach ($sub in @('objects'), @('objects', 'info'), @('refs'), @('info')) {  # one segment per argument: a \\?\ path (the RunDir fall-back) does not translate a slash
        $sd = [System.IO.Path]::Combine([string[]](@($sg) + $sub)); [void][System.IO.Directory]::CreateDirectory($sd)
        if ($script:onWin) { $hd = Get-FinalDir $sd; $script:heldSub.Add($hd.Handle); Assert-HandleAtPath $hd.Handle $sd }
        else { Assert-LinkFree $sd }  # POSIX cannot hold a directory: the new subdirectory is proved link-free once made (the documented limit, a swap after this proof, stays)
    }
    $enc = [System.Text.UTF8Encoding]::new($false)
    Write-NewFile ([System.IO.Path]::Combine($sg, 'HEAD')) $enc.GetBytes("$HeadFull`n")
    Write-NewFile ([System.IO.Path]::Combine($sg, 'config')) ([System.Text.Encoding]::Latin1.GetBytes($cfg))
    $alt = if ($objects.IndexOfAny([char[]]@("`n", "`r", '"', '\')) -ge 0 -or $objects.StartsWith('#', [System.StringComparison]::Ordinal)) { '"' + $objects.Replace('\', '\\').Replace('"', '\"').Replace("`n", '\n').Replace("`r", '\r') + '"' } else { $objects }  # alternates is one path per LINE: a path holding an LF or a quote is written C-quoted (adversary r10 A3)
    Write-NewFile ([System.IO.Path]::Combine($sg, 'objects', 'info', 'alternates')) $enc.GetBytes("$alt`n")
    foreach ($pair in @(, @($attr, 'attributes')) + @(, @($excl, 'exclude'))) {  # attribute and ignore RULES are data; a filter attribute without a configured driver runs nothing
        $dest = [System.IO.Path]::Combine($sg, 'info', $pair[1])
        try { [System.IO.File]::Copy($pair[0], $dest) }
        catch [System.IO.FileNotFoundException], [System.IO.DirectoryNotFoundException] { continue }  # absent is the only silent case: File.Exists answers false for a file it may not look at, which dropped the rules and let the sealed git compare by other ones (adversary r11 A2). Any other failure ends in the typed private-git-dir refusal (40)
        [System.IO.File]::SetAttributes($dest, [System.IO.FileAttributes]::Normal)
    }
    if (-not $script:onWin) { Assert-LinkFree $sg; if (Test-Inside $realWork $sg) { throw 'private-dir-inside-worktree' } }  # r16 (adversary A1): POSIX cannot hold the directory, so after the CREATE_NEW writes it is proved link-free and outside the worktree
    $nullDev = if ($script:onWin) { 'NUL' } else { '/dev/null' }
    $script:seal = @{ Args = $script:noCallbacks; Env = @{ GIT_DIR = $sg; GIT_WORK_TREE = $WorkDir; GIT_INDEX_FILE = $index; GIT_CONFIG_NOSYSTEM = '1'; GIT_CONFIG_GLOBAL = $nullDev } }  # the REAL index file: copying it would reset its timestamp and hide racily-clean entries
}
$script:onWin = [System.IO.Path]::DirectorySeparatorChar -eq '\'  # a backslash separates path parts on Windows only; on POSIX it is a literal filename character (dot B1 on #722)
function Test-DirPresent([string]$Path) {  # $false ONLY when the directory is really gone. Directory.Exists answers false for one it may not look at (a parent it cannot search), which read as a clean removal and left the private directory unnamed (adversary r11 A2). Unknown is present
    try { [void][System.IO.Directory]::GetFileSystemEntries($Path); return $true }
    catch [System.IO.DirectoryNotFoundException] { return $false }
    catch { return $true }
}
function Test-ResolvesInto([string]$Work, [string]$RelDir, [string]$Priv) {  # Windows: $true when the directory $RelDir (a git path below $Work) IS the private directory $Priv or is below it, $false when it is read and is not. Identity, not spelling or name: the directory is opened and the kernel reads its true path back (Get-FinalDir), so 8.3 names, other casing, nested links and a path that only passes THROUGH a link (an inward junction to the temp directory: `jtemp/lane-dirty-save-<guid>`) are all resolved, then judged by file identity (Test-Inside). r14 (sol r13 B2): a directory that merely carries the private directory's NAME resolves elsewhere and stays. THREE states (r13 sol r12 F3): anything that cannot be examined is $null (unknown), never $false: the caller keeps the name AND reports the collection as incomplete
    try {
        $abs = [System.IO.Path]::Combine($Work, [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::Latin1.GetBytes($RelDir)).Replace('/', '\'))
        $f = Get-FinalDir $abs; $f.Handle.Dispose()
        return (Test-Inside $Priv $f.Path)
    } catch { return $null }
}
function Remove-AliasedNames([byte[]]$Bytes, [string]$Work, [string]$Private) {  # r12 (sol r11 B3): the saver never observes itself, by IDENTITY. A junction made straight to the private directory has no GUID in its name, so git lists alias/HEAD, alias/config ... and the clean-up then deletes the files those names stand for. Every name below a directory link that resolves into the private directory is the saver's own file and is dropped. A link listed as a file itself (a Windows symbolic link, which git does not walk) survives clean-up and stays in the list. POSIX git does not walk symlinks, so there is nothing to resolve there. An unresolvable private directory throws (typed exit 40), never a quiet pass. A directory whose probe cannot be read (r13 sol r12 F3) is UNKNOWN: its names stay in the list and one "alias-inspection-failed" warning per directory is added to $script:probeWarnings, which makes the run collection=incomplete (exit 60)
    if (-not $script:onWin -or -not $Private -or $Bytes.Length -eq 0) { return , $Bytes }
    $f = Get-FinalDir $Private; $f.Handle.Dispose(); $priv = $f.Path  # the handle is released at once: it would block the clean-up delete
    $into = [System.Collections.Generic.Dictionary[string, int]]::new([System.StringComparer]::Ordinal)  # 1 resolves into the private directory, 0 does not, -1 unknown
    $keep = [System.Collections.Generic.List[string]]::new()
    foreach ($name in ([System.Text.Encoding]::Latin1.GetString($Bytes) -split "`0" | Where-Object { $_ })) {
        $hit = $false; $at = -1
        while (-not $hit -and ($at = $name.IndexOf([char]47, $at + 1)) -ge 0) {
            $dir = $name.Substring(0, $at)
            if (-not $into.ContainsKey($dir)) {
                $state = Test-ResolvesInto $Work $dir $priv
                $into[$dir] = if ($null -eq $state) { -1 } elseif ($state) { 1 } else { 0 }
                if ($into[$dir] -lt 0) { $script:probeWarnings += 'alias-inspection-failed dir=' + [regex]::Replace($dir.Length -gt 200 ? $dir.Substring(0, 200) : $dir, '[\x00-\x1f\x7f]', '?') }
            }
            $hit = $into[$dir] -gt 0
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
function Get-DirId([string]$Path, [scriptblock]$ReadId = $null) {  # r14 (sol r13 B3): one directory's FILE IDENTITY: Windows volume serial + file index (FILE_ID_INFO, else BY_HANDLE_FILE_INFORMATION), POSIX st_dev:st_ino. Two spellings of one directory share it, two directories that differ only by case (a case-sensitive NTFS folder) do not. $ReadId is a test seam: a scriptblock that takes the path and returns its identity (or throws). A read that fails is exit 30 (run-dir-identity-unprovable), never an answer
    try {
        if ($ReadId) { return [string](& $ReadId $Path) }
        if (-not $script:onWin) { $r = Get-DirIds @($Path) $false; if ($null -eq $r) { throw 'unprovable' }; return [string]$r[0] }
        Initialize-LdsNative
        $h = [LdsNative.Win]::CreateFileW((ConvertTo-NativePath $Path), 0x80, 7, [System.IntPtr]::Zero, 3, 0x02000000, [System.IntPtr]::Zero)  # FILE_READ_ATTRIBUTES only (a parent that may be traversed but not listed still opens); share all; OPEN_EXISTING; BACKUP_SEMANTICS (a directory)
        if ($h.IsInvalid) { throw 'cannot-open' }
        try {
            $b = [byte[]]::new(24)
            if ([LdsNative.Win]::GetFileInformationByHandleEx($h, 18, $b, 24)) { return 'x:' + [System.BitConverter]::ToString($b) }  # FileIdInfo: 64-bit volume serial + 128-bit file id
            $b = [byte[]]::new(52)
            if (-not [LdsNative.Win]::GetFileInformationByHandle($h, $b)) { throw 'cannot-read' }
            return 'b:' + [System.BitConverter]::ToString($b, 28, 4) + '-' + [System.BitConverter]::ToString($b, 44, 8)  # dwVolumeSerialNumber, nFileIndexHigh + nFileIndexLow
        } finally { $h.Dispose() }
    } catch { throw 'run-dir-identity-unprovable' }
}
function Get-DirIdList([string[]]$Paths, [scriptblock]$ReadId = $null) {  # the identities of $Paths, in order; throws when any cannot be read. POSIX asks for all of them in one stat call
    if ($ReadId -or $script:onWin) {
        $ids = @(); foreach ($p in $Paths) { $ids += Get-DirId $p $ReadId }
        return , $ids
    }
    $r = Get-DirIds $Paths $false
    if ($null -eq $r -or @($r).Count -ne $Paths.Count) { throw 'run-dir-identity-unprovable' }
    return , @($r)
}
function Test-Inside([string]$Parent, [string]$Child, [scriptblock]$ReadId = $null) {  # $true when $Child IS $Parent or is below it, decided by FILE IDENTITY along $Child's parent chain (r14, sol r13 B3), never by comparing path strings: GetRelativePath compares case-insensitively on Windows, so distinct case-sensitive siblings `wt` and `WT` read as one directory. Throws (run-dir-identity-unprovable) when an identity cannot be read: unknown is a refusal
    $chain = [System.Collections.Generic.List[string]]::new()
    for ($d = $Child; $d; $d = [System.IO.Path]::GetDirectoryName($d)) { $chain.Add($d) }  # no length cap (adversary r14 pass 1 A1: a cap at N levels dropped the worktree root from a deeper RunDir's chain, which then read as outside). GetDirectoryName strictly shortens the text, so the walk ends at the root
    $ids = Get-DirIdList (@($Parent) + $chain.ToArray()) $ReadId
    for ($i = 1; $i -lt $ids.Count; $i++) { if ([string]::Equals([string]$ids[$i], [string]$ids[0], [System.StringComparison]::Ordinal)) { return $true } }
    return $false
}
function Get-ShareOf([string]$Path) {  # Windows: the \\server\share a final path lives on, '' for a drive path. A share can expose a local folder (\\localhost\C$) and the kernel keeps the two spellings apart; only an identical \\server\share can be compared by name (r7 fix-2 A2)
    if ($Path.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) { return [System.IO.Path]::GetPathRoot($Path).ToLowerInvariant() }
    return ''
}
function Get-PrivateBase {  # where the saver's own temporary directory may live: the OS temp directory, unless that is not provably outside the worktree (the lane's TEMP can be inside it, on the hub VM it is), then the RunDir, which is already proven to be. The temp directory gets the SAME proofs the RunDir got, not a name compare alone: a bind mount (POSIX) or a loopback share (Windows) aliases a worktree folder under an outside-looking name (adversary r11 A1)
    foreach ($cand in @([System.IO.Path]::GetTempPath(), $RunDir)) {
        $f = $null; $accepted = $false  # r14 (adversary pass 2 A2): on Windows the accepted base stays OPEN (no share-delete) in $script:heldBase for the rest of the run, so it cannot be moved away and replaced by a junction into the worktree between this proof and the private directory's creation; a rejected candidate's handle is released at once
        try {
            if (-not [System.IO.Path]::IsPathFullyQualified($cand) -or (Test-HasDotDot $cand)) { continue }  # r13 (sol r12 F1, F2): the base is an OS-supplied path (TMPDIR may be relative on POSIX, or hold a link whose target holds `..`): it is used only when fully qualified and link-free, else the next candidate (the RunDir, already proven) is
            Assert-LinkFree $cand  # throws on a link or an unreadable component: this candidate is skipped
            if ($script:onWin) {
                $f = Get-FinalDir $cand; $real = $f.Path
                if (-not [string]::Equals((Get-ShareOf $real), (Get-ShareOf $realWork), [System.StringComparison]::Ordinal)) { continue }
            }
            else { $real = [System.IO.Path]::TrimEndingDirectorySeparator([System.IO.Path]::GetFullPath($cand)); if (-not [string]::Equals($cand, $RunDir, [System.StringComparison]::Ordinal)) { Assert-RunDirNotAlias $real $realWork } }  # throws when the temp directory is, or sits in, the worktree by identity, or when that cannot be proven: not used
            if (Test-Inside $realWork $real) { continue }
            if ($script:onWin) { $cand = $real }  # r15 (adversary A1): the proven directory is returned by the kernel's own path, never by the candidate's spelling. A subst drive or loopback share can be re-mapped to another folder after this proof, and the clean-up would then have deleted the OTHER folder's `lane-dirty-save-<guid>`; the real path (and the open handle that holds it) cannot be re-mapped
            if ($script:onWin -and $cand.StartsWith('\\?\', [System.StringComparison]::Ordinal)) {  # git rejects a \\?\ GIT_DIR, so it gets the plain spelling, and only when Win32 would not re-read it as another directory (a component ending in a dot or a space is trimmed)
                $cand = if ($cand.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) { '\\' + $cand.Substring(8) } else { $cand.Substring(4) }
                if (@($cand.Split('\') | Where-Object { $_ -and ($_.EndsWith([char]46) -or $_.EndsWith([char]32)) }).Count) { continue }
            }
            $accepted = $true; if ($f) { $script:heldBase = $f.Handle }
            return $cand
        } catch { continue }
        finally { if ($f -and -not $accepted) { $f.Handle.Dispose() } }
    }
    throw 'no-private-dir-outside-worktree'
}
function Assert-SameRepo([string]$Start, [string]$GitDir) {  # r14 (sol r13 B1): called with $WorkDir already lifted to the top level git named. The top level must be the start directory or an ancestor of it, and git asked again FROM the top level must name the same git dir and the same work tree, all by file identity, never by spelling. Anything else (or an answer that cannot be had) is worktree-redirect: the lift would have moved the run to another repository
    if (-not (Test-Inside $WorkDir $Start)) { throw 'worktree-redirect' }
    $a = Invoke-GitBytes @('rev-parse', '--path-format=absolute', '--absolute-git-dir'); $t = Invoke-GitBytes @('rev-parse', '--show-toplevel')
    if ($a.Code -ne 0 -or $t.Code -ne 0) { throw 'worktree-redirect' }
    $ids = Get-DirIdList @($GitDir, (Remove-GitTerminator $a.Bytes), $WorkDir, (Remove-GitTerminator $t.Bytes))
    if (-not ([string]::Equals([string]$ids[0], [string]$ids[1], [System.StringComparison]::Ordinal) -and [string]::Equals([string]$ids[2], [string]$ids[3], [System.StringComparison]::Ordinal))) { throw 'worktree-redirect' }
    $script:repoMeta = Get-RepoMeta $GitDir  # r15 (adversary A3): and the pointer file that decides WHICH repository a linked worktree's refs and objects come from
    $script:repoIds = @([string]$ids[0], [string]$ids[2])  # the identities every later step is measured against (Assert-RepoUnchanged)
}
function Get-RepoMeta([string]$GitDir) {  # r15 (adversary A3): the bytes of <git dir>/commondir, the file a linked worktree's git dir uses to name the repository whose refs and objects it reads. Rewriting it leaves the git dir's own file identity unchanged, so the identity alone could not see the swap. Absent (an ordinary repository) is a state of its own; any other read failure is thrown and ends the run in exit 30
    try { return 'commondir=' + [System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes([System.IO.Path]::Combine($GitDir, 'commondir'))) }
    catch [System.IO.FileNotFoundException], [System.IO.DirectoryNotFoundException] { return 'commondir=absent' }
}
function Assert-RepoUnchanged([string]$GitDir, [string]$Work, [string[]]$Want, [scriptblock]$ReadId = $null) {  # r14 (adversary pass 1 A2): the git dir and the work tree are read again at the END of collection and must still be the directories captured at the start. A path string is not an identity: a peer could have moved .git away and put another repository's git dir at the same path, and every read in between would then have come from the other repository (clean-tree/10 for the first one's dirt). Windows also HOLDS the git dir open for the whole run so the move is refused; POSIX has no such hold, so the swap is detected here and never reported as a clean tree
    $now = Get-DirIdList @($GitDir, $Work) $ReadId
    if ($null -ne $script:repoMeta -and -not [string]::Equals((Get-RepoMeta $GitDir), $script:repoMeta, [System.StringComparison]::Ordinal)) { throw 'repository-changed' }
    if (-not ([string]::Equals([string]$now[0], $Want[0], [System.StringComparison]::Ordinal) -and [string]::Equals([string]$now[1], $Want[1], [System.StringComparison]::Ordinal))) { throw 'repository-changed' }
}
function Assert-LinkFree([string]$Path, [scriptblock]$ReadAttr = $null) {  # r13 (sol r12 F2): NO LINK IS RESOLVED, so none can be resolved wrongly. Resolve-RealPath used to follow each symlink with ResolveLinkTarget().FullName, which removes a `..` held in the link's TARGET before the OS follows the link (`runlink -> hop/../evidence` with `hop -> wt/sub`: the OS opens wt/evidence, the saver judged evidence). Now every component below the root of a fully qualified path is read (attributes and link target, never followed) and ANY reparse point, symbolic link or junction ends in a refusal (exit 30 reason=link-in-path); a component whose attributes cannot be read ends in reason=path-unreadable. Paths this function is given have no `..` segment (Test-HasDotDot ran first), so their text is their identity once no component is a link. Windows reads the same \\?\ spelling Get-FinalDir will open. $ReadAttr is a test seam: a scriptblock that takes a component path and returns its FileAttributes (or throws)
    $p = ConvertTo-NativePath $Path; $root = [System.IO.Path]::GetPathRoot($p); $cur = $root
    foreach ($seg in $p.Substring($root.Length).Split([char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar), [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $cur = [System.IO.Path]::Combine($cur, $seg)
        try {
            if ($ReadAttr) { $attr = & $ReadAttr $cur; $target = $null }
            else { $attr = [System.IO.File]::GetAttributes($cur); $target = [System.IO.DirectoryInfo]::new($cur).LinkTarget }
        } catch { throw 'path-unreadable' }
        if (($attr -band [System.IO.FileAttributes]::ReparsePoint) -or $null -ne $target) { throw 'link-in-path' }
    }
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
function Initialize-LdsNative {  # Windows: the kernel32 entry points the path and identity reads share (compiled once per process)
    if (-not ('LdsNative.Win' -as [type])) {
        Add-Type -Namespace LdsNative -Name Win -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFileW(string name, uint access, uint share, System.IntPtr security, uint disposition, uint flags, System.IntPtr template);
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern uint GetFinalPathNameByHandleW(Microsoft.Win32.SafeHandles.SafeFileHandle handle, System.Text.StringBuilder buffer, uint length, uint flags);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetFileInformationByHandleEx(Microsoft.Win32.SafeHandles.SafeFileHandle handle, int infoClass, [System.Runtime.InteropServices.Out] byte[] buffer, uint length);
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetFileInformationByHandle(Microsoft.Win32.SafeHandles.SafeFileHandle handle, [System.Runtime.InteropServices.Out] byte[] buffer);
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern uint GetLongPathNameW(string shortPath, System.Text.StringBuilder buffer, uint length);
'@
    }
}
function Get-FinalDir([string]$Path) {  # Windows: open the directory and read its true path back, so links, junctions, subst drives, 8.3 and \\?\ spellings are all resolved by the kernel (r7 A1, A2). The handle stays open (no share-delete), so the directory and its parents cannot be renamed or replaced before the artifacts are written (r7 A3)
    Initialize-LdsNative
    $h = [LdsNative.Win]::CreateFileW((ConvertTo-NativePath $Path), 0x81, 3, [System.IntPtr]::Zero, 3, 0x02000000, [System.IntPtr]::Zero)  # LIST_DIRECTORY+READ_ATTRIBUTES (an attributes-only open is exempt from sharing checks, so it would hold nothing); share read+write, not delete; OPEN_EXISTING; BACKUP_SEMANTICS (a directory)
    if ($h.IsInvalid) { throw 'cannot-open-directory' }
    $sb = [System.Text.StringBuilder]::new(32768)
    $n = [LdsNative.Win]::GetFinalPathNameByHandleW($h, $sb, 32768, 0)
    if ($n -eq 0 -or $n -ge 32768) { $h.Dispose(); throw 'cannot-resolve-directory' }
    return @{ Path = $sb.ToString(); Handle = $h }  # the kernel's own spelling, \\?\C:\... or \\?\UNC\..., which Win32 and .NET then use without trimming
}
function ConvertTo-PlainPath([string]$P) {  # the \\?\ namespace prefix removed, so a kernel final path and a long path compare as text
    if ($P.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) { return '\\' + $P.Substring(8) }
    if ($P.StartsWith('\\?\', [System.StringComparison]::Ordinal)) { return $P.Substring(4) }
    return $P
}
function Assert-HandleAtPath($Handle, [string]$Path) {  # r16 (adversary A1, A3), r17 (sol r16 B1, S106): Windows. The kernel says where an open file or directory REALLY is (GetFinalPathNameByHandle resolves every link and junction on the way). TWO proofs, both must pass. (1) LINK: the handle's real path is the same NAME as the requested path's long spelling (GetLongPathName follows nothing, it only respells), so a handle reached THROUGH a link still in place is refused; GetLongPathName respells every component in its ON-DISK case, so a genuine request spelled in another case still matches, and the compare is Ordinal below the root (r17, adversary A1: a link that STAYS in place makes both handles agree on the decoy, and a folded name compare then accepted `records` for `RECORDS`); only the root (drive letter, server and share) is compared without case, because GetLongPathName keeps the root as the caller spelled it. (2) CASE (r17): the REQUESTED path is opened too, here and now, and the kernel's final path of THAT handle must equal the object's final path Ordinal, both sides the kernel's own spelling: on a case-sensitive directory `records` and `RECORDS` are two directories, and the folded compare alone accepted a decoy opened on `RECORDS` for the requested `records`. Nothing below the root is accepted on a folded compare. A request that cannot be opened or a path that cannot be read is a refusal
    Initialize-LdsNative
    $fin = [System.Text.StringBuilder]::new(32768); $lng = [System.Text.StringBuilder]::new(32768); $req = [System.Text.StringBuilder]::new(32768)
    $n = [LdsNative.Win]::GetFinalPathNameByHandleW($Handle, $fin, 32768, 0)
    if ($n -eq 0 -or $n -ge 32768) { throw 'cannot-resolve-handle' }
    $m = [LdsNative.Win]::GetLongPathNameW((ConvertTo-NativePath $Path), $lng, 32768)
    if ($m -eq 0 -or $m -ge 32768) { throw 'cannot-resolve-path' }
    $pa = ConvertTo-PlainPath $fin.ToString(); $pb = ConvertTo-PlainPath $lng.ToString(); $ra = [System.IO.Path]::GetPathRoot($pa); $rb = [System.IO.Path]::GetPathRoot($pb)
    if (-not ([string]::Equals($ra, $rb, [System.StringComparison]::OrdinalIgnoreCase) -and [string]::Equals($pa.Substring($ra.Length), $pb.Substring($rb.Length), [System.StringComparison]::Ordinal))) { throw 'opened-through-a-link' }
    $want = [LdsNative.Win]::CreateFileW((ConvertTo-NativePath $Path), 0x80, 7, [System.IntPtr]::Zero, 3, 0x02000000, [System.IntPtr]::Zero)  # READ_ATTRIBUTES only (exempt from sharing checks, so it conflicts with no handle held on the same object); share read+write+delete; OPEN_EXISTING; BACKUP_SEMANTICS (a directory or a file)
    if ($want.IsInvalid) { throw 'cannot-resolve-path' }
    try {
        $k = [LdsNative.Win]::GetFinalPathNameByHandleW($want, $req, 32768, 0)
        if ($k -eq 0 -or $k -ge 32768) { throw 'cannot-resolve-path' }
    } finally { $want.Dispose() }
    if (-not [string]::Equals((ConvertTo-PlainPath $fin.ToString()), (ConvertTo-PlainPath $req.ToString()), [System.StringComparison]::Ordinal)) { throw 'opened-through-a-link' }
}
function Write-NewFile([string]$Path, [byte[]]$Bytes) {  # r16 (adversary A1): a private file is only ever CREATED; one that already exists (a worktree file reached through a swapped link included) is an error, never truncated
    $fs = [System.IO.File]::Open($Path, 'CreateNew', 'Write', 'None')
    try { $fs.Write($Bytes, 0, $Bytes.Length) } finally { $fs.Dispose() }
}
function Test-LaneAlive {
    if ($LanePid -and $ChildJson) { throw 'mixed-selectors' }  # one pid with another lane's createdUtc would judge a live lane reused (dot B1 on #722)
    $procId = $LanePid; $born = $null
    if ($ChildJson) {
        # System.Text.Json, not ConvertFrom-Json: PowerShell turns '/Date(0)/' into a DateTime, unwraps one-item lists and casts arrays to int, so every shape check was passed by something that is no ISO string (adversary r8 A2, A3)
        $cfs = [System.IO.File]::Open($ChildJson, 'Open', 'Read', 'Read')  # r15 (adversary A4): opened FIRST, then proved link-free again with the file held, then read from that handle: a folder swapped for a link after the first check (line "foreach ($raw in") cannot make the saver read another file by the same name
        if ($script:onWin) { try { Assert-HandleAtPath $cfs.SafeFileHandle $ChildJson } catch { $cfs.Dispose(); throw } }  # r16 (adversary A3): the open file must really BE at the requested path, by the kernel's answer; a folder that was a link when it was opened is refused whatever the name holds afterwards
        try { Assert-LinkFree $ChildJson; $cjson = [System.IO.StreamReader]::new($cfs).ReadToEnd() } finally { $cfs.Dispose() }
        if (-not [string]::Equals([System.IO.File]::ReadAllText($ChildJson), $cjson, [System.StringComparison]::Ordinal)) { throw 'child-record-changed' }  # r15 (adversary pass 2 A3): a link that was in place when the file was opened and removed before the second proof left the saver holding the decoy; the name now holds another file, so the two reads disagree and the run is refused
        $root = [System.Text.Json.JsonDocument]::Parse($cjson).RootElement
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
function Save-Bytes([string]$Ext, [byte[]]$Bytes) {  # r16 (sol r15 B2, hub ruling b): an artifact is only ever CREATED. Every name is tried with CreateNew; a name that is taken (a file, a directory, a link, anything) is skipped for the next suffix and is never opened, read or compared, so a link swapped in at a name cannot be followed and its bytes cannot be reported as evidence that lives outside the worktree. The receipt names the file this run wrote
    for ($n = 1; ; $n++) {
        $script:written = $Bytes
        $name = if ($n -eq 1) { "dirty-save-$head8.$Ext" } else { "dirty-save-$head8.$n.$Ext" }
        $f = [System.IO.Path]::Combine($RunDir, $name)
        try { $fs = [System.IO.File]::Open($f, 'CreateNew', 'Write', 'None') }
        catch [System.IO.IOException], [System.UnauthorizedAccessException] {  # a directory at the name raises UnauthorizedAccess (sol r4 B3)
            if (-not ([System.IO.File]::Exists($f) -or [System.IO.Directory]::Exists($f))) { throw }  # nothing is at the name: a real write failure (read-only or full RunDir), not a collision
            continue
        }
        try { $fs.Write($Bytes, 0, $Bytes.Length) } finally { $fs.Dispose() }
        return $name
    }
}
try {
    foreach ($v in 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR') { if ($null -ne [System.Environment]::GetEnvironmentVariable($v)) { throw 'worktree-redirect' } }  # r14 (sol r13 B1): the caller names the repository from outside; the saver's child processes never see these (they are removed), so what the saver then judged and what git opened could differ. Refused, never stripped silently
    foreach ($raw in $WorkDir, $RunDir, $ChildJson) { if ($raw -and $raw.Contains([char]0xFFFD)) { throw 'lossy-argument' } }  # r14 (adversary pass 2 A3): the host decodes a POSIX argument's bytes as UTF-8 BEFORE this script runs and turns every invalid byte into U+FFFD, which is itself a valid character naming a DIFFERENT sibling directory (`wt\xff` read as `wt` + U+FFFD). The saver cannot tell the two apart afterwards, so it refuses the character (the argument may have been lossy) instead of collecting a sibling's repository
    foreach ($raw in $WorkDir, $RunDir, $ChildJson) { if ($raw -and (Test-HasDotDot $raw)) { throw 'dotdot-path' } }  # before any normalisation or read (r12 sol r11 B1); exit 30 with a reason of its own
    foreach ($raw in $WorkDir, $RunDir, $ChildJson) { if ($raw -and -not [System.IO.Path]::IsPathFullyQualified($raw)) { throw 'not-fully-qualified' } }  # r13 (sol r12 F1): on the RAW text, before any normalisation or read. `C:..\wt` has no `..` segment (its first segment is `C:..`) yet GetFullPath resolves it against the drive's current directory; a relative or root-relative path names whatever the current directory is. The old relative-path support (resolved against PowerShell's location) is gone with it: nothing is resolved against a current directory any more
    $WorkDir = [System.IO.Path]::GetFullPath($WorkDir); $RunDir = [System.IO.Path]::GetFullPath($RunDir)  # .NET only: PowerShell's path layer may re-spell a POSIX backslash as '/' (dot B1 on #722). Fully qualified and without `..`, so this only folds `.` segments and repeated separators
    if ($ChildJson) { $ChildJson = [System.IO.Path]::GetFullPath($ChildJson) }
    if (-not [System.IO.Directory]::Exists($WorkDir) -or -not [System.IO.Directory]::Exists($RunDir)) { throw 'no-such-directory' }
    foreach ($raw in $WorkDir, $RunDir, $ChildJson) { if ($raw) { Assert-LinkFree $raw } }  # r13 (sol r12 F2): before the record is read and before any write; the child record's own file name is checked too (a symlinked child.json is another file)
    if (Test-LaneAlive) { Write-Output 'DIRTY_SAVE_REFUSED reason=lane-alive'; exit 20 }
} catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)"; exit 30 }
$flags = @('--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--no-relative', '--submodule=short', '--ignore-submodules=dirty', '--unified=3', '--src-prefix=a/', '--dst-prefix=b/')
$top = Invoke-GitBytes @('rev-parse', '--show-toplevel')
$startDir = $WorkDir; $gitDir = $null
if ($top.Code -eq 0) {
    # r14 (sol r13 B1): ONE repository identity for the whole run. Asked from the directory the caller named, BEFORE the top level is adopted: the absolute git dir, and the effective config (`config --list` sees every scope: stored config, -c, GIT_CONFIG_*, includes). A core.worktree anywhere means the work tree is another directory than the one git was started in, and top-level lifting would adopt it (a dirty repository A naming clean B as its work tree was reported clean-tree/10). Refused, typed, before any collection
    $gdR = Invoke-GitBytes @('rev-parse', '--path-format=absolute', '--absolute-git-dir'); $cfgR = Invoke-GitBytes @('config', '--null', '--list') -KeepConfigEnv
    $failed = @($gdR, $cfgR) | Where-Object { $_.Code -ne 0 } | Select-Object -First 1
    if ($failed) { Write-Output "DIRTY_SAVE_GIT_FAILED reason=$($failed.Err.Trim())"; exit 40 }  # a git dir or config git cannot give (an injection it cannot parse included, r11 hub ruling B1) is the existing typed git failure, before anything is collected
    $redirect = $false
    foreach ($e in ([System.Text.Encoding]::Latin1.GetString($cfgR.Bytes) -split "`0" | Where-Object { $_ })) { $i = $e.IndexOf([char]10); if ([string]::Equals($(if ($i -ge 0) { $e.Substring(0, $i) } else { $e }), 'core.worktree', [System.StringComparison]::OrdinalIgnoreCase)) { $redirect = $true } }
    if ($redirect) { Write-Output 'DIRTY_SAVE_BAD_ARGS reason=worktree-redirect'; exit 30 }
    try { $WorkDir = Remove-GitTerminator $top.Bytes } catch { Write-Output 'DIRTY_SAVE_BAD_ARGS reason=toplevel-not-utf8'; exit 30 }
    try { $gitDir = Remove-GitTerminator $gdR.Bytes } catch { Write-Output 'DIRTY_SAVE_BAD_ARGS reason=gitdir-not-utf8'; exit 30 }
}
try {
    if ($top.Code -eq 0) { if (Test-HasDotDot $WorkDir) { throw 'dotdot-path' }; Assert-LinkFree $WorkDir; Assert-SameRepo $startDir $gitDir; $script:pin = @(('--git-dir=' + $gitDir), ('--work-tree=' + $WorkDir)); if ($script:onWin) { $script:heldGit = Get-FinalDir $gitDir } }  # the captured git dir is held open (no share-delete) like the RunDir and work tree: it cannot be renamed away and replaced for the rest of the run  # r13: the top level is what git printed (forward slashes on Windows); it is judged like the argument was, not trusted. r14: and it must be the SAME repository by identity; from here every git call is handed the captured git dir and work tree
    if ($script:onWin) {
        $script:held = @((Get-FinalDir $RunDir), (Get-FinalDir $WorkDir)); $RunDir = $script:held[0].Path; $realWork = $script:held[1].Path
        if (-not [string]::Equals((Get-ShareOf $RunDir), (Get-ShareOf $realWork), [System.StringComparison]::Ordinal)) { throw 'run-dir-namespace-unprovable' }  # see Get-ShareOf (r7 fix-2 A2)
    }
    else { $RunDir = [System.IO.Path]::TrimEndingDirectorySeparator($RunDir); $realWork = [System.IO.Path]::TrimEndingDirectorySeparator($WorkDir); if ($top.Code -eq 0) { Assert-RunDirNotAlias $RunDir $realWork } }  # no link is in either chain (Assert-LinkFree), so the text is the real path
    $runInside = ($top.Code -eq 0) -and (Test-Inside $realWork $RunDir)  # r14 (sol r13 B3): by file identity along the RunDir's parent chain, not by comparing path strings; an identity that cannot be read is exit 30 (run-dir-identity-unprovable). With no repository there is nothing to contain and git ends the run with 40
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
        if ($git.untracked.Code -eq 0) { $git.untracked.Bytes = Remove-AliasedNames $git.untracked.Bytes $WorkDir $script:sealDir }  # r14 (sol r13 B2): the private directory is proven outside the worktree (Get-PrivateBase), so no name is dropped by its spelling; only names below a directory that resolves INTO it are
    }
}
catch { $git.seal = @{ Code = -1; Err = "private-git-dir: $($_.Exception.Message)" } }
# r16 (sol r15 B1, hub ruling a): the saver removes NOTHING. A removal by name that follows an ownership check can act on a directory that replaced the checked one (no check is atomic with it), and a failed identity capture made it worse (it removed with no proof at all). The private directory is left where this run made it, and is NAMED in the output and the receipt as privateGitDirLeft, never swallowed (adversary r10 A4).
$left = if ($script:sealDir -and (Test-DirPresent $script:sealDir)) { $script:sealDir } else { $null }
$leftUnverified = $false  # r16 (adversary A2): the leftover is named as the directory this run made only while its name still holds that directory (file identity); a name that now holds another directory, or an identity that cannot be read, is reported as unverified, never as a plain match
if ($left) { try { $leftUnverified = -not ($script:sealId -and [string]::Equals((Get-DirId $left), $script:sealId, [System.StringComparison]::Ordinal)) } catch { $leftUnverified = $true } }
$leftNote = if ($left) { " privateGitDirLeft=$left" + $(if ($leftUnverified) { ' privateGitDirLeftIdentity=unverified' } else { '' }) } else { '' }
if ($script:repoChanged) { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($script:repoChanged)$leftNote"; exit 30 }  # r15 (sol r14 B1): a collection that could have read another repository is never a clean tree (10), a git failure (40) or a save: it fails closed, typed, and writes nothing
$bad = $git.Values | Where-Object { $_.Code -ne 0 } | Select-Object -First 1
if ($bad) { Write-Output "DIRTY_SAVE_GIT_FAILED reason=$($bad.Err.Trim())$leftNote"; exit 40 }
if ($gitDir) { try { Assert-RepoUnchanged $gitDir $WorkDir $script:repoIds } catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)$leftNote"; exit 30 } }  # r14 (adversary pass 1 A2): everything read above came from the repository captured at the start, or the run says it did not (reason=repository-changed) and writes nothing
$todo = 'patch', 'staged', 'untracked' | Where-Object { $git[$_].Bytes.Length -gt 0 }
# r11 (sol r10 B2): exit 0 with words on stderr is not a complete collection (git walks past a directory it cannot open and says so only there). Line-ending conversion notices are the one exception: git printed them for a file it DID compare.
$warnLines = @(Get-WarningLines $script:gitStderr) + @($script:probeWarnings)  # r13 (sol r12 F3): an identity probe that could not be read is a warning too
$warnShown = @($warnLines | Select-Object -First 20 | ForEach-Object { if ($_.Length -gt 300) { $_.Substring(0, 300) + '...' } else { $_ } })
if ($warnLines.Count -gt 20) { $warnShown += "... and $($warnLines.Count - 20) more" }
if (-not $todo) {
    if ($warnLines) { Write-Output "DIRTY_SAVE_INCOMPLETE reason=git-warning count=$($warnLines.Count) warnings=$($warnShown -join ' | ')$leftNote"; exit 60 }
    Write-Output "DIRTY_SAVE_NOTHING reason=clean-tree$leftNote"; exit 10
}
$head8 = $headFull.Substring(0, 8)
$ext = @{ patch = 'patch'; staged = 'staged.patch'; untracked = 'untracked.txt' }
$r = [ordered]@{ schema = 'lane-dirty-save/v1'; head = $headFull; untrackedFormat = 'nul-separated'; untrackedContents = 'not-saved'; submodules = "skipped $subs" }
if ($left) { $r.privateGitDirLeft = $left; if ($leftUnverified) { $r.privateGitDirLeftIdentity = 'unverified' } }
if ($warnLines) { $r.collection = 'incomplete'; $r.warnings = @($warnShown) }
try {
    foreach ($k in $todo) { $r[$k] = Save-Bytes $ext[$k] $git[$k].Bytes }
    if ($r.Contains('patch')) { $r.patchSha256 = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData($git.patch.Bytes)).Replace('-', '').ToLowerInvariant() }
    [void](Save-Bytes 'receipt.json' ([System.Text.UTF8Encoding]::new($false).GetBytes(($r | ConvertTo-Json -Compress) + "`n")))
} catch { Write-Output "DIRTY_SAVE_WRITE_FAILED reason=$($_.Exception.Message)$leftNote"; exit 50 }  # r6 A4: a read-only or full RunDir ends in a documented status; artifacts already written stay
Write-Output ([System.Text.UTF8Encoding]::new($false).GetString($script:written).TrimEnd("`n"))
exit $(if ($warnLines) { 60 } else { 0 })