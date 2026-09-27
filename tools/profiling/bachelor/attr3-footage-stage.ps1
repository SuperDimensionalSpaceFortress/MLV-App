# attr3-footage-stage.ps1 -- the ONE tracked, id-only entry point that stages an owner-consented
# clip onto Bachelor, fully automated: resolve -> verify the source on THIS host -> transfer to
# the agent share -> emit and submit a Bachelor job that places the bytes at the spec path.
# ATTR3-FOOTAGE-STAGE-1.
#
# WHAT RUNS WHERE. Steps 1-3 below run ON THIS HOST (Virtual-Ten): resolving the clip, verifying
# every source part's bytes, and copying them to the agent share under neutral, index-derived
# names (tools/profiling/bachelor/AttrCudaOwnerFootage.psm1's Send-AttrCudaOwnerFootagePartTo-
# Staging). Step 4 builds a job (tools/profiling/bachelor/Attr3FootageStageJob.psm1) and submits
# it through tools/profiling/um-run.ps1, which is the ONLY tracked writer of the agent's inbox --
# step 4's own job then runs on Bachelor, with no checkout and no module path of its own, and does
# the actual placement at the spec path. This script never places a file at that path itself.
#
# THE RESOLVER CHILD PROCESS IS THE ONLY WAY THIS SCRIPT OBTAINS PARTS. There is no parameter
# that accepts a caller-typed path for any part, mirroring attr3-footage-presence-job.ps1's own
# contract: tools/gates/resolve_consented_clip.py alone proves the frozen spec agrees with the
# hook's frozen consent table for -ClipId, and only its --emit-json output -- read by this
# process, never printed -- ever names a part's real path.
#
# NO PATH EVER REACHES THIS SCRIPT'S OWN OUTPUT. Every Write-Output line below names the clip id,
# a part index, a part's staging/verification STATUS TOKEN, or a job id -- never a footage path,
# source or target. The resolver's own stdout (captured only to fold into a refusal message) is
# already path-free by contract (its CLI never prints a part path); Send-AttrCudaOwnerFootagePart-
# ToStaging's and um-run.ps1's own thrown/returned text carry the same guarantee.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\attr3-footage-stage.ps1 -ClipId M16-1243
# Exits 0 with RESULT=FOOTAGE_STAGED when Bachelor now holds every part at its spec path; exits 1
# with RESULT=FOOTAGE_STAGE_REFUSED (or an ATTR3_FOOTAGE_STAGE_* refusal token) on an ordinary,
# safely-retryable failure; exits 2 with RESULT=FOOTAGE_STAGE_UNRESOLVED when um-run.ps1 itself
# stopped waiting on the placement submission without proof the agent is either done with it or
# dead (ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11) -- NOT a failure and NOT safely retryable on
# this evidence alone: the agent may still be running the job and may still publish a receipt for
# it after this process has already exited. This attempt's own already-staged share-side parts are
# LEFT IN PLACE on UNRESOLVED (RESIDUE=RETAINED, round 12) -- never deleted while the agent may
# still be reading them. Idempotent: a clip already fully present on Bachelor at
# its spec path transfers nothing new and still reports FOOTAGE_STAGED (every part lands
# ALREADY_PRESENT).

$ErrorActionPreference = 'Stop'

# ATTR3-FOOTAGE-STAGE-1 round 11 (astra major: inherited verbose diagnostics disclose full
# paths). Pinned FIRST, before anything else in this script runs -- including the Import-Module
# calls below, whose own Write-Verbose logging ("Loading module from path '...'") names this
# checkout's absolute path once a caller's ambient $VerbosePreference is Continue, and which this
# script's own outer catch and per-call output filtering (ConvertTo-Attr3FootageStageSafeOutput,
# the cleanup WarningAction suppression) never touched, because they operate on THIS script's own
# success-stream text, never on a PowerShell diagnostic stream a cmdlet writes to directly. A
# script-scoped preference variable is read by dynamic scope lookup -- every cmdlet this script
# calls directly, every function it defines, and every already-loaded module function it calls
# (proven empirically: an Import-Module invoked from inside a module function still honours a
# caller-set $VerbosePreference several scopes up, since neither PowerShell's own module-loading
# cmdlet nor these repository modules override it locally) -- all resolve these five preference
# variables from this scope unless something between here and the call overrides them again. This
# is the SAME boundary this script's own header already claims ("NO PATH EVER REACHES THIS
# SCRIPT'S OWN OUTPUT") -- these five diagnostic streams are part of that boundary, not separate
# from it. $ProgressPreference is included even though no progress record has been observed here;
# Write-Progress records can carry arbitrary caller-supplied activity text and cost nothing to
# suppress alongside the other four.
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

