# playback-attr-3-cuda-stage-job.ps1 -- GENERATOR (runs locally / in a lane; nothing here runs
# on Bachelor). Emits a self-contained <jobId>.job.ps1 that takes the package
# tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1 built on the board host -- dropped
# into the Bachelor agent inbox as plain side-files -- and publishes it into the agent cache in
# the layout tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 (the attribution job)
# verifies.
#
# WHY A JOB AND NOT A COPY. NA-7: direct hooked writes to \\bachelor\... are refused; the
# tracked submitter tools/profiling/um-run.ps1 is the route, and the agent only ever EXECUTES
# inbox\*.job.ps1 while leaving every other inbox file alone. So the hub drops the artifacts
# beside the job and this job does the publishing, on the host, with the hashes re-checked
# there. Nothing in this file hides a path in a variable to route around that boundary.
#
# WHAT IT PROVES. Every expected file name and lowercase sha256 is BAKED IN at generation time.
# On the host the job re-hashes each side-file and refuses on the first mismatch, then
# cross-checks the arriving build.json against the same baked values -- so neither a stale
# side-file nor an edited manifest can publish a package that does not match what was assembled.
#
# NAMES ARE DERIVED, NEVER TAKEN FROM THE MANIFEST (sol, PR #133 r2, BLOCKER). build.json used to
# supply the file names that were then handed to Join-Path for cache writes and inbox deletion,
# with containment checked only on the directory roots -- so a crafted `..\..\x` name produced a
# path that passed the root check and still escaped it, on an unattended host, for a Remove-Item.
# Both this generator and the emitted job now derive the expected basenames from the (baked)
# sourceCommit through Get-AttrCudaArtifactNames and require an EXACT match; the manifest is
# reduced to a source of hashes. Every name is additionally run through
# Assert-AttrCudaSafeArtifactName, and every resolved cache and inbox path through
# Assert-AttrCudaDirectChild, BEFORE any Copy-Item, Move-Item or Remove-Item. Exit 6.
#
# `<jobId>.job.ps1 -VerifyOnly` runs the whole read-only prefix -- name derivation, path
# containment, side-file presence, hashes, manifest binding -- and exits without creating,
# copying, publishing or deleting anything.
#
# ORDER. Artifacts are copied to <name>.partial in the cache, hash-verified there, renamed, and
# only then is build.json published -- LAST, exactly as the attribution job assumes: its mere
# presence means the exe, the DLL and the package zip beside it are complete and verified.
#
# NO FOOTAGE. This job moves build artifacts. It never opens, names, globs or resolves a media
# file, and it never enumerates the cache.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-stage-job.ps1 `
#       -SourceCommit <40-hex> -BuildDir <assembler -OutDir> -OutDir <staging-dir>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$SourceCommit,

    # The assembler's -OutDir: holds playback-attr-3-cuda-<sha12>-build.json and the three
    # artifacts it names.
    [Parameter(Mandatory = $true)]
    [string]$BuildDir,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    # `~` is admitted because Windows temp roots carry 8.3 short names (RUNNER~1, OBABAL~1) and
    # the behavioural tests point -AgentRoot at one; it is inert everywhere this value is used.
    [ValidatePattern('^[A-Za-z]:\\[A-Za-z0-9 _.~\\-]+$')]
    [string]$AgentRoot = 'C:\mlvtmp\mlv-agent'
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force

$names = Get-AttrCudaArtifactNames -SourceCommit $SourceCommit
$JobId = "playback-attr-3-cuda-stage-$($names.shortSha)"

if (-not (Test-Path -LiteralPath $BuildDir -PathType Container)) { throw "BuildDir is not a directory: $BuildDir" }
$BuildDir = (Resolve-Path -LiteralPath $BuildDir).Path
$buildManifestPath = Join-Path $BuildDir $names.buildManifestName
if (-not (Test-Path -LiteralPath $buildManifestPath)) {
    throw "build manifest not found: $buildManifestPath (the assembler writes it LAST; its absence means that build did not complete)"
}
$buildManifest = Get-Content -Raw -LiteralPath $buildManifestPath | ConvertFrom-Json
if ($buildManifest.sourceCommit -ne $SourceCommit) {
    throw "build manifest sourceCommit=$($buildManifest.sourceCommit) does not match -SourceCommit $SourceCommit"
}
if ($buildManifest.pendingSymbolPresence -isnot [bool]) {
    throw "build manifest has no boolean pendingSymbolPresence; the attribution job would refuse it on the host"
}

