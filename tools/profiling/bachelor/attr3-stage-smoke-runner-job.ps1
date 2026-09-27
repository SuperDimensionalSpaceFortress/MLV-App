# attr3-stage-smoke-runner-job.ps1 -- GENERATOR (runs locally; nothing here runs on Bachelor).
# Emits a job that publishes the FULL dependency CLOSURE of run-release-gui-smoke.ps1 -- the
# smoke runner tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 launches -- into the
# Bachelor agent cache under one content-addressed cache DIRECTORY, so that job's closure pin
# (ATTRCUDA_SMOKE_RUNNER_STALE) has verified bytes to match against every file in it
# (ATTR3-SMOKE-RUNNER-DEPS-1).
#
# THE DEFECT THIS CLOSES. ATTR3-SMOKE-RUNNER-PIN-1 staged the runner alone. Bachelor still could
# not launch it: run-release-gui-smoke.ps1 dot-sources gui-smoke-screenshot-provenance.ps1,
# gui-smoke-color-artifact-scan.ps1, gui-smoke-gpu-texture-route-validation.ps1 and
# provenance-stamp.ps1, and imports gui-smoke-process-boundary.psm1, all resolved through
# $PSScriptRoot at runtime -- none of the five was ever staged, so the runner died at its own
# first dot-source line
# ("...gui-smoke-screenshot-provenance.ps1 is not recognized"). The app never launched;
# PresentMon never saw its target and never exited; PRESENTMON_TIMEOUT masked the real cause. The
# fix stages the runner's WHOLE closure together, under their original file names, in one
# directory, so $PSScriptRoot resolves every one of them.
#
# THE CLOSURE IS AN EXPLICIT, PINNED MANIFEST -- NEVER DISCOVERED (ATTR3-SMOKE-RUNNER-DEPS-1
# round 3, NARROW BY REDESIGN; contract restated honestly round 4, PR #144). Round 1 and round 2
# derived this closure by SCANNING committed text; a design swarm ruled that undiscoverable-by-
# patching (a literal-based scanner cannot see every real load shape, and a basename-only
# classifier can be fooled by an unrelated file sharing a staged one's name). The closure is now
# the fixed list in Get-AttrCudaSmokeRunnerClosureManifest (AttrCudaArtifacts.psm1);
# Resolve-AttrCudaSmokeRunnerClosure does nothing but resolve each pinned path's committed bytes.
# Assert-AttrCudaClosureComplete is the generator-time REGRESSION TRIPWIRE that keeps the pinned
# list honest -- an AST census over each manifest file's own committed text, throwing
# ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE the moment a load site cannot be classified. It is a
# tripwire over these six reviewed files, not a proof that covers every future edit; the runtime
# path (playback-attr-3-cuda-job.ps1's SMOKE_RUN_FAILED branch) is the real safety property for
# whatever it cannot see. Full contract at AttrCudaArtifacts.psm1, above the manifest.
#
# CONTENT-ADDRESSED DIRECTORY, NOT A FIXED NAME (mirrors ATTR3-SMOKE-RUNNER-PIN-1 round 2's
# reasoning for the single-file cache name). The published directory is named
# `smoke-runner-<first 16 hex of the closure digest>`, where the digest is the sha256 of the
# sorted `<sha256>  <name>` lines of the closure's committed blobs (Get-AttrCudaClosureDigestHex).
# Distinct closures publish under distinct directory names, so this stager never has to contend
# with -- or touch -- whatever a prior commit's closure already staged.
#
# NO SIDE FILE (mirrors ATTR3-SMOKE-RUNNER-PIN-1 round 2). Every closure file's exact committed
# bytes travel INLINE in the emitted job, base64-encoded, one entry per file. On Bachelor the job
# decodes and verifies EVERY file's sha256 against its own baked pin BEFORE a single byte is
# written to disk, then publishes the whole directory through one atomic, non-overwriting rename
# -- built under a temp name, verified, then renamed into place.
#
# `<jobId>.job.ps1 -VerifyOnly` runs the read-only prefix (name safety, base64 decode, hash for
# every file) and exits without creating, publishing or deleting anything.
#
# NO FOOTAGE. This job moves tracked repository scripts. It never opens, names, globs or resolves
# a media file, and it never enumerates the cache.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\attr3-stage-smoke-runner-job.ps1 `
#       -SourceCommit <40-hex> -OutDir <staging-dir>
# then submit the emitted <jobId>.job.ps1 to Bachelor the same way any other job in this route is
# submitted -- it needs no side file at all; every closure file's bytes travel inside the job.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    # `~` is admitted because Windows temp roots carry 8.3 short names (RUNNER~1, OBABAL~1) and
    # the behavioural tests point -AgentRoot at one; it is inert everywhere this value is used.
    [ValidatePattern('^[A-Za-z]:\\[A-Za-z0-9 _.~\\-]+$')]
    [string]$AgentRoot = 'C:\mlvtmp\mlv-agent',

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path