# ATTR3-FOOTAGE-STAGE-1 round 7 (class b: outer boundary). Everything from the argument parsing
# below through this script's own final `exit 0` runs inside ONE try/catch -- most notably the
# two job-emission calls (New-Attr3FootagePresenceJob, New-Attr3FootageStageJob) that were
# previously the only two statements in this script's whole body with no try/catch of their own
# at all. Every throw site between here and the bottom of this script already follows this
# script's own established contract -- a FIXED, all-caps TOKEN followed by a path-free
# description (ATTR3_FOOTAGE_STAGE_*, and the ATTR3_PRESENCE_*/ATTR3_STAGE_* tokens the two job
# builders throw on their own) -- so the catch below forwards a message already shaped that way
# VERBATIM, preserving every existing refusal token's own text and exit behaviour. Anything NOT
# shaped that way is an exception this script never anticipated -- a raw .NET exception surfaced
# through an untried code path, whose own .Message can carry a real footage path -- and is never
# forwarded: only the exception's TYPE NAME (never its .Message) is reported.
try {

# ATTR3-FOOTAGE-STAGE-1 round 9 (sol + astra major: no PowerShell parameter binding at all). This
# script has no `param()` block and no `[CmdletBinding()]` -- a plain script (never an "advanced"
# one) gets no parameter binder of its own at all, so PowerShell never intercepts a common
# parameter (-ErrorAction, -Verbose, -Debug, ...) on its own behalf, and there is no auto-generated
# binding-failure message that could ever echo a caller-supplied value verbatim (the exact class of
# leak every prior round's own [ValidatePattern]/typed-parameter removal already worked around one
# parameter at a time -- see this file's git history). Every element PowerShell hands this script
# lands, untouched and in order, in $args; parsed here, in the body, where every refusal is a
# FIXED token, never $args itself. Two named arguments are accepted, matched case-insensitively:
# -ClipId (mandatory) and -TimeoutSec (optional). Anything else at all -- an unknown name, a
# missing value, a duplicate, a bare/unnamed surplus token, or a common-parameter name like
# -ErrorAction/-Verbose (which PowerShell tokenizes into its own -Name/value pair at the language
# level even with no binder present, per this round's own probe) -- is refused by the SAME fixed
# token below, before the offending token is ever compared to anything but a fixed literal name.
$ClipId = $null
$TimeoutSec = '1800'
$sawClipId = $false
$sawTimeoutSec = $false
$argCursor = 0
while ($argCursor -lt $args.Count) {
    $argToken = [string]$args[$argCursor]
    if ($argToken -ieq '-ClipId' -and -not $sawClipId) {
        if (($argCursor + 1) -ge $args.Count) {
            throw 'ATTR3_FOOTAGE_STAGE_SURPLUS_ARGUMENT an unsupported or unexpected argument was supplied'
        }
        $ClipId = [string]$args[$argCursor + 1]
        $sawClipId = $true
        $argCursor += 2
        continue
    }
    if ($argToken -ieq '-TimeoutSec' -and -not $sawTimeoutSec) {
        if (($argCursor + 1) -ge $args.Count) {
            throw 'ATTR3_FOOTAGE_STAGE_SURPLUS_ARGUMENT an unsupported or unexpected argument was supplied'
        }
        $TimeoutSec = [string]$args[$argCursor + 1]
        $sawTimeoutSec = $true
        $argCursor += 2
        continue
    }
    throw 'ATTR3_FOOTAGE_STAGE_SURPLUS_ARGUMENT an unsupported or unexpected argument was supplied'
}
if (-not $sawClipId) {
    throw 'ATTR3_FOOTAGE_STAGE_SURPLUS_ARGUMENT an unsupported or unexpected argument was supplied'
}

# ATTR3-FOOTAGE-STAGE-1 round 5 (sol/astra: ClipId validation failure prints a fixed token). A
# caller-typed value that fails this pattern -- e.g. a real path where a clip id belongs -- gets
# only a fixed token, never $ClipId itself.
if ($ClipId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$') {
    throw 'ATTR3_FOOTAGE_STAGE_CLIP_ID_INVALID -ClipId does not match the required id pattern'
}

# ATTR3-FOOTAGE-STAGE-1 round 6 (sol blocker / astra major: sweep staleness threshold), still
# body-validated post-round-9 for the same reason: a [ValidateRange] attribute cannot range-check
# a string, and there is no parameter attribute of any kind on this script any more regardless.
# $MinAttr3FootageStageTimeoutSec is the one place both the CLI's own accepted range and the
# staleness sweeps' floor (Attr3FootageStageJob.psm1, AttrCudaOwnerFootage.psm1) are meant to agree
# on; a caller passing zero, a negative number, or anything not a plain integer gets a fixed token,
# never the offending value.
#
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 6 (sol major 2): $MaxAttr3FootageStageTimeoutSec mirrors
# UmRunDrop.psm1's own accepted 1..86400 range for -JobTimeoutSec. Before this, a value above 86400
# was accepted here and discovered only inside Invoke-UmRunDrop, AFTER step 5's multi-GB transfer
# had already run -- an inevitably refused request paid for and then discarded the whole placement.
# Both bounds are checked here, before step 1 (the resolver) even runs, so an out-of-range request
# never reaches a single byte of work.
$MinAttr3FootageStageTimeoutSec = 30
$MaxAttr3FootageStageTimeoutSec = 86400
$timeoutSecValue = 0
if (-not [int]::TryParse($TimeoutSec, [ref]$timeoutSecValue) -or
    $timeoutSecValue -lt $MinAttr3FootageStageTimeoutSec -or
    $timeoutSecValue -gt $MaxAttr3FootageStageTimeoutSec) {
    throw "ATTR3_FOOTAGE_STAGE_TIMEOUT_SEC_INVALID -TimeoutSec must be an integer between $MinAttr3FootageStageTimeoutSec and $MaxAttr3FootageStageTimeoutSec"
}

# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (sol minor): the presence preflight below is a hash
# verification optimisation with a bounded, known-cheap workload (it never transfers a byte) -- it
# is not the placement itself, so it must not inherit the placement's own (possibly hour-long)
# -TimeoutSec. Before this, a stuck preflight on a large -TimeoutSec silently delayed fallback to the
# real transfer for the full placement budget instead of the old fixed 1800s agent ceiling. Capped
# independently, and never below the CLI's own floor.
$MaxAttr3FootagePresenceTimeoutSec = 300
$presenceTimeoutSecValue = [Math]::Max($MinAttr3FootageStageTimeoutSec, [Math]::Min($timeoutSecValue, $MaxAttr3FootagePresenceTimeoutSec))

# ATTR3-FOOTAGE-STAGE-1 round 4 (sol BLOCKER 1): -AgentShare and -AgentRootOnHost used to be
# public parameters -- the id-only interface's actual remaining authority boundary, since a
# caller-supplied share or agent root could redirect every byte this script transfers and every
# job it submits to a destination of the CALLER's choosing, not the owner-consented one. Both are
# now fixed constants, equal to what were previously their only-ever-used defaults; nothing on
# this script's public surface can change either. A test that needs a synthetic stand-in share
# calls the underlying functions (the AttrCudaOwnerFootage.psm1 part-to-share transfer function,
# New-Attr3FootageStageJob, New-Attr3FootagePresenceJob) directly with its own
# -AgentRoot/-StagingDirectory, never through this CLI -- exactly the same split the
# resolver-only-source-of-parts contract above already relies on.
$AgentShare = '\\bachelor\mlv-agent'
$AgentRootOnHost = 'C:\mlvtmp\mlv-agent'

# ATTR3-FOOTAGE-STAGE-1 round 11: -Verbose:$false belt-and-suspenders over the script-scoped
# $VerbosePreference pin above -- an explicit -Verbose:$false always wins over ambient preference
# regardless of how a future caller's environment sets it, so this line does not depend on the
# pin above staying in place, or staying first, to keep these four imports path-free.
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force -Verbose:$false
Import-Module (Join-Path $PSScriptRoot 'AttrCudaOwnerFootage.psm1') -Force -Verbose:$false
Import-Module (Join-Path $PSScriptRoot 'Attr3FootageStageJob.psm1') -Force -Verbose:$false
Import-Module (Join-Path $PSScriptRoot 'Attr3FootagePresenceJob.psm1') -Force -Verbose:$false

# ATTR3-FOOTAGE-STAGE-1 round 8 (scope cut): the round 5 start-of-run stale-attempt sweep was
# removed here -- see Attr3FootageStageJob.psm1's own header CHANGELOG note. A failed attempt's
# own per-job share staging directory (named from a fresh random jobId) is simply left for a
# human to clear; nothing about a later, unrelated run depends on it being gone.
$shareStageRoot = Join-Path $AgentShare 'footage-stage'

# ATTR3-FOOTAGE-STAGE-1 round 3 (sol BLOCKER, astra MAJOR): -RepoRoot used to be a public
# parameter, so a caller-supplied alternate tree could supply a replacement resolver and
# submitter -- the id-only interface's actual authority boundary. The resolver and submitter
# now come ONLY from the tracked checkout that holds THIS script, never from caller input.
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path

function Resolve-Attr3StagePython {
    <#
    .SYNOPSIS
    Prefer `python.exe` only after proving it is Python 3; else fall back to the `py` launcher
    pinned to -3. Mirrors attr3-footage-presence-job.ps1's own copy of this probe (and
    tools/coordination/board-health-sweep.ps1's) -- this machine still carries a Python 2
    `python.exe` on some hosts, so simply assuming `py -3` (or that `python.exe` is 3.x) is not a
    valid availability check.
    #>
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $major = @(& $python.Source -c 'import sys; print(sys.version_info[0])' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $major.Count -eq 1 -and $major[0] -eq '3') {
            return [pscustomobject]@{ Exe = $python.Source; PrefixArgs = @() }
        }
    }
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        return [pscustomobject]@{ Exe = $pyLauncher.Source; PrefixArgs = @('-3') }
    }
    throw 'ATTR3_FOOTAGE_STAGE_NO_PYTHON no Python 3 interpreter is available to run the resolver'
}

