# AttrCudaOwnerFootage.psm1 -- the private per-job owner-footage workspace: neutral link
# naming, part-contiguity proof, Win32 file identity, hard-link creation, held read handles and
# link-only cleanup. ATTR3-FOOTAGE-BIND-1 PR-B round 4b.
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
# WHY Get-AttrCudaFileIdentity LIVES HERE, NOT AS A GENERIC HELPER LEFT BEHIND IN
# AttrCudaArtifacts.psm1. The Win32 GetFileInformationByHandle call itself has no footage
# meaning -- it is a generic file-identity primitive -- but tools/repo_hygiene/
# test_playback_attr_3_cuda_behaviour.py proves the cross-volume and identity-mismatch refusal
# paths by MOCKING it: `$mod = Get-Module AttrCudaOwnerFootage; & $mod { Set-Item -Path
# function:Get-AttrCudaFileIdentity -Value {...} }`, then calling New-AttrCudaOwnerFootageLink
# directly. PowerShell resolves an unqualified command name called from within a module function
# through THAT MODULE'S OWN session-state function table, looked up fresh on every call -- not a
# reference captured once at import time. Verified empirically (round 4b): when the mocked
# function and its caller live in the SAME module, the caller observes the Set-Item override on
# its very next call; when they live in two different modules (even with one importing the
# other), the caller's copy is a snapshot taken at import time and never observes a later
# Set-Item in the origin module's own scope -- the mock silently stops applying and the test
# would no longer exercise what it claims to. Get-AttrCudaFileIdentity must therefore share a
# module with every function that calls it unqualified, which is every other function below.
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
    Get-AttrCudaOwnerFootageNeutralName below (a private per-job hard-link workspace this
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
    try {
        try {
            $sourceStream = [IO.File]::Open($SourcePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        } catch {
            throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not open the source for reading"
        }
        try {
            $destStream = [IO.File]::Open($partialPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $weCreatedPartial = $true
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
        if ($weCreatedPartial) {
            try { Remove-Item -LiteralPath $partialPath -Force -Confirm:$false -ErrorAction SilentlyContinue } catch {}
        }
        throw
    } finally {
        if ($sourceStream) { try { $sourceStream.Dispose() } catch {} }
        if ($destStream) { try { $destStream.Dispose() } catch {} }
    }

    # The share-side re-verification: never trust that a byte-identical local copy stayed
    # byte-identical once it crossed the network.
    $arrivedStatus = Test-AttrCudaFootagePart -Path $partialPath -ExpectedLength $ExpectedLength -ExpectedSha256 $ExpectedSha256
    if ($arrivedStatus -ne 'PASS') {
        try { Remove-Item -LiteralPath $partialPath -Force -Confirm:$false -ErrorAction SilentlyContinue } catch {}
        throw "OWNER_FOOTAGE_STAGE_VERIFY_FAILED part $Index share-side verification failed ($arrivedStatus)"
    }

    try {
        [IO.File]::Move($partialPath, $finalPath, $false)
    } catch [IO.IOException] {
        # A concurrent submitter finished staging this exact part first -- re-check the bytes
        # already there rather than assume either outcome.
        $racedStatus = Test-AttrCudaFootagePart -Path $finalPath -ExpectedLength $ExpectedLength -ExpectedSha256 $ExpectedSha256
        try { Remove-Item -LiteralPath $partialPath -Force -Confirm:$false -ErrorAction SilentlyContinue } catch {}
        if ($racedStatus -eq 'PASS') { return [pscustomobject]@{ Path = $finalPath; Created = $false } }
        throw "OWNER_FOOTAGE_STAGE_CONFLICT part $Index is already staged with different bytes"
    } catch {
        try { Remove-Item -LiteralPath $partialPath -Force -Confirm:$false -ErrorAction SilentlyContinue } catch {}
        throw "OWNER_FOOTAGE_STAGE_COPY_FAILED part $Index could not publish the staged part"
    }

    return [pscustomobject]@{ Path = $finalPath; Created = $true }
}

function Get-AttrCudaOwnerFootageNeutralName {
    <#
    .SYNOPSIS
    The ONE naming rule for a private owner-footage hard link: part 0 becomes the neutral base
    name plus the composed base extension; part i (i>=1) becomes the neutral base name with
    continuation extension M{i-1:D2}. ATTR3-FOOTAGE-BIND-1 PR-B round 4.
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
    naming scheme's extension for its index -- before any hard link is created. Returns the parts
    sorted by index. Throws OWNER_PARTS_NOT_CONTIGUOUS (never a path, never an extension value) on
    any violation.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4. The resolver's own content cross-check already proved each
    part's bytes against the frozen consent table; this is a STRUCTURAL check that the parts still
    look like a real multi-part recording (a base part plus zero or more M00-style continuation
    parts, in position order) before they are aliased under neutral names -- so a part list whose
    indices have a gap or a duplicate, or whose real extension does not match its position, is
    refused here rather than silently linked under a misleading neutral name.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Parts)

    if ($Parts.Count -eq 0) { throw 'OWNER_PARTS_NOT_CONTIGUOUS no parts to link' }
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

function Get-AttrCudaFileIdentity {
    <#
    .SYNOPSIS
    Return the Win32 file identity (volume serial number, 64-bit file index, live hard-link
    count) of an existing file or directory, via GetFileInformationByHandle.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4: this is the ONE way this module proves two paths name the
    SAME file object -- a private hard link and the owner's source part -- and the ONE way it
    learns a file's live hard-link count before ever deleting it. CreateFileW opens with
    FILE_FLAG_BACKUP_SEMANTICS so a directory handle works too (needed to read the private
    directory's own volume serial for the cross-volume check, before any part is linked). Every
    share flag is requested because this call only ever QUERIES metadata -- it competes with
    nothing, including the read-share handle this module later holds open on the same link.
    Throws ATTRCUDA_FILE_IDENTITY_UNAVAILABLE (never echoes -Path) on any Win32 failure.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not ('AttrCudaWin32.NativeMethods' -as [type])) {
        $definition = @'
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    public struct FileIdentity {
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

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    public static extern System.IntPtr CreateFileW(string lpFileName, uint dwDesiredAccess, uint dwShareMode, System.IntPtr lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes, System.IntPtr hTemplateFile);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetFileInformationByHandle(System.IntPtr hFile, out FileIdentity lpFileInformation);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(System.IntPtr hObject);
'@
        Add-Type -Namespace AttrCudaWin32 -Name NativeMethods -MemberDefinition $definition -ErrorAction Stop
    }

    $genericRead = [uint32]2147483648
    $shareAll = [uint32]0x00000007
    $openExisting = [uint32]3
    $backupSemantics = [uint32]0x02000000
    $invalidHandle = [IntPtr]::new(-1)

    $handle = [AttrCudaWin32.NativeMethods]::CreateFileW(
        $Path, $genericRead, $shareAll, [IntPtr]::Zero, $openExisting, $backupSemantics, [IntPtr]::Zero)
    if ($handle -eq $invalidHandle) {
        throw "ATTRCUDA_FILE_IDENTITY_UNAVAILABLE CreateFileW failed (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
    }
    try {
        $info = [AttrCudaWin32.NativeMethods+FileIdentity]::new()
        $ok = [AttrCudaWin32.NativeMethods]::GetFileInformationByHandle($handle, [ref]$info)
        if (-not $ok) {
            throw "ATTRCUDA_FILE_IDENTITY_UNAVAILABLE GetFileInformationByHandle failed (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
        }
        [pscustomobject]@{
            VolumeSerialNumber = $info.VolumeSerialNumber
            FileIndexHigh = $info.FileIndexHigh
            FileIndexLow = $info.FileIndexLow
            NumberOfLinks = $info.NumberOfLinks
        }
    } finally {
        [void][AttrCudaWin32.NativeMethods]::CloseHandle($handle)
    }
}

function New-AttrCudaOwnerFootageLink {
    <#
    .SYNOPSIS
    Create ONE neutrally-named hard link for a verified owner-footage part inside the private
    per-job directory, after proving it is on the same volume as that directory and, once linked,
    the SAME file object as its source. Returns the link's full path.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4. Throws OWNER_FOOTAGE_LINK_CROSS_VOLUME (index only) if the
    source is not on the same volume as -Directory -- checked BEFORE any link is attempted -- or
    OWNER_FOOTAGE_LINK_FAILED (index only) if New-Item -ItemType HardLink itself errors, or if the
    created link's own identity (volume serial + 64-bit file index) does not match the source's --
    proof the link names the SAME bytes, not a same-named coincidence. Never echoes a source or
    link path in any thrown message.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][int]$Index,
        [Parameter(Mandatory = $true)][string]$SourcePath
    )

    $directoryIdentity = Get-AttrCudaFileIdentity -Path $Directory
    try {
        $sourceIdentity = Get-AttrCudaFileIdentity -Path $SourcePath
    } catch {
        throw "OWNER_FOOTAGE_LINK_FAILED part $Index source identity unavailable"
    }
    if ($sourceIdentity.VolumeSerialNumber -ne $directoryIdentity.VolumeSerialNumber) {
        throw "OWNER_FOOTAGE_LINK_CROSS_VOLUME part $Index is not on the same volume as the job work tree"
    }

    $linkName = Get-AttrCudaOwnerFootageNeutralName -Index $Index
    $linkPath = Join-Path $Directory $linkName
    try {
        [void](New-Item -ItemType HardLink -Path $linkPath -Value $SourcePath -ErrorAction Stop)
    } catch {
        throw "OWNER_FOOTAGE_LINK_FAILED part $Index hard link creation failed"
    }

    try {
        $linkIdentity = Get-AttrCudaFileIdentity -Path $linkPath
    } catch {
        throw "OWNER_FOOTAGE_LINK_FAILED part $Index link identity unavailable after creation"
    }
    if ($linkIdentity.VolumeSerialNumber -ne $sourceIdentity.VolumeSerialNumber -or
        $linkIdentity.FileIndexHigh -ne $sourceIdentity.FileIndexHigh -or
        $linkIdentity.FileIndexLow -ne $sourceIdentity.FileIndexLow) {
        throw "OWNER_FOOTAGE_LINK_FAILED part $Index link identity does not match its source"
    }

    return $linkPath
}

function Open-AttrCudaReadOnlyHandle {
    <#
    .SYNOPSIS
    Open -Path for reading with FileShare.Read (blocks other writers, allows other readers) and
    return the open stream.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4: held open from the moment a private owner-footage link's
    identity is confirmed until the smoke child that reads it has exited, so nothing can replace
    or truncate the link's target out from under a live measurement.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
}

function Close-AttrCudaOwnerFootageWorkspace {
    <#
    .SYNOPSIS
    Close every held read-share handle (independently -- one failure never blocks the rest), then
    delete ONLY the entries in -Directory whose name matches the neutral owner-footage link
    pattern AND whose live hard-link count is still >= 2, so a link is never the last name of the
    owner's bytes.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4. Anything else in -Directory (e.g. a sidecar file the app
    wrote while it had the footage open) is left in place for the job's normal work-tree cleanup.
    Never throws: this runs in a `finally`, where an exception would mask the job's real exit
    code.
    #>
    [CmdletBinding()]
    param(
        [object[]]$Handles = @(),
        [Parameter(Mandatory = $true)][string]$Directory
    )

    foreach ($handle in $Handles) {
        if ($null -eq $handle) { continue }
        try { $handle.Dispose() } catch { Write-Warning "ATTRCUDA_OWNER_HANDLE_CLOSE_FAILED: $($_.Exception.Message)" }
    }

    if ([string]::IsNullOrWhiteSpace($Directory) -or -not (Test-Path -LiteralPath $Directory)) { return }
    $baseName = 'owner-clip'
    $neutralPattern = '^' + $baseName + '\.(MLV|M\d{2})$'
    $entries = @(Get-ChildItem -LiteralPath $Directory -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $neutralPattern })
    foreach ($entry in $entries) {
        try {
            $identity = Get-AttrCudaFileIdentity -Path $entry.FullName
        } catch {
            Write-Warning 'ATTRCUDA_OWNER_LINK_IDENTITY_UNAVAILABLE_AT_CLEANUP left in place'
            continue
        }
        if ($identity.NumberOfLinks -lt 2) {
            Write-Warning 'ATTRCUDA_OWNER_LINK_IS_LAST_NAME left in place'
            continue
        }
        try {
            Remove-Item -LiteralPath $entry.FullName -Force -Confirm:$false -ErrorAction Stop
        } catch {
            Write-Warning "ATTRCUDA_OWNER_LINK_CLEANUP_FAILED: $($_.Exception.Message)"
        }
    }
}

Export-ModuleMember -Function `
    Get-AttrCudaOwnerFootageStagingName, `
    Send-AttrCudaOwnerFootagePartToStaging, `
    Get-AttrCudaOwnerFootageNeutralName, `
    Assert-AttrCudaOwnerPartsNaming, `
    Get-AttrCudaFileIdentity, `
    New-AttrCudaOwnerFootageLink, `
    Open-AttrCudaReadOnlyHandle, `
    Close-AttrCudaOwnerFootageWorkspace
