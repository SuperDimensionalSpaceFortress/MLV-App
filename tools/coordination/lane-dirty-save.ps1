# lane-dirty-save.ps1 - LANE-HUBCHAT-DETACHED-DISPATCH-1.
# A lane that dies mid-edit (2026-10-26 09:09Z: the hub chat's tool tree ended with the Desktop app) leaves tracked changes in its worktree, and a dispatcher that then
# refuses a dirty tree spends every retry on work nobody owns. Save-DirtyWorktreeEvidence copies that work into the refused run dir BEFORE the refusal, so it survives:
#   dirty-worktree-<sha8>.patch        git diff HEAD --binary (tracked changes only; sha8 = first 8 hex of the patch SHA-256)
#   dirty-worktree-<sha8>.status.txt   git status --porcelain (names untracked files too; their content is NOT in the patch); a later save with a different
#                                      untracked set for the same patch is kept beside it as dirty-worktree-<sha8>.status-<status sha8>.txt
#   dirty-worktree-staged-<sha8>.diff  git diff --cached --binary, only when something is staged (covers staged work the working tree no longer has)
#   dirty-worktree-merge-state-<sha8>.txt  MERGE_HEAD / CHERRY_PICK_HEAD / REVERT_HEAD and `git ls-files -u`, only when the index is unmerged (see below)
# It RETURNS one line (the caller prints it): DIRTY_TREE_SAVED ... staged=<path|none> verified=reverse-apply-ok|empty-diff|reverse-apply-FAILED reapply=<command>, or DIRTY_TREE_SAVE_FAILED reason=<why>.
# A staged diff that cannot be written whole (git exit, empty or truncated file, no `git apply --cached --check -R` against the index) is DIRTY_TREE_SAVE_FAILED, never SAVED; no evidence name is ever overwritten.
# An unmerged index (a lane that died mid `git merge`) has no staged diff to write: `git diff --cached` prints only '* Unmerged path <file>' and exits 0. That state is recorded in the merge-state file and the
# line is DIRTY_TREE_SAVE_FAILED reason=unmerged-index. Every DIRTY_TREE_SAVE_FAILED that comes after evidence was placed ends with the files already there: patch=<path> status=<path> merge-state=<path>.
# The helper judges every git exit code itself and sets $PSNativeCommandUseErrorActionPreference = $false for its own calls, so the expected exit 1 of `git diff --cached --quiet` cannot throw under a caller that set it.
# It refuses a -Dest inside the worktree it describes (reason=dest-inside-worktree): evidence written there would show in the next status and be captured by the next save.
# -Dest is resolved once to its physical path and every write uses that path; a destination that changed identity before a write or publish is reason=dest-identity-changed.
# Read-only on the worktree (nothing is restored, so the caller's refusal and exit code are unchanged), idempotent (same diff = same name), fail-soft (never throws), and it
# refuses a directory that is not itself a worktree root (that would resolve to an ENCLOSING repo and save its diff). The same logic is deployed board-local in
# hub-autopilot\dispatch-fix-round.ps1; keep the two in step.
# Moves $Tmp to <Dir>\<Stem><first 8|16|32|64 hex of $Full><Ext> and returns @{ Path; Id }, or $null when every length is taken by a DIFFERENT file.
# The move fails rather than overwrites, so an occupied name is compared by hash (same bytes = reuse, anything else = lengthen the name) and never clobbered.
# When the name already holds these exact bytes, $Tmp is left for the caller's cleanup. A non-empty $FirstName is tried before the hash-derived names.
# It always returns a hashtable: Path is the placed (or reused) file, or $null with Reason = name-collision (every length is taken by a DIFFERENT file) or
# move-failed:<HResult> <message> (the move threw an IOException and the target does not exist: a sharing violation or an AV lock on $Tmp, not a name clash).
function Invoke-DirtyFileMove([string]$From, [string]$To) { [System.IO.File]::Move($From, $To) }  # one seam, so a test can stand in for a locked file