function ConvertTo-Attr3FootageStageSafeOutput {
    <#
    .SYNOPSIS
    Parse a submitted Bachelor job's raw stdout+stderr and return ONLY the PART=/RESULT= lines
    whose shape this script already knows, reconstructed from their own matched groups -- never
    the raw job text itself.
    .DESCRIPTION
    ATTR3-FOOTAGE-STAGE-1 round 4 (sol MAJOR, astra 4): the submitted job's own text is trusted to
    be path-free by THAT job's own contract, but this script's job is never to simply believe that
    contract by forwarding it verbatim -- a submitter/agent bug, or a future job template that
    stops upholding it, must not turn into a path leaking through THIS script's own output. Every
    line that does not fully match one of the two allowlisted shapes below -- including anything
    that merely LOOKS like one, and the job's own path-free JSON summary line -- is silently
    dropped, never echoed.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][string]$ClipId
    )

    $safeLines = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^PART=(\d+) STATUS=([A-Z][A-Z0-9_]*)$') {
            $safeLines.Add("PART=$($Matches[1]) STATUS=$($Matches[2])")
            continue
        }
        if ($line -match '^RESULT=([A-Z][A-Z0-9_]*) CLIP=([A-Za-z0-9][A-Za-z0-9_.-]{0,63}) PARTS=(\d+)$' -and $Matches[2] -ceq $ClipId) {
            $safeLines.Add("SUBMITTER RESULT=$($Matches[1]) PARTS=$($Matches[3])")
        }
    }
    return ,@($safeLines)
}

