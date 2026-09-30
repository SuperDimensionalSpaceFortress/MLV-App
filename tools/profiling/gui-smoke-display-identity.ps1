# gui-smoke-display-identity.ps1 -- the ONE parser for the display identity the app logs
# (UM-DISPLAY-SELECT-AND-LOG-1 round 3, binding design-review item opus-blocker-1).
#
# WHY ONE. MainWindow.cpp logs three lines per smoke run: gui_smoke.display_screen (one per
# attached QScreen), gui_smoke.display_target and gui_smoke.window_placement. Two consumers read
# them: the attribution job (playback-attr-3-cuda-job.ps1, on the Bachelor/Ultra-Magnus host) and
# the smoke runner (run-release-gui-smoke.ps1), whose result.json feeds
# compare-release-gui-smoke-ab.ps1's comparator. Each used to carry its own regexes, and they
# disagreed: the job's display_screen pattern was field-order strict and needed every field, the
# runner's read keys by name; the job took the FIRST display_screen line for a screen name, the
# runner the LAST; both window_placement patterns were field-order strict. Two parsers that can
# disagree publish two different identities for one leg -- which is what this file removes.
#
# HOW IT REACHES EACH CONSUMER.
#   - the runner dot-sources this file (a pinned member of the smoke-runner closure,
#     Get-AttrCudaSmokeRunnerClosureManifest, so it is staged next to the runner);
#   - the job embeds these functions VERBATIM, extracted at generation time from this file's
#     COMMITTED bytes at -SourceCommit (the same bytes the closure stages) by
#     Get-AttrCudaEmbeddedFunctionSource -ModulePath;
#   - AttrCudaArtifacts.psm1's Get-AttrCudaGuiSmokeDisplaySelection is a thin delegate to
#     ConvertFrom-GuiSmokeDisplayLog (no parsing of its own).
# Every function opens `function <Name> {` at column 0 and closes with `}` at column 0 with no
# unindented `}` inside -- the contract Get-AttrCudaEmbeddedFunctionSource extracts by. Do not
# set strict mode here: a dot-sourced file's Set-StrictMode would leak into the runner's scope.
#
# PARSING RULE. Fields are read BY KEY, in any order, from the text after the line's marker, by
# one sequential tokenizer that consumes a quoted value whole (so a value cannot inject a key).
# The FIRST occurrence of a key on a line wins. A required key that is absent or malformed makes
# the whole line a shape error (a third state with a reason), never a guess.

function ConvertFrom-GuiSmokeLogFields {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Line,

        [Parameter(Mandatory = $true)]
        [string]$Marker
    )

    $fields = @{}
    $at = $Line.IndexOf($Marker, [StringComparison]::Ordinal)
    if ($at -lt 0) { return $fields }
    $rest = $Line.Substring($at + $Marker.Length)
    $tokenPattern = '(?<!\w)(?:(?<sk>geometry|window)=(?<sx>-?\d+),(?<sy>-?\d+) (?<sw>\d+)x(?<sh>\d+)' +
        '|(?<k>[A-Za-z_]\w*)=(?:"(?<q>[^"]*)"|(?<v>[^\s"]\S*)?))'
    foreach ($m in [regex]::Matches($rest, $tokenPattern)) {
        if ($m.Groups['sk'].Success) {
            $key = $m.Groups['sk'].Value
            if (-not $fields.ContainsKey($key)) {
                $fields[$key] = [pscustomobject]@{
                    x = [int]$m.Groups['sx'].Value
                    y = [int]$m.Groups['sy'].Value
                    width = [int]$m.Groups['sw'].Value
                    height = [int]$m.Groups['sh'].Value
                }
            }
        } else {
            $key = $m.Groups['k'].Value
            if ($fields.ContainsKey($key)) { continue }
            if ($m.Groups['q'].Success) {
                $fields[$key] = $m.Groups['q'].Value
            } elseif ($m.Groups['v'].Success) {
                $fields[$key] = $m.Groups['v'].Value
            } else {
                $fields[$key] = ''
            }
        }
    }
    return $fields
}

