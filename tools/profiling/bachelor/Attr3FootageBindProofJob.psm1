# Attr3FootageBindProofJob.psm1 -- job-TEXT construction for the bounded owner-footage BIND PROOF:
# the footage presence/identity path of the attribution job and nothing after it. UM-OWNER-FOOTAGE-
# CROSS-VOLUME-1.
#
# WHAT THE EMITTED JOB DOES, ON THE MEASUREMENT HOST. It runs the attribution job's own owner-clip
# sequence with the SAME embedded functions (AttrCudaOwnerFootage.psm1 and AttrCudaArtifacts.psm1,
# extracted verbatim): the cheap screen of every part, the neutral-name contiguity proof, the
# private link directory (Resolve-AttrCudaOwnerFootageDirectory -- on the clip's volume when the
# work tree is elsewhere), one hard link per part with its same-file-object proof, a read-share
# handle held on each link, and the ONE full identity hash per part through that handle. It then
# builds the verified binding the runner would receive, proves a write-open of the link is refused,
# releases the held handles, and proves every owner part is STILL THERE, the same file object with
# the same length. It launches nothing, plays nothing and copies nothing, and it DELETES NO NAME
# OF THE OWNER'S FOOTAGE (UM-OWNER-FOOTAGE-CROSS-VOLUME-2): the private link names it created are
# left where they are and reported (integrity.leftover) so a later sweep can find them. Its whole
# stdout is path-free tokens, volume serials and the job-private link directory -- never an owner
# path.
#
# WHY -WorkRoot. The defect this proves fixed only exists when the work tree and the clip are on
# different volumes (Ultra-Magnus: the agent share and scratch are on G:, the clip under proof is
# on C:). The job's work tree is therefore placed by the caller, not assumed.
#
# Like Attr3FootagePresenceJob.psm1 this takes ALREADY-RESOLVED parts (the CLI obtains them from
# tools/gates/resolve_consented_clip.py alone) and embeds each path as base64 of its UTF-8 bytes.

Set-StrictMode -Version Latest

if (-not (Get-Command -Name 'Assert-AttrCudaSafeArtifactName' -ErrorAction SilentlyContinue)) {
    Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Global -ErrorAction Stop -Verbose:$false
}

