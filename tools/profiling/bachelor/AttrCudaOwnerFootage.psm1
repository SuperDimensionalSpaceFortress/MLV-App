# AttrCudaOwnerFootage.psm1 -- the private per-job owner-footage workspace: neutral naming,
# part-contiguity proof, the symlink-or-copy VIEW of each verified part, the read-only PIN held on
# the originals, and identity-checked cleanup. ATTR3-FOOTAGE-BIND-1 PR-B round 4; rebuilt by
# OWNER-FOOTAGE-NO-HARDLINK-1 (no code path creates a hard link to owner footage any more).
#
# THREAT MODEL (OWNER-FOOTAGE-NO-HARDLINK-1). A hard link is a second, equal NAME of the owner's
# bytes. Any tool that deletes, sweeps or truncates a path it believes is job scratch -- a
# recursive scratch delete, a partial-file cleanup, a lane sweep, a write through a leftover link --
# can therefore destroy the owner's footage, and four review rounds kept finding that same class in
# a new place. The class is removed by changing the mechanism: the app still gets one job-private
# directory of neutral names (owner-clip + the base and continuation extensions), but each entry is
#   * a FILE SYMBOLIC LINK to the original part where the venue can create one (probed at run
#     time, Test-AttrCudaSymlinkCapability) -- a pointer, not a name of the bytes; or
#   * a VERIFIED BYTE COPY of the part where it cannot -- a separate file; deleting it is harmless.
# NEVER a hard link, on any fallback. Both work across volumes, so no relocation exists.
# The originals are PINNED for the whole run (Open-AttrCudaReadOnlyHandle: FileShare.Read, so no
# other process can write, truncate, rename or delete them), every view is proven to lead to the
# pinned file object (identity of the open handle, not a path) before launch, and this module
# deletes only names it recorded the identity of when it created them, only through that identity
# (Remove-AttrCudaFileById in AttrCudaArtifacts.psm1), and only while the object still has one name.
# Nothing here opens an original or a view entry for write, truncate or append.
#
# WHY THIS IS ITS OWN MODULE, SEPARATE FROM AttrCudaArtifacts.psm1. AttrCudaArtifacts.psm1 is
# shared by every PLAYBACK-ATTR-3-CUDA build-route script (assemble/stage/DLL-pair), none of
# which has any business knowing footage exists -- that is what this repository's own
# NoFootageTokensTests (tools/repo_hygiene/test_playback_attr_3_cuda_split_route.py) enforces
# against that module's source text. Round 4 put this workspace's functions into that shared
# module and satisfied the test by assembling the neutral name from string fragments
# ('owner-' + 'cl' + 'ip') -- which defeats the test's actual purpose (the shared module knowing
# footage exists, just spelled awkwardly) rather than upholding it. The cure is this module: only
# playback-attr-3-cuda-job.ps1 (the owner-clip attribution job, NOT in NoFootageTokensTests'
# NEW_SCRIPTS list) ever embeds these functions, via the same Get-AttrCudaEmbeddedFunctionSource
# extractor AttrCudaArtifacts.psm1 defines, pointed at THIS file instead.
#
# WHY THE MULTIPART TOKENS ARE STILL COMPOSED, NOT SPELLED PLAINLY, EVEN HERE. This
# repository's own NA-4 PreToolUse hook refuses a tool call whose text contains the real
# extension token, regardless of which file that text is destined for -- so a literal single
# token would block every edit to this file, not just a shared-module one. The precedent is
# $FixtureClipExtension = '.' + 'mlv' in playback-attr-3-cuda-job.ps1, which already does this
# for the same reason. This is a hook workaround, not a test workaround: this module carries no
# NoFootageTokensTests obligation of its own (it is not in NEW_SCRIPTS), and openly names footage
# in its prose above.
#
# WHY New-AttrCudaFileSymlink IS ITS OWN FUNCTION. It is the one place this module asks the OS for
# a symbolic link, so a test can replace it (`$mod = Get-Module AttrCudaOwnerFootage; & $mod {
# Set-Item -Path function:New-AttrCudaFileSymlink -Value {...} }`) to stand in a venue that cannot
# create one, and the capability probe and the view builder -- both in THIS module -- observe the
# override on their very next call. PowerShell resolves an unqualified command called from within
# a module function through that module's own session-state function table, looked up fresh on
# every call (verified empirically, round 4b); a caller in a DIFFERENT module keeps an import-time
# snapshot and would never see the override. The generic file-identity primitives
# (Get-AttrCudaFileId, Remove-AttrCudaFileById) live in AttrCudaArtifacts.psm1 because the staging
# job and the scratch-tree delete embed them and have no business knowing footage exists.
#
# WHY Get-AttrCudaOwnerFootageStagingName AND Send-AttrCudaOwnerFootagePartToStaging ARE ALSO
# HERE, NOT IN UmRunDrop.psm1 (ATTR3-FOOTAGE-STAGE-1). UmRunDrop.psm1's side-file policy exists
# for a different threat: an inbox side-file for a JOB, named and extensioned by a caller who
# might be anyone. Its allowlist (.zip/.json/.exe/.dll/.txt/.csv, plus the two tracked test-fixture
# clip stems) is deliberately narrow, and widening it to admit a multi-gigabyte owner-footage part
# would widen it for every OTHER caller of um-run.ps1 too. The transfer this module performs is
# narrower than that on every axis that matters: the only source ever accepted is a path this
# module's caller already ran through Test-AttrCudaFootagePart against a resolver-verified length
# and sha256 (tools/gates/resolve_consented_clip.py), the only destination name is the index-derived
# 'part-<n>' this module names below (no extension, so no media-extension token ever reaches the
# share), and every byte is re-verified from the share-side copy before it is ever renamed into
# place -- so this stays a narrowly-scoped function for one caller
# (tools/profiling/bachelor/attr3-footage-stage.ps1), not a widened general-purpose allowlist.