function ConvertFrom-GuiSmokeDisplayLog {
    <#
    .SYNOPSIS
    Parses the app's own gui_smoke.display_screen / display_target / window_placement lines
    (MainWindow.cpp, UM-DISPLAY-SELECT-AND-LOG-1) out of raw smoke-log text.
    .DESCRIPTION
    Independent of any Windows-API view: this is what the APP itself reported choosing and
    presenting on. Each of the three lines is optional in the parse -- an older build or a run
    that never reached that log statement leaves the corresponding field $null with its own
    *Error reason, never guessed or defaulted. The LAST matching line wins for
    display_target/window_placement; display_screen is logged once per attached QScreen so every
    matching line is kept, in log order.
    .OUTPUTS
    [pscustomobject] { screensCollected (bool); screens (array of {index,name,manufacturer,model,
    serial,device,geometryX,geometryY,width,height,physicalWidth,physicalHeight,devicePixelRatio,
    refreshHz,primary}); screensError; target ({name,reason,candidates,fallback,preferred,
    preferredMatched} or $null); targetError; placement ({mode,screenName,verified,windowX,
    windowY,windowWidth,windowHeight,previewWidth,previewHeight,targetScreenName,
    presentationScreenName,presentationPhysicalWidth,presentationPhysicalHeight} or $null);
    placementError }. manufacturer/model/serial read $null on a display_screen line that predates
    them; targetScreenName/presentationScreenName/presentationPhysical* and
    preferred/preferredMatched read $null on a line that predates them.
    `name` is whatever QScreen::name() reported -- on a monitor with an EDID name that is the
    FRIENDLY name ("PA329C"), NOT the GDI device name (measured on UM, Qt 6.10.2; see
    tests/fixtures/display/um-qscreen-windows-mapping-20260930.txt). `device` is the Windows GDI
    device (\\.\DISPLAYn) the app DERIVED for that screen from its native origin and physical size
    (platform/qt/DisplayDeviceMapping.h): "" when it could not be established uniquely, $null on a
    line that predates the field. It is the bridge to the Windows inventory; `name` is not.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$LogText
    )

    $result = [ordered]@{
        screensCollected = $false
        screens = @()
        screensError = $null
        target = $null
        targetError = $null
        placement = $null
        placementError = $null
    }

    $lines = @($LogText -split "`r?`n")
    $screenMarker = 'gui_smoke.display_screen '
    $targetMarker = 'gui_smoke.display_target '
    $placementMarker = 'gui_smoke.window_placement '

    try {
        $screens = New-Object System.Collections.Generic.List[object]
        foreach ($line in $lines) {
            if ($line.IndexOf($screenMarker, [StringComparison]::Ordinal) -lt 0) { continue }
            $f = ConvertFrom-GuiSmokeLogFields -Line $line -Marker $screenMarker
            if (-not ($f.ContainsKey('index') -and $f.ContainsKey('name') -and $f.ContainsKey('geometry') -and
                    $f.ContainsKey('physical') -and $f.ContainsKey('dpr') -and $f.ContainsKey('refresh_hz') -and
                    $f.ContainsKey('primary'))) { continue }
            $physical = [regex]::Match([string]$f['physical'], '^(?<w>\d+)x(?<h>\d+)$')
            if (-not $physical.Success) { continue }
            if ([string]$f['index'] -notmatch '^\d+$') { continue }
            if ([string]$f['dpr'] -notmatch '^[0-9.]+$' -or [string]$f['refresh_hz'] -notmatch '^[0-9.]+$') { continue }
            if ([string]$f['primary'] -notmatch '^[01]$') { continue }
            $screens.Add([ordered]@{
                index = [int]$f['index']
                name = [string]$f['name']
                manufacturer = $(if ($f.ContainsKey('manufacturer')) { [string]$f['manufacturer'] } else { $null })
                model = $(if ($f.ContainsKey('model')) { [string]$f['model'] } else { $null })
                serial = $(if ($f.ContainsKey('serial')) { [string]$f['serial'] } else { $null })
                device = $(if ($f.ContainsKey('device')) { [string]$f['device'] } else { $null })
                geometryX = $f['geometry'].x
                geometryY = $f['geometry'].y
                width = $f['geometry'].width
                height = $f['geometry'].height
                physicalWidth = [int]$physical.Groups['w'].Value
                physicalHeight = [int]$physical.Groups['h'].Value
                devicePixelRatio = [double]$f['dpr']
                refreshHz = [double]$f['refresh_hz']
                primary = ([string]$f['primary'] -eq '1')
            })
        }
        if ($screens.Count -gt 0) {
            $result.screensCollected = $true
            $result.screens = $screens.ToArray()
        } else {
            $result.screensError = 'no gui_smoke.display_screen lines found in the smoke log'
        }
    } catch {
        $result.screensCollected = $false
        $result.screens = @()
        $result.screensError = $_.Exception.GetType().Name
    }

    try {
        $targetLine = $null
        foreach ($line in $lines) {
            if ($line.IndexOf($targetMarker, [StringComparison]::Ordinal) -ge 0) { $targetLine = $line }
        }
        if ($null -eq $targetLine) {
            $result.targetError = 'no gui_smoke.display_target line found in the smoke log'
        } else {
            $f = ConvertFrom-GuiSmokeLogFields -Line $targetLine -Marker $targetMarker
            $shapeOk = ($f.ContainsKey('screen') -and $f.ContainsKey('reason') -and $f.ContainsKey('candidates') -and
                $f.ContainsKey('fallback') -and ([string]$f['reason'] -ne '') -and
                ([string]$f['candidates'] -match '^\d+$') -and ([string]$f['fallback'] -match '^[01]$'))
            if (-not $shapeOk) {
                $result.targetError = 'gui_smoke.display_target line did not match the expected shape'
            } else {
                $result.target = [ordered]@{
                    name = [string]$f['screen']
                    reason = [string]$f['reason']
                    candidates = [int]$f['candidates']
                    fallback = ([string]$f['fallback'] -eq '1')
                    preferred = $(if ($f.ContainsKey('preferred') -and $f.ContainsKey('preferred_matched')) { [string]$f['preferred'] } else { $null })
                    preferredMatched = $(if ($f.ContainsKey('preferred') -and $f.ContainsKey('preferred_matched')) { [string]$f['preferred_matched'] } else { $null })
                }
            }
        }
    } catch {
        $result.targetError = $_.Exception.GetType().Name
    }

    try {
        $placementLine = $null
        foreach ($line in $lines) {
            if ($line.IndexOf($placementMarker, [StringComparison]::Ordinal) -ge 0) { $placementLine = $line }
        }
        if ($null -eq $placementLine) {
            $result.placementError = 'no gui_smoke.window_placement line found in the smoke log'
        } else {
            $f = ConvertFrom-GuiSmokeLogFields -Line $placementLine -Marker $placementMarker
            $preview = $null
            if ($f.ContainsKey('preview')) { $preview = [regex]::Match([string]$f['preview'], '^(?<w>\d+)x(?<h>\d+)$') }
            $shapeOk = ($f.ContainsKey('mode') -and $f.ContainsKey('screen') -and $f.ContainsKey('verified') -and
                $f.ContainsKey('window') -and $null -ne $preview -and $preview.Success -and
                ([string]$f['mode'] -match '^\w+$') -and ([string]$f['verified'] -match '^[01]$'))
            if (-not $shapeOk) {
                $result.placementError = 'gui_smoke.window_placement line did not match the expected shape'
            } else {
                $presentationPhysical = $null
                if ($f.ContainsKey('presentation_physical')) {
                    $pp = [regex]::Match([string]$f['presentation_physical'], '^(?<w>\d+)x(?<h>\d+)$')
                    if ($pp.Success) { $presentationPhysical = $pp }
                }
                $result.placement = [ordered]@{
                    mode = [string]$f['mode']
                    screenName = [string]$f['screen']
                    verified = ([string]$f['verified'] -eq '1')
                    windowX = $f['window'].x
                    windowY = $f['window'].y
                    windowWidth = $f['window'].width
                    windowHeight = $f['window'].height
                    previewWidth = [int]$preview.Groups['w'].Value
                    previewHeight = [int]$preview.Groups['h'].Value
                    targetScreenName = $(if ($f.ContainsKey('target_screen')) { [string]$f['target_screen'] } else { $null })
                    presentationScreenName = $(if ($f.ContainsKey('presentation_screen')) { [string]$f['presentation_screen'] } else { $null })
                    presentationPhysicalWidth = $(if ($null -ne $presentationPhysical) { [int]$presentationPhysical.Groups['w'].Value } else { $null })
                    presentationPhysicalHeight = $(if ($null -ne $presentationPhysical) { [int]$presentationPhysical.Groups['h'].Value } else { $null })
                }
            }
        }
    } catch {
        $result.placementError = $_.Exception.GetType().Name
    }

    [pscustomobject]$result
}

