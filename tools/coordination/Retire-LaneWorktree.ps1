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
      kept     cwd-unknown             (r2) a live process that could hold the path has a current directory the probe could
                                       not read (other user, elevated, protected) and that was created at/after the worktree
                                       or has no readable creation time (r3), and whose owner the SCM cannot name as a
                                       well-known service identity (r4), or runs a relative script path with an
                                       unknown cwd. Fail closed. Fields: disposition.cwdProbe, cwdUnknown, cwdExempt,
                                       worktreeCreatedUtc, cwdExemptOwner, cwdOwnerProbe.
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
      `<pid> <name> exempt=service-sid:<sid> via=scm:<services>`; disposition.cwdOwnerProbe is scm | scm-failed.
      Why it cannot exempt a lane: a lane runs as THIS user (often a Scheduled Task in session 0); no SCM entry names it,
      and a service configured to run as a user, a per-user service instance or an NT Service\ account is not exempt.
      A session id is still not evidence. Fails closed when the SCM query fails, the probe is unavailable, the pid hosts
      services under mixed accounts, the image differs (pid reuse) or the script path is relative-unresolved. Not exempt
      and still refusing: non-service SYSTEM children such as SearchProtocolHost / SearchFilterHost (owner unreadable).
      RESIDUAL RISK, accepted: a SYSTEM service that changes its own cwd into a worktree is not caught by cwd.

    RELATIVE SCRIPT PATHS (r2): a script path on a holder's command line (-File, dot-source in -Command, positional)
    is resolved against THAT holder's current directory, taken from the cwd index, when it is relative or quoted-relative,
    then followed into the transitive corpus exactly like an absolute one. If the holder's cwd is unknown the path cannot
    be followed, so the holder is treated as possibly naming the worktree (cwd-unknown), not skipped.

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

$script:SweepStampName = 'mlv-sweep-examined'
$script:RetireDebrisPattern ='(^|/)(__pycache__|\.pytest_cache|\.hypothesis|build-release|build-debug|build-avx-parity|build-console)/$|\.pyc$'
$script:HolderScriptExt = 'ps1|psm1|py|cmd|bat|js|mjs'
$script:HolderMaxFilesPerProcess = 40
$script:HolderMaxBytes = 524288
$script:HolderMaxDepth = 3

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
        if ([string]$proc.Name -match $scriptRunner) {
            $corpus = Get-ScriptCorpus -CommandLine ([string]$proc.CommandLine) -FileCache $fileCache -HolderCwd ([string]$c)
            $relUnresolved = [bool]$corpus.RelativeUnresolved
            foreach ($f in @($corpus.Files)) {
                $scripts.Add([pscustomobject]@{ ProcessId = $procId; File = $f.File; Text = $f.Text })
            }
        }
        if ($c) { continue }
        # cwd unknown. Only a pid that exited since the snapshot holds nothing, and pids 0 and 4 (System Idle, System) are
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
        $why = if ($relUnresolved) { 'relative-script-unresolved' } elseif ($probeOk) { 'cwd-unreadable' } else { 'cwd-probe-unavailable' }
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
        Cwd = $cwd; Scripts = $scripts.ToArray(); ScriptsByPid = $byPid
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