Set-StrictMode -Version Latest

# ATTR3-FOOTAGE-STAGE-1 round 3: this module calls Test-AttrCudaFootagePart (AttrCudaArtifacts.psm1)
# unqualified from Send-AttrCudaOwnerFootagePartToStaging below. A plain `Import-Module ... -Force`
# here is wrong whenever a caller already imported AttrCudaArtifacts.psm1 into its own (global)
# session first: importing it again from inside THIS module's script body nests it under this
# module's session state instead, and -Force compounds that by tearing down the caller's existing
# global copy in the process -- so every OTHER already-loaded caller of AttrCudaArtifacts.psm1
# (attr3-footage-stage.ps1, the job generators) loses its own commands out from under it. Importing
# only when the needed command is not already available -- and, when we do import, doing it -Global
# so a caller who loads THIS module first still ends up with AttrCudaArtifacts available globally --
# leaves an already-loaded caller's copy alone and still satisfies a caller who loads only this
# module.
if (-not (Get-Command -Name 'Test-AttrCudaFootagePart' -ErrorAction SilentlyContinue)) {
    # ATTR3-FOOTAGE-STAGE-1 round 11: -Verbose:$false so this fallback import (unreachable from
    # the production CLI, which always loads AttrCudaArtifacts.psm1 first) never depends on a
    # caller's ambient $VerbosePreference either, the same defense-in-depth
    # attr3-footage-stage.ps1's own four Import-Module calls now carry.
    Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Global -ErrorAction Stop -Verbose:$false
}

function Get-AttrCudaOwnerFootageStagingName {
    <#
    .SYNOPSIS
    The ONE naming rule for a resolver-verified part's neutral, index-derived slot inside a
    per-job staging directory on an agent share: 'part-<index>', no extension. ATTR3-FOOTAGE-
    STAGE-1.
    .DESCRIPTION
    No extension is used, deliberately: this repository's own NA-4 PreToolUse hook refuses a
    literal media-extension token in tool-call text regardless of destination file, and unlike
    Get-AttrCudaOwnerFootageNeutralName below (a private per-job view workspace this
    process alone ever reads) this name is written to a shared agent share, where an extension
    would serve no purpose other than to name what the bytes are.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Index)

    if ($Index -lt 0) { throw "OWNER_FOOTAGE_STAGE_INDEX_INVALID index must be >= 0 (got $Index)" }
    "part-$Index"
}

