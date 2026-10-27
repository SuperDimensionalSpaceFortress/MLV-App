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
# line is DIRTY_TREE_SAVE_FAILED reason=unmerged-index. Every DIRTY_TREE_SAVE_FAILED that comes after evidence was placed ends with the files already there: patch=<path> status=<path> merge-state=<path>,
# and a file moved into place whose bytes could not be proven to be the validated ones is named last as unverified=<path>. Every diff is digested before it is checked
# and must still hold those bytes when it is placed (reason=evidence-changed-after-validation otherwise). A .git that names another repository by the end of the root checks is reason=worktree-git-dir-changed;
# the files that name the repository (.git gitfile, commondir) and the git and common directories are held for the save, and one that cannot be held is reason=git-metadata-not-held.
# The helper judges every git exit code itself and sets $PSNativeCommandUseErrorActionPreference = $false for its own calls, so the expected exit 1 of `git diff --cached --quiet` cannot throw under a caller that set it.
# It refuses a -Dest inside the worktree it describes (reason=dest-inside-worktree): evidence written there would show in the next status and be captured by the next save.
# -Dest is resolved once to its physical path and every write uses that path; a destination that changed identity before a write or publish is reason=dest-identity-changed.
# On Windows -Dest and -Wt are each held open, and the held handle must be the directory whose identity was sampled before the resolve (worktree-identity-changed / dest-identity-changed).
# Where no directory can be held (any other platform) every save is refused before anything is written: reason=no-directory-hold-on-this-platform. A tracked diff holding a
# gitlink is refused the same way (reason=gitlink-change-not-saved): the plain `git apply` reapply command would leave the submodule pointer where it was.
# Read-only on the worktree (nothing is restored, so the caller's refusal and exit code are unchanged), idempotent (same diff = same name), fail-soft (never throws), and it
# refuses a directory that is not itself a worktree root (that would resolve to an ENCLOSING repo and save its diff). The same logic is deployed board-local in
# hub-autopilot\dispatch-fix-round.ps1; keep the two in step.
# Moves $Tmp to <Dir>\<Stem><first 8|16|32|64 hex of $Full><Ext> and returns @{ Path; Id; Moved }, or @{ Path = $null; Reason } when it cannot place it (below).
# The move fails rather than overwrites, so an occupied name is compared by hash (same bytes = reuse, anything else = lengthen the name) and never clobbered.
# When the name already holds these exact bytes, $Tmp is left for the caller's cleanup. A non-empty $FirstName is tried before the hash-derived names.
# It always returns a hashtable: Path is the placed (or reused) file, or $null with Reason = name-collision (every length is taken by a DIFFERENT file) or
# move-failed:<HResult> <message> (the move threw an IOException and the target does not exist: a sharing violation or an AV lock on $Tmp, not a name clash).
# A file moved into place whose bytes cannot be proven to hash to $Full comes back as Path = $null, Placed = <that file> and Reason = evidence-changed-before-publish:<file>
# or evidence-unverified-after-publish (its hash could not be read).
function Invoke-DirtyFileMove([string]$From, [string]$To) { [System.IO.File]::Move($From, $To) }  # one seam, so a test can stand in for a locked file

