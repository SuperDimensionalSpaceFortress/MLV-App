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

function Read-SmokeJson {
    param([string]$Path)

    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    if (-not (Test-Path -LiteralPath $resolved)) {
        throw "Smoke JSON not found: $resolved"
    }
    Get-Content -LiteralPath $resolved -Raw | ConvertFrom-Json -Depth 100
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
        [string]$JsonPath
    )

    $json = Resolve-ExistingFileBinding -Path $JsonPath -Label "$Name smoke JSON"
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
    param(
        [object]$BeforeSmoke,
        [object]$AfterSmoke
    )

    $evidence = [ordered]@{}
    $failures = @()
    foreach ($leg in @(
        [pscustomobject]@{ Name = "before"; Smoke = $BeforeSmoke },
        [pscustomobject]@{ Name = "after"; Smoke = $AfterSmoke }
    )) {
        $legHostLoad = Get-NestedValue $leg.Smoke "hostLoad"
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
        $legProvisionalRaw = if ($null -eq $legHostLoad) { $null } else { Get-NestedValue $legHostLoad "provisional" }
        $legProvisionalDeclared = if ($null -eq $legHostLoad -or $null -eq $legProvisionalRaw) { $true } else { [bool]$legProvisionalRaw }
        $legStateRaw = if ($null -eq $legHostLoad) { $null } else { Get-NestedValue $legHostLoad "state" }
        $legState = if ($null -eq $legHostLoad -or [string]::IsNullOrWhiteSpace([string]$legStateRaw)) { "unknown" } else { [string]$legStateRaw }
        $legProvisional = ($legProvisionalDeclared -or $legState -ne "quiet")
        $legReason = if ($null -eq $legHostLoad) {
            "no hostLoad telemetry recorded in the smoke result"
        } else {
            [string](Get-NestedValue $legHostLoad "reason")
        }
        $evidence[$leg.Name] = [pscustomobject]@{
            state = $legState
            provisional = $legProvisional
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

$beforeSmoke = Read-SmokeJson -Path $Before
$afterSmoke = Read-SmokeJson -Path $After
$beforeJsonPath = (Resolve-Path -LiteralPath $Before).Path
$afterJsonPath = (Resolve-Path -LiteralPath $After).Path
$resolvedOutput = $null
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $resolvedOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
    if ([string]::Equals($resolvedOutput, $beforeJsonPath, [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($resolvedOutput, $afterJsonPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Output must not overwrite either smoke JSON input."
    }
}
$beforeScreenshot = Resolve-SmokeScreenshotPath -Smoke $beforeSmoke
$afterScreenshot = Resolve-SmokeScreenshotPath -Smoke $afterSmoke
$screenshotCompare = Compare-SmokeScreenshots `
    -BeforePath $beforeScreenshot `
    -AfterPath $afterScreenshot `
    -RequestedSampleStep $SampleStep

$failures = @()
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
        -Name "before" -Smoke $beforeSmoke -JsonPath $beforeJsonPath
    $afterBinding = Get-LegEvidenceBinding `
        -Name "after" -Smoke $afterSmoke -JsonPath $afterJsonPath
    $evidenceBindings = [pscustomobject]@{
        before = $beforeBinding
        after = $afterBinding
    }
    if ([string]::Equals($beforeBinding.json.path, $afterBinding.json.path, [StringComparison]::OrdinalIgnoreCase)) {
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
$hostLoadComparison = Get-HostLoadComparisonEvidence -BeforeSmoke $beforeSmoke -AfterSmoke $afterSmoke
$hostLoadEvidence = $hostLoadComparison.evidence
$failures += @($hostLoadComparison.failures)

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
    verdict = if ($failures.Count -eq 0) { "PASS" } else { "FAIL" }
}

$json = $result | ConvertTo-Json -Depth 24
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $outputDir = Split-Path -Parent $resolvedOutput
    if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
        New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
    }
    Set-Content -LiteralPath $resolvedOutput -Value $json -Encoding UTF8
}

Write-Host ((
    "GUI-SMOKE-AB verdict={0} screenshot_status={1} mean_abs_rgb_delta={2} " +
    "changed_sample_ratio={3} gui_fps_delta={4} presented_fps_delta={5} " +
    "auto_reason_before={6} auto_reason_after={7} auto_avg_ms_delta={8} " +
    "auto_avg_fps_eq_delta={9} host_load_before={10} host_load_after={11} output={12}") -f
    $result.verdict,
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

if ($failures.Count -gt 0) {
    exit 1
}
