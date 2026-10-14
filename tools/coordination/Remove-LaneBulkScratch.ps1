<#
.SYNOPSIS
    Remove the bulk scratch a lane registered, after copying its small text receipts; refuse anything unsafe.

.DESCRIPTION
    Dot-source this file and call Invoke-RemoveLaneBulkScratch. It never throws: every outcome is
    returned as a disposition object so Invoke-Lane.ps1 can put it in the receipt.

    Origin (LANE-RECEIPT-SCRATCH-CLEANUP-1, 2026-10-12): Invoke-Lane.ps1 removes only its own
    -ScratchRoot dir, and Invoke-MlvtmpRetention.ps1 files every other non-worktree dir under
    C:\mlvtmp as kind=other / keep, so lane-authored asm, shadow-build and probe dirs (4-5 GiB in
    one day) were never removed by anything.

    CONTRACT: a lane lists each bulk path it creates, one absolute path per line, in
    <RunDir>\scratch-paths.txt (blank lines and lines starting with # are ignored). Register-LaneBulkScratch.ps1
    appends to it; the launcher gives the child MLV_LANE_RUN_DIR and MLV_LANE_SCRATCH_MANIFEST. After the lane exits, each listed path
    gets its small text files (*.txt, *.json, *.log, under 1 MiB) copied to
    <RunDir>\scratch-receipts\<n>-<leaf>\ and is then removed.

    HARD REFUSALS (recorded per path, never thrown; every probe that cannot answer refuses):
      relative-path / dotdot / short-name-alias   the path is not a plain absolute path
      outside-root                                not strictly inside -Root (default C:\mlvtmp)
      lane-scratch                                is, contains, or is inside -LaneScratchRoot
      workdir                                     is, contains, or is inside -WorkDir
      git-worktree                                is or contains a git worktree (registered, or a .git marker)
      worktree-list-unavailable                   `git worktree list` could not be read
      reparse-point                               the path, an ancestor below -Root, or anything inside it
      live-process (action kept)                  a live process other than the caller's own chain holds the path or one
                                                  under it: as its cwd, on its command line, or named by a script it runs
      run-dir-active (action kept)                a live process names this run's own dir (command line, cwd or script text):
                                                  the detached chain that registered the path is still running
      holder-cwd-unknown (action kept)            a live process's current directory could not be read and Retire-LaneWorktree.ps1's
                                                  Get-CwdUnknownRefusal does not exempt it, so it cannot be ruled out as a holder
      holder-probe-failed                         the process scan or the cwd probe could not answer; remembered for the run, so
                                                  every remaining entry gets the same refusal with no new scan
      producer-pr-open / pr-state-unknown         the lane's branch has an open PR, or that could not be learned (`gh` gets
                                                  -PrTimeoutSeconds, default 60; a stall or a failed branch lookup is unknown)
      The PR state is asked first and the process snapshot is taken after it, so a holder that starts during the probe is seen.
      walk-failed (action failed)                 the size / reparse walk of the path threw; nothing was removed
      copy-failed                                 a small file could not be copied out, so nothing is removed
    A path that is already gone is recorded `absent`. -Keep records `kept` / `opt-out` for every path.
    More than 200 distinct manifest lines: the first 200 are processed and `truncated` is the count dropped (0 = none).
#>

# LANE-RECEIPT-SCRATCH-CLEANUP-1 r2: the live-holder probe is the worktree-retirement probe, not a copy of it.
# A load failure is remembered and turns into `holder-probe-failed` per path (a probe that cannot answer refuses).
$script:LaneBulkHolderLoadError = $null
if (-not (Get-Command Find-WorktreeHolders -ErrorAction SilentlyContinue)) {
    try { . (Join-Path $PSScriptRoot 'Retire-LaneWorktree.ps1') }
    catch { $script:LaneBulkHolderLoadError = $_.Exception.Message }
    if (-not (Get-Command Find-WorktreeHolders -ErrorAction SilentlyContinue) -and -not $script:LaneBulkHolderLoadError) { $script:LaneBulkHolderLoadError = 'Find-WorktreeHolders not defined' }
}

# The repository the PR probe asks about: the same literal Export-PrReviewEvidence.ps1, landing-probe.ps1 and the other
# tools/coordination callers use (repo-slug.ps1; Export-PrReviewEvidence.ps1 and set-required-checks.ps1 keep a pinned literal equal to it).
if (-not (Get-Command Get-MlvRepoSlug -ErrorAction SilentlyContinue)) { . (Join-Path $PSScriptRoot 'repo-slug.ps1') }
$script:LaneBulkRepo = Get-MlvRepoSlug

