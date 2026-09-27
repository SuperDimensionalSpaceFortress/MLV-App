function Convert-GuiSmokeProvenanceLogLineToObject {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Line)

    $result = [ordered]@{}
    foreach ($match in [regex]::Matches($Line, '(?<key>[A-Za-z0-9_]+)=(?<value>"[^"]*"|\S+)')) {
        $key = $match.Groups['key'].Value
        $rawValue = $match.Groups['value'].Value.Trim('"')
        $intValue = 0L
        if ([long]::TryParse($rawValue, [ref]$intValue)) {
            $result[$key] = $intValue
        }
        else {
            $result[$key] = $rawValue
        }
    }
    [pscustomobject]$result
}

function Assert-GuiSmokeChildEvidenceReady {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [Parameter(Mandatory = $true)][bool]$ResultExists,
        [Parameter(Mandatory = $true)][bool]$ScreenshotExists
    )

    $failures = [System.Collections.Generic.List[string]]::new()
    if ($ExitCode -ne 0) {
        $failures.Add("smoke-child-exit-$ExitCode")
    }
    if (-not $ResultExists) {
        $failures.Add('missing-result-json')
    }
    if (-not $ScreenshotExists) {
        $failures.Add('missing-screenshot')
    }
    if ($failures.Count -gt 0) {
        throw "GUI smoke child evidence is unusable: $($failures -join ', ')."
    }

    [pscustomobject]@{
        ok = $true
        exitCode = $ExitCode
        resultExists = $ResultExists
        screenshotExists = $ScreenshotExists
    }
}

function Get-GuiSmokeScreenshotProvenanceV1 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$OrderedLogLines)

    $screenshotIndex = -1
    for ($index = 0; $index -lt $OrderedLogLines.Count; ++$index) {
        if ($OrderedLogLines[$index] -like '*gui_smoke.screenshot*') {
            $screenshotIndex = $index
        }
    }

    $presentIndex = -1
    $manifestIndex = -1
    if ($screenshotIndex -ge 0) {
        for ($index = 0; $index -lt $screenshotIndex; ++$index) {
            if ($OrderedLogLines[$index] -like '*draw_frame_ready.present_content*') {
                $presentIndex = $index
            }
            if ($OrderedLogLines[$index] -like '*playback_smoke.render_manifest*') {
                $manifestIndex = $index
            }
        }
    }

    $presentLine = if ($presentIndex -ge 0) { $OrderedLogLines[$presentIndex] } else { $null }
    $manifestLine = if ($manifestIndex -ge 0) { $OrderedLogLines[$manifestIndex] } else { $null }
    $screenshotLine = if ($screenshotIndex -ge 0) { $OrderedLogLines[$screenshotIndex] } else { $null }
    $present = if ($presentLine) { Convert-GuiSmokeProvenanceLogLineToObject $presentLine } else { $null }
    $manifest = if ($manifestLine) { Convert-GuiSmokeProvenanceLogLineToObject $manifestLine } else { $null }
    $screenshot = if ($screenshotLine) { Convert-GuiSmokeProvenanceLogLineToObject $screenshotLine } else { $null }

    $failures = [System.Collections.Generic.List[string]]::new()
    if ($screenshotIndex -lt 0) {
        $failures.Add('missing-screenshot-event')
    }
    $screenshotMethod = Get-GuiSmokeObjectPropertyValue $screenshot 'method'
    $glWindowActive = Get-GuiSmokeObjectPropertyValue $screenshot 'gl_window_active'
    if ($null -ne $glWindowActive -and [long]$glWindowActive -ne 0 -and
        [string]$screenshotMethod -ne 'gl_window_framebuffer_readback') {
        $failures.Add('gl-window-active-non-gl-capture-method')
    }
    if ($presentIndex -lt 0) {
        $failures.Add('missing-present-content-before-screenshot')
    }
    if ($manifestIndex -lt 0) {
        $failures.Add('missing-render-manifest-before-screenshot')
    }
    if ($presentIndex -ge 0 -and $manifestIndex -ge 0 -and $presentIndex -ge $manifestIndex) {
        $failures.Add('present-manifest-order-mismatch')
    }

    $displayFrame = if ($present) { $present.PSObject.Properties['display_frame'] } else { $null }
    $contentHash = if ($present) { $present.PSObject.Properties['hash'] } else { $null }
    $pathSource = if ($manifest) { $manifest.PSObject.Properties['path_source'] } else { $null }
    $cacheHit = if ($manifest) { $manifest.PSObject.Properties['processed8_cache_hit'] } else { $null }
    $rawPrefetch = if ($manifest) { $manifest.PSObject.Properties['raw_prefetch'] } else { $null }

    if ($presentIndex -ge 0 -and $null -eq $displayFrame) {
        $failures.Add('missing-display-frame')
    }
    if ($presentIndex -ge 0 -and $null -eq $contentHash) {
        $failures.Add('missing-present-content-hash')
    }
    if ($manifestIndex -ge 0 -and $null -eq $pathSource) {
        $failures.Add('missing-render-path-source')
    }
    elseif ($pathSource -and [string]$pathSource.Value -ne 'render_thread') {
        $failures.Add('render-path-not-fresh')
    }
    if ($manifestIndex -ge 0 -and $null -eq $cacheHit) {
        $failures.Add('missing-processed8-cache-hit-state')
    }
    elseif ($cacheHit -and [int64]$cacheHit.Value -ne 0) {
        $failures.Add('processed8-cache-hit')
    }

    [pscustomobject]@{
        schema = 'mlvapp-gui-smoke-screenshot-provenance.v1'
        validFresh = ($failures.Count -eq 0)
        failures = @($failures)
        displayFrame = if ($displayFrame) { [int64]$displayFrame.Value } else { $null }
        contentHash = if ($contentHash) { [string]$contentHash.Value } else { $null }
        pathSource = if ($pathSource) { [string]$pathSource.Value } else { $null }
        processed8CacheHit = if ($cacheHit) { [int64]$cacheHit.Value } else { $null }
        rawPrefetch = if ($rawPrefetch) { [int64]$rawPrefetch.Value } else { $null }
        eventIndices = [pscustomobject]@{
            presentContent = $presentIndex
            renderManifest = $manifestIndex
            screenshot = $screenshotIndex
        }
        raw = [pscustomobject]@{
            presentContent = $presentLine
            renderManifest = $manifestLine
            screenshot = $screenshotLine
        }
    }
}

