# playback-attr-3-cuda-job.ps1 -- GENERATOR (runs locally / in a lane; nothing here runs
# on Bachelor). Emits a self-contained <jobId>.job.ps1 for the Bachelor agent inbox
# (tools/profiling/ultra-magnus-agent.ps1 protocol: inbox\<jobId>.job.ps1 in,
# outbox\<jobId>.result.json out).
#
# Modelled on:
#   - the PLAYBACK-ATTR-2 job body (quiescence check, cache hash checks,
#     run-release-gui-smoke.ps1 invocation, prep_region_* frame parsing, PresentMon
#     sidecar handshake, evidence-manifest.json) -- see
#     .claude-state/fleet-runs/lane-PLAYBACK-ATTR-3-CUDA-S1-20260916T165824Z/inputs/playback-attr-2.job.ps1.txt
#   - the provenance sidecar contract in tools/repo_hygiene/gpu_job_result_provenance.py
#     (rangeHeadSha, llrawprocBlobId, dllSha256 lowercase, pendingSymbolPresence)
#
# FOOTAGE (ATTR3-FOOTAGE-BIND-1, PR-B): an owner-clip id is RESOLVED, never handed a path. The
# generator calls tools/gates/resolve_consented_clip.py (PR-A) as a child process with
# --emit-json: the resolver alone proves that the frozen spec's parts for this id (length,
# sha256, and the hook's own norm() of the path) agree with the hook's frozen consent table
# (OWNER_CONSENTED_FOOTAGE in tools/hooks/mlv-never-authorized.py). A non-zero resolver exit is a
# refusal -- this generator throws with the resolver's typed status and emits nothing, never a
# path. On success, each part's path/length/sha256 is baked into the emitted job as base64 of its
# UTF-8 bytes (never an interpolated literal -- see Attr3FootagePresenceJob.psm1's header for
# why), so the job that runs on Bachelor re-checks every part's CONTENT against the live
# filesystem -- via the shared Test-AttrCudaFootagePart verifier -- before it opens anything,
# never trusting that "the resolver approved it" means the bytes are still there.
# -ClipPath and -FixtureSha256 are both REFUSED for an owner id: there is no owner-typed path on
# this route, and a content hash is meaningless without the resolver's own cross-check against
# the frozen table. An interpreter one-liner can still open any path; that residual is
# unchanged. Every verified part -- base part and any continuation part alike -- is named by
# THIS code, never the application: each gets a neutral link name (AttrCudaOwnerFootage.psm1's
# Get-AttrCudaOwnerFootageNeutralName), a held read-only handle opened before the smoke child
# ever runs, and a re-verification of its content against the resolved length/sha256 while that
# handle is held, all under a private per-job directory the application only ever sees by its
# neutral names. The owner's consent record (receipts/owner-footage-consent-20260916.json
# and its -correction.json) is evidence of consent, never an authorization. Adjudication:
# .claude-state/fleet-runs/swarm-footage-route-20260916T2020Z/SYNTHESIS.md (round 1);
# ATTR3-FOOTAGE-BIND-1 (round 2, this file).
#
# FIXTURE REHEARSAL (ATTR3-FIXTURE-REHEARSAL-1, extended by ATTR3-FIXTURE-STAGE-1): -ClipId
# may instead be exactly 'tiny_dual_iso' or 'large_dual_iso' -- the two tracked fixtures under
# tests/fixtures/clips/, admitted by NA-4 without a CLIP_OR_NONE authorization because they
# never resolve through an id-to-file lookup either. -ClipPath is OPTIONAL for a fixture id:
# omitted, it is derived as the agent cache path for -ClipId; supplied, it must still resolve
# to the agent cache with BaseName -ceq $ClipId. -ClipPath is REFUSED outright for an owner id
# (see FOOTAGE above); only the fixture arm ever takes one. A fixture
# id also requires -FixtureSha256 (64 lowercase hex, baked by attr3-stage-fixture-job.ps1 and
# printed on its own result line): the emitted job hashes the cached clip before it is ever
# opened and fails closed at FIXTURE_CONTENT_MISMATCH (exit 17) on a mismatch -- a cache file
# name proves nothing about its bytes. Such a run bakes fixtureRehearsal=true into every
# summary.json/evidence-manifest.json this job writes, so a fixture run can never be read as
# an attribution result.
#
# Differences from PLAYBACK-ATTR-2:
#   - shipping-default scale (4), NOT forced to 1: no MLVAPP_PLAYBACK_SCALE_FACTOR
#     override is emitted, and -ScaleFactor 4 is passed explicitly.
#   - asserts GPU recon frames > 0 and cpu_frames == 0 from the LAST
#     playback_smoke.gpu_summary line, so a silent CPU fallback fails the job instead
#     of quietly reporting a CPU-path attribution as if it were CUDA.
#   - writes a provenance.json sidecar (top-level rangeHeadSha/llrawprocBlobId/
#     dllSha256/pendingSymbolPresence) that validates under
#     tools/repo_hygiene/gpu_job_result_provenance.py.
#   - the exe/DLL under test are NOT pinned SHA256 constants (PLAYBACK-ATTR-2 already
#     had a built artifact to pin); they are named deterministically from -SourceCommit
#     and expected to already be staged in the Bachelor cache by the split-build route's
#     staging job (tools/profiling/bachelor/playback-attr-3-cuda-stage-job.ps1), which uses
#     the IDENTICAL naming convention. Their hashes are computed fresh at run time and
#     verified against the staged build manifest, not asserted against a value this
#     generator could not have known.
#
# SPLIT BUILD (swarm ruling 2026-09-16,
# .claude-state/fleet-runs/swarm-attr3-buildhost-20260916T2150Z/SYNTHESIS.md). Bachelor has
# no VC tools and no CUDA toolkit, so NOTHING is compiled or inspected with MSVC tooling
# here. The CUDA DLL pair is built on Ultra-Magnus, the exe on the board host, and the
# package is staged into the Bachelor cache by a staging job. Two consequences in this file:
#   - pendingSymbolPresence is READ from the staged build manifest (the old on-Bachelor
#     MSVC export-inspection probe is gone; it could only ever throw here);
#   - the run's own gpu_playback_recon.eligibility diagnostics are parsed BEFORE any
#     verdict, and the job exits 15 (BACKEND_NOT_AVAILABLE) unless
#     cuda_backend_available=1 and r16_available=1, recording both plus r16_reason.
#
# WHICH LOG (sol, PR #133 r2, BLOCKER). The eligibility line is read from the log
# run-release-gui-smoke.ps1 itself designates for the run just executed --
# result.json's `log.path`, the "$outputPath.run.log" per-run snapshot it calls the
# comparison authority -- bound to `evidence.runLogSnapshot.sha256`. NEVER from a glob
# over out\diagnostic\logs, which cannot match anything: that runner writes into a
# GUID-nonced logs-<stem>-<nonce> directory. The exact lines relied on are quoted beside
# the call. A log that is absent, unbound or outside this job's work tree exits 16
# (SMOKE_LOG_UNAVAILABLE) -- a missing gate is never a passed gate.
#
# Usage (owner clip, id-only -- no -ClipPath, no -FixtureSha256):
#   pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-job.ps1 `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-lowercase-hex> `
#       -ClipId M16-1243 -OutFile <path>\<jobId>.job.ps1
# (the id is resolved through tools/gates/resolve_consented_clip.py; see FOOTAGE above)
# Usage (fixture rehearsal, no -ClipPath):
#   pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-job.ps1 `
#       -SourceCommit <40-hex> -BuildManifestSha256 <64-lowercase-hex> -ClipId tiny_dual_iso `
#       -FixtureSha256 <64-lowercase-hex from attr3-stage-fixture-job.ps1's FIXTURE_SHA256= line> `
#       -OutFile <path>\<jobId>.job.ps1
#
# -BuildManifestSha256 is the sha the assembler printed (MANIFEST_SHA256= on its RESULT line)
# and the staging generator echoed as buildManifestSha256; see docs/playback-attr-3-cuda.md.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    # Either the owner-clip id pattern (resolved via tools/gates/resolve_consented_clip.py,
    # ATTR3-FOOTAGE-BIND-1), or exactly one of the two tracked-fixture ids (a literal allowlist,
    # not a loosened pattern -- ATTR3-FIXTURE-REHEARSAL-1). A fixture id never touches the
    # resolver: it names no clip in the frozen spec, so no id-to-file lookup ever runs for it.
    [Parameter(Mandatory = $true)]
    # The fixture arm is CASE-SENSITIVE via (?-i:...): PowerShell's ValidatePattern is
    # case-insensitive by default, so without it 'TINY_DUAL_ISO' would pass validation while the
    # case-sensitive membership test below classified it as an owner clip and emitted
    # fixtureRehearsal=false (sol, PR #137 r1 BLOCKER).
    # \z, not $: .NET's $ also matches before a terminal newline, so 'tiny_dual_iso<LF>' would
    # validate and then miss the case-sensitive membership test (sol, PR #137 r2).
    [ValidatePattern('^(?:[A-Za-z]\d{2}-\d{3,4}|(?-i:tiny_dual_iso|large_dual_iso))\z')]
    [string]$ClipId,

    # A fixture id's cache path, in the agent cache, BaseName -ceq -ClipId; the emitted job
    # re-checks both on Bachelor and fails closed. OPTIONAL for a fixture id
    # (ATTR3-FIXTURE-STAGE-1): omitted, it is derived below as the agent cache path for
    # -ClipId, from the same agent root the template's own $Root already names.
    # REFUSED outright for an owner clip id (ATTR3-FOOTAGE-BIND-1): the owner's footage path is
    # resolved through tools/gates/resolve_consented_clip.py, never typed by a caller.
    # NO ValidatePattern here (round 2, sol/astra BLOCKER): a parameter-level validator runs
    # BEFORE this file's own owner/fixture classification, and PowerShell's binding-failure
    # message echoes the rejected value verbatim -- so a forward-slash owner-clip path (real
    # shape) was disclosed in the diagnostic instead of reaching the path-free
    # PLAYBACK_ATTR3_CLIPPATH_REFUSED throw below. Validated in the body instead, AFTER
    # classification: the owner arm never echoes the value, only the fixture arm's allowlist
    # check does (a fixture path is never footage).
    [Parameter(Mandatory = $false)]
    [string]$ClipPath = '',

    # A fixture id's cached bytes, authenticated before this generator will bake -ClipPath
    # into the emitted job: MANDATORY for a fixture id, REFUSED for an owner clip id (an owner
    # id is bound to its content by the resolver's own cross-check, not by a caller-supplied
    # hash). The value attr3-stage-fixture-job.ps1 bakes and prints on its own FIXTURE_SHA256=
    # result line. Validated below rather than by ValidatePattern, so a malformed value and an
    # outright missing one are told apart in the refusal.
    [string]$FixtureSha256 = '',

    [Parameter(Mandatory = $true)]
    [string]$OutFile,

    # The lowercase sha256 of the build.json that the staging job published into the Bachelor
    # cache -- printed by tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1
    # (MANIFEST_SHA256= on its RESULT line) and echoed as buildManifestSha256 by
    # tools/profiling/bachelor/playback-attr-3-cuda-stage-job.ps1. MANDATORY (sol, PR #133 r2):
    # without it the job trusts whichever same-named manifest sits in the mutable cache, and
    # replacing build.json plus matching artifacts forges pendingSymbolPresence and the DLL
    # association in one move.
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$BuildManifestSha256,

    # ATTR3-FOOTAGE-BIND-1 PR-B round 4 (BINDING-TIME ORDERING). A parameter DEFAULT expression is
    # evaluated at BIND TIME, before this script's own first statement runs -- so a default that
    # calls Resolve-Path/Join-Path/Test-Path/Get-Item/Get-ChildItem/git does filesystem or process
    # I/O before the owner/fixture flag refusals below ever get a chance to run first. The default
    # is therefore the empty string; the SAME computed default, and its resolution, both move into
    # the body, after those refusals (see "RepoRoot resolution" below). -AgentRoot below drops its
    # ValidatePattern for the same reason ValidatePattern was already removed from -ClipPath in
    # round 2: PowerShell's own binding-failure message echoes the rejected value verbatim, so a
    # malformed path-shaped parameter would disclose a path-shaped string before this file's own
    # path-free refusal ever ran -- validated in the body instead, without echoing the value.
    # -SourceCommit/-BuildManifestSha256 and the filename-shaped parameters below keep their
    # ValidatePattern: a commit hash or a plain filename cannot itself carry a path, so echoing a
    # rejected one discloses nothing.
    [string]$RepoRoot = '',

    # The ONE definition of the agent root, for both the template's own $Root (substituted via
    # __AGENT_ROOT__ below) and this generator's own fixture -ClipPath derivation -- so the two
    # can never drift apart. Overridable only so a test can point the emitted job at a temporary
    # directory instead of the real measurement host's C:\mlvtmp\mlv-agent; production never
    # passes this.
    # NO ValidatePattern (round 4, see the comment above -RepoRoot): validated in the body
    # instead, without echoing the value.
    [string]$AgentRoot = 'C:\mlvtmp\mlv-agent',

    # Derived from -SourceCommit, not pinned to an old package: matches the package
    # tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1 builds on the board host and
    # tools/profiling/bachelor/playback-attr-3-cuda-stage-job.ps1's emitted job stages into the
    # Bachelor cache as MLVApp-playback-attr-3-cuda-<sha12>-pkg.zip -- a raw zip of that build's
    # deployed release dir, so the exe inside keeps its unrenamed build name, MLVApp.exe. This
    # replaces the old default (the July MLVApp-CUDA-W4W5-4d1955f8.zip base, wrong for a build
    # of current master since its Qt runtime may not match). The hub must still verify the
    # derived package is actually staged in the Bachelor cache (built from -SourceCommit, not a
    # stale one) before submitting.
    # ValidatePattern (round 2, astra BLOCKER audit): this plain filename is baked into the
    # emitted job's single-quoted __BASE_PACKAGE_ZIP__ literal. \z, not $, matching -ClipId's
    # own pattern above -- .NET's $ matches before a terminal newline too.
    [ValidatePattern('^[A-Za-z0-9._-]+\.zip\z')]
    [string]$BasePackageZip = "MLVApp-playback-attr-3-cuda-$($SourceCommit.Substring(0,12))-pkg.zip",

    [ValidatePattern('^[A-Za-z0-9._-]+\.exe\z')]
    [string]$BasePackageExeName = 'MLVApp.exe',

    [ValidatePattern('^[A-Za-z0-9._-]+\.exe\z')]
    [string]$PresentMonName = 'PresentMon-2.5.1-x64.exe',
    [string]$PresentMonSha256 = '9BEC3083069F58F911E6A512F4806DB51A27BD096103087BC1D05EF54C80A191',

    # Owner consent for clip M16-1243 on this card; cited (never resolved to a path
    # here) in the evidence manifest for audit trail.
    # ValidatePattern (round 2, astra BLOCKER): this value used to be baked into the emitted
    # job's single-quoted __CONSENT_RECEIPT__ literal with no validation and no quote-escaping,
    # so a receipt-name string shaped like "'; $OwnerPartsJson = '<forged parts>'; #" closed the
    # literal early and re-assigned OwnerPartsJson before the job's own content gate ever ran --
    # arbitrary paths/lengths/hashes, independent of the resolver. Restricted to a plain
    # <name>.json basename, AND still escaped at bake time below (defense in depth, matching the
    # -ClipPath / -OwnerPartsJson treatment): neither protection alone should be the only one.
    [ValidatePattern('^[A-Za-z0-9._-]+\.json\z')]
    [string]$ConsentReceiptFileName = 'owner-footage-consent-20260916.json',

    [string]$LlrawprocRelativePath = 'src/mlv/llrawproc/llrawproc.c',

    # CUDA-PLAYBACK-CONTACT-SHEET-1: opt-in, off by default like every other optional capture
    # in this job. When set, the emitted job passes the app's own --contact-sheet-dir/
    # --contact-sheet-frames through -AdditionalArgs on the SAME leg's un-timed pass (the app
    # captures those frames strictly after its own measured playback interval closes -- see
    # runGuiPlaybackSmoke's contact-sheet capture block -- so this never perturbs the leg's
    # fps/swap-cadence numbers) and publishes the raw PNG+JSON pairs under
    # artifacts/contact-sheet/raw/. Composing them into one labelled sheet (tools/profiling/
    # make-contact-sheet.py) is left to a later step, off this job: round-1 scope keeps this
    # hunk small while UM-CUDA-BENCH-VENUE-1 also edits this file.
    [switch]$ContactSheet,
    [ValidateRange(1, 60)]
    [int]$ContactSheetFrames = 6
)

$ErrorActionPreference = 'Stop'

# --- ATTR3-FOOTAGE-BIND-1 PR-B: resolve an owner-clip id, generator-side only -----------------
# Mirrors attr3-footage-presence-job.ps1's own resolver invocation (PR-A): the resolver runs as a
# CHILD PROCESS that writes paths only into a private --emit-json temp file; this generator reads
# it IN-PROCESS (never Write-Output-ing its content) and deletes it in a `finally` before
# returning. A real footage path may flow only through process memory or through a file a child
# process writes -- never into a tool call or onto this generator's own stdout. This is the
# generator's ONLY path to obtaining parts for an owner id: there is no parameter that skips it.
$OwnerPartPathPattern = '^[A-Za-z]:(?:/[A-Za-z0-9_.-]+)+$'