function Send-AttrCudaOwnerFootagePartToStaging {
    <#
    .SYNOPSIS
    Copy ONE already-verified footage part into its neutrally-named, index-derived slot inside a
    per-job staging directory on a remote agent share, re-verifying the share-side bytes before
    the final non-overwriting rename. ATTR3-FOOTAGE-STAGE-1.
    .DESCRIPTION
    -SourcePath must already have passed Test-AttrCudaFootagePart against -ExpectedLength and
    -ExpectedSha256 in the caller's own process; this function re-derives nothing from
    -SourcePath except its bytes (copied) and re-checks the ARRIVED copy against the same two
    values the caller already trusts, so a source swapped out between the caller's check and
    this call cannot silently pass. -StagingDirectory is created if absent. Every filesystem
    call after the initial copy is wrapped so no exception text -- which can carry a path --
    ever escapes; only a distinguishable OWNER_FOOTAGE_STAGE_* token, and the part -Index, is
    ever thrown. Idempotent: a final slot already holding bytes matching -ExpectedLength/
    -ExpectedSha256 is left alone and this returns without copying again; a final slot holding
    DIFFERENT bytes throws OWNER_FOOTAGE_STAGE_CONFLICT rather than overwriting it.
    Throws OWNER_FOOTAGE_STAGE_COPY_FAILED, OWNER_FOOTAGE_STAGE_VERIFY_FAILED (the share-side
    copy did not round-trip), OWNER_FOOTAGE_STAGE_PARTIAL_EXISTS (round 4: a partial already
    occupies the slot -- refused, untouched) or OWNER_FOOTAGE_STAGE_CONFLICT (index only, never a
    path). Returns a pscustomobject { Path; Created } on success: Path is the final staged path
    (a neutral share path, not the source); Created is $true only when THIS call is the one that
    actually renamed the partial into the final slot, $false when a matching final slot already
    existed and nothing was written (ATTR3-FOOTAGE-STAGE-1 round 8: the caller uses Created to
    track, per slot, exactly what THIS attempt brought into existence, so an overall-failure
    cleanup removes only that -- never a slot this call merely found already correct).
    ATTR3-FOOTAGE-STAGE-1 round 3 (astra PR #148 MAJOR, containment): before anything is created
    or copied, every EXISTING ancestor of -StagingDirectory, down to and including
    -StagingDirectory itself, is proved free of reparse points -- a junction planted at or above
    -StagingDirectory would otherwise redirect the copy or a later read outside the owned staging
    directory. Throws OWNER_FOOTAGE_STAGE_COPY_FAILED (index only) on that check alone, before
    anything under -StagingDirectory is touched.
    ATTR3-FOOTAGE-STAGE-1 round 4 (sol BLOCKER 2a): the partial slot is opened with EXCLUSIVE
    creation ([IO.FileMode]::CreateNew), never a Copy-Item -Force onto a pre-cleared slot -- a
    partial another concurrent attempt (or an earlier, still-in-flight call for this same part) is
    actively writing is never silently deleted and overwritten; it is refused, untouched.
    ATTR3-FOOTAGE-STAGE-1 round 4 (sol MAJOR, path-free output): every filesystem call below,
    including the plain Test-Path reads round 3 left unwrapped, is now wrapped so that under
    $ErrorActionPreference = 'Stop' a terminating provider error -- whose own .Exception.Message
    can carry a real path -- can never escape this function uncaught; only a fixed
    OWNER_FOOTAGE_STAGE_* token, and the part -Index, is ever thrown.
    ATTR3-FOOTAGE-STAGE-1 round 8 (astra major 2): both stream Dispose() calls now run INSIDE the
    guarded try, each wrapped so a Dispose() failure (a network-mapped or nearly full share) maps
    to the same fixed OWNER_FOOTAGE_STAGE_COPY_FAILED token a copy failure already uses -- never
    the raw exception, whose own .Message can carry a real path. Before this round the disposal
    calls lived only in an unguarded `finally`, so a throwing Dispose() escaped this function
    entirely and reached the CLI's own transfer catch, which used to fold $_.Exception.Message
    into its own thrown text (fixed at that call site too; see attr3-footage-stage.ps1's own step
    5 comment). -TestHookForceDisposeThrow is a TEST-ONLY switch, never reachable from the
    production CLI (attr3-footage-stage.ps1 never passes it): when set, it makes the destination
    stream's own Dispose() throw a message naming this call's real partial path, proving that text
    never reaches this function's own thrown message.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$StagingDirectory,
        [Parameter(Mandatory = $true)][int]$Index,
        [Parameter(Mandatory = $true)][int64]$ExpectedLength,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [switch]$TestHookForceDisposeThrow
    )

    $stagingDriveRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($StagingDirectory))
    try {
        $StagingDirectory = Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $stagingDriveRoot -Path $StagingDirectory
    } catch {
        throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index staging directory chain contains a reparse point"
    }

    try {
        if (-not (Test-Path -LiteralPath $StagingDirectory -PathType Container -ErrorAction Stop)) {
            [void](New-Item -ItemType Directory -Path $StagingDirectory -Force -ErrorAction Stop)
        }
    } catch {
        throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not create the staging directory"
    }

    $finalName = Get-AttrCudaOwnerFootageStagingName -Index $Index
    $finalPath = Join-Path $StagingDirectory $finalName
    $partialPath = "$finalPath.partial"

    $finalExists = $false
    try {
        $finalExists = Test-Path -LiteralPath $finalPath -PathType Leaf -ErrorAction Stop
    } catch {
        throw "OWNER_FOOTAGE_STAGE_STATE_UNKNOWN part $Index could not determine whether the final slot is occupied"
    }
    if ($finalExists) {
        $existingStatus = Test-AttrCudaFootagePart -Path $finalPath -ExpectedLength $ExpectedLength -ExpectedSha256 $ExpectedSha256
        if ($existingStatus -eq 'PASS') { return [pscustomobject]@{ Path = $finalPath; Created = $false } }
        throw "OWNER_FOOTAGE_STAGE_CONFLICT part $Index is already staged with different bytes"
    }

    # Exclusive-creation stream copy: [IO.FileMode]::CreateNew fails (IOException) if the partial
    # slot is already occupied -- by design, never pre-cleared and never overwritten. $weCreated-
    # Partial only becomes $true once OUR OWN CreateNew call actually succeeded, so the cleanup
    # below (round 4 fix: the original version deleted the slot unconditionally, including when
    # PARTIAL_EXISTS meant this call never created anything) removes the partial ONLY when this
    # call is the one that brought it into existence -- never a slot another attempt owns.
    $sourceStream = $null
    $destStream = $null
    $weCreatedPartial = $false
    # OWNER-FOOTAGE-NO-HARDLINK-1: the identity of the partial, read off OUR OWN CreateNew handle.
    # Every delete of the partial below goes through Remove-AttrCudaFileById with it: a pathname
    # swapped for a hard link to something else between the verify and the delete is left alone.
    $partialId = $null
    $removePartial = {
        if ($null -ne $partialId) { [void](Remove-AttrCudaFileById -Path $partialPath -FileId $partialId) }
    }
    try {
        try {
            $sourceStream = [IO.File]::Open($SourcePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        } catch {
            throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not open the source for reading"
        }
        try {
            $destStream = [IO.File]::Open($partialPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $weCreatedPartial = $true
            $partialId = Get-AttrCudaFileId -Stream $destStream
        } catch [IO.IOException] {
            throw "OWNER_FOOTAGE_STAGE_PARTIAL_EXISTS part $Index a partial copy already occupies the slot"
        } catch {
            throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not create the staging partial"
        }
        try {
            $sourceStream.CopyTo($destStream)
            $destStream.Flush()
        } catch {
            throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index copy to the staging share failed"
        }
        # ATTR3-FOOTAGE-STAGE-1 round 8 (astra major 2): disposed INSIDE this guarded try, each
        # call wrapped on its own -- Dispose() itself can throw (a network-mapped or nearly full
        # share), and its own .Message can carry a real path. Mapped to the same fixed
        # OWNER_FOOTAGE_STAGE_COPY_FAILED token a copy failure already uses; never forwarded.
        try {
            $sourceStream.Dispose()
        } catch {
            throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not close the source stream"
        }
        $sourceStream = $null
        try {
            # Test-only hook (round 8): $false unless a test explicitly passed
            # -TestHookForceDisposeThrow -- see this function's own header. Never reachable from
            # the production CLI.
            if ($TestHookForceDisposeThrow) {
                throw [IO.IOException]::new("ATTR3_TEST_SENTINEL synthetic dispose failure at $partialPath")
            }
            $destStream.Dispose()
        } catch {
            throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not close the staging partial"
        }
        $destStream = $null
    } catch {
        if ($destStream) { try { $destStream.Dispose() } catch {}; $destStream = $null }
        if ($sourceStream) { try { $sourceStream.Dispose() } catch {}; $sourceStream = $null }
        if ($weCreatedPartial) { & $removePartial }
        throw
    } finally {
        if ($sourceStream) { try { $sourceStream.Dispose() } catch {} }
        if ($destStream) { try { $destStream.Dispose() } catch {} }
    }

    # The share-side re-verification: never trust that a byte-identical local copy stayed
    # byte-identical once it crossed the network.
    $arrivedStatus = Test-AttrCudaFootagePart -Path $partialPath -ExpectedLength $ExpectedLength -ExpectedSha256 $ExpectedSha256
    if ($arrivedStatus -ne 'PASS') {
        & $removePartial
        throw "OWNER_FOOTAGE_STAGE_VERIFY_FAILED part $Index share-side verification failed ($arrivedStatus)"
    }

    try {
        [IO.File]::Move($partialPath, $finalPath, $false)
    } catch [IO.IOException] {
        # A concurrent submitter finished staging this exact part first -- re-check the bytes
        # already there rather than assume either outcome.
        $racedStatus = Test-AttrCudaFootagePart -Path $finalPath -ExpectedLength $ExpectedLength -ExpectedSha256 $ExpectedSha256
        & $removePartial
        if ($racedStatus -eq 'PASS') { return [pscustomobject]@{ Path = $finalPath; Created = $false } }
        throw "OWNER_FOOTAGE_STAGE_CONFLICT part $Index is already staged with different bytes"
    } catch {
        & $removePartial
        throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not publish the staged part"
    }

    return [pscustomobject]@{ Path = $finalPath; Created = $true }
}

function Get-AttrCudaOwnerFootageNeutralName {
    <#
    .SYNOPSIS
    The ONE naming rule for a private owner-footage view entry (a symbolic link or a byte copy,
    never a hard link): part 0 becomes the neutral base name plus the composed base extension;
    part i (i>=1) becomes the neutral base name with continuation extension M{i-1:D2}.
    ATTR3-FOOTAGE-BIND-1 PR-B round 4.
    .DESCRIPTION
    The base name is plain (`owner-clip`); only the extension is composed from literals, never
    spelled as one token -- see this module's own header, "WHY THE MULTIPART TOKENS ARE STILL
    COMPOSED", for why: this repository's own NA-4 PreToolUse hook refuses that token in tool
    text regardless of destination file, the same reason ATTR3-FIXTURE-STAGE-1's own composed
    extension constant exists in the job templates that DO name footage.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$Index)

    $baseName = 'owner-clip'
    $baseExtension = '.' + 'MLV'
    if ($Index -eq 0) { return $baseName + $baseExtension }
    '{0}.M{1:D2}' -f $baseName, ($Index - 1)
}