function Find-GuiSmokeDisplayScreen {
    <#
    .SYNOPSIS
    The ONE lookup rule for "the display_screen record for this screen name": case-insensitive
    name match; one screen logged again (same index) is the LAST record; two DIFFERENT screens
    (distinct indexes) that carry the name are ambiguous and match nothing.
    .DESCRIPTION
    Every consumer that resolves a screen name to its display_screen record -- this file's
    Get-GuiSmokeDisplayIdentity and the attribution job's Build-AttrCudaDisplayBlock (target and
    presentation blocks) -- goes through this function, so one summary.json can never carry two
    different refresh/size values for one screen name (UM-DISPLAY-SELECT-AND-LOG-1 round 4,
    fable DISPLAY-BLOCK-LOOKUP-LAST-WINS-1). Returns $null when no record carries the name.
    UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1: QScreen::name() is the EDID friendly name on a monitor
    that has one, and two monitors of one model share it -- the app logs only that name in
    display_target/window_placement, so a name that belongs to more than one attached screen cannot
    say WHICH one presented. That is the third state ($null, never "whichever came last", which would
    publish the other monitor's serial and refresh); Get-GuiSmokeDisplayIdentity names the reason.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()]
        $Screens,

        [AllowNull()]
        [AllowEmptyString()]
        [string]$Name
    )

    $found = $null
    $indexes = @{}
    if ([string]::IsNullOrEmpty($Name)) { return $null }
    foreach ($candidate in @($Screens)) {
        if ($null -ne $candidate -and [string]$candidate.name -ieq $Name) {
            $found = $candidate
            $indexes[[string]$candidate.index] = $true
        }
    }
    if ($indexes.Count -gt 1) { return $null }
    return $found
}