function Get-Attr3FootageUmRunFailureClass {
    <#
    .SYNOPSIS
    Classify an um-run.ps1 failure as RETRACTED (queue ceiling reached, withdrawn before the agent
    ever claimed it), UNRESOLVED (claimed, then this client stopped waiting without proof of
    success OR death), or UNKNOWN (anything else -- a dead heartbeat, a missing script, ...), from
    a FIXED substring match only -- the exception's own .Message is never returned or otherwise
    echoed, since um-run.ps1's own diagnoses name real agent-share paths (the result file, the
    claim marker).
    .DESCRIPTION
    ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 4 (fable major): the round-2 CLASS=$exceptionClass
    slot on the outer catch already exists for an unanticipated .NET exception; this reuses that
    same convention for um-run.ps1's own named outcomes so a refusal says WHICH one fired without
    ever forwarding the message that proves it.
    ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (hub ruling): um-run.ps1 no longer synthesizes a
    generic timeout failure at all -- every non-receipt outcome it can throw is now RETRACTED or
    UNRESOLVED, named as a fixed prefix on the thrown message (see um-run.ps1's own header).
    Neither is a failure this script may treat as one: RETRACTED is a clean refusal (the job never
    ran), and UNRESOLVED means the agent may still own the job and its receipt may still land
    later -- this script's own callers must not retry on UNRESOLVED as if it were an ordinary,
    safely-retryable failure.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Message)

    if ($Message -match '^RETRACTED:') { return 'RETRACTED' }
    if ($Message -match '^UNRESOLVED:') { return 'UNRESOLVED' }
    return 'UNKNOWN'
}

$ResolverPath = Join-Path $RepoRoot 'tools\gates\resolve_consented_clip.py'
if (-not (Test-Path -LiteralPath $ResolverPath -PathType Leaf)) {
    # ATTR3-FOOTAGE-STAGE-1 round 3 (no path in any branch): the resolver's location is fixed
    # relative to this tracked script, never caller input, so naming it here has no diagnostic
    # value a caller could act on and only ever leaked this host's own directory layout.
    throw 'ATTR3_FOOTAGE_STAGE_RESOLVER_MISSING the tracked resolver script is missing from this checkout'
}
$py = Resolve-Attr3StagePython