function Get-GuiSmokeObjectPropertyValue {
    param([object]$Object, [string]$Name)

    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Find-GuiSmokeLogEventIndex {
    param(
        [string[]]$Lines,
        [int]$StartIndex,
        [int]$EndIndex,
        [scriptblock]$Predicate,
        [switch]$Reverse
    )

    if ($Lines.Count -eq 0 -or $StartIndex -lt 0 -or $EndIndex -lt 0) { return -1 }
    if ($Reverse) {
        for ($index = $EndIndex; $index -ge $StartIndex; --$index) {
            if (& $Predicate $Lines[$index]) { return $index }
        }
    }
    else {
        for ($index = $StartIndex; $index -le $EndIndex; ++$index) {
            if (& $Predicate $Lines[$index]) { return $index }
        }
    }
    return -1
}

function Copy-GuiSmokeStableProperties {
    param([object]$Object, [string[]]$Exclude = @())

    $copy = [ordered]@{}
    if ($null -eq $Object) { return [pscustomobject]$copy }
    foreach ($property in $Object.PSObject.Properties) {
        if ($Exclude -notcontains $property.Name) {
            $copy[$property.Name] = $property.Value
        }
    }
    return [pscustomobject]$copy
}

# KNOWN LIMITATION (ATTR3-VISUAL-QUALITY-EVIDENCE-1, part 1; not fixed this round -- carried
# forward as documented follow-up work, not silently accepted). The GPU-recon TEXTURE
# presentation path never emits a draw_frame_ready.present_content line (or its associated
# ready-begin/render-request) for the frame it presents, only the CPU displayImage path does
# (platform/qt/MainWindow.cpp ~:5880-5906). So -RequireFreshScreenshotRender can never pass on
# that texture path today: the chain below always fails at 'missing-associated-present-content'
# (and the hash checks that depend on it), even for a valid, correctly-presented capture. This
# is fail-closed and correct -- it never falsely certifies freshness it cannot prove -- but a red
# result on that path must be read as "provenance not yet wired for this path", never as a flake
# or as evidence of a bad frame. Part 2 is expected to add present-content logging to the texture
# path; until then, treat any texture-path -RequireFreshScreenshotRender run as permanently red.
function Get-GuiSmokeScreenshotProvenanceV2 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$OrderedLogLines,
        [Parameter(Mandatory = $true)][long]$RequestedStartFrame
    )

    $failures = [System.Collections.Generic.List[string]]::new()
    $screenshotIndex = Find-GuiSmokeLogEventIndex `
        -Lines $OrderedLogLines -StartIndex 0 -EndIndex ($OrderedLogLines.Count - 1) `
        -Reverse -Predicate { param($line) $line -like '*gui_smoke.screenshot*' }
    if ($screenshotIndex -lt 0) {
        $failures.Add('missing-screenshot-event')
    }
    $screenshot = if ($screenshotIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$screenshotIndex]
    } else { $null }
    $screenshotPath = Get-GuiSmokeObjectPropertyValue $screenshot 'path'
    $screenshotMethod = Get-GuiSmokeObjectPropertyValue $screenshot 'method'
    $screenshotWidth = Get-GuiSmokeObjectPropertyValue $screenshot 'width'
    $screenshotHeight = Get-GuiSmokeObjectPropertyValue $screenshot 'height'
    if ([string]::IsNullOrWhiteSpace([string]$screenshotPath)) {
        $failures.Add('missing-screenshot-path')
    }
    if ([string]::IsNullOrWhiteSpace([string]$screenshotMethod)) {
        $failures.Add('missing-screenshot-method')
    }
    if ($null -eq $screenshotWidth -or [long]$screenshotWidth -le 0) {
        $failures.Add('missing-screenshot-width')
    }
    if ($null -eq $screenshotHeight -or [long]$screenshotHeight -le 0) {
        $failures.Add('missing-screenshot-height')
    }
    $glWindowActive = Get-GuiSmokeObjectPropertyValue $screenshot 'gl_window_active'
    if ($null -ne $glWindowActive -and [long]$glWindowActive -ne 0 -and
        [string]$screenshotMethod -ne 'gl_window_framebuffer_readback') {
        $failures.Add('gl-window-active-non-gl-capture-method')
    }

    $searchEnd = if ($screenshotIndex -ge 0) { $screenshotIndex - 1 } else { $OrderedLogLines.Count - 1 }
    $frameIndex = Find-GuiSmokeLogEventIndex `
        -Lines $OrderedLogLines -StartIndex 0 -EndIndex $searchEnd `
        -Reverse -Predicate { param($line) $line -like '*playback_smoke.frame *' }
    $frame = if ($frameIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$frameIndex]
    } else { $null }
    if ($frameIndex -lt 0) { $failures.Add('missing-playback-frame-before-screenshot') }

    $session = Get-GuiSmokeObjectPropertyValue $frame 'session'
    $presentationIndex = Get-GuiSmokeObjectPropertyValue $frame 'index'
    $displayFrame = Get-GuiSmokeObjectPropertyValue $frame 'display_frame'
    $requestSerial = Get-GuiSmokeObjectPropertyValue $frame 'serial'
    foreach ($required in @(
        @{ value = $session; failure = 'missing-playback-session' },
        @{ value = $presentationIndex; failure = 'missing-presentation-index' },
        @{ value = $displayFrame; failure = 'missing-display-frame' },
        @{ value = $requestSerial; failure = 'missing-request-serial' }
    )) {
        if ($null -eq $required.value) { $failures.Add($required.failure) }
    }

    # The GL-window path reports, on the screenshot event itself, the presentation serial
    # of the frame grabPresentedFramebufferIfActive() actually captured (see
    # platform/qt/GpuDisplayWindow.cpp paintGL()/grabPresentedFramebufferIfActive()) --
    # this is the frame's OWN account of what it captured, independent of the
    # playback_smoke.frame binding derived above. Cross-checking the two closes the gap
    # where a screenshot could otherwise be attributed to the wrong frame (e.g. queued
    # telemetry running ahead of what was actually captured). Only meaningful for the
    # GL-window readback method: other capture methods never populate this field and
    # legitimately report serial_valid=0, which must not be treated as a failure here.
    $glWindowPresentedSerial = Get-GuiSmokeObjectPropertyValue $screenshot 'gl_window_presented_serial'
    $glWindowPresentedSerialValid = Get-GuiSmokeObjectPropertyValue $screenshot 'gl_window_presented_serial_valid'
    if ([string]$screenshotMethod -eq 'gl_window_framebuffer_readback') {
        if ($null -eq $glWindowPresentedSerialValid) {
            $failures.Add('missing-gl-window-presented-serial-valid')
        }
        elseif ([long]$glWindowPresentedSerialValid -eq 0) {
            $failures.Add('gl-window-presented-serial-invalid')
        }
        if ($null -eq $glWindowPresentedSerial) {
            $failures.Add('missing-gl-window-presented-serial')
        }
        elseif ($null -ne $requestSerial -and [long]$glWindowPresentedSerial -ne [long]$requestSerial) {
            $failures.Add('gl-window-presented-serial-mismatch')
        }
    }

    $manifestIndex = -1
    if ($frameIndex -ge 0 -and $screenshotIndex -gt $frameIndex) {
        $manifestIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex ($frameIndex + 1) -EndIndex ($screenshotIndex - 1) `
            -Predicate {
                param($line)
                if ($line -notlike '*playback_smoke.render_manifest*') { return $false }
                $candidate = Convert-GuiSmokeProvenanceLogLineToObject $line
                return ((Get-GuiSmokeObjectPropertyValue $candidate 'session') -eq $session -and
                    (Get-GuiSmokeObjectPropertyValue $candidate 'index') -eq $presentationIndex)
            }
    }
    $manifest = if ($manifestIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$manifestIndex]
    } else { $null }
    if ($manifestIndex -lt 0) { $failures.Add('missing-associated-render-manifest') }

    $presentIndex = -1
    if ($frameIndex -gt 0) {
        $presentIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex 0 -EndIndex ($frameIndex - 1) `
            -Reverse -Predicate {
                param($line)
                if ($line -notlike '*draw_frame_ready.present_content*') { return $false }
                $candidate = Convert-GuiSmokeProvenanceLogLineToObject $line
                return (Get-GuiSmokeObjectPropertyValue $candidate 'display_frame') -eq $displayFrame
            }
    }
    $present = if ($presentIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$presentIndex]
    } else { $null }
    if ($presentIndex -lt 0) { $failures.Add('missing-associated-present-content') }

    $readyBeginIndex = -1
    if ($presentIndex -gt 0) {
        $readyBeginIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex 0 -EndIndex ($presentIndex - 1) `
            -Reverse -Predicate {
                param($line)
                if ($line -notlike '*draw_frame_ready.begin*') { return $false }
                $candidate = Convert-GuiSmokeProvenanceLogLineToObject $line
                return ((Get-GuiSmokeObjectPropertyValue $candidate 'serial') -eq $requestSerial -and
                    (Get-GuiSmokeObjectPropertyValue $candidate 'display_frame') -eq $displayFrame)
            }
    }
    $readyBegin = if ($readyBeginIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$readyBeginIndex]
    } else { $null }
    if ($readyBeginIndex -lt 0) { $failures.Add('missing-associated-ready-begin') }

    $requestIndex = -1
    if ($readyBeginIndex -gt 0) {
        $requestIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex 0 -EndIndex ($readyBeginIndex - 1) `
            -Reverse -Predicate {
                param($line)
                if ($line -notlike '*draw_frame.request*') { return $false }
                $candidate = Convert-GuiSmokeProvenanceLogLineToObject $line
                return (Get-GuiSmokeObjectPropertyValue $candidate 'serial') -eq $requestSerial
            }
    }
    $request = if ($requestIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$requestIndex]
    } else { $null }
    if ($requestIndex -lt 0) { $failures.Add('missing-associated-render-request') }

    $readyEndIndex = -1
    if ($manifestIndex -ge 0 -and $screenshotIndex -gt $manifestIndex) {
        $readyEndIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex ($manifestIndex + 1) -EndIndex ($screenshotIndex - 1) `
            -Predicate {
                param($line)
                if ($line -notlike '*draw_frame_ready.end*') { return $false }
                $candidate = Convert-GuiSmokeProvenanceLogLineToObject $line
                return ((Get-GuiSmokeObjectPropertyValue $candidate 'serial') -eq $requestSerial -and
                    (Get-GuiSmokeObjectPropertyValue $candidate 'display_frame') -eq $displayFrame)
            }
    }
    if ($readyEndIndex -lt 0) { $failures.Add('missing-associated-ready-end') }

    $startIndex = -1
    if ($frameIndex -gt 0) {
        $startIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex 0 -EndIndex ($frameIndex - 1) `
            -Reverse -Predicate {
                param($line)
                if ($line -notlike '*playback_smoke.start*') { return $false }
                $candidate = Convert-GuiSmokeProvenanceLogLineToObject $line
                return (Get-GuiSmokeObjectPropertyValue $candidate 'session') -eq $session
            }
    }
    $playbackStart = if ($startIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$startIndex]
    } else { $null }
    if ($startIndex -lt 0) { $failures.Add('missing-associated-playback-start') }

    $effectiveStartFrame = Get-GuiSmokeObjectPropertyValue $playbackStart 'position'
    $startSerial = Get-GuiSmokeObjectPropertyValue $playbackStart 'start_serial'
    if ($null -eq $effectiveStartFrame) { $failures.Add('missing-effective-start-frame') }
    elseif ([long]$effectiveStartFrame -ne $RequestedStartFrame) { $failures.Add('requested-effective-start-mismatch') }
    if ($null -eq $startSerial) { $failures.Add('missing-start-serial') }

    $requestedFrame = Get-GuiSmokeObjectPropertyValue $request 'requested_frame'
    $requestGeneration = Get-GuiSmokeObjectPropertyValue $request 'generation'
    $readyGeneration = Get-GuiSmokeObjectPropertyValue $readyBegin 'generation'
    if ($null -eq $requestedFrame) { $failures.Add('missing-requested-frame') }
    elseif ($null -ne $displayFrame -and [long]$requestedFrame -ne [long]$displayFrame) {
        $failures.Add('request-display-frame-mismatch')
    }
    if ($null -eq $requestGeneration -or $null -eq $readyGeneration) {
        $failures.Add('missing-render-generation')
    }
    elseif ([long]$requestGeneration -ne [long]$readyGeneration) {
        $failures.Add('request-ready-generation-mismatch')
    }

    if ($readyBeginIndex -ge 0 -and $presentIndex -ge 0 -and $frameIndex -ge 0 -and
        $manifestIndex -ge 0 -and $readyEndIndex -ge 0 -and $screenshotIndex -ge 0 -and
        -not ($requestIndex -lt $readyBeginIndex -and $readyBeginIndex -lt $presentIndex -and
              $presentIndex -lt $frameIndex -and $frameIndex -lt $manifestIndex -and
              $manifestIndex -lt $readyEndIndex -and $readyEndIndex -lt $screenshotIndex)) {
        $failures.Add('presentation-event-order-mismatch')
    }

    if ($frameIndex -ge 0 -and $screenshotIndex -gt $frameIndex) {
        $laterPresentIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex ($frameIndex + 1) -EndIndex ($screenshotIndex - 1) `
            -Predicate { param($line) $line -like '*draw_frame_ready.present_content*' }
        if ($laterPresentIndex -ge 0) { $failures.Add('later-unassociated-present-content') }
    }

    $contentHash = Get-GuiSmokeObjectPropertyValue $present 'hash'
    if ($null -eq $contentHash) { $failures.Add('missing-present-content-hash') }
    $pathSource = Get-GuiSmokeObjectPropertyValue $manifest 'path_source'
    $cacheHit = Get-GuiSmokeObjectPropertyValue $manifest 'processed8_cache_hit'
    $rawPrefetch = Get-GuiSmokeObjectPropertyValue $manifest 'raw_prefetch'
    if ($null -eq $pathSource) { $failures.Add('missing-render-path-source') }
    elseif ([string]$pathSource -ne 'render_thread') { $failures.Add('render-path-not-fresh') }
    if ($null -eq $cacheHit) { $failures.Add('missing-processed8-cache-hit-state') }
    elseif ([long]$cacheHit -ne 0) { $failures.Add('processed8-cache-hit') }

    $history = [System.Collections.Generic.List[object]]::new()
    if ($startIndex -ge 0 -and $frameIndex -ge $startIndex) {
        for ($index = $startIndex + 1; $index -le $frameIndex; ++$index) {
            if ($OrderedLogLines[$index] -notlike '*playback_smoke.frame *') { continue }
            $historyFrame = Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$index]
            if ((Get-GuiSmokeObjectPropertyValue $historyFrame 'session') -ne $session) { continue }
            $historySerial = Get-GuiSmokeObjectPropertyValue $historyFrame 'serial'
            $historyRequest = $null
            for ($requestSearch = $index - 1; $requestSearch -ge $startIndex; --$requestSearch) {
                if ($OrderedLogLines[$requestSearch] -notlike '*draw_frame.request*') { continue }
                $candidate = Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$requestSearch]
                if ((Get-GuiSmokeObjectPropertyValue $candidate 'serial') -eq $historySerial) {
                    $historyRequest = $candidate
                    break
                }
            }
            $history.Add([pscustomobject]@{
                index = Get-GuiSmokeObjectPropertyValue $historyFrame 'index'
                displayFrame = Get-GuiSmokeObjectPropertyValue $historyFrame 'display_frame'
                serial = $historySerial
                serialOffset = if ($null -ne $startSerial -and $null -ne $historySerial) {
                    [long]$historySerial - [long]$startSerial
                } else { $null }
                requestedFrame = Get-GuiSmokeObjectPropertyValue $historyRequest 'requested_frame'
                generation = Get-GuiSmokeObjectPropertyValue $historyRequest 'generation'
            })
        }
    }
    if ($null -ne $presentationIndex -and $history.Count -ne [long]$presentationIndex) {
        $failures.Add('presentation-history-count-mismatch')
    }

    # State and policy must describe the transaction that produced the selected
    # frame. A later replacement can otherwise make two JSON proofs look equal
    # even though one screenshot was rendered under an earlier state.
    $stateAnchorIndex = @($startIndex, $requestIndex | Where-Object { $_ -ge 0 } |
        Measure-Object -Minimum).Minimum
    $stateSearchEnd = if ($null -ne $stateAnchorIndex) { [int]$stateAnchorIndex - 1 } else { -1 }
    $visualStateIndex = Find-GuiSmokeLogEventIndex `
        -Lines $OrderedLogLines -StartIndex 0 -EndIndex $stateSearchEnd -Reverse `
        -Predicate { param($line) $line -like '*gui_smoke.visual_state*' }
    $policyIndex = Find-GuiSmokeLogEventIndex `
        -Lines $OrderedLogLines -StartIndex 0 -EndIndex $stateSearchEnd -Reverse `
        -Predicate { param($line) $line -like '*gui_smoke.playback_policy*' }
    $visualState = if ($visualStateIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$visualStateIndex]
    } else { $null }
    $playbackPolicy = if ($policyIndex -ge 0) {
        Convert-GuiSmokeProvenanceLogLineToObject $OrderedLogLines[$policyIndex]
    } else { $null }
    if ($null -eq $visualState) { $failures.Add('missing-effective-visual-state') }
    if ($null -eq $playbackPolicy) { $failures.Add('missing-effective-playback-policy') }
    if ($null -ne $stateAnchorIndex -and $screenshotIndex -gt [int]$stateAnchorIndex) {
        $lateVisualStateIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex ([int]$stateAnchorIndex + 1) `
            -EndIndex ($screenshotIndex - 1) `
            -Predicate { param($line) $line -like '*gui_smoke.visual_state*' }
        $latePolicyIndex = Find-GuiSmokeLogEventIndex `
            -Lines $OrderedLogLines -StartIndex ([int]$stateAnchorIndex + 1) `
            -EndIndex ($screenshotIndex - 1) `
            -Predicate { param($line) $line -like '*gui_smoke.playback_policy*' }
        if ($lateVisualStateIndex -ge 0) { $failures.Add('late-visual-state-replacement') }
        if ($latePolicyIndex -ge 0) { $failures.Add('late-playback-policy-replacement') }
    }

    [pscustomobject]@{
        schema = 'mlvapp-gui-smoke-screenshot-provenance.v2'
        validAssociation = -not (@($failures) | Where-Object {
            $_ -notin @('render-path-not-fresh', 'processed8-cache-hit')
        })
        validFresh = ($failures.Count -eq 0)
        failures = @($failures)
        requestedStartFrame = $RequestedStartFrame
        effectiveStartFrame = $effectiveStartFrame
        playbackSession = $session
        startSerial = $startSerial
        presentationIndex = $presentationIndex
        requestSerial = $requestSerial
        requestSerialOffset = if ($null -ne $requestSerial -and $null -ne $startSerial) {
            [long]$requestSerial - [long]$startSerial
        } else { $null }
        requestedFrame = $requestedFrame
        displayFrame = $displayFrame
        generation = $requestGeneration
        contentHash = $contentHash
        pathSource = $pathSource
        processed8CacheHit = $cacheHit
        rawPrefetch = $rawPrefetch
        screenshotPath = $screenshotPath
        screenshotMethod = $screenshotMethod
        screenshotWidth = $screenshotWidth
        screenshotHeight = $screenshotHeight
        glWindowPresentedSerial = $glWindowPresentedSerial
        glWindowPresentedSerialValid = $glWindowPresentedSerialValid
        presentedHistory = @($history)
        effectiveState = [pscustomobject]@{
            visualState = Copy-GuiSmokeStableProperties $visualState @('event')
            playbackPolicy = Copy-GuiSmokeStableProperties $playbackPolicy @('event')
            frame = [pscustomobject]@{
                scale_request = Get-GuiSmokeObjectPropertyValue $frame 'scale_request'
                scale_active = Get-GuiSmokeObjectPropertyValue $frame 'scale_active'
                worker_threads = Get-GuiSmokeObjectPropertyValue $frame 'worker_threads'
                worker_thread_cap_active = Get-GuiSmokeObjectPropertyValue $frame 'worker_thread_cap_active'
                openmp_threads = Get-GuiSmokeObjectPropertyValue $frame 'openmp_threads'
                openmp_thread_cap_active = Get-GuiSmokeObjectPropertyValue $frame 'openmp_thread_cap_active'
            }
            renderManifest = Copy-GuiSmokeStableProperties $manifest @('session', 'index')
        }
        eventIndices = [pscustomobject]@{
            playbackStart = $startIndex
            renderRequest = $requestIndex
            readyBegin = $readyBeginIndex
            presentContent = $presentIndex
            playbackFrame = $frameIndex
            renderManifest = $manifestIndex
            readyEnd = $readyEndIndex
            screenshot = $screenshotIndex
            visualState = $visualStateIndex
            playbackPolicy = $policyIndex
        }
        raw = [pscustomobject]@{
            playbackStart = if ($startIndex -ge 0) { $OrderedLogLines[$startIndex] } else { $null }
            renderRequest = if ($requestIndex -ge 0) { $OrderedLogLines[$requestIndex] } else { $null }
            readyBegin = if ($readyBeginIndex -ge 0) { $OrderedLogLines[$readyBeginIndex] } else { $null }
            presentContent = if ($presentIndex -ge 0) { $OrderedLogLines[$presentIndex] } else { $null }
            playbackFrame = if ($frameIndex -ge 0) { $OrderedLogLines[$frameIndex] } else { $null }
            renderManifest = if ($manifestIndex -ge 0) { $OrderedLogLines[$manifestIndex] } else { $null }
            readyEnd = if ($readyEndIndex -ge 0) { $OrderedLogLines[$readyEndIndex] } else { $null }
            screenshot = if ($screenshotIndex -ge 0) { $OrderedLogLines[$screenshotIndex] } else { $null }
        }
    }
}