function Get-GuiSmokeDisplayIdentity {
    <#
    .SYNOPSIS
    The identity/mode block a leg publishes, derived from a ConvertFrom-GuiSmokeDisplayLog result.
    .DESCRIPTION
    Physical identity is name + manufacturer + model + serial of the screen the leg ACTUALLY
    presented on (window_placement's presentation_screen, never the merely-intended target),
    plus its physical size, rounded refresh, DPR, window mode, preview size and whether the app's
    own placement verification passed. Anything that cannot be known is the third state: every
    identity field $null, verified $false and identityUnknownReason set -- never a "known" block
    with null-valued fields (two of those used to compare 'same'). A screen line that predates
    manufacturer/model/serial is identity-unknown too, since the \\.\DISPLAYn ordinal alone can
    be reused by a different monitor between two legs. An EMPTY (or whitespace-only) manufacturer,
    model or serial is NOT a known value either (round 4, sol BLOCKER): Qt reports an empty string
    for a display with no EDID descriptor, and two different displays that reuse one device name
    would then agree on every "field" and compare 'same'. Empty means identity UNKNOWN, so the
    comparison is refused and the reason names the empty fields. When two display_screen lines
    carry the same name (case-insensitively) the LAST one wins (Find-GuiSmokeDisplayScreen).
    .OUTPUTS
    [pscustomobject] { presentationScreenName; presentationManufacturer; presentationModel;
    presentationSerial; physicalWidth; physicalHeight; refreshHzRounded; dpr; windowMode;
    previewWidth; previewHeight; verified; identityUnknownReason }.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()]
        $Selection
    )

    $unknownReason = $null
    $placement = $null
    $screen = $null
    if ($null -eq $Selection) {
        $unknownReason = 'no display selection available'
    } else {
        $placement = $Selection.placement
        if ($null -eq $placement) {
            $unknownReason = $(if ($Selection.placementError) { [string]$Selection.placementError } else { 'no gui_smoke.window_placement line found in the smoke log' })
        } elseif ($null -eq $placement.presentationScreenName) {
            $unknownReason = 'legacy binary: window_placement predates presentation_screen= (verified/mode/preview still known but identity is not)'
        } elseif ($null -eq $placement.presentationPhysicalWidth -or $null -eq $placement.presentationPhysicalHeight) {
            $unknownReason = 'window_placement has presentation_screen= but no readable presentation_physical='
        } else {
            $screen = Find-GuiSmokeDisplayScreen -Screens $Selection.screens -Name ([string]$placement.presentationScreenName)
            if ($null -eq $screen) {
                # A presentation screen with no matching display_screen record is identity UNKNOWN
                # -- refresh/DPR/model/serial are all unknowable. A name shared by several attached
                # screens is UNKNOWN too (Find-GuiSmokeDisplayScreen), with its own reason.
                $sameName = @(@($Selection.screens) | Where-Object { $null -ne $_ -and [string]$_.name -ieq [string]$placement.presentationScreenName })
                if ($sameName.Count -gt 1) {
                    $unknownReason = 'presentation screen name is shared by ' + $sameName.Count + ' attached screens (indexes ' +
                        (($sameName | ForEach-Object { [string]$_.index } | Sort-Object -Unique) -join ',') +
                        '): the name cannot tell them apart, so identity is not knowable'
                } else {
                    $unknownReason = 'presentation screen has no matching gui_smoke.display_screen line'
                }
            } elseif ($null -eq $screen.manufacturer -or $null -eq $screen.model -or $null -eq $screen.serial) {
                $unknownReason = 'presentation screen display_screen line predates manufacturer/model/serial (identity not knowable)'
            } else {
                $emptyFields = @()
                if ([string]::IsNullOrWhiteSpace([string]$screen.manufacturer)) { $emptyFields += 'manufacturer' }
                if ([string]::IsNullOrWhiteSpace([string]$screen.model)) { $emptyFields += 'model' }
                if ([string]::IsNullOrWhiteSpace([string]$screen.serial)) { $emptyFields += 'serial' }
                if ($emptyFields.Count -gt 0) {
                    $unknownReason = 'presentation screen reports empty ' + ($emptyFields -join '/') +
                        ' (no EDID descriptor): displays sharing this device name cannot be told apart, so identity is not knowable'
                }
            }
        }
    }

    if ($null -ne $unknownReason) {
        return [pscustomobject]@{
            presentationScreenName = $null
            presentationManufacturer = $null
            presentationModel = $null
            presentationSerial = $null
            physicalWidth = $null
            physicalHeight = $null
            refreshHzRounded = $null
            dpr = $null
            windowMode = $null
            previewWidth = $null
            previewHeight = $null
            verified = $false
            identityUnknownReason = $unknownReason
        }
    }

    [pscustomobject]@{
        presentationScreenName = [string]$placement.presentationScreenName
        presentationManufacturer = [string]$screen.manufacturer
        presentationModel = [string]$screen.model
        presentationSerial = [string]$screen.serial
        physicalWidth = [int]$placement.presentationPhysicalWidth
        physicalHeight = [int]$placement.presentationPhysicalHeight
        refreshHzRounded = [Math]::Round([double]$screen.refreshHz)
        dpr = [double]$screen.devicePixelRatio
        windowMode = [string]$placement.mode
        previewWidth = [int]$placement.previewWidth
        previewHeight = [int]$placement.previewHeight
        verified = [bool]$placement.verified
        identityUnknownReason = $null
    }
}
