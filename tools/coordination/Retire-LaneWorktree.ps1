<#
.SYNOPSIS
    Retire a lane worktree ONLY if it passes the SAFE gate; otherwise keep it and say why.

.DESCRIPTION
    Dot-source this file and call Invoke-RetireLaneWorktree. It never throws: every
    outcome is returned as a disposition object so a caller can put it in a receipt.

    Origin: the 2026-09-14 disk-hygiene run found 85 registered worktrees (~53 GiB under
    .claude-state\worktrees alone) because nothing retired a worktree when its lane ended.
    It also found that `git worktree remove` treats git-IGNORED files as clean, so a
    worktree holding an ignored .claude-state\ (receipts, evidence) is deleted silently.

    THE SAFE GATE - every probe has three outcomes; CANNOT-DETERMINE is never folded into pass:
      skipped  not-a-linked-worktree   WorkDir is the main checkout, or not a git worktree at all
      kept     live-process            a process other than this one HOLDS the path: it is on its command line, it is
                                       the process current directory (or an ancestor of it), or a script it runs / loads
                                       from its directory names it (2026-10-09, WORKTREE-REMOVED-UNDER-LIVE-CHAIN-1: a
                                       measure chain named only its run dir; the worktree sat in a dot-sourced arms.ps1).
                                       One `REFUSED ... pid=N` line per holder goes to stderr; the check is repeated on a
                                       fresh scan right before anything is moved or deleted.
      kept     cwd-probe-unavailable   (r2) the current-directory probe cannot run here (Add-Type failed, ConstrainedLanguage,
                                       32-bit host), so a holder whose only link is its cwd cannot be seen. Fail closed.
      kept     cwd-unknown             (r2) a live process that could hold the path runs a relative script path with an unknown
                                       cwd, or an -EncodedCommand the gate cannot decode (r6). Fail closed. (r7: an UNREADABLE
                                       cwd (other user, elevated, protected) is no longer in this set - see RENAME PROBE below;
                                       it is still listed in disposition.cwdUnknown as evidence, with the r3-r5 exemption
                                       fields cwdExempt, worktreeCreatedUtc, cwdExemptOwner, cwdOwnerProbe, cwdOwnerSkipped.)
      kept     in-use                  (r7) the worktree root could not be renamed to <root>.retiring: some handle is open beneath it
      kept     cannot-determine        (r7) <root>.retiring already exists, or the rename failed for a reason other than a handle
      kept     remove-failed           (r7) the removal failed after a good rename: the root is renamed back and git repointed at it
      kept     stranded-retiring       (r8) WorkDir is a <root>.retiring, or WorkDir is absent and its <root>.retiring could not be restored
      kept     dirty                  `git status --porcelain -uall` is non-empty
      kept     unpushed                HEAD has commits not on any remote
      kept     unmerged                HEAD is on a remote but not an ancestor of -MergeTarget
      kept     stash                   the repository stash list is non-empty
      kept     nested-worktree         another registered worktree lives inside this one
      kept     rundir-inside           -ProtectPath (e.g. the receipt RunDir) is inside the worktree
      kept     cannot-determine:<why>  a probe failed
      retired  ok                      ignored NON-debris entries moved to -QuarantineRoot,
                                       `git clean -fdX`, then `git worktree remove` (never --force)
      kept     remove-failed:<why>     removal was refused; nothing further attempted

    CWD PROBE IS LOUD (r2, 2026-10-09, WORKTREE-REMOVED-UNDER-LIVE-CHAIN-1): an unreadable current directory is never
    folded into "not a holder". Order: a positive hit (live-process) is reported first; otherwise, if the probe cannot
    run, the result is kept / cwd-probe-unavailable; if it runs but cannot read a candidate holder, kept / cwd-unknown
    naming the pids. A would-retire (-WhatIf) is therefore a verdict the probe actually backed. Candidate holder = any
    live process outside this caller's own chain, whatever its command line or session id. 64-bit and 32-bit (WOW64)
    processes of the caller's user are both read; a pid that exited since the snapshot is not a candidate, and neither
    are pids 0 and 4 (System Idle, System: kernel pseudo-processes with no user-mode current directory).

    NULL COMMAND LINE, CREATION TIME (r3, 2026-10-09, hub ruling HUB-TICK 20261009T1004Z, sol blockers on PR #340 at 2d261a18):
      A. A process whose CommandLine reads null is a LIVE process, not an absent one. The snapshot keeps every live non-self
         process; a null command line only means the cmdline check cannot match, and the process still goes through the cwd
         index (a readable cwd inside the worktree is via=cwd). Prior art: fleet doctrine TRAPS.md:15808 "a NULL command
         line is not an absent process" (dng-auto-processor 2026-09-24) and TRAPS.md:15482-15492 (12 live pids read null).
      B. A session id is NOT evidence; creation time is. The r2 session-0 exemption is removed (a service or scheduled
         holder with an unreadable cwd was waved through). The ONE exemption left: a candidate whose cwd is unreadable is
         exempt only when its process CreationDate is strictly EARLIER than the worktree root's CreationTimeUtc (the
         timestamp used, recorded as disposition.worktreeCreatedUtc; disposition.cwdExempt counts the exempted pids), because
         a process that started before the directory existed cannot have had it as its startup cwd. It fails closed
         everywhere else: process creation date unreadable, worktree creation time unreadable, or created at/after the
         worktree -> kept / cwd-unknown naming the pids. It is not applied when the probe itself is unavailable, and never to
         relative-script-unresolved. RESIDUAL RISK, accepted: a process that pre-dates the worktree, later changes
         directory into it, and whose cwd is unreadable to us is not caught (a cmdline or script hit still is).
         A deleted-and-recreated directory of the same name inside the Windows 15 s file-system tunnelling window can keep
         its old CreationTime; the same blind spot, same acceptance.

    SERVICE-OWNER EXEMPTION (r4, 2026-10-09, RETIRE-CWD-UNKNOWN-STARVES-SWEEP-1): with r3 alone the sweep never retired:
      some service host started after every worktree is always alive (HUB-TICK 20261009T1632Z: 4 of 4 merged, clean
      worktrees refused). Census on this host, non-elevated caller: of the unreadable-cwd candidates created after a
      worktree, NONE had a readable owner by token, WMI GetOwnerSid or WTS (access denied / null SID), but the svchost
      ones are named by the SCM. So a candidate whose cwd is unreadable is ALSO exempt when the SCM started that pid as
      LocalSystem, LocalService or NetworkService (S-1-5-18/19/20; Get-ServiceAccountByPid) and the snapshot image matches
      the service binary. Each such pid is listed in disposition.cwdExemptOwner as
      `<pid> <name> exempt=service-sid:<sid> via=scm:<services> bound=config-older-than-process`. disposition.cwdOwnerProbe
      is scm | scm-failed when some candidate reached the owner check, and null when none did (every candidate exempt by
      creation time, or the probe unavailable). disposition.cwdOwnerSkipped names each SCM row that could not be parsed
      (`<svc> pid=<n> scm-row-unparsed:<error>`): that row is skipped and its pid dropped, the other rows still map.
      Why it cannot exempt a lane: a lane runs as THIS user (often a Scheduled Task in session 0); no SCM entry names it,
      and a service configured to run as a user, a per-user service instance or an NT Service\ account is not exempt.
      A session id is still not evidence. Fails closed when the SCM query fails, the probe is unavailable, the pid hosts
      services under mixed accounts, the image differs (pid reuse) or the script path is relative-unresolved. Not exempt
      and still refusing: non-service SYSTEM children such as SearchProtocolHost / SearchFilterHost (owner unreadable).
      RESIDUAL RISK, accepted: a SYSTEM service that changes its own cwd into a worktree is not caught by cwd.

    LIVE BINDING (r5, 2026-10-09, sol blocker 1 on PR #353, HUB-TICK 20261009T1840Z): the SCM StartName is the CONFIGURED
      account, which a running service only adopts at its next start, so a user-owned service reconfigured to LocalSystem
      and not yet restarted was exempted. Now the owner exemption also requires, for EVERY service the SCM maps to the pid,
      that HKLM\SYSTEM\CurrentControlSet\Services\<name> was last written strictly EARLIER than the process CreationDate
      (Get-ServiceKeyLastWriteUtc; a non-elevated KEY_QUERY_VALUE read, nothing written). A key written at/after the
      process start, an unreadable key time or an unreadable process time refuses the pid as cwd-unknown, naming
      scm-config-newer-than-process:<svc> | scm-config-time-unreadable:<svc> | scm-process-time-unreadable. A service that
      rewrites its own key after starting is refused too (fail-closed; seen once on this host, 1 of 164).

    RELATIVE SCRIPT PATHS (r2): a script path on a holder's command line (-File, dot-source in -Command, positional)
    is resolved against THAT holder's current directory, taken from the cwd index, when it is relative or quoted-relative,
    then followed into the transitive corpus exactly like an absolute one. If the holder's cwd is unknown the path cannot
    be followed, so the holder is treated as possibly naming the worktree (cwd-unknown), not skipped.

    ENCODED COMMAND (r6, RETIRE-HOLDER-ENCODEDCOMMAND-DECODE-1; adopts fleet doctrine specs/dng-auto-processor.md section 10
    "Task worktrees"): agent tools launch a shell as `pwsh -EncodedCommand <base64>`, so a path that sits only in the base64
    is invisible to a raw command-line match. A pwsh / powershell holder's command line is therefore ALSO read with every
    -EncodedCommand argument decoded (UTF-16LE): the decoded text gets the same worktree-path test (via=cmdline-encoded) and is
    followed into the script corpus like the raw line. Switch spelling is pwsh's own: -e, -ec, -en ... -encodedcommand, any
    case, `-` or `/`, optional `:value`; -ExecutionPolicy (-ex, -exec) and -EncodedArguments (-ea) are NOT read. An argument
    that is not base64 of UTF-16LE text (or a switch with no value) is a holder the gate cannot read, so it is kept like an
    unreadable cwd (kept / cwd-unknown, Why=encoded-command-undecodable, no creation-time or service-owner exemption).
    NOT adopted from dng: its seat-ancestry exemption and report-leftovers-only reclaim (retire refuses on ANY live holder).

    RENAME PROBE (r7, 2026-10-23, LANE-RETIRE-CWD-UNKNOWN-RENAME-PROBE-1): the cwd-unknown rule retired 0 worktrees in 55 lane
      receipts. Its question - "could a process whose cwd I cannot read be sitting in this tree?" - is one the ACL of that
      process puts out of reach, and on this host ~14 unreadable pids (IIS w3wp.exe app pools, TeamViewer_Desktop, WmiPrvSE,
      elevated pwsh/conhost, sleep.exe) were always newer than the worktree, so every MERGED-PR lane checkout was kept.
      RENAMING the worktree root to <root>.retiring is the probe instead: NTFS refuses a directory rename while ANY handle
      beneath it is open (a cwd counts; so does a file, whatever its share mode - measured 2026-10-22), and it does so whoever
      owns the handle. The rename asks the file system the question directly, so it needs no read access to the holder.
      Order: the readable-cwd, command-line, script and encoded-command checks run first and still name their holders
      (live-process, cwd-unknown for a relative script / undecodable -EncodedCommand, cwd-probe-unavailable); then every
      other gate; then the fresh rescan; then the rename.
      Rename fails -> kept / in-use, nothing moved. Rename ok -> `git worktree repair` repoints git's admin entry at the renamed
      directory (CHOSEN over renaming back before the removal: the tree stays under a name no process knows for the whole
      removal, so nothing can step into it between the probe and the delete, and `git worktree remove` / `list` / `prune` agree
      with the disk at every moment), and the existing clean / quarantine / `git worktree remove` / retry steps run on the renamed
      path. Any outcome but `retired` renames the root BACK and repairs again, so a kept worktree is never left as .retiring;
      the outcome is in disposition.renameRestore (state, note, attempts; the states are listed under OWNERSHIP below). A leftover
      <root>.retiring from a crash between the rename and the repair is never overwritten (kept, cannot-determine). -WhatIf never
      renames: its would-retire says renameProbe 'not-run: WhatIf' and means "retires unless the rename finds a handle". One rename-probe
      attempt per call (a transient scanner handle keeps the worktree as in-use for this sweep; the next lane exit asks again).

    RENAME BACK (r8, 2026-10-23, sol r2 blocker on PR #603; catalogue row: successful rename -> persistent late handle beneath .retiring
      -> failed removal): a handle opened AFTER the rename (a scanner, the indexer) fails the removal, and git half-fails there (it deletes the
      admin entry and the `.git` link, then stops with files left). The same handle then refused the ONE rename back, so <root>.retiring was
      stranded, deregistered, for good. Now (Restore-LaneWorktreeRename):
        a. the rename back is retried like the removal: only RemoveRetryablePattern failures, bounded by RestoreRetryBudgetMs (15 s) /
           RestoreRetryMaxAttempts, counted in renameRestore.attempts. Once back, a registration git deleted is restored too: the admin entry
           from a copy taken before the rename, the `.git` link, `git worktree repair`, then `git checkout-index -a`, which re-creates only
           MISSING tracked files (never overwrites one), so the tree is again the clean, merged checkout the gate passed.
        b. a RENAME RECORD (<git common dir>\mlv-retire-pending\<id>\: record.json + the admin copy) is written BEFORE the rename (no record,
           no rename) and dropped once the tree is retired or restored. If a handle outlives (a) as well, the record stays (stderr:
           LEFT-RETIRING ...) and the NEXT invocation restores the tree first, before any gate decision: the sweep for every record under
           -Root (summary.strandedRestore) before it reads the worktree list, and Invoke-RetireLaneWorktree for an absent WorkDir whose
           <WorkDir>.retiring exists (disposition.strandedRestore; record found via -RepoRoot or the tree's own `.git` link).
           Refused, both paths left alone: the original path exists again (refused), or no record names the .retiring (not a rename this
           tool made). A <root>.retiring is never a retirement candidate: as WorkDir it is kept (stranded-retiring), listed in the sweep it
           is counted kept{stranded-retiring} and never gated. -WhatIf restores nothing (state 'not-run: WhatIf').
      RESIDUAL RISK, accepted: a handle that outlives the rename-back budget leaves <root>.retiring (registered or not) until the next lane
      exit's sweep, whose restore runs under the same budget; a record whose admin copy cannot be taken refuses the rename probe, so that
      worktree is kept as cannot-determine.
      Get-CwdUnknownRefusal keeps its fail-closed default for its other caller (Remove-LaneBulkScratch.ps1, which has no rename
      to lean on); this function opts in with -DeferUnreadableCwd.

    OWNERSHIP (r9, 2026-10-23, fable r4 + sol r4 blocker on PR #603; catalogue row: overlapping sweeps with a live pending record -> never
      restore another live operation, never report retired while the original path exists): two lane exits sweep at once (Invoke-Lane.ps1
      runs one per exit, no mutex) and the fair order gives both the same first candidate. A renamed X and its removal half-failed on a late
      handle; while A backed off, B's restore-first read A's record, moved X.retiring back and deleted the record, and A, finding its path
      gone, reported retired/ok with X present and registered (or, deleting the record while B copied the admin entry out of it, left a
      deregistered tree and no record). Now a rename record BELONGS to the operation that wrote it until that operation decides:
        a. the record's owner.lock is opened FileShare.None before the rename and held to the final decision (retired, restored, or the record
           left for the next call). Any other restorer - the sweep's restore-first, the per-candidate restore of an absent WorkDir - must open
           it the same way first; if it cannot, the record is 'owned' and neither path nor the record is touched. A crashed owner's handle is
           closed by the OS, so its record is claimable at once: no pid or start-time bookkeeping to go stale (ownerPid / ownerStartUtc in
           record.json are diagnostics only). Nothing else needs serializing: a second sweep gating the same X loses the rename itself.
        b. retired needs <root>.retiring gone AND <root> absent AND this call still owning its record. A tree moved back during the removal is
           kept (cannot-determine: ... exists again) and re-registered from this call's record; a record no longer this call's is kept as
           cannot-determine, never retired.
        c. <root>.retiring gone with <root> present is a rename back already done. If <root> is this record's tree (Test-RenamedTreeIdentity:
           the recorded `.git` link, or, with that link half-removed, the root CreationTimeUtc a rename keeps) the registration steps run on it;
           otherwise the record is gone.
        d. H2: a good rename back whose repair fails is restored-unrepaired, never restored: the record stays and the next call repairs it.
        e. H1: the sweep's restore-first counts against -BudgetSeconds; once spent, each remaining record is 'not-reached: budget' and kept.
        f. r10 (Dot R18 B1): re-creating the admin entry or the `.git` link records checkoutOwed in record.json (under the claim, before
           either is re-created), so a retry after a failed repair still runs checkout-index; the record is dropped only once checkout-index
           and status have run. A record.json absent at the sweep's read is a record settled (or being written) by its owner: gone / owned.
      renameRestore.state (and each sweep strandedRestore row / disposition.strandedRestore):
        not-renamed          no rename happened in this call
        retired              the rename was followed by a real retirement; the record is dropped
        restored             back at <root>, registered, record dropped
        restored-unrepaired  back at <root> but git does not list it there, or its owed re-checkout did not run; record kept, the next
                             call or sweep repairs it
        failed               the rename back kept failing (or cannot-determine); <root>.retiring and the record stay for the next try
        refused              <root> exists again and <root>.retiring too; neither is touched, the record stays
        gone                 nothing left to restore (<root>.retiring absent and <root> absent or not this tree); the record is dropped
        owned                (restorers only) another live operation holds the record; nothing touched
        not-reached: budget  (sweep only) -BudgetSeconds was spent first; record kept
        not-run: WhatIf      -WhatIf restores nothing
      RESIDUAL RISK, accepted: a tree that is half-removed (no `.git` link), moved back by something other than this tool, and whose
      directory CreationTimeUtc differs from the recorded one (only if it was copied, not renamed) is not recognised, so its record is dropped
      as gone and the tree stays unregistered at its path; `git worktree list` then simply does not show it.

    Branch refs are NEVER deleted, so every retirement is undoable with `git worktree add`.
    ASCII-only by project convention.

    THE SWEEP - Invoke-SweepMergedLaneWorktrees (2026-10-03, DISK-MERGED-WORKTREE-SWEEP-1):
    The gate above runs at lane exit, which is BEFORE the lane's PR merges, so it always
    answers `unmerged` and keeps the worktree; nothing asked again after the merge. C: fell to
    ~53 GiB free with lane worktrees (~0.7 GiB each, 11-20 created per day) piling up under
    C:\mlvtmp. The sweep makes every lane exit re-ask the SAME gate about every OTHER linked
    worktree under -Root, so steady-state disk use is bounded by the number of UNMERGED
    worktrees, never by history. It adds no deletion rule of its own; the gate decides.
    Two sweep-only guards run around the gate:
      young     the newest of {worktree dir CreationTime, <gitdir>\HEAD mtime, <gitdir>\index mtime}
                is within -MinIdleHours: a hub that just ran `git worktree add` at the merge target
                has a clean, MERGED tree no process names yet, which the gate alone would retire
                before its lane starts. Unreadable stamps are kept as cannot-determine.
      budget    after -BudgetSeconds no further worktree is examined; the remainder is reported as
                notReached, never assumed done.
    Returns one summary object (schema mlv-app/merged-worktree-sweep/v1) and never throws.

    FAIR ORDER (2026-10-03, DISK-SWEEP-FAIR-ORDER-1): candidates are visited least-recently-examined
    first. A stable `git worktree list` order let the same long-lived unmerged/dirty head eat the
    whole budget on every exit (11 of 21 never reached). After the gate returns for a worktree
    (anything but retired) the sweep writes a UTC stamp to <gitdir>\mlv-sweep-examined; missing =
    oldest, ties keep list order. The stamp lives in the git admin dir, never in the worktree, and
    the young guard reads only HEAD/index mtimes and the dir CreationTime, so it cannot age a worktree.
    Summary adds examined (gate calls), elapsedMs, stampWriteFailed. The gate itself was ~36 s per
    worktree, ~93% in two CIM scans, so the sweep passes ONE process snapshot (-ProcessSnapshot).