# --- 1. RESOLVE: the resolver child process is the only way this script obtains parts ----------
# Same pattern as attr3-footage-presence-job.ps1: paths flow only through a private temp file this
# process reads IN-PROCESS and deletes in a `finally`, never through a tool call or this script's
# own stdout.
$emitPath = Join-Path ([IO.Path]::GetTempPath()) ("attr3-footage-stage-resolve-$([guid]::NewGuid().ToString('N')).json")
try {
    $resolverArgs = @($py.PrefixArgs) + @($ResolverPath, '--clip-id', $ClipId, '--repo-root', $RepoRoot, '--emit-json', $emitPath)
    # ATTR3-FOOTAGE-STAGE-1 round 5 (sol/astra: resolver failure output mapped to a fixed token,
    # never interpolated). The resolver's own CLI contract says its stdout/stderr never carries a
    # part path -- but this script no longer trusts that contract by folding the raw text into its
    # own thrown message (the same "don't trust an inner contract, however documented" standard
    # ConvertTo-Attr3FootageStageSafeOutput below already applies to the submitted job's output).
    # The resolver's own text is still READ (so $resolverExit is meaningful), just never echoed.
    try {
        $null = @(& $py.Exe @resolverArgs 2>&1)
        $resolverExit = $LASTEXITCODE
    } catch {
        # A launch failure (not a nonzero exit -- the child never even started) throws a raw
        # PowerShell exception whose own message can name this host's interpreter path.
        throw 'ATTR3_FOOTAGE_STAGE_RESOLVER_LAUNCH_FAILED the resolver could not be started'
    }
    if ($resolverExit -ne 0) {
        throw "ATTR3_FOOTAGE_STAGE_RESOLVE_REFUSED clip '$ClipId' was refused by the resolver (exit $resolverExit)"
    }
    if (-not (Test-Path -LiteralPath $emitPath -PathType Leaf)) {
        throw 'ATTR3_FOOTAGE_STAGE_EMIT_MISSING resolver exited 0 but wrote no --emit-json file'
    }
    $emitBytes = [IO.File]::ReadAllBytes($emitPath)
} finally {
    if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }
}

$resolved = [Text.Encoding]::UTF8.GetString($emitBytes) | ConvertFrom-Json
if ($resolved.clipId -cne $ClipId) {
    throw "ATTR3_FOOTAGE_STAGE_CLIP_ID_MISMATCH resolver emitted clipId '$($resolved.clipId)' for requested '$ClipId'"
}
$parts = @($resolved.parts | Sort-Object { [int]$_.index })
Write-Output "RESOLVED clip=$ClipId parts=$($parts.Count)"

$umRun = Join-Path $RepoRoot 'tools\profiling\um-run.ps1'

# --- 2. PRESENCE PREFLIGHT: ask Bachelor whether it already holds every part, byte-exact, at its
#        spec path, BEFORE verifying or transferring a single byte (ATTR3-FOOTAGE-STAGE-1 round 3,
#        sol/astra PR #148 MAJOR: a successful prior run consumes its staged copies, so without
#        this a rerun always retransfers before it can even ask). Reuses the existing footage-
#        presence probe (Attr3FootagePresenceJob.psm1) -- never a second implementation of "does
#        Bachelor already have this". Its jobId now carries a fresh random component on every
#        call, so a retained receipt from an EARLIER attempt's own preflight never blocks this
#        one (UMRUN_JOBID_IN_USE), which would otherwise silently fall through to a full
#        retransfer.
# ATTR3-FOOTAGE-STAGE-1 round 4 (sol minor / astra 5: per-part resume). The preflight's own
# per-part JSON payload decides which parts are already good, via Get-Attr3FootagePresentPart-
# Indexes (Attr3FootagePresenceJob.psm1 -- split out so a test can drive the parsing directly),
# so a rerun after a partial success transfers only what is actually missing rather than
# retransferring everything.
$presenceOutDir = Join-Path ([IO.Path]::GetTempPath()) ("attr3-footage-stage-presence-$([guid]::NewGuid().ToString('N'))")
$presenceJob = New-Attr3FootagePresenceJob -ClipId $ClipId -Parts $parts -OutDir $presenceOutDir
$presentIndexArray = @()
try {
    # ATTR3-FOOTAGE-STAGE-1 round 5 (sol/astra: output channels). um-run.ps1 itself (and
    # UmRunDrop.psm1 underneath it) reports side-file placement and submission progress via
    # Write-Host -- the Information stream (6), not this call's own success-stream return value --
    # including a "submitted <id> -> <path>" line that names a real agent-share path. `6>$null`
    # discards that stream at the call site so it never reaches this process's own console.
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 4 (fable major): um-run.ps1's own -MaxQueueWaitSec
    # defaults to 86400s -- a bound sized for a job ahead of THIS one in the agent's queue, not for
    # a dead/wedged agent that never claims the job at all. Left at that default, a preflight this
    # script capped at $MaxAttr3FootagePresenceTimeoutSec seconds precisely so a stuck run cannot
    # delay fallback (see that constant's own comment) could still sit unclaimed for up to a day.
    # Bounded here to the SAME per-caller value already governing the preflight's own execution
    # budget -- derived from the caller's -TimeoutSec, never the global default -- so the whole
    # preflight (queue + exec + grace) stays a small multiple of what the caller actually asked for.
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (fable minor, item 3): -MaxClaimedWaitSec is
    # bounded the same way -MaxQueueWaitSec already is -- left at um-run.ps1's own 86400s default,
    # an agent that claims this preflight and then goes idle (a lost receipt, an untagged
    # between-jobs heartbeat that is not proof of work on OUR job) could hold this "optimization,
    # not a correctness requirement" preflight for up to a day before the exception handler below
    # ever gets a chance to fall through to the real transfer path.
    $presenceResult = & $umRun -ScriptPath $presenceJob.jobFile -JobId $presenceJob.jobId -AgentShare $AgentShare -TimeoutSec $presenceTimeoutSecValue -MaxQueueWaitSec $presenceTimeoutSecValue -MaxClaimedWaitSec $presenceTimeoutSecValue 6>$null
    $presentIndexArray = @(Get-Attr3FootagePresentPartIndexes -Stdout $presenceResult.stdout -ClipId $ClipId)
} catch {
    # Presence is an optimization, not a correctness requirement -- if the preflight itself could
    # not be submitted or timed out, fall through to the normal verify-and-transfer path below
    # rather than failing the whole run over an inconclusive probe. CLASS distinguishes "never
    # claimed" (queue-bound expiry) from "claimed but slow" (exec-bound expiry) from anything else,
    # the same fixed-token phase diagnosis the real placement submission below reports on refusal --
    # never the wrapped message itself, which can name a real agent-share path.
    $presenceFailClass = Get-Attr3FootageUmRunFailureClass -Message ([string]$_.Exception.Message)
    Write-Output "PRESENCE PREFLIGHT=INCONCLUSIVE CLASS=$presenceFailClass"
}
$presentIndexes = New-Object 'System.Collections.Generic.HashSet[int]'
foreach ($presentIndex in $presentIndexArray) { [void]$presentIndexes.Add([int]$presentIndex) }