function Move-DirtyEvidenceToFreeName([string]$Tmp, [string]$Dir, [string]$Stem, [string]$Ext, [string]$Full, [string]$FirstName = '', [scriptblock]$Guard = $null) {
    $names = @()
    if ($FirstName) { $names += , @($FirstName, '') }
    foreach ($n in 8, 16, 32, 64) { $names += , @("$Stem$($Full.Substring(0, $n))$Ext", $Full.Substring(0, $n)) }
    foreach ($pair in $names) {
        $cand = Join-Path $Dir $pair[0]
        if ($Guard) { $why = & $Guard 'before-publish'; if ($why) { return @{ Path = $null; Reason = $why } } }  # the destination is re-proven right before each rename into place
        try { Invoke-DirtyFileMove $Tmp $cand; return @{ Path = $cand; Id = $pair[1]; Moved = $true } }
        catch [System.IO.IOException] {
            $ex = $_.Exception
            while ($ex.InnerException -and $ex -isnot [System.IO.IOException]) { $ex = $ex.InnerException }  # a method call wraps the real IOException
            if (-not (Test-Path -LiteralPath $cand)) { return @{ Path = $null; Reason = ('move-failed:0x{0:X8} {1}' -f $ex.HResult, ($ex.Message -replace '\s+', ' ')) } }
            if ((Test-Path -LiteralPath $cand -PathType Leaf) -and (Get-FileHash -LiteralPath $cand -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Full) { return @{ Path = $cand; Id = $pair[1]; Moved = $false } }
        }
    }
    return @{ Path = $null; Reason = 'name-collision' }
}

# The canonical spelling of an existing path: every component in the case and long-name form the file system stores it under (an 8.3 alias or a different case
# would defeat a prefix comparison), and every directory link or junction on the way replaced by its target. Used only to decide whether two paths overlap.
# On Windows the kernel answers first (GetFinalPathNameByHandle), because only it sees through a SUBST drive, which no managed API reports; the walk below is the fallback.
function Get-DirtyKernelPath([string]$Path) {
    if (-not ($IsWindows -or $env:OS -eq 'Windows_NT')) { return $null }
    try {
        if (-not ('DirtyFinalPath' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class DirtyFinalPath {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFinalPathNameByHandleW(IntPtr handle, StringBuilder buffer, uint length, uint flags);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [StructLayout(LayoutKind.Sequential)]
    struct ByHandleInfo { public uint Attributes, CtLo, CtHi, AtLo, AtHi, WtLo, WtHi, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow; }
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetFileInformationByHandle(IntPtr handle, out ByHandleInfo info);
    // Volume serial + file index of what the path opens onto after every link is followed: two names for the same directory share it, a retargeted junction does not.
    public static string Identity(string path) {
        IntPtr h = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
        if (h == (IntPtr)(-1)) return null;
        try {
            ByHandleInfo i;
            if (!GetFileInformationByHandle(h, out i)) return null;
            return i.Volume.ToString("X8") + ":" + i.IndexHigh.ToString("X8") + i.IndexLow.ToString("X8");
        } finally { CloseHandle(h); }
    }
    // No access rights, full sharing, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS (a directory can be opened).
    public static string Resolve(string path) {
        IntPtr h = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
        if (h == (IntPtr)(-1)) return null;
        try {
            StringBuilder sb = new StringBuilder(2048);
            uint n = GetFinalPathNameByHandleW(h, sb, 2048, 0);
            return (n == 0 || n >= 2048) ? null : sb.ToString();
        } finally { CloseHandle(h); }
    }
}
'@
        }
        $final = [DirtyFinalPath]::Resolve($Path)
        if (-not $final) { return $null }
        if ($final.StartsWith('\\?\UNC\')) { return '\\' + $final.Substring(8) }
        if ($final.StartsWith('\\?\')) { return $final.Substring(4) }
        return $final
    } catch { return $null }
}

function Resolve-DirtyRealPath([string]$Path) {
    $kernel = Get-DirtyKernelPath ([System.IO.Path]::GetFullPath($Path))
    if ($kernel) { return $kernel.TrimEnd([char[]]@('\', '/')) }
    $cur = [System.IO.Path]::GetFullPath($Path)
    for ($round = 0; $round -lt 32; $round++) {
        $root = [System.IO.Path]::GetPathRoot($cur)
        $parts = @($cur.Substring($root.Length).Split([char[]]@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries))
        $acc = $root; $relinked = $false
        for ($i = 0; $i -lt $parts.Count; $i++) {
            $hit = @([System.IO.Directory]::GetFileSystemEntries($acc, $parts[$i]))
            $next = if ($hit.Count -eq 1) { $hit[0] } else { Join-Path $acc $parts[$i] }
            $info = [System.IO.DirectoryInfo]$next
            if ($info.LinkTarget) {
                $final = $info.ResolveLinkTarget($true)
                $tail = if ($i + 1 -lt $parts.Count) { $parts[($i + 1)..($parts.Count - 1)] } else { @() }
                $cur = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($final.FullName, ($tail -join [System.IO.Path]::DirectorySeparatorChar)))
                $relinked = $true; break
            }
            $acc = $next
        }
        if (-not $relinked) { return $acc.TrimEnd([char[]]@('\', '/')) }
    }
    return $cur.TrimEnd([char[]]@('\', '/'))
}

# Volume serial + file index of the directory a path opens onto (every link followed); where the kernel cannot answer, the resolved path stands in for it.
function Get-DirtyDirIdentity([string]$Path) {
    if ($IsWindows -or $env:OS -eq 'Windows_NT') {
        try { if ('DirtyFinalPath' -as [type]) { return [DirtyFinalPath]::Identity($Path) } } catch { return $null }
    }
    return 'path:' + (Resolve-DirtyRealPath $Path)
}

# One seam, so a test can retarget or swap the destination at a named point; $Stage is 'before-write' or 'before-publish'.
function Invoke-DirtyDestCheckpoint([string]$Stage) { }

# The destination was resolved once, to its physical path, and every write goes to that literal path. Before each write and each publish the directory behind that path
# must still be the same directory (same identity, same resolved path) and still outside the worktree; returns '' when it is, otherwise the typed reason.
function Test-DirtyDestStable([string]$Dest, [string]$Identity, [string]$WtReal, [string]$Stage) {
    $null = Invoke-DirtyDestCheckpoint $Stage
    if ((Get-DirtyDirIdentity $Dest) -cne $Identity) { return 'dest-identity-changed' }
    $now = Resolve-DirtyRealPath $Dest
    if ($now -ine $Dest) { return 'dest-identity-changed' }
    if (Test-DirtyPathInside -Inner $now -Outer $WtReal) { return 'dest-inside-worktree' }
    return ''
}

# A PowerShell literal that survives pasting: inside double quotes a path such as C:\work\$lane would expand a variable; single quotes expand nothing, and the quote characters
# PowerShell accepts (the ASCII one and the four typographic ones) are doubled.
function ConvertTo-DirtySingleQuoted([string]$Text) {
    $quotes = "['" + [char]0x2018 + [char]0x2019 + [char]0x201A + [char]0x201B + ']'
    return "'" + [regex]::Replace($Text, $quotes, '$0$0') + "'"
}

function Test-DirtyPathInside([string]$Inner, [string]$Outer) {
    $cmp = if ($IsWindows -or $env:OS -eq 'Windows_NT') { [System.StringComparison]::OrdinalIgnoreCase } else { [System.StringComparison]::Ordinal }
    $sep = [System.IO.Path]::DirectorySeparatorChar
    return ($Inner + $sep).StartsWith($Outer + $sep, $cmp)
}

# True only when the saved file is byte-identical to what git writes to a pipe for the same diff (GitDiffArgs = the arguments after `git -C <Wt>`, without --output).
# A cut at a file boundary, a hunk boundary or just before `\ No newline at end of file` is still a well-formed patch that keeps every line count and reverse-applies, and a
# full volume can make git write less than it was given without a non-zero exit; only a byte-exact comparison against git's own stream sees every such loss.
function Test-DirtyPatchComplete([string]$Wt, [string]$Patch, [string[]]$GitDiffArgs) {
    if (-not (Test-Path -LiteralPath $Patch)) { return $false }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = @(Get-Command git -CommandType Application)[0].Source
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.CreateNoWindow = $true
    foreach ($a in (@('-C', $Wt) + $GitDiffArgs)) { [void]$psi.ArgumentList.Add($a) }
    $p = [System.Diagnostics.Process]::Start($psi)
    try {
        $null = $p.StandardError.ReadToEndAsync()  # drained so a chatty git cannot block on a full stderr pipe
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $live = ([BitConverter]::ToString($sha.ComputeHash($p.StandardOutput.BaseStream)) -replace '-', '').ToLowerInvariant()
        $p.WaitForExit()
        if ($p.ExitCode -ne 0) { return $false }
    } finally { $p.Dispose() }
    return ($live -ceq (Get-FileHash -LiteralPath $Patch -Algorithm SHA256).Hash.ToLowerInvariant())
}

function Save-DirtyWorktreeEvidence([string]$Wt, [string]$Dest, [string]$CardName = '', [string]$RoundName = '') {
    $tmp = $null; $stagedTmp = $null; $statTmp = $null; $mergeTmp = $null
    $PSNativeCommandUseErrorActionPreference = $false  # local to this call: every git exit code below is judged here, never thrown (git diff --cached --quiet exits 1 when something is staged)
    $evidence = ''  # ' patch=.. status=..' for the files placed so far; every failure after the first placement ends with it
    $CardName = $CardName -replace '\s+', ' '; $RoundName = $RoundName -replace '\s+', ' '  # the result is one line
    # -C $Wt names the repository, so a GIT_DIR / GIT_INDEX_FILE / GIT_DIFF_OPTS inherited from the caller is cleared for the call and put back afterwards.
    # These variables change which repository git reads or the shape of the diff it writes; GIT_DIFF_OPTS=--unified=0, for one, beats the explicit -U3.
    $gitEnv = 'GIT_DIFF_OPTS', 'GIT_EXTERNAL_DIFF', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX'
    $savedEnv = @{}
    foreach ($v in $gitEnv) { if (Test-Path -LiteralPath "Env:$v") { $savedEnv[$v] = (Get-Item -LiteralPath "Env:$v").Value; Remove-Item -LiteralPath "Env:$v" } }  # removed, not emptied: git reads an empty GIT_DIR as a path
    try {
        if (-not (Test-Path -LiteralPath $Dest -PathType Container)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=no-run-dir" }
        $Dest = (Resolve-Path -LiteralPath $Dest).ProviderPath  # git -C $Wt would resolve a relative --output against the worktree, not the caller
        # A relative -Wt means the caller's PowerShell location; git -C and [IO.Path]::GetFullPath would read it against the process directory, which can differ (r4 adversary A1).
        if (-not (Test-Path -LiteralPath $Wt -PathType Container)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        $Wt = (Resolve-Path -LiteralPath $Wt).ProviderPath
        $prefix = @(& git -C $Wt rev-parse --show-prefix 2>$null)
        if ($LASTEXITCODE -ne 0 -or ($prefix -join '').Trim() -ne '') { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        # -Dest is resolved ONCE, to its physical path (every link and junction followed), and containment is judged on that path. Every write below goes to that literal path and never
        # reopens the -Dest spelling, so a junction retargeted after this point cannot steer evidence into the worktree; $guard also re-proves the directory (same volume serial + file
        # index, same resolved path, still outside the worktree) right before each write and each rename into place, and a change is DIRTY_TREE_SAVE_FAILED reason=dest-identity-changed.
        $destReal = Resolve-DirtyRealPath $Dest
        $wtReal = Resolve-DirtyRealPath $Wt
        if (Test-DirtyPathInside -Inner $destReal -Outer $wtReal) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-inside-worktree" }
        $destId = Get-DirtyDirIdentity $destReal
        if (-not $destId) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-identity-changed" }
        $Dest = if ($destReal.Length -eq 2 -and $destReal[1] -eq ':') { $destReal + '\' } else { $destReal }  # a drive root keeps its separator, or Join-Path would mean the drive's current directory
        $guard = { param($stage) Test-DirtyDestStable -Dest $destReal -Identity $destId -WtReal $wtReal -Stage $stage }
        $why = & $guard 'before-write'
        if ($why) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$why" }
        $tmp = Join-Path $Dest ('dirty-worktree-saving-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        # The diff shape is pinned on every diff the saver runs, so no user, global or repo config can change what `git apply` (default -p1) has to read back:
        # a/ b/ prefixes (diff.noprefix / diff.mnemonicPrefix), no external driver or textconv (diff.external), -U3 (diff.context=0 and diff.interHunkContext would
        # emit a patch plain git apply rejects), no color (color.ui=always), short submodule lines (diff.submodule=log), submodules never ignored
        # (diff.ignoreSubmodules=all would hide a gitlink change) and no rename detection (diff.renames).
        $shape = @('--binary', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames', '--submodule=short', '--ignore-submodules=none', '-U3', '--src-prefix=a/', '--dst-prefix=b/')
        & git -C $Wt diff HEAD @shape "--output=$tmp" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $tmp)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-diff-exit-$LASTEXITCODE" }
        if (-not (Test-DirtyPatchComplete -Wt $Wt -Patch $tmp -GitDiffArgs (@('diff', 'HEAD') + $shape))) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=patch-incomplete" }
        $full = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
        # The name is the first 8 hex of the hash, lengthened until it is free or already holds these exact bytes (two different diffs may share a prefix).
        $bytes = (Get-Item -LiteralPath $tmp).Length
        $placed = Move-DirtyEvidenceToFreeName -Tmp $tmp -Dir $Dest -Stem 'dirty-worktree-' -Ext '.patch' -Full $full -Guard $guard
        if (-not $placed.Path) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($placed.Reason)" }
        if ($placed.Moved) { $tmp = $null }
        $id = $placed.Id
        $patch = $placed.Path
        $evidence = " patch=$patch"
        # --untracked-files=all: a status.showUntrackedFiles=no config must not hide the untracked names this file exists to keep.
        # --ignore-submodules=none: diff.ignoreSubmodules=all / submodule.<name>.ignore=all would hide a submodule checked out at another commit, which the patch (pinned the same way) records.
        $status = @(& git -C $Wt -c core.quotepath=false status --porcelain --untracked-files=all --ignore-submodules=none 2>$null)
        $statusExit = $LASTEXITCODE
        if ($statusExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-status-exit-$statusExit$evidence" }
        # The same tracked diff can come back with a different untracked set, so the status file is placed like every other evidence file: <patch name>.status.txt when
        # free or identical, otherwise a hash-named sibling. A later save never rewrites an earlier snapshot.
        $why = & $guard 'before-write'
        if ($why) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$why$evidence" }
        $statTmp = Join-Path $Dest ('dirty-worktree-status-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        [System.IO.File]::WriteAllLines($statTmp, [string[]]$status, (New-Object System.Text.UTF8Encoding($false)))  # UTF-8: quotepath=false prints literal non-ASCII names
        $statFull = (Get-FileHash -LiteralPath $statTmp -Algorithm SHA256).Hash.ToLowerInvariant()
        $statPlaced = Move-DirtyEvidenceToFreeName -Tmp $statTmp -Dir $Dest -Stem "dirty-worktree-$id.status-" -Ext '.txt' -Full $statFull -FirstName "dirty-worktree-$id.status.txt" -Guard $guard
        if (-not $statPlaced.Path) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($statPlaced.Reason)$evidence" }
        if ($statPlaced.Moved) { $statTmp = $null }
        $stat = $statPlaced.Path
        $evidence += " status=$stat"
        # An unmerged index (a lane that died mid `git merge fork/master`) has stages 1-3 and no stage 0 for the file: `git diff --cached` writes only '* Unmerged path <file>' and exits 0,
        # which is not a diff anybody can apply. Record the merge state itself and fail typed, naming everything already placed.
        $unmerged = @(& git -C $Wt -c core.quotepath=false ls-files -u 2>$null)
        $unmergedExit = $LASTEXITCODE
        if ($unmergedExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-ls-files-exit-$unmergedExit$evidence" }
        if ($unmerged.Count -gt 0) {
            $state = @()
            foreach ($ref in 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD') {
                $sha = @(& git -C $Wt rev-parse -q --verify $ref 2>$null)
                $state += "$ref=" + $(if ($LASTEXITCODE -eq 0 -and $sha.Count -eq 1) { $sha[0] } else { 'none' })
            }
            $state += 'unmerged index entries (mode sha stage<TAB>path):'
            $state += $unmerged
            $why = & $guard 'before-write'
            if ($why) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$why$evidence" }
            $mergeTmp = Join-Path $Dest ('dirty-worktree-merge-state-saving-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            [System.IO.File]::WriteAllLines($mergeTmp, [string[]]$state, (New-Object System.Text.UTF8Encoding($false)))
            $mergeFull = (Get-FileHash -LiteralPath $mergeTmp -Algorithm SHA256).Hash.ToLowerInvariant()
            $mergePlaced = Move-DirtyEvidenceToFreeName -Tmp $mergeTmp -Dir $Dest -Stem 'dirty-worktree-merge-state-' -Ext '.txt' -Full $mergeFull -Guard $guard
            if (-not $mergePlaced.Path) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($mergePlaced.Reason)$evidence" }
            if ($mergePlaced.Moved) { $mergeTmp = $null }
            return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=unmerged-index$evidence merge-state=$($mergePlaced.Path)"
        }
        # Staged content the working tree no longer has (staged, then edited back) is invisible to diff HEAD; keep it in its own file when it exists.
        # Whether anything is staged is asked of git (exit 1 = yes), never inferred from the size of a file a failed write may have emptied.
        & git -C $Wt diff --cached --quiet --no-ext-diff --ignore-submodules=none 2>$null
        $stagedState = $LASTEXITCODE
        if ($stagedState -notin 0, 1) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-staged-check-exit-$stagedState$evidence" }
        $stagedOut = 'none'
        if ($stagedState -eq 1) {
            $why = & $guard 'before-write'
            if ($why) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$why$evidence" }
            $stagedTmp = Join-Path $Dest ('dirty-worktree-staging-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            & git -C $Wt diff --cached @shape "--output=$stagedTmp" 2>$null
            $stagedExit = $LASTEXITCODE
            if ($stagedExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-staged-diff-exit-$stagedExit$evidence" }
            # A full volume can cut the file short without git saying so: the staged diff must be non-empty and byte-identical to git's own stream for the index.
            if (-not (Test-Path -LiteralPath $stagedTmp) -or (Get-Item -LiteralPath $stagedTmp).Length -eq 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-incomplete$evidence" }
            if (-not (Test-DirtyPatchComplete -Wt $Wt -Patch $stagedTmp -GitDiffArgs (@('diff', '--cached') + $shape))) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-incomplete$evidence" }
            # The header's promise, kept for every input: the diff must reverse-apply to the index it was taken from. --whitespace=nowarn: apply.whitespace=error in a user or repo config would
            # reject a reversed line with trailing blanks, which says nothing about whether the evidence is whole.
            & git -C $Wt apply --cached --check -R --whitespace=nowarn $stagedTmp 2>$null
            $applyExit = $LASTEXITCODE
            if ($applyExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-not-applicable$evidence" }
            $stagedFull = (Get-FileHash -LiteralPath $stagedTmp -Algorithm SHA256).Hash.ToLowerInvariant()
            $stagedPlaced = Move-DirtyEvidenceToFreeName -Tmp $stagedTmp -Dir $Dest -Stem 'dirty-worktree-staged-' -Ext '.diff' -Full $stagedFull -Guard $guard
            if (-not $stagedPlaced.Path) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($stagedPlaced.Reason)$evidence" }
            if ($stagedPlaced.Moved) { $stagedTmp = $null }
            $stagedOut = $stagedPlaced.Path
            $evidence += " staged=$stagedOut"
        }
        $verified = 'empty-diff'  # git apply refuses an empty patch (exit 128), so there is nothing to ask it
        if ($bytes -ne 0) {
            & git -C $Wt apply --check -R --whitespace=nowarn $patch 2>$null
            $verified = if ($LASTEXITCODE -eq 0) { 'reverse-apply-ok' } else { 'reverse-apply-FAILED' }
        }
        return "DIRTY_TREE_SAVED card=$CardName round=$RoundName files=$($status.Count) bytes=$bytes staged=$stagedOut verified=$verified patch=$patch status=$stat reapply=git -C $(ConvertTo-DirtySingleQuoted $Wt) apply $(ConvertTo-DirtySingleQuoted $patch)"
    }
    catch { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($_.Exception.Message -replace '\s+', ' ')$evidence" }  # a throw after a placement still names what was placed (r4 adversary A2)
    finally {
        foreach ($t in $tmp, $stagedTmp, $statTmp, $mergeTmp) { if ($t -and (Test-Path -LiteralPath $t)) { Remove-Item -LiteralPath $t -Force -ErrorAction SilentlyContinue } }
        foreach ($v in $savedEnv.Keys) { Set-Item -LiteralPath "Env:$v" -Value $savedEnv[$v] }
    }
}