if (-not (Test-Path -LiteralPath $OutDir)) { [void](New-Item -ItemType Directory -Path $OutDir -Force) }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

# PINNED, never scanned (ATTR3-SMOKE-RUNNER-DEPS-1 round 3, NARROW BY REDESIGN): the closure is
# Get-AttrCudaSmokeRunnerClosureManifest, proved complete against the real committed files by
# Assert-AttrCudaClosureComplete -- an AST census, not a text scan, so it cannot be fooled by an
# extension-less load or a basename collision the way round 1/2's scanners could. Committed
# bytes, never the working tree (ATTR3-SMOKE-RUNNER-PIN-1): the same closure
# playback-attr-3-cuda-job.ps1 resolves independently from the same -SourceCommit, so the two
# generators agree without either one taking the other's word for it.
[void](Assert-AttrCudaClosureComplete -RepoRoot $RepoRoot -Commit $SourceCommit)
$closure = @(Resolve-AttrCudaSmokeRunnerClosure -RepoRoot $RepoRoot -Commit $SourceCommit)
foreach ($entry in $closure) {
    [void](Assert-AttrCudaSafeArtifactName -Name $entry.name)
    # Fable minor (round 2, carried forward): validated as 64 lowercase hex before it is
    # substituted into the emitted job, the same way playback-attr-3-cuda-job.ps1 validates
    # -PresentMonSha256 -- a value from this helper is trusted enough to gate a publish decision
    # and deserves the same shape check.
    if ($entry.sha256 -notmatch '^[0-9a-f]{64}$') {
        throw "ATTRCUDA_BLOB_SHA_MALFORMED resolved closure file sha256 is not 64 lowercase hex for $($entry.name): '$($entry.sha256)'"
    }
}

$closureDigest = Get-AttrCudaClosureDigestHex -Closure $closure
if ($closureDigest -notmatch '^[0-9a-f]{64}$') {
    throw "ATTRCUDA_BLOB_SHA_MALFORMED closure digest is not 64 lowercase hex: '$closureDigest'"
}
# Content-addressed: a distinct closure publishes under a distinct directory name, so this
# stager never has to contend with -- or touch -- whatever bytes already sit under another name.
$CacheDirName = "smoke-runner-$($closureDigest.Substring(0, 16))"
[void](Assert-AttrCudaSafeArtifactName -Name $CacheDirName)

function ConvertTo-AttrCudaGeneratorPsLiteral([string]$Value) { "'" + $Value.Replace("'", "''") + "'" }

# Save each file's committed bytes locally (for the base64 embed) and re-verify the sha256 the
# closure resolver already computed -- one extra independent check before it is trusted into the
# emitted job.
$closureEntries = foreach ($entry in $closure) {
    $localCopy = Join-Path $OutDir $entry.name
    $savedSha256 = Save-AttrCudaCommittedBlobBytes -RepoRoot $RepoRoot -BlobId $entry.blobId -Destination $localCopy
    if ($savedSha256 -ne $entry.sha256) {
        throw "ATTRCUDA_BLOB_SHA_MALFORMED re-saved bytes for $($entry.name) hash to $savedSha256, expected $($entry.sha256)"
    }
    [pscustomobject]@{
        name = $entry.name
        sha256 = $entry.sha256
        base64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($localCopy))
    }
}

$jobId = "attr3-stage-smoke-runner-$($SourceCommit.Substring(0,12))-$($closureDigest.Substring(0,12))"