function Get-ShaLower([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

# Bake the artifact set. The NAMES come from the naming convention applied to -SourceCommit, not
# from the manifest: build.json supplies hashes and nothing else. A manifest whose names are not
# exactly the canonical three is refused outright -- that is a manifest for a different build, or
# a crafted one, and there is no third possibility worth guessing at.
$canonical = @(
    [ordered]@{ label = 'packageZip'; name = $names.packageZipName; claim = $buildManifest.packageZip },
    [ordered]@{ label = 'exe'; name = $names.exeName; claim = $buildManifest.exe },
    [ordered]@{ label = 'dll'; name = $names.reconName; claim = $buildManifest.dll }
)
$expected = @()
foreach ($item in $canonical) {
    [void](Assert-AttrCudaSafeArtifactName -Name ([string]$item.name))
    $claimedName = if ($null -eq $item.claim) { '' } else { [string]$item.claim.name }
    if ($claimedName -cne [string]$item.name) {
        throw "build manifest names $($item.label) '$claimedName'; the canonical name for $SourceCommit is '$($item.name)'. Names are derived, never taken from the manifest."
    }
    $sha = if ($null -eq $item.claim) { '' } else { ([string]$item.claim.sha256).ToLowerInvariant() }
    if ($sha -notmatch '^[0-9a-f]{64}$') { throw "build manifest sha256 for $($item.name) is not a lowercase sha256" }
    $expected += [ordered]@{ name = [string]$item.name; sha256 = $sha }
}
# Verify each one HERE first: a side-file that does not match on this machine can never match on
# the host, and failing now costs a generator run instead of an agent round trip.
foreach ($item in $expected) {
    $localPath = Assert-AttrCudaDirectChild -Root $BuildDir -Path (Join-Path $BuildDir ([string]$item.name)) -Label "BuildDir/$($item.name)"
    if (-not (Test-Path -LiteralPath $localPath)) { throw "artifact named by the build manifest is missing from BuildDir: $localPath" }
    $actual = Get-ShaLower $localPath
    if ($actual -ne [string]$item.sha256) { throw "sha256 mismatch in BuildDir for $($item.name): manifest $($item.sha256), on disk $actual" }
}
$manifestSha256 = Get-ShaLower $buildManifestPath

$embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
    'Get-AttrCudaArtifactNames',
    'Assert-AttrCudaSafeArtifactName',
    'Assert-AttrCudaDirectChild',
    'Assert-AttrCudaWritableFileSlot',
    'Publish-AttrCudaText',
    'Publish-AttrCudaFileCopy',
    'Publish-AttrCudaFileMove',
    'New-AttrCudaDirectory',
    'Remove-AttrCudaPartialFile',
    'Assert-AttrCudaNoLinkBelowRoot',
    'Remove-AttrCudaTree'
)

if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$jobPath = Join-Path $OutDir "$JobId.job.ps1"

# --- job body template (placeholders are substituted below) ------------------------------------
$template = @'
# -VerifyOnly runs the read-only prefix (name derivation and safety, path containment, side-file
# presence, hashes, manifest binding) and exits without creating, copying, publishing or deleting
# anything. The agent never passes it; the behavioural tests do.
param([switch]$VerifyOnly)

$ErrorActionPreference = 'Stop'
$SourceCommit = '__SOURCE_COMMIT__'
$JobId = '__JOB_ID__'
$AgentRoot = '__AGENT_ROOT__'
$ManifestName = '__MANIFEST_NAME__'
$ManifestSha256 = '__MANIFEST_SHA256__'
$Expected = __EXPECTED_LITERAL__
$Cache = Join-Path $AgentRoot 'cache'
$Inbox = Join-Path $AgentRoot 'inbox'
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
            schema = 'mlvapp.playback-attr-3-cuda-stage.v1'
            sourceCommit = $SourceCommit
            jobId = $JobId
            exitCode = $Code
            failedStep = $Step
            message = $Message
            stagedOnHost = $env:COMPUTERNAME
            steps = $StepLog
        }
        # Still a publish write: slot-checked, and a refusal never masks the original exit code.
        try {
            [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value ($partial | ConvertTo-Json -Depth 10))
        } catch {
            Say "RESULT_JSON_NOT_WRITTEN $($_.Exception.Message)"
        }
    }
    Write-Output "RESULT=STAGE_FAILED STEP=$Step EXIT=$Code"
    exit $Code
}