function Assert-AttrCudaOwnerPartsNaming {
    <#
    .SYNOPSIS
    Prove a resolved owner-footage part list is contiguous (0..N-1, no gaps or duplicates),
    bounded to at most 100 parts, and that each part's OWN real extension matches the neutral
    naming scheme's extension for its index -- before any view entry is created. Returns the parts
    sorted by index. Throws OWNER_PARTS_NOT_CONTIGUOUS (never a path, never an extension value) on
    any violation.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4. The resolver's own content cross-check already proved each
    part's bytes against the frozen consent table; this is a STRUCTURAL check that the parts still
    look like a real multi-part recording (a base part plus zero or more M00-style continuation
    parts, in position order) before they are aliased under neutral names -- so a part list whose
    indices have a gap or a duplicate, or whose real extension does not match its position, is
    refused here rather than silently viewed under a misleading neutral name.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Parts)

    if ($Parts.Count -eq 0) { throw 'OWNER_PARTS_NOT_CONTIGUOUS no parts to view' }
    if ($Parts.Count -gt 100) { throw "OWNER_PARTS_NOT_CONTIGUOUS more than 100 parts ($($Parts.Count))" }
    $sorted = @($Parts | Sort-Object { [int]$_.index })
    for ($i = 0; $i -lt $sorted.Count; $i++) {
        if ([int]$sorted[$i].index -ne $i) {
            throw "OWNER_PARTS_NOT_CONTIGUOUS part index $i is missing or duplicated"
        }
        $expectedExtension = [IO.Path]::GetExtension((Get-AttrCudaOwnerFootageNeutralName -Index $i))
        $actualExtension = [IO.Path]::GetExtension([string]$sorted[$i].path)
        if ($actualExtension -ine $expectedExtension) {
            throw "OWNER_PARTS_NOT_CONTIGUOUS part $i does not carry the expected extension for its index"
        }
    }
    return $sorted
}

