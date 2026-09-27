# UmRunDrop.psm1 -- how tools/profiling/um-run.ps1 places side-files and a job into a file-drop
# agent's inbox. Kept in a module so tests can drive the exact code with an observing or faulty
# copier (sol, PR #135 r1: final-state assertions could not prove share verification or ordering).
#
# Invariants:
#   - a side-file name is a plain basename with an allowlisted extension, no trailing dot/space,
#     not a Windows device name, and never job-shaped; after Windows path normalisation it must
#     still be exactly that name (sol r1 BLOCKER: 'evil.job.ps1.' resolves to 'evil.job.ps1');
#   - every temporary name is unique per submission (GUID), so concurrent submitters never share
#     a .sidepart or .job.tmp;
#   - a temporary copy's bytes are re-read FROM THE SHARE and verified before it is renamed, under
#     a handle that denies further WRITES to that path for its whole lifetime but does not deny
#     delete-and-recreate at that same pathname (FileShare.Delete); Move-Item then resolves the
#     PATHNAME, not this handle's identity, so a delete-and-recreate race is a real, OPEN residual,
#     not a closed window -- see Row C in the check/use window table (round 2g applies this
#     identical mechanism, with the identical residual, to the job file's own temporary copy too,
#     which previously had no verification at all);
#   - renames never overwrite: a destination that appeared concurrently makes the rename fail, and
#     the result is accepted only if that destination already holds identical bytes (side-file) --
#     a job file is never replaced;
#   - every side-file is in place before the job is dropped;
#   - a tracked fixture clip is admitted only when its WORKING-TREE bytes are exactly the committed
#     blob at HEAD, never by name/tracked-status alone (ATTR3-ADMIT-CONTENT-PIN-1, fable key on
#     PR #137) -- see Test-UmRunFixtureContentPin;
#   - admission's OWN verification and the SHA256 it hands back are bound to ONE read: the
#     working-tree bytes are opened once, through a single handle that denies concurrent writers
#     for its whole lifetime, and both the committed-blob comparison and the returned pin are
#     computed from that same in-memory buffer (sol BLOCKER / fable MAJOR, PR #140 r2c
#     ATTR3-ADMIT-CONTENT-PIN-1 round 2d: the two used to be SEPARATE reads of the same mutable
#     path inside admission itself -- Test-UmRunFixtureContentPin's own git comparison, then a
#     second, independent Get-FileHash -- so bytes exchanged in that internal gap became the
#     trusted pin, and every downstream binding below faithfully agreed on the swapped bytes
#     because it never saw anything else to compare against);
#   - that admission and the bytes actually placed on the share are the SAME bytes: placement
#     refuses to proceed if a later read of the source no longer matches the pin admission handed
#     back (sol, PR #140 r2 MAJOR: admission and placement used to be independent reads of the
#     same source path, so a swap in THAT gap was never caught either).
#
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10 (hub scope ruling reversed: sol proved BOTH round-9
# premises false -- see summary.md). OWNERSHIP OF A JobId IS NOW CLAIMED ATOMICALLY, BY
# CONSTRUCTION, BEFORE ANY OTHER WORK -- not just when a caller happens to pass a budget:
#   - inbox\<id>.meta.json is written FIRST, before a single side-file byte or job byte is copied,
#     for EVERY submission -- the same no-overwrite atomic rename this module already uses and
#     tests extensively for side-files and the job file itself (Move-Item, no -Force: NTFS and SMB
#     both make this a single all-or-nothing operation, so of two racing renames to the same name
#     exactly one can ever land). A caller with no budget still claims the JobId; its metadata just
#     omits the `timeoutSec` field, which both the tracked and deployed agents already treat as
#     "fall back to my own default" (never a new parser rule -- confirmed against the DEPLOYED
#     agent, see summary.md).
#   - every claim also carries a fresh per-submission `nonce`, so the metadata on disk always
#     identifies which submission attempt actually owns it.
#   - this REVERSES round 4's own ordering rule ("job bytes copied before metadata is published,
#     so metadata is only ever visible once its job already exists"). That rule solved a narrower
#     problem -- a hard-killed submission leaving orphaned metadata -- by making metadata-without-
#     a-job rare (a single rename's width). Round 10 needs metadata to be the FIRST thing written,
#     because ownership has to be established before the side-file/job work it is meant to guard,
#     not after it -- so metadata-without-a-job-yet becomes the NORMAL shape of an in-flight
#     submission, not just a crash artifact. The tolerance mechanism for that is unchanged (see
#     immediately below): it was already built to treat metadata-without-a-job as "maybe still
#     live" up to a grace period, and this reversal simply means that grace period is doing its job
#     across the WHOLE submission now, not just a single rename.
#
# THE TWO CLAIM OUTCOMES (Invoke-UmRunDrop, top of function; round 11 removes the former third,
# age-based-reclaim outcome -- see the header above):
#   1. no metadata exists for this JobId -> this call claims it, writing its own nonce, and
#      becomes the sole owner of every side-file and job placement that follows;
#   2. metadata already exists for this JobId, at ANY age -> refused with UMRUN_JOBID_IN_USE
#      before this call ever touches a side-file or the job, naming "choose a new -JobId" -- the
#      incumbent's ownership, and its side-files/job in flight, are left completely untouched.
#
# ASTRA round 8 / sol+fable round 10 (both proved this false, hub scope ruling reversed again at
# round 11 -- see summary.md): a purely age-based reclaim cannot PROVE a claim's owner is dead --
# only that it has been quiet for a while -- so a genuinely live submitter that is merely SLOW (a
# big side-file transfer, not a crash) could be reclaimed out from under it if its own placement
# legitimately took longer than -OrphanMetaGraceSec. Round 10 tried to fix this by making the grace
# period an explicit caller-declared promise instead of a guessed constant; both round-10 reviewers
# showed that framing still has no answer for what the DISPLACED owner does when it resumes: it
# never re-checks its own nonce, so it can go on to roll back or overwrite the NEW owner's live
# claim (round 10's own disclosed residual, sol/fable BLOCKER+MAJOR at round 10). Round 11 removes
# age-based reclamation ENTIRELY rather than narrowing it further:
#   - a claim, once made, is held until the submission that made it either finishes (rolling its
#     own claim back on failure, or handing off to the agent on success) or an OPERATOR removes it
#     by hand;
#   - a second submission for the SAME JobId while a claim exists is refused outright --
#     "UMRUN_JOBID_IN_USE ... choose a new -JobId" -- regardless of the claim's age;
#   - production JobIds carry a fresh random component on every submission (see
#     attr3-footage-stage.ps1), so an orphaned claim from a hard-killed submission blocks nothing
#     real there -- the next attempt simply mints a new id, exactly like the job-id and result-id
#     pre-checks above already assume. The one caller who deliberately reuses a FIXED JobId across
#     retries (the manual ATTR3-FIXTURE-REHEARSAL-1 operator workflow, docs/playback-attr-3-
#     cuda.md) now gets an explicit, honest refusal instead of a guessed timeout, and picks a new
#     -JobId to retry -- or, being a human who can inspect the share, removes the stale
#     inbox\<id>.meta.json by hand first if truly certain the earlier attempt is dead, at which
#     point the retry's own claim proceeds exactly as it would for a brand-new id. This is the
#     round-10 brief's own "or never reclaim" alternative, now taken in full rather than narrowed.
#   - the rollback below (a submission's OWN claim, removed on ITS OWN later failure) now checks
#     the nonce before deleting: since no code path ever reclaims another submission's claim
#     automatically any more, the only way $metaFinal could hold a DIFFERENT submission's claim by
#     the time this one's rollback runs is an operator manually clearing this submission's stuck
#     claim by hand and resubmitting the same id WHILE this submission was merely slow, not dead --
#     precisely the round-10 residual, still possible via manual override, now guarded against
#     directly instead of via an age heuristic. See the rollback's own comment for the window this
#     compare-then-delete leaves open and why it is safe.
#
# Because the claim now happens BEFORE any side-file/job work, the round-8 fix that re-checked
# metaFinal/final immediately before writing metadata (to narrow a window in which a second,
# no-budget submitter could finish its ENTIRE flow while the first was still mid-transfer) is no
# longer needed as a separate check: that race required a submitter to be able to complete
# everything before ANOTHER submitter had even published its own metadata, which claim-first makes
# impossible by construction -- every submitter's first write is its claim, so the atomic rename
# on inbox\<id>.meta.json is itself the only tiebreaker that can ever matter, for every caller,
# with or without a budget, not a narrower recheck bolted on for the budgeted path alone.

Set-StrictMode -Version Latest

$script:AllowedSideFileExtensions = @('.zip', '.json', '.exe', '.dll', '.txt', '.csv')
$script:DeviceNames = @('CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9')
# The clip fixtures, by STEM. sol, PR #137 r2 BLOCKER: "tracked under tests/fixtures/clips" admits
# every tracked file there, including that directory's README -- and a discovery helper that picked
# the smallest tracked file then proved the bypass rather than the feature. The same two stems the
# attribution generator accepts as -ClipId are the admissible set here, and no extension is named.
$script:TrackedFixtureClipStems = @('tiny_dual_iso', 'large_dual_iso')
# ATTR3-ADMIT-CONTENT-PIN-1 round 2d: the tokens Get-UmRunFixtureAdmission's content-pin check
# throws when it could not even DETERMINE admissibility (git or the bachelor module missing, or
# the single content-pin read could not be bound) -- as opposed to a definite refusal (untracked,
# working tree dirty, foreign repo). See that function's .Indeterminate.
# round 2e: ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE added -- `git rev-parse HEAD:<path>` failing for
# any reason OTHER than git's own "never committed" text (a corrupted object store, an I/O error,
# an unexpected git-version message) is an operational failure, not a content verdict, and must
# not be folded into the definite ATTR3_FIXTURE_NOT_COMMITTED refusal it used to share a token with.
#
# round 2g (sol/fable MAJOR): naming this honestly -- it is a TOKEN ALLOWLIST, not an exhaustive
# classification, and it is only as complete as the closed, documented set of tokens
# Test-UmRunFixtureContentPin / Assert-AttrCudaFixtureCommittedBytes / Get-AttrCudaGitBlobHash-
# FromBytes actually throw (see the catch below, and that function's own docstring for the full
# token list). A throw whose first token is NOT in this list is folded into the definite-refusal
# branch by omission, not by evidence about the fixture's bytes -- this round found and fixed one
# such gap (the repository-discovery `git rev-parse --show-toplevel` call discarding stderr and
# always throwing the definite ATTR3_FIXTURE_NOT_IN_A_REPO token; see AttrCudaArtifacts.psm1).
# Two narrower escape hatches remain, both named and left OPEN in that function's docstring rather
# than fixed here: an unanchored "invalid object name" stderr match that could in principle
# misclassify a corrupted-ref failure, and a large-fixture OOM that surfaces as a raw, untokened
# exception instead of ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE.
$script:UmRunIndeterminateAdmissionTokens = @('UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE', 'ATTR3_FIXTURE_GIT_UNAVAILABLE', 'ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE', 'ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE', 'UMRUN_FIXTURE_ADMISSION_PATH_RESOLUTION_UNAVAILABLE')
# tools/profiling/bachelor/AttrCudaArtifacts.psm1's Assert-AttrCudaFixtureCommittedBytes -- the twin
# check ATTR3-FIXTURE-STAGE-1 already wrote for this identical defect class -- is reused by
# Test-UmRunFixtureContentPin below, imported ON DEMAND from this path so a host that never ships
# the bachelor module still runs every other um-run side-file rule unchanged.
$script:AttrCudaArtifactsModulePath = Join-Path (Join-Path $PSScriptRoot 'bachelor') 'AttrCudaArtifacts.psm1'

function Test-UmRunTrackedFixtureSource {
    <#
    .SYNOPSIS
    True when a side-file's SOURCE is a repository fixture clip, i.e. it sits in a
    `tests/fixtures/clips` directory.
    .DESCRIPTION
    ATTR3-FIXTURE-REHEARSAL-1 has to stage a tracked fixture clip onto a measurement host. Its media
    extension is deliberately NOT added to $AllowedSideFileExtensions: the board's NA-4 gate refuses a
    bare media-extension token in a tools file without an authorization, and composing that token from
    pieces to satisfy the gate's text scan would be routing around a guard rather than meeting it.
    The admissible property is not the extension anyway -- it is that the bytes are a TRACKED FIXTURE
    in THIS repository, which is exactly what NA-4 itself admits. So that is what this tests.

    sol, PR #137 r1 BLOCKER: a lexical segment match admitted any lookalike `tests\fixtures\clips`
    tree anywhere on the machine, including a UNC share and a path THROUGH a junction. Admission is
    therefore anchored to the repository this module ships in, and compared on REAL paths: the
    source's directory, with links resolved, must be the resolved `<repoRoot>\tests\fixtures\clips`
    itself, and the file must be tracked there by git.

    ATTR3-ADMIT-CONTENT-PIN-1 (fable key on PR #137): being tracked under that name says nothing
    about whether the WORKING-TREE bytes at that path are still the committed ones -- a working
    copy overwritten with foreign bytes was still admitted and staged onto the measurement host
    under the fixture's name. Test-UmRunFixtureContentPin closes that: admission now requires the
    bytes on disk to equal the committed blob at HEAD, and fails closed (git missing, not a repo,
    untracked/absent from HEAD, or a bytes mismatch) rather than admitting on name alone.

    A thin boolean wrapper over Get-UmRunFixtureAdmission, which also returns the verified
    committed hash a caller needs to bind later reads to (sol, PR #140 r2 MAJOR) -- kept separate
    so this predicate's exported return contract (a plain boolean, asserted by name in existing
    tests) never changes.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        # The repository this module ships in: tools\profiling\UmRunDrop.psm1 -> two levels up.
        [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    )

    (Get-UmRunFixtureAdmission -SourcePath $SourcePath -RepoRoot $RepoRoot).IsFixture
}

function Get-UmRunFixtureAdmission {
    <#
    .SYNOPSIS
    The core fixture-admission check: whether $SourcePath is a content-pinned tracked fixture
    clip, and if so a SHA256 of the bytes admission verified.
    .DESCRIPTION
    sol, PR #140 r2 MAJOR. Test-UmRunTrackedFixtureSource's boolean contract cannot carry a
    verified hash out to a caller, and re-deriving one with a second, independent read is exactly
    the check/use race this exists to close: a caller that re-hashes the source AFTER admission
    has already let go of the file is hashing whatever is there NOW, not what admission verified.
    This runs the identical directory/stem/content-pin checks Test-UmRunTrackedFixtureSource used
    to run inline, once, and hands back both the verdict and a hash so a caller can bind them
    together (see Invoke-UmRunDrop's use of -FixtureContentSha256).

    ContentSha256 is the SHA256 Test-UmRunFixtureContentPin hands back on its returned object,
    itself derived from the SAME single read Assert-AttrCudaFixtureCommittedBytes takes to compare
    the working-tree bytes against the committed blob (round 2d, sol BLOCKER / fable MAJOR) -- NOT
    a second, independent Get-FileHash of this mutable path taken afterward. That second read used
    to be exactly the internal check/use gap this whole card exists to close: bytes exchanged
    between the content-pin comparison and a later independent hash became the trusted pin, and
    every downstream binding this module added (the drop race check below, the share round-trip,
    the generator bake) then faithfully agreed on the swapped bytes, because nothing after
    admission ever saw the original ones to compare against.

    Reason / Indeterminate: round 2d (sol MAJOR / fable MAJOR). IsFixture=$false collapses THREE
    different outcomes -- "definitely not this kind of file" (wrong directory, unknown stem,
    untracked, working tree dirty, foreign repo), "could not determine" (git or the bachelor
    module unavailable, or the single content-pin read could not be bound), and previously nothing
    distinguished either from the other, so a caller reported one generic token and the real
    reason survived only under -Verbose. Reason carries that message out always; Indeterminate is
    $true only for the "could not determine" outcomes, so a caller can tell "refused" from
    "unknown" instead of folding the third state into either of the other two.

    sol, PR #140 r2c: exported (not just called internally by Assert-UmRunSideFileName) so
    tools/profiling/bachelor/attr3-stage-fixture-job.ps1's generator-time bake of $fixtureSha
    can reuse this exact verified-hash pairing instead of re-deriving an independent one with
    its own second Get-FileHash call -- see that generator's own binding of its later read back
    to .ContentSha256.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    )

    $result = [pscustomobject]@{ IsFixture = $false; ContentSha256 = $null; Reason = $null; Indeterminate = $false }

    $fixturesDir = Join-Path (Join-Path (Join-Path $RepoRoot 'tests') 'fixtures') 'clips'
    if (-not (Test-Path -LiteralPath $fixturesDir -PathType Container)) {
        $result.Reason = "UMRUN_FIXTURE_ADMISSION_NO_FIXTURES_DIR '$fixturesDir' does not exist under '$RepoRoot'"
        return $result
    }
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        $result.Reason = "UMRUN_FIXTURE_ADMISSION_SOURCE_MISSING '$SourcePath' does not exist"
        return $result
    }

    # Real paths, so a junction/symlink cannot present an outside file as a fixture.
    #
    # round 2e (sol/fable MAJOR): Get-Item/ResolveLinkTarget below can throw a RAW, untyped
    # exception (e.g. the file vanishing in the narrow TOCTOU between the Test-Path existence
    # check just above and this call) that used to propagate straight out of this function
    # uncaught -- escaping the admit/refuse-with-reason/could-not-determine three-way split
    # entirely rather than landing in any of the three. Wrapped so every path through this
    # function returns the $result object.
    try {
        $resolvedDir = Resolve-UmRunRealDirectory -Path $fixturesDir
        $sourceItem = Get-Item -LiteralPath $SourcePath -Force
        $link = $sourceItem.ResolveLinkTarget($true)
        if ($null -ne $link) { $sourceItem = $link }
        $sourceDir = Resolve-UmRunRealDirectory -Path ([IO.Path]::GetDirectoryName($sourceItem.FullName))
    } catch {
        $result.Reason = "UMRUN_FIXTURE_ADMISSION_PATH_RESOLUTION_UNAVAILABLE could not resolve '$SourcePath': $($_.Exception.Message)"
        $result.Indeterminate = $true
        return $result
    }
    if (-not [string]::Equals($sourceDir, $resolvedDir, [StringComparison]::OrdinalIgnoreCase)) {
        $result.Reason = "UMRUN_FIXTURE_ADMISSION_WRONG_DIRECTORY '$($sourceItem.FullName)' does not resolve inside '$resolvedDir'"
        return $result
    }

    # A tracked FIXTURE, not merely a tracked file in that directory (sol r2: README.md is tracked there).
    $name = [IO.Path]::GetFileName($sourceItem.FullName)
    $stem = [IO.Path]::GetFileNameWithoutExtension($name)
    if ($script:TrackedFixtureClipStems -cnotcontains $stem) {
        $result.Reason = "UMRUN_FIXTURE_ADMISSION_UNKNOWN_STEM '$stem' is not an admissible fixture stem"
        return $result
    }

    # Tracked AND content-pinned: git rev-parse HEAD:<path> (inside Test-UmRunFixtureContentPin)
    # fails the same way for "never committed" and "committed but the working copy has drifted", so
    # a single fail-closed call covers both -- see that function for the distinct thrown reasons.
    try {
        $pin = Test-UmRunFixtureContentPin -Path $sourceItem.FullName -RepoRoot $RepoRoot
    } catch {
        $message = $_.Exception.Message
        Write-Verbose $message
        $result.Reason = $message
        $firstToken = ($message -split '\s+', 2)[0]
        $result.Indeterminate = $script:UmRunIndeterminateAdmissionTokens -contains $firstToken
        return $result
    }
    $result.IsFixture = $true
    $result.ContentSha256 = $pin.Sha256
    return $result
}

function Test-UmRunFixtureContentPin {
    <#
    .SYNOPSIS
    Throw unless a fixture's working-tree bytes are exactly its committed blob at HEAD; return the
    verified git blob hash AND a SHA256 pin of the SAME bytes.
    .DESCRIPTION
    ATTR3-ADMIT-CONTENT-PIN-1. Reuses tools/profiling/bachelor/AttrCudaArtifacts.psm1's
    Assert-AttrCudaFixtureCommittedBytes -- the twin check ATTR3-FIXTURE-STAGE-1 already wrote for
    the identical defect class, comparing the working-tree file's git blob hash to
    `git rev-parse HEAD:<repo-relative path>` -- rather than re-deriving the same comparison here
    and letting the two drift. The bachelor module is imported ON DEMAND, and only if present, so a
    host that never ships it still runs every OTHER um-run side-file rule unchanged: it simply
    cannot admit a tracked fixture by content, and this fails closed instead of admitting on name
    alone. Deliberately NO -Force: a caller (attr3-stage-fixture-job.ps1) already imports this same
    module itself before calling Test-UmRunTrackedFixtureSource, and Import-Module -Force first
    REMOVES any existing same-named module from the whole session -- unbinding it from that
    caller's own scope, not just this one -- before rebinding it here alone. Plain Import-Module is
    a no-op when the module is already loaded, so the caller's binding survives untouched.

    sol, PR #140 r2 BLOCKER: -RepoRoot is forwarded to Assert-AttrCudaFixtureCommittedBytes so a
    nested repository under the fixture's own directory cannot authorize its bytes -- see that
    function's docstring.

    ROUND 2d (sol BLOCKER / fable MAJOR): Sha256 on the returned object comes from
    Assert-AttrCudaFixtureCommittedBytes's -Sha256Pin out-parameter, which is derived from the
    IDENTICAL byte buffer that function's own git-blob comparison used -- one held, write-denying
    file handle, read once. The previous shape called this function for its verdict alone (its
    return value discarded with [void] by Get-UmRunFixtureAdmission) and then took a completely
    separate Get-FileHash of the same mutable path for the pin; bytes exchanged in that internal
    gap became the trusted pin, undetected, because nothing else in admission ever compared
    against the original bytes.

    Throws UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE when the bachelor module is not present, or one of
    Assert-AttrCudaFixtureCommittedBytes's own distinct tokens: ATTR3_FIXTURE_GIT_UNAVAILABLE (git
    missing), ATTR3_FIXTURE_NOT_IN_A_REPO (not inside a repo), ATTR3_FIXTURE_NOT_COMMITTED
    (untracked or absent from HEAD), ATTR3_FIXTURE_WORKING_TREE_DIRTY (bytes differ),
    ATTR3_FIXTURE_FOREIGN_REPO (tracked by a repository other than the trusted -RepoRoot), or
    ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE (the single read-locked handle could not be opened or
    fully read) -- an honest refusal, never a hash it cannot stand behind.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    )

    if (-not (Test-Path -LiteralPath $script:AttrCudaArtifactsModulePath -PathType Leaf)) {
        throw "UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE $($script:AttrCudaArtifactsModulePath) is not present; cannot verify '$Path' against its committed blob"
    }
    try {
        Import-Module $script:AttrCudaArtifactsModulePath -ErrorAction Stop
    } catch {
        # round 2e (sol/fable MAJOR): a PRESENT but unloadable module (corrupted file, a syntax
        # error, a permission denial) used to propagate PowerShell's own raw exception message,
        # whose first whitespace-delimited token is never one of $UmRunIndeterminateAdmissionTokens
        # -- so Get-UmRunFixtureAdmission's catch classified this exact "could not determine"
        # scenario as a DEFINITE refusal (Indeterminate=$false) purely because the message shape
        # did not match, not because anything about the fixture's bytes was actually refused.
        throw "UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE $($script:AttrCudaArtifactsModulePath) is present but failed to load; cannot verify '$Path' against its committed blob: $($_.Exception.Message)"
    }
    $sha256Ref = [ref]$null
    $gitBlobHash = Assert-AttrCudaFixtureCommittedBytes -Path $Path -RepoRoot $RepoRoot -Sha256Pin $sha256Ref
    [pscustomobject]@{ GitBlobSha1 = $gitBlobHash; Sha256 = $sha256Ref.Value }
}

