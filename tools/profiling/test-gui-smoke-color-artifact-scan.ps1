$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1')

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

Add-Type -AssemblyName System.Drawing

function New-SolidPng {
    param(
        [string]$Path,
        [int]$Width,
        [int]$Height,
        [int]$R,
        [int]$G,
        [int]$B
    )
    $bitmap = [System.Drawing.Bitmap]::new($Width, $Height)
    try {
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
            $brush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb($R, $G, $B))
            try {
                $graphics.FillRectangle($brush, 0, 0, $Width, $Height)
            }
            finally { $brush.Dispose() }
        }
        finally { $graphics.Dispose() }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally { $bitmap.Dispose() }
}

function New-CheckerPng {
    # A high-variance, color-neutral (gray/white) checkerboard: enough sampled-pixel
    # spread to clear the uniform-image guard, with no magenta/green axis content so
    # it lands on "clear-heuristic" rather than an artifact verdict.
    param([string]$Path, [int]$Width, [int]$Height, [int]$TileSize = 24)
    $bitmap = [System.Drawing.Bitmap]::new($Width, $Height)
    try {
        for ($y = 0; $y -lt $Height; ++$y) {
            for ($x = 0; $x -lt $Width; ++$x) {
                $tileX = [int]($x / $TileSize)
                $tileY = [int]($y / $TileSize)
                $light = (($tileX + $tileY) % 2) -eq 0
                $shade = if ($light) { 235 } else { 30 }
                $bitmap.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($shade, $shade, $shade))
            }
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally { $bitmap.Dispose() }
}

function New-DarkPedestalPng {
    # Reproduces the ATTR3-VISUAL-QUALITY-EVIDENCE-1 bad GPU scale-1 capture's statistic
    # in miniature: textured (per-channel range > 2, so the uniform-image guard does not
    # catch it), but every sampled pixel stays under a low peak value in every channel, and
    # the channels are clipped unevenly relative to each other -- peak(R,G,B) < 96 and
    # peak/trough > 1.3, the "black-pedestal" signature this scanner fails closed on.
    param([string]$Path, [int]$Width = 640, [int]$Height = 360)
    $bitmap = [System.Drawing.Bitmap]::new($Width, $Height)
    try {
        for ($y = 0; $y -lt $Height; ++$y) {
            for ($x = 0; $x -lt $Width; ++$x) {
                $r = 4 + (($x + $y) % 36)
                $g = 4 + ((($x * 2) + $y) % 56)
                $b = 4 + (($x + ($y * 2)) % 51)
                $bitmap.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($r, $g, $b))
            }
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally { $bitmap.Dispose() }
}

function New-DarkBalancedWideRangePng {
    # A genuinely dark-but-correct analog to the receipt-B evidence frame (a real dark
    # scene, not a corrupted readback): textured, with a per-channel peak well above the
    # too-dark threshold and the channels moving together (no skew -- R, G, and B are the
    # same value at every pixel), so it must NOT be flagged even though most of the frame
    # is near-black. Proves the guard is not a blanket darkness check.
    param([string]$Path, [int]$Width = 640, [int]$Height = 360)
    $bitmap = [System.Drawing.Bitmap]::new($Width, $Height)
    try {
        for ($y = 0; $y -lt $Height; ++$y) {
            for ($x = 0; $x -lt $Width; ++$x) {
                $shade = 4 + (($x -bxor $y) % 122)
                $bitmap.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($shade, $shade, $shade))
            }
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally { $bitmap.Dispose() }
}

function New-DarkBalancedNarrowRangePng {
    # KNOWN LIMITATION, exercised deliberately: a legitimately dark, low-peak scene with
    # NO channel skew (R = G = B throughout, peak well under 96). The peak+skew rule is
    # intentionally conservative (AND, not OR) so it never flags a balanced dark image --
    # this synthetic capture is exactly the shape the guard does not catch, and is asserted
    # to still pass so a future change to the rule cannot silently loosen it further.
    param([string]$Path, [int]$Width = 640, [int]$Height = 360)
    $bitmap = [System.Drawing.Bitmap]::new($Width, $Height)
    try {
        for ($y = 0; $y -lt $Height; ++$y) {
            for ($x = 0; $x -lt $Width; ++$x) {
                $shade = 4 + (($x + $y) % 56)
                $bitmap.SetPixel($x, $y, [System.Drawing.Color]::FromArgb($shade, $shade, $shade))
            }
        }
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally { $bitmap.Dispose() }
}

$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mlvapp-color-artifact-scan-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

try {
    # Tiny capture (the observed 150x45 GL-window placeholder shape) must never
    # read as clear -- it must fail closed as capture-invalid, below the sane
    # minimum size, regardless of its (uniform, blank) pixel content.
    $tinyPath = Join-Path $tempDir "tiny.png"
    New-SolidPng -Path $tinyPath -Width 150 -Height 45 -R 200 -G 200 -B 200
    $tinyScan = Get-ScreenshotColorArtifactScan -Path $tinyPath
    Assert-True ($tinyScan.verdict -eq "capture-invalid") `
        "Tiny capture should scan as capture-invalid; observed $($tinyScan.verdict)."
    Assert-True (-not [string]::IsNullOrWhiteSpace($tinyScan.captureInvalidReason)) `
        "Tiny capture must record a capture-invalid reason."
    Assert-True ($tinyScan.thresholds.verdictsThatFailWhenRequested -contains "capture-invalid") `
        "capture-invalid must be listed as a failing verdict when requested."

    # A sanely-sized but perfectly flat capture (a cleared-but-never-painted
    # framebuffer readback) must also fail closed rather than pass as clear.
    $uniformPath = Join-Path $tempDir "uniform.png"
    New-SolidPng -Path $uniformPath -Width 640 -Height 360 -R 32 -G 32 -B 32
    $uniformScan = Get-ScreenshotColorArtifactScan -Path $uniformPath
    Assert-True ($uniformScan.verdict -eq "capture-invalid") `
        "Uniform capture should scan as capture-invalid; observed $($uniformScan.verdict)."
    Assert-True (-not [string]::IsNullOrWhiteSpace($uniformScan.captureInvalidReason)) `
        "Uniform capture must record a capture-invalid reason."

    # A sanely-sized, high-variance, color-neutral capture must still pass as
    # clear -- the new guard must not turn into a blanket false-positive.
    $checkerPath = Join-Path $tempDir "checker.png"
    New-CheckerPng -Path $checkerPath -Width 640 -Height 360
    $checkerScan = Get-ScreenshotColorArtifactScan -Path $checkerPath
    Assert-True ($checkerScan.verdict -eq "clear-heuristic") `
        "Varied color-neutral capture should scan as clear-heuristic; observed $($checkerScan.verdict)."
    Assert-True ([string]::IsNullOrEmpty($checkerScan.captureInvalidReason)) `
        "A clear capture must not carry a capture-invalid reason."

    # A dark-but-textured capture whose every sampled pixel stays under a low peak in every
    # channel, with the channels clipped unevenly relative to each other, must fail closed
    # as capture-too-dark rather than compute artifact ratios over an unusable frame and
    # pass as clear-heuristic (the S1-frame.png shape from evidence/vq1-check-63b346d2).
    $darkPedestalPath = Join-Path $tempDir "dark-pedestal.png"
    New-DarkPedestalPng -Path $darkPedestalPath
    $darkPedestalScan = Get-ScreenshotColorArtifactScan -Path $darkPedestalPath
    Assert-True ($darkPedestalScan.verdict -eq "capture-too-dark") `
        "Dark, channel-skewed, low-peak capture should scan as capture-too-dark; observed $($darkPedestalScan.verdict)."
    Assert-True (-not [string]::IsNullOrWhiteSpace($darkPedestalScan.captureInvalidReason)) `
        "capture-too-dark must record the measured statistics as its reason."
    Assert-True ($darkPedestalScan.thresholds.verdictsThatFailWhenRequested -contains "capture-too-dark") `
        "capture-too-dark must be listed as a failing verdict when requested."

    # A genuinely dark scene (most sampled pixels near-black) whose channels move together
    # and whose peak clears the too-dark threshold must NOT be flagged -- this guard is not
    # a blanket darkness check (the receipt-B evidence frame's shape: real, dark, correct).
    $darkWidePath = Join-Path $tempDir "dark-wide-range.png"
    New-DarkBalancedWideRangePng -Path $darkWidePath
    $darkWideScan = Get-ScreenshotColorArtifactScan -Path $darkWidePath
    Assert-True ($darkWideScan.verdict -ne "capture-too-dark") `
        "A dark but wide-range, channel-balanced capture must not be flagged as capture-too-dark; observed $($darkWideScan.verdict)."

    # KNOWN LIMITATION exercised deliberately: a legitimately dark, low-peak, channel-
    # balanced capture is not caught by this (intentionally conservative, AND-combined)
    # rule. Pinned here so a future tightening of the rule is a deliberate decision, not
    # a silent behavior change.
    $darkNarrowPath = Join-Path $tempDir "dark-balanced-narrow-range.png"
    New-DarkBalancedNarrowRangePng -Path $darkNarrowPath
    $darkNarrowScan = Get-ScreenshotColorArtifactScan -Path $darkNarrowPath
    Assert-True ($darkNarrowScan.verdict -ne "capture-too-dark") `
        "A low-peak but channel-balanced capture is a documented gap and must not be flagged as capture-too-dark; observed $($darkNarrowScan.verdict)."

    # A missing/unreadable path must still report the pre-existing not-captured
    # verdict -- the new guard only applies once an image is actually opened.
    $missingScan = Get-ScreenshotColorArtifactScan -Path (Join-Path $tempDir "does-not-exist.png")
    Assert-True ($missingScan.verdict -eq "not-captured") `
        "A missing screenshot path should scan as not-captured; observed $($missingScan.verdict)."
}
finally {
    Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

# The runner must dot-source the extracted scanner rather than redefining it
# inline, and must accept the GL-window readback method wherever it previously
# hard-required the GL-viewport-widget method.
$wrapperPath = Join-Path $PSScriptRoot 'run-release-gui-smoke.ps1'
$wrapperText = Get-Content -LiteralPath $wrapperPath -Raw
foreach ($requiredSymbol in @(
    "'gui-smoke-color-artifact-scan.ps1'",
    'Get-ColorArtifactFailingVerdicts',
    '"app_internal_gl_viewport_grab", "gl_window_framebuffer_readback"'
)) {
    Assert-True $wrapperText.Contains($requiredSymbol) `
        "run-release-gui-smoke.ps1 is missing integration symbol: $requiredSymbol"
}
Assert-True (-not $wrapperText.Contains('function Add-ColorArtifactScannerType')) `
    'The color-artifact scanner type must be defined only in the extracted, dot-sourced file.'
Assert-True (-not $wrapperText.Contains(
    '@("suspect-block-or-bar", "scan-error", "capture-invalid", "capture-too-dark")')) `
    'run-release-gui-smoke.ps1 must call the shared Get-ColorArtifactFailingVerdicts, not hand-keep its own copy of the failing-verdict list.'

# PR147 round 3 shipped a SECOND, independently hand-kept color-artifact-failure list in
# the A/B comparator that omitted capture-invalid and capture-too-dark, so an A/B leg with
# an unusable or artifacted capture could still report PASS. Round 4 makes
# Get-ColorArtifactFailingVerdicts the single source of truth for every consumer; this
# guards against that comparator quietly regressing back to its own copy.
$comparatorPath = Join-Path $PSScriptRoot 'compare-release-gui-smoke-ab.ps1'
$comparatorText = Get-Content -LiteralPath $comparatorPath -Raw
foreach ($requiredSymbol in @(
    "'gui-smoke-color-artifact-scan.ps1'",
    'Get-ColorArtifactFailingVerdicts'
)) {
    Assert-True $comparatorText.Contains($requiredSymbol) `
        "compare-release-gui-smoke-ab.ps1 is missing integration symbol: $requiredSymbol"
}
Assert-True (-not $comparatorText.Contains('@("suspect-block-or-bar", "scan-error")')) `
    'compare-release-gui-smoke-ab.ps1 must not hand-keep its own (stale, capture-invalid/capture-too-dark-blind) color-artifact-failure list.'

$expectedFailingVerdicts = @("suspect-block-or-bar", "scan-error", "capture-invalid", "capture-too-dark")
$actualFailingVerdicts = @(Get-ColorArtifactFailingVerdicts)
Assert-True ($actualFailingVerdicts.Count -eq $expectedFailingVerdicts.Count) `
    "Get-ColorArtifactFailingVerdicts returned $($actualFailingVerdicts.Count) verdicts, expected $($expectedFailingVerdicts.Count)."
foreach ($verdict in $expectedFailingVerdicts) {
    Assert-True ($actualFailingVerdicts -contains $verdict) `
        "Get-ColorArtifactFailingVerdicts is missing required failing verdict: $verdict"
}

$scannerPath = Join-Path $PSScriptRoot 'gui-smoke-color-artifact-scan.ps1'
$parseErrors = $null
$parseTokens = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    $scannerPath,
    [ref]$parseTokens,
    [ref]$parseErrors) | Out-Null
Assert-True ($parseErrors.Count -eq 0) `
    "gui-smoke-color-artifact-scan.ps1 has parser errors: $($parseErrors -join '; ')"

Write-Host 'PASS: GUI smoke color-artifact capture-invalid scan tests'