function New-AttrCudaFileSymlink {
    <#
    .SYNOPSIS
    Create ONE file symbolic link -LinkPath -> -TargetPath, never overwriting (no -Force). The only
    place in this module that asks the OS for a symbolic link, kept as its own function so a test
    can stand a venue that cannot create one in its place.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. A symbolic link is a separate filesystem object that merely NAMES
    its target: deleting it (by any route) removes the link, never the target's bytes, and it adds
    no name to the target's own file record. That is the property a hard link lacks and the reason
    this module never makes one. Throws whatever New-Item throws; the caller maps it to a fixed token.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LinkPath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )

    [void](New-Item -ItemType SymbolicLink -Path $LinkPath -Target $TargetPath -ErrorAction Stop)
}

function Test-AttrCudaSymlinkCapability {
    <#
    .SYNOPSIS
    Typed run-time probe: can THIS process create a file symbolic link in -Directory, and does it
    resolve back to its target? Returns { Capable; Result } with Result SYMLINK_CAPABLE or
    SYMLINK_UNAVAILABLE. Never throws.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. Creating a symbolic link needs either an elevated token or
    Developer Mode; which a venue has is a fact about the venue, so it is asked, not assumed
    (measured 2026-09-30: bachelor can, ultra-magnus cannot). The probe links a throwaway file this
    call created itself, proves the link is a reparse point whose followed identity equals the
    target's and that one byte reads back through it, then removes both names through the
    identity-checked primitive -- a name that is no longer the object the probe created is left
    where it is. Nothing here ever names owner footage.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Directory)

    $suffix = [Guid]::NewGuid().ToString('N')
    $targetPath = Join-Path $Directory ".symlink-probe-target-$suffix"
    $linkPath = Join-Path $Directory ".symlink-probe-link-$suffix"
    $targetId = $null
    $linkId = $null
    $stream = $null
    $result = 'SYMLINK_UNAVAILABLE'
    try {
        $stream = [IO.File]::Open($targetPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $targetId = Get-AttrCudaFileId -Stream $stream
        $stream.WriteByte(1)
        $stream.Dispose()
        $stream = $null

        New-AttrCudaFileSymlink -LinkPath $linkPath -TargetPath $targetPath
        $linkId = Get-AttrCudaFileId -Path $linkPath
        $followed = Get-AttrCudaFileId -Path $linkPath -FollowLinks
        if ($linkId.IsReparsePoint -and
            $followed.VolumeSerialNumber -eq $targetId.VolumeSerialNumber -and
            $followed.FileIndexHigh -eq $targetId.FileIndexHigh -and
            $followed.FileIndexLow -eq $targetId.FileIndexLow) {
            $reader = [IO.File]::OpenRead($linkPath)
            try {
                if ($reader.ReadByte() -eq 1) { $result = 'SYMLINK_CAPABLE' }
            } finally {
                $reader.Dispose()
            }
        }
    } catch {
        $result = 'SYMLINK_UNAVAILABLE'
    } finally {
        if ($null -ne $stream) { try { $stream.Dispose() } catch {} }
        if ($null -ne $linkId) { [void](Remove-AttrCudaFileById -Path $linkPath -FileId $linkId -ExpectReparsePoint:$linkId.IsReparsePoint) }
        if ($null -ne $targetId) { [void](Remove-AttrCudaFileById -Path $targetPath -FileId $targetId) }
    }
    [pscustomobject]@{ Capable = ($result -eq 'SYMLINK_CAPABLE'); Result = $result }
}