function Get-Attr3BindProofLongPath {
    # Expand every 8.3 alias in an absolute path to its long name. GetLongPathNameW needs the path
    # to exist, so the deepest existing ancestor is expanded and the not-yet-existing tail (which
    # cannot be an alias) is appended unchanged.
    param([Parameter(Mandatory = $true)][string]$FullPath)

    if (-not ('Attr3BindProofNative' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class Attr3BindProofNative {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern uint GetLongPathNameW(string shortPath, StringBuilder longPath, uint cch);
}
"@
    }
    $existing = $FullPath
    $tail = [System.Collections.Generic.List[string]]::new()
    while ($existing -and -not (Test-Path -LiteralPath $existing)) {
        $tail.Insert(0, (Split-Path -Leaf $existing))
        $existing = Split-Path -Parent $existing
    }
    if (-not $existing) { throw "ATTR3_BINDPROOF_WORKROOT_UNRESOLVABLE no ancestor of the work root exists" }
    $buffer = [System.Text.StringBuilder]::new(1024)
    $needed = [Attr3BindProofNative]::GetLongPathNameW($existing, $buffer, [uint32]$buffer.Capacity)
    if ($needed -gt $buffer.Capacity) {
        $buffer = [System.Text.StringBuilder]::new([int]$needed + 1)
        $needed = [Attr3BindProofNative]::GetLongPathNameW($existing, $buffer, [uint32]$buffer.Capacity)
    }
    if ($needed -eq 0) { throw "ATTR3_BINDPROOF_WORKROOT_UNRESOLVABLE the long name of an ancestor could not be read" }
    $resolved = $buffer.ToString()
    foreach ($leaf in $tail) { $resolved = Join-Path $resolved $leaf }
    $resolved
}

function Resolve-Attr3BindProofWorkRoot {
    # -WorkRoot is validated HERE, on its resolved long path, and not by a parameter-level
    # ValidatePattern. The final pattern (no '~', no quote) is what keeps the emitted job's
    # single-quoted literal safe, but a hosted runner's temp directory is spelled with an 8.3
    # alias (C:\Users\RUNNER~1\...), which a pattern on the RAW argument refuses although it names
    # an ordinary directory. Order: refuse traversal on the raw text, normalise, expand the
    # aliases, prove the expansion rewrote only alias segments, then apply the strict pattern.
    param([Parameter(Mandatory = $true)][string]$WorkRoot)

    if ($WorkRoot -match '[\x00-\x1f]' -or $WorkRoot -notmatch '^[A-Za-z]:[\\/]') {
        throw "ATTR3_BINDPROOF_WORKROOT_INVALID the work root must be a drive-absolute path"
    }
    foreach ($segment in ($WorkRoot -split '[\\/]')) {
        if ($segment -eq '..') { throw "ATTR3_BINDPROOF_WORKROOT_TRAVERSAL the work root may not contain a '..' segment" }
    }
    $full = [IO.Path]::GetFullPath($WorkRoot).TrimEnd('\')
    $long = (Get-Attr3BindProofLongPath -FullPath $full).TrimEnd('\')

    # The expansion may only rewrite alias segments (those containing '~'); any other difference
    # (bar letter case) means the path did not resolve to what the caller named.
    $fullSegments = @($full -split '\\')
    $longSegments = @($long -split '\\')
    $redirected = $fullSegments.Count -ne $longSegments.Count
    if (-not $redirected) {
        for ($i = 0; $i -lt $fullSegments.Count; $i++) {
            if (-not $fullSegments[$i].Contains('~') `
                    -and -not $fullSegments[$i].Equals($longSegments[$i], [StringComparison]::OrdinalIgnoreCase)) {
                $redirected = $true
                break
            }
        }
    }
    if ($redirected) { throw "ATTR3_BINDPROOF_WORKROOT_REDIRECTED the work root did not resolve to the path named" }
    if ($long -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') {
        throw "ATTR3_BINDPROOF_WORKROOT_INVALID the resolved work root has a character outside the allowed set"
    }
    $long
}

function New-Attr3FootageBindProofJob {
    <#
    .SYNOPSIS
    Build and write a <jobId>.job.ps1 owner-footage bind proof from already-resolved parts.
    .DESCRIPTION
    Emitted job exit codes: 0 OWNER_FOOTAGE_BIND_PROVEN, 19 OWNER_FOOTAGE_NOT_VERIFIED, 20
    OWNER_PARTS_NOT_CONTIGUOUS, 21 OWNER_FOOTAGE_LINK_CROSS_VOLUME, 22 OWNER_FOOTAGE_LINK_FAILED,
    23 OWNER_FOOTAGE_BIND_NOT_PINNED (the held link accepted a write-open), 24
    OWNER_FOOTAGE_BIND_MISMATCH (the binding differs from the verified parts), 25
    OWNER_FOOTAGE_BIND_HASH_READS (not exactly one full read per part), 26
    OWNER_FOOTAGE_BIND_SOURCE_NOT_INTACT (an owner part is missing after the job, or is no longer
    the same file object with the same length), 4 anything else.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
        [string]$ClipId,

        [Parameter(Mandatory = $true)][object[]]$Parts,

        [Parameter(Mandatory = $true)][string]$OutDir,

        # No ValidatePattern: see Resolve-Attr3BindProofWorkRoot (8.3 aliases are expanded first).
        [Parameter(Mandatory = $true)][string]$WorkRoot
    )

    $WorkRoot = Resolve-Attr3BindProofWorkRoot -WorkRoot $WorkRoot
    if ($Parts.Count -eq 0) { throw "ATTR3_BINDPROOF_NO_PARTS zero parts supplied for '$ClipId'" }
    foreach ($part in $Parts) {
        $path = [string]$part.path
        if ($path -notmatch '^[A-Za-z]:[\\/]' -or $path -match '[\x00-\x1f]') {
            throw "ATTR3_BINDPROOF_PART_PATH_INVALID part $($part.index)"
        }
        if ($part.sha256 -notmatch '^[0-9a-f]{64}$') { throw "ATTR3_BINDPROOF_PART_SHA_INVALID part $($part.index)" }
        if ($part.length -isnot [long] -and $part.length -isnot [int]) { throw "ATTR3_BINDPROOF_PART_LENGTH_INVALID part $($part.index)" }
    }

    $partsForJob = @($Parts | Sort-Object { [int]$_.index } | ForEach-Object {
        [ordered]@{
            index = [int]$_.index
            pathBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$_.path))
            length = [int64]$_.length
            sha256 = [string]$_.sha256
        }
    })
    $partsJson = $partsForJob | ConvertTo-Json -Compress -Depth 5
    if ($partsForJob.Count -eq 1) { $partsJson = "[$partsJson]" }

    $attemptNonce = [guid]::NewGuid().ToString('N').Substring(0, 10)
    $jobId = "attr3-footage-bind-proof-$ClipId-$attemptNonce"
    [void](Assert-AttrCudaSafeArtifactName -Name "$jobId.job.ps1")

    $embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
        'Read-AttrCudaBase64Payload', 'ConvertTo-AttrCudaUtf8String', 'Add-AttrCudaTraceLine',
        'Get-AttrCudaFileSha256Blocks', 'Test-AttrCudaFootagePart', 'New-AttrCudaDirectory')
    $embeddedFunctions = $embeddedFunctions + "`r`n`r`n" + (Get-AttrCudaEmbeddedFunctionSource -ModulePath (Join-Path $PSScriptRoot 'AttrCudaOwnerFootage.psm1') -Name @(
        'Get-AttrCudaOwnerFootageNeutralName', 'Assert-AttrCudaOwnerPartsNaming', 'Get-AttrCudaFileIdentity',
        'New-AttrCudaOwnerFootageLink', 'Open-AttrCudaReadOnlyHandle', 'New-AttrCudaVerifiedClipBinding',
        'Close-AttrCudaOwnerFootageWorkspace', 'Resolve-AttrCudaOwnerFootageDirectory',
        'Remove-AttrCudaEmptyOwnerFootageDirectory', 'Get-AttrCudaOwnerFootageLeftoverRecord',
        # UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2: the directory pin (no delete primitive).
        'Initialize-AttrCudaPinNativeMethods', 'Get-AttrCudaNoFollowIdentity',
        'Test-AttrCudaPathAncestorsHaveNoReparsePoint', 'Get-AttrCudaOwnerFootagePinTable',
        'Get-AttrCudaOwnerFootagePinKey', 'Get-AttrCudaOwnerFootageDirectoryPin',
        'Register-AttrCudaOwnerFootageDirectoryPin', 'Register-AttrCudaOwnerFootageDirectoryPinBestEffort',
        'Test-AttrCudaOwnerFootageDirectoryPin'))

    $template = @'
$ErrorActionPreference = 'Stop'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$JobId = '__JOB_ID__'
$ClipId = '__CLIP_ID__'
$PartsJson = '__PARTS_JSON__'
$WorkRoot = '__WORK_ROOT__'

# --- verifiers, embedded VERBATIM from AttrCudaArtifacts.psm1 and AttrCudaOwnerFootage.psm1 ----
__EMBEDDED_FUNCTIONS__
# --- end embedded verifiers -------------------------------------------------------------------

function Say([string]$Message) { Write-Output "[$JobId] $Message" }

$handles = [System.Collections.Generic.List[object]]::new()
$work = $null
$preferred = $null
$linkDir = $null
$relocated = $false
$firstSource = $null
$sourceBefore = @()
$leftover = $null
$token = $null
$exitCode = 4
$proof = $null
try {
    $rawParts = @($PartsJson | ConvertFrom-Json)
    Say "START clip=$ClipId parts=$($rawParts.Count)"
    $decodedParts = [System.Collections.Generic.List[object]]::new()
    foreach ($rawPart in ($rawParts | Sort-Object { [int]$_.index })) {
        $decoded = Read-AttrCudaBase64Payload -Base64 $rawPart.pathBase64
        $partPath = ConvertTo-AttrCudaUtf8String -Bytes $decoded.bytes
        $status = Test-AttrCudaFootagePart -Path $partPath -ExpectedLength ([int64]$rawPart.length) -ExpectedSha256 ([string]$rawPart.sha256) -LengthOnly
        Say "screen part=$($rawPart.index) status=$status"
        if ($status -ne 'PASS') { $token = 'OWNER_FOOTAGE_NOT_VERIFIED'; $exitCode = 19; throw $token }
        [void]$decodedParts.Add([ordered]@{ index = [int]$rawPart.index; path = $partPath; length = [int64]$rawPart.length; sha256 = [string]$rawPart.sha256 })
    }
    # @() matters: a single-part clip comes back as one bare dictionary, and [0] on that indexes by position.
    try { $asserted = @(Assert-AttrCudaOwnerPartsNaming -Parts $decodedParts) } catch { $token = 'OWNER_PARTS_NOT_CONTIGUOUS'; $exitCode = 20; throw $token }
    $firstSource = $asserted[0].path
    # The safety property of this proof: what each owner part IS before anything is linked, so the
    # end of the job can prove every one is still there, the same file object, the same length.
    $sourceBefore = @($asserted | ForEach-Object {
        $before = Get-AttrCudaFileIdentity -Path $_.path
        [ordered]@{ path = $_.path; volume = $before.VolumeSerialNumber; high = $before.FileIndexHigh; low = $before.FileIndexLow; length = ([IO.FileInfo]::new($_.path)).Length }
    })

    $work = Join-Path $WorkRoot $JobId
    [void](New-Item -ItemType Directory -Path $work -Force)
    $preferred = New-AttrCudaDirectory -Path (Join-Path $work 'owner-clip')
    $workVolume = (Get-AttrCudaFileIdentity -Path $work).VolumeSerialNumber
    $sourceVolume = (Get-AttrCudaFileIdentity -Path $firstSource).VolumeSerialNumber

    $linkDir = Resolve-AttrCudaOwnerFootageDirectory -PreferredDirectory $preferred -SourcePath @($asserted | ForEach-Object { $_.path })
    $relocated = ($linkDir -ne $preferred)
    $leftover = Get-AttrCudaOwnerFootageLeftoverRecord -Directory $linkDir -Relocated $relocated -LinkName @($asserted | ForEach-Object { Get-AttrCudaOwnerFootageNeutralName -Index $_.index })
    $linkVolume = (Get-AttrCudaFileIdentity -Path $linkDir).VolumeSerialNumber
    Say "link directory relocated=$relocated workVolume=$workVolume sourceVolume=$sourceVolume linkVolume=$linkVolume"

    $verified = [System.Collections.Generic.List[object]]::new()
    $trace = Join-Path $work 'trace.txt'
    $hashSeconds = 0.0
    foreach ($part in $asserted) {
        $linkPath = New-AttrCudaOwnerFootageLink -Directory $linkDir -Index $part.index -SourcePath $part.path
        $handle = Open-AttrCudaReadOnlyHandle -Path $linkPath
        [void]$handles.Add($handle)
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $status = Test-AttrCudaFootagePart -Path $linkPath -ExpectedLength $part.length -ExpectedSha256 $part.sha256 -TracePath $trace -TraceLabel "footage-part$($part.index)-identity-hash"
        $hashSeconds += $watch.Elapsed.TotalSeconds
        Say "identity part=$($part.index) status=$status"
        if ($status -ne 'PASS') { $token = 'OWNER_FOOTAGE_NOT_VERIFIED'; $exitCode = 19; throw $token }
        [void]$verified.Add([ordered]@{ path = $linkPath; length = $part.length; sha256 = $part.sha256 })
    }
    $hashReads = @(Get-Content -LiteralPath $trace | Where-Object { $_ -match 'identity-hash done bytes=' }).Count

    $binding = New-AttrCudaVerifiedClipBinding -Parts @($verified) | ConvertFrom-Json
    $bindingMatches = ($binding.parts.Count -eq $verified.Count)
    for ($i = 0; $i -lt $verified.Count -and $bindingMatches; $i++) {
        $bindingMatches = ($binding.parts[$i].length -eq $verified[$i].length -and $binding.parts[$i].sha256 -eq $verified[$i].sha256.ToLowerInvariant())
    }

    # The runner honours a binding entry only while the file is pinned: a write-capable open that
    # tolerates every sharer must be REFUSED with a sharing violation (Win32 32). Never truncates.
    $pinned = $true
    foreach ($entry in $verified) {
        $probe = $null
        try {
            $probe = [IO.File]::Open($entry.path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
            $pinned = $false
        } catch [System.IO.IOException] {
            if (($_.Exception.HResult -band 0xFFFF) -ne 32) { $pinned = $false }
        } catch {
            $pinned = $false
        } finally {
            if ($probe) { $probe.Dispose() }
        }
    }
    if (-not $pinned) { $token = 'OWNER_FOOTAGE_BIND_NOT_PINNED'; $exitCode = 23; throw $token }

    $proof = [ordered]@{
        partCount = $verified.Count
        relocated = $relocated
        workVolume = $workVolume
        sourceVolume = $sourceVolume
        linkVolume = $linkVolume
        workOnAnotherVolumeThanSource = ($workVolume -ne $sourceVolume)
        linkOnSourceVolume = ($linkVolume -eq $sourceVolume)
        hashReads = $hashReads
        hashSeconds = [math]::Round($hashSeconds, 1)
        bindingMatches = $bindingMatches
        writeBlockedWhileHeld = $pinned
    }
    # Success is gated on every invariant the proof reports (sol r1 hardening), not just on nothing
    # having thrown: the proof object above is kept so the refusal still shows what was measured.
    if (-not $bindingMatches) { $token = 'OWNER_FOOTAGE_BIND_MISMATCH'; $exitCode = 24; throw $token }
    if ($hashReads -ne $verified.Count) { $token = 'OWNER_FOOTAGE_BIND_HASH_READS'; $exitCode = 25; throw $token }
    $exitCode = 0
} catch {
    if (-not $token) {
        $first = (([string]$_.Exception.Message) -split '\s+')[0]
        if ($first -eq 'OWNER_FOOTAGE_LINK_CROSS_VOLUME') { $token = $first; $exitCode = 21 }
        elseif ($first -match '^OWNER_FOOTAGE_LINK_FAILED$') { $token = $first; $exitCode = 22 }
        else { $token = 'OWNER_FOOTAGE_BIND_JOB_ERROR'; $exitCode = 4 }
    }
} finally {
    # UM-OWNER-FOOTAGE-CROSS-VOLUME-2: handles are released and NOTHING is deleted that could be a
    # name of the owner's footage. The link names stay (recorded in $leftover); the only removals
    # are of directories that are EMPTY, by a non-recursive delete the OS itself refuses otherwise.
    if ($linkDir) {
        Close-AttrCudaOwnerFootageWorkspace -Handles $handles -Directory $linkDir 3>$null
        [void](Remove-AttrCudaEmptyOwnerFootageDirectory -Directory $linkDir 3>$null)
    }
    if ($preferred -and $preferred -ne $linkDir) { [void](Remove-AttrCudaEmptyOwnerFootageDirectory -Directory $preferred 3>$null) }
}

$linksAfter = 'unknown'
if ($firstSource) { try { $linksAfter = (Get-AttrCudaFileIdentity -Path $firstSource).NumberOfLinks } catch { } }
# The real safety property: every owner part still exists under its own name, is the very file
# object it was before the job, and has the same length.
$sourcesIntact = $true
foreach ($before in $sourceBefore) {
    try {
        $now = Get-AttrCudaFileIdentity -Path $before.path
        if ($now.VolumeSerialNumber -ne $before.volume -or $now.FileIndexHigh -ne $before.high -or $now.FileIndexLow -ne $before.low -or ([IO.FileInfo]::new($before.path)).Length -ne $before.length) { $sourcesIntact = $false }
    } catch { $sourcesIntact = $false }
}
$workTreeLeft = if ($work) { Test-Path -LiteralPath $work } else { $false }
$linkDirLeft = if ($linkDir) { Test-Path -LiteralPath $linkDir } else { $false }
if ($leftover) { $leftover['leftover'] = $linkDirLeft }
$integrity = [ordered]@{ sourcesIntact = $sourcesIntact; sourceLinkCountAfter = $linksAfter; workTreeLeftover = $workTreeLeft; leftover = $leftover }
if ($exitCode -eq 0 -and -not $sourcesIntact) { $token = 'OWNER_FOOTAGE_BIND_SOURCE_NOT_INTACT'; $exitCode = 26 }
$result = if ($exitCode -eq 0) { 'OWNER_FOOTAGE_BIND_PROVEN' } else { $token }
if ($proof) { $proof['sourcesIntact'] = $sourcesIntact }
Write-Output "RESULT=$result CLIP=$ClipId"
Write-Output (([ordered]@{ schema = 'mlvapp.attr3-footage-bind-proof.v1'; jobId = $JobId; clipId = $ClipId; result = $result; exitCode = $exitCode; proof = $proof; integrity = $integrity }) | ConvertTo-Json -Compress -Depth 6)
exit $exitCode
'@

    $text = Expand-AttrCudaTemplate -Template $template -Tokens ([ordered]@{
        JOB_ID = $jobId
        CLIP_ID = $ClipId
        PARTS_JSON = $partsJson
        WORK_ROOT = $WorkRoot.Replace("'", "''")
        EMBEDDED_FUNCTIONS = $embeddedFunctions
    })

    if (-not (Test-Path -LiteralPath $OutDir)) { [void](New-Item -ItemType Directory -Path $OutDir -Force) }
    $OutDir = (Resolve-Path -LiteralPath $OutDir).Path
    $jobPath = Join-Path $OutDir "$jobId.job.ps1"
    [IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

    Write-Output "RESULT=FOOTAGE_BIND_PROOF_JOB_EMITTED CLIP=$ClipId PARTS=$($partsForJob.Count) JOB=$jobId"
    [pscustomobject]@{ jobFile = $jobPath; jobId = $jobId; clipId = $ClipId; partCount = $partsForJob.Count }
}

Export-ModuleMember -Function New-Attr3FootageBindProofJob