function Get-ServiceAccountByPid {
    # r4: pid -> {Sid; Services; Image} for processes the SCM started as a well-known service identity. Every service the
    # SCM lists at that pid must be an own/share-process service whose configured account is LocalSystem (S-1-5-18),
    # LocalService (S-1-5-19) or NetworkService (S-1-5-20), all naming the same image; any other account (a user, a
    # per-user service instance, an NT Service\ virtual account) drops the pid. Throws when the SCM query fails.
    $map = @{}; $bad = @{}
    foreach ($s in @(Get-RunningServiceRows)) {
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
        if (-not $map.ContainsKey($procId)) { $map[$procId] = [pscustomobject]@{ Sid = $sid; Services = [string]$s.Name; Image = $img }; continue }
        if ($map[$procId].Sid -ne $sid -or $map[$procId].Image -ine $img) { $bad[$procId] = $true; continue }
        $map[$procId].Services += ',' + [string]$s.Name
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
    param([object]$Snapshot, [string]$WorktreePath)
    $idx = Get-LaneHolderIndex -Snapshot $Snapshot
    $wtCreated = try { (Get-Item -LiteralPath $WorktreePath -ErrorAction Stop).CreationTimeUtc } catch { $null }
    $unk = @(); $exempt = 0; $owner = @(); $ownerProbe = $null
    foreach ($u in @($idx.CwdUnknown)) {
        if ($idx.CwdProbe -eq 'ok' -and $u.Why -eq 'cwd-unreadable' -and $null -ne $wtCreated -and $null -ne $u.CreatedUtc -and $u.CreatedUtc -lt $wtCreated) { $exempt++; continue }
        if ($idx.CwdProbe -eq 'ok' -and $u.Why -eq 'cwd-unreadable') {
            # One SCM query per snapshot, cached on the index; a failed query exempts nothing.
            if (-not $idx.PSObject.Properties['ServiceOwners']) {
                $so = try { [pscustomobject]@{ Probe = 'scm'; ByPid = (Get-ServiceAccountByPid) } } catch { [pscustomobject]@{ Probe = 'scm-failed'; ByPid = @{} } }
                $idx | Add-Member -NotePropertyName ServiceOwners -NotePropertyValue $so -Force
            }
            $ownerProbe = $idx.ServiceOwners.Probe
            $o = $idx.ServiceOwners.ByPid[[int]$u.ProcessId]
            if ($o -and [string]$o.Image -ieq [string]$u.Name) { $owner += "$($u.ProcessId) $($u.Name) exempt=service-sid:$($o.Sid) via=scm:$($o.Services)"; continue }
        }
        $unk += $u
    }
    $made = if ($null -ne $wtCreated) { ([datetime]$wtCreated).ToString('o') } else { $null }
    $res = [pscustomobject]@{ Probe = [string]$idx.CwdProbe; Reason = $null; Rows = @(); Unknown = @(); Exempt = $exempt; WorktreeCreatedUtc = $made
                              ExemptOwner = $owner; OwnerProbe = $ownerProbe }
    if (-not $unk.Count) { return $res }
    $first = ($unk | Select-Object -First 5 | ForEach-Object { "$($_.ProcessId) $($_.Name) ($($_.Why))" }) -join ', '
    $res.Reason = if ($idx.CwdProbe -ne 'ok') { "cwd-probe-unavailable: the current-directory probe cannot run, so $($unk.Count) process(es) cannot be ruled out as holders, first: $first" }
                  else { "cwd-unknown: $($unk.Count) process(es) with an unreadable current directory, created at/after the worktree or with no readable creation time, cannot be ruled out as holders, first: $first" }
    $via = if ($idx.CwdProbe -ne 'ok') { 'cwd-probe-unavailable' } else { 'cwd-unknown' }
    $res.Rows = @($unk | Select-Object -First 20 | ForEach-Object { [pscustomobject]@{ ProcessId = $_.ProcessId; Name = $_.Name; Via = $via } })
    $res.Unknown = @($unk | ForEach-Object { $t = if ($_.CreatedUtc) { ([datetime]$_.CreatedUtc).ToString('o') } else { 'unreadable' }; "$($_.ProcessId) $($_.Name) $($_.Why) created=$t" })
    return $res
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
        [switch]$WhatIf
    )
    $d = [ordered]@{
        schema = 'mlv-app/lane-worktree-disposition/v1'; workDir = $WorkDir; action = 'kept'; reason = $null
        head = $null; branch = $null; quarantined = @(); holders = @(); cwdProbe = $null; cwdUnknown = @()
        cwdExempt = 0; worktreeCreatedUtc = $null; cwdExemptOwner = @(); cwdOwnerProbe = $null
        utc = (Get-Date).ToUniversalTime().ToString('o')
    }
    # Fail closed on the cwd probe's own blind spots (see CWD PROBE IS LOUD): true = refused, disposition filled in.
    function Test-CwdRefusal([object]$Sn) {
        $r = Get-CwdUnknownRefusal -Snapshot $Sn -WorktreePath $wd
        $d.cwdProbe = [string]$r.Probe
        $d.cwdExempt = [int]$r.Exempt
        $d.worktreeCreatedUtc = $r.WorktreeCreatedUtc
        $d.cwdExemptOwner = @($r.ExemptOwner)
        $d.cwdOwnerProbe = $r.OwnerProbe
        if (-not @($r.Unknown).Count) { return $false }
        $d.cwdUnknown = $r.Unknown
        Write-RefusedHolders -WorktreePath $wd -Holders $r.Rows
        $d.reason = $r.Reason
        return $true
    }
    function Run-Git([string]$C, [string[]]$A) {
        $o = & git.exe -C $C @A 2>&1
        [pscustomobject]@{ code = $LASTEXITCODE; out = @($o | ForEach-Object { "$_" }) }
    }
    try {
        if (-not (Test-Path -LiteralPath $WorkDir -PathType Container)) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: path absent'; return [pscustomobject]$d }
        $wd = (Resolve-Path -LiteralPath $WorkDir).Path.TrimEnd('\')
        $gd = Run-Git $wd @('rev-parse', '--path-format=absolute', '--git-dir')
        $cd = Run-Git $wd @('rev-parse', '--path-format=absolute', '--git-common-dir')
        $top = Run-Git $wd @('rev-parse', '--show-toplevel')
        if ($gd.code -or $cd.code -or $top.code) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: not a git checkout'; return [pscustomobject]$d }
        if ($gd.out[0] -eq $cd.out[0]) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: main checkout'; return [pscustomobject]$d }
        if (($top.out[0] -replace '/', '\').TrimEnd('\') -ne $wd) { $d.action = 'skipped'; $d.reason = 'not-a-linked-worktree: WorkDir is a subdirectory'; return [pscustomobject]$d }
        $main = Split-Path -Parent ($cd.out[0] -replace '/', '\')

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
        if ($WhatIf) { $d.action = 'would-retire'; $d.reason = 'ok'; $d.quarantined = $keep; return [pscustomobject]$d }

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

        # <leaf>-<utc stamp>: two same-named worktrees retired the same day must not nest into each other.
        $qDir = Join-Path $QuarantineRoot ('{0}-{1}' -f (Split-Path $wd -Leaf), (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ'))
        foreach ($rel in $keep) {
            $relWin = $rel.TrimEnd('/') -replace '/', '\'
            $dst = Join-Path $qDir $relWin
            New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
            Move-Item -LiteralPath (Join-Path $wd $relWin) -Destination $dst -ErrorAction Stop
            $d.quarantined += $dst
        }
        $c = Run-Git $wd @('clean', '-fdX'); if ($c.code) { $d.reason = 'remove-failed: git clean -fdX: ' + ($c.out -join ' | '); return [pscustomobject]$d }
        $r = Run-Git $main @('-c', 'core.longpaths=true', 'worktree', 'remove', $wd)
        if ($r.code) { $d.reason = 'remove-failed: ' + ($r.out -join ' | '); return [pscustomobject]$d }
        $d.action = 'retired'; $d.reason = 'ok'
    } catch {
        $d.action = 'kept'; $d.reason = "cannot-determine: $($_.Exception.Message)"
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
                    -ProtectPath $ProtectPath -ProcessSnapshot $snap -WhatIf:$WhatIf
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