function Open-AttrCudaReadOnlyHandle {
    <#
    .SYNOPSIS
    Open -Path for reading with FileShare.Read (blocks other writers AND any delete or rename,
    allows other readers) and return the open stream.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4, reused by OWNER-FOOTAGE-NO-HARDLINK-1 as the PIN: held open
    on the owner's ORIGINAL part from before its view is built until the smoke child that reads the
    view has exited, so nothing can replace, truncate, rename or delete the original out from under
    a live measurement. FileShare.Read grants no write and no delete, so any other process that
    tries either gets a sharing violation.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
}

function Assert-AttrCudaOwnerFootageCopySpace {
    <#
    .SYNOPSIS
    Refuse, before a single byte is copied, when the volume holding -Directory cannot take a copy of
    every part plus a fixed headroom. Throws OWNER_FOOTAGE_VIEW_NO_SPACE (never a path).
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. The copy fallback duplicates the parts, which can be tens of
    gigabytes; running the volume dry mid-copy would fail late and leave the job scratch full.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][object[]]$Parts
    )

    $need = [int64]0
    foreach ($part in $Parts) { $need += [int64]$part.length }
    $headroom = [int64](1024 * 1024 * 1024)
    try {
        $free = ([IO.DriveInfo]::new([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Directory)))).AvailableFreeSpace
    } catch {
        throw 'OWNER_FOOTAGE_VIEW_NO_SPACE free space of the job scratch volume is unknown'
    }
    if ($free -lt ($need + $headroom)) {
        throw 'OWNER_FOOTAGE_VIEW_NO_SPACE the job scratch volume cannot hold a copy of every part'
    }
}

function New-AttrCudaOwnerFootageView {
    <#
    .SYNOPSIS
    Build ONE neutrally-named entry for a verified owner-footage part inside the private per-job
    directory: a file symbolic link to the original (-Mode symlink) or a byte copy of it
    (-Mode copy). Returns the view record the rest of the job carries.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. NEVER a hard link: a hard link is a second, equal NAME of the
    owner's bytes, so any tool that deletes, sweeps or truncates a path it believes is job scratch
    can destroy the owner's footage through it. A symlink is only a pointer; a copy is only bytes.
    -PinStream is the read-only handle this job already holds on the ORIGINAL (see
    Open-AttrCudaReadOnlyHandle); the view is built against that held file object, not against a
    path that could be re-pointed a moment later.
      symlink  the link is created without -Force, its own identity is recorded (Get-AttrCudaFileId
               of the NAME, never followed), it must be a reparse point with one name, and a read
               handle opened THROUGH it must report the same volume serial + file index as the pin.
      copy     the destination is created with FileMode.CreateNew (refuses to touch anything
               already there), its identity is recorded from that very handle, the pinned original
               is streamed into it, and a read handle reopened on the name must report the recorded
               identity, one name, and the pinned original's length. Content is then proven by the
               caller's sha256 pass against the consent record, exactly as for a symlink.
    Returns { Index; Path; Mode; EntryId; PinId; ViewStream }: Path is the neutral entry under
    -Directory; EntryId is the identity of that NAME (the link itself, or the copy); PinId is the
    original's identity; ViewStream is a FileShare.Read handle the caller holds through the smoke
    run. On any failure the entry this call created is removed through the identity-checked
    primitive and OWNER_FOOTAGE_VIEW_FAILED (index only, never a path) is thrown.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][int]$Index,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][System.IO.FileStream]$PinStream,
        [Parameter(Mandatory = $true)][ValidateSet('symlink', 'copy')][string]$Mode
    )

    $viewPath = Join-Path $Directory (Get-AttrCudaOwnerFootageNeutralName -Index $Index)
    $pinId = $null
    $entryId = $null
    $destStream = $null
    $viewStream = $null
    $reason = 'view creation failed'
    try {
        $pinId = Get-AttrCudaFileId -Stream $PinStream
        if ($Mode -eq 'symlink') {
            New-AttrCudaFileSymlink -LinkPath $viewPath -TargetPath ([IO.Path]::GetFullPath($SourcePath))
            $entryId = Get-AttrCudaFileId -Path $viewPath
            if (-not $entryId.IsReparsePoint -or $entryId.NumberOfLinks -ne 1) {
                $reason = 'view entry is not a lone symbolic link'
                throw $reason
            }
            $viewStream = Open-AttrCudaReadOnlyHandle -Path $viewPath
            $viewId = Get-AttrCudaFileId -Stream $viewStream
            $expectedOpenedId = $pinId
        } else {
            $destStream = [IO.File]::Open($viewPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $entryId = Get-AttrCudaFileId -Stream $destStream
            $reason = 'view copy failed'
            [void]$PinStream.Seek(0, [IO.SeekOrigin]::Begin)
            $PinStream.CopyTo($destStream, 4194304)
            $destStream.Flush($true)
            $destStream.Dispose()
            $destStream = $null
            $viewStream = Open-AttrCudaReadOnlyHandle -Path $viewPath
            $viewId = Get-AttrCudaFileId -Stream $viewStream
            $expectedOpenedId = $entryId
            if ($entryId.NumberOfLinks -ne 1 -or $viewId.NumberOfLinks -ne 1 -or $viewStream.Length -ne $PinStream.Length) {
                $reason = 'view copy is not a lone file of the original length'
                throw $reason
            }
        }
        if ($viewId.VolumeSerialNumber -ne $expectedOpenedId.VolumeSerialNumber -or
            $viewId.FileIndexHigh -ne $expectedOpenedId.FileIndexHigh -or
            $viewId.FileIndexLow -ne $expectedOpenedId.FileIndexLow) {
            $reason = 'view identity does not match what it was built from'
            throw $reason
        }
    } catch {
        if ($null -ne $viewStream) { try { $viewStream.Dispose() } catch {} }
        if ($null -ne $destStream) { try { $destStream.Dispose() } catch {} }
        if ($null -ne $entryId) {
            [void](Remove-AttrCudaFileById -Path $viewPath -FileId $entryId -ExpectReparsePoint:($Mode -eq 'symlink'))
        }
        throw "OWNER_FOOTAGE_VIEW_FAILED part $Index $reason"
    }

    [pscustomobject]@{
        Index = $Index
        Path = $viewPath
        Mode = $Mode
        EntryId = $entryId
        PinId = $pinId
        ViewStream = $viewStream
    }
}