$closureEntriesLiteral = "@(`r`n" + (($closureEntries | ForEach-Object {
    "    [pscustomobject]@{ name = $(ConvertTo-AttrCudaGeneratorPsLiteral $_.name); sha256 = '$($_.sha256)'; base64 = '$($_.base64)' }"
}) -join ",`r`n") + "`r`n)"

# --- job body template (placeholders are substituted below) ------------------------------------
$template = @'
# -VerifyOnly runs the read-only prefix (name safety, base64 decode, hash for every closure
# file) and exits WITHOUT publishing or deleting anything. The agent never passes it; the
# behavioural tests do.
param([switch]$VerifyOnly)

$ErrorActionPreference = 'Stop'
$JobId = '__JOB_ID__'
$AgentRoot = '__AGENT_ROOT__'
$CacheDirName = '__CACHE_DIR_NAME__'
$ClosureEntries = __CLOSURE_ENTRIES__
$Cache = Join-Path $AgentRoot 'cache'
$Work = Join-Path $AgentRoot "work\$JobId"
$Pub = Join-Path $AgentRoot "outbox\$JobId.artifacts"

function Say([string]$Message) { Write-Output "[$JobId] $Message" }
function Get-ShaLower([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

# --- verifiers, embedded VERBATIM from tools/profiling/bachelor/AttrCudaArtifacts.psm1 --------
__EMBEDDED_FUNCTIONS__
# --- end embedded verifiers -------------------------------------------------------------------

$StepLog = [System.Collections.Specialized.OrderedDictionary]::new()
# Set only once this run owns a freshly created outbox. Until then a failure must not write into
# a previous run's artifacts directory -- and -VerifyOnly must not write at all.
$PubReady = $false

function Complete-Failed([int]$Code, [string]$Step, [string]$Message) {
    $StepLog[$Step] = $Code
    Say "FAIL step=$Step exit=$Code $Message"
    if ($PubReady -and (Test-Path -LiteralPath $Pub)) {
        $partial = [ordered]@{
            schema = 'mlvapp.attr3-stage-smoke-runner-deps.v1'
            jobId = $JobId
            exitCode = $Code
            failedStep = $Step
            message = $Message
            stagedOnHost = $env:COMPUTERNAME
            steps = $StepLog
        }
        try {
            [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value ($partial | ConvertTo-Json -Depth 10))
        } catch {
            Say "RESULT_JSON_NOT_WRITTEN $($_.Exception.Message)"
        }
    }
    Write-Output "RESULT=SMOKE_RUNNER_STAGE_FAILED STEP=$Step EXIT=$Code"
    exit $Code
}

# ATTR3-SMOKE-RUNNER-DEPS-1: one exit for "the cache already holds this exact closure's content",
# whether that was found by the pre-flight check or by losing the publish race to a concurrent
# identical-content publisher -- mirrors attr3-stage-fixture-job.ps1's Complete-AlreadyStaged.
function Complete-AlreadyStaged([string]$CacheDirPathValue) {
    $StepLog['publish'] = 0
    [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value (([ordered]@{
        schema = 'mlvapp.attr3-stage-smoke-runner-deps.v1'; jobId = $JobId
        exitCode = 0; alreadyStaged = $true
        cacheDirName = $CacheDirName; cacheDirPath = $CacheDirPathValue
        files = @($ClosureEntries | ForEach-Object { [ordered]@{ name = $_.name; sha256 = $_.sha256 } })
        stagedOnHost = $env:COMPUTERNAME; steps = $StepLog
    }) | ConvertTo-Json -Depth 10))
    Write-Output "RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR=$CacheDirName PATH=$CacheDirPathValue"
    exit 0
}

function Assert-UnderAgentRoot([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($AgentRoot)
    if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "job-owned path '$Label' resolves outside the agent root: $full"
    }
}
foreach ($check in @(
    @{ path = $Cache; label = 'Cache' },
    @{ path = $Work; label = 'Work' },
    @{ path = $Pub; label = 'Pub' }
)) { Assert-UnderAgentRoot $check.path $check.label }

Say "START cacheDir=$CacheDirName files=$($ClosureEntries.Count)"

# --- name safety, before anything is decoded, written or renamed ------------------------------
try {
    [void](Assert-AttrCudaSafeArtifactName -Name $CacheDirName)
    foreach ($entry in $ClosureEntries) { [void](Assert-AttrCudaSafeArtifactName -Name $entry.name) }
    $cacheDirPath = Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache $CacheDirName) -Label "cache/$CacheDirName"
    # sol PR #144 round 4 minor: every same-name concurrent stager used to share this ONE
    # deterministic partial name, so one job could remove-and-recreate another job's not-yet-
    # complete scratch directory between its per-file verification and its rename, letting a
    # partial directory publish as a false success (attribution's exact-set check still refuses
    # the resulting incomplete directory, so wrong bytes were never USED -- only the staging
    # result could be wrong). A GUID suffix generated at job-body runtime, not at generator time,
    # makes this run's partial directory unique among any concurrently running instance of the
    # SAME emitted job, so no invocation ever touches another invocation's scratch directory.
    $partialDirSuffix = [Guid]::NewGuid().ToString('N')
    $partialDirPath = Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache "$CacheDirName.partial-$partialDirSuffix") -Label "cache/$CacheDirName.partial-$partialDirSuffix"
} catch {
    Complete-Failed 6 'artifactNameSafety' $_.Exception.Message
}
$StepLog['artifactNameSafety'] = 0