$needsWork = @($parts | Where-Object { -not $presentIndexes.Contains([int]$_.index) })

if ($needsWork.Count -eq 0) {
    Write-Output "RESULT=FOOTAGE_STAGED CLIP=$ClipId PARTS=$($parts.Count) JOB=$($presenceJob.jobId) ALREADY_PRESENT=true"
    exit 0
}

# --- 3. VERIFY only the parts the preflight found missing or mismatched, before anything is sent
#        anywhere -----------------------------------------------------------------------------
foreach ($part in $needsWork) {
    # ATTR3-FOOTAGE-STAGE-1 round 5 (astra major, source link check): the resolver's own
    # cross-check proves this path's BYTES agree with the frozen consent table -- it says nothing
    # about whether the path itself is a reparse point planted since. Every EXISTING ancestor of
    # -SourcePath, down to and including the leaf, is proved link-free BEFORE a single byte is
    # read or hashed here -- the same target-chain check Attr3FootagePresenceJob.psm1's own probe
    # and Attr3FootageStageJob.psm1's own target-path check already apply on their own sides.
    # This gates step 5's own transfer too: that loop below only ever processes a part already
    # proved link-free by this same check, since both loops iterate the same $needsWork list.
    $sourceDriveRoot = [IO.Path]::GetPathRoot([string]$part.path)
    try {
        [void](Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $sourceDriveRoot -Path $part.path)
    } catch {
        Write-Output "SOURCE PART=$($part.index) STATUS=SOURCE_PATH_UNSAFE"
        throw "ATTR3_FOOTAGE_STAGE_SOURCE_PATH_UNSAFE part $($part.index) source path chain contains a reparse point"
    }
    $status = Test-AttrCudaFootagePart -Path $part.path -ExpectedLength ([int64]$part.length) -ExpectedSha256 ([string]$part.sha256)
    Write-Output "SOURCE PART=$($part.index) STATUS=$status"
    if ($status -ne 'PASS') {
        throw "ATTR3_FOOTAGE_STAGE_SOURCE_VERIFY_FAILED part $($part.index) failed source verification ($status)"
    }
}

# --- 4. BUILD the job first, for only the parts that still need work (round 4: per-part resume):
#        its jobId names the SAME per-job staging directory this script transfers into next, so
#        both sides agree on it without exchanging state. A fresh random component in the jobId
#        (round 3) means a retried invocation never collides with a retained receipt from an
#        earlier attempt's own submission (UMRUN_JOBID_IN_USE).
$stageOutDir = Join-Path ([IO.Path]::GetTempPath()) ("attr3-footage-stage-job-$([guid]::NewGuid().ToString('N'))")
$job = New-Attr3FootageStageJob -ClipId $ClipId -Parts $needsWork -OutDir $stageOutDir -AgentRoot $AgentRootOnHost
$shareStageDir = Join-Path $shareStageRoot $job.jobId