#>

# HOST-FSMONITOR-DAEMON-LEAK-1: Disable-/Stop-LaneWorktreeFsmonitor live in a light sibling so callers that only create worktrees need not compile this
# file's Add-Type readers. Loaded fail-soft (a fixture that copies this file alone just skips the fsmonitor calls below).
$laneFsmonitorLib = Join-Path $PSScriptRoot 'lane-fsmonitor.ps1'
if (Test-Path -LiteralPath $laneFsmonitorLib) { . $laneFsmonitorLib }
$script:SweepStampName = 'mlv-sweep-examined'
$script:RetireDebrisPattern ='(^|/)(__pycache__|\.pytest_cache|\.hypothesis|build-release|build-debug|build-avx-parity|build-console)/$|\.pyc$'
$script:HolderScriptExt = 'ps1|psm1|py|cmd|bat|js|mjs'
$script:HolderMaxFilesPerProcess = 40
$script:HolderMaxBytes = 524288
$script:HolderMaxDepth = 3
# CI-FLAKE-RETIRE-HOLDER-EXIT-PERMISSION-DENIED-1: a handle can outlive the process that held it (a exited holder, Defender, the indexer), so the one
# `git worktree remove` can fail with access-denied after the gate saw nothing. Git renders the Windows errors as "Permission denied" (EACCES) or
# "Invalid argument" (EINVAL from a sharing violation on a file open without FILE_SHARE_DELETE); native tools as "Access is denied" / "being used by
# another process". Only a failure matching this pattern is retried; any other message is the first and only attempt.
$script:RemoveRetryablePattern = 'Permission denied|Access is denied|Access to the path|Invalid argument|being used by another process|sharing violation|Device or resource busy'
# The retry budget is bounded in both time and attempts; tests shrink it through these two seams.
$script:RemoveRetryBudgetMs = 15000
$script:RemoveRetryDelayMs = 250
$script:RemoveRetryMaxAttempts = 40
# r8 (sol r2 blocker on PR #603): the rename BACK after a failed removal is retried the same way (same pattern, its own budget), and a
# rename record under <git common dir>\mlv-retire-pending lets the next call or sweep restore a <root>.retiring this budget could not.
$script:RestoreRetryBudgetMs = 15000
$script:RestoreRetryDelayMs = 250
$script:RestoreRetryMaxAttempts = 40
$script:RetirePendingDirName = 'mlv-retire-pending'
# r9 (fable r4 + sol r4 blocker on PR #603): a record's owner.lock is held open (FileShare.None) by the operation that owns the record; a
# restorer must open it the same way first. The OS closes the handle when the owner exits or crashes, so an orphaned record is claimable.
$script:RetirePendingLockName = 'owner.lock'

if (-not ('MlvProcCwd' -as [type])) {
    # Current directory of another process: Win32_Process does not expose it. Reads PEB->ProcessParameters->CurrentDirectory
    # of a 64-bit process, or the PEB32 copy of a 32-bit (WOW64) one, that the caller may open. Anything unreadable
    # (other user, elevated, protected) returns null; the caller turns that into cwd-unknown, never into "not a holder".
    try {
        Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class MlvProcCwd {
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool IsWow64Process(IntPtr h, out bool wow);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool ReadProcessMemory(IntPtr h, IntPtr addr, byte[] buf, IntPtr size, out IntPtr read);
    [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr h, int cls, byte[] info, int len, out int retLen);
    public static string Get(int pid) {
        if (!Environment.Is64BitProcess || pid <= 4) return null;
        IntPtr h = OpenProcess(0x0410, false, pid);
        if (h == IntPtr.Zero) return null;
        try {
            bool wow;
            if (!IsWow64Process(h, out wow)) return null;
            IntPtr rd; byte[] b8 = new byte[8];
            if (wow) {
                // 32-bit process: PEB32 address (class 26), PEB32+0x10 = ProcessParameters32, +0x24 = CurrentDirectory.DosPath
                byte[] w = new byte[8]; int wl;
                if (NtQueryInformationProcess(h, 26, w, 8, out wl) != 0) return null;
                long peb32 = BitConverter.ToInt64(w, 0);
                if (peb32 == 0) return null;
                byte[] b4 = new byte[4];
                if (!ReadProcessMemory(h, new IntPtr(peb32 + 0x10), b4, (IntPtr)4, out rd)) return null;
                long pp32 = BitConverter.ToUInt32(b4, 0);
                if (pp32 == 0) return null;
                byte[] us32 = new byte[8];
                if (!ReadProcessMemory(h, new IntPtr(pp32 + 0x24), us32, (IntPtr)8, out rd)) return null;
                int len32 = BitConverter.ToUInt16(us32, 0); long buf32 = BitConverter.ToUInt32(us32, 4);
                if (len32 <= 0 || buf32 == 0) return null;
                byte[] sb32 = new byte[len32];
                if (!ReadProcessMemory(h, new IntPtr(buf32), sb32, (IntPtr)len32, out rd)) return null;
                return Encoding.Unicode.GetString(sb32);
            }
            byte[] pbi = new byte[48]; int rl;
            if (NtQueryInformationProcess(h, 0, pbi, 48, out rl) != 0) return null;
            long peb = BitConverter.ToInt64(pbi, 8);
            if (peb == 0) return null;
            if (!ReadProcessMemory(h, new IntPtr(peb + 0x20), b8, (IntPtr)8, out rd)) return null;
            long pp = BitConverter.ToInt64(b8, 0);
            if (pp == 0) return null;
            byte[] us = new byte[16];
            if (!ReadProcessMemory(h, new IntPtr(pp + 0x38), us, (IntPtr)16, out rd)) return null;
            int len = BitConverter.ToUInt16(us, 0); long buf = BitConverter.ToInt64(us, 8);
            if (len <= 0 || buf == 0) return null;
            byte[] sb = new byte[len];
            if (!ReadProcessMemory(h, new IntPtr(buf), sb, (IntPtr)len, out rd)) return null;
            return Encoding.Unicode.GetString(sb);
        } finally { CloseHandle(h); }
    }
}
'@
    } catch { }
}

if (-not ('MlvRegKeyTime' -as [type])) {
    # r5: LastWriteTime of an HKLM key as a FILETIME, opened KEY_QUERY_VALUE only (read, never write). <= 0 = unreadable.
    try {
        Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class MlvRegKeyTime {
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)] static extern int RegOpenKeyExW(IntPtr hKey, string sub, int opt, int sam, out IntPtr res);
    [DllImport("advapi32.dll")] static extern int RegCloseKey(IntPtr h);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)] static extern int RegQueryInfoKeyW(IntPtr h, IntPtr cls, IntPtr clsLen, IntPtr res, IntPtr subKeys, IntPtr maxSub, IntPtr maxCls, IntPtr vals, IntPtr maxValName, IntPtr maxValLen, IntPtr secDesc, out long lastWrite);
    public static long Get(string sub) {
        IntPtr h;
        if (RegOpenKeyExW(new IntPtr(unchecked((int)0x80000002)), sub, 0, 0x0001, out h) != 0) return 0;
        try {
            long ft;
            if (RegQueryInfoKeyW(h, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, out ft) != 0) return 0;
            return ft;
        } finally { RegCloseKey(h); }
    }
}
'@
    } catch { }
}

