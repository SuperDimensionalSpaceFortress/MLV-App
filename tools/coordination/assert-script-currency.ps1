#!/usr/bin/env pwsh
# assert-script-currency.ps1 -- refuse to run a board-root script whose checkout is STALE.
#
# WHY THIS EXISTS
# .claude-state/ is gitignored, so the board root is the only checkout that holds it -- and the
# board root routinely sits on a PEER BRANCH (a diag/ or lane branch), not on master. A session
# that runs "pwsh -File <board-root>/tools/coordination/queue-derive.ps1" by absolute path then
# executes whatever version of that script the peer branch happens to carry. That version can be
# months old. The failure is SILENT: the script runs, exits 0, and prints a WRONG ANSWER.
#
# This has recurred at least seven times across sessions (grep, absolute-path invocation, owner-of
# resolving its map from the caller's cwd, importing a pre-fix checker module). Every previous
# mitigation was a document, and documents did not stop it. This one is executable.
#
# POLARITY: fail-CLOSED on proven drift, fail-OPEN on missing infrastructure. We stop only when we
# can positively demonstrate that the file on disk differs from the reference; if git is absent,
# the ref does not exist, or the file is not tracked there, we say nothing and let the caller run.
# A guard that blocks on "I could not check" would be worse than the bug.
#
# USAGE (three lines at the top of a guarded script, after param()):
#   $guard = Join-Path $PSScriptRoot 'assert-script-currency.ps1'
#   if (Test-Path -LiteralPath $guard) { & $guard -ScriptPath $PSCommandPath }
# A drift THROWS, which propagates out of the child script and terminates the caller.
#
# ESCAPE HATCH: set MLV_ALLOW_STALE_TOOLS=1 when you are deliberately running a modified copy --
# for example while developing a change to the guarded script itself. The refusal message names it.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ScriptPath,
    [string]$Ref = 'refs/remotes/fork/master',   # full refname: a same-named tag would win the bare name in rev-parse
    [switch]$PassThru
)
$ErrorActionPreference = 'Continue'

function Write-Result([string]$status, [string]$detail) {
    if ($PassThru) { Write-Output ([pscustomobject]@{ status = $status; detail = $detail; script = $ScriptPath; ref = $Ref }) }
}

if ($env:MLV_ALLOW_STALE_TOOLS -eq '1') { Write-Result 'skipped' 'MLV_ALLOW_STALE_TOOLS=1'; return }
if (-not (Test-Path -LiteralPath $ScriptPath)) { Write-Result 'unknown' 'script path does not exist'; return }

$full = (Resolve-Path -LiteralPath $ScriptPath).Path
$dir  = Split-Path -Parent $full

$root = (& git -C $dir rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $root) { Write-Result 'unknown' 'not a git working tree'; return }
$root = $root.Trim()

# Repo-relative, forward-slashed, case-preserved.
$rel = $full.Substring($root.Length).TrimStart([char]92, [char]47).Replace([string][char]92, '/')
if (-not $rel) { Write-Result 'unknown' 'could not derive a repo-relative path'; return }

# Does the reference exist at all? No 'fork' remote (a fresh clone, a lane sandbox) is not drift.
# r8 (sol r7 ADV-A1): resolve ONCE to a commit SHA and use only that SHA afterwards. The input is split at the FIRST
# revision operator (~ ^ @{ :). A base that is a full refname must exist EXACTLY - rev-parse would fall through to
# refs/tags/<name> for a missing ref, and does so for the base of an expression too (refs/remotes/fork/master~1 with the
# ref absent and a tag of that name present) - so the base is read with `show-ref --verify --hash` (r6 B1: a ref that
# vanishes after the read can never be re-resolved through a shadow tag) and only `<sha><suffix>^{commit}` is resolved.
# A base that is NOT a full refname keeps the r7 behaviour (the whole input goes to rev-parse once).
$refSpec = $Ref
$refSplit = [regex]::Match($Ref, '(?s)^(?<base>.*?)(?<suffix>(?:[~^:]|@\{).*)$')
$refBase = if ($refSplit.Success) { $refSplit.Groups['base'].Value } else { $Ref }
$refSuffix = if ($refSplit.Success) { $refSplit.Groups['suffix'].Value } else { '' }
if ($refBase -match '^refs/[^?*\[\\\s]+$' -and $refBase -notmatch '\.\.|//|/$') {
    $exactSha = [string](& git -C $root show-ref --verify --hash $refBase 2>$null | Select-Object -First 1)
    if ($exactSha -notmatch '^[0-9a-f]{40}$') { Write-Result 'unknown' "ref '$Ref' does not exist"; return }
    $refSpec = "$exactSha$refSuffix"
}
$refCommit = [string](& git -C $root rev-parse --verify --quiet --end-of-options "$refSpec^{commit}" 2>$null | Select-Object -First 1)
if ($refCommit -notmatch '^[0-9a-f]{40}$') { Write-Result 'unknown' "ref '$Ref' does not resolve"; return }

# Is the file tracked on that ref? A NEW file that has not landed yet is not stale.
& git -C $root cat-file -e "${refCommit}:${rel}" *> $null
if ($LASTEXITCODE -ne 0) { Write-Result 'untracked-on-ref' "not present at ${Ref}:${rel}"; return }

$refBlob = (& git -C $root rev-parse "${refCommit}:${rel}" 2>$null)
$diskBlob = (& git -C $root hash-object -- $full 2>$null)
if (-not $refBlob -or -not $diskBlob) { Write-Result 'unknown' 'could not hash one of the two versions'; return }
$refBlob = $refBlob.Trim(); $diskBlob = $diskBlob.Trim()

if ($refBlob -eq $diskBlob) { Write-Result 'current' "matches ${Ref}"; return }

$branch = (& git -C $root rev-parse --abbrev-ref HEAD 2>$null)
if ($branch) { $branch = $branch.Trim() } else { $branch = '(unknown)' }

$msg = @(
    "STALE SCRIPT REFUSED: $rel"
    "  checkout   : $root  (on branch '$branch')"
    "  on disk    : blob $($diskBlob.Substring(0,12))"
    "  at $Ref : blob $($refBlob.Substring(0,12))"
    ""
    "  This checkout's copy of the script DIFFERS from $Ref, so running it here would"
    "  execute code that is not the code you believe you are running. When the board root sits"
    "  on a peer branch this is silent: the script exits 0 and prints a WRONG ANSWER."
    ""
    "  Run it from a master-pinned worktree instead:"
    "    git -C `"$root`" worktree add --detach C:\mlvtmp\wt-tools $Ref"
    "    pwsh -NoProfile -File C:\mlvtmp\wt-tools\$rel <args>"
    ""
    "  If you are DELIBERATELY running a modified copy (developing a change to this very"
    "  script), set MLV_ALLOW_STALE_TOOLS=1 for that invocation."
) -join [Environment]::NewLine

Write-Result 'stale' $msg
throw $msg