# Machine-safety boundary: every path this job touches lives under the agent root.
function Assert-UnderAgentRoot([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($AgentRoot)
    if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "job-owned path '$Label' resolves outside the agent root: $full"
    }
}
foreach ($check in @(
    @{ path = $Cache; label = 'Cache' },
    @{ path = $Inbox; label = 'Inbox' },
    @{ path = $Work; label = 'Work' },
    @{ path = $Pub; label = 'Pub' }
)) { Assert-UnderAgentRoot $check.path $check.label }

Say "START source=$SourceCommit files=$($Expected.Count)"

# --- names: DERIVED here, then matched against what was baked in ------------------------------
# The baked set is not taken on trust either: the canonical basenames are re-derived on this host
# from $SourceCommit through the same convention the assembler and the attribution job use, and
# the baked names must be exactly those. Then every name is proved to be a plain basename and
# every resolved path a direct child of the directory it belongs to -- before anything is copied,
# renamed or removed. Exit 6 is reserved for this.
try {
    $derived = Get-AttrCudaArtifactNames -SourceCommit $SourceCommit
    $canonicalNames = @($derived.packageZipName, $derived.exeName, $derived.reconName)
    if (@($Expected).Count -ne $canonicalNames.Count) {
        throw "the job was baked with $(@($Expected).Count) artifacts; the convention yields $($canonicalNames.Count) for $SourceCommit"
    }
    if ($ManifestName -cne $derived.buildManifestName) {
        throw "baked manifest name '$ManifestName' is not the canonical '$($derived.buildManifestName)' for $SourceCommit"
    }
    [void](Assert-AttrCudaSafeArtifactName -Name $ManifestName)
    $sideFiles = @()
    foreach ($item in $Expected) {
        $name = [string]$item.name
        [void](Assert-AttrCudaSafeArtifactName -Name $name)
        if ($canonicalNames -cnotcontains $name) {
            throw "baked artifact name '$name' is not one of the canonical names for ${SourceCommit}: $($canonicalNames -join ', ')"
        }
        $sideFiles += [ordered]@{
            name = $name
            path = (Assert-AttrCudaDirectChild -Root $Inbox -Path (Join-Path $Inbox $name) -Label "inbox/$name")
            cachePath = (Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache $name) -Label "cache/$name")
            partialPath = (Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache "$name.partial") -Label "cache/$name.partial")
            sha256 = [string]$item.sha256
        }
    }
    $manifestSide = Assert-AttrCudaDirectChild -Root $Inbox -Path (Join-Path $Inbox $ManifestName) -Label "inbox/$ManifestName"
    $manifestCachePath = Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache $ManifestName) -Label "cache/$ManifestName"
    $manifestPartialPath = Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache "$ManifestName.partial") -Label "cache/$ManifestName.partial"
} catch {
    Complete-Failed 6 'artifactNameSafety' $_.Exception.Message
}
$StepLog['artifactNameSafety'] = 0

# Each side-file is addressed by its EXACT derived name -- no directory enumeration, no pattern.
foreach ($item in $sideFiles) {
    if (-not (Test-Path -LiteralPath ([string]$item.path) -PathType Leaf)) {
        Complete-Failed 3 'inboxSideFiles' "expected side-file missing from the inbox: $($item.name)"
    }
}
if (-not (Test-Path -LiteralPath $manifestSide -PathType Leaf)) {
    Complete-Failed 3 'inboxSideFiles' "expected side-file missing from the inbox: $ManifestName"
}
$StepLog['inboxSideFiles'] = 0