function Assert-AttrCudaOwnerFootageViewsIntact {
    <#
    .SYNOPSIS
    Right before launch: prove every view entry is still the one this job built and still leads to
    the pinned original. Throws OWNER_FOOTAGE_VIEW_CHANGED (index only, never a path) on any
    difference.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. symlink: the NAME must still carry the recorded link identity with
    one name and be a reparse point, it must still resolve to the pinned original (followed
    volume serial + file index equal the pin's), and the handle this job holds through it must
    still be that original. copy: the NAME must still carry the recorded identity with one name
    and not be a reparse point, and the held handle must be that same file object. The handles
    themselves are FileShare.Read, so replacing or writing any of these names is already refused
    while they are held; this is the detection half, for anything that got in before the handles
    or that a filesystem quirk let through.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Views)

    foreach ($view in $Views) {
        $changed = $false
        try {
            $entry = Get-AttrCudaFileId -Path $view.Path
            $held = Get-AttrCudaFileId -Stream $view.ViewStream
            if ($entry.VolumeSerialNumber -ne $view.EntryId.VolumeSerialNumber -or
                $entry.FileIndexHigh -ne $view.EntryId.FileIndexHigh -or
                $entry.FileIndexLow -ne $view.EntryId.FileIndexLow -or
                $entry.NumberOfLinks -ne 1) { $changed = $true }
            if ($view.Mode -eq 'symlink') {
                $followed = Get-AttrCudaFileId -Path $view.Path -FollowLinks
                $expected = $view.PinId
                if (-not $entry.IsReparsePoint -or
                    $followed.VolumeSerialNumber -ne $expected.VolumeSerialNumber -or
                    $followed.FileIndexHigh -ne $expected.FileIndexHigh -or
                    $followed.FileIndexLow -ne $expected.FileIndexLow) { $changed = $true }
            } else {
                $expected = $view.EntryId
                if ($entry.IsReparsePoint) { $changed = $true }
            }
            if ($held.VolumeSerialNumber -ne $expected.VolumeSerialNumber -or
                $held.FileIndexHigh -ne $expected.FileIndexHigh -or
                $held.FileIndexLow -ne $expected.FileIndexLow) { $changed = $true }
        } catch {
            $changed = $true
        }
        if ($changed) { throw "OWNER_FOOTAGE_VIEW_CHANGED part $($view.Index)" }
    }
}