function Resolve-UmRunRealDirectory {
    <# Full path of a directory with every link in the chain resolved; '' when it does not exist. #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return '' }
    $link = $item.ResolveLinkTarget($true)
    if ($null -ne $link) { $item = $link }
    return $item.FullName.TrimEnd('\')
}

function Get-UmRunShareNowUtc {
    <#
    .SYNOPSIS
    "Now", stamped by the SAME clock domain as a file already on the given share directory --
    never the caller's own clock.
    .DESCRIPTION
    ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 6 (fable minor, orphan-age check): comparing two
    timestamps that both come from a filesystem's own LastWriteTimeUtc needs no assumption that
    the caller's own clock agrees with that filesystem's -- comparing one filesystem timestamp
    against the caller's own Get-Date does. Round 10 factors this out of Invoke-UmRunDrop's own
    orphan-age check so um-run.ps1's liveness wait (comparing heartbeat.txt's own LastWriteTimeUtc
    against elapsed time) can reuse the exact same probe, on the exact same share, instead of a
    second untested copy of it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        # Test-only: invoked with the probe file's path immediately after it is written, BEFORE its
        # LastWriteTimeUtc is read -- lets a test stamp the probe file with a timestamp that cannot
        # arise from Get-Date, proving the returned value really was read back off the share and
        # not silently substituted with the caller's own clock (round 8 narrowing: two clocks that
        # happen to coincide in a test environment cannot otherwise be told apart by final state).
        [scriptblock]$TestHookAfterProbeWritten = $null,
        # Test-only: invoked with the computed value immediately after it is read, still inside the
        # probe's own try block -- proves this function actually executed and returned a genuine
        # reading, not e.g. a caller that silently reverted to Get-Date at the call site instead.
        [scriptblock]$TestHookAfterProbeRead = $null
    )

    $probe = Join-Path $Directory ".umrun-clock-probe.$([guid]::NewGuid().ToString('N'))"
    try {
        Set-Content -LiteralPath $probe -Value '' -Encoding ascii -NoNewline
        if ($TestHookAfterProbeWritten) { & $TestHookAfterProbeWritten $probe }
        $now = (Get-Item -LiteralPath $probe -Force).LastWriteTimeUtc
        if ($TestHookAfterProbeRead) { & $TestHookAfterProbeRead $now }
        return $now
    } finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
}