function Test-LaneScratchSame([string]$A, [string]$B) {
    return ($A.TrimEnd('\') -ieq $B.TrimEnd('\'))
}
function Test-LaneScratchInside([string]$Child, [string]$Parent) {
    $p = $Parent.TrimEnd('\') + '\'
    return $Child.StartsWith($p, [StringComparison]::OrdinalIgnoreCase)
}
function Test-LaneScratchReparse([string]$Path) {
    try { return [bool]((Get-Item -LiteralPath $Path -Force -ErrorAction Stop).Attributes -band [IO.FileAttributes]::ReparsePoint) }
    catch { return $true }   # cannot read attributes -> treat as unsafe
}

function Get-LaneScratchWorktreePath([string]$RepoDir) {
    # $null when git cannot answer; callers refuse in that case.
    try {
        $out = @(& git -C $RepoDir worktree list --porcelain 2>$null)
        if ($LASTEXITCODE -ne 0) { return $null }
        return @($out | Where-Object { $_ -like 'worktree *' } | ForEach-Object { ([string]$_ -replace '^worktree ', '').Replace('/', '\').TrimEnd('\') })
    } catch { return $null }
}

function Get-LaneScratchPrState([string]$WorkDir, [string]$GhPath = 'gh', [int]$TimeoutSeconds = 60) {
    # 'open' | 'none' | 'unknown' | 'unknown:<detail>'. gh gets a deadline and is killed (with its children) past it.
    try {
        $branch = @(& git -C $WorkDir branch --show-current 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'unknown:branch-lookup-failed' }   # git could not answer: never read that as "no PR"
        if (-not $branch.Count -or -not [string]$branch[0]) { return 'none' }   # detached / no branch (git answered, exit 0): no PR to protect
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $GhPath
        foreach ($a in @('pr', 'list', '--repo', $script:LaneBulkRepo, '--head', [string]$branch[0], '--state', 'open', '--json', 'number')) { [void]$psi.ArgumentList.Add($a) }
        $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        try {
            $outTask = $p.StandardOutput.ReadToEndAsync(); $errTask = $p.StandardError.ReadToEndAsync()
            if (-not $p.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)) {
                try { $p.Kill($true) } catch { }
                return 'unknown:gh-timeout'
            }
            $p.WaitForExit()
            if ($p.ExitCode -ne 0) { return 'unknown:gh-exit-' + $p.ExitCode }
            $rows = @(([string]$outTask.GetAwaiter().GetResult()) | ConvertFrom-Json)
            if ($rows.Count -gt 0) { return 'open' } else { return 'none' }
        } finally { $p.Dispose() }
    } catch { return 'unknown' }
}

function Copy-LaneScratchReceipts([string]$Path, [string]$Dest) {
    # Small text files only; bounded so a 13k-file tree cannot stall the lane exit.
    $maxFiles = 200; $maxTotal = 20MB; $maxEach = 1MB
    $copied = 0; $total = [int64]0; $truncated = $false
    foreach ($f in Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction Stop |
                   Where-Object { $_.Extension -in '.txt', '.json', '.log' -and $_.Length -lt $maxEach }) {
        if ($copied -ge $maxFiles -or ($total + $f.Length) -gt $maxTotal) { $truncated = $true; break }
        $rel = $f.FullName.Substring($Path.TrimEnd('\').Length).TrimStart('\')
        $target = Join-Path $Dest $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $f.FullName -Destination $target -Force -ErrorAction Stop
        $copied++; $total += $f.Length
    }
    return [ordered]@{ files = $copied; bytes = $total; truncated = $truncated }
}

function Invoke-RemoveLaneBulkScratch {
    param(
        [Parameter(Mandatory)][string]$RunDir,
        [Parameter(Mandatory)][string]$WorkDir,
        [string]$Root = 'C:\mlvtmp',
        [string]$LaneScratchRoot = 'C:\mlvtmp\lane-scratch',
        [string]$ManifestName = 'scratch-paths.txt',
        # Test seam: a scriptblock returning 'open' | 'none' | 'unknown'. Default asks gh about the lane's branch.
        [scriptblock]$PrProbe = $null,
        # Test seams: the gh executable, its deadline, a scriptblock returning a process snapshot, and the entry cap.
        [string]$GhPath = 'gh',
        [int]$PrTimeoutSeconds = 60,
        [scriptblock]$SnapshotProvider = $null,
        [int]$MaxEntries = 200,
        [switch]$Keep
    )
    $manifest = Join-Path $RunDir $ManifestName
    $result = [ordered]@{ manifest = $manifest; optOut = [bool]$Keep; truncated = 0; entries = @(); error = $null }
    try {
        if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { return $null }
        $all = @(Get-Content -LiteralPath $manifest -Encoding utf8 -ErrorAction Stop |
                 ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') } |
                 Group-Object { $_.TrimEnd('\').ToLowerInvariant() } | ForEach-Object { $_.Group[0] })   # case- and trailing-backslash-insensitive, as Register-LaneBulkScratch.ps1 dedupes
        $lines = @($all | Select-Object -First $MaxEntries)
        $result['truncated'] = [Math]::Max(0, $all.Count - $lines.Count)
        $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        $workFull = [IO.Path]::GetFullPath($WorkDir).TrimEnd('\')
        $laneScratchFull = if ($LaneScratchRoot) { [IO.Path]::GetFullPath($LaneScratchRoot).TrimEnd('\') } else { $null }
        $worktrees = $null; $worktreesRead = $false; $pr = $null; $prDoneUtc = $null; $snap = $null; $runHold = $null; $probeFailure = $null
        $runDirFull = [IO.Path]::GetFullPath($RunDir).TrimEnd('\')
        $receiptsRoot = Join-Path $RunDir 'scratch-receipts'
        $entries = New-Object System.Collections.Generic.List[object]
        $n = 0
        foreach ($raw in $lines) {
            $n++
            $e = [ordered]@{ path = $raw; action = 'refused'; reason = $null; bytes = $null; copiedFiles = $null; copyTruncated = $null; holders = $null; error = $null }
            $entries.Add($e)
            try {
                if (-not [IO.Path]::IsPathRooted($raw) -or $raw -notmatch '^[A-Za-z]:\\') { $e.reason = 'relative-path'; continue }
                if ($raw -match '(^|\\)\.\.(\\|$)') { $e.reason = 'dotdot'; continue }
                if ($raw -match '~\d') { $e.reason = 'short-name-alias'; continue }
                $full = [IO.Path]::GetFullPath($raw).TrimEnd('\')
                if (-not (Test-LaneScratchInside $full $rootFull)) { $e.reason = 'outside-root'; continue }
                if ($laneScratchFull -and ((Test-LaneScratchSame $full $laneScratchFull) -or (Test-LaneScratchInside $full $laneScratchFull) -or (Test-LaneScratchInside $laneScratchFull $full))) { $e.reason = 'lane-scratch'; continue }
                if ((Test-LaneScratchSame $full $workFull) -or (Test-LaneScratchInside $full $workFull) -or (Test-LaneScratchInside $workFull $full)) { $e.reason = 'workdir'; continue }
                if (-not (Test-Path -LiteralPath $full)) { $e.action = 'absent'; $e.reason = 'not-found'; continue }
                # Ancestors strictly between -Root and the path must not be reparse points either.
                $anc = Split-Path -Parent $full; $ancBad = $false
                while ($anc -and (Test-LaneScratchInside $anc $rootFull)) { if (Test-LaneScratchReparse $anc) { $ancBad = $true; break }; $anc = Split-Path -Parent $anc }
                if ($ancBad -or (Test-LaneScratchReparse $full)) { $e.reason = 'reparse-point'; continue }
                if (-not (Test-Path -LiteralPath $full -PathType Container)) { $e.reason = 'not-a-directory'; continue }
                # Worktree: marker at the root, any registered worktree equal/inside/containing it, or one nested below.
                if (Test-Path -LiteralPath (Join-Path $full '.git')) { $e.reason = 'git-worktree'; continue }
                if (-not $worktreesRead) { $worktrees = Get-LaneScratchWorktreePath $WorkDir; $worktreesRead = $true }
                if ($null -eq $worktrees) { $e.reason = 'worktree-list-unavailable'; continue }
                $isWt = $false
                foreach ($w in $worktrees) { if ((Test-LaneScratchSame $full $w) -or (Test-LaneScratchInside $full $w) -or (Test-LaneScratchInside $w $full)) { $isWt = $true; break } }
                if ($isWt) { $e.reason = 'git-worktree'; continue }
                # One walk: size, reparse points anywhere inside, and nested .git markers. Nothing is followed.
                $bytes = [int64]0; $bad = $null
                try {
                    foreach ($it in Get-ChildItem -LiteralPath $full -Recurse -Force -ErrorAction Stop) {
                        if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) { $bad = 'reparse-point'; break }
                        if ($it.Name -eq '.git') { $bad = 'git-worktree'; break }
                        if (-not $it.PSIsContainer) { $bytes += $it.Length }
                    }
                } catch { $e.action = 'failed'; $e.reason = 'walk-failed'; $e.error = $_.Exception.Message; continue }
                $e.bytes = $bytes
                if ($bad) { $e.reason = $bad; continue }
                if ($Keep) { $e.action = 'kept'; $e.reason = 'opt-out'; continue }
                # PR state first (asked once per run): the process snapshot below must be taken AFTER it, so a holder that
                # starts while the probe runs is seen. A snapshot older than the PR probe never authorizes a removal.
                if ($null -eq $pr) {
                    $pr = if ($PrProbe) { [string](& $PrProbe) } else { Get-LaneScratchPrState $WorkDir $GhPath $PrTimeoutSeconds }
                    $prDoneUtc = (Get-Date).ToUniversalTime()
                }
                if ($pr -eq 'open') { $e.reason = 'producer-pr-open'; continue }
                if ($pr -ne 'none') { $e.reason = 'pr-state-unknown'; if ($pr -like 'unknown:*') { $e.error = $pr.Substring(8) }; continue }
                # Live-holder guard: a detached chain may still be writing here after its lane exited. Every probe that cannot
                # answer refuses, and a probe that failed once is not retried: every remaining entry gets the same refusal.
                if ($probeFailure) { $e.reason = 'holder-probe-failed'; $e.error = $probeFailure; continue }
                $hold = $null; $unk = $null
                try {
                    if ($script:LaneBulkHolderLoadError) { throw "holder probe unavailable: $($script:LaneBulkHolderLoadError)" }
                    $stale = $null -eq $snap -or $null -eq $snap.CapturedUtc -or $snap.CapturedUtc -lt $prDoneUtc -or
                             ((Get-Date).ToUniversalTime() - $snap.CapturedUtc).TotalSeconds -ge 10
                    if ($stale) {
                        $snap = if ($SnapshotProvider) { & $SnapshotProvider } else { Get-LaneProcessSnapshot }
                        $runHold = $null
                    }
                    if ([string](Get-LaneHolderIndex -Snapshot $snap).CwdProbe -ne 'ok') { throw 'current-directory probe unavailable' }
                    $hold = @(Find-WorktreeHolders -WorktreePath $full -Snapshot $snap)
                    # The run dir is where the detached chain scripts live and are launched from: a live process that names it
                    # (command line, cwd or script text) holds every path this run registered, even one built at run time.
                    if ($null -eq $runHold) { $runHold = @(Find-WorktreeHolders -WorktreePath $runDirFull -Snapshot $snap) }
                    $unk = Get-CwdUnknownRefusal -Snapshot $snap -WorktreePath $full
                } catch { $probeFailure = $_.Exception.Message; $snap = $null; $e.reason = 'holder-probe-failed'; $e.error = $probeFailure; continue }
                if ($hold.Count) {
                    $e.action = 'kept'; $e.reason = 'live-process'
                    $e.holders = @($hold | Select-Object -First 5 | ForEach-Object { "$($_.ProcessId) $($_.Name) via=$($_.Via)" })
                    continue
                }
                if ($runHold.Count) {
                    $e.action = 'kept'; $e.reason = 'run-dir-active'
                    $e.holders = @($runHold | Select-Object -First 5 | ForEach-Object { "$($_.ProcessId) $($_.Name) via=$($_.Via)" })
                    continue
                }
                # A live process whose current directory could not be read cannot be ruled out as a holder (the retirement
                # path's own rule, with its exemptions: started before the dir existed, or a service-account process).
                if ($unk.Reason) {
                    $e.action = 'kept'; $e.reason = 'holder-cwd-unknown'; $e.error = [string]$unk.Reason
                    $e.holders = @($unk.Rows | Select-Object -First 5 | ForEach-Object { "$($_.ProcessId) $($_.Name) via=$($_.Via)" })
                    continue
                }
                $dest = Join-Path $receiptsRoot ('{0}-{1}' -f $n, (Split-Path -Leaf $full))
                try {
                    $c = Copy-LaneScratchReceipts $full $dest
                    $e.copiedFiles = $c.files; $e.copyTruncated = $c.truncated
                } catch { $e.reason = 'copy-failed'; $e.error = $_.Exception.Message; continue }
                Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop
                $e.action = 'removed'; $e.reason = $null
            } catch {
                $e.action = 'failed'; $e.reason = 'remove-failed'; $e.error = $_.Exception.Message
            }
        }
        $result['entries'] = $entries.ToArray()
    } catch {
        $result.error = "cannot-determine: $($_.Exception.Message)"
    }
    return $result
}