function Get-GuiSmokeScreenshotProvenance {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$OrderedLogLines,
        [long]$RequestedStartFrame
    )

    if ($PSBoundParameters.ContainsKey('RequestedStartFrame')) {
        return Get-GuiSmokeScreenshotProvenanceV2 `
            -OrderedLogLines $OrderedLogLines `
            -RequestedStartFrame $RequestedStartFrame
    }
    return Get-GuiSmokeScreenshotProvenanceV1 -OrderedLogLines $OrderedLogLines
}

function ConvertTo-GuiSmokeFlatMap {
    param([object]$Value, [string]$Prefix = '')

    $result = [ordered]@{}
    if ($null -eq $Value) {
        $result[$Prefix] = $null
        return $result
    }
    $properties = @($Value.PSObject.Properties)
    if ($properties.Count -eq 0 -or $Value -is [string] -or $Value -is [ValueType]) {
        $result[$Prefix] = $Value
        return $result
    }
    foreach ($property in $properties) {
        $path = if ([string]::IsNullOrWhiteSpace($Prefix)) { $property.Name } else { "$Prefix.$($property.Name)" }
        $child = ConvertTo-GuiSmokeFlatMap -Value $property.Value -Prefix $path
        foreach ($key in $child.Keys) { $result[$key] = $child[$key] }
    }
    return $result
}

