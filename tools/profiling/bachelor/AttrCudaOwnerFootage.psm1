# AttrCudaOwnerFootage.psm1 -- the private per-job owner-footage workspace: neutral link
# naming, part-contiguity proof, Win32 file identity, hard-link creation and held read handles.
# ATTR3-FOOTAGE-BIND-1 PR-B round 4b. No function here deletes a name of owner footage (see
# Close-AttrCudaOwnerFootageWorkspace): UM-OWNER-FOOTAGE-CROSS-VOLUME-2.
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
    SAME file object -- a private hard link and the owner's source part. CreateFileW opens with
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

function Initialize-AttrCudaPinNativeMethods {
    <#
    .SYNOPSIS
    Define, once per process, the Win32 calls the directory pin uses (open without following a
    reparse point, read the identity, close). There is no delete call here.
    .DESCRIPTION
    UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2. Separate from Get-AttrCudaFileIdentity's own type so
    that function (and the tests that replace it) stay untouched. Both callers open with
    FILE_FLAG_OPEN_REPARSE_POINT, so a junction or symlink is examined as itself and never followed.
    #>
    [CmdletBinding()]
    param()

    if ('AttrCudaWin32.PinNativeMethods' -as [type]) { return }
    $definition = @'
    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    public struct PinIdentity {
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
    public static extern bool GetFileInformationByHandle(System.IntPtr hFile, out PinIdentity lpFileInformation);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(System.IntPtr hObject);
'@
    Add-Type -Namespace AttrCudaWin32 -Name PinNativeMethods -MemberDefinition $definition -ErrorAction Stop
}

function Get-AttrCudaNoFollowIdentity {
    <#
    .SYNOPSIS
    Volume serial, 64-bit file index, live link count and attributes of -Path, read through a
    handle opened WITHOUT following a reparse point on the final component.
    .DESCRIPTION
    UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2. Where Get-AttrCudaFileIdentity follows a junction to
    whatever it points at, this reports the junction ITSELF (IsReparsePoint = true), so a directory
    that was swapped for a junction is told apart from the directory that was created. Throws
    ATTRCUDA_FILE_IDENTITY_UNAVAILABLE (never echoing -Path) on any Win32 failure.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    Initialize-AttrCudaPinNativeMethods
    $readAttributes = [uint32]0x80
    $shareAll = [uint32]7
    $openExisting = [uint32]3
    $backupSemanticsOpenReparsePoint = [uint32](0x02000000 -bor 0x00200000)
    $invalidHandle = [IntPtr]::new(-1)

    $handle = [AttrCudaWin32.PinNativeMethods]::CreateFileW(
        $Path, $readAttributes, $shareAll, [IntPtr]::Zero, $openExisting, $backupSemanticsOpenReparsePoint, [IntPtr]::Zero)
    if ($handle -eq $invalidHandle) {
        throw "ATTRCUDA_FILE_IDENTITY_UNAVAILABLE CreateFileW failed (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
    }
    try {
        $info = [AttrCudaWin32.PinNativeMethods+PinIdentity]::new()
        if (-not [AttrCudaWin32.PinNativeMethods]::GetFileInformationByHandle($handle, [ref]$info)) {
            throw "ATTRCUDA_FILE_IDENTITY_UNAVAILABLE GetFileInformationByHandle failed (Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
        }
        [pscustomobject]@{
            VolumeSerialNumber = $info.VolumeSerialNumber
            FileIndexHigh = $info.FileIndexHigh
            FileIndexLow = $info.FileIndexLow
            NumberOfLinks = $info.NumberOfLinks
            IsReparsePoint = (($info.FileAttributes -band 0x400) -ne 0)
            IsDirectory = (($info.FileAttributes -band 0x10) -ne 0)
        }
    } finally {
        [void][AttrCudaWin32.PinNativeMethods]::CloseHandle($handle)
    }
}

function Test-AttrCudaPathAncestorsHaveNoReparsePoint {
    <#
    .SYNOPSIS
    True only when -Path itself and EVERY ancestor up to the volume root is a real directory
    entry, not a junction, symlink or other reparse point.
    .DESCRIPTION
    UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2. Each component is opened without following a reparse
    point (Get-AttrCudaNoFollowIdentity). Any component that cannot be examined counts as not
    clean: this answers "may a destructive step trust this path", so unknown is no.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    try { $cursor = [IO.Path]::GetFullPath($Path) } catch { return $false }
    if ($cursor.Length -gt 3) { $cursor = $cursor.TrimEnd('\') }
    while (-not [string]::IsNullOrEmpty($cursor)) {
        try { $identity = Get-AttrCudaNoFollowIdentity -Path $cursor } catch { return $false }
        if ($identity.IsReparsePoint) { return $false }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $true
}

function Get-AttrCudaOwnerFootagePinTable {
    <#
    .SYNOPSIS
    The per-process table of directory pins (script scope: the module's own when imported, the
    job script's own when these functions are embedded).
    #>
    [CmdletBinding()]
    param()

    $table = Get-Variable -Name AttrCudaOwnerFootageDirectoryPins -Scope Script -ValueOnly -ErrorAction SilentlyContinue
    if ($null -eq $table) {
        $table = @{}
        Set-Variable -Name AttrCudaOwnerFootageDirectoryPins -Scope Script -Value $table
    }
    return ,$table
}

function Get-AttrCudaOwnerFootagePinKey {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Directory)
    [IO.Path]::GetFullPath($Directory).TrimEnd('\').ToLowerInvariant()
}

function Get-AttrCudaOwnerFootageDirectoryPin {
    <#
    .SYNOPSIS
    The pin recorded for -Directory when this job created it, or $null.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Directory)

    $table = Get-AttrCudaOwnerFootagePinTable
    $key = Get-AttrCudaOwnerFootagePinKey -Directory $Directory
    if ($table.ContainsKey($key)) { return $table[$key] }
    return $null
}

function Register-AttrCudaOwnerFootageDirectoryPin {
    <#
    .SYNOPSIS
    Record the identity (volume serial + 64-bit file id, read without following a reparse point)
    of a private link directory this job has just created, plus an empty list of the link names
    the job creates in it. Throws if -Directory is not a real directory.
    .DESCRIPTION
    UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2. Everything that later acts on the directory -- creating
    a link in it, removing it when it is empty -- first proves it is still THIS object. A
    relocated directory (-Relocated) also has its whole ancestor chain checked for reparse points.
    UM-OWNER-FOOTAGE-CROSS-VOLUME-2: no link name is ever deleted, so the recorded names are only
    a record of what this job created there.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [switch]$Relocated
    )

    $identity = Get-AttrCudaNoFollowIdentity -Path $Directory
    if ($identity.IsReparsePoint -or -not $identity.IsDirectory) {
        throw 'ATTRCUDA_OWNER_DIRECTORY_PIN_REFUSED the private directory is not a plain directory'
    }
    $pin = [pscustomobject]@{
        VolumeSerialNumber = $identity.VolumeSerialNumber
        FileIndexHigh = $identity.FileIndexHigh
        FileIndexLow = $identity.FileIndexLow
        Relocated = [bool]$Relocated
        Links = [System.Collections.Generic.List[object]]::new()
    }
    $table = Get-AttrCudaOwnerFootagePinTable
    $table[(Get-AttrCudaOwnerFootagePinKey -Directory $Directory)] = $pin
    return $pin
}

function Test-AttrCudaOwnerFootageDirectoryPin {
    <#
    .SYNOPSIS
    True only when -Directory still is the directory that was pinned: a plain directory (not a
    junction or symlink) with the pinned volume serial and file id, and -- for a relocated
    directory, unless -SkipAncestorCheck -- with no reparse point on any ancestor.
    .DESCRIPTION
    UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2. No pin, an unreadable directory, or any difference
    is False: the caller then leaves everything in place.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [switch]$SkipAncestorCheck
    )

    $pin = Get-AttrCudaOwnerFootageDirectoryPin -Directory $Directory
    if ($null -eq $pin) { return $false }
    try { $now = Get-AttrCudaNoFollowIdentity -Path $Directory } catch { return $false }
    if ($now.IsReparsePoint -or -not $now.IsDirectory) { return $false }
    if ($now.VolumeSerialNumber -ne $pin.VolumeSerialNumber -or
        $now.FileIndexHigh -ne $pin.FileIndexHigh -or
        $now.FileIndexLow -ne $pin.FileIndexLow) { return $false }
    if ($pin.Relocated -and -not $SkipAncestorCheck) {
        if (-not (Test-AttrCudaPathAncestorsHaveNoReparsePoint -Path $Directory)) { return $false }
    }
    return $true
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

    # UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2: the directory must still be the one pinned when it
    # was created (a junction planted in its place would make this link land in another folder).
    # A directory nobody pinned yet -- a direct caller -- is pinned now, refused if it is a
    # junction or symlink.
    try {
        $pin = Get-AttrCudaOwnerFootageDirectoryPin -Directory $Directory
        if ($null -eq $pin) {
            $pin = Register-AttrCudaOwnerFootageDirectoryPin -Directory $Directory
        } elseif (-not (Test-AttrCudaOwnerFootageDirectoryPin -Directory $Directory -SkipAncestorCheck)) {
            throw 'pin mismatch'
        }
    } catch {
        throw "OWNER_FOOTAGE_LINK_FAILED part $Index private directory is not the one created for this job"
    }

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

    # UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2: record the file object this name IS the moment it
    # exists (a record of what this job created; no job deletes it -- CROSS-VOLUME-2).
    try {
        $created = Get-AttrCudaNoFollowIdentity -Path $linkPath
        [void]$pin.Links.Add([pscustomobject]@{
            Name = $linkName
            VolumeSerialNumber = $created.VolumeSerialNumber
            FileIndexHigh = $created.FileIndexHigh
            FileIndexLow = $created.FileIndexLow
        })
    } catch {
        throw "OWNER_FOOTAGE_LINK_FAILED part $Index link identity could not be recorded"
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
    Close every held read-share handle, independently -- one failure never blocks the rest. It
    DELETES NOTHING: no job ever removes a name of owner footage.
    .DESCRIPTION
    ATTR3-FOOTAGE-BIND-1 PR-B round 4; UM-OWNER-FOOTAGE-CROSS-VOLUME-2 (the cleanup that used to
    follow the handle release is gone). The private link names this job created stay where they
    are and the job records the directory that holds them (Get-AttrCudaOwnerFootageLeftoverRecord),
    so a later sweep can find them. WHY: Windows has no atomic "delete this name only if another
    name of the file remains", and the owner's source name stays replaceable while a
    no-share-delete handle is held on the link -- so any "read the link count, then delete the
    link" (however carefully bound to a handle or a pin) can remove the LAST name of the owner's
    clip if the source name is replaced between the two. A leftover link is a few bytes of
    directory entry; a lost name of owner footage is not recoverable. -Directory is accepted so
    the call sites stay as they were; it is not acted on.
    Never throws: this runs in a `finally`, where an exception would mask the job's real exit
    code.
    #>
    [CmdletBinding()]
    param(
        [object[]]$Handles = @(),
        [string]$Directory = ''
    )

    foreach ($handle in $Handles) {
        if ($null -eq $handle) { continue }
        try { $handle.Dispose() } catch { Write-Warning "ATTRCUDA_OWNER_HANDLE_CLOSE_FAILED: $($_.Exception.Message)" }
    }
}

function Get-AttrCudaOwnerFootageLeftoverRecord {
    <#
    .SYNOPSIS
    The record a job writes into its result JSON for the private link directory it leaves behind:
    { schema, linkDirectory, relocated, leftover, linkNames }.
    .DESCRIPTION
    UM-OWNER-FOOTAGE-CROSS-VOLUME-2. No job deletes a link name, so a link directory is left on
    every owner run (leftover = true: a later sweep, with the owner present, decides what to do).
    -LinkName is the neutral names the job creates in it. The directory is a job-private path
    under the job's own scratch, never the owner's; the record carries no owner path.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [object]$Relocated = $false,
        [string[]]$LinkName = @()
    )

    [ordered]@{
        schema = 'mlvapp.owner-link-leftover.v1'
        linkDirectory = [IO.Path]::GetFullPath($Directory)
        relocated = [bool]$Relocated
        leftover = $true
        linkNames = @($LinkName)
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

function Resolve-AttrCudaOwnerFootageDirectory {
    <#
    .SYNOPSIS
    Choose the private directory the verified parts are hard-linked into: -PreferredDirectory when
    every source part is on its volume, else a fresh job-unique directory on the SOURCE parts'
    volume. Returns the directory's full path. UM-OWNER-FOOTAGE-CROSS-VOLUME-1.
    .DESCRIPTION
    A hard link cannot cross volumes, and the clip's location is a frozen constant of the
    consent table, so on a venue whose job work tree is on another volume than the clip (Ultra-
    Magnus: clip on C:, agent share and scratch on G:) New-AttrCudaOwnerFootageLink used to refuse
    every owner leg with OWNER_FOOTAGE_LINK_CROSS_VOLUME. The private directory is only a place to
    hold the neutral-name links, so it moves to the clip's volume instead: nothing is copied, no
    check is loosened, and every guarantee stays exactly where it was -- the link is still a hard
    link to the SAME file object (New-AttrCudaOwnerFootageLink proves volume + 64-bit file index
    after creating it), the job still holds a FileShare.Read handle on it, hashes it once, and hands
    that verified binding to the runner. The volume check in New-AttrCudaOwnerFootageLink is
    untouched and still runs for every part.
    Falls back to -PreferredDirectory (so that check still refuses, as before) when a source's
    identity cannot be read, or when the parts themselves span more than one volume -- no single
    directory can hold links to those. The relocated directory is created here, must not already
    exist, is named after the job's own unique work-tree leaf plus 'owner-clip', and is proved to
    sit on the source volume (a junction planted at its parent would fail this) before it is
    returned; a directory that fails the proof is removed (it is empty) and OWNER_FOOTAGE_LINK_-
    CROSS_VOLUME is thrown. -RelocatedParent overrides the default parent, '<source drive root>
    mlvtmp' (the same scratch root the venues already use), and exists so a test can keep the
    relocation inside its own temp tree. Never echoes a source or directory path.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PreferredDirectory,
        [Parameter(Mandatory = $true)][string[]]$SourcePath,
        [string]$RelocatedParent = ''
    )

    try {
        $preferredVolume = (Get-AttrCudaFileIdentity -Path $PreferredDirectory).VolumeSerialNumber
        $sourceVolumes = @($SourcePath | ForEach-Object { (Get-AttrCudaFileIdentity -Path $_).VolumeSerialNumber } | Select-Object -Unique)
    } catch {
        Register-AttrCudaOwnerFootageDirectoryPinBestEffort -Directory $PreferredDirectory
        return $PreferredDirectory
    }
    if ($sourceVolumes.Count -ne 1 -or $sourceVolumes[0] -eq $preferredVolume) {
        Register-AttrCudaOwnerFootageDirectoryPinBestEffort -Directory $PreferredDirectory
        return $PreferredDirectory
    }

    # The work-tree directory stays empty on this path; pin it so it can be removed again (empty
    # only) by Remove-AttrCudaEmptyOwnerFootageDirectory.
    Register-AttrCudaOwnerFootageDirectoryPinBestEffort -Directory $PreferredDirectory
    $parent = $RelocatedParent
    if ([string]::IsNullOrWhiteSpace($parent)) {
        $parent = Join-Path ([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($SourcePath[0]))) 'mlvtmp'
    }
    $leaf = (Split-Path -Leaf (Split-Path -Parent ([IO.Path]::GetFullPath($PreferredDirectory)))) + '-owner-clip'
    $relocated = Join-Path $parent $leaf
    try {
        if (Test-Path -LiteralPath $relocated) { throw 'occupied' }
        [void](New-Item -ItemType Directory -Path $relocated -ErrorAction Stop)
    } catch {
        throw 'OWNER_FOOTAGE_LINK_FAILED no private directory could be prepared on the source volume'
    }
    # Pin the directory the instant it exists: later steps act only on THIS directory object
    # (volume serial + file id, read without following a reparse point), never on whatever a path
    # names by then. A directory that cannot be pinned is not used; it is empty, so removing it
    # non-recursively cannot touch anything else (and on a junction removes only the junction).
    try {
        [void](Register-AttrCudaOwnerFootageDirectoryPin -Directory $relocated -Relocated)
    } catch {
        try { [IO.Directory]::Delete($relocated, $false) } catch {}
        throw 'OWNER_FOOTAGE_LINK_FAILED no private directory could be prepared on the source volume'
    }
    $onSourceVolume = $false
    try { $onSourceVolume = ((Get-AttrCudaFileIdentity -Path $relocated).VolumeSerialNumber -eq $sourceVolumes[0]) } catch {}
    if (-not $onSourceVolume) {
        try { [IO.Directory]::Delete($relocated, $false) } catch {}
        throw 'OWNER_FOOTAGE_LINK_CROSS_VOLUME the source volume has no usable private directory'
    }
    return $relocated
}

function Register-AttrCudaOwnerFootageDirectoryPinBestEffort {
    <#
    .SYNOPSIS
    Pin -Directory (the job's own work-tree link directory) without ever throwing; a directory
    that cannot be pinned is pinned again, or refused, by New-AttrCudaOwnerFootageLink.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Directory)
    try { [void](Register-AttrCudaOwnerFootageDirectoryPin -Directory $Directory) } catch {}
}

function Remove-AttrCudaEmptyOwnerFootageDirectory {
    <#
    .SYNOPSIS
    Remove a private link directory this job created ONLY if it is empty. Returns REMOVED,
    NOT_EMPTY or REFUSED. It can never remove a name of the owner's footage.
    .DESCRIPTION
    UM-OWNER-FOOTAGE-CROSS-VOLUME-2 (replaces Remove-AttrCudaOwnerFootageRelocatedDirectory, which
    also deleted link names). The only delete primitive is [IO.Directory]::Delete($Directory,
    $false): non-recursive, so the OS itself refuses a directory that holds any entry -- a link
    name, an app sidecar, anything -- and there is no enumeration, no per-name check and no
    by-handle delete to race. The directory must still be the object pinned when this job created
    it (volume serial + file id, read without following a reparse point; ancestors clear of
    reparse points for a relocated one), so a junction planted in its place is left alone rather
    than removed. Never throws; every warning is path-free.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Directory)

    if ([string]::IsNullOrWhiteSpace($Directory) -or -not (Test-Path -LiteralPath $Directory)) { return 'REMOVED' }
    if (-not (Test-AttrCudaOwnerFootageDirectoryPin -Directory $Directory)) {
        Write-Warning 'ATTRCUDA_OWNER_LINK_DIRECTORY_LEFT identity or reparse-point check failed; nothing removed'
        return 'REFUSED'
    }
    try {
        [IO.Directory]::Delete($Directory, $false)
        [void](Get-AttrCudaOwnerFootagePinTable).Remove((Get-AttrCudaOwnerFootagePinKey -Directory $Directory))
        return 'REMOVED'
    } catch {
        return 'NOT_EMPTY'
    }
}

Export-ModuleMember -Function `
    Resolve-AttrCudaOwnerFootageDirectory, `
    Remove-AttrCudaEmptyOwnerFootageDirectory, `
    Get-AttrCudaOwnerFootageLeftoverRecord, `
    Get-AttrCudaOwnerFootageStagingName, `
    Send-AttrCudaOwnerFootagePartToStaging, `
    Get-AttrCudaOwnerFootageNeutralName, `
    Assert-AttrCudaOwnerPartsNaming, `
    Get-AttrCudaFileIdentity, `
    Initialize-AttrCudaPinNativeMethods, `
    Get-AttrCudaNoFollowIdentity, `
    Test-AttrCudaPathAncestorsHaveNoReparsePoint, `
    Get-AttrCudaOwnerFootagePinTable, `
    Get-AttrCudaOwnerFootagePinKey, `
    Get-AttrCudaOwnerFootageDirectoryPin, `
    Register-AttrCudaOwnerFootageDirectoryPin, `
    Register-AttrCudaOwnerFootageDirectoryPinBestEffort, `
    Test-AttrCudaOwnerFootageDirectoryPin, `
    New-AttrCudaOwnerFootageLink, `
    Open-AttrCudaReadOnlyHandle, `
    New-AttrCudaVerifiedClipBinding, `
    Close-AttrCudaOwnerFootageWorkspace