$observed = [System.Collections.Specialized.OrderedDictionary]::new()
foreach ($item in $sideFiles) {
    $actual = Get-ShaLower ([string]$item.path)
    $observed[[string]$item.name] = $actual
    if ($actual -ne [string]$item.sha256) {
        Complete-Failed 4 'sideFileHashes' "sha256 mismatch for $($item.name): expected $($item.sha256), arrived $actual"
    }
}
$manifestActualSha = Get-ShaLower $manifestSide
if ($manifestActualSha -ne $ManifestSha256) {
    Complete-Failed 4 'sideFileHashes' "sha256 mismatch for $ManifestName : expected $ManifestSha256, arrived $manifestActualSha"
}
$StepLog['sideFileHashes'] = 0

# The manifest's own claims must still agree with the baked set: identical bytes cannot
# disagree, so this fails only if the baking itself was wrong -- which is worth knowing.
$arrivedManifest = Get-Content -Raw -LiteralPath $manifestSide | ConvertFrom-Json
if ($arrivedManifest.sourceCommit -ne $SourceCommit) {
    Complete-Failed 5 'manifestBinding' "arrived manifest sourceCommit=$($arrivedManifest.sourceCommit) does not match $SourceCommit"
}
if ($arrivedManifest.pendingSymbolPresence -isnot [bool]) {
    Complete-Failed 5 'manifestBinding' 'arrived manifest has no boolean pendingSymbolPresence; the attribution job would refuse it'
}
foreach ($claim in @($arrivedManifest.packageZip, $arrivedManifest.exe, $arrivedManifest.dll)) {
    $claimedName = [string]$claim.name
    if (-not $observed.Contains($claimedName)) {
        Complete-Failed 5 'manifestBinding' "arrived manifest names $claimedName, which is not in the staged set"
    }
    if ($observed[$claimedName] -ne ([string]$claim.sha256).ToLowerInvariant()) {
        Complete-Failed 5 'manifestBinding' "arrived manifest sha256 for $claimedName disagrees with the staged bytes"
    }
}
$StepLog['manifestBinding'] = 0

# Everything above is read-only. -VerifyOnly stops here, having touched nothing.
if ($VerifyOnly) {
    Write-Output "RESULT=VERIFY_ONLY_OK SOURCE=$SourceCommit FILES=$($sideFiles.Count) MANIFEST=$ManifestName"
    exit 0
}

Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $Work
Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $Pub
New-Item -ItemType Directory -Path $Work -Force | Out-Null
# The outbox is created explicitly (never as a side effect of -Force on a deeper path), so its
# parent is checked for links like every other directory this job creates.
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'outbox'))
[void](New-AttrCudaDirectory -Path $Pub)
[void](New-AttrCudaDirectory -Path $Cache)
$PubReady = $true

# Job-owned TEMP before anything else runs.
$Scratch = Join-Path $Work '.job-tmp'
New-Item -ItemType Directory -Path $Scratch -Force | Out-Null
$env:TEMP = $Scratch
$env:TMP = $Scratch

# --- transactional publish into the cache: .partial, verify, rename, manifest LAST -------------
# Every path below was resolved and proved a direct child of $Cache during artifactNameSafety;
# none is reconstructed from a string here.
$partialPaths = @()
foreach ($item in $sideFiles) { $partialPaths += [string]$item.partialPath }
$partialPaths += $manifestPartialPath
function Remove-JobPartials {
    # Files only: a .partial occupied by a directory or a link is LEFT IN PLACE and reported, never
    # recursed into (sol PR #133 r3: -Recurse can follow a junction out of the cache). It cannot
    # prompt, because Remove-AttrCudaPartialFile never asks Remove-Item to delete a container.
    foreach ($p in $script:partialPaths) { [void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path $p) }
}

try {
    foreach ($item in $sideFiles) {
        $partial = [string]$item.partialPath
        [void](Publish-AttrCudaFileCopy -Source ([string]$item.path) -Destination $partial)
        if ((Get-ShaLower $partial) -ne [string]$item.sha256) { throw "sha256 did not round-trip into the cache for $($item.name)" }
    }
} catch {
    Remove-JobPartials
    Complete-Failed 20 'publishPartials' $_.Exception.Message
}
$StepLog['publishPartials'] = 0

