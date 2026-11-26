param(
    [Parameter(Mandatory=$true)]
    [string]$Before,
    [Parameter(Mandatory=$true)]
    [string]$After,
    [string]$Output = "",
    [double]$MaxMeanAbsRgbDelta = 2.0,
    [double]$MaxChangedSampleRatio = 0.01,
    [int]$SampleStep = 4,
    [switch]$FailOnScreenshotDelta
)

$ErrorActionPreference = "Stop"

# Get-ColorArtifactFailingVerdicts is the single source of truth for which
# colorArtifactScan.verdict values fail a leg -- see gui-smoke-color-artifact-scan.ps1.
# Sourcing it here (rather than hand-keeping a second copy) is what closes PR147 round 3's
# gap: this comparator's own list used to omit capture-invalid and capture-too-dark, so an
# A/B leg with an unusable or artifacted capture could still report PASS.
. (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (sol r5 B1): the host-load block is judged by the venue reader's ONE strict rule (VenueChain.psm1
# ConvertFrom-VenueSmokeHostLoad and Get-VenueReferenceHostLoadRefusal), never by a second, looser parser here.
Import-Module (Join-Path $PSScriptRoot 'dual-venue\VenueChain.psm1') -Function ConvertFrom-VenueSmokeHostLoad, Get-VenueReferenceHostLoadRefusal, ConvertFrom-VenueSmokeResultText -DisableNameChecking

function Read-SmokeJson {
    param([string]$Path)

    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    if (-not (Test-Path -LiteralPath $resolved)) {
        throw "Smoke JSON not found: $resolved"
    }
    Get-Content -LiteralPath $resolved -Raw | ConvertFrom-Json -Depth 100
}

function Get-SmokeFileIdentity {
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (adversary-07b6a932.json A6): the physical identity of an OPEN file: on Windows the volume serial
    # number and the 64-bit file index of the handle (GetFileInformationByHandle), equal for every hard link and every spelling of one file and
    # different for two files; elsewhere the device and inode the runtime reports for the path. A handle whose identity cannot be read throws, so the
    # caller records a typed read failure rather than guessing.
    param([IO.FileStream]$Stream, [string]$ResolvedPath)

    if ($IsWindows -or $null -eq $IsWindows) {
        # adversary-88890049.json A1: the native struct has 4-byte alignment (its FILETIMEs are two DWORDs), so it is declared Pack = 4: with the default
        # 8-byte alignment of a long the volume serial and file index were read from the wrong offsets (VolumeSerialNumber at 32 instead of 28). The type
        # is versioned so a session holding the old layout never reuses it.
        if (-not ('MlvSmokeFileIdentityV2' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class MlvSmokeFileIdentityV2 {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    private struct BY_HANDLE_FILE_INFORMATION {
        public uint FileAttributes; public long CreationTime; public long LastAccessTime; public long LastWriteTime;
        public uint VolumeSerialNumber; public uint FileSizeHigh; public uint FileSizeLow; public uint NumberOfLinks;
        public uint FileIndexHigh; public uint FileIndexLow;
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out BY_HANDLE_FILE_INFORMATION info);
    public static string Of(SafeFileHandle handle) {
        BY_HANDLE_FILE_INFORMATION info;
        if (!GetFileInformationByHandle(handle, out info)) { throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()); }
        return info.VolumeSerialNumber.ToString("X8") + ":" + info.FileIndexHigh.ToString("X8") + info.FileIndexLow.ToString("X8");
    }
    public static int VolumeSerialOffset() { return (int)Marshal.OffsetOf(typeof(BY_HANDLE_FILE_INFORMATION), "VolumeSerialNumber"); }
    public static int FileIndexHighOffset() { return (int)Marshal.OffsetOf(typeof(BY_HANDLE_FILE_INFORMATION), "FileIndexHigh"); }
    public static int Size() { return Marshal.SizeOf(typeof(BY_HANDLE_FILE_INFORMATION)); }
} // MlvSmokeFileIdentityV2 (never a bare column-0 brace: the function extractors end a function at one)
'@
        }
        return [MlvSmokeFileIdentityV2]::Of($Stream.SafeFileHandle)
    }
    # adversary-c2bbe5f8.json A8: off Windows the identity is read from the OPEN descriptor (/proc/<pid>/fd/<fd>), never by re-resolving the path, so a
    # rename between the open and the stat cannot attribute another file's identity to these bytes; a system without /proc fails closed
    $fd = $Stream.SafeFileHandle.DangerousGetHandle().ToInt64()
    $fdPath = "/proc/$PID/fd/$fd"
    if (-not (Test-Path -LiteralPath $fdPath)) { throw "the identity of $ResolvedPath could not be read from its open descriptor" }
    $stat = & stat -L -c '%d:%i' -- $fdPath 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$stat)) { throw "the identity of $ResolvedPath could not be read" }
    return ([string]$stat).Trim()
}

function Read-SmokeSourceOnce {
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B2): the comparator's ONE read of a smoke file. One ReadAllBytes feeds both the parse (the
    # figures) and the strict host-load reader (the text). A text the parser refuses (keys differing only in case, malformed JSON) does not throw: the
    # figures object is empty and parseError says why, so the caller records a typed failure and the host-load reader still judges the same text.
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (adversary-07b6a932.json A6): -SameFileAs is the source already read for the other leg. Whether two
    # inputs are one physical file is decided by the file system's identity of the OPEN handle (volume serial + file index), never by path spelling:
    # a hard link or a case variant of one file is that same file (returned as-is, so it is read once and the same-input refusal fires), and two
    # paths that differ only in case in a case-sensitive directory are two files, each read independently.
    param([string]$Path, [object]$SameFileAs = $null)

    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Smoke JSON not found: $resolved"
    }
    # adversary-ac6205bd.json A4: a read that fails (a lock taken by another process, a sharing violation) is a typed parse error, never a throw that
    # aborts the report. A5: the bytes are decoded by their BOM (UTF-8, UTF-16 LE/BE), as Get-Content did; no BOM reads UTF-8.
    $bytes = [byte[]]@()
    $text = ''
    $smoke = $null
    $parseError = $null
    $identity = $null
    try {
        $stream = [IO.FileStream]::new($resolved, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try {
            $identity = Get-SmokeFileIdentity -Stream $stream -ResolvedPath $resolved
            if ($null -ne $SameFileAs -and $null -ne $SameFileAs.identity -and [string]::Equals($identity, [string]$SameFileAs.identity, [StringComparison]::Ordinal)) {
                return $SameFileAs
            }
            $buffer = [IO.MemoryStream]::new()
            $stream.CopyTo($buffer)
            $bytes = $buffer.ToArray()
        }
        finally {
            $stream.Dispose()
        }
        $text = [IO.StreamReader]::new([IO.MemoryStream]::new($bytes), [Text.UTF8Encoding]::new($false), $true).ReadToEnd()
    }
    catch {
        $parseError = "the smoke JSON could not be read: $($_.Exception.Message -replace '\s+', ' ')"
    }
    $parseText = if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text.Substring(1) } else { $text }
    if ($null -eq $parseError) {
        try {
            $smoke = ConvertFrom-Json -InputObject $parseText -Depth 100
        }
        catch {
            $parseError = ($_.Exception.Message -replace '\s+', ' ')
        }
    }
    if ($null -eq $smoke -or $smoke -is [System.Collections.IList]) {
        if ($null -eq $parseError) { $parseError = 'the smoke JSON is not one object' }
        $smoke = [pscustomobject]@{}
    }
    [pscustomobject]@{
        path = $resolved
        text = $text
        smoke = $smoke
        parseError = $parseError
        identity = $identity
        sha256 = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
        length = [int64]$bytes.Length
    }
}

function Resolve-ExistingFileBinding {
    param(
        [object]$Path,
        [string]$Label
    )

    if ($null -eq $Path -or [string]::IsNullOrWhiteSpace([string]$Path)) {
        throw "$Label path is missing."
    }
    $unresolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath([string]$Path)
    if (-not (Test-Path -LiteralPath $unresolved -PathType Leaf)) {
        throw "$Label file not found: $unresolved"
    }
    $resolved = (Resolve-Path -LiteralPath $unresolved).Path
    [pscustomobject]@{
        path = $resolved
        sha256 = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash
        length = (Get-Item -LiteralPath $resolved).Length
    }
}

function Assert-RecordedFileBinding {
    param(
        [object]$Actual,
        [object]$Recorded,
        [string]$Label
    )

    if ($null -eq $Recorded) {
        throw "$Label capture-time binding is missing."
    }
    $recordedPath = [string](Get-NestedValue -Object $Recorded -Path "path")
    $recordedSha = [string](Get-NestedValue -Object $Recorded -Path "sha256")
    $recordedLength = Get-NestedValue -Object $Recorded -Path "length"
    if (-not [string]::Equals($Actual.path, $recordedPath, [StringComparison]::OrdinalIgnoreCase) -or
        $Actual.sha256 -ne $recordedSha -or
        [int64]$Actual.length -ne [int64]$recordedLength) {
        throw "$Label current bytes do not match its capture-time binding."
    }
}