function Get-UmRunHeartbeatJobTag {
    <#
    .SYNOPSIS
    The job=<id> tag from a heartbeat line, or $null if the line has no such tag. A pure text-shape
    read with no freshness/mismatch judgement of its own -- see Get-UmRunAgentLiveness, which calls
    this TWICE to guard against a torn read.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$HeartbeatPath)

    $line = Get-Content -LiteralPath $HeartbeatPath -Raw -ErrorAction SilentlyContinue
    if ($line -and $line -match '(?:^|\s)job=(\S+)\s*$') { return $Matches[1] }
    return $null
}

function Get-UmRunAgentLiveness {
    <#
    .SYNOPSIS
    Is the agent still proving liveness on JobId, per heartbeat.txt?
    .DESCRIPTION
    ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10: um-run.ps1's claimed-phase wait trusts
    heartbeat.txt fresh by the SHARE's own clock (Get-UmRunShareNowUtc -- the same round-6 probe
    this module's own former orphan-age check used) rather than a constant nobody could derive a
    real bound for. The agent tags heartbeat.txt with " job=<id>" (normal execution) or
    " adopt job=<id>" (post-restart adoption) every wait-slice while a job is genuinely running --
    the agent's own wait-slice is hard-capped at 5s regardless of its own -PollSeconds -- so a tag
    naming a DIFFERENT job proves the agent has moved off this one without ever producing a
    receipt, which is liveness-lost for OUR purposes even if the agent itself is fine. A heartbeat
    line with no job= tag at all (a plain between-jobs heartbeat, or one read mid-write) is not
    treated as a mismatch -- only an EXPLICIT different job id is.

    Round 11 (sol MAJOR): a torn read of a real, complete "job=<id>" tag -- the agent's own
    Write-AsciiFileWithRetry is not atomic across processes -- can look like a complete tag for a
    SHORTER, different id (e.g. "job=dem" read mid-write of "job=demo"), which the anchored regex
    below matches just as readily as a genuine one, producing a false mismatch on a single unlucky
    read. This module (moved here from um-run.ps1 at round 11 so it can be driven directly, the
    same way every other function here already is) now requires TWO reads, a short delay apart, to
    agree on the SAME different id before reporting a mismatch: a transient tear essentially never
    repeats identically on the very next read (the writer has since finished, or is mid a DIFFERENT
    tear), while a genuinely different, stable job tag reads the same both times.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$HeartbeatPath,
        [Parameter(Mandatory = $true)][string]$Inbox,
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)][int]$MaxHeartbeatAgeSec,
        [int]$TornReadRetryDelayMs = 75,
        # Test-only: invoked with the tentative first-read job tag (or $null) immediately after the
        # first heartbeat read, before the confirmation delay and second read -- lets a test rewrite
        # heartbeat.txt in between, proving the SECOND read is what actually decides a mismatch, not
        # the first alone.
        [scriptblock]$TestHookAfterFirstHeartbeatRead = $null,
        [scriptblock]$TestHookAfterProbeWritten = $null,
        [scriptblock]$TestHookAfterProbeRead = $null
    )

    if (-not (Test-Path -LiteralPath $HeartbeatPath)) {
        return [pscustomobject]@{ Fresh = $false; AgeSec = $null; HeartbeatUtc = $null; JobMismatch = $false; OtherJobId = $null }
    }
    $shareNowUtc = Get-UmRunShareNowUtc -Directory $Inbox `
        -TestHookAfterProbeWritten $TestHookAfterProbeWritten `
        -TestHookAfterProbeRead $TestHookAfterProbeRead
    $heartbeatUtc = (Get-Item -LiteralPath $HeartbeatPath -Force).LastWriteTimeUtc
    $ageSec = ($shareNowUtc - $heartbeatUtc).TotalSeconds

    $jobMismatch = $false
    $otherJobId = $null
    $firstTag = Get-UmRunHeartbeatJobTag -HeartbeatPath $HeartbeatPath
    if ($TestHookAfterFirstHeartbeatRead) { & $TestHookAfterFirstHeartbeatRead $firstTag }
    if ($null -ne $firstTag -and $firstTag -ne $JobId) {
        Start-Sleep -Milliseconds $TornReadRetryDelayMs
        $secondTag = Get-UmRunHeartbeatJobTag -HeartbeatPath $HeartbeatPath
        if ($secondTag -eq $firstTag) {
            $jobMismatch = $true
            $otherJobId = $firstTag
        }
    }
    return [pscustomobject]@{
        Fresh        = (-not $jobMismatch) -and ($ageSec -le $MaxHeartbeatAgeSec)
        AgeSec       = $ageSec
        HeartbeatUtc = $heartbeatUtc
        JobMismatch  = $jobMismatch
        OtherJobId   = $otherJobId
    }
}