# --- decode and verify EVERY embedded payload BEFORE a single byte reaches disk ----------------
$decoded = @{}
try {
    foreach ($entry in $ClosureEntries) {
        $payload = Read-AttrCudaBase64Payload -Base64 $entry.base64
        if ($payload.sha256 -ne $entry.sha256) {
            throw "sha256 mismatch for $($entry.name): baked $($entry.sha256), decoded $($payload.sha256)"
        }
        $decoded[$entry.name] = $payload.bytes
    }
} catch {
    Complete-Failed 4 'closureDecode' $_.Exception.Message
}
$StepLog['closureDecode'] = 0

# Everything above is read-only: no file has been created or written. -VerifyOnly stops here.
if ($VerifyOnly) {
    Write-Output "RESULT=VERIFY_ONLY_OK CACHE_DIR=$CacheDirName FILES=$($ClosureEntries.Count)"
    exit 0
}

Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $Work
# Each container is created explicitly, parent first (never as a side effect of -Force on a
# deeper path), so every one of them is link-checked like the directories this job publishes into.
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'work'))
[void](New-AttrCudaDirectory -Path $Work)
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'outbox'))
[void](New-AttrCudaDirectory -Path $Pub)
[void](New-AttrCudaDirectory -Path $Cache)
$PubReady = $true

$Scratch = Join-Path $Work '.job-tmp'
[void](New-AttrCudaDirectory -Path $Scratch)
$env:TEMP = $Scratch
$env:TMP = $Scratch

# Already staged with identical content: nothing to do, and re-staging must not churn the cache.
# ATTR3-SMOKE-RUNNER-DEPS-1 round 3: "already staged" means the directory is EXACTLY the expected
# closure -- Get-AttrCudaClosureDirectoryMismatch (embedded, byte-identical to the attribution
# job's copy) is the one definition of that rule now, never two inline copies that could drift.
if (Test-Path -LiteralPath $cacheDirPath) {
    if ($null -eq (Get-AttrCudaClosureDirectoryMismatch -Dir $cacheDirPath -Entries $ClosureEntries)) {
        Complete-AlreadyStaged -CacheDirPathValue $cacheDirPath
    }
    Complete-Failed 21 'publishRename' "cache already holds $CacheDirName with DIFFERENT content"
}

# Build under a temp name unique to THIS invocation (see $partialDirSuffix above) -- the
# pre-removal below is defensive only (the GUID-suffixed name should never already exist) and,
# unlike the old shared deterministic name, can never remove a concurrent invocation's own
# in-progress partial directory.
Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $partialDirPath
[void](New-AttrCudaDirectory -Path $partialDirPath)
try {
    foreach ($entry in $ClosureEntries) {
        $entryPath = Join-Path $partialDirPath $entry.name
        [void](Publish-AttrCudaBytes -Path $entryPath -Bytes $decoded[$entry.name])
        if ((Get-ShaLower $entryPath) -ne $entry.sha256) { throw "sha256 did not round-trip into the cache for $($entry.name)" }
    }
} catch {
    Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $partialDirPath
    Complete-Failed 20 'publishPartial' $_.Exception.Message
}
$StepLog['publishPartial'] = 0