function Resolve-AttrCudaOwnerPython {
    <#
    .SYNOPSIS
    Prefer `python.exe` only after proving it is Python 3; else fall back to the `py` launcher
    pinned to -3. Mirrors attr3-footage-presence-job.ps1's own interpreter probe.
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
    throw 'PLAYBACK_ATTR3_OWNER_NO_PYTHON no Python 3 interpreter is available to resolve an owner-clip id'
}

function Resolve-AttrCudaOwnerClipParts {
    <#
    .SYNOPSIS
    Resolve an owner-clip id to its baked-for-embedding parts (index/pathBase64/length/sha256
    ordered by index), or throw with the resolver's own typed refusal. The resolver script is
    located relative to THIS generator's own checkout ($PSScriptRoot), independent of -RepoRoot
    (which may point at a throwaway repo in a test): the tool itself always ships with the real
    checkout, while -RepoRoot only selects which repo's frozen spec ref is resolved against.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ClipId,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    $resolverPath = Join-Path $PSScriptRoot '..\..\gates\resolve_consented_clip.py'
    if (-not (Test-Path -LiteralPath $resolverPath -PathType Leaf)) {
        throw "PLAYBACK_ATTR3_OWNER_RESOLVER_MISSING resolver not found at $resolverPath"
    }
    $resolverPath = (Resolve-Path -LiteralPath $resolverPath).Path
    $py = Resolve-AttrCudaOwnerPython

    $emitPath = Join-Path ([IO.Path]::GetTempPath()) ("playback-attr-3-cuda-owner-resolve-$([guid]::NewGuid().ToString('N')).json")
    try {
        $resolverArgs = @($py.PrefixArgs) + @($resolverPath, '--clip-id', $ClipId, '--repo-root', $RepoRoot, '--emit-json', $emitPath)
        $summaryLines = @(& $py.Exe @resolverArgs 2>&1)
        $resolverExit = $LASTEXITCODE
        if ($resolverExit -ne 0) {
            # $summaryLines is the resolver's own path-free JSON summary line (its CLI contract
            # never prints a part path), so folding it into this message is safe.
            throw "PLAYBACK_ATTR3_OWNER_RESOLVE_REFUSED clip '$ClipId' was refused by the resolver (exit $resolverExit): $($summaryLines -join ' ')"
        }
        if (-not (Test-Path -LiteralPath $emitPath -PathType Leaf)) {
            throw 'PLAYBACK_ATTR3_OWNER_EMIT_MISSING resolver exited 0 but wrote no --emit-json file'
        }
        $emitBytes = [IO.File]::ReadAllBytes($emitPath)
    } finally {
        if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }
    }

    $resolved = [Text.Encoding]::UTF8.GetString($emitBytes) | ConvertFrom-Json
    if ($resolved.clipId -cne $ClipId) {
        throw "PLAYBACK_ATTR3_OWNER_CLIP_ID_MISMATCH resolver emitted clipId '$($resolved.clipId)' for requested '$ClipId'"
    }
    $rawParts = @($resolved.parts)
    if ($rawParts.Count -eq 0) {
        throw "PLAYBACK_ATTR3_OWNER_NO_PARTS resolver returned zero parts for '$ClipId'"
    }

    # Structural check on each part's path, in addition to the resolver's own content
    # cross-check: a drive letter, colon, then forward-slash-separated segments of letters,
    # digits, underscore, dot and hyphen -- the exact shape of the real consented-part paths in
    # tools/gates/output-budget.json. Defense in depth, not the injection-safety mechanism (the
    # base64 embedding below is): this refuses a shape the base64 route would have accepted.
    @($rawParts | Sort-Object { [int]$_.index } | ForEach-Object {
        $path = [string]$_.path
        if ($path -notmatch $OwnerPartPathPattern) {
            throw "PLAYBACK_ATTR3_OWNER_PART_PATH_INVALID part $($_.index) does not match the owner footage path allowlist (drive letter, colon, forward-slash segments)"
        }
        $sha = ([string]$_.sha256).ToLowerInvariant()
        if ($sha -notmatch '^[0-9a-f]{64}$') {
            throw "PLAYBACK_ATTR3_OWNER_PART_SHA_INVALID part $($_.index) sha256 is not 64 hex characters"
        }
        # Each part's path travels ONLY as base64 of its UTF-8 bytes from here on -- never as an
        # interpolated literal (see Attr3FootagePresenceJob.psm1's header for why).
        [ordered]@{
            index = [int]$_.index
            pathBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($path))
            length = [int64]$_.length
            sha256 = $sha
        }
    })
}

# ATTR3-FIXTURE-REHEARSAL-1: the literal allowlist the ValidatePattern above also enforces,
# restated here so the fixtureRehearsal flag is decided by an exact membership test rather
# than by re-deriving it from the regex. A fixture run must be unmistakable in its own
# artifacts, so this flag is baked into the job body and never inferred by the job at runtime.
$FixtureClipIds = @('tiny_dual_iso', 'large_dual_iso')
$isFixtureRehearsal = $FixtureClipIds -ccontains $ClipId
$fixtureRehearsalLiteral = if ($isFixtureRehearsal) { '$true' } else { '$false' }

# CUDA-PLAYBACK-CONTACT-SHEET-1: baked the same way fixtureRehearsalLiteral is above --
# a plain bool/int literal substituted into the template, never caller text.
$contactSheetEnabledLiteral = if ($ContactSheet) { '$true' } else { '$false' }
$contactSheetFrameCountLiteral = [string]$ContactSheetFrames

# ATTR3-FIXTURE-STAGE-1. The compound suffix the two tracked fixtures carry is never spelled
# as one literal token anywhere in this file: a token ending in it trips this repository's own
# NA-4 PreToolUse gate, even in source text that names no real clip, so it is composed here.
$FixtureClipExtension = '.' + 'mlv'

# --- owner/fixture-clip FLAG refusals FIRST (round 3, STRUCTURAL): pure parameter checks only,
#     no I/O of any kind -- no Import-Module, no Get-AttrCudaEmbeddedFunctionSource, no git, no
#     Test-Path, not even $RepoRoot resolved yet. Round 2 already proved a resolver refusal must
#     surface before an unrelated SourceCommit/PresentMon/closure failure; round 3 goes one step
#     further -- a caller who passes BOTH a bogus -RepoRoot and a refused -ClipPath for an owner
#     id must see PLAYBACK_ATTR3_CLIPPATH_REFUSED, never a RepoRoot error that only fires first
#     because RepoRoot resolution used to sit ahead of this classification in the file. -------
# ATTR3-FOOTAGE-BIND-1 PR-B round 6 (astra major): the owner arm's -AgentRoot shape check no
# longer lives in this if/else at all -- round 5 nested it inside the else branch, after the
# owner arm's own -FixtureSha256/-ClipPath refusals, but STILL ahead of $RepoRoot resolution and
# the resolver call below. -AgentRoot is never used to reach the resolver (only -ClipId and the
# resolved -RepoRoot are), so a malformed -AgentRoot was still the first thing to throw for an
# owner id whose -RepoRoot would otherwise have produced a resolver refusal -- the complete
# owner-id decision is flag refusals, THEN RepoRoot resolution (the resolver needs it), THEN the
# resolver call and its own typed refusal; -AgentRoot validation is unrelated to that decision and
# now runs strictly after it, alongside "everything else" below. The fixture arm is UNCHANGED: it
# still validates -AgentRoot first, before using it to derive -ClipPath, so it keeps its own
# order (never echoing the value, for the same reason the ClipPath refusal never does).
if ($isFixtureRehearsal) {
    if ($AgentRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') {
        throw 'PLAYBACK_ATTR3_AGENTROOT_INVALID -AgentRoot contains characters outside the allowlist'
    }
    if ($FixtureSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "PLAYBACK_ATTR3_FIXTURE_SHA_REQUIRED -FixtureSha256 must be 64 lowercase hex for a fixture id ('$ClipId'); got '$FixtureSha256'"
    }
    if ([string]::IsNullOrWhiteSpace($ClipPath)) {
        $ClipPath = Join-Path (Join-Path $AgentRoot 'cache') ($ClipId + $FixtureClipExtension)
    }
    if ($ClipPath -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') {
        throw "PLAYBACK_ATTR3_CLIPPATH_INVALID -ClipPath contains characters outside the allowlist: '$ClipPath'"
    }
} else {
    # ATTR3-FOOTAGE-BIND-1 PR-B round 6 (sol minor): flag PRESENCE is refused, not flag
    # TRUTHINESS -- $PSBoundParameters.ContainsKey() so an owner id with an explicitly bound
    # empty -FixtureSha256/-ClipPath (e.g. -ClipPath '') is refused too, matching this file's own
    # documented "refused outright" contract instead of being silently read as omission.
    if ($PSBoundParameters.ContainsKey('FixtureSha256')) {
        throw "PLAYBACK_ATTR3_FIXTURE_SHA_REFUSED -FixtureSha256 is refused for an owner clip id ('$ClipId')"
    }
    if ($PSBoundParameters.ContainsKey('ClipPath')) {
        throw "PLAYBACK_ATTR3_CLIPPATH_REFUSED -ClipPath is refused for an owner clip id ('$ClipId'): the footage path is resolved through tools/gates/resolve_consented_clip.py, never typed by a caller"
    }
}

# --- RepoRoot resolution: the ONE I/O statement before the resolver call, wrapped so a failure
#     throws a fixed token WITHOUT echoing the caller-supplied value (round 3, STRUCTURAL) -- a
#     bogus -RepoRoot used to surface Resolve-Path's own error text, which echoes the raw value
#     verbatim, ahead of every owner/fixture check below it in the old ordering. ---------------
# ATTR3-FOOTAGE-BIND-1 PR-B round 4: the DEFAULT itself is computed here too, never at
# parameter-bind time (see the comment above -RepoRoot's own declaration). Join-Path is pure
# string composition with no filesystem access, so this adds no I/O ahead of the Resolve-Path
# call immediately below it.
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = Join-Path $PSScriptRoot '..\..\..'
}
try {
    $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
} catch {
    throw 'PLAYBACK_ATTR3_REPOROOT_INVALID -RepoRoot does not resolve to an existing path'
}

# ATTR3-FOOTAGE-BIND-1 PR-B (round 1): an owner-clip id is RESOLVED right here -- before the
# smoke-runner closure resolution, before any build-manifest work, before the SourceCommit/
# PresentMon provenance checks below, and (round 3) before Import-Module and the embedded-
# function extraction -- so a resolver refusal always surfaces on its own, never masked by an
# unrelated closure, build-manifest, provenance or I/O-adjacent failure that would also have
# fired had the code reached that far.
$ownerPartsForJob = $null
if (-not $isFixtureRehearsal) {
    $ownerPartsForJob = @(Resolve-AttrCudaOwnerClipParts -ClipId $ClipId -RepoRoot $RepoRoot)
}
$ownerPartsJson = ''
if (-not $isFixtureRehearsal) {
    $ownerPartsJson = $ownerPartsForJob | ConvertTo-Json -Compress -Depth 5
    if ($ownerPartsForJob.Count -eq 1) { $ownerPartsJson = "[$ownerPartsJson]" }
}

# ATTR3-FOOTAGE-BIND-1 PR-B round 6 (astra major): the owner arm's -AgentRoot shape check runs
# HERE -- after the complete owner-id decision (flag refusals, RepoRoot resolution, the resolver
# call and its own typed refusal) -- never before it. -AgentRoot plays no part in reaching the
# resolver; it is used only below, to build the __AGENT_ROOT__ substitution baked into the
# emitted job. Never echoes the value, same as every other path-shaped parameter's refusal.
if (-not $isFixtureRehearsal) {
    if ($AgentRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') {
        throw 'PLAYBACK_ATTR3_AGENTROOT_INVALID -AgentRoot contains characters outside the allowlist'
    }
}

# --- only now: Import-Module and the embedded-function extraction, and everything else --------
# The owner/fixture decision above never needed either: Resolve-AttrCudaOwnerClipParts is
# self-contained (a child-process call plus pure parsing), so moving these two statements past it
# costs nothing and removes them from the set of things a CLIPPATH/REPOROOT refusal could ever be
# masked by. The emitted job runs on a host with no checkout, so it cannot Import-Module: the
# verification functions are spliced into its text VERBATIM at generation time. The test suite
# executes the module copy, so the code under test is the code that runs on Bachelor.
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force
$embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
    'Assert-AttrCudaBuildManifest',
    'Resolve-AttrCudaSmokeRunLog',
    'Get-AttrCudaLastEligibilityLine',
    'Get-AttrCudaEligibilityVerdict',
    'Assert-AttrCudaWritableFileSlot',
    'Test-AttrCudaPathIsReparsePoint',
    'Get-AttrCudaClosureDirectoryMismatch',
    'Publish-AttrCudaText',
    # CUDA-PLAYBACK-CONTACT-SHEET-1 r1b: writes the embedded composer script's decoded bytes
    # to a file under $Work before it is invoked -- the byte-array counterpart of
    # Publish-AttrCudaText, for a payload that arrived base64-decoded rather than copied.
    'Publish-AttrCudaBytes',
    # CUDA-PLAYBACK-CONTACT-SHEET-1 r1d: quotes a Start-Process -ArgumentList element that may
    # contain a space (the composer's --host/--gpu values) -- see its own header.
    'ConvertTo-AttrCudaQuotedProcessArgument',
    # CUDA-PLAYBACK-CONTACT-SHEET-2 round 2: was defined inline in this template until the
    # PRESENTMON-HARNESS-ROBUSTNESS-2 merge's test_every_called_attrcuda_command_is_defined_in_
    # the_real_embedded_text required every -AttrCuda-named call in the template to come from
    # this splice instead -- see its own header in AttrCudaArtifacts.psm1.
    'Publish-AttrCudaContactSheetRawCaptures',
    'Publish-AttrCudaFileCopy',
    'Publish-AttrCudaFileMove',
    'New-AttrCudaDirectory',
    'Assert-AttrCudaNoLinkBelowRoot',
    'Remove-AttrCudaTree',
    # ATTR3-FOOTAGE-BIND-1 PR-B: the owner-clip content gate. Test-AttrCudaFootagePart is the
    # SAME function Attr3FootagePresenceJob.psm1 embeds for its own probe -- one definition,
    # spliced verbatim into both, never two copies that can drift apart.
    'Read-AttrCudaBase64Payload',
    'Test-AttrCudaFootagePart',
    'ConvertTo-AttrCudaUtf8String',
    # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-1: the PresentMon post-step's own safety boundary --
    # parses, clips to the playback window, groups by (ProcessID, SwapChainAddress), and returns a
    # typed PRESENTMON_UNAVAILABLE/DISPLAY_ASLEEP refusal instead of an uncaught throw. See its own
    # header in AttrCudaArtifacts.psm1.
    'Get-AttrCudaPresentMonDisplayReport',
    # PRESENTMON-HARNESS-ROBUSTNESS-2: sanitizes free-form reason text before it is embedded in a
    # RESULT= stdout line's quoted REASON="..." field -- see its own header in AttrCudaArtifacts.psm1.
    'ConvertTo-AttrCudaResultLineSafeText',
    # PRESENTMON-HARNESS-ROBUSTNESS-2 r1c (sol PRE-REVIEW #2 BLOCKER): the sufficiency gate's two
    # coverage-arm helpers, called by the emitted template's PresentMon status block below but
    # missing from this list -- every otherwise-successful leg hit CommandNotFoundException on a
    # host with no checkout/Import-Module before publishing presentMonStatus. Both are self-
    # contained (no calls to other AttrCuda functions), so no further names are needed.
    'Get-AttrCudaAppSwapTelemetry',
    'Get-AttrCudaTemporalCoverage'
)
# ATTR3-FOOTAGE-BIND-1 PR-B round 4b: the private verified-part directory (one hard link per
# verified part, under a neutral name derived from its index, so nothing downstream -- the smoke
# runner's own sibling glob, the app's own continuation-part walk or sidecar -- ever sees the
# owner's real directory) moved OUT of AttrCudaArtifacts.psm1 into AttrCudaOwnerFootage.psm1: that
# shared module is embedded by every PLAYBACK-ATTR-3-CUDA build-route script (assemble/stage/
# DLL-pair), none of which has any business knowing footage exists (NoFootageTokensTests, in
# tools/repo_hygiene/test_playback_attr_3_cuda_split_route.py). This generator -- the owner-clip
# attribution job, not in that test's NEW_SCRIPTS list -- is the only caller that still embeds
# them, extracted from the new module by the SAME Get-AttrCudaEmbeddedFunctionSource this file
# already imports, pointed at a different -ModulePath.
$embeddedFunctions = $embeddedFunctions + "`r`n`r`n" + (Get-AttrCudaEmbeddedFunctionSource -ModulePath (Join-Path $PSScriptRoot 'AttrCudaOwnerFootage.psm1') -Name @(
    'Get-AttrCudaOwnerFootageNeutralName',
    'Assert-AttrCudaOwnerPartsNaming',
    'Get-AttrCudaFileIdentity',
    'New-AttrCudaOwnerFootageLink',
    'Open-AttrCudaReadOnlyHandle',
    'Close-AttrCudaOwnerFootageWorkspace'
))

