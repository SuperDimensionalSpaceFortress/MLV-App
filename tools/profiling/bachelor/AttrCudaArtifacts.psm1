# AttrCudaArtifacts.psm1 -- the ONE definition of the PLAYBACK-ATTR-3-CUDA package naming
# convention, of the build-identity header injected into an archived source tree, and of every
# VERIFICATION the emitted jobs perform before they trust an input.
#
# Three surfaces have to agree on these names without exchanging state:
#   - tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1 (board host: builds them)
#   - tools/profiling/bachelor/playback-attr-3-cuda-stage-job.ps1 (stages them into the cache)
#   - tools/profiling/bachelor/playback-attr-3-cuda-job.ps1 (attribution: verifies them)
# The attribution job derives the same names independently from -SourceCommit, which is why
# this module exists: two copies of a convention drift, and the drift shows up as a cache miss
# on a host nobody is watching.
#
# WHY THE VERIFIERS LIVE HERE TOO (sol, PR #133 r2). Each generator emits a self-contained
# <jobId>.job.ps1 that runs on a host with no checkout and no module path, so a job cannot
# Import-Module this file. Instead the generators EMBED the function text VERBATIM, extracted by
# Get-AttrCudaEmbeddedFunctionSource at generation time. The test suite
# (tools/repo_hygiene/test_playback_attr_3_cuda_split_route.py) executes these same functions
# through pwsh, so the code the tests exercise is byte-identical to the code the jobs run --
# which a re-implementation inside a here-string template could never be.

Set-StrictMode -Version Latest

$script:AttrCudaModulePath = $PSCommandPath

# STAGE-STALL card: worst-case cold sequential read rate of an owner input on the
# measurement host (Bachelor), in MB/s -- the figure Get-AttrCudaLegTimeBudget sizes a leg's
# timeouts from. MEASURED 2026-09-29 on the host, from the traces the jobs themselves left:
# playback-attr-3-cuda-74464398c1cf-M16-1243-20260929-193043.trace.txt (a real leg-D job) read the
# 2.2 GB first part cold in ONE pass, 4 MiB blocks + incremental SHA-256, in 1352 s = 1.6 MB/s
# whole-part, with progress windows between 0.9 and 1.9 MB/s; the first 256 MB of the second part
# ran 1.3 MB/s. (An earlier attr3-footage-read-rate-job.ps1 pass saw 2.1-2.4 MB/s and one isolated
# 128 MB cold region 6.1 MB/s -- the host is not steady, so the figure sized from is the whole-part
# mean of a real leg, rounded DOWN.) 4 KiB blocks (what Get-FileHash uses) ran 0.55 MB/s even WARM;
# warm 4 MiB reads ran 13 MB/s. Small files are slow cold too (an 11 MB exe hashed in 125 s).
$script:AttrCudaMeasuredColdReadMBps = 1.5

# STAGE-STALL card round 1f (fable hardening 2): the fixed (size-independent) part
# of a leg's wall time, from the two real leg-D traces on Bachelor (bocs-legd-proof-01 and -02,
# build 74464398, the two-part 3.3 GB owner input; both in the r1b summary.md "Measured on bachelor"):
#   agent child start -> first script line   570 s (proof-01, 9.5 min)   646 s (proof-02, 10.8 min)   -> 650
#   display-wake                               270 s (4.5 min)             598 s (10 min)              -> 600
#   cold small-artifact hashes (exe/dll/zip)   ~150 s (13 s + 125 s + ..)   49 s (34 + 13 + 2)         -> 150
#   footage length screen (both parts)         80 s                        46 s                        -> 100
#   worst observed sum before the identity read starts                                                 = 1500
#   package-expand + deploy + quiescence + PresentMon spawn (after the identity read): NOT MEASURED --
#   no run reached them (proof-02 was killed as package-expand started) -- so a stated allowance     =  300
# = 1800 s. PostRunSeconds (PresentMon wait <= 35 s in the job + publishing/hashing the artifact
# files) is likewise NOT MEASURED; 300 s is an allowance until a leg gets past launch. Both are
# generator parameters (-FixedPreLaunchSeconds / -PostRunSeconds) so a fresh measurement overrides
# them without editing this file.
$script:AttrCudaMeasuredFixedPreLaunchSeconds = 1800
$script:AttrCudaAllowancePostRunSeconds = 300

function Get-AttrCudaEmbeddedFunctionSource {
    <#
    .SYNOPSIS
    Return the verbatim source text of named functions in this module, for embedding in an
    emitted job script.
    .DESCRIPTION
    Extraction is textual on purpose: Get-Command .ScriptBlock would return a REBUILT body
    (comments stripped, formatting normalised), so a test asserting "the job embeds what the
    module defines" would be comparing two different strings. Every function in this module
    opens with `function <Name> {` at column 0 and closes with `}` at column 0, and no function
    body contains a line starting with an unindented `}`; that is the contract the regex below
    relies on, and test_embedded_functions_round_trip pins it.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Name,

        [string]$ModulePath
    )

    if ([string]::IsNullOrWhiteSpace($ModulePath)) { $ModulePath = $script:AttrCudaModulePath }
    $text = [IO.File]::ReadAllText($ModulePath)
    $blocks = @()
    foreach ($functionName in $Name) {
        $pattern = '(?ms)^function\s+' + [regex]::Escape($functionName) + '\s*\{.*?^\}'
        $match = [regex]::Match($text, $pattern)
        if (-not $match.Success) {
            throw "AttrCudaArtifacts.psm1 defines no extractable function named '$functionName'"
        }
        $blocks += $match.Value
    }
    ($blocks -join "`r`n`r`n")
}

function Expand-AttrCudaTemplate {
    <#
    .SYNOPSIS
    Single-pass job-template substitution: every __TOKEN__ placeholder in -Template is replaced
    by -Tokens['TOKEN'] in ONE regex pass, so a substituted value is never rescanned for further
    placeholders.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL). Every generator that emits a <jobId>.job.ps1
    body used to substitute placeholders through a CHAINED .Replace(...).Replace(...) sequence:
    each later .Replace call rescans the ENTIRE string, including text an earlier .Replace call
    just spliced in. A caller-controlled value shaped like another placeholder's own token (e.g.
    -ConsentReceiptFileName 'a__EMBEDDED_FUNCTIONS__b.json', which passes that parameter's own
    ValidatePattern) therefore collided with the LATER __EMBEDDED_FUNCTIONS__ substitution and
    spliced ~600 lines of verifier source into the middle of an unrelated string literal, breaking
    the emitted job's own parse -- and the same window existed for every underscore-permitting
    value and for every generated blob substituted early in the chain.
    [regex]::Replace with a MatchEvaluator processes the ORIGINAL input in ONE pass: the
    evaluator's return value for one match is never itself rescanned for further matches, so this
    closes the whole class at once, for every token, in every generator, rather than patching one
    collision at a time. Per-token quoting/escaping (e.g. doubling an embedded `'` for a
    single-quoted literal context) stays the CALLER's job -- -Tokens values are expected to
    already be escaped for the quoting context they land in, exactly as before this function
    existed; this function only decides WHICH bytes replace WHICH placeholder, never how a value
    is made safe for where it lands.
    Throws AttrCudaTemplateUnknownToken for a template placeholder absent from -Tokens (a typo in
    the template, or a caller who forgot a token), and AttrCudaTemplateUnusedToken for a -Tokens
    entry the template never references (a caller who renamed a placeholder in one place and not
    the other) -- both fail closed rather than silently emitting a literal placeholder or silently
    dropping a caller-supplied value.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Template,

        [Parameter(Mandatory = $true)]
        [System.Collections.Specialized.OrderedDictionary]$Tokens
    )

    $consumed = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $unknown = [System.Collections.Generic.List[string]]::new()
    $evaluator = {
        param($match)
        $name = $match.Value.Substring(2, $match.Value.Length - 4)
        if (-not $Tokens.Contains($name)) {
            [void]$unknown.Add($name)
            return $match.Value
        }
        [void]$consumed.Add($name)
        [string]$Tokens[$name]
    }
    $expanded = [regex]::Replace($Template, '__[A-Z0-9_]+__', $evaluator)

    if ($unknown.Count -gt 0) {
        $distinctUnknown = @($unknown | Select-Object -Unique)
        throw "ATTRCUDA_TEMPLATE_UNKNOWN_TOKEN template placeholder(s) have no entry in -Tokens: $(($distinctUnknown | ForEach-Object { "__${_}__" }) -join ', ')"
    }
    $unusedKeys = @($Tokens.Keys | Where-Object { -not $consumed.Contains($_) })
    if ($unusedKeys.Count -gt 0) {
        throw "ATTRCUDA_TEMPLATE_UNUSED_TOKEN -Tokens entries never referenced by the template: $($unusedKeys -join ', ')"
    }
    $expanded
}

function Get-AttrCudaZipArchiveComment {
    <#
    .SYNOPSIS
    Read the End-Of-Central-Directory comment of a zip file.
    .DESCRIPTION
    `git archive --format=zip <commit>` stores the 40-hex commit id in this field (git's own
    documented behaviour for a commit-ish, as opposed to a tree). Reading it is how a host with
    no git and no checkout can prove WHICH COMMIT a source zip carries -- a sha256 alone only
    proves the bytes are the ones the generator saw, not what is inside them.
    The EOCD record is the last >=22 bytes of the file: signature 0x06054b50, then 18 bytes of
    fixed fields, then a uint16 comment length at offset 20 and the comment itself at offset 22.
    It is located by scanning backwards, because the comment is variable-length.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 22) { return '' }
    $start = $bytes.Length - 22
    for ($index = $start; $index -ge 0; $index--) {
        if ($bytes[$index] -ne 0x50) { continue }
        if ($bytes[$index + 1] -ne 0x4B -or $bytes[$index + 2] -ne 0x05 -or $bytes[$index + 3] -ne 0x06) { continue }
        $commentLength = [BitConverter]::ToUInt16($bytes, $index + 20)
        if ($commentLength -eq 0) { return '' }
        if (($index + 22 + $commentLength) -gt $bytes.Length) { return '' }
        return [Text.Encoding]::ASCII.GetString($bytes, $index + 22, $commentLength)
    }
    ''
}

function Assert-AttrCudaSourceArchive {
    <#
    .SYNOPSIS
    Refuse a source zip that is not, byte for byte, the archive the generator produced from the
    expected commit.
    .DESCRIPTION
    Two independent bindings, because they fail differently:
      - sha256: these are the exact bytes the generator hashed (catches a stale or swapped drop);
      - zip comment: the archive was produced by `git archive` FROM THIS COMMIT (catches an
        archive of some other tree that someone re-hashed into a re-generated job).
    Throws with a distinguishable ATTRCUDA_ARCHIVE_* token; returns the verified sha256.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ArchivePath,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256,

        [string]$ExpectedCommit = ''
    )

    if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "ATTRCUDA_ARCHIVE_SHA_MALFORMED expected sha256 is not 64 lowercase hex: '$ExpectedSha256'"
    }
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
        throw "ATTRCUDA_ARCHIVE_MISSING $ArchivePath"
    }
    $actual = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256) {
        throw "ATTRCUDA_ARCHIVE_SHA_MISMATCH $ArchivePath expected=$ExpectedSha256 actual=$actual"
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedCommit)) {
        $comment = Get-AttrCudaZipArchiveComment -Path $ArchivePath
        if ($comment -ne $ExpectedCommit) {
            throw "ATTRCUDA_ARCHIVE_COMMIT_MISMATCH $ArchivePath declares commit '$comment', expected '$ExpectedCommit'"
        }
    }
    $actual
}

function Get-AttrCudaArtifactNames {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$SourceCommit
    )

    $shortSha = $SourceCommit.Substring(0, 12)
    [pscustomobject]@{
        sourceCommit = $SourceCommit
        shortSha = $shortSha
        exeName = "MLVApp-playback-attr-3-cuda-$shortSha.exe"
        reconName = "igpu_recon_cuda-playback-attr-3-cuda-$shortSha.dll"
        packageZipName = "MLVApp-playback-attr-3-cuda-$shortSha-pkg.zip"
        buildManifestName = "playback-attr-3-cuda-$shortSha-build.json"
        # The unrenamed build name kept inside the package zip.
        packageExeName = 'MLVApp.exe'
    }
}

function New-AttrCudaBuildInfoHeader {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$SourceCommit,

        [string]$BuildTimeUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    )

    # The source comes from `git archive`, so there is no .git beside it and MLVApp.pro's
    # qmake-time git probe stamps "unknown". This writes the SAME macro set
    # tools/gen-buildinfo.ps1 emits, directly into OUT_PWD before qmake runs, pinned to the
    # commit that was archived (dirty=0, which is what an archive means).
    @"
/* AUTO-GENERATED by tools/gen-buildinfo.ps1 at build time. Do not edit; do not commit. */
#ifndef MLVAPP_BUILD_BUILDINFO_H
#define MLVAPP_BUILD_BUILDINFO_H
/* A qmake-time -D may be stale or "unknown" when qmake cannot execute git.
   This build-time header is generated immediately before compilation and is
   authoritative, so replace any earlier definition rather than preserving it. */
#ifdef MLVAPP_GIT_SHA
#undef MLVAPP_GIT_SHA
#endif
#define MLVAPP_GIT_SHA "$SourceCommit"
#define MLVAPP_BUILD_SHA "$SourceCommit"
#define MLVAPP_GIT_DIRTY 0
#define MLVAPP_GIT_DESCRIBE "$SourceCommit"
#define MLVAPP_BUILD_TIME_UTC "$BuildTimeUtc"
#define MLVAPP_BUILD_STAMP "MLVAPP_BUILDSTAMP_v1|sha=$SourceCommit|dirty=0"
#endif
"@
}

function Assert-AttrCudaSafeArtifactName {
    <#
    .SYNOPSIS
    Refuse anything that is not a plain file name, and return it.
    .DESCRIPTION
    The staging job used to take artifact names straight out of build.json and feed them to
    Join-Path for cache writes and inbox deletion, while its containment check only covered the
    DIRECTORY roots (sol, PR #133 r2). A name of `..\..\something` therefore produced a path that
    passed the root check and still landed outside it -- on an unattended host, for a
    Remove-Item. A basename cannot do that, so this refuses everything that is not one:
    separators in either direction, any `..` at all, a colon (and so any drive qualifier or
    alternate data stream), the characters Windows forbids in a file name, and surrounding
    whitespace. Assert-AttrCudaDirectChild is the second, independent line.
    Throws with a distinguishable ATTRCUDA_NAME_* token.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Name
    )

    if ([string]::IsNullOrWhiteSpace($Name)) { throw 'ATTRCUDA_NAME_EMPTY an artifact was named with an empty string' }
    if ($Name -ne $Name.Trim()) { throw "ATTRCUDA_NAME_PADDED '$Name' has leading or trailing whitespace" }
    if ($Name.Contains('..')) { throw "ATTRCUDA_NAME_TRAVERSAL '$Name' contains '..'" }
    foreach ($character in @('\', '/', ':')) {
        if ($Name.Contains($character)) { throw "ATTRCUDA_NAME_NOT_A_BASENAME '$Name' contains '$character'" }
    }
    if ($Name -match '[<>"|?*]') { throw "ATTRCUDA_NAME_ILLEGAL_CHARACTER '$Name'" }
    if ($Name -ne [IO.Path]::GetFileName($Name)) { throw "ATTRCUDA_NAME_NOT_A_BASENAME '$Name'" }
    $Name
}

function Assert-AttrCudaDirectChild {
    <#
    .SYNOPSIS
    Require a resolved path to be a DIRECT child of $Root, and return its full path.
    .DESCRIPTION
    Stronger than "starts with the root", and deliberately so: the staging job writes into and
    deletes out of two flat directories, so anything at a deeper level is already wrong even when
    it is technically inside. Resolution is through [IO.Path]::GetFullPath, which collapses `..`
    and relative segments, so the comparison is made on what the filesystem would actually touch
    rather than on the string that was passed in. Called before every Copy-Item, Move-Item and
    Remove-Item, never after.
    Throws ATTRCUDA_PATH_NOT_DIRECT_CHILD.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$Path,

        [string]$Label = 'path'
    )

    $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $fullPath = [IO.Path]::GetFullPath($Path)
    $parent = [IO.Path]::GetDirectoryName($fullPath)
    if ([string]::IsNullOrEmpty($parent)) {
        throw "ATTRCUDA_PATH_NOT_DIRECT_CHILD $Label '$fullPath' has no parent directory"
    }
    if ($parent.TrimEnd('\') -ne $fullRoot) {
        throw "ATTRCUDA_PATH_NOT_DIRECT_CHILD $Label '$fullPath' is not a direct child of '$fullRoot'"
    }
    $fullPath
}

function Assert-AttrCudaBuildManifest {
    <#
    .SYNOPSIS
    Authenticate a staged build.json before a single one of its fields is trusted, and return it.
    .DESCRIPTION
    The hash chain used to stop at staging (sol, PR #133 r2): the attribution job took whichever
    same-named build.json happened to be in the MUTABLE agent cache and believed its
    pendingSymbolPresence, its artifact sha256s and its sourceCommit. Replacing build.json plus
    the matching artifacts forged all three at once.
    So: the file's own sha256 is checked against a value baked in by the generator BEFORE
    ConvertFrom-Json runs -- parse order matters, because a parsed field is already a trusted
    field. Then three claims the downstream evidence rests on:
      - sourceCommit must equal the commit the job was generated for;
      - dllPairManifestSha256 must be present and well formed, so the exe's DLL pair is itself
        chained back to the Ultra-Magnus manifest rather than merely asserted;
      - pendingSymbolPresence must be a real boolean, because gpu_job_result_provenance.py
        rejects null/omitted and this host cannot re-derive it.
    Throws with a distinguishable ATTRCUDA_BUILD_MANIFEST_* token; returns the parsed manifest.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSourceCommit
    )

    if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "ATTRCUDA_BUILD_MANIFEST_SHA_MALFORMED expected sha256 is not 64 lowercase hex: '$ExpectedSha256'"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "ATTRCUDA_BUILD_MANIFEST_MISSING $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256) {
        throw "ATTRCUDA_BUILD_MANIFEST_SHA_MISMATCH $Path expected=$ExpectedSha256 actual=$actual"
    }

    $manifest = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    if ($null -eq $manifest) { throw "ATTRCUDA_BUILD_MANIFEST_UNPARSEABLE $Path" }
    $commitProperty = $manifest.PSObject.Properties['sourceCommit']
    $commit = if ($null -eq $commitProperty) { '' } else { [string]$commitProperty.Value }
    if ($commit -ne $ExpectedSourceCommit) {
        throw "ATTRCUDA_BUILD_MANIFEST_COMMIT_MISMATCH $Path declares '$commit', expected '$ExpectedSourceCommit'"
    }
    $dllPairProperty = $manifest.PSObject.Properties['dllPairManifestSha256']
    $dllPairSha = if ($null -eq $dllPairProperty) { '' } else { ([string]$dllPairProperty.Value).ToLowerInvariant() }
    if ($dllPairSha -notmatch '^[0-9a-f]{64}$') {
        throw "ATTRCUDA_BUILD_MANIFEST_DLLPAIR_UNBOUND $Path carries no well-formed dllPairManifestSha256 (got '$dllPairSha')"
    }
    $symbolProperty = $manifest.PSObject.Properties['pendingSymbolPresence']
    if ($null -eq $symbolProperty -or $symbolProperty.Value -isnot [bool]) {
        $observed = if ($null -eq $symbolProperty) { '<absent>' } else { [string]$symbolProperty.Value }
        throw "ATTRCUDA_BUILD_MANIFEST_SYMBOL_PRESENCE_INVALID $Path pendingSymbolPresence is not a boolean (got '$observed')"
    }
    $manifest
}

function Get-AttrCudaGitBlobHashFromBytes {
    <#
    .SYNOPSIS
    Git's blob hash of a byte buffer already in memory, exactly as `git hash-object` would compute
    it for a file living at $RelativePath -- content filters (autocrlf, .gitattributes) included.
    .DESCRIPTION
    ATTR3-ADMIT-CONTENT-PIN-1 round 2d. `git hash-object -- <path>` opens and reads $Path itself --
    a SECOND read of the same mutable file, which is exactly the internal check/use gap this whole
    round closes. `git hash-object --stdin --path <RelativePath>` hashes whatever bytes are piped
    to it on stdin while still selecting content filters BY that path (the same ones `--path` would
    apply if it were reading the file itself), so this can feed it the identical buffer
    Assert-AttrCudaFixtureCommittedBytes already read through its own single, write-denying handle
    -- no second read of the file.

    Round-2d SELF-CAUGHT DEFECT: an earlier version of this function computed
    SHA1("blob " + <byte length> + "\0" + <content>) directly over the raw in-memory buffer,
    entirely in .NET, with no git subprocess at all. That is only the correct git blob hash when
    nothing normalises the bytes on the way into the object database. It silently disagreed with
    the committed blob for any autocrlf/gitattributes-normalised text fixture -- caught by
    FixtureCommittedBytesTests.test_an_unmodified_tracked_file_is_accepted (a `.c` source file)
    failing closed on completely unmodified content, before this ever reached review. Filtering
    is a repository-configuration concern only git itself can resolve correctly; reproducing its
    hash FORMAT locally without also reproducing its FILTERS is not a safe shortcut.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'git'
    foreach ($arg in @('-C', $RepoRoot, 'hash-object', '--stdin', '--path', $RelativePath)) {
        [void]$psi.ArgumentList.Add($arg)
    }
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    # round 2e (sol/fable MAJOR): Process.Start (and the stream I/O around it) can throw a raw,
    # untyped .NET exception -- e.g. Win32Exception if the git binary that
    # Assert-AttrCudaFixtureCommittedBytes's earlier Get-Command probe found a moment ago is no
    # longer resolvable when actually launched -- whose message never starts with an ATTR3_FIXTURE_*
    # token. Left uncaught, that escaped Get-UmRunFixtureAdmission's classification entirely: an
    # environmental failure with nothing to say about the fixture's bytes would be graded on
    # whether its first word happened to match, not on what it actually was.
    try {
        $proc = [Diagnostics.Process]::Start($psi)
        try {
            $proc.StandardInput.BaseStream.Write($Bytes, 0, $Bytes.Length)
        } finally {
            $proc.StandardInput.Close()
        }
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()
    } catch {
        throw "ATTR3_FIXTURE_GIT_UNAVAILABLE could not launch git to hash-object (via stdin) '$RelativePath' in '$RepoRoot': $($_.Exception.Message)"
    }
    if ($proc.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($stdout)) {
        throw "ATTR3_FIXTURE_GIT_UNAVAILABLE could not hash-object (via stdin) '$RelativePath' in '$RepoRoot': $stderr"
    }
    $stdout.Trim().ToLowerInvariant()
}

function Assert-AttrCudaFixtureCommittedBytes {
    <#
    .SYNOPSIS
    Refuse a working-tree fixture whose bytes are not exactly what HEAD has for it.
    .DESCRIPTION
    ATTR3-FIXTURE-STAGE-1. A fixture is admissible because it is a TRACKED repository fixture
    (Test-UmRunTrackedFixtureSource, tools/profiling/UmRunDrop.psm1) -- but "tracked" says
    nothing about whether the WORKING-TREE bytes at that path are still the committed ones. A
    dirty or corrupted working copy would otherwise bake a sha256 into the staging job that
    no reviewed commit ever produced. This compares git's blob hash of the file on disk to
    `git rev-parse HEAD:<repo-relative path>`.
    sol, PR #140 r2 BLOCKER (ATTR3-ADMIT-CONTENT-PIN-1): without -RepoRoot this discovered the
    repository from the FILE'S OWN DIRECTORY via `git rev-parse --show-toplevel` -- so a nested
    repository committed under the fixture's own directory could authorize bytes the OUTER
    repository's HEAD never held. Every fixture-admission caller (Test-UmRunFixtureContentPin in
    tools/profiling/UmRunDrop.psm1, and tools/profiling/bachelor/attr3-stage-fixture-job.ps1's
    own generator-time check) now passes its trusted -RepoRoot; the discovered repository must
    resolve to EXACTLY that root (case-insensitive, full-path normalised) or this throws
    ATTR3_FIXTURE_FOREIGN_REPO, never merely trusting whichever .git happens to be nearest.
    Callers that omit -RepoRoot (this function's generic "is a tracked source file unmodified"
    use, unrelated to the measurement-fixture admission surface) keep the original nearest-repo
    behaviour -- the trusted-root check is opt-in via -RepoRoot, not universal.
    KNOWN LIMITATION (round-2 recon): neither side of the root comparison canonicalises an 8.3
    short name or a junction/symlink component; an equivalent root reached through one of those
    can be falsely rejected as ATTR3_FIXTURE_FOREIGN_REPO rather than accepted. Undefended here --
    callers that might pass such a root should resolve it themselves first -- but no longer
    untested: test_a_root_reached_through_a_junction_is_falsely_refused_known_limitation
    (tools/repo_hygiene/test_playback_attr_3_cuda_behaviour.py) pins the refusal empirically, so a
    future change cannot silently start accepting -- or silently start crashing on -- a
    junction-reached root without that test being touched.
    .PARAMETER Sha256Pin
    Optional [ref]; ATTR3-ADMIT-CONTENT-PIN-1 round 2d (sol BLOCKER / fable MAJOR, PR #140 r2c).
    On a verified match, .Value is set to a SHA256 of the EXACT SAME byte buffer this function
    hashed to produce the git blob comparison below -- one file handle, opened deny-write for its
    whole lifetime and read once, feeds both hashes. The old shape (this function's own `git
    hash-object` subprocess, then a caller's SEPARATE Get-FileHash afterward) read the mutable
    path twice; bytes exchanged in that gap became the trusted pin, and stayed the trusted pin for
    every downstream binding this module added, because nothing downstream ever saw the original
    bytes to compare against. Left unset (the default) when this throws, since a caller must never
    bind to a pin taken from bytes that failed verification.
    Throws with a distinguishable ATTR3_FIXTURE_* token; returns the verified (matching) hash.
    round 2e (sol/fable MAJOR): every throw here now lands in exactly one of DEFINITE REFUSAL
    (ATTR3_FIXTURE_MISSING, ATTR3_FIXTURE_NOT_IN_A_REPO, ATTR3_FIXTURE_FOREIGN_REPO,
    ATTR3_FIXTURE_NOT_COMMITTED, ATTR3_FIXTURE_WORKING_TREE_DIRTY -- a content verdict this
    function stands behind) or COULD NOT DETERMINE (ATTR3_FIXTURE_GIT_UNAVAILABLE,
    ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE, ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE -- an
    environmental/operational failure that says nothing about the bytes), never untyped and
    never folded into the wrong bucket; see UmRunDrop.psm1's $UmRunIndeterminateAdmissionTokens,
    which every one of the second group is registered in. The `git rev-parse HEAD:<path>` call
    below used to throw ATTR3_FIXTURE_NOT_COMMITTED for ANY failure there (non-zero exit or empty
    stdout), which classified a corrupted object store, a git I/O error or an unexpected git
    version's message identically to a genuinely untracked path; stderr is now inspected, and only
    git's own distinct messages for an actually-untracked path -- "path does not exist in
    <tree-ish>" (never existed at that path), "exists on disk, but not in '<tree-ish>'"
    (untracked working-tree file), or "invalid object name 'HEAD'" (unborn HEAD, no commits at
    all) -- are treated as the definite refusal; everything else is
    ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE.
    round 2g (sol/fable MAJOR): the repository-DISCOVERY call (`git rev-parse --show-toplevel`,
    below) had the identical any-failure-becomes-a-verdict defect and is fixed the same way --
    stderr inspected, only git's own "fatal: not a git repository" text is the definite
    ATTR3_FIXTURE_NOT_IN_A_REPO refusal, everything else (dubious ownership, a corrupted repo
    config, an I/O error) is ATTR3_FIXTURE_GIT_UNAVAILABLE.
    round 2h (sol/fable MAJOR, independently found): the show-toplevel match landed with the exact
    same unanchored-substring shape the HEAD:<path> match below already carried -- a bare
    'not a git repository' match, matchable by any stderr line that happens to contain that
    phrase, not only git's own fatal verdict. Anchored to require the 'fatal: ' prefix git itself
    always emits ahead of it (confirmed empirically: `git rev-parse --show-toplevel` outside any
    repository prints exactly "fatal: not a git repository (or any of the parent directories):
    .git" on this git version), narrowing, not closing, the edge -- this is still a substring
    match on the joined stderr text, not an anchored `^fatal: ...$` match against a single known
    line, so a hypothetical advisory line that itself echoes "fatal: not a git repository" as
    quoted text (rather than as git's own verdict) would still match. Three edges are now KNOWN
    and left OPEN, not fixed here: the HEAD:<path> lookup's stderr match for "invalid object name"
    is unanchored and could in principle match a corrupted-ref message that is not actually an
    unborn HEAD; the show-toplevel match above, narrowed but not fully anchored, for the same
    reason; and a fixture between int32.MaxValue and the practical process-memory ceiling can OOM
    the `byte[]` allocation below and surface as a raw, untokened exception rather than
    ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$RepoRoot = '',
        [ref]$Sha256Pin
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "ATTR3_FIXTURE_MISSING $Path"
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "ATTR3_FIXTURE_GIT_UNAVAILABLE git is required to verify $Path against its committed blob"
    }
    $full = [IO.Path]::GetFullPath($Path)
    $dir = [IO.Path]::GetDirectoryName($full)
    $trustedRoot = if ([string]::IsNullOrWhiteSpace($RepoRoot)) { '' } else { ([IO.Path]::GetFullPath($RepoRoot)).TrimEnd('\') }
    if ($trustedRoot -and -not $full.StartsWith($trustedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "ATTR3_FIXTURE_NOT_IN_A_REPO $full does not resolve under the trusted repository root $trustedRoot"
    }
    # round 2g (sol/fable MAJOR): this used to discard stderr (2>$null) and fold EVERY failure --
    # dubious ownership, a corrupted repo config, a git I/O error -- into the definite refusal
    # ATTR3_FIXTURE_NOT_IN_A_REPO, exactly the any-failure-becomes-a-verdict shape the HEAD:<path>
    # lookup below was already fixed for in round 2e. stderr is now inspected the same way: only
    # git's own distinct "fatal: not a git repository" text is a genuine not-in-a-repo verdict
    # (round 2h: anchored to the "fatal: " prefix git itself always emits ahead of it, narrowing --
    # not closing -- the unanchored-substring edge round 2g shipped); every other
    # failure is ATTR3_FIXTURE_GIT_UNAVAILABLE (already a registered indeterminate token), not a
    # content verdict.
    $rawShowToplevel = & git -C $dir rev-parse --show-toplevel 2>&1
    $discoveredRoot = (($rawShowToplevel | Where-Object { $_ -is [string] }) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($discoveredRoot)) {
        $stderrText = (($rawShowToplevel | Where-Object { $_ -is [Management.Automation.ErrorRecord] } |
            ForEach-Object { $_.ToString() }) -join ' ')
        if ($stderrText -match 'fatal: not a git repository') {
            throw "ATTR3_FIXTURE_NOT_IN_A_REPO $full is not inside a git working tree: $stderrText"
        }
        throw "ATTR3_FIXTURE_GIT_UNAVAILABLE git rev-parse --show-toplevel for '$dir' failed for a reason other than the path not being inside a git working tree: $stderrText"
    }
    $discoveredRoot = (($discoveredRoot.Trim()) -replace '/', '\').TrimEnd('\')
    if ($trustedRoot) {
        if (-not [string]::Equals($discoveredRoot, $trustedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "ATTR3_FIXTURE_FOREIGN_REPO $full resolves to git repository '$discoveredRoot', not the trusted root '$trustedRoot' -- a nested repository cannot authorize this fixture's bytes"
        }
        $repoRootForGit = $trustedRoot
    } else {
        if (-not $full.StartsWith($discoveredRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "ATTR3_FIXTURE_NOT_IN_A_REPO $full does not resolve under its own repository root $discoveredRoot"
        }
        $repoRootForGit = $discoveredRoot
    }
    $relative = ($full.Substring($repoRootForGit.Length + 1)) -replace '\\', '/'

    # ATTR3-ADMIT-CONTENT-PIN-1 round 2d: ONE handle, opened deny-write for its whole lifetime, is
    # read ONCE into a buffer. Both the working-tree comparison hash below and -Sha256Pin's value
    # (on success) are derived from that SAME buffer -- there is no internal gap left for a swap
    # to win. FileShare.Read denies any concurrent writer for as long as this handle stays open
    # (and, incidentally, blocks a rename-based swap too, since that needs delete access we never
    # grant).
    try {
        $stream = [IO.File]::Open($full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    } catch {
        throw "ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE could not open $full for a write-denying read: $($_.Exception.Message)"
    }
    try {
        if ($stream.Length -gt [int32]::MaxValue) {
            throw "ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE $full is too large ($($stream.Length) bytes) to hash from a single in-memory buffer"
        }
        $bytes = [byte[]]::new([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) {
                throw "ATTR3_FIXTURE_CONTENT_PIN_UNBINDABLE ${full}: read stopped after $offset of $($bytes.Length) bytes"
            }
            $offset += $read
        }
    } finally {
        $stream.Dispose()
    }
    $workingHash = Get-AttrCudaGitBlobHashFromBytes -Bytes $bytes -RepoRoot $repoRootForGit -RelativePath $relative

    # round 2e (sol/fable, PR #140): stderr is captured (2>&1, not discarded) so a genuine "this
    # path was never committed" refusal -- git's own distinct fatal text for a tree lookup that
    # otherwise ran fine -- can be told apart from ANY OTHER git failure here (a corrupted object
    # store, an I/O error, an unexpected git-version message). Only the former is a verdict this
    # function can stand behind as a definite refusal; everything else fails toward "could not
    # determine" rather than silently becoming the same refusal as a genuinely untracked file.
    $rawHeadLookup = & git -C $repoRootForGit rev-parse "HEAD:$relative" 2>&1
    $committedHash = (($rawHeadLookup | Where-Object { $_ -is [string] }) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($committedHash)) {
        $stderrText = (($rawHeadLookup | Where-Object { $_ -is [Management.Automation.ErrorRecord] } |
            ForEach-Object { $_.ToString() }) -join ' ')
        if ($stderrText -match 'does not exist in|exists on disk, but not in|invalid object name') {
            throw "ATTR3_FIXTURE_NOT_COMMITTED HEAD:$relative could not be resolved in $repoRootForGit"
        }
        throw "ATTR3_FIXTURE_HEAD_LOOKUP_UNAVAILABLE HEAD:$relative lookup in $repoRootForGit failed for a reason other than the path never having been committed: $stderrText"
    }
    $committedHash = $committedHash.Trim().ToLowerInvariant()
    if ($workingHash -ne $committedHash) {
        throw "ATTR3_FIXTURE_WORKING_TREE_DIRTY $relative working-tree blob $workingHash differs from the committed blob $committedHash"
    }
    if ($null -ne $Sha256Pin) {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $Sha256Pin.Value = (($sha256.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
        } finally {
            $sha256.Dispose()
        }
    }
    $workingHash
}

function Resolve-AttrCudaCommittedBlobId {
    <#
    .SYNOPSIS
    Resolve the git blob id of a repo-relative path AS COMMITTED at a given commit.
    .DESCRIPTION
    Generator-only: never embedded into an emitted job, because the measurement host has no
    checkout and no git. This lets two independent generators -- one that pins an expected
    hash into the attribution job, one that stages the matching bytes into the cache -- each
    derive the SAME blob id from the SAME -SourceCommit without either taking the other's word
    for it (ATTR3-SMOKE-RUNNER-PIN-1).
    Throws ATTRCUDA_BLOB_UNRESOLVED.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$Commit,

        [Parameter(Mandatory = $true)]
        [string]$RepoRelativePath
    )

    $blobId = (& git -C $RepoRoot rev-parse "${Commit}:${RepoRelativePath}" 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($blobId)) {
        throw "ATTRCUDA_BLOB_UNRESOLVED could not resolve ${Commit}:${RepoRelativePath} in $RepoRoot"
    }
    $blobId = $blobId.Trim()
    if ($blobId -notmatch '^[0-9a-f]{40}$') {
        throw "ATTRCUDA_BLOB_UNRESOLVED ${Commit}:${RepoRelativePath} did not resolve to a blob id (got '$blobId')"
    }
    $blobId
}

function Save-AttrCudaCommittedBlobBytes {
    <#
    .SYNOPSIS
    Write a git blob's exact committed bytes to -Destination and return their sha256.
    .DESCRIPTION
    Generator-only, same reason as Resolve-AttrCudaCommittedBlobId. `git cat-file blob <id>` is
    streamed to -Destination through Start-Process -RedirectStandardOutput, which redirects the
    child process's raw stdout HANDLE straight to the file -- never through a PowerShell text
    pipeline, which decodes/re-encodes command output and would silently rewrite CRLF sequences
    on the way through. Byte-exactness is the entire point: hashing anything else (a working-tree
    checkout, a captured `git show` string) would pin bytes that a core.autocrlf setting or a
    dirty tree could quietly disagree with.
    ATTR3-SMOKE-RUNNER-PIN-1 (BLOCKER, round 2): -RepoRoot used to be passed as a `-C $RepoRoot`
    pair inside -ArgumentList, which Start-Process joins into a single command line WITHOUT
    quoting each element. The real repository root contains a space
    (`C:\!Layi Wkspc\MLV-App`), so that command line handed git the bare token `Wkspc\MLV-App` as
    if it were the next argument, and every real-repository call threw ATTRCUDA_BLOB_READ_FAILED.
    -WorkingDirectory sets the child process's start directory directly (never tokenized onto the
    command line), so no argument here can ever contain a space to mis-split.
    Throws ATTRCUDA_BLOB_READ_FAILED.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$BlobId,

        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    $proc = Start-Process -FilePath 'git' -WorkingDirectory $RepoRoot -ArgumentList @('cat-file', 'blob', $BlobId) `
        -RedirectStandardOutput $Destination -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "ATTRCUDA_BLOB_READ_FAILED git cat-file blob $BlobId exited $($proc.ExitCode)"
    }
    (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
}

# ATTR3-SMOKE-RUNNER-DEPS-1 round 3 (NARROW BY REDESIGN), round 4 (contract stated honestly,
# PR #144). Round 1 and round 2 both discovered this closure by SCANNING committed text -- first
# a regex over one literal shape (Join-Path $PSScriptRoot '<name>'), then a fail-closed AST
# literal scan layered on top of it. A design swarm ruled both undiscoverable-by-patching: a
# scanner built on string literals cannot see an extension-less load
# (`& "$PSScriptRoot\helper"`) or a bareword `Import-Module $PSScriptRoot/modx` -- both load
# code; both scanners returned nothing for either -- and the literal-scan classifier matched a
# resolved dependency by BASENAME alone, so an absolute path like 'C:\evil\provenance-stamp.ps1'
# classified cleanly just by sharing a name with a staged file. Removing the heuristic from the
# trust path removes both gaps permanently: the closure is now this EXPLICIT, pinned list --
# never discovered, never inferred.
#
# WHAT Assert-AttrCudaClosureComplete (below) ACTUALLY PROVES, STATED HONESTLY. It is a
# REGRESSION TRIPWIRE over these six reviewed, byte-pinned files at generator time -- never an
# exhaustive proof that no future edit to them can smuggle in an unstaged load. PowerShell
# resolves some commands dynamically (a computed string, a resolved alias), and no static census
# can enumerate every spelling of "load a file" a determined future edit could use. What it DOES
# guarantee: every load site Get-AttrCudaScriptLoadSites' AST census can see in the manifest's
# own committed text -- including a module-qualified or aliased loader name and any abbreviated
# Add-Type -Path/-LiteralPath parameter (sol round-4 majors 1 and 3) -- classifies as a manifest
# sibling, a scriptblock-only invocation, a pathless Add-Type, or a reviewed exact-pair exclusion,
# or generation refuses outright with ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE. An alias
# definition (Set-Alias/sal/New-Alias/nal) is never resolved at census time -- it is ALWAYS
# unclassified, because what it names is undecidable statically (round-4 sol major 2).
#
# THE SAFETY PROPERTY FOR WHATEVER THIS CENSUS CANNOT SEE IS THE RUNTIME PATH, NOT THIS
# FUNCTION: a staged file that fails to load in the smoke child is reported as SMOKE_RUN_FAILED
# (playback-attr-3-cuda-job.ps1's failure branch, ~:734-780), never silently masked as a
# PresentMon timeout or a clean result. Today's six real files contain none of the forms this
# census cannot see (hub-verified against the real repo by
# test_the_real_current_repo_at_head_classifies_cleanly); this census exists to catch a
# regression the moment one of these six files is edited to add one, not to prove no such form
# could ever exist anywhere PowerShell can run.
#
# ATTR3-VISUAL-QUALITY-EVIDENCE-1 round 2: added gui-smoke-color-artifact-scan.ps1 -- round 1
# had the runner dot-source it directly (~:92) without staging it, so this same census caught the
# gap the same way it was designed to (ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE at that site).
#
# CUDA-S4-TEXTURE-ROUTE-CLAMP-1 round 2: added gui-smoke-gpu-texture-route-validation.ps1 --
# run-release-gui-smoke.ps1 dot-sources it (~:93) but round 1 never staged it, so the same
# closure-completeness tests caught the same gap the same way, again.
#
# UM-DISPLAY-SELECT-AND-LOG-1 round 3: added gui-smoke-display-identity.ps1 -- the ONE parser for
# the app's gui_smoke.display_screen/display_target/window_placement lines, dot-sourced by the
# runner and embedded verbatim into the attribution job (see that file's header).
#
# PLAYBACK-LENGTH-ENFORCE-1: added gui-smoke-length-gate.ps1 -- the runner dot-sources it (the gate that
# refuses footage under 20 s or shorter than the play window), so the venue's staged closure must
# carry it or the runner dies at its first dot-source. (The name deliberately avoids the word this
# build-route module is forbidden to contain: see NoFootageTokensTests.)
$script:AttrCudaSmokeRunnerClosureManifest = @(
    'tools/profiling/run-release-gui-smoke.ps1',
    'tools/profiling/gui-smoke-screenshot-provenance.ps1',
    'tools/profiling/provenance-stamp.ps1',
    'tools/profiling/gui-smoke-process-boundary.psm1',
    'tools/profiling/gui-smoke-color-artifact-scan.ps1',
    'tools/profiling/gui-smoke-gpu-texture-route-validation.ps1',
    'tools/profiling/gui-smoke-display-identity.ps1',
    'tools/profiling/gui-smoke-length-gate.ps1'
)

function Get-AttrCudaSmokeRunnerClosureManifest {
    <#
    .SYNOPSIS
    The pinned, explicit smoke-runner closure, as repo-relative paths, root script first.
    #>
    [CmdletBinding()]
    param()
    return @($script:AttrCudaSmokeRunnerClosureManifest)
}

function Get-AttrCudaScriptLoadSites {
    <#
    .SYNOPSIS
    AST census of every code-loading SITE in PowerShell source text. FAILS CLOSED on a parse
    error. Never a text/regex scan.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-DEPS-1 round 3. Parses with
    [System.Management.Automation.Language.Parser] and returns every:
      - CommandAst invoked with the dot (.) or call (&) operator (its target may be a literal, a
        variable, or a dynamic expression -- classification is the caller's job);
      - CommandAst named (case-insensitively) Import-Module/ipmo, Start-Process/saps/start,
        pwsh/powershell(.exe), Invoke-Expression/iex, Invoke-Command/icm, Start-Job,
        Start-ThreadJob, Add-Type, Set-Alias/sal or New-Alias/nal -- matched on the segment AFTER
        the last `\`, so a module-qualified invocation
        (`Microsoft.PowerShell.Core\Import-Module ...`) is recognized exactly like the
        unqualified form, never invisible to this census (round 4, sol PR #144 major 1);
      - UsingStatementAst (a `using module|namespace|assembly` statement -- never a C# `using`
        keyword sitting inert inside a string literal handed to Add-Type, which this AST walk
        does not descend into because it is a StringConstantExpressionAst, not PowerShell code);
      - InvokeMemberExpressionAst whose member name is (case-insensitively) Create, AddScript,
        AddCommand, InvokeScript or NewScriptBlock;
      - a STATIC InvokeMemberExpressionAst named (case-insensitively) Start on the type
        expression [System.Diagnostics.Process] / [Diagnostics.Process] -- process launch, per
        fable's round-3 minor (round 4, PR #144).
    This finds every SITE that can execute or generate code regardless of how its target is
    spelled -- an extension-less `& "$PSScriptRoot\helper"` or a bareword
    `Import-Module $PSScriptRoot/modx` are both real AST nodes the parser sees even though
    neither contains a string literal a text scanner could match on. Classifying what each site
    loads is Assert-AttrCudaClosureComplete's job, never this function's.
    Throws ATTRCUDA_SCRIPT_PARSE_ERROR on any parse error: a file this scan cannot understand is
    never silently treated as clean.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$ScriptText,

        [string]$SourceLabel = '<script>'
    )

    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($ScriptText, [ref]$tokens, [ref]$parseErrors)
    if ($null -ne $parseErrors -and $parseErrors.Count -gt 0) {
        $messages = ($parseErrors | ForEach-Object { $_.Message }) -join '; '
        throw "ATTRCUDA_SCRIPT_PARSE_ERROR ${SourceLabel}: $messages"
    }

    $loaderCommandNames = @(
        'Import-Module', 'ipmo',
        'Start-Process', 'saps', 'start',
        'pwsh', 'pwsh.exe', 'powershell', 'powershell.exe',
        'Invoke-Expression', 'iex',
        'Invoke-Command', 'icm',
        'Start-Job', 'Start-ThreadJob',
        'Add-Type',
        'Set-Alias', 'sal', 'New-Alias', 'nal'
    )
    $memberNames = @('Create', 'AddScript', 'AddCommand', 'InvokeScript', 'NewScriptBlock')
    $sites = [System.Collections.Generic.List[object]]::new()

    $commandAsts = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true)
    foreach ($command in $commandAsts) {
        $rawCommandName = $command.GetCommandName()
        # sol PR #144 round 4 major 1: GetCommandName() returns the QUALIFIED string for a
        # module-qualified invocation (`Microsoft.PowerShell.Core\Import-Module ...`), which
        # never matched $loaderCommandNames by exact string. Matching on the segment after the
        # last `\` recognizes the qualified and unqualified spellings identically.
        $commandName = if ($null -ne $rawCommandName) {
            $lastSeparator = $rawCommandName.LastIndexOf('\')
            if ($lastSeparator -ge 0) { $rawCommandName.Substring($lastSeparator + 1) } else { $rawCommandName }
        } else { $null }
        $isOperatorInvocation = ($command.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Dot) -or
            ($command.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand)
        $isNamedLoader = ($null -ne $commandName) -and (@($loaderCommandNames) -icontains $commandName)
        if ($isOperatorInvocation -or $isNamedLoader) {
            [void]$sites.Add([pscustomobject]@{
                kind = if ($isOperatorInvocation) { 'InvocationOperator' } else { 'CommandName' }
                operator = [string]$command.InvocationOperator
                commandName = $commandName
                memberName = $null
                text = $command.Extent.Text
                line = $command.Extent.StartLineNumber
                ast = $command
            })
        }
    }

    $usingAsts = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.UsingStatementAst] }, $true)
    foreach ($using in $usingAsts) {
        [void]$sites.Add([pscustomobject]@{
            kind = 'UsingStatement'
            operator = $null
            commandName = $null
            memberName = $null
            text = $using.Extent.Text
            line = $using.Extent.StartLineNumber
            ast = $using
        })
    }

    $memberAsts = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)
    foreach ($member in $memberAsts) {
        $memberNameValue = $null
        if ($member.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
            $memberNameValue = $member.Member.Value
        }
        $isTrackedMemberCall = ($null -ne $memberNameValue) -and (@($memberNames) -icontains $memberNameValue)
        # fable minor (round 3), fixed round 4: narrowly scoped to the one static member this
        # closure's real files use to launch anything -- [System.Diagnostics.Process]::Start --
        # rather than every member named Start, which would flag unrelated instance calls
        # (e.g. a Stopwatch or a Job) that never load code.
        $isProcessStartCall = $false
        if ($member.Static -and $null -ne $memberNameValue -and $memberNameValue -ieq 'Start' -and
            $member.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
            $typeFullName = $member.Expression.TypeName.FullName
            if ($typeFullName -ieq 'System.Diagnostics.Process' -or $typeFullName -ieq 'Diagnostics.Process') {
                $isProcessStartCall = $true
            }
        }
        if ($isTrackedMemberCall -or $isProcessStartCall) {
            [void]$sites.Add([pscustomobject]@{
                kind = if ($isProcessStartCall) { 'ProcessStart' } else { 'MemberCall' }
                operator = $null
                commandName = $null
                memberName = $memberNameValue
                text = $member.Extent.Text
                line = $member.Extent.StartLineNumber
                ast = $member
            })
        }
    }

    return @($sites)
}

function Get-AttrCudaPSScriptRootJoinPathLiteral {
    <#
    .SYNOPSIS
    Private classifier for class (a): if $CommandElement is (a parenthesized)
    `Join-Path $PSScriptRoot '<bare file name>'`, return the bare file name; otherwise $null.
    .DESCRIPTION
    Deliberately narrow and syntactic, never evaluated: the root argument must be the literal
    variable $PSScriptRoot (not $root or any other name -- that is the exact gap sol's basename-
    collision case exploits) and the name argument must be a plain string constant with no path
    separator in it (a bare file name, never a relative or absolute path) -- ValidatePattern-style
    refusal-by-shape rather than a resolve-and-hope.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$CommandElement)

    $expr = $CommandElement
    if ($expr -is [System.Management.Automation.Language.ParenExpressionAst]) {
        $elements = @($expr.Pipeline.PipelineElements)
        if ($elements.Count -ne 1 -or -not ($elements[0] -is [System.Management.Automation.Language.CommandAst])) { return $null }
        $expr = $elements[0]
    }
    if (-not ($expr -is [System.Management.Automation.Language.CommandAst])) { return $null }
    if ($expr.GetCommandName() -ine 'Join-Path') { return $null }
    $args = @($expr.CommandElements | Select-Object -Skip 1)
    if ($args.Count -lt 2) { return $null }
    $rootArg = $args[0]
    $nameArg = $args[1]
    $rootIsPSScriptRoot = ($rootArg -is [System.Management.Automation.Language.VariableExpressionAst]) -and
        ($rootArg.VariablePath.UserPath -ieq 'PSScriptRoot')
    if (-not $rootIsPSScriptRoot) { return $null }
    if (-not ($nameArg -is [System.Management.Automation.Language.StringConstantExpressionAst])) { return $null }
    $name = $nameArg.Value
    if ([string]::IsNullOrWhiteSpace($name) -or $name -match '[\\/]') { return $null }
    return $name
}

function Get-AttrCudaAssignmentExpression {
    <#
    .SYNOPSIS
    Private helper: unwrap an AssignmentStatementAst's Right side to the expression it assigns,
    or $null if it is not a single simple expression.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Assignment)

    $right = $Assignment.Right
    # PowerShell's parser hands back Right as a bare CommandExpressionAst for a simple `$x = ...`
    # assignment, but wraps it in a one-element PipelineAst in other shapes (e.g. inside a nested
    # statement); both are unwrapped the same way here.
    if ($right -is [System.Management.Automation.Language.PipelineAst] -and $right.PipelineElements.Count -eq 1) {
        $right = $right.PipelineElements[0]
    }
    if ($right -is [System.Management.Automation.Language.CommandExpressionAst]) {
        return $right.Expression
    }
    return $null
}

function Test-AttrCudaScriptblockOnlyInvocationTarget {
    <#
    .SYNOPSIS
    Private classifier for class (b): true when $CommandAst's `.`/`&` target is a variable whose
    ONLY assignment anywhere in $Ast is a scriptblock literal, or a [scriptblock]-typed parameter.
    .DESCRIPTION
    A variable assigned a scriptblock literal, or nothing else, can only ever invoke code that
    was already visible to this same census as a literal `{ ... }` -- there is no separate file
    load to miss. A variable with even one non-scriptblock-literal assignment (e.g. a resolved
    executable path) is refused here and falls through to be classified some other way or thrown
    as unclassified: this check proves nothing was smuggled through under a scriptblock's cover.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$CommandAst,
        [Parameter(Mandatory = $true)]$Ast
    )

    if ($CommandAst.CommandElements.Count -lt 1) { return $false }
    $target = $CommandAst.CommandElements[0]
    if (-not ($target -is [System.Management.Automation.Language.VariableExpressionAst])) { return $false }
    $varName = $target.VariablePath.UserPath

    $paramAsts = $Ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.ParameterAst] }, $true)
    foreach ($parameter in $paramAsts) {
        if ($parameter.Name.VariablePath.UserPath -ine $varName) { continue }
        foreach ($attribute in $parameter.Attributes) {
            if ($attribute -is [System.Management.Automation.Language.TypeConstraintAst] -and
                $attribute.TypeName.Name -ieq 'scriptblock') {
                return $true
            }
        }
    }

    $assignments = $Ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $node.Left.VariablePath.UserPath -ieq $varName
    }, $true)
    if ($assignments.Count -eq 0) { return $false }
    foreach ($assignment in $assignments) {
        $value = Get-AttrCudaAssignmentExpression -Assignment $assignment
        if (-not ($value -is [System.Management.Automation.Language.ScriptBlockExpressionAst])) { return $false }
    }
    return $true
}

function Test-AttrCudaCommandHasPathParameter {
    <#
    .SYNOPSIS
    Private classifier for class (c): true if $CommandAst names a -Path or -LiteralPath
    parameter, spelled in full, abbreviated, or by its PSPath/LP alias. Used to refuse
    auto-classifying an Add-Type that loads FROM a file.
    .DESCRIPTION
    sol PR #144 round 4 major 3: the exact `-ieq 'Path'`/`-ieq 'LiteralPath'` comparison this
    replaced read `Add-Type -Pat x.cs` or `-Lit x.cs` as pathless, because PowerShell accepts any
    unambiguous parameter-name PREFIX -- it bound those abbreviations to -Path/-LiteralPath at
    runtime even though the AST's parameter name is the shorter text actually written. A
    parameter counts as a path parameter here if 'Path' or 'LiteralPath' STARTS WITH the written
    name (case-insensitive, so any valid prefix abbreviation is caught, including the empty-string
    edge of neither name), or if the written name is the PSPath or LP alias. A parameter that is
    merely AMBIGUOUS between a path name and some other Add-Type parameter (e.g. `-Pa` could bind
    to -Path or -PassThru) still counts as a path parameter here: this classifier's job is to
    refuse auto-classifying, never to resolve the ambiguity itself, so it fails closed.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$CommandAst)

    foreach ($element in $CommandAst.CommandElements) {
        if ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
            $name = $element.ParameterName
            if ([string]::IsNullOrEmpty($name)) { continue }
            if ('Path'.StartsWith($name, [StringComparison]::OrdinalIgnoreCase) -or
                'LiteralPath'.StartsWith($name, [StringComparison]::OrdinalIgnoreCase) -or
                $name -ieq 'PSPath' -or $name -ieq 'LP') {
                return $true
            }
        }
    }
    return $false
}

# ATTR3-SMOKE-RUNNER-DEPS-1 round 3. The explicit, reasoned exclusion list kept NEXT TO the
# classifier it qualifies: every load site Get-AttrCudaScriptLoadSites finds in a manifest file
# must be classified (a)/(b)/(c) above or listed here with a reason, keyed on the EXACT
# (repoRelativePath, site text) pair -- never on a basename or a substring -- so an exclusion can
# never silently widen to cover a different, unreviewed site.
$script:AttrCudaClosureScanExclusions = @(
    [pscustomobject]@{
        repoRelativePath = 'tools/profiling/run-release-gui-smoke.ps1'
        literal = '& $detectorPwsh @detectorArgs 2>&1'
        reason = 'The dormant detect-playback-artifacts.ps1 launch, reached only under ' +
            '-DetectPlaybackArtifacts. The ATTR-3 attribution job (playback-attr-3-cuda-job.ps1) ' +
            'never passes that switch in its emitted smoke invocation -- ' +
            'test_the_emitted_smoke_command_never_passes_detectplaybackartifacts asserts this -- ' +
            'and the runner itself Test-Path-guards the call, falling back to verdict="no-data" ' +
            'if the file is ever missing. Dormant for this route by construction, not by luck.'
    },
    [pscustomobject]@{
        repoRelativePath = 'tools/profiling/run-release-gui-smoke.ps1'
        literal = '[System.Diagnostics.Process]::Start($startInfo)'
        reason = 'Launches the hash-pinned, already-deployed application executable -- ' +
            '$startInfo.FileName is set to $exe, the built app path resolved before this line, ' +
            'never a $PSScriptRoot sibling script. Process.Start executes an OS binary directly; ' +
            'it does not load or run PowerShell/.NET code from a file this census needs to see ' +
            '(round 4, fable round-3 minor, PR #144). UM-DISPLAY-SELECT-AND-LOG-1 round 4 (fable ' +
            'BLOCKER 1): the runner''s --display-prefer feature probe, ' +
            'Test-GuiSmokeDisplayPreferSupport, launches through the identical literal -- its ' +
            '$startInfo.FileName is the same pinned application executable ($ExePath is passed ' +
            '$exe), run with `--gui-smoke-playback --help` under a hard timeout and killed by pid. ' +
            'It replaced a bare `& $exe --help 2>&1` site whose own exclusion was deleted with it.'
    },
    [pscustomobject]@{
        repoRelativePath = 'tools/profiling/run-release-gui-smoke.ps1'
        literal = '& taskkill.exe /PID $probePid /T /F 2>&1'
        reason = 'UM-DISPLAY-SELECT-AND-LOG-1 round 4 (fable BLOCKER 1): the kill-by-pid of the ' +
            '--display-prefer feature probe that outlived its timeout (Test-GuiSmokeDisplayPreferSupport). ' +
            'taskkill.exe is the Windows system binary, named bare and resolved by the OS -- never ' +
            'a $PSScriptRoot sibling script -- and its only argument is the numeric pid Process.Start ' +
            'returned for the probe. It loads and runs no PowerShell/.NET code from a file this ' +
            'census needs to see.'
    }
)

function Test-AttrCudaClosureScanExclusionMatch {
    <#
    .SYNOPSIS
    True if a (repoRelativePath, exact site text) pair is on the explicit exclusion list above.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRelativePath,

        [Parameter(Mandatory = $true)]
        [string]$Literal
    )

    foreach ($exclusion in $script:AttrCudaClosureScanExclusions) {
        if ($exclusion.repoRelativePath -eq $RepoRelativePath -and $exclusion.literal -eq $Literal) {
            return $true
        }
    }
    return $false
}

function Assert-AttrCudaClosureComplete {
    <#
    .SYNOPSIS
    Prove the pinned smoke-runner closure manifest is COMPLETE: every load site in every manifest
    file's own committed text classifies cleanly, and the resolved class-(a) targets are EXACTLY
    the manifest's non-root siblings, in both directions.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-DEPS-1 round 3, round 4 (PR #144). Generator-time (and CI-time) only --
    never embedded in an emitted job, same reason as Resolve-AttrCudaCommittedBlobId. This is a
    REGRESSION TRIPWIRE over these six reviewed files, not an exhaustive proof -- see the module
    header above the manifest for the honest statement of what it does and does not guarantee. It
    runs Get-AttrCudaScriptLoadSites over each manifest file's committed text at -Commit and
    requires every site to be exactly one of:
      (a) `Join-Path $PSScriptRoot '<bare file name>'`, whose resolved repo-relative path is a
          manifest entry, by FULL PATH equality -- never by basename alone;
      (b) `&`/`.` on a scriptblock-only variable or a [scriptblock]-typed parameter
          (Test-AttrCudaScriptblockOnlyInvocationTarget);
      (c) `Add-Type` with no -Path/-LiteralPath, matched by prefix and alias so an abbreviated
          parameter cannot pass as pathless (Test-AttrCudaCommandHasPathParameter);
      (d) a pinned exclusion (Test-AttrCudaClosureScanExclusionMatch).
    A Set-Alias/sal/New-Alias/nal site is never a candidate for (a)-(c) and matches (d) only if
    explicitly pinned there -- its target is never resolved statically, so it is always
    unclassified unless excluded by name (round 4, sol major 2). Anything else throws
    ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE. Finally, the set of resolved
    class-(a) targets must equal the manifest minus its root (first) entry, in both directions --
    a manifest entry never reached by a real load, or a real load that resolves outside the
    manifest, is a completeness failure, not silently accepted either way.
    Throws ATTRCUDA_BLOB_UNRESOLVED if a manifest path is not a committed blob at -Commit,
    ATTRCUDA_SCRIPT_PARSE_ERROR (from Get-AttrCudaScriptLoadSites), or
    ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE (above).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$Commit,

        [string[]]$RepoRelativePaths = $script:AttrCudaSmokeRunnerClosureManifest
    )

    $normalizedPaths = @($RepoRelativePaths | ForEach-Object { $_ -replace '\\', '/' })
    $manifestBasenames = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($path in $normalizedPaths) {
        [void]$manifestBasenames.Add($path.Substring($path.LastIndexOf('/') + 1))
    }
    $rootPath = $normalizedPaths[0]
    $rootName = $rootPath.Substring($rootPath.LastIndexOf('/') + 1)
    $siblingBasenames = [System.Collections.Generic.HashSet[string]]::new([string[]]$manifestBasenames, [StringComparer]::Ordinal)
    [void]$siblingBasenames.Remove($rootName)

    $resolvedTargets = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)

    foreach ($relativePath in $normalizedPaths) {
        $directory = $relativePath.Substring(0, $relativePath.LastIndexOf('/'))
        $blobId = Resolve-AttrCudaCommittedBlobId -RepoRoot $RepoRoot -Commit $Commit -RepoRelativePath $relativePath
        $tempFile = Join-Path ([IO.Path]::GetTempPath()) "attrcuda-census-$([Guid]::NewGuid().ToString('N')).tmp"
        try {
            [void](Save-AttrCudaCommittedBlobBytes -RepoRoot $RepoRoot -BlobId $blobId -Destination $tempFile)
            $text = [IO.File]::ReadAllText($tempFile)
        } finally {
            if (Test-Path -LiteralPath $tempFile) { Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue }
        }

        # Parsed once here (beyond Get-AttrCudaScriptLoadSites' own internal parse) so the
        # scriptblock-only classifier below can search the WHOLE file for $varName's assignments
        # and parameter declarations -- not just the neighborhood of the one site being classified.
        $fileTokens = $null
        $fileParseErrors = $null
        $fileAst = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$fileTokens, [ref]$fileParseErrors)

        foreach ($site in (Get-AttrCudaScriptLoadSites -ScriptText $text -SourceLabel $relativePath)) {
            $classified = $false

            if ($site.kind -eq 'InvocationOperator' -or $site.kind -eq 'CommandName') {
                $argIndex = if ($site.kind -eq 'InvocationOperator') { 0 } else { 1 }
                if ($site.ast.CommandElements.Count -gt $argIndex) {
                    $target = Get-AttrCudaPSScriptRootJoinPathLiteral -CommandElement $site.ast.CommandElements[$argIndex]
                    if ($null -ne $target) {
                        $resolvedPath = "$directory/$target"
                        if ($manifestBasenames.Contains($target) -and ($normalizedPaths -contains $resolvedPath)) {
                            [void]$resolvedTargets.Add($target)
                            $classified = $true
                        }
                    }
                }
            }

            if (-not $classified -and $site.kind -eq 'InvocationOperator') {
                if (Test-AttrCudaScriptblockOnlyInvocationTarget -CommandAst $site.ast -Ast $fileAst) {
                    $classified = $true
                }
            }

            if (-not $classified -and $site.kind -eq 'CommandName' -and $site.commandName -ieq 'Add-Type') {
                if (-not (Test-AttrCudaCommandHasPathParameter -CommandAst $site.ast)) {
                    $classified = $true
                }
            }

            if (-not $classified -and (Test-AttrCudaClosureScanExclusionMatch -RepoRelativePath $relativePath -Literal $site.text)) {
                $classified = $true
            }

            if (-not $classified) {
                throw ("ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE ${relativePath}: site '$($site.text)' " +
                    "(kind=$($site.kind)) is neither a resolved `$PSScriptRoot manifest load (full-path " +
                    "match, never basename), a scriptblock-only invocation, an Add-Type with no -Path, " +
                    "nor on the pinned exclusion list next to Test-AttrCudaClosureScanExclusionMatch in " +
                    "AttrCudaArtifacts.psm1 -- classify it before this closure can be trusted")
            }
        }
    }

    $missing = @($siblingBasenames | Where-Object { -not $resolvedTargets.Contains($_) })
    $extra = @($resolvedTargets | Where-Object { -not $siblingBasenames.Contains($_) })
    if ($missing.Count -gt 0 -or $extra.Count -gt 0) {
        throw ("ATTRCUDA_UNCLASSIFIED_SCRIPT_REFERENCE closure completeness mismatch: " +
            "missing=[$($missing -join ',')] extra=[$($extra -join ',')] -- the pinned manifest and " +
            "the resolved `$PSScriptRoot load sites across its own files must name exactly the same " +
            "siblings, in both directions")
    }
}

function Resolve-AttrCudaSmokeRunnerClosure {
    <#
    .SYNOPSIS
    Resolve the PINNED smoke-runner closure's committed bytes, AS COMMITTED at a given commit.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-DEPS-1 round 3 (NARROW BY REDESIGN). No scan, no recursion, no traversal:
    the set of paths is Get-AttrCudaSmokeRunnerClosureManifest, proved complete against the real
    script text by Assert-AttrCudaClosureComplete (call that first; this function does not
    re-verify completeness, only resolution). Returns an ordered list of [pscustomobject]@{ name;
    repoRelativePath; blobId; sha256 }, in manifest order (root script first).
    Throws ATTRCUDA_BLOB_UNRESOLVED (from Resolve-AttrCudaCommittedBlobId) if a pinned path is not
    a committed blob at -Commit.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$Commit,

        [string[]]$RepoRelativePaths = $script:AttrCudaSmokeRunnerClosureManifest
    )

    $closure = [System.Collections.Generic.List[object]]::new()
    foreach ($relativePath in $RepoRelativePaths) {
        $normalized = $relativePath -replace '\\', '/'
        $name = $normalized.Substring($normalized.LastIndexOf('/') + 1)
        $blobId = Resolve-AttrCudaCommittedBlobId -RepoRoot $RepoRoot -Commit $Commit -RepoRelativePath $normalized
        $tempFile = Join-Path ([IO.Path]::GetTempPath()) "attrcuda-closure-$([Guid]::NewGuid().ToString('N')).tmp"
        try {
            $sha256 = Save-AttrCudaCommittedBlobBytes -RepoRoot $RepoRoot -BlobId $blobId -Destination $tempFile
        } finally {
            if (Test-Path -LiteralPath $tempFile) { Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue }
        }
        [void]$closure.Add([pscustomobject]@{
            name = $name
            repoRelativePath = $normalized
            blobId = $blobId
            sha256 = $sha256
        })
    }
    return @($closure)
}

function Get-AttrCudaClosureDigestHex {
    <#
    .SYNOPSIS
    The content-addressed digest of a resolved dependency closure.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-DEPS-1. sha256 of the sorted `<sha256>  <name>` lines (two-space
    separator, sha256sum-shaped) of the closure's entries, newline-joined with a trailing
    newline -- so any two generators that resolve the SAME closure at the SAME commit derive
    the SAME digest regardless of discovery order, and the published cache directory name
    (`smoke-runner-<digest16>`) is a pure function of the closure's content.
    Sorted with [StringComparer]::Ordinal (fable minor, PR #144 round 3, carried forward from
    round 2's culture-aware Sort-Object): the digest is a cross-host content address and must
    not depend on the sorting host's culture, even though the 64-hex sha256 prefix makes an
    actual ordinal/culture divergence practically impossible here. The Python test's sorted()
    is ordinal, so this makes the two definitions exact rather than merely agreeing in practice.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Closure
    )

    $lines = [string[]]@($Closure | ForEach-Object { "$($_.sha256)  $($_.name)" })
    [Array]::Sort($lines, [StringComparer]::Ordinal)
    $joined = ($lines -join "`n") + "`n"
    $bytes = [Text.Encoding]::UTF8.GetBytes($joined)
    $stream = [IO.MemoryStream]::new($bytes)
    try {
        (Get-FileHash -InputStream $stream -Algorithm SHA256).Hash.ToLowerInvariant()
    } finally {
        $stream.Dispose()
    }
}

function Test-AttrCudaPathIsReparsePoint {
    <#
    .SYNOPSIS
    True if Path exists and is a reparse point (symlink, junction or mount point); false if it
    is a plain file/directory or does not exist at all.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-DEPS-1 (sol, PR #144 major 2). Test-Path and Get-FileHash both resolve
    THROUGH a reparse point to whatever it targets, so a hash-pin check that only ever calls
    those two can be satisfied by a link whose TARGET -- not the staged, verified directory --
    happens to carry the pinned bytes. Every closure directory and every member is checked with
    this, before its content is trusted, so a link is refused rather than silently followed.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $false }
    return (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Get-AttrCudaClosureDirectoryMismatch {
    <#
    .SYNOPSIS
    Compare a directory against the expected closure EXACT SET; return $null when it matches
    exactly, or a short reason string naming the first mismatch found. Never throws.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-DEPS-1 round 3 (NARROW BY REDESIGN). Round 1/2 defined this exact-set rule
    twice -- once inline in the stager's "already staged" check, once inline in the attribution
    job's ATTRCUDA_SMOKE_RUNNER_STALE gate -- which is exactly how the two could silently drift
    apart. Moved here so both emitted jobs embed the BYTE-IDENTICAL function text
    (Get-AttrCudaEmbeddedFunctionSource), never two copies that only look the same. "Matches
    exactly" means: the directory exists, is not itself a reparse point, has EXACTLY -Entries.Count
    children (no extra entries, no subdirectories among them), none of those children is a reparse
    point, and every -Entries member is present with the pinned sha256.
    -Entries is an array of [pscustomobject]@{ name; sha256 } (sha256 in any case; compared
    case-insensitively). A plain array + -contains, not a HashSet: the template lint
    (attr3_publish_write_scan.ps1 R4) only allowlists specific .NET static/instance members by
    name, and this closure is a handful of files, so an O(n) membership test costs nothing here.
    Returns $null on an exact match; otherwise a reason string. Never throws -- the caller (an
    "already staged?" check vs. a hard pin gate) decides what a mismatch means.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Dir,
        [Parameter(Mandatory = $true)][object[]]$Entries
    )

    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) { return "missing closure directory $Dir" }
    if (Test-AttrCudaPathIsReparsePoint -Path $Dir) { return "closure directory $Dir is a reparse point" }

    # Every EXPECTED entry is checked first, by name, so a refusal names WHICH dependency is
    # stale or missing whenever one is -- before the broader "anything extra?" sweep below, whose
    # own reason (a bare count) would otherwise mask that more useful, specific answer.
    foreach ($entry in $Entries) {
        $path = Join-Path $Dir $entry.name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return "closure directory $Dir is missing $($entry.name)"
        }
        if (Test-AttrCudaPathIsReparsePoint -Path $path) {
            return "closure directory $Dir entry $($entry.name) is a reparse point"
        }
        $actualSha = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha -ne $entry.sha256.ToLowerInvariant()) {
            return "closure directory $Dir entry $($entry.name) sha256 mismatch: expected $($entry.sha256), actual $actualSha"
        }
    }

    # Every expected entry is present and correct -- now prove there is nothing ELSE: no extra
    # file, no extra subdirectory, no reparse point standing in for a plain file.
    $actualEntries = @(Get-ChildItem -LiteralPath $Dir -Force)
    if ($actualEntries.Count -ne $Entries.Count) {
        return "closure directory $Dir has $($actualEntries.Count) entries, expected $($Entries.Count)"
    }
    $expectedNames = @($Entries | ForEach-Object { $_.name })
    foreach ($actual in $actualEntries) {
        if ($expectedNames -notcontains $actual.Name) {
            return "closure directory $Dir has an unexpected entry $($actual.Name)"
        }
        if ($actual.PSIsContainer) {
            return "closure directory $Dir entry $($actual.Name) is a subdirectory"
        }
        if (Test-AttrCudaPathIsReparsePoint -Path $actual.FullName) {
            return "closure directory $Dir entry $($actual.Name) is a reparse point"
        }
    }
    return $null
}

function Assert-AttrCudaWritableFileSlot {
    <#
    .SYNOPSIS
    Prove a publish destination can only ever be written as a plain local FILE, then return it.
    .DESCRIPTION
    sol, PR #133 r3: Copy-Item / Move-Item / Set-Content onto a path that is a junction or a
    directory writes INTO the link target -- possibly outside the job root -- before any hash
    check can fail. Every publish write therefore calls this first. The parent directory must
    exist and must not be a reparse point; the slot itself must be absent or a plain file (which
    is removed so the write creates a fresh file). Anything else throws ATTRCUDA_SLOT_OCCUPIED
    or ATTRCUDA_SLOT_PARENT_IS_LINK, and nothing is written.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path)
    $parent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($full)) -Force -ErrorAction SilentlyContinue
    if ($null -eq $parent -or -not $parent.PSIsContainer) {
        throw "ATTRCUDA_SLOT_PARENT_MISSING $full"
    }
    if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "ATTRCUDA_SLOT_PARENT_IS_LINK $($parent.FullName)"
    }
    $existing = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        $isReparse = (($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        if ($existing.PSIsContainer -or $isReparse) {
            throw "ATTRCUDA_SLOT_OCCUPIED $full is a directory or reparse point; refusing to write through it"
        }
        # OWNER-FOOTAGE-NO-HARDLINK-2: a name that is one of several names of one file object is never
        # the slot's own stale output (every file this tree publishes has exactly one name), so it is
        # refused rather than removed. The remaining pathname remove is the replace-in-place of a
        # fixed, derived PUBLISH name (a canonical artifact name, result.json, a job log): it is
        # allowlisted by name in tools/repo_hygiene/test_owner_footage_no_hardlink_class.py with the
        # reason such a name cannot be a neutral owner-footage name.
        try { $occupant = Get-AttrCudaFileId -Path $full } catch { throw "ATTRCUDA_SLOT_OCCUPANT_UNREADABLE $full" }
        if ($occupant.NumberOfLinks -gt 1) {
            throw "ATTRCUDA_SLOT_OCCUPIED_MULTI_LINK $full has $($occupant.NumberOfLinks) names; refusing to remove one of them"
        }
        Remove-Item -LiteralPath $full -Force -Confirm:$false
    }
    return $full
}

function New-AttrCudaOwnedFileStream {
    <#
    .SYNOPSIS
    Create a NEW file (FileMode.CreateNew), journal the identity read off that very creating handle,
    and only then hand the stream back for writing.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-2. This is how a job comes to OWN a file it deletes later: the record
    is made from the creating handle, durably, before a byte is written. If the record cannot be
    made the file is removed by the identity just read (the stream closed first) and this throws, so
    a caller never holds an unrecorded file it made. The caller must have cleared the slot
    (Assert-AttrCudaWritableFileSlot); a name that is occupied makes CreateNew throw.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$OwnedJournal
    )

    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read, 65536)
    $id = $null
    try {
        $id = Get-AttrCudaFileId -Stream $stream
        Add-AttrCudaOwnedRecord -Journal $OwnedJournal -Path $Path -FileId $id
    } catch {
        $stream.Dispose()
        if ($null -ne $id) { [void](Remove-AttrCudaFileById -Path $Path -FileId $id) }
        throw
    }
    return $stream
}

function Publish-AttrCudaText {
    <#
    .SYNOPSIS
    The ONLY way an emitted job writes text outside its job-owned work tree: slot check and write
    in one call, so a guard can never be separated from the write it guards (sol PR #133 r5).
    .DESCRIPTION
    -OwnedJournal (OWNER-FOOTAGE-NO-HARDLINK-2): when given, the file is created with CreateNew and its
    identity is journalled from the creating handle before the text is written, so the job can later
    delete it by proof (Remove-AttrCudaPartialFile -OwnedJournal). Without it nothing is recorded and
    a later delete helper will leave the name.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][AllowNull()][object]$Value,
        [string]$OwnedJournal = ''
    )

    $slot = Assert-AttrCudaWritableFileSlot -Path $Path
    $text = if ($null -eq $Value) { '' } else { (@($Value) | ForEach-Object { [string]$_ }) -join [Environment]::NewLine }
    if ($OwnedJournal -ne '') {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($text + [Environment]::NewLine)
        $stream = New-AttrCudaOwnedFileStream -Path $slot -OwnedJournal $OwnedJournal
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        return $slot
    }
    [IO.File]::WriteAllText($slot, $text + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    return $slot
}

function Read-AttrCudaBase64Payload {
    <#
    .SYNOPSIS
    Decode a base64-embedded payload and return both its raw bytes and lowercase sha256.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-PIN-1, round 2: the smoke-runner stager embeds the runner's committed
    bytes INLINE in the emitted job (no side file, no inbox -- UmRunDrop.psm1's side-file name
    policy rejects a bare `.ps1`). This exists so [Convert]::FromBase64String never appears as
    literal template text: tools/repo_hygiene/attr3_publish_write_scan.ps1's R4 rule allowlists
    .NET static members in the TEMPLATE by name, and this repository's policy is that a job
    template stays inside that allowlist -- decode-and-hash instead lives here, in the module
    that is the job's actual safety boundary, spliced in and called by NAME like every other
    embedded verifier. Get-FileHash -InputStream is used for the digest (an already-allowlisted
    cmdlet) rather than a raw [Security.Cryptography.SHA256] call, for the same reason.
    Throws ATTRCUDA_BASE64_MALFORMED.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Base64
    )

    try {
        $bytes = [Convert]::FromBase64String($Base64)
    } catch {
        throw "ATTRCUDA_BASE64_MALFORMED payload is not valid base64: $($_.Exception.Message)"
    }
    $stream = [IO.MemoryStream]::new($bytes)
    try {
        $sha256 = (Get-FileHash -InputStream $stream -Algorithm SHA256).Hash.ToLowerInvariant()
    } finally {
        $stream.Dispose()
    }
    [pscustomobject]@{ bytes = $bytes; sha256 = $sha256 }
}

function Get-AttrCudaLegTimeBudget {
    <#
    .SYNOPSIS
    Derive the timeouts of an owner-input attribution leg from the input's size and a MEASURED cold
    read rate -- never a guessed round number. GENERATOR-side (not embedded in any job).
    .DESCRIPTION
    STAGE-STALL card. A leg's wall time before the app even launches is the
    one full identity read of the input, and the app's own load can pay that read again if the
    pages were evicted, so both are bounded by  inputBytes / coldReadMBps  (with a margin), plus
    a fixed launch allowance, the measured play interval and the runner's own fixed slack:
      identityReadSec       = ceil(inputMB / ColdReadMBps * ReadMargin)
      smokeProcessTimeoutMs = 1000 * (identityReadSec + LaunchSeconds + PlaySeconds + SettleSeconds + RunnerSlackSeconds)
                              -- passed to run-release-gui-smoke.ps1 as -ProcessTimeoutMs; its own
                              derived default (~75 s) has no allowance for loading the input at all
      jobTimeoutSec         = identityReadSec + smokeProcessTimeoutMs/1000 + FixedPreLaunchSeconds + PostRunSeconds
                              -- what to pass um-run as -TimeoutSec, so the agent's cap cannot fire
                                 inside a step that is still making progress
    ColdReadMBps is the measured worst case (see $script:AttrCudaMeasuredColdReadMBps, sourced
    from the read-rate job's receipt named beside it); pass -ColdReadMBps to override with a fresh
    measurement. InputBytes 0 (a tiny tracked fixture) yields the fixed allowances only.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateRange(0, 1099511627776)][int64]$InputBytes,
        [ValidateRange(0.01, 100000)][double]$ColdReadMBps = $script:AttrCudaMeasuredColdReadMBps,
        [ValidateRange(1.0, 10.0)][double]$ReadMargin = 1.5,
        [ValidateRange(0, 3600)][int]$LaunchSeconds = 60,
        [ValidateRange(0, 3600)][int]$PlaySeconds = 40,
        [ValidateRange(0, 3600)][int]$SettleSeconds = 3,
        [ValidateRange(0, 3600)][int]$RunnerSlackSeconds = 30,
        [ValidateRange(0, 7200)][int]$FixedPreLaunchSeconds = $script:AttrCudaMeasuredFixedPreLaunchSeconds,
        [ValidateRange(0, 7200)][int]$PostRunSeconds = $script:AttrCudaAllowancePostRunSeconds,
        # CPU-LEG-SMOKE-CEILING-1: set ONLY for a CPU-informational leg (its Play may run to 765 s). See the clamp below. Never set for CUDA; a fixture CPU leg passes it but has no input bytes, so it never clamps.
        [switch]$ShareSmokeCeiling
    )

    $inputMB = $InputBytes / 1048576.0
    $identityReadSec = [int][math]::Ceiling($inputMB / $ColdReadMBps * $ReadMargin)
    $fixedSmokeSec = $LaunchSeconds + $PlaySeconds + $SettleSeconds + $RunnerSlackSeconds
    $smokeSec = $identityReadSec + $fixedSmokeSec
    $smokeProcessTimeoutMs = [int64]$smokeSec * 1000
    $appReadAllowanceSec = $identityReadSec
    $smokeCeilingClamped = $false
    if ($smokeProcessTimeoutMs -gt 3600000 -and $ShareSmokeCeiling) {
        # CPU-LEG-SMOKE-CEILING-1: the job's own FULL margined identity read ($identityReadSec) runs BEFORE the runner and stays in jobTimeoutSec below, so
        # what the runner's 3 600 s must hold is only the app's re-read of an input that was just read (page cache), at the ceiling's remainder. It is
        # shrunk to that remainder, never below the measured worst-case read with NO margin; the margin the unclamped budget would have carried is
        # what is given up, and the result says so (smokeCeilingClamped / appReadAllowanceSec).
        $appReadAllowanceSec = 3600 - $fixedSmokeSec
        $minimumReadSec = [int][math]::Ceiling($inputMB / $ColdReadMBps)
        if ($appReadAllowanceSec -lt $minimumReadSec) {
            throw "ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING the smoke ceiling of 3600 s leaves $appReadAllowanceSec s for the app to re-read the input after its $fixedSmokeSec s of launch, play, settle and slack, and the input needs $minimumReadSec s even with no margin (inputMB=$([math]::Round($inputMB)) coldReadMBps=$ColdReadMBps): the input cannot be read in a bounded leg at this rate"
        }
        $smokeSec = 3600
        $smokeProcessTimeoutMs = 3600000
        $smokeCeilingClamped = $true
    }
    if ($smokeProcessTimeoutMs -gt 3600000) {
        # run-release-gui-smoke.ps1's own safety ceiling on a DERIVED timeout is 3 600 000 ms.
        throw "ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING derived smoke process timeout $smokeProcessTimeoutMs ms is over 3600000 ms (inputMB=$([math]::Round($inputMB)) coldReadMBps=$ColdReadMBps): the input cannot be read in a bounded leg at this rate"
    }
    [ordered]@{
        inputMB = [math]::Round($inputMB, 1)
        coldReadMBps = $ColdReadMBps
        identityReadSec = $identityReadSec
        appReadAllowanceSec = $appReadAllowanceSec
        smokeCeilingClamped = $smokeCeilingClamped
        smokeProcessTimeoutMs = [int]$smokeProcessTimeoutMs
        jobTimeoutSec = [int]($identityReadSec + $smokeSec + $FixedPreLaunchSeconds + $PostRunSeconds)
    }
}

function Add-AttrCudaTraceLine {
    <#
    .SYNOPSIS
    Append ONE timestamped line to a trace file, flushed to disk before returning. Never throws.
    .DESCRIPTION
    STAGE-STALL card. A job the agent kills at its cap returns NO stdout, so a
    stall in a pre-launch step used to leave nothing at all (r1c: "can exceed 20 min with no
    output"; r1e: two 2400 s kills, empty stdout). Each pre-launch step now appends a line here
    as it starts and as it ends, so a killed job still leaves the last step it reached and how
    long the ones before it took. Fetch the file with attr3-trace-fetch-job.ps1's emitted job.
    A blank -TracePath is a no-op (a caller that wants no trace passes none), and every failure
    is swallowed: a trace that cannot be written must never turn a working job into a failed one.
    The caller supplies only path-free text -- CR/LF are folded to spaces here so one call is
    always exactly one line.
    #>
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$TracePath = '',
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Message
    )

    if ([string]::IsNullOrWhiteSpace($TracePath)) { return }
    try {
        $directory = [IO.Path]::GetDirectoryName($TracePath)
        if (-not [IO.Directory]::Exists($directory)) { [void][IO.Directory]::CreateDirectory($directory) }
        $line = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ') + ' ' + ($Message -replace '[\r\n]+', ' ') + [Environment]::NewLine
        [IO.File]::AppendAllText($TracePath, $line, [Text.UTF8Encoding]::new($false))
    } catch {
        return
    }
}

function Get-AttrCudaFileSha256Blocks {
    <#
    .SYNOPSIS
    SHA-256 a file with ONE sequential large-block read, timing it and tracing its progress.
    Returns @{ sha256 (lowercase hex); bytes; seconds; mbPerSec }.
    .DESCRIPTION
    STAGE-STALL card. Get-FileHash reads in small blocks (0.55 MB/s at 4 KiB blocks on Bachelor,
    even warm); the storage there is simply slow (cold reads 1.3-2.3 MB/s at any block size; the
    read-rate receipt shows Defender using no CPU), so every full read of a 1.3-2.2 GB owner part
    costs 8-25 minutes and must happen exactly once. This reads in
    -BlockBytes (default 4 MiB) chunks with FILE_FLAG_SEQUENTIAL_SCAN and feeds an incremental
    SHA-256, so the identity check and the OS-file-cache pre-warm are the SAME single pass: the
    app's own load that follows reads warm pages. A progress line is traced every -ProgressBytes
    so a hung read shows how far it got. Throws whatever the open/read throws (callers map it to a
    status token; this function never echoes -Path).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateRange(4096, 67108864)][int]$BlockBytes = 4194304,
        [AllowEmptyString()][string]$TracePath = '',
        [string]$Label = 'hash',
        [ValidateRange(1048576, 1099511627776)][int64]$ProgressBytes = 268435456
    )

    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read, $BlockBytes, [IO.FileOptions]::SequentialScan)
    $incremental = [Security.Cryptography.IncrementalHash]::CreateHash([Security.Cryptography.HashAlgorithmName]::SHA256)
    $total = [int64]0
    $sha256 = $null
    try {
        Add-AttrCudaTraceLine -TracePath $TracePath -Message "$Label start bytes=$($stream.Length) blockBytes=$BlockBytes"
        $buffer = [byte[]]::new($BlockBytes)
        $nextProgress = $ProgressBytes
        while ($true) {
            $read = $stream.Read($buffer, 0, $BlockBytes)
            if ($read -le 0) { break }
            $incremental.AppendData($buffer, 0, $read)
            $total += $read
            if ($total -ge $nextProgress) {
                $elapsed = [math]::Max(0.001, $stopwatch.Elapsed.TotalSeconds)
                Add-AttrCudaTraceLine -TracePath $TracePath -Message ("$Label progress bytes=$total elapsedSec={0:N1} MBps={1:N1}" -f $elapsed, ($total / 1048576.0 / $elapsed))
                $nextProgress += $ProgressBytes
            }
        }
        $sha256 = ([BitConverter]::ToString($incremental.GetHashAndReset()).Replace('-', '')).ToLowerInvariant()
    } finally {
        $incremental.Dispose()
        $stream.Dispose()
    }
    $stopwatch.Stop()
    $seconds = [math]::Max(0.001, $stopwatch.Elapsed.TotalSeconds)
    $mbPerSec = $total / 1048576.0 / $seconds
    Add-AttrCudaTraceLine -TracePath $TracePath -Message ("$Label done bytes=$total seconds={0:N1} MBps={1:N1}" -f $seconds, $mbPerSec)
    [pscustomobject]@{ sha256 = $sha256; bytes = $total; seconds = $seconds; mbPerSec = $mbPerSec }
}

function Test-AttrCudaFootagePart {
    <#
    .SYNOPSIS
    Verify one footage part's content on THIS host: existence, readability, length, then sha256.
    .DESCRIPTION
    STAGE-STALL card: the sha256 is one large-block sequential read
    (Get-AttrCudaFileSha256Blocks), traced when -TracePath is given, and -LengthOnly stops after
    the existence/readability/length checks WITHOUT reading the content -- so a job that must
    hash a part exactly once can screen every part cheaply first and pay the full read only where
    it holds the part's handle.
    ATTR3-FOOTAGE-BIND-1 PR-B: shared by attr3-footage-presence-job.ps1's emitted probe and
    playback-attr-3-cuda-job.ps1's owner-id content gate -- ONE definition of "does this part's
    bytes match", embedded verbatim in both via Get-AttrCudaEmbeddedFunctionSource so the two jobs
    run the same characters instead of two copies that can quietly drift apart.
    Every filesystem call is wrapped in its own try/catch: under $ErrorActionPreference = 'Stop' an
    unwrapped Test-Path/Get-Item call can throw a TERMINATING error that would escape a caller's
    loop and print the exception's own text -- which can contain the real path -- to output.
    Nothing here ever returns exception text, only a fixed status TOKEN.
    Returns one of PASS / NOT_FOUND / ACCESS_DENIED / UNREADABLE / LENGTH_MISMATCH /
    SHA256_MISMATCH. Readability is established BEFORE a length mismatch is ever reported: a part
    whose metadata is readable but whose CONTENT read is denied is UNREADABLE/ACCESS_DENIED, never
    LENGTH_MISMATCH, since no byte was ever actually observed to differ.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [int64]$ExpectedLength,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256,

        [switch]$LengthOnly,

        [AllowEmptyString()]
        [string]$TracePath = '',

        [string]$TraceLabel = 'footage-hash'
    )

    $expectedSha256Lower = $ExpectedSha256.ToLowerInvariant()
    $status = $null
    $actualLength = $null
    try {
        $actualLength = (Get-Item -LiteralPath $Path -Force -ErrorAction Stop).Length
    } catch [System.Management.Automation.ItemNotFoundException] {
        $status = 'NOT_FOUND'
    } catch [System.UnauthorizedAccessException] {
        $status = 'ACCESS_DENIED'
    } catch {
        $status = if ($_.CategoryInfo.Category -eq 'PermissionDenied') { 'ACCESS_DENIED' } else { 'UNREADABLE' }
    }

    if ($status) {
        return $status
    }

    if ($actualLength -ne $ExpectedLength) {
        # A differing length alone does not prove the content was ever actually OBSERVED to
        # differ -- a part whose metadata is readable (Get-Item above succeeded) but whose CONTENT
        # read is denied must not be reported as LENGTH_MISMATCH. Establish readability first: open
        # for read and consume at least one byte when the file is non-empty. Only a successful
        # open-and-read yields LENGTH_MISMATCH; any failure maps the same way the sha256 branch
        # below does, and never leaks the exception's own text.
        $readStream = $null
        try {
            $readStream = [IO.File]::OpenRead($Path)
            if ($actualLength -gt 0) {
                [void]$readStream.ReadByte()
            }
            return 'LENGTH_MISMATCH'
        } catch [System.UnauthorizedAccessException] {
            return 'ACCESS_DENIED'
        } catch {
            return $(if ($_.CategoryInfo.Category -eq 'PermissionDenied') { 'ACCESS_DENIED' } else { 'UNREADABLE' })
        } finally {
            if ($readStream) { $readStream.Dispose() }
        }
    }

    if ($LengthOnly) {
        # Screen only: existence, length (above) and that the content can actually be opened and
        # one byte read. The single full-content read happens later, on the held link.
        $probeStream = $null
        try {
            $probeStream = [IO.File]::OpenRead($Path)
            if ($actualLength -gt 0) { [void]$probeStream.ReadByte() }
            return 'PASS'
        } catch [System.UnauthorizedAccessException] {
            return 'ACCESS_DENIED'
        } catch {
            return $(if ($_.CategoryInfo.Category -eq 'PermissionDenied') { 'ACCESS_DENIED' } else { 'UNREADABLE' })
        } finally {
            if ($probeStream) { $probeStream.Dispose() }
        }
    }

    try {
        $actualSha256 = (Get-AttrCudaFileSha256Blocks -Path $Path -TracePath $TracePath -Label $TraceLabel).sha256
        return $(if ($actualSha256 -eq $expectedSha256Lower) { 'PASS' } else { 'SHA256_MISMATCH' })
    } catch [System.UnauthorizedAccessException] {
        return 'ACCESS_DENIED'
    } catch {
        return $(if ($_.CategoryInfo.Category -eq 'PermissionDenied') { 'ACCESS_DENIED' } else { 'UNREADABLE' })
    }
}

function ConvertTo-AttrCudaUtf8String {
    <#
    .SYNOPSIS
    Decode raw bytes as UTF-8 text.
    .DESCRIPTION
    Exists so a job TEMPLATE never needs [Text.Encoding]::UTF8.GetString() directly --
    tools/repo_hygiene/attr3_publish_write_scan.ps1's R4 rule allowlists .NET static/instance
    members in the template by name, and this repository's policy is that a job template stays
    inside that allowlist; the decode lives here instead, spliced in and called by NAME like
    every other embedded verifier (see Read-AttrCudaBase64Payload's own header for the same
    reasoning about base64).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [byte[]]$Bytes
    )
    [Text.Encoding]::UTF8.GetString($Bytes)
}

function Publish-AttrCudaBytes {
    <#
    .SYNOPSIS
    Write raw bytes to a destination outside the job-owned work tree, slot-checked in the same
    call -- the byte-array counterpart of Publish-AttrCudaText (sol PR #133 r5's guard-with-the-
    write pattern), for a payload that arrived decoded rather than copied from another file.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Bytes,
        [string]$OwnedJournal = ''
    )

    $slot = Assert-AttrCudaWritableFileSlot -Path $Path
    if ($OwnedJournal -ne '') {
        $stream = New-AttrCudaOwnedFileStream -Path $slot -OwnedJournal $OwnedJournal
        try { $stream.Write($Bytes, 0, $Bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        return $slot
    }
    [IO.File]::WriteAllBytes($slot, $Bytes)
    return $slot
}

function Publish-AttrCudaFileCopy {
    <#
    .SYNOPSIS
    Copy a file to a destination outside the job-owned work tree, slot-checked in the same call.
    .DESCRIPTION
    -OwnedJournal (OWNER-FOOTAGE-NO-HARDLINK-2): when given, the destination is created with CreateNew,
    its identity is journalled from the creating handle BEFORE any byte is copied, and the bytes are
    streamed in -- so the job can later delete exactly this file by proof
    (Remove-AttrCudaPartialFile -OwnedJournal), including after a copy that failed half way. Without
    it Copy-Item runs as before and nothing is recorded (a later delete helper will leave the name).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string]$OwnedJournal = ''
    )

    $slot = Assert-AttrCudaWritableFileSlot -Path $Destination
    if ($OwnedJournal -ne '') {
        $out = New-AttrCudaOwnedFileStream -Path $slot -OwnedJournal $OwnedJournal
        try {
            $in = [IO.File]::Open($Source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            try { $in.CopyTo($out) } finally { $in.Dispose() }
            $out.Flush($true)
        } finally {
            $out.Dispose()
        }
        return $slot
    }
    Copy-Item -LiteralPath $Source -Destination $slot -Force
    return $slot
}

function Publish-AttrCudaBoundedTextCopy {
    <#
    .SYNOPSIS
    Publish a captured console stream (PresentMon's stdout / stderr) at most -MaxBytes long, slot-checked like every other publish, and say what happened.
    .DESCRIPTION
    DVE-PRESENTMON-EVIDENCE-1: the capturing process may still hold its stream file (a PresentMon that survived even the kill fallback), so it is opened
    with FileShare.ReadWrite. A stream longer than the bound publishes only its TAIL (the last -MaxBytes), under one first line that says so and how long
    the capture was. An EMPTY stream is published as an empty file (an empty stderr is itself the answer); a missing one is reported missing. Never throws:
    the result carries the error, because the caller is already on a failure path and must not lose its typed terminal to the stream copy.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [int]$MaxBytes = 65536
    )

    $result = [ordered]@{ exists = $false; bytes = $null; published = $false; truncated = $false; error = $null }
    try {
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { return $result }
        $result.exists = $true
        $in = [IO.File]::Open($Source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $length = [int64]$in.Length
            $result.bytes = $length
            $keep = [int][math]::Min($length, [int64]$MaxBytes)
            if ($length -gt $keep) {
                [void]$in.Seek($length - $keep, [IO.SeekOrigin]::Begin)
                $result.truncated = $true
            }
            $buffer = New-Object byte[] $keep
            $read = 0
            while ($read -lt $keep) {
                $n = $in.Read($buffer, $read, $keep - $read)
                if ($n -le 0) { break }
                $read += $n
            }
        } finally {
            $in.Dispose()
        }
        $text = [Text.UTF8Encoding]::new($false).GetString($buffer, 0, $read)
        if ($result.truncated) { $text = "[truncated: $length bytes captured, the last $keep shown]" + [Environment]::NewLine + $text }
        [void](Publish-AttrCudaText -Path $Destination -Value $text)
        $result.published = $true
    } catch {
        $result.error = $_.Exception.Message
    }
    return $result
}

function Publish-AttrCudaPresentMonCaptureEvidence {
    <#
    .SYNOPSIS
    After PresentMon's stop (or its failed start): publish its stdout / stderr beside the other artifacts and record whether its CSV ever existed.
    .DESCRIPTION
    DVE-PRESENTMON-EVIDENCE-1 (Ultra-Magnus, VENUE-OWNER-LEGS-UM-2 r1): a clean, immediate stop and no CSV anywhere said nothing about WHY, because PresentMon ran
    hidden with no captured streams. csvEverExisted is true when the CSV was seen while the launch waited for trace readiness (-CsvSeenDuringReadiness) or exists
    now; csvSizeAtStop is its size now, null when it is absent. A CSV seen early and gone at stop therefore reads (true, null) -- never the same as "never created".
    Never throws.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$CsvPath,
        [bool]$CsvSeenDuringReadiness = $false,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [Parameter(Mandatory = $true)][string]$PubRoot
    )

    $ever = $CsvSeenDuringReadiness
    $size = $null
    try {
        $item = @(Get-ChildItem -LiteralPath $CsvPath -File -ErrorAction SilentlyContinue)
        if ($item.Count -eq 1) {
            $ever = $true
            $size = [int64]$item[0].Length
        }
    } catch {
        $size = $null
    }
    [ordered]@{
        csvEverExisted = $ever
        csvSizeAtStop = $size
        streams = [ordered]@{
            stdout = Publish-AttrCudaBoundedTextCopy -Source $StdoutPath -Destination (Join-Path $PubRoot 'presentmon-stdout.txt')
            stderr = Publish-AttrCudaBoundedTextCopy -Source $StderrPath -Destination (Join-Path $PubRoot 'presentmon-stderr.txt')
        }
    }
}

function Get-AttrCudaPresentMonOrphanSessionName {
    <#
    .SYNOPSIS
    The ETW session names in a `logman query -ets` listing that a PresentMon capture of this harness may have left orphaned: the default `PresentMon` and every `MLVAttr3-*`.
    .DESCRIPTION
    UM-PRESENTMON-ORPHAN-SWEEP-1 (Ultra-Magnus, 2026-10-03): an orphaned default-named session made every OTHER-named PresentMon session lose all of its events, and a killed
    capture leaves its own MLVAttr3-* session behind. Only those two shapes are ever candidates -- a name that merely starts with either (PresentMon_other, PresentMonitor2) or
    contains it (MyMLVAttr3-x) belongs to somebody else and is never listed here. A listing line is `<name> <type> <status>`, so the name must be followed by whitespace; the
    header, the rule line and the footer never match. Each name is returned once, as the listing spells it (names are case-insensitive to PresentMon and logman). Pure text in,
    names out: it never runs logman.
    #>
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$ListingText = ''
    )

    $seen = @{}
    $names = @()
    foreach ($line in ($ListingText -split "\r?\n")) {
        $match = [regex]::Match($line, '^\s*(PresentMon|MLVAttr3-[A-Za-z0-9_-]+)\s+\S', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if (-not $match.Success) { continue }
        $name = $match.Groups[1].Value
        if ($seen.ContainsKey($name.ToLowerInvariant())) { continue }
        $seen[$name.ToLowerInvariant()] = $true
        $names += $name
    }
    return $names
}

function Get-AttrCudaEtsSessionListing {
    <#
    .SYNOPSIS
    `logman query -ets` run once: its text and exit code. The job's orphan sweep reads the live ETW session list through this, never through a call operator in the scanned template.
    .DESCRIPTION
    UM-PRESENTMON-ORPHAN-SWEEP-1. The emitted job template is write-scanned (attr3_publish_write_scan.ps1 R3: `&` only on an allowlisted child executable), so the one
    external command the sweep needs lives here, in the runtime helper boundary, like the module's other Win32 surface. Read-only: `query` changes nothing. Never throws; a failure
    to run it is in `error` and leaves `exitCode` null.
    #>
    [CmdletBinding()]
    param(
        [string]$LogmanPath = '',
        [int]$TimeoutSeconds = 15
    )

    $run = Invoke-AttrCudaLogman -Argument @('query', '-ets') -LogmanPath $LogmanPath -TimeoutSeconds $TimeoutSeconds
    return [ordered]@{ exitCode = $run.exitCode; text = $run.text; timedOut = $run.timedOut; error = $run.error }
}

function Invoke-AttrCudaLogman {
    <#
    .SYNOPSIS
    One `logman` invocation with a deadline: its exit code, its stdout text, whether it timed out, and any failure to run it. Never throws, never waits longer than -TimeoutSeconds (plus a short drain).
    .DESCRIPTION
    UM-SWEEP-LOGMAN-BOUND-1 (fable hardening 3, sol hardening): the orphan sweep makes three queries and one stop per stubborn session before every capture, and a native `logman` that stalls would hold
    the capture's start until the agent's job budget killed the whole job. The call goes through a child process that is killed (with its tree) when the deadline passes; `timedOut` and `error`
    then say so and `exitCode` stays null. -LogmanPath is the executable to run (default: logman.exe in the system directory); a test passes a stub, and the default is what the venue runs.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Argument,
        [string]$LogmanPath = '',
        [int]$TimeoutSeconds = 15
    )

    $result = [ordered]@{ exitCode = $null; text = ''; timedOut = $false; error = $null }
    try {
        $executable = $LogmanPath
        if ([string]::IsNullOrEmpty($executable)) { $executable = Join-Path ([Environment]::GetFolderPath('System')) 'logman.exe' }
        $startInfo = [Diagnostics.ProcessStartInfo]::new($executable)
        foreach ($item in $Argument) { [void]$startInfo.ArgumentList.Add($item) }
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.CreateNoWindow = $true
        $child = [Diagnostics.Process]::Start($startInfo)
        try {
            $outTask = $child.StandardOutput.ReadToEndAsync()
            $errTask = $child.StandardError.ReadToEndAsync()
            if ($child.WaitForExit($TimeoutSeconds * 1000)) {
                [void]$outTask.Wait(5000)
                [void]$errTask.Wait(5000)
                $result.exitCode = [int]$child.ExitCode
                if ($outTask.IsCompleted) { $result.text = [string]$outTask.Result }
            } else {
                $result.timedOut = $true
                $result.error = "logman did not exit within $TimeoutSeconds s"
                try { $child.Kill($true) } catch { $result.error = $result.error + "; Kill() failed: " + $_.Exception.Message }
                [void]$child.WaitForExit(2000)
            }
        } finally {
            $child.Dispose()
        }
    } catch {
        $result.error = $_.Exception.Message
    }
    return $result
}

function Stop-AttrCudaEtsSession {
    <#
    .SYNOPSIS
    `logman stop <name> -ets` for ONE of this harness's own PresentMon session names (the default `PresentMon` or an `MLVAttr3-*`), refusing every other name; returns the exit code.
    .DESCRIPTION
    UM-PRESENTMON-ORPHAN-SWEEP-1: the fallback of the orphan sweep, used only for a session still listed after the pinned PresentMon's own --terminate_existing_session. The name is
    held to the same shape Get-AttrCudaPresentMonOrphanSessionName lists, so this can never stop somebody else's trace session whatever a caller passes. Never throws.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SessionName,
        [string]$LogmanPath = '',
        [int]$TimeoutSeconds = 15
    )

    $result = [ordered]@{ exitCode = $null; timedOut = $false; error = $null }
    if ($SessionName -notmatch '^(?i:PresentMon|MLVAttr3-[A-Za-z0-9_-]+)$') {
        $result.error = 'refused: not a PresentMon or MLVAttr3-* session name'
        return $result
    }
    $run = Invoke-AttrCudaLogman -Argument @('stop', $SessionName, '-ets') -LogmanPath $LogmanPath -TimeoutSeconds $TimeoutSeconds
    $result.exitCode = $run.exitCode
    $result.timedOut = $run.timedOut
    $result.error = $run.error
    return $result
}

function Get-AttrCudaTextEncodingFromHead {
    <#
    .SYNOPSIS
    The text encoding of a stream from its first bytes: UTF-8, UTF-16LE or UTF-16BE, with the length of its BOM and the size of its code unit.
    .DESCRIPTION
    UM-PRESENTMON-ORPHAN-SWEEP-1 r2 (sol blocker 3): the diagnostic's real PresentMon stderr files are UTF-16LE with a FF FE BOM (Start-Process redirecting a console child under Windows PowerShell's
    codepage), so a UTF-8-only read never saw the lost-events warning. A BOM decides (EF BB BF UTF-8, FF FE UTF-16LE, FE FF UTF-16BE); without one, a head whose odd-position bytes are mostly NUL is
    UTF-16LE (ASCII text in UTF-16 is every second byte zero) and one whose even-position bytes are is UTF-16BE; anything else, ASCII included, is UTF-8. Pure bytes in, an answer out; never throws.
    #>
    [CmdletBinding()]
    param(
        [AllowEmptyCollection()][byte[]]$Head = @()
    )

    $utf8 = [pscustomobject]@{ name = 'utf-8'; encoding = [Text.UTF8Encoding]::new($false); bomLength = 0; codeUnitBytes = 1 }
    $count = $Head.Length
    if ($count -ge 3 -and $Head[0] -eq 0xEF -and $Head[1] -eq 0xBB -and $Head[2] -eq 0xBF) {
        return [pscustomobject]@{ name = 'utf-8'; encoding = [Text.UTF8Encoding]::new($false); bomLength = 3; codeUnitBytes = 1 }
    }
    if ($count -ge 2 -and $Head[0] -eq 0xFF -and $Head[1] -eq 0xFE) {
        return [pscustomobject]@{ name = 'utf-16le'; encoding = [Text.UnicodeEncoding]::new($false, $false); bomLength = 2; codeUnitBytes = 2 }
    }
    if ($count -ge 2 -and $Head[0] -eq 0xFE -and $Head[1] -eq 0xFF) {
        return [pscustomobject]@{ name = 'utf-16be'; encoding = [Text.UnicodeEncoding]::new($true, $false); bomLength = 2; codeUnitBytes = 2 }
    }
    $pairs = [int][math]::Floor([math]::Min($count, 512) / 2)
    if ($pairs -ge 2) {
        $oddNul = 0
        $evenNul = 0
        for ($i = 0; $i -lt ($pairs * 2); $i++) {
            if ($Head[$i] -ne 0) { continue }
            if (($i % 2) -eq 1) { $oddNul++ } else { $evenNul++ }
        }
        if (($oddNul * 2) -ge $pairs) { return [pscustomobject]@{ name = 'utf-16le'; encoding = [Text.UnicodeEncoding]::new($false, $false); bomLength = 0; codeUnitBytes = 2 } }
        if (($evenNul * 2) -ge $pairs) { return [pscustomobject]@{ name = 'utf-16be'; encoding = [Text.UnicodeEncoding]::new($true, $false); bomLength = 0; codeUnitBytes = 2 } }
    }
    return $utf8
}

function Get-AttrCudaPresentMonEventsLost {
    <#
    .SYNOPSIS
    Whether PresentMon's captured stderr reports lost trace events, as a typed record: detected, how many messages, the largest count reported, and a PRESENTMON_EVENTS_LOST detail.
    .DESCRIPTION
    UM-PRESENTMON-ORPHAN-SWEEP-1: with a leftover session on the host, a capture lost 15-17k events per 12 s and wrote no CSV, which read only as "PresentMon output does not
    exist". PresentMon prints a `warning: N ... events were lost.` line on stderr (the pinned 2.5.1 wording is matched below); this turns that line into a fact the evidence carries. Only the last 256 KB of the stream is read (opened
    with FileShare.ReadWrite, like the publish: the capturing process may still hold it). Several lines are reported as a message count and the largest N -- they are not summed,
    because the counts may be cumulative. A missing or unreadable stream detects nothing. Never throws.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StderrPath
    )

    $result = [ordered]@{ detected = $false; messages = 0; maxReported = $null; detail = $null }
    try {
        if (-not (Test-Path -LiteralPath $StderrPath -PathType Leaf)) { return $result }
        $in = [IO.File]::Open($StderrPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $length = [int64]$in.Length
            # the encoding is read from the start of the stream (its BOM, else the shape of its bytes) before the tail is cut: PresentMon's stderr as Start-Process redirects it is UTF-16LE with a BOM
            $headLength = [int][math]::Min($length, [int64]512)
            $head = New-Object byte[] $headLength
            $headRead = 0
            while ($headRead -lt $headLength) {
                $n = $in.Read($head, $headRead, $headLength - $headRead)
                if ($n -le 0) { break }
                $headRead += $n
            }
            $encoding = Get-AttrCudaTextEncodingFromHead -Head $head
            $start = [int64][math]::Max([int64]0, $length - [int64]262144)
            # a tail that begins between the two bytes of a UTF-16 code unit would read as noise: it starts on the next unit boundary
            if ($start -gt 0 -and $encoding.codeUnitBytes -gt 1 -and ($start % $encoding.codeUnitBytes) -ne 0) { $start += ($encoding.codeUnitBytes - ($start % $encoding.codeUnitBytes)) }
            if ($start -eq 0) { $start = [int64][math]::Min($length, [int64]$encoding.bomLength) }
            [void]$in.Seek($start, [IO.SeekOrigin]::Begin)
            $keep = [int]($length - $start)
            $buffer = New-Object byte[] $keep
            $read = 0
            while ($read -lt $keep) {
                $n = $in.Read($buffer, $read, $keep - $read)
                if ($n -le 0) { break }
                $read += $n
            }
        } finally {
            $in.Dispose()
        }
        $text = $encoding.encoding.GetString($buffer, 0, $read)
        $largest = [int64]-1
        $messages = 0
        foreach ($match in [regex]::Matches($text, '(\d+)\s+ETW events were lost', [Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
            $messages++
            $count = [int64]0
            if ([int64]::TryParse($match.Groups[1].Value, [ref]$count) -and $count -gt $largest) { $largest = $count }
        }
        if ($messages -gt 0) {
            $result.detected = $true
            $result.messages = $messages
            if ($largest -ge 0) { $result.maxReported = $largest }
            $result.detail = "PRESENTMON_EVENTS_LOST: PresentMon reported dropped trace events (largest count $($result.maxReported) in $messages message(s)); a capture that loses its events writes no CSV rows, e.g. when another PresentMon ETW session is left on the host"
        }
    } catch {
        $result.detected = $false
    }
    return $result
}

function Add-AttrCudaPresentMonEventsLostDetail {
    <#
    .SYNOPSIS
    The PresentMon failure reason with the typed PRESENTMON_EVENTS_LOST detail appended when the capture lost events; the reason unchanged otherwise.
    .DESCRIPTION
    UM-PRESENTMON-ORPHAN-SWEEP-1: a recurrence of the starved capture must read as its cause in the summary's PresentMon reason, not as the symptom. A reason that already carries the
    token is never given it twice, and a missing -EventsLost record (the scan did not run) leaves the reason alone.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()][AllowEmptyString()][string]$Reason,
        [AllowNull()][object]$EventsLost
    )

    if ($null -eq $EventsLost) { return $Reason }
    $detected = $false
    $detail = $null
    try {
        $detected = [bool]$EventsLost.detected
        $detail = [string]$EventsLost.detail
    } catch {
        return $Reason
    }
    if (-not $detected -or [string]::IsNullOrEmpty($detail)) { return $Reason }
    if (-not [string]::IsNullOrEmpty($Reason) -and $Reason.Contains('PRESENTMON_EVENTS_LOST')) { return $Reason }
    if ([string]::IsNullOrEmpty($Reason)) { return $detail }
    return "$Reason [$detail]"
}

function Add-AttrCudaPresentMonEventsLostDetailToReport {
    <#
    .SYNOPSIS
    A PresentMon display report (a pscustomobject with a `reason`) whose reason carries the typed PRESENTMON_EVENTS_LOST detail; the same object when there is nothing to add.
    .DESCRIPTION
    UM-PRESENTMON-ORPHAN-SWEEP-1: the display-report failure branch reads `$displayReport.reason` in two places, and the scanned template may not assign to a member (R5), so the
    report is replaced by a copy (properties and their order unchanged, only `reason` different) -- a plain variable assignment at the call site.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Report,
        [AllowNull()][object]$EventsLost
    )

    $current = [string]$Report.reason
    $detailed = Add-AttrCudaPresentMonEventsLostDetail -Reason $current -EventsLost $EventsLost
    if ($detailed -ceq $current) { return $Report }
    $copy = [ordered]@{}
    foreach ($property in $Report.PSObject.Properties) { $copy[$property.Name] = $property.Value }
    $copy['reason'] = $detailed
    return [pscustomobject]$copy
}

function Publish-AttrCudaContactSheetRawCaptures {
    <#
    .SYNOPSIS
    Publish the app's raw --contact-sheet-dir PNG+JSON pairs under $PubRoot\contact-sheet\raw,
    a no-op when the option was off or nothing was captured.
    .DESCRIPTION
    NOTE fix (fable, CUDA-PLAYBACK-CONTACT-SHEET-2): a leg that refuses at the eligibility gate
    (BACKEND_NOT_AVAILABLE) or the GPU-frame gate (GPU_RECON_FRAMES_ZERO) used to exit before
    the main publish step ever ran this copy, leaving that leg's raw captures stranded in
    $Work with no measurement to compare against AND no evidence of what was captured. Both
    early-refusal call sites below now call this too, so a refused -ContactSheet leg still
    publishes its raw frames -- composing them into a labelled sheet stays a main-flow-only
    step (it needs the run's own eligibility verdict for GPU/scale labels, which a refused run
    has no reliable measurement behind anyway).
    CUDA-PLAYBACK-CONTACT-SHEET-2 round 2: moved here from an inline definition inside
    playback-attr-3-cuda-job.ps1's own $template (both call sites are inside that same
    template, run on Bachelor) -- test_every_called_attrcuda_command_is_defined_in_the_real_
    embedded_text (PRESENTMON-HARNESS-ROBUSTNESS-2) requires every -AttrCuda-named function
    the template calls to come from this module's spliced text, not be defined inline.
    #>
    param(
        [bool]$Enabled,
        [string]$SourceDir,
        [string]$PubRoot
    )

    if (-not $Enabled -or -not $SourceDir -or -not (Test-Path -LiteralPath $SourceDir)) { return $null }
    # New-AttrCudaDirectory only (never a raw New-Item -Force) -- matches every other $Pub
    # subdirectory this job creates.
    [void](New-AttrCudaDirectory -Path (Join-Path $PubRoot 'contact-sheet'))
    $rawDir = Join-Path $PubRoot 'contact-sheet\raw'
    [void](New-AttrCudaDirectory -Path $rawDir)
    Get-ChildItem -LiteralPath $SourceDir -File | ForEach-Object {
        [void](Publish-AttrCudaFileCopy -Source $_.FullName -Destination (Join-Path $rawDir $_.Name))
    }
    return $rawDir
}

function Publish-AttrCudaFileMove {
    <#
    .SYNOPSIS
    Rename a file into a destination outside the job-owned work tree, slot-checked in the same call.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $slot = Assert-AttrCudaWritableFileSlot -Path $Destination
    Move-Item -LiteralPath $Source -Destination $slot -Force
    return $slot
}

function Assert-AttrCudaNonOverwritingFileSlot {
    <#
    .SYNOPSIS
    Prove a publish destination's PARENT is safe to write into, without touching whatever (if
    anything) already occupies the destination itself.
    .DESCRIPTION
    ATTR3-FIXTURE-STAGE-1 (sol, PR #139 r1 MAJOR). Assert-AttrCudaWritableFileSlot removes a
    plain file already occupying the slot so its caller's subsequent -Force write always
    succeeds -- exactly the race a non-overwriting publish must not have: a different-bytes
    file that a concurrent publisher placed at the destination, deleted out from under it and
    replaced. This checks only what Assert-AttrCudaWritableFileSlot checks about the PARENT
    (must exist, must not itself be a reparse point) and leaves the destination path alone.
    Whether the destination is occupied is never decided here -- Publish-AttrCudaFileMove-
    NonOverwriting's [System.IO.File]::Move(..., $false) is the sole, atomic arbiter of that.
    Throws ATTRCUDA_SLOT_PARENT_MISSING or ATTRCUDA_SLOT_PARENT_IS_LINK; returns the full path
    otherwise.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path)
    $parent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($full)) -Force -ErrorAction SilentlyContinue
    if ($null -eq $parent -or -not $parent.PSIsContainer) {
        throw "ATTRCUDA_SLOT_PARENT_MISSING $full"
    }
    if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "ATTRCUDA_SLOT_PARENT_IS_LINK $($parent.FullName)"
    }
    $full
}

function Publish-AttrCudaFileMoveNonOverwriting {
    <#
    .SYNOPSIS
    Atomically rename a file into a destination outside the job-owned work tree WITHOUT ever
    overwriting or deleting a same-named file already there.
    .DESCRIPTION
    ATTR3-FIXTURE-STAGE-1 (sol, PR #139 r1 MAJOR), narrowly scoped to the fixture stager --
    Publish-AttrCudaFileMove's slot check deletes ANY plain file occupying the destination and
    then moves with -Force, so a different-bytes cache file that arrives between the fixture
    job's own initial existence check and this call is silently removed and replaced. That
    race is closed here, not in the shared helper: the package stager (playback-attr-3-cuda-
    stage-job.ps1) still calls Publish-AttrCudaFileMove and still has it, tracked separately.
    The parent is checked exactly like Assert-AttrCudaWritableFileSlot (must exist, must not be
    a link), but the destination slot itself is never inspected or removed first. .NET's
    [System.IO.File]::Move($Source, $Destination, $false) is the sole arbiter of "is it
    occupied" -- overwrite=$false is atomic on NTFS, so there is no check-then-act window for a
    concurrent writer to land in between the check and the rename.
    On IOException the destination already exists; nothing has been moved, deleted or written
    -- the caller re-hashes the destination (identical bytes: a concurrent publisher already
    finished this exact fixture; different bytes: fail closed) instead of this helper silently
    reporting success either way.
    Throws ATTRCUDA_NONOVERWRITE_DESTINATION_EXISTS when the destination is occupied.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $slot = Assert-AttrCudaNonOverwritingFileSlot -Path $Destination
    try {
        [IO.File]::Move($Source, $slot, $false)
    } catch [IO.IOException] {
        throw "ATTRCUDA_NONOVERWRITE_DESTINATION_EXISTS $slot already exists: $($_.Exception.Message)"
    }
    return $slot
}

function Publish-AttrCudaDirectoryMoveNonOverwriting {
    <#
    .SYNOPSIS
    Atomically rename a directory into a destination outside the job-owned work tree WITHOUT
    ever overwriting or deleting a same-named directory already there.
    .DESCRIPTION
    ATTR3-SMOKE-RUNNER-DEPS-1: the smoke-runner closure directory is built under a temp name
    and published in one rename, mirroring Publish-AttrCudaFileMoveNonOverwriting's race-free
    publish for a single file. The parent is checked exactly like
    Assert-AttrCudaNonOverwritingFileSlot (must exist, must not be a link); the destination slot
    itself is never inspected or removed first. [System.IO.Directory]::Move throws IOException
    when the destination already exists on Windows (no overwrite semantics for a directory
    move), so there is no check-then-act window for a concurrent writer to land in between the
    check and the rename.
    On IOException the destination already exists; nothing has been moved, deleted or written --
    the caller re-verifies the destination's content instead of this helper silently reporting
    success either way.
    Throws ATTRCUDA_NONOVERWRITE_DESTINATION_EXISTS when the destination is occupied.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $slot = Assert-AttrCudaNonOverwritingFileSlot -Path $Destination
    try {
        [IO.Directory]::Move($Source, $slot)
    } catch [IO.IOException] {
        throw "ATTRCUDA_NONOVERWRITE_DESTINATION_EXISTS $slot already exists: $($_.Exception.Message)"
    }
    return $slot
}

function New-AttrCudaDirectory {
    <#
    .SYNOPSIS
    Create (or accept) a directory whose parent is not a link and which is not itself a link.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path)
    $parent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($full)) -Force -ErrorAction SilentlyContinue
    if ($null -eq $parent -or -not $parent.PSIsContainer) {
        throw "ATTRCUDA_DIR_PARENT_MISSING $full"
    }
    if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "ATTRCUDA_DIR_PARENT_IS_LINK $($parent.FullName)"
    }
    $existing = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        if (-not $existing.PSIsContainer -or (($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "ATTRCUDA_DIR_OCCUPIED $full is a file or a reparse point"
        }
        return $full
    }
    [void](New-Item -ItemType Directory -Path $full)
    return $full
}

function Remove-AttrCudaPartialFile {
    <#
    .SYNOPSIS
    Delete one .partial path only if it is a plain FILE this job PROVABLY created; never recurse,
    never follow a link, never adopt a name because it looks like ours.
    .DESCRIPTION
    sol, PR #133 r3: `Remove-Item -Recurse` on a .partial path that is occupied by a directory can
    traverse an NTFS junction inside it and delete the junction's TARGET, outside the job root
    (PowerShell/PowerShell#26913). A .partial is only ever written as a file, so anything else --
    a directory, a symlink, a junction -- is left exactly where it is and reported. Returns $true
    when the path is absent or was a file that is now gone, $false when it was left.
    sol, PR #133 r8: the ANCESTOR chain from -TrustedRoot is checked too (a plain file reached
    through a linked inbox/cache/outbox is outside the root). A refusal never throws: this runs in
    cleanup and failure paths, where an exception would mask the job's real exit code.
    OWNER-FOOTAGE-NO-HARDLINK-2: -OwnedJournal is MANDATORY and has no default. The name is deleted
    only on the journal's proof (Get-AttrCudaOwnershipProof): the identity recorded from the handle
    that CREATED it (Publish-AttrCuda* -OwnedJournal), or a fresh root this job journalled that
    contains it; either is then checked on the one share-none deleting handle
    (Remove-AttrCudaFileByProof). A name the journal does not prove -- including one that merely
    looks like this job's .partial -- is LEFT_UNOWNED: a warning, nothing deleted, $false. The old
    behaviour (read the name's identity and use it as the authority) is gone: that is how the last
    name of an owner recording left by an older build would have been deleted.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TrustedRoot,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$OwnedJournal
    )

    try {
        $Path = Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $TrustedRoot -Path $Path
    } catch {
        Write-Warning "ATTRCUDA_PARTIAL_OUTSIDE_TRUSTED_ROOT left in place: $($_.Exception.Message)"
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $true }
    $isReparse = (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    if ($item.PSIsContainer -or $isReparse) {
        Write-Warning "ATTRCUDA_PARTIAL_NOT_A_FILE left in place (directory or reparse point): $Path"
        return $false
    }
    try {
        $journalData = Read-AttrCudaOwnedJournal -Journal $OwnedJournal
        $proof = Get-AttrCudaOwnershipProof -Journal $journalData -Path $Path -RootCache @{}
        if ($null -eq $proof) {
            $token = 'LEFT_UNOWNED'
        } elseif ($proof.Kind -eq 'file') {
            $token = Remove-AttrCudaFileByProof -Path $Path -FileId $proof.FileId -ExpectReparsePoint:$proof.ExpectReparsePoint
        } else {
            $token = Remove-AttrCudaFileByProof -Path $Path -NotBeforeFileTime $proof.NotBeforeFileTime
        }
    } catch {
        $token = 'LEFT_UNAVAILABLE'
    }
    if ($token -eq 'DELETED' -or $token -eq 'ABSENT') { return $true }
    Write-Warning "ATTRCUDA_PARTIAL_LEFT $token left in place: $Path"
    return $false
}

function Remove-AttrCudaInputFileByContent {
    <#
    .SYNOPSIS
    Remove a SUBMITTED INPUT file a job did not create (a build package or tracked fixture left in
    its inbox), only if its bytes -- read through the deleting handle -- are exactly the bytes the
    job already verified; otherwise leave it. Returns $true when absent or gone, $false when left.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-2. An inbox file arrives from the submitter, so there is no creating
    handle to record. The proof is its CONTENT: -ExpectedSha256 is the hash the job verified the file
    against (the manifest-bound package hash, the content-pinned fixture hash); the deleting handle
    is share-none, so the bytes hashed are the bytes removed, and a name that is anything else --
    owner footage included -- is left (LEFT_CONTENT_MISMATCH). Mandatory arguments, no default.
    Same refusals as Remove-AttrCudaPartialFile (ancestor links, directory, reparse point); never
    throws.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TrustedRoot,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    try {
        $Path = Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $TrustedRoot -Path $Path
    } catch {
        Write-Warning "ATTRCUDA_INPUT_OUTSIDE_TRUSTED_ROOT left in place: $($_.Exception.Message)"
        return $false
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $true }
    if ($item.PSIsContainer -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        Write-Warning "ATTRCUDA_INPUT_NOT_A_FILE left in place (directory or reparse point): $Path"
        return $false
    }
    try {
        $token = Remove-AttrCudaFileByProof -Path $Path -ExpectedSha256 $ExpectedSha256.ToLowerInvariant()
    } catch {
        $token = 'LEFT_UNAVAILABLE'
    }
    if ($token -eq 'DELETED' -or $token -eq 'ABSENT') { return $true }
    Write-Warning "ATTRCUDA_INPUT_LEFT $token left in place: $Path"
    return $false
}

function Assert-AttrCudaNoLinkBelowRoot {
    <#
    .SYNOPSIS
    Prove $Path lies strictly under $TrustedRoot and that no EXISTING component between them is a
    reparse point; return the full path.
    .DESCRIPTION
    sol, PR #133 r6: a delete of AgentRoot\outbox\<job> followed an outbox JUNCTION outside the agent
    root before any later check could refuse the link. Every component after the trusted root, up to
    and including the path itself, is inspected; the trusted root is the provisioning boundary.
    Throws ATTRCUDA_PATH_NOT_UNDER_ROOT or ATTRCUDA_ANCESTOR_IS_LINK and touches nothing.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TrustedRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootFull = [IO.Path]::GetFullPath($TrustedRoot).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not $full.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "ATTRCUDA_PATH_NOT_UNDER_ROOT $full is not strictly under $rootFull"
    }
    $relative = $full.Substring($rootFull.Length + 1)
    $cursor = $rootFull
    foreach ($component in $relative.Split('\')) {
        if ([string]::IsNullOrEmpty($component) -or $component -eq '.' -or $component -eq '..') {
            throw "ATTRCUDA_PATH_NOT_UNDER_ROOT $full has an empty or relative component"
        }
        $cursor = $cursor + '\' + $component
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { break }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "ATTRCUDA_ANCESTOR_IS_LINK $cursor (on the way to $full)"
        }
    }
    return $full
}

function Add-AttrCudaOwnedRecord {
    <#
    .SYNOPSIS
    Append ONE creator-recorded ownership line to a job's journal, durably, before the caller does
    anything else with the file (or directory) it just created.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1/2. Ownership of a name is a fact about how it came to exist, never
    about what a scan later finds: the ONLY things a job may delete are the ones it created itself,
    and it proves that by reading the volume serial + 64-bit file index off the very handle that
    CREATED the object (Get-AttrCudaFileId -Stream, FileMode.CreateNew) and journalling that identity
    here. An identity first SEEN later -- by a sweep, by an adoption pass -- never confers ownership:
    a name that merely looks like ours may be the last name of old footage (a hard link an older
    build left, whose other name the owner has since replaced), and NumberOfLinks = 1 cannot tell the
    difference.
    The line is {p = path relative to the journal's directory, v/h/l = the identity, r = the name is a
    symbolic link this caller created}. -Kind root journals a DIRECTORY this job created fresh
    (New-AttrCudaOwnedRoot) and adds c = its creation FILETIME, the bound for the created-after proof;
    -Kind gone retires a root record. The journal file is itself created with CreateNew and its own
    identity is the first line it carries (k = self), so even the journal is deleted only by a
    creator-recorded identity. The append is flushed through to disk before this returns, a sharing
    violation from another job appending is retried for ten seconds, a journal that is a link or has
    a second name is refused BEFORE a byte is written, and a failure THROWS: a caller that cannot
    record a file it just made must remove it by identity and fail, not carry on holding an
    unrecorded file.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Journal,
        [Parameter(Mandatory = $true)][string]$Path,
        $FileId = $null,
        [switch]$IsReparsePoint,
        [ValidateSet('file', 'root', 'gone')][string]$Kind = 'file',
        [switch]$AllowOutside
    )

    $journalFull = [IO.Path]::GetFullPath($Journal)
    $journalDir = [IO.Path]::GetDirectoryName($journalFull).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path)
    $inside = $full.StartsWith($journalDir + '\', [StringComparison]::OrdinalIgnoreCase)
    # -AllowOutside (a durable creator record for a file made on ANOTHER directory tree, e.g. the staging
    # job's target-volume partial) journals the ABSOLUTE path; the default still refuses a path that is
    # not under the journal's own directory.
    if (-not $inside -and -not $AllowOutside) {
        throw 'ATTRCUDA_OWNED_RECORD_OUTSIDE_JOURNAL_DIRECTORY the recorded path is not under the journal directory'
    }
    if ($Kind -ne 'gone' -and $null -eq $FileId) {
        throw 'ATTRCUDA_OWNED_RECORD_NO_IDENTITY a file or root record needs the identity read off its creating handle'
    }
    $record = [ordered]@{ p = $(if ($inside) { $full.Substring($journalDir.Length + 1) } else { $full }) }
    if ($Kind -eq 'gone') {
        $record['k'] = 'gone'
    } else {
        $record['v'] = [uint32]$FileId.VolumeSerialNumber
        $record['h'] = [uint32]$FileId.FileIndexHigh
        $record['l'] = [uint32]$FileId.FileIndexLow
        $record['r'] = [bool]$IsReparsePoint
        if ($Kind -eq 'root') {
            $record['k'] = 'root'
            $record['c'] = [int64]$FileId.CreationFileTime
        }
    }
    $encoding = [Text.UTF8Encoding]::new($false)
    $bytes = $encoding.GetBytes(($record | ConvertTo-Json -Compress) + "`n")
    $existing = Get-Item -LiteralPath $journalFull -Force -ErrorAction SilentlyContinue
    if ($null -ne $existing -and (($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw 'ATTRCUDA_OWNED_JOURNAL_IS_LINK the journal name is a reparse point; nothing was written'
    }
    # APPEND-ONLY access (FILE_APPEND_DATA without FILE_WRITE_DATA, through CreateFileW): every write lands at the
    # CURRENT end of file, atomically, whoever else appends. FileMode.Append only seeks to the end when the handle
    # is OPENED, so two jobs appending at once would overwrite each other's record.
    Initialize-AttrCudaFileIdNative
    $appendAccess = [uint32](0x00000004 -bor 0x00000080 -bor 0x00100000)
    $writeThrough = [uint32]2147483648
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ($true) {
        $stream = $null
        try {
            $created = $false
            $handle = [AttrCudaWin32.FileIdNative]::CreateFileW($journalFull, $appendAccess, [uint32]3, [IntPtr]::Zero, [uint32]1, $writeThrough, [IntPtr]::Zero)
            if ($handle -ne [IntPtr]::new(-1)) {
                $created = $true
            } else {
                $createError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                if ($createError -ne 80 -and $createError -ne 183) { throw "ATTRCUDA_OWNED_JOURNAL_UNAVAILABLE CreateFileW failed (Win32 error $createError)" }
                $handle = [AttrCudaWin32.FileIdNative]::CreateFileW($journalFull, $appendAccess, [uint32]3, [IntPtr]::Zero, [uint32]3, $writeThrough, [IntPtr]::Zero)
                if ($handle -eq [IntPtr]::new(-1)) { throw "ATTRCUDA_OWNED_JOURNAL_BUSY CreateFileW failed (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))" }
            }
            $stream = [IO.FileStream]::new([Microsoft.Win32.SafeHandles.SafeFileHandle]::new($handle, $true), [IO.FileAccess]::Write, 1, $false)
            $own = Get-AttrCudaFileId -Stream $stream
            if ($own.IsDirectory -or $own.NumberOfLinks -ne 1) {
                throw 'ATTRCUDA_OWNED_JOURNAL_HAS_SECOND_NAME the journal has more than one name; nothing was written'
            }
            if ($created) {
                $selfLine = ([ordered]@{ p = ''; k = 'self'; v = [uint32]$own.VolumeSerialNumber; h = [uint32]$own.FileIndexHigh; l = [uint32]$own.FileIndexLow } | ConvertTo-Json -Compress) + "`n"
                $selfBytes = $encoding.GetBytes($selfLine)
                $stream.Write($selfBytes, 0, $selfBytes.Length)
            }
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
            return
        } catch {
            if ($_.Exception.Message.StartsWith('ATTRCUDA_OWNED_JOURNAL_IS_LINK') -or $_.Exception.Message.StartsWith('ATTRCUDA_OWNED_JOURNAL_HAS_SECOND_NAME') -or [DateTime]::UtcNow -gt $deadline) { throw }
            Start-Sleep -Milliseconds 100
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

function Read-AttrCudaOwnedJournal {
    <#
    .SYNOPSIS
    Read a job's creator journal (Add-AttrCudaOwnedRecord) into { Dir; Files; Roots; Self }, keyed by
    the lower-cased path relative to the journal's directory. A missing journal is an EMPTY one, which
    proves nothing and therefore authorises no delete. Malformed lines are skipped.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Journal)

    $journalFull = [IO.Path]::GetFullPath($Journal)
    $files = @{}
    $roots = @{}
    $self = $null
    if (Test-Path -LiteralPath $journalFull -PathType Leaf) {
        $stream = [IO.File]::Open($journalFull, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8)
            try {
                while ($null -ne ($line = $reader.ReadLine())) {
                    if ([string]::IsNullOrWhiteSpace($line)) { continue }
                    try { $record = $line | ConvertFrom-Json } catch { continue }
                    $kind = if ($null -ne $record.PSObject.Properties['k']) { [string]$record.k } else { '' }
                    $key = ([string]$record.p).ToLowerInvariant()
                    if ($kind -eq 'self') { $self = $record; continue }
                    if ($kind -eq 'gone') { $roots.Remove($key); continue }
                    if ($kind -eq 'root') { $roots[$key] = $record; continue }
                    $files[$key] = $record
                }
            } finally {
                $reader.Dispose()
            }
        } finally {
            $stream.Dispose()
        }
    }
    [pscustomobject]@{
        Dir = [IO.Path]::GetDirectoryName($journalFull).TrimEnd('\')
        Files = $files
        Roots = $roots
        Self = $self
    }
}

function Get-AttrCudaOwnershipProof {
    <#
    .SYNOPSIS
    Resolve the proof the journal holds for ONE path: a creator-recorded file identity, or an
    attested fresh root that contains it; $null when the journal proves nothing.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-2. This NEVER reads the name's own identity and treats it as authority.
    A file record (written from the creating handle) wins. Otherwise the path must lie inside a
    directory this job created fresh and journalled (k = root) AND that directory must still be the
    very object that was recorded (its identity and creation time are re-read and compared here, so a
    directory swapped in under the name attests nothing). -RootCache memoises that check per call.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Journal,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$RootCache
    )

    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($Journal.Dir + '\', [StringComparison]::OrdinalIgnoreCase)) {
        # a record made with -AllowOutside is keyed by its absolute path; no root attests an outside path
        $outsideKey = $full.ToLowerInvariant()
        if (-not $Journal.Files.ContainsKey($outsideKey)) { return $null }
        $outside = $Journal.Files[$outsideKey]
        return [pscustomobject]@{
            Kind = 'file'
            FileId = [pscustomobject]@{ VolumeSerialNumber = [uint32]$outside.v; FileIndexHigh = [uint32]$outside.h; FileIndexLow = [uint32]$outside.l }
            ExpectReparsePoint = [bool]$outside.r
            NotBeforeFileTime = [int64]0
        }
    }
    $key = $full.Substring($Journal.Dir.Length + 1).ToLowerInvariant()
    if ($Journal.Files.ContainsKey($key)) {
        $record = $Journal.Files[$key]
        return [pscustomobject]@{
            Kind = 'file'
            FileId = [pscustomobject]@{ VolumeSerialNumber = [uint32]$record.v; FileIndexHigh = [uint32]$record.h; FileIndexLow = [uint32]$record.l }
            ExpectReparsePoint = [bool]$record.r
            NotBeforeFileTime = [int64]0
        }
    }
    foreach ($rootKey in @($Journal.Roots.Keys)) {
        if (-not $key.StartsWith($rootKey + '\', [StringComparison]::Ordinal)) { continue }
        if (-not $RootCache.ContainsKey($rootKey)) {
            $record = $Journal.Roots[$rootKey]
            $holds = $false
            try {
                $now = Get-AttrCudaFileId -Path (Join-Path $Journal.Dir ([string]$record.p))
                $holds = ($now.IsDirectory -and -not $now.IsReparsePoint -and
                    $now.VolumeSerialNumber -eq [uint32]$record.v -and
                    $now.FileIndexHigh -eq [uint32]$record.h -and
                    $now.FileIndexLow -eq [uint32]$record.l -and
                    $now.CreationFileTime -eq [int64]$record.c)
            } catch {
                $holds = $false
            }
            $RootCache[$rootKey] = $holds
        }
        if ($RootCache[$rootKey]) {
            return [pscustomobject]@{
                Kind = 'root'
                FileId = $null
                ExpectReparsePoint = $false
                NotBeforeFileTime = [int64]$Journal.Roots[$rootKey].c
            }
        }
    }
    return $null
}

function Remove-AttrCudaTree {
    <#
    .SYNOPSIS
    Empty and remove a job-owned directory under a trusted root WITHOUT a recursive pathname delete:
    every file goes through Remove-AttrCudaFileByProof (check and delete on one handle), directories
    go only when empty, and anything the journal does not PROVE is this job's is left where it is.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-2: -OwnedJournal is MANDATORY and has no default; there is no mode in
    which this function reads a file's current identity and treats it as authority (the previous
    no-journal mode did exactly that, and a legacy bind-proof hard link under a build job's scratch
    tree that had become the last name of an owner recording would have been deleted). A file is
    deleted only when the journal proves it:
      * a file record (Add-AttrCudaOwnedRecord, written at creation from the creating handle) names
        it AND its identity on the deleting handle is the recorded one; or
      * it lies inside a directory this job created fresh and journalled (New-AttrCudaOwnedRoot)
        that is still the recorded object, AND the file's own creation time is not before that
        directory's (a hard link to older bytes keeps the old creation time) -- see
        Remove-AttrCudaFileByProof.
    In both cases the object must also have exactly one name. Everything else is LEFT and reported:
    LEFT_UNOWNED (the journal proves nothing: never adopted), LEFT_MULTI_LINK, LEFT_ID_MISMATCH,
    LEFT_PREDATES_ROOT, LEFT_NOT_A_FILE, LEFT_UNAVAILABLE; the directories above it stay. Returns
    { Removed; Left = @({ Name; Rel; Token }); TreeRemoved } and throws nothing about leftovers: the
    caller decides what a non-empty result means. The ancestor chain is checked FIRST
    (Assert-AttrCudaNoLinkBelowRoot), even when the directory is absent (sol PR #133 r6). When the
    journal itself lives inside the tree it is removed last, by the identity it recorded for itself.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TrustedRoot,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$OwnedJournal
    )

    $Path = Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $TrustedRoot -Path $Path
    $root = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $root) { return [pscustomobject]@{ Removed = 0; Left = @(); TreeRemoved = $true } }

    $journalFull = [IO.Path]::GetFullPath($OwnedJournal)
    $journalData = Read-AttrCudaOwnedJournal -Journal $journalFull
    $rootCache = @{}
    $journalInside = $journalFull.StartsWith($Path + '\', [StringComparison]::OrdinalIgnoreCase)

    # Enumerate once, without descending into a reparse point.
    $ordered = [System.Collections.Generic.List[System.IO.FileSystemInfo]]::new()
    $stack = [System.Collections.Generic.Stack[System.IO.FileSystemInfo]]::new()
    $stack.Push($root)
    while ($stack.Count -gt 0) {
        $entry = $stack.Pop()
        [void]$ordered.Add($entry)
        $isReparse = (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        if ($entry -is [System.IO.DirectoryInfo] -and -not $isReparse) {
            foreach ($child in $entry.EnumerateFileSystemInfos()) { $stack.Push($child) }
        }
    }

    $removed = 0
    $left = [System.Collections.Generic.List[object]]::new()
    for ($i = $ordered.Count - 1; $i -ge 1; $i--) {
        $entry = $ordered[$i]
        $isReparse = (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        if ($journalInside -and [string]::Equals($entry.FullName, $journalFull, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($entry -is [System.IO.DirectoryInfo] -and -not $isReparse) {
            try { [IO.Directory]::Delete($entry.FullName, $false) } catch { }
            continue
        }
        $rel = ''
        if ($entry.FullName.StartsWith($Path + '\', [StringComparison]::OrdinalIgnoreCase)) { $rel = $entry.FullName.Substring($Path.Length + 1) }
        $proof = Get-AttrCudaOwnershipProof -Journal $journalData -Path $entry.FullName -RootCache $rootCache
        if ($null -eq $proof) {
            $token = 'LEFT_UNOWNED'
            try { if ((Get-AttrCudaFileId -Path $entry.FullName).NumberOfLinks -gt 1) { $token = 'LEFT_MULTI_LINK' } } catch { }
        } elseif ($proof.Kind -eq 'file') {
            $token = Remove-AttrCudaFileByProof -Path $entry.FullName -FileId $proof.FileId -ExpectReparsePoint:$proof.ExpectReparsePoint
        } elseif ($isReparse) {
            $token = 'LEFT_NOT_A_FILE'
        } else {
            $token = Remove-AttrCudaFileByProof -Path $entry.FullName -NotBeforeFileTime $proof.NotBeforeFileTime
        }
        if ($token -eq 'DELETED' -or $token -eq 'ABSENT') { $removed++ } else {
            [void]$left.Add([pscustomobject]@{ Name = $entry.Name; Rel = $rel; Token = $token })
        }
    }

    # The journal is this job's own bookkeeping file: it goes last, only when it lives inside this
    # tree and nothing else is left standing that it describes, and only by the identity it recorded
    # for ITSELF when it was created.
    if ($journalInside -and $left.Count -eq 0 -and $null -ne $journalData.Self -and (Test-Path -LiteralPath $journalFull -PathType Leaf)) {
        $selfId = [pscustomobject]@{ VolumeSerialNumber = [uint32]$journalData.Self.v; FileIndexHigh = [uint32]$journalData.Self.h; FileIndexLow = [uint32]$journalData.Self.l }
        $journalToken = Remove-AttrCudaFileById -Path $journalFull -FileId $selfId
        if ($journalToken -ne 'DELETED' -and $journalToken -ne 'ABSENT') {
            [void]$left.Add([pscustomobject]@{ Name = [IO.Path]::GetFileName($journalFull); Rel = $journalFull.Substring($Path.Length + 1); Token = $journalToken })
        }
    }
    if ($left.Count -eq 0) {
        try { [IO.Directory]::Delete($root.FullName, $false) } catch { }
    }
    [pscustomobject]@{ Removed = $removed; Left = @($left); TreeRemoved = (-not (Test-Path -LiteralPath $Path)) }
}

function New-AttrCudaOwnedRoot {
    <#
    .SYNOPSIS
    Create a job's scratch/publish directory FRESH, record its creation, and return the path: a
    directory that already stands under that name is never adopted and never deleted unproven.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-2. Build jobs write into trees whose files are made by child tools
    (qmake, make, nvcc, expand-archive), so no per-file creating handle exists. Ownership is instead
    proved at the TREE: this function creates the directory itself (it must not exist, must be empty,
    and its own creation time must be "now", so a directory someone else made is refused), journals
    its identity + creation FILETIME (Add-AttrCudaOwnedRecord -Kind root), and Remove-AttrCudaTree
    later deletes inside it only what was created after it (Remove-AttrCudaFileByProof
    -NotBeforeFileTime) with one name. A pre-existing directory at the name is handled by PROOF, not
    by looks: one this journal recorded as a fresh root (an earlier run of this code) is swept through
    Remove-AttrCudaTree; anything else -- a legacy tree an older build left, which may hold a hard
    link that is now the last name of an owner recording -- is MOVED ASIDE to
    '<name>.unproven-<utc>-<id>' (a rename deletes no name of any file inside it) and a warning
    records it, then the fresh directory is created. A tree that could not be fully swept is moved
    aside the same way. Returns { Path; Swept; Quarantined }.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TrustedRoot,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$OwnedJournal
    )

    $full = Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $TrustedRoot -Path $Path
    $parent = Get-Item -LiteralPath ([IO.Path]::GetDirectoryName($full)) -Force -ErrorAction SilentlyContinue
    if ($null -eq $parent -or -not $parent.PSIsContainer) { throw "ATTRCUDA_DIR_PARENT_MISSING $full" }
    if (($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "ATTRCUDA_DIR_PARENT_IS_LINK $($parent.FullName)" }

    $swept = 0
    $quarantined = ''
    $standing = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $standing) {
        if (-not $standing.PSIsContainer) { throw "ATTRCUDA_ROOT_OCCUPIED $full is a file" }
        $result = Remove-AttrCudaTree -TrustedRoot $TrustedRoot -Path $full -OwnedJournal $OwnedJournal
        $swept = [int]$result.Removed
        if (-not $result.TreeRemoved) {
            $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
            $quarantined = "$full.unproven-$stamp-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
            [IO.Directory]::Move($full, $quarantined)
            Write-Warning "ATTRCUDA_ROOT_QUARANTINED a standing tree could not be proved this job's ($(@($result.Left).Count) entries left); moved aside, nothing deleted: $quarantined"
        }
        Add-AttrCudaOwnedRecord -Journal $OwnedJournal -Path $full -Kind gone
    }
    $notBefore = [DateTime]::UtcNow.ToFileTimeUtc() - 20000000L
    [void](New-Item -ItemType Directory -Path $full)
    $id = Get-AttrCudaFileId -Path $full
    $children = @(Get-ChildItem -LiteralPath $full -Force -ErrorAction SilentlyContinue)
    if (-not $id.IsDirectory -or $id.IsReparsePoint -or $id.CreationFileTime -lt $notBefore -or $children.Count -gt 0) {
        throw "ATTRCUDA_ROOT_NOT_FRESH $full was not created by this call; it is not recorded and will not be deleted"
    }
    Add-AttrCudaOwnedRecord -Journal $OwnedJournal -Path $full -FileId $id -Kind root
    [pscustomobject]@{ Path = $full; Swept = $swept; Quarantined = $quarantined }
}

function Initialize-AttrCudaFileIdNative {
    <#
    .SYNOPSIS
    Define (once per process) the Win32 surface the file-identity functions below share:
    CreateFileW, GetFileInformationByHandle, SetFileInformationByHandle (delete-on-close
    disposition only) and CloseHandle.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1: ONE definition, so Get-AttrCudaFileId and Remove-AttrCudaFileById
    (and, through them, Remove-AttrCudaTree) never define the same native type twice with
    different members -- a second Add-Type for an already-defined name would silently keep the
    FIRST definition and lose the members only the second one declared. Idempotent; footage-neutral.
    #>
    [CmdletBinding()]
    param()

    if ('AttrCudaWin32.FileIdNative' -as [type]) { return }
    $definition = @'
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    public struct FileIdInfo {
        public uint FileAttributes;
        public uint CreationTimeLow;
        public uint CreationTimeHigh;
        public uint LastAccessTimeLow;
        public uint LastAccessTimeHigh;
        public uint LastWriteTimeLow;
        public uint LastWriteTimeHigh;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    public struct FileDispositionInfo {
        [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.U1)]
        public bool DeleteFile;
    }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    public struct FileBasicInfo {
        public long CreationTime;
        public long LastAccessTime;
        public long LastWriteTime;
        public long ChangeTime;
        public uint FileAttributes;
    }

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetFileInformationByHandle(System.IntPtr hFile, int fileInformationClass, ref FileBasicInfo lpFileInformation, uint dwBufferSize);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    public static extern System.IntPtr CreateFileW(string lpFileName, uint dwDesiredAccess, uint dwShareMode, System.IntPtr lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes, System.IntPtr hTemplateFile);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetFileInformationByHandle(System.IntPtr hFile, out FileIdInfo lpFileInformation);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetFileInformationByHandle(System.IntPtr hFile, int fileInformationClass, ref FileDispositionInfo lpFileInformation, uint dwBufferSize);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(System.IntPtr hObject);
'@
    Add-Type -Namespace AttrCudaWin32 -Name FileIdNative -MemberDefinition $definition -ErrorAction Stop
}

function Test-AttrCudaPresentMonTraceReady {
    <#
    .SYNOPSIS
    Has this PresentMon process finished starting its trace session? Returns an object with
    `ready` (bool) and `detail` (string); never throws.
    .DESCRIPTION
    UM-PRESENTMON-STOP-2 r2 (sol blocker). PresentMon 2.5.1 fixes its TimeInMs origin at the END of
    PMTraceSession::Start() (PresentData/PresentMonTraceSession.cpp: mStartTimestamp from QPC, after
    EnableProviders and OpenTraceW), and creates its output CSV lazily at the first present of the
    target process (PresentMon/CsvOutput.cpp UpdateCsvT) -- after the app is launched -- so neither the
    CSV nor its header can say "ready" before the launch. What does: PresentMon creates its message-only
    window (class 'PresentMon', title 'PresentMonWnd') BEFORE Start(), but its main thread only pumps
    that window's queue once Start() has returned and the consumer and output threads are up
    (PresentMon/MainThread.cpp). A cross-thread WM_NULL (SendMessageTimeout) to that window is therefore
    answered only AFTER the origin was set, so an answer is a verified upper bound for it. No window of
    that pid, or no answer within -TimeoutMs, is "not ready yet" -- never an assumption. A window of
    ANOTHER pid (a second PresentMon on the host) never counts.
    The Win32 surface is defined once per process (idempotent), like Initialize-AttrCudaFileIdNative.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Proc,
        [int]$TimeoutMs = 200
    )

    $detail = $null
    try {
        if (-not ('AttrCudaWin32.PresentMonProbe' -as [type])) {
            $definition = @'
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    public static extern System.IntPtr FindWindowExW(System.IntPtr parent, System.IntPtr after, string cls, string title);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(System.IntPtr hWnd, out uint pid);

    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    public static extern System.IntPtr SendMessageTimeoutW(System.IntPtr hWnd, uint msg, System.IntPtr wParam, System.IntPtr lParam, uint flags, uint timeoutMs, out System.IntPtr result);

    // 0 = no PresentMon message window for pid; 1 = its thread answered; 2 = window exists, no answer in time.
    public static int Probe(uint pid, uint timeoutMs) {
        System.IntPtr h = System.IntPtr.Zero;
        while (true) {
            h = FindWindowExW(new System.IntPtr(-3), h, "PresentMon", "PresentMonWnd");
            if (h == System.IntPtr.Zero) { return 0; }
            uint p;
            GetWindowThreadProcessId(h, out p);
            if (p != pid) { continue; }
            System.IntPtr r;
            return SendMessageTimeoutW(h, 0, System.IntPtr.Zero, System.IntPtr.Zero, 2, timeoutMs, out r) == System.IntPtr.Zero ? 2 : 1;
        }
    }
'@
            Add-Type -Namespace AttrCudaWin32 -Name PresentMonProbe -MemberDefinition $definition -ErrorAction Stop
        }
        $answer = [AttrCudaWin32.PresentMonProbe]::Probe([uint32]$Proc.Id, [uint32]$TimeoutMs)
        if ($answer -eq 1) { return [pscustomobject]@{ ready = $true; detail = 'the PresentMon message pump answered' } }
        $detail = if ($answer -eq 2) { 'the PresentMon message window exists but its main thread is not answering (trace session still starting)' } else { "no PresentMon message window for pid $($Proc.Id) yet" }
    } catch {
        $detail = "readiness probe failed: $($_.Exception.Message)"
    }
    [pscustomobject]@{ ready = $false; detail = $detail }
}

function ConvertTo-AttrCudaFileIdObject {
    <#
    .SYNOPSIS
    Shape a GetFileInformationByHandle result: volume serial, 64-bit file index (high/low), live
    hard-link count, attributes, and the two attribute bits callers branch on.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Info)

    [pscustomobject]@{
        VolumeSerialNumber = $Info.VolumeSerialNumber
        FileIndexHigh = $Info.FileIndexHigh
        FileIndexLow = $Info.FileIndexLow
        NumberOfLinks = $Info.NumberOfLinks
        FileAttributes = $Info.FileAttributes
        IsDirectory = (($Info.FileAttributes -band [uint32]0x10) -ne 0)
        IsReparsePoint = (($Info.FileAttributes -band [uint32]0x400) -ne 0)
        CreationFileTime = [int64](([uint64]$Info.CreationTimeHigh -shl 32) -bor [uint64]$Info.CreationTimeLow)
    }
}

function Get-AttrCudaFileId {
    <#
    .SYNOPSIS
    Return the Win32 file identity (volume serial, 64-bit file index, live hard-link count,
    attributes) of a path -- by default of the NAME ITSELF, never following a symbolic link or
    junction -- or of an open FileStream's own handle.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. -Path opens with FILE_READ_ATTRIBUTES only (no data access, so it
    conflicts with no other handle's share mode and never blocks or is blocked by a reader or
    writer) and FILE_FLAG_OPEN_REPARSE_POINT unless -FollowLinks is given, so a symlink reports its
    OWN identity and IsReparsePoint = $true rather than its target's. -FollowLinks reports the
    target's. -Stream reads the identity off the stream's own handle -- race-free, because no path
    is resolved at all: it is the identity of the very file object the stream holds.
    Exactly one of -Path / -Stream is required. Throws ATTRCUDA_FILE_ID_UNAVAILABLE on any Win32
    failure; the message never echoes the path.
    #>
    [CmdletBinding()]
    param(
        [string]$Path = '',
        [System.IO.FileStream]$Stream = $null,
        [switch]$FollowLinks
    )

    if (([string]::IsNullOrEmpty($Path)) -eq ($null -eq $Stream)) {
        throw 'ATTRCUDA_FILE_ID_UNAVAILABLE exactly one of -Path / -Stream is required'
    }
    Initialize-AttrCudaFileIdNative

    $handle = [IntPtr]::Zero
    $ownsHandle = $false
    if ($null -ne $Stream) {
        $handle = $Stream.SafeFileHandle.DangerousGetHandle()
    } else {
        $readAttributes = [uint32]0x80
        $shareAll = [uint32]0x00000007
        $openExisting = [uint32]3
        $flags = [uint32]0x02000000
        if (-not $FollowLinks) { $flags = [uint32]($flags -bor [uint32]0x00200000) }
        # fable r1 (ATTRCUDA-FILE-ID-LONG-PATH-1): CreateFileW refuses a path of 260+ characters unless it
        # carries the extended-length prefix, and Remove-Item -Recurse used to succeed there where the
        # identity-checked tree delete would otherwise refuse with ATTRCUDA_TREE_FILE_ID_UNAVAILABLE.
        $nativePath = $Path
        if ($nativePath.Length -ge 240 -and -not $nativePath.StartsWith('\\?\')) {
            $nativePath = [IO.Path]::GetFullPath($nativePath)
            if ($nativePath.StartsWith('\\')) { $nativePath = '\\?\UNC\' + $nativePath.Substring(2) } else { $nativePath = '\\?\' + $nativePath }
        }
        $handle = [AttrCudaWin32.FileIdNative]::CreateFileW(
            $nativePath, $readAttributes, $shareAll, [IntPtr]::Zero, $openExisting, $flags, [IntPtr]::Zero)
        if ($handle -eq [IntPtr]::new(-1)) {
            throw "ATTRCUDA_FILE_ID_UNAVAILABLE CreateFileW failed (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
        }
        $ownsHandle = $true
    }
    try {
        $info = [AttrCudaWin32.FileIdNative+FileIdInfo]::new()
        if (-not [AttrCudaWin32.FileIdNative]::GetFileInformationByHandle($handle, [ref]$info)) {
            throw "ATTRCUDA_FILE_ID_UNAVAILABLE GetFileInformationByHandle failed (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
        }
        ConvertTo-AttrCudaFileIdObject -Info $info
    } finally {
        if ($ownsHandle) { [void][AttrCudaWin32.FileIdNative]::CloseHandle($handle) }
    }
}

function Remove-AttrCudaFileByProof {
    <#
    .SYNOPSIS
    Delete ONE name, only against a PROOF the caller must supply; check and delete are one handle.
    Returns a fixed token; never throws. There is no mode without a proof.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-2: this is the ONLY function in the tree that sets a delete disposition,
    and its parameter sets each make exactly one kind of proof MANDATORY (none is defaulted, and no
    parameter set exists without one). A pathname is not an identity: between "this is my file" and
    "delete it" another process can replace the name with a hard link to something that matters, so
    the name is opened (FILE_FLAG_OPEN_REPARSE_POINT -- a symlink is opened as itself), share mode
    NONE, with DELETE access, every check is made on THAT handle, and the disposition is set on it.
    The three proofs, and what each can prove:
      -FileId <id>           CREATOR-RECORDED identity (volume serial + 64-bit file index read off the
                             handle that CREATED the file, Get-AttrCudaFileId -Stream). The object
                             on the name must be that exact one. Use -ExpectReparsePoint to delete a
                             symbolic link this caller created (the link only, never its target).
      -NotBeforeFileTime <n> the name is inside a directory this job CREATED FRESH and recorded
                             (New-AttrCudaOwnedRoot); an object whose own creation time predates that
                             directory cannot have been made by this job -- a hard link to an older
                             file carries the OLD file's creation time -- so it is left
                             (LEFT_PREDATES_ROOT). Never an identity read off the name.
      -ExpectedSha256 <hex>  CONTENT proof for a SUBMITTED INPUT the job did not create (a build
                             package or tracked fixture that was verified against its committed
                             hash): the bytes read through the deleting handle must hash to it, so a
                             name that is anything else -- owner footage included -- is left.
    Every set also requires: a plain file (not a directory, not a reparse point unless
    -ExpectReparsePoint) with exactly ONE name. Tokens: DELETED, ABSENT, LEFT_ID_MISMATCH,
    LEFT_PREDATES_ROOT, LEFT_CONTENT_MISMATCH, LEFT_MULTI_LINK, LEFT_NOT_A_FILE, LEFT_UNAVAILABLE.
    Anything but DELETED or ABSENT leaves the name exactly where it is.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true, ParameterSetName = 'ById')]$FileId,
        [Parameter(ParameterSetName = 'ById')][switch]$ExpectReparsePoint,
        [Parameter(Mandatory = $true, ParameterSetName = 'CreatedAfter')][long]$NotBeforeFileTime,
        [Parameter(Mandatory = $true, ParameterSetName = 'Content')][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256
    )

    try {
        Initialize-AttrCudaFileIdNative
        $set = $PSCmdlet.ParameterSetName
        $access = [uint64]0x00010000 -bor [uint64]0x80 -bor [uint64]0x100
        # The content proof reads the bytes through the SAME handle that deletes them (GENERIC_READ).
        if ($set -eq 'Content') { $access = $access -bor [uint64]2147483648 }
        # Share mode 0: while this handle is open nobody else can open the name to read, write, delete
        # or rename it, so what is read below is what is deleted.
        $shareNone = [uint32]0
        $openExisting = [uint32]3
        $flags = [uint32](0x02000000 -bor 0x00200000)
        $nativePath = $Path
        if ($nativePath.Length -ge 240 -and -not $nativePath.StartsWith('\\?\')) {
            $nativePath = [IO.Path]::GetFullPath($nativePath)
            if ($nativePath.StartsWith('\\')) { $nativePath = '\\?\UNC\' + $nativePath.Substring(2) } else { $nativePath = '\\?\' + $nativePath }
        }
        $handle = [AttrCudaWin32.FileIdNative]::CreateFileW(
            $nativePath, [uint32]$access, $shareNone, [IntPtr]::Zero, $openExisting, $flags, [IntPtr]::Zero)
        if ($handle -eq [IntPtr]::new(-1)) {
            $error32 = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            if ($error32 -eq 2 -or $error32 -eq 3) { return 'ABSENT' }
            return 'LEFT_UNAVAILABLE'
        }
        try {
            $info = [AttrCudaWin32.FileIdNative+FileIdInfo]::new()
            if (-not [AttrCudaWin32.FileIdNative]::GetFileInformationByHandle($handle, [ref]$info)) { return 'LEFT_UNAVAILABLE' }
            $id = ConvertTo-AttrCudaFileIdObject -Info $info
            if ($id.IsDirectory) { return 'LEFT_NOT_A_FILE' }
            if ($set -eq 'ById') {
                if ($id.IsReparsePoint -ne [bool]$ExpectReparsePoint) {
                    if ($id.IsReparsePoint) { return 'LEFT_NOT_A_FILE' }
                    return 'LEFT_ID_MISMATCH'
                }
                if ($id.VolumeSerialNumber -ne $FileId.VolumeSerialNumber -or
                    $id.FileIndexHigh -ne $FileId.FileIndexHigh -or
                    $id.FileIndexLow -ne $FileId.FileIndexLow) { return 'LEFT_ID_MISMATCH' }
            } else {
                if ($id.IsReparsePoint) { return 'LEFT_NOT_A_FILE' }
                if ($set -eq 'CreatedAfter' -and $id.CreationFileTime -lt $NotBeforeFileTime) { return 'LEFT_PREDATES_ROOT' }
            }
            if ($id.NumberOfLinks -ne 1) { return 'LEFT_MULTI_LINK' }
            if ($set -eq 'Content') {
                $sha = [Security.Cryptography.SHA256]::Create()
                try {
                    $safe = [Microsoft.Win32.SafeHandles.SafeFileHandle]::new($handle, $false)
                    $stream = [IO.FileStream]::new($safe, [IO.FileAccess]::Read, 65536, $false)
                    try {
                        $digest = ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLowerInvariant()
                    } finally {
                        $stream.Dispose()
                    }
                } finally {
                    $sha.Dispose()
                }
                if ($digest -ne $ExpectedSha256) { return 'LEFT_CONTENT_MISMATCH' }
            }
            $disposition = [AttrCudaWin32.FileIdNative+FileDispositionInfo]::new()
            $disposition.DeleteFile = $true
            if (-not [AttrCudaWin32.FileIdNative]::SetFileInformationByHandle($handle, 4, [ref]$disposition, [uint32]1)) {
                # A read-only file refuses the delete disposition. It is the verified object (proof and
                # one name checked above, on this handle), so clear the flag on THIS handle -- never by
                # path -- and retry once.
                $readOnly = [uint32]1
                if (($id.FileAttributes -band $readOnly) -eq 0) { return 'LEFT_UNAVAILABLE' }
                $basic = [AttrCudaWin32.FileIdNative+FileBasicInfo]::new()
                $newAttributes = [uint32]($id.FileAttributes -band (-bnot $readOnly))
                if ($newAttributes -eq 0) { $newAttributes = [uint32]0x80 }
                $basic.FileAttributes = $newAttributes
                if (-not [AttrCudaWin32.FileIdNative]::SetFileInformationByHandle($handle, 0, [ref]$basic, [uint32]40)) { return 'LEFT_UNAVAILABLE' }
                if (-not [AttrCudaWin32.FileIdNative]::SetFileInformationByHandle($handle, 4, [ref]$disposition, [uint32]1)) { return 'LEFT_UNAVAILABLE' }
            }
            return 'DELETED'
        } finally {
            [void][AttrCudaWin32.FileIdNative]::CloseHandle($handle)
        }
    } catch {
        return 'LEFT_UNAVAILABLE'
    }
}

function Remove-AttrCudaFileById {
    <#
    .SYNOPSIS
    Delete ONE name, only if it is still the exact file object this caller recorded when it created
    it, and that object still has exactly one name. Returns a fixed token; never throws.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1/2. The creator-recorded-identity proof of Remove-AttrCudaFileByProof
    (which holds the whole mechanism and its token list): -FileId is MANDATORY, read off the handle
    that created the file; a caller that never recorded one has no way to call this. The caller must
    already have closed its own handles to the name (the delete handle is share-none).
    -ExpectReparsePoint is for deleting a symbolic link this caller created: the link is opened as
    itself, its own identity is compared, and ONLY the link is removed -- never its target.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$FileId,
        [switch]$ExpectReparsePoint
    )

    Remove-AttrCudaFileByProof -Path $Path -FileId $FileId -ExpectReparsePoint:$ExpectReparsePoint
}

function Resolve-AttrCudaSmokeRunLog {
    <#
    .SYNOPSIS
    Return the authoritative log of the smoke run that produced $ResultJsonPath, or throw.
    .DESCRIPTION
    tools/profiling/run-release-gui-smoke.ps1 does NOT write into <output dir>\logs. It creates a
    GUID-nonced directory per run and then snapshots the exact lines its own result consumed:

        $runNonce = [Guid]::NewGuid().ToString("N")
        $outputStem = [IO.Path]::GetFileNameWithoutExtension($outputPath)
        $logRoot = Join-Path $outputDir ("logs-{0}-{1}" -f $outputStem, $runNonce)
        ...
        # Preserve the exact lines consumed by this result in a per-run immutable
        # snapshot.  The aggregate rotating app log is allowed to grow later and is
        # therefore diagnostic only; comparison authority comes from this snapshot.
        $runLogSnapshotPath = "$outputPath.run.log"
        [System.IO.File]::WriteAllLines($runLogSnapshotPath, [string[]]$recentLines, $utf8NoBom)
        $runLogSnapshotBinding = Get-EvidenceFileBinding `
            -Path $runLogSnapshotPath -Label "run log snapshot"

    and exposes both through result.json:

        log = [pscustomobject]@{
            path = $runLogSnapshotPath
            aggregateSourcePath = if ($logFile) { $logFile.FullName } else { $null }
        ...
        evidence = [pscustomobject]@{
            runNonce = $runNonce
            inputs = $captureInputBindings
            runLogSnapshot = $runLogSnapshotBinding

    So `logs\mlvapp-*.log` under the job's own output directory NEVER EXISTS, and a glob for it
    cannot reach any gate behind it (sol, PR #133 r2). This resolves `log.path`, the snapshot the
    runner itself calls the comparison authority, and binds it to
    `evidence.runLogSnapshot.sha256` so a log swapped after the run is refused rather than read.
    `log.aggregateSourcePath` is returned for the record but never read: the runner's own comment
    says the aggregate rotating log may grow after the run and is diagnostic only.
    Throws with a distinguishable ATTRCUDA_SMOKE_* token.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ResultJsonPath,

        # When set, the resolved log must live under this directory: the smoke runner is handed
        # an -Output inside the job's work tree, so a log.path pointing anywhere else means the
        # result.json being read is not this run's.
        [string]$ContainingRoot = ''
    )

    if (-not (Test-Path -LiteralPath $ResultJsonPath -PathType Leaf)) {
        throw "ATTRCUDA_SMOKE_RESULT_MISSING $ResultJsonPath"
    }
    $result = [IO.File]::ReadAllText($ResultJsonPath) | ConvertFrom-Json
    $logNode = $null
    if ($null -ne $result) { $logNode = $result.PSObject.Properties['log'] }
    if ($null -eq $logNode -or $null -eq $logNode.Value) {
        throw "ATTRCUDA_SMOKE_LOG_NODE_MISSING $ResultJsonPath has no log node"
    }
    $pathProperty = $logNode.Value.PSObject.Properties['path']
    $logPath = if ($null -eq $pathProperty) { '' } else { [string]$pathProperty.Value }
    if ([string]::IsNullOrWhiteSpace($logPath)) {
        throw "ATTRCUDA_SMOKE_LOG_PATH_ABSENT $ResultJsonPath declares no log.path"
    }
    if (-not (Test-Path -LiteralPath $logPath -PathType Leaf)) {
        throw "ATTRCUDA_SMOKE_LOG_MISSING $logPath (named by log.path in $ResultJsonPath)"
    }
    $fullLogPath = [IO.Path]::GetFullPath($logPath)
    if (-not [string]::IsNullOrWhiteSpace($ContainingRoot)) {
        $root = [IO.Path]::GetFullPath($ContainingRoot)
        if (-not $fullLogPath.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "ATTRCUDA_SMOKE_LOG_OUTSIDE_ROOT $fullLogPath is not under $root"
        }
    }
    $actualSha = (Get-FileHash -LiteralPath $fullLogPath -Algorithm SHA256).Hash.ToLowerInvariant()

    $declaredSha = ''
    $declaredLength = $null
    $runNonce = ''
    $evidenceNode = $result.PSObject.Properties['evidence']
    if ($null -ne $evidenceNode -and $null -ne $evidenceNode.Value) {
        $nonceProperty = $evidenceNode.Value.PSObject.Properties['runNonce']
        if ($null -ne $nonceProperty) { $runNonce = [string]$nonceProperty.Value }
        $snapshotProperty = $evidenceNode.Value.PSObject.Properties['runLogSnapshot']
        if ($null -ne $snapshotProperty -and $null -ne $snapshotProperty.Value) {
            $shaProperty = $snapshotProperty.Value.PSObject.Properties['sha256']
            if ($null -ne $shaProperty) { $declaredSha = ([string]$shaProperty.Value).ToLowerInvariant() }
            $lengthProperty = $snapshotProperty.Value.PSObject.Properties['length']
            if ($null -ne $lengthProperty -and $null -ne $lengthProperty.Value) { $declaredLength = [string]$lengthProperty.Value }
        }
    }
    if ([string]::IsNullOrWhiteSpace($declaredSha)) {
        throw "ATTRCUDA_SMOKE_LOG_UNBOUND $ResultJsonPath carries no evidence.runLogSnapshot.sha256 to bind $fullLogPath to"
    }
    if ($declaredSha -ne $actualSha) {
        throw "ATTRCUDA_SMOKE_LOG_SHA_MISMATCH $fullLogPath declared=$declaredSha actual=$actualSha"
    }
    # sol, PR #133 r3: the smoke runner binds the snapshot by sha256 AND length; both are checked.
    $actualLength = [int64](Get-Item -LiteralPath $fullLogPath).Length
    [int64]$parsedLength = -1
    if ($null -eq $declaredLength -or -not [int64]::TryParse($declaredLength, [ref]$parsedLength)) {
        throw "ATTRCUDA_SMOKE_LOG_LENGTH_UNBOUND $ResultJsonPath carries no integer evidence.runLogSnapshot.length"
    }
    if ($parsedLength -ne $actualLength) {
        throw "ATTRCUDA_SMOKE_LOG_LENGTH_MISMATCH $fullLogPath declared=$parsedLength actual=$actualLength"
    }

    $aggregateProperty = $logNode.Value.PSObject.Properties['aggregateSourcePath']
    [pscustomobject]@{
        path = $fullLogPath
        sha256 = $actualSha
        bytes = (Get-Item -LiteralPath $fullLogPath).Length
        runNonce = $runNonce
        source = 'result.json log.path (run log snapshot)'
        aggregateSourcePath = if ($null -eq $aggregateProperty) { $null } else { $aggregateProperty.Value }
    }
}

function Get-AttrCudaLastEligibilityLine {
    <#
    .SYNOPSIS
    Parse the LAST gpu_playback_recon.eligibility line out of a log, as a key/value hashtable.
    .DESCRIPTION
    The line (platform/qt/MainWindow.cpp, emitted under
    MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1) carries both bare (key=1) and quoted
    (key="some text") values, so the value alternation has to handle quotes -- r16_reason is
    quoted and routinely contains spaces. The LAST line wins: the probe re-runs per render
    policy evaluation and the final one describes the state the run ended in.
    Returns $null when the log carries no such line.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$LogText
    )

    $last = $null
    foreach ($line in ($LogText -split "`r?`n")) {
        if ($line -notmatch 'gpu_playback_recon\.eligibility ') { continue }
        $values = @{}
        foreach ($match in [regex]::Matches($line, '(?<key>[A-Za-z0-9_]+)=(?:"(?<quoted>[^"]*)"|(?<bare>[^\s]+))')) {
            $key = $match.Groups['key'].Value
            if ($match.Groups['quoted'].Success) { $values[$key] = $match.Groups['quoted'].Value }
            else { $values[$key] = $match.Groups['bare'].Value }
        }
        $last = $values
    }
    $last
}

function Get-AttrCudaEligibilityVerdict {
    <#
    .SYNOPSIS
    Decide whether a run may be attributed to the CUDA path at all, and with which exit code.
    .DESCRIPTION
    A run where the CUDA backend never loaded, or where the R16 texture path was not admitted,
    cannot produce a CUDA attribution -- the frame counters alone would happily describe some
    other path. A log with NO eligibility line at all is treated exactly like one that says no:
    absence of the diagnostic is not evidence of eligibility. exitCode is 15
    (BACKEND_NOT_AVAILABLE) whenever the run is not admitted, and every field is carried either
    way so a refusal says WHY.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$LogText
    )

    $values = Get-AttrCudaLastEligibilityLine -LogText $LogText
    $read = {
        param([string]$Key)
        if ($null -eq $values -or -not $values.ContainsKey($Key)) { return $null }
        [string]$values[$Key]
    }
    $cudaBackendAvailable = & $read 'cuda_backend_available'
    $r16Available = & $read 'r16_available'
    $admitted = ($cudaBackendAvailable -eq '1' -and $r16Available -eq '1')
    [pscustomobject]@{
        source = 'gpu_playback_recon.eligibility'
        linePresent = [bool]($null -ne $values)
        cudaBackendAvailable = $cudaBackendAvailable
        r16Available = $r16Available
        r16Reason = & $read 'r16_reason'
        cudaBackendAttempted = & $read 'cuda_backend_attempted'
        cudaBackendResolved = & $read 'cuda_backend_resolved'
        # CUDA-PLAYBACK-CONTACT-SHEET-1 r1d: the SAME line already carries the actual GPU
        # identity ("CUDA / <device name>", from igpu_recon_cuda.cu's cudaGetDeviceProperties
        # probe) and the actual playback scale factor the run used -- read them here rather
        # than have a caller re-probe or assume a constant, so a contact sheet composed from
        # this run can be labelled with the identity the run itself measured.
        cudaBackendDescription = & $read 'cuda_backend_description'
        scale = & $read 'scale'
        r16ProbeRan = & $read 'r16_probe_ran'
        admitted = $admitted
        exitCode = $(if ($admitted) { 0 } else { 15 })
    }
}

function ConvertTo-AttrCudaQuotedProcessArgument {
    <#
    .SYNOPSIS
    Quote one value for a Start-Process -ArgumentList element that may contain a space.
    .DESCRIPTION
    Start-Process's -ArgumentList does NOT quote array elements before joining them into the
    child's command line -- confirmed empirically: an unquoted element containing a space
    (e.g. a GPU description such as "CUDA / NVIDIA GeForce RTX 4090") silently splits into
    several argv entries in the child process, the exact class of bug the r1c -c-quoting
    BLOCKER was about. Wrapping every element in double quotes keeps it as one argv entry
    regardless of embedded spaces.

    HARDENING (fable NOTE, CUDA-PLAYBACK-CONTACT-SHEET-2): the embedded-quote escaping follows
    the CommandLineToArgvW/MSVCRT argv-quoting algorithm every value this function quotes is
    eventually parsed by (Start-Process's child is always a python.exe/py.exe interpreter, or
    in principle any Windows argv[] consumer): backslashes are literal EXCEPT immediately
    before a double quote, where each backslash must be doubled and the quote itself escaped
    as \" -- and a run of backslashes immediately before the CLOSING quote this function adds
    must also be doubled, since a quote follows them too. A naive doubled-quote ("" instead of
    \") is a *different* convention (cmd.exe's own), and a value ending in a bare backslash
    (e.g. a directory path with a trailing separator) would previously reach the parser as an
    escaped closing quote, corrupting the argument boundary. Every value this helper is
    actually called with today (hostname, GPU description, scale, build sha, the caller's own
    footage identifier, frames-dir/sheet-out/stats-out paths without a trailing backslash) contains neither
    backslashes nor quotes, so this is a correctness hardening with no behaviour change for
    any current caller.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $pendingBackslashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') {
            $pendingBackslashes++
            continue
        }
        if ($ch -eq '"') {
            # n backslashes immediately before a quote become 2n backslashes plus one escaped
            # quote -- each original backslash is preserved AND escaped, then the quote itself.
            [void]$builder.Append('\' * ($pendingBackslashes * 2))
            [void]$builder.Append('\"')
            $pendingBackslashes = 0
            continue
        }
        if ($pendingBackslashes -gt 0) {
            [void]$builder.Append('\' * $pendingBackslashes)
            $pendingBackslashes = 0
        }
        [void]$builder.Append($ch)
    }
    # Trailing backslashes (none seen yet followed by a quote) must be doubled: the closing
    # quote this function appends next would otherwise escape the last one instead of ending
    # the argument.
    if ($pendingBackslashes -gt 0) {
        [void]$builder.Append('\' * ($pendingBackslashes * 2))
    }
    [void]$builder.Append('"')
    $builder.ToString()
}

function Get-AttrCudaPresentMonDisplayReport {
    <#
    .SYNOPSIS
    Parse a raw PresentMon CSV into presented/displayed rates for the MLVApp preview chain, or a
    typed non-throwing refusal -- PRESENTMON_UNAVAILABLE or DISPLAY_ASLEEP -- never an uncaught
    exception once the smoke run itself has already passed.
    .DESCRIPTION
    CUDA-PERF-DISPLAY-IDENTITY-HARNESS-1/2/3. Defects this closes:
      - a missing or unreadable presentmon.csv used to reach Import-Csv unguarded and crash the
        whole job with nothing published (evidence: 3 of 8 baseline attempts on Bachelor died at
        Import-Csv of a missing out\diagnostic\presentmon.csv after a full smoke run);
      - "no positive MsBetweenDisplayChange samples" was an uncaught throw AFTER a passed smoke
        run, destroying every artifact already produced instead of reporting a typed outcome;
      - PresentMon runs --timed 55 against a --seconds 40 playback, so its raw CSV always
        contains idle desktop/startup presents outside the measured window; without restricting
        to that window, those contaminate the rate for whichever swap chain looks busiest;
      - (HARNESS-2, sol BLOCKER 3) the required-column set used to include a `DisplayedTime`
        column that the pinned PresentMon 2.5.1 legacy launch never emits -- every real capture
        read PRESENTMON_UNAVAILABLE. The required set now matches the real legacy CSV header
        exactly (Application, ProcessID, SwapChainAddress, PresentMode, MsBetweenPresents,
        MsBetweenDisplayChange, MsUntilDisplayed, TimeInMs, all confirmed present in real Bachelor
        captures), and "displayed" is derived from MsBetweenDisplayChange or MsUntilDisplayed --
        both real columns -- never from the fictional one;
      - (HARNESS-2, sol HARDENING) a swap chain recreated mid-run (e.g. a resize) used to split
        one continuous MLVApp preview across two (ProcessID, SwapChainAddress) groups, and only
        the busier one was reported, silently dropping the other's frames. The logical preview is
        now every row for the target PID, summed across every swap chain address it used inside
        the window.
      - (HARNESS-3, sol+fable HARDENING) HARNESS-2 windowed rows against a single guessed
        TimeInMs origin and claimed, in a comment, that anchoring on the earlier endpoint of the
        capture-start bracket "never excludes a row that truly falls inside the playback window".
        That direction argument was inverted: an anchor at or before PresentMon's true trace-
        session origin makes every row's own TimeInMs read SMALLER than it would under the true
        origin, which shifts the comparison window LATER and CAN exclude a genuine front-edge
        row -- the opposite of the claim. Neither endpoint is asserted exact in either direction
        any more. This function now windows the rows under BOTH endpoints of the caller's
        persisted capture-start bracket, reports presented/displayed counts and rates for each,
        counts how many rows' in/out window membership disagrees between them, and heads the
        report with whichever endpoint admits more DISPLAYED MLVApp rows (then more presented
        rows; ties to the earlier endpoint), so no display verdict rests on the endpoint that
        happened to miss the displayed rows. See .clockBracket below.
    Every row is also grouped by (ProcessID, SwapChainAddress) for .chains -- the actual display
    identity PresentMon reports, since a PID alone conflates multiple swap chains and a swap chain
    address alone says nothing about which process owns it -- but .selectedChain and
    .selectedChainRows are the PID-level aggregate described above, not a single address's rows.
    Windowing: TimeInMs is read as milliseconds since each of -EarliestCaptureStartUtc and
    -LatestCaptureStartUtc in turn (the two endpoints of the wall-clock bracket the caller places
    around PresentMon's own startup -- see playback-attr-3-cuda-job.ps1); only rows whose derived
    timestamp falls within [process.startedAtUtc, process.endedAtUtc] -- the exact lifetime of the
    launched MLVApp process -- count toward either rate, so idle time before launch or after exit
    (up to ~15s of it, per --timed 55 against --seconds 40) never counts as a display sample under
    either endpoint. .clockBracket on every returned status reports both endpoints' counts/rates,
    .rowsDifferingInWindowMembership (the count of rows -- across every process, not just
    MLVApp's -- whose in/out status flips between the two endpoints), and .headline/.headlineReason
    naming which endpoint .chains/.selectedChain/.selectedChainRows are actually built from.
    A "displayed" sample is MsBetweenDisplayChange > 0, or (when that field is NA -- observed on
    the very first present of a capture) MsUntilDisplayed > 0: either is a genuine screen update.
    A "presented" sample is any row for the PID, including one that never displayed -- kept, never
    discarded, so the presented rate is not silently inflated by discarding it and not silently
    deflated by treating it as a display.
    Returns .status one of 'OK' | 'PRESENTMON_UNAVAILABLE' | 'DISPLAY_ASLEEP'; .reason is $null
    only for 'OK'. On 'OK', .chains lists every (ProcessID, SwapChainAddress) group observed in
    the window, for audit; .selectedChain is the PID-level aggregate (its swapChainAddresses lists
    every address summed into it). Both carry .presentModes -- every distinct PresentMode string
    observed in that chain's rows, with its own row count (PRESENTMON-HARNESS-ROBUSTNESS-1) -- so
    a leg admitting only a handful of rows (e.g. a full-screen leg that lost most of its samples)
    can be diagnosed from evidence alone: an exclusive/hardware mode dropping to a composed one
    mid-capture is a real signal PresentMon itself reports, not something counts alone can show.
    .selectedChainRows carries only its positive-display rows,
    shaped exactly like this job's historical pmRows
    (ordinal/timeInMs/msBetweenDisplayChange/displayFpsEquivalent/presentMode), for
    presentmon-series.csv -- tools/profiling/refresh_period_histogram.py depends on that exact
    column name and never sees this function or its chain-selection at all. A row displayed only
    via MsUntilDisplayed (MsBetweenDisplayChange reads NA) carries msBetweenDisplayChange=$null and
    displayFpsEquivalent=$null here -- it is a genuine displayed sample with no interval to report,
    never a zero one; every caller aggregating msBetweenDisplayChange across .selectedChainRows
    must filter $null/non-positive values out BEFORE computing statistics, exactly as
    refresh_period_histogram.py already does reading the CSV this produces.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$CsvPath,

        [Parameter(Mandatory = $true)]
        [AllowNull()]
        [object]$ResultJson,

        # The earlier endpoint of the caller's capture-start bracket (e.g. the OS-reported
        # PresentMon process start, or the pre-spawn wall clock when the OS did not report one).
        [Parameter(Mandatory = $true)]
        [datetime]$EarliestCaptureStartUtc,

        # The later endpoint (e.g. the wall clock sampled after Start-PresentMonCapture's own
        # liveness confirmation returns).
        [Parameter(Mandatory = $true)]
        [datetime]$LatestCaptureStartUtc
    )

    function Get-AttrCudaJsonProperty($Object, [string]$Name) {
        if ($null -eq $Object) { return $null }
        $prop = $Object.PSObject.Properties[$Name]
        if ($null -eq $prop) { return $null }
        $prop.Value
    }

    $unavailable = {
        param([string]$Reason, [object[]]$Chains = @(), [object]$Selected = $null, [object]$ClockBracket = $null)
        [pscustomobject]@{
            status = 'PRESENTMON_UNAVAILABLE'
            reason = $Reason
            chains = @($Chains)
            selectedChain = $Selected
            selectedChainRows = @()
            clockBracket = $ClockBracket
        }
    }

    $processNode = Get-AttrCudaJsonProperty $ResultJson 'process'
    $rawPid = Get-AttrCudaJsonProperty $processNode 'id'
    $rawStart = Get-AttrCudaJsonProperty $processNode 'startedAtUtc'
    $rawEnd = Get-AttrCudaJsonProperty $processNode 'endedAtUtc'

    [int64]$targetPid = 0
    $windowStartUtc = [datetime]::MinValue
    $windowEndUtc = [datetime]::MinValue
    $identityOk = $true
    if ($null -eq $rawPid -or -not [int64]::TryParse([string]$rawPid, [ref]$targetPid) -or $targetPid -le 0) { $identityOk = $false }
    if ($identityOk) {
        try {
            $windowStartUtc = [datetime]::Parse([string]$rawStart, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
        } catch { $identityOk = $false }
    }
    if ($identityOk) {
        try {
            $windowEndUtc = [datetime]::Parse([string]$rawEnd, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
        } catch { $identityOk = $false }
    }
    if ($identityOk -and $windowEndUtc -le $windowStartUtc) { $identityOk = $false }
    if (-not $identityOk) {
        return (& $unavailable "result.json process.id/startedAtUtc/endedAtUtc is missing or malformed -- cannot identify the MLVApp process or its playback window")
    }

    if (-not (Test-Path -LiteralPath $CsvPath -PathType Leaf)) {
        return (& $unavailable "PresentMon output does not exist: $CsvPath")
    }
    try {
        $rawRows = @(Import-Csv -LiteralPath $CsvPath)
    } catch {
        return (& $unavailable "PresentMon output at $CsvPath could not be parsed: $($_.Exception.Message)")
    }
    if ($rawRows.Count -eq 0) {
        return (& $unavailable "PresentMon output at $CsvPath has no rows")
    }

    # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 3): matches the columns the PINNED
    # PresentMon 2.5.1 legacy launch actually emits -- confirmed against real Bachelor captures
    # (tools/profiling/bachelor/playback-attr-3-cuda-*.artifacts/presentmon.csv). There is no
    # DisplayedTime column in that schema (that was only ever a synthetic-fixture column, never a
    # real one, and required it made every real capture PRESENTMON_UNAVAILABLE); 'displayed' is
    # derived below from MsBetweenDisplayChange and MsUntilDisplayed instead, both real columns.
    $requiredColumns = @('Application', 'ProcessID', 'SwapChainAddress', 'PresentMode', 'MsBetweenPresents', 'MsBetweenDisplayChange', 'MsUntilDisplayed', 'TimeInMs')
    $columns = @($rawRows[0].PSObject.Properties.Name)
    $missingColumns = @($requiredColumns | Where-Object { $columns -notcontains $_ })
    if ($missingColumns.Count -gt 0) {
        return (& $unavailable "PresentMon output at $CsvPath is missing required column(s): $($missingColumns -join ', ')")
    }

    # Every row inside a window is kept, including MsBetweenDisplayChange == 0 -- discarding those
    # (the old behaviour) silently inflated the displayed rate to equal the presented rate. This
    # first pass only parses each row once, independent of either bracket endpoint; windowing
    # (which depends on the endpoint) happens separately below, per endpoint.
    $parsedRows = [System.Collections.Generic.List[object]]::new()
    # PLAYBACK-VSYNC-DEFAULT-1 >>>
    # SyncInterval and AllowsTearing are real PresentMon 2.5.1 columns, but not required ones (an older
    # capture or a fixture may lack them): a missing column reads 'absent'.
    $hasSyncIntervalColumn = $columns -contains 'SyncInterval'
    $hasAllowsTearingColumn = $columns -contains 'AllowsTearing'
    # PLAYBACK-VSYNC-DEFAULT-1 <<<
    $ordinal = 0
    foreach ($row in $rawRows) {
        [double]$timeInMs = 0.0
        if (-not [double]::TryParse([string]$row.TimeInMs, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$timeInMs)) { continue }

        [int64]$rowPid = 0
        [void][int64]::TryParse([string]$row.ProcessID, [ref]$rowPid)

        [double]$displayChange = 0.0
        $hasDisplayChange = [double]::TryParse([string]$row.MsBetweenDisplayChange, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$displayChange)

        [double]$betweenPresents = 0.0
        $hasBetweenPresents = [double]::TryParse([string]$row.MsBetweenPresents, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$betweenPresents)

        [double]$untilDisplayed = 0.0
        $hasUntilDisplayed = [double]::TryParse([string]$row.MsUntilDisplayed, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$untilDisplayed)

        [void]$parsedRows.Add([pscustomobject]@{
            ordinal = $ordinal
            application = [string]$row.Application
            processId = $rowPid
            swapChainAddress = [string]$row.SwapChainAddress
            presentMode = [string]$row.PresentMode
            # PLAYBACK-VSYNC-DEFAULT-1 >>>
            syncInterval = if ($hasSyncIntervalColumn) { [string]$row.SyncInterval } else { 'absent' }
            allowsTearing = if ($hasAllowsTearingColumn) { [string]$row.AllowsTearing } else { 'absent' }
            # PLAYBACK-VSYNC-DEFAULT-1 <<<
            timeInMs = $timeInMs
            msBetweenPresents = if ($hasBetweenPresents) { $betweenPresents } else { $null }
            msBetweenDisplayChange = if ($hasDisplayChange) { $displayChange } else { $null }
            msUntilDisplayed = if ($hasUntilDisplayed) { $untilDisplayed } else { $null }
            # PresentMon's real legacy schema carries no boolean "was this frame displayed"
            # column -- a genuine screen update is either a positive MsBetweenDisplayChange (this
            # present changed what is on screen, relative to the previous display change) or,
            # when that field reads NA (observed on the very first present of a capture, before
            # any prior display change exists to measure from), a positive MsUntilDisplayed (this
            # present was itself clocked reaching the screen).
            displayed = (($hasDisplayChange -and $displayChange -gt 0) -or ($hasUntilDisplayed -and $untilDisplayed -gt 0))
        })
        $ordinal++
    }

    # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3: builds one bracket endpoint's whole view (windowed
    # rows, per-chain grouping, the PID-level selected aggregate) so both endpoints can be built
    # identically and compared -- see the corrected windowing-direction note in .DESCRIPTION above.
    $buildWindow = {
        param([datetime]$AnchorUtc)
        $windowStartMs = ($windowStartUtc - $AnchorUtc).TotalMilliseconds
        $windowEndMs = ($windowEndUtc - $AnchorUtc).TotalMilliseconds
        # windowSeconds (the real playback duration) is invariant to which endpoint anchors the
        # window: both windowStartMs and windowEndMs shift by the same amount as AnchorUtc moves.
        $windowSeconds = ($windowEndMs - $windowStartMs) / 1000.0
        $windowedRows = @($parsedRows | Where-Object { $_.timeInMs -ge $windowStartMs -and $_.timeInMs -le $windowEndMs })

        $chains = [System.Collections.Generic.List[object]]::new()
        foreach ($group in ($windowedRows | Group-Object -Property processId, swapChainAddress)) {
            $groupRows = @($group.Group)
            $displayedRows = @($groupRows | Where-Object { $_.displayed })
            [void]$chains.Add([pscustomobject]@{
                processId = $groupRows[0].processId
                swapChainAddress = $groupRows[0].swapChainAddress
                application = $groupRows[0].application
                presentedCount = $groupRows.Count
                displayedCount = $displayedRows.Count
                presentedFps = if ($windowSeconds -gt 0) { $groupRows.Count / $windowSeconds } else { $null }
                displayedFps = if ($windowSeconds -gt 0) { $displayedRows.Count / $windowSeconds } else { $null }
                isMlvAppChain = ($groupRows[0].processId -eq $targetPid)
                # PRESENTMON-HARNESS-ROBUSTNESS-1: a thin/full-screen-style sample loss (e.g. a
                # leg admitting only 1 presented/displayed row) cannot be diagnosed from counts
                # alone -- PresentMode ("Hardware: Independent Flip" vs "Composed: Flip" vs a
                # borderless/exclusive-fullscreen variant) is the field that actually explains
                # WHY PresentMon lost samples on a given chain. Recorded per chain, every mode
                # actually observed, so a future full-screen leg's evidence answers the question
                # on its own instead of needing a live repro to diagnose.
                presentModes = @(
                    $groupRows | Group-Object -Property presentMode | ForEach-Object {
                        [pscustomobject]@{ presentMode = $_.Name; count = $_.Count }
                    }
                )
                # PLAYBACK-VSYNC-DEFAULT-1 >>>
                # The interval each present asked for and whether it was allowed to tear, as
                # PresentMon saw them -- the app's own swap-interval log is not proof of either.
                # Same per-value counts as presentModes.
                syncIntervals = @(
                    $groupRows | Group-Object -Property syncInterval | ForEach-Object {
                        [pscustomobject]@{ syncInterval = $_.Name; count = $_.Count }
                    }
                )
                allowsTearing = @(
                    $groupRows | Group-Object -Property allowsTearing | ForEach-Object {
                        [pscustomobject]@{ allowsTearing = $_.Name; count = $_.Count }
                    }
                )
                # PLAYBACK-VSYNC-DEFAULT-1 <<<
            })
        }
        $mlvAppChains = @($chains | Where-Object { $_.isMlvAppChain } | Sort-Object -Property presentedCount -Descending)

        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol HARDENING): the logical MLVApp preview is
        # bound to its PID, not to a single swap chain address -- see $chains above for the
        # per-address audit view. Every row for the target PID, across every address it used
        # inside this endpoint's window, is summed into one logical chain here.
        $mlvAppRows = @($windowedRows | Where-Object { $_.processId -eq $targetPid })
        $mlvAppDisplayedRows = @($mlvAppRows | Where-Object { $_.displayed })
        # Set-StrictMode -Version Latest (module-scoped, line 21) makes member access on an EMPTY
        # collection a terminating error ("the property cannot be found on this object") instead
        # of the usual silent $null -- unlike the earlier per-anchor early-return, $selected is
        # now always built (even when $mlvAppChains is empty, e.g. a headline-losing endpoint),
        # so the empty case is guarded explicitly here rather than relying on that early return.
        $mlvAppAddresses = if ($mlvAppChains.Count -gt 0) { @($mlvAppChains.swapChainAddress) } else { @() }
        $selected = [pscustomobject]@{
            processId = $targetPid
            swapChainAddress = ($mlvAppAddresses -join ', ')
            swapChainAddresses = $mlvAppAddresses
            application = if ($mlvAppChains.Count -gt 0) { $mlvAppChains[0].application } else { $null }
            presentedCount = $mlvAppRows.Count
            displayedCount = $mlvAppDisplayedRows.Count
            presentedFps = if ($windowSeconds -gt 0) { $mlvAppRows.Count / $windowSeconds } else { $null }
            displayedFps = if ($windowSeconds -gt 0) { $mlvAppDisplayedRows.Count / $windowSeconds } else { $null }
            isMlvAppChain = $true
            # PRESENTMON-HARNESS-ROBUSTNESS-1: summed across every swap chain address this PID
            # used in the window, mirroring $chains' per-chain presentModes above -- see that
            # comment for why this field exists.
            presentModes = @(
                $mlvAppRows | Group-Object -Property presentMode | ForEach-Object {
                    [pscustomobject]@{ presentMode = $_.Name; count = $_.Count }
                }
            )
            # PLAYBACK-VSYNC-DEFAULT-1 >>>
            # See the per-chain syncIntervals/allowsTearing above.
            syncIntervals = @(
                $mlvAppRows | Group-Object -Property syncInterval | ForEach-Object {
                    [pscustomobject]@{ syncInterval = $_.Name; count = $_.Count }
                }
            )
            allowsTearing = @(
                $mlvAppRows | Group-Object -Property allowsTearing | ForEach-Object {
                    [pscustomobject]@{ allowsTearing = $_.Name; count = $_.Count }
                }
            )
            # PLAYBACK-VSYNC-DEFAULT-1 <<<
        }
        $selectedChainRows = @(
            $mlvAppDisplayedRows |
                ForEach-Object {
                    [pscustomobject]@{
                        ordinal = $_.ordinal
                        timeInMs = $_.timeInMs
                        msBetweenDisplayChange = $_.msBetweenDisplayChange
                        displayFpsEquivalent = if ($null -ne $_.msBetweenDisplayChange -and $_.msBetweenDisplayChange -gt 0) { 1000.0 / $_.msBetweenDisplayChange } else { $null }
                        presentMode = $_.presentMode
                    }
                }
        )

        [pscustomobject]@{
            windowSeconds = $windowSeconds
            # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b: the job's temporal-coverage arm needs the
            # window's own bounds (relative to this same anchor as every row's timeInMs) to measure
            # head/tail gaps against -- not just windowSeconds (the span), which cannot say WHERE
            # inside the window a run of positive-interval rows sits.
            windowStartMs = $windowStartMs
            windowEndMs = $windowEndMs
            windowedRows = $windowedRows
            # UM-PRESENTMON-STOP-2: every MLVApp present (displayed or not) this endpoint admits, as its raw
            # TimeInMs, so a job-stopped capture can be judged by POSITION against the app's swap window.
            selectedPresentTimesMs = @($mlvAppRows | ForEach-Object { [double]$_.timeInMs })
            chains = @($chains)
            # Named targetChains, not the more obvious name built from "MLVApp" + "Chains", purely
            # so dot-accessing it below never spells a footage-extension-shaped token: tools/
            # repo_hygiene/test_playback_attr_3_cuda_split_route.py::NoFootageTokensTests forbids
            # that substring anywhere in this file as a media-extension guard, and a property
            # access on the obvious name would trip it exactly like a stray media file path would.
            targetChains = $mlvAppChains
            selected = $selected
            selectedChainRows = $selectedChainRows
        }
    }

    $earliestBuild = & $buildWindow $EarliestCaptureStartUtc
    $latestBuild = & $buildWindow $LatestCaptureStartUtc

    # The count of rows (any process, not just MLVApp's) whose window membership disagrees
    # between the two endpoints -- how much the choice of clock origin actually moves the data.
    $earliestOrdinals = [System.Collections.Generic.HashSet[int]]::new([int[]]@($earliestBuild.windowedRows | ForEach-Object { $_.ordinal }))
    $latestOrdinals = [System.Collections.Generic.HashSet[int]]::new([int[]]@($latestBuild.windowedRows | ForEach-Object { $_.ordinal }))
    $onlyEarliest = [System.Collections.Generic.HashSet[int]]::new($earliestOrdinals)
    $onlyEarliest.ExceptWith($latestOrdinals)
    $onlyLatest = [System.Collections.Generic.HashSet[int]]::new($latestOrdinals)
    $onlyLatest.ExceptWith($earliestOrdinals)
    $rowsDiffering = $onlyEarliest.Count + $onlyLatest.Count

    # Neither endpoint is asserted to be the exact TimeInMs origin (see .DESCRIPTION).
    # HARNESS-4 (sol BLOCKER / fable HARDENING on #163, agreed fix): rank endpoints by DISPLAYED MLVApp rows first, then
    # presented rows, ties to earliest. Ranking by presented alone could head the report with an endpoint that admits only
    # an undisplayed tail row while the other endpoint admits a genuinely displayed front-edge row -> a false DISPLAY_ASLEEP.
    # With displayed ranked first, the headline's displayedCount is the maximum over both endpoints, so DISPLAY_ASLEEP below
    # fires only when NEITHER endpoint admits a displayed MLVApp row.
    $latestWins = ($latestBuild.selected.displayedCount -gt $earliestBuild.selected.displayedCount) -or
        (($latestBuild.selected.displayedCount -eq $earliestBuild.selected.displayedCount) -and
         ($latestBuild.selected.presentedCount -gt $earliestBuild.selected.presentedCount))
    $headline = if ($latestWins) { 'latest' } else { 'earliest' }
    $headlineBuild = if ($headline -eq 'latest') { $latestBuild } else { $earliestBuild }
    $headlineReason = "the earliest bracket endpoint ($($EarliestCaptureStartUtc.ToString('o'))) admits $($earliestBuild.selected.presentedCount) MLVApp-presented row(s) into the playback window; the latest endpoint ($($LatestCaptureStartUtc.ToString('o'))) admits $($latestBuild.selected.presentedCount); displayed counts are earliest=$($earliestBuild.selected.displayedCount) latest=$($latestBuild.selected.displayedCount); the headline uses the '$headline' endpoint because it admits more DISPLAYED rows (then more presented rows; ties to earliest), so a display verdict never rests on the endpoint that happened to miss the displayed rows -- neither endpoint's admitted set is asserted to be the exact one, and $rowsDiffering row(s) (any process) disagree on window membership between them"
    $clockBracket = [pscustomobject]@{
        earliestOriginUtc = $EarliestCaptureStartUtc.ToString('o')
        latestOriginUtc = $LatestCaptureStartUtc.ToString('o')
        headline = $headline
        headlineReason = $headlineReason
        rowsDifferingInWindowMembership = $rowsDiffering
        earliest = [pscustomobject]@{
            presentedCount = $earliestBuild.selected.presentedCount
            displayedCount = $earliestBuild.selected.displayedCount
            presentedFps = $earliestBuild.selected.presentedFps
            displayedFps = $earliestBuild.selected.displayedFps
        }
        latest = [pscustomobject]@{
            presentedCount = $latestBuild.selected.presentedCount
            displayedCount = $latestBuild.selected.displayedCount
            presentedFps = $latestBuild.selected.presentedFps
            displayedFps = $latestBuild.selected.displayedFps
        }
    }

    if ($earliestBuild.windowedRows.Count -eq 0 -and $latestBuild.windowedRows.Count -eq 0) {
        return (& $unavailable "no PresentMon rows fall inside the playback window [$($windowStartUtc.ToString('o')), $($windowEndUtc.ToString('o'))] under either endpoint of the capture-start bracket [$($EarliestCaptureStartUtc.ToString('o')), $($LatestCaptureStartUtc.ToString('o'))]" @() $null $clockBracket)
    }

    if ($headlineBuild.selected.presentedCount -le 0) {
        return (& $unavailable "no PresentMon rows in the playback window belong to MLVApp process id $targetPid under either bracket endpoint (earliest presented=$($earliestBuild.selected.presentedCount), latest presented=$($latestBuild.selected.presentedCount))" $headlineBuild.chains $null $clockBracket)
    }

    if ($headlineBuild.selected.displayedCount -le 0) {
        return [pscustomobject]@{
            status = 'DISPLAY_ASLEEP'
            reason = "MLVApp process id $targetPid presented $($headlineBuild.selected.presentedCount) frame(s) across $($headlineBuild.targetChains.Count) swap chain(s) in the playback window (headline endpoint: $headline) but displayed 0 -- the panel may be asleep or the window occluded"
            chains = @($headlineBuild.chains)
            selectedChain = $headlineBuild.selected
            selectedChainRows = @()
            clockBracket = $clockBracket
        }
    }

    [pscustomobject]@{
        status = 'OK'
        reason = $null
        chains = @($headlineBuild.chains)
        selectedChain = $headlineBuild.selected
        selectedChainRows = $headlineBuild.selectedChainRows
        selectedPresentTimesMs = $headlineBuild.selectedPresentTimesMs
        clockBracket = $clockBracket
        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b: the headline endpoint's own window bounds, on the
        # same anchor as selectedChainRows' timeInMs -- see the buildWindow comment above.
        windowStartMs = $headlineBuild.windowStartMs
        windowEndMs = $headlineBuild.windowEndMs
    }
}

function Get-AttrCudaQuiescenceSample {
    <#
    .SYNOPSIS
    One venue-quiescence sample: BOTH the legacy Win32_Processor.LoadPercentage (utility, kept
    for continuity) and the \Processor(_Total)\% Processor Time counter (busy TIME, the gating
    metric) -- never only the first.
    .DESCRIPTION
    UM-DISPLAY-SELECT-AND-LOG-1 round 2b. LoadPercentage is frequency-scaled processor UTILITY,
    not busy time: a hub probe on Ultra-Magnus (2026-09-26T15:51Z, same ~26s window) read
    LoadPercentage at 73/82/83 while \Processor(_Total)\% Processor Time read 21.4/43.2/37 on the
    same i9-13900KS -- turbo inflates the former, so gating on it refused three legs at 58-83%
    while actual busy time was ~15-40%. Both are still recorded (utilityPercent is diagnostic
    context, never the gate), but timePercent is the one a caller compares to a threshold.
    .OUTPUTS
    [pscustomobject] with utilityPercent (double, $null if Win32_Processor could not be read),
    timePercent (double, $null if the counter could not be read) and timePercentError (the
    exception's bare type name, sanitized, when timePercent is $null; $null otherwise). Third
    state, never folded into either number: a caller that finds timePercent -eq $null must treat
    the whole sample as UNKNOWN, never as "0% busy".
    #>
    [CmdletBinding()]
    param()

    $utilityPercent = $null
    try {
        $utilityPercent = [double](Get-CimInstance Win32_Processor -ErrorAction Stop |
            Measure-Object -Property LoadPercentage -Average).Average
    } catch {
        $utilityPercent = $null
    }

    $timePercent = $null
    $timePercentError = $null
    try {
        $counter = Get-Counter -Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop
        $sample = $counter.CounterSamples[0]
        # UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol BLOCKER 4 / opus design-review hardening):
        # validate the RAW sample BEFORE any cast. [double]$null casts to 0, so an unread/null
        # CookedValue used to silently pass as "0% busy" (a caller compares timePercent to a
        # threshold and would then never refuse). Status is PDH's own signal that the value is
        # trustworthy -- 0 (VALID_DATA) or 1 (NEW_DATA); anything else is an unread sample, never a
        # "0% busy" one. Round 3 (sol hardening): an ABSENT Status is no longer accepted as valid --
        # a real PDH CounterSample always carries one, so a sample without it cannot be vouched for.
        # A finite range check catches NaN/Infinity/out-of-
        # range CookedValue values that would otherwise cast cleanly and compare as neither
        # -gt nor -le the threshold.
        # $sample.Status (not .PSObject.Properties['Status'].Value) throws
        # PropertyNotFoundException under $ErrorActionPreference='Stop' when the sample object
        # has no such member at all (this module's own mocks/tests included) -- the property
        # lookup below never throws for a missing member, it simply returns $null.
        $statusProp = $sample.PSObject.Properties['Status']
        $status = if ($statusProp) { $statusProp.Value } else { $null }
        if ($null -eq $status) {
            throw [System.InvalidOperationException]::new('counter sample carries no Status')
        }
        if ($status -ne 0 -and $status -ne 1) {
            throw [System.InvalidOperationException]::new("counter sample status $status is not valid")
        }
        if ($null -eq $sample.CookedValue) {
            throw [System.InvalidOperationException]::new('counter sample CookedValue is null')
        }
        $cooked = [double]$sample.CookedValue
        if (-not [double]::IsFinite($cooked) -or $cooked -lt 0.0 -or $cooked -gt 100.0) {
            throw [System.InvalidOperationException]::new("counter sample CookedValue $cooked is out of range")
        }
        $timePercent = $cooked
    } catch {
        $timePercent = $null
        $timePercentError = $_.Exception.GetType().Name
    }

    [pscustomobject]@{
        utilityPercent = $utilityPercent
        timePercent = $timePercent
        timePercentError = $timePercentError
    }
}

function Get-AttrCudaProcessCpuSnapshot {
    <#
    .SYNOPSIS
    {Id, Name, cpuSeconds} for every readable process right now, for Get-AttrCudaTopCpuProcesses.
    .DESCRIPTION
    A process that disappears, or whose CPU time is momentarily unreadable, is dropped from the
    snapshot rather than failing the whole collection -- this is evidentiary (which process was
    busy), never the gate itself, so a single throwing process must not take the sample down.
    #>
    [CmdletBinding()]
    param()

    $rows = @()
    try {
        $procs = Get-Process -ErrorAction Stop
    } catch {
        return $rows
    }
    foreach ($p in $procs) {
        try {
            if ($null -eq $p.TotalProcessorTime) { continue }
            $rows += [pscustomobject]@{
                id = $p.Id
                name = $p.Name
                cpuSeconds = $p.TotalProcessorTime.TotalSeconds
            }
        } catch {
            continue
        }
    }
    $rows
}

function Get-AttrCudaTopCpuProcesses {
    <#
    .SYNOPSIS
    Top -Count processes by CPU-SECONDS CONSUMED between two Get-AttrCudaProcessCpuSnapshot calls
    (never a point-in-time percentage), published as venue-quiescence evidence on both the pass
    and refusal paths.
    .DESCRIPTION
    UM-DISPLAY-SELECT-AND-LOG-1 round 2b: the hub probe that found LoadPercentage's turbo-
    inflation also found vmware-vmx (the board VM, which runs OTHER projects' builds) as the top
    CPU consumer over the same window -- callers are expected to annotate that name specially
    (see -VmProcessName), but this function itself makes no policy decision, only the ranking.
    A process present in only one snapshot (started or exited mid-window) contributes nothing --
    matched by Id, so PID reuse across the window cannot merge two different processes' time.
    .OUTPUTS
    Up to -Count [pscustomobject] rows, each {name, pid, cpuSeconds, note}, sorted by cpuSeconds
    descending. note is $null except for -VmProcessName's exact name match (case-insensitive).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Before,

        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$After,

        [int]$Count = 10,

        [string]$VmProcessName = 'vmware-vmx'
    )

    $beforeById = @{}
    foreach ($p in $Before) { $beforeById[[int]$p.id] = $p }

    $deltas = @()
    foreach ($p in $After) {
        $prior = $beforeById[[int]$p.id]
        if ($null -eq $prior) { continue }
        $delta = [double]$p.cpuSeconds - [double]$prior.cpuSeconds
        if ($delta -le 0) { continue }
        $deltas += [pscustomobject]@{
            name = $p.name
            pid = $p.id
            cpuSeconds = [math]::Round($delta, 3)
            note = $(if ($p.name -ieq $VmProcessName) { 'board VM (other projects can build here)' } else { $null })
        }
    }
    $deltas | Sort-Object -Property cpuSeconds -Descending | Select-Object -First $Count
}

function Get-AttrCudaWindowsDisplayInventory {
    <#
    .SYNOPSIS
    The Windows view of every active display, independent of Qt: EnumDisplayDevices (adapter
    name, adapter string, monitor friendly name, primary flag) and EnumDisplaySettings's CURRENT
    mode (width, height, refresh, bits) for each one -- UM-DISPLAY-SELECT-AND-LOG-1 item 2.
    .DESCRIPTION
    Ultra-Magnus's primary display is an LG TV through a Denon AVR, both at 4K when on; when the
    TV is off the primary falls back to the Denon's headless output at a DEGRADED resolution
    (evidence: a 2026-09-26 probe recorded "2x 2560x1440"). This is the ground truth a caller
    compares the app's own QScreen inventory against, so a leg that silently benchmarked the
    degraded fallback is provable independent of what the app itself reported.
    Read-only: never calls ChangeDisplaySettings or any SPI_SET* -- enumeration only.
    .OUTPUTS
    [pscustomobject] { collected (bool); devices (array of {deviceName, adapterString,
    monitorName, isPrimary, modeCollected, width, height, refreshHz, bitsPerPixel}); error (the
    exception's bare type name, sanitized, when collected is $false) }. Third state: collected
    -eq $false means NOTHING here is trustworthy -- a caller must treat the whole inventory as
    unknown, never as "zero displays". A single adapter's mode failing to read (modeCollected
    -eq $false) does not fail the rest of the inventory -- that adapter's width/height/refreshHz/
    bitsPerPixel are $null, its own third state.
    #>
    [CmdletBinding()]
    param()

    $result = [ordered]@{ collected = $false; devices = @(); error = $null }
    try {
        if (-not ("AttrCudaNativeDisplay" -as [type])) {
            Add-Type -TypeDefinition @"
                using System;
                using System.Runtime.InteropServices;

                [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
                public struct AttrCudaDisplayDevice
                {
                    public int cb;
                    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
                    public string DeviceName;
                    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
                    public string DeviceString;
                    public int StateFlags;
                    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
                    public string DeviceID;
                    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
                    public string DeviceKey;
                }

                [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
                public struct AttrCudaDevMode
                {
                    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
                    public string dmDeviceName;
                    public short dmSpecVersion;
                    public short dmDriverVersion;
                    public short dmSize;
                    public short dmDriverExtra;
                    public int dmFields;
                    public int dmPositionX;
                    public int dmPositionY;
                    public int dmDisplayOrientation;
                    public int dmDisplayFixedOutput;
                    public short dmColor;
                    public short dmDuplex;
                    public short dmYResolution;
                    public short dmTTOption;
                    public short dmCollate;
                    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
                    public string dmFormName;
                    public short dmLogPixels;
                    public int dmBitsPerPel;
                    public int dmPelsWidth;
                    public int dmPelsHeight;
                    public int dmDisplayFlags;
                    public int dmDisplayFrequency;
                    public int dmICMMethod;
                    public int dmICMIntent;
                    public int dmMediaType;
                    public int dmDitherType;
                    public int dmReserved1;
                    public int dmReserved2;
                    public int dmPanningWidth;
                    public int dmPanningHeight;
                }

                public static class AttrCudaNativeDisplay
                {
                    // Two overloads, deliberately: PowerShell's $null binds to a P/Invoke
                    // 'string' parameter as an EMPTY string, not a true NULL pointer -- and
                    // EnumDisplayDevices treats lpDevice="" as "no such adapter" (returns
                    // false immediately), not as "enumerate adapters" (lpDevice=NULL). The
                    // adapter-enumeration call below always passes IntPtr.Zero through the
                    // first overload; every other call passes a real device name string
                    // through the second.
                    [DllImport("user32.dll", EntryPoint = "EnumDisplayDevicesW", SetLastError = true)]
                    public static extern bool EnumDisplayDevices(IntPtr lpDevice, uint iDevNum, ref AttrCudaDisplayDevice lpDisplayDevice, uint dwFlags);

                    [DllImport("user32.dll", EntryPoint = "EnumDisplayDevicesW", SetLastError = true, CharSet = CharSet.Unicode)]
                    public static extern bool EnumDisplayDevicesNamed(string lpDevice, uint iDevNum, ref AttrCudaDisplayDevice lpDisplayDevice, uint dwFlags);

                    [DllImport("user32.dll", EntryPoint = "EnumDisplaySettingsW", SetLastError = true, CharSet = CharSet.Unicode)]
                    public static extern bool EnumDisplaySettings(string lpszDeviceName, int iModeNum, ref AttrCudaDevMode lpDevMode);
                }
"@
        }

        # ATTACHED_TO_DESKTOP = 0x1, PRIMARY_DEVICE = 0x4 -- read-only enumeration flags, not
        # ChangeDisplaySettings/SPI_SET* (this function never mutates display state).
        $attachedFlag = 0x1
        $primaryFlag = 0x4
        $currentSettingsMode = -1
        $devices = New-Object System.Collections.Generic.List[object]
        $adapterIndex = 0
        while ($true) {
            $adapter = New-Object AttrCudaDisplayDevice
            $adapter.cb = [System.Runtime.InteropServices.Marshal]::SizeOf($adapter)
            $adapterOk = [AttrCudaNativeDisplay]::EnumDisplayDevices([IntPtr]::Zero, $adapterIndex, [ref]$adapter, 0)
            if (-not $adapterOk) { break }
            $adapterIndex++
            if (($adapter.StateFlags -band $attachedFlag) -eq 0) { continue }

            $monitorName = $null
            $monitor = New-Object AttrCudaDisplayDevice
            $monitor.cb = [System.Runtime.InteropServices.Marshal]::SizeOf($monitor)
            if ([AttrCudaNativeDisplay]::EnumDisplayDevicesNamed($adapter.DeviceName, 0, [ref]$monitor, 0)) {
                if (-not [string]::IsNullOrWhiteSpace($monitor.DeviceString)) {
                    $monitorName = $monitor.DeviceString
                }
            }

            $mode = New-Object AttrCudaDevMode
            $mode.dmSize = [System.Runtime.InteropServices.Marshal]::SizeOf($mode)
            $modeOk = [AttrCudaNativeDisplay]::EnumDisplaySettings($adapter.DeviceName, $currentSettingsMode, [ref]$mode)

            $devices.Add([pscustomobject]@{
                deviceName = $adapter.DeviceName
                adapterString = $adapter.DeviceString
                monitorName = $monitorName
                isPrimary = (($adapter.StateFlags -band $primaryFlag) -ne 0)
                modeCollected = $modeOk
                width = $(if ($modeOk) { $mode.dmPelsWidth } else { $null })
                height = $(if ($modeOk) { $mode.dmPelsHeight } else { $null })
                refreshHz = $(if ($modeOk) { $mode.dmDisplayFrequency } else { $null })
                bitsPerPixel = $(if ($modeOk) { $mode.dmBitsPerPel } else { $null })
            })
        }

        $result.collected = $true
        # .ToArray(), never @($devices): wrapping a List[object] directly in the array
        # subexpression operator throws "Argument types do not match" -- measured on both
        # Windows PowerShell 5.1 and pwsh 7 in this environment -- so the List's own ToArray()
        # is used instead of relying on @()'s enumeration of a generic List.
        $result.devices = $devices.ToArray()
    } catch {
        $result.collected = $false
        $result.devices = @()
        $result.error = $_.Exception.GetType().Name
    }

    [pscustomobject]$result
}

function Get-AttrCudaMeasurementVenue {
    <#
    .SYNOPSIS
    Which display-expectation venue this leg is running in -- UM-DISPLAY-SELECT-AND-LOG-1 item 2.
    .DESCRIPTION
    Only 'ultra-magnus' carries a known expected resolution (the owner's LG-TV/Denon-AVR rig,
    4K when the TV is on -- see .claude-state/project-memory/
    um-display-topology-lg-tv-denon-fallback-20260926.md). No display resolution for any other
    host (Bachelor included) is pinned anywhere in this repo, so every other name -- Bachelor's
    own included -- returns 'bachelor': record the display identity and mode, assert nothing.
    -ComputerName defaults to $env:COMPUTERNAME (the real host at run time); a caller overrides it
    to test the classification without touching the process environment.
    .OUTPUTS
    'ultra-magnus' or 'bachelor' (string).
    #>
    [CmdletBinding()]
    param([string]$ComputerName = $env:COMPUTERNAME)

    if ($ComputerName -and $ComputerName -match '(?i)ultra.?magnus') { return 'ultra-magnus' }
    return 'bachelor'
}

function Resolve-AttrCudaPreferredDisplay {
    <#
    .SYNOPSIS
    Maps the venue's preferred monitor NAME (as Windows reports it, e.g. 'ASUS PA329C') to the
    GDI device name (\\.\DISPLAYn) the app can match exactly -- UM-DISPLAY-SELECT-AND-LOG-1
    round 2 (sol PRE-REVIEW #2 BLOCKER a).
    .DESCRIPTION
    The Windows inventory pairs each deviceName with its monitorName, so the job resolves the pair
    here and hands the app the GDI device name. CORRECTED (UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1):
    this used to claim QScreen::name() IS the GDI device name. It is not, on a monitor that has an
    EDID name: measured on UM (Qt 6.10.2, session 1) name()/model() are the friendly name
    ('PA329C', 'LG TV'), and only a monitor without one reports '\\.\DISPLAYn'. The app therefore
    derives each screen's GDI device from its native origin + physical size
    (platform/qt/DisplayDeviceMapping.h, logged as display_screen device=) and compares THAT with
    the device name handed over here; before that fix the preference silently vanished and the
    equal-4K refresh tie fell to the primary (the LG TV). Statuses (recorded, never
    gated): 'mapped' (exactly one device's monitorName contains the substring, case-insensitive;
    argument = its deviceName), 'ambiguous' (two or more), 'absent' (none), 'unknown' (the
    inventory itself is unreadable), 'none' (no preference configured). In every non-mapped case
    argument stays the original substring, so the app's own name/model/manufacturer match remains
    the fallback rather than the preference being dropped.
    .OUTPUTS
    [pscustomobject] { argument; status; deviceName; monitorName; matches (int) }.
    #>
    [CmdletBinding()]
    param($WindowsInventory, [string]$Substring)

    $result = [ordered]@{ argument = $Substring; status = 'none'; deviceName = $null; monitorName = $null; matches = 0 }
    if ([string]::IsNullOrWhiteSpace($Substring)) { $result.argument = ''; return [pscustomobject]$result }
    if ($null -eq $WindowsInventory -or -not $WindowsInventory.collected) {
        $result.status = 'unknown'
        return [pscustomobject]$result
    }
    $found = @($WindowsInventory.devices | Where-Object {
        $_.deviceName -and $_.monitorName -and ([string]$_.monitorName).IndexOf($Substring, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    $result.matches = $found.Count
    if ($found.Count -eq 1) {
        $result.status = 'mapped'
        $result.argument = [string]$found[0].deviceName
        $result.deviceName = [string]$found[0].deviceName
        $result.monitorName = [string]$found[0].monitorName
    } elseif ($found.Count -gt 1) {
        $result.status = 'ambiguous'
    } else {
        $result.status = 'absent'
    }
    return [pscustomobject]$result
}

function Get-AttrCudaDisplayDegradedState {
    <#
    .SYNOPSIS
    Three-state degraded verdict for the chosen display target vs this venue's expectation.
    .DESCRIPTION
    UM-DISPLAY-SELECT-AND-LOG-1 item 3: a leg on a degraded display is RECORDED, never gated, but
    it must never be silently reported as "not degraded" when it is really "cannot tell". Returns
    'unknown' (string) whenever a verdict cannot be asserted either way -- no expected resolution
    for this venue (ExpectedWidth/Height $null, e.g. Bachelor), the target's own width/height
    could not be read, OR (round 1c, sol BLOCKER 2 / opus design-review hardening) the INDEPENDENT
    Windows-API inventory this verdict is meant to be cross-checked against was itself unreadable
    -- -WindowsCollected $false (Get-AttrCudaWindowsDisplayInventory threw) or
    -WindowsAnyModeCollected $false (it returned collected=true but zero devices, or every device
    had modeCollected=false, e.g. the headless/Session-0 contexts this fleet has hit before) both
    make the verdict unknown, never "not degraded" purely on the app's own Qt-reported size. Both
    parameters default to $true so an existing caller that has already established the Windows
    inventory is trustworthy (or is testing the target-vs-expected comparison in isolation) is
    unaffected. Never averages or guesses through a $null.
    .OUTPUTS
    'unknown', or a [bool] (true = degraded: target narrower or shorter than expected).
    #>
    [CmdletBinding()]
    param($TargetWidth, $TargetHeight, $ExpectedWidth, $ExpectedHeight,
          [bool]$WindowsCollected = $true, [bool]$WindowsAnyModeCollected = $true)

    if (-not $WindowsCollected) { return 'unknown' }
    if (-not $WindowsAnyModeCollected) { return 'unknown' }
    if ($null -eq $ExpectedWidth -or $null -eq $ExpectedHeight) { return 'unknown' }
    if ($null -eq $TargetWidth -or $null -eq $TargetHeight) { return 'unknown' }
    return [bool]($TargetWidth -lt $ExpectedWidth -or $TargetHeight -lt $ExpectedHeight)
}

function Get-AttrCudaGuiSmokeDisplaySelection {
    <#
    .SYNOPSIS
    Parses the app's own gui_smoke.display_screen / display_target / window_placement lines
    (MainWindow.cpp, UM-DISPLAY-SELECT-AND-LOG-1) out of a smoke run's raw log text.
    .DESCRIPTION
    A THIN DELEGATE (round 3, binding design-review item opus-blocker-1): the one parser is
    ConvertFrom-GuiSmokeDisplayLog in tools/profiling/gui-smoke-display-identity.ps1, which the
    smoke runner also dot-sources -- so the job and the runner cannot publish two different
    identities for one leg. In an emitted job that function is already embedded (from the same
    committed bytes the smoke-runner closure stages), so Get-Command finds it; imported as a
    module (the tests, the generators) it is dot-sourced from the checkout, once per call.
    See ConvertFrom-GuiSmokeDisplayLog for the returned shape and the parsing rule.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$LogText
    )

    if (-not (Get-Command ConvertFrom-GuiSmokeDisplayLog -CommandType Function -ErrorAction SilentlyContinue)) {
        . (Join-Path $PSScriptRoot '..\gui-smoke-display-identity.ps1')
    }
    ConvertFrom-GuiSmokeDisplayLog -LogText $LogText
}

function Find-AttrCudaFailedSmokeDisplayLog {
    <#
    .SYNOPSIS
    DISPLAY-SMOKE-FAILED-LOG-PRESERVE-1 (origin PR #191): locates the app log a FAILED smoke run left
    behind -- with no result.json to point at it -- and parses its display lines with the one shared
    parser, so a SMOKE_RUN_FAILED leg can publish a post-smoke/measured display block when the app
    really did report one.
    .DESCRIPTION
    run-release-gui-smoke.ps1 creates `logs-<stem>-<32-hex nonce>` next to its -Output file (the
    job's result.json, so <stem> is 'result') and the app writes mlvapp-*.log into it; only the
    runner's LATER result.json + `<output>.run.log` snapshot are missing after a failure. Never
    guessed: the directory must (a) match that exact name shape, (b) have been CREATED at or after
    the launch instant (a leftover from an earlier run is stale), and (c) be the ONLY such
    directory (two candidates are ambiguous -- reported, not resolved by picking one). In it the
    newest mlvapp-*.log is read (the runner's own choice) and only lines timestamped at or after the
    launch instant (-2 s, the runner's own window) are kept. found is $true only when the parsed
    selection carries at least one display screen, target or placement -- a log that says nothing
    about the display leaves the caller pre-smoke. Never throws.
    .OUTPUTS
    [pscustomobject] { found (bool); reason (string, why not found); logPath; selection (the
    Get-AttrCudaGuiSmokeDisplaySelection result, or $null) }.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LegOut,

        [Parameter(Mandatory = $true)]
        [datetime]$LaunchedAtUtc,

        [string]$ResultStem = 'result'
    )

    $result = [pscustomobject]@{ found = $false; reason = $null; logPath = $null; selection = $null }
    try {
        $since = $LaunchedAtUtc.ToUniversalTime().AddSeconds(-2)
        $dirs = @()
        if (Test-Path -LiteralPath $LegOut -PathType Container) {
            $namePattern = '^logs-' + [regex]::Escape($ResultStem) + '-[0-9a-f]{32}$'
            $dirs = @(Get-ChildItem -LiteralPath $LegOut -Directory -ErrorAction Stop |
                Where-Object { $_.Name -match $namePattern -and $_.CreationTimeUtc -ge $since })
        }
        if ($dirs.Count -eq 0) {
            $result.reason = 'no logs-<stem>-<nonce> directory was created since the smoke launch'
            return $result
        }
        if ($dirs.Count -gt 1) {
            $result.reason = "ambiguous: $($dirs.Count) logs-<stem>-<nonce> directories were created since the smoke launch"
            return $result
        }
        $logFile = @(Get-ChildItem -LiteralPath $dirs[0].FullName -Filter 'mlvapp-*.log' -File -ErrorAction Stop |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1)
        if ($logFile.Count -eq 0) {
            $result.reason = 'the run log directory holds no mlvapp-*.log'
            return $result
        }
        $kept = New-Object System.Collections.Generic.List[string]
        foreach ($line in Get-Content -LiteralPath $logFile[0].FullName) {
            $stamp = [regex]::Match([string]$line, '^\[(?<ts>[^\]]+)\]')
            if (-not $stamp.Success) { continue }
            $parsed = [datetime]::MinValue
            $styles = [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal
            if (-not [datetime]::TryParse($stamp.Groups['ts'].Value, [System.Globalization.CultureInfo]::InvariantCulture, $styles, [ref]$parsed)) { continue }
            if ($parsed -ge $since) { $kept.Add([string]$line) }
        }
        $selection = Get-AttrCudaGuiSmokeDisplaySelection -LogText ($kept.ToArray() -join "`n")
        if (-not ($selection.screensCollected -or $null -ne $selection.target -or $null -ne $selection.placement)) {
            $result.reason = 'the run log was located but carries no gui_smoke.display_* line since the smoke launch (display never reported)'
            return $result
        }
        $result.found = $true
        $result.logPath = $logFile[0].FullName
        $result.selection = $selection
    } catch {
        $result.found = $false
        $result.selection = $null
        $result.reason = 'display-log recovery failed: ' + $_.Exception.GetType().Name
    }
    return $result
}

# CUDA-PERF-DISPLAY-WAKE-1. OWNER (2026-09-25): "if display is asleep just wake it. its just the
# blank screensaver". Measured legs on Bachelor kept ending DISPLAY_ASLEEP (presented but
# displayed 0) while the interactive session's blank screensaver was up. These three functions
# wake the display from that session before a leg launches MLVApp and hold it awake for the leg's
# duration, exactly the way a real user's mouse would -- never gated on whether the leg turns out
# to need it, and never allowed to fail the leg: every Win32 call is wrapped so a failure is
# RECORDED in the returned evidence object, never thrown.

function Register-AttrCudaDisplayWakeNativeMethods {
    <#
    .SYNOPSIS
    Loads the small P/Invoke surface (SendInput, SystemParametersInfo, SetThreadExecutionState)
    the display-wake functions below need, or returns $false -- never throws. Idempotent: a type
    already loaded (e.g. a second call within the same job) is detected and Add-Type is skipped,
    since redefining the same type in one process throws.
    #>
    [CmdletBinding()]
    param()
    if ("MLVAppAttrCudaDisplayWake.NativeMethods" -as [type]) { return $true }
    try {
        # Indented so NO line of this embedded C# starts with an unindented '}' -- that column-0
        # shape is exactly what Get-AttrCudaEmbeddedFunctionSource's own extraction regex (see
        # its header above) uses to find THIS function's closing brace, and a namespace/class
        # brace sitting at column 0 would end the extraction early, silently truncating this
        # function wherever it is embedded.
        Add-Type -TypeDefinition @'
    using System;
    using System.Runtime.InteropServices;
    using System.Text;

    namespace MLVAppAttrCudaDisplayWake
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct MOUSEINPUT
        {
            public int dx;
            public int dy;
            public uint mouseData;
            public uint dwFlags;
            public uint time;
            public IntPtr dwExtraInfo;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct KEYBDINPUT
        {
            public ushort wVk;
            public ushort wScan;
            public uint dwFlags;
            public uint time;
            public IntPtr dwExtraInfo;
        }

        // INPUT is really a union (mouse/keyboard/hardware); only the mouse and keyboard arms
        // are ever populated here, so it is flattened with explicit offsets rather than modelled
        // as a full union -- both mi and ki are pinned at the same offset 8, the well-known
        // layout for a SendInput caller on x64 (dwType at 0, the union member at 8 for the
        // 8-byte alignment MOUSEINPUT's/KEYBDINPUT's trailing IntPtr requires; total size 40
        // bytes -- MOUSEINPUT is the larger of the two arms at 32 bytes -- matching the real
        // Windows INPUT struct on x64).
        [StructLayout(LayoutKind.Explicit)]
        public struct INPUT
        {
            [FieldOffset(0)] public int type;
            [FieldOffset(8)] public MOUSEINPUT mi;
            [FieldOffset(8)] public KEYBDINPUT ki;
        }

        public static class NativeMethods
        {
            [DllImport("user32.dll", SetLastError = true)]
            public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

            [DllImport("user32.dll", SetLastError = true)]
            public static extern bool SystemParametersInfo(uint uiAction, uint uiParam, ref bool pvParam, uint fWinIni);

            // Same native function as above (SystemParametersInfoW), a second managed overload
            // for the SPI_* actions whose pvParam is an int (e.g. SPI_GETSCREENSAVETIMEOUT) rather
            // than a bool -- EntryPoint pins both to the one Win32 export.
            [DllImport("user32.dll", EntryPoint = "SystemParametersInfo", SetLastError = true)]
            public static extern bool SystemParametersInfoInt(uint uiAction, uint uiParam, ref int pvParam, uint fWinIni);

            [DllImport("kernel32.dll")]
            public static extern uint SetThreadExecutionState(uint esFlags);

            // CUDA-PERF-DISPLAY-WAKE-2 round 1c: OpenInputDesktop resolves whichever desktop is
            // CURRENTLY receiving input (round 1b evidence: a plain SendInput failed with
            // ERROR_ACCESS_DENIED once a non-secure screen saver had already engaged), and
            // SetThreadDesktop reassigns a caller-supplied thread to it -- see
            // Invoke-AttrCudaInputDesktopNudge's own header for the "why a dedicated thread"
            // constraint Microsoft documents for SetThreadDesktop.
            [DllImport("user32.dll", SetLastError = true)]
            public static extern IntPtr OpenInputDesktop(uint dwFlags, bool fInherit, uint dwDesiredAccess);

            [DllImport("user32.dll", SetLastError = true)]
            public static extern bool SetThreadDesktop(IntPtr hDesktop);

            [DllImport("user32.dll", SetLastError = true)]
            public static extern bool CloseDesktop(IntPtr hDesktop);

            // CUDA-PERF-DISPLAY-WAKE-3 round 1 (sol hardening): read back the calling thread's
            // OWN desktop before SetThreadDesktop reassigns it away, so it can be switched back
            // before CloseDesktop -- Microsoft documents that CloseDesktop fails while any thread
            // is still using the desktop, and the nudge thread itself is exactly such a thread
            // until it switches off hDesktop again.
            [DllImport("user32.dll", SetLastError = true)]
            public static extern IntPtr GetThreadDesktop(uint dwThreadId);

            [DllImport("kernel32.dll")]
            public static extern uint GetCurrentThreadId();

            // CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol BLOCKER fix): reads back the NAME of the
            // desktop hDesktop resolves to, from INSIDE the same dedicated thread and IMMEDIATELY
            // before SendInput -- see InputDesktopNudge.Run below for why this, not a caller-side
            // state re-check, is what actually closes the race a secure saver activating between
            // the PowerShell tick's own probe read and this thread's SendInput.
            [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
            public static extern bool GetUserObjectInformation(IntPtr hObj, int nIndex, StringBuilder pvInfo, int nLength, out int lpnLengthNeeded);
            // VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            // r2 (sol blocker 1): the console-lock read InputDesktopNudge.Run makes from inside its
            // dedicated thread, immediately before SendInput -- the same WTSSessionInfoEx read
            // Get-AttrCudaSessionLocked makes, on the thread that injects.
            [DllImport("kernel32.dll")]
            public static extern uint GetCurrentProcessId();

            [DllImport("kernel32.dll", SetLastError = true)]
            public static extern bool ProcessIdToSessionId(uint dwProcessId, out uint pSessionId);

            [DllImport("wtsapi32.dll", SetLastError = true)]
            public static extern bool WTSQuerySessionInformationW(IntPtr hServer, uint sessionId, int wtsInfoClass, out IntPtr ppBuffer, out uint pBytesReturned);

            [DllImport("wtsapi32.dll")]
            public static extern void WTSFreeMemory(IntPtr pMemory);

            // true = locked, false = unlocked, null = could not be read. Same WTSINFOEXW layout and
            // checks as Get-AttrCudaSessionLocked: Level 1 at 0, SessionId at 8, SessionFlags at 16
            // (0 = WTS_SESSIONSTATE_LOCK, 1 = WTS_SESSIONSTATE_UNLOCK, anything else unknown).
            public static bool? ReadSessionLocked()
            {
                try
                {
                    uint sessionId;
                    if (!ProcessIdToSessionId(GetCurrentProcessId(), out sessionId)) { return null; }
                    IntPtr buffer;
                    uint bytes;
                    // WTS_CURRENT_SERVER_HANDLE = 0; WTSSessionInfoEx = 25.
                    if (!WTSQuerySessionInformationW(IntPtr.Zero, sessionId, 25, out buffer, out bytes)) { return null; }
                    if (buffer == IntPtr.Zero) { return null; }
                    try
                    {
                        if (bytes < 20) { return null; }
                        int level = Marshal.ReadInt32(buffer, 0);
                        int reportedSessionId = Marshal.ReadInt32(buffer, 8);
                        int sessionFlags = Marshal.ReadInt32(buffer, 16);
                        if (level != 1 || (long)reportedSessionId != (long)sessionId) { return null; }
                        if (sessionFlags == 0) { return true; }
                        if (sessionFlags == 1) { return false; }
                        return null;
                    }
                    finally
                    {
                        WTSFreeMemory(buffer);
                    }
                }
                catch
                {
                    return null;
                }
            }
            // VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }

        // CUDA-PERF-DISPLAY-WAKE-2 round 1c. Invoking a PowerShell scriptblock on a raw
        // System.Threading.Thread throws PSInvalidOperationException ("There is no Runspace
        // available to run scripts in this thread") the instant the delegate is invoked -- a new
        // .NET thread does not inherit [Runspace]::DefaultRunspace, which the PowerShell engine
        // needs before it can execute even the scriptblock's first statement, so there is no way
        // to set it from PowerShell code running ON that thread (chicken-and-egg). The dedicated
        // thread this needs is therefore built entirely in C#, which has no such requirement --
        // it is plain P/Invoke and CLR threading, nothing PowerShell-specific runs on it at all.
        public class InputDesktopNudgeResult
        {
            public string OpenInputDesktopError;
            public string SetThreadDesktopError;
            public string SendInputError;
            // CUDA-PERF-DISPLAY-WAKE-3 round 1 (sol hardening): CloseDesktop's own Boolean
            // result, never silently discarded -- $null on success, an error string (with
            // GetLastWin32Error) otherwise.
            public string CloseDesktopError;
            public bool ThreadJoined;
            // CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol BLOCKER fix): the desktop name this thread
            // actually read back right before SendInput (or null when unreadable), and whether it
            // was refused on that basis -- see InputDesktopNudge.Run's own new comment block for
            // what this closes.
            public string DesktopName;
            public bool DesktopNameRefused;
            // CUDA-PERF-DISPLAY-WAKE-4 round 1c (sonnet hardening): the running/secure state this
            // thread read back, from INSIDE itself, immediately before SendInput -- see
            // InputDesktopNudge.Run's own new comment block for why the desktop-name gate alone
            // (above) cannot substitute for this: a secure ("on resume, display logon screen")
            // screen saver can run on the SAME "Screen-saver" desktop name a non-secure one does,
            // so only this state re-read actually separates them. $null when that particular probe
            // itself could not be read (SystemParametersInfo returning false); SecureRefused is
            // $true when this gate is the reason SendInput was never attempted.
            public bool? ScreensaverRunningAtInject;
            public bool? ScreensaverSecureAtInject;
            public bool SecureRefused;
            // VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            // r2 (sol blocker 1): the console-lock state this thread read immediately before
            // SendInput (null when unreadable), and whether that read refused the injection.
            public bool? SessionLockedAtInject;
            public bool SessionLockRefused;
            // VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }

        public static class InputDesktopNudge
        {
            public static InputDesktopNudgeResult Run(int joinTimeoutMilliseconds)
            {
                InputDesktopNudgeResult result = new InputDesktopNudgeResult();
                System.Threading.Thread thread = new System.Threading.Thread(delegate ()
                {
                    // CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable note): a minimal DESKTOP_* mask
                    // instead of GENERIC_ALL -- READOBJECTS/WRITEOBJECTS cover the SendInput
                    // nudge itself, and SWITCHDESKTOP is what SetThreadDesktop's own contract
                    // requires of the handle it is given. No fallback to a broader mask: a
                    // denial here is recorded exactly like any other OpenInputDesktop failure
                    // (fails closed, never thrown).
                    // CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence, two legs on integration
                    // sha 56e14896): the mask above let OpenInputDesktop/SetThreadDesktop succeed
                    // (both errors $null) but every SendInput on the reassigned thread still
                    // returned 0 with lastError=5 (ERROR_ACCESS_DENIED). Microsoft documents
                    // SendInput as requiring DESKTOP_JOURNALPLAYBACK on the desktop it injects
                    // into -- READOBJECTS/WRITEOBJECTS/SWITCHDESKTOP alone do not cover the inject
                    // itself, only opening the handle and reassigning the thread to it. Added here,
                    // nothing broader: a continued denial with this bit present would point at UIPI
                    // (a higher-integrity input desktop/process) rather than a missing access bit,
                    // and still fails closed, recorded exactly as before.
                    uint DESKTOP_READOBJECTS = 0x0001;
                    uint DESKTOP_WRITEOBJECTS = 0x0080;
                    uint DESKTOP_SWITCHDESKTOP = 0x0100;
                    uint DESKTOP_JOURNALPLAYBACK = 0x0020;
                    uint desiredAccess = DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP | DESKTOP_JOURNALPLAYBACK;
                    IntPtr hDesktop = NativeMethods.OpenInputDesktop(0, false, desiredAccess);
                    if (hDesktop == IntPtr.Zero)
                    {
                        result.OpenInputDesktopError = "OpenInputDesktop failed (lastError=" + Marshal.GetLastWin32Error() + ")";
                        return;
                    }
                    // CUDA-PERF-DISPLAY-WAKE-3 round 1 (sol hardening): the thread's own desktop
                    // BEFORE SetThreadDesktop reassigns it -- read while it is still cheap/certain
                    // to succeed, so there is something to switch back to afterwards. A failure
                    // here ($null-equivalent IntPtr.Zero) is not fatal to the nudge itself: the
                    // switch-back below is then simply skipped, and CloseDesktop is attempted
                    // anyway (recorded either way, never thrown).
                    IntPtr originalDesktop = NativeMethods.GetThreadDesktop(NativeMethods.GetCurrentThreadId());
                    try
                    {
                        if (!NativeMethods.SetThreadDesktop(hDesktop))
                        {
                            result.SetThreadDesktopError = "SetThreadDesktop failed (lastError=" + Marshal.GetLastWin32Error() + ")";
                            return;
                        }
                        // CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol BLOCKER fix): a secure saver can
                        // become active in the gap between the PowerShell tick's own
                        // Get-AttrCudaScreensaverRunning/-Secure probe read and this dedicated
                        // thread's SendInput call below -- Windows has no atomic check-and-inject,
                        // and that gap is unavoidable from the calling (PowerShell tick) side no
                        // matter how tightly the probes are re-read there. What CAN be tightened is
                        // narrowing the window to THIS thread itself: read the name of the desktop
                        // hDesktop (the one OpenInputDesktop just resolved as "currently receiving
                        // input") actually is, immediately before SendInput, and inject only when it
                        // is one of the two names a NON-secure desktop is documented/observed to use.
                        // Windows names the plain interactive desktop "Default"; a RUNNING screen
                        // saver -- secure or not -- switches the input desktop to one named
                        // "Screen-saver" (CUDA-PERF-DISPLAY-WAKE-4 round 1c, read-only hub probe on
                        // Ultra-Magnus: screensaverRunning=True, inputDesktopName=Screen-saver, for a
                        // NON-secure scrnsave.scr). Round 1b's original comment here claimed every
                        // logged leg resolved to "Default" and treated that as the only safe name --
                        // that was only ever true because none of those legs had a screen saver
                        // actually RUNNING at nudge time; it was never evidence that a running
                        // non-secure saver also uses "Default", and narrowing the allow-list to
                        // "Default" alone made every running-saver dismiss (the golden path this
                        // helper exists for) fail closed as a false secure-desktop refusal. "Default"
                        // and "Screen-saver" are therefore both treated as safe here; "Winlogon" (the
                        // desktop Windows documents the secure/password-prompt screen saver, UAC and
                        // the lock screen itself as running on) and any other or unreadable name are
                        // refused with NO SendInput of any kind. Crucially, the desktop NAME alone
                        // does not separate a secure saver from a non-secure one -- a secure
                        // ("on resume, display logon screen") saver can likewise run on this SAME
                        // "Screen-saver" desktop -- so this name check is necessary but not
                        // sufficient; the SPI_GETSCREENSAVESECURE/-RUNNING re-read immediately below,
                        // taken from inside this same thread, is what actually excludes it. The
                        // remaining window -- between this name read and SendInput, both on this same
                        // thread with nothing else able to run in between -- is closed by Windows
                        // itself, not by this code: SendInput from a non-SYSTEM process is documented
                        // (and this file's own round-2 evidence above independently observed, as
                        // ERROR_ACCESS_DENIED) to be rejected by any desktop the calling thread is not
                        // actually attached to, and a secure desktop switch reassigns the ACTIVE input
                        // desktop away from hDesktop, not this thread's own SetThreadDesktop
                        // attachment to it -- so even a secure-desktop switch landing in that last
                        // instant leaves SendInput injecting into the (by then background, non-secure)
                        // desktop this thread is still attached to, never onto the new secure one,
                        // which is exactly the security boundary this job must never cross.
                        int desktopNameLengthNeeded;
                        StringBuilder desktopNameBuilder = new StringBuilder(256);
                        bool desktopNameRead = NativeMethods.GetUserObjectInformation(
                            hDesktop, 2 /* UOI_NAME */, desktopNameBuilder, desktopNameBuilder.Capacity, out desktopNameLengthNeeded);
                        string desktopName = desktopNameRead ? desktopNameBuilder.ToString() : null;
                        result.DesktopName = desktopName;
                        bool desktopNameSafe = desktopNameRead && (
                            string.Equals(desktopName, "Default", StringComparison.Ordinal) ||
                            string.Equals(desktopName, "Screen-saver", StringComparison.Ordinal));
                        if (!desktopNameSafe)
                        {
                            result.DesktopNameRefused = true;
                            result.SendInputError = desktopNameRead
                                ? "ATTRCUDA_SECURE_DESKTOP_REFUSED reason=secure_desktop desktop name '" + desktopName + "' is not the expected non-secure desktop; no SendInput attempted"
                                : "ATTRCUDA_SECURE_DESKTOP_REFUSED reason=desktop_name_unknown desktop name could not be read (lastError=" + Marshal.GetLastWin32Error() + "); no SendInput attempted";
                            return;
                        }
                        // CUDA-PERF-DISPLAY-WAKE-4 round 1c (sonnet hardening): the desktop-name gate
                        // above admits "Screen-saver" -- but a SECURE screen saver can run on that
                        // very same desktop name, so the name alone never proved this is safe to
                        // inject into. Re-read SPI_GETSCREENSAVERRUNNING and SPI_GETSCREENSAVESECURE
                        // from INSIDE this dedicated thread, immediately before SendInput -- the same
                        // "narrow the window to this thread itself" argument the name read above
                        // makes, applied to the state a name check cannot see. Fails closed exactly
                        // like Start-AttrCudaDisplayWake's own claim-time gate
                        // (.screensaverSecureOwnerOnly): only a CONFIRMED running=false, or
                        // (running=true AND secure=false), may reach SendInput; an unreadable running
                        // or secure state is treated the same as running+secure, never as "not
                        // running". The residual window is between THIS read and SendInput, both
                        // still on this same thread with nothing else able to run in between -- not
                        // eliminated by shrinking it further, same as the desktop-name comment above.
                        bool runningNow = false;
                        bool runningRead = NativeMethods.SystemParametersInfo(0x0072 /* SPI_GETSCREENSAVERRUNNING */, 0, ref runningNow, 0);
                        bool secureNow = false;
                        bool secureRead = NativeMethods.SystemParametersInfo(0x0076 /* SPI_GETSCREENSAVESECURE */, 0, ref secureNow, 0);
                        result.ScreensaverRunningAtInject = runningRead ? (bool?)runningNow : null;
                        result.ScreensaverSecureAtInject = secureRead ? (bool?)secureNow : null;
                        string secureRefusalReason = null;
                        if (!runningRead)
                        {
                            secureRefusalReason = "state_unknown";
                        }
                        else if (runningNow)
                        {
                            secureRefusalReason = !secureRead ? "state_unknown" : (secureNow ? "secure_screensaver" : null);
                        }
                        if (secureRefusalReason != null)
                        {
                            result.SecureRefused = true;
                            result.SendInputError = "ATTRCUDA_SECURE_DESKTOP_REFUSED reason=" + secureRefusalReason +
                                " screen saver running/secure state at inject time forbids SendInput; no SendInput attempted";
                            return;
                        }
                        // CUDA-PERF-DISPLAY-WAKE-3 round 3 (live UM evidence, defect class fix):
                        // three UM legs all recorded OpenInputDesktop/SetThreadDesktop/SendInput
                        // succeeding (every error field null, thread joined) yet
                        // screensaverRunningAfter stayed true -- the net-zero 1-pixel move this
                        // replaces is exactly the kind of sub-threshold WM_MOUSEMOVE the classic
                        // scrnsave window procedure is documented to ignore. Two independent,
                        // side-effect-free inputs instead, still net-zero on the pointer so the
                        // owner's cursor never visibly moves: an 8-pixel move (round-tripped back
                        // to 0,0) well above that ignore threshold, plus a single down+up tap of
                        // VK_F15 -- one of the F13-F24 block Windows binds to nothing by default
                        // (no Start Menu, no window, no app shortcut) and, unlike Shift, cannot
                        // ever trigger the Sticky Keys prompt (that accessibility feature watches
                        // for Shift specifically, pressed five times; this is a different key,
                        // pressed once). One tap only, deliberately -- never repeated within this
                        // single call, so a fast keep-alive interval cannot accumulate toward any
                        // key-specific OS gesture threshold either.
                        uint INPUT_MOUSE = 0;
                        uint INPUT_KEYBOARD = 1;
                        uint MOUSEEVENTF_MOVE = 0x0001;
                        uint KEYEVENTF_KEYUP = 0x0002;
                        ushort VK_F15 = 0x7E;
                        INPUT[] nudge = new INPUT[] {
                            new INPUT { type = (int)INPUT_MOUSE, mi = new MOUSEINPUT { dx = 8, dy = 0, mouseData = 0, dwFlags = MOUSEEVENTF_MOVE, time = 0, dwExtraInfo = IntPtr.Zero } },
                            new INPUT { type = (int)INPUT_MOUSE, mi = new MOUSEINPUT { dx = -8, dy = 0, mouseData = 0, dwFlags = MOUSEEVENTF_MOVE, time = 0, dwExtraInfo = IntPtr.Zero } },
                            new INPUT { type = (int)INPUT_KEYBOARD, ki = new KEYBDINPUT { wVk = VK_F15, wScan = 0, dwFlags = 0, time = 0, dwExtraInfo = IntPtr.Zero } },
                            new INPUT { type = (int)INPUT_KEYBOARD, ki = new KEYBDINPUT { wVk = VK_F15, wScan = 0, dwFlags = KEYEVENTF_KEYUP, time = 0, dwExtraInfo = IntPtr.Zero } }
                        };
                        // VENUE-SESSION-LOCKED-REFUSAL-1 >>>
                        // r2 (sol blocker 1): the console lock is read LAST, on this thread, as the
                        // statement before SendInput. A lock that arrives after the caller's own read
                        // (the PowerShell tick or claim) is caught here; a locked console's input
                        // desktop still reads "Default", so the name gate above cannot see it. Only a
                        // CONFIRMED unlocked session reaches SendInput. The window left is this one
                        // WTS read to the SendInput call on the same thread; Windows has no atomic
                        // check-and-inject, so it cannot be closed further from user mode.
                        bool? sessionLockedNow = NativeMethods.ReadSessionLocked();
                        result.SessionLockedAtInject = sessionLockedNow;
                        if (sessionLockedNow != false)
                        {
                            result.SessionLockRefused = true;
                            result.SendInputError = "ATTRCUDA_SESSION_LOCKED_OWNER_ONLY reason=" +
                                (sessionLockedNow == null ? "session_lock_unknown" : "session_locked") +
                                " console session lock state at inject time forbids SendInput; no SendInput attempted";
                            return;
                        }
                        // VENUE-SESSION-LOCKED-REFUSAL-1 <<<
                        uint sent = NativeMethods.SendInput((uint)nudge.Length, nudge, Marshal.SizeOf(typeof(INPUT)));
                        if (sent != nudge.Length)
                        {
                            result.SendInputError = "SendInput sent " + sent + " of " + nudge.Length + " events (lastError=" + Marshal.GetLastWin32Error() + ")";
                        }
                    }
                    finally
                    {
                        // Switch the thread back to its OWN original desktop first (when that
                        // read above actually succeeded) so it is no longer "using" hDesktop --
                        // CloseDesktop is documented to fail while any thread still is. The
                        // switch-back's own result is not separately recorded: CloseDesktop's
                        // result below is the one outcome that actually matters (whether the
                        // handle was released), and a switch-back failure would show up there
                        // too, since CloseDesktop would then still see this thread attached.
                        if (originalDesktop != IntPtr.Zero)
                        {
                            NativeMethods.SetThreadDesktop(originalDesktop);
                        }
                        if (!NativeMethods.CloseDesktop(hDesktop))
                        {
                            result.CloseDesktopError = "CloseDesktop failed (lastError=" + Marshal.GetLastWin32Error() + ")";
                        }
                    }
                });
                thread.IsBackground = true;
                thread.Start();
                result.ThreadJoined = thread.Join(joinTimeoutMilliseconds);
                return result;
            }
        }
    }
'@ -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Get-AttrCudaScreensaverRunning {
    <#
    .SYNOPSIS
    SPI_GETSCREENSAVERRUNNING via SystemParametersInfo: $true/$false, or $null if the native type
    could not load or the call itself failed -- never throws.
    #>
    [CmdletBinding()]
    param()
    if (-not (Register-AttrCudaDisplayWakeNativeMethods)) { return $null }
    try {
        $running = $false
        # SPI_GETSCREENSAVERRUNNING = 0x0072.
        $ok = [MLVAppAttrCudaDisplayWake.NativeMethods]::SystemParametersInfo(0x0072, 0, [ref]$running, 0)
        if (-not $ok) { return $null }
        return [bool]$running
    } catch {
        return $null
    }
}

function Get-AttrCudaScreensaverTimeoutSeconds {
    <#
    .SYNOPSIS
    SPI_GETSCREENSAVETIMEOUT via SystemParametersInfo: the configured screen-saver timeout in
    seconds, or $null if the native type could not load or the call itself failed -- never throws.
    Read-only, like Get-AttrCudaScreensaverRunning: this module never calls SPI_SET* and never
    changes a screen-saver or power setting (CUDA-PERF-DISPLAY-WAKE-2).
    #>
    [CmdletBinding()]
    param()
    if (-not (Register-AttrCudaDisplayWakeNativeMethods)) { return $null }
    try {
        $timeoutSeconds = 0
        # SPI_GETSCREENSAVETIMEOUT = 0x000E.
        $ok = [MLVAppAttrCudaDisplayWake.NativeMethods]::SystemParametersInfoInt(0x000E, 0, [ref]$timeoutSeconds, 0)
        if (-not $ok) { return $null }
        return [int]$timeoutSeconds
    } catch {
        return $null
    }
}

function Get-AttrCudaScreensaverActive {
    <#
    .SYNOPSIS
    SPI_GETSCREENSAVEACTIVE via SystemParametersInfo: whether the screen saver is enabled at all --
    distinct from Get-AttrCudaScreensaverRunning, which reports whether it is CURRENTLY running.
    $null if the native type could not load or the call itself failed -- never throws. Read-only,
    like its siblings above.
    #>
    [CmdletBinding()]
    param()
    if (-not (Register-AttrCudaDisplayWakeNativeMethods)) { return $null }
    try {
        $active = $false
        # SPI_GETSCREENSAVEACTIVE = 0x0010.
        $ok = [MLVAppAttrCudaDisplayWake.NativeMethods]::SystemParametersInfo(0x0010, 0, [ref]$active, 0)
        if (-not $ok) { return $null }
        return [bool]$active
    } catch {
        return $null
    }
}

function Get-AttrCudaScreensaverSecure {
    <#
    .SYNOPSIS
    SPI_GETSCREENSAVESECURE via SystemParametersInfo: whether resuming from the screen saver
    requires the logon password. $null if the native type could not load or the call itself
    failed -- never throws. Read-only, like its siblings above.
    .DESCRIPTION
    CUDA-PERF-DISPLAY-WAKE-2 round 1c. This is the ONE check that gates every screen-saver-dismiss
    attempt in Start-AttrCudaDisplayWake below: a secure screen saver's password prompt is a
    security boundary this job never attempts to cross -- ending it is an owner action, never an
    automated one.
    #>
    [CmdletBinding()]
    param()
    if (-not (Register-AttrCudaDisplayWakeNativeMethods)) { return $null }
    try {
        $secure = $false
        # SPI_GETSCREENSAVESECURE = 0x0076.
        $ok = [MLVAppAttrCudaDisplayWake.NativeMethods]::SystemParametersInfo(0x0076, 0, [ref]$secure, 0)
        if (-not $ok) { return $null }
        return [bool]$secure
    } catch {
        return $null
    }
}

function Invoke-AttrCudaBoundedProbe {
    <#
    .SYNOPSIS
    Runs a zero-argument probe function -- by name, resolved via Get-Command IN THE CALLER'S OWN
    scope/runspace, the same late-binding-by-name mechanism Start-AttrCudaDisplayWakeKeepAlive's own
    SessionStateFunctionEntry setup already relies on, so a test's override of e.g.
    Get-AttrCudaScreensaverRunning is honored here too -- on a dedicated PowerShell instance/Runspace
    of its own, bounded by -TimeoutMilliseconds. Non-throwing, like every other function in this
    file.
    .DESCRIPTION
    CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol hardening). A plain PowerShell function call has no
    built-in per-call timeout of its own -- the PowerShell-native equivalent of the bounded dedicated
    THREAD [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run already uses for SendInput (a fresh
    thread, joined with a timeout) is a fresh Runspace here, since a scriptblock cannot be run on a
    raw .NET thread without one (see InputDesktopNudge's own header for exactly why). Without this,
    a probe that never returns (a test stub that blocks, or -- live -- a wedged SystemParametersInfo
    call) blocks the calling tick indefinitely, and in turn blocks
    Stop-AttrCudaDisplayWakeKeepAlive's own EndInvoke wait the same way, since the loop's pipeline
    thread never reaches its next -StopEvent.Wait() check.
    Returns the probe's own result when it completes within -TimeoutMilliseconds. Returns $null --
    the SAME "could not determine" value every probe in this file already returns on any other kind
    of failure, which the keep-alive loop's existing fail-closed gate already treats as owner-only/
    state_unknown -- when it does not. A timed-out probe's PowerShell/Runspace pair is deliberately
    never Stop()/Dispose()d: there is no safe way in .NET to force-terminate a thread stuck inside a
    P/Invoke call, and attempting Stop()/Dispose() on a still-running pipeline can itself block,
    which would defeat the entire point of this wrapper -- so it is left running and abandoned, and
    THIS call still always returns within -TimeoutMilliseconds regardless.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$FunctionName,
        [int]$TimeoutMilliseconds = 3000
    )
    try {
        $probeCommand = Get-Command -Name $FunctionName -CommandType Function -ErrorAction Stop
        $probeIss = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
        $probeIss.Commands.Add(
            [System.Management.Automation.Runspaces.SessionStateFunctionEntry]::new(
                $FunctionName, $probeCommand.Definition))
        # CUDA-PERF-DISPLAY-WAKE-4 round 1d (sol PRE-REVIEW #2 BLOCKER): every probe this function is
        # actually called with (Get-AttrCudaScreensaverRunning/-Secure) calls
        # Register-AttrCudaDisplayWakeNativeMethods internally. This nested Runspace starts from
        # CreateDefault() and, unlike the outer keep-alive Runspace's own InitialSessionState above,
        # inherits none of the caller's functions -- so that call used to fail with
        # CommandNotFoundException and this probe always returned $null. Added the same late-binding-
        # by-name way as the outer Runspace does, resolved fresh in the caller's own scope so a test
        # override is honored here too; skipped when $FunctionName already IS this dependency (so
        # Commands.Add is never asked to add the same key twice), and left unresolved dependencies to
        # fall through to the catch below -- the same fail-closed $null every other failure here
        # already returns, never a thrown error out of this function.
        foreach ($probeDependencyName in @('Register-AttrCudaDisplayWakeNativeMethods')) {
            if ($probeDependencyName -eq $FunctionName) { continue }
            $probeDependencyCommand = Get-Command -Name $probeDependencyName -CommandType Function -ErrorAction Stop
            $probeIss.Commands.Add(
                [System.Management.Automation.Runspaces.SessionStateFunctionEntry]::new(
                    $probeDependencyName, $probeDependencyCommand.Definition))
        }
        $probeRunspace = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($probeIss)
        $probeRunspace.Open()
        $probeShell = [System.Management.Automation.PowerShell]::Create()
        $probeShell.Runspace = $probeRunspace
        [void]$probeShell.AddCommand($FunctionName)
        $probeAsync = $probeShell.BeginInvoke()
        if (-not $probeAsync.AsyncWaitHandle.WaitOne([int]$TimeoutMilliseconds)) {
            # Bounded timeout: deliberately does not Stop()/Dispose()/Close() a pipeline that is
            # still running -- see .DESCRIPTION. Leaked on purpose, in this one path only.
            return $null
        }
        $probeResult = $null
        try { $probeResult = $probeShell.EndInvoke($probeAsync) | Select-Object -First 1 } catch { $probeResult = $null }
        try { $probeShell.Dispose() } catch { }
        try { $probeRunspace.Close() } catch { }
        try { $probeRunspace.Dispose() } catch { }
        return $probeResult
    } catch {
        return $null
    }
}

function Wait-AttrCudaScreensaverDismissed {
    <#
    .SYNOPSIS
    Poll Get-AttrCudaScreensaverRunning for up to -TimeoutMilliseconds, stopping the instant a poll
    reads a CONFIRMED $false. Non-throwing, like every other function in this file.
    .DESCRIPTION
    CUDA-PERF-DISPLAY-WAKE-3 round 3 (live UM evidence, defect class fix). Every prior round's
    after-dismiss read called Get-AttrCudaScreensaverRunning exactly once, IMMEDIATELY after the
    nudge returned -- with no allowance for the screen saver process to actually receive the
    injected input and exit. Three UM legs recorded every nudge step succeeding (OpenInputDesktop,
    SetThreadDesktop, SendInput and the thread join all clean) yet screensaverRunningAfter still
    read $true. This gives the screen saver a bounded window to react before the after-state is
    read as a failure, without ever blocking indefinitely: elapsed time is bounded by
    -TimeoutMilliseconds regardless of how many polls that takes (a slow poll or a slow Start-Sleep
    can only shrink the number of polls left, never extend the wall-clock budget), and the LAST
    reading is returned exactly as read -- never coerced into a guess when the window simply runs
    out.
    .PARAMETER TimeoutMilliseconds
    Total wall-clock budget for polling, default 5000 (contract: "up to 5 s").
    .PARAMETER PollIntervalMilliseconds
    Sleep between polls, default 250 (contract: "at 250 ms").
    .OUTPUTS
    An ordered hashtable: .running (the final $true/$false/$null reading -- $null only when the
    LAST poll's own probe call itself failed), .pollCount (polls actually performed, always >= 1),
    .elapsedMilliseconds (wall-clock actually spent polling, from a Stopwatch -- never assumed from
    -PollIntervalMilliseconds * .pollCount, since the probe call and the loop overhead both take
    their own time too).
    #>
    [CmdletBinding()]
    param(
        [int]$TimeoutMilliseconds = 5000,
        [int]$PollIntervalMilliseconds = 250
    )

    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $pollCount = 0
    $running = $null
    while ($true) {
        $pollCount++
        $running = Get-AttrCudaScreensaverRunning
        if ($running -eq $false) { break }
        if ($stopwatch.ElapsedMilliseconds -ge $TimeoutMilliseconds) { break }
        Start-Sleep -Milliseconds $PollIntervalMilliseconds
    }
    [ordered]@{
        running = $running
        pollCount = $pollCount
        elapsedMilliseconds = $stopwatch.ElapsedMilliseconds
    }
}

function Invoke-AttrCudaInputDesktopNudge {
    <#
    .SYNOPSIS
    Sends a SendInput nudge from a brand-new dedicated thread that first calls OpenInputDesktop +
    SetThreadDesktop -- the supported way to inject input into whichever desktop is CURRENTLY
    receiving input, when that is not the desktop this job's own thread was created on. CUDA-PERF-
    DISPLAY-WAKE-3 round 3: a stronger nudge than Start-AttrCudaDisplayWake's own plain (not-
    dismissing) path -- see the C# InputDesktopNudge.Run body for what it sends and why -- because
    this path is the one actually DISMISSING an already-engaged screen saver, never just resetting
    an idle timer before one has engaged.
    .DESCRIPTION
    CUDA-PERF-DISPLAY-WAKE-2 round 1c. A live leg on Bachelor (round 1b) recorded
    screensaverRunningBefore=true and a plain SendInput failing with lastError=5
    (ERROR_ACCESS_DENIED): Microsoft documents SendInput as injecting only into the CALLING
    THREAD's own desktop, and a non-secure screen saver that has already engaged switches the
    input desktop out from under this job's thread. OpenInputDesktop resolves the desktop that is
    actually receiving input right now, and SetThreadDesktop reassigns a thread to it -- but
    Microsoft also documents that "you cannot set the desktop for a thread if the thread has any
    windows or hooks on the current desktop", so this MUST run on a freshly created
    System.Threading.Thread that has never created a window or hook, never this job's own thread
    (which already touched user32 via Register-AttrCudaDisplayWakeNativeMethods/SendInput above).
    Never called when the screen saver is SECURE (SPI_GETSCREENSAVESECURE) -- that gate lives in
    Start-AttrCudaDisplayWake, one level up, not here: ending a password-protected screen saver is
    an owner action, never something this job attempts. CUDA-PERF-DISPLAY-WAKE-4 round 1c
    (sonnet hardening): that claim-time gate cannot close the gap between its own read and this
    helper's SendInput, so the C# InputDesktopNudge.Run body re-reads SPI_GETSCREENSAVERRUNNING/
    -SECURE a second time, from inside its dedicated thread, immediately before SendInput -- this
    is defense in depth, not a replacement for the claim-time/per-tick gates above it.
    Non-throwing by construction, like every other function in this file: every failure is
    recorded in the returned evidence, never allowed to propagate to the caller. The dedicated
    thread itself is built entirely in C# (Register-AttrCudaDisplayWakeNativeMethods's
    InputDesktopNudge helper), never as a PowerShell scriptblock run on a raw thread: a new
    System.Threading.Thread does not inherit [Runspace]::DefaultRunspace, so invoking PowerShell
    code on it throws PSInvalidOperationException before a single statement runs -- there is no
    way to set that from PowerShell code running ON the new thread, since setting it is itself
    PowerShell code needing the very runspace that is missing. Plain P/Invoke and CLR threading
    have no such requirement, so the thread body is C# instead.
    .OUTPUTS
    An ordered hashtable: .attempted, .openInputDesktopError/.setThreadDesktopError/
    .sendInputError/.closeDesktopError (each $null on success -- CUDA-PERF-DISPLAY-WAKE-3 round 1
    adds .closeDesktopError, previously discarded), and .threadJoined (whether the dedicated
    thread finished within its join timeout -- $false is itself evidence, not a throw).
    #>
    [CmdletBinding()]
    param(
        [int]$JoinTimeoutMilliseconds = 5000
    )

    if (-not (Register-AttrCudaDisplayWakeNativeMethods)) {
        return [ordered]@{
            attempted = $false
            openInputDesktopError = 'ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE native P/Invoke type could not be loaded'
            setThreadDesktopError = $null
            sendInputError = $null
            closeDesktopError = $null
            threadJoined = $false
            desktopName = $null
            desktopNameRefused = $false
            screensaverRunningAtInject = $null
            screensaverSecureAtInject = $null
            secureRefused = $false
            # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            sessionLockedAtInject = $null
            sessionLockRefused = $false
            # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }
    }

    try {
        $result = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run($JoinTimeoutMilliseconds)
        [ordered]@{
            attempted = $true
            openInputDesktopError = $result.OpenInputDesktopError
            setThreadDesktopError = $result.SetThreadDesktopError
            sendInputError = $result.SendInputError
            closeDesktopError = $result.CloseDesktopError
            threadJoined = [bool]$result.ThreadJoined
            # CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol BLOCKER fix): the desktop name the dedicated
            # thread actually read back right before SendInput, and whether it was refused on that
            # basis -- see InputDesktopNudge.Run's own comment for why this in-thread check, not a
            # caller-side re-check, is what closes the read-then-inject race.
            desktopName = $result.DesktopName
            desktopNameRefused = [bool]$result.DesktopNameRefused
            # CUDA-PERF-DISPLAY-WAKE-4 round 1c (sonnet hardening): the running/secure state the
            # same dedicated thread read back right before SendInput, and whether THAT is what
            # refused the injection -- see InputDesktopNudge.Run's own comment for why the desktop
            # name above cannot substitute for this.
            screensaverRunningAtInject = $result.ScreensaverRunningAtInject
            screensaverSecureAtInject = $result.ScreensaverSecureAtInject
            secureRefused = [bool]$result.SecureRefused
            # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            # r2 (sol blocker 1): the lock state the dedicated thread read right before SendInput,
            # and whether it refused on it. Read defensively: an older result shape has no such field.
            sessionLockedAtInject = $(try { $result.SessionLockedAtInject } catch { $null })
            sessionLockRefused = $(try { [bool]$result.SessionLockRefused } catch { $false })
            # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }
    } catch {
        [ordered]@{
            attempted = $true
            openInputDesktopError = $_.Exception.Message
            setThreadDesktopError = $null
            sendInputError = $null
            closeDesktopError = $null
            threadJoined = $false
            desktopName = $null
            desktopNameRefused = $false
            screensaverRunningAtInject = $null
            screensaverSecureAtInject = $null
            secureRefused = $false
            # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            sessionLockedAtInject = $null
            sessionLockRefused = $false
            # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }
    }
}

function Get-AttrCudaSessionLocked {
    <#
    .SYNOPSIS
    Whether the CALLING process's Windows session is locked: $true (locked), $false (unlocked), or
    $null when that could not be read. Read-only and non-throwing, like every other probe here.
    .DESCRIPTION
    VENUE-SESSION-LOCKED-REFUSAL-1. A locked console is an owner-only boundary, and the desktop-name
    gate in InputDesktopNudge.Run cannot see it: while the lock curtain or Modern Standby is
    showing, the input desktop still reads "Default". Measured on Bachelor 2026-10-09 (probe
    vkad-probe-r1-20261009T110317Z, run lane-VENUE-KEEPALIVE-ACCESS-DENIED-1-r1-20261009T1045Z):
    the session stayed locked after a Windows Update reboot, the keep-alive's VK_F15 tap raised the
    sign-in screen, and OpenInputDesktop then failed with ERROR_ACCESS_DENIED for about 32 s.
    Reads this process's own session (ProcessIdToSessionId), then
    WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, sid, WTSSessionInfoEx = 25). The buffer is
    a WTSINFOEXW: DWORD Level at offset 0, then the WTSINFOEX_LEVEL1_W union, 8-aligned, so
    SessionId is at 8 and SessionFlags at 16. SessionFlags 0 = WTS_SESSIONSTATE_LOCK and
    1 = WTS_SESSIONSTATE_UNLOCK; anything else (WTS_SESSIONSTATE_UNKNOWN is 0xFFFFFFFF) is unknown.
    The layout is checked (Level == 1 and SessionId == the session asked for), and a mismatch is
    unknown, never unlocked. Measured: Bachelor locked reads level=1 flags=0; VIRTUAL-TEN unlocked
    reads level=1 flags=1. Callers treat $null exactly like $true (fail closed): only a CONFIRMED
    $false may ever reach SendInput.
    #>
    [CmdletBinding()]
    param()
    try {
        if (-not ("MLVAppAttrCudaSessionLock.NativeMethods" -as [type])) {
            # Indented so NO line of this embedded C# starts with an unindented '}' -- see
            # Register-AttrCudaDisplayWakeNativeMethods for why that matters to the extraction.
            Add-Type -TypeDefinition @'
    using System;
    using System.Runtime.InteropServices;

    namespace MLVAppAttrCudaSessionLock
    {
        public static class NativeMethods
        {
            [DllImport("kernel32.dll", SetLastError = true)]
            public static extern bool ProcessIdToSessionId(uint dwProcessId, out uint pSessionId);

            [DllImport("wtsapi32.dll", SetLastError = true)]
            public static extern bool WTSQuerySessionInformationW(IntPtr hServer, uint sessionId, int wtsInfoClass, out IntPtr ppBuffer, out uint pBytesReturned);

            [DllImport("wtsapi32.dll")]
            public static extern void WTSFreeMemory(IntPtr pMemory);
        }
    }
'@ -ErrorAction Stop
        }
        $sessionId = [uint32]0
        if (-not [MLVAppAttrCudaSessionLock.NativeMethods]::ProcessIdToSessionId([uint32]$PID, [ref]$sessionId)) { return $null }
        $buffer = [IntPtr]::Zero
        $bytes = [uint32]0
        # WTS_CURRENT_SERVER_HANDLE = 0; WTSSessionInfoEx = 25.
        if (-not [MLVAppAttrCudaSessionLock.NativeMethods]::WTSQuerySessionInformationW([IntPtr]::Zero, $sessionId, 25, [ref]$buffer, [ref]$bytes)) { return $null }
        if ($buffer -eq [IntPtr]::Zero) { return $null }
        try {
            if ($bytes -lt 20) { return $null }
            $level = [System.Runtime.InteropServices.Marshal]::ReadInt32($buffer, 0)
            $reportedSessionId = [System.Runtime.InteropServices.Marshal]::ReadInt32($buffer, 8)
            $sessionFlags = [System.Runtime.InteropServices.Marshal]::ReadInt32($buffer, 16)
            if ($level -ne 1 -or [int64]$reportedSessionId -ne [int64]$sessionId) { return $null }
            if ($sessionFlags -eq 0) { return $true }
            if ($sessionFlags -eq 1) { return $false }
            return $null
        } finally {
            [MLVAppAttrCudaSessionLock.NativeMethods]::WTSFreeMemory($buffer)
        }
    } catch {
        return $null
    }
}

function Start-AttrCudaDisplayWake {
    <#
    .SYNOPSIS
    Ends a blank screensaver and holds the display awake for the caller's leg, from the
    interactive session this job runs in. Bounded and non-throwing: a Win32 call failure is
    recorded in the returned evidence, never allowed to block or fail the leg.
    .DESCRIPTION
    Two independent mechanisms, both attempted regardless of whether the other succeeds:
      - a pointer/keyboard nudge -- the same kind of input a real user produces, which ends an
        active screensaver. CUDA-PERF-DISPLAY-WAKE-2 round 1c: if the screen saver is ALREADY
        RUNNING (SPI_GETSCREENSAVERRUNNING) when this is called, a plain SendInput from this
        thread is expected to fail with ERROR_ACCESS_DENIED (round 1b's live evidence on Bachelor)
        because the screen saver has taken over the input desktop -- so this dispatches to
        Invoke-AttrCudaInputDesktopNudge instead, which sends a nudge from a dedicated thread
        attached to whichever desktop is currently receiving input (CUDA-PERF-DISPLAY-WAKE-3
        round 3: a stronger nudge than the plain path below sends -- see
        Invoke-AttrCudaInputDesktopNudge's own header). Neither is ever attempted when the screen
        saver is SECURE (SPI_GETSCREENSAVESECURE) -- see .screensaverSecureOwnerOnly below;
      - SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED), held
        until the caller releases it via Stop-AttrCudaDisplayWake -- attempted regardless of the
        screen-saver branch above, since it never touches the screen saver's own desktop.
    Returns .attempted (always $true -- this function ran), .method, .screensaverRunningBefore /
    .screensaverRunningAfter (each $true/$false/$null -- $null only when that probe itself
    failed), .screensaverSecure (SPI_GETSCREENSAVESECURE, read-only, CUDA-PERF-DISPLAY-WAKE-2
    round 1c: $true/$false/$null -- $null when the probe itself failed), .screensaverSecureOwnerOnly
    ($true unless the screen saver is CONFIRMED not running (.screensaverRunningBefore -eq $false),
    or CONFIRMED running with a CONFIRMED not-secure state (-eq $true / -eq $false) -- CUDA-PERF-
    DISPLAY-WAKE-3 round 1 fail-closed fix: a secure-probe failure is never treated as "not secure";
    round 1b fail-closed fix: a running-probe failure ($null) is never treated as "not running"
    either, since either failure would arm a dismissal attempt against a screen saver this job
    cannot prove is safe to touch), .screensaverSecureReason ($null, 'secure', 'unknown', or
    'running_unknown' -- which condition set .screensaverSecureOwnerOnly, for evidence/diagnostics), the
    caller's signal to stop the leg with a typed SCREENSAVER_SECURE_OWNER_ONLY result rather than
    attempting anything: ending a password-protected (or unprovably-not-password-protected) screen
    saver is an owner action), .dismissFailed (CUDA-PERF-DISPLAY-WAKE-3 round 2 defect-class fix:
    $true when a dismiss WAS attempted -- .screensaverRunningBefore -eq $true and not
    .screensaverSecureOwnerOnly -- and .screensaverRunningAfter did not come back CONFIRMED $false;
    fails closed the same way .screensaverSecureOwnerOnly does, so a still-running or unknown
    (probe-failed) after-state is never read as a quiet success. Always $false when no dismiss was
    attempted at all. The caller's signal to stop the leg with a typed DISPLAY_WAKE_DISMISS_FAILED
    result rather than proceeding to a measurement it never actually protected from the screen
    saver), .dismissWait (CUDA-PERF-DISPLAY-WAKE-3 round 3, live UM evidence: three legs recorded
    every nudge step succeeding yet screensaverRunningAfter still true, because the after-state was
    read IMMEDIATELY after the nudge with no allowance for the screen saver process to receive and
    act on it. .screensaverRunningAfter now comes from Wait-AttrCudaScreensaverDismissed's bounded
    poll -- see its own header -- whenever a dismiss was actually attempted; $null when it was not,
    same gating as .inputDesktopNudge below. .dismissWait itself carries .pollCount and
    .elapsedMilliseconds, so a still-failing dismiss shows how long/how many polls this job actually
    waited before giving up), .inputDesktopNudge (the nested evidence from
    Invoke-AttrCudaInputDesktopNudge, or $null when that path was not taken),
    .sendInputError/.executionStateError (each $null on success -- .sendInputError reflects
    whichever nudge path actually ran), .screensaverTimeoutSeconds (SPI_GETSCREENSAVETIMEOUT,
    read-only) and .screensaverActive (SPI_GETSCREENSAVEACTIVE, read-only) -- CUDA-PERF-DISPLAY-
    WAKE-2, so a run that still ends DISPLAY_ASLEEP shows what timeout it was racing -- and .utc.
    Never calls an SPI_SET* action and never changes a screen-saver or power setting.
    VENUE-SESSION-LOCKED-REFUSAL-1 >>>
    VENUE-SESSION-LOCKED-REFUSAL-1: .sessionLocked (Get-AttrCudaSessionLocked, read FIRST, before
    anything else here: $true/$false/$null). Anything but a CONFIRMED $false is a locked console
    (unknown fails closed): no input of any kind is sent on either nudge path, .sendInputError
    carries ATTRCUDA_SESSION_LOCKED_OWNER_ONLY, .dismissFailed stays $false, and the caller stops
    the leg with a typed SESSION_LOCKED_OWNER_ONLY result. Signing in is an owner action. An
    unlocked session behaves exactly as before; the only difference is the added .sessionLocked.
    r2 (sol blocker 1): the first read is not the last. The lock is read again after every
    screen-saver probe, again as the statement before the plain SendInput, and by the dedicated
    nudge thread as its last step before its own SendInput. A lock found by any of them refuses the
    same way, and .sessionLocked carries the read that refused.
    VENUE-SESSION-LOCKED-REFUSAL-1 <<<
    #>
    [CmdletBinding()]
    param()

    # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
    # The lock read comes before every other read and every input. A missing or throwing probe
    # reads as unknown, which is refused like a lock.
    # r4 (CLAIM-TIME-LOCK-READ-BOUNDED-1): every claim-time lock read here goes through the same
    # bounded reader the keep-alive tick uses, so a read that never returns is unknown after
    # -TimeoutMilliseconds (refused, no input) instead of holding the claim forever.
    $sessionLocked = $null
    try { $sessionLocked = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked' } catch { $sessionLocked = $null }
    $sessionLockedOwnerOnly = ($sessionLocked -ne $false)
    # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
    $screensaverTimeoutSeconds = Get-AttrCudaScreensaverTimeoutSeconds
    $screensaverActive = Get-AttrCudaScreensaverActive
    $screensaverBefore = Get-AttrCudaScreensaverRunning
    $screensaverSecure = Get-AttrCudaScreensaverSecure
    # CUDA-PERF-DISPLAY-WAKE-3 round 1 (sol BLOCKER, fail-closed on unknown secure state) and
    # round 1b (sol BLOCKER, fail-closed on unknown RUNNING state): only a CONFIRMED $false reads
    # as "not secure", and only a CONFIRMED $false reads as "not running". A prior `-eq $true`
    # gate on screensaverBefore let a running-state probe failure fall through to the plain
    # SendInput dismissal branch below (elseif ($nativeAvailable) at ~line 2678) -- exactly the
    # branch meant for "confirmed not running" -- on a screen saver this job never actually
    # confirmed was safe to touch. Only a definite running=$false, or (running=$true AND
    # secure=$false), may ever reach a dismissal path; running=$null is treated the same as
    # running=$true+secure-unknown (owner-only, no input of any kind).
    $screensaverSecureOwnerOnly = if ($screensaverBefore -eq $false) {
        $false
    } elseif ($screensaverBefore -eq $true) {
        $screensaverSecure -ne $false
    } else {
        $true
    }
    $screensaverSecureReason = if (-not $screensaverSecureOwnerOnly) {
        $null
    } elseif ($null -eq $screensaverBefore) {
        'running_unknown'
    } elseif ($null -eq $screensaverSecure) {
        'unknown'
    } else {
        'secure'
    }
    $sendInputError = $null
    $executionStateError = $null
    $inputDesktopNudge = $null
    $nativeAvailable = Register-AttrCudaDisplayWakeNativeMethods

    # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
    # A locked (or unprovably-unlocked) console: no input of any kind. A key or mouse event raises
    # the sign-in screen, and the secure desktop then takes the input desktop. The unchanged
    # screen-saver branches below run only for a CONFIRMED unlocked session.
    # r2 (sol blocker 1): the first read above can be stale by now -- the console can lock while the
    # screen-saver probes run. So the lock is read again here, after every probe and immediately
    # before the branch that injects; the plain nudge reads it once more as the statement before
    # SendInput, and the dedicated thread reads it as its last step before its own SendInput.
    if (-not $sessionLockedOwnerOnly) {
        try { $sessionLocked = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked' } catch { $sessionLocked = $null }
        $sessionLockedOwnerOnly = ($sessionLocked -ne $false)
    }
    if (-not $sessionLockedOwnerOnly) {
    # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
    if ($screensaverSecureOwnerOnly) {
        # A password-protected (or unprovably-not-password-protected) screen saver is a security
        # boundary: no dismiss attempt of any kind is made, on either path below. The caller (the
        # attribution job) is expected to stop the leg on this flag before touching footage or the
        # smoke run.
        $sendInputError = if ($screensaverSecureReason -eq 'running_unknown') {
            'ATTRCUDA_SCREENSAVER_SECURE_OWNER_ONLY whether a screen saver is running could not be read, so secure dismissal cannot be ruled out; treated as running+secure -- no dismiss attempted -- ending it is an owner action'
        } elseif ($screensaverSecureReason -eq 'unknown') {
            'ATTRCUDA_SCREENSAVER_SECURE_OWNER_ONLY screen saver is running and whether it is secure (password on resume) could not be read; treated as secure -- no dismiss attempted -- ending it is an owner action'
        } else {
            'ATTRCUDA_SCREENSAVER_SECURE_OWNER_ONLY screen saver is running and secure (password on resume); no dismiss attempted -- ending it is an owner action'
        }
    } elseif ($nativeAvailable -and $screensaverBefore -eq $true) {
        $inputDesktopNudge = Invoke-AttrCudaInputDesktopNudge
        $sendInputError = @($inputDesktopNudge.openInputDesktopError, $inputDesktopNudge.setThreadDesktopError, $inputDesktopNudge.sendInputError) |
            Where-Object { $_ } | Select-Object -First 1
        # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
        # r2: the dedicated thread found the console locked (or unreadable) at its last read and sent nothing.
        if ($inputDesktopNudge.sessionLockRefused) {
            $sessionLocked = $inputDesktopNudge.sessionLockedAtInject
            $sessionLockedOwnerOnly = $true
        }
        # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
    } elseif ($nativeAvailable) {
        try {
            $INPUT_MOUSE = 0
            $MOUSEEVENTF_MOVE = [uint32]0x0001
            $nudge = [MLVAppAttrCudaDisplayWake.INPUT[]]@(
                [MLVAppAttrCudaDisplayWake.INPUT]@{ type = $INPUT_MOUSE; mi = [MLVAppAttrCudaDisplayWake.MOUSEINPUT]@{ dx = 1; dy = 0; mouseData = 0; dwFlags = $MOUSEEVENTF_MOVE; time = 0; dwExtraInfo = [IntPtr]::Zero } },
                [MLVAppAttrCudaDisplayWake.INPUT]@{ type = $INPUT_MOUSE; mi = [MLVAppAttrCudaDisplayWake.MOUSEINPUT]@{ dx = -1; dy = 0; mouseData = 0; dwFlags = $MOUSEEVENTF_MOVE; time = 0; dwExtraInfo = [IntPtr]::Zero } }
            )
            $structSize = [System.Runtime.InteropServices.Marshal]::SizeOf([type][MLVAppAttrCudaDisplayWake.INPUT])
            # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            # r2: this nudge runs on this thread, so the lock is read here, as the statement before
            # SendInput. Locked or unknown: no SendInput (the throw lands in the catch below).
            $sessionLockedAtInject = $null
            try { $sessionLockedAtInject = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked' } catch { $sessionLockedAtInject = $null }
            if ($sessionLockedAtInject -ne $false) {
                $sessionLocked = $sessionLockedAtInject
                $sessionLockedOwnerOnly = $true
                throw 'ATTRCUDA_SESSION_LOCKED_OWNER_ONLY no SendInput attempted'
            }
            # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
            $sent = [MLVAppAttrCudaDisplayWake.NativeMethods]::SendInput([uint32]$nudge.Count, $nudge, $structSize)
            if ($sent -ne $nudge.Count) {
                $sendInputError = "SendInput sent $sent of $($nudge.Count) events (lastError=$([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
            }
        } catch {
            $sendInputError = $_.Exception.Message
        }
    } else {
        $sendInputError = 'ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE native P/Invoke type could not be loaded'
    }
    # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
    }
    # Every lock refusal -- at the first read, at the re-read after the probes, before the plain
    # SendInput, or inside the dedicated thread -- reports the same typed reason.
    if ($sessionLockedOwnerOnly) {
        $sendInputError = if ($null -eq $sessionLocked) {
            'ATTRCUDA_SESSION_LOCKED_OWNER_ONLY whether this session is locked could not be read; treated as locked -- no input sent -- signing in is an owner action'
        } else {
            'ATTRCUDA_SESSION_LOCKED_OWNER_ONLY this session is locked; no input sent -- signing in is an owner action'
        }
    }
    # VENUE-SESSION-LOCKED-REFUSAL-1 <<<

    if ($nativeAvailable) {
        try {
            # ES_CONTINUOUS = 0x80000000 (a bare hex literal this large parses as a negative
            # Int32 in PowerShell, not an auto-widened UInt32, so it is spelled decimal instead).
            $ES_CONTINUOUS = [uint32]2147483648
            $ES_SYSTEM_REQUIRED = [uint32]0x00000001
            $ES_DISPLAY_REQUIRED = [uint32]0x00000002
            $result = [MLVAppAttrCudaDisplayWake.NativeMethods]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED -bor $ES_DISPLAY_REQUIRED)
            if ($result -eq 0) { $executionStateError = 'SetThreadExecutionState returned 0 (failed)' }
        } catch {
            $executionStateError = $_.Exception.Message
        }
    } else {
        $executionStateError = 'ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE native P/Invoke type could not be loaded'
    }
    # CUDA-PERF-DISPLAY-WAKE-3 round 3 (live UM evidence, defect class fix): a dismiss was actually
    # attempted under the exact same predicate $dismissFailed below already gates on -- a screen
    # saver CONFIRMED running before, and not secure/unknown. Only then is the after-state worth
    # WAITING for (Wait-AttrCudaScreensaverDismissed's bounded poll, see its own header for why an
    # immediate single read missed all three live UM legs); when nothing was running before, or the
    # secure/unknown gate already stopped short, a single immediate read is unchanged -- there is
    # nothing to wait on either way.
    $dismissAttempted = (-not $screensaverSecureOwnerOnly) -and ($screensaverBefore -eq $true)
    # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
    if ($sessionLockedOwnerOnly) { $dismissAttempted = $false }
    # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
    $dismissWait = $null
    if ($dismissAttempted) {
        $dismissWait = Wait-AttrCudaScreensaverDismissed
        $screensaverAfter = $dismissWait.running
    } else {
        $screensaverAfter = Get-AttrCudaScreensaverRunning
    }

    $method = if ($screensaverSecureOwnerOnly) {
        "SecureScreensaverNoDismissAttempted(reason=$screensaverSecureReason)+SetThreadExecutionState(ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)"
    } elseif ($screensaverBefore -eq $true) {
        'OpenInputDesktop+SetThreadDesktop+SendInputPointerNudge(dedicated thread)+SetThreadExecutionState(ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)'
    } else {
        'SendInputPointerNudge+SetThreadExecutionState(ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)'
    }

    # CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence, defect class fix): a dismiss was actually
    # ATTEMPTED only when a screen saver was CONFIRMED running before and it was not secure/unknown
    # (the branch immediately above this that dispatches to Invoke-AttrCudaInputDesktopNudge, or --
    # when the native P/Invoke type itself could not load -- the ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE
    # branch, which also never actually dismissed anything). Only a CONFIRMED $false reading
    # afterward counts as dismissed; still-$true or unknown ($null, the probe itself failing) both
    # mean this job cannot show the screen saver was actually ended, so both fail closed here --
    # same fail-closed shape as screensaverSecureOwnerOnly above, never treating an unknown result as
    # a quiet success. Never set when no dismiss was attempted at all ($screensaverBefore -eq $false,
    # or the secure/unknown owner-only gate already stopped the leg with its own typed refusal).
    $dismissFailed = (-not $screensaverSecureOwnerOnly) -and ($screensaverBefore -eq $true) -and ($screensaverAfter -ne $false)
    # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
    if ($sessionLockedOwnerOnly) {
        $method = "SessionLockedNoInputAttempted(sessionLocked=$(if ($null -eq $sessionLocked) { 'unknown' } else { 'true' }))+SetThreadExecutionState(ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)"
        $dismissFailed = $false
    }
    # VENUE-SESSION-LOCKED-REFUSAL-1 <<<

    [ordered]@{
        attempted = $true
        method = $method
        # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
        sessionLocked = $sessionLocked
        # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        screensaverRunningBefore = $screensaverBefore
        screensaverRunningAfter = $screensaverAfter
        screensaverSecure = $screensaverSecure
        screensaverSecureOwnerOnly = $screensaverSecureOwnerOnly
        screensaverSecureReason = $screensaverSecureReason
        dismissFailed = $dismissFailed
        dismissWait = $dismissWait
        inputDesktopNudge = $inputDesktopNudge
        sendInputError = $sendInputError
        executionStateError = $executionStateError
        screensaverTimeoutSeconds = $screensaverTimeoutSeconds
        screensaverActive = $screensaverActive
        utc = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Stop-AttrCudaDisplayWake {
    <#
    .SYNOPSIS
    Releases the display-required request Start-AttrCudaDisplayWake made (SetThreadExecutionState
    back to plain ES_CONTINUOUS). Non-throwing, like its counterpart: a failure is recorded in the
    returned evidence, never allowed to block the leg's own exit.
    #>
    [CmdletBinding()]
    param()

    $releaseError = $null
    if (Register-AttrCudaDisplayWakeNativeMethods) {
        try {
            $ES_CONTINUOUS = [uint32]2147483648
            [void][MLVAppAttrCudaDisplayWake.NativeMethods]::SetThreadExecutionState($ES_CONTINUOUS)
        } catch {
            $releaseError = $_.Exception.Message
        }
    } else {
        $releaseError = 'ATTRCUDA_DISPLAY_WAKE_NATIVE_UNAVAILABLE native P/Invoke type could not be loaded'
    }
    [ordered]@{
        released = ($null -eq $releaseError)
        error = $releaseError
        utc = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Start-AttrCudaDisplayWakeKeepAlive {
    <#
    .SYNOPSIS
    Starts a bounded background keep-alive that repeats the input-desktop pointer nudge on an
    interval, independent of Start-AttrCudaDisplayWake's own one-time nudge.
    .DESCRIPTION
    SetThreadExecutionState alone does not stop the screen saver (see Start-AttrCudaDisplayWake's
    own header); a leg or playback session longer than the configured screen-saver timeout needs
    periodic input, not just a continuous execution-state request. This starts a dedicated
    background Runspace inside THIS process (never a new process, never SPI_SET*, never a power
    setting) that loops on -IntervalSeconds until the caller calls Stop-AttrCudaDisplayWakeKeepAlive
    with the returned handle -- so it keeps nudging while the caller's own thread is blocked on
    something else (e.g. a nested smoke launch that runs MLVApp.exe synchronously).
    CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence): each tick now calls the SAME
    [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run helper Invoke-AttrCudaInputDesktopNudge uses
    for the one-time dismiss -- OpenInputDesktop + SetThreadDesktop on a fresh dedicated thread,
    then SendInput, then switch back and CloseDesktop -- rather than a plain SendInput on this
    Runspace's own (Default) desktop. A round 1 leg on Ultra-Magnus recorded
    screensaverRunningBefore=true with EVERY keep-alive tick failing lastError=5
    (ERROR_ACCESS_DENIED): a plain SendInput only ever injects into the CALLING THREAD's own
    desktop, and this Runspace's thread never followed the screen saver's input desktop the way the
    one-time nudge already did. The helper's own dedicated thread is spawned fresh per call (never
    reused across ticks) precisely because Microsoft documents SetThreadDesktop as unusable on a
    thread that has already created a window or hook on its current desktop -- reusing this
    Runspace's own thread for the P/Invoke would eventually hit that. Non-throwing by construction:
    every nudge attempt inside the loop is wrapped in try/catch, and a nudge failure never stops the
    loop or reaches the caller. The caller MUST call Stop-AttrCudaDisplayWakeKeepAlive from a
    `finally` block -- an unstopped keep-alive keeps injecting input and keeps its Runspace open for
    the life of the process.
    .PARAMETER IntervalSeconds
    Nudge period; contract is "periodically (<= every 20 s)", default 15.
    .PARAMETER NudgeJoinTimeoutMilliseconds
    Per-tick join timeout passed to InputDesktopNudge.Run, same default (5000) as
    Invoke-AttrCudaInputDesktopNudge's own -JoinTimeoutMilliseconds. Bounds each tick so a stuck
    dedicated thread cannot delay the next one indefinitely; a join timeout still counts the tick as
    a recorded failure below, never a silent skip.
    .PARAMETER ProbeTimeoutMilliseconds
    CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol hardening). Bound passed to every
    Invoke-AttrCudaBoundedProbe call this loop makes (the running/secure re-read at the top of each
    tick, and the after-nudge confirmation poll) -- see that function's own header for why a probe
    needs a bound at all. A probe that does not return in time reads as $null (state_unknown), the
    same fail-closed value every probe here already returns on any other failure; it is never a
    silent skip, and it never lets a hung probe delay this tick -- or, downstream,
    Stop-AttrCudaDisplayWakeKeepAlive's own EndInvoke wait -- indefinitely.
    .OUTPUTS
    A handle for Stop-AttrCudaDisplayWakeKeepAlive and Get-AttrCudaDisplayWakeKeepAliveHealth.
    .nudgeState.count is a live, thread-safe counter of nudge attempts (incremented whether or not
    that attempt's SendInput itself succeeded, for back-compatible readers); CUDA-PERF-
    DISPLAY-WAKE-3 round 1 adds .nudgeState.successCount/.failureCount/.lastError/
    .lastFailureUtc, so a caller can tell a healthy tick from a failed one instead of only
    counting attempts -- round 2: a tick now also counts as a failure when OpenInputDesktop,
    SetThreadDesktop or the join itself fails, not only when SendInput itself returns short.
    CUDA-PERF-DISPLAY-WAKE-4 round 1: every tick now also re-reads the running/secure probes
    FIRST, with the same fail-closed rule Start-AttrCudaDisplayWake's own claim-time gate uses --
    a screen saver that becomes secure (or whose state becomes unknown) after the claim-time gate
    already passed is never touched, and that tick is recorded as a typed failure
    (ATTRCUDA_KEEPALIVE_BLOCKED reason=secure_screensaver_mid_leg|state_unknown) rather than a
    silent pass; a tick that IS allowed to inject now also only counts as success once the screen
    saver is CONFIRMED not running afterward (bounded re-read), never merely because SendInput
    reported delivering its events; and CloseDesktopError now joins the same tick-failure
    candidate list OpenInputDesktop/SetThreadDesktop/SendInput already used, so a leaked desktop
    handle is no longer invisible to keep-alive health. CUDA-PERF-DISPLAY-WAKE-4 round 1c (sonnet
    hardening): .nudgeState.lastDesktopName records the desktop name the dedicated thread read
    back on the MOST RECENT tick that actually reached InputDesktopNudge.Run (attempted or
    refused alike) -- $null before the first such tick, unchanged by a tick that never reached
    InputDesktopNudge.Run at all (the owner-only gate above tripping).
    VENUE-SESSION-LOCKED-REFUSAL-1 >>>
    VENUE-SESSION-LOCKED-REFUSAL-1: every tick reads Get-AttrCudaSessionLocked FIRST (bounded, like
    the probes above). Anything but a CONFIRMED $false is a typed failure with no injection:
    ATTRCUDA_KEEPALIVE_BLOCKED reason=session_locked, or reason=session_lock_unknown.
    r2: a tick that would inject reads the lock again after the screen-saver probes, as its last
    step before InputDesktopNudge.Run, and that thread reads it once more before SendInput. Any lock
    refusal also sets .nudgeState.sessionLockReason (sticky), which
    Get-AttrCudaDisplayWakeKeepAliveHealth reports so the job ends the leg SESSION_LOCKED_OWNER_ONLY.
    r4: a lock-refusing tick writes .sessionLockReason before .failureCount, so a checkpoint that
    reads the state mid-tick never sees the failure without its lock reason.
    VENUE-SESSION-LOCKED-REFUSAL-1 <<<
    .setupError is $null when the background pipeline started; non-$null means either
    CreateRunspace/Open/BeginInvoke itself failed (recorded, never thrown -- CUDA-PERF-
    DISPLAY-WAKE-3 round 1 hardening) or the native P/Invoke type could not be loaded at all
    (CUDA-PERF-DISPLAY-WAKE-3 round 1b: no Runspace is even started in that case, since a loop with
    no native method to call could never do anything -- a prior version discarded that Boolean and
    started an always-healthy-looking loop that silently nudged nothing on every tick), and in
    either case .runspace/.powershell/.asyncResult are all $null, so Stop-AttrCudaDisplayWakeKeepAlive
    and Get-AttrCudaDisplayWakeKeepAliveHealth both already tolerate that shape.
    #>
    [CmdletBinding()]
    param(
        [ValidateRange(1, 20)]
        [int]$IntervalSeconds = 15,
        [int]$NudgeJoinTimeoutMilliseconds = 5000,
        [int]$ProbeTimeoutMilliseconds = 3000
    )

    # CUDA-PERF-DISPLAY-WAKE-3 round 1b (sol BLOCKER): the Boolean result was previously discarded
    # ([void]) -- when the native P/Invoke type could not be loaded, the loop below silently did
    # nothing on every tick (its own "if type exists" guard just never matched), leaving
    # .nudgeState at all zeros and .setupError $null. Get-AttrCudaDisplayWakeKeepAliveHealth reads
    # exactly those two fields, so it reported a completely non-functional keep-alive as healthy.
    # Captured here instead: a load failure is recorded as a typed .setupError up front, the same
    # non-throwing shape a CreateRunspace/Open/BeginInvoke failure already gets below, and no
    # Runspace is even started for a keep-alive that could never do anything.
    $nativeAvailable = Register-AttrCudaDisplayWakeNativeMethods
    if (-not $nativeAvailable) {
        return [ordered]@{
            stopEvent = [System.Threading.ManualResetEventSlim]::new($false)
            runspace = $null
            powershell = $null
            asyncResult = $null
            nudgeState = [System.Collections.Hashtable]::Synchronized(@{
                count = 0
                successCount = 0
                failureCount = 0
                lastError = $null
                lastFailureUtc = $null
                lastDesktopName = $null
                # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
                sessionLockReason = $null
                # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
            })
            intervalSeconds = $IntervalSeconds
            nudgeJoinTimeoutMilliseconds = $NudgeJoinTimeoutMilliseconds
            probeTimeoutMilliseconds = $ProbeTimeoutMilliseconds
            setupError = 'ATTRCUDA_KEEPALIVE_NATIVE_UNAVAILABLE native P/Invoke type could not be loaded; no keep-alive loop was started'
            startedUtc = (Get-Date).ToUniversalTime().ToString('o')
        }
    }
    $stopEvent = [System.Threading.ManualResetEventSlim]::new($false)
    # Synchronized wrapper: the loop thread below and this (the caller's) thread both touch the
    # same underlying Hashtable instance -- a plain Hashtable is not safe for that, .Synchronized
    # is.
    $nudgeState = [System.Collections.Hashtable]::Synchronized(@{
        count = 0
        successCount = 0
        failureCount = 0
        lastError = $null
        lastFailureUtc = $null
        lastDesktopName = $null
        # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
        # r2: set (sticky) by any tick refused for a locked or unreadable console lock.
        sessionLockReason = $null
        # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
    })
    $loopScript = {
        param($StopEvent, $IntervalSeconds, $NudgeState, $JoinTimeoutMilliseconds, $ProbeTimeoutMilliseconds)
        while (-not $StopEvent.Wait([int]($IntervalSeconds * 1000))) {
            try {
                # CUDA-PERF-DISPLAY-WAKE-4 round 1 (BLOCKER fix): re-read the running/secure probes
                # before EVERY tick -- not just once at claim time -- with the SAME fail-closed rule
                # Start-AttrCudaDisplayWake's own .screensaverSecureOwnerOnly applies: only a
                # CONFIRMED $false running reads as safe to touch, and a CONFIRMED-running screen
                # saver may only be touched when its secure state is CONFIRMED $false too. A secure
                # (or unprovably-not-secure) screen saver that engages AFTER the claim-time gate
                # already passed must never receive an injection from this loop. Both functions are
                # defined in this Runspace's InitialSessionState (see Start-AttrCudaDisplayWakeKeepAlive
                # above) from their CURRENT definitions in the caller's own scope, so a test can
                # override this exact behavior the same way Start-AttrCudaDisplayWake's own probes are
                # already overridden. Round 1b (sol hardening): each read now goes through
                # Invoke-AttrCudaBoundedProbe -- itself copied into this Runspace's
                # InitialSessionState the same way -- so a probe (or a test's override of one) that
                # never returns cannot block this tick, or Stop-AttrCudaDisplayWakeKeepAlive's own
                # EndInvoke wait, indefinitely; see that function's own header.
                # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
                # The session lock is re-read FIRST, every tick, also bounded. A console locked
                # mid-leg (or one whose lock state cannot be read) gets no injection: the desktop-name
                # gate inside InputDesktopNudge.Run cannot see a lock, because a locked console's
                # input desktop still reads "Default".
                $tickSessionLocked = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked' -TimeoutMilliseconds $ProbeTimeoutMilliseconds
                # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
                $tickRunning = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverRunning' -TimeoutMilliseconds $ProbeTimeoutMilliseconds
                $tickSecure = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverSecure' -TimeoutMilliseconds $ProbeTimeoutMilliseconds
                $tickSecureOwnerOnly = if ($tickRunning -eq $false) {
                    $false
                } elseif ($tickRunning -eq $true) {
                    $tickSecure -ne $false
                } else {
                    $true
                }
                # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
                # Recorded as a typed failure like the screen-saver block below, so the job's next
                # keep-alive checkpoint stops the leg. The unchanged branches below run only for a
                # CONFIRMED unlocked session.
                # r2 (sol blocker 1): the read at the top of the tick can be stale once the screen-saver
                # probes have run, so a tick that would inject reads the lock again here, as its last
                # step before InputDesktopNudge.Run (whose thread reads it once more before SendInput).
                if ($tickSessionLocked -eq $false -and -not $tickSecureOwnerOnly) {
                    $tickSessionLocked = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaSessionLocked' -TimeoutMilliseconds $ProbeTimeoutMilliseconds
                }
                if ($tickSessionLocked -ne $false) {
                    $sessionBlockedReason = if ($null -eq $tickSessionLocked) { 'session_lock_unknown' } else { 'session_locked' }
                    # r2 (sol blocker 2): sticky, so the job's checkpoint ends the leg owner-only even
                    # when a later failure overwrites .lastError.
                    # r4 (sol r3 blocker): the classification is published BEFORE failureCount, which is
                    # written last. A checkpoint that runs mid-publication therefore never sees this
                    # tick's failure without its lock reason (Get-AttrCudaDisplayWakeKeepAliveHealth
                    # reads failureCount first, then the reason).
                    $NudgeState.sessionLockReason = $sessionBlockedReason
                    $NudgeState.lastError = "ATTRCUDA_KEEPALIVE_BLOCKED reason=$sessionBlockedReason no injection attempted (a locked console is owner-only; signing in is an owner action)"
                    $NudgeState.lastFailureUtc = (Get-Date).ToUniversalTime().ToString('o')
                    $NudgeState.count = [int]$NudgeState.count + 1
                    $NudgeState.failureCount = [int]$NudgeState.failureCount + 1
                } else {
                # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
                if ($tickSecureOwnerOnly) {
                    # No injection of any kind: ending a password-protected (or unprovably-not-
                    # password-protected) screen saver is an owner action, never an automated one --
                    # same boundary Start-AttrCudaDisplayWake's own claim-time gate enforces. Recorded
                    # as a typed failure (never a silent skip) so the next keep-alive checkpoint stops
                    # the leg via Get-AttrCudaDisplayWakeKeepAliveHealth's existing failureCount gate.
                    # KEEPALIVE-FAILURE-PUBLISH-LAST-1: lastError and lastFailureUtc are published BEFORE
                    # failureCount, which is written last (same rule as the lock branch above), so a
                    # checkpoint that runs mid-publication never sees this failure with an empty last error.
                    $NudgeState.count = [int]$NudgeState.count + 1
                    $blockedReason = if ($null -eq $tickRunning) { 'state_unknown' } else { 'secure_screensaver_mid_leg' }
                    $NudgeState.lastError = "ATTRCUDA_KEEPALIVE_BLOCKED reason=$blockedReason no injection attempted (screen saver running/secure state mid-leg forbids touching it)"
                    $NudgeState.lastFailureUtc = (Get-Date).ToUniversalTime().ToString('o')
                    $NudgeState.failureCount = [int]$NudgeState.failureCount + 1
                } elseif ("MLVAppAttrCudaDisplayWake.InputDesktopNudge" -as [type]) {
                    # CUDA-PERF-DISPLAY-WAKE-3 round 2 (live UM evidence): the SAME dedicated-thread
                    # OpenInputDesktop+SetThreadDesktop+SendInput+CloseDesktop helper the one-time
                    # dismiss uses (Invoke-AttrCudaInputDesktopNudge), never a plain SendInput on this
                    # Runspace's own thread -- see this function's own header for why a plain SendInput
                    # here failed on every tick once a screen saver had taken the input desktop.
                    $result = [MLVAppAttrCudaDisplayWake.InputDesktopNudge]::Run([int]$JoinTimeoutMilliseconds)
                    # CUDA-PERF-DISPLAY-WAKE-4 round 1c (sonnet hardening): the desktop name this
                    # tick's dedicated thread actually read back right before SendInput (or refused
                    # on), so a still-live keep-alive shows what desktop it has been injecting into --
                    # not only visible after the fact in the one-time dismiss's own evidence.
                    $NudgeState.lastDesktopName = $result.DesktopName
                    $NudgeState.count = [int]$NudgeState.count + 1
                    # CUDA-PERF-DISPLAY-WAKE-3 round 1 (sol BLOCKER): SendInput's own return is the
                    # number of events it actually inserted -- Microsoft documents fewer than
                    # requested as failure. The prior code discarded this and always advanced the
                    # counter as if the tick had succeeded, so a screen saver re-engaging mid-leg
                    # (or any other injection failure) went unnoticed here. Round 2: the same is now
                    # also true of OpenInputDesktop/SetThreadDesktop failing, or the dedicated thread
                    # itself not joining in time -- any of the three is a tick that did not deliver
                    # input, not just a short SendInput. Round 4 (sol hardening): CloseDesktopError
                    # joins the same candidate list -- a leaked desktop handle every tick used to be
                    # invisible to keep-alive health because only Open/SetThreadDesktop/SendInput were
                    # ever selected here.
                    $tickError = @($result.OpenInputDesktopError, $result.SetThreadDesktopError, $result.SendInputError, $result.CloseDesktopError) |
                        Where-Object { $_ } | Select-Object -First 1
                    # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
                    # r2 (sol blocker 1): the dedicated thread's own last read found the console locked
                    # (or unreadable) and sent nothing -- the same typed, sticky refusal as above.
                    $tickSessionLockRefused = $false
                    try { $tickSessionLockRefused = [bool]$result.SessionLockRefused } catch { $tickSessionLockRefused = $false }
                    if ($tickSessionLockRefused) {
                        $sessionBlockedReason = if ($null -eq $result.SessionLockedAtInject) { 'session_lock_unknown' } else { 'session_locked' }
                        $NudgeState.sessionLockReason = $sessionBlockedReason
                        $tickError = "ATTRCUDA_KEEPALIVE_BLOCKED reason=$sessionBlockedReason no SendInput attempted (the console locked before the nudge thread's SendInput; signing in is an owner action)"
                    }
                    # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
                    if (-not $tickError -and -not [bool]$result.ThreadJoined) {
                        $tickError = "InputDesktopNudge dedicated thread did not join within ${JoinTimeoutMilliseconds}ms"
                    }
                    if (-not $tickError) {
                        # CUDA-PERF-DISPLAY-WAKE-4 round 1 (fable hardening): a tick counts as
                        # success only once the screen saver is CONFIRMED not running after the
                        # nudge -- never merely because SendInput reported delivering its events --
                        # so a screen saver that re-engages mid-leg despite an apparently-clean
                        # injection (the round-3 class of defect Wait-AttrCudaScreensaverDismissed
                        # was added to catch at claim time) is still caught here. Bounded at 2s/250ms,
                        # a smaller budget than the 5s claim-time dismiss wait since a healthy tick
                        # only needs to confirm the idle timer was actually reset, not end an
                        # already-engaged screen saver. Round 1b (sol hardening): this call is now
                        # ALSO bounded via Invoke-AttrCudaBoundedProbe -- without it, a single hung
                        # underlying probe call inside this while loop could block past the 2000ms
                        # budget below (the budget is only ever checked BETWEEN calls, never during
                        # one), same class of defect as the top-of-tick probes above.
                        $afterRunning = $null
                        $afterPoll = [System.Diagnostics.Stopwatch]::StartNew()
                        while ($true) {
                            $afterRunning = Invoke-AttrCudaBoundedProbe -FunctionName 'Get-AttrCudaScreensaverRunning' -TimeoutMilliseconds $ProbeTimeoutMilliseconds
                            if ($afterRunning -eq $false) { break }
                            if ($afterPoll.ElapsedMilliseconds -ge 2000) { break }
                            Start-Sleep -Milliseconds 250
                        }
                        if ($afterRunning -ne $false) {
                            $tickError = "ATTRCUDA_KEEPALIVE_STILL_RUNNING screen saver still running (confirmed=$afterRunning) after the keep-alive nudge"
                        }
                    }
                    if ($tickError) {
                        # KEEPALIVE-FAILURE-PUBLISH-LAST-1: failureCount is written last, after lastError
                        # and lastFailureUtc (and after .sessionLockReason, set above for a lock refusal).
                        $NudgeState.lastError = $tickError
                        $NudgeState.lastFailureUtc = (Get-Date).ToUniversalTime().ToString('o')
                        $NudgeState.failureCount = [int]$NudgeState.failureCount + 1
                    } else {
                        $NudgeState.successCount = [int]$NudgeState.successCount + 1
                    }
                }
                # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
                }
                # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
            } catch {
                # Non-throwing by construction: a single nudge failure must never stop the loop or
                # escape to the caller -- the next tick simply tries again. Still counted as a
                # failure, unlike before, so it is visible to Get-AttrCudaDisplayWakeKeepAliveHealth.
                # KEEPALIVE-FAILURE-PUBLISH-LAST-1: failureCount is written last, after lastError and
                # lastFailureUtc, so a checkpoint never sees this failure with an empty last error.
                $NudgeState.lastError = $_.Exception.Message
                $NudgeState.lastFailureUtc = (Get-Date).ToUniversalTime().ToString('o')
                $NudgeState.failureCount = [int]$NudgeState.failureCount + 1
            }
        }
    }

    # CUDA-PERF-DISPLAY-WAKE-3 round 1 (fable hardening): CreateRunspace/Open/BeginInvoke are the
    # one part of this file's "never throws" contract that used to sit OUTSIDE any try/catch --
    # every other function here records a failure into its returned evidence instead of letting it
    # propagate. A setup failure (e.g. resource exhaustion) is now recorded the same way: the
    # caller gets a handle back (never a thrown error) with .setupError set and no live pipeline.
    $runspace = $null
    $shell = $null
    $asyncResult = $null
    $setupError = $null
    try {
        # CUDA-PERF-DISPLAY-WAKE-4 round 1 (BLOCKER fix): the new Runspace below shares this
        # PROCESS's loaded .NET types (Add-Type is AppDomain-wide -- Register-AttrCudaDisplayWakeNativeMethods
        # already ran above) but inherits NONE of this scope's PowerShell FUNCTIONS --
        # Get-AttrCudaScreensaverRunning/-Secure do not exist there unless explicitly added. Each
        # one's CURRENT definition (Get-Command, resolved dynamically in THIS scope -- the same
        # late-binding-by-name mechanism the deployed flat job script and this module's own
        # _wake_with_overrides test helper already rely on) is added to the new Runspace's
        # InitialSessionState via SessionStateFunctionEntry, so $loopScript's per-tick re-check
        # above can call them, and a test can override this exact behavior by redefining the three
        # functions in its own flat script before calling this one. Round 1b (sol hardening):
        # Invoke-AttrCudaBoundedProbe joins the same list -- the loop calls IT, not the two probes
        # directly, so it must exist in this Runspace too; it resolves 'Get-AttrCudaScreensaverRunning'/
        # '-Secure' by name FROM WITHIN this same Runspace when it runs, so it still sees whichever
        # definition (default or test-overridden) was loaded here.
        $initialSessionState = [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault()
        foreach ($tickProbeFunctionName in @(
                'Register-AttrCudaDisplayWakeNativeMethods', 'Get-AttrCudaScreensaverRunning',
                'Get-AttrCudaScreensaverSecure', 'Invoke-AttrCudaBoundedProbe')) {
            $tickProbeCommand = Get-Command -Name $tickProbeFunctionName -CommandType Function
            $initialSessionState.Commands.Add(
                [System.Management.Automation.Runspaces.SessionStateFunctionEntry]::new(
                    $tickProbeFunctionName, $tickProbeCommand.Definition))
        }
        # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
        # The per-tick lock read, added the same late-binding-by-name way as the probes above.
        $initialSessionState.Commands.Add(
            [System.Management.Automation.Runspaces.SessionStateFunctionEntry]::new(
                'Get-AttrCudaSessionLocked', (Get-Command -Name 'Get-AttrCudaSessionLocked' -CommandType Function).Definition))
        # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        $runspace = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace($initialSessionState)
        $runspace.Open()
        $shell = [System.Management.Automation.PowerShell]::Create()
        $shell.Runspace = $runspace
        [void]$shell.AddScript($loopScript).AddArgument($stopEvent).AddArgument($IntervalSeconds).AddArgument($nudgeState).AddArgument($NudgeJoinTimeoutMilliseconds).AddArgument($ProbeTimeoutMilliseconds)
        $asyncResult = $shell.BeginInvoke()
    } catch {
        $setupError = $_.Exception.Message
        try { if ($shell) { $shell.Dispose() } } catch {
        }
        try {
            if ($runspace) {
                $runspace.Close()
                $runspace.Dispose()
            }
        } catch {
        }
        $runspace = $null
        $shell = $null
        $asyncResult = $null
    }

    [ordered]@{
        stopEvent = $stopEvent
        runspace = $runspace
        powershell = $shell
        asyncResult = $asyncResult
        nudgeState = $nudgeState
        intervalSeconds = $IntervalSeconds
        nudgeJoinTimeoutMilliseconds = $NudgeJoinTimeoutMilliseconds
        probeTimeoutMilliseconds = $ProbeTimeoutMilliseconds
        setupError = $setupError
        startedUtc = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Get-AttrCudaDisplayWakeKeepAliveHealth {
    <#
    .SYNOPSIS
    Non-throwing point-in-time health read of a Start-AttrCudaDisplayWakeKeepAlive handle -- the
    caller's "never proceed silently" check, meant to be called at more than one point in a leg
    (CUDA-PERF-DISPLAY-WAKE-3 round 1: before the smoke launch, and again at the start of the
    measured interval; round 1b adds a third call right after the measured interval ends -- a
    failure discovered only there must still stop the leg typed, never read as a clean
    measurement just because at least one frame displayed).
    .DESCRIPTION
    .healthy is $false when: the keep-alive never started at all (.setupError, from
    Start-AttrCudaDisplayWakeKeepAlive's own setup failure -- CUDA-PERF-DISPLAY-WAKE-3 round 1b:
    this now also covers a native P/Invoke type that never loaded, which used to look like a
    healthy zero-attempt loop instead), at least one nudge attempt has failed since it started
    (.failureCount -gt 0, via the loop's own recorded .lastError), or its background pipeline has
    completed on its own (.asyncResult.IsCompleted) while .stopEvent was never signalled -- the
    caller never asked it to stop, so a completed pipeline means the loop thread died. Never
    throws: a read that itself fails folds into .reason as ATTRCUDA_KEEPALIVE_HEALTH_CHECK_FAILED
    rather than propagating, since a health CHECK failing must never be mistaken for "healthy" by
    a caller that only checked for a thrown error.
    .PARAMETER RequireSuccessSoFar
    CUDA-PERF-DISPLAY-WAKE-3 round 1b: when set, a zero .successCount (with no other unhealthy
    condition already true) is ALSO reported unhealthy. Meant for the job's FIRST checkpoint only
    -- by then the keep-alive has already been running since before footage resolution, package
    verification and the CPU-quiescence sleeps (round 1c's own 4.5-minute gap), so a genuinely
    ticking loop is expected to have succeeded at least once; a loop stuck with zero attempts for
    reasons the three existing checks above do not catch (e.g. a Runspace that opened but never
    actually invoked the script) must not read as healthy just because nothing has failed yet.
    Omitted at later checkpoints, where the same "zero so far" reading would otherwise be
    redundant with whatever already made the earlier checkpoint pass.
    .OUTPUTS
    An ordered hashtable: .healthy, .reason (a typed string prefix, $null when healthy),
    .failureCount, .successCount, .lastError, .lastFailureUtc, .setupError, .runspaceStopped.
    #>
    [CmdletBinding()]
    param(
        $Handle,
        [switch]$RequireSuccessSoFar
    )

    if (-not $Handle) {
        return [ordered]@{
            healthy = $false
            reason = 'ATTRCUDA_KEEPALIVE_MISSING no keep-alive handle was supplied'
            failureCount = $null
            successCount = $null
            lastError = $null
            lastFailureUtc = $null
            setupError = $null
            runspaceStopped = $null
            # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            sessionLockReason = $null
            # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }
    }

    try {
        $setupError = $Handle.setupError
        $failureCount = 0
        $successCount = 0
        $lastError = $null
        $lastFailureUtc = $null
        if ($Handle.nudgeState) {
            $failureCount = [int]$Handle.nudgeState.failureCount
            # CUDA-PERF-DISPLAY-WAKE-3 round 1b: .successCount is read defensively -- a caller can
            # still duck-type a handle from an older shape that never had it (this module's own
            # Set-StrictMode -Version Latest, line 21, turns that missing-member read into a
            # terminating error), and this function's whole contract is that no shape of $Handle
            # ever makes it throw. A local try/catch, not a property-existence check: .PSObject.
            # Properties.Match never sees a Hashtable/OrderedDictionary's keys as properties at
            # all (it would report 0 even for a key that IS present), so it cannot tell "missing"
            # from "present" on the fabricated ordered-hashtable handles this module's own test
            # suite already builds.
            try { $successCount = [int]$Handle.nudgeState.successCount } catch { }
            $lastError = $Handle.nudgeState.lastError
            $lastFailureUtc = $Handle.nudgeState.lastFailureUtc
        }
        # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
        # r2 (sol blocker 2): the keep-alive's own sticky classification of a lock refusal
        # ('session_locked' or 'session_lock_unknown', set by the tick that refused), read as written
        # -- never re-derived from .lastError text. Read defensively, like .successCount above.
        # r4 (sol r3 blocker): read AFTER .failureCount on purpose. Every lock-refusing tick publishes
        # this reason before it increments .failureCount, so a failure counted above is never missing
        # its lock reason here, even when the tick is still publishing.
        $sessionLockReason = $null
        if ($Handle.nudgeState) {
            try { $sessionLockReason = $Handle.nudgeState.sessionLockReason } catch { $sessionLockReason = $null }
        }
        # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        $runspaceStopped = $false
        if ($Handle.asyncResult -and $Handle.stopEvent -and [bool]$Handle.asyncResult.IsCompleted -and -not [bool]$Handle.stopEvent.IsSet) {
            $runspaceStopped = $true
        }
        $reason = if ($setupError) {
            "ATTRCUDA_KEEPALIVE_SETUP_FAILED $setupError"
        } elseif ($runspaceStopped) {
            'ATTRCUDA_KEEPALIVE_RUNSPACE_STOPPED background pipeline completed without a stop request'
        } elseif ($failureCount -gt 0) {
            "ATTRCUDA_KEEPALIVE_NUDGE_FAILED $failureCount failed nudge(s), last: $lastError"
        } elseif ($RequireSuccessSoFar -and $successCount -eq 0) {
            'ATTRCUDA_KEEPALIVE_NO_SUCCESSFUL_NUDGE_YET no nudge has succeeded since the keep-alive started'
        } else {
            $null
        }
        [ordered]@{
            healthy = ($null -eq $reason)
            reason = $reason
            failureCount = $failureCount
            successCount = $successCount
            lastError = $lastError
            lastFailureUtc = $lastFailureUtc
            setupError = $setupError
            runspaceStopped = $runspaceStopped
            # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            sessionLockReason = $sessionLockReason
            # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }
    } catch {
        [ordered]@{
            healthy = $false
            reason = "ATTRCUDA_KEEPALIVE_HEALTH_CHECK_FAILED $($_.Exception.Message)"
            failureCount = $null
            successCount = $null
            lastError = $null
            lastFailureUtc = $null
            setupError = $null
            runspaceStopped = $null
            # VENUE-SESSION-LOCKED-REFUSAL-1 >>>
            sessionLockReason = $null
            # VENUE-SESSION-LOCKED-REFUSAL-1 <<<
        }
    }
}

function Stop-AttrCudaDisplayWakeKeepAlive {
    <#
    .SYNOPSIS
    Stops a keep-alive started by Start-AttrCudaDisplayWakeKeepAlive and releases its Runspace.
    Non-throwing, and safe to call with $null or an already-stopped handle -- a job's `finally`
    block may reach here even when Start-AttrCudaDisplayWakeKeepAlive was never reached, or reached
    only as far as recording a .setupError.
    .PARAMETER TimeoutMilliseconds
    CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol hardening). Bounds this call's own wait for the loop's
    background pipeline to actually finish after -StopEvent.Set() -- defense in depth alongside the
    per-tick probe bound (Invoke-AttrCudaBoundedProbe) Start-AttrCudaDisplayWakeKeepAlive's loop now
    uses: EndInvoke on its own blocks until the pipeline's CURRENT tick completes, which would be
    unbounded if that tick were ever stuck. This call therefore never blocks past
    -TimeoutMilliseconds regardless of what the loop's current tick is doing.
    #>
    [CmdletBinding()]
    param(
        $Handle,
        [int]$TimeoutMilliseconds = 15000
    )

    $stopError = $null
    $nudgeCount = $null
    $successCount = $null
    $failureCount = $null
    $lastError = $null
    $lastFailureUtc = $null
    $setupError = $null
    if ($Handle) {
        $setupError = $Handle.setupError
        if ($Handle.nudgeState) {
            $nudgeCount = [int]$Handle.nudgeState.count
            $successCount = [int]$Handle.nudgeState.successCount
            $failureCount = [int]$Handle.nudgeState.failureCount
            $lastError = $Handle.nudgeState.lastError
            $lastFailureUtc = $Handle.nudgeState.lastFailureUtc
        }
        try {
            if ($Handle.stopEvent) { $Handle.stopEvent.Set() }
            if ($Handle.powershell -and $Handle.asyncResult) {
                # CUDA-PERF-DISPLAY-WAKE-4 round 1b (sol hardening): wait on the async handle
                # directly first, bounded -- EndInvoke's own wait has no such bound. EndInvoke below
                # then returns immediately (the pipeline has already completed) when that wait
                # succeeds; when it does not, EndInvoke is skipped entirely rather than called
                # unbounded, and the timeout is recorded as a typed .error instead.
                if ($Handle.asyncResult.AsyncWaitHandle.WaitOne([int]$TimeoutMilliseconds)) {
                    [void]$Handle.powershell.EndInvoke($Handle.asyncResult)
                } else {
                    $stopError = "ATTRCUDA_KEEPALIVE_STOP_TIMEOUT background pipeline did not complete within ${TimeoutMilliseconds}ms of the stop request"
                }
            }
        } catch {
            $stopError = $_.Exception.Message
        }
        # CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1 >>>
        # PowerShell.Dispose()/Runspace.Close() on a pipeline that is STILL RUNNING block until it
        # stops -- i.e. until the stuck tick's bounded probe returns -- so after the timed-out wait
        # above this call used to take ~ the probe bound (measured 12000ms for
        # -ProbeTimeoutMilliseconds 12000, -TimeoutMilliseconds 300), making -TimeoutMilliseconds a
        # lie. Same leak-on-purpose choice Invoke-AttrCudaBoundedProbe makes for the same reason: a
        # still-running pipeline (and the stop event it is still waiting on, already Set above) is
        # abandoned rather than torn down. Only the timeout branch above sets a stop error with
        # this prefix (a thrown EndInvoke means the pipeline already completed), so the prefix
        # alone says "still running". Dropping the handle makes the teardown below a no-op: each
        # of its three blocks guards on a member of $Handle inside its own try.
        if ($stopError -like 'ATTRCUDA_KEEPALIVE_STOP_TIMEOUT*') { $Handle = $null }
        # CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1 <<<
        try { if ($Handle.powershell) { $Handle.powershell.Dispose() } } catch {
        }
        try { if ($Handle.stopEvent) { $Handle.stopEvent.Dispose() } } catch {
        }
        try {
            if ($Handle.runspace) {
                $Handle.runspace.Close()
                $Handle.runspace.Dispose()
            }
        } catch {
        }
    }
    [ordered]@{
        stopped = ($null -eq $stopError)
        error = $stopError
        nudgeCount = $nudgeCount
        successCount = $successCount
        failureCount = $failureCount
        lastError = $lastError
        lastFailureUtc = $lastFailureUtc
        setupError = $setupError
        utc = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Get-AttrCudaAppSwapTelemetry {
    <#
    .SYNOPSIS
    An app-side count of on-screen swaps for the measured leg, independent of PresentMon.
    .DESCRIPTION
    PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): the sufficiency gate's coverage
    arm must not divide PresentMon's own positive-interval rows by a count PresentMon itself also
    produced -- that is circular, and a capture that lost the tail of a leg after a short healthy
    prefix still reads 100% coverage of its own truncated rows. The MLVApp log's own
    playback_smoke.gpu_window_swaps line (platform/qt/GpuDisplayWindow.cpp,
    swapTelemetrySnapshot -- real confirmed on-screen swaps) is the preferred, more precise source,
    used only when it reports both telemetry_enabled=1 and window_active=1. playback_smoke.gate's
    frames_presented (frame SUBMISSIONS, unconditionally logged every run, LIGHT arm included) is
    the fallback when swap telemetry itself is unavailable -- still an app-side signal PresentMon
    never touches, just coarser than a confirmed on-screen swap count.
    Returns swapCount=$null (source=$null) only when NEITHER line is present/usable in the log --
    the caller must treat that as coverage being unmeasurable, never as coverage=0 or coverage=1.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$LogText
    )

    function Get-AttrCudaLastKeyValueLine([string]$Text, [string]$LinePrefixPattern) {
        $last = $null
        foreach ($line in ($Text -split "`r?`n")) {
            if ($line -notmatch $LinePrefixPattern) { continue }
            $values = @{}
            foreach ($match in [regex]::Matches($line, '(?<key>[A-Za-z0-9_]+)=(?<value>[^\s]+)')) {
                $values[$match.Groups['key'].Value] = $match.Groups['value'].Value
            }
            $last = $values
        }
        $last
    }

    $swapsValues = Get-AttrCudaLastKeyValueLine $LogText 'playback_smoke\.gpu_window_swaps '
    [int]$swaps = 0
    $swapsUsable = ($null -ne $swapsValues) -and
        $swapsValues.ContainsKey('telemetry_enabled') -and $swapsValues['telemetry_enabled'] -eq '1' -and
        $swapsValues.ContainsKey('window_active') -and $swapsValues['window_active'] -eq '1' -and
        $swapsValues.ContainsKey('swaps') -and [int]::TryParse([string]$swapsValues['swaps'], [ref]$swaps)
    if ($swapsUsable) {
        # CUDA-PLAYBACK-PRESENT-CADENCE-2 round 1: new_frame_swaps (platform/qt/MainWindow.cpp's
        # playback_smoke.gpu_window_swaps line, GpuWindowSwapTelemetryCounters::newFrameSwapCount)
        # -- swaps that displayed genuinely new content, as opposed to swaps= above which counts
        # every real swap including a repaint of already-shown content. $null (not 0) when the
        # field is absent (an older build's log, predating this round), so a caller can tell
        # "no new-frame field in this log" apart from "the field reported zero".
        [int]$newFrameSwaps = 0
        $newFrameSwapsPresent = $swapsValues.ContainsKey('new_frame_swaps') -and
            [int]::TryParse([string]$swapsValues['new_frame_swaps'], [ref]$newFrameSwaps)
        return [pscustomobject]@{
            source = 'gpu_window_swaps'
            swapCount = $swaps
            newFrameSwapCount = $(if ($newFrameSwapsPresent) { $newFrameSwaps } else { $null })
        }
    }

    $gateValues = Get-AttrCudaLastKeyValueLine $LogText 'playback_smoke\.gate '
    [int]$framesPresented = 0
    $gateUsable = ($null -ne $gateValues) -and $gateValues.ContainsKey('frames_presented') -and
        [int]::TryParse([string]$gateValues['frames_presented'], [ref]$framesPresented)
    if ($gateUsable) {
        # The gate fallback has no new-frame concept of its own (frames_presented counts
        # SUBMISSIONS, before the window's one-pending-slot mailbox can even drop or repaint
        # one) -- $null here, same as the "neither line present" case below, so a caller
        # never mistakes a coarser fallback source for a measured zero.
        return [pscustomobject]@{ source = 'gate_frames_presented'; swapCount = $framesPresented; newFrameSwapCount = $null }
    }

    [pscustomobject]@{ source = $null; swapCount = $null; newFrameSwapCount = $null }
}

function Get-AttrCudaForegroundVerification {
    <#
    .SYNOPSIS
    App-side confirmation that the measured leg ran fullscreen and in the foreground
    throughout, independent of PresentMon.
    .DESCRIPTION
    CUDA-PLAYBACK-PRESENT-CADENCE-2 round 1: the DISPLAY_ASLEEP override (see
    playback-attr-3-cuda-job.ps1) must never trust the app's own new_frame_swaps count alone
    -- a leg that lost fullscreen or foreground mid-run could still swap frames while genuinely
    not being the on-screen content a viewer would see. Reads MainWindow.cpp's
    playback_smoke.foreground line (finishPlaybackSmokeTelemetry): "verified" requires telemetry
    to have been enabled AND fullscreen at BOTH begin and gate AND foreground at BOTH begin and
    gate AND zero fullscreen losses AND zero foreground losses in between -- the same
    event-driven counters #171/CUDA-PERF-PLAYBACK-FOREGROUND-1 already accumulate, reused here
    rather than re-derived. round 1c (sol blocker): fullscreen_at_begin/_at_gate alone let a
    backgrounded-but-fullscreen run (foreground_at_begin=0, foreground_at_gate=0) read as
    verified, since only the *_lost_count deltas were checked and a run that starts and ends
    backgrounded never "loses" foreground in between; foreground_at_begin/_at_gate are now
    required fields too, so a background run fails closed. Returns verified=$false (never
    $true) when the line is absent/unusable -- absence is never treated as passing evidence.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$LogText
    )

    $last = $null
    foreach ($line in ($LogText -split "`r?`n")) {
        if ($line -notmatch 'playback_smoke\.foreground ') { continue }
        $values = @{}
        foreach ($match in [regex]::Matches($line, '(?<key>[A-Za-z0-9_]+)=(?<value>[^\s]+)')) {
            $values[$match.Groups['key'].Value] = $match.Groups['value'].Value
        }
        $last = $values
    }
    if ($null -eq $last) {
        return [pscustomobject]@{ verified = $false; reason = 'no playback_smoke.foreground line found in the log' }
    }

    [int]$foregroundLostCount = -1
    [int]$fullscreenLostCount = -1
    $usable =
        $last.ContainsKey('telemetry_enabled') -and $last['telemetry_enabled'] -eq '1' -and
        $last.ContainsKey('fullscreen_at_begin') -and $last['fullscreen_at_begin'] -eq '1' -and
        $last.ContainsKey('fullscreen_at_gate') -and $last['fullscreen_at_gate'] -eq '1' -and
        $last.ContainsKey('foreground_at_begin') -and $last['foreground_at_begin'] -eq '1' -and
        $last.ContainsKey('foreground_at_gate') -and $last['foreground_at_gate'] -eq '1' -and
        $last.ContainsKey('foreground_lost_count') -and [int]::TryParse([string]$last['foreground_lost_count'], [ref]$foregroundLostCount) -and
        $last.ContainsKey('fullscreen_lost_count') -and [int]::TryParse([string]$last['fullscreen_lost_count'], [ref]$fullscreenLostCount)
    if (-not $usable) {
        return [pscustomobject]@{ verified = $false; reason = 'playback_smoke.foreground line present but missing/unparseable required fields' }
    }
    if ($foregroundLostCount -ne 0 -or $fullscreenLostCount -ne 0) {
        return [pscustomobject]@{
            verified = $false
            reason = "foreground_lost_count=$foregroundLostCount fullscreen_lost_count=$fullscreenLostCount (both must be 0)"
        }
    }
    [pscustomobject]@{ verified = $true; reason = $null }
}

function Get-AttrCudaTemporalCoverage {
    <#
    .SYNOPSIS
    Whether positive-interval rows span the measured window without a silent gap.
    .DESCRIPTION
    PRESENTMON-HARNESS-ROBUSTNESS-2 r1b: count and app-swap coverage alone cannot catch a captured
    PREFIX followed by silence -- a leg that captures its minimum row count in the first couple of
    seconds of a 25-40s window, then loses the rest, can clear both a count floor and a swap-count
    ratio while having actually measured almost none of the leg. This checks the head gap (first
    row after WindowStartMs), every internal gap between consecutive rows, and the tail gap
    (WindowEndMs after the last row); 'sufficient' requires every one of them to be <= MaxGapMs.
    Zero rows is always insufficient -- with no rows the whole window is one gap, start to end.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowNull()]
        [AllowEmptyCollection()]
        [double[]]$TimeInMsValues,

        [Parameter(Mandatory = $true)]
        [double]$WindowStartMs,

        [Parameter(Mandatory = $true)]
        [double]$WindowEndMs,

        [Parameter(Mandatory = $true)]
        [double]$MaxGapMs
    )

    $sorted = @($TimeInMsValues | Sort-Object)
    if ($sorted.Count -eq 0) {
        return [pscustomobject]@{
            sufficient = $false
            maxGapMs = ($WindowEndMs - $WindowStartMs)
            gapKind = 'no-positive-interval-rows'
        }
    }

    # PowerShell variable names are case-insensitive -- an "$maxGapMs" LOCAL accumulator would be
    # the SAME variable slot as the "$MaxGapMs" PARAMETER above, silently overwriting the caller's
    # ceiling with the observed value and making "-le $MaxGapMs" compare the accumulator to itself
    # (always true). Named "$observedGapMs" specifically to avoid that collision.
    $observedGapMs = [double]($sorted[0] - $WindowStartMs)
    $gapKind = 'head'
    for ($i = 1; $i -lt $sorted.Count; $i++) {
        $gap = $sorted[$i] - $sorted[$i - 1]
        if ($gap -gt $observedGapMs) { $observedGapMs = $gap; $gapKind = 'internal' }
    }
    $tailGap = $WindowEndMs - $sorted[$sorted.Count - 1]
    if ($tailGap -gt $observedGapMs) { $observedGapMs = $tailGap; $gapKind = 'tail' }

    [pscustomobject]@{
        sufficient = ($observedGapMs -le $MaxGapMs)
        maxGapMs = $observedGapMs
        gapKind = $gapKind
    }
}

function ConvertTo-AttrCudaResultLineSafeText {
    <#
    .SYNOPSIS
    Sanitize free-form text (an exception message, a typed reason) for embedding inside a
    RESULT= stdout line's quoted REASON="..." field.
    .DESCRIPTION
    PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note): the RESULT line is read by naive downstream
    parsing (a regex over stdout text), never real shell quoting -- a literal '"' inside the
    embedded text (e.g. an exception message containing a quoted path or argument) would
    terminate that field early for such a parser. Quotes are substituted with a visually adjacent
    apostrophe rather than backslash-escaped, since nothing downstream unescapes backslashes.
    $null passes through unchanged, so callers do not need their own null guard first.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Text
    )
    if ($null -eq $Text) { return $Text }
    $Text.Replace('"', "'")
}

# ENFORCE-3 RECEIPT ORACLE for the attribution job. "20 s of real footage" is a SOURCE-FRAME
# quantity: the measured session's playback_smoke.summary line carries source_advanced (the distinct source frames
# the engine advanced) and required_source_frames (ceil(window x NATIVE fps)). The result is INVALID -- never a
# measurement -- when the line is absent, either figure is missing, the requirement is unknown, source_advanced is
# under it, the run was paced by a persisted fps override, the engine paced at anything but the footage's native fps,
# or the timeline wrapped. The decision is the SAME one gui-smoke-length-gate.ps1's Get-GuiSmokeSourceFramesVerdict
# makes for the smoke runner (the emitted job cannot dot-source that file); a class test executes both on one table.
function Get-AttrCudaSourceFramesVerdict {
    [CmdletBinding()]
    param([AllowNull()][AllowEmptyString()][string]$SummaryLine, [AllowNull()][AllowEmptyString()][string]$ExpectedRunNonce = '')
    $fields = @{}
    if (-not [string]::IsNullOrWhiteSpace($SummaryLine)) {
        foreach ($match in [regex]::Matches($SummaryLine, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) {
            $fields[$match.Groups['k'].Value] = $match.Groups['v'].Value
        }
    }
    $failures = @()
    # ENFORCE-4 r2 (sol BLOCKER): the receipt must be THIS run's. The nonce the app echoed on the summary line must be the one
    # the smoke runner generated for the run whose log this job read ($runLog.runNonce); a missing, mismatched or unbound nonce
    # is INVALID (the same rule as Get-GuiSmokeRunNonceFailure in gui-smoke-length-gate.ps1).
    if ($fields.Count -gt 0) {
        if ([string]::IsNullOrWhiteSpace($ExpectedRunNonce)) {
            $failures += "RECEIPT_NOT_THIS_RUN: the job bound no run nonce to the log it read, so no receipt can be shown to be this run's."
        } elseif (-not $fields.ContainsKey('run_nonce') -or [string]::IsNullOrWhiteSpace([string]$fields['run_nonce'])) {
            $failures += 'RECEIPT_NOT_THIS_RUN: the summary line carries no run_nonce (a build that predates it); it cannot be shown to be this run''s.'
        } elseif ([string]$fields['run_nonce'] -cne $ExpectedRunNonce) {
            $failures += 'RECEIPT_NOT_THIS_RUN: the summary line''s run_nonce is not the nonce the smoke runner generated for this run; an earlier run wrote it.'
        }
    }
    $advanced = $null
    $required = $null
    if ($fields.ContainsKey('source_advanced')) { $advanced = [int64]$fields['source_advanced'] }
    if ($fields.ContainsKey('required_source_frames')) { $required = [int64]$fields['required_source_frames'] }
    if ($fields.Count -eq 0) {
        $failures += 'INVALID_SOURCE_FRAMES: the run produced no playback_smoke.summary for the measured session, so no source frames can be proven.'
    } elseif ($null -eq $advanced -or $null -eq $required) {
        $failures += 'INVALID_SOURCE_FRAMES: RECEIPT_FIELD_ABSENT: playback_smoke.summary carries no source_advanced / required_source_frames (a build that predates ENFORCE-3); the footage played cannot be proven.'
    } elseif ($required -le 0) {
        $failures += "INVALID_SOURCE_FRAMES: required_source_frames=$required; the admitted window is unknown."
    } elseif ($advanced -lt $required) {
        $failures += "INVALID_SOURCE_FRAMES: the engine advanced source_advanced=$advanced distinct source frames but the Play had to consume required_source_frames=$required; under 20 s of real footage is never playback evidence."
    }
    # ENFORCE-4: the oracle re-derives the 20 s floor itself (see Get-GuiSmokeSourceFramesVerdict): a requirement under
    # ceil(20 s x native fps) was admitted for less than 20 s of footage, and a native fps of 0 cannot measure 20 s.
    if ($fields.ContainsKey('native_fps')) {
        $nativeForFloor = [double]::Parse($fields['native_fps'], [Globalization.CultureInfo]::InvariantCulture)
        if ($nativeForFloor -le 0) {
            $failures += "INVALID_SOURCE_FRAMES: native_fps=$nativeForFloor; the native frame rate is unknown, so 20 s of footage cannot be measured."
        } elseif ($null -ne $required -and $required -gt 0 -and $required -lt [int64][Math]::Ceiling(20.0 * $nativeForFloor - 0.02)) {
            $failures += "INVALID_SOURCE_FRAMES: required_source_frames=$required is under ceil(20 s x native_fps=$nativeForFloor); the Play was admitted for less than 20 s of footage."
        }
    }
    # ENFORCE-4: evidence is valid only when EVERY field the oracle judges is PRESENT. A summary that does not carry the
    # pace / override / wrap fields (a build that predates them) is INVALID, not "no override" / "pace unchecked" / "no wrap".
    $absentFields = @('native_fps', 'pace_fps', 'fps_override', 'wrapped', 'wrap_count' | Where-Object { $fields.Count -gt 0 -and -not $fields.ContainsKey($_) })
    if ($absentFields.Count -gt 0) {
        $failures += "INVALID_SOURCE_FRAMES: RECEIPT_FIELD_ABSENT: playback_smoke.summary carries no $($absentFields -join ' / '); the run's pace, override and wrapping cannot be proven."
    }
    if ($fields.Count -gt 0 -and $fields.ContainsKey('fps_override') -and [int]$fields['fps_override'] -ne 0) {
        $failures += 'INVALID_SOURCE_FRAMES: the run was paced by a persisted fps override; evidence is paced at the footage native fps.'
    }
    if ($fields.ContainsKey('pace_fps')) {
        $pace = [double]::Parse($fields['pace_fps'], [Globalization.CultureInfo]::InvariantCulture)
        if ($pace -le 0) {
            # ENFORCE-4 r2 (fable H5): a PRESENT pace that is not positive is unknown, never "pace unchecked".
            $failures += "INVALID_SOURCE_FRAMES: pace_fps=$pace; a present engine pace that is not positive is unknown, so wall clock cannot be tied to the footage played."
        } elseif ($fields.ContainsKey('native_fps')) {
            $native = [double]::Parse($fields['native_fps'], [Globalization.CultureInfo]::InvariantCulture)
            if ($native -gt 0 -and [Math]::Abs($pace - $native) -gt (0.005 * $native)) {
                $failures += "INVALID_SOURCE_FRAMES: the engine paced at pace_fps=$pace but the footage native fps is $native; 20 s of wall clock is not 20 s of footage."
            }
        }
    }
    $wrapped = $false
    if ($fields.ContainsKey('wrapped') -and [int]$fields['wrapped'] -ne 0) { $wrapped = $true }
    if ($fields.ContainsKey('wrap_count') -and [int64]$fields['wrap_count'] -gt 0) { $wrapped = $true }
    [pscustomobject]@{
        invalid = ($failures.Count -gt 0 -or $wrapped)
        failures = $failures
        wrapped = $wrapped
        sourceAdvanced = $advanced
        requiredSourceFrames = $required
    }
}

Export-ModuleMember -Function `
    Get-AttrCudaArtifactNames, `
    New-AttrCudaBuildInfoHeader, `
    Get-AttrCudaEmbeddedFunctionSource, `
    Expand-AttrCudaTemplate, `
    Get-AttrCudaZipArchiveComment, `
    Assert-AttrCudaSourceArchive, `
    Assert-AttrCudaSafeArtifactName, `
    Assert-AttrCudaDirectChild, `
    Assert-AttrCudaBuildManifest, `
    Assert-AttrCudaFixtureCommittedBytes, `
    Resolve-AttrCudaCommittedBlobId, `
    Save-AttrCudaCommittedBlobBytes, `
    Get-AttrCudaSmokeRunnerClosureManifest, `
    Get-AttrCudaScriptLoadSites, `
    Test-AttrCudaClosureScanExclusionMatch, `
    Assert-AttrCudaClosureComplete, `
    Resolve-AttrCudaSmokeRunnerClosure, `
    Get-AttrCudaClosureDigestHex, `
    Assert-AttrCudaWritableFileSlot, `
    Test-AttrCudaPathIsReparsePoint, `
    Get-AttrCudaClosureDirectoryMismatch, `
    Assert-AttrCudaNonOverwritingFileSlot, `
    Get-AttrCudaLegTimeBudget, `
    Add-AttrCudaTraceLine, `
    Get-AttrCudaFileSha256Blocks, `
    Read-AttrCudaBase64Payload, `
    Test-AttrCudaFootagePart, `
    ConvertTo-AttrCudaUtf8String, `
    Publish-AttrCudaBytes, `
    Publish-AttrCudaText, `
    Publish-AttrCudaFileCopy, `
    Publish-AttrCudaBoundedTextCopy, `
    Publish-AttrCudaPresentMonCaptureEvidence, `
    Get-AttrCudaPresentMonOrphanSessionName, `
    Get-AttrCudaPresentMonEventsLost, `
    Add-AttrCudaPresentMonEventsLostDetail, `
    Add-AttrCudaPresentMonEventsLostDetailToReport, `
    Get-AttrCudaEtsSessionListing, `
    Invoke-AttrCudaLogman, `
    Get-AttrCudaTextEncodingFromHead, `
    Stop-AttrCudaEtsSession, `
    Publish-AttrCudaFileMove, `
    Publish-AttrCudaFileMoveNonOverwriting, `
    Publish-AttrCudaDirectoryMoveNonOverwriting, `
    New-AttrCudaDirectory, `
    Remove-AttrCudaPartialFile, `
    Remove-AttrCudaInputFileByContent, `
    Assert-AttrCudaNoLinkBelowRoot, `
    Remove-AttrCudaTree, `
    New-AttrCudaOwnedRoot, `
    New-AttrCudaOwnedFileStream, `
    Add-AttrCudaOwnedRecord, `
    Read-AttrCudaOwnedJournal, `
    Get-AttrCudaOwnershipProof, `
    Initialize-AttrCudaFileIdNative, `
    Test-AttrCudaPresentMonTraceReady, `
    ConvertTo-AttrCudaFileIdObject, `
    Get-AttrCudaFileId, `
    Remove-AttrCudaFileByProof, `
    Remove-AttrCudaFileById, `
    Resolve-AttrCudaSmokeRunLog, `
    Get-AttrCudaLastEligibilityLine, `
    Get-AttrCudaEligibilityVerdict, `
    ConvertTo-AttrCudaQuotedProcessArgument, `
    Publish-AttrCudaContactSheetRawCaptures, `
    Get-AttrCudaPresentMonDisplayReport, `
    Get-AttrCudaAppSwapTelemetry, `
    Get-AttrCudaForegroundVerification, `
    Get-AttrCudaTemporalCoverage, `
    ConvertTo-AttrCudaResultLineSafeText, `
    Register-AttrCudaDisplayWakeNativeMethods, `
    Get-AttrCudaScreensaverRunning, `
    Get-AttrCudaScreensaverTimeoutSeconds, `
    Get-AttrCudaScreensaverActive, `
    Get-AttrCudaScreensaverSecure, `
    Invoke-AttrCudaBoundedProbe, `
    Wait-AttrCudaScreensaverDismissed, `
    Invoke-AttrCudaInputDesktopNudge, `
    Get-AttrCudaSessionLocked, `
    Start-AttrCudaDisplayWake, `
    Stop-AttrCudaDisplayWake, `
    Start-AttrCudaDisplayWakeKeepAlive, `
    Get-AttrCudaDisplayWakeKeepAliveHealth, `
    Stop-AttrCudaDisplayWakeKeepAlive, `
    Get-AttrCudaQuiescenceSample, `
    Get-AttrCudaProcessCpuSnapshot, `
    Get-AttrCudaTopCpuProcesses, `
    Get-AttrCudaWindowsDisplayInventory, `
    Get-AttrCudaMeasurementVenue, `
    Resolve-AttrCudaPreferredDisplay, `
    Get-AttrCudaDisplayDegradedState, `
    Get-AttrCudaGuiSmokeDisplaySelection, `
    Find-AttrCudaFailedSmokeDisplayLog, `
    Get-AttrCudaSourceFramesVerdict
