# lane-dirty-save.ps1 - LANE-HUBCHAT-DETACHED-DISPATCH-1.
# A lane that dies mid-edit (2026-10-26 09:09Z: the hub chat's tool tree ended with the Desktop app) leaves tracked changes in its worktree, and a dispatcher that then
# refuses a dirty tree spends every retry on work nobody owns. Save-DirtyWorktreeEvidence copies that work into the refused run dir BEFORE the refusal, so it survives:
#   dirty-worktree-<sha8>.patch        git diff HEAD --binary (tracked changes only; sha8 = first 8 hex of the patch SHA-256)
#   dirty-worktree-<sha8>.status.txt   git status --porcelain (names untracked files too; their content is NOT in the patch)
# It RETURNS one line (the caller prints it): DIRTY_TREE_SAVED ... verified=reverse-apply-ok|empty-diff|reverse-apply-FAILED reapply=<command>, or DIRTY_TREE_SAVE_FAILED reason=<why>.
# Read-only on the worktree (nothing is restored, so the caller's refusal and exit code are unchanged), idempotent (same diff = same name), fail-soft (never throws), and it
# refuses a directory that is not itself a worktree root (that would resolve to an ENCLOSING repo and save its diff). The same logic is deployed board-local in
# hub-autopilot\dispatch-fix-round.ps1; keep the two in step.
function Save-DirtyWorktreeEvidence([string]$Wt, [string]$Dest, [string]$CardName = '', [string]$RoundName = '') {
    $tmp = $null
    try {
        if (-not (Test-Path -LiteralPath $Dest -PathType Container)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=no-run-dir" }
        $Dest = (Resolve-Path -LiteralPath $Dest).ProviderPath  # git -C $Wt would resolve a relative --output against the worktree, not the caller
        $prefix = @(& git -C $Wt rev-parse --show-prefix 2>$null)
        if ($LASTEXITCODE -ne 0 -or ($prefix -join '').Trim() -ne '') { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        $tmp = Join-Path $Dest ('dirty-worktree-saving-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        # Explicit a/ b/ prefixes and no external diff driver: diff.noprefix / diff.mnemonicPrefix / diff.external in a user or global config would otherwise change the patch
        # `git apply` (default -p1) has to read back.
        & git -C $Wt diff HEAD --binary --no-ext-diff --src-prefix=a/ --dst-prefix=b/ "--output=$tmp" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $tmp)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-diff-exit-$LASTEXITCODE" }
        $full = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
        # The name is the first 8 hex of the hash, lengthened until it is free or already holds these exact bytes (two different diffs may share a prefix).
        $id = $null
        foreach ($n in 8, 16, 32, 64) {
            $cand = Join-Path $Dest "dirty-worktree-$($full.Substring(0, $n)).patch"
            if (-not (Test-Path -LiteralPath $cand) -or (Get-FileHash -LiteralPath $cand -Algorithm SHA256).Hash.ToLowerInvariant() -eq $full) { $id = $full.Substring(0, $n); break }
        }
        if (-not $id) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=name-collision" }
        $patch = Join-Path $Dest "dirty-worktree-$id.patch"
        $stat = Join-Path $Dest "dirty-worktree-$id.status.txt"
        $bytes = (Get-Item -LiteralPath $tmp).Length
        if (Test-Path -LiteralPath $patch) { Remove-Item -LiteralPath $tmp -Force } else { Move-Item -LiteralPath $tmp -Destination $patch }
        $tmp = $null
        $status = @(& git -C $Wt -c core.quotepath=false status --porcelain 2>$null)
        $statusExit = $LASTEXITCODE
        if ($statusExit -ne 0) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-status-exit-$statusExit patch=$patch" }
        [System.IO.File]::WriteAllLines($stat, [string[]]$status, (New-Object System.Text.UTF8Encoding($false)))  # UTF-8: quotepath=false prints literal non-ASCII names
        & git -C $Wt apply --check -R $patch 2>$null
        $verified = if ($bytes -eq 0) { 'empty-diff' } elseif ($LASTEXITCODE -eq 0) { 'reverse-apply-ok' } else { 'reverse-apply-FAILED' }
        return "DIRTY_TREE_SAVED card=$CardName round=$RoundName files=$($status.Count) bytes=$bytes verified=$verified patch=$patch status=$stat reapply=git -C `"$Wt`" apply `"$patch`""
    }
    catch { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($_.Exception.Message -replace '\s+', ' ')" }
    finally { if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
}