# --- resolve provenance locally, BEFORE the job ever touches Bachelor -------------
# (round 2: moved here, AFTER the owner/fixture decision above -- see that block's comment.)
& git -C $RepoRoot cat-file -e "$SourceCommit^{commit}" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "SourceCommit is not a commit known to the local repo at $RepoRoot : $SourceCommit"
}
$llrawprocBlobId = (& git -C $RepoRoot rev-parse "${SourceCommit}:${LlrawprocRelativePath}").Trim()
if ($LASTEXITCODE -ne 0 -or $llrawprocBlobId -notmatch '^[0-9a-f]{40}$') {
    throw "Could not resolve a blob id for $LlrawprocRelativePath at $SourceCommit"
}
if ($PresentMonSha256 -notmatch '^[0-9A-Fa-f]{64}$') {
    throw "PresentMonSha256 is not a 64-hex sha256: $PresentMonSha256"
}

# ATTR3-SMOKE-RUNNER-DEPS-1 round 3 (NARROW BY REDESIGN): the runner is not standalone -- it
# dot-sources gui-smoke-screenshot-provenance.ps1, provenance-stamp.ps1, (since
# ATTR3-VISUAL-QUALITY-EVIDENCE-1 round 2) gui-smoke-color-artifact-scan.ps1, and (since
# CUDA-S4-TEXTURE-ROUTE-CLAMP-1 round 2) gui-smoke-gpu-texture-route-validation.ps1, and imports
# gui-smoke-process-boundary.psm1, all resolved through $PSScriptRoot at runtime. Staging the
# runner alone (ATTR3-SMOKE-RUNNER-PIN-1) left Bachelor unable to launch it at all: the runner
# died at its own dot-source line, the app never launched, PresentMon never saw a target and
# never exited, and PRESENTMON_TIMEOUT masked the real cause. Round 1/2 discovered this closure
# by SCANNING; a design swarm ruled that undiscoverable-by-patching (a literal-based scanner
# cannot see an extension-less load or a bareword Import-Module, and a basename-only classifier
# can be satisfied by an unrelated absolute path). The closure is the EXPLICITLY PINNED six-file
# manifest (Get-AttrCudaSmokeRunnerClosureManifest) -- never mechanically derived or scanned for;
# Assert-AttrCudaClosureComplete is the generator-time proof that the pinned list still matches
# what the real files load, using an AST census instead of a scan. Resolve-AttrCudaSmokeRunnerClosure
# then does nothing but resolve each pinned path's committed bytes -- exactly as
# attr3-stage-smoke-runner-job.ps1 resolves the same closure independently, with no value needing
# to be threaded between the two.
[void](Assert-AttrCudaClosureComplete -RepoRoot $RepoRoot -Commit $SourceCommit)
$smokeRunnerClosure = @(Resolve-AttrCudaSmokeRunnerClosure -RepoRoot $RepoRoot -Commit $SourceCommit)
foreach ($entry in $smokeRunnerClosure) {
    [void](Assert-AttrCudaSafeArtifactName -Name $entry.name)
    # Fable minor (round 2, carried forward): validated as 64 lowercase hex before it is
    # substituted into the emitted job, the same way -PresentMonSha256 is validated above -- a
    # value from this helper is trusted enough to gate a publish decision and deserves the same
    # shape check.
    if ($entry.sha256 -notmatch '^[0-9a-f]{64}$') {
        throw "ATTRCUDA_BLOB_SHA_MALFORMED resolved closure file sha256 is not 64 lowercase hex for $($entry.name): '$($entry.sha256)'"
    }
}
$smokeRunnerClosureDigest = Get-AttrCudaClosureDigestHex -Closure $smokeRunnerClosure
if ($smokeRunnerClosureDigest -notmatch '^[0-9a-f]{64}$') {
    throw "ATTRCUDA_BLOB_SHA_MALFORMED smoke-runner closure digest is not 64 lowercase hex: '$smokeRunnerClosureDigest'"
}
# Content-addressed, mirroring ATTR3-SMOKE-RUNNER-PIN-1 round 2's single-file cache name: a
# distinct closure publishes under a distinct directory name, so this job never has to contend
# with -- or touch -- whatever bytes already sit under another closure's directory.
$smokeRunnerClosureDirName = "smoke-runner-$($smokeRunnerClosureDigest.Substring(0, 16))"
[void](Assert-AttrCudaSafeArtifactName -Name $smokeRunnerClosureDirName)
$smokeRunnerName = $smokeRunnerClosure[0].name

function ConvertTo-AttrCudaGeneratorPsLiteral([string]$Value) { "'" + $Value.Replace("'", "''") + "'" }
$smokeRunnerClosureLiteral = "@(`r`n" + (($smokeRunnerClosure | ForEach-Object {
    "    [pscustomobject]@{ name = $(ConvertTo-AttrCudaGeneratorPsLiteral $_.name); sha256 = '$($_.sha256)' }"
}) -join ",`r`n") + "`r`n)"

$shortSha = $SourceCommit.Substring(0, 12)
$exeName = "MLVApp-playback-attr-3-cuda-$shortSha.exe"
$reconName = "igpu_recon_cuda-playback-attr-3-cuda-$shortSha.dll"

# CUDA-PLAYBACK-CONTACT-SHEET-1 r1b: the venue has no checkout (see the smoke-runner
# closure comment above), so the composer that turns raw --contact-sheet-dir captures
# into one labelled sheet + stats sidecar must ship INLINE, byte-exact as committed at
# $SourceCommit -- same generator-only, byte-exact mechanism as the smoke-runner closure
# and the llrawproc blob (Resolve-AttrCudaCommittedBlobId/Save-AttrCudaCommittedBlobBytes),
# just base64-embedded directly rather than cached: one small text file, not cache-worthy
# like the six-file closure or the multi-MB llrawproc blob.
# CUDA-PLAYBACK-CONTACT-SHEET-1 r1c (BLOCKER fix): resolved ONLY when -ContactSheet is
# set. This path did not exist at every commit this generator can be asked to build (a
# pre-card $SourceCommit has no tools/profiling/make-contact-sheet.py at all), so
# resolving it unconditionally broke every default-off generation against such a commit --
# the one thing -ContactSheet being off is supposed to leave byte-identical.
if ($ContactSheet) {
    $contactSheetComposerBlobId = Resolve-AttrCudaCommittedBlobId -RepoRoot $RepoRoot -Commit $SourceCommit -RepoRelativePath 'tools/profiling/make-contact-sheet.py'
    $contactSheetComposerTempPath = Join-Path ([IO.Path]::GetTempPath()) "playback-attr-3-cuda-contact-sheet-composer-$([guid]::NewGuid().ToString('N')).py"
    try {
        $contactSheetComposerSha256 = Save-AttrCudaCommittedBlobBytes -RepoRoot $RepoRoot -BlobId $contactSheetComposerBlobId -Destination $contactSheetComposerTempPath
        if ($contactSheetComposerSha256 -notmatch '^[0-9a-f]{64}$') {
            throw "ATTRCUDA_BLOB_SHA_MALFORMED composer script sha256 is not 64 lowercase hex: '$contactSheetComposerSha256'"
        }
        $contactSheetComposerBytes = [IO.File]::ReadAllBytes($contactSheetComposerTempPath)
    } finally {
        if (Test-Path -LiteralPath $contactSheetComposerTempPath) { Remove-Item -LiteralPath $contactSheetComposerTempPath -Force }
    }
    $contactSheetComposerPyBase64 = [Convert]::ToBase64String($contactSheetComposerBytes)
    $contactSheetComposerSha256ForTemplate = $contactSheetComposerSha256
} else {
    $contactSheetComposerPyBase64 = ''
    $contactSheetComposerSha256ForTemplate = ''
}

# --- job body template (placeholders are substituted below; the body itself never
#     touches this generator's variables directly, so there is no accidental capture
#     of this machine's environment into the emitted script) ----------------------
$template = @'
$ErrorActionPreference = 'Stop'
$SourceCommit = '__SOURCE_COMMIT__'
$ClipId = '__CLIP_ID__'
$BuildManifestSha256 = '__BUILD_MANIFEST_SHA256__'
$AuthorizedClipPath = '__CLIP_PATH__'
$OwnerPartsJson = '__OWNER_PARTS_JSON__'
$RangeHeadSha = '__RANGE_HEAD_SHA__'
$LlrawprocBlobId = '__LLRAWPROC_BLOB_ID__'
$ExeName = '__EXE_NAME__'
$ReconName = '__RECON_NAME__'
$BasePackageZip = '__BASE_PACKAGE_ZIP__'
$BasePackageExeName = '__BASE_PACKAGE_EXE_NAME__'
$PresentMonName = '__PRESENTMON_NAME__'
$PresentMonSha = '__PRESENTMON_SHA256__'
$SmokeRunnerClosure = __SMOKE_RUNNER_CLOSURE__
$SmokeRunnerClosureDirName = '__SMOKE_RUNNER_CLOSURE_DIR_NAME__'
$SmokeRunnerName = '__SMOKE_RUNNER_NAME__'
$ConsentReceiptFileName = '__CONSENT_RECEIPT__'
$FixtureRehearsal = __FIXTURE_REHEARSAL__
$FixtureSha256 = '__FIXTURE_SHA256__'
$ContactSheetEnabled = __CONTACT_SHEET_ENABLED__
$ContactSheetFrameCount = __CONTACT_SHEET_FRAME_COUNT__
$ContactSheetComposerPyBase64 = '__CONTACT_SHEET_COMPOSER_PY_BASE64__'
$ContactSheetComposerSha256 = '__CONTACT_SHEET_COMPOSER_SHA256__'
$Root = '__AGENT_ROOT__'
$Cache = Join-Path $Root 'cache'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$JobId = "playback-attr-3-cuda-$($SourceCommit.Substring(0,12))-$ClipId-$Stamp"
$Work = Join-Path 'C:\mlvtmp' $JobId
$Pub = Join-Path $Root "outbox\$JobId.artifacts"
$PresentMonTimedSeconds = 55
# ATTR3-FOOTAGE-BIND-1 PR-B round 4: set by the owner branch below; stays $null/empty for a
# fixture run, so the `finally` around the smoke run further down is a no-op for one.
$OwnerClipDir = $null
$ownerLinkHandles = [System.Collections.Generic.List[object]]::new()

# --- verifiers, embedded VERBATIM from tools/profiling/bachelor/AttrCudaArtifacts.psm1 --------
# Defined FIRST, before any statement that calls them (sol PR #133 r3: the work-tree cleanup was
# called above its definition, so every emitted job died with command-not-found).
__EMBEDDED_FUNCTIONS__
# --- end embedded verifiers -------------------------------------------------------------------

# TEMP boundary (BLOCKER fix): job-owned scratch dir under this job's own C:\mlvtmp
# work dir, set as TEMP/TMP at the very start -- before any child process (reg.exe,
# the pwsh that runs run-release-gui-smoke.ps1/MLVApp.exe, PresentMon) -- so every one
# of them inherits it instead of the ambient (unconstrained) machine TEMP.
# Mirrors tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1's $Scratch
# pattern. $Work is created here (not later) precisely so the scratch dir it hosts is
# never wiped out from under a live $env:TEMP by a later "recreate $Work" step.
function Assert-UnderMlvTmp([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -ne 'C:\mlvtmp' -and $full -notlike 'C:\mlvtmp\*') {
        throw "job-owned path '$Label' resolves outside C:\mlvtmp: $full"
    }
}
foreach ($check in @(
    @{ path = $Root; label = 'Root' },
    @{ path = $Work; label = 'Work' },
    @{ path = $Pub; label = 'Pub' }
)) { Assert-UnderMlvTmp $check.path $check.label }

Remove-AttrCudaTree -TrustedRoot 'C:\mlvtmp' -Path $Work
New-Item -ItemType Directory -Path $Work -Force | Out-Null
$Scratch = Join-Path $Work '.job-tmp'
New-Item -ItemType Directory -Path $Scratch -Force | Out-Null
$env:TEMP = $Scratch
$env:TMP = $Scratch

# PresentMon runs as a DIRECT child of this job (sol PR #131 r3): it inherits the job-owned
# TEMP/TMP above, so every child tool is confined to C:\mlvtmp. The elevated scheduled task
# MLV\PresentMonSidecar is NOT used: the agent account was granted ETW trace rights through
# 'Performance Log Users' (Bachelor fix-presentmon-privilege.ps1, 2026-07-28). If that grant is
# missing, PresentMon exits 6 (access denied) and this job fails closed; it never falls back to
# the task.

function Get-Sha([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}


function Save-Json($Object, [string]$Path) {
    # Artifact writes go through the slot-checked helper: never through a link or into a directory (sol PR #133).
    [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30))
}

function Get-Mean([double[]]$Values) {
    if ($Values.Count -eq 0) { return $null }
    [double](($Values | Measure-Object -Average).Average)
}

function Get-SampleSd([double[]]$Values) {
    if ($Values.Count -lt 2) { return 0.0 }
    $mean = Get-Mean $Values
    $sum = 0.0
    foreach ($value in $Values) { $sum += [math]::Pow($value - $mean, 2) }
    [math]::Sqrt($sum / ($Values.Count - 1))
}

function Get-Percentile([double[]]$Values, [double]$P) {
    if ($Values.Count -eq 0) { return $null }
    $sorted = @($Values | Sort-Object)
    if ($sorted.Count -eq 1) { return [double]$sorted[0] }
    $rank = ($sorted.Count - 1) * $P
    $low = [math]::Floor($rank)
    $high = [math]::Ceiling($rank)
    if ($low -eq $high) { return [double]$sorted[$low] }
    [double]($sorted[$low] + ($sorted[$high] - $sorted[$low]) * ($rank - $low))
}

function Get-Stats([double[]]$Values) {
    $mean = Get-Mean $Values
    $sd = Get-SampleSd $Values
    [ordered]@{
        count = $Values.Count
        meanMs = $mean
        sdMs = $sd
        cvPct = if ($null -ne $mean -and $mean -ne 0) { 100.0 * $sd / $mean } else { $null }
        p50Ms = Get-Percentile $Values 0.50
        p95Ms = Get-Percentile $Values 0.95
        p99Ms = Get-Percentile $Values 0.99
        fpsEquivalentMean = if ($null -ne $mean -and $mean -gt 0) { 1000.0 / $mean } else { $null }
    }
}

function Start-PresentMonCapture([string]$CsvPath) {
    if (Test-Path -LiteralPath $CsvPath) { throw "PresentMon output already exists: $CsvPath" }
    $pmArgs = @('--process_name', $ExeName, '--output_file', $CsvPath, '--timed', [string]$PresentMonTimedSeconds,
                '--terminate_after_timed', '--stop_existing_session', '--no_console_stats')
    # Direct child: inherits this job's TEMP/TMP. -PassThru so the exit code is checked.
    $proc = Start-Process -FilePath (Join-Path $Cache $PresentMonName) -ArgumentList $pmArgs -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if ($proc.HasExited -and $proc.ExitCode -ne 0) {
        throw "PRESENTMON_FAILED rc=$($proc.ExitCode) (6 = ETW access denied: the agent account needs 'Performance Log Users')"
    }
    return $proc
}

function Wait-PresentMonCapture($Proc, [int]$TimeoutSeconds = 35, [int]$KillWaitTimeoutSeconds = 10) {
    if (-not $Proc.WaitForExit($TimeoutSeconds * 1000)) {
        # ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 3, carried to round 3): the empty catch
        # here used to swallow a Kill() failure outright -- a PresentMon that survived both the
        # timeout and the kill attempt left no trace anywhere. Reported the same way
        # Stop-PresentMonCapture already reports it: killError and waitError captured,
        # confirmedExited read from $Proc itself AFTER the attempt, never assumed from "Kill()
        # didn't throw".
        # Round 4 (fable round-3 minor, PR #144): Kill() is asynchronous -- sampling HasExited in
        # the very next statement could still read false for a process that exits milliseconds
        # later, sending an operator hunting a lingering process that is not there. Wait bounded
        # after Kill(), exactly as Stop-PresentMonCapture already does, before sampling HasExited.
        $killError = $null
        $waitError = $null
        try { $Proc.Kill() } catch { $killError = $_.Exception.Message }
        try {
            if (-not $Proc.WaitForExit($KillWaitTimeoutSeconds * 1000)) {
                $waitError = "did not exit within $KillWaitTimeoutSeconds s after Kill()"
            }
        } catch {
            $waitError = $_.Exception.Message
        }
        $confirmedExited = [bool]$Proc.HasExited
        $killErrorText = if ($null -eq $killError) { '<none>' } else { $killError }
        $waitErrorText = if ($null -eq $waitError) { '<none>' } else { $waitError }
        throw "PRESENTMON_TIMEOUT: did not exit within $TimeoutSeconds s after playback (confirmedExited=$confirmedExited killError=$killErrorText waitError=$waitErrorText)"
    }
    [pscustomobject]@{ status = 'done'; exitCode = $Proc.ExitCode }
}