function Test-GuiSmokeScreenshotPair {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Left,
        [Parameter(Mandatory = $true)][object]$Right,
        [string[]]$AllowedEffectiveStateDifferences = @()
    )

    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($leg in @(@{ name = 'left'; value = $Left }, @{ name = 'right'; value = $Right })) {
        if ([string]$leg.value.schema -ne 'mlvapp-gui-smoke-screenshot-provenance.v2') {
            $failures.Add("$($leg.name)-provenance-not-v2")
        }
        if (-not $leg.value.validAssociation) { $failures.Add("$($leg.name)-association-invalid") }
        if (-not $leg.value.validFresh) { $failures.Add("$($leg.name)-fresh-provenance-invalid") }
    }

    foreach ($comparison in @(
        @{ property = 'requestedStartFrame'; failure = 'requested-start-frame-mismatch' },
        @{ property = 'effectiveStartFrame'; failure = 'effective-start-frame-mismatch' },
        @{ property = 'requestedFrame'; failure = 'requested-frame-mismatch' },
        @{ property = 'displayFrame'; failure = 'display-frame-mismatch' },
        @{ property = 'presentationIndex'; failure = 'presentation-index-mismatch' },
        @{ property = 'requestSerial'; failure = 'request-serial-mismatch' },
        @{ property = 'requestSerialOffset'; failure = 'request-serial-offset-mismatch' },
        @{ property = 'screenshotMethod'; failure = 'screenshot-method-mismatch' },
        @{ property = 'screenshotWidth'; failure = 'screenshot-width-mismatch' },
        @{ property = 'screenshotHeight'; failure = 'screenshot-height-mismatch' }
    )) {
        $leftValue = Get-GuiSmokeObjectPropertyValue $Left $comparison.property
        $rightValue = Get-GuiSmokeObjectPropertyValue $Right $comparison.property
        if ($null -eq $leftValue -or $null -eq $rightValue) {
            $failures.Add("missing-$($comparison.property)")
        }
        elseif ([string]$leftValue -cne [string]$rightValue) {
            $failures.Add($comparison.failure)
        }
    }

    $leftHistory = $Left.presentedHistory | ConvertTo-Json -Depth 8 -Compress
    $rightHistory = $Right.presentedHistory | ConvertTo-Json -Depth 8 -Compress
    if ($leftHistory -cne $rightHistory) { $failures.Add('presented-history-mismatch') }

    $leftState = ConvertTo-GuiSmokeFlatMap -Value $Left.effectiveState -Prefix 'effectiveState'
    $rightState = ConvertTo-GuiSmokeFlatMap -Value $Right.effectiveState -Prefix 'effectiveState'
    $stateKeys = @($leftState.Keys + $rightState.Keys | Sort-Object -Unique)
    foreach ($key in $stateKeys) {
        if ($AllowedEffectiveStateDifferences -contains $key) { continue }
        if (-not $leftState.Contains($key) -or -not $rightState.Contains($key) -or
            [string]$leftState[$key] -cne [string]$rightState[$key]) {
            $failures.Add("effective-state-mismatch:$key")
        }
    }

    [pscustomobject]@{
        schema = 'mlvapp-gui-smoke-screenshot-pair.v1'
        validComparable = ($failures.Count -eq 0)
        failures = @($failures)
        allowedEffectiveStateDifferences = @($AllowedEffectiveStateDifferences)
        left = $Left
        right = $Right
    }
}

function Assert-GuiSmokeScreenshotPair {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Left,
        [Parameter(Mandatory = $true)][object]$Right,
        [string[]]$AllowedEffectiveStateDifferences = @()
    )

    $result = Test-GuiSmokeScreenshotPair `
        -Left $Left -Right $Right `
        -AllowedEffectiveStateDifferences $AllowedEffectiveStateDifferences
    if (-not $result.validComparable) {
        throw "GUI smoke pair is not comparable: $($result.failures -join ', ')."
    }
    return $result
}