try {
    foreach ($item in $sideFiles) {
        [void](Publish-AttrCudaFileMove -Source ([string]$item.partialPath) -Destination ([string]$item.cachePath))
    }
} catch {
    Remove-JobPartials
    Complete-Failed 21 'publishRename' $_.Exception.Message
}
$StepLog['publishRename'] = 0

try {
    [void](Assert-AttrCudaWritableFileSlot -Path $manifestCachePath)
    [void](Publish-AttrCudaFileCopy -Source $manifestSide -Destination $manifestPartialPath)
    if ((Get-ShaLower $manifestPartialPath) -ne $ManifestSha256) { throw "sha256 did not round-trip into the cache for $ManifestName" }
    [void](Publish-AttrCudaFileMove -Source $manifestPartialPath -Destination $manifestCachePath)
} catch {
    Remove-JobPartials
    Complete-Failed 22 'publishManifest' $_.Exception.Message
}
$StepLog['publishManifest'] = 0

# The side-files are MOVED, not copied: leaving the package sitting in the inbox invites a
# later job to stage a stale copy of it. Removal happens only after the cache publish is
# complete, so an interrupted run leaves the inbox intact and is simply re-runnable.
# Plain-file removal only (never a directory, never through a link): the same guarded helper.
# The ancestor chain from $AgentRoot is re-checked at deletion time (sol PR #133 r8: a linked inbox).
foreach ($item in $sideFiles) { [void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path ([string]$item.path)) }
[void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path $manifestSide)
$StepLog['inboxCleanup'] = 0
# $item.path and $manifestSide are the values Assert-AttrCudaDirectChild returned: a full path
# already proved to sit directly in the inbox. This is the Remove-Item the traversal finding was
# about, and it no longer has a string to reconstruct.

$published = [System.Collections.Specialized.OrderedDictionary]::new()
foreach ($item in $sideFiles) { $published[[string]$item.name] = [string]$item.sha256 }
$published[$ManifestName] = $ManifestSha256
$result = [ordered]@{
    schema = 'mlvapp.playback-attr-3-cuda-stage.v1'
    sourceCommit = $SourceCommit
    jobId = $JobId
    exitCode = 0
    cache = $Cache
    published = $published
    manifestPublishedLast = $true
    stagedOnHost = $env:COMPUTERNAME
    stagedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    steps = $StepLog
}
try { [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value ($result | ConvertTo-Json -Depth 10)) } catch { Complete-Failed 23 'publishResult' $_.Exception.Message }
Write-Output "RESULT=STAGE_OK SOURCE=$SourceCommit FILES=$($published.Count) CACHE=$Cache ARTIFACTS=$Pub"
exit 0
'@

$expectedLiteral = '@(' + (($expected | ForEach-Object {
    "[ordered]@{ name = '" + ([string]$_.name).Replace("'", "''") + "'; sha256 = '" + [string]$_.sha256 + "' }"
}) -join ', ') + ')'

$text = $template.
    Replace('__SOURCE_COMMIT__', $SourceCommit).
    Replace('__JOB_ID__', $JobId).
    Replace('__AGENT_ROOT__', $AgentRoot.Replace("'", "''")).
    Replace('__MANIFEST_NAME__', $names.buildManifestName).
    Replace('__MANIFEST_SHA256__', $manifestSha256).
    Replace('__EXPECTED_LITERAL__', $expectedLiteral)
# LAST: the module text is spliced in after every other substitution, so no placeholder rule can
# rewrite a character inside the verbatim verifier source.
$text = $text.Replace('__EMBEDDED_FUNCTIONS__', $embeddedFunctions)

[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

[pscustomobject]@{
    jobFile = $jobPath
    jobId = $JobId
    sourceCommit = $SourceCommit
    agentRoot = $AgentRoot
    # Exactly these files must be dropped into <agent root>\inbox beside the job, by their
    # own names. The agent executes only *.job.ps1 and leaves the rest untouched.
    inboxSideFiles = @(@($expected | ForEach-Object { [string]$_.name }) + @($names.buildManifestName))
    buildManifestName = $names.buildManifestName
    buildManifestSha256 = $manifestSha256
}