# ATTR3-FOOTAGE-STAGE-1 round 8 (scope cut): on ANY failure below, cleanup removes only the
# share-side slots THIS ATTEMPT itself created -- tracked here by path, per slot, as each
# Send-AttrCudaOwnerFootagePartToStaging call reports whether it actually created its final slot
# (Created=$true) or found one already correct and untouched (Created=$false; never this
# attempt's to delete). This replaces the round 4 directory-pattern sweep
# (Remove-AttrCudaOwnerFootageStagingResidue), which enumerated and deleted every part-<n>-shaped
# entry in the directory regardless of which attempt actually wrote it.
$createdSharePaths = New-Object System.Collections.Generic.List[string]
function Remove-Attr3FootageStageAttemptResidue {
    # ATTR3-FOOTAGE-STAGE-1 round 10 (path disclosure): Remove-AttrCudaPartialFile
    # (AttrCudaArtifacts.psm1) Write-Warnings the real path on a refused removal
    # (ATTRCUDA_PARTIAL_OUTSIDE_TRUSTED_ROOT / ATTRCUDA_PARTIAL_NOT_A_FILE) -- suppressed here the
    # same way Attr3FootageStageJob.psm1's own Record-PartResult already suppresses it, and a
    # refusal is reported with a fixed token instead, never the path or the return value alone.
    foreach ($createdPath in $createdSharePaths) {
        $partialRemoved = Remove-AttrCudaPartialFile -TrustedRoot $shareStageRoot -Path $createdPath -WarningAction SilentlyContinue
        if (-not $partialRemoved) {
            Write-Output 'ATTR3_STAGE_RESIDUE_LEFT_IN_PLACE a created part slot could not be removed and was left in place'
        }
    }
    # Every created part slot is already gone (or was never this attempt's), so $shareStageDir
    # itself is removed only if that leaves it empty. Re-proves the chain link-free first (the
    # individual removals above already did, for each created path, but only when
    # $createdSharePaths is non-empty).
    #
    # ATTR3-FOOTAGE-STAGE-1 round 9 (astra major): a plain `Remove-Item -Force -Confirm:$false`
    # here used to be trusted to behave like "delete only if empty" because Remove-Item without
    # -Recurse normally REFUSES a non-empty directory via an interactive confirmation prompt --
    # but -Confirm:$false suppresses exactly that prompt, and once suppressed Remove-Item silently
    # recurses into and deletes the directory's contents anyway, which is precisely the
    # un-owned-content deletion this whole function exists to avoid (see its own header: only what
    # THIS ATTEMPT created). [IO.Directory]::Delete($path, $false) is the non-recursive,
    # non-prompting .NET primitive instead: it deletes ONLY an already-empty directory and throws
    # -- never recurses, never prompts -- if anything (this attempt's own untracked residue, or
    # another attempt's) is still inside. A non-empty directory is left exactly where it is and
    # reported with a fixed token, never its own path.
    try {
        [void](Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $shareStageRoot -Path $shareStageDir)
    } catch {
        return
    }
    if (-not (Test-Path -LiteralPath $shareStageDir -PathType Container -ErrorAction SilentlyContinue)) {
        return
    }
    try {
        [IO.Directory]::Delete($shareStageDir, $false)
    } catch {
        Write-Output 'ATTR3_FOOTAGE_STAGE_ATTEMPT_RESIDUE_LEFT the attempt staging directory was left in place (not empty)'
    }
}