function Move-DirtyEvidenceToFreeName([string]$Tmp, [string]$Dir, [string]$Stem, [string]$Ext, [string]$Full, [string]$FirstName = '', [scriptblock]$Guard = $null) {
    if ($Full -cnotmatch '^[0-9a-f]{64}\z') { return @{ Path = $null; Reason = 'bad-digest' } }  # every name below is cut from a lower-case SHA-256; anything else is refused, not thrown (r6 adversary A3; \z, not $, which matches before a final LF)
    if (-not $Tmp -or -not $Dir) { return @{ Path = $null; Reason = 'bad-argument' } }  # Join-Path below throws on an empty -Dir (r6 iter 2 adversary A4)
    $names = @()
    if ($FirstName) { $names += , @($FirstName, '') }
    foreach ($n in 8, 16, 32, 64) { $names += , @("$Stem$($Full.Substring(0, $n))$Ext", $Full.Substring(0, $n)) }
    foreach ($pair in $names) {
        $cand = Join-Path $Dir $pair[0]
        if ($Guard) { $why = & $Guard 'before-publish'; if ($why) { return @{ Path = $null; Reason = $why } } }  # the destination is re-proven right before each rename into place
        try { Invoke-DirtyFileMove $Tmp $cand }
        catch [System.IO.IOException] {
            $ex = $_.Exception
            while ($ex.InnerException -and $ex -isnot [System.IO.IOException]) { $ex = $ex.InnerException }  # a method call wraps the real IOException
            if (-not (Test-Path -LiteralPath $cand)) { return @{ Path = $null; Reason = ('move-failed:0x{0:X8} {1}' -f $ex.HResult, ($ex.Message -replace '\s+', ' ')) } }
            # An occupied name that cannot be read is not proven to hold these bytes, so the next length is tried (r8, adversary 98e161fc-A3 sibling :48: the hash used to throw out of the helper).
            $held = $null
            try { if (Test-Path -LiteralPath $cand -PathType Leaf) { $held = (Get-FileHash -LiteralPath $cand -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() } } catch { $held = $null }
            if ($held -ceq $Full) { return @{ Path = $cand; Id = $pair[1]; Moved = $false } }
            continue
        }
        # Placed. The bytes published must be the bytes validated: a temp file rewritten after its checks would otherwise be placed and reported (r7 iter 2, adversary 43a1998e-A1).
        # From here every failure names the placed file as Placed, so the caller's line reports it (unverified=<path>) instead of losing it (r8, adversary 98e161fc-A3).
        $now = $null
        try { $now = (Get-FileHash -LiteralPath $cand -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() } catch { $now = $null }
        if ($null -eq $now) { return @{ Path = $null; Placed = $cand; Reason = 'evidence-unverified-after-publish' } }
        if ($now -cne $Full) { return @{ Path = $null; Placed = $cand; Reason = "evidence-changed-before-publish:$cand" } }
        return @{ Path = $cand; Id = $pair[1]; Moved = $true }
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
        try { return HandleIdentity(h); } finally { CloseHandle(h); }
    }
    // The same identity read from a handle already open (a Pin): it names the directory the handle holds, whatever has been renamed onto its path since.
    public static string HandleIdentity(IntPtr h) {
        ByHandleInfo i;
        if (h == IntPtr.Zero || !GetFileInformationByHandle(h, out i)) return null;
        return i.Volume.ToString("X8") + ":" + i.IndexHigh.ToString("X8") + i.IndexLow.ToString("X8");
    }
    // No access rights, full sharing, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS (a directory can be opened).
    public static string Resolve(string path) {
        IntPtr h = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
        if (h == (IntPtr)(-1)) return null;
        try {
            // The call returns the length it needs when the buffer is too small (a long path under a SUBST drive), so ask again with that size instead of giving up.
            uint size = 2048;
            for (int attempt = 0; attempt < 3; attempt++) {
                StringBuilder sb = new StringBuilder((int)size);
                uint n = GetFinalPathNameByHandleW(h, sb, size, 0);
                if (n == 0) return null;
                if (n < size) return sb.ToString();
                size = n + 1;
            }
            return null;
        } finally { CloseHandle(h); }
    }
    // Opens the directory for listing (FILE_LIST_DIRECTORY) and shares READ|WRITE but not DELETE: while the handle is open Windows refuses to rename or remove that directory or any
    // directory above it, so the physical path resolved earlier cannot be swapped for a junction. (A zero-access open takes no part in share-mode checks and pins nothing.)
    // Returns IntPtr.Zero when it cannot be opened.
    public static IntPtr Pin(string path) {
        IntPtr h = CreateFileW(path, 1, 3, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
        return h == (IntPtr)(-1) ? IntPtr.Zero : h;
    }
    // Opens a file for reading and shares READ only: others can still read it (git does), but nobody can write, truncate, rename or delete it until Unpin. IntPtr.Zero when it cannot be opened.
    public static IntPtr PinFile(string path) {
        IntPtr h = CreateFileW(path, 0x80000000, 1, IntPtr.Zero, 3, 0, IntPtr.Zero);
        return h == (IntPtr)(-1) ? IntPtr.Zero : h;
    }
    public static void Unpin(IntPtr handle) { if (handle != IntPtr.Zero) CloseHandle(handle); }
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
    if ($kernel) { return Format-DirtyResolved $kernel }
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
        if (-not $relinked) { return Format-DirtyResolved $acc }
    }
    return Format-DirtyResolved $cur
}

# No trailing separator, except on a drive root: 'C:' alone means the drive's current directory, 'C:\' is the root (r5 adversary A6).
function Format-DirtyResolved([string]$Path) {
    $t = $Path.TrimEnd([char[]]@('\', '/'))
    if ($t.Length -eq 2 -and $t[1] -eq ':') { return $t + '\' }
    return $t
}

# Volume serial + file index of the directory a path opens onto (every link followed); where the kernel cannot answer, the resolved path stands in for it.
function Get-DirtyDirIdentity([string]$Path) {
    if ($IsWindows -or $env:OS -eq 'Windows_NT') {
        try {
            if (-not ('DirtyFinalPath' -as [type])) { $null = Get-DirtyKernelPath $Path }  # defines the type; without it an identity would come back in the path-based form
            if ('DirtyFinalPath' -as [type]) { return [DirtyFinalPath]::Identity($Path) }
        } catch { return $null }
    }
    return 'path:' + (Resolve-DirtyRealPath $Path)
}

# One seam, so a test can retarget or swap the destination at a named point; $Stage is 'before-write' or 'before-publish'.
function Invoke-DirtyDestCheckpoint([string]$Stage) { }

# Holds a directory open so that it (and every directory above it) cannot be renamed or removed until Unlock-DirtyDirectory (see DirtyFinalPath.Pin); $null where that is not possible,
# and the saver then refuses (dest-not-held / worktree-not-held, or no-directory-hold-on-this-platform off Windows). Seam: a test overrides this to model a failed hold.
function Lock-DirtyDirectory([string]$Path) {
    if (-not ($IsWindows -or $env:OS -eq 'Windows_NT')) { return $null }
    try {
        if (-not ('DirtyFinalPath' -as [type])) { $null = Get-DirtyKernelPath $Path }  # defines the type
        $h = [DirtyFinalPath]::Pin($Path)
        if ($h -ne [IntPtr]::Zero) { return $h }
    } catch { }
    return $null
}
function Unlock-DirtyDirectory($Handle) { if ($null -ne $Handle) { try { [DirtyFinalPath]::Unpin($Handle) } catch { } } }
# Holds a file open so that nobody can rewrite, rename or delete it (git can still read it) until Unlock-DirtyDirectory; $null where that is not possible. Seam, like Lock-DirtyDirectory.
function Lock-DirtyFile([string]$Path) {
    if (-not ($IsWindows -or $env:OS -eq 'Windows_NT')) { return $null }
    try {
        if (-not ('DirtyFinalPath' -as [type])) { $null = Get-DirtyKernelPath $Path }  # defines the type
        $h = [DirtyFinalPath]::PinFile($Path)
        if ($h -ne [IntPtr]::Zero) { return $h }
    } catch { }
    return $null
}
# The identity of the directory a hold has open, read from the held handle and never by reopening a path (two plain renames can put another directory behind the same path); $null when unknown.
function Get-DirtyHeldIdentity($Handle) { if ($null -eq $Handle) { return $null }; try { return [DirtyFinalPath]::HandleIdentity([IntPtr]$Handle) } catch { return $null } }

# True when $Path is the directory with this identity or sits anywhere below it. A spelling the path comparison cannot relate to the worktree (an SMB share of it, \\localhost\share,
# whose resolved name is in the share's namespace) still opens onto the same volume serial + file index (r5 adversary A4).
function Test-DirtyUnderIdentity([string]$Path, [string]$Identity) {
    if (-not $Identity) { return $false }
    $cur = $Path
    while ($cur) {  # every step shortens the path, so this ends at the root however deep the destination is (r5 adversary A2: a cap of 128 stopped short of the worktree)
        if ((Get-DirtyDirIdentity $cur) -ceq $Identity) { return $true }
        $cur = [System.IO.Path]::GetDirectoryName($cur)
    }
    return $false
}

# True for a UNC path (\\host\share\...) whose host is this machine under ANY name: localhost, the machine name, a DNS alias, an address. Such a share can expose the worktree itself,
# and its ancestry stops at the share root, which the volume serial + file index walk above can then never relate to the worktree (r5 adversary A2). A host on another machine cannot.
function Test-DirtyUncLocalHost([string]$Path) {
    if (-not $Path.StartsWith('\\') -or $Path.StartsWith('\\?\')) { return $false }
    $hostName = ($Path.Substring(2) -split '\\')[0]
    if (-not $hostName) { return $false }
    try {
        $local = @([System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | ForEach-Object { $_.GetIPProperties().UnicastAddresses.Address.IPAddressToString })
        $target = @([System.Net.Dns]::GetHostAddresses($hostName.Split('%')[0].Trim('[', ']')) | ForEach-Object { $_.IPAddressToString })
        foreach ($a in $target) { if ($a -in $local -or $a -eq '::1' -or $a.StartsWith('127.')) { return $true } }
        return $false
    } catch { return $true }  # a name that cannot be placed is not proven remote
}

# The destination was resolved once, to its physical path, and every write goes to that literal path. Before each write and each publish the directory behind that path
# must still be the same directory (same identity, same resolved path) and still outside the worktree; returns '' when it is, otherwise the typed reason.
function Test-DirtyDestStable([string]$Dest, [string]$Identity, [string]$WtReal, [string]$WtIdentity, [string]$Stage) {
    $null = Invoke-DirtyDestCheckpoint $Stage
    if ((Get-DirtyDirIdentity $Dest) -cne $Identity) { return 'dest-identity-changed' }
    $now = Resolve-DirtyRealPath $Dest
    if ($now -ine $Dest) { return 'dest-identity-changed' }
    if ((Test-DirtyPathInside -Inner $now -Outer $WtReal) -or (Test-DirtyUnderIdentity -Path $now -Identity $WtIdentity)) { return 'dest-inside-worktree' }
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
    $trim = [char[]]@('\', '/')
    return ($Inner.TrimEnd($trim) + $sep).StartsWith($Outer.TrimEnd($trim) + $sep, $cmp)  # a drive root keeps its separator when resolved; the comparison must not double it
}

# Reads an exported diff ONCE: Full is the SHA-256 of exactly these Bytes. Every check after this runs on the bytes or their digest, never on a re-read of the file, and the file must
# still hash to Full right before it is placed (Test-DirtyEvidenceUnchanged) and right after (Move-DirtyEvidenceToFreeName). A digest taken after the checks would accept whatever a
# concurrent writer left in the temp file between the last check and the hash (r8, adversary 98e161fc-A1).
function Read-DirtyEvidence([string]$Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $full = ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
    return @{ Bytes = $bytes; Full = $full }
}
function Test-DirtyEvidenceUnchanged([string]$Path, [string]$Full) {
    try { return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ceq $Full) } catch { return $false }
}

# True only when the saved bytes (their digest, $Full) are byte-identical to what git writes to a pipe for the same diff (GitDiffArgs = the arguments after `git -C <Wt>`, without --output).
# A cut at a file boundary, a hunk boundary or just before `\ No newline at end of file` is still a well-formed patch that keeps every line count and reverse-applies, and a
# full volume can make git write less than it was given without a non-zero exit; only a byte-exact comparison against git's own stream sees every such loss.
function Test-DirtyPatchComplete([string]$Wt, [string]$Full, [string[]]$GitDiffArgs) {
    if ($Full -cnotmatch '^[0-9a-f]{64}\z') { return $false }
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
    return ($live -ceq $Full)
}

# True when an exported diff records a gitlink (mode 160000) in a file header. Content lines start with ' ', '+', '-' or '\', and a base85 binary line holds no space,
# so only a header can match. Checked on the exported bytes, so a gitlink staged after the up-front scan is still refused before the diff is placed (r7 iter 1, adversary 6433989c-A1).
# It reads the bytes Read-DirtyEvidence returned, not the file (r8, adversary 98e161fc-A1).
function Test-DirtyDiffHasGitlink([byte[]]$Bytes) {
    if ($null -eq $Bytes) { $Bytes = [byte[]]::new(0) }
    $reader = [System.IO.StreamReader]::new([System.IO.MemoryStream]::new($Bytes))
    try {
        while ($null -ne ($line = $reader.ReadLine())) { if ($line -cmatch '^(index [0-9a-f]+\.\.[0-9a-f]+ 160000|(new file|deleted file|old|new) mode 160000)$') { return $true } }
        return $false
    } finally { $reader.Dispose() }
}

function Save-DirtyWorktreeEvidence([string]$Wt, [string]$Dest, [string]$CardName = '', [string]$RoundName = '') {
    $tmp = $null; $stagedTmp = $null; $statTmp = $null; $mergeTmp = $null; $destLock = $null; $wtLock = $null; $prevEnc = $null; $metaLocks = @()
    $PSNativeCommandUseErrorActionPreference = $false  # local to this call: every git exit code below is judged here, never thrown (git diff --cached --quiet exits 1 when something is staged)
    $evidence = ''  # ' patch=.. status=..' for the files placed so far; every failure after the first placement ends with it
    $CardName = $CardName -replace '\s+', ' '; $RoundName = $RoundName -replace '\s+', ' '  # the result is one line
    # -C $Wt names the repository, so a GIT_DIR / GIT_INDEX_FILE / GIT_DIFF_OPTS inherited from the caller is cleared for the call and put back afterwards.
    # These variables change which repository git reads or the shape of the diff it writes; GIT_DIFF_OPTS=--unified=0, for one, beats the explicit -U3.
    $gitEnv = 'GIT_DIFF_OPTS', 'GIT_EXTERNAL_DIFF', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX'
    $savedEnv = @{}
    foreach ($v in $gitEnv) { if (Test-Path -LiteralPath "Env:$v") { $savedEnv[$v] = (Get-Item -LiteralPath "Env:$v").Value; Remove-Item -LiteralPath "Env:$v" } }  # removed, not emptied: git reads an empty GIT_DIR as a path
    try {
        # PowerShell decodes git's output with [Console]::OutputEncoding; under an ASCII or OEM console a UTF-8 file name would come back as '??' and be saved under the wrong name (r5 adversary A4).
        try { $prevEnc = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { $prevEnc = $null }
        if (-not (Test-Path -LiteralPath $Dest -PathType Container)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=no-run-dir" }
        $Dest = (Resolve-Path -LiteralPath $Dest).ProviderPath  # git -C $Wt would resolve a relative --output against the worktree, not the caller
        # A relative -Wt means the caller's PowerShell location; git -C and [IO.Path]::GetFullPath would read it against the process directory, which can differ (r4 adversary A1).
        if (-not (Test-Path -LiteralPath $Wt -PathType Container)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        $Wt = (Resolve-Path -LiteralPath $Wt).ProviderPath
        # -Dest and -Wt are each resolved ONCE, to their physical paths (every link and junction followed), and held open (Lock-DirtyDirectory) so neither can be renamed or swapped for a
        # junction while the save runs. Every git call and write below uses those literal paths and never reopens the -Dest / -Wt spelling, so a junction retargeted after this point cannot
        # steer evidence into the worktree or make git read another repository. $guard also re-proves the destination (same volume serial + file index, same resolved path, outside the
        # worktree by path and by identity) right before each write and each rename into place, and a change is DIRTY_TREE_SAVE_FAILED reason=dest-identity-changed.
        # Each directory's identity is sampled FIRST, before it is resolved or held, and the hold must then have that same directory open (read from the held handle, not the path):
        # two plain renames between the resolve and the hold (a -> a.old, b -> a) keep the resolved spelling, so only the identity shows that the hold pins another directory (r6, Dot R5-B1).
        # A resolved path holding a line break or other control character is refused before anything is held or written: it would split the one-line result (r6, sol r5 blocker 3).
        $onWindows = [bool]($IsWindows -or $env:OS -eq 'Windows_NT')
        $breaksLine = '[\p{Cc}' + [char]0x2028 + [char]0x2029 + ']'  # C0/C1 controls and the Unicode line and paragraph separators
        $destId = Get-DirtyDirIdentity $Dest
        if (-not $destId) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-identity-changed" }
        $destReal = Resolve-DirtyRealPath $Dest
        if ($destReal -match $breaksLine) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-path-control-char" }
        if (Test-DirtyUncLocalHost $destReal) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-unc-local-host" }
        # A destination that cannot be held cannot be trusted not to be swapped (r5 adversary A5). Off Windows no hold exists at all: a rename after the last guard could steer the
        # diff --output or the temp cleanup, and the identity there is only a path. The saver runs on the Windows board host, so there it fails closed before any write (r7, Dot 3d137411).
        $platformNoHold = 'no-directory-hold-on-this-platform'
        $destLock = Lock-DirtyDirectory $destReal
        if ($null -eq $destLock) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$(if ($onWindows) { 'dest-not-held' } else { $platformNoHold })" }
        if ((Get-DirtyHeldIdentity $destLock) -cne $destId) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-identity-changed" }
        $wtId = Get-DirtyDirIdentity $Wt
        if (-not $wtId) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=worktree-identity-changed" }
        $wtReal = Resolve-DirtyRealPath $Wt
        if ($wtReal -match $breaksLine) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=worktree-path-control-char" }
        # Over SMB a path's ancestry ends at its share root, so neither the prefix nor the identity walk can relate two shares where one exposes a folder inside the other
        # (ReviewRoot$ = D:\repo, ReviewRuns$ = D:\repo\run): a UNC destination is refused when the worktree is UNC as well, and a worktree reached over a share of this machine
        # is refused like such a destination (r6 adversary A2).
        $isUnc = { param($p) $p.StartsWith('\\') -and -not $p.StartsWith('\\?\') }
        if (Test-DirtyUncLocalHost $wtReal) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=worktree-unc-local-host" }
        if ((& $isUnc $destReal) -and (& $isUnc $wtReal)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-unc-unproven" }
        $wtLock = Lock-DirtyDirectory $wtReal
        if ($null -eq $wtLock) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$(if ($onWindows) { 'worktree-not-held' } else { $platformNoHold })" }
        if ((Get-DirtyHeldIdentity $wtLock) -cne $wtId) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=worktree-identity-changed" }
        # Between the resolve and the hold a rename plus a junction could have put another directory behind the same name: the hold would then pin that one. Resolving again, now that
        # the hold is in place, must give the same physical path (r5 adversary A3).
        if ((Resolve-DirtyRealPath $wtReal) -ine $wtReal) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=worktree-identity-changed" }
        $Dest = $destReal; $Wt = $wtReal
        # The repository is read FIRST, the root checks run, and the repository is read again and must be the same one: a .git gitfile rewritten anywhere in between would
        # otherwise have the checks prove one repository and the pins below select another, whose HEAD and index are then saved under the proven work tree (r8, adversary 98e161fc-A2).
        # The common directory (refs and objects) is read with it: a linked worktree names it in its commondir file, which every unpinned git call reads again (r8 iter 1, adversary a8807691-A1).
        $gitDir = @(& git -C $Wt rev-parse --path-format=absolute --absolute-git-dir --git-common-dir 2>$null)
        if ($LASTEXITCODE -ne 0 -or $gitDir.Count -ne 2) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        # Pinning GIT_COMMON_DIR is not enough: git's ref store reads a linked worktree's commondir file itself. So the files that name the repository (the -Wt\.git gitfile and
        # commondir) are held against any write, rename or delete, and the git and common directories like -Dest, until the save ends; the second read below then binds (r8 iter 1).
        foreach ($meta in @((Join-Path $Wt '.git'), (Join-Path $gitDir[0] 'commondir'))) {
            if (-not (Test-Path -LiteralPath $meta -PathType Leaf)) { continue }  # a main worktree has a .git directory and no commondir
            $h = Lock-DirtyFile $meta
            if ($null -eq $h) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-metadata-not-held" }
            $metaLocks += , $h
        }
        foreach ($meta in $gitDir) {
            $h = Lock-DirtyDirectory $meta
            if ($null -eq $h) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-metadata-not-held" }
            $metaLocks += , $h
        }
        $prefix = @(& git -C $Wt rev-parse --show-prefix 2>$null)
        if ($LASTEXITCODE -ne 0 -or ($prefix -join '').Trim() -ne '') { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        # The prefix is also empty OUTSIDE the work tree git uses: a core.worktree in the (worktree) config sends every later git call to another directory while the holds and
        # containment checks describe -Wt. The top level git reports must be the held directory itself (r7 iter 1, adversary 6433989c-A2).
        $top = @(& git -C $Wt rev-parse --show-toplevel 2>$null)
        if ($LASTEXITCODE -ne 0 -or $top.Count -ne 1 -or (Get-DirtyDirIdentity $top[0]) -cne $wtId) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        $again = @(& git -C $Wt rev-parse --path-format=absolute --absolute-git-dir --git-common-dir 2>$null)
        if ($LASTEXITCODE -ne 0 -or $again.Count -ne 2 -or $again[0] -cne $gitDir[0] -or $again[1] -cne $gitDir[1]) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=worktree-git-dir-changed" }
        # Every later git call rereads its config, so a core.worktree written after this check would move it elsewhere again: the repository and work tree just proven are pinned
        # for the rest of the save (GIT_DIR / GIT_WORK_TREE beat core.worktree and the gitfile, GIT_COMMON_DIR beats commondir; the finally puts the caller's values back)
        # (r7 iter 2, adversary 43a1998e-A2; r8 iter 1, adversary a8807691-A1).
        $env:GIT_DIR = $gitDir[0]; $env:GIT_COMMON_DIR = $gitDir[1]; $env:GIT_WORK_TREE = $Wt
        if (Test-DirtyPathInside -Inner $destReal -Outer $wtReal) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=dest-inside-worktree" }
        # A gitlink (mode 160000: a submodule pointer, or a submodule checked out at another commit) is written into the patch, but the advertised plain `git apply` skips its
        # commit ids and the reverse-apply check never looks at them, so SAVED would promise a recovery that drops the change. Such a diff is refused before anything is written (r7, adversary cd31735a-A2).
        # The index is asked as well as the working tree: a file turned into a gitlink only in the index shows in neither the HEAD diff nor its scan (r7 iter 1, adversary 6433989c-A1).
        foreach ($side in 'HEAD', '--cached') {
            $raw = @(& git -C $Wt diff $side --raw --no-renames --no-ext-diff --ignore-submodules=none 2>$null)
            if ($LASTEXITCODE -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-diff-raw-exit-$LASTEXITCODE" }
            if (@($raw | Where-Object { $_ -cmatch '^:(160000 [0-7]{6}|[0-7]{6} 160000) ' }).Count -gt 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=gitlink-change-not-saved" }
        }
        $guard ={ param($stage) Test-DirtyDestStable -Dest $destReal -Identity $destId -WtReal $wtReal -WtIdentity $wtId -Stage $stage }
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
        # Digest first, then the checks on those bytes, then the file must still hold them before it is placed (r8, adversary 98e161fc-A1; see Read-DirtyEvidence).
        $patchEv = Read-DirtyEvidence $tmp
        $full = $patchEv.Full
        if (-not (Test-DirtyPatchComplete -Wt $Wt -Full $full -GitDiffArgs (@('diff', 'HEAD') + $shape))) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=patch-incomplete" }
        if (Test-DirtyDiffHasGitlink $patchEv.Bytes) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=gitlink-change-not-saved" }
        if (-not (Test-DirtyEvidenceUnchanged -Path $tmp -Full $full)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=evidence-changed-after-validation" }
        # The name is the first 8 hex of the hash, lengthened until it is free or already holds these exact bytes (two different diffs may share a prefix).
        $bytes = $patchEv.Bytes.Length
        # A placement that moved a file but could not prove its bytes names it as unverified=<path> after the evidence placed so far (r8, adversary 98e161fc-A3).
        $placeFailed = { param($r) "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($r.Reason)$evidence" + $(if ($r.Placed) { " unverified=$($r.Placed)" } else { '' }) }
        $placed = Move-DirtyEvidenceToFreeName -Tmp $tmp -Dir $Dest -Stem 'dirty-worktree-' -Ext '.patch' -Full $full -Guard $guard
        if (-not $placed.Path) { if ($placed.Placed) { $tmp = $null }; return (& $placeFailed $placed) }
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
        if (-not $statPlaced.Path) { if ($statPlaced.Placed) { $statTmp = $null }; return (& $placeFailed $statPlaced) }
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
            if (-not $mergePlaced.Path) { if ($mergePlaced.Placed) { $mergeTmp = $null }; return (& $placeFailed $mergePlaced) }
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
            # Digest first; every check below runs on these bytes, and the file must still hold them before it is placed (r8, adversary 98e161fc-A1).
            if (-not (Test-Path -LiteralPath $stagedTmp)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-incomplete$evidence" }
            $stagedEv = Read-DirtyEvidence $stagedTmp
            $stagedFull = $stagedEv.Full
            if ($stagedEv.Bytes.Length -eq 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-incomplete$evidence" }
            if (-not (Test-DirtyPatchComplete -Wt $Wt -Full $stagedFull -GitDiffArgs (@('diff', '--cached') + $shape))) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-incomplete$evidence" }
            # The header's promise, kept for every input: the diff must reverse-apply to the index it was taken from. --whitespace=nowarn: apply.whitespace=error in a user or repo config would
            # reject a reversed line with trailing blanks, which says nothing about whether the evidence is whole.
            & git -C $Wt apply --cached --check -R --whitespace=nowarn $stagedTmp 2>$null
            $applyExit = $LASTEXITCODE
            if ($applyExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-not-applicable$evidence" }
            if (Test-DirtyDiffHasGitlink $stagedEv.Bytes) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=gitlink-change-not-saved$evidence" }
            # `git apply` above read the file, not the bytes; this re-hash also proves it read the bytes that were digested (unless rewritten and put back in between: LANE-DIRTY-SAVE-RACE-CLASS-1).
            if (-not (Test-DirtyEvidenceUnchanged -Path $stagedTmp -Full $stagedFull)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=evidence-changed-after-validation$evidence" }
            $stagedPlaced = Move-DirtyEvidenceToFreeName -Tmp $stagedTmp -Dir $Dest -Stem 'dirty-worktree-staged-' -Ext '.diff' -Full $stagedFull -Guard $guard
            if (-not $stagedPlaced.Path) { if ($stagedPlaced.Placed) { $stagedTmp = $null }; return (& $placeFailed $stagedPlaced) }
            if ($stagedPlaced.Moved) { $stagedTmp = $null }
            $stagedOut = $stagedPlaced.Path
            $evidence += " staged=$stagedOut"
        }
        $verified = 'empty-diff'  # git apply refuses an empty patch (exit 128), so there is nothing to ask it
        if ($bytes -ne 0) {
            & git -C $Wt apply --check -R --whitespace=nowarn $patch 2>$null
            $verified = if ($LASTEXITCODE -eq 0) { 'reverse-apply-ok' } else { 'reverse-apply-FAILED' }
        }
        return "DIRTY_TREE_SAVED card=$CardName round=$RoundName files=$($status.Count) bytes=$bytes staged=$stagedOut verified=$verified patch=$patch status=$stat reapply=git -C $(ConvertTo-DirtySingleQuoted $Wt) --git-dir $(ConvertTo-DirtySingleQuoted (Join-Path $Wt '.git')) --work-tree $(ConvertTo-DirtySingleQuoted $Wt) apply --whitespace=nowarn $(ConvertTo-DirtySingleQuoted $patch)"
        # nowarn: apply.whitespace=error would refuse a line with trailing blanks that the saver verified. --git-dir / --work-tree: the saver clears GIT_DIR and GIT_WORK_TREE only
        # for its own calls, and the command is pasted where they are back; the explicit options beat them (r6 adversary A1).
    }
    catch { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($_.Exception.Message -replace '\s+', ' ')$evidence" }  # a throw after a placement still names what was placed (r4 adversary A2)
    finally {
        foreach ($t in $tmp, $stagedTmp, $statTmp, $mergeTmp) { if ($t -and (Test-Path -LiteralPath $t)) { Remove-Item -LiteralPath $t -Force -ErrorAction SilentlyContinue } }
        if ($null -ne $prevEnc) { try { [Console]::OutputEncoding = $prevEnc } catch { } }
        foreach ($h in $metaLocks) { Unlock-DirtyDirectory $h }
        Unlock-DirtyDirectory $wtLock; Unlock-DirtyDirectory $destLock  # released only after the temp cleanup, which still runs inside the held destination
        foreach ($v in 'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE') { Remove-Item -LiteralPath "Env:$v" -ErrorAction SilentlyContinue }  # the pins set above; the caller's own values come back next
        foreach ($v in $savedEnv.Keys) { Set-Item -LiteralPath "Env:$v" -Value $savedEnv[$v] }
    }
}
