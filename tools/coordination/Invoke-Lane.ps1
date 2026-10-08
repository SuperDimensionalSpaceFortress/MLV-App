<#
.SYNOPSIS
    Invoke one fleet lane as a bounded CLI process. No seat, no lease, no registry.

.DESCRIPTION
    Claude-driven fleet topology (adopted 2026-08-29, Layi's ruling). Replaces the
    Codex-Desktop scheduled-automation lane model, under which the board spent
    seven heartbeat rotations in three days and landed zero product changes.

    THE CENTRAL DESIGN CHANGE: a lane is a PROCESS, not a SEAT.

    The prior topology simulated liveness in documents - seat-registry.json,
    health/leases/*.json, durableTaskId, automation.toml target_thread_id - and
    every one of those could disagree with reality. A lane was "alive" because a
    file said so, and the board deadlocked when the file was stale (LIVENESS-KEY-1,
    BOARD-DARK-1, and the v13-r15 transition-receipt block that left the hub dark
    for 53 hours).

    A process needs none of it. It is alive because it is running and dead when it
    exits, and the exit code is the truth. So this script:
      - takes NO lease and renews nothing
      - writes NO shared coordination state (no registry, no queue, no pen)
      - therefore requires NO seat and has no succession gate to pass

    The audit record is a per-invocation RECEIPT. Receipts accumulate; nothing is
    ever renewed or reconciled.

    WHAT THE RECEIPT ACTUALLY GUARANTEES - stated narrowly on purpose, because an
    earlier version of this header implied a durability the code did not provide:
      - its slot is reserved ATOMICALLY before any work starts, so concurrent
        lanes cannot overwrite each other's evidence
      - it is written from a finally block, so it exists on every exit path FROM
        THE RESERVATION ONWARD, including a throw; `complete` and `failure` say
        which path was taken. Argument-validation failures BEFORE the reservation
        (bad -PromptFile, unresolvable -WorkDir) produce NO receipt - correctly,
        because no slot was taken and no work was attempted. Verified by
        injecting a post-reservation throw: receipt written, complete=false,
        failure carried, exitCode -999.
      - prompt and output are hashed, so the record cannot drift from what ran
    It does NOT guarantee anything about a host that dies mid-write, and it makes
    no claim of immutability - a later run with the same explicit -RunDir and a
    freed slot could reuse the name.

.NOTES
    ASCII-only by project convention (non-ASCII in .ps1 has broken parsing here).
    Receipts are UTF-8 without BOM for the same reason.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('opus', 'sonnet', 'fable', 'sol', 'luna', 'astra')]
    [string]$Lane,

    [string]$Prompt,
    [string]$PromptFile,

    # Working root the lane reasons about. Defaults to the repo this script lives in.
    [string]$WorkDir,

    # Where receipts and outputs land. Defaults to a timestamped run dir.
    [string]$RunDir,

    [int]$TimeoutSec = 900,

    # Grant the lane write access. OFF by default: an analysis or review lane that
    # cannot mutate the tree cannot corrupt the thing it is judging.
    [switch]$AllowEdits,

    # Free-text tag recorded in the receipt (e.g. the card id).
    [string]$Card = '',

    # Backstop against a runaway lane. Claude only (codex exec has no equivalent).
    # 0 disables the cap. Measured 2026-09-03: real lanes used 13-21 turns, so 40 is a
    # runaway guard, NOT the spend control - that is -DenyBulkReads below.
    # Updated 2026-09-10: raised from 40 to 65 to prevent mid-work cutoffs (lanes executed 41 turns).
    [int]$MaxTurns = 65,

    # Optional per-process override; never changes the user's provider settings.
    [ValidateSet('', 'low', 'medium', 'high')]
    [string]$ReasoningEffort = '',

    # LANE-MODEL-CURRENCY-1: an explicit escape hatch that bypasses the normal model
    # resolution (a claude alias floating with the CLI, or a codex tier resolved at
    # launch from the current models cache) and launches this EXACT model id instead.
    # Recorded in the receipt as `pinnedByOverride` so a reader can never mistake an
    # overridden run for the fleet's own currency-preserving resolution.
    [string]$ModelOverride = '',

    # Let the lane read the bulk coordination files. OFF by default.
    # MEASURED 2026-09-03: three unattended fable lanes cost USD 22-25 EACH, every one
    # burning ~970,000 cache-creation tokens - almost exactly the 1.6 MB coordination
    # surface. Invoke-Workstream.ps1 already reads that bulk in PowerShell and inlines
    # the facts into a 4-7 KB brief, so a lane re-reading it pays full price for context
    # it was already given. A compact brief bounds the PROMPT; it does not bound the
    # READING, and nothing here did until now.
    [switch]$AllowBulkReads,

    # 0.1: the explicit tool allowlist an editing lane is granted. REQUIRED with
    # -AllowEdits; 'ALL' is never accepted (allowlist-required). Comma-separated,
    # passed straight through to claude's --allowedTools.
    [string]$AllowedTools = '',

    # 0.1: an extra directory the lane may read beyond -WorkDir (e.g. board
    # coordination paths an editing lane needs without a full -AllowBulkReads
    # grant). Optional; claude engine only (--add-dir).
    [string]$ExtraReadDir = '',

    # Disk hygiene (2026-09-14): when the lane exits, retire -WorkDir if it is a linked
    # worktree that passes the SAFE gate in Retire-LaneWorktree.ps1; otherwise keep it.
    # Either way the receipt carries `worktreeDisposition` saying which and why.
    # Never applies to the main checkout, and never deletes a branch ref.
    [switch]$RetireWorktree,

    # Disk hygiene (2026-10-03): the gate above runs at lane exit, BEFORE this lane's PR merges, so
    # it always answers `unmerged` and nothing asks again after the merge; lane worktrees piled up
    # at ~10 GiB/day. So every lane exit also re-asks the SAME gate (Invoke-SweepMergedLaneWorktrees)
    # about every OTHER linked worktree under -WorktreeSweepRoot. ON BY DEFAULT; -NoWorktreeSweep
    # (or a -WorktreeSweepRoot inside a test's tmp dir) opts out. The outcome is the receipt's
    # `worktreeSweep`; it can never throw or block the receipt.
    [string]$WorktreeSweepRoot = 'C:\mlvtmp',
    [switch]$NoWorktreeSweep,

    # Lane scratch. The child's TEMP/TMP point at <ScratchRoot>\<lane>-NNN, never bare
    # %TEMP% (which every project on this box shares). C:\mlvtmp is a DiskGuard-registered
    # MLV root. The run's own scratch dir is removed when the lane exits unless -KeepScratch;
    # its size is recorded in the receipt either way.
    [string]$ScratchRoot = 'C:\mlvtmp\lane-scratch',
    # Set only by Invoke-Workstream.ps1, which has already written the 'reserved' row for this
    # launch. This lane then writes a 'linked' row naming its receipt instead of a second
    # 'reserved' row, so the product-ratio guard counts the launch once.
    [string]$DispatchReservationId = '',
    [switch]$KeepScratch
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------------------------------------------------------------- lane table
# Engine, model and effort per lane. This table IS the topology; there is no
# other place a lane's model is decided, so a lane cannot silently drift onto
# the wrong tier the way a registry field could.
#
# LANE-MODEL-CURRENCY-1 (owner ruling 2026-09-26: always the latest model per tier, at
# HIGH effort -- never max/xhigh): no row here names a pinned model id. A claude-engine
# row's `model` is a CLI ALIAS ('opus'/'sonnet'/'fable') -- the alias floats with the
# claude CLI itself (verified 2026-09-26: `--model opus` resolved to canonicalModel
# `claude-opus-5-5`, `--model fable` to `claude-fable-5-1`), so a superseded pinned id
# (`claude-opus-5`, `claude-fable-5`, ...) can never sit here going stale. A codex-engine
# row's `tier` is resolved to the current highest-version slug AT LAUNCH by
# Resolve-CodexModelTier (below), which reads live from ~/.codex/models_cache.json --
# Codex has no floating alias, so this is the equivalent resolution step. Every effort is
# 'high': the owner's ruling is high for every lane, sonnet included, never max/xhigh.
$LANES = @{
    opus   = @{ engine = 'claude'; model = 'opus';   effort = 'high'; role = 'orchestrator' }
    sonnet = @{ engine = 'claude'; model = 'sonnet'; effort = 'high'; role = 'implementer' }
    fable  = @{ engine = 'claude'; model = 'fable';  effort = 'high'; role = 'review-guidance-planning' }
    sol    = @{ engine = 'codex';  tier = 'sol';     effort = 'high'; role = 'adversarial-verifier' }
    luna   = @{ engine = 'codex';  tier = 'luna';    effort = 'high'; role = 'breadth-recon' }
    astra  = @{ engine = 'codex';  tier = 'astra';   effort = 'high'; role = 'design-arbiter' }
}

# Absolute launcher paths. NEITHER is on the Git Bash PATH on this host, and a
# bare name resolves differently per shell - measured 2026-08-29.
$CLAUDE_EXE = Join-Path $env:APPDATA 'npm\claude.cmd'
$CODEX_EXE  = Join-Path $env:APPDATA 'npm\codex.cmd'

# LANE-MODEL-CURRENCY-1 round 2 (sol blocker 1): the REAL absolute launcher paths, named and
# computed separately from $CLAUDE_EXE/$CODEX_EXE above so the effort-must-be-high enforcement
# below can tell an actual CLI launch apart from the containment fixture, which reaches this
# launcher ONLY by text-replacing the two exact lines above with a disposable shim path
# (tests/coordination/test_lane_containment.py's prepare()). That substitution can never touch
# these two lines -- different text, never matched by the fixture's literal string replace -- so
# $REAL_CLAUDE_EXE/$REAL_CODEX_EXE always hold the genuine npm-cmd path even inside a
# fixture-patched copy of this script, and a launch whose $CLAUDE_EXE/$CODEX_EXE no longer equals
# its own $REAL_* twin is thereby proven incapable of ever reaching the real CLI.
$REAL_CLAUDE_EXE = Join-Path $env:APPDATA 'npm\claude.cmd'
$REAL_CODEX_EXE  = Join-Path $env:APPDATA 'npm\codex.cmd'

# LANE-NO-BACKGROUND-END-TURN-1 round 13 (hub ruling): rounds 8-12 each closed one hole in
# proving that whichever bash.exe/powershell.exe this launcher self-tested was the SAME
# executable Claude Code's own, undocumented shell auto-detection would spawn for a SHELL-FORM
# `--settings` hook command (a bare `command` string with no `args` field) -- a WSL launcher
# stub on PATH, a second Git Bash reachable only via PATH, an inherited
# `CLAUDE_CODE_GIT_BASH_PATH`. The vendor docs name the exit directly (code.claude.com/docs/en/
# hooks, "Exec form and shell form", fetched 2026-09-22; full quote in this round's summary.md):
# "Shell form runs when `args` is absent. The `command` string is passed to a shell... Exec form
# runs when `args` is present. Claude Code resolves `command` as an executable... and spawns it
# directly with `args` as the argument vector. There is no shell." EXEC FORM has no shell to
# resolve, classify, or enumerate candidates for -- so `Resolve-LaneExecutableAllCandidates`, the
# Git Bash and PowerShell resolvers, the per-shell-kind self-test loop, and the
# `MLV_GIT_BASH`/`MLV_LANE_POWERSHELL_EXE` overrides are DELETED, not disabled. See
# docs/lane-containment.md for the full removal rationale.
#
# What still needs resolving: the INTERPRETER (`command` in exec form still names an executable
# this launcher chooses). Resolved the same way rounds 9-10 established, for the reason they
# measured directly against hosted CI (run 35758428267, job 106850299120, 2026-09-22: a pinned
# per-user $PYTHON_EXE broke the runner, a resolved one does not) -- an explicit override env var
# (deterministic, test/operator use only), then curated known install locations, then PATH as a
# last resort. Known-locations-before-PATH guards against the same class of hazard the WSL stub
# was for bash (a PATH-shadowing shim that exists but does not run a real interpreter), even
# though no such Python shim has actually been measured on this host.
function Resolve-LaneExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$OverrideEnvName,
        [Parameter(Mandatory = $true)][string[]]$KnownLocations,
        [Parameter(Mandatory = $true)][string[]]$PathCommandNames
    )
    $override = [Environment]::GetEnvironmentVariable($OverrideEnvName)
    if ($override) {
        # An explicit override is AUTHORITATIVE: it does not fall through to known locations or
        # PATH even when the named path does not exist. A "force unresolvable" test (or an
        # operator ruling out a specific broken install) would not be deterministic if a real
        # known-location/PATH hit could silently rescue a deliberately-wrong override.
        if (Test-Path -LiteralPath $override -PathType Leaf) {
            return [pscustomobject]@{ Path = $override; Source = "override:$OverrideEnvName" }
        }
        return [pscustomobject]@{ Path = $null; Source = "override:$OverrideEnvName=$override (not found)" }
    }
    foreach ($loc in $KnownLocations) {
        if (Test-Path -LiteralPath $loc -PathType Leaf) {
            return [pscustomobject]@{ Path = $loc; Source = "known-location:$loc" }
        }
    }
    foreach ($name in $PathCommandNames) {
        $cmd = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd -and (Test-Path -LiteralPath $cmd.Source -PathType Leaf)) {
            return [pscustomobject]@{ Path = $cmd.Source; Source = "PATH:$name" }
        }
    }
    return [pscustomobject]@{ Path = $null; Source = 'unresolved (checked override, known locations, PATH)' }
}

$PYTHON_EXE_RESOLUTION = Resolve-LaneExecutable -OverrideEnvName 'MLV_LANE_PYTHON_EXE' `
    -PathCommandNames @('python.exe', 'python3.exe') `
    -KnownLocations @('C:/Users/obabalola/AppData/Local/Python/bin/python.exe')
$PYTHON_EXE = $PYTHON_EXE_RESOLUTION.Path
$LANE_NO_BACKGROUND_HOOK = Join-Path $PSScriptRoot 'lane-no-background.py'
$RESOLVE_CODEX_TIER_SCRIPT = Join-Path $PSScriptRoot 'resolve-codex-tier.py'

# LANE-MODEL-CURRENCY-1: resolves a codex-engine lane's TIER (e.g. 'sol') to the current
# highest-version slug via resolve-codex-tier.py, which reads live from
# ~/.codex/models_cache.json (overridable for tests via MLV_CODEX_MODELS_CACHE). FAILS
# CLOSED -- throws, never returns a guess -- when the interpreter is unresolved, the
# script cannot be invoked, its output does not parse, or it reports "ok": false (no
# cache, or no exact tier match). The caller's own top-level try/catch turns that throw
# into a well-formed 'failed' receipt naming this exact reason, the same pattern the
# background-gate self-test above already uses.
function Resolve-CodexModelTier {
    param(
        [Parameter(Mandatory = $true)][string]$Tier,
        [Parameter(Mandatory = $true)][string]$PythonExe
    )
    $resolverArgs = @($RESOLVE_CODEX_TIER_SCRIPT, '--tier', $Tier)
    if ($env:MLV_CODEX_MODELS_CACHE) { $resolverArgs += @('--cache', $env:MLV_CODEX_MODELS_CACHE) }
    try {
        $resolverOutput = & $PythonExe @resolverArgs 2>&1
        $resolverExit = $LASTEXITCODE
    } catch {
        throw "codex-tier-resolution-failed: interpreter invocation threw for tier '$Tier': $($_.Exception.Message)"
    }
    $resolverOutputText = ($resolverOutput -join "`n")
    try {
        $resolverParsed = $resolverOutputText | ConvertFrom-Json
    } catch {
        throw "codex-tier-resolution-failed: unparseable resolver output for tier '$Tier' (exit $resolverExit): $resolverOutputText"
    }
    # CODEX-RESOLVER-UNTYPED-EDGE-1 (fable hardening): the ok-check and error extraction live
    # INSIDE this try, and use the `.PSObject.Properties[name]` INDEXER -- never bare dot access
    # (`$resolverParsed.ok`, which throws PropertyNotFoundException under this file's own
    # `Set-StrictMode -Version Latest` when the resolver emits valid JSON that simply lacks an
    # `ok` key, e.g. a corrupted resolve-codex-tier.py) and never `.PSObject.Properties.Name
    # -contains name` (which throws enumerating an EMPTY PSMemberInfoCollection, e.g. `{}` -- the
    # exact hazard this file's own modelUsage comment above already documents and works around).
    # Both degenerate shapes now fail closed through the SAME typed 'codex-tier-resolution-failed'
    # token as an ordinary ok:false result, instead of leaking a raw StrictMode exception message.
    try {
        $okProp = $resolverParsed.PSObject.Properties['ok']
        $resolverOk = ($null -ne $okProp) -and [bool]$okProp.Value
        if ($resolverExit -ne 0 -or -not $resolverOk) {
            $errorProp = $resolverParsed.PSObject.Properties['error']
            $resolverError = if ($null -ne $errorProp) { $errorProp.Value } else { 'unknown' }
            throw "codex-tier-resolution-failed: tier '$Tier' did not resolve: $resolverError"
        }
    } catch {
        if ($_.Exception.Message -like 'codex-tier-resolution-failed:*') { throw }
        throw "codex-tier-resolution-failed: tier '$Tier' produced a malformed resolver result: $($_.Exception.Message)"
    }
    return [string]$resolverParsed.resolvedModel
}

# Every tool that either fans out to another agent (Agent, Task, Workflow, TaskCreate) or
# promises a LATER turn a headless lane cannot receive (Monitor, ScheduleWakeup, CronCreate,
# CronDelete, RemoteTrigger). ONE constant feeds the pre-reservation allowlist rejection, the
# --disallowedTools argv, and the receipt's authority.disallowedTools, so the enforcement
# points cannot drift apart the way sol PR #150 round 1 found them (rejection checked only
# Agent/Task while the deny list covered all seven). Round 3 (fable minor 1): Workflow spawns
# a background-orchestrated fan-out the launching lane cannot supervise, and TaskCreate is the
# same background-promise shape as ScheduleWakeup/CronCreate -- both belong beside the other
# six, not only in a caller's allowlist check.
$DENIED_TOOLS_DISPLAY = @('Agent', 'Task', 'Monitor', 'ScheduleWakeup', 'CronCreate', 'CronDelete', 'RemoteTrigger', 'Workflow', 'TaskCreate')
$DENIED_TOOLS = @($DENIED_TOOLS_DISPLAY | ForEach-Object { $_.ToLowerInvariant() })

function Get-Sha256([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '')
    } finally { $sha.Dispose() }
}

# LANE-NO-BACKGROUND-END-TURN-1 round 3 (sol minor / fable minor 2): a porcelain STATUS LINE
# (e.g. ' M tracked.txt') is the same text before and after a lane makes a SECOND edit to a
# file that was already dirty when the lane started -- the status code does not change, only
# the content does -- so round 2's line-text comparison could not see it. This returns a
# path -> content-identity map instead: the sha256 of `git diff HEAD -- <path>`, which changes
# whenever the path's actual content (staged or unstaged, relative to HEAD) changes, covering
# modify/add/delete uniformly. Callers compare this map's VALUES for a given key, not just key
# presence, so a further edit to an already-dirty path is visible even though the path was
# already in the map.
#
# Round 4 (sol minor / fable minor 3): round 3's parser read `git status --porcelain` (no -z)
# and stripped one leading/trailing '"' per path -- git's default quoting escapes a space with
# nothing (a space needs no quoting) but escapes non-ASCII bytes as C-style octal sequences
# ('\346\226\207...') and wraps the whole path in quotes when it does, so a tracked path with
# both a space AND a non-ASCII character round-tripped through Trim('"') as a still-escaped,
# not-actually-restored string that never matched the real on-disk path. `-z` asks git for
# NUL-separated records with quoting turned OFF entirely: each record is "XY <path>" for an
# ordinary change, or "XY <newpath>\0<origpath>\0" for a rename/copy (two NUL-separated fields,
# no " -> " text to parse). PowerShell's line-based capture of `&` output only ever splits on
# LF, never NUL, so the whole -z stream comes back as one string to split ourselves. `&`-captured
# output is also decoded using [Console]::OutputEncoding, which is not reliably UTF-8 in this
# script's redirected/non-interactive invocation -- measured: a non-ASCII byte in a path
# silently mis-decoded, so the returned path never matched the real on-disk path and the diff
# lookup below found nothing for it. Invoke-GitCaptureUtf8 routes through ProcessStartInfo with
# an explicit StandardOutputEncoding instead (the same mechanism this script already uses for
# provider child processes), which sidesteps console state entirely.
#
# Round 5 (sol minor 1 / fable minor 1): every caller used to receive plain '' on ANY failure --
# process-start failure, a stream-read exception, or (previously unchecked entirely) a non-zero
# git exit -- indistinguishable from '' being git's true, successful, empty output (e.g. `git
# status --porcelain` on a clean tree). A caller could not tell "git could not be asked" from
# "git answered: nothing is dirty", which is exactly the false-clean the dirty-no-commit check
# below exists to rule out. This now returns a result object every caller must check `.ok` on
# before touching `.stdout`; `.error` names WHERE the call failed, not git's raw stderr text (the
# receipt is not the place to surface arbitrary process output as if it were a diagnosis).
function Invoke-GitCaptureUtf8([string]$WorkDir, [string[]]$GitArgs) {
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'git'
    foreach ($a in (@('-C', $WorkDir) + $GitArgs)) { [void]$psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
    } catch {
        return [ordered]@{ ok = $false; stdout = $null; exitCode = $null; error = "git-start-failed: $($_.Exception.Message)" }
    }
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    $p.WaitForExit()
    try {
        $out = $outTask.GetAwaiter().GetResult()
    } catch {
        return [ordered]@{ ok = $false; stdout = $null; exitCode = $p.ExitCode; error = "git-read-failed: $($_.Exception.Message)" }
    }
    if ($p.ExitCode -ne 0) {
        $errText = try { $errTask.GetAwaiter().GetResult() } catch { '' }
        return [ordered]@{ ok = $false; stdout = $out; exitCode = $p.ExitCode; error = ("git-exit-{0}: {1}" -f $p.ExitCode, $errText).Trim() }
    }
    return [ordered]@{ ok = $true; stdout = $out; exitCode = 0; error = $null }
}

# Round 5 (sol minor 2): content identity of a dirty tracked path is now the git blob hash of the
# WORKING-TREE FILE ITSELF (`git hash-object`), not a hash of rendered `git diff` text. Diff
# rendering passes through config a caller does not control here (textconv filters, whitespace/
# rename-detection options), which can make two DIFFERENT on-disk contents render as the SAME
# diff text -- the opposite of what a content-identity check must guarantee. `hash-object`
# reports the identity of the bytes git would actually commit, with nothing interpretive between
# the file and the hash. A path absent from the working tree (deleted, or a rename's old path
# already consumed below) has no bytes to hash, so it gets a fixed 'DELETED' marker instead --
# distinct from any real hash-object output, so a delete can never collide with a real blob hash.
#
# Returns a result object, never a bare map: a git failure partway through (e.g. `status`
# succeeds but a later `hash-object` fails) must be visible to the caller as "this snapshot could
# not be taken" rather than silently returning whatever partial map had been built so far, which
# would read as "here is the complete truth" when it is not.
function Get-TrackedDirtyContentIdentity([string]$WorkDir) {
    $statusResult = Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('status', '--porcelain=v1', '-z')
    if (-not $statusResult.ok) {
        return [ordered]@{ ok = $false; map = $null; reason = "git status failed: $($statusResult.error)" }
    }
    $raw = $statusResult.stdout
    if ([string]::IsNullOrEmpty($raw)) { return [ordered]@{ ok = $true; map = [ordered]@{}; reason = $null } }
    $fields = @($raw -split "`0" | Where-Object { $_ -ne '' })
    $map = [ordered]@{}
    $i = 0
    while ($i -lt $fields.Count) {
        $entry = $fields[$i]; $i++
        if ($entry.Length -lt 3) { continue }
        $statusCode = $entry.Substring(0, 2)
        $path = $entry.Substring(3)
        if ($statusCode[0] -eq 'R' -or $statusCode[0] -eq 'C') {
            # Rename/copy: $path (just read) is the NEW path -- the one that still exists
            # in the working tree and is what content identity keys on. The ORIGINAL path
            # is the NEXT NUL-separated field; consume it here so it is never mistaken for
            # a separate status entry of its own.
            if ($i -lt $fields.Count) { $i++ }
        }
        if ($statusCode -eq '??') { continue }
        $fullPath = Join-Path $WorkDir $path
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            $map[$path] = 'DELETED'
            continue
        }
        $hashResult = Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('hash-object', '--', $path)
        if (-not $hashResult.ok) {
            return [ordered]@{ ok = $false; map = $null; reason = "git hash-object failed for '$path': $($hashResult.error)" }
        }
        $map[$path] = $hashResult.stdout.Trim()
    }
    return [ordered]@{ ok = $true; map = $map; reason = $null }
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    # UTF-8 WITHOUT BOM: a BOM has broken JSON consumers on this box before.
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Add-DispatchLedgerRow([string]$Path, $Row) {
    # MLV-DISPATCH-LEDGER-WRITER-V3-SERIALIZED - the product-ratio guard requires this exact token in BOTH ledger writer files of every
    # registered checkout. Change it only together with a change to how rows are serialized.
    # SERIALIZED with every other ledger writer by one machine-wide mutex. FileMode.Append records EOF
    # as each stream's private offset, so two unserialized writers can overwrite each other's row.
    # Invoke-Workstream.ps1's Write-DispatchReservation takes the SAME mutex name; keep them equal.
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Row | ConvertTo-Json -Compress -Depth 4) + "`n")
    $mutex = [Threading.Mutex]::new($false, 'Global\MLV-App-DispatchLedger')
    $held = $false
    try {
        try { $held = $mutex.WaitOne(30000) } catch [Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw 'dispatch ledger lock timeout' }
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Write-Utf8NoBomAtomic([string]$Path, [string]$Content) {
    $tmp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($tmp, $Content, [Text.UTF8Encoding]::new($false))
        # CI-FLAKE-LANE-CONTAINMENT-OWNER-LOSS-MOVE-1: replacing a file that another process holds open
        # without FILE_SHARE_DELETE (a polling reader such as Python's open(), an AV scan, the indexer)
        # throws "Access to the path is denied" for as long as that handle lives. The lock is transient, and
        # one unretried throw turned a healthy lane into exitCode -999 / state=failed (PR #232, #272, #289).
        # Retry with a short bounded backoff; the final attempt's exception still propagates unchanged.
        $retry = [Diagnostics.Stopwatch]::StartNew(); $delayMs = 10
        while ($true) {
            try { [IO.File]::Move($tmp, $Path, $true); break }
            catch [UnauthorizedAccessException], [IO.IOException] {
                if ($retry.ElapsedMilliseconds -ge 2000) { throw }
                Start-Sleep -Milliseconds $delayMs; $delayMs = [Math]::Min($delayMs * 2, 200)
            }
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
}

# Windows job ownership is established around an inert PowerShell host before that
# host receives any provider configuration. Descendants then inherit kill-on-close.
if (-not ('MlvLaneJob' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class MlvLaneJob {
  [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS { public UInt64 a,b,c,d,e,f; }
  [StructLayout(LayoutKind.Sequential)] struct BASIC_LIMIT {
    public Int64 PerProcessUserTimeLimit, PerJobUserTimeLimit;
    public UInt32 LimitFlags;
    public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
    public UInt32 ActiveProcessLimit;
    public UIntPtr Affinity;
    public UInt32 PriorityClass, SchedulingClass;
  }
  [StructLayout(LayoutKind.Sequential)] struct EXTENDED_LIMIT {
    public BASIC_LIMIT BasicLimitInformation;
    public IO_COUNTERS IoInfo;
    public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
  }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern IntPtr CreateJobObject(IntPtr attributes, string name);
  [DllImport("kernel32.dll", SetLastError=true)]
  static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool CloseHandle(IntPtr handle);
  public static IntPtr CreateKillOnClose() {
    IntPtr job=CreateJobObject(IntPtr.Zero, null);
    if(job==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject");
    EXTENDED_LIMIT value=new EXTENDED_LIMIT();
    value.BasicLimitInformation.LimitFlags=0x2000;
    int size=Marshal.SizeOf(value); IntPtr mem=Marshal.AllocHGlobal(size);
    try {
      Marshal.StructureToPtr(value,mem,false);
      if(!SetInformationJobObject(job,9,mem,(uint)size)) {
        int error=Marshal.GetLastWin32Error(); CloseHandle(job);
        throw new Win32Exception(error,"SetInformationJobObject");
      }
    } finally { Marshal.FreeHGlobal(mem); }
    return job;
  }
  public static void AssignOrThrow(IntPtr job, IntPtr process) {
    if(!AssignProcessToJobObject(job,process))
      throw new Win32Exception(Marshal.GetLastWin32Error(),"AssignProcessToJobObject");
  }
}
'@
}

# ---------------------------------------------------------------- resolve inputs
if (-not $Prompt -and -not $PromptFile) { throw 'Supply -Prompt or -PromptFile.' }
if ($PromptFile) {
    if (-not (Test-Path -LiteralPath $PromptFile)) { throw "PromptFile not found: $PromptFile" }
    $Prompt = Get-Content -LiteralPath $PromptFile -Raw
}

if (-not $WorkDir) { $WorkDir = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path }
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path

# ---------------------------------------------------------------- 0.1 pre-flight (before any process, before any run dir)
# (a) A codex lane can never be granted write access: no Claude hook is visible to codex exec,
# so nothing here could enforce NA-1..NA-10 against it. Keyed on ENGINE, not an enumerated list
# of lane names -- a new codex row (e.g. astra) refuses edits by construction, with nothing left
# to remember to add to a name list (that omission was the hole this rekey closes).
if ($AllowEdits -and $LANES[$Lane].engine -eq 'codex') {
    throw "codex-lane-never-edits: -Lane $Lane with -AllowEdits (no Claude hook is visible to codex exec)"
}
# (b) An editing lane's tool grant must be an explicit, auditable list. 'ALL' is
# never accepted - that is the exact grant this whole patch exists to narrow.
if ($AllowEdits -and ([string]::IsNullOrWhiteSpace($AllowedTools) -or $AllowedTools -eq 'ALL')) {
    throw "allowlist-required: -AllowEdits requires -AllowedTools <comma-separated list>; 'ALL' is never granted"
}
# Nested agent dispatch and every background-promise tool are forbidden even when embedded in a
# caller-supplied editing allowlist. Checked against the SAME $DENIED_TOOLS list the
# --disallowedTools argv is built from below, not a re-enumerated Agent/Task pair -- sol PR #150
# round 1 found the two lists had drifted (rejection: Agent/Task only; deny list: seven tools).
# Normalize comma tokens for the decision; preserve the original argv text.
# Round 4 (sol major 1): explicitly gated to engine -eq 'claude', not just $AllowEdits. Today the
# codex-never-edits throw at (a) above always fires first for any codex+-AllowEdits combination,
# so this block is already unreachable for codex in practice -- but that unreachability was an
# ACCIDENT of check ORDER, not a stated invariant, and DENIED_TOOLS/DENIED_TOOLS_DISPLAY are
# claude-CLI tool names (Agent/Task/Monitor/... are claude flags; codex has no --disallowedTools
# equivalent and its own $AllowedTools value is never consumed for argv -- see the codex argv
# branch below, which hardcodes authority.allowedTools = 'ALL'). Codex lanes keep their previous
# behaviour: this check never applies to them, explicitly, independent of check order elsewhere.
if ($AllowEdits -and $LANES[$Lane].engine -eq 'claude') {
    $forbiddenTools = @($AllowedTools -split ',' | ForEach-Object { $_.Trim().ToLowerInvariant() } |
        Where-Object { $DENIED_TOOLS -contains $_ })
    if ($forbiddenTools.Count -gt 0) {
        throw "nested-agent-tool-forbidden: -AllowedTools cannot contain $($forbiddenTools -join ', ')"
    }
}
# MLV_BOARD_ROOT: only a test sets it (a tmp-dir board fixture); the default is the
# real board (mirrors Start-EditingLane.ps1's own resolution, O107).
$RepoRoot = if ($env:MLV_BOARD_ROOT) { $env:MLV_BOARD_ROOT } else { 'C:\!Layi Wkspc\MLV-App' }
$HookEnforcedReceipt = Join-Path $RepoRoot '.claude-state\coordination\dual-lane\receipts\0.05-hook-enforced.json'
$DispatchLedgerPath  = Join-Path $RepoRoot '.claude-state\coordination\dual-lane\receipts\dispatch-reservations.jsonl'
# Invoke-Workstream passes its reservation id through the environment so it also crosses
# Start-EditingLane.ps1. It is consumed here and removed from the child's environment below, so a
# process the lane starts can never claim the dispatcher's reservation.
if (-not $DispatchReservationId -and $env:MLV_DISPATCH_RESERVATION_ID) { $DispatchReservationId = $env:MLV_DISPATCH_RESERVATION_ID }
$WorkDirHookPath     = Join-Path $WorkDir 'tools\hooks\mlv-never-authorized.py'
# hookSha256 is computed unconditionally (when the file exists) so the receipt can
# always carry it, per 0.1's receipt-fields requirement - not only on editing lanes.
$WorkDirHookSha256 = if (Test-Path -LiteralPath $WorkDirHookPath) {
    (Get-FileHash -LiteralPath $WorkDirHookPath -Algorithm SHA256).Hash.ToLowerInvariant()
} else { $null }
# (e) The worktree's OWN hook copy is what actually governs an editing lane's
# session (Claude Code loads it from -WorkDir), so this checks THAT copy against
# the board's receipt of what the ratified hook hashes to - never the board root's
# own copy, which the lane never runs against.
if ($AllowEdits) {
    if (-not (Test-Path -LiteralPath $HookEnforcedReceipt)) {
        throw "hook-not-enforced: receipt missing at $HookEnforcedReceipt"
    }
    if ($null -eq $WorkDirHookSha256) {
        throw "hook-not-enforced: hook script missing in worktree: $WorkDirHookPath"
    }
    $hookEnforcedRec = Get-Content -LiteralPath $HookEnforcedReceipt -Raw | ConvertFrom-Json
    $receiptHookSha256 = ([string]$hookEnforcedRec.hookSha256).ToLowerInvariant()
    if ($WorkDirHookSha256 -ne $receiptHookSha256) {
        throw ("hook-not-enforced: worktree hook sha256={0} != receipt hookSha256={1}" -f $WorkDirHookSha256, $receiptHookSha256)
    }
}
# Round 7 (sol minor): routed through Invoke-GitCaptureUtf8 instead of a bare `& git ...`
# catch, so a failed rev-parse is distinguishable from "this gate does not apply to this
# lane" downstream -- the old form read a failed call and a legitimately-inapplicable gate
# as the same falsy $BaseSha, which the post-exit block then misclassified as 'not-applicable'
# instead of 'unavailable' (or, at post-exit, as "HEAD moved").
$BaseShaCapture = Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('rev-parse', 'HEAD')
$BaseSha = $null
$BaseShaCaptureFailureReason = $null
if ($BaseShaCapture.ok) {
    $trimmed = $BaseShaCapture.stdout.Trim()
    if (-not [string]::IsNullOrWhiteSpace($trimmed)) { $BaseSha = $trimmed }
} else {
    $BaseShaCaptureFailureReason = "pre-launch rev-parse HEAD failed: $($BaseShaCapture.error)"
}
# LANE-NO-BACKGROUND-END-TURN-1 round 2 (sol major 1 / fable minor 3): the dirty-no-commit
# override below applies ONLY to editing Claude lanes, and only to tracked dirt the LANE
# ITSELF introduced. Snapshot a per-path CONTENT IDENTITY (round 3: sol minor / fable minor 2 --
# a raw status line cannot tell a further edit of an already-dirty path from no edit at all)
# HERE, before the child ever starts, so pre-existing dirt in a shared or already-dirty
# worktree can never be mistaken for work the lane left behind. Nothing between here and
# process launch touches $WorkDir's tracked files (settings.json and the prompt file are
# written under $RunDir / the board root, not $WorkDir), so this snapshot is equivalent to
# "immediately before launch".
$InitialTrackedDirt = $null
# Round 5 (sol minor 1): a failed PRE-launch snapshot must never read as "nothing was dirty" --
# that reading is a false-clean the post-exit comparison could then act on as ground truth. Kept
# separate from $InitialTrackedDirt itself (rather than folding a sentinel into the map) so the
# post-exit block below can distinguish "captured, empty" from "never captured" without
# inspecting map contents.
$InitialTrackedDirtUnavailableReason = $null
if ($AllowEdits -and $LANES[$Lane].engine -eq 'claude' -and -not $BaseShaCaptureFailureReason -and $BaseSha) {
    $preSnapshot = Get-TrackedDirtyContentIdentity -WorkDir $WorkDir
    if ($preSnapshot.ok) {
        $InitialTrackedDirt = $preSnapshot.map
    } else {
        $InitialTrackedDirtUnavailableReason = $preSnapshot.reason
    }
}

if (-not $RunDir) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    # The BOARD's fleet-runs, not the worktree's: a receipt inside a worktree vanishes with it, and the
    # product-ratio guard's coverage check reads the board tree first.
    $RunDir = Join-Path $RepoRoot ".claude-state\fleet-runs\$stamp"
}
if (-not (Test-Path -LiteralPath $RunDir)) { New-Item -ItemType Directory -Path $RunDir -Force | Out-Null }
$RunDir = (Resolve-Path -LiteralPath $RunDir).Path

$cfg = $LANES[$Lane].Clone()
if ($ReasoningEffort) { $cfg.effort = $ReasoningEffort }

# ---------------------------------------------------------------- model identity (LANE-MODEL-CURRENCY-1)
# requestedModel is the lane's IDENTITY as authored in the table: an alias for a claude
# lane, a tier for a codex lane. Recorded even when -ModelOverride bypasses the normal
# resolution below, so a receipt always shows what was ASKED for beside what actually
# ran. resolvedModel starts 'unknown' -- a third state, never the requested string
# copied over -- and is filled in per engine below: a codex lane fills it the instant
# its tier resolves (or its override is accepted); a claude lane fills it only after the
# child exits, from the canonicalModel(s) its own JSON output reports it actually ran as.
$RequestedModel = if ($cfg.engine -eq 'claude') { $cfg.model } else { $cfg.tier }
$PinnedByOverride = [bool]$ModelOverride
$ResolvedModel = 'unknown'
$AuxiliaryModels = @()
# LANE-MODEL-CURRENCY-1 round 2 (sol blocker 2): selectedModel is the PRE-LAUNCH choice (the
# exact `--model`/`-m` argument value) -- filled in per engine below, the instant that choice is
# made, never mistaken for proof the child ran. resolvedModel is filled in ONLY from evidence
# gathered after the child is known to have run (a codex lane: after WaitForExit, from a clean
# exit that produced real output; a claude lane: from its own JSON modelUsage, unchanged from
# above). A launch that never starts (Process.Start throws) still leaves selectedModel recorded,
# so a reader can always see what was ASKED to start, but resolvedModel stays 'unknown' -- never
# copied from selectedModel before the child is proven to have run.
$SelectedModel = $null

# LANE-MODEL-CURRENCY-1 round 2 (sol blocker 1): owner ruling 2026-09-26 is that EVERY lane runs
# at high effort. $cfg.effort is already the fully-resolved effective value at this point (table
# default, then any -ReasoningEffort override just above), so this is the one place that can
# refuse a non-high value before it ever reaches a real provider process. The refusal is skipped
# only when $CLAUDE_EXE/$CODEX_EXE no longer equal their own $REAL_* twins declared beside them --
# the one way that happens is the containment fixture's text-replace of those two lines with a
# disposable shim (tests/coordination/test_lane_containment.py's prepare()), which by
# construction can never reach a real model regardless of what effort value is requested. The
# actual `throw` is deferred to just inside the main try below, so a refusal still produces a
# normal 'failed' receipt and non-zero exit rather than a silent pre-reservation script error.
$RealExeForLane = if ($cfg.engine -eq 'claude') { $REAL_CLAUDE_EXE } else { $REAL_CODEX_EXE }
$ConfiguredExeForLane = if ($cfg.engine -eq 'claude') { $CLAUDE_EXE } else { $CODEX_EXE }
$LaneEffortMustBeHighRefusal = $null
if ($ConfiguredExeForLane -eq $RealExeForLane -and $cfg.effort -ne 'high') {
    $LaneEffortMustBeHighRefusal = "lane-effort-must-be-high: a real lane may only run at 'high' effort (owner ruling 2026-09-26); refusing effort '$($cfg.effort)' for lane '$Lane'."
}

# ATOMIC SLOT RESERVATION. The previous form was
#     while (Test-Path <candidate>) { $n++ }
# which is CHECK-THEN-ACT: two lanes launched concurrently into the same run dir
# both see the slot free, both pick it, and the second silently overwrites the
# first one's evidence. This session ran concurrent lanes, so the race was live.
# FileMode::CreateNew is a SINGLE filesystem operation that FAILS if the path
# already exists, so the winner is decided by the OS, not by our timing.
$base = $null
for ($n = 1; $n -le 999; $n++) {
    $candidate = Join-Path $RunDir ("{0}-{1:d3}" -f $Lane, $n)
    try {
        $fs = [System.IO.File]::Open(
            "$candidate.receipt.json",
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None)
        $fs.Dispose()
        $base = $candidate
        break
    } catch [System.IO.IOException] {
        continue   # someone else owns this slot; try the next
    }
}
if ($null -eq $base) { throw "Could not reserve a receipt slot in $RunDir after 999 attempts." }
$promptPath = "$base.prompt.txt"
$outPath    = "$base.out.txt"
$errPath    = "$base.err.txt"
$lastPath   = "$base.last.txt"
$rcptPath   = "$base.receipt.json"
$settingsPath = "$base.settings.json"

# RESERVED MARKER, written the instant the slot is taken and before ANY work.
# The atomic reservation above creates an EMPTY file, which is a valid claim but
# INVALID JSON - anything reading receipts while a lane is in flight crashes on
# it, and a crash between reservation and completion would leave that empty file
# as the only record. This is PEN-GAP-1's concern in receipt form: durable
# mutation must not precede durable explanation. The slot now always parses and
# always says what it is.
Write-Utf8NoBom $rcptPath (([ordered]@{
    schema   = 'mlv-app/fleet-lane-receipt/v1'
    state    = 'reserved'
    complete = $false
    lane     = $Lane
    card     = $Card
    reservedUtc = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json -Depth 4))

# CRASH-TOTAL RECEIPT. Every field the receipt needs is initialised BEFORE the
# try, so a failure anywhere still produces a well-formed receipt instead of
# nothing. The old code wrote the receipt only on the normal tail, outside any
# try/finally, with $ErrorActionPreference='Stop' - so any throw yielded ZERO
# receipt and a failed run was indistinguishable from one that never ran. That
# is the exact defect this runner's own card list called SWEEP-RECEIPT-1.
$startedUtc = (Get-Date).ToUniversalTime()
$sw         = [System.Diagnostics.Stopwatch]::StartNew()
$exitCode   = -999
$timedOut   = $false
$final      = ''
$failure    = $null
# Initialized here, before the try, so the finally never resolves a parent-scope $scratchDir
# (dynamic scoping) and removes a path this run did not create.
$scratchDir = $null
# A PROVIDER REFUSAL is a third outcome beside ran/threw: the child exited cleanly and
# the provider did no work. Detected from raw output after harvest; see lane-provider-refusal.ps1.
$providerRefusal = $null
# Set at the top of the try; initialised here so the finally's receipt never reads an unset variable.
$dispatchLedgerState = $null
$dispatchLedgerReservationId = $null
# Harvested child stdout; initialised here so the finally can classify work evidence on every path.
$stdout = ''
$authority  =[ordered]@{ permissionMode = 'unset'; allowedTools = 'unset'; sandbox = 'unset'; writableRoot = $null }
$denyRules  = @()
# LANE-NO-BACKGROUND-END-TURN-1 round 7: set here so a codex lane (which never enters the
# claude branch below) can be checked against $null under Set-StrictMode without a separate
# engine test at every read site.
$hookCopyPath = $null
$backgroundHookLaunchSha256 = $null
$backgroundInterpreterLaunchSha256 = $null
$jobHandle = [IntPtr]::Zero
$jobAssigned = $false
$promptDelivered = $false
$containedHost = $null
# Set to $true the INSTANT Process::Start returns for the claude engine (a plain
# boolean assignment cannot throw), BEFORE the pid/dictionary construction that
# builds $containedHost -- which CAN throw (PR #105 round 4). $containedHost alone
# cannot tell "Start was never called/never returned" apart from "Start returned
# but the record of it never got built"; $hostStarted can, because it is set
# unconditionally the moment a host process exists.
$hostStarted = $false
# Captured on its OWN line the instant Start returns (PR #105 round 5, this
# packet): a bare property read on a live Process, which the .NET contract
# binds before Start returns and which therefore cannot throw. Kept separate
# from $containedHost (whose own dictionary/build CAN still throw) so the
# post-start-unrecorded fallback always has a pid to report -- the pid is the
# only channel by which anyone later learns an orphan existed.
$hostPid = $null
# Fixed tokens for containment.ownerAbsentReason, chosen by WHERE the failure
# happened rather than by what its exception message said -- free text is not
# admissible evidence about a safety property (PR #105 round 4). Kept in one
# place; tests/coordination/test_lane_containment.py pins these as literals
# since it cannot import a .ps1 file, with a comment pointing back here.
$OWNER_ABSENT_NO_HOST_BUDGET = 'launch-budget-exhausted'  # unchanged text: line ~471's throw message, asserted verbatim since PR #105 round 3
$OWNER_ABSENT_NO_HOST_START_THREW = 'start-threw'         # Process::Start itself threw; no host was ever created
$OWNER_ABSENT_POST_START_UNRECORDED = 'post-start-unrecorded'  # Start returned (a host EXISTS) but $containedHost's own construction threw -- the genuinely ambiguous state
# Outcome of the PRE-ASSIGNMENT kill at line ~677 (host started but never
# joined the job, so a swallowed kill failure there is a genuine orphan, unlike
# the post-timeout kill at line ~602 where the job is kill-on-close and the
# tree is already terminated). A kill cannot be made infallible, but its
# failure can be made visible instead of vanishing into a bare `catch { }`
# (PR #105 round 5, sol PR #105 blocker).
$ownerKillAttempted = $false
$ownerKillOutcome = $null
$ownerKillDetail = $null
$OWNER_KILL_OUTCOME_ALREADY_EXITED = 'already-exited'  # host had already exited before the pre-assignment kill was attempted
$OWNER_KILL_OUTCOME_KILLED = 'killed'                  # Kill() did not throw AND WaitForExit's own return value was $true -- the host was OBSERVED to exit within the bounded wait
$OWNER_KILL_OUTCOME_KILL_WAIT_TIMEOUT = 'kill-wait-timeout'  # Kill() did not throw, but WaitForExit's return value was $false -- the bounded wait expired before the host was observed to exit. It MAY STILL BE ALIVE. (PR #105 final: WaitForExit(Int32) returns a bool and round 5 discarded it with [void], so a host that outlived the wait was misreported as 'killed' -- worse than the bare catch{} it replaced, since it manufactured false evidence instead of merely omitting true evidence.)
$OWNER_KILL_OUTCOME_KILL_THREW = 'kill-threw'          # Kill() or WaitForExit itself threw -- see ownerKillDetail for the raw message
$childIdentity = $null
$deadlineUtc = $startedUtc.AddSeconds($TimeoutSec)
$containment = $null
$proc = $null
# $null, never 0. An engine that does not REPORT cost and a run that cost nothing are
# different facts and this receipt will not merge them - the same rule the dispatcher's
# malformed-row counter follows.
$costUsd = $null; $numTurns = $null
$cacheCreateTokens = $null; $cacheReadTokens = $null; $outputTokens = $null
. (Join-Path $PSScriptRoot 'lane-provider-refusal.ps1')

try {

# LANE-MODEL-CURRENCY-1 round 2 (sol blocker 1): thrown here, first thing inside the main try --
# after the receipt slot is reserved (so this refusal gets a normal 'failed' receipt and non-zero
# exit, not a silent pre-reservation script error) but before the dispatch ledger row or any
# provider process, so a refused launch is never counted as an attempt.
if ($LaneEffortMustBeHighRefusal) { throw $LaneEffortMustBeHighRefusal }

# DISPATCH LEDGER ROW (plan 0.6: version-enforced all-venue accounting). Written after the receipt
# slot exists, so the row can name it, and before any provider process starts. A direct launch
# writes 'reserved' and counts toward the product-ratio guard's rate; a launch that
# Invoke-Workstream already reserved writes 'linked' and does not. A row that cannot be written
# refuses the launch, because a launch the ledger does not show breaks coverage for seven days.
$dispatchLedgerState = if ($DispatchReservationId) { 'linked' } else { 'reserved' }
$dispatchLedgerReservationId = if ($DispatchReservationId) { $DispatchReservationId } else { [guid]::NewGuid().ToString() }
try {
    Add-DispatchLedgerRow -Path $DispatchLedgerPath -Row ([ordered]@{
        schemaVersion = 2
        venue         = 'invoke-lane'
        reservationId = $dispatchLedgerReservationId
        state         = $dispatchLedgerState
        card          = $Card
        lane          = $Lane
        allowEdits    = [bool]$AllowEdits
        runDir        = $RunDir
        receiptPath   = $rcptPath
        recordedUtc   = (Get-Date).ToUniversalTime().ToString('o')
    })
} catch {
    throw "dispatch-ledger-write-failed: $($_.Exception.Message)"
}

Write-Utf8NoBom $promptPath $Prompt

# ---------------------------------------------------------------- build argv
# Argument ARRAYS, never a concatenated string: this repo has paths with spaces
# and a '!' in them, and string-built command lines have mis-split here before.
if ($cfg.engine -eq 'claude') {
    $exe  = $CLAUDE_EXE
    $claudeLaunchModel = if ($ModelOverride) { $ModelOverride } else { $cfg.model }
    # LANE-MODEL-CURRENCY-1 round 2: the pre-launch choice, same discipline as the codex branch
    # below -- resolvedModel is still filled in only from the child's own modelUsage, never copied
    # from this.
    $SelectedModel = $claudeLaunchModel
    $argv = @('-p', '--model', $claudeLaunchModel, '--output-format', 'json', '--add-dir', $WorkDir)
    if ($ExtraReadDir) { $argv += @('--add-dir', $ExtraReadDir) }
    if ($MaxTurns -gt 0) { $argv += @('--max-turns', [string]$MaxTurns) }
    # Settings file written to the RUN DIR so the grant is auditable beside the receipt that
    # it produced, rather than being an invisible property of the invocation. Round 6
    # (swarm ruling, 2026-09-22): every Claude-engine lane now gets this file -- previously
    # only a non-bulk-reads lane did -- because it is now also how the lane-no-background
    # PreToolUse hook is wired; the Read deny rules stay conditional on -AllowBulkReads exactly
    # as before.
    # Round 7 (sol major 3): docs/Start-EditingLane.ps1 resolves and runs Invoke-Lane.ps1 FROM
    # $WorkDir for every editing lane, so $PSScriptRoot -- and $LANE_NO_BACKGROUND_HOOK beside
    # it -- is INSIDE the lane's own writable worktree; an editing lane keeps Write/Edit/Bash
    # for its whole session and could alter its own gate script before ever calling Bash. Copy
    # the source into THIS run's reserved, board-rooted slot (never $WorkDir, unlike
    # $PSScriptRoot) before it is ever registered, and point the registration at the copy, not
    # at the source. Hashed immediately so the receipt records exactly what shipped.
    $hookCopyPath = "$base.lane-no-background.py"
    try {
        Copy-Item -LiteralPath $LANE_NO_BACKGROUND_HOOK -Destination $hookCopyPath -Force -ErrorAction Stop
        $backgroundHookLaunchSha256 = (Get-FileHash -LiteralPath $hookCopyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } catch {
        throw "background-gate-copy-failed: $($_.Exception.Message)"
    }
    # Round 9: resolution can come back empty (checked override, PATH, and known locations, and
    # none of them exist on this host). An unrunnable hook command denies nothing, so refuse the
    # launch here, before ever building the registered string or running the self-test against a
    # command that could never have worked -- same fail-closed posture the self-test itself
    # enforces below, just one step earlier because there is no command to even attempt yet.
    if (-not $PYTHON_EXE) {
        throw "background-gate-interpreter-not-found: no Python interpreter resolved ($($PYTHON_EXE_RESOLUTION.Source)); an unrunnable hook command denies nothing, so the launch is refused rather than registering one that cannot run."
    }
    # Round 14 (sol major, r13): the hook COPY gets a launch-hash/post-run-rehash tamper check
    # (below and at the post-run section), but the INTERPRETER named as exec form's `command` --
    # a user-writable executable this launcher only ever proves ONCE, at self-test time -- got
    # none. An editing lane's own allowed foreground Bash/PowerShell call can delete or replace
    # that executable after the self-test passes; every later hook spawn then fails, which the
    # vendor docs this PR already cites (round 7 comment above) treat as a NON-BLOCKING error, so
    # `run_in_background: true` would proceed while the receipt still read `denied-by-settings-
    # hook`. Same treatment as the hook copy: hash it now, before it is ever spawned, and compare
    # again after the run (post-run section, below `background-gate-tampered` for the hook copy).
    # Hashed BEFORE the self-test below so the recorded hash is provably of the exact bytes the
    # self-test itself just proved deny -- not a later, possibly-already-different file at the
    # same path.
    try {
        $backgroundInterpreterLaunchSha256 = (Get-FileHash -LiteralPath $PYTHON_EXE -Algorithm SHA256).Hash.ToLowerInvariant()
    } catch {
        throw "background-gate-interpreter-not-found: resolved interpreter $PYTHON_EXE could not be hashed: $($_.Exception.Message)"
    }
    # Round 13 (hub ruling): registered in EXEC FORM (`args` present) -- Claude Code resolves
    # `command` as an executable and spawns it directly with `args` as the argument vector, "no
    # shell" (code.claude.com/docs/en/hooks, "Exec form and shell form", quoted in full above
    # $PYTHON_EXE's resolver and in this round's summary.md). There is no shell kind to select,
    # classify, or fall back between, so rounds 10-12's Git-Bash-preferred/PowerShell-fallback
    # loop, and round 9's "neither shell resolved" refusal, are gone entirely -- the only
    # remaining executable is the already-resolved interpreter.
    #
    # Round 7 (sol major 2): a missing or wrong-path interpreter cannot be detected FROM INSIDE
    # the hook -- Claude Code treats a failed hook COMMAND as a non-blocking error and the tool
    # call proceeds, so a receipt claiming the gate denies background work could be false while
    # every route stayed open. Prove it, synchronously, before this lane is ever launched: invoke
    # the EXACT executable-plus-argv the settings file registers and require the fail-closed deny
    # (exit 2, lane-no-background.py's own protocol). A launch whose self-test does not pass never
    # starts the provider -- the throw below is caught by this script's own top-level try/catch,
    # which still writes a well-formed 'failed' receipt naming this exact reason.
    #
    # Round 11 (sol minor / fable minor 2), still true under exec form: exit 2 ALONE is not proof
    # the hook ran and denied THIS payload -- lane-no-background.py's own fail-closed empty-stdin
    # path also exits 2, and Python itself exits 2 when the registered script path is missing or
    # misquoted (it prints its own "can't open file" message and nothing the hook would ever say).
    # The hook's own DENY_REASON text ("headless lane: ... a headless lane has no later turn")
    # only ever reaches stdout/stderr on the genuine `run_in_background: true` branch -- neither
    # fail-closed-input path nor a Python launch failure prints it -- so requiring it in the
    # captured self-test output is a positive proof the DENY branch itself ran, not merely that
    # something upstream returned 2.
    $backgroundGateExpectedDenySubstring = 'headless lane'
    $backgroundGateSelfTestDenyPayload = '{"tool_name":"Bash","tool_input":{"command":"echo x","run_in_background":true}}'
    # Round 11 (sol minor / fable minor 2): a deny-only self-test cannot distinguish "the gate
    # discriminates on run_in_background" from "this executable always exits 2 no matter what" --
    # a subject and a control that differ is the only thing that proves discrimination. Run the
    # SAME executable-plus-argv against a non-background payload and require it to be ALLOWED
    # (exit 0) before trusting the deny result above.
    $backgroundGateSelfTestAllowPayload = '{"tool_name":"Bash","tool_input":{"command":"echo x","run_in_background":false}}'
    # Round 13: PowerShell's call operator with an ARGUMENT ARRAY ($PYTHON_EXE, $hookCopyPath as
    # two distinct elements, never a joined string) invokes the exact same executable-plus-argv
    # pair exec form itself performs -- no shell tokenizes either side. One executable, one argv,
    # one proof; there is no candidate list left to enumerate.
    try {
        $denyOutput = $backgroundGateSelfTestDenyPayload | & $PYTHON_EXE $hookCopyPath 2>&1
        $denyExit = $LASTEXITCODE
    } catch {
        throw "background-gate-selftest-failed: interpreter invocation threw: $($_.Exception.Message)"
    }
    $denyOutputText = ($denyOutput -join ' | ')
    if ($denyExit -ne 2 -or $denyOutputText -notlike "*$backgroundGateExpectedDenySubstring*") {
        throw "background-gate-selftest-failed: expected exit 2 with a deny reason containing '$backgroundGateExpectedDenySubstring' from the exact registered executable and argv, got exit $denyExit; output: $denyOutputText"
    }
    try {
        $allowOutput = $backgroundGateSelfTestAllowPayload | & $PYTHON_EXE $hookCopyPath 2>&1
        $allowExit = $LASTEXITCODE
    } catch {
        throw "background-gate-selftest-failed: positive-control invocation threw: $($_.Exception.Message)"
    }
    if ($allowExit -ne 0) {
        throw "background-gate-selftest-failed: positive control failed -- a non-background payload through the exact registered executable and argv was expected to be ALLOWED (exit 0) but got exit $allowExit; output: $($allowOutput -join ' | '); a self-test that cannot show BOTH a deny and an allow does not prove the gate discriminates"
    }
    # Matcher covers BOTH shell tools carrying `run_in_background` (round 7, sol major 1 /
    # fable major): PowerShell has the same parameter Bash does, and the sanctioned editing
    # dispatch (docs/Start-EditingLane.ps1) grants PowerShell to every editing lane. The
    # script's own should_deny check is now tool-name-agnostic as a second, independent gate
    # (see lane-no-background.py), so this matcher narrows WHICH calls invoke the hook at all,
    # not which calls the hook is capable of denying.
    # Round 13: `args` present makes this EXEC FORM -- `command` is the executable
    # (code.claude.com/docs/en/hooks, "Command hook fields": "With `args`, the executable to
    # spawn directly"), `args` is the argument vector, and `shell` is "ignored when `args` is
    # set" -- omitted rather than written misleadingly.
    $settingsObj = [ordered]@{
        hooks = [ordered]@{
            PreToolUse = @(
                [ordered]@{
                    matcher = 'Bash|PowerShell'
                    hooks   = @(
                        [ordered]@{
                            type    = 'command'
                            command = $PYTHON_EXE
                            args    = @($hookCopyPath)
                        }
                    )
                }
            )
        }
    }
    if (-not $AllowBulkReads) {
        $denyRules = @(
            'Read(**/gpu-lane-impl-review-sync.md)',
            'Read(**/queue.json)',
            'Read(**/claude-resume-CURRENT.md)',
            'Read(**/fable-resume-CURRENT.md)',
            'Read(**/orchestrator-resume-CURRENT.md)'
        )
        $settingsObj.permissions = [ordered]@{ deny = $denyRules }
    }
    Write-Utf8NoBom $settingsPath ($settingsObj | ConvertTo-Json -Depth 6)
    $argv += @('--settings', $settingsPath)
    if ($AllowEdits) {
        # 0.1: acceptEdits still takes an explicit --allowedTools list - the mode
        # decides HOW an allowed tool behaves (auto-accept vs prompt), the list
        # decides WHICH tools are allowed at all. 'ALL' was refused above.
        $argv += @('--permission-mode', 'acceptEdits', '--allowedTools', $AllowedTools)
    } else {
        # No read-only permission mode exists, so restrict the TOOLS instead.
        # COMMA-SEPARATED, ONE TOKEN: --allowedTools is VARIADIC, so passing the
        # tools as separate arguments makes it swallow the positional prompt that
        # follows and the CLI dies with "Input must be provided...".
        $argv += @('--permission-mode', 'dontAsk',
                   '--allowedTools', 'Read,Grep,Glob')
        # A permission allowlist does not hide other tools from the model. A
        # readonly review previously burned its turn cap retrying denied shells.
        $capabilityNotice = 'This read-only lane has permission to use only Read, Grep, and Glob. Bash, PowerShell, editing tools, Agent, and Task are unavailable: do not call or retry them. Inspect hub-exported diffs and evidence with the available read tools. If a required export is missing, name that missing evidence and return an unmeasured finding; do not claim you ran shell commands or tests.'
        $argv += @('--append-system-prompt', $capabilityNotice)
    }
    # Prevent nested provider fan-out through the CLI's supported deny surface, AND
    # deny every tool that hands a headless lane a LATER turn it does not have.
    # LANE-NO-BACKGROUND-END-TURN-1 (2026-09-22): four headless `claude -p` implementer
    # lanes ended their final turn on "I'll resume when the background job completes" --
    # a headless lane gets no later turn, so the work sat uncommitted with no receipt.
    # Monitor/ScheduleWakeup/CronCreate/CronDelete/RemoteTrigger/TaskCreate all promise a
    # callback this process cannot receive, and Workflow fans out background-orchestrated
    # work the launching lane cannot supervise. One comma-separated token avoids the same
    # variadic swallowing hazard as allowedTools. Built from $DENIED_TOOLS_DISPLAY, the same
    # list the pre-reservation rejection above checks against.
    $argv += @('--disallowedTools', ($DENIED_TOOLS_DISPLAY -join ','))
    # PROMPT GOES VIA STDIN, NOT AS A POSITIONAL ARGUMENT. Several claude flags
    # (--allowedTools, --add-dir) are VARIADIC and keep consuming every following
    # token that does not start with '-', so a trailing positional prompt is
    # silently absorbed into the flag's value list and the CLI then dies with
    # "Input must be provided either through stdin or as a prompt argument".
    # stdin has no such ambiguity and no command-line length limit.
    $stdinContent = $Prompt
    # CAUSAL-REACH-1: the receipt used to record only allowEdits, which says what a
    # lane may WRITE and nothing about what it may CAUSE. Record the actual granted
    # authority so a receipt can be audited against the policy that produced it.
    $authority = [ordered]@{
        permissionMode = if ($AllowEdits) { 'acceptEdits' } else { 'dontAsk' }
        allowedTools   = if ($AllowEdits) { $AllowedTools } else { 'Read,Grep,Glob' }
        sandbox        = 'n/a (claude)'
        writableRoot   = if ($AllowEdits) { $WorkDir } else { $null }
        maxTurns       = if ($MaxTurns -gt 0) { $MaxTurns } else { 'unset' }
        bulkReads      = if ($AllowBulkReads) { 'ALLOWED' } else { 'DENIED' }
        denyRules      = if ($AllowBulkReads) { @() } else { $denyRules }
        disallowedTools = $DENIED_TOOLS_DISPLAY
        capabilityNotice = if ($AllowEdits) { $null } else { $capabilityNotice }
        # Round 6 (swarm ruling): --disallowedTools cannot reach run_in_background -- it is a
        # parameter of a tool call, not a separate tool name -- so the settings-file
        # PreToolUse hook wired above is the actual mechanism, with the dirty-no-commit receipt
        # check as the after-the-fact backstop. See docs/lane-containment.md.
        # Round 7: renamed from backgroundBash now that the matcher and the hook's own check
        # both cover PowerShell (and, for the hook's own check, any tool) alongside Bash. The
        # self-test above already proved the copy denies before this lane ever started;
        # backgroundHookSha256 is the copy's hash at that instant -- compared again after the
        # run below, where a mismatch overwrites this value with 'background-gate-tampered'.
        backgroundGate       = 'denied-by-settings-hook'
        backgroundHookSha256 = $backgroundHookLaunchSha256
        # Round 9: the resolved interpreter path (never a pin) plus WHERE it came from --
        # override, PATH, or a known location -- so a receipt can be audited against what
        # actually ran the gate on this host, not what a stale comment claims is pinned.
        # Round 13: rounds 9-12's backgroundGateShellKind/-Path/-Source/-ValidatedCandidates are
        # GONE -- there is no shell kind, no shell executable, and no candidate list once the
        # hook is registered in exec form (see the round-13 comment above the settings object).
        # backgroundGateForm records that fact directly: 'exec' means Claude Code spawns
        # backgroundGateInterpreterPath with backgroundGateHookArgs as its argv, with no shell
        # auto-detection step this launcher had to out-guess.
        backgroundGateInterpreterPath   = $PYTHON_EXE
        backgroundGateInterpreterSource = $PYTHON_EXE_RESOLUTION.Source
        backgroundGateForm              = 'exec'
        backgroundGateHookArgs          = @($hookCopyPath)
        # Round 14: same treatment, same field shape, as backgroundHookSha256 above -- the
        # resolved interpreter's own hash at the instant the self-test proved it denies. Compared
        # again post-run below; a mismatch or a now-missing interpreter overwrites
        # backgroundGate to 'background-gate-tampered' exactly the way a hook-copy mismatch does,
        # never a separate state -- both are "this run's proof no longer describes what could
        # have run" and are not distinguishable in severity, so this reuses that one literal
        # rather than inventing an 'unverified' third state that would only blur the same fact.
        backgroundGateInterpreterSha256 = $backgroundInterpreterLaunchSha256
    }
} else {
    $exe  = $CODEX_EXE
    $sandbox = if ($AllowEdits) { 'workspace-write' } else { 'read-only' }
    # LANE-MODEL-CURRENCY-1: the table names a TIER, never a pinned slug, so it is
    # resolved to the current highest-version model HERE, at launch -- fail closed
    # (throws) rather than falling back to a guess. -ModelOverride bypasses this
    # resolution entirely for an explicit, auditable pin (recorded as pinnedByOverride).
    $codexLaunchModel = if ($ModelOverride) {
        $ModelOverride
    } else {
        if (-not $PYTHON_EXE) {
            throw "codex-tier-resolution-failed: no Python interpreter resolved ($($PYTHON_EXE_RESOLUTION.Source)) to resolve tier '$($cfg.tier)'."
        }
        Resolve-CodexModelTier -Tier $cfg.tier -PythonExe $PYTHON_EXE
    }
    # LANE-MODEL-CURRENCY-1 round 2 (sol blocker 2): this is the PRE-LAUNCH choice, recorded as
    # selectedModel -- NOT resolvedModel. The previous code assigned this straight to
    # resolvedModel here, before Process.Start below, so a missing/unstartable codex executable
    # (Process.Start throws) produced a 'failed' receipt that still named this slug as
    # resolvedModel -- a model that never ran. resolvedModel is now filled in only after the
    # child is proven to have run (see the harvest section below), and stays 'unknown' on any
    # start failure.
    $SelectedModel = $codexLaunchModel
    # -s and -c are set EXPLICITLY per call. ~/.codex/config.toml carries
    # approval_policy=never + sandbox_mode=danger-full-access globally, which is
    # fine for a watched interactive session and NOT fine for automated fan-out.
    $argv = @('exec',
              '-m', $codexLaunchModel,
              '-c', ("model_reasoning_effort=`"{0}`"" -f $cfg.effort),
              '-s', $sandbox,
              '-C', $WorkDir,
              '-o', $lastPath,
              '--skip-git-repo-check',
              '-')
    # '-' MEANS "READ THE PROMPT FROM STDIN", and it is not optional here.
    # Passing a multi-line prompt POSITIONALLY is silently TRUNCATED AT THE FIRST
    # NEWLINE, because the launcher is a .cmd batch wrapper and cmd.exe breaks the
    # argument there. Measured 2026-08-29: a 3,243-byte review prompt arrived as
    # its first line only, and the lane answered "what would you like me to do?"
    # in 10.9s with exit 0 - a SUCCESSFUL-LOOKING run that reviewed nothing.
    $stdinContent = $Prompt
    $authority = [ordered]@{
        permissionMode = 'n/a (codex)'
        allowedTools   = 'ALL'
        sandbox        = $sandbox
        writableRoot   = if ($AllowEdits) { $WorkDir } else { $null }
        maxTurns       = 'n/a (codex exec has no turn cap)'
        bulkReads      = 'ALLOWED (codex takes no settings deny-list)'
        denyRules      = @()
    }
}

# ---------------------------------------------------------------- run, bounded
# Keep the stopwatch started at reservation: setup and child startup consume the
# same wall budget as provider execution.

# LAUNCH VIA ProcessStartInfo.ArgumentList, NOT Start-Process -ArgumentList.
# Start-Process joins an array into ONE command-line string without quoting the
# elements, so this repo's own path - "C:\!Layi Wkspc\MLV-App", which contains a
# space - arrives SPLIT and codex rejects the fragment as an unexpected argument.
# ArgumentList is a real collection and .NET applies correct per-argument
# escaping (including the special .cmd rules), so a path with spaces survives.
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.WorkingDirectory       = $WorkDir
$psi.UseShellExecute        = $false
$psi.CreateNoWindow         = $true
[void]$psi.Environment.Remove('MLV_DISPATCH_RESERVATION_ID')
# $cfg.effort is already the EFFECTIVE value here (table default, overridden by -ReasoningEffort
# a few lines up), so gating on it -- rather than only on the raw override -- makes a table
# effort (e.g. fable/opus/sol/luna at 'high') actually reach the child instead of being recorded
# in the receipt but never applied to the process that ran.
if ($cfg.engine -eq 'claude' -and $cfg.effort) {
    # LANE-NO-BACKGROUND-END-TURN-1 round 3 (hub ruling): this PR does not ADD any
    # ANTHROPIC_/OPENAI_/CLAUDE_CODE_ assignment; the pre-existing CLAUDE_CODE_EFFORT_LEVEL
    # assignment (from 8ad69168, 2026-09-07, previously reviewed) is tracked under
    # NA3-CHILD-ENV-SCOPE-1 -- whether NA-3's prefix rule covers a launcher setting a child
    # process's environment in code is a governance interpretation filed separately there,
    # not decided by this PR. Do not remove or change this assignment to "fix" NA-3 here.
    $psi.Environment['CLAUDE_CODE_EFFORT_LEVEL'] = $cfg.effort
}
# KEY-VERDICT-OVERWRITTEN-BY-ROTATION-CHECKPOINT-1: mark the claude child as a bounded fleet lane.
# The user-level Stop hook usage-guard.py blocks the first stop of a session at plan level prep/hold so
# an interactive session writes a rotation checkpoint; in a headless `-p` lane that extra turn becomes
# the whole `result` of --output-format json, so <lane>-NNN.last.txt held the checkpoint and the verdict
# was lost. The hook skips its Stop block when this variable is set. Not an ANTHROPIC_/OPENAI_/CLAUDE_CODE_
# name (NA-3). The contained host inherits it from $psi and starts the provider without an override.
if ($cfg.engine -eq 'claude') {
    $psi.Environment['MLV_FLEET_LANE'] = '1'
}
# LANE-NO-BACKGROUND-END-TURN-1 round 2 (hub ruling): a CLAUDE_CODE_DISABLE_BACKGROUND_TASKS
# child-environment flag was here through round 1. docs/never-authorized.json NA-3 prohibits
# assigning ANY CLAUDE_CODE_* variable, and widening that rule is an authority change the hub
# will not make -- so the flag is removed entirely, not narrowed (this PR adds no new
# ANTHROPIC_/OPENAI_/CLAUDE_CODE_ assignment of its own; see the NA3-CHILD-ENV-SCOPE-1 note
# above for the one pre-existing assignment that remains). Containment of background work now
# rests on the --disallowedTools deny list above, the dirty-no-commit receipt check below
# (scoped to lane-introduced tracked-file changes only), and the brief-level prohibition on
# background work; see docs/lane-containment.md.
# Per-run lane scratch under an MLV-owned root instead of the shared %TEMP%.
$scratchDir = $null
if ($ScratchRoot) {
    $scratchDir = Join-Path $ScratchRoot ('{0}-{1}' -f (Split-Path $RunDir -Leaf), (Split-Path $base -Leaf))
    New-Item -ItemType Directory -Force -Path $scratchDir | Out-Null
    foreach ($v in 'TEMP', 'TMP', 'TMPDIR') { $psi.Environment[$v] = $scratchDir }
}
$psi.RedirectStandardInput  = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true
$psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
$psi.StandardErrorEncoding  = [System.Text.UTF8Encoding]::new($false)

if ($sw.Elapsed.TotalMilliseconds -ge ($TimeoutSec * 1000.0)) {
    throw [TimeoutException]::new('launch-budget-exhausted')
}

if ($cfg.engine -eq 'claude') {
    # This trusted host is inert until it reads frame one. It is assigned to the job
    # before frame one is sent, so the provider and every descendant inherit the job.
    $hostSource = @'
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
$line=[Console]::In.ReadLine(); if([string]::IsNullOrWhiteSpace($line)){throw 'launch-frame-missing'}
$launch=$line|ConvertFrom-Json; if([string]$launch.schema -ne 'mlv-lane-launch/v1'){throw 'launch-frame-schema'}
$p=[Diagnostics.ProcessStartInfo]::new(); $p.FileName=[string]$launch.exe
foreach($a in @($launch.argv)){[void]$p.ArgumentList.Add([string]$a)}
$p.WorkingDirectory=[string]$launch.cwd; $p.UseShellExecute=$false
$p.RedirectStandardInput=$true; $p.RedirectStandardOutput=$true; $p.RedirectStandardError=$true
$p.StandardOutputEncoding=[Text.UTF8Encoding]::new($false); $p.StandardErrorEncoding=[Text.UTF8Encoding]::new($false)
$child=[Diagnostics.Process]::Start($p); $ot=$child.StandardOutput.ReadToEndAsync(); $et=$child.StandardError.ReadToEndAsync()
$control=[ordered]@{schema='mlv-lane-child/v1';pid=$child.Id;createdUtc=$child.StartTime.ToUniversalTime().ToString('o')}|ConvertTo-Json -Compress
$tmp=[string]$launch.controlPath+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
[IO.File]::WriteAllText($tmp,$control,[Text.UTF8Encoding]::new($false)); [IO.File]::Move($tmp,[string]$launch.controlPath)
$line=[Console]::In.ReadLine(); if([string]::IsNullOrWhiteSpace($line)){throw 'prompt-frame-missing'}
$frame=$line|ConvertFrom-Json; if([string]$frame.schema -ne 'mlv-lane-prompt/v1'){throw 'prompt-frame-schema'}
$prompt=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$frame.promptBase64))
$child.StandardInput.Write($prompt); $child.StandardInput.Close(); $child.WaitForExit()
[Console]::Out.Write($ot.GetAwaiter().GetResult()); [Console]::Error.Write($et.GetAwaiter().GetResult()); exit $child.ExitCode
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($hostSource))
    $psi.FileName = (Get-Command pwsh.exe -ErrorAction Stop).Source
    foreach ($a in @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded)) {
        [void]$psi.ArgumentList.Add($a)
    }
    $jobHandle = [MlvLaneJob]::CreateKillOnClose()
    $proc = [Diagnostics.Process]::Start($psi)
    $hostStarted = $true
    # Record the pid the instant Start returns, before anything that can throw:
    # once Start succeeds a host EXISTS, and a receipt that omits its pid is
    # indistinguishable from "no host was started" (PR #105 round 2 blocker).
    # $hostPid is a bare property read on a live Process -- it cannot throw,
    # unlike $containedHost's own ordered-map build below (PR #105 round 4
    # blocker), so it survives even when that build itself throws (PR #105
    # round 5): the post-start-unrecorded fallback reports $hostPid instead of
    # null, so a host that exists is never reported as if it did not.
    # createdUtc is read defensively in its own try -- a StartTime failure must
    # not erase the pid we already have.
    $hostPid = $proc.Id
    $containedHost = [ordered]@{ pid=$hostPid; createdUtc=$null }
    try { $containedHost.createdUtc = $proc.StartTime.ToUniversalTime().ToString('o') } catch { }
    [MlvLaneJob]::AssignOrThrow($jobHandle, $proc.Handle)
    $jobAssigned = $true
} else {
    $psi.FileName = $exe
    foreach ($a in $argv) { [void]$psi.ArgumentList.Add($a) }
    $proc = [Diagnostics.Process]::Start($psi)
}

# Start the async reads BEFORE waiting: a child that fills a redirected pipe
# buffer blocks forever if nobody is draining it, and the timeout below would
# then measure a deadlock we caused rather than a slow lane.
$outTask = $proc.StandardOutput.ReadToEndAsync()
$errTask = $proc.StandardError.ReadToEndAsync()

# The prompt reaches claude this way; codex gets an empty stdin that is CLOSED,
# which is what stops it waiting on "Reading additional input from stdin...".
if ($cfg.engine -eq 'claude') {
    $controlPath = "$base.child.json"
    if (Test-Path -LiteralPath $controlPath) { throw "control-path-exists: $controlPath" }
    $launchFrame = [ordered]@{ schema='mlv-lane-launch/v1'; exe=$exe; argv=$argv; cwd=$WorkDir; controlPath=$controlPath } | ConvertTo-Json -Compress -Depth 5
    $proc.StandardInput.WriteLine($launchFrame); $proc.StandardInput.Flush()
    $controlDeadlineMs = [math]::Min($sw.Elapsed.TotalMilliseconds + 10000.0, $TimeoutSec * 1000.0)
    while (-not (Test-Path -LiteralPath $controlPath)) {
        if ($proc.HasExited) { throw "contained-host-exited-before-child: $($proc.ExitCode)" }
        if ($sw.Elapsed.TotalMilliseconds -ge $controlDeadlineMs) { throw [TimeoutException]::new('contained-child-start-timeout') }
        Start-Sleep -Milliseconds 25
    }
    # ConvertFrom-Json can turn ISO strings into DateTime values on newer pwsh;
    # casting back to string then loses precision and uses the current culture.
    # Keep the exact UTC creation identity emitted by the contained host.
    $controlJson = [System.Text.Json.JsonDocument]::Parse([IO.File]::ReadAllText($controlPath))
    try {
        $childIdentity = [pscustomobject]@{
            schema = $controlJson.RootElement.GetProperty('schema').GetString()
            pid = $controlJson.RootElement.GetProperty('pid').GetInt32()
            createdUtc = $controlJson.RootElement.GetProperty('createdUtc').GetString()
        }
    } finally { $controlJson.Dispose() }
    if ([string]$childIdentity.schema -ne 'mlv-lane-child/v1') { throw 'contained-child-schema' }
    $containment = [ordered]@{
        kind='windows-job-kill-on-close'; jobAssigned=$jobAssigned
        runnerPid=$PID; runnerCreatedUtc=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
        ownerPid=$containedHost.pid; ownerCreatedUtc=$containedHost.createdUtc
        childPid=[int]$childIdentity.pid; childCreatedUtc=[string]$childIdentity.createdUtc
        deadlineUtc=$deadlineUtc.ToString('o'); promptDelivered=$false; assignmentErrorCode=$null
    }
    $runningReceipt = [ordered]@{
        schema='mlv-app/fleet-lane-receipt/v1'; state='running'; complete=$false
        lane=$Lane; card=$Card; startedUtc=$startedUtc.ToString('o'); timeoutSec=$TimeoutSec
        promptSha256=(Get-Sha256 $Prompt); promptBytes=[Text.Encoding]::UTF8.GetByteCount($Prompt)
        containment=$containment
    }
    Write-Utf8NoBomAtomic $rcptPath ($runningReceipt | ConvertTo-Json -Depth 6)
    if ($sw.Elapsed.TotalMilliseconds -ge ($TimeoutSec * 1000.0)) { throw [TimeoutException]::new('contained-prompt-deadline-exhausted') }
    $promptFrame = [ordered]@{schema='mlv-lane-prompt/v1';promptBase64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($stdinContent))}|ConvertTo-Json -Compress
    $proc.StandardInput.WriteLine($promptFrame); $proc.StandardInput.Flush()
    $promptDelivered = $true; $containment.promptDelivered = $true
    Write-Utf8NoBomAtomic $rcptPath ($runningReceipt | ConvertTo-Json -Depth 6)
    $proc.StandardInput.Close()
} else {
    $proc.StandardInput.Write($stdinContent)
    $proc.StandardInput.Close()
}

$timedOut = $false
$remainingMs = [math]::Max(0, [math]::Floor(($TimeoutSec * 1000.0) - $sw.Elapsed.TotalMilliseconds))
if ($remainingMs -eq 0 -or -not $proc.WaitForExit([int]$remainingMs)) {
    $timedOut = $true
    # Closing the job is the authoritative descendant cleanup. Do it before
    # harvesting pipes, which descendants could otherwise keep open indefinitely.
    if ($jobHandle -ne [IntPtr]::Zero) {
        [void][MlvLaneJob]::CloseHandle($jobHandle)
        $jobHandle = [IntPtr]::Zero
    }
    try { $proc.Kill($true) } catch { }
    try { [void]$proc.WaitForExit(15000) } catch { }
}
$sw.Stop()
$exitCode = if ($timedOut) { -1 } else { $proc.ExitCode }

# ---------------------------------------------------------------- harvest
$stdout = try { $outTask.Result } catch { '' }
if ($null -eq $stdout) { $stdout = '' }
$stderrText = try { $errTask.Result } catch { '' }
if ($null -eq $stderrText) { $stderrText = '' }
Write-Utf8NoBom $outPath $stdout
Write-Utf8NoBom $errPath $stderrText
# Classify BEFORE parsing the answer: a refused run has no answer, and the 2026-09-07 sol
# receipt proved that exitCode alone cannot tell "refused in 7 s" from "reviewed and objected".
# Structurally, never by scanning the transcript (sol PR #80 R1+R2, both BLOCKER): the
# transcript legitimately CONTAINS the refusal vocabulary even when the run answered.
$providerRefusal = Get-ProviderRefusal -Text $stderrText -Answer $stdout -Engine $cfg.engine -Prompt $Prompt

$final = ''
if ($cfg.engine -eq 'claude') {
    # --output-format json wraps the answer; fall back to raw stdout if the
    # process died before emitting well-formed JSON.
    try {
        $j = $stdout | ConvertFrom-Json
        if ($j.PSObject.Properties.Name -contains 'result') { $final = [string]$j.result }
        # SPEND IS A FIRST-CLASS RECEIPT FACT. Until 2026-09-03 the receipt recorded
        # exitCode (the truth about completion) and byte counts, but NOTHING about cost -
        # so "what did this board spend" was answerable only by grepping raw stdout of
        # every run dir by hand. That is how USD 74.62 accumulated unnoticed in 4 h.
        if ($j.PSObject.Properties.Name -contains 'total_cost_usd') {
            $costUsd = [double]$j.total_cost_usd
        }
        if ($j.PSObject.Properties.Name -contains 'num_turns') { $numTurns = [int]$j.num_turns }
        # LANE-MODEL-CURRENCY-1: `--model opus`/`fable`/`sonnet` is a FLOATING ALIAS -- what
        # actually ran is only knowable from the child's own JSON, never assumed from the
        # alias string. `modelUsage` is AGGREGATE usage: it can carry auxiliary/tool-call
        # entries (e.g. a haiku sub-call) alongside the lane's own model, and sol's pre-review
        # proved treating every key as equally authoritative lets an auxiliary model's id (or,
        # worse, a raw un-proven key) overwrite the lane's real identity. So only the entry
        # whose canonicalModel belongs to the REQUESTED ALIAS'S FAMILY (a canonicalModel of the
        # form `claude-<alias>-...`, e.g. `claude-sonnet-5` for alias 'sonnet') is ever recorded
        # as resolvedModel; every other entry is aggregated separately into auxiliaryModels,
        # never into resolvedModel. resolvedModel stays 'unknown' (never a raw modelUsage key,
        # never the requested alias copied over) when no entry of that family is present or the
        # matching entry has no canonicalModel -- a died-before-emitting-JSON run, or a run that
        # silently fell back to a different family entirely, must never claim a model it cannot
        # prove ran as the requested one.
        # Property-existence checks below use the `.PSObject.Properties[name]` INDEXER, never
        # `.PSObject.Properties.Name -contains name` -- under this file's own `Set-StrictMode
        # -Version Latest` (line 127), PowerShell's multi-value member enumeration over an
        # EMPTY PSMemberInfoCollection throws "The property 'Name' cannot be found on this
        # object" instead of returning an empty list. A modelUsage entry commonly IS an empty
        # JSON object (`{}`, no canonicalModel yet recorded), so that idiom would silently
        # abort this entire try block via the catch below, discarding costUsd/numTurns/result
        # along with the model identity -- a strictly worse failure than the one this fix
        # exists to close. The indexer form returns $null for a missing property on any
        # collection, empty or not, so it is safe unconditionally.
        $modelUsageProp = $j.PSObject.Properties['modelUsage']
        if ($null -ne $modelUsageProp -and $null -ne $modelUsageProp.Value) {
            $modelUsageProps = @($modelUsageProp.Value.PSObject.Properties)
            if ($modelUsageProps.Count -gt 0) {
                $modelUsageNames = @($modelUsageProps.Name)
                $familyPattern = '^claude-' + [regex]::Escape($RequestedModel) + '-'
                $familyCanonicals = @()
                $auxiliaryEntries = @()
                foreach ($name in $modelUsageNames) {
                    $entry = $modelUsageProp.Value.$name
                    $canonicalProp = if ($null -ne $entry) { $entry.PSObject.Properties['canonicalModel'] } else { $null }
                    $canonical = $null
                    if ($null -ne $canonicalProp -and $canonicalProp.Value) { $canonical = [string]$canonicalProp.Value }
                    if ($canonical -and ($canonical -match $familyPattern)) {
                        $familyCanonicals += $canonical
                    } elseif ($canonical) {
                        $auxiliaryEntries += $canonical
                    } else {
                        $auxiliaryEntries += $name
                    }
                }
                $familyCanonicals = @($familyCanonicals | Select-Object -Unique)
                if ($familyCanonicals.Count -gt 0) { $ResolvedModel = ($familyCanonicals -join ',') }
                $AuxiliaryModels = @($auxiliaryEntries | Select-Object -Unique)
            }
        }
        if ($j.PSObject.Properties.Name -contains 'usage' -and $null -ne $j.usage) {
            if ($j.usage.PSObject.Properties.Name -contains 'cache_creation_input_tokens') {
                $cacheCreateTokens = [int64]$j.usage.cache_creation_input_tokens
            }
            if ($j.usage.PSObject.Properties.Name -contains 'cache_read_input_tokens') {
                $cacheReadTokens = [int64]$j.usage.cache_read_input_tokens
            }
            if ($j.usage.PSObject.Properties.Name -contains 'output_tokens') {
                $outputTokens = [int64]$j.usage.output_tokens
            }
        }
    } catch { $final = '' }
    if (-not $final) { $final = $stdout }
    Write-Utf8NoBom $lastPath $final
} else {
    $final = if (Test-Path -LiteralPath $lastPath) { Get-Content -LiteralPath $lastPath -Raw } else { '' }
    if ([string]::IsNullOrWhiteSpace($final)) { $final = $stdout }
    # LAST-RESORT FALLBACK TO STDERR. codex writes its -o last-message file only on
    # a CLEAN exit and streams its working narration to STDERR, not stdout - so a
    # lane killed at the timeout leaves the last-message file absent and stdout
    # EMPTY, i.e. a 15-minute run yielding literally zero bytes. Measured
    # 2026-08-29 on a timed-out luna recon. Partial narration is worth far more
    # than nothing when deciding whether to retry, widen the timeout, or change
    # approach entirely.
    if ([string]::IsNullOrWhiteSpace($final)) { $final = $stderrText }
    if ($null -eq $final) { $final = '' }
    if ($timedOut) { Write-Utf8NoBom $lastPath $final }
    # LANE-MODEL-CURRENCY-1 round 2 (sol blocker 2): resolvedModel is proof, not intent. codex
    # exec emits no modelUsage-style attestation the way claude's own JSON does, so the bar here
    # is the card's stated minimum: a clean exit (never timed out, exit 0) that produced real
    # output FROM THIS INVOCATION -- never merely that a host process existed. A kill, crash, or
    # non-zero exit leaves resolvedModel 'unknown' with selectedModel still recorded above, same
    # discipline as the claude branch's family-matched modelUsage check.
    if (-not $timedOut -and $exitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($final)) {
        $ResolvedModel = $SelectedModel
    }
}

}
catch {
    if ($_.Exception -is [TimeoutException]) {
        $timedOut = $true
        $exitCode = -1
        $failure = $null
    } else {
        $failure = $_.Exception.Message
    }
    # Before assignment the inert host is outside the job. Terminate only the exact
    # Process object created by this invocation; it has received no launch frame.
    # UNLIKE the post-timeout kill at line ~602 -- where the job handle has just
    # been closed and the job is kill-on-close, so the tree is already terminated
    # and that catch is belt-and-braces -- this host was NEVER inside the job, so
    # a swallowed failure here is a GENUINE orphan (PR #105 round 5, sol PR #105
    # blocker). The kill call itself still must not throw out of this catch (this
    # is already a failure path; a second exception would only mask the first),
    # but its OUTCOME is recorded into the containment record below instead of
    # vanishing silently -- the honest fix for a swallow is not to make the kill
    # infallible (it cannot be), but to make its failure visible.
    if ($cfg.engine -eq 'claude' -and -not $jobAssigned -and $null -ne $proc) {
        $ownerKillAttempted = $true
        try {
            if ($proc.HasExited) {
                $ownerKillOutcome = $OWNER_KILL_OUTCOME_ALREADY_EXITED
            } else {
                # WaitForExit(Int32) RETURNS a bool -- $true iff the process exited
                # within the timeout, $false if the wait merely expired. Round 5
                # discarded that return with [void] and recorded 'killed'
                # unconditionally, so a host that survived the bounded wait (exactly
                # the orphan this whole change exists to make visible) was reported
                # as killed -- manufactured evidence, not just an omission. Capture
                # the observed boolean and branch on IT, never on what was attempted.
                $proc.Kill($true)
                $exitedWithinWait = $proc.WaitForExit(5000)
                $ownerKillOutcome = if ($exitedWithinWait) { $OWNER_KILL_OUTCOME_KILLED } else { $OWNER_KILL_OUTCOME_KILL_WAIT_TIMEOUT }
            }
        } catch {
            $ownerKillOutcome = $OWNER_KILL_OUTCOME_KILL_THREW
            $ownerKillDetail = $_.Exception.Message
        }
        # Considered also stamping $proc.HasExited at the moment the containment
        # record is BUILT (further below, well after this try/catch) as a second,
        # cheaper corroborating observation. Declined: that build site sits inside
        # the outer exception handler with no catch of its own, so a HasExited
        # read there that throws (it CAN -- Win32Exception/InvalidOperationException
        # per the .NET contract) would escape uncaught and blow past receipt
        # construction entirely, the same "second exception masks the first"
        # failure mode the comment on this catch already warns against. Not worth
        # it for a value that WaitForExit's own return already answers.
    }
    if ($cfg.engine -eq 'claude' -and $null -eq $containment) {
        $native = if ($_.Exception.PSObject.Properties.Name -contains 'NativeErrorCode') { [int]$_.Exception.NativeErrorCode } else { $null }
        # CLASSIFYING THE THIRD STATE (PR #105 round 4): round 3 recorded the raw exception
        # MESSAGE as ownerAbsentReason, and the test accepted any truthy string as proof no
        # host existed. But $containedHost's own construction (the pid/dictionary build
        # above) can itself throw AFTER Start already returned and a host is alive -- so
        # that exception's message would pose as a legitimate no-host reason. Free text is
        # not admissible evidence about a safety property, so classify by WHERE the failure
        # happened instead of by WHAT it said:
        #   - $containedHost non-null  -> a pid was recorded; no absence to explain.
        #   - $containedHost null, $hostStarted false -> Start never returned: either the
        #     launch budget was already gone (line ~470, throws before Start is reached) or
        #     Start itself threw. Distinguish cheaply by exception type/message where we can;
        #     otherwise fall back to the generic "start threw" token. Both are legitimate
        #     no-host reasons, and $hostPid is still null in both (no host ever existed).
        #   - $containedHost null, $hostStarted true -> Start returned (a host EXISTS) but
        #     the record of it was never built. This is the genuinely ambiguous state and it
        #     is named as such, never hidden behind a message that merely looks legitimate.
        #     $hostPid (captured on its own non-throwing line before this build) is used for
        #     ownerPid below, so this state is never reported as if no host existed (PR #105
        #     round 5): the pid is the only channel by which anyone learns the orphan existed.
        # The raw message is kept for humans in ownerAbsentDetail, a field no decision reads.
        $ownerAbsentReason = $null
        $ownerAbsentDetail = $null
        if ($null -eq $containedHost) {
            $ownerAbsentDetail = $_.Exception.Message
            if (-not $hostStarted) {
                $ownerAbsentReason = if ($_.Exception -is [TimeoutException] -and $_.Exception.Message -eq $OWNER_ABSENT_NO_HOST_BUDGET) {
                    $OWNER_ABSENT_NO_HOST_BUDGET
                } else {
                    $OWNER_ABSENT_NO_HOST_START_THREW
                }
            } else {
                $ownerAbsentReason = $OWNER_ABSENT_POST_START_UNRECORDED
            }
        }
        $containment = [ordered]@{
            kind='windows-job-kill-on-close'; jobAssigned=$jobAssigned
            runnerPid=$PID; runnerCreatedUtc=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
            ownerPid=if($null-ne $containedHost){$containedHost.pid}else{$hostPid}
            ownerCreatedUtc=if($null-ne $containedHost){$containedHost.createdUtc}else{$null}
            ownerAbsentReason=$ownerAbsentReason
            ownerAbsentDetail=$ownerAbsentDetail
            ownerKillAttempted=$ownerKillAttempted
            ownerKillOutcome=$ownerKillOutcome
            ownerKillDetail=$ownerKillDetail
            childPid=$null; childCreatedUtc=$null; deadlineUtc=$deadlineUtc.ToString('o')
            promptDelivered=$promptDelivered; assignmentErrorCode=$native
        }
    }
    # Convert managed failures into the receipt/exit taxonomy below. Rethrowing here
    # bypasses the final `exit $propagated` and turns the documented 127 into shell 1.
}
finally {

# Cleanup must precede all receipt construction and I/O, including exceptional
# startup paths. Failure to write evidence cannot keep a provider running.
if ($jobHandle -ne [IntPtr]::Zero) {
    [void][MlvLaneJob]::CloseHandle($jobHandle)
    $jobHandle = [IntPtr]::Zero
}
$sw.Stop()
# Disk hygiene. Neither step may throw: a failure here is recorded, never propagated,
# because the receipt below must still be written on every exit path.
$scratchDisposition = $null
if ($scratchDir) {
    try {
        $sb = [int64]0
        if (Test-Path -LiteralPath $scratchDir) { Get-ChildItem -LiteralPath $scratchDir -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $sb += $_.Length } }
        $removed = $false
        if (-not $KeepScratch -and (Test-Path -LiteralPath $scratchDir)) { Remove-Item -LiteralPath $scratchDir -Recurse -Force -ErrorAction Stop; $removed = $true }
        $scratchDisposition = [ordered]@{ path = $scratchDir; bytes = $sb; removed = $removed; error = $null }
    } catch {
        $scratchDisposition = [ordered]@{ path = $scratchDir; bytes = $null; removed = $false; error = $_.Exception.Message }
    }
}
$worktreeDisposition = $null
if ($RetireWorktree) {
    try {
        . (Join-Path $PSScriptRoot 'Retire-LaneWorktree.ps1')
        # The CANONICAL board (parent of the common .git), never the checkout this script runs
        # from: a copy of this script inside a linked worktree must not quarantine into that worktree.
        # No `| Select-Object -First 1` on the native call: it can stop the pipeline before
        # $LASTEXITCODE is set, and reading it then throws under StrictMode Latest.
        $commonOut = @(& git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
        $commonDir = if ($commonOut.Count) { [string]$commonOut[0] } else { $null }
        if (-not $commonDir -or -not (Test-Path -LiteralPath $commonDir)) { throw 'cannot resolve board root from git common dir' }
        $boardRoot = Split-Path -Parent ($commonDir -replace '/', '\')
        $worktreeDisposition = Invoke-RetireLaneWorktree -WorkDir $WorkDir -ProtectPath @($RunDir) `
            -QuarantineRoot (Join-Path $boardRoot ('.claude-state\disk-hygiene\quarantine\lane-exit\' + (Get-Date).ToUniversalTime().ToString('yyyyMMdd')))
    } catch {
        $worktreeDisposition = [ordered]@{ action = 'kept'; reason = "cannot-determine: $($_.Exception.Message)" }
    }
}
# Sweep AFTER the block above: this lane's own worktree is -Exclude'd (it was just judged, or
# deliberately kept), and its RunDir is protected so a receipt directory is never swept away.
$worktreeSweep = $null
if (-not $NoWorktreeSweep) {
    try {
        . (Join-Path $PSScriptRoot 'Retire-LaneWorktree.ps1')
        $commonOut = @(& git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
        $commonDir = if ($commonOut.Count) { [string]$commonOut[0] } else { $null }
        if (-not $commonDir -or -not (Test-Path -LiteralPath $commonDir)) { throw 'cannot resolve board root from git common dir' }
        $boardRoot = Split-Path -Parent ($commonDir -replace '/', '\')
        $worktreeSweep = Invoke-SweepMergedLaneWorktrees -RepoRoot $boardRoot -Root $WorktreeSweepRoot `
            -Exclude @($WorkDir) -ProtectPath @($RunDir) `
            -QuarantineRoot (Join-Path $boardRoot ('.claude-state\disk-hygiene\quarantine\lane-exit\' + (Get-Date).ToUniversalTime().ToString('yyyyMMdd')))
    } catch {
        $worktreeSweep = [ordered]@{ error = "cannot-determine: $($_.Exception.Message)" }
    }
}
# `processEnded` records that the child exited; `complete` records POSITIVE evidence that the WORK
# finished (exit 0 and, for claude, a success envelope). Until 2026-09-14 `complete` meant only the
# former, so an exit-1 error_max_turns run was receipted state=complete, complete=true.
$processEnded = ($exitCode -ne -999)
try {
    $workEvidence = Get-LaneWorkEvidence -Engine $cfg.engine -Answer $stdout -ExitCode $exitCode
} catch {
    $workEvidence = [ordered]@{ workCompleted = $false; reason = "cannot-determine: $($_.Exception.Message)"; subtype = $null; terminalReason = $null; isError = $null }
}
# LANE-NO-BACKGROUND-END-TURN-1: a success envelope is not positive evidence of work if the
# tree never moved. HEAD still equal to the sha this lane started from, with tracked files
# left dirty BY THIS LANE, is exactly what "I'll resume when the background job completes"
# leaves behind on a headless lane that has no later turn to make good on that promise -- so
# it overrides whatever the envelope claims. Untracked-only dirt (scratch files, logs) does
# not count.
# Round 2 (sol major 1 / fable minor 3): gated to editing Claude lanes only, and compared
# against $InitialTrackedDirt so PRE-EXISTING tracked dirt never flips a receipt. A read-only
# lane can never move HEAD by construction, so without this gate the check degenerated to
# "was the surrounding checkout dirty" -- a fact outside a review lane's control. A codex
# lane never sets $InitialTrackedDirt (claude-only capture above) and is likewise excluded.
# Round 3 (sol minor / fable minor 2): a path counts as lane-introduced if it is NEWLY dirty
# (absent from $InitialTrackedDirt) OR its CONTENT IDENTITY changed -- not merely if its
# porcelain status line differs. A further edit to an already-dirty tracked file keeps the
# same status code (still ' M path') both before and after, so status-line comparison alone
# missed it; comparing the per-path content hash catches it.
# Round 5 (sol minor 1 / fable minor 1): `$dirtyCheck` records what this block actually managed
# to determine, and is carried into the receipt below so a reader never has to infer it from
# `workEvidence.reason` alone. Both git-capture failure paths -- the PRE-launch snapshot
# ($InitialTrackedDirtUnavailableReason, set above) and this POST-exit snapshot -- resolve to
# 'unavailable' and leave `workEvidence` completely untouched: an unavailable check must never
# manufacture a false 'ended-incomplete' (git was merely unreachable, not evidence of a dirty
# tree) NOR silently fall back to a false claim of 'clean' (an unavailable check has no basis to
# claim the tree is clean either).
# Round 7 (sol minor): a failed PRE-launch rev-parse ($BaseShaCaptureFailureReason) and a
# failed POST-exit rev-parse are now both surfaced as 'unavailable' with a reason naming
# exactly which call failed and why, instead of collapsing into 'not-applicable' (pre-launch
# failure) or the misleading 'HEAD moved' (post-exit failure) the way a bare, uncaptured
# `$null -ne $BaseSha` comparison did.
$dirtyCheck = 'not-applicable'
$dirtyCheckReason = $null
if ($AllowEdits -and $cfg.engine -eq 'claude') {
    if ($BaseShaCaptureFailureReason) {
        $dirtyCheck = 'unavailable'
        $dirtyCheckReason = $BaseShaCaptureFailureReason
    } elseif ($InitialTrackedDirtUnavailableReason) {
        $dirtyCheck = 'unavailable'
        $dirtyCheckReason = "pre-launch snapshot: $InitialTrackedDirtUnavailableReason"
    } elseif (-not $BaseSha) {
        $dirtyCheck = 'not-applicable'
        $dirtyCheckReason = 'no base sha: pre-launch rev-parse HEAD succeeded but returned nothing'
    } else {
        $headAfterCapture = Invoke-GitCaptureUtf8 -WorkDir $WorkDir -GitArgs @('rev-parse', 'HEAD')
        if (-not $headAfterCapture.ok) {
            $dirtyCheck = 'unavailable'
            $dirtyCheckReason = "post-exit rev-parse HEAD failed: $($headAfterCapture.error)"
        } else {
            $headAfter = $headAfterCapture.stdout.Trim()
            if ($headAfter -ne $BaseSha) {
                $dirtyCheck = 'not-applicable'
                $dirtyCheckReason = 'HEAD moved: a commit landed, so pre-commit leftover dirt is out of scope for this check'
            } else {
                $postSnapshot = Get-TrackedDirtyContentIdentity -WorkDir $WorkDir
                if (-not $postSnapshot.ok) {
                    $dirtyCheck = 'unavailable'
                    $dirtyCheckReason = "post-exit snapshot: $($postSnapshot.reason)"
                } else {
                    $trackedDirtyAfter = $postSnapshot.map
                    $introducedPaths = @($trackedDirtyAfter.Keys | Where-Object {
                        -not $InitialTrackedDirt.Contains($_) -or $InitialTrackedDirt[$_] -ne $trackedDirtyAfter[$_]
                    })
                    if ($introducedPaths.Count -gt 0) {
                        $dirtyCheck = 'dirty'
                        $dirtyCheckReason = "lane-introduced tracked dirt: $($introducedPaths -join ', ')"
                        $workEvidence.workCompleted = $false
                        # Preserve any existing reason (e.g. a subtype-* classification from a partially
                        # successful envelope) by appending rather than overwriting it -- sol/fable round 1
                        # noted an unconditional overwrite can mask the original failure reason.
                        $workEvidence.reason = if ($workEvidence.reason) { "$($workEvidence.reason) | dirty-worktree-no-commit" } else { 'dirty-worktree-no-commit' }
                    } else {
                        $dirtyCheck = 'clean'
                    }
                }
            }
        }
    }
}
# LANE-NO-BACKGROUND-END-TURN-1 round 7 (sol major 3): the copy this lane's hook actually ran
# against sits in the per-run reserved slot specifically so it is outside the lane's own
# writable worktree -- but an editing lane keeps Bash for its whole session, and Bash is not
# confined to --add-dir, so the lane could still reach out and rewrite the copy after its
# self-test-proven launch. Detection, not prevention: re-hash the copy now and compare against
# the hash taken the instant it was written, before the child ever started. A read-only lane
# has no Write/Edit/Bash grant at all, so this can only ever fire for an editing lane, but the
# check is cheap and unconditional for every claude lane that got a copy.
# Round 8 (fable minor): stated plainly because it is easy to over-read as continuous
# monitoring -- it is NOT. This compares the copy's hash at exactly two points in time: the
# instant it was written, before the child ever started, and the instant the provider process
# exits. A rewrite that happens mid-run and is RESTORED to the original bytes before the
# provider exits leaves the two hashes identical, so this check records no tamper and the
# receipt still claims 'denied-by-settings-hook' -- even though the gate was provably absent
# for however long the rewrite was in effect. See docs/lane-containment.md for the full
# statement of what this does and does not detect.
if ($cfg.engine -eq 'claude' -and $backgroundHookLaunchSha256) {
    $backgroundHookPostRunSha256 = if (Test-Path -LiteralPath $hookCopyPath) {
        (Get-FileHash -LiteralPath $hookCopyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { $null }
    if ($backgroundHookPostRunSha256 -ne $backgroundHookLaunchSha256) {
        $authority.backgroundGate = 'background-gate-tampered'
        $authority.backgroundGateTamperDetail = if ($null -eq $backgroundHookPostRunSha256) {
            'background-gate-tampered: hook copy missing after run'
        } else {
            "background-gate-tampered: sha256 was $backgroundHookLaunchSha256, now $backgroundHookPostRunSha256"
        }
    }
}
# Round 14 (sol major, r13): the SAME two-point-in-time check as directly above, now also for
# the registered `command` executable itself -- the interpreter, hashed at self-test time
# (above, before the provider ever started). An editing lane's own foreground Bash/PowerShell
# grant can delete or replace a user-writable interpreter mid-run; this is the only thing that
# can catch it after the fact. Same literal, same field, appended (not overwritten) if the hook
# copy above ALSO tampered, so neither finding masks the other -- see workEvidence.reason above
# for the same append-not-overwrite reasoning. Exactly two points in time here too (launch,
# exit): a replace-then-restore inside the run leaves both hashes matching and records nothing,
# same limitation docs/lane-containment.md already states for the hook copy.
if ($cfg.engine -eq 'claude' -and $backgroundInterpreterLaunchSha256) {
    $backgroundInterpreterPostRunSha256 = if (Test-Path -LiteralPath $PYTHON_EXE) {
        (Get-FileHash -LiteralPath $PYTHON_EXE -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { $null }
    if ($backgroundInterpreterPostRunSha256 -ne $backgroundInterpreterLaunchSha256) {
        $authority.backgroundGate = 'background-gate-tampered'
        $interpreterTamperDetail = if ($null -eq $backgroundInterpreterPostRunSha256) {
            'background-gate-tampered: interpreter missing after run'
        } else {
            "background-gate-tampered: interpreter sha256 was $backgroundInterpreterLaunchSha256, now $backgroundInterpreterPostRunSha256"
        }
        $authority.backgroundGateTamperDetail = if ($authority.Contains('backgroundGateTamperDetail') -and $authority.backgroundGateTamperDetail) {
            "$($authority.backgroundGateTamperDetail) | $interpreterTamperDetail"
        } else { $interpreterTamperDetail }
    }
}
$workCompleted = ($null -eq $failure -and $null -eq $providerRefusal -and $processEnded -and $workEvidence.workCompleted -eq $true)
$receipt = [ordered]@{
    schema       = 'mlv-app/fleet-lane-receipt/v1'
    # SAME KEY AT EVERY STAGE. A reader checks `state` once - reserved, complete or
    # failed - instead of inferring liveness from which fields happen to be present.
    # 'ended-incomplete' = the process exited but there is no positive evidence the work finished.
    state        = if ($null -ne $failure) { 'failed' }
                   elseif ($null -ne $providerRefusal) { 'refused' }
                   elseif ($workCompleted) { 'complete' }
                   elseif ($processEnded) { 'ended-incomplete' }
                   else { 'incomplete' }
    lane         = $Lane
    role         = $cfg.role
    engine       = $cfg.engine
    # LANE-MODEL-CURRENCY-1: requestedModel is the table's alias/tier (the lane's
    # identity); resolvedModel is what actually ran -- a codex tier's resolved slug, or,
    # for a claude lane, the canonicalModel(s) of the modelUsage entry matching the
    # requested alias's family -- and stays 'unknown' (never the requested string copied
    # over, never a raw modelUsage key) when that cannot be determined. auxiliaryModels
    # carries every OTHER modelUsage entry (auxiliary/tool-call models, e.g. haiku) that
    # was seen but never counted as the lane's own identity. selectedModel (round 2) is the
    # PRE-LAUNCH choice -- the exact --model/-m argument value -- recorded even when the child
    # never started, so a start failure is never silently unrecorded; it is never a substitute
    # for resolvedModel's proof.
    requestedModel   = $RequestedModel
    selectedModel    = $SelectedModel
    resolvedModel    = $ResolvedModel
    auxiliaryModels  = $AuxiliaryModels
    pinnedByOverride = $PinnedByOverride
    effort       = $cfg.effort
    card         = $Card
    workDir      = $WorkDir
    allowEdits   = [bool]$AllowEdits
    allowedTools = if ($AllowEdits) { $AllowedTools } else { $authority.allowedTools }
    baseSha      = $BaseSha
    hookSha256   = $WorkDirHookSha256
    authority    = $authority
    startedUtc   = $startedUtc.ToString('o')
    endedUtc     = (Get-Date).ToUniversalTime().ToString('o')
    durationSec  = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    timedOut     = $timedOut
    timeoutSec   = $TimeoutSec
    exitCode     = $exitCode
    promptSha256 = Get-Sha256 $Prompt
    promptBytes  = [System.Text.Encoding]::UTF8.GetByteCount($Prompt)
    outputSha256 = Get-Sha256 $final
    outputBytes  = [System.Text.Encoding]::UTF8.GetByteCount($final)
    promptPath   = $promptPath
    outputPath   = $lastPath
    stdoutPath   = $outPath
    stderrPath   = $errPath
    failure      = $failure
    dispatchLedger = [ordered]@{ state = $dispatchLedgerState; reservationId = $dispatchLedgerReservationId }
    # null when the provider did the work. Otherwise {kind, engine, match, retryAfter, remedy};
    # `complete` is false in that case even though `failure` is null -- the lane script did not
    # fail, the provider declined, and a reader must never mistake that for a verdict.
    providerRefusal = $providerRefusal
    containment  = $containment
    scratch      = $scratchDisposition
    worktreeDisposition = $worktreeDisposition
    worktreeSweep = $worktreeSweep
    processEnded = $processEnded
    complete     = $workCompleted
    workEvidence = $workEvidence
    # Round 5 (sol minor 1): what the dirty-no-commit check above actually determined --
    # 'not-applicable' (gate did not apply, or HEAD moved), 'unavailable' (a git capture failed;
    # see dirtyCheckReason for pre- vs post-snapshot and why), 'clean', or 'dirty' (folded into
    # workEvidence.reason as dirty-worktree-no-commit). A reader must never treat 'unavailable'
    # as either verdict.
    dirtyCheck       = $dirtyCheck
    dirtyCheckReason = $dirtyCheckReason
    spend        = [ordered]@{
        costUsd            = $costUsd
        costReported       = ($null -ne $costUsd)
        # Absence is NOT zero. codex exec emits no cost telemetry, so a codex receipt
        # says so explicitly instead of implying a free run.
        costUnavailableWhy = if ($null -ne $costUsd) { $null }
                             elseif ($cfg.engine -eq 'codex') { 'codex exec reports no cost telemetry' }
                             else { 'claude json carried no total_cost_usd (died before emitting it?)' }
        numTurns           = $numTurns
        cacheCreateTokens  = $cacheCreateTokens
        cacheReadTokens    = $cacheReadTokens
        outputTokens       = $outputTokens
    }
}
try {
    Write-Utf8NoBomAtomic $rcptPath (($receipt | ConvertTo-Json -Depth 6))
} finally {
    # Receipt I/O failure must not retain the job handle and its provider tree.
    if ($jobHandle -ne [IntPtr]::Zero) {
        [void][MlvLaneJob]::CloseHandle($jobHandle)
        $jobHandle = [IntPtr]::Zero
    }
}

}   # end finally - the receipt is now written on EVERY exit path

Write-Host ("[{0}] {1}/{2}->{3} effort={4} exit={5} {6}s cost={7} -> {8}" -f `
    $Lane, $cfg.engine, $RequestedModel, $ResolvedModel, $cfg.effort, $exitCode, $receipt.durationSec,
    $(if ($null -ne $costUsd) { 'USD ' + ([math]::Round($costUsd,2)) } else { 'unreported' }),
    $rcptPath)

if ($timedOut) { Write-Host "  TIMED OUT after ${TimeoutSec}s - output is partial." }
if ($null -ne $providerRefusal) {
    Write-Host ("  PROVIDER REFUSED ({0}): {1}" -f $providerRefusal.kind, $providerRefusal.match)
    Write-Host ("  remedy: {0}" -f $providerRefusal.remedy)
}

# Emit the receipt so a caller can pipeline on it.
[pscustomobject]$receipt

# PROPAGATE THE OUTCOME. This script's own header says "it is alive because it is running and
# dead when it exits, and THE EXIT CODE IS THE TRUTH" -- and until now it did not honour that at
# its own boundary. It computed $exitCode, wrote it into the receipt, printed it, and then fell
# off the end, which in PowerShell exits 0. Invoke-Workstream.ps1 faithfully captured that
# $LASTEXITCODE and logged laneExitCode=0, so EVERY failed lane was recorded as a success in the
# only durable log the loop consults.
#
# Measured 2026-09-05, two dispatches of twelve:
#   CITE-TXN-1          181.5s  USD 2.24  api_error_status 429 "You've hit your session limit"
#   GATE-FAMILY-BOOKED    2.8s  USD 0     api_error_status 429
# Both receipts carried exitCode 1. Both dispatch-log rows carried laneExitCode 0. Two of the
# day's twelve dispatches produced nothing and were indistinguishable from work that succeeded.
#
# Sentinels are mapped rather than passed through: a negative value does not survive a process
# exit code intact, and -1 would surface as 255. 124 for a timeout matches the taxonomy the repo
# already uses in boundedRunnerExitCodes; 127 marks "reserved but never completed", which the
# receipt also records as complete=false with the failure carried.
# 125 = the provider refused (usage limit, 429, auth). Chosen beside 124/127 so a dispatcher
# reading laneExitCode can tell "nobody did the work" from "the work was done and objected".
$propagated = switch ($exitCode) {
    # First and with `break`: PowerShell evaluates EVERY matching clause, and a refusal can
    # arrive with any child exit code. "Nobody did the work" outranks how the child coded it.
    { $null -ne $providerRefusal } { 125; break }   # provider refused; see receipt.providerRefusal
    -1      { 124 }   # timed out
    -999    { 127 }   # slot reserved, no completion recorded
    default { if ($exitCode -ge 0 -and $exitCode -le 255) { $exitCode } else { 1 } }
}
exit $propagated