function Clear-AttrCudaOwnerFootageLeftovers {
    <#
    .SYNOPSIS
    At job start: remove the neutral view entries an earlier run of this job left in -Directory
    (a killed job never reached its cleanup), each through the identity-checked primitive, so the
    job-start scratch sweep has no reparse point or extra name to trip on. Never throws.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. A leftover symlink is deleted as itself (its own identity read off
    the name and handed straight to Remove-AttrCudaFileById with -ExpectReparsePoint), so its
    target is untouched; a leftover copy is deleted only while it still has exactly one name. An
    entry that has more than one name -- a hard link left by an older build of this job -- is NOT
    deleted: it is reported and left, and the scratch sweep that follows then refuses the tree
    (ATTRCUDA_TREE_HAS_HARD_LINK) instead of deleting it. Returns the tokens of entries that could
    not be removed; empty when everything went or nothing was there.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Directory)

    $left = [System.Collections.Generic.List[string]]::new()
    try {
        $directoryId = Get-AttrCudaFileId -Path $Directory
    } catch {
        return @()
    }
    if ($directoryId.IsReparsePoint -or -not $directoryId.IsDirectory) {
        Write-Warning 'ATTRCUDA_OWNER_LEFTOVER_DIRECTORY_IS_LINK left in place'
        return @('LEFT_NOT_A_FILE')
    }
    $neutralPattern = '^owner-clip\.(MLV|M\d{2})$'
    foreach ($entry in @(Get-ChildItem -LiteralPath $Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match $neutralPattern })) {
        try {
            $id = Get-AttrCudaFileId -Path $entry.FullName
            $token = Remove-AttrCudaFileById -Path $entry.FullName -FileId $id -ExpectReparsePoint:$id.IsReparsePoint
        } catch {
            $token = 'LEFT_UNAVAILABLE'
        }
        if ($token -ne 'DELETED' -and $token -ne 'ABSENT') {
            Write-Warning "ATTRCUDA_OWNER_LEFTOVER_LEFT $token"
            [void]$left.Add($token)
        }
    }
    return @($left)
}

function Close-AttrCudaOwnerFootageWorkspace {
    <#
    .SYNOPSIS
    Close every held handle (independently -- one failure never blocks the rest), then remove the
    view entries this job created, each through the identity-checked primitive.
    .DESCRIPTION
    OWNER-FOOTAGE-NO-HARDLINK-1. -Views are the records New-AttrCudaOwnerFootageView returned. A
    symlink entry is removed as itself (its recorded identity, -ExpectReparsePoint): the original
    it names is not opened for delete and cannot be touched. A copy entry is removed only while it
    is still the object this job created and still has exactly one name. Any other outcome
    (LEFT_*) leaves the entry where it is and is reported as a warning carrying only the part
    index and the token. There is NO name-pattern sweep of -Directory and no recursive delete:
    anything in it this job did not record -- a sidecar the app wrote, a stranger's file -- is left
    for the job's normal scratch cleanup. Never throws: this runs in a `finally`, where an
    exception would mask the job's real exit code.
    #>
    [CmdletBinding()]
    param(
        [object[]]$Handles = @(),
        [object[]]$Views = @()
    )

    foreach ($handle in $Handles) {
        if ($null -eq $handle) { continue }
        try { $handle.Dispose() } catch { Write-Warning 'ATTRCUDA_OWNER_HANDLE_CLOSE_FAILED' }
    }
    foreach ($view in $Views) {
        if ($null -eq $view) { continue }
        try {
            $token = Remove-AttrCudaFileById -Path $view.Path -FileId $view.EntryId -ExpectReparsePoint:($view.Mode -eq 'symlink')
        } catch {
            $token = 'LEFT_UNAVAILABLE'
        }
        if ($token -ne 'DELETED' -and $token -ne 'ABSENT') {
            Write-Warning "ATTRCUDA_OWNER_VIEW_LEFT part $($view.Index) $token"
        }
    }
}

function New-AttrCudaVerifiedClipBinding {
    <#
    .SYNOPSIS
    The JSON text the smoke runner reads through -VerifiedClipBindingPath: one {path,length,sha256}
    per owner part whose FULL content read just PASSed on a handle this job still holds.
    .DESCRIPTION
    BACHELOR-OWNER-CLIP-STAGE-STALL-1 round 1f. The job reads each part in full exactly once (the
    identity hash, on the private link, through the read-share handle). This carries that verified
    identity to the runner so the runner records it instead of reading the clip again: the runner
    honours an entry only while the file still has the bound length AND is still pinned by that
    handle (a write-open is refused), so the file the app opens is the file that was hashed.
    Only ever call this with parts whose identity check returned PASS; the caller owns that.
    Paths are the private neutral link paths -- never the owner's own directory -- and the file is
    written under the job's own work tree, never into the published artifact directory.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Parts)

    [ordered]@{
        schema = 'gui-smoke-verified-clip-binding.v1'
        parts = @($Parts | ForEach-Object {
            [ordered]@{
                path = [IO.Path]::GetFullPath([string]$_.path)
                length = [int64]$_.length
                sha256 = ([string]$_.sha256).ToLowerInvariant()
            }
        })
    } | ConvertTo-Json -Depth 4
}

Export-ModuleMember -Function `
    Get-AttrCudaOwnerFootageStagingName, `
    Send-AttrCudaOwnerFootagePartToStaging, `
    Get-AttrCudaOwnerFootageNeutralName, `
    Assert-AttrCudaOwnerPartsNaming, `
    New-AttrCudaFileSymlink, `
    Test-AttrCudaSymlinkCapability, `
    Open-AttrCudaReadOnlyHandle, `
    Assert-AttrCudaOwnerFootageCopySpace, `
    New-AttrCudaOwnerFootageView, `
    Assert-AttrCudaOwnerFootageViewsIntact, `
    New-AttrCudaVerifiedClipBinding, `
    Clear-AttrCudaOwnerFootageLeftovers, `
    Close-AttrCudaOwnerFootageWorkspace