try {
    [void](Publish-AttrCudaDirectoryMoveNonOverwriting -Source $partialDirPath -Destination $cacheDirPath)
} catch {
    # A plain directory move with overwrite here would delete and replace whatever a concurrent
    # publisher had just placed at $cacheDirPath. The non-overwriting move above never touches an
    # occupied destination -- it either renamed cleanly or the destination is exactly as some
    # other writer left it. Re-verify it to tell "a concurrent publisher already finished this
    # exact closure" (this run is simply done) from "something else is there" (fail closed at the
    # same code the pre-flight check above uses for that).
    Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $partialDirPath
    if ($null -eq (Get-AttrCudaClosureDirectoryMismatch -Dir $cacheDirPath -Entries $ClosureEntries)) {
        Complete-AlreadyStaged -CacheDirPathValue $cacheDirPath
    }
    Complete-Failed 21 'publishRename' $_.Exception.Message
}
$StepLog['publishRename'] = 0

try { [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value (([ordered]@{
    schema = 'mlvapp.attr3-stage-smoke-runner-deps.v1'; jobId = $JobId
    exitCode = 0; alreadyStaged = $false
    cacheDirName = $CacheDirName; cacheDirPath = $cacheDirPath
    files = @($ClosureEntries | ForEach-Object { [ordered]@{ name = $_.name; sha256 = $_.sha256 } })
    stagedOnHost = $env:COMPUTERNAME; steps = $StepLog
}) | ConvertTo-Json -Depth 10)) } catch { Complete-Failed 23 'publishResult' $_.Exception.Message }
Write-Output "RESULT=SMOKE_RUNNER_STAGE_OK CACHE_DIR=$CacheDirName PATH=$cacheDirPath FILES=$($ClosureEntries.Count)"
exit 0
'@

$embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
    'Assert-AttrCudaSafeArtifactName',
    'Assert-AttrCudaDirectChild',
    'Assert-AttrCudaNoLinkBelowRoot',
    'Assert-AttrCudaWritableFileSlot',
    'Assert-AttrCudaNonOverwritingFileSlot',
    'Test-AttrCudaPathIsReparsePoint',
    'Get-AttrCudaClosureDirectoryMismatch',
    'Read-AttrCudaBase64Payload',
    'Publish-AttrCudaBytes',
    'Publish-AttrCudaText',
    'Publish-AttrCudaDirectoryMoveNonOverwriting',
    'New-AttrCudaDirectory',
    'Remove-AttrCudaTree'
)

$jobPath = Join-Path $OutDir "$jobId.job.ps1"
# ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL): a single-pass substitution over the WHOLE token
# map at once -- see Expand-AttrCudaTemplate's own header in AttrCudaArtifacts.psm1.
# -AgentRoot's own ValidatePattern admits underscores, so a value shaped like
# 'C:\mlv_agent__EMBEDDED_FUNCTIONS__' used to collide with a later .Replace() call in the old
# chained substitution; a single regex pass over the original template never rescans a
# substituted value, so that collision class cannot occur here regardless of which token a
# caller-controlled value happens to spell.
$text = Expand-AttrCudaTemplate -Template $template -Tokens ([ordered]@{
    JOB_ID = $jobId
    AGENT_ROOT = $AgentRoot
    CACHE_DIR_NAME = $CacheDirName
    CLOSURE_ENTRIES = $closureEntriesLiteral
    EMBEDDED_FUNCTIONS = $embeddedFunctions
})
[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

# So the hub can pass the baked digest straight to playback-attr-3-cuda-job.ps1's verification
# without re-deriving it, and so a reader can confirm the two generators agree.
Write-Output "RESULT=SMOKE_RUNNER_STAGE_JOB_EMITTED SOURCE=$SourceCommit CLOSURE_DIGEST=$closureDigest CACHE_DIR_NAME=$CacheDirName JOB=$jobPath"

[pscustomobject]@{
    jobFile = $jobPath
    jobId = $jobId
    sourceCommit = $SourceCommit
    closureDigest = $closureDigest
    cacheDirName = $CacheDirName
    files = @($closureEntries | ForEach-Object { [ordered]@{ name = $_.name; sha256 = $_.sha256 } })
    agentRoot = $AgentRoot
    cacheDirPath = (Join-Path (Join-Path $AgentRoot 'cache') $CacheDirName)
}