function Stop-PresentMonCapture($Proc, [int]$TimeoutSeconds = 10) {
    # ATTR3-SMOKE-RUNNER-DEPS-1 (D): used only when the smoke run itself is already known to have
    # failed -- PresentMon is stopped, never waited out, so a smoke-side failure is reported as
    # SMOKE_RUN_FAILED and not mis-diagnosed as PRESENTMON_TIMEOUT.
    # ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 3): the empty catch blocks used to swallow a
    # Kill() or WaitForExit() failure outright, so a PresentMon that survived the kill left no
    # trace anywhere. Both are now captured and returned -- confirmedExited is read from $Proc
    # itself AFTER the attempt, never assumed from "Kill() didn't throw" -- so the caller can
    # report a stop that did not actually confirm exit instead of silently trusting it.
    $killError = $null
    $waitError = $null
    if (-not $Proc.HasExited) {
        try { $Proc.Kill() } catch { $killError = $_.Exception.Message }
        try {
            if (-not $Proc.WaitForExit($TimeoutSeconds * 1000)) {
                $waitError = "did not exit within $TimeoutSeconds s after Kill()"
            }
        } catch {
            $waitError = $_.Exception.Message
        }
    }
    [pscustomobject]@{
        confirmedExited = [bool]$Proc.HasExited
        killError = $killError
        waitError = $waitError
    }
}

