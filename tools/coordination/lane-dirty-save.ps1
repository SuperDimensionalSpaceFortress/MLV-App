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
        $prefix = @(& git -C $Wt rev-parse --show-prefix 2>$null)
        if ($LASTEXITCODE -ne 0 -or ($prefix -join '').Trim() -ne '') { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=not-a-worktree-root" }
        $tmp = Join-Path $Dest ('dirty-worktree-saving-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        & git -C $Wt diff HEAD --binary "--output=$tmp" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $tmp)) { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=git-diff-exit-$LASTEXITCODE" }
        $sha8 = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.Substring(0, 8).ToLowerInvariant()
        $patch = Join-Path $Dest "dirty-worktree-$sha8.patch"
        $stat = Join-Path $Dest "dirty-worktree-$sha8.status.txt"
        $bytes = (Get-Item -LiteralPath $tmp).Length
        if (Test-Path -LiteralPath $patch) { Remove-Item -LiteralPath $tmp -Force } else { Move-Item -LiteralPath $tmp -Destination $patch }
        $tmp = $null
        $status = @(& git -C $Wt -c core.quotepath=false status --porcelain 2>$null)
        Set-Content -LiteralPath $stat -Value $status -Encoding ascii
        & git -C $Wt apply --check -R $patch 2>$null
        $verified = if ($bytes -eq 0) { 'empty-diff' } elseif ($LASTEXITCODE -eq 0) { 'reverse-apply-ok' } else { 'reverse-apply-FAILED' }
        return "DIRTY_TREE_SAVED card=$CardName round=$RoundName files=$($status.Count) bytes=$bytes verified=$verified patch=$patch status=$stat reapply=git -C `"$Wt`" apply `"$patch`""
    }
    catch { return "DIRTY_TREE_SAVE_FAILED card=$CardName reason=$($_.Exception.Message -replace '\s+', ' ')" }
    finally { if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
}