function Assert-UmRunSideFileName {
    <#
    .PARAMETER FixtureContentSha256
    Optional [ref]; when $SourcePath was admitted as a content-pinned tracked fixture, its
    .Value is set to a SHA256 (lowercase hex) of the bytes Get-UmRunFixtureAdmission verified,
    so a caller can bind a LATER read of the same bytes back to what admission actually saw
    (sol, PR #140 r2 MAJOR) instead of trusting that nothing changed in between.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Inbox,
        [string]$SourcePath = '',
        [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
        [ref]$FixtureContentSha256
    )

    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$') {
        throw "UMRUN_SIDEFILE_NAME_INVALID '$Name' is not a plain basename (letters, digits, '_', '-', single dots between parts)"
    }
    if ($Name -match '\.job\.' -or $Name -match '\.(sidepart|tmp|ps1|psm1|psd1|bat|cmd|vbs|js)$') {
        throw "UMRUN_SIDEFILE_NAME_INVALID '$Name' is job-shaped or executable-script-shaped"
    }
    $extension = [IO.Path]::GetExtension($Name).ToLowerInvariant()
    $admission = if ($SourcePath) { Get-UmRunFixtureAdmission -SourcePath $SourcePath -RepoRoot $RepoRoot } else { [pscustomobject]@{ IsFixture = $false; ContentSha256 = $null; Reason = $null; Indeterminate = $false } }
    $trackedFixture = $admission.IsFixture
    if (-not $trackedFixture -and $script:AllowedSideFileExtensions -notcontains $extension) {
        # round 2d (sol/fable MAJOR): the distinct admission-refusal reason used to survive only
        # under -Verbose; carried out here so a caller sees WHY without needing verbose logging.
        $reasonSuffix = if ($admission.Reason) { " ($($admission.Reason))" } else { '' }
        # round 2e (sol/fable MAJOR): a DEFINITE non-fixture (wrong directory, unknown stem,
        # untracked, dirty, foreign repo) and a COULD-NOT-DETERMINE outcome (git or the bachelor
        # module unavailable, the content-pin read unbindable) used to throw the identical token
        # here, distinguished only by prose buried in the parenthetical -- so a caller filtering on
        # the token alone (the common case; UMRUN_SIDEFILE_NAME_INVALID is this function's one
        # documented refusal token) could not tell "this fixture is bad" from "the environment
        # could not tell". A refusal is still the fail-closed action either way -- unpinned
        # admission is never safe -- but the outcome is now reported under its own token.
        if ($admission.Indeterminate) {
            throw "UMRUN_SIDEFILE_ADMISSION_INDETERMINATE '$Name' extension '$extension' is not in the allowlist and fixture admissibility could not be determined$reasonSuffix"
        }
        throw "UMRUN_SIDEFILE_NAME_INVALID '$Name' extension '$extension' is not in the allowlist and its source is not a tracked fixture clip$reasonSuffix"
    }
    $stem = $Name.Split('.')[0].ToUpperInvariant()
    if ($script:DeviceNames -contains $stem) {
        throw "UMRUN_SIDEFILE_NAME_INVALID '$Name' is a Windows device name"
    }
    $full = [IO.Path]::GetFullPath((Join-Path $Inbox $Name))
    if ([IO.Path]::GetFileName($full) -cne $Name -or
        -not [string]::Equals([IO.Path]::GetDirectoryName($full).TrimEnd('\'), [IO.Path]::GetFullPath($Inbox).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "UMRUN_SIDEFILE_NAME_INVALID '$Name' does not normalise to itself directly inside the inbox"
    }
    if ($null -ne $FixtureContentSha256) { $FixtureContentSha256.Value = $admission.ContentSha256 }
    return $full
}

function Invoke-UmRunDrop {
    <#
    .SYNOPSIS
    Claim a JobId, then place verified side-files, then the job, into an agent inbox. Returns the
    job id.
    .PARAMETER Copier
    Performs one copy: & $Copier <source> <destination>. Defaults to Copy-Item. Tests pass an
    observing or faulty copier; production never does.
    .PARAMETER PostAdmissionHook
    Test-only seam: invoked with the source path immediately after that side-file's admission
    check has returned and before its bytes are read again for placement -- the exact gap a
    swap would need to win the check/use race (sol, PR #140 r2 MAJOR). Defaults to a no-op;
    production never sets it.
    .PARAMETER PreRenameRaceHook
    Test-only seam (round 2e, sol BLOCKER): invoked with the share-side .sidepart's path after its
    round-trip hash has been verified and while this function's own deny-write handle on it is
    STILL OPEN, immediately before the rename that publishes it. A hook that attempts to write to
    that path here is exercising the exact gap the held handle exists to close, not a gap that is
    still open -- see Invoke-UmRunDrop's own comment at the file-open call. Defaults to a no-op;
    production never sets it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Inbox,
        [Parameter(Mandatory = $true)][string]$Outbox,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string]$JobId = '',
        [string[]]$SideFile = @(),
        [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1: the AGENT's per-job budget, written to
        # inbox\<id>.meta.json as part of the claim (round 10: every submission claims metadata,
        # with or without a budget -- 0 means the claim's own metadata omits `timeoutSec`, leaving
        # the agent on its own -JobTimeoutSec default (1800 s), the behaviour every caller had
        # before this whole feature existed. um-run's -TimeoutSec now reaches the agent as the
        # job's own budget for a caller that does state one.
        [int]$JobTimeoutSec = 0,
        [scriptblock]$Copier = { param($Source, $Destination) Copy-Item -LiteralPath $Source -Destination $Destination },
        [scriptblock]$PostAdmissionHook = { param($Source) },
        [scriptblock]$PreRenameRaceHook = { param($PartPath) },
        # Test-only: invoked with no arguments immediately before the job's own rename into view,
        # i.e. the last instant at which "is metadata already published?" is the real contract this
        # module owes the agent (metadata-before-VISIBILITY, not metadata-before-the-job's-own-
        # temporary-copy, which sol's round-4 review named as the wrong thing to have proved).
        [scriptblock]$TestHookBeforeJobVisible = $null,
        # Test-only: invoked with the metadata temp file's path immediately after it is written,
        # before the write-back verification -- lets a test corrupt it to prove that verification
        # actually rejects a torn write rather than merely being present and untested, OR (round 10)
        # plant a competing winner's metadata directly at $metaFinal to prove a losing concurrent
        # claim never overwrites it.
        [scriptblock]$TestHookAfterMetaTmpWritten = $null
    )

    if ($JobId -and $JobId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw "UMRUN_JOBID_INVALID '$JobId'" }
    if ($JobId -and $JobId.EndsWith('.')) { throw "UMRUN_JOBID_INVALID '$JobId' ends with a dot" }
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol minor 1): checked BEFORE anything that
    # touches the share -- an invalid budget must never be discovered only after paying for a
    # possibly multi-GB side-file transfer.
    if ($JobTimeoutSec -ne 0 -and ($JobTimeoutSec -lt 1 -or $JobTimeoutSec -gt 86400)) {
        throw "UMRUN_JOB_TIMEOUT_INVALID $JobTimeoutSec is outside the agent's accepted 1..86400 range"
    }
    $id = if ($JobId) { $JobId } else { "job_{0}_{1}" -f (Get-Date -Format 'yyyyMMdd_HHmmss'), ([guid]::NewGuid().ToString('N').Substring(0, 8)) }
    # Fast pre-checks, not the actual protection (the claim's own atomic rename below is): cheap,
    # so still worth failing fast on, but racy on their own (TOCTOU) -- a caller relies on the claim
    # for correctness, on these only for a quick, honest-looking refusal in the common case.
    if (Test-Path -LiteralPath (Join-Path $Outbox "$id.result.json")) {
        throw "UMRUN_JOBID_IN_USE outbox already holds $id.result.json"
    }
    $final = Join-Path $Inbox "$id.job.ps1"
    if (Test-Path -LiteralPath $final) { throw "UMRUN_JOBID_IN_USE inbox already holds $id.job.ps1" }
    if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) { throw "UMRUN_SCRIPT_MISSING $ScriptPath" }

    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol BLOCKER + fable MAJOR): no age-based
    # reclamation -- see this module's own header. Existing metadata, at ANY age, is refused
    # outright; only an operator manually removing inbox\<id>.meta.json (or this submission's own
    # nonce-checked rollback below, on ITS OWN later failure) ever clears a claim.
    $metaFinal = Join-Path $Inbox "$id.meta.json"
    if (Test-Path -LiteralPath $metaFinal) {
        throw "UMRUN_JOBID_IN_USE inbox already holds $id.meta.json; this JobId is already claimed (or was claimed by an earlier submission that never cleaned up) -- choose a new -JobId to retry, or remove inbox\$id.meta.json by hand if you are certain the earlier submission is dead"
    }

    $nonce = [guid]::NewGuid().ToString('N')

    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10 (the claim itself -- see module header for the
    # full rationale). Written through a nonce temp and renamed, never overwriting: of two racing
    # submitters for the SAME JobId, whichever's Move-Item lands first becomes the sole owner of
    # every side-file and job placement that follows; the other is refused here, before it has
    # touched a single side-file or job byte. `timeoutSec` is included only when a real budget was
    # requested -- omitted otherwise, which both the tracked and deployed agents already treat as
    # "fall back to my own default" (confirmed against the deployed agent; see summary.md).
    $metaTmp = Join-Path $Inbox "$id.$nonce.meta.tmp"
    $metaObj = [ordered]@{ jobId = $id; nonce = $nonce }
    if ($JobTimeoutSec -ne 0) { $metaObj['timeoutSec'] = $JobTimeoutSec }
    $metaJson = $metaObj | ConvertTo-Json -Compress
    try {
        Set-Content -LiteralPath $metaTmp -Value $metaJson -Encoding ascii -NoNewline
        if ($TestHookAfterMetaTmpWritten) { & $TestHookAfterMetaTmpWritten $metaTmp }
        # fable/sol minor 2: side-files are re-read from the share and hash-verified before their
        # rename; the metadata previously was not, so a torn write silently reverted the agent to
        # its own default -- the original bug, undetected. Re-read and compare bytes.
        $metaWrittenBack = Get-Content -LiteralPath $metaTmp -Raw -Encoding ascii
        if ($metaWrittenBack -ne $metaJson) {
            throw "UMRUN_JOB_METADATA_VERIFY_FAILED $id.meta.json did not round-trip to the share"
        }
        try {
            Move-Item -LiteralPath $metaTmp -Destination $metaFinal -ErrorAction Stop   # no -Force
        } catch {
            throw "UMRUN_JOBID_IN_USE inbox\$id.meta.json appeared concurrently; refusing to replace it"
        }
    } finally {
        if (Test-Path -LiteralPath $metaTmp) { Remove-Item -LiteralPath $metaTmp -Force -ErrorAction SilentlyContinue }
    }
    if ($JobTimeoutSec -ne 0) {
        Write-Output ("job metadata placed: {0}.meta.json timeoutSec={1}" -f $id, $JobTimeoutSec)
    } else {
        Write-Output ("job claimed: {0}.meta.json (no budget requested)" -f $id)
    }

    # From here on, this call OWNS the claim above -- any failure in the side-file loop or the
    # job's own placement must roll that claim back (round 2/4's original guarantee, now covering
    # the whole post-claim flow instead of only the metadata-then-job-rename tail): otherwise it
    # outlives the refused submission and bricks every later retry that reuses the same JobId
    # (UMRUN_JOBID_IN_USE at the top of this function) even though no job or result exists.
    try {
        foreach ($path in @($SideFile | ForEach-Object { $_ -split ';' } | Where-Object { $_ })) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "UMRUN_SIDEFILE_MISSING $path" }
            $sourceItem = Get-Item -LiteralPath $path -Force
            $name = $sourceItem.Name
            # The name must be the file's own name AND survive validation; a trailing-dot alias
            # passed as a path reports its canonical Name here, and the literal argument is
            # compared too.
            if ([IO.Path]::GetFileName($path) -cne $name) {
                throw "UMRUN_SIDEFILE_NAME_INVALID '$([IO.Path]::GetFileName($path))' is an alias of '$name'"
            }
            $fixtureHashRef = [ref]$null
            $destination = Assert-UmRunSideFileName -Name $name -Inbox $Inbox -SourcePath $sourceItem.FullName `
                -RepoRoot $RepoRoot -FixtureContentSha256 $fixtureHashRef
            $pinnedSha = $fixtureHashRef.Value

            # Test-only seam (no-op in production): fires in the exact gap a swap would need to win
            # the check/use race admission just closed.
            & $PostAdmissionHook $sourceItem.FullName
            $localSha = (Get-FileHash -LiteralPath $sourceItem.FullName -Algorithm SHA256).Hash
            if ($pinnedSha -and $localSha.ToLowerInvariant() -ne $pinnedSha) {
                # sol, PR #140 r2 MAJOR: admission verified $pinnedSha; a read taken any time after
                # admission returned is bound back to that value here, so bytes swapped in the gap
                # between admission and this read can never reach the share as if they were the
                # blob that passed.
                throw "UMRUN_FIXTURE_CONTENT_PIN_RACE $name changed between admission and placement (admission verified $pinnedSha, now $($localSha.ToLowerInvariant()))"
            }

            if (Test-Path -LiteralPath $destination) {
                $existing = Get-Item -LiteralPath $destination -Force
                if (-not $existing.PSIsContainer -and (($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) -and
                    (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -eq $localSha) {
                    Write-Output "side-file already present with matching sha256: $name"
                    continue
                }
                throw "UMRUN_SIDEFILE_CONFLICT inbox\$name already exists with DIFFERENT content or is not a plain file"
            }

            $part = Join-Path $Inbox "$name.$nonce.sidepart"
            try {
                & $Copier $sourceItem.FullName $part
                # ATTR3-ADMIT-CONTENT-PIN-1 round 2e (sol BLOCKER): the share round-trip hash and the
                # rename used to be TWO separate operations on the mutable $part path -- Get-FileHash
                # opened, read and closed its own handle, then Move-Item opened a completely different
                # one, leaving a gap for the verified bytes to be swapped before the rename picked them
                # up. One handle, opened deny-write (FileShare.Delete only, so the rename below can
                # still succeed while this handle stays open), spans the hash AND the rename: nothing
                # else can WRITE $part for as long as it is held.
                # round 2g (sol/fable MAJOR): naming the mechanism's own escape hatch rather than
                # claiming full closure -- FileShare.Delete grants exactly what its name says: another
                # process CAN delete $part's directory entry (and a third can then create a brand-new
                # file at the identical pathname) while this handle stays open, because delete/rename
                # rights are governed by the Delete share flag, not the Read/Write flags this handle
                # denies. The Move-Item below then resolves $part by PATHNAME, not by this handle's
                # identity, so it would publish whatever now occupies that path, not necessarily the
                # bytes just hashed. This row is recorded OPEN in the check/use window table, not
                # CLOSED, for exactly this reason; the write-attempt test just below proves only the
                # narrower "in-place write is denied" property, not pathname-identity across the rename.
                $partStream = [IO.File]::Open($part, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::Read -bor [IO.FileShare]::Delete))
                try {
                    $sha256 = [Security.Cryptography.SHA256]::Create()
                    try {
                        $remoteSha = [BitConverter]::ToString($sha256.ComputeHash($partStream)) -replace '-', ''
                    } finally {
                        $sha256.Dispose()
                    }
                    if ($remoteSha -ne $localSha) {
                        throw "UMRUN_SIDEFILE_VERIFY_FAILED $name did not round-trip to the share (local $localSha, share $remoteSha)"
                    }
                    # Test-only seam (no-op in production): fires while $partStream's deny-write handle
                    # is still held, so a hook that tries to write $part here is racing the closed
                    # window, not an open one.
                    & $PreRenameRaceHook $part
                    try {
                        Move-Item -LiteralPath $part -Destination $destination -ErrorAction Stop   # no -Force: never overwrite
                    } catch {
                        if ((Test-Path -LiteralPath $destination) -and (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -eq $localSha) {
                            Write-Output "side-file placed concurrently with matching sha256: $name"
                        } else {
                            throw "UMRUN_SIDEFILE_CONFLICT inbox\$name appeared concurrently with different content"
                        }
                    }
                } finally {
                    $partStream.Dispose()
                }
            } finally {
                if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue }
            }
            Write-Output ("side-file placed: {0} sha256={1}" -f $name, $localSha.ToLowerInvariant())
        }

        # The job's own bytes are copied to their nonce temp, verified never to have been
        # requested with a budget this call already rejected, then renamed into view -- the agent
        # may claim it the instant it appears, and by now this call's own metadata already
        # governs it (claimed before a single side-file or job byte was copied).
        $jobLocalSha = (Get-FileHash -LiteralPath $ScriptPath -Algorithm SHA256).Hash
        $tmp = Join-Path $Inbox "$id.$nonce.job.tmp"
        try {
            try {
                & $Copier $ScriptPath $tmp
            } catch {
                if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
                throw
            }
            # ATTR3-ADMIT-CONTENT-PIN-1 round 2g (fable MAJOR): the job's own temporary copy used to be
            # renamed with NO verification at all -- contradicting this module's own header invariant
            # (above) that a share-side temporary is re-read and verified before its rename, which the
            # side-file path above already does. Same mechanism, applied here for the first time: one
            # handle, opened deny-write (FileShare.Delete only, so the rename below can still succeed
            # while the handle stays open), spans the round-trip hash and the rename. This closes "no
            # verification at all"; it does NOT close Row C's own residual (recorded OPEN, not CLOSED,
            # in the check/use window table) -- FileShare.Delete still permits another process to
            # delete-and-recreate different bytes at $tmp's PATHNAME while this handle is held, and the
            # Move-Item below resolves that pathname, not this handle's identity, at rename time.
            $tmpStream = [IO.File]::Open($tmp, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::Read -bor [IO.FileShare]::Delete))
            try {
                $sha256 = [Security.Cryptography.SHA256]::Create()
                try {
                    $tmpRemoteSha = [BitConverter]::ToString($sha256.ComputeHash($tmpStream)) -replace '-', ''
                } finally {
                    $sha256.Dispose()
                }
                if ($tmpRemoteSha -ne $jobLocalSha) {
                    throw "UMRUN_JOB_VERIFY_FAILED $id.job.ps1 did not round-trip to the share (local $jobLocalSha, share $tmpRemoteSha)"
                }
                if ($TestHookBeforeJobVisible) { & $TestHookBeforeJobVisible }
                try {
                    Move-Item -LiteralPath $tmp -Destination $final -ErrorAction Stop   # no -Force: a job is never replaced
                } catch {
                    throw "UMRUN_JOBID_IN_USE inbox\$id.job.ps1 appeared concurrently; refusing to replace it"
                }
            } finally {
                $tmpStream.Dispose()
            }
        } finally {
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        }
    } catch {
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 6 (sol minor): a rollback deletion failure here
        # used to be silently swallowed (SilentlyContinue), so metadata from a refused submission
        # could outlive it, with no indication why. The ORIGINAL failure is still why this
        # submission failed, so it is captured before attempting cleanup and folded into whatever
        # is thrown, rather than replaced by a cleanup-only error.
        #
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol BLOCKER + fable MAJOR, round 10's own
        # disclosed residual): this used to delete $metaFinal unconditionally -- correct only
        # because round 10 could otherwise displace THIS submission's own live claim out from under
        # it, making "whatever currently occupies metaFinal" and "this submission's own claim"
        # provably the same file. Round 11 removes that displacement entirely (see the module
        # header), but an OPERATOR can still manually clear a stuck-looking claim and resubmit the
        # same JobId by hand while the original submission was merely slow, not dead -- so this
        # reads $metaFinal back and deletes it ONLY if its nonce still matches this call's own,
        # never blindly. Between that read and the Remove-Item, an operator could in principle swap
        # the file again, but the read already proved OUR claim was still there at that instant --
        # the same single-rename-width race every other TOCTOU pre-check in this module already
        # accepts (see the fast pre-checks at the top of this function), and the failure mode of
        # losing that narrow race is a claim left behind for the operator to notice and clear by
        # hand, never silent data loss for whoever now owns it.
        $originalError = $_
        if (Test-Path -LiteralPath $metaFinal) {
            # Only a SUCCESSFUL read that proves a DIFFERENT nonce blocks the delete -- a read that
            # fails outright (e.g. the exact sharing violation the rollback's own Remove-Item is
            # about to hit too) proves nothing about ownership either way, so it falls through to
            # the plain removal attempt below and surfaces THAT failure, unchanged from before this
            # nonce check existed.
            $ownsClaim = $true
            $readSucceeded = $false
            try {
                $currentMeta = (Get-Content -LiteralPath $metaFinal -Raw -Encoding ascii) | ConvertFrom-Json
                $readSucceeded = $true
                $ownsClaim = ($null -ne $currentMeta) -and ($currentMeta.nonce -eq $nonce)
            } catch {
                $readSucceeded = $false
            }
            if ($readSucceeded -and -not $ownsClaim) {
                throw "$($originalError.Exception.Message) -- additionally, inbox\$id.meta.json is no longer this submission's own claim (nonce mismatch) and was left untouched, not removed during rollback"
            }
            try {
                Remove-Item -LiteralPath $metaFinal -Force -ErrorAction Stop
            } catch {
                throw "$($originalError.Exception.Message) -- additionally, inbox\$id.meta.json could not be removed during rollback and may outlive this refused submission"
            }
        }
        throw
    }
    Write-Output "submitted $id -> $final"
    Write-Output "UMRUN_JOBID=$id"
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11: the caller needs its OWN claim's nonce back --
    # never guessed, never re-derived -- to clean up its own metadata later without a blind delete
    # (um-run.ps1's new RETRACTED path, item 2). Emitted the same way UMRUN_JOBID= already is, and
    # only ever reached on the same full-success path.
    Write-Output "UMRUN_NONCE=$nonce"
}

Export-ModuleMember -Function Assert-UmRunSideFileName, Test-UmRunTrackedFixtureSource, Get-UmRunFixtureAdmission, Resolve-UmRunRealDirectory, Get-UmRunShareNowUtc, Get-UmRunHeartbeatJobTag, Get-UmRunAgentLiveness, Invoke-UmRunDrop
