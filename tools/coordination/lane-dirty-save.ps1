# lane-dirty-save.ps1 - LANE-HUBCHAT-DETACHED-DISPATCH-1.
# A lane that dies mid-edit (2026-10-26 09:09Z: the hub chat's tool tree ended with the Desktop app) leaves tracked changes in its worktree, and a dispatcher that then
# refuses a dirty tree spends every retry on work nobody owns. Save-DirtyWorktreeEvidence copies that work into the refused run dir BEFORE the refusal, so it survives:
#   dirty-worktree-<sha8>.patch        git diff HEAD --binary (tracked changes only; sha8 = first 8 hex of the patch SHA-256)
#   dirty-worktree-<sha8>.status.txt   git status --porcelain (names untracked files too; their content is NOT in the patch); a later save with a different
#                                      untracked set for the same patch is kept beside it as dirty-worktree-<sha8>.status-<status sha8>.txt
#   dirty-worktree-staged-<sha8>.diff  git diff --cached --binary, only when something is staged (covers staged work the working tree no longer has)
# It RETURNS one line (the caller prints it): DIRTY_TREE_SAVED ... staged=<path|none> verified=reverse-apply-ok|empty-diff|reverse-apply-FAILED reapply=<command>, or DIRTY_TREE_SAVE_FAILED reason=<why>.
# A staged diff that cannot be written whole (git exit, empty or truncated file, no reverse-apply to the index) is DIRTY_TREE_SAVE_FAILED, never SAVED; no evidence name is ever overwritten.
# Read-only on the worktree (nothing is restored, so the caller's refusal and exit code are unchanged), idempotent (same diff = same name), fail-soft (never throws), and it
# refuses a directory that is not itself a worktree root (that would resolve to an ENCLOSING repo and save its diff). The same logic is deployed board-local in
# hub-autopilot\dispatch-fix-round.ps1; keep the two in step.
# Moves $Tmp to <Dir>\<Stem><first 8|16|32|64 hex of $Full><Ext> and returns @{ Path; Id }, or $null when every length is taken by a DIFFERENT file.
# The move fails rather than overwrites, so an occupied name is compared by hash (same bytes = reuse, anything else = lengthen the name) and never clobbered.
# When the name already holds these exact bytes, $Tmp is left for the caller's cleanup. A non-empty $FirstName is tried before the hash-derived names.
function Move-DirtyEvidenceToFreeName([string]$Tmp, [string]$Dir, [string]$Stem, [string]$Ext, [string]$Full, [string]$FirstName = '') {
    $names = @()
    if ($FirstName) { $names += , @($FirstName, '') }
    foreach ($n in 8, 16, 32, 64) { $names += , @("$Stem$($Full.Substring(0, $n))$Ext", $Full.Substring(0, $n)) }
    foreach ($pair in $names) {
        $cand = Join-Path $Dir $pair[0]
        try { [System.IO.File]::Move($Tmp, $cand); return @{ Path = $cand; Id = $pair[1]; Moved = $true } }
        catch [System.IO.IOException] {
            if ((Test-Path -LiteralPath $cand) -and (Get-FileHash -LiteralPath $cand -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Full) { return @{ Path = $cand; Id = $pair[1]; Moved = $false } }
        }
    }
    return $null
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
    $tmp = $null; $stagedTmp = $null; $statTmp = $null
    $CardName = $CardName -replace '\s+', ' '; $RoundName = $RoundName -replace '\s+', ' '  # the result is one line
    # -C $Wt names the repository, so a GIT_DIR / GIT_INDEX_FILE / GIT_DIFF_OPTS inherited from the caller is cleared for the call and put back afterwards.
    # These variables change which repository git reads or the shape of the diff it writes; GIT_DIFF_OPTS=--unified=0, for one, beats the explicit -U3.
    $gitEnv = 'GIT_DIFF_OPTS', 'GIT_EXTERNAL_DIFF', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX'
    $savedEnv = @{}
    foreach ($v in $gitEnv) { if (Test-Path -LiteralPath "Env:$v") { $savedEnv[$v] = (Get-Item -LiteralPath "Env:$v").Value; Remove-Item -LiteralPath "Env:$v" } }  # removed, not emptied: git reads an empty GIT_DIR as a path
    try {
        if (-not (Test-Path -LiteralPath $Dest -PathType Container)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=no-run-dir" }
        $Dest = (Resolve-Path -LiteralPath $Dest).ProviderPath  # git -C $Wt would resolve a relative --output against the worktree, not the caller
        $prefix = @(& git -C $Wt rev-parse --show-prefix 2>$null)
        if ($LASTEXITCODE -ne 0 -or ($prefix -join '').Trim() -ne '') { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
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
        $placed = Move-DirtyEvidenceToFreeName -Tmp $tmp -Dir $Dest -Stem 'dirty-worktree-' -Ext '.patch' -Full $full
        if (-not $placed) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=name-collision" }
        if ($placed.Moved) { $tmp = $null }
        $id = $placed.Id
        $patch = $placed.Path
        # --untracked-files=all: a status.showUntrackedFiles=no config must not hide the untracked names this file exists to keep.
        $status = @(& git -C $Wt -c core.quotepath=false status --porcelain --untracked-files=all 2>$null)
        $statusExit = $LASTEXITCODE
        if ($statusExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-status-exit-$statusExit patch=$patch" }
        # The same tracked diff can come back with a different untracked set, so the status file is placed like every other evidence file: <patch name>.status.txt when
        # free or identical, otherwise a hash-named sibling. A later save never rewrites an earlier snapshot.
        $statTmp = Join-Path $Dest ('dirty-worktree-status-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        [System.IO.File]::WriteAllLines($statTmp, [string[]]$status, (New-Object System.Text.UTF8Encoding($false)))  # UTF-8: quotepath=false prints literal non-ASCII names
        $statFull = (Get-FileHash -LiteralPath $statTmp -Algorithm SHA256).Hash.ToLowerInvariant()
        $statPlaced = Move-DirtyEvidenceToFreeName -Tmp $statTmp -Dir $Dest -Stem "dirty-worktree-$id.status-" -Ext '.txt' -Full $statFull -FirstName "dirty-worktree-$id.status.txt"
        if (-not $statPlaced) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=name-collision patch=$patch" }
        if ($statPlaced.Moved) { $statTmp = $null }
        $stat = $statPlaced.Path
        # Staged content the working tree no longer has (staged, then edited back) is invisible to diff HEAD; keep it in its own file when it exists.
        # Whether anything is staged is asked of git (exit 1 = yes), never inferred from the size of a file a failed write may have emptied.
        & git -C $Wt diff --cached --quiet --no-ext-diff --ignore-submodules=none 2>$null
        $stagedState = $LASTEXITCODE
        if ($stagedState -notin 0, 1) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-staged-check-exit-$stagedState patch=$patch" }
        $stagedOut = 'none'
        if ($stagedState -eq 1) {
            $stagedTmp = Join-Path $Dest ('dirty-worktree-staging-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            & git -C $Wt diff --cached @shape "--output=$stagedTmp" 2>$null
            $stagedExit = $LASTEXITCODE
            if ($stagedExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-staged-diff-exit-$stagedExit patch=$patch" }
            # A full volume can cut the file short without git saying so: the staged diff must be non-empty and byte-identical to git's own stream for the index.
            if (-not (Test-Path -LiteralPath $stagedTmp) -or (Get-Item -LiteralPath $stagedTmp).Length -eq 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-incomplete patch=$patch" }
            if (-not (Test-DirtyPatchComplete -Wt $Wt -Patch $stagedTmp -GitDiffArgs (@('diff', '--cached') + $shape))) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=staged-diff-incomplete patch=$patch" }
            $stagedFull = (Get-FileHash -LiteralPath $stagedTmp -Algorithm SHA256).Hash.ToLowerInvariant()
            $stagedPlaced = Move-DirtyEvidenceToFreeName -Tmp $stagedTmp -Dir $Dest -Stem 'dirty-worktree-staged-' -Ext '.diff' -Full $stagedFull
            if (-not $stagedPlaced) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=name-collision patch=$patch" }
            if ($stagedPlaced.Moved) { $stagedTmp = $null }
            $stagedOut = $stagedPlaced.Path
        }
        & git -C $Wt apply --check -R $patch 2>$null
        $verified = if ($bytes -eq 0) { 'empty-diff' } elseif ($LASTEXITCODE -eq 0) { 'reverse-apply-ok' } else { 'reverse-apply-FAILED' }
        return "DIRTY_TREE_SAVED card=$CardName round=$RoundName files=$($status.Count) bytes=$bytes staged=$stagedOut verified=$verified patch=$patch status=$stat reapply=git -C `"$Wt`" apply `"$patch`""
    }
    catch { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($_.Exception.Message -replace '\s+', ' ')" }
    finally {
        foreach ($t in $tmp, $stagedTmp, $statTmp) { if ($t -and (Test-Path -LiteralPath $t)) { Remove-Item -LiteralPath $t -Force -ErrorAction SilentlyContinue } }
        foreach ($v in $savedEnv.Keys) { Set-Item -LiteralPath "Env:$v" -Value $savedEnv[$v] }
    }
}