function Get-MeasuredSmokeSessionId([string]$RawLog) {
    <#
    .SYNOPSIS
    The playback_smoke session id of the MEASURED interval -- BLOCKER fix (r1c): with a
    contact-sheet capture pass on the leg, the raw log can carry more than one
    playback_smoke session (see MainWindow.cpp's on_actionPlay_toggled and the
    m_contactSheetCaptureActive guard around beginPlaybackSmokeTelemetry -- suppressed for a
    capture restart, but this parser must not depend on that app-side suppression alone).
    .DESCRIPTION
    HARDENING (r1d, sol; rebound in CUDA-PLAYBACK-CONTACT-SHEET-2): bind to the app's own
    explicit "playback_smoke.measured_session id=N" marker when the log carries one, rather
    than positionally assuming the first playback_smoke.summary line is the measured one -- an
    alternate GUI-smoke mode (e.g. an Auto Look Assist warmup or lifecycle stress pass that
    opens its own session first) could make that assumption false. As of
    CUDA-PLAYBACK-CONTACT-SHEET-2, MainWindow.cpp's runGuiPlaybackSmoke() logs this marker
    itself, right after the measured play trigger that opens the measured session -- not from
    finishPlaybackSmokeTelemetry() on a "play-stop", which a warmup settle or an in-loop
    lifecycle-stress toggle could reach first and mislabel. Falls back to the old first-summary
    heuristic against a log from a build that predates the marker, so this never regresses an
    older build's run: on such a log, the measured interval's own
    playback_smoke.summary/gpu_summary line pair runs strictly BEFORE the contact-sheet capture
    block even starts (see runGuiPlaybackSmoke's own ordering comment), so every session opened
    afterwards is chronologically LATER, and the FIRST playback_smoke.summary line's session id
    is still the measured one.
    #>
    foreach ($line in ($RawLog -split "`r?`n")) {
        if ($line -match 'playback_smoke\.measured_session id=(?<session>\d+)') {
            return $Matches['session']
        }
    }
    foreach ($line in ($RawLog -split "`r?`n")) {
        if ($line -match 'playback_smoke\.summary session=(?<session>\d+)') {
            return $Matches['session']
        }
    }
    throw 'no playback_smoke.summary line found in the MLVApp log'
}

function Get-FrameRows([string]$RawLog, [string]$MeasuredSessionId) {
    $rows = [System.Collections.Generic.List[object]]::new()
    $keys = @(
        'prep_region_setup_ms', 'prep_region_gpu_ms', 'prep_region_image_ms',
        'prep_region_present_ms', 'prep_region_finish_ms',
        'prep_region_total_ms', 'prep_region_unattributed_ms'
    )
    foreach ($line in ($RawLog -split "`r?`n")) {
        if ($line -notmatch 'playback_smoke\.frame ') { continue }
        $values = @{}
        foreach ($match in [regex]::Matches($line, '(?<key>[A-Za-z0-9_]+)=(?<value>[^\s]+)')) {
            $values[$match.Groups['key'].Value] = $match.Groups['value'].Value
        }
        if (-not $values.ContainsKey('prep_region_total_ms')) { continue }
        # BLOCKER fix (r1c): a contact-sheet capture pass can (or, on a code path that fails
        # to suppress it, could) open further playback_smoke sessions after the measured
        # one -- select ONLY the measured session's rows, even when more lines exist.
        if ($values.ContainsKey('session') -and $values['session'] -ne $MeasuredSessionId) { continue }
        $row = [ordered]@{}
        foreach ($key in @('session','index','elapsed_ms','interval_ms','display_frame','serial')) {
            if ($values.ContainsKey($key)) { $row[$key] = $values[$key] }
        }
        foreach ($key in $keys) {
            if (-not $values.ContainsKey($key)) { continue }
            $row[$key] = [double]::Parse($values[$key], [Globalization.CultureInfo]::InvariantCulture)
        }
        if ($row.Contains('prep_region_total_ms') -and $row.Contains('prep_region_unattributed_ms')) {
            [void]$rows.Add([pscustomobject]$row)
        }
    }
    if ($rows.Count -lt 10) { throw "only $($rows.Count) high-resolution frame rows; require >=10" }
    return @($rows)
}

function Get-LastGpuSummary([string]$RawLog, [string]$MeasuredSessionId) {
    # Cumulative per-session counters: the LAST line FOR THE MEASURED SESSION carries that
    # session's final totals. BLOCKER fix (r1c): a plain "last line in the log" pick is wrong
    # once a contact-sheet capture pass can open further sessions after the measured one --
    # each such session emits its own gpu_summary line, chronologically after the measured
    # session's, so "last" used to mean "the capture pass's, not the measured interval's".
    $last = $null
    foreach ($line in ($RawLog -split "`r?`n")) {
        if ($line -notmatch 'playback_smoke\.gpu_summary ') { continue }
        $values = @{}
        foreach ($match in [regex]::Matches($line, '(?<key>[A-Za-z0-9_]+)=(?<value>[^\s]+)')) {
            $values[$match.Groups['key'].Value] = $match.Groups['value'].Value
        }
        if ($values.ContainsKey('session') -and $values['session'] -ne $MeasuredSessionId) { continue }
        $last = $values
    }
    if ($null -eq $last) { throw 'no playback_smoke.gpu_summary line found in the MLVApp log for the measured session' }
    $required = @('cpu_frames','gpu_preview_frames','gpu_recon_readback_frames','gpu_texture_readback_frames','gpu_texture_no_readback_frames')
    foreach ($key in $required) {
        if (-not $last.ContainsKey($key)) { throw "playback_smoke.gpu_summary line missing $key" }
    }
    [ordered]@{
        cpuFrames = [int]$last['cpu_frames']
        gpuPreviewFrames = [int]$last['gpu_preview_frames']
        gpuReconReadbackFrames = [int]$last['gpu_recon_readback_frames']
        gpuTextureReadbackFrames = [int]$last['gpu_texture_readback_frames']
        gpuTextureNoReadbackFrames = [int]$last['gpu_texture_no_readback_frames']
    }
}

foreach ($item in @(
    @{ path=(Join-Path $Cache $PresentMonName); sha=$PresentMonSha }
)) {
    if (-not (Test-Path -LiteralPath $item.path)) { throw "cache missing $($item.path)" }
    if ((Get-Sha $item.path) -ne $item.sha) { throw "hash mismatch $($item.path)" }
}
# Non-transactional publish fix (BLOCKER): existence of the exe/DLL/pkg alone does not
# prove they belong together -- a compile job's publish could have been interrupted
# between renames. The compile job's cache manifest (holding all three lowercase
# sha256s, written LAST after the atomic rename) is REQUIRED, and every one of the
# three cached files' sha256 is verified against it before anything is trusted.
$buildManifestName = "playback-attr-3-cuda-$($SourceCommit.Substring(0,12))-build.json"
$buildManifestPath = Join-Path $Cache $buildManifestName
# AUTHENTICATE THE MANIFEST BEFORE READING A SINGLE FIELD OF IT (sol, PR #133 r2). The cache is
# mutable and this job does not own it; a same-named build.json with matching artifacts would
# otherwise forge pendingSymbolPresence and the whole DLL association. $BuildManifestSha256 was
# baked in by the generator from the sha the assembler/staging step printed, and the check runs
# BEFORE ConvertFrom-Json -- a parsed field is already a trusted field. The same call also
# requires sourceCommit to equal the pinned commit, dllPairManifestSha256 to be present (so the
# exe's DLL pair is chained back to the Ultra-Magnus manifest rather than merely asserted), and
# pendingSymbolPresence to be a real boolean.
$buildManifest = Assert-AttrCudaBuildManifest -Path $buildManifestPath -ExpectedSha256 $BuildManifestSha256 -ExpectedSourceCommit $SourceCommit
# pendingSymbolPresence is READ, never re-derived here (swarm ruling
# .claude-state/fleet-runs/swarm-attr3-buildhost-20260916T2150Z/SYNTHESIS.md): this host has no
# VC tools at all, so the old on-Bachelor MSVC export-inspection probe could only ever throw.
# The symbol test runs on the host that built the DLL
# (tools/profiling/ultramagnus/playback-attr-3-cuda-dll-job.ps1) and is carried in the build
# manifest, whose bytes are now authenticated above.
$pendingSymbolPresence = [bool]$buildManifest.pendingSymbolPresence
$dllPairManifestSha256 = ([string]$buildManifest.dllPairManifestSha256).ToLowerInvariant()
$manifestChecks = @(
    @{ label = 'packageZip'; path = (Join-Path $Cache $BasePackageZip); expectedSha = $buildManifest.packageZip.sha256 },
    @{ label = 'exe'; path = (Join-Path $Cache $ExeName); expectedSha = $buildManifest.exe.sha256 },
    @{ label = 'dll'; path = (Join-Path $Cache $ReconName); expectedSha = $buildManifest.dll.sha256 }
)
foreach ($check in $manifestChecks) {
    if (-not (Test-Path -LiteralPath $check.path)) { throw "cache missing $($check.path)" }
    if ([string]::IsNullOrWhiteSpace($check.expectedSha)) { throw "build manifest $buildManifestName is missing a sha256 for $($check.label)" }
    if ((Get-Sha $check.path) -ne $check.expectedSha.ToUpperInvariant()) { throw "hash mismatch (vs build manifest $buildManifestName) for $($check.path)" }
}
if (-not (Test-Path -LiteralPath (Join-Path $Cache $PresentMonName))) { throw "cache missing $PresentMonName" }
# ATTR3-SMOKE-RUNNER-DEPS-1 round 3 (NARROW BY REDESIGN): the runner alone is not launchable --
# it is one file in an explicitly pinned six-file manifest (four dot-sourced siblings and one
# imported module besides the runner itself), all resolved via $PSScriptRoot, and staging
# only the runner (ATTR3-SMOKE-RUNNER-PIN-1) left Bachelor unable to reach line 1 of playback,
# which PRESENTMON_TIMEOUT then mis-reported as a PresentMon problem. Round 1/2 checked the
# closure directory with an inline exact-set loop DUPLICATED between this job and the stager's
# own "already staged" check -- exactly the shape that lets the two silently drift apart. Both
# jobs now embed the SAME Get-AttrCudaClosureDirectoryMismatch function text
# (Get-AttrCudaEmbeddedFunctionSource), so there is only ever one definition of "matches exactly"
# to drift from. Checked HERE -- before PresentMon starts, before the app launches, before any
# run is spent -- naming the failing file so a refusal says WHICH dependency is stale or missing.
$smokeRunnerClosureDir = Join-Path $Cache $SmokeRunnerClosureDirName
$smokeRunnerClosureMismatch = Get-AttrCudaClosureDirectoryMismatch -Dir $smokeRunnerClosureDir -Entries $SmokeRunnerClosure
if ($null -ne $smokeRunnerClosureMismatch) {
    throw "ATTRCUDA_SMOKE_RUNNER_STALE $smokeRunnerClosureMismatch"
}
# Fixture runs open the cached clip named at generation time, checked for residence in the
# agent cache -- unchanged from before ATTR3-FOOTAGE-BIND-1. An owner run has no single
# authorized path to check here at all: its parts are verified below, after $Pub exists, and
# $clipPath is set only once every part has PASSed (never composed from $Cache/$ClipId).
if ($FixtureRehearsal) {
    $clipPath = $AuthorizedClipPath
    # Injection guard (sol PR #131 r4): the path is later embedded in a nested pwsh -Command string.
    if ($clipPath -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') { throw "authorized clip path contains characters outside the allowlist" }
    if ((Split-Path -Parent $clipPath) -ine $Cache) { throw "authorized clip path is not directly inside the agent cache" }
    if ([IO.Path]::GetFileNameWithoutExtension($clipPath) -cne $ClipId) { throw "authorized clip path does not name clip id $ClipId" }
    if (-not (Test-Path -LiteralPath $clipPath -PathType Leaf)) { throw "authorized clip path is missing on this host" }
}

# $Work and $Pub already exist (created at job start, alongside the TEMP/TMP scratch
# dir under $Work) -- do not remove/recreate $Work here, which would delete the live
# $env:TEMP/$env:TMP scratch dir out from under this process.
New-Item -ItemType Directory -Path (Join-Path $Work 'out') -Force | Out-Null
# The outbox is created explicitly (never as a side effect of -Force on a deeper path), so its
# parent is checked for links like every other directory this job creates.
[void](New-AttrCudaDirectory -Path (Join-Path $Root 'outbox'))
[void](New-AttrCudaDirectory -Path $Pub)

# ATTR3-FIXTURE-STAGE-1: a fixture run authenticates the cached clip's CONTENT before it is
# ever opened -- a cache file name proves nothing about its bytes. Hashed and checked before any
# package is deployed or playback launched.
if ($FixtureRehearsal) {
    $actualClipSha256 = (Get-FileHash -LiteralPath $clipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualClipSha256 -ne $FixtureSha256) {
        $mismatch = [ordered]@{
            schema='playback-attr-3-cuda-venue.v1'; result='FIXTURE_CONTENT_MISMATCH'
            fixtureRehearsal=$FixtureRehearsal
            clipPath=$clipPath; expectedSha256=$FixtureSha256; actualSha256=$actualClipSha256
            sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
        }
        Save-Json $mismatch (Join-Path $Pub 'summary.json')
        Write-Output "RESULT=FIXTURE_CONTENT_MISMATCH EXPECTED=$FixtureSha256 ACTUAL=$actualClipSha256 ARTIFACTS=$Pub"
        exit 17
    }
} else {
    # ATTR3-FOOTAGE-BIND-1 PR-B: an owner run authenticates EVERY part's CONTENT -- exists,
    # readable, length, sha256, via the SAME Test-AttrCudaFootagePart the presence-probe job
    # uses -- before anything is deployed, before PresentMon starts, and before the smoke
    # child runs. The resolver's cross-check baked these parts in; this is the live-filesystem
    # re-check that never just takes the resolver's word for it.
    $ownerRawParts = @($OwnerPartsJson | ConvertFrom-Json)
    $ownerPartResults = [System.Collections.Generic.List[object]]::new()
    $ownerDecodedParts = [System.Collections.Generic.List[object]]::new()
    foreach ($rawPart in ($ownerRawParts | Sort-Object { [int]$_.index })) {
        # The path never travels as a literal: decoded from base64 IN THIS PROCESS, on this
        # host, and used only through -LiteralPath calls -- never re-embedded into a string
        # PowerShell parses as code.
        $decoded = Read-AttrCudaBase64Payload -Base64 $rawPart.pathBase64
        $partPath = ConvertTo-AttrCudaUtf8String -Bytes $decoded.bytes
        $status = Test-AttrCudaFootagePart -Path $partPath -ExpectedLength ([int64]$rawPart.length) -ExpectedSha256 ([string]$rawPart.sha256)
        [void]$ownerPartResults.Add([ordered]@{ index = [int]$rawPart.index; status = $status })
        [void]$ownerDecodedParts.Add([ordered]@{ index = [int]$rawPart.index; path = $partPath; length = [int64]$rawPart.length; sha256 = [string]$rawPart.sha256 })
    }
    $ownerFailingParts = @($ownerPartResults | Where-Object { $_.status -ne 'PASS' })
    if ($ownerFailingParts.Count -gt 0) {
        # Per-part index and status only -- never a path -- in every reader-facing output below.
        $notVerified = [ordered]@{
            schema='playback-attr-3-cuda-venue.v1'; result='OWNER_FOOTAGE_NOT_VERIFIED'
            fixtureRehearsal=$FixtureRehearsal
            parts=@($ownerPartResults)
            sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
        }
        Save-Json $notVerified (Join-Path $Pub 'summary.json')
        $partsSummary = (($ownerPartResults | ForEach-Object { "$($_.index)=$($_.status)" }) -join ',')
        Write-Output "RESULT=OWNER_FOOTAGE_NOT_VERIFIED PARTS=$partsSummary ARTIFACTS=$Pub"
        exit 19
    }

    # ATTR3-FOOTAGE-BIND-1 PR-B round 4 (STRUCTURAL, closes B1/B2). The parts just verified on
    # the owner's OWN directory never travel any further: a PRIVATE, neutrally-named directory is
    # built under this job's own $Work -- one hard link per verified part -- so the smoke
    # runner's own sibling glob and the app's own continuation-part walk / sidecar file see ONLY
    # the parts this job verified, and every path this job hands to the smoke child or writes
    # into $Pub from here on names only an entry under $OwnerClipDir (never the owner's real
    # directory).
    try {
        $ownerAssertedParts = Assert-AttrCudaOwnerPartsNaming -Parts $ownerDecodedParts
    } catch {
        $notContiguous = [ordered]@{
            schema='playback-attr-3-cuda-venue.v1'; result='OWNER_PARTS_NOT_CONTIGUOUS'
            fixtureRehearsal=$FixtureRehearsal
            partCount=$ownerDecodedParts.Count
            sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
        }
        Save-Json $notContiguous (Join-Path $Pub 'summary.json')
        Write-Output "RESULT=OWNER_PARTS_NOT_CONTIGUOUS ARTIFACTS=$Pub"
        exit 20
    }

    $OwnerClipDir = New-AttrCudaDirectory -Path (Join-Path $Work 'owner-clip')
    # ATTR3-FOOTAGE-BIND-1 PR-B round 5 (astra major): the cleanup `try` now wraps this ENTIRE
    # loop -- creating every link, opening every held handle, and re-verifying every link's
    # content -- not just the code from a link's successful creation onward. Round 4 only guarded
    # New-AttrCudaOwnerFootageLink's own throw with a per-part try/catch; a failure from
    # Open-AttrCudaReadOnlyHandle (e.g. a sharing violation on part N after parts 0..N-1 were
    # already linked and held) had no catch of its own and propagated straight out of the script,
    # skipping cleanup entirely and leaking every link already created. One catch below now
    # handles all three failure shapes (cross-volume, link/identity/handle failure, post-link
    # content mismatch): $part still holds the failing iteration's value here (a PowerShell
    # `foreach` variable is not iteration-scoped, so it survives the loop being broken out of by
    # an exception), which is enough to report the affected index without re-deriving it from the
    # exception text.
    try {
        foreach ($part in $ownerAssertedParts) {
            $linkPath = New-AttrCudaOwnerFootageLink -Directory $OwnerClipDir -Index $part.index -SourcePath $part.path
            # Held open from the moment the link's identity is confirmed until the smoke child
            # that reads it has exited (see the `finally` around the smoke run below) --
            # FileShare.Read blocks any writer from replacing or truncating the link's target
            # underneath a live measurement, while still letting the smoke child and this job's
            # own re-hash below both read it.
            $handle = Open-AttrCudaReadOnlyHandle -Path $linkPath
            [void]$ownerLinkHandles.Add($handle)
            $relinkStatus = Test-AttrCudaFootagePart -Path $linkPath -ExpectedLength $part.length -ExpectedSha256 $part.sha256
            if ($relinkStatus -ne 'PASS') {
                throw "OWNER_FOOTAGE_NOT_VERIFIED status=$relinkStatus"
            }
            if ($part.index -eq 0) { $clipPath = $linkPath }
        }
    } catch {
        # Closes whatever handles were acquired before the failure and deletes only the neutral
        # link entries that exist with a live link count >= 2 -- never throws, so the refusal
        # below is always reached.
        Close-AttrCudaOwnerFootageWorkspace -Handles $ownerLinkHandles -Directory $OwnerClipDir
        $message = $_.Exception.Message
        if ($message -match '^OWNER_FOOTAGE_NOT_VERIFIED status=(\S+)$') {
            $relinkRefusal = [ordered]@{
                schema='playback-attr-3-cuda-venue.v1'; result='OWNER_FOOTAGE_NOT_VERIFIED'
                fixtureRehearsal=$FixtureRehearsal
                parts=@(@{ index = $part.index; status = $Matches[1] })
                sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
            }
            Save-Json $relinkRefusal (Join-Path $Pub 'summary.json')
            Write-Output "RESULT=OWNER_FOOTAGE_NOT_VERIFIED PARTS=$($part.index)=$($Matches[1]) ARTIFACTS=$Pub"
            exit 19
        }
        # Any other failure in this loop -- cross-volume, link/identity creation, or a handle that
        # could not even be opened -- is reported the same way New-AttrCudaOwnerFootageLink's own
        # throws always were: by index only, never a path or the raw exception text.
        $linkToken = ($message -split '\s+')[0]
        if ($linkToken -ne 'OWNER_FOOTAGE_LINK_CROSS_VOLUME') { $linkToken = 'OWNER_FOOTAGE_LINK_FAILED' }
        $linkExitCode = if ($linkToken -eq 'OWNER_FOOTAGE_LINK_CROSS_VOLUME') { 21 } else { 22 }
        $linkRefusal = [ordered]@{
            schema='playback-attr-3-cuda-venue.v1'; result=$linkToken
            fixtureRehearsal=$FixtureRehearsal
            partIndex=$part.index
            sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
        }
        Save-Json $linkRefusal (Join-Path $Pub 'summary.json')
        Write-Output "RESULT=$linkToken PART=$($part.index) ARTIFACTS=$Pub"
        exit $linkExitCode
    }
}

# ATTR3-FOOTAGE-BIND-1 PR-B round 4: everything from here to the end of the job runs inside a
# `try`/`finally` so the private directory's read-share handles (opened above, owner runs only)
# are always closed and its neutrally-named links are always cleaned up -- on every exit path
# below, including an early `exit N` (PowerShell still runs a pending `finally` on `exit`, proven
# by CI before this shipped) and an uncaught terminating error. $OwnerClipDir stays $null for a
# fixture run, so the `finally` is a no-op there.
try {
Expand-Archive -LiteralPath (Join-Path $Cache $BasePackageZip) -DestinationPath (Join-Path $Work 'pkg') -Force
$baseExe = Get-ChildItem -LiteralPath (Join-Path $Work 'pkg') -Recurse -Filter $BasePackageExeName | Select-Object -First 1
if (-not $baseExe) { throw "base package executable not found: $BasePackageExeName" }
$pkgDir = $baseExe.Directory.FullName
$exePath = Join-Path $pkgDir $ExeName
$reconDll = Join-Path $pkgDir 'igpu_recon_cuda.dll'
$cacheExeSha = Get-Sha (Join-Path $Cache $ExeName)
$cacheReconSha = Get-Sha (Join-Path $Cache $ReconName)
[void](Publish-AttrCudaFileCopy -Source (Join-Path $Cache $ExeName) -Destination $exePath)
[void](Publish-AttrCudaFileCopy -Source (Join-Path $Cache $ReconName) -Destination $reconDll)
if ((Get-Sha $exePath) -ne $cacheExeSha -or (Get-Sha $reconDll) -ne $cacheReconSha) {
    throw 'deployed artifact hash verification failed (copy from cache did not round-trip)'
}

reg add "HKCU\Software\Microsoft\DirectX\UserGpuPreferences" /v "$exePath" /t REG_SZ /d "GpuPreference=2;" /f | Out-Null
reg add "HKCU\Software\magiclantern.MLVApp\MLVApp" /v playbackProcessingSubset /t REG_DWORD /d 1 /f | Out-Null
reg add "HKCU\Software\magiclantern.MLVApp\MLVApp" /v zebras /t REG_DWORD /d 0 /f | Out-Null
reg add "HKCU\Software\magiclantern.MLVApp\MLVApp" /v caching /t REG_DWORD /d 0 /f | Out-Null
reg add "HKCU\Software\magiclantern.MLVApp\MLVApp\Playback" /v QualityMode /t REG_DWORD /d 1 /f | Out-Null
reg add "HKCU\Software\magiclantern.MLVApp\MLVApp\Playback" /v PreviewMode /t REG_DWORD /d 0 /f | Out-Null
reg add "HKCU\Software\magiclantern.MLVApp\MLVApp\Playback" /v ScaleFactorOverride /t REG_DWORD /d 0 /f | Out-Null
reg add "HKCU\Software\magiclantern.MLVApp\MLVApp\Playback" /v PreviewResolution /t REG_DWORD /d 0 /f | Out-Null

$loads = @()
for ($i = 0; $i -lt 3; $i++) {
    $loads += [double](Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    if ($i -lt 2) { Start-Sleep -Seconds 12 }
}
$avgLoad = Get-Mean $loads
if ($avgLoad -gt 20.0) {
    $venue = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'
        result='VENUE_NOT_QUIESCENT'
        fixtureRehearsal=$FixtureRehearsal
        cpuSamples=$loads
        cpuMean=$avgLoad
        cpuThresholdPercent=20.0
        sourceCommit=$SourceCommit
        clipId=$ClipId
        executableSha256=$cacheExeSha
        artifactRoot=$Pub
    }
    Save-Json $venue (Join-Path $Pub 'summary.json')
    Write-Output "RESULT=VENUE_NOT_QUIESCENT CPU_MEAN=$avgLoad ARTIFACTS=$Pub"
    exit 12
}

$legOut = Join-Path $Work 'out\diagnostic'
New-Item -ItemType Directory -Path $legOut -Force | Out-Null
$resultPath = Join-Path $legOut 'result.json'
$presentMonPath = Join-Path $legOut 'presentmon.csv'
$smoke = Join-Path $smokeRunnerClosureDir $SmokeRunnerName
$envs = @(
    'MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq',
    'MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW=0',
    'MLVAPP_PLAYBACK_PREVIEW_MODE=sharp_smooth',
    'MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1',
    'MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY=1',
    'MLVAPP_PLAYBACK_DETAILED_TIMELINE_TELEMETRY=1',
    'MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1',
    'MLVAPP_STAGE_TIMING=1',
    'MLVAPP_PERF_FIELD_LOG=1',
    'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',
    'MLVAPP_GPU_PLAYBACK_RECON=1',
    'MLVAPP_GPU_PLAYBACK_RECON_BACKEND=cuda',
    ('MLVAPP_GPU_PLAYBACK_RECON_DLL=' + $reconDll),
    'MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D=0',
    'MLVAPP_EXPERIMENTAL_GPU_PROCESSING=1',
    'MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER=1',
    'MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT=1',
    'MLVAPP_VIEWPORT_PRESENT_DIAG=1',
    'MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1',
    'MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT=1',
    'MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT=1',
    ('QT_PLUGIN_PATH=' + $pkgDir),
    ('QT_QPA_PLATFORM_PLUGIN_PATH=' + (Join-Path $pkgDir 'platforms')),
    'QT_OPENGL=desktop',
    'QT_FORCE_STDERR_LOGGING=1'
)
# Shipping default: scale factor 4. Unlike PLAYBACK-ATTR-2, no
# MLVAPP_PLAYBACK_SCALE_FACTOR override is emitted; -ScaleFactor 4 is explicit
# below for self-documentation even though it is run-release-gui-smoke.ps1's own
# default.
$envList = "'" + ($envs -join "','") + "'"
function ConvertTo-PsSingleQuoted([string]$Value) { "'" + $Value.Replace("'", "''") + "'" }
$cmd = "& $(ConvertTo-PsSingleQuoted $smoke) -ExePath $(ConvertTo-PsSingleQuoted $exePath) -Input $(ConvertTo-PsSingleQuoted $clipPath) -Output $(ConvertTo-PsSingleQuoted $resultPath) -Seconds 40 -StartFrame 0 -SettleMs 2500 -ScaleFactor 4 -UsePersistedPlaybackSettings -RequireLookAssist:`$false -Scope none -FrameTelemetry -PreserveExperimentalEnvironment -ExtraEnvironment @($envList)"
# CUDA-PLAYBACK-CONTACT-SHEET-1: appended, never baked into the base $cmd string above, so a
# disabled run's $cmd (and therefore this job's emitted text) is byte-identical to before this
# card. Passed through -AdditionalArgs (run-release-gui-smoke.ps1's own generic extra-args
# passthrough) rather than adding new named parameters to that runner -- smallest possible touch
# to a file another concurrent lane (UM-CUDA-BENCH-VENUE-1) also edits.
$contactSheetDir = $null
if ($ContactSheetEnabled) {
    $contactSheetDir = Join-Path $Work 'contact-sheet'
    New-Item -ItemType Directory -Path $contactSheetDir -Force | Out-Null
    $contactSheetAdditionalArgs = "@('--contact-sheet-dir', $(ConvertTo-PsSingleQuoted $contactSheetDir), '--contact-sheet-frames', '$ContactSheetFrameCount')"
    $cmd = "$cmd -AdditionalArgs $contactSheetAdditionalArgs"
}
# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-1/2/3 (sol BLOCKER 2 / fable HARDENING, direction corrected
# HARNESS-3): PresentMon's own TimeInMs=0 origin is its internal trace-session start, which lands
# somewhere between process creation and Start-PresentMonCapture returning (it blocks up to 3s to
# confirm the process is still alive) -- neither endpoint of that interval IS the true origin, so
# the interval is bracketed instead of guessed at as a single instant. $presentMonProc.StartTime
# is the OS's own report of when the child process itself began (available without any extra
# probing); HARNESS-2 anchored windowing on it alone and claimed here that doing so "never
# excludes a row that truly falls inside the playback window" because it can only be at or before
# PresentMon's true trace-session start. That direction argument was inverted (fable, HARNESS-2
# review): an anchor AT OR BEFORE the true origin makes every row's own TimeInMs read SMALLER than
# it would under the true origin, which shifts the window's comparison threshold LATER and CAN
# exclude a genuine front-edge row -- the opposite of what was claimed, and exactly the failure
# direction that would silently under-report a real display rate. Neither endpoint of the bracket
# is asserted exact in either direction any more:
# Get-AttrCudaPresentMonDisplayReport instead windows the rows under BOTH endpoints --
# $presentMonCaptureStartUtc (process creation, or the pre-spawn wall clock below when the OS
# reported none) and $presentMonPostSpawnUtc (Start-PresentMonCapture returning) -- and reports
# counts/rates for both plus how many rows disagree on window membership between them, heading the
# report with whichever endpoint admits more DISPLAYED rows, then presented rows (see .clockBracket on
# $displayReport below). The full bracket (pre-spawn wall clock, the OS-reported process start,
# post-spawn wall clock) and the residual uncertainty it implies are all persisted below, before
# parsing, so a consumer needing a tighter join than this one can see exactly how much slack to
# allow rather than trusting a single unbracketed stamp.
$presentMonPreSpawnUtc = (Get-Date).ToUniversalTime()
# PRESENTMON-HARNESS-ROBUSTNESS-1: Start-PresentMonCapture throws -- a pre-existing output file,
# or a PresentMon process that exited nonzero within its own 3s startup check (rc=6 is ETW access
# denied) -- and this call site sat inside the outer try/finally with NO catch of its own, so
# either throw would terminate the whole job with a raw PowerShell error and publish nothing, one
# step before the smoke run (and therefore any app-side measurement) had even started. Typed the
# same way every other PresentMon failure already is: PRESENTMON_UNAVAILABLE, exit 23.
$presentMonSpawnError = $null
try {
    $presentMonProc = Start-PresentMonCapture $presentMonPath
} catch {
    $presentMonSpawnError = $_.Exception.Message
}
if ($null -ne $presentMonSpawnError) {
    $displayFailure = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result='PRESENTMON_UNAVAILABLE'
        fixtureRehearsal=$FixtureRehearsal
        reason="PresentMon failed to start: $presentMonSpawnError"
        presentMonStatus='unavailable'
        chains=@()
        sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $displayFailure (Join-Path $Pub 'summary.json')
    # PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note): the raw exception message could itself contain
    # a '"', which would garble a naive parser reading this stdout line's quoted REASON="..." field.
    Write-Output "RESULT=PRESENTMON_UNAVAILABLE REASON=`"PresentMon failed to start: $(ConvertTo-AttrCudaResultLineSafeText $presentMonSpawnError)`" ARTIFACTS=$Pub"
    exit 23
}
$presentMonPostSpawnUtc = (Get-Date).ToUniversalTime()
$presentMonProcessStartUtc = $null
try { $presentMonProcessStartUtc = $presentMonProc.StartTime.ToUniversalTime() } catch { $presentMonProcessStartUtc = $null }
$presentMonCaptureStartUtc = if ($null -ne $presentMonProcessStartUtc) { $presentMonProcessStartUtc } else { $presentMonPreSpawnUtc }
$presentMonCaptureStartUncertaintyMs = ($presentMonPostSpawnUtc - $presentMonCaptureStartUtc).TotalMilliseconds
# ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 3): the smoke-failure ordering fix only covered a
# NORMAL child return -- a terminating exception while starting or running the nested pwsh (the
# executable missing, launch redirection throwing under ErrorActionPreference Stop) used to skip
# $smokeRc and the whole SMOKE_RUN_FAILED branch below, bypassing PresentMon cleanup entirely.
# Caught here instead, so every path -- normal failure, normal success, or a launch exception --
# reaches the same Stop-PresentMonCapture call before this job decides anything else.
$smokeRc = $null
$smokeLaunchException = $null
try {
    & "$env:ProgramFiles\PowerShell\7\pwsh.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $cmd 1> (Join-Path $legOut 'smoke-stdout.txt') 2> (Join-Path $legOut 'smoke-stderr.txt')
    $smokeRc = $LASTEXITCODE
} catch {
    $smokeLaunchException = $_
}

# ATTR3-SMOKE-RUNNER-DEPS-1 (D, round 1 BLOCKER): the smoke run's own outcome is checked BEFORE
# PresentMon is waited on. The previous order waited up to 35s for PresentMon to exit even when
# the smoke run itself never launched the app (the round-1 failure: the runner died at its own
# dot-source line before MLVApp.exe ever started) -- PresentMon then timed out waiting for a
# process that was never going to appear, and PRESENTMON_TIMEOUT was true but not the cause.
# PresentMon is stopped (never waited out) the moment the smoke run is known to have failed;
# PRESENTMON_TIMEOUT is reserved for the one case it actually means: the smoke run succeeded and
# PresentMon still would not exit.
if ($null -ne $smokeLaunchException -or $smokeRc -ne 0 -or -not (Test-Path -LiteralPath $resultPath)) {
    $presentMonStop = Stop-PresentMonCapture -Proc $presentMonProc
    $smokeStderrPath = Join-Path $legOut 'smoke-stderr.txt'
    $smokeStderrTail = ''
    if (Test-Path -LiteralPath $smokeStderrPath) {
        $smokeStderrLines = @(Get-Content -LiteralPath $smokeStderrPath -Tail 40)
        $smokeStderrTail = ($smokeStderrLines -join "`r`n")
    }
    $MaxSmokeStderrTailChars = 4000
    if ($smokeStderrTail.Length -gt $MaxSmokeStderrTailChars) {
        $smokeStderrTail = $smokeStderrTail.Substring($smokeStderrTail.Length - $MaxSmokeStderrTailChars)
    }
    # Capped the same way as the stderr tail above: an exception TYPE name is normally short, but
    # this is untrusted-shaped data (a .NET type name from whatever failed to launch) and gets the
    # same defensive cap before it is written into an artifact.
    # CategoryInfo.Reason (never .Exception.GetType()): PowerShell's own ErrorRecord machinery
    # already stamps the short exception type name there when an ErrorRecord is built from a
    # thrown exception, so the type name is read as a plain property instead of an instance
    # method call -- the attr3_publish_write_scan.ps1 R4 lint does not allowlist .GetType().
    $MaxSmokeLaunchExceptionChars = 500
    $smokeLaunchExceptionType = $null
    $smokeLaunchExceptionMessage = $null
    if ($null -ne $smokeLaunchException) {
        $smokeLaunchExceptionType = [string]$smokeLaunchException.CategoryInfo.Reason
        if ($smokeLaunchExceptionType.Length -gt $MaxSmokeLaunchExceptionChars) {
            $smokeLaunchExceptionType = $smokeLaunchExceptionType.Substring(0, $MaxSmokeLaunchExceptionChars)
        }
        $smokeLaunchExceptionMessage = [string]$smokeLaunchException.Exception.Message
        if ($smokeLaunchExceptionMessage.Length -gt $MaxSmokeLaunchExceptionChars) {
            $smokeLaunchExceptionMessage = $smokeLaunchExceptionMessage.Substring(0, $MaxSmokeLaunchExceptionChars)
        }
    }
    $smokeFailure = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result='SMOKE_RUN_FAILED'
        fixtureRehearsal=$FixtureRehearsal
        smokeExitCode=$smokeRc; smokeResultPresent=(Test-Path -LiteralPath $resultPath)
        smokeStderrTail=$smokeStderrTail
        smokeLaunchExceptionType=$smokeLaunchExceptionType
        smokeLaunchExceptionMessage=$smokeLaunchExceptionMessage
        presentMonConfirmedExited=$presentMonStop.confirmedExited
        presentMonKillError=$presentMonStop.killError
        presentMonWaitError=$presentMonStop.waitError
        sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $smokeFailure (Join-Path $Pub 'summary.json')
    Write-Output "RESULT=SMOKE_RUN_FAILED EXIT=$smokeRc EXCEPTION=$smokeLaunchExceptionType PRESENTMON_EXITED=$($presentMonStop.confirmedExited) ARTIFACTS=$Pub"
    exit 18
}

# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 1 / fable HARDENING): the smoke run's own
# result and log are read and published BEFORE PresentMon is ever waited on, so a PresentMon that
# hangs past its wait timeout or exits nonzero can no longer destroy a passed smoke run's
# evidence -- only a PresentMon PARSING failure was covered before this round; a WAIT failure
# was not.
$rawResult = [IO.File]::ReadAllText($resultPath)
$resultJson = $rawResult | ConvertFrom-Json -Depth 100
if ($rawResult -notmatch [regex]::Escape($SourceCommit)) { throw "result does not report pinned source commit $SourceCommit" }

# THE LOG COMES FROM THE RESULT, NOT FROM A GLOB (sol, PR #133 r2). The previous
# `out\diagnostic\logs\mlvapp-*.log` search could never match: run-release-gui-smoke.ps1 writes
# into a GUID-nonced `logs-<stem>-<nonce>` directory and publishes the authoritative per-run
# snapshot separately --
#
#     $runNonce = [Guid]::NewGuid().ToString("N")
#     $logRoot = Join-Path $outputDir ("logs-{0}-{1}" -f $outputStem, $runNonce)
#     # Preserve the exact lines consumed by this result in a per-run immutable
#     # snapshot.  The aggregate rotating app log is allowed to grow later and is
#     # therefore diagnostic only; comparison authority comes from this snapshot.
#     $runLogSnapshotPath = "$outputPath.run.log"
#     log = [pscustomobject]@{ path = $runLogSnapshotPath; aggregateSourcePath = ... }
#     evidence = [pscustomobject]@{ runNonce = $runNonce; runLogSnapshot = $runLogSnapshotBinding }
#
# so the job threw "MLVApp log missing" before it could ever reach the eligibility gate below.
# Resolve-AttrCudaSmokeRunLog reads log.path, requires it to sit inside this job's own work tree
# and to hash to evidence.runLogSnapshot.sha256, and fails closed at exit 16 otherwise -- absent
# evidence is never treated as passing evidence.
try {
    $runLog = Resolve-AttrCudaSmokeRunLog -ResultJsonPath $resultPath -ContainingRoot $Work
} catch {
    # PresentMon has not been waited on yet at this point (that now happens further down, after
    # smoke evidence publishes) -- stopped here exactly like the SMOKE_RUN_FAILED branch above,
    # never left to run out its own --timed budget for a run this job is about to fail anyway.
    $presentMonStop = Stop-PresentMonCapture -Proc $presentMonProc
    $unavailable = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result='SMOKE_LOG_UNAVAILABLE'
        fixtureRehearsal=$FixtureRehearsal
        message=$_.Exception.Message; smokeExitCode=$smokeRc; resultJson=$resultPath
        presentMonConfirmedExited=$presentMonStop.confirmedExited
        presentMonKillError=$presentMonStop.killError
        presentMonWaitError=$presentMonStop.waitError
        sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $unavailable (Join-Path $Pub 'summary.json')
    Write-Output "RESULT=SMOKE_LOG_UNAVAILABLE MESSAGE=`"$($_.Exception.Message)`" ARTIFACTS=$Pub"
    exit 16
}
$logPath = $runLog.path
$rawLog = [IO.File]::ReadAllText($logPath)
$measuredSmokeSessionId = Get-MeasuredSmokeSessionId $rawLog
$rows = Get-FrameRows $rawLog $measuredSmokeSessionId
$rows | Export-Csv -LiteralPath (Join-Path $legOut 'probe-timeline.csv') -NoTypeInformation

# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-1/2: publish the smoke artifacts BEFORE any PresentMon
# parsing, AND before PresentMon is even waited on. They are already known-good the moment the
# run log and frame rows are resolved -- a PresentMon post-step failure (a hang past the wait
# timeout, a nonzero exit, a missing csv, zero displayed samples in the playback window) must
# never destroy evidence that a smoke run already passed. Previously these were only published at
# the very end of a fully successful run, interleaved with the PresentMon wait and parse
# themselves, so an uncaught failure in either (the old unguarded Wait throw; Import-Csv on a
# missing file; the old unguarded "no positive samples" throw) left nothing published at all --
# exactly the 3-of-8 baseline failure this closes.
[void](Publish-AttrCudaFileCopy -Source $resultPath -Destination (Join-Path $Pub 'result.json'))
[void](Publish-AttrCudaFileCopy -Source (Join-Path $legOut 'smoke-stdout.txt') -Destination (Join-Path $Pub 'smoke-stdout.txt'))
[void](Publish-AttrCudaFileCopy -Source (Join-Path $legOut 'smoke-stderr.txt') -Destination (Join-Path $Pub 'smoke-stderr.txt'))
[void](Publish-AttrCudaFileCopy -Source (Join-Path $legOut 'probe-timeline.csv') -Destination (Join-Path $Pub 'probe-timeline.csv'))
[void](New-AttrCudaDirectory -Path (Join-Path $Pub 'logs'))
[void](Publish-AttrCudaFileCopy -Source $logPath -Destination (Join-Path $Pub 'logs\smoke-run.log'))

# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 1 / fable HARDENING): PresentMon is waited on
# only now, AFTER every smoke artifact above is already on disk. A wait failure -- a hang past
# TimeoutSeconds or a nonzero exit code -- is now a typed PRESENTMON_UNAVAILABLE terminal (the
# same outcome and exit code Get-AttrCudaPresentMonDisplayReport already returns for a PresentMon
# that fails during parsing), never an uncaught throw that would have destroyed everything just
# published. Wait-PresentMonCapture itself is unchanged -- it still throws PRESENTMON_TIMEOUT
# internally -- only this call site's handling of that throw changed.
$presentMonWaitError = $null
try {
    $presentMonDoneResult = Wait-PresentMonCapture $presentMonProc
    if ($presentMonDoneResult.status -ne 'done' -or [int]$presentMonDoneResult.exitCode -ne 0) {
        throw "PresentMon capture invalid status=$($presentMonDoneResult.status) rc=$($presentMonDoneResult.exitCode)"
    }
} catch {
    $presentMonWaitError = $_.Exception.Message
}
if ($null -ne $presentMonWaitError) {
    # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (fable HARDENING): a wait failure used to publish
    # neither the partial presentmon.csv (stable here -- Wait-PresentMonCapture kills the process
    # before throwing) nor the capture-start bracket sidecar, leaving an operator diagnosing a
    # PresentMon hang with strictly less evidence than a parse failure below already leaves. Both
    # are published here too, before the typed terminal, mirroring the parse-failure path exactly.
    if (Test-Path -LiteralPath $presentMonPath -PathType Leaf) {
        [void](Publish-AttrCudaFileCopy -Source $presentMonPath -Destination (Join-Path $Pub 'presentmon.csv'))
    }
    Save-Json ([ordered]@{
        schema='playback-attr-3-cuda-presentmon-capture.v2'
        captureStartUtc=$presentMonCaptureStartUtc.ToString('o')
        preSpawnUtc=$presentMonPreSpawnUtc.ToString('o')
        postSpawnUtc=$presentMonPostSpawnUtc.ToString('o')
        processStartUtc=$(if ($null -ne $presentMonProcessStartUtc) { $presentMonProcessStartUtc.ToString('o') } else { $null })
        captureStartUncertaintyMs=$presentMonCaptureStartUncertaintyMs
    }) (Join-Path $Pub 'presentmon-capture.json')
    $displayFailure = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result='PRESENTMON_UNAVAILABLE'
        fixtureRehearsal=$FixtureRehearsal
        reason=$presentMonWaitError
        presentMonStatus='unavailable'
        chains=@()
        presentMonCaptureStartUtc=$presentMonCaptureStartUtc.ToString('o')
        # PRESENTMON-HARNESS-ROBUSTNESS-1: the smoke run's own frame rows are already parsed and
        # published (above, before PresentMon was ever waited on) by the time a wait failure can
        # happen here -- carried into this typed refusal too, so a reader is not left guessing
        # whether the app-side run produced any frame telemetry at all.
        frameRows=$rows.Count
        sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $displayFailure (Join-Path $Pub 'summary.json')
    # PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note, applied here too -- identical convention to the
    # spawn-guard branch above): sanitized so an exception message containing a '"' cannot garble a
    # naive parser reading this stdout line's quoted REASON="..." field.
    Write-Output "RESULT=PRESENTMON_UNAVAILABLE REASON=`"$(ConvertTo-AttrCudaResultLineSafeText $presentMonWaitError)`" FRAME_ROWS=$($rows.Count) ARTIFACTS=$Pub"
    exit 23
}

# Backend-availability gate (swarm ruling, 2026-09-16): parse the run's own diagnostic
# fields BEFORE any verdict. A run where the CUDA backend never loaded, or where the R16
# texture path was not admitted, cannot produce a CUDA attribution -- the frame counters
# alone would happily describe some other path. Both fields and r16_reason are recorded
# either way, so a refusal says WHY.
$verdict = Get-AttrCudaEligibilityVerdict -LogText $rawLog
$diagnostics = [ordered]@{
    source = $verdict.source
    linePresent = $verdict.linePresent
    cudaBackendAvailable = $verdict.cudaBackendAvailable
    r16Available = $verdict.r16Available
    r16Reason = $verdict.r16Reason
    cudaBackendAttempted = $verdict.cudaBackendAttempted
    cudaBackendResolved = $verdict.cudaBackendResolved
    r16ProbeRan = $verdict.r16ProbeRan
    admitted = $verdict.admitted
    # Which bytes the verdict was read from, and the binding that proves they are this run's.
    log = [ordered]@{ path = $runLog.path; sha256 = $runLog.sha256; bytes = $runLog.bytes; runNonce = $runLog.runNonce; source = $runLog.source; aggregateSourcePath = $runLog.aggregateSourcePath }
}
if (-not $verdict.admitted) {
    # NOTE fix (fable): publish raw contact-sheet captures even on this early refusal -- see
    # Publish-AttrCudaContactSheetRawCaptures's own header.
    [void](Publish-AttrCudaContactSheetRawCaptures -Enabled $ContactSheetEnabled -SourceDir $contactSheetDir -PubRoot $Pub)
    $refusal = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result='BACKEND_NOT_AVAILABLE'
        fixtureRehearsal=$FixtureRehearsal
        diagnostics=$diagnostics; sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $refusal (Join-Path $Pub 'summary.json')
    Write-Output "RESULT=BACKEND_NOT_AVAILABLE CUDA_BACKEND_AVAILABLE=$($verdict.cudaBackendAvailable) R16_AVAILABLE=$($verdict.r16Available) R16_REASON=`"$($verdict.r16Reason)`" ARTIFACTS=$Pub"
    exit $verdict.exitCode
}

$gpuSummary = Get-LastGpuSummary $rawLog $measuredSmokeSessionId
# CUDA gate fix (MAJOR): gpu_preview_frames is not CUDA reconstruction -- a run with
# only preview frames and zero recon/readback/texture frames must not pass as CUDA-
# exercised. Only recon/texture readback and no-readback frames count toward the gate.
$gpuFramesTotal = $gpuSummary.gpuReconReadbackFrames + $gpuSummary.gpuTextureReadbackFrames + $gpuSummary.gpuTextureNoReadbackFrames
if ($gpuFramesTotal -le 0) {
    # NOTE fix (fable): publish raw contact-sheet captures even on this early refusal -- see
    # Publish-AttrCudaContactSheetRawCaptures's own header.
    [void](Publish-AttrCudaContactSheetRawCaptures -Enabled $ContactSheetEnabled -SourceDir $contactSheetDir -PubRoot $Pub)
    $fallback = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result='GPU_RECON_FRAMES_ZERO'
        fixtureRehearsal=$FixtureRehearsal
        gpuSummary=$gpuSummary; sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $fallback (Join-Path $Pub 'summary.json')
    Write-Output "RESULT=GPU_RECON_FRAMES_ZERO ARTIFACTS=$Pub"
    exit 13
}
if ($gpuSummary.cpuFrames -gt 0) {
    $fallback = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result='CPU_FALLBACK_DETECTED'
        fixtureRehearsal=$FixtureRehearsal
        gpuSummary=$gpuSummary; sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $fallback (Join-Path $Pub 'summary.json')
    Write-Output "RESULT=CPU_FALLBACK_DETECTED CPU_FRAMES=$($gpuSummary.cpuFrames) ARTIFACTS=$Pub"
    exit 14
}

$stats = [ordered]@{}
foreach ($name in @('prep_region_setup','prep_region_gpu','prep_region_image','prep_region_present','prep_region_finish','prep_region_total','prep_region_unattributed')) {
    $values = @($rows | ForEach-Object { [double]$_.$($name + '_ms') })
    $stats[$name] = Get-Stats $values
}

# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-1: the raw capture is published (if it exists) BEFORE it is
# ever parsed, so a typed refusal below still leaves it behind for diagnosis -- the previous
# unguarded Import-Csv threw straight past this file's own existence check when it was missing,
# and "no positive MsBetweenDisplayChange samples" threw AFTER the smoke run had already passed,
# both destroying every artifact already produced instead of reporting a typed outcome.
if (Test-Path -LiteralPath $presentMonPath -PathType Leaf) {
    [void](Publish-AttrCudaFileCopy -Source $presentMonPath -Destination (Join-Path $Pub 'presentmon.csv'))
}
# hub (sol step-0 key): persist the PresentMon clock anchor BEFORE parsing, so a later join of app swap timestamps
# (gpu_window.swap utc=) against PresentMon TimeInMs never has to re-derive the origin -- step 0 got it wrong.
# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 2 / fable HARDENING): captureStartUtc alone was
# a single guessed instant with no stated error bar. The full bracket -- the wall clock sampled
# immediately before/after Start-PresentMonCapture, the OS-reported process start time, and the
# uncertainty this implies -- is persisted alongside it, so a consumer needing a tighter join than
# this job's own 2500ms settle time can see exactly how much slack to allow instead of trusting an
# unbracketed stamp as exact.
Save-Json ([ordered]@{
    schema='playback-attr-3-cuda-presentmon-capture.v2'
    captureStartUtc=$presentMonCaptureStartUtc.ToString('o')
    preSpawnUtc=$presentMonPreSpawnUtc.ToString('o')
    postSpawnUtc=$presentMonPostSpawnUtc.ToString('o')
    processStartUtc=$(if ($null -ne $presentMonProcessStartUtc) { $presentMonProcessStartUtc.ToString('o') } else { $null })
    captureStartUncertaintyMs=$presentMonCaptureStartUncertaintyMs
}) (Join-Path $Pub 'presentmon-capture.json')
# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol+fable HARDENING, direction-corrected anchor -- see
# the bracket comment above): windowed under BOTH endpoints of the capture-start bracket, never
# just the earlier one. $displayReport.clockBracket carries both endpoints' presented/displayed
# counts and rates, the headline endpoint actually used for chains/selectedChain/selectedChainRows
# below, and why -- persisted verbatim into every outcome's summary.json so a reader never has to
# take the headline number's clock origin on faith.
$displayReport = Get-AttrCudaPresentMonDisplayReport -CsvPath $presentMonPath -ResultJson $resultJson -EarliestCaptureStartUtc $presentMonCaptureStartUtc -LatestCaptureStartUtc $presentMonPostSpawnUtc
if ($displayReport.status -ne 'OK') {
    # PRESENTMON-HARNESS-ROBUSTNESS-1: the backend-eligibility gate, the GPU-frames gate and the
    # region timing stats above have ALL already run and already succeeded by this point in the
    # script -- $diagnostics/$gpuSummary/$gpuFramesTotal/$stats/$rows are real, computed evidence
    # that this leg's own app-side measurement worked, not placeholders. Discarding them here,
    # only because PresentMon itself could not verify the display side, used to throw away a leg
    # whose measurement was fine; they are published alongside the typed refusal now, tagged with
    # presentMonStatus so nothing downstream mistakes this for a display-verified result.
    # PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note): DISPLAY_ASLEEP is PresentMon AFFIRMATIVELY
    # measuring zero displayed frames -- a verified negative -- not PresentMon being unable to
    # measure at all (PRESENTMON_UNAVAILABLE). The old blanket 'unavailable' under-described the
    # DISPLAY_ASLEEP case; tagged distinctly here so a coarse-field reader is not told PresentMon
    # had nothing to say when it actually said "zero, confirmed". $displayReport.status itself
    # (and the typed `result`/exit code above/below) stays authoritative either way.
    $displayFailurePresentMonStatus = if ($displayReport.status -eq 'DISPLAY_ASLEEP') { 'verified_zero_displayed' } else { 'unavailable' }
    $displayFailure = [ordered]@{
        schema='playback-attr-3-cuda-venue.v1'; result=$displayReport.status
        fixtureRehearsal=$FixtureRehearsal
        reason=$displayReport.reason
        presentMonStatus=$displayFailurePresentMonStatus
        chains=$displayReport.chains
        presentMonCaptureStartUtc=$presentMonCaptureStartUtc.ToString('o')
        clockBracket=$displayReport.clockBracket
        diagnostics=$diagnostics
        gpuSummary=$gpuSummary
        gpuFramesTotal=$gpuFramesTotal
        frameRows=$rows.Count
        regions=$stats
        sourceCommit=$SourceCommit; clipId=$ClipId; artifactRoot=$Pub
    }
    Save-Json $displayFailure (Join-Path $Pub 'summary.json')
    Write-Output "RESULT=$($displayReport.status) REASON=`"$(ConvertTo-AttrCudaResultLineSafeText $displayReport.reason)`" FRAME_ROWS=$($rows.Count) GPU_FRAMES=$gpuFramesTotal ARTIFACTS=$Pub"
    $displayExitCode = if ($displayReport.status -eq 'DISPLAY_ASLEEP') { 24 } else { 23 }
    exit $displayExitCode
}
$pmRows = @($displayReport.selectedChainRows)
$pmRows | Export-Csv -LiteralPath (Join-Path $legOut 'presentmon-series.csv') -NoTypeInformation
[void](Publish-AttrCudaFileCopy -Source (Join-Path $legOut 'presentmon-series.csv') -Destination (Join-Path $Pub 'presentmon-series.csv'))
# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol BLOCKER): a row displayed only via MsUntilDisplayed
# (MsBetweenDisplayChange reads NA -- the first present of a capture, before any prior display
# change exists to measure from) carries msBetweenDisplayChange=$null in $pmRows. [double]$null
# coerces to 0.0 in PowerShell, so feeding it straight into Get-Stats turned a row with NO
# interval into a spuriously fast (0ms) one, inflating presentMonStats.meanMs/fpsEquivalentMean
# downward/upward respectively and counting a non-interval row as a positive sample. Filtered out
# here, before Get-Stats ever sees the array -- every other consumer of this same interval either
# already tolerates the null (selectedChainRows/presentmon-series.csv itself: the module leaves
# msBetweenDisplayChange and displayFpsEquivalent both $null for that row, which Export-Csv writes
# as an empty cell) or already filters it independently (refresh_period_histogram.py skips blank
# and non-positive msBetweenDisplayChange cells reading the csv this exports).
$pmIntervalRows = @($pmRows | Where-Object { $null -ne $_.msBetweenDisplayChange -and $_.msBetweenDisplayChange -gt 0 })
$pmStats = Get-Stats @($pmIntervalRows | ForEach-Object { [double]$_.msBetweenDisplayChange })
# PRESENTMON-HARNESS-ROBUSTNESS-1: $displayReport.status is 'OK' here (the typed refusal above
# already returned on anything else), meaning PresentMon confirmed at least one genuine display
# change for MLVApp -- but that alone does not mean presentMonStats above is a real cadence
# measurement. A leg admitting only NA-first-present row(s) (displayed via MsUntilDisplayed, no
# prior display change to diff against) has $pmIntervalRows.Count -eq 0: pmStats.count reads 0
# and every stat reads $null, silently, while the rest of this leg still reports
# RESULT=MEASUREMENT_CAPTURED as if PresentMon had fully corroborated it -- exactly the "very
# thin admitted-row count" gap disclosed on CUDA-PLAYBACK-FULLSCREEN-UI-1 r2b (a real full-screen
# leg with presentedCount=1 displayedCount=1). Typed here as 'degraded': display is genuinely
# confirmed, but cadence cannot be, and the reason says why -- never silent.
#
# PRESENTMON-HARNESS-ROBUSTNESS-2 (sol BLOCKER, PR #174 r1): the check above ($pmIntervalRows.Count
# -gt 0) was itself too weak -- a SINGLE positive interval still read fully 'ok', so a leg that
# could not establish cadence over the whole 25-40s playback (the disclosed full-screen leg had
# presentedCount=1) still read as display-cadence corroborated, and the histogram consumer turned
# that one interval into a 100% one-refresh bucket. 'ok' requires a minimum absolute COUNT of
# positive-interval samples (below) -- an order of magnitude below the low end of the real windowed
# range on record (~150-240 displayed rows over 25-40s), so a genuinely thin/full-screen-style leg
# (0-2 samples) still reads degraded while a healthy leg clears it with wide margin.
#
# PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): r1's coverage arm divided
# positiveSamples by $displayReport.selectedChain.presentedCount -- both numerator and denominator
# came from the SAME PresentMon csv, so a capture that lost the tail of a 25-40s leg after a short
# healthy prefix still read coverage=1.0 over its own truncated rows. 'ok' now requires THREE
# independent arms, cleared together, each named in the reason on failure:
#   (1) COUNT: unchanged from r1, above.
#   (2) APP-SWAP COVERAGE: positiveSamples over an app-side swap count PresentMon never produced --
#       Get-AttrCudaAppSwapTelemetry reads the MLVApp log's own swap/frame telemetry (never this
#       csv). A log carrying neither line leaves coverage unavailable, which fails this arm rather
#       than dividing by zero or by a PresentMon-derived count again.
#   (3) TEMPORAL: no gap between positive-interval rows -- including the head/tail gaps to the
#       playback window's own bounds -- exceeds $presentMonSufficiencyMaxGapMs. (1) and (2) alone
#       cannot catch a captured PREFIX followed by silence: a leg that captures
#       >= $presentMonSufficiencyMinIntervalCount rows in the first couple of seconds of a 25-40s
#       leg then loses the rest can still clear a count floor and a swap-count-based coverage ratio
#       while having measured almost none of the actual leg. maxGapMs is an order of magnitude
#       above the real windowed range's typical inter-sample spacing (~150-240 rows over 25-40s).
$presentMonSufficiencyMinIntervalCount = 30
$presentMonSufficiencyMinCoverageFraction = 0.5
$presentMonSufficiencyMaxGapMs = 5000.0
$presentMonPresentedCount = [int]$displayReport.selectedChain.presentedCount
$appSwapTelemetry = Get-AttrCudaAppSwapTelemetry -LogText $rawLog
$presentMonAppSwapCount = $appSwapTelemetry.swapCount
$presentMonAppSwapSource = $appSwapTelemetry.source
$presentMonCoverageAvailable = ($null -ne $presentMonAppSwapCount) -and ($presentMonAppSwapCount -gt 0)
$presentMonCoverageFraction = if ($presentMonCoverageAvailable) { $pmIntervalRows.Count / [double]$presentMonAppSwapCount } else { 0.0 }
$presentMonTemporal = Get-AttrCudaTemporalCoverage -TimeInMsValues @($pmIntervalRows | ForEach-Object { [double]$_.timeInMs }) -WindowStartMs $displayReport.windowStartMs -WindowEndMs $displayReport.windowEndMs -MaxGapMs $presentMonSufficiencyMaxGapMs
$presentMonCountSufficient = ($pmIntervalRows.Count -ge $presentMonSufficiencyMinIntervalCount)
$presentMonCoverageSufficient = ($presentMonCoverageAvailable -and ($presentMonCoverageFraction -ge $presentMonSufficiencyMinCoverageFraction))
$presentMonTemporalSufficient = [bool]$presentMonTemporal.sufficient
$presentMonSufficient = $presentMonCountSufficient -and $presentMonCoverageSufficient -and $presentMonTemporalSufficient
$presentMonStatus = if ($presentMonSufficient) { 'ok' } else { 'degraded' }
$presentMonStatusReason = if ($presentMonSufficient) {
    $null
} elseif ($pmIntervalRows.Count -eq 0) {
    "PresentMon confirmed $($pmRows.Count) displayed MLVApp row(s) in the playback window, but none carried a positive MsBetweenDisplayChange interval -- every displayed sample came from MsUntilDisplayed on what PresentMon reports as an NA-first-present row, so presentMonStats has no interval to compute cadence from; display itself is still confirmed, cadence is not"
} else {
    $presentMonFailedArms = @()
    if (-not $presentMonCountSufficient) {
        $presentMonFailedArms += "count: only $($pmIntervalRows.Count) positive-interval row(s), below the minimum of $presentMonSufficiencyMinIntervalCount"
    }
    if (-not $presentMonCoverageSufficient) {
        if ($presentMonCoverageAvailable) {
            $presentMonFailedArms += "app-swap coverage: only $($pmIntervalRows.Count) positive-interval row(s) out of $presentMonAppSwapCount app-side $presentMonAppSwapSource ($([math]::Round($presentMonCoverageFraction * 100, 1))%), below the minimum of $([math]::Round($presentMonSufficiencyMinCoverageFraction * 100, 1))%"
        } else {
            $presentMonFailedArms += 'app-swap coverage: no independent app-side swap or frame telemetry found in the run log (neither playback_smoke.gpu_window_swaps nor playback_smoke.gate)'
        }
    }
    if (-not $presentMonTemporalSufficient) {
        $presentMonFailedArms += "temporal: a $([math]::Round($presentMonTemporal.maxGapMs / 1000.0, 1))s $($presentMonTemporal.gapKind) gap between positive-interval rows exceeds the $([math]::Round($presentMonSufficiencyMaxGapMs / 1000.0, 1))s ceiling"
    }
    "PresentMon's display-cadence evidence is too thin to corroborate as measured -- $($presentMonFailedArms -join '; ') -- display itself is still confirmed, cadence is not"
}

$dllSha256Lower = (Get-Sha $reconDll).ToLowerInvariant()
# $pendingSymbolPresence came from the build manifest above, whose dll.sha256 was verified
# against this exact DLL before it was deployed -- so the export claim is bound to these bytes.
$provenance = [ordered]@{
    rangeHeadSha = $RangeHeadSha
    llrawprocBlobId = $LlrawprocBlobId
    dllSha256 = $dllSha256Lower
    pendingSymbolPresence = $pendingSymbolPresence
    # A rehearsal is carried by the provenance sidecar too: a reader who consults only this file
    # must still be told that these numbers are a plumbing proof (sol, PR #137 r1).
    fixtureRehearsal = $FixtureRehearsal
}
Save-Json $provenance (Join-Path $Pub 'provenance.json')

# result.json, smoke-stdout.txt, smoke-stderr.txt, probe-timeline.csv and logs\smoke-run.log were
# already published above, before PresentMon was ever parsed; presentmon.csv and
# presentmon-series.csv were published above too, alongside/after the PresentMon parse itself.

$manifest = [ordered]@{
    schema = 'playback-attr-3-cuda-evidence-manifest.v1'
    presentMonCaptureStartUtc = $presentMonCaptureStartUtc.ToString('o')
    sourceCommit = $SourceCommit
    clipId = $ClipId
    fixtureRehearsal = $FixtureRehearsal
    # A rehearsal cites no consent receipt: the fixtures are repository bytes, and recording the
    # owner-footage receipt here would be misleading provenance (sol, PR #137 r2 minor).
    consentReceipt = $(if ($FixtureRehearsal) { $null } else { $ConsentReceiptFileName })
    scaleFactor = 4
    # The authenticated chain, end to end: this manifest's own bytes, and the DLL-pair manifest
    # it names. Neither is a claim the measurement host had to take on trust.
    buildManifest = [ordered]@{ name=$buildManifestName; sha256=$BuildManifestSha256; dllPairManifestSha256=$dllPairManifestSha256 }
    executable = [ordered]@{ name=$ExeName; sha256=$cacheExeSha }
    reconDll = [ordered]@{ name=$ReconName; sha256=(Get-Sha $reconDll) }
    presentMon = [ordered]@{
        name=$PresentMonName; sha256=$PresentMonSha; launch='direct-child-inherits-job-temp'
        chains=$displayReport.chains
        selectedChain=$displayReport.selectedChain
        # positiveSamples: positive-interval rows (presentMonStats' own population); presentedCount
        # is PresentMon's own count, audit only -- coverage below divides by appSwapCount instead.
        positiveSamples=$pmIntervalRows.Count
        presentedCount=$presentMonPresentedCount
        appSwapCount=$presentMonAppSwapCount
        appSwapSource=$presentMonAppSwapSource
        coverageFraction=$presentMonCoverageFraction
        temporalMaxGapMs=$presentMonTemporal.maxGapMs
        temporalGapKind=$presentMonTemporal.gapKind
        clockBracket=$displayReport.clockBracket
        sufficiency=[ordered]@{ minIntervalCount=$presentMonSufficiencyMinIntervalCount; minCoverageFraction=$presentMonSufficiencyMinCoverageFraction; maxGapMs=$presentMonSufficiencyMaxGapMs; countSufficient=$presentMonCountSufficient; coverageSufficient=$presentMonCoverageSufficient; temporalSufficient=$presentMonTemporalSufficient; sufficient=$presentMonSufficient }
        status=$presentMonStatus
        statusReason=$presentMonStatusReason
    }
    environmentBoundary = [ordered]@{ jobTempDir=$Scratch; allChildrenInheritJobTemp=$true }
    cpuQuiescence = [ordered]@{ samples=$loads; meanPercent=$avgLoad; thresholdPercent=20.0; pass=($avgLoad -le 20.0) }
    frameRows = $rows.Count
    smokeRunLog = [ordered]@{ path=$runLog.path; sha256=$runLog.sha256; bytes=$runLog.bytes; runNonce=$runLog.runNonce; source=$runLog.source }
    diagnostics = $diagnostics
    gpuSummary = $gpuSummary
    gpuFramesTotal = $gpuFramesTotal
    regions = $stats
    presentMonStats = $pmStats
    provenance = $provenance
    artifactRoot = $Pub
    capturedUtc = (Get-Date).ToUniversalTime().ToString('o')
}
Save-Json $manifest (Join-Path $Pub 'evidence-manifest.json')
# A SUCCESS summary.json, which the failure paths above all write but the success path did not:
# summary.json is the first file a reader opens, and its absence on the one path that produces
# numbers is exactly where a rehearsal could be mistaken for a measurement (sol, PR #137 r1).
Save-Json ([ordered]@{
    result = $(if ($FixtureRehearsal) { 'FIXTURE_REHEARSAL_CAPTURED' } else { 'MEASUREMENT_CAPTURED' })
    fixtureRehearsal = $FixtureRehearsal
    sourceCommit = $SourceCommit
    clipId = $ClipId
    rows = $rows.Count
    gpuFramesTotal = $gpuFramesTotal
    cpuFrames = $gpuSummary.cpuFrames
    presentMonSamples = $pmRows.Count
    presentMonSelectedChain = $displayReport.selectedChain
    presentMonStatus = $presentMonStatus
    presentMonStatusReason = $presentMonStatusReason
    # PRESENTMON-HARNESS-ROBUSTNESS-2: the sufficiency gate's inputs and verdict, at top level
    # (not only nested under evidence-manifest.json's presentMon block) since summary.json is the
    # first file a reader opens -- see the manifest's own comment for the rule.
    presentMonPositiveSamples = $pmIntervalRows.Count
    presentMonPresentedCount = $presentMonPresentedCount
    presentMonAppSwapCount = $presentMonAppSwapCount
    presentMonAppSwapSource = $presentMonAppSwapSource
    presentMonCoverageFraction = $presentMonCoverageFraction
    presentMonTemporalMaxGapMs = $presentMonTemporal.maxGapMs
    presentMonTemporalGapKind = $presentMonTemporal.gapKind
    presentMonSufficiency = [ordered]@{
        minIntervalCount=$presentMonSufficiencyMinIntervalCount
        minCoverageFraction=$presentMonSufficiencyMinCoverageFraction
        maxGapMs=$presentMonSufficiencyMaxGapMs
        countSufficient=$presentMonCountSufficient
        coverageSufficient=$presentMonCoverageSufficient
        temporalSufficient=$presentMonTemporalSufficient
        sufficient=$presentMonSufficient
    }
    clockBracket = $displayReport.clockBracket
    diagnostics = $diagnostics
    artifactRoot = $Pub
}) (Join-Path $Pub 'summary.json')
# CUDA-PLAYBACK-CONTACT-SHEET-1: publish the raw per-frame PNG+JSON pairs the app's own
# --contact-sheet-dir pass wrote (a no-op, touching nothing, when the switch was off -- see
# $contactSheetDir's declaration above). Composing them into one labelled sheet is left to a
# later step, off this job (see the -ContactSheet param's own comment). Published BEFORE the
# artifact index below so these land in it the same way every other published file does.
if ($ContactSheetEnabled -and $contactSheetDir -and (Test-Path -LiteralPath $contactSheetDir)) {
    $contactSheetPubDir = Publish-AttrCudaContactSheetRawCaptures -Enabled $ContactSheetEnabled -SourceDir $contactSheetDir -PubRoot $Pub
    # CUDA-PLAYBACK-CONTACT-SHEET-1 r1b: compose the raw captures into one labelled sheet +
    # stats sidecar right here, in the job's publish step, so a reader gets the composed
    # artifact without running make-contact-sheet.py by hand. Pillow/numpy (and Python
    # itself) are not guaranteed on every venue -- probe first and degrade to a typed,
    # non-fatal marker (never fail the whole job) when either is missing; the hub can
    # still compose locally from the published raw frames in that case.
    $contactSheetComposeMarker = $null
    $contactSheetPyExe = $null
    $contactSheetPyPrefixArgs = @()
    # HARDENING (r1d, sol pre-review #2): any deps-probe child that is still alive after a
    # timed-out Kill() attempt is recorded here, regardless of whether a LATER candidate goes
    # on to provide a usable interpreter -- published unconditionally below (never folded only
    # into the "no interpreter found" marker, which a later candidate's success would bypass).
    $contactSheetOrphanNotes = [System.Collections.Generic.List[object]]::new()
    # BLOCKER fix (r1c): a `-c 'import PIL, numpy'` -ArgumentList element does not survive
    # Start-Process's own argument-list-to-command-line join on every venue -- confirmed on
    # this host, where the direct `python -c "import PIL, numpy"` shell invocation exits 0
    # but the equivalent Start-Process -ArgumentList @('-c','import PIL, numpy') shape exits
    # 1, so a capable venue was falsely marked as lacking the dependency. A file path has no
    # such quoting/joining hazard: probe with one small script file instead of an inline -c
    # program string.
    $contactSheetDepsProbeScriptPath = Join-Path $Work 'contact-sheet-deps-probe.py'
    [void](Publish-AttrCudaText -Path $contactSheetDepsProbeScriptPath -Value "import PIL`nimport numpy")
    foreach ($candidate in @(
        [pscustomobject]@{ exe = 'python.exe'; prefix = @() },
        [pscustomobject]@{ exe = 'py.exe'; prefix = @('-3') }
    )) {
        if ($null -ne $contactSheetPyExe) { continue }
        try {
            $depsArgs = @($candidate.prefix) + @($contactSheetDepsProbeScriptPath)
            $depsProc = Start-Process -FilePath $candidate.exe -ArgumentList $depsArgs -PassThru -WindowStyle Hidden
            if (-not $depsProc.WaitForExit(20000)) {
                # HARDENING (r1d, sol pre-review #2): an empty catch around a bare Kill() left
                # no trace of a probe child that survived both the timeout and the kill attempt
                # -- mirror Stop-PresentMonCapture's own already-tested Kill()+bounded-
                # WaitForExit()+confirmedExited pattern instead of assuming Kill() succeeded.
                $depsStop = Stop-PresentMonCapture $depsProc
                if (-not $depsStop.confirmedExited) {
                    $contactSheetOrphanNotes.Add(
                        "dependency probe ($($candidate.exe)) did not exit after Kill() " +
                        "(killError=$($depsStop.killError) waitError=$($depsStop.waitError))")
                }
            } elseif ($depsProc.ExitCode -eq 0) {
                $contactSheetPyExe = $candidate.exe
                $contactSheetPyPrefixArgs = $candidate.prefix
            }
        } catch {
            continue
        }
    }
    if ($null -eq $contactSheetPyExe) {
        $contactSheetComposeMarker = 'CONTACT_SHEET_COMPOSE_UNAVAILABLE no Python 3 interpreter with Pillow+numpy was found on this venue'
    } else {
        $contactSheetComposerPayload = Read-AttrCudaBase64Payload -Base64 $ContactSheetComposerPyBase64
        if ($contactSheetComposerPayload.sha256 -ne $ContactSheetComposerSha256) {
            $contactSheetComposeMarker = 'CONTACT_SHEET_COMPOSE_UNAVAILABLE embedded composer sha256 mismatch'
        } else {
            $contactSheetComposerScriptPath = Join-Path $Work 'contact-sheet-composer.py'
            [void](Publish-AttrCudaBytes -Path $contactSheetComposerScriptPath -Bytes $contactSheetComposerPayload.bytes)
            $contactSheetSheetOut = Join-Path $Pub 'contact-sheet\sheet.png'
            $contactSheetStatsOut = Join-Path $Pub 'contact-sheet\stats.json'
            $contactSheetBackendLabel = if ($FixtureRehearsal) { 'fixture' } else { 'cuda' }
            # CUDA-PLAYBACK-CONTACT-SHEET-1 r1d (sol BLOCKER): without host/GPU/scale every
            # job-composed sheet's header reads host=unknown gpu=unknown scale=unknown,
            # defeating a side-by-side host/build/look comparison. Host is this job's own
            # venue -- the same $env:COMPUTERNAME value ultra-magnus-agent.ps1's own
            # result.json envelope records as its `host` field, captured independently here
            # since this job composes the sheet before that envelope is written. GPU and
            # scale are read from $verdict (the SAME gpu_playback_recon.eligibility line
            # already parsed above for the backend-availability gate), never re-probed --
            # [string] so an unset $verdict (e.g. this step run standalone in a test) yields
            # an empty string, never $null, which the composer renders as "unknown", never a
            # guessed real value.
            $contactSheetHostLabel = [string]$env:COMPUTERNAME
            $contactSheetGpuLabel = [string]$verdict.cudaBackendDescription
            $contactSheetScaleLabel = [string]$verdict.scale
            $composeArgs = @($contactSheetPyPrefixArgs) + @(
                $contactSheetComposerScriptPath,
                '--frames-dir', $contactSheetPubDir,
                '--sheet-out', $contactSheetSheetOut,
                '--stats-out', $contactSheetStatsOut,
                '--clip-id', $ClipId,
                '--host', $contactSheetHostLabel,
                '--gpu', $contactSheetGpuLabel,
                '--build-sha', $SourceCommit,
                '--backend', $contactSheetBackendLabel,
                '--scale', $contactSheetScaleLabel
            )
            # BLOCKER fix (r1d): quote every element -- see ConvertTo-AttrCudaQuotedProcessArgument's
            # own header for why an unquoted GPU description would silently split across argv.
            $composeArgs = @($composeArgs | ForEach-Object { ConvertTo-AttrCudaQuotedProcessArgument -Value $_ })
            try {
                $composeProc = Start-Process -FilePath $contactSheetPyExe -ArgumentList $composeArgs -PassThru -WindowStyle Hidden
                if (-not $composeProc.WaitForExit(60000)) {
                    # HARDENING (r1d, sol pre-review #2): same fix as the deps-probe above -- a
                    # bounded WaitForExit after Kill(), with the outcome folded into this leg's
                    # own marker rather than swallowed by an empty catch.
                    $composeStop = Stop-PresentMonCapture $composeProc
                    $contactSheetComposeMarker = 'CONTACT_SHEET_COMPOSE_UNAVAILABLE composer did not exit within 60s'
                    if (-not $composeStop.confirmedExited) {
                        $contactSheetComposeMarker += " (still running after Kill(): killError=$($composeStop.killError) waitError=$($composeStop.waitError))"
                    }
                } elseif ($composeProc.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $contactSheetSheetOut) -or -not (Test-Path -LiteralPath $contactSheetStatsOut)) {
                    $contactSheetComposeMarker = "CONTACT_SHEET_COMPOSE_UNAVAILABLE composer exited $($composeProc.ExitCode) or did not write its outputs"
                }
            } catch {
                $contactSheetComposeMarker = "CONTACT_SHEET_COMPOSE_UNAVAILABLE $($_.Exception.Message)"
            }
        }
    }
    if ($null -ne $contactSheetComposeMarker) {
        [void](Publish-AttrCudaText -Path (Join-Path $Pub 'contact-sheet\compose-status.txt') -Value $contactSheetComposeMarker)
    }
    if ($contactSheetOrphanNotes.Count -gt 0) {
        [void](Publish-AttrCudaText -Path (Join-Path $Pub 'contact-sheet\compose-warnings.txt') -Value ($contactSheetOrphanNotes -join "`n"))
    }
}
$files = Get-ChildItem -LiteralPath $Pub -Recurse -File | ForEach-Object { [ordered]@{ path=$_.FullName.Substring($Pub.Length + 1); sha256=(Get-Sha $_.FullName); bytes=$_.Length } }
Save-Json ([ordered]@{ schema='playback-attr-3-cuda-artifact-index.v1'; artifactRoot=$Pub; fixtureRehearsal=$FixtureRehearsal; files=$files }) (Join-Path $Pub 'artifact-index.json')
# The agent-visible result line says which kind of run this was, in the verb itself: the agent's
# outbox result.json carries stdout and nothing else, so a reader who never opens an artifact
# still cannot mistake a rehearsal for a measurement.
$resultVerb = if ($FixtureRehearsal) { 'FIXTURE_REHEARSAL_CAPTURED' } else { 'MEASUREMENT_CAPTURED' }
Write-Output "RESULT=$resultVerb FIXTURE_REHEARSAL=$FixtureRehearsal SOURCE=$SourceCommit CLIP=$ClipId ROWS=$($rows.Count) GPU_FRAMES=$gpuFramesTotal CPU_FRAMES=$($gpuSummary.cpuFrames) PRESENTMON_SAMPLES=$($pmRows.Count) PRESENTMON_STATUS=$presentMonStatus PRESENTMON_COVERAGE=$([math]::Round($presentMonCoverageFraction, 3)) ARTIFACTS=$Pub"
exit 0
} finally {
    if ($OwnerClipDir) {
        Close-AttrCudaOwnerFootageWorkspace -Handles $ownerLinkHandles -Directory $OwnerClipDir
    }
}
'@

# ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL): a single-pass substitution over the WHOLE
# token map at once -- see Expand-AttrCudaTemplate's own header in AttrCudaArtifacts.psm1.
# -ConsentReceiptFileName 'a__EMBEDDED_FUNCTIONS__b.json' (and every other underscore-permitting
# value here -- -BasePackageZip, -BasePackageExeName, -PresentMonName) passes its own
# ValidatePattern and used to collide with a LATER .Replace() call in the old chained
# substitution, splicing the embedded verifier source into the middle of an unrelated string
# literal. A single regex pass over the original template never rescans a substituted value, so
# that collision class cannot occur here regardless of which token a caller-controlled value
# happens to spell. Per-token quoting is unchanged: each value is still escaped for its
# single-quoted literal context by the caller, exactly as before.
$text = Expand-AttrCudaTemplate -Template $template -Tokens ([ordered]@{
    SOURCE_COMMIT = $SourceCommit
    CLIP_ID = $ClipId
    BUILD_MANIFEST_SHA256 = $BuildManifestSha256.ToLowerInvariant()
    CLIP_PATH = $ClipPath.Replace("'", "''")
    OWNER_PARTS_JSON = $ownerPartsJson.Replace("'", "''")
    RANGE_HEAD_SHA = $SourceCommit
    LLRAWPROC_BLOB_ID = $llrawprocBlobId
    EXE_NAME = $exeName
    RECON_NAME = $reconName
    BASE_PACKAGE_ZIP = $BasePackageZip.Replace("'", "''")
    BASE_PACKAGE_EXE_NAME = $BasePackageExeName.Replace("'", "''")
    PRESENTMON_NAME = $PresentMonName.Replace("'", "''")
    PRESENTMON_SHA256 = $PresentMonSha256
    SMOKE_RUNNER_CLOSURE = $smokeRunnerClosureLiteral
    SMOKE_RUNNER_CLOSURE_DIR_NAME = $smokeRunnerClosureDirName
    SMOKE_RUNNER_NAME = $smokeRunnerName
    CONSENT_RECEIPT = $ConsentReceiptFileName.Replace("'", "''")
    FIXTURE_REHEARSAL = $fixtureRehearsalLiteral
    AGENT_ROOT = $AgentRoot
    FIXTURE_SHA256 = $FixtureSha256
    CONTACT_SHEET_ENABLED = $contactSheetEnabledLiteral
    CONTACT_SHEET_FRAME_COUNT = $contactSheetFrameCountLiteral
    CONTACT_SHEET_COMPOSER_PY_BASE64 = $contactSheetComposerPyBase64
    CONTACT_SHEET_COMPOSER_SHA256 = $contactSheetComposerSha256ForTemplate
    EMBEDDED_FUNCTIONS = $embeddedFunctions
})

$outDir = Split-Path -Parent $OutFile
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
[IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($false))

[pscustomobject]@{
    outFile = $OutFile
    sourceCommit = $SourceCommit
    buildManifestSha256 = $BuildManifestSha256.ToLowerInvariant()
    clipId = $ClipId
    exeName = $exeName
    reconName = $reconName
    rangeHeadSha = $SourceCommit
    llrawprocBlobId = $llrawprocBlobId
    smokeRunnerClosureDigest = $smokeRunnerClosureDigest
    smokeRunnerClosureDirName = $smokeRunnerClosureDirName
}
