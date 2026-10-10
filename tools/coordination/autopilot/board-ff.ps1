# board-ff.ps1 - tracked, profile-driven copy of the autopilot's board fast-forward (BOARD-AUTOPILOT-TRACKED-1 slice 1).
# Fast-forward the canonical board checkout to <remote>/<branch> so everything the autopilot runs FROM THE BOARD is merged code.
# Board, Remote and Branch default from the board profile (board-profile.json beside this script, or -ProfilePath); an explicit
# parameter always wins. StateFile and ScratchDir never default to a tracked path: they default to <ScratchDir> =
# $env:BOARD_FF_SCRATCH, else <temp>\board-ff-<project>, and StateFile = <ScratchDir>\board-ff-state.json.
# Called by a scheduler at most once per MinIntervalSec (own state file). Returns one object:
#   Result (ok|current|refused), Reason, From, To, Sha, Line = "BOARD-FF <result> <reason> from=<sha8> to=<sha8>"   ($null when skipped by the interval)
# The target ref is resolved once, right after the fetch, to a full commit SHA (Sha); every check, the merge and the final
# verification use that SHA, so a concurrent fetch that moves the ref cannot change what is merged.
# Refuses (never moves the board) when: fetch fails or exceeds FetchTimeoutSec (the git child is killed); not on <branch>; tracked changes;
# HEAD is not an ancestor of <remote>/<branch> (diverged); the ff would change a profile boardOwnedPaths entry (tools/hooks, .claude or
# CLAUDE.md for MLV; that stays with refresh-hook-receipt.ps1, which re-pins the hook receipt); a merge-enqueue.ps1 / refresh-hook-receipt.ps1
# process is alive.
# Never edits, commits, stashes, resets or switches branches; the only mutation is `git merge --ff-only`. -WhatIf fetches and checks but
# does not merge and does not touch the state file (a behind board reports `ok would-ff`). -Force ignores the interval.
param(
    [string]$Board,
    [string]$Remote,
    [string]$Branch,
    [string]$ProfilePath = (Join-Path $PSScriptRoot 'board-profile.json'),
    [string]$StateFile,
    [string]$ScratchDir,
    [int]$MinIntervalSec = 600,
    [int]$FetchTimeoutSec = 60,
    [switch]$WhatIf,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'BoardProfile.psm1') -Force
$prof = Get-BoardProfile -Path $ProfilePath
if (-not $PSBoundParameters.ContainsKey('Board')) { $Board = $prof.boardRoot }
if (-not $PSBoundParameters.ContainsKey('Remote')) { $Remote = $prof.remote }
if (-not $PSBoundParameters.ContainsKey('Branch')) { $Branch = $prof.branch }
if (-not $PSBoundParameters.ContainsKey('ScratchDir')) {
    $ScratchDir = if ($env:BOARD_FF_SCRATCH) { $env:BOARD_FF_SCRATCH } else { Join-Path ([IO.Path]::GetTempPath()) "board-ff-$($prof.project)" }
}
if (-not $PSBoundParameters.ContainsKey('StateFile')) { $StateFile = Join-Path $ScratchDir 'board-ff-state.json' }
foreach ($d in @($ScratchDir, (Split-Path -Parent $StateFile))) {
    if ($d -and -not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

function Invoke-BoardGit([string[]]$GitArgs) {
    # git writes paths as UTF-8; decode them as UTF-8 whatever the console code page is.
    $oldEnc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}
    try { $o = & git -C $Board @GitArgs 2>&1 } finally { try { [Console]::OutputEncoding = $oldEnc } catch {} }
    $code = $LASTEXITCODE
    # stderr stays out of Out: a warning at exit 0 must never be read as a path or a status line.
    [pscustomobject]@{
        Code = $code
        Out  = (@($o | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) | ForEach-Object { "$_" }) -join "`n"
        Err  = (@($o | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }) | ForEach-Object { "$_" }) -join "`n"
    }
}
function Get-Sha8([string]$Rev) {
    $r = Invoke-BoardGit @('rev-parse', '--short=8', '--verify', '--quiet', "$Rev^{commit}")
    if ($r.Code -eq 0) { $r.Out.Trim() } else { '?' }
}
function New-Result([string]$Result, [string]$Reason, [string]$From, [string]$To, [string]$Sha = '') {
    [pscustomobject]@{ Result = $Result; Reason = $Reason; From = $From; To = $To; Sha = $Sha; Line = "BOARD-FF $Result $Reason from=$From to=$To" }
}

$target = "$Remote/$Branch"
$nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
if (-not $Force -and -not $WhatIf -and (Test-Path -LiteralPath $StateFile)) {
    $last = try { [int64](Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json).lastEpoch } catch { 0 }
    if (($nowEpoch - $last) -lt $MinIntervalSec) { return $null }
}
# Stamp first, so a hang or crash below is retried after the interval, not on every beat.
if (-not $WhatIf) { @{ lastEpoch = $nowEpoch } | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding ascii }

$from = Get-Sha8 'HEAD'