# --- 5+6. TRANSFER the missing parts to the agent share, then SUBMIT the pre-built job through
#        um-run.ps1, the only tracked writer of the agent inbox. ATTR3-FOOTAGE-STAGE-1 round 4
#        (sol minor / astra 5: no stranded parts): any failure anywhere in this block -- a
#        transfer error, a submit error, or the job itself reporting anything other than
#        FOOTAGE_STAGED for every part it was given -- removes only the slots THIS ATTEMPT itself
#        created (see $createdSharePaths above), and the directory itself if that leaves it empty.
$attemptFailed = $false
try {
    foreach ($part in $needsWork) {
        try {
            $sendResult = Send-AttrCudaOwnerFootagePartToStaging `
                -SourcePath $part.path `
                -StagingDirectory $shareStageDir `
                -Index ([int]$part.index) `
                -ExpectedLength ([int64]$part.length) `
                -ExpectedSha256 ([string]$part.sha256)
        } catch {
            # ATTR3-FOOTAGE-STAGE-1 round 8 (astra major 2): the underlying exception's own
            # .Message is NEVER folded in here any more -- Send-AttrCudaOwnerFootagePartToStaging
            # now disposes its own streams inside its OWN guarded try (see that function's own
            # header), so a Dispose() failure already maps to a fixed OWNER_FOOTAGE_STAGE_* token
            # there; this catch adds nothing but the part index to a fixed token of its own.
            throw "ATTR3_FOOTAGE_STAGE_TRANSFER_FAILED part $($part.index)"
        }
        if ($sendResult.Created) { $createdSharePaths.Add($sendResult.Path) }
        Write-Output "TRANSFER PART=$($part.index) STATUS=STAGED"
    }

    # ATTR3-FOOTAGE-STAGE-1 round 3 (no-path-in-any-branch): any exception um-run.ps1 itself
    # throws (a dead-agent heartbeat path, a missing script path, a poll timeout naming the result
    # file) carries an operational path in its own text -- converted to a fixed token before it can
    # ever reach this script's own output.
    # ATTR3-FOOTAGE-STAGE-1 round 5 (sol/astra: output channels): `6>$null` discards um-run.ps1's
    # (and UmRunDrop.psm1's) own Write-Host progress lines -- see the presence preflight's own
    # call above for why that stream, left unredirected, can print a real agent-share path.
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 4 (fable major): -MaxQueueWaitSec is bounded to this
    # run's own -TimeoutSec -- derived from the caller's intent, never um-run.ps1's 86400s default
    # -- so a dead or wedged agent that never claims this job fails in a bounded multiple of what
    # the caller asked for instead of sitting queued for up to a day. A caller who wants more queue
    # patience simply asks for a larger -TimeoutSec, which raises both bounds together.
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 3): -MaxClaimedWaitSec is bounded the same
    # way -MaxQueueWaitSec already is, rather than left at um-run.ps1's own 86400s default -- an
    # agent that claims this job and then goes idle (a lost receipt, an untagged heartbeat that is
    # not proof of work on OUR job) would otherwise hold this client for up to a day past this
    # run's own budget before ever reporting UNRESOLVED.
    try {
        $result = & $umRun -ScriptPath $job.jobFile -JobId $job.jobId -AgentShare $AgentShare -TimeoutSec $timeoutSecValue -MaxQueueWaitSec $timeoutSecValue -MaxClaimedWaitSec $timeoutSecValue 6>$null
    } catch {
        $submitFailClass = Get-Attr3FootageUmRunFailureClass -Message ([string]$_.Exception.Message)
        if ($submitFailClass -eq 'UNRESOLVED') {
            # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (hub ruling): UNRESOLVED means the agent
            # may still own this job and may still publish a receipt for it later -- it is neither
            # a success nor a safely-retryable failure (unlike FOOTAGE_STAGE_REFUSED, which a caller
            # may treat as clear to resubmit), so it gets its own RESULT= line and exit code.
            #
            # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (sol BLOCKER + fable MINOR, item 2): this
            # used to call Remove-Attr3FootageStageAttemptResidue here regardless -- deleting this
            # attempt's own already-staged share-side parts out from under a job the very same
            # message says the agent may still be reading, converting a possible late SUCCESS into a
            # near-certain late FAILURE. The staged parts are left in place; RESIDUE=RETAINED names
            # that (a fixed token, never a path, per this script's own contract) so an operator, or a
            # later invocation of this same ClipId once the earlier attempt is known dead, knows
            # there is something at this JobId's own share staging directory to reclaim once a
            # receipt lands or the attempt is abandoned.
            Write-Output "RESULT=FOOTAGE_STAGE_UNRESOLVED CLIP=$ClipId PARTS=$($parts.Count) JOB=$($job.jobId) RESIDUE=RETAINED"
            exit 2
        }
        throw "ATTR3_FOOTAGE_STAGE_SUBMIT_FAILED job could not be submitted or its result could not be retrieved CLASS=$submitFailClass"
    }

    # ATTR3-FOOTAGE-STAGE-1 round 4 (sol MAJOR, astra 4: path-free output): the submitted job's own
    # stdout/stderr are never forwarded verbatim -- only lines matching an allowlist of known
    # shapes, re-emitted from their own matched groups.
    $safeJobOutput = ConvertTo-Attr3FootageStageSafeOutput -Text (@($result.stdout, $result.stderr) -join "`n") -ClipId $ClipId
    foreach ($safeLine in $safeJobOutput) { Write-Output $safeLine }

    if ($result.exitCode -ne 0) { $attemptFailed = $true }
} catch {
    $attemptFailed = $true
    Remove-Attr3FootageStageAttemptResidue
    throw
}

if ($attemptFailed) {
    Remove-Attr3FootageStageAttemptResidue
    Write-Output "RESULT=FOOTAGE_STAGE_REFUSED CLIP=$ClipId PARTS=$($parts.Count) JOB=$($job.jobId)"
    exit 1
}

Write-Output "RESULT=FOOTAGE_STAGED CLIP=$ClipId PARTS=$($parts.Count) JOB=$($job.jobId)"
exit 0
} catch {
    # ATTR3-FOOTAGE-STAGE-1 round 7 (class b: outer boundary) -- see the opening comment on this
    # try block for the forwarding rule.
    $exceptionMessage = [string]$_.Exception.Message
    # -cmatch (case-SENSITIVE), never plain -match: PowerShell's -match is case-insensitive by
    # default, so [A-Z] would otherwise admit lowercase letters too -- "Could not access '...'"
    # (an ordinary .NET exception message) matches [A-Z][A-Z0-9_]{2,}\s under plain -match just as
    # readily as a real ATTR3_FOOTAGE_STAGE_* token does, defeating this whole check.
    if ($exceptionMessage -cmatch '^[A-Z][A-Z0-9_]{2,}\s') {
        [Console]::Error.WriteLine($exceptionMessage)
        exit 1
    }
    $exceptionClass = $_.Exception.GetType().Name
    Write-Output "RESULT=FOOTAGE_STAGE_ERROR CLIP=$ClipId CLASS=$exceptionClass"
    exit 1
}
