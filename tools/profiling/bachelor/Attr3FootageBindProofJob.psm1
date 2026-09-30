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
# releases everything, and proves the owner's part has one name again. It launches nothing, plays
# nothing and copies nothing; its whole stdout is path-free tokens and volume serials.
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
    OWNER_FOOTAGE_BIND_CLEANUP_INCOMPLETE (the source's link count, the work tree or the link
    directory was not restored), 4 anything else.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
        [string]$ClipId,

        [Parameter(Mandatory = $true)][object[]]$Parts,

        [Parameter(Mandatory = $true)][string]$OutDir,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$')]
        [string]$WorkRoot
    )

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
        'Remove-AttrCudaOwnerFootageRelocatedDirectory',
        # UM-OWNER-FOOTAGE-CROSS-VOLUME-1 round 2: the directory pin and the by-handle link delete.
        'Initialize-AttrCudaPinNativeMethods', 'Get-AttrCudaNoFollowIdentity',
        'Test-AttrCudaPathAncestorsHaveNoReparsePoint', 'Get-AttrCudaOwnerFootagePinTable',
        'Get-AttrCudaOwnerFootagePinKey', 'Get-AttrCudaOwnerFootageDirectoryPin',
        'Register-AttrCudaOwnerFootageDirectoryPin', 'Register-AttrCudaOwnerFootageDirectoryPinBestEffort',
        'Test-AttrCudaOwnerFootageDirectoryPin', 'Remove-AttrCudaOwnerFootageLinkName',
        'Remove-AttrCudaOwnerFootageRecordedLinks'))

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

    $work = Join-Path $WorkRoot $JobId
    [void](New-Item -ItemType Directory -Path $work -Force)
    $preferred = New-AttrCudaDirectory -Path (Join-Path $work 'owner-clip')
    $workVolume = (Get-AttrCudaFileIdentity -Path $work).VolumeSerialNumber
    $sourceVolume = (Get-AttrCudaFileIdentity -Path $firstSource).VolumeSerialNumber

    $linkDir = Resolve-AttrCudaOwnerFootageDirectory -PreferredDirectory $preferred -SourcePath @($asserted | ForEach-Object { $_.path })
    $relocated = ($linkDir -ne $preferred)
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
    if ($linkDir) {
        Close-AttrCudaOwnerFootageWorkspace -Handles $handles -Directory $linkDir 3>$null
        if ($relocated) { Remove-AttrCudaOwnerFootageRelocatedDirectory -Directory $linkDir 3>$null }
    }
    try {
        if ($work -and (Test-Path -LiteralPath (Join-Path $work 'trace.txt'))) { Remove-Item -LiteralPath (Join-Path $work 'trace.txt') -Force -ErrorAction Stop }
        if ($preferred) { [IO.Directory]::Delete($preferred, $false) }
        if ($work) { [IO.Directory]::Delete($work, $false) }
    } catch { }
}

$linksAfter = 'unknown'
if ($firstSource) { try { $linksAfter = (Get-AttrCudaFileIdentity -Path $firstSource).NumberOfLinks } catch { } }
$workGone = if ($work) { -not (Test-Path -LiteralPath $work) } else { $true }
$linkDirGone = if ($linkDir) { -not (Test-Path -LiteralPath $linkDir) } else { $true }
if ($exitCode -eq 0 -and ($linksAfter -ne 1 -or -not $workGone -or -not $linkDirGone)) { $token = 'OWNER_FOOTAGE_BIND_CLEANUP_INCOMPLETE'; $exitCode = 26 }
$result = if ($exitCode -eq 0) { 'OWNER_FOOTAGE_BIND_PROVEN' } else { $token }
if ($proof) { $proof['sourceLinkCountAfterCleanup'] = $linksAfter; $proof['workTreeRemoved'] = $workGone; $proof['linkDirectoryRemoved'] = $linkDirGone }
Write-Output "RESULT=$result CLIP=$ClipId"
Write-Output (([ordered]@{ schema = 'mlvapp.attr3-footage-bind-proof.v1'; jobId = $JobId; clipId = $ClipId; result = $result; exitCode = $exitCode; proof = $proof }) | ConvertTo-Json -Compress -Depth 5)
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