function Test-CwdProbeAvailable {
    # False when the compiled reader is absent (Add-Type failed, ConstrainedLanguage) or this host is not a 64-bit process.
    # A seam too: a test redefines this function to force the unavailable path.
    return [bool](('MlvProcCwd' -as [type]) -and [Environment]::Is64BitProcess)
}

function Get-ProcessCurrentDirectory {
    param([int]$ProcessId)
    if (-not ('MlvProcCwd' -as [type])) { return $null }
    try { return [MlvProcCwd]::Get($ProcessId) } catch { return $null }
}

function Get-LaneProcessSnapshot {
    # ONE Win32_Process scan, then the self/ancestor chain is walked IN MEMORY from its ParentProcessId
    # column. Measured 2026-10-03: the old per-ancestor `Get-CimInstance -Filter "ProcessId=N"` loop cost
    # ~31 s of a ~36 s gate call; the one full scan costs ~2.4 s. Throws when the scan fails (the caller
    # turns that into cannot-determine, never into "no live process").
    $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $byPid = @{}
    foreach ($p in $all) { $byPid[[int]$p.ProcessId] = $p }
    $self = @($PID)
    $node = $byPid[[int]$PID]
    $pp = if ($node) { $node.ParentProcessId } else { 0 }
    while ($pp -and $self -notcontains $pp) {
        $self += $pp
        $node = $byPid[[int]$pp]
        $pp = if ($node) { $node.ParentProcessId } else { 0 }
    }
    [pscustomobject]@{
        # r3: a NULL command line is not an absent process (fleet doctrine TRAPS.md:15808; 12 live pids read it null at
        # TRAPS.md:15482-15492). Every live non-self process stays; the cmdline check simply cannot match it.
        Procs       = @($all | Where-Object { $self -notcontains $_.ProcessId })
        SelfPids    = $self
        CapturedUtc = (Get-Date).ToUniversalTime()
    }
}

