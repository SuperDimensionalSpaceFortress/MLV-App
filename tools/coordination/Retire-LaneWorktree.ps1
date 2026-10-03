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
      kept     live-process            a process other than this one names the path on its command line
      kept     dirty                   `git status --porcelain -uall` is non-empty
      kept     unpushed                HEAD has commits not on any remote
      kept     unmerged                HEAD is on a remote but not an ancestor of -MergeTarget
      kept     stash                   the repository stash list is non-empty
      kept     nested-worktree         another registered worktree lives inside this one
      kept     rundir-inside           -ProtectPath (e.g. the receipt RunDir) is inside the worktree
      kept     cannot-determine:<why>  a probe failed
      retired  ok                      ignored NON-debris entries moved to -QuarantineRoot,
                                       `git clean -fdX`, then `git worktree remove` (never --force)
      kept     remove-failed:<why>     removal was refused; nothing further attempted

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
        Procs       = @($all | Where-Object { $_.CommandLine -and $self -notcontains $_.ProcessId })
        SelfPids    = $self
        CapturedUtc = (Get-Date).ToUniversalTime()
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
        head = $null; branch = $null; quarantined = @(); utc = (Get-Date).ToUniversalTime().ToString('o')
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
        $self = @($snap.SelfPids)
        $procs = @($snap.Procs | Where-Object { $_.CommandLine -and $self -notcontains $_.ProcessId })
        $slash = $wd -replace '\\', '/'
        $live = @($procs | Where-Object { $_.CommandLine.IndexOf($wd, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or $_.CommandLine.IndexOf($slash, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
        if ($live.Count) { $d.reason = 'live-process: ' + (($live | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join ', '); return [pscustomobject]$d }

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

        $ign = Run-Git $wd @('status', '--porcelain', '--ignored', '-unormal'); if ($ign.code) { $d.reason = 'cannot-determine: ignored listing'; return [pscustomobject]$d }
        $keep = @($ign.out | Where-Object { $_ -like '!!*' } | ForEach-Object { $_.Substring(3) } | Where-Object { $_ -notmatch $script:RetireDebrisPattern })
        if ($keep.Count -and -not $QuarantineRoot) { $d.reason = "cannot-determine: $($keep.Count) ignored non-debris entr(y/ies) and no -QuarantineRoot"; return [pscustomobject]$d }
        if ($WhatIf) { $d.action = 'would-retire'; $d.reason = 'ok'; $d.quarantined = $keep; return [pscustomobject]$d }

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
