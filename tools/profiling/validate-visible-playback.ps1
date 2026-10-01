# validate-visible-playback.ps1 -- LIVE filmstrip capturer for REAL MLVApp playback.
#
# The settled-grab gates (review-dualiso-fullres-recon.ps1, run-release-gui-smoke.ps1 single screenshot)
# are BLIND to the LIVE look: the cold (first-uncached) pass, the dark->bright Look Assist shift, temporal
# grain/chroma residual, and whether the displayed frame actually ADVANCES. This rebuilds the deleted
# capturer: it launches the GUI-smoke playback (which auto-plays ONCE, never loops: a clip under 20 s is refused), PrintWindow-captures the MLVApp
# window every ~1s into a cap-*.png filmstrip, then runs filmstrip-balance-trace.ps1 so the green/warm
# cast is quantified per frame. VALIDATE BY PIXELS -- then open the caps and LOOK (the artifact is the
# verdict; FPS/timer telemetry can read "smooth" over a frozen viewport).
#
# Window-based PrintWindow(hwnd, dc, 2 /*PW_RENDERFULLCONTENT*/) grabs MLVApp's own backing store, so it
# is z-order/overlap independent -- keep MLVApp WINDOWED (the gui-smoke does), never maximize. Do NOT
# capture DURING the fragile play-start (it can prevent playback establishing); SETTLE first, then capture.
#
# Repo path has a SPACE: this script launches the exe via ProcessStartInfo with an explicit ArgumentList
# + WorkingDirectory (no Start-Process -ArgumentList absolute-path splitting), and uses relative tool
# paths resolved against $PSScriptRoot.
param(
    [string]$ExePath = "platform\qt\build-release\release\MLVApp.exe",
    [Parameter(Mandatory = $true)][string]$ClipPath,
    [string]$OutDir = "",
    [int]$Captures = 26,                 # number of filmstrip frames
    [int]$IntervalMs = 1000,             # ~1s between captures (resume rule)
    [int]$SettleMs = 8000,               # wait AFTER launch before the first capture (let playback establish)
    [string]$ScaleFactor = "2",          # playback scale leg (2 = the gate's default leg)
    [int]$Seconds = 40,                  # gui-smoke play window; must exceed SettleMs + Captures*IntervalMs
    [switch]$NoLookAssist,               # default: Look Assist ON (we WANT to see its WB cast)
    [string]$QtBinPrepend = "C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin",
    [string]$Receipt = ""                # optional .marxml; when set, passes --receipt (locked-WB gate parity)
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$clip = (Resolve-Path -LiteralPath $ClipPath).Path
if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $base = [IO.Path]::GetFileNameWithoutExtension($clip)
    $OutDir = Join-Path $repoRoot ".claude-state\profiling\live-filmstrip-$stamp\$base"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$logRoot = Join-Path $OutDir "logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$captureScript = Join-Path $PSScriptRoot "capture-window-screenshot.ps1"
$balanceScript = Join-Path $PSScriptRoot "filmstrip-balance-trace.ps1"

# Required play window must cover settle + all captures, with margin.
$needSeconds = [int]([math]::Ceiling(($SettleMs + $Captures * $IntervalMs) / 1000.0)) + 4
if ($Seconds -lt $needSeconds) { $Seconds = $needSeconds }

# PLAYBACK-CLIP-LENGTH-ENFORCE-1 (owner rule 2026-09-30): this launches the app directly, so it runs
# the same clip-length gate run-release-gui-smoke.ps1 does. A clip under 20 s, or shorter than the
# play window, is refused (exit 41 CLIP_TOO_SHORT / 42 CLIP_LENGTH_UNKNOWN); --loop is never passed.
. (Join-Path $PSScriptRoot 'gui-smoke-length-gate.ps1')
# ENFORCE-2: this script computes its own window; it is never below the 20 s floor, and an inherited
# MLVAPP_AUTOPLAY_* variable (which plays with no tool gate) is refused.
if ($Seconds -lt 20) { $Seconds = 20 }
$parentEnvironmentGate = Test-GuiSmokeParentEnvironment
if ($parentEnvironmentGate.verdict -ne 'OK') {
    [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-2: $($parentEnvironmentGate.message)")
    exit 44
}
$clipLengthGate = Test-GuiSmokeClipLength -Path $clip -WindowSeconds $Seconds
if ($clipLengthGate.verdict -ne 'OK') {
    [Console]::Error.WriteLine("PLAYBACK-CLIP-LENGTH-ENFORCE-1: $($clipLengthGate.message)")
    exit (Get-GuiSmokeGateExitCode -Verdict $clipLengthGate.verdict)
}

$argList = @(
    "--gui-smoke-playback",
    "--input", $clip,
    "--seconds", [string]$Seconds,
    "--start-frame", "0",
    "--drop-frame-mode", "persisted",
    "--settle-ms", "2500",
    "--settle-cpu-percent", "10",
    "--settle-cpu-stable-ms", "1000",
    "--settle-cpu-max-ms", "45000"
)
if ($NoLookAssist) { $argList += "--no-look-assist" }
if (-not [string]::IsNullOrWhiteSpace($Receipt)) { $argList += @("--receipt", (Resolve-Path -LiteralPath $Receipt).Path) }

$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $exe
$psi.WorkingDirectory = $repoRoot
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
foreach ($a in $argList) { [void]$psi.ArgumentList.Add($a) }
$psi.EnvironmentVariables["PATH"] = $QtBinPrepend + ";" + $psi.EnvironmentVariables["PATH"]
$psi.EnvironmentVariables["MLVAPP_PLAYBACK_SCALE_FACTOR"] = $ScaleFactor
$psi.EnvironmentVariables["MLVAPP_PLAYBACK_QUALITY_MODE"] = "auto"
$psi.EnvironmentVariables["MLVAPP_CRASH_FORENSICS_LOG_DIR"] = $logRoot

Write-Host "[live-filmstrip] launching: $exe (scale=$ScaleFactor, seconds=$Seconds, lookAssist=$([bool](-not $NoLookAssist)))"
$proc = [System.Diagnostics.Process]::new()
$proc.StartInfo = $psi
# Drain redirected streams so the child never blocks on a full pipe.
$null = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {}
$null = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {}
[void]$proc.Start()
$proc.BeginOutputReadLine()
$proc.BeginErrorReadLine()

try {
    Write-Host "[live-filmstrip] settling ${SettleMs}ms before first capture (do NOT capture during play-start)..."
    Start-Sleep -Milliseconds $SettleMs
    $captured = 0
    for ($i = 0; $i -lt $Captures; $i++) {
        if ($proc.HasExited) { Write-Warning "[live-filmstrip] process exited early after $captured captures."; break }
        $cap = Join-Path $OutDir ("cap-{0:000}.png" -f $i)
        try {
            & $captureScript -Process $proc -OutputPath $cap -WindowWaitMs 4000 -CaptureTimeoutMs 2500 | Out-Null
            $captured++
        } catch {
            Write-Warning "[live-filmstrip] capture $i failed: $($_.Exception.Message)"
        }
        Start-Sleep -Milliseconds $IntervalMs
    }
    Write-Host "[live-filmstrip] captured $captured frames -> $OutDir"
}
finally {
    if (-not $proc.HasExited) {
        try { $proc.Kill($true) } catch { try { $proc.Kill() } catch {} }
    }
    Get-EventSubscriber | Where-Object { $_.SourceObject -eq $proc } | Unregister-Event -ErrorAction SilentlyContinue
}

# Quantify the cast per frame (R/G/B mean + warmCool + greenAxis).
Write-Host "[live-filmstrip] balance trace:"
& $balanceScript -Dirs $OutDir

[pscustomobject]@{
    outDir   = $OutDir
    captures = (Get-ChildItem $OutDir -Filter 'cap-*.png' | Measure-Object).Count
    exe      = $exe
    clip     = $clip
    scale    = $ScaleFactor
    lookAssist = [bool](-not $NoLookAssist)
} | Format-List
