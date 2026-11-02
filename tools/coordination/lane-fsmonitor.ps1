# HOST-FSMONITOR-DAEMON-LEAK-1 (2026-10-17). Dot-source this file; it only defines functions (no Add-Type, no side effects), so a lane launch pays nothing for it.
#
# The board's .git/config carries core.fsmonitor=true and every linked worktree inherits it, so the first `git status` in a lane worktree starts a
# `git fsmonitor--daemon run --detach` that nothing ever stops: 65 daemons for 64 worktrees, the oldest 17 h old, ~51 MiB private each, host commit
# at 61 GiB against the 60 GiB gate. Two halves, both fail-soft (an optimisation must never turn a lane or a retirement into a failure):
#   Disable-LaneWorktreeFsmonitor  (a) core.fsmonitor=false in THIS worktree's config.worktree, so a status there starts nothing. Only when
#                                      extensions.worktreeConfig is already on; otherwise `--worktree` errors (git 2.40, several worktrees)
#                                      or may write the SHARED config and switch the board's fsmonitor off too, so it is skipped, never flipped on.
#   Stop-LaneWorktreeFsmonitor     (b) `git fsmonitor--daemon stop` for exactly this worktree. NEVER kill by name or pid: the daemon is asked
#                                      to exit over its own IPC. A daemon also holds the worktree directory open, which is a removal hazard.
# Both refuse the main checkout (git dir == common dir): the board's own daemon is not a lane daemon. Both return an object, never throw.
# Callers: Invoke-Lane.ps1 (every dispatched lane), Invoke-Workstream.ps1 and Invoke-WorkstreamLoop.ps1 (worktree creation), Retire-LaneWorktree.ps1.
function Get-LaneFsmonitorRefusal {
    param([string]$WorkDir)
    if (-not (Test-Path -LiteralPath $WorkDir -PathType Container)) { return 'path absent' }
    $gd = @(& git.exe -C $WorkDir rev-parse --path-format=absolute --git-dir 2>$null); $gdCode = $LASTEXITCODE
    $cd = @(& git.exe -C $WorkDir rev-parse --path-format=absolute --git-common-dir 2>$null); $cdCode = $LASTEXITCODE
    if ($gdCode -or $cdCode -or -not $gd.Count -or -not $cd.Count) { return 'not a git checkout' }
    if ((([string]$gd[0]) -replace '/', '\').TrimEnd('\') -ieq (([string]$cd[0]) -replace '/', '\').TrimEnd('\')) { return 'main checkout: its daemon is not a lane daemon' }
    return $null
}

function Disable-LaneWorktreeFsmonitor {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$WorkDir)
    $r = [ordered]@{ schema = 'mlv-app/lane-fsmonitor/v1'; workDir = $WorkDir; action = 'skipped'; reason = $null }
    try {
        $refusal = Get-LaneFsmonitorRefusal -WorkDir $WorkDir
        if ($refusal) { $r.reason = $refusal; return [pscustomobject]$r }
        $ext = @(& git.exe -C $WorkDir config --type=bool --get extensions.worktreeConfig 2>$null)
        if (-not $ext.Count -or ([string]$ext[0]).Trim() -ne 'true') {
            $r.reason = 'extensions.worktreeConfig is not enabled: --worktree would write the shared config'
            return [pscustomobject]$r
        }
        $o = @(& git.exe -C $WorkDir config --worktree core.fsmonitor false 2>&1)
        if ($LASTEXITCODE) { $r.action = 'failed'; $r.reason = 'config --worktree failed: ' + (($o | ForEach-Object { "$_" }) -join ' | ') }
        else { $r.action = 'disabled' }
    } catch { $r.action = 'failed'; $r.reason = "cannot-determine: $($_.Exception.Message)" }
    return [pscustomobject]$r
}

function Stop-LaneWorktreeFsmonitor {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$WorkDir)
    $r = [ordered]@{ schema = 'mlv-app/lane-fsmonitor/v1'; workDir = $WorkDir; action = 'skipped'; reason = $null }
    try {
        $refusal = Get-LaneFsmonitorRefusal -WorkDir $WorkDir
        if ($refusal) { $r.reason = $refusal; return [pscustomobject]$r }
        $st = (& git.exe -C $WorkDir fsmonitor--daemon status 2>&1 | Out-String)
        if ($st -notmatch 'is watching') { $r.action = 'none'; $r.reason = 'no daemon'; return [pscustomobject]$r }
        $o = @(& git.exe -C $WorkDir fsmonitor--daemon stop 2>&1)
        if ($LASTEXITCODE) { $r.action = 'failed'; $r.reason = 'fsmonitor--daemon stop failed: ' + (($o | ForEach-Object { "$_" }) -join ' | ') }
        else { $r.action = 'stopped' }
    } catch { $r.action = 'failed'; $r.reason = "cannot-determine: $($_.Exception.Message)" }
    return [pscustomobject]$r
}

# r2 (sol r1 BLOCKER): for a worktree that ALREADY EXISTS, so a daemon may already be running (a lane worktree made before this fix, or one a
# status touched first). Order matters: disable first so nothing can restart the daemon, then stop the one that is there.
# Call it BEFORE the first git command that reads the worktree's index (status, diff, reset, checkout). Never throws.
function Protect-LaneWorktreeFsmonitor {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$WorkDir)
    $d = Disable-LaneWorktreeFsmonitor -WorkDir $WorkDir
    $s = Stop-LaneWorktreeFsmonitor -WorkDir $WorkDir
    return [pscustomobject][ordered]@{ schema = 'mlv-app/lane-fsmonitor/v1'; workDir = $WorkDir; disable = $d.action; stop = $s.action; reason = (@($d.reason, $s.reason) | Where-Object { $_ }) -join ' | ' }
}