function Convert-PlaybackLogLineToObject {
    param([string]$Line)

    $result = [ordered]@{}
    $matches = [regex]::Matches($Line, '(?<key>[A-Za-z0-9_]+)=(?<value>"[^"]*"|\S+)')
    foreach ($match in $matches) {
        $key = $match.Groups["key"].Value
        $rawValue = $match.Groups["value"].Value.Trim('"')
        $integer = 0L
        $doubleValue = 0.0
        if ([long]::TryParse($rawValue, [ref]$integer)) {
            $result[$key] = $integer
        }
        elseif ([double]::TryParse(
            $rawValue,
            [System.Globalization.NumberStyles]::Float,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [ref]$doubleValue)) {
            $result[$key] = $doubleValue
        }
        else {
            $result[$key] = $rawValue
        }
    }
    [pscustomobject]$result
}

function Get-EmbeddedBuildStamp {
    param([string]$Path)

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 256 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) {
        throw "Executable is not a PE image (missing MZ header)."
    }
    $peOffset = [BitConverter]::ToInt32($bytes, 0x3c)
    if ($peOffset -lt 0x40 -or $peOffset + 4 -gt $bytes.Length -or
        $bytes[$peOffset] -ne 0x50 -or $bytes[$peOffset + 1] -ne 0x45 -or
        $bytes[$peOffset + 2] -ne 0 -or $bytes[$peOffset + 3] -ne 0) {
        throw "Executable is not a valid PE image (missing PE signature)."
    }
    $text = [System.Text.Encoding]::ASCII.GetString($bytes)
    $matches = [regex]::Matches(
        $text,
        'MLVAPP_BUILDSTAMP_v1\|sha=([0-9a-f]{40})\|dirty=([01])')
    if ($matches.Count -ne 1) {
        throw "Executable must contain exactly one MLVAPP_BUILDSTAMP_v1 field."
    }
    [pscustomobject]@{
        sha = $matches[0].Groups[1].Value
        dirty = [int]$matches[0].Groups[2].Value
    }
}

function Assert-UniqueLaunchArguments {
    param(
        [object[]]$Arguments,
        [string]$Label
    )

    $seen = @{}
    foreach ($argument in $Arguments) {
        $text = [string]$argument
        if (-not $text.StartsWith("--", [StringComparison]::Ordinal)) { continue }
        $name = $text.Split('=', 2)[0]
        if ($seen.ContainsKey($name)) {
            throw "$Label launch arguments repeat controlling option $name."
        }
        $seen[$name] = $true
    }
}

function Get-NestedValue {
    param(
        [object]$Object,
        [string]$Path
    )

    $current = $Object
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $current) {
            return $null
        }
        $property = $current.PSObject.Properties[$part]
        if (-not $property) {
            return $null
        }
        $current = $property.Value
    }
    $current
}

function Get-FirstNestedValue {
    param(
        [object]$Object,
        [string[]]$Paths
    )

    foreach ($path in $Paths) {
        $value = Get-NestedValue -Object $Object -Path $path
        if ($null -ne $value -and -not [string]::IsNullOrWhiteSpace([string]$value)) {
            return $value
        }
    }
    $null
}

function Get-LaunchArgumentValue {
    param(
        [object]$Smoke,
        [string]$Name
    )

    $arguments = @(Get-NestedValue -Object $Smoke -Path "launch.arguments")
    for ($index = 0; $index -lt $arguments.Count; ++$index) {
        if ([string]$arguments[$index] -eq $Name) {
            if ($index + 1 -ge $arguments.Count) {
                throw "Launch argument $Name has no value."
            }
            return [string]$arguments[$index + 1]
        }
    }
    throw "Launch argument $Name is missing."
}

function Get-GitTreeBinding {
    param(
        [object]$Smoke,
        [string]$BuildSha,
        [string]$Label
    )

    if ($BuildSha -notmatch '^[0-9a-fA-F]{40}$') {
        throw "$Label build SHA is not a full 40-character Git commit: $BuildSha"
    }
    $repoRoot = [string](Get-NestedValue -Object $Smoke -Path "repoRoot")
    if ([string]::IsNullOrWhiteSpace($repoRoot) -or -not (Test-Path -LiteralPath $repoRoot -PathType Container)) {
        throw "$Label repoRoot is missing or unavailable: $repoRoot"
    }
    $treeLines = @(& git -C $repoRoot show -s --format=%T $BuildSha 2>$null)
    $gitExit = $LASTEXITCODE
    $tree = $treeLines | Select-Object -First 1
    if ($gitExit -ne 0 -or [string]$tree -notmatch '^[0-9a-fA-F]{40}$') {
        throw "$Label build commit is unavailable in the recorded repository: $BuildSha"
    }
    [pscustomobject]@{
        commit = $BuildSha.ToLowerInvariant()
        tree = ([string]$tree).Trim().ToLowerInvariant()
    }
}