# 1. Bounded fetch. A hung network must not eat the beat: kill the git process tree at FetchTimeoutSec.
$fetchOut = Join-Path $ScratchDir 'board-ff-fetch-last.txt'
$fetchErr = Join-Path $ScratchDir 'board-ff-fetch-last.err.txt'
$oldPrompt = $env:GIT_TERMINAL_PROMPT; $env:GIT_TERMINAL_PROMPT = '0'
try {
    $fp = Start-Process -FilePath 'git' -ArgumentList @('-C', "`"$Board`"", 'fetch', $Remote, '--quiet') -NoNewWindow -PassThru -RedirectStandardOutput $fetchOut -RedirectStandardError $fetchErr
    if (-not $fp.WaitForExit($FetchTimeoutSec * 1000)) {
        try { $fp.Kill($true) } catch {}
        return (New-Result 'refused' "fetch-timeout (git fetch $Remote killed after ${FetchTimeoutSec}s)" $from (Get-Sha8 $target))
    }
    if ($fp.ExitCode -ne 0) {
        $why = (Get-Content -LiteralPath $fetchErr -Raw -ErrorAction SilentlyContinue); $why = if ($why) { ($why.Trim() -replace '\s+', ' ') } else { '' }
        if ($why.Length -gt 120) { $why = $why.Substring(0, 120) }
        return (New-Result 'refused' "fetch-failed exit=$($fp.ExitCode) $why" $from (Get-Sha8 $target))
    }
} finally { $env:GIT_TERMINAL_PROMPT = $oldPrompt }

# Pin the target ONCE, to a full commit SHA. The ref name is mutable (a concurrent fetch can move it), so every check below, the merge
# and the verification use this SHA and never the name.
$pinR = Invoke-BoardGit @('rev-parse', '--verify', '--quiet', "$target^{commit}")
$pin = $pinR.Out.Trim()
if ($pinR.Code -ne 0 -or -not $pin) { return (New-Result 'refused' "no-target-ref ($target)" $from '?') }
if ($pin -notmatch '^[0-9a-f]{40}$') { return (New-Result 'refused' "target-sha-invalid ($target resolved to '$pin')" $from '?') }
$to = $pin.Substring(0, 8)

# 2. Local-drift and board-owned-path refusals.
$br = (Invoke-BoardGit @('branch', '--show-current')).Out.Trim()
if ($br -ne $Branch) { return (New-Result 'refused' "wrong-branch (on '$br', need $Branch)" $from $to $pin) }
$st = Invoke-BoardGit @('status', '--porcelain', '-z', '--untracked-files=no')
if ($st.Code -ne 0) { return (New-Result 'refused' 'status-failed' $from $to $pin) }
if ($st.Out.Trim("`0", ' ', "`r", "`n")) { $n = @($st.Out -split "`0" | Where-Object { $_ -match '^[ MADRCTU?!]{2} ' }).Count; return (New-Result 'refused' "dirty-tracked ($n path(s))" $from $to $pin) }
if ((Invoke-BoardGit @('merge-base', '--is-ancestor', 'HEAD', $pin)).Code -ne 0) { return (New-Result 'refused' "diverged (HEAD is not an ancestor of $target)" $from $to $pin) }
$hk = Invoke-BoardGit (@('diff', '--name-only', '-z', 'HEAD', $pin, '--') + @($prof.boardOwnedPaths))
if ($hk.Code -ne 0) { return (New-Result 'refused' 'hook-diff-failed' $from $to $pin) }
$hkFiles = @($hk.Out -split "`0" | Where-Object { $_.Trim() })
if ($hkFiles.Count -gt 0) {
    $shown = ($hkFiles | Select-Object -First 5) -join ','
    return (New-Result 'refused' "hook-change-pending (left to refresh-hook-receipt.ps1; $($hkFiles.Count) file(s): $shown$(if ($hkFiles.Count -gt 5) { ',...' }))" $from $to $pin)
}
if ((Invoke-BoardGit @('rev-parse', 'HEAD')).Out.Trim() -eq $pin) { return (New-Result 'current' 'HEAD == target' $from $to $pin) }

# 3. Another board mover is alive: do nothing, so two actors never move the board at once.
$movers = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.ProcessId -ne $PID -and $_.Name -match '^(pwsh|powershell)\.exe$' -and $_.CommandLine -match '(?i)[\\/''"\s](merge-enqueue|refresh-hook-receipt)\.ps1'
    })
if ($movers.Count -gt 0) { return (New-Result 'refused' "mover-alive (pid $(($movers | ForEach-Object ProcessId) -join ','))" $from $to $pin) }

# 4. Fast-forward to the pinned SHA and verify HEAD is exactly there.
if ($WhatIf) { return (New-Result 'ok' 'would-ff (WhatIf, board untouched)' $from $to $pin) }
$mg = Invoke-BoardGit @('merge', '--ff-only', $pin)
$after = Get-Sha8 'HEAD'
$same = (Invoke-BoardGit @('rev-parse', 'HEAD')).Out.Trim() -eq $pin
if ($mg.Code -ne 0 -or -not $same) {
    $why = ((@($mg.Out, $mg.Err) -join ' ') -replace '\s+', ' '); if ($why.Length -gt 120) { $why = $why.Substring(0, 120) }
    return (New-Result 'refused' "merge-failed exit=$($mg.Code) head-not-at-target $why" $from $after $pin)
}
New-Result 'ok' "fast-forwarded to $target" $from $after $pin