function Resolve-HolderScriptPath {
    # A script path from a holder's command line, made absolute against THAT holder's current directory when it is
    # relative (or drive-relative, like \x.ps1). Returns $null when it is relative and the holder's cwd is unknown.
    param([string]$Raw, [string]$HolderCwd)
    $p = $Raw -replace '/', '\'
    if ($p -match '^[A-Za-z]:\\' -or $p.StartsWith('\\')) { return $p }
    if (-not $HolderCwd) { return $null }
    if ($p.StartsWith('\')) { $p = $HolderCwd.Substring(0, 2) + $p }
    elseif ($p -match '^[A-Za-z]:') { return $null }
    return [IO.Path]::GetFullPath([IO.Path]::Combine($HolderCwd, $p))
}

function Get-EncodedCommandText {
    # r6 (RETIRE-HOLDER-ENCODEDCOMMAND-DECODE-1, fleet doctrine specs/dng-auto-processor.md section 10): a PowerShell holder
    # started as `pwsh -EncodedCommand <base64>` carries its code as UTF-16LE base64, so its raw command line never names the
    # worktree. Returns {Present; Failed; Text}: Text is every decoded argument joined by newlines. Present = the line carries
    # the switch; Failed = a switch had no value, or its value is not base64 of UTF-16LE text (odd byte count, a lone
    # surrogate, or empty), so the caller must treat the holder as unknown (kept), never as "not a holder".
    # Only pwsh / powershell are read. Switch spelling follows pwsh itself (measured, pwsh 7 and Windows PowerShell 5.1):
    # -e, -ec and every prefix of -EncodedCommand from -en up, any case, a leading '/' as well as '-', and a `:value` suffix.
    # -ExecutionPolicy (-ex, -exec) and -EncodedArguments (-ea, -encodeda ...) are other parameters and are not read.
    param([string]$Name, [string]$CommandLine)
    $res = [pscustomobject]@{ Present = $false; Failed = $false; Text = '' }
    if ([string]$Name -notmatch '(?i)^(pwsh|pwsh-preview|powershell)(\.exe)?$' -or [string]::IsNullOrEmpty($CommandLine)) { return $res }
    $tokens = @([regex]::Matches($CommandLine, '"[^"]*"|\S+') | ForEach-Object { $_.Value })
    $texts = New-Object System.Collections.Generic.List[string]
    $strict = New-Object System.Text.UnicodeEncoding($false, $false, $true)   # little-endian, no BOM, throw on invalid
    for ($i = 1; $i -lt $tokens.Count; $i++) {   # token 0 is the executable
        $m = [regex]::Match($tokens[$i].Trim('"'), '^[-/]([A-Za-z]+)(?::(.*))?$')
        if (-not $m.Success) { continue }
        $sw = $m.Groups[1].Value.ToLowerInvariant()
        if (-not ($sw -eq 'ec' -or $sw -eq 'e' -or ($sw.Length -ge 2 -and 'encodedcommand'.StartsWith($sw)))) { continue }
        $res.Present = $true
        $arg = if ($m.Groups[2].Success -and $m.Groups[2].Value) { $m.Groups[2].Value } elseif ($i + 1 -lt $tokens.Count) { $i++; $tokens[$i] } else { $null }
        try {
            if ($null -eq $arg) { throw 'no value' }
            $txt = $strict.GetString([Convert]::FromBase64String($arg.Trim('"').Trim()))
            if ([string]::IsNullOrWhiteSpace($txt)) { throw 'empty' }
            $texts.Add($txt)
        } catch { $res.Failed = $true }
    }
    $res.Text = $texts -join "`n"
    return $res
}

function Get-ScriptCorpus {
    # The text a live script process can load: the script on its command line plus, transitively (same directory,
    # or an absolute path written in the text), the scripts it names. A chain whose command line names only its RUN
    # DIR still reaches the worktree through a dot-sourced arms.ps1 (2026-10-09). Bounded by file count, size and depth.
    # r2: a relative script path on the command line (quoted or not) is resolved against the HOLDER's cwd; if that is
    # unknown the path cannot be followed and RelativeUnresolved is set so the caller fails closed.
    param([string]$CommandLine, [hashtable]$FileCache, [string]$HolderCwd = '')
    $ext = $script:HolderScriptExt
    $queue = New-Object System.Collections.Generic.Queue[object]
    $unresolved = $false
    $rx = "(?i)`"([^`"]+\.(?:$ext))`"|([A-Za-z]:[\\/][^\s`"]+\.(?:$ext))|(?<![\w:\\/.\-])((?:[\w.\-]+[\\/])*[\w.\-]+\.(?:$ext))(?![\w\\/])"
    foreach ($m in [regex]::Matches($CommandLine, $rx)) {
        $raw = if ($m.Groups[1].Success) { $m.Groups[1].Value } elseif ($m.Groups[2].Success) { $m.Groups[2].Value } else { $m.Groups[3].Value }
        $first = Resolve-HolderScriptPath -Raw $raw -HolderCwd $HolderCwd
        if ($null -eq $first) { $unresolved = $true; continue }
        $queue.Enqueue(@($first, 0))
    }
    $seen = @{}
    $out = New-Object System.Collections.Generic.List[object]
    while ($queue.Count -gt 0 -and $out.Count -lt $script:HolderMaxFilesPerProcess) {
        $item = $queue.Dequeue()
        $full = $null; $text = $null
        try {
            $full = [IO.Path]::GetFullPath(([string]$item[0] -replace '/', '\'))
            if ($seen.ContainsKey($full.ToLowerInvariant())) { continue }
            $seen[$full.ToLowerInvariant()] = $true
            if ($FileCache.ContainsKey($full.ToLowerInvariant())) { $text = $FileCache[$full.ToLowerInvariant()] }
            else {
                $fi = New-Object IO.FileInfo $full
                $text = if ($fi.Exists -and $fi.Length -le $script:HolderMaxBytes) { [IO.File]::ReadAllText($full) } else { $null }
                $FileCache[$full.ToLowerInvariant()] = $text
            }
        } catch { continue }
        if ($null -eq $text) { continue }
        $out.Add([pscustomobject]@{ File = $full; Text = $text })
        if ([int]$item[1] -ge $script:HolderMaxDepth) { continue }
        $dir = Split-Path -Parent $full
        foreach ($m in [regex]::Matches($text, "(?i)[A-Za-z]:[\\/][^\s`"'<>|]+\.(?:$ext)\b|[\w.\-]+\.(?:$ext)\b")) {
            $cand = if ($m.Value -match '^[A-Za-z]:') { $m.Value } else { Join-Path $dir $m.Value }
            $queue.Enqueue(@($cand, ([int]$item[1] + 1)))
        }
    }
    return [pscustomobject]@{ Files = $out.ToArray(); RelativeUnresolved = $unresolved }
}

function Get-LaneHolderIndex {
    # Per-snapshot cache: {Cwd: pid -> current directory, Scripts: [{ProcessId; File; Text}]}. Built once, on first use,
    # so a sweep over N worktrees pays for the cwd reads and script reads once. A snapshot row may carry its own
    # CurrentDirectory (tests, or a caller that already has it); otherwise it is read from the process.
    param([object]$Snapshot)
    $prop = $Snapshot.PSObject.Properties['HolderIndex']
    if ($prop -and $null -ne $prop.Value) { return $prop.Value }
    $cwd = @{}
    $decoded = @{}
    $scripts = New-Object System.Collections.Generic.List[object]
    $unknown = New-Object System.Collections.Generic.List[object]
    $fileCache = @{}
    $scriptRunner = '(?i)^(pwsh|powershell|python|pythonw|py|node|cmd|cscript|wscript)(\.exe)?$'
    $probeOk = [bool](Test-CwdProbeAvailable)
    $selfPids = @($Snapshot.SelfPids)
    $livePids = $null
    foreach ($proc in @($Snapshot.Procs)) {
        $procId = [int]$proc.ProcessId
        if ($selfPids -contains $procId) { continue }   # the caller's own chain legitimately sits in the worktree
        $own =$proc.PSObject.Properties['CurrentDirectory']
        $c = if ($own -and [string]$own.Value) { [string]$own.Value } elseif ($probeOk) { Get-ProcessCurrentDirectory -ProcessId $procId } else { $null }
        $c = if ($c) { (([string]$c -replace '/', '\').TrimEnd('\')) } else { $null }
        if ($c) { $cwd[$procId] = $c }
        $relUnresolved = $false
        # r6: the command line read WITH any -EncodedCommand argument decoded. The decoded text is matched like the raw line
        # and followed into the script corpus; an argument that does not decode makes the holder unknown (kept) below.
        $enc = Get-EncodedCommandText -Name ([string]$proc.Name) -CommandLine ([string]$proc.CommandLine)
        if ($enc.Text) { $decoded[$procId] = [string]$enc.Text }
        if ([string]$proc.Name -match $scriptRunner) {
            $corpus = Get-ScriptCorpus -CommandLine (([string]$proc.CommandLine) + "`n" + $enc.Text) -FileCache $fileCache -HolderCwd ([string]$c)
            $relUnresolved = [bool]$corpus.RelativeUnresolved
            foreach ($f in @($corpus.Files)) {
                $scripts.Add([pscustomobject]@{ ProcessId = $procId; File = $f.File; Text = $f.Text })
            }
        }
        if ($c -and -not $enc.Failed) { continue }
        # cwd unknown (or, r6, an -EncodedCommand argument that does not decode). Only a pid that exited since the snapshot holds nothing, and pids 0 and 4 (System Idle, System) are
        # kernel pseudo-processes with no user-mode current directory. Every other process is a candidate whatever its
        # session id (r3: a session id is not evidence); Get-CwdUnknownRefusal applies the one creation-time exemption.
        if ($procId -le 4) { continue }
        # One process listing for the whole index, not one Get-Process per candidate (measured: 172 candidates = ~11 s).
        if ($null -eq $livePids) {
            $livePids = @{}
            try { foreach ($q in [Diagnostics.Process]::GetProcesses()) { $livePids[[int]$q.Id] = $true; $q.Dispose() } } catch { $livePids = $null }
        }
        $alive = if ($null -ne $livePids) { $livePids.ContainsKey($procId) } else { [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue) }
        if (-not $alive) { continue }
        $why = if ($enc.Failed) { 'encoded-command-undecodable' } elseif ($relUnresolved) { 'relative-script-unresolved' } elseif ($probeOk) { 'cwd-unreadable' } else { 'cwd-probe-unavailable' }
        $cdp = $proc.PSObject.Properties['CreationDate']
        $created = if ($cdp -and $cdp.Value -is [datetime]) { ([datetime]$cdp.Value).ToUniversalTime() } else { $null }
        $unknown.Add([pscustomobject]@{ ProcessId = $procId; Name = [string]$proc.Name; Why = $why; CreatedUtc = $created })
    }
    $byPid = @{}
    foreach ($s in $scripts) {
        if (-not $byPid.ContainsKey($s.ProcessId)) { $byPid[$s.ProcessId] = New-Object System.Collections.Generic.List[object] }
        $byPid[$s.ProcessId].Add($s)
    }
    $idx = [pscustomobject]@{
        Cwd = $cwd; Decoded = $decoded; Scripts = $scripts.ToArray(); ScriptsByPid = $byPid
        CwdProbe = $(if ($probeOk) { 'ok' } else { 'unavailable' }); CwdUnknown = $unknown.ToArray()
    }
    $Snapshot | Add-Member -NotePropertyName HolderIndex -NotePropertyValue $idx -Force
    return $idx
}

function Find-WorktreeHolders {
    # Live processes (other than the caller's own chain) that HOLD the worktree, by any of:
    #   cmdline  the path is on the process command line (either slash spelling, case-insensitive)
    #   cwd      the process current directory is the worktree or inside it
    #   script   a script the process runs, or one it loads from its directory, names the path
    # Returns one row per pid {ProcessId; Name; Via}. Unreadable cwd / script is not a hit here (system and other-user
    # processes cannot be opened) - Get-CwdUnknownRefusal turns it into a refusal; the three probes together are what a
    # command-line-only check missed. A null command line cannot match 'cmdline' but is still checked by cwd and script.
    param([string]$WorktreePath, [object]$Snapshot)
    $wd = $WorktreePath.TrimEnd('\')
    $slash = $wd -replace '\\', '/'
    $self = @($Snapshot.SelfPids)
    $procs = @($Snapshot.Procs | Where-Object { $self -notcontains $_.ProcessId })
    $idx = Get-LaneHolderIndex -Snapshot $Snapshot
    $names = @($wd, $slash, ($wd -replace '\\', '\\')) | ForEach-Object { [regex]::Escape($_) + '(?![A-Za-z0-9_.\-])' }
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($p in $procs) {
        $procId = [int]$p.ProcessId
        $via = $null
        $cl = [string]$p.CommandLine   # null when the command line is unreadable: nothing to match, but the cwd probe still runs
        if ($cl.IndexOf($wd, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or $cl.IndexOf($slash, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $via = 'cmdline' }
        # A holder index built by a caller (a -SnapshotProvider) predates Decoded: read it only when present (StrictMode throws on a missing property).
        if (-not $via -and $idx.PSObject.Properties['Decoded'] -and $idx.Decoded.ContainsKey($procId)) {
            $dt = [string]$idx.Decoded[$procId]   # r6: the same path test over the decoded -EncodedCommand text
            if ($dt.IndexOf($wd, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or $dt.IndexOf($slash, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $via = 'cmdline-encoded' }
        }
        if (-not $via -and $idx.Cwd.ContainsKey($procId)) {
            $c = [string]$idx.Cwd[$procId]
            if ($c -ieq $wd -or $c.StartsWith($wd + '\', [StringComparison]::OrdinalIgnoreCase)) { $via = 'cwd' }
        }
        if (-not $via) {
            $mine = if ($idx.ScriptsByPid.ContainsKey($procId)) { $idx.ScriptsByPid[$procId] } else { @() }
            foreach ($s in $mine) {
                foreach ($rx in $names) {
                    if ([regex]::IsMatch($s.Text, $rx, [Text.RegularExpressions.RegexOptions]::IgnoreCase)) { $via = 'script:' + (Split-Path -Leaf $s.File); break }
                }
                if ($via) { break }
            }
        }
        if ($via) { $rows.Add([pscustomobject]@{ ProcessId = $procId; Name = [string]$p.Name; Via = $via }) }
    }
    return $rows.ToArray()
}

function Get-RunningServiceRows {
    # Win32_Service rows of running services. A seam: a test redefines this function to feed fixed rows.
    return @(Get-CimInstance Win32_Service -Filter "State='Running'" -Property ProcessId, Name, StartName, ServiceType, PathName -ErrorAction Stop)
}

function Get-ServiceKeyLastWriteUtc {
    # r5: LastWriteTime (UTC) of HKLM\SYSTEM\CurrentControlSet\Services\<Name>, read non-elevated with KEY_QUERY_VALUE
    # (2026-10-09: 164 of 164 running services readable as a standard user). $null when unreadable. A seam for tests.
    param([string]$Name)
    if (-not $Name -or -not ('MlvRegKeyTime' -as [type])) { return $null }
    $ft = try { [MlvRegKeyTime]::Get('SYSTEM\CurrentControlSet\Services\' + $Name) } catch { 0 }
    if ($ft -le 0) { return $null }
    return [datetime]::FromFileTimeUtc($ft)
}

function Get-ServiceAccountByPid {
    # r4: pid -> {Sid; Services; Names; Image} for processes the SCM started as a well-known service identity. Every service
    # the SCM lists at that pid must be an own/share-process service whose configured account is LocalSystem (S-1-5-18),
    # LocalService (S-1-5-19) or NetworkService (S-1-5-20), all naming the same image; any other account (a user, a
    # per-user service instance, an NT Service\ virtual account) drops the pid. Throws when the SCM query fails.
    # r5: a row that cannot be parsed is skipped and named in -Skipped (its pid, if read, is dropped); the rest still map.
    param([System.Collections.Generic.List[string]]$Skipped = $null)
    $map = @{}; $bad = @{}
    foreach ($s in @(Get-RunningServiceRows)) {
        $procId = 0
        try {
            $procId = [int]$s.ProcessId
            if ($procId -le 4) { continue }
            $sid = switch -Regex ([string]$s.StartName) {
                '^(\.\\)?LocalSystem$|^NT AUTHORITY\\SYSTEM$' { 'S-1-5-18'; break }
                '^NT AUTHORITY\\(LocalService|LOCAL SERVICE)$' { 'S-1-5-19'; break }
                '^NT AUTHORITY\\(NetworkService|NETWORK SERVICE)$' { 'S-1-5-20'; break }
                default { $null }
            }
            $m = [regex]::Match([string]$s.PathName, '^\s*"?([^"]*?\.exe)', 'IgnoreCase')
            $img = if ($m.Success) { Split-Path -Leaf $m.Groups[1].Value } else { $null }
            if (-not $sid -or -not $img -or [string]$s.ServiceType -notin @('Own Process', 'Share Process')) { $bad[$procId] = $true; continue }
            if (-not $map.ContainsKey($procId)) { $map[$procId] = [pscustomobject]@{ Sid = $sid; Services = [string]$s.Name; Names = @([string]$s.Name); Image = $img }; continue }
            if ($map[$procId].Sid -ne $sid -or $map[$procId].Image -ine $img) { $bad[$procId] = $true; continue }
            $map[$procId].Services += ',' + [string]$s.Name
            $map[$procId].Names += [string]$s.Name
        } catch {
            $err = $_.Exception.GetType().Name
            if ($procId -gt 4) { $bad[$procId] = $true }
            $nm = try { [string]$s.Name } catch { '?' }
            if ($null -ne $Skipped) { $Skipped.Add("$nm pid=$procId scm-row-unparsed:$err") }
        }
    }
    foreach ($k in @($bad.Keys)) { $map.Remove($k) }
    return $map
}

function Get-CwdUnknownRefusal {
    # r2: the cwd probe's own verdict for this snapshot and worktree. Unknown is empty when every candidate holder's cwd was
    # read or exempted; otherwise Reason/Rows/Unknown describe a kept / cwd-probe-unavailable | cwd-unknown result. Never a
    # would-retire. r3: the first exemption is creation time - a candidate whose cwd is unreadable is exempt only when its
    # CreationDate is strictly EARLIER than the worktree root's CreationTimeUtc (a process that started before the directory
    # existed cannot have had it as its startup cwd). It needs a working probe (an unavailable probe is a host fault, not a
    # per-process blind spot) and never applies to relative-script-unresolved. Creation date or worktree time unreadable,
    # or created at/after the worktree: refused. r4: the second exemption is the owner, under the same two conditions -
    # a pid the SCM started as S-1-5-18/19/20 (Get-ServiceAccountByPid) whose snapshot image matches; listed in ExemptOwner.
    # r5: and only when that configured account is bound to the LIVE process: every service at the pid has a registry key
    # last written strictly EARLIER than the process CreationDate. Key newer (scm-config-newer-than-process:<svc>), key
    # time unreadable (scm-config-time-unreadable:<svc>) or process time unreadable (scm-process-time-unreadable): refused.
    # r7: -DeferUnreadableCwd (the retirement gate only) moves every row whose ONLY problem is an unreadable cwd (cwd-unreadable, or
    # the r5 scm-* binding refusals derived from it) out of Unknown/Reason/Rows into Deferred: the rename probe answers it. Rows
    # that name a different problem (relative-script-unresolved, encoded-command-undecodable, cwd-probe-unavailable) stay refusals.
    param([object]$Snapshot, [string]$WorktreePath, [switch]$DeferUnreadableCwd)
    $idx = Get-LaneHolderIndex -Snapshot $Snapshot
    $wtCreated = try { (Get-Item -LiteralPath $WorktreePath -ErrorAction Stop).CreationTimeUtc } catch { $null }
    $unk = @(); $exempt = 0; $owner = @(); $ownerProbe = $null; $ownerSkipped = @()
    foreach ($u in @($idx.CwdUnknown)) {
        if ($idx.CwdProbe -eq 'ok' -and $u.Why -eq 'cwd-unreadable' -and $null -ne $wtCreated -and $null -ne $u.CreatedUtc -and $u.CreatedUtc -lt $wtCreated) { $exempt++; continue }
        if ($idx.CwdProbe -eq 'ok' -and $u.Why -eq 'cwd-unreadable') {
            # One SCM query per snapshot, cached on the index; a failed query exempts nothing.
            if (-not $idx.PSObject.Properties['ServiceOwners']) {
                $skip = New-Object System.Collections.Generic.List[string]
                $so = try { [pscustomobject]@{ Probe = 'scm'; ByPid = (Get-ServiceAccountByPid -Skipped $skip); Skipped = $skip; KeyTimes = @{} } }
                      catch { [pscustomobject]@{ Probe = 'scm-failed'; ByPid = @{}; Skipped = $skip; KeyTimes = @{} } }
                $idx | Add-Member -NotePropertyName ServiceOwners -NotePropertyValue $so -Force
            }
            $ownerProbe = $idx.ServiceOwners.Probe
            $ownerSkipped = @($idx.ServiceOwners.Skipped)
            $o = $idx.ServiceOwners.ByPid[[int]$u.ProcessId]
            if ($o -and [string]$o.Image -ieq [string]$u.Name) {
                $unbound = if ($null -eq $u.CreatedUtc) { 'scm-process-time-unreadable' } else { $null }
                foreach ($n in @($o.Names)) {
                    if ($unbound) { break }
                    if (-not $idx.ServiceOwners.KeyTimes.ContainsKey($n)) { $idx.ServiceOwners.KeyTimes[$n] = Get-ServiceKeyLastWriteUtc -Name $n }
                    $kt = $idx.ServiceOwners.KeyTimes[$n]
                    if ($null -eq $kt) { $unbound = "scm-config-time-unreadable:$n" }
                    elseif ([datetime]$kt -ge $u.CreatedUtc) { $unbound = "scm-config-newer-than-process:$n" }
                }
                if (-not $unbound) { $owner += "$($u.ProcessId) $($u.Name) exempt=service-sid:$($o.Sid) via=scm:$($o.Services) bound=config-older-than-process"; continue }
                # A copy: the index row is shared by every worktree of a sweep and must keep Why=cwd-unreadable.
                $u = [pscustomobject]@{ ProcessId = $u.ProcessId; Name = $u.Name; Why = $unbound; CreatedUtc = $u.CreatedUtc }
            }
        }
        $unk += $u
    }
    $made = if ($null -ne $wtCreated) { ([datetime]$wtCreated).ToString('o') } else { $null }
    $describe = { param($u) $t = if ($u.CreatedUtc) { ([datetime]$u.CreatedUtc).ToString('o') } else { 'unreadable' }; "$($u.ProcessId) $($u.Name) $($u.Why) created=$t" }
    $deferred = @()
    if ($DeferUnreadableCwd -and $idx.CwdProbe -eq 'ok') {
        $deferred = @($unk | Where-Object { $_.Why -ne 'relative-script-unresolved' -and $_.Why -ne 'encoded-command-undecodable' })
        $unk = @($unk | Where-Object { $_.Why -eq 'relative-script-unresolved' -or $_.Why -eq 'encoded-command-undecodable' })
    }
    $res = [pscustomobject]@{ Probe = [string]$idx.CwdProbe; Reason = $null; Rows = @(); Unknown = @(); Exempt = $exempt; WorktreeCreatedUtc = $made
                              ExemptOwner = $owner; OwnerProbe = $ownerProbe; OwnerSkipped = $ownerSkipped
                              Deferred = @($deferred | ForEach-Object { & $describe $_ }) }
    if (-not $unk.Count) { return $res }
    $first = ($unk | Select-Object -First 5 | ForEach-Object { "$($_.ProcessId) $($_.Name) ($($_.Why))" }) -join ', '
    $res.Reason = if ($idx.CwdProbe -ne 'ok') { "cwd-probe-unavailable: the current-directory probe cannot run, so $($unk.Count) process(es) cannot be ruled out as holders, first: $first" }
                  else { "cwd-unknown: $($unk.Count) process(es) with an unreadable current directory, created at/after the worktree or with no readable creation time, cannot be ruled out as holders, first: $first" }
    $via = if ($idx.CwdProbe -ne 'ok') { 'cwd-probe-unavailable' } else { 'cwd-unknown' }
    if (@($unk | Where-Object { $_.Why -eq 'encoded-command-undecodable' }).Count) {
        $res.Reason += ' (encoded-command-undecodable: an -EncodedCommand argument that does not decode as UTF-16LE base64 is never treated as "not a holder")'
    }
    $res.Rows = @($unk | Select-Object -First 20 | ForEach-Object { [pscustomobject]@{ ProcessId = $_.ProcessId; Name = $_.Name; Via = $(if ($_.Why -eq 'encoded-command-undecodable') { 'encoded-command-undecodable' } else { $via }) } })
    $res.Unknown = @($unk | ForEach-Object { & $describe $_ })
    return $res
}

function Move-LaneWorktreeRoot {
    # r7: rename a worktree root. Returns $null on success, else the failure message. NTFS refuses the rename while any handle is
    # open beneath the directory; that refusal IS the holder test. A seam: a test wraps it to act between the probe and the removal.
    param([string]$From, [string]$To)
    try { [IO.Directory]::Move($From, $To); return $null } catch { return [string]$_.Exception.Message }
}

function Repair-LaneWorktreePath {
    # r7: after a worktree root is renamed, repoint git's admin entry (<common>\worktrees\<name>\gitdir) at its new location, then
    # confirm `git worktree list` shows exactly that path. Returns $null on success, else the failure message. A seam for tests.
    param([string]$Main, [string]$Path)
    $o = @(& git.exe -c core.longpaths=true -C $Main worktree repair $Path 2>&1 | ForEach-Object { "$_" })
    if ($LASTEXITCODE) { return 'git worktree repair: ' + ($o -join ' | ') }
    $wl = @(& git.exe -C $Main worktree list --porcelain 2>&1 | ForEach-Object { "$_" })
    if ($LASTEXITCODE) { return 'git worktree list after repair: ' + ($wl -join ' | ') }
    $want = ($Path -replace '/', '\').TrimEnd('\')
    $listed = @($wl | Where-Object { $_ -like 'worktree *' } | Where-Object { (($_.Substring(9) -replace '/', '\').TrimEnd('\')) -ieq $want })
    if (-not $listed.Count) { return "git worktree list does not show $want after the repair" + $(if ($o.Count) { ': ' + ($o -join ' | ') } else { '' }) }
    return $null
}

function New-RetirePendingRecord {
    # r8: written BEFORE the rename probe renames anything; it is the proof that THIS tool renamed <root> to <root>.retiring. It also holds
    # what a restore needs after `git worktree remove` half-fails (git deletes the admin entry and the `.git` link even when files stay):
    # a copy of the admin entry and the `.git` link text. Returns { Dir; Record; Lock }; throws on any failure, leaving no partial record.
    # r9: Lock is the open owner.lock (see OWNERSHIP in the header): the caller holds it until its final decision and hands it to
    # Restore-LaneWorktreeRename or Remove-RetirePendingRecord, which close it. The directory name ends in a random suffix, so two calls in
    # the same millisecond never share (or clean up) one record. createdUtcTicks is the root's CreationTimeUtc, which a rename keeps: it is
    # how a restorer recognises this tree at <original> once a `.git` link git half-removed can no longer say so. ownerPid / ownerStartUtc
    # are diagnostics only; ownership is the lock.
    param([string]$CommonDir, [string]$Main, [string]$Original, [string]$Retiring, [string]$AdminDir)
    $dir = Join-Path (Join-Path $CommonDir $script:RetirePendingDirName) ('{0}-{1}-{2}' -f (Split-Path $AdminDir -Leaf), (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ'), [guid]::NewGuid().ToString('N').Substring(0, 8))
    $lock = $null
    try {
        New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
        $lock = [IO.File]::Open((Join-Path $dir $script:RetirePendingLockName), [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        Copy-Item -LiteralPath $AdminDir -Destination (Join-Path $dir 'admin') -Recurse -ErrorAction Stop
        $start = try { (Get-Process -Id $PID -ErrorAction Stop).StartTime.ToUniversalTime().ToString('o') } catch { $null }
        $rec = [ordered]@{
            schema = 'mlv-app/retire-rename-record/v1'; original = $Original; retiring = $Retiring; main = $Main; adminDir = $AdminDir
            gitLink = [IO.File]::ReadAllText((Join-Path $Original '.git')); utc = (Get-Date).ToUniversalTime().ToString('o')
            createdUtcTicks = [string](Get-Item -LiteralPath $Original -Force -ErrorAction Stop).CreationTimeUtc.Ticks; ownerPid = $PID; ownerStartUtc = $start
        }
        [IO.File]::WriteAllText((Join-Path $dir 'record.json'), ($rec | ConvertTo-Json))
    } catch { Remove-RetirePendingRecord -Dir $dir -Claim $lock; throw }
    return [pscustomobject]@{ Dir = $dir; Record = [pscustomobject]$rec; Lock = $lock }
}

function Write-RetirePendingRecordFile {
    # r10: rewrite record.json of a record the caller has CLAIMED (holds its owner.lock). Written beside it, then moved over it (one rename on
    # the same volume), so a reader sees the old or the new record, never a torn one. Throws on failure; the old record.json then stays.
    param([string]$Dir, [object]$Record)
    $dest = Join-Path $Dir 'record.json'
    $tmp = "$dest.$([guid]::NewGuid().ToString('N').Substring(0, 8)).tmp"
    try { [IO.File]::WriteAllText($tmp, ($Record | ConvertTo-Json)); [IO.File]::Move($tmp, $dest, $true) }
    finally { if (Test-Path -LiteralPath $tmp) { try { [IO.File]::Delete($tmp) } catch { } } }
}

function Remove-RetirePendingRecord {
    # r8: drop a rename record once its rename is settled (retired or restored). Best effort.
    # r9: -Claim is the caller's open owner.lock. Everything but the lock goes first, WHILE it is held, so a restorer that claims the lock in
    # the instant between its release and the directory delete finds no record.json and only finishes the delete.
    param([string]$Dir, [object]$Claim = $null)
    if ($Dir -and (Test-Path -LiteralPath $Dir)) {
        foreach ($e in @(Get-ChildItem -LiteralPath $Dir -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne $script:RetirePendingLockName })) {
            try { if ($e.PSIsContainer) { [IO.Directory]::Delete($e.FullName, $true) } else { [IO.File]::Delete($e.FullName) } } catch { }
        }
    }
    Close-RetirePendingClaim $Claim
    if ($Dir -and (Test-Path -LiteralPath $Dir)) { try { [IO.Directory]::Delete($Dir, $true) } catch { } }
}

function Enter-RetirePendingRecord {
    # r9: claim a rename record: open its owner.lock exclusively. Returns the open stream (the caller now owns the record and must close it),
    # or $null when another live operation holds it (a sharing or lock violation). Any other failure throws.
    param([string]$Dir)
    try { return [IO.File]::Open((Join-Path $Dir $script:RetirePendingLockName), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch {
        $ex = $_.Exception; while ($ex.InnerException) { $ex = $ex.InnerException }
        if ($ex -is [IO.IOException] -and -not ($ex -is [IO.DirectoryNotFoundException]) -and (($ex.HResult -band 0xFFFF) -in @(32, 33))) { return $null }
        throw
    }
}

function Close-RetirePendingClaim {
    # r9: release a claim without dropping the record (the next sweep may take it). Never throws.
    param([object]$Claim)
    if ($Claim) { try { $Claim.Dispose() } catch { } }
}

function Get-RetireRecordValue {
    # A record field, or $null when an older record does not have it (StrictMode Latest throws on a missing property).
    param([object]$Record, [string]$Name)
    $p = $Record.PSObject.Properties[$Name]
    if ($p) { return $p.Value }
    return $null
}

function Test-RenamedTreeIdentity {
    # r9: is the directory at <original> the very tree this record renamed? A `.git` link equal to the recorded one says yes, a different
    # one or a `.git` directory says no. With no `.git` link at all (git half-removed it), the root's CreationTimeUtc, which a rename keeps,
    # must equal the recorded one; a record without it cannot tell, so the answer is no.
    param([object]$Record)
    $orig = [string]$Record.original
    $link = Join-Path $orig '.git'
    if (Test-Path -LiteralPath $link -PathType Leaf) { return ([IO.File]::ReadAllText($link) -eq [string]$Record.gitLink) }
    if (Test-Path -LiteralPath $link) { return $false }
    $ticks = Get-RetireRecordValue $Record 'createdUtcTicks'
    if (-not $ticks) { return $false }
    return ((Get-Item -LiteralPath $orig -Force -ErrorAction Stop).CreationTimeUtc.Ticks -eq [long]$ticks)
}

function Get-RetirePendingRecords {
    # r8: every rename record under <CommonDir>\mlv-retire-pending as { Dir; Record; Error } (Record = $null when record.json is unreadable).
    param([string]$CommonDir)
    $root = Join-Path $CommonDir $script:RetirePendingDirName
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction Stop | Sort-Object Name | ForEach-Object {
        $rec = $null; $err = $null
        try { $rec = Get-Content -LiteralPath (Join-Path $_.FullName 'record.json') -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { $err = $_.Exception.Message }
        [pscustomobject]@{ Dir = $_.FullName; Record = $rec; Error = $err }
    })
}

function Restore-LaneWorktreeRename {
    # r8 (sol r2 blocker on PR #603): put <root>.retiring back at <root>, from its rename record. Called by the retire call itself (its finally)
    # and, for a rename an earlier call could not undo, by the next call or sweep before any gate decision. Never throws.
    # state: restored | restored-unrepaired | failed (the rename back kept failing; the record stays for the next try) | refused (<root> exists
    # again; neither path is touched) | gone (nothing left to restore; the record is dropped) | owned (another live operation holds it).
    # The rename back is retried like the removal: only a failure matching RemoveRetryablePattern (a handle still open beneath it), bounded by
    # RestoreRetryBudgetMs and RestoreRetryMaxAttempts. Once back, a registration a half-failed `git worktree remove` deleted is put back: the
    # admin entry from the record's copy, the `.git` link, `git worktree repair`, then `git checkout-index -a`, which re-creates only tracked
    # files that are MISSING and never overwrites one that exists, so the tree is again the clean, merged checkout the gate passed.
    # r9 OWNERSHIP: -Claim is the caller's open owner.lock (the retire call passes the one it has held since before its rename). Without it the
    # record is claimed here first, and a record another live operation holds is 'owned': neither path nor the record is touched. The claim is
    # always closed (or the record dropped with it) before this returns. -BudgetMs (> 0) caps the rename-back retry below RestoreRetryBudgetMs
    # (the sweep passes what is left of -BudgetSeconds). <root>.retiring gone with <root> present is the rename back already done (by an
    # earlier call that could not repair, or by another actor): if Test-RenamedTreeIdentity says it is this record's tree, the registration
    # steps below run on it; otherwise the record is 'gone'. A failed repair is 'restored-unrepaired': the tree is back but git does not list
    # it there, so the record stays and the next call or sweep repairs it.
    param([object]$Record, [string]$RecordDir, [object]$Claim = $null, [int]$BudgetMs = 0)
    $res = [ordered]@{ state = 'failed'; note = $null; attempts = 0 }
    $drop = $false
    try {
        if (-not $Claim) {
            if (-not (Test-Path -LiteralPath $RecordDir)) { $res.state = 'gone'; $res.note = "the rename record $RecordDir no longer exists"; return $res }
            $Claim = Enter-RetirePendingRecord $RecordDir
            if (-not $Claim) {
                $op = if ($null -ne $Record) { Get-RetireRecordValue $Record 'ownerPid' }
                $res.state = 'owned'; $res.note = "held by a live operation$(if ($op) { " (record written by pid $op)" }): its owner.lock is open, so neither path nor the record is touched"
                return $res
            }
            # The owner empties its record before releasing it (Remove-RetirePendingRecord); a claim taken in between finds no record.json.
            if (-not (Test-Path -LiteralPath (Join-Path $RecordDir 'record.json'))) { $res.state = 'gone'; $res.note = "the rename record $RecordDir was settled by its owner"; $drop = $true; return $res }
            # r10: -Record $null is the sweep's record whose record.json was absent at its read; present now, its owner wrote it after the
            # listing (under the lock it has since let go). It was not read in this pass, so nothing is touched; the next sweep reads it.
            if ($null -eq $Record) { $res.state = 'owned'; $res.note = "record.json of $RecordDir was written by its owner after this sweep listed it; nothing touched, the next sweep reads it"; return $res }
        }
        $orig = [string]$Record.original; $ret = [string]$Record.retiring
        $notes = @()
        if (-not (Test-Path -LiteralPath $ret)) {
            if (-not (Test-Path -LiteralPath $orig)) { $res.state = 'gone'; $res.note = "$ret no longer exists"; $drop = $true; return $res }
            if (-not (Test-RenamedTreeIdentity $Record)) { $res.state = 'gone'; $res.note = "$ret no longer exists and $orig is not the tree this record renamed"; $drop = $true; return $res }
            $notes += "$ret is already back at $orig"
        } else {
            if (Test-Path -LiteralPath $orig) { $res.state = 'refused'; $res.note = "$orig exists again; $ret is not moved over it (both left as they are)"; return $res }
            $budget = if ($BudgetMs -gt 0) { [Math]::Min($BudgetMs, $script:RestoreRetryBudgetMs) } else { $script:RestoreRetryBudgetMs }
            $sw = [Diagnostics.Stopwatch]::StartNew()
            while ($true) {
                $res.attempts++
                $e = Move-LaneWorktreeRoot -From $ret -To $orig
                if (-not $e -or $e -notmatch $script:RemoveRetryablePattern) { break }
                if ($sw.ElapsedMilliseconds -ge $budget -or $res.attempts -ge $script:RestoreRetryMaxAttempts) { break }
                Start-Sleep -Milliseconds $script:RestoreRetryDelayMs
            }
            if ($e) {
                $res.note = "rename back to $orig failed after $($res.attempts) attempt(s) over $($sw.ElapsedMilliseconds) ms; budget $budget ms ($e); the directory stays at $ret and the next call or the next sweep restores it first"
                return $res
            }
        }
        $admin = [string]$Record.adminDir
        $link = Join-Path $orig '.git'
        $needAdmin = -not (Test-Path -LiteralPath $admin); $needLink = -not (Test-Path -LiteralPath $link)
        # r10 (Dot R18 B1, RETIRE-RESTORE-PENDING-CHECKOUT-PERSIST-1): re-creating the admin entry or the link means git half-removed the tree,
        # so tracked files it deleted are owed a checkout-index. That debt goes into the record (under this call's claim) BEFORE either is
        # re-created: a repair that fails leaves both present, and the retry, finding nothing to re-create, would otherwise skip the re-checkout
        # and drop the record over a tree with tracked files missing.
        $needCheckout = [bool](Get-RetireRecordValue $Record 'checkoutOwed')
        if (($needAdmin -or $needLink) -and -not $needCheckout) {
            $Record | Add-Member -NotePropertyName checkoutOwed -NotePropertyValue $true -Force
            Write-RetirePendingRecordFile -Dir $RecordDir -Record $Record
            $needCheckout = $true
        }
        if ($needAdmin) {
            New-Item -ItemType Directory -Force -Path (Split-Path $admin -Parent) -ErrorAction Stop | Out-Null
            Copy-Item -LiteralPath (Join-Path $RecordDir 'admin') -Destination $admin -Recurse -ErrorAction Stop
            $notes += 'git admin entry restored from the pre-rename copy'
        }
        if ($needLink) { [IO.File]::WriteAllText($link, [string]$Record.gitLink); $notes += '.git link rewritten' }
        $fix = Repair-LaneWorktreePath -Main ([string]$Record.main) -Path $orig
        if ($fix) {
            # H2 (RETIRE-RESTORE-REPAIR-FAILED-STATE-1): back at its path but not registered there. Keep the record: the next call recognises
            # the tree at <original> (Test-RenamedTreeIdentity) and repairs it, instead of marking the record gone over an unregistered tree.
            $notes += "git repoint after the rename back: $fix"
            $res.state = 'restored-unrepaired'; $res.note = ($notes -join '; ') + '; the rename record stays and the next call or sweep repairs it'
            return $res
        }
        if ($needCheckout) {
            # r10: a re-checkout that did not run to the end is not 'restored'. It is restored-unrepaired, the state that already means "back at
            # <root>, the record stays and the next call or sweep finishes it"; the record keeps checkoutOwed, so that call runs this again.
            $co = @(& git.exe -c core.longpaths=true -C $orig checkout-index -a -q 2>&1 | ForEach-Object { "$_" })
            if ($LASTEXITCODE) { $notes += 'checkout-index after the re-registration failed: ' + ($co -join ' | ') }
            else {
                $st = @(& git.exe -c core.quotepath=false -C $orig status --porcelain 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ })
                if ($LASTEXITCODE) { $notes += 'status after the re-checkout failed: ' + ($st -join ' | ') }
                else {
                    $notes += $(if ($st.Count) { "$($st.Count) path(s) still differ from HEAD after the re-checkout, first: $($st[0])" }
                                else { 'missing tracked files re-checked-out; status clean' })
                    $needCheckout = $false
                }
            }
            if ($needCheckout) {
                $res.state = 'restored-unrepaired'; $res.note = ($notes -join '; ') + '; the rename record stays and the next call or sweep re-checks it out'
                return $res
            }
        }
        $res.state = 'restored'; $res.note = $(if ($notes.Count) { $notes -join '; ' } else { $null })
        $drop = $true
    } catch { $res.state = 'failed'; $res.note = "cannot-determine: $($_.Exception.Message)"; $drop = $false }
    finally {
        if ($drop) { Remove-RetirePendingRecord -Dir $RecordDir -Claim $Claim } else { Close-RetirePendingClaim $Claim }
    }
    return $res
}

function Restore-StrandedRetiring {
    # r8: <Original> is absent and <Original>.retiring exists. Restore it if a rename record of this repository names it; otherwise this tool
    # did not rename it and nothing is touched (state refused). Records are looked up under the git common dir of -RepoRoot, else of the
    # .retiring tree itself while it still has its `.git` link.
    param([string]$Original, [string]$RepoRoot)
    $ret = $Original + '.retiring'
    $common = $null
    foreach ($c in @($RepoRoot, $(if (Test-Path -LiteralPath (Join-Path $ret '.git')) { $ret }))) {
        if (-not $c) { continue }
        $o = @(& git.exe -C $c rev-parse --path-format=absolute --git-common-dir 2>$null)
        if (-not $LASTEXITCODE -and $o.Count) { $common = ($o[0] -replace '/', '\'); break }
    }
    if (-not $common) { return [ordered]@{ state = 'refused'; note = "$ret exists but there is no repository to find its rename record in (pass -RepoRoot); not touched"; attempts = 0 } }
    $hit = @(Get-RetirePendingRecords -CommonDir $common | Where-Object { $_.Record -and ([string]$_.Record.original).TrimEnd('\') -ieq $Original })
    if (-not $hit.Count) { return [ordered]@{ state = 'refused'; note = "$ret exists but no rename record names it, so this tool did not rename it; not touched"; attempts = 0 } }
    return Restore-LaneWorktreeRename -Record $hit[-1].Record -RecordDir $hit[-1].Dir
}

function Write-RefusedHolders {
    # One REFUSED line per holder pid, on stderr (stdout carries the caller's pipeline objects).
    param([string]$WorktreePath, [object[]]$Holders)
    foreach ($h in $Holders) {
        try { [Console]::Error.WriteLine("REFUSED worktree-removal path=$WorktreePath pid=$($h.ProcessId) name=$($h.Name) via=$($h.Via) utc=$((Get-Date).ToUniversalTime().ToString('o'))") } catch { }
    }
}

function Invoke-RetireLaneWorktree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [string]$MergeTarget = 'master',
        [string]$QuarantineRoot = '',
        [string[]]$ProtectPath = @(),
        # Optional output of Get-LaneProcessSnapshot ({Procs; SelfPids}). Without it the gate takes its
        # own snapshot, so a lone caller behaves exactly as before; the sweep passes one so N worktrees
        # cost one process scan instead of N.
        [object]$ProcessSnapshot = $null,
        # r8: where to look for the rename record of a stranded <WorkDir>.retiring whose own `.git` link is gone (the sweep passes its -RepoRoot).
        [string]$RepoRoot = '',
        [switch]$WhatIf
    )
    $d = [ordered]@{
        schema = 'mlv-app/lane-worktree-disposition/v1'; workDir = $WorkDir; action = 'kept'; reason = $null
        head = $null; branch = $null; quarantined = @(); holders = @(); cwdProbe = $null; cwdUnknown = @()
        cwdExempt = 0; worktreeCreatedUtc = $null; cwdExemptOwner = @(); cwdOwnerProbe = $null; cwdOwnerSkipped = @()
        # LANE-FSMONITOR-PIN-AND-RETIRE-HARDENING-1 r2: what each fsmonitor step did, as 'action' or 'action: reason' ('stopped', 'none: no daemon',
        # 'failed: ...', 'not-run: ...'). Best effort: never changes `action`/`reason`, but a 'failed' here means a daemon may still be running.
        fsmonitorStop = [ordered]@{ disable = $null; early = $null; remove = $null; final = $null }
        # CI-FLAKE-RETIRE-HOLDER-EXIT-PERMISSION-DENIED-1: how many removal attempts ran (0 = never reached) and one note per failed attempt.
        removeAttempts = 0; removeNotes = @()
        # r7 (RENAME PROBE): 'not-run' | 'not-run: WhatIf ...' | 'ok' | 'in-use: <why>'. renameRestore is a nested table on purpose: the finally
        # block fills it after a `return` has already copied the top-level fields (states: see OWNERSHIP in the header, 'retired' included).
        # r8: renameRestore.attempts counts the rename-back tries; strandedRestore is $null unless WorkDir was absent with a <WorkDir>.retiring
        # left by an earlier call, which is restored (Restore-StrandedRetiring) before any gate decision.
        renameProbe = 'not-run'; renameRestore = [ordered]@{ state = 'not-renamed'; note = $null; attempts = 0 }; strandedRestore = $null
        utc = (Get-Date).ToUniversalTime().ToString('o')
    }
    function Get-FsmonitorStepNote([object]$Result) {
        if ($null -eq $Result) { return 'not-run: helper not loaded' }
        if ($Result.reason) { return "$($Result.action): $($Result.reason)" }
        return [string]$Result.action
    }
    # Fail closed on the cwd probe's own blind spots (see CWD PROBE IS LOUD): true = refused, disposition filled in.
    function Test-CwdRefusal([object]$Sn) {
        $r = Get-CwdUnknownRefusal -Snapshot $Sn -WorktreePath $wd -DeferUnreadableCwd
        $d.cwdProbe = [string]$r.Probe
        # r7: unreadable-cwd pids are evidence, not a refusal; the rename probe settles them.
        $d.cwdUnknown = @($r.Deferred) + @($r.Unknown)
        $d.cwdExempt = [int]$r.Exempt
        $d.worktreeCreatedUtc = $r.WorktreeCreatedUtc
        $d.cwdExemptOwner = @($r.ExemptOwner)
        $d.cwdOwnerProbe = $r.OwnerProbe
        $d.cwdOwnerSkipped = @($r.OwnerSkipped)
        if (-not @($r.Unknown).Count) { return $false }
        Write-RefusedHolders -WorktreePath $wd -Holders $r.Rows
        $d.reason = $r.Reason
        return $true
    }
    function Run-Git([string]$C, [string[]]$A) {
        $o = & git.exe -C $C @A 2>&1
        [pscustomobject]@{ code = $LASTEXITCODE; out = @($o | ForEach-Object { "$_" }) }
    }
    $wd = $null; $origWd = $null; $renamed = $false; $rec = $null
    try {
        # r8: a <root>.retiring is a rename-probe leftover, never a retirement candidate; only its restore (below, or in the sweep) touches it.
        if ($WorkDir -match '\.retiring[\\/]*$') { $d.reason = "stranded-retiring: $WorkDir is a rename-probe leftover, never a retirement candidate; it is restored to its own path when a rename record names it"; return [pscustomobject]$d }
        if (-not (Test-Path -LiteralPath $WorkDir -PathType Container)) {
            $full = [IO.Path]::GetFullPath(($WorkDir -replace '/', '\')).TrimEnd('\')
            if (-not (Test-Path -LiteralPath ($full + '.retiring') -PathType Container)) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: path absent'; return [pscustomobject]$d }
            $d.strandedRestore = if ($WhatIf) { [ordered]@{ state = 'not-run: WhatIf'; note = 'a real call restores it first'; attempts = 0 } }
                                 else { Restore-StrandedRetiring -Original $full -RepoRoot $RepoRoot }
            if ($d.strandedRestore.state -ne 'restored') { $d.reason = "stranded-retiring: $full.retiring $($d.strandedRestore.state): $($d.strandedRestore.note)"; return [pscustomobject]$d }
        }
        $wd = (Resolve-Path -LiteralPath $WorkDir).Path.TrimEnd('\')
        $gd = Run-Git $wd @('rev-parse', '--path-format=absolute', '--git-dir')
        $cd = Run-Git $wd @('rev-parse', '--path-format=absolute', '--git-common-dir')
        $top = Run-Git $wd @('rev-parse', '--show-toplevel')
        if ($gd.code -or $cd.code -or $top.code) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: not a git checkout'; return [pscustomobject]$d }
        if ($gd.out[0] -eq $cd.out[0]) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: main checkout'; return [pscustomobject]$d }
        if (($top.out[0] -replace '/', '\').TrimEnd('\') -ne $wd) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: WorkDir is a subdirectory'; return [pscustomobject]$d }
        $main = Split-Path -Parent ($cd.out[0] -replace '/', '\')
        # HOST-FSMONITOR-DAEMON-LEAK-1 (a): off BEFORE the gate's own `status` calls, which would otherwise start a daemon in a worktree kept for any reason.
        $disableResult = $null
        if (Get-Command Disable-LaneWorktreeFsmonitor -ErrorAction SilentlyContinue) { $disableResult = Disable-LaneWorktreeFsmonitor -WorkDir $wd }
        $d.fsmonitorStop.disable = Get-FsmonitorStepNote $disableResult
        # LANE-FSMONITOR-PIN-AND-RETIRE-HARDENING-1: a daemon already running here (a worktree that predates the fix) survives every path below that KEEPS the
        # worktree, so stop it now, before the gate. The finally further down repeats it only when the disable did not take (skipped or failed): a
        # disabled worktree cannot have a daemon restarted by the gate's own status.
        $stopResult = $null
        if (Get-Command Stop-LaneWorktreeFsmonitor -ErrorAction SilentlyContinue) { $stopResult = Stop-LaneWorktreeFsmonitor -WorkDir $wd }
        $d.fsmonitorStop.early = Get-FsmonitorStepNote $stopResult

        $h = Run-Git $wd @('rev-parse', 'HEAD'); if ($h.code) { $d.reason = 'cannot-determine: rev-parse HEAD'; return [pscustomobject]$d }
        $d.head = $h.out[0]
        $b = Run-Git $wd @('symbolic-ref', '-q', 'HEAD'); if ($b.code -eq 0) { $d.branch = $b.out[0] }

        foreach ($p in $ProtectPath) {
            if ($p -and ([IO.Path]::GetFullPath($p) + '\').StartsWith($wd + '\', [StringComparison]::OrdinalIgnoreCase)) { $d.reason = "rundir-inside: $p"; return [pscustomobject]$d }
        }

        $snap = if ($null -ne $ProcessSnapshot) { $ProcessSnapshot } else { Get-LaneProcessSnapshot }
        $live = @(Find-WorktreeHolders -WorktreePath $wd -Snapshot $snap)
        $d.cwdProbe = [string](Get-LaneHolderIndex -Snapshot $snap).CwdProbe
        if ($live.Count) {
            $d.holders = @($live | ForEach-Object { "$($_.ProcessId) $($_.Name) $($_.Via)" })
            Write-RefusedHolders -WorktreePath $wd -Holders $live
            $d.reason = 'live-process: ' + (($live | ForEach-Object { "$($_.ProcessId) $($_.Name) [$($_.Via)]" }) -join ', ')
            return [pscustomobject]$d
        }

        $s = Run-Git $wd @('status', '--porcelain', '-uall'); if ($s.code) { $d.reason = 'cannot-determine: status'; return [pscustomobject]$d }
        $dirty = @($s.out | Where-Object { $_ })
        if ($dirty.Count) { $d.reason = "dirty: $($dirty.Count) path(s), first: $($dirty[0])"; return [pscustomobject]$d }

        $u = Run-Git $main @('rev-list', '--count', $d.head, '--not', '--remotes'); if ($u.code) { $d.reason = 'cannot-determine: rev-list --not --remotes'; return [pscustomobject]$d }
        if ([int]$u.out[0] -gt 0) { $d.reason = "unpushed: $($u.out[0]) commit(s) on no remote"; return [pscustomobject]$d }

        & git.exe -C $main merge-base --is-ancestor $d.head $MergeTarget 2>$null
        switch ($LASTEXITCODE) { 0 { } 1 { $d.reason = "unmerged: HEAD not an ancestor of $MergeTarget"; return [pscustomobject]$d } default { $d.reason = "cannot-determine: merge-base ($MergeTarget)"; return [pscustomobject]$d } }

        $st = Run-Git $main @('stash', 'list'); if ($st.code) { $d.reason = 'cannot-determine: stash list'; return [pscustomobject]$d }
        if (@($st.out | Where-Object { $_ }).Count) { $d.reason = 'stash: repository stash list is not empty'; return [pscustomobject]$d }

        $wl = Run-Git $main @('worktree', 'list', '--porcelain'); if ($wl.code) { $d.reason = 'cannot-determine: worktree list'; return [pscustomobject]$d }
        $nested = @($wl.out | Where-Object { $_ -like 'worktree *' } | ForEach-Object { ($_.Substring(9) -replace '/', '\').TrimEnd('\') } | Where-Object { $_.StartsWith($wd + '\', [StringComparison]::OrdinalIgnoreCase) })
        if ($nested.Count) { $d.reason = "nested-worktree: $($nested -join ', ')"; return [pscustomobject]$d }

        $ign = Run-Git $wd @('-c', 'core.quotepath=false', 'status', '--porcelain', '--ignored', '-unormal'); if ($ign.code) { $d.reason = 'cannot-determine: ignored listing'; return [pscustomobject]$d }
        $keep = @($ign.out | Where-Object { $_ -like '!!*' } | ForEach-Object { $_.Substring(3) } | Where-Object { $_ -notmatch $script:RetireDebrisPattern })
        if ($keep.Count -and -not $QuarantineRoot) { $d.reason = "cannot-determine: $($keep.Count) ignored non-debris entr(y/ies) and no -QuarantineRoot"; return [pscustomobject]$d }
        if (Test-CwdRefusal $snap) { return [pscustomobject]$d }
        if ($WhatIf) {
            $d.action = 'would-retire'; $d.reason = 'ok'; $d.quarantined = $keep
            $d.renameProbe = 'not-run: WhatIf (a real retire renames the root to <root>.retiring first; an open handle there keeps it as in-use)'
            return [pscustomobject]$d
        }

        # Last look before anything is moved or deleted, on a FRESH scan: the gate above took seconds (a sweep reuses a
        # snapshot up to 30 s old), and a holder that started since is not in it. A failed scan is not an all-clear.
        $fresh = try { Get-LaneProcessSnapshot } catch { $null }
        if ($null -eq $fresh) { $d.reason = 'cannot-determine: process rescan before removal'; return [pscustomobject]$d }
        $live = @(Find-WorktreeHolders -WorktreePath $wd -Snapshot $fresh)
        if ($live.Count) {
            $d.holders = @($live | ForEach-Object { "$($_.ProcessId) $($_.Name) $($_.Via)" })
            Write-RefusedHolders -WorktreePath $wd -Holders $live
            $d.reason = 'live-process: ' + (($live | ForEach-Object { "$($_.ProcessId) $($_.Name) [$($_.Via)]" }) -join ', ')
            return [pscustomobject]$d
        }
        if (Test-CwdRefusal $fresh) { return [pscustomobject]$d }

        # r7 RENAME PROBE (see the header): the file system, not the ACL of a holder, says whether anything is open beneath the root.
        $origWd = $wd
        $retiring = $wd + '.retiring'
        if (Test-Path -LiteralPath $retiring) { $d.reason = "cannot-determine: $retiring already exists (left by an earlier run); not overwritten"; return [pscustomobject]$d }
        # r8: the rename record first, so a rename this call cannot undo is still restorable by the next call or sweep. No record, no rename.
        try { $rec = New-RetirePendingRecord -CommonDir ($cd.out[0] -replace '/', '\') -Main $main -Original $wd -Retiring $retiring -AdminDir ($gd.out[0] -replace '/', '\') }
        catch { $d.reason = "cannot-determine: the rename record could not be written ($($_.Exception.Message)); nothing renamed"; return [pscustomobject]$d }
        $mv = Move-LaneWorktreeRoot -From $wd -To $retiring
        if ($mv) {
            Remove-RetirePendingRecord -Dir $rec.Dir -Claim $rec.Lock; $rec.Lock = $null
            $d.renameProbe = 'in-use: ' + $mv
            $d.reason = if ($mv -match $script:RemoveRetryablePattern) { "in-use: the worktree root cannot be renamed to $retiring, so a handle is open beneath it ($mv)" }
                        else { "cannot-determine: renaming the worktree root to $retiring failed ($mv)" }
            return [pscustomobject]$d
        }
        $renamed = $true; $wd = $retiring
        $d.renameProbe = 'ok'
        $fix = Repair-LaneWorktreePath -Main $main -Path $wd
        if ($fix) { $d.reason = "cannot-determine: git could not be repointed at the renamed worktree ($fix)"; return [pscustomobject]$d }

        # <leaf>-<utc stamp>: two same-named worktrees retired the same day must not nest into each other.
        $qDir = Join-Path $QuarantineRoot ('{0}-{1}' -f (Split-Path $origWd -Leaf), (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ'))
        foreach ($rel in $keep) {
            $relWin = $rel.TrimEnd('/') -replace '/', '\'
            $dst = Join-Path $qDir $relWin
            New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
            Move-Item -LiteralPath (Join-Path $wd $relWin) -Destination $dst -ErrorAction Stop
            $d.quarantined += $dst
        }
        $c = Run-Git $wd @('clean', '-fdX'); if ($c.code) { $d.reason = 'remove-failed: git clean -fdX: ' + ($c.out -join ' | '); return [pscustomobject]$d }
        # HOST-FSMONITOR-DAEMON-LEAK-1 (b): ask this worktree's daemon to exit before its directory goes. Best effort; the result never changes the disposition.
        $removeStop = $null
        if (Get-Command Stop-LaneWorktreeFsmonitor -ErrorAction SilentlyContinue) { $removeStop = Stop-LaneWorktreeFsmonitor -WorkDir $wd }
        $d.fsmonitorStop.remove = Get-FsmonitorStepNote $removeStop
        $removeArgs = @('-c', 'core.longpaths=true', 'worktree', 'remove', $wd)
        $r = Run-Git $main $removeArgs
        $d.removeAttempts = 1
        $last = if ($r.code) { $r.out -join ' | ' } elseif (Test-Path -LiteralPath $wd) { 'Permission denied: worktree remove exited 0 but the directory is still there' } else { $null }
        if ($last) {
            # CI-FLAKE-RETIRE-HOLDER-EXIT-PERMISSION-DENIED-1: the gate saw no holder, yet a handle can still be open (an exited holder's, Defender's).
            # Only an access-denied / sharing-violation failure is retried; anything else is the answer as it stands.
            if ($last -notmatch $script:RemoveRetryablePattern) { $d.reason = 'remove-failed: ' + $last; return [pscustomobject]$d }
            # git half-fails here (TRAPS, worktree removal on Windows): it deregisters the worktree and deletes its `.git` link, then stops with
            # files left behind, so the same command cannot be re-run ("is not a working tree"). Each round therefore asks what state git left:
            # still registered -> run git again; deregistered and the directory remains -> finish the delete ourselves (nothing the gate did not
            # already clear: clean, pushed, merged, no live holder), after a fresh holder + cwd scan before EACH attempt.
            $sw = [Diagnostics.Stopwatch]::StartNew()
            while ($true) {
                $d.removeNotes += "attempt $($d.removeAttempts) at $($sw.ElapsedMilliseconds) ms: $last"
                if ($sw.ElapsedMilliseconds -ge $script:RemoveRetryBudgetMs -or $d.removeAttempts -ge $script:RemoveRetryMaxAttempts) { break }
                Start-Sleep -Milliseconds $script:RemoveRetryDelayMs
                $d.removeAttempts++
                $wl2 = Run-Git $main @('worktree', 'list', '--porcelain')
                if ($wl2.code) { $last = 'cannot-determine: worktree list: ' + ($wl2.out -join ' | '); break }
                $stillRegistered = @($wl2.out | Where-Object { $_ -like 'worktree *' } | Where-Object { (($_.Substring(9) -replace '/', '\').TrimEnd('\')) -ieq $wd }).Count -gt 0
                # Sol r1 B1/B2: EVERY attempt that may delete (git again, or our own delete) first runs the first decision's refusal set - positive
                # holders, then the cwd probe - on a snapshot taken now. A holder that started during the retry window refuses here, as it would have
                # at the gate; the worktree is kept and nothing more is deleted. A failed scan is not an all-clear.
                if ($stillRegistered -or (Test-Path -LiteralPath $wd)) {
                    $fresh2 = try { Get-LaneProcessSnapshot } catch { $null }
                    if ($null -eq $fresh2) { $last = 'cannot-determine: process rescan before the removal retry'; break }
                    $live2 = @(Find-WorktreeHolders -WorktreePath $wd -Snapshot $fresh2)
                    if ($live2.Count) {
                        $d.holders = @($live2 | ForEach-Object { "$($_.ProcessId) $($_.Name) $($_.Via)" })
                        Write-RefusedHolders -WorktreePath $wd -Holders $live2
                        $d.reason = 'live-process: ' + (($live2 | ForEach-Object { "$($_.ProcessId) $($_.Name) [$($_.Via)]" }) -join ', ')
                        return [pscustomobject]$d
                    }
                    if (Test-CwdRefusal $fresh2) { return [pscustomobject]$d }
                }
                if ($stillRegistered) {
                    $r = Run-Git $main $removeArgs
                    $last = if ($r.code) { $r.out -join ' | ' } else { $null }
                } elseif (Test-Path -LiteralPath $wd) {
                    if (Test-Path -LiteralPath (Join-Path $wd '.git')) { $last = 'a .git entry is still in a deregistered directory; not deleting it'; break }
                    $last = try { [IO.Directory]::Delete($wd, $true); $null } catch { $_.Exception.Message }
                } else {
                    $last = $null
                }
                # Success is the directory being gone, never an exit code.
                if (-not (Test-Path -LiteralPath $wd)) { $last = $null; break }
                if (-not $last) { $last = 'Permission denied: the directory is still there after the removal' }
                if ($last -notmatch $script:RemoveRetryablePattern) { break }
            }
            if (Test-Path -LiteralPath $wd) {
                $d.reason = "remove-failed: $last (after $($d.removeAttempts) attempt(s) over $($sw.ElapsedMilliseconds) ms; budget $($script:RemoveRetryBudgetMs) ms)"
                return [pscustomobject]$d
            }
        }
        if (Test-Path -LiteralPath $wd) { $d.reason = 'remove-failed: the directory is still there after the removal'; return [pscustomobject]$d }
        # r9 (fable r4 + sol r4 blocker): <root>.retiring being gone is not enough. Retired only while this call still owns its rename record
        # and the original path is absent too; a tree something moved back during the removal is kept, and the finally below re-registers it
        # from this call's own record.
        if (Test-Path -LiteralPath $origWd) { $d.reason = "cannot-determine: $origWd exists again: the tree was moved back during the removal, so it is not retired"; return [pscustomobject]$d }
        if (-not $rec.Lock -or -not (Test-Path -LiteralPath (Join-Path $rec.Dir 'record.json'))) { $d.reason = "cannot-determine: the rename record $($rec.Dir) is no longer this call's, so $retiring being gone is not proof of a retirement"; return [pscustomobject]$d }
        $d.action = 'retired'; $d.reason = 'ok'; $d.renameRestore.state = 'retired'
        Remove-RetirePendingRecord -Dir $rec.Dir -Claim $rec.Lock; $rec.Lock = $null
    } catch {
        $d.action = 'kept'; $d.reason = "cannot-determine: $($_.Exception.Message)"
    } finally {
        # r7: undo the rename probe's rename for every outcome but `retired` (the early `return`s above all pass through here). The rename
        # back comes first: the fsmonitor stop below, and the caller, address the worktree by its own path. Never throws.
        # r8: Restore-LaneWorktreeRename retries the rename back within its own budget and re-registers a tree git half-removed; if the handle
        # outlives that budget too, the rename record stays and the next call or sweep restores the tree before any gate decision.
        if ($renamed -and $d.action -ne 'retired') {
            $rs = $d.renameRestore
            # r9: this call's own lock goes in, so the restore never has to claim (or contend for) the record it has owned since the rename.
            $back = Restore-LaneWorktreeRename -Record $rec.Record -RecordDir $rec.Dir -Claim $rec.Lock
            $rec.Lock = $null
            $rs.state = $back.state; $rs.note = $back.note; $rs.attempts = $back.attempts
            if ($back.state -in @('restored', 'restored-unrepaired')) { $wd = $origWd; $renamed = $false }
            if ($back.state -in @('failed', 'restored-unrepaired')) {
                $tag = if ($back.state -eq 'failed') { 'LEFT-RETIRING worktree-rename-back' } else { 'LEFT-UNREPAIRED worktree-repair-after-rename-back' }
                try { [Console]::Error.WriteLine("$tag path=$wd wanted=$origWd attempts=$($back.attempts) utc=$((Get-Date).ToUniversalTime().ToString('o')) why=$($back.note)") } catch { }
            }
        }
        # r9: a record written but never renamed under (an exception between the two) is this call's to drop; one handed on above is closed.
        if ($rec -and $rec.Lock) { Remove-RetirePendingRecord -Dir $rec.Dir -Claim $rec.Lock; $rec.Lock = $null }
        # Every path that leaves the worktree standing (kept, would-retire) tries to leave no daemon in it; a retired worktree is gone. Best effort,
        # never throws, and not a guarantee: a failed stop is recorded in fsmonitorStop, and a daemon may still be running.
        # Only when the disable did not take: after a successful disable the early stop already left none and nothing can start one.
        # LANE-FSMONITOR-FOLLOWUPS-1 (b): unless that early stop FAILED. The disable only prevents a new daemon; a daemon already running is still
        # there, so this is the one retry (bounded in count: it is this single call, never a loop; not bounded in elapsed time, because
        # Stop-LaneWorktreeFsmonitor sets no deadline of its own). Its outcome lands in `final`, the early failure stays in `early`.
        if ($wd -and $d.action -ne 'retired' -and $d.action -ne 'skipped') {
            $earlyStopFailed = ($null -ne $d.fsmonitorStop.early -and $d.fsmonitorStop.early -like 'failed*')
            if ($null -ne $d.fsmonitorStop.disable -and $d.fsmonitorStop.disable -eq 'disabled' -and -not $earlyStopFailed) {
                $d.fsmonitorStop.final = 'not-run: disable succeeded'
            } elseif (Get-Command Stop-LaneWorktreeFsmonitor -ErrorAction SilentlyContinue) {
                try { $d.fsmonitorStop.final = Get-FsmonitorStepNote (Stop-LaneWorktreeFsmonitor -WorkDir $wd) }
                catch { $d.fsmonitorStop.final = "failed: cannot-determine: $($_.Exception.Message)" }
            } else {
                $d.fsmonitorStop.final = 'not-run: helper not loaded'
            }
        }
    }
    return [pscustomobject]$d
}

function Invoke-SweepMergedLaneWorktrees {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [string]$Root = 'C:\mlvtmp',
        [string]$MergeTarget = 'fork/master',
        [string]$QuarantineRoot = '',
        [string[]]$Exclude = @(),
        [string[]]$ProtectPath = @(),
        [int]$MinIdleHours = 6,
        [int]$BudgetSeconds = 180,
        # Cap on GATE CALLS this sweep (0 = no cap). A deterministic stand-in for the wall-clock budget so
        # the fair-order tests do not depend on how slow the host's process scan happens to be.
        [int]$MaxExamine = 0,
        # A snapshot older than this is re-taken before the next gate call, so a process that starts
        # naming a worktree mid-sweep is seen within this window rather than never.
        [int]$SnapshotMaxAgeSeconds = 30,
        [switch]$WhatIf
    )
    $kept = [ordered]@{}
    $retired = New-Object System.Collections.Generic.List[string]
    $sum = [ordered]@{
        schema = 'mlv-app/merged-worktree-sweep/v1'; root = $Root; mergeTarget = $MergeTarget
        considered = 0; retired = @(); young = 0; kept = $kept; notReached = 0; error = $null
        examined = 0; stampWriteFailed = 0; elapsedMs = 0
        # r8: one row { original; state; note; attempts } per rename record under -Root, settled before any candidate is judged.
        strandedRestore = @()
        utc = (Get-Date).ToUniversalTime().ToString('o')
    }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    function Add-Kept([string]$Reason) {
        $k = if ($Reason) { ($Reason -split ':', 2)[0].Trim() } else { 'unknown' }
        if ($kept.Contains($k)) { $kept[$k] = [int]$kept[$k] + 1 } else { $kept[$k] = 1 }
    }
    function Get-FullNoSlash([string]$P) { [IO.Path]::GetFullPath(($P -replace '/', '\')).TrimEnd('\') }
    # The worktree's git admin dir (<common>\worktrees\<name>), from the `gitdir:` line of its .git file.
    function Get-WorktreeGitDir([string]$Wt) {
        $gf = Join-Path $Wt '.git'
        $m = [regex]::Match((Get-Content -LiteralPath $gf -Raw -ErrorAction Stop), '(?m)^gitdir:\s*(.+?)\s*$')
        if (-not $m.Success) { throw "no gitdir line in $gf" }
        $g = $m.Groups[1].Value -replace '/', '\'
        if (-not [IO.Path]::IsPathRooted($g)) { $g = Join-Path $Wt $g }
        $g
    }
    # Last-examined stamp in UTC ticks; missing or unreadable = 0 = oldest, so a never-examined worktree
    # is always visited before one that has been looked at.
    function Get-ExaminedTicks([string]$Wt) {
        try {
            $raw = (Get-Content -LiteralPath (Join-Path (Get-WorktreeGitDir $Wt) $script:SweepStampName) -Raw -ErrorAction Stop).Trim()
            $t = [DateTime]::Parse($raw, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal)
            [long]$t.Ticks
        } catch { [long]0 }
    }
    try {
        $rootFull = Get-FullNoSlash $Root
        $excl = @($Exclude | Where-Object { $_ } | ForEach-Object { Get-FullNoSlash $_ })
        # r8 RESTORE FIRST: a <root>.retiring an earlier call could not rename back (a handle outlived its budget) is put back at <root> before
        # the worktree list is read, so it is judged at its own path in this same sweep. Only records this tool wrote, only under -Root, never
        # an excluded path; a refused or failed restore leaves both paths as they are. -WhatIf renames nothing, so it only reports them.
        # r9: a record another live operation holds (a retirement in progress in an overlapping sweep) is 'owned' and left alone; and the
        # restore counts against -BudgetSeconds (H1): once it is spent, each remaining record is 'not-reached: budget' and kept for the next sweep.
        $cdo = @(& git.exe -C $RepoRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
        if (-not $LASTEXITCODE -and $cdo.Count) {
            foreach ($pr in @(Get-RetirePendingRecords -CommonDir ($cdo[0] -replace '/', '\'))) {
                # r10: no record.json at the read is not a failure. Its owner empties a record (record.json first) before it deletes the
                # directory, and writes record.json only after it has claimed it, so this is a record settled (or still being written) between
                # the listing and the read: the restore's claim says which (gone / owned). Only a record.json that exists but cannot be read fails.
                $po = $null
                if ($null -eq $pr.Record) {
                    if (Test-Path -LiteralPath (Join-Path $pr.Dir 'record.json')) { $sum.strandedRestore += [pscustomobject]@{ original = $null; state = 'failed'; note = "unreadable rename record $($pr.Dir): $($pr.Error)"; attempts = 0 }; continue }
                } else {
                    $po = Get-FullNoSlash ([string]$pr.Record.original)
                    if (-not $po.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase) -or @($excl | Where-Object { $_ -ieq $po }).Count) { continue }
                }
                $leftMs = [int]([Math]::Max(0, $BudgetSeconds * 1000 - $sw.ElapsedMilliseconds))
                $r = if ($WhatIf) { [ordered]@{ state = 'not-run: WhatIf'; note = 'a real sweep restores it first'; attempts = 0 } }
                     elseif ($leftMs -le 0) { [ordered]@{ state = 'not-reached: budget'; note = "-BudgetSeconds $BudgetSeconds was spent before this record; it and the tree are kept for the next sweep"; attempts = 0 } }
                     else { Restore-LaneWorktreeRename -Record $pr.Record -RecordDir $pr.Dir -BudgetMs $leftMs }
                $sum.strandedRestore += [pscustomobject]@{ original = $po; state = $r.state; note = $r.note; attempts = $r.attempts }
            }
        }
        $o = @(& git.exe -C $RepoRoot worktree list --porcelain 2>&1 | ForEach-Object { "$_" })
        if ($LASTEXITCODE -ne 0) { $sum.error = 'cannot-determine: worktree list: ' + ($o -join ' | '); $sum.elapsedMs = [int]$sw.ElapsedMilliseconds; return [pscustomobject]$sum }

        $cands = @()
        foreach ($line in $o) {
            if (-not $line.StartsWith('worktree ')) { continue }
            $p = Get-FullNoSlash $line.Substring(9)
            if (-not $p.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase)) { continue }
            if (@($excl | Where-Object { $_ -ieq $p }).Count) { continue }
            $cands += $p
        }
        $sum.considered = $cands.Count

        # FAIR ORDER: least-recently-examined first (missing stamp = oldest; ties keep `git worktree
        # list` order). `git worktree list` order is stable, and a budgeted sweep in a stable order
        # starves its tail whenever the head is permanently kept (long-lived unmerged or dirty
        # worktrees): measured 2026-10-03, 11 of 21 candidates were never reached. No randomness.
        $keyed = for ($k = 0; $k -lt $cands.Count; $k++) { [pscustomobject]@{ Path = $cands[$k]; Ticks = (Get-ExaminedTicks $cands[$k]); Idx = $k } }
        $cands = @($keyed | Sort-Object -Property Ticks, Idx | ForEach-Object { $_.Path })

        $snap = $null
        for ($i = 0; $i -lt $cands.Count; $i++) {
            $wt = $cands[$i]
            if ($sw.Elapsed.TotalSeconds -ge $BudgetSeconds -or ($MaxExamine -gt 0 -and $sum.examined -ge $MaxExamine)) { $sum.notReached = $cands.Count - $i; break }
            # r8: a listed <root>.retiring is one the restore above could not put back; it is never gated, so never removed.
            if ($wt -match '\.retiring$') { Add-Kept "stranded-retiring: $wt"; continue }

            # YOUNG guard: `git worktree add` leaves a clean tree already at the merge target that
            # no process names yet; the SAFE gate alone would retire it before its lane starts.
            # It reads ONLY {worktree dir CreationTime, <gitdir>\HEAD mtime, <gitdir>\index mtime}: the
            # examined stamp written below is a new file in <gitdir>, which touches none of the three.
            try {
                $gdir = Get-WorktreeGitDir $wt
                $stamps = @(
                    (Get-Item -LiteralPath $wt -ErrorAction Stop).CreationTimeUtc,
                    (Get-Item -LiteralPath (Join-Path $gdir 'HEAD') -ErrorAction Stop).LastWriteTimeUtc,
                    (Get-Item -LiteralPath (Join-Path $gdir 'index') -ErrorAction Stop).LastWriteTimeUtc)
                $newest = ($stamps | Measure-Object -Maximum).Maximum
            } catch {
                Add-Kept "cannot-determine: idle stamps: $($_.Exception.Message)"
                continue
            }
            if (((Get-Date).ToUniversalTime() - $newest).TotalHours -lt $MinIdleHours) { $sum.young++; continue }

            $sum.examined++
            $action = 'kept'
            try {
                # One process scan serves many gate calls; re-taken when stale. If the scan fails,
                # pass nothing and let the gate take its own (it reports cannot-determine, never a pass).
                if ($null -eq $snap -or ((Get-Date).ToUniversalTime() - $snap.CapturedUtc).TotalSeconds -ge $SnapshotMaxAgeSeconds) {
                    $snap = try { Get-LaneProcessSnapshot } catch { $null }
                }
                $d = Invoke-RetireLaneWorktree -WorkDir $wt -MergeTarget $MergeTarget -QuarantineRoot $QuarantineRoot `
                    -ProtectPath $ProtectPath -ProcessSnapshot $snap -RepoRoot $RepoRoot -WhatIf:$WhatIf
                $action = $d.action
                if ($d.action -in @('retired', 'would-retire')) { $retired.Add($wt) } else { Add-Kept $d.reason }
            } catch {
                Add-Kept "cannot-determine: $($_.Exception.Message)"
            }
            # Stamp every examined worktree the gate did not remove (kept, skipped, would-retire, or a
            # gate that threw: a worktree that always fails must not hog the head of the queue). A
            # retired worktree's admin dir is gone with it. A write failure is a bookkeeping failure,
            # not a verdict: it is counted apart from kept{} (so sum(kept) stays one reason per
            # worktree) and never fails the sweep; the worktree merely stays "oldest" next time.
            if ($action -ne 'retired') {
                try {
                    [IO.File]::WriteAllText((Join-Path (Get-WorktreeGitDir $wt) $script:SweepStampName), (Get-Date).ToUniversalTime().ToString('o'))
                } catch { $sum.stampWriteFailed++ }
            }
        }
    } catch {
        $sum.error = "cannot-determine: $($_.Exception.Message)"
    }
    $sum.retired = @($retired)
    $sum.elapsedMs = [int]$sw.ElapsedMilliseconds
    return [pscustomobject]$sum
}