function Get-LegEvidenceBinding {
    param(
        [string]$Name,
        [object]$Smoke,
        [string]$JsonPath,
        [object]$JsonSource
    )

    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B2): the smoke JSON's binding comes from the comparator's one read (Read-SmokeSourceOnce) when
    # given, so the file is not opened a second time to hash it.
    $json = if ($null -ne $JsonSource) {
        [pscustomobject]@{ path = [string]$JsonSource.path; sha256 = [string]$JsonSource.sha256; length = [int64]$JsonSource.length }
    } else {
        Resolve-ExistingFileBinding -Path $JsonPath -Label "$Name smoke JSON"
    }
    if ([string](Get-NestedValue $Smoke "schema") -ne "mlvapp-gui-smoke-result.v2") {
        throw "$Name smoke result schema is not mlvapp-gui-smoke-result.v2."
    }
    $exe = Resolve-ExistingFileBinding `
        -Path (Get-NestedValue -Object $Smoke -Path "exePath") `
        -Label "$Name executable"
    Assert-RecordedFileBinding `
        -Actual $exe -Recorded (Get-NestedValue $Smoke "evidence.inputs.executable") `
        -Label "$Name executable"
    $clip = Resolve-ExistingFileBinding `
        -Path (Get-NestedValue -Object $Smoke -Path "clipPath") `
        -Label "$Name clip"
    Assert-RecordedFileBinding `
        -Actual $clip -Recorded (Get-NestedValue $Smoke "evidence.inputs.clip") `
        -Label "$Name clip"
    $receipt = Resolve-ExistingFileBinding `
        -Path (Get-LaunchArgumentValue -Smoke $Smoke -Name "--receipt") `
        -Label "$Name receipt"
    Assert-RecordedFileBinding `
        -Actual $receipt -Recorded (Get-NestedValue $Smoke "evidence.inputs.receipt") `
        -Label "$Name receipt"
    $log = Resolve-ExistingFileBinding `
        -Path (Get-NestedValue -Object $Smoke -Path "log.path") `
        -Label "$Name per-run source log"
    Assert-RecordedFileBinding `
        -Actual $log -Recorded (Get-NestedValue $Smoke "evidence.runLogSnapshot") `
        -Label "$Name per-run source log"

    $manifest = Resolve-ExistingFileBinding `
        -Path (Get-NestedValue $Smoke "evidence.inputs.buildManifest.path") `
        -Label "$Name build manifest"
    Assert-RecordedFileBinding `
        -Actual $manifest -Recorded (Get-NestedValue $Smoke "evidence.inputs.buildManifest") `
        -Label "$Name build manifest"
    $manifestData = Get-Content -LiteralPath $manifest.path -Raw | ConvertFrom-Json -Depth 50
    $buildSha = [string](Get-NestedValue -Object $manifestData -Path "head")
    $git = Get-GitTreeBinding -Smoke $Smoke -BuildSha $buildSha -Label $Name
    if ((Get-NestedValue $manifestData "dirty") -ne $false -or
        [int](Get-NestedValue $manifestData "provenanceStampDirty") -ne 0 -or
        (Get-NestedValue $manifestData "provenanceStampShaMatchesHead") -ne $true -or
        [string](Get-NestedValue $manifestData "provenanceStampSha") -ne $buildSha -or
        [string](Get-NestedValue $manifestData "sha256") -ne $exe.sha256 -or
        [string](Get-NestedValue $manifestData "exe") -ne [IO.Path]::GetFileName($exe.path) -or
        (Get-NestedValue $manifestData "selfContained") -ne $true -or
        [int](Get-NestedValue $manifestData "launchProbe.exitCode") -ne 0) {
        throw "$Name build manifest is dirty, unbound, or not independently runnable."
    }
    $stamp = Get-EmbeddedBuildStamp -Path $exe.path
    if ($stamp.sha -ne $buildSha -or $stamp.dirty -ne 0) {
        throw "$Name embedded provenance stamp does not bind a clean manifest head."
    }

    $logLines = @(Get-Content -LiteralPath $log.path)
    $rawPaths = @(
        "log.raw.runMetadata",
        "log.raw.summary",
        "log.raw.visualState",
        "log.raw.screenshotEvent"
    )
    foreach ($rawPath in $rawPaths) {
        $rawLine = [string](Get-NestedValue $Smoke $rawPath)
        if ([string]::IsNullOrWhiteSpace($rawLine) -or
            @($logLines | Where-Object { $_ -ceq $rawLine }).Count -ne 1) {
            throw "$Name $rawPath is missing or not unique in the per-run log snapshot."
        }
    }

    $rawMetadata = [string](Get-NestedValue $Smoke "log.raw.runMetadata")
    if ($rawMetadata -notmatch 'run_metadata=(?<json>\{.*\})') {
        throw "$Name run metadata line is malformed."
    }
    $loggedMetadata = $Matches["json"] | ConvertFrom-Json -Depth 50
    if ((Convert-ToStableValueText $loggedMetadata) -ne
        (Convert-ToStableValueText (Get-NestedValue $Smoke "log.runMetadata"))) {
        throw "$Name parsed run metadata differs from the per-run log snapshot."
    }
    $nonce = [string](Get-NestedValue $Smoke "evidence.runNonce")
    if ($nonce -notmatch '^[0-9a-f]{32}$' -or
        [string](Get-NestedValue $loggedMetadata "gui_smoke_run_nonce") -ne $nonce -or
        [int64](Get-NestedValue $loggedMetadata "process_id") -ne
            [int64](Get-NestedValue $Smoke "process.id") -or
        [string](Get-NestedValue $loggedMetadata "build_sha") -ne $buildSha) {
        throw "$Name run nonce/process/build binding is invalid."
    }

    $commandLine = @(Get-NestedValue -Object $loggedMetadata -Path "command_line")
    if ($commandLine.Count -eq 0 -or [string]::IsNullOrWhiteSpace([string]$commandLine[0])) {
        throw "$Name log metadata does not record the launched executable."
    }
    $resolvedCommandExe = (Resolve-Path -LiteralPath ([string]$commandLine[0])).Path
    if (-not [string]::Equals($resolvedCommandExe, $exe.path, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name executable path does not match log run metadata."
    }
    $launchArguments = @(Get-NestedValue -Object $Smoke -Path "launch.arguments")
    Assert-UniqueLaunchArguments -Arguments $launchArguments -Label $Name
    $loggedArguments = @($commandLine | Select-Object -Skip 1)
    if ((Convert-ToStableValueText $launchArguments) -ne (Convert-ToStableValueText $loggedArguments)) {
        throw "$Name launch arguments do not match log run metadata."
    }
    $resolvedInput = (Resolve-Path -LiteralPath (Get-LaunchArgumentValue -Smoke $Smoke -Name "--input")).Path
    if (-not [string]::Equals($resolvedInput, $clip.path, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name clip path does not match its --input argument."
    }

    $summary = Convert-PlaybackLogLineToObject `
        -Line ([string](Get-NestedValue $Smoke "log.raw.summary"))
    $visualState = Convert-PlaybackLogLineToObject `
        -Line ([string](Get-NestedValue $Smoke "log.raw.visualState"))
    $screenshotEvent = Convert-PlaybackLogLineToObject `
        -Line ([string](Get-NestedValue $Smoke "log.raw.screenshotEvent"))
    if ((Convert-ToStableValueText $visualState) -ne
        (Convert-ToStableValueText (Get-NestedValue $Smoke "visualQuality.visualState"))) {
        throw "$Name visual state differs from the per-run log snapshot."
    }
    if ([int64](Get-NestedValue $summary "last_presented_frame") -ne
            [int64](Get-NestedValue $Smoke "validation.lastPresentedFrame") -or
        [int64](Get-NestedValue $summary "first_presented_frame") -ne
            [int64](Get-NestedValue $Smoke "validation.firstPresentedFrame") -or
        [int64](Get-NestedValue $summary "presented_frames") -ne
            [int64](Get-NestedValue $Smoke "validation.presentedFrames")) {
        throw "$Name playback summary differs from the per-run log snapshot."
    }

    $screenshotPath = Resolve-SmokeScreenshotPath -Smoke $Smoke
    $screenshot = Resolve-ExistingFileBinding -Path $screenshotPath -Label "$Name screenshot"
    $eventScreenshot = Resolve-ExistingFileBinding `
        -Path ([string](Get-NestedValue $screenshotEvent "path")) `
        -Label "$Name screenshot event"
    $image = Get-ImageMetadata -Path $screenshot.path
    $capture = Get-NestedValue $Smoke "screenshot.capture"
    if (-not [string]::Equals(
            $screenshot.path, $eventScreenshot.path,
            [StringComparison]::OrdinalIgnoreCase) -or
        $screenshot.sha256 -ne [string](Get-NestedValue $capture "image.sha256") -or
        $screenshot.sha256 -ne [string](Get-NestedValue $screenshotEvent "sha256") -or
        [int64]$screenshot.length -ne [int64](Get-NestedValue $capture "length") -or
        [int64]$screenshot.length -ne [int64](Get-NestedValue $screenshotEvent "bytes") -or
        [int]$image.width -ne [int](Get-NestedValue $screenshotEvent "width") -or
        [int]$image.height -ne [int](Get-NestedValue $screenshotEvent "height") -or
        [int64](Get-NestedValue $capture "frame") -ne [int64](Get-NestedValue $screenshotEvent "frame") -or
        [int64](Get-NestedValue $capture "serial") -ne [int64](Get-NestedValue $screenshotEvent "serial") -or
        [int64](Get-NestedValue $capture "generation") -ne [int64](Get-NestedValue $screenshotEvent "generation") -or
        [int64](Get-NestedValue $screenshotEvent "frame") -ne
            [int64](Get-NestedValue $summary "last_presented_frame") -or
        [int64](Get-NestedValue $screenshotEvent "serial") -le 0) {
        throw "$Name screenshot bytes/frame/serial are not atomically bound to the run log."
    }

    [pscustomobject]@{
        json = $json
        executable = $exe
        clip = $clip
        receipt = $receipt
        sourceLog = $log
        buildManifest = $manifest
        buildManifestData = $manifestData
        embeddedStamp = $stamp
        screenshot = $screenshot
        screenshotEvent = $screenshotEvent
        summary = $summary
        visualState = $visualState
        git = $git
    }
}

function Get-HostLoadComparisonEvidence {
    # PLAYBACK-MEASURE-HOST-LOAD-GATE-1: refuse to compare fps across a pair where either leg's
    # host load is PROVISIONAL (bar exceeded, or telemetry UNKNOWN -- a run recorded before this
    # gate existed has no hostLoad field at all, which is also UNKNOWN, never "quiet"). An fps
    # number measured on a loaded host is not a property of the build, so a provisional leg is
    # never usable as a regression signal, whatever the other leg's state is.
    # adversary-de8e3799.json A1: -BeforeText / -AfterText (the smoke result files' own text) are judged by VenueChain.psm1 ConvertFrom-VenueSmokeResultText,
    # so a singleton root or hostLoad array is never unrolled and a repeated member is 'unknown'. Without the text, the hostLoad member is read as it is
    # (never through the pipeline), so an array-valued hostLoad stays an array and reads 'unknown'.
    param(
        [object]$BeforeSmoke,
        [object]$AfterSmoke,
        [AllowNull()][AllowEmptyString()][string]$BeforeText,
        [AllowNull()][AllowEmptyString()][string]$AfterText
    )

    $evidence = [ordered]@{}
    $failures = @()
    foreach ($leg in @(
        [pscustomobject]@{ Name = "before"; Smoke = $BeforeSmoke; HasText = $PSBoundParameters.ContainsKey('BeforeText'); Text = $BeforeText },
        [pscustomobject]@{ Name = "after"; Smoke = $AfterSmoke; HasText = $PSBoundParameters.ContainsKey('AfterText'); Text = $AfterText }
    )) {
        $legHostLoad = $null
        $smokeObj = $leg.Smoke
        if ($null -ne $smokeObj -and $smokeObj -isnot [Array] -and $smokeObj -isnot [string] -and $smokeObj -isnot [ValueType]) {
            # a plain assignment keeps an array value an array (no pipeline), so ConvertFrom-VenueSmokeHostLoad sees it and refuses it
            if ($smokeObj -is [System.Collections.IDictionary]) { if ($smokeObj.Contains('hostLoad')) { $legHostLoad = $smokeObj['hostLoad'] } }
            else { $hlProp = $smokeObj.PSObject.Properties['hostLoad']; if ($null -ne $hlProp) { $legHostLoad = $hlProp.Value } }
        }
        # round 3: a hostLoad block that is PRESENT but missing "provisional" (or "state") must
        # read the same as a missing block entirely -- provisional/unknown, never coerced to
        # clean. [bool]$null -eq $false is the trap: it would make a degenerate block silently
        # pass as a clean leg, exactly backwards from this gate's fail-toward-provisional stance.
        # round 4 (sol major): "provisional" and "state" used to be derived independently, so a
        # block carrying an explicit provisional=false alongside a missing/blank state read as
        # state=unknown PROVISIONAL=false -- an inconsistent, clean-reading combination that let
        # UNKNOWN enter comparison unrefused. state=unknown now always forces provisional=true.
        # round 8 (sol MAJOR item 3): that fix only special-cased state=="unknown" -- a leg with
        # state="exceeded" and an inconsistent/fabricated provisional=false still read clean. THE
        # CANONICAL PREDICATE (identical at every one of this card's six sites -- see
        # compare-machine-perf.ps1's Get-PlaybackAbLegHostLoadProvisional for the full cross-file
        # note): clean iff state=="quiet" AND provisional==false; every other combination is
        # provisional.
        # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (sol r5 B1): the block is judged by VenueChain.psm1's ConvertFrom-VenueSmokeHostLoad, the venue reader's own rule:
        # clean only when state is exactly the string 'quiet', provisional is a real [bool] $false (not 0, not [false], not a string) and the before and after
        # snapshots (and every during sample) are collected = a real [bool] $true. A quiet/false block beside before.collected=false (a CimException), state
        # 'QUIET', provisional 0 or [false] all read state 'unknown' (or the declared state) and provisional, so the verdict is INCONCLUSIVE, never PASS.
        $judged = if ($leg.HasText) { ConvertFrom-VenueSmokeResultText -Text $leg.Text } else { ConvertFrom-VenueSmokeHostLoad -HostLoad $legHostLoad }
        $legState = [string]$judged.state
        $legProvisional = [bool]$judged.provisional
        $legReason = if ($null -eq $legHostLoad) {
            "no hostLoad telemetry recorded in the smoke result"
        } else {
            [string]$judged.reason
        }
        $evidence[$leg.Name] = [pscustomobject]@{
            state = $legState
            provisional = $legProvisional
            collected = [bool]$judged.collected
            reason = $legReason
        }
        if ($legProvisional) {
            $failures += (
                "$($leg.Name) smoke host load is PROVISIONAL (state=$legState" +
                $(if ($legReason) { ": $legReason" } else { "" }) +
                "); an fps comparison against a PROVISIONAL run is refused."
            )
        }
    }

    [pscustomobject]@{
        evidence = [pscustomobject]$evidence
        failures = @($failures)
    }
}

function Get-GuiSmokeAbVerdict {
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r3 (sol r2 B1): a leg whose host load is provisional or unknown (the
    # before leg is the control) is never counted in the comparison verdict. It used to join the failures and
    # read FAIL (a regression-shaped verdict). Now each such leg prints the typed line
    # REFERENCE_PROVISIONAL leg=<before|after> reason=hostload-<state> and the verdict is INCONCLUSIVE, never
    # PASS and never FAIL; the other failures are still listed. Pure: -HostLoadEvidence is
    # Get-HostLoadComparisonEvidence's evidence (its provisional is already the canonical predicate).
    param(
        [object[]]$Failures,
        [object]$HostLoadEvidence
    )
    $refusals = @()
    foreach ($legName in @("before", "after")) {
        # adversary-b3d61554.json A13: plain assignments, never `$x = if (...) { $list }`: an if-expression's output unrolls a one-element list into its member, which then read as a
        # clean standing. A leg evidence that is a list (not one object) is no standing: it is passed on as $null, which Get-VenueReferenceHostLoadRefusal refuses.
        $legEvidence = $null
        if ($null -ne $HostLoadEvidence) { $legEvidence = $HostLoadEvidence.$legName }
        # r6 (sol r5 B1): the venue reader's own refusal rule (VenueChain.psm1 Get-VenueReferenceHostLoadRefusal): clean only for state exactly 'quiet',
        # provisional a real $false and collected a real $true. Only those three members are passed, so no caller-supplied applies=$false can skip the rule.
        $standing = $null
        if ($null -ne $legEvidence -and $legEvidence -isnot [System.Collections.IList]) {
            $standing = [pscustomobject]@{ state = $legEvidence.state; provisional = $legEvidence.provisional; collected = $legEvidence.collected }
        }
        $line = Get-VenueReferenceHostLoadRefusal -LegId $legName -Standing $standing
        if ($null -eq $line) { continue }
        $refusals += $line
    }
    $failureList = @($Failures | Where-Object { $null -ne $_ })
    $verdictText = if ($refusals.Count -gt 0) { "INCONCLUSIVE" } elseif ($failureList.Count -eq 0) { "PASS" } else { "FAIL" }
    [pscustomobject]@{
        verdict = $verdictText
        referenceProvisional = @($refusals)
    }
}

function Convert-ToStableValueText {
    param([object]$Value)

    if ($null -eq $Value) {
        return $null
    }
    $Value | ConvertTo-Json -Compress -Depth 16
}

function Convert-ToNullableDouble {
    param([object]$Value)

    if ($null -eq $Value) {
        return $null
    }
    $parsed = 0.0
    if ([double]::TryParse(
        [string]$Value,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed)) {
        return $parsed
    }
    $null
}

function New-DeltaObject {
    param(
        [object]$BeforeValue,
        [object]$AfterValue
    )

    $beforeDouble = Convert-ToNullableDouble $BeforeValue
    $afterDouble = Convert-ToNullableDouble $AfterValue
    $delta = $null
    $deltaPercent = $null
    if ($null -ne $beforeDouble -and $null -ne $afterDouble) {
        $delta = [Math]::Round($afterDouble - $beforeDouble, 6)
        if ([Math]::Abs($beforeDouble) -gt 0.000001) {
            $deltaPercent = [Math]::Round((($afterDouble - $beforeDouble) / $beforeDouble) * 100.0, 3)
        }
    }

    [pscustomobject]@{
        before = $beforeDouble
        after = $afterDouble
        delta = $delta
        deltaPercent = $deltaPercent
    }
}

function Get-DisplayComparability {
    <#
    .SYNOPSIS
    UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 1): whether the before/after
    GUI-smoke legs are known to have presented on the same display identity and mode -- an
    FPS comparison between legs that did not (a 4K leg vs a degraded-fallback leg, or two
    different physical outputs, or an unverified placement on either side) is not meaningful.
    .DESCRIPTION
    Third state, never guessed into "same": 'unknown' when either leg's own display block
    could not be resolved (a legacy binary that predates it, or its window_placement line
    failed to parse -- reasonCode DISPLAY_IDENTITY_UNKNOWN) or either leg's placement never
    verified (reasonCode PLACEMENT_UNVERIFIED). 'different' (DISPLAY_MISMATCH) when both are
    known and verified but disagree on identity or mode. 'same' only when both are known,
    verified, and agree on presentation screen name, physical size, rounded refresh, DPR,
    window mode, and preview size.
    .OUTPUTS
    [pscustomobject] { status ('same'|'different'|'unknown'); reasonCode (string or $null);
    before; after }.
    #>
    param([object]$BeforeDisplay, [object]$AfterDisplay)

    if ($null -eq $BeforeDisplay -or $null -eq $AfterDisplay -or
        $BeforeDisplay.identityUnknownReason -or $AfterDisplay.identityUnknownReason) {
        return [pscustomobject]@{
            status = 'unknown'; reasonCode = 'DISPLAY_IDENTITY_UNKNOWN'
            before = $BeforeDisplay; after = $AfterDisplay
        }
    }
    # DISPLAY-SMOKE-FAILED-LOG-PRESERVE-1 (origin PR #191): a block that says it was never measured
    # (the attribution job's pre-smoke block, `phase` = 'pre-smoke' / `measured` = $false, published
    # when a smoke run failed before its display log could be read) is identity UNKNOWN even if
    # every other identity field is populated -- it describes the venue BEFORE the run, not the
    # screen the leg presented on. Property lookups go through PSObject.Properties so a runner
    # result.json block (no such fields) is unaffected.
    foreach ($side in @([pscustomobject]@{ name = 'before'; block = $BeforeDisplay },
            [pscustomobject]@{ name = 'after'; block = $AfterDisplay })) {
        $phaseProperty = $side.block.PSObject.Properties['phase']
        $measuredProperty = $side.block.PSObject.Properties['measured']
        if (($null -ne $phaseProperty -and [string]$phaseProperty.Value -eq 'pre-smoke') -or
            ($null -ne $measuredProperty -and $measuredProperty.Value -eq $false)) {
            return [pscustomobject]@{
                status = 'unknown'; reasonCode = 'DISPLAY_IDENTITY_UNKNOWN'
                detail = "$($side.name) leg display block was not measured (pre-smoke: the smoke run failed before the app reported a display), so its identity is not knowable"
                before = $BeforeDisplay; after = $AfterDisplay
            }
        }
    }
    # Round 2 (sol PRE-REVIEW #2 BLOCKER c): identity is name+manufacturer+model+serial. A block
    # from a runner that predates those fields does not carry the properties at all -- unknown,
    # never "absent equals absent".
    foreach ($identityProperty in 'presentationManufacturer', 'presentationModel', 'presentationSerial') {
        if ($null -eq $BeforeDisplay.PSObject.Properties[$identityProperty] -or
            $null -eq $AfterDisplay.PSObject.Properties[$identityProperty]) {
            return [pscustomobject]@{
                status = 'unknown'; reasonCode = 'DISPLAY_IDENTITY_UNKNOWN'
                before = $BeforeDisplay; after = $AfterDisplay
            }
        }
    }
    # Round 4 (sol r1 BLOCKER): a PRESENT-but-empty manufacturer/model/serial is not a known
    # identity either (Qt reports an empty string for a display with no EDID descriptor). The
    # shared parser already turns that into identityUnknownReason above; this refuses a block
    # that reaches the comparator without going through it (hand-built or legacy result.json),
    # so two empty-EDID legs can never agree on "empty == empty" and compare 'same'.
    foreach ($identityProperty in 'presentationManufacturer', 'presentationModel', 'presentationSerial') {
        foreach ($side in @([pscustomobject]@{ name = 'before'; block = $BeforeDisplay },
                [pscustomobject]@{ name = 'after'; block = $AfterDisplay })) {
            if ([string]::IsNullOrWhiteSpace([string]$side.block.PSObject.Properties[$identityProperty].Value)) {
                return [pscustomobject]@{
                    status = 'unknown'; reasonCode = 'DISPLAY_IDENTITY_UNKNOWN'
                    detail = "$($side.name) leg has an empty $identityProperty (no EDID descriptor): two displays sharing one device name cannot be told apart"
                    before = $BeforeDisplay; after = $AfterDisplay
                }
            }
        }
    }
    if ($BeforeDisplay.verified -ne $true -or $AfterDisplay.verified -ne $true) {
        return [pscustomobject]@{
            status = 'unknown'; reasonCode = 'PLACEMENT_UNVERIFIED'
            before = $BeforeDisplay; after = $AfterDisplay
        }
    }
    $same = (
        ([string]$BeforeDisplay.presentationScreenName -eq [string]$AfterDisplay.presentationScreenName) -and
        ([string]$BeforeDisplay.presentationManufacturer -eq [string]$AfterDisplay.presentationManufacturer) -and
        ([string]$BeforeDisplay.presentationModel -eq [string]$AfterDisplay.presentationModel) -and
        ([string]$BeforeDisplay.presentationSerial -eq [string]$AfterDisplay.presentationSerial) -and
        ($BeforeDisplay.physicalWidth -eq $AfterDisplay.physicalWidth) -and
        ($BeforeDisplay.physicalHeight -eq $AfterDisplay.physicalHeight) -and
        ($BeforeDisplay.refreshHzRounded -eq $AfterDisplay.refreshHzRounded) -and
        ($BeforeDisplay.dpr -eq $AfterDisplay.dpr) -and
        ([string]$BeforeDisplay.windowMode -eq [string]$AfterDisplay.windowMode) -and
        ($BeforeDisplay.previewWidth -eq $AfterDisplay.previewWidth) -and
        ($BeforeDisplay.previewHeight -eq $AfterDisplay.previewHeight)
    )
    if ($same) {
        [pscustomobject]@{ status = 'same'; reasonCode = $null; before = $BeforeDisplay; after = $AfterDisplay }
    } else {
        [pscustomobject]@{
            status = 'different'; reasonCode = 'DISPLAY_MISMATCH'
            before = $BeforeDisplay; after = $AfterDisplay
        }
    }
}

function New-ValueChangeObject {
    param(
        [object]$BeforeValue,
        [object]$AfterValue
    )

    [pscustomobject]@{
        before = $BeforeValue
        after = $AfterValue
        changed = ([string]$BeforeValue) -ne ([string]$AfterValue)
    }
}

function Convert-ToNonNullArray {
    param([object]$Value)

    if ($null -eq $Value) {
        return @()
    }

    @(@($Value) | Where-Object { $null -ne $_ })
}

function New-AutoDecisionComparison {
    param(
        [object]$BeforeSmoke,
        [object]$AfterSmoke
    )

    [pscustomobject]@{
        expectedAuto = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.expectedAuto") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.expectedAuto")
        fieldsPresent = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.fieldsPresent") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.fieldsPresent")
        source = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.source") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.source")
        reason = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.reason") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.reason")
        targetFps = New-DeltaObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.targetFps") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.targetFps")
        averageMs = New-DeltaObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.averageMs") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.averageMs")
        budgetMs = New-DeltaObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.budgetMs") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.budgetMs")
        sampleCount = New-DeltaObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.sampleCount") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.sampleCount")
        averageFpsEquivalent = New-DeltaObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.averageFpsEquivalent") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.averageFpsEquivalent")
        budgetFpsEquivalent = New-DeltaObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.budgetFpsEquivalent") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.budgetFpsEquivalent")
        headroomCapabilityLast = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.headroomCapabilityLastBool") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.headroomCapabilityLastBool")
        validatedNoReadbackCapabilityObserved = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.validatedNoReadbackCapabilityObservedBool") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.validatedNoReadbackCapabilityObservedBool")
        validatedNoReadbackCapabilityDemotedLast = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.validatedNoReadbackCapabilityDemotedLastBool") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.validatedNoReadbackCapabilityDemotedLastBool")
        capabilityConsistent = New-ValueChangeObject `
            -BeforeValue (Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.capabilityConsistent") `
            -AfterValue (Get-NestedValue $AfterSmoke "visualQuality.autoDecision.capabilityConsistent")
        beforeCapabilityFailures = @(Convert-ToNonNullArray (
            Get-NestedValue $BeforeSmoke "visualQuality.autoDecision.capabilityFailures"
        ))
        afterCapabilityFailures = @(Convert-ToNonNullArray (
            Get-NestedValue $AfterSmoke "visualQuality.autoDecision.capabilityFailures"
        ))
    }
}

function Resolve-SmokeScreenshotPath {
    param([object]$Smoke)

    $path = Get-FirstNestedValue -Object $Smoke -Paths @(
        "screenshot.capture.outputPath",
        "screenshot.path",
        "visualQuality.glOutputProof.screenshotPath"
    )
    if ($null -eq $path) {
        return $null
    }
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath([string]$path)
    if (Test-Path -LiteralPath $resolved) {
        return $resolved
    }
    [string]$path
}

function Get-ImageMetadata {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    Add-Type -AssemblyName System.Drawing
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $item = Get-Item -LiteralPath $resolved
    $image = [System.Drawing.Image]::FromFile($resolved)
    try {
        [pscustomobject]@{
            path = $resolved
            width = $image.Width
            height = $image.Height
            length = $item.Length
            sha256 = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash
        }
    }
    finally {
        $image.Dispose()
    }
}

function Compare-SmokeScreenshots {
    param(
        [string]$BeforePath,
        [string]$AfterPath,
        [int]$RequestedSampleStep
    )

    $beforeMeta = Get-ImageMetadata -Path $BeforePath
    $afterMeta = Get-ImageMetadata -Path $AfterPath
    if ($null -eq $beforeMeta -or $null -eq $afterMeta) {
        return [pscustomobject]@{
            status = "not-compared"
            reason = "one-or-both-screenshots-missing"
            before = $beforeMeta
            after = $afterMeta
            sameSha256 = $false
            dimensionsMatch = $false
            meanAbsRgbDelta = $null
            maxAbsRgbDelta = $null
            changedSampleRatio = $null
            sampledPixels = 0
        }
    }

    if ($beforeMeta.width -ne $afterMeta.width -or $beforeMeta.height -ne $afterMeta.height) {
        return [pscustomobject]@{
            status = "dimension-mismatch"
            reason = "screenshot-dimensions-differ"
            before = $beforeMeta
            after = $afterMeta
            sameSha256 = $beforeMeta.sha256 -eq $afterMeta.sha256
            dimensionsMatch = $false
            meanAbsRgbDelta = $null
            maxAbsRgbDelta = $null
            changedSampleRatio = $null
            sampledPixels = 0
        }
    }

    Add-Type -AssemblyName System.Drawing
    $beforeBitmap = [System.Drawing.Bitmap]::FromFile($beforeMeta.path)
    $afterBitmap = [System.Drawing.Bitmap]::FromFile($afterMeta.path)
    try {
        $width = $beforeBitmap.Width
        $height = $beforeBitmap.Height
        $autoStep = [Math]::Max(1, [int]([Math]::Min($width, $height) / 512))
        $step = [Math]::Max(1, [Math]::Max($RequestedSampleStep, $autoStep))
        $sampled = 0
        $changed = 0
        $sumAbs = 0.0
        $maxAbs = 0
        for ($y = 0; $y -lt $height; $y += $step) {
            for ($x = 0; $x -lt $width; $x += $step) {
                $beforePixel = $beforeBitmap.GetPixel($x, $y)
                $afterPixel = $afterBitmap.GetPixel($x, $y)
                $dr = [Math]::Abs([int]$afterPixel.R - [int]$beforePixel.R)
                $dg = [Math]::Abs([int]$afterPixel.G - [int]$beforePixel.G)
                $db = [Math]::Abs([int]$afterPixel.B - [int]$beforePixel.B)
                $pixelMax = [Math]::Max($dr, [Math]::Max($dg, $db))
                $sumAbs += $dr + $dg + $db
                $maxAbs = [Math]::Max($maxAbs, $pixelMax)
                if ($pixelMax -gt 3) {
                    ++$changed
                }
                ++$sampled
            }
        }

        $meanAbs = if ($sampled -gt 0) { $sumAbs / ($sampled * 3.0) } else { 0.0 }
        $changedRatio = if ($sampled -gt 0) { $changed / [double]$sampled } else { 0.0 }
        [pscustomobject]@{
            status = "compared"
            reason = $null
            before = $beforeMeta
            after = $afterMeta
            sameSha256 = $beforeMeta.sha256 -eq $afterMeta.sha256
            dimensionsMatch = $true
            sampleStep = $step
            sampledPixels = $sampled
            meanAbsRgbDelta = [Math]::Round($meanAbs, 6)
            maxAbsRgbDelta = $maxAbs
            changedSampleRatio = [Math]::Round($changedRatio, 6)
        }
    }
    finally {
        $beforeBitmap.Dispose()
        $afterBitmap.Dispose()
    }
}

# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B2): each smoke file is opened ONCE (Read-SmokeSourceOnce: one ReadAllBytes); the figures and the
# host-load verdict both come from that one text, so a file locked or replaced after the read changes nothing. A text ConvertFrom-Json refuses
# (hostLoad beside HostLoad: KeysWithDifferentCasingInJsonString) is no throw: its figures read empty, a typed failure names it, and the strict host-load
# reader judges the same text unknown (a repeated member), so the comparison is INCONCLUSIVE with REFERENCE_PROVISIONAL, never an abort.
$beforeSource = Read-SmokeSourceOnce -Path $Before
# adversary-ac6205bd.json A4: the same physical file given twice is read once (the same-input refusal below then records it).
# adversary-07b6a932.json A6: "the same file" is the open handle's file-system identity (a hard link or a case variant is the same file; two
# case-distinct files in a case-sensitive directory are two files), never path spelling.
$afterSource = Read-SmokeSourceOnce -Path $After -SameFileAs $beforeSource
# adversary-b3d61554.json A2: the paths the OUTPUT must never alias are the two paths the caller GAVE, each resolved on its own. When -After is the same physical file as -Before,
# Read-SmokeSourceOnce returns the before source (read once), whose .path is the before path: taking the after path from it left the real -After path unwatched, so a later swap of it
# for a hard link to the report was never seen at the write. The source objects still carry the figures and the identity; the paths come from the parameters.
$beforeJsonPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Before)
$afterJsonPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($After)
$beforeSmokeText = $beforeSource.text
$afterSmokeText = $afterSource.text
$beforeSmoke = $beforeSource.smoke
$afterSmoke = $afterSource.smoke
$resolvedOutput = $null
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $resolvedOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
    if ([string]::Equals($resolvedOutput, $beforeJsonPath, [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($resolvedOutput, $afterJsonPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Output must not overwrite either smoke JSON input."
    }
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-R3-ADV-RESIDUE-1 r1 (adversary-fd0141e8.json A1): an input whose file identity could not be read (its first read failed:
    # a lock, a sharing violation) has identity null, which no alias check can compare, so an -Output that is a hard link to it would overwrite it. The
    # identity of BOTH inputs is therefore required before ANY write: a report with no readable input identity is refused, typed, and nothing is written.
    foreach ($identityLeg in @([pscustomobject]@{ Name = 'before'; Source = $beforeSource }, [pscustomobject]@{ Name = 'after'; Source = $afterSource })) {
        if ($null -eq $identityLeg.Source.identity) { throw "Output refused: the file identity of the $($identityLeg.Name) smoke JSON could not be read, so an -Output that is a hard link to it cannot be ruled out; nothing was written." }
    }
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (adversary-07b6a932.json A6 sibling): an existing output that IS an input under another spelling (a hard
    # link) is that input too, judged by the same handle identity the inputs were read by
    if (Test-Path -LiteralPath $resolvedOutput -PathType Leaf) {
        $outputStream = [IO.FileStream]::new($resolvedOutput, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try { $outputIdentity = Get-SmokeFileIdentity -Stream $outputStream -ResolvedPath $resolvedOutput } finally { $outputStream.Dispose() }
        foreach ($inputIdentity in @($beforeSource.identity, $afterSource.identity)) {
            if ($null -eq $inputIdentity -or [string]::Equals([string]$inputIdentity, $outputIdentity, [StringComparison]::Ordinal)) { throw "Output must not overwrite either smoke JSON input." }
        }
    }
}
$beforeScreenshot = Resolve-SmokeScreenshotPath -Smoke $beforeSmoke
$afterScreenshot = Resolve-SmokeScreenshotPath -Smoke $afterSmoke
$screenshotCompare = Compare-SmokeScreenshots `
    -BeforePath $beforeScreenshot `
    -AfterPath $afterScreenshot `
    -RequestedSampleStep $SampleStep

$failures = @()
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B2): a smoke text the parser refused is a typed failure (its figures read empty); the host-load
# reader below judges the same text, and an ambiguous hostLoad/HostLoad member reads unknown, so the verdict is INCONCLUSIVE.
foreach ($sourceLeg in @([pscustomobject]@{ Name = 'before'; Source = $beforeSource }, [pscustomobject]@{ Name = 'after'; Source = $afterSource })) {
    if ($sourceLeg.Name -ceq 'after' -and [object]::ReferenceEquals($afterSource, $beforeSource)) {
        $failures += "Before and after name the same smoke JSON file ($($beforeSource.path)); same-input A/B is refused."
    }
    if ($null -ne $sourceLeg.Source.parseError) {
        $failures += "$($sourceLeg.Name) smoke JSON could not be parsed (ambiguous or malformed member): $($sourceLeg.Source.parseError)"
    }
}
$declaredBeforeCommit = [string](Get-NestedValue $beforeSmoke "log.runMetadata.build_sha")
$declaredAfterCommit = [string](Get-NestedValue $afterSmoke "log.runMetadata.build_sha")
if (-not [string]::IsNullOrWhiteSpace($declaredBeforeCommit) -and
    $declaredBeforeCommit -eq $declaredAfterCommit) {
    $failures += "Before and after build commits are identical; same-arm A/B is forbidden."
}
try {
    $declaredBeforeGit = Get-GitTreeBinding `
        -Smoke $beforeSmoke -BuildSha $declaredBeforeCommit -Label "before declared"
    $declaredAfterGit = Get-GitTreeBinding `
        -Smoke $afterSmoke -BuildSha $declaredAfterCommit -Label "after declared"
    if ($declaredBeforeGit.tree -eq $declaredAfterGit.tree) {
        $failures += "Before and after build trees are identical; same-tree A/B is forbidden."
    }
}
catch {
    $failures += "Declared Git binding failed: $($_.Exception.Message)"
}
$evidenceBindings = $null
try {
    $beforeBinding = Get-LegEvidenceBinding `
        -Name "before" -Smoke $beforeSmoke -JsonPath $beforeJsonPath -JsonSource $beforeSource
    $afterBinding = Get-LegEvidenceBinding `
        -Name "after" -Smoke $afterSmoke -JsonPath $afterJsonPath -JsonSource $afterSource
    $evidenceBindings = [pscustomobject]@{
        before = $beforeBinding
        after = $afterBinding
    }
    # adversary-07b6a932.json A6: distinct means distinct physical files (the handle identity Read-SmokeSourceOnce recorded), not distinct spellings.
    if ([object]::ReferenceEquals($afterSource, $beforeSource) -or $null -eq $beforeSource.identity -or $null -eq $afterSource.identity -or
        [string]::Equals([string]$beforeSource.identity, [string]$afterSource.identity, [StringComparison]::Ordinal)) {
        $failures += "Before and after smoke JSON paths must be distinct."
    }
    if ([string]::Equals($beforeBinding.executable.path, $afterBinding.executable.path, [StringComparison]::OrdinalIgnoreCase)) {
        $failures += "Before and after executables must be distinct files."
    }
    if ($beforeBinding.executable.sha256 -eq $afterBinding.executable.sha256) {
        $failures += "Before and after executables have the same SHA256; same-arm A/B is forbidden."
    }
    if ($beforeBinding.git.commit -eq $afterBinding.git.commit) {
        $failures += "Before and after build commits are identical; same-arm A/B is forbidden."
    }
    if ($beforeBinding.git.tree -eq $afterBinding.git.tree) {
        $failures += "Before and after build trees are identical; same-tree A/B is forbidden."
    }
    if (-not [string]::Equals($beforeBinding.clip.path, $afterBinding.clip.path, [StringComparison]::OrdinalIgnoreCase) -or
        $beforeBinding.clip.sha256 -ne $afterBinding.clip.sha256) {
        $failures += "Before and after legs do not bind the same clip bytes."
    }
    if (-not [string]::Equals($beforeBinding.receipt.path, $afterBinding.receipt.path, [StringComparison]::OrdinalIgnoreCase) -or
        $beforeBinding.receipt.sha256 -ne $afterBinding.receipt.sha256) {
        $failures += "Before and after legs do not bind the same receipt bytes."
    }
    if ((Convert-ToStableValueText $beforeBinding.visualState) -ne
        (Convert-ToStableValueText $afterBinding.visualState)) {
        $failures += "Before and after full visual-state telemetry differs."
    }
}
catch {
    $failures += "Evidence binding failed: $($_.Exception.Message)"
}

$visualStateKeys = @(
    "look_assist_enabled", "temperature", "tint", "raw_black", "raw_white",
    "chroma_smooth", "stretch_x", "stretch_y", "h_stretch_index", "v_stretch_index",
    "dual_iso_mode", "dual_iso_interp", "dual_iso_alias_map", "dual_iso_fullres",
    "drop_frame", "scale_request", "quality_mode", "receipt_supplied"
)
$visualStateEvidence = [ordered]@{}
foreach ($key in $visualStateKeys) {
    $beforeValue = Get-NestedValue $beforeSmoke "visualQuality.visualState.$key"
    $afterValue = Get-NestedValue $afterSmoke "visualQuality.visualState.$key"
    $matches = (
        $null -ne $beforeValue -and
        $null -ne $afterValue -and
        (Convert-ToStableValueText $beforeValue) -eq (Convert-ToStableValueText $afterValue)
    )
    $visualStateEvidence[$key] = [pscustomobject]@{
        before = $beforeValue
        after = $afterValue
        matches = $matches
    }
    if (-not $matches) {
        $failures += "Visual-state mismatch or missing value for $key."
    }
}

foreach ($leg in @(
    [pscustomobject]@{ Name = "before"; Smoke = $beforeSmoke },
    [pscustomobject]@{ Name = "after"; Smoke = $afterSmoke }
)) {
    $colorVerdict = [string](Get-NestedValue $leg.Smoke "visualQuality.colorArtifactScan.verdict")
    if ([string]::IsNullOrWhiteSpace($colorVerdict)) {
        $failures += "$($leg.Name) smoke has no color-artifact verdict."
    }
    elseif ($colorVerdict -in (Get-ColorArtifactFailingVerdicts)) {
        $failures += "$($leg.Name) smoke color-artifact verdict is $colorVerdict."
    }
}

$presentedFrameEvidence = [ordered]@{}
foreach ($leg in @(
    [pscustomobject]@{ Name = "before"; Smoke = $beforeSmoke },
    [pscustomobject]@{ Name = "after"; Smoke = $afterSmoke }
)) {
    $validationOk = Get-NestedValue $leg.Smoke "validation.ok"
    $launchOnly = Get-NestedValue $leg.Smoke "validation.launchOnlyProbe"
    $presented = Get-FirstNestedValue $leg.Smoke @(
        "validation.presentedFrames", "log.summary.presented_frames")
    $firstPresented = Get-FirstNestedValue $leg.Smoke @(
        "validation.firstPresentedFrame", "log.summary.first_presented_frame")
    $lastPresented = Get-FirstNestedValue $leg.Smoke @(
        "validation.lastPresentedFrame", "log.summary.last_presented_frame")
    $presentedFrameEvidence[$leg.Name] = [pscustomobject]@{
        presentedFrames = $presented
        firstPresentedFrame = $firstPresented
        lastPresentedFrame = $lastPresented
    }
    if ($validationOk -ne $true) {
        $failures += "$($leg.Name) smoke did not independently pass validation."
    }
    if ($launchOnly -eq $true) {
        $failures += "$($leg.Name) smoke is launch-only and cannot enter a playback A/B."
    }
    if ($null -eq $presented -or [int64]$presented -lt 2) {
        $failures += "$($leg.Name) smoke did not present at least two frames."
    }
    if ($null -eq $firstPresented -or $null -eq $lastPresented -or
        [int64]$firstPresented -eq [int64]$lastPresented) {
        $failures += "$($leg.Name) smoke did not prove displayed-frame advancement."
    }
}
# adversary-de8e3799.json A1: the host load is judged from each smoke file's own text (no root/hostLoad unrolling, a repeated member is unknown)
$hostLoadComparison = Get-HostLoadComparisonEvidence -BeforeSmoke $beforeSmoke -AfterSmoke $afterSmoke -BeforeText $beforeSmokeText -AfterText $afterSmokeText
$hostLoadEvidence = $hostLoadComparison.evidence
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r3 (sol r2 B1): a host-load refusal is no longer a failure (it read FAIL);
# it is kept in hostLoadRefusals and makes the verdict INCONCLUSIVE (Get-GuiSmokeAbVerdict, below).
$hostLoadRefusals = @($hostLoadComparison.failures)

# UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 1): refuse the whole comparison
# -- never merely note it -- when the two legs are not known to have presented on the same
# display identity and mode. FAILs closed on 'unknown' exactly like 'different': an unproven
# match is never treated as a pass.
$displayComparability = Get-DisplayComparability `
    -BeforeDisplay (Get-NestedValue $beforeSmoke "display") `
    -AfterDisplay (Get-NestedValue $afterSmoke "display")
if ($displayComparability.status -ne 'same') {
    # Round 4: the refusal says WHY -- the comparator's own detail and each leg's own
    # identityUnknownReason (e.g. empty EDID fields), never just an opaque reason code.
    $displayWhy = @()
    if ($displayComparability.PSObject.Properties['detail'] -and $displayComparability.detail) {
        $displayWhy += [string]$displayComparability.detail
    }
    foreach ($leg in 'before', 'after') {
        $legBlock = $displayComparability.$leg
        if ($null -ne $legBlock -and $legBlock.identityUnknownReason) {
            $displayWhy += "$leg leg: $($legBlock.identityUnknownReason)"
        }
    }
    $failures += (
        "Display comparability is '$($displayComparability.status)' ($($displayComparability.reasonCode)): " +
        "the before/after legs are not known to have presented on the same display identity and " +
        "mode, so an FPS comparison between them is not meaningful." +
        $(if ($displayWhy.Count -gt 0) { " Why: " + ($displayWhy -join '; ') } else { '' })
    )
}

$beforeLastPresented = $presentedFrameEvidence.before.lastPresentedFrame
$afterLastPresented = $presentedFrameEvidence.after.lastPresentedFrame
if ($null -eq $beforeLastPresented -or $null -eq $afterLastPresented -or
    [int64]$beforeLastPresented -ne [int64]$afterLastPresented) {
    $failures += (
        "Screenshot A/B is not frame-locked: before lastPresentedFrame=" +
        "$beforeLastPresented, after lastPresentedFrame=$afterLastPresented."
    )
}
if ($FailOnScreenshotDelta) {
    if ($screenshotCompare.status -ne "compared") {
        $failures += "Screenshot comparison status was $($screenshotCompare.status)."
    }
    elseif (-not $screenshotCompare.sameSha256 -and
            ($screenshotCompare.meanAbsRgbDelta -gt $MaxMeanAbsRgbDelta -or
             $screenshotCompare.changedSampleRatio -gt $MaxChangedSampleRatio)) {
        $failures += (
            "Screenshot delta exceeded threshold: meanAbsRgbDelta=$($screenshotCompare.meanAbsRgbDelta) " +
            "(max $MaxMeanAbsRgbDelta), changedSampleRatio=$($screenshotCompare.changedSampleRatio) " +
            "(max $MaxChangedSampleRatio)."
        )
    }
}

$abVerdict = Get-GuiSmokeAbVerdict -Failures $failures -HostLoadEvidence $hostLoadEvidence
foreach ($referenceLine in @($abVerdict.referenceProvisional)) { Write-Host $referenceLine }

$result = [pscustomobject]@{
    schema = "gui-smoke-ab-compare.v3"
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
    before = [pscustomobject]@{
        jsonPath = $beforeJsonPath
        screenshotPath = $beforeScreenshot
    }
    after = [pscustomobject]@{
        jsonPath = $afterJsonPath
        screenshotPath = $afterScreenshot
    }
    evidenceBindings = $evidenceBindings
    visualStateEvidence = [pscustomobject]$visualStateEvidence
    thresholds = [pscustomobject]@{
        failOnScreenshotDelta = [bool]$FailOnScreenshotDelta
        maxMeanAbsRgbDelta = $MaxMeanAbsRgbDelta
        maxChangedSampleRatio = $MaxChangedSampleRatio
    }
    screenshot = $screenshotCompare
    presentedFrameEvidence = [pscustomobject]$presentedFrameEvidence
    hostLoadEvidence = $hostLoadEvidence
    autoDecision = New-AutoDecisionComparison `
        -BeforeSmoke $beforeSmoke `
        -AfterSmoke $afterSmoke
    displayComparability = $displayComparability
    # UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 1): before/after raw values
    # are always kept (each leg's own FPS is still true regardless of the OTHER leg), but
    # delta/deltaPercent are withheld -- set $null below, right after this block -- whenever
    # displayComparability.status is not 'same', so a comparison across different displays can
    # never present as a meaningful FPS delta.
    fps = [pscustomobject]@{
        visibleBottomLeftGuiFps = New-DeltaObject `
            -BeforeValue (Get-NestedValue $beforeSmoke "playbackFps.visibleBottomLeftGuiFps") `
            -AfterValue (Get-NestedValue $afterSmoke "playbackFps.visibleBottomLeftGuiFps")
        smokePresentedFps = New-DeltaObject `
            -BeforeValue (Get-NestedValue $beforeSmoke "playbackFps.smokePresentedFps") `
            -AfterValue (Get-NestedValue $afterSmoke "playbackFps.smokePresentedFps")
        smokeTimelineFps = New-DeltaObject `
            -BeforeValue (Get-NestedValue $beforeSmoke "playbackFps.smokeTimelineFps") `
            -AfterValue (Get-NestedValue $afterSmoke "playbackFps.smokeTimelineFps")
    }
    quality = [pscustomobject]@{
        beforeMode = Get-NestedValue $beforeSmoke "visualQuality.qualityModeLast"
        afterMode = Get-NestedValue $afterSmoke "visualQuality.qualityModeLast"
        beforeScaleRequest = Get-NestedValue $beforeSmoke "visualQuality.scaleRequestLast"
        afterScaleRequest = Get-NestedValue $afterSmoke "visualQuality.scaleRequestLast"
        beforeScaleActive = Get-NestedValue $beforeSmoke "visualQuality.scaleActiveLast"
        afterScaleActive = Get-NestedValue $afterSmoke "visualQuality.scaleActiveLast"
        beforePipeline = Get-NestedValue $beforeSmoke "visualQuality.glOutputProof.pipelineStatus"
        afterPipeline = Get-NestedValue $afterSmoke "visualQuality.glOutputProof.pipelineStatus"
        beforeColorArtifactVerdict = Get-NestedValue $beforeSmoke "visualQuality.colorArtifactScan.verdict"
        afterColorArtifactVerdict = Get-NestedValue $afterSmoke "visualQuality.colorArtifactScan.verdict"
    }
    failures = $failures
    hostLoadRefusals = @($hostLoadRefusals)
    referenceProvisional = @($abVerdict.referenceProvisional)
    verdict = $abVerdict.verdict
}

# UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 1): withheld here, AFTER
# $result exists, rather than skipping New-DeltaObject above -- each leg's own raw FPS value
# is still true regardless of the other leg, and is kept; only delta/deltaPercent (the
# cross-leg comparison) are nulled when the legs are not known to be the same display/mode.
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (fable key3 r5, GUI-SMOKE-AB-INCONCLUSIVE-WITHHOLD-DELTA-1): an INCONCLUSIVE verdict (a leg whose host load is
# provisional or unknown) withholds the cross-leg deltas the same way, so no fps delta is printed or stored beside it.
if ($displayComparability.status -ne 'same' -or [string]::Equals([string]$result.verdict, 'INCONCLUSIVE', [StringComparison]::Ordinal)) {
    foreach ($fpsField in @('visibleBottomLeftGuiFps', 'smokePresentedFps', 'smokeTimelineFps')) {
        $result.fps.$fpsField.delta = $null
        $result.fps.$fpsField.deltaPercent = $null
    }
}
# adversary-de8e3799.json A2: an INCONCLUSIVE comparison publishes no cross-leg delta at all: every autoDecision delta object (averageMs,
# averageFpsEquivalent, targetFps, ...) loses its delta and deltaPercent too, in the JSON and in the summary line below.
if ([string]::Equals([string]$result.verdict, 'INCONCLUSIVE', [StringComparison]::Ordinal) -and $null -ne $result.autoDecision) {
    foreach ($adProp in @($result.autoDecision.PSObject.Properties)) {
        $adVal = $adProp.Value
        if ($null -ne $adVal -and $adVal -isnot [string] -and $adVal -isnot [ValueType] -and $null -ne $adVal.PSObject.Properties['delta']) {
            $adVal.delta = $null
            if ($null -ne $adVal.PSObject.Properties['deltaPercent']) { $adVal.deltaPercent = $null }
        }
    }
}
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (adversary-07b6a932.json A7): the screenshot comparison is a cross-leg delta too. An INCONCLUSIVE
# comparison keeps each leg's own image metadata but withholds every before-vs-after figure (sameSha256, meanAbsRgbDelta, maxAbsRgbDelta,
# changedSampleRatio, sampledPixels), in the JSON and in the summary line below; status reads withheld-inconclusive.
# adversary-c2bbe5f8.json A1: the -FailOnScreenshotDelta failure text names the figures too, so an INCONCLUSIVE report keeps the failure but not its numbers.
if ([string]::Equals([string]$result.verdict, 'INCONCLUSIVE', [StringComparison]::Ordinal)) {
    $result.failures = @(@($result.failures) | ForEach-Object {
        if (([string]$_).StartsWith('Screenshot delta exceeded threshold', [StringComparison]::Ordinal)) { 'Screenshot delta exceeded threshold (figures withheld: the verdict is INCONCLUSIVE).' } else { $_ }
    })
}
if ([string]::Equals([string]$result.verdict, 'INCONCLUSIVE', [StringComparison]::Ordinal) -and $null -ne $result.screenshot) {
    $result.screenshot = [pscustomobject]@{
        status = 'withheld-inconclusive'
        reason = 'verdict-inconclusive'
        comparedStatus = [string]$result.screenshot.status
        before = $result.screenshot.before
        after = $result.screenshot.after
        sameSha256 = $null
        sampledPixels = $null
        meanAbsRgbDelta = $null
        maxAbsRgbDelta = $null
        changedSampleRatio = $null
    }
}

$json = $result | ConvertTo-Json -Depth 24
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $outputDir = Split-Path -Parent $resolvedOutput
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R3-MERGE-RESIDUE-1 r1 (adversary-55dcf49e.json A2 and A7): the report is NEVER written through a handle on the path it names. The old
    # code opened that path (OpenOrCreate), judged its identity, then SetLength(0)/Write on the same open handle, so an input path swapped for a link to the report
    # between the last identity check and the write was truncated and rewritten, and a refused or half-finished write left a partial report (or the folder this call
    # made). Now the bytes go to a NEW file (CreateNew: it can alias nothing) beside the report, and only a complete one is moved over the report name: a rename replaces
    # the directory entry and never touches the inode any other name (an input, a link, a symlink target) still points at. Any refusal or failure removes that
    # temporary file and, when this call created it, the empty folder; the report that was there before is left exactly as it was.
    $createdDir = $false
    $tempOutput = $null
    $wroteOutput = $false
    $outBytes = [Text.UTF8Encoding]::new($false).GetBytes($json + [Environment]::NewLine)
    # adversary-6596e055.json A2: New-Item -Force makes EVERY missing level of the folder, so the topmost missing one is what this call created (the cleanup removes the chain up to it)
    $firstMissingDir = $null
    if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
        for ($probe = $outputDir; -not [string]::IsNullOrEmpty($probe) -and -not (Test-Path -LiteralPath $probe -PathType Container); $probe = Split-Path -Parent $probe) { $firstMissingDir = $probe }
    }
    try {
        if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
            $createdDir = $null -ne $firstMissingDir
            New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
        }
        # a report that already exists is judged by its handle identity (read-only, nothing truncated); an input that IS it is refused before anything is written
        $outIdentity = $null
        if (Test-Path -LiteralPath $resolvedOutput -PathType Leaf) {
            $outStream = [IO.FileStream]::new($resolvedOutput, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            try { $outIdentity = Get-SmokeFileIdentity -Stream $outStream -ResolvedPath $resolvedOutput } finally { $outStream.Dispose() }
        }
        if ($null -ne $outIdentity) {
            foreach ($inputIdentity in @($beforeSource.identity, $afterSource.identity)) {
                if ($null -eq $inputIdentity -or [string]::Equals([string]$inputIdentity, $outIdentity, [StringComparison]::Ordinal)) { throw "Output must not overwrite either smoke JSON input." }
            }
        }
        # adversary-7efea617.json A1: the identities above were read when the inputs were first opened. An input path swapped for a hard link to the report since then is
        # the report, so the CURRENT file each input path names is read again here through a fresh handle; one that cannot be read is refused, nothing written.
        foreach ($currentLeg in @([pscustomobject]@{ Name = 'before'; Path = $beforeJsonPath }, [pscustomobject]@{ Name = 'after'; Path = $afterJsonPath })) {
            $currentStream = $null
            try {
                $currentStream = [IO.FileStream]::new($currentLeg.Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
                $currentIdentity = Get-SmokeFileIdentity -Stream $currentStream -ResolvedPath $currentLeg.Path
            }
            catch { throw "Output refused: the current file identity of the $($currentLeg.Name) smoke JSON could not be read at the write ($($_.Exception.Message -replace '\s+', ' ')); nothing was written." }
            finally { if ($null -ne $currentStream) { $currentStream.Dispose() } }
            if ($null -ne $outIdentity -and [string]::Equals([string]$currentIdentity, $outIdentity, [StringComparison]::Ordinal)) { throw "Output must not overwrite either smoke JSON input." }
        }
        $tempOutput = Join-Path $outputDir ('.' + [IO.Path]::GetFileName($resolvedOutput) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
        $tempStream = [IO.FileStream]::new($tempOutput, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $tempStream.Write($outBytes, 0, $outBytes.Length)
            $tempStream.Flush($true)
        }
        finally { $tempStream.Dispose() }
        [IO.File]::Move($tempOutput, $resolvedOutput, $true)
        $tempOutput = $null
        $wroteOutput = $true
    }
    finally {
        if ($null -ne $tempOutput) { try { Remove-Item -LiteralPath $tempOutput -Force -ErrorAction Stop } catch { } }
        if (-not $wroteOutput -and $createdDir) {
            # the folders this call made (the deepest up to the topmost missing one) are removed only while each is empty (a file another process put there is never ours to delete)
            for ($cleanDir = $outputDir; -not [string]::IsNullOrEmpty($cleanDir); $cleanDir = Split-Path -Parent $cleanDir) {
                try { if (@(Get-ChildItem -LiteralPath $cleanDir -Force -ErrorAction Stop).Count -eq 0) { Remove-Item -LiteralPath $cleanDir -Force -ErrorAction Stop } else { break } } catch { break }
                if ([string]::Equals($cleanDir, $firstMissingDir, [StringComparison]::OrdinalIgnoreCase)) { break }
            }
        }
    }
}

Write-Host ((
    "GUI-SMOKE-AB verdict={0} display_comparable={1} screenshot_status={2} mean_abs_rgb_delta={3} " +
    "changed_sample_ratio={4} gui_fps_delta={5} presented_fps_delta={6} " +
    "auto_reason_before={7} auto_reason_after={8} auto_avg_ms_delta={9} " +
    "auto_avg_fps_eq_delta={10} host_load_before={11} host_load_after={12} output={13}") -f
    $result.verdict,
    $displayComparability.status,
    $result.screenshot.status,
    $result.screenshot.meanAbsRgbDelta,
    $result.screenshot.changedSampleRatio,
    $result.fps.visibleBottomLeftGuiFps.delta,
    $result.fps.smokePresentedFps.delta,
    $result.autoDecision.reason.before,
    $result.autoDecision.reason.after,
    $result.autoDecision.averageMs.delta,
    $result.autoDecision.averageFpsEquivalent.delta,
    $result.hostLoadEvidence.before.state,
    $result.hostLoadEvidence.after.state,
    $(if ([string]::IsNullOrWhiteSpace($Output)) { "<stdout-json>" } else { $resolvedOutput })
)

$json

# An INCONCLUSIVE comparison is not a pass: it exits 1 as the host-load refusal always did.
if ($failures.Count -gt 0 -or $result.verdict -ne "PASS") {
    exit 1
}
