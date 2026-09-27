# run-local-gpu-capability.ps1
#
# Local laptop/desktop GPU capability profiler for MLV-App.
#
# Run this on the physical machine you want to measure, not inside a VM and not
# from an RDP session. It inventories visible display adapters, records common
# GPU tool availability, optionally compares Windows' current GPU choice against
# the "High performance" per-app GPU preference, then runs the same playback
# profiling matrix used for the Ultra-Magnus smoke:
#
#   cpu_baseline, gpu_preview_processing, gpu_bilinear_debayer, gpu_both
#
# The point is not just "is there a GPU"; it is "which adapter did MLV-App's GL
# path actually use, and did the app activate the GPU preview seams?"

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$Exe = "",
    [string]$Clip = "",
    [string]$Receipt = "",
    [int]$Frames = 16,
    [int]$ScaleFactor = 1,
    [ValidateSet("Current", "HighPerformance", "Compare")]
    [string]$GpuPreferenceMode = "Compare",
    [switch]$KeepGpuPreference,
    [switch]$InventoryOnly,
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"

function Resolve-ScriptRoot {
    if ($PSScriptRoot) { return $PSScriptRoot }
    if ($PSCommandPath) { return (Split-Path -Parent $PSCommandPath) }
    if ($MyInvocation.MyCommand.Path) { return (Split-Path -Parent $MyInvocation.MyCommand.Path) }
    return (Get-Location).Path
}

function Resolve-FirstExistingPath {
    param([string[]]$Candidates, [string]$Description)
    foreach ($candidate in $Candidates) {
        if (-not $candidate) { continue }
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "Could not find $Description. Tried: $($Candidates -join '; ')"
}

function Resolve-OptionalExistingPath {
    param([string]$Path)
    if ($Path -and (Test-Path -LiteralPath $Path)) {
        return (Resolve-Path -LiteralPath $Path).Path
    }
    return $Path
}

function Test-ExistingFile {
    param([string]$Path)
    return [bool]($Path -and (Test-Path -LiteralPath $Path -PathType Leaf))
}

function Get-ToolInfo {
    param([string]$Name, [string[]]$Args = @())
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        return [pscustomobject]@{ name = $Name; found = $false; path = $null; output = $null }
    }
    $output = $null
    if ($Args.Count -gt 0) {
        try {
            $output = (& $cmd.Source @Args 2>&1 | Select-Object -First 8) -join "`n"
        } catch {
            $output = "ERROR: $($_.Exception.Message)"
        }
    }
    return [pscustomobject]@{ name = $Name; found = $true; path = $cmd.Source; output = $output }
}

function Get-AdapterInventory {
    @(Get-CimInstance Win32_VideoController | ForEach-Object {
        [pscustomobject]@{
            name = $_.Name
            adapter_compatibility = $_.AdapterCompatibility
            video_processor = $_.VideoProcessor
            driver_version = $_.DriverVersion
            adapter_ram_mb = if ($_.AdapterRAM) { [math]::Round($_.AdapterRAM / 1MB, 1) } else { $null }
            pnp_device_id = $_.PNPDeviceID
        }
    })
}

function Get-GpuPreferenceValue {
    param([string]$Executable)
    $prefKey = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
    if (-not (Test-Path -LiteralPath $prefKey)) { return $null }
    $props = Get-ItemProperty -LiteralPath $prefKey -ErrorAction SilentlyContinue
    if (-not $props) { return $null }
    $prop = $props.PSObject.Properties | Where-Object { $_.Name -eq $Executable } | Select-Object -First 1
    if ($prop) { return [string]$prop.Value }
    return $null
}

function Set-GpuPreferenceValue {
    param([string]$Executable, [string]$Value)
    $prefKey = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
    New-Item -Path $prefKey -Force | Out-Null
    New-ItemProperty -Path $prefKey -Name $Executable -Value $Value -PropertyType String -Force | Out-Null
}

function Restore-GpuPreferenceValue {
    param([string]$Executable, [AllowNull()][string]$OriginalValue)
    $prefKey = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
    if ($null -eq $OriginalValue) {
        if (Test-Path -LiteralPath $prefKey) {
            Remove-ItemProperty -Path $prefKey -Name $Executable -ErrorAction SilentlyContinue
        }
    } else {
        Set-GpuPreferenceValue -Executable $Executable -Value $OriginalValue
    }
}

function Get-RendererVerdict {
    param([AllowNull()][string]$Renderer)
    if (-not $Renderer) { return "unknown-renderer" }
    if ($Renderer -match "llvmpipe|software|GDI|Microsoft Basic Render") { return "software-gl" }
    if ($Renderer -match "NVIDIA|GeForce|RTX|GTX|Quadro") { return "nvidia-hardware-gl" }
    if ($Renderer -match "Intel|UHD|Iris|Arc") { return "intel-hardware-gl" }
    if ($Renderer -match "AMD|Radeon") { return "amd-hardware-gl" }
    return "hardware-gl"
}

function Get-AverageProperty {
    param([object[]]$Rows, [string]$Property)
    $values = @(
        foreach ($row in $Rows) {
            $prop = $row.PSObject.Properties | Where-Object { $_.Name -eq $Property } | Select-Object -First 1
            if ($prop -and $null -ne $prop.Value) { [double]$prop.Value }
        }
    )
    if ($values.Count -eq 0) { return $null }
    return [math]::Round((($values | Measure-Object -Average).Average), 2)
}

function Wait-ForFile {
    param([string]$Path, [int]$TimeoutSeconds = 10)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $Path) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return (Test-Path -LiteralPath $Path)
}

function Run-ProfileMatrix {
    param(
        [string]$Label,
        [string]$Executable,
        [string]$ClipPath,
        [string]$ReceiptPath,
        [string]$ResultDir,
        [int]$FrameCount,
        [int]$Scale
    )

    New-Item -ItemType Directory -Force -Path $ResultDir | Out-Null
    $oldQtOpenGl = $env:QT_OPENGL
    $oldScale = $env:MLVAPP_PLAYBACK_SCALE_FACTOR
    $env:QT_OPENGL = "desktop"
    $env:MLVAPP_PLAYBACK_SCALE_FACTOR = "$Scale"

    $runs = @(
        @{ tag = "cpu_baseline"; args = @() },
        @{ tag = "gpu_processing"; args = @("--gpu-preview-processing", "gpu") },
        @{ tag = "gpu_debayer"; args = @("--gpu-bilinear-debayer", "gpu") },
        @{ tag = "gpu_both"; args = @("--gpu-preview-processing", "gpu", "--gpu-bilinear-debayer", "gpu") }
    )

    $rows = @()
    try {
        foreach ($run in $runs) {
            $json = Join-Path $ResultDir ("local_{0}_{1}_x{2}.json" -f $Label, $run.tag, $Scale)
            $stageLog = Join-Path $ResultDir ("local_{0}_{1}_x{2}.stagelog" -f $Label, $run.tag, $Scale)
            $stdoutLog = Join-Path $ResultDir ("local_{0}_{1}_x{2}.stdout.txt" -f $Label, $run.tag, $Scale)
            $args = @(
                "--profile-playback",
                "--input", $ClipPath,
                "--receipt", $ReceiptPath,
                "--frames", "$FrameCount",
                "--threads", "auto",
                "--raw-cache-mb", "0",
                "--output", $json,
                "--stage-log", $stageLog
            ) + $run.args

            Write-Host ("[RUN] {0}/{1}: MLVApp {2}" -f $Label, $run.tag, ($run.args -join " "))
            $stdout = & $Executable @args 2>&1
            if ($null -eq $stdout -or @($stdout).Count -eq 0) {
                "" | Set-Content -Encoding ASCII $stdoutLog
            } else {
                $stdout | Set-Content -Encoding ASCII $stdoutLog
            }
            $done = $stdout | Select-String "PROFILE\] DONE" | Select-Object -Last 1
            if ($done) { Write-Host "      $($done.Line)" }

            if (-not (Wait-ForFile -Path $json -TimeoutSeconds 10)) {
                $rows += [pscustomobject]@{
                    label = $Label
                    run = $run.tag
                    status = "missing-json"
                    stdout = $stdoutLog
                    # round 6 (astra MAJOR, consumer sweep): no fps signal here at all (the run
                    # never produced a JSON to read one from) -- mirrors New-RemoteCdngSummaryRow's
                    # "nothing to report" stance rather than New-ProfileRow's "unrecorded" one.
                    host_load_provisional = $null
                }
                continue
            }

            $j = Get-Content -LiteralPath $json -Raw | ConvertFrom-Json
            $warm = @($j.frames | Where-Object { $_.requested_frame -ge 2 })
            $sample = $warm | Select-Object -First 1
            $renderer = [string]$j.metadata.gpu_bilinear_debayer_probe_renderer
            $cadenceMs = if ($j.metadata.average_cadence_ms) { [double]$j.metadata.average_cadence_ms } else { $null }

            $rows += [pscustomobject]@{
                label = $Label
                run = $run.tag
                status = "ok"
                renderer = $renderer
                renderer_verdict = Get-RendererVerdict -Renderer $renderer
                gpu_proc_active = if ($sample) { $sample.gpu_preview_processing_active } else { $null }
                gpu_deb_active = if ($sample) { $sample.gpu_bilinear_debayer_active } else { $null }
                llrawproc_ms = Get-AverageProperty -Rows $warm -Property "llrawproc_ms"
                processing_ms = Get-AverageProperty -Rows $warm -Property "processing_ms"
                debayer_ms = Get-AverageProperty -Rows $warm -Property "debayered_frame_ms"
                render_ms = Get-AverageProperty -Rows $warm -Property "render_thread_total_ms"
                latency_ms = if ($j.metadata.average_latency_ms) { [math]::Round([double]$j.metadata.average_latency_ms, 2) } else { $null }
                cadence_ms = if ($cadenceMs) { [math]::Round($cadenceMs, 2) } else { $null }
                fps = if ($cadenceMs -and $cadenceMs -gt 0) { [math]::Round(1000.0 / $cadenceMs, 2) } else { $null }
                json = $json
                stdout = $stdoutLog
                # round 6 (astra MAJOR, consumer sweep): this script has no PLAYBACK-MEASURE-HOST-
                # LOAD-GATE-1 telemetry at all -- fps above was never checked against host load.
                # Mark unrecorded/provisional, same stance as compare-machine-perf.ps1's
                # New-ProfileRow/New-FieldLogRow. See PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 6
                # summary for why this is marked rather than brought under real host-load sampling
                # this round.
                host_load_provisional = $true
            }
        }
    } finally {
        $env:QT_OPENGL = $oldQtOpenGl
        $env:MLVAPP_PLAYBACK_SCALE_FACTOR = $oldScale
    }
    return $rows
}

$scriptRoot = Resolve-ScriptRoot
if (-not $RepoRoot) {
    $candidateRepoRoot = (Resolve-Path -LiteralPath (Join-Path $scriptRoot "..\..")).Path
    if ((Test-Path -LiteralPath (Join-Path $candidateRepoRoot ".git")) -or
        (Test-Path -LiteralPath (Join-Path $candidateRepoRoot "platform\qt"))) {
        $RepoRoot = $candidateRepoRoot
    } else {
        # Standalone inventory mode: if someone copied only this script to a
        # laptop, write outputs beside the script rather than under C:\.
        $RepoRoot = $scriptRoot
    }
} else {
    $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
}

if (-not $OutputRoot) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    if (Test-Path -LiteralPath (Join-Path $RepoRoot ".git")) {
        $OutputRoot = Join-Path $RepoRoot ".claude-state\profiling\local-gpu-capability-$stamp"
    } else {
        $OutputRoot = Join-Path $scriptRoot "local-gpu-capability-$stamp"
    }
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$OutputRoot = (Resolve-Path -LiteralPath $OutputRoot).Path

$exeCandidate = if ($Exe) { $Exe } else { Join-Path $RepoRoot "platform\qt\build-release\release\MLVApp.exe" }
$clipCandidate = if ($Clip) { $Clip } else {
    $candidateClip = Resolve-OptionalExistingPath -Path "C:\temp\MLV\M16-1327.MLV"
    if (-not (Test-ExistingFile -Path $candidateClip)) {
        $candidateClip = Resolve-OptionalExistingPath -Path (Join-Path $RepoRoot "tests\fixtures\clips\large_dual_iso.mlv")
    }
    $candidateClip
}
$receiptCandidate = if ($Receipt) { $Receipt } else {
    Resolve-OptionalExistingPath -Path (Join-Path $RepoRoot "tests\fixtures\receipts\large_dual_iso_hq.marxml")
}

$Exe = Resolve-OptionalExistingPath -Path $exeCandidate
$Clip = Resolve-OptionalExistingPath -Path $clipCandidate
$Receipt = Resolve-OptionalExistingPath -Path $receiptCandidate
$exeExists = Test-ExistingFile -Path $Exe
$clipExists = Test-ExistingFile -Path $Clip
$receiptExists = Test-ExistingFile -Path $Receipt

$adapters = Get-AdapterInventory
$tools = @(
    (Get-ToolInfo -Name "nvidia-smi" -Args @("--query-gpu=name,compute_cap,memory.total,driver_version", "--format=csv,noheader")),
    (Get-ToolInfo -Name "nvcc" -Args @("--version")),
    (Get-ToolInfo -Name "vulkaninfo" -Args @("--summary"))
)

$originalPref = if ($exeExists) { Get-GpuPreferenceValue -Executable $Exe } else { $null }
$inventory = [pscustomobject]@{
    host = $env:COMPUTERNAME
    captured = (Get-Date).ToString("o")
    repo_root = $RepoRoot
    exe = $Exe
    exe_exists = $exeExists
    clip = $Clip
    clip_exists = $clipExists
    receipt = $Receipt
    receipt_exists = $receiptExists
    frames = $Frames
    scale_factor = $ScaleFactor
    session_name = $env:SESSIONNAME
    likely_rdp = if ($env:SESSIONNAME) { [bool]($env:SESSIONNAME -match "^RDP") } else { $false }
    gpu_preference_mode = $GpuPreferenceMode
    original_gpu_preference = $originalPref
    adapters = $adapters
    tools = $tools
}

$inventoryPath = Join-Path $OutputRoot "inventory.json"
$inventory | ConvertTo-Json -Depth 8 | Set-Content -Encoding ASCII $inventoryPath

Write-Host ""
Write-Host "=================== GPU INVENTORY ==================="
$adapters | Format-Table name, adapter_compatibility, driver_version, adapter_ram_mb -AutoSize
Write-Host "session        : $($inventory.session_name)"
Write-Host "likely RDP     : $($inventory.likely_rdp)"
Write-Host "exe            : $Exe (exists=$exeExists)"
Write-Host "clip           : $Clip (exists=$clipExists)"
Write-Host "receipt        : $Receipt (exists=$receiptExists)"
Write-Host "output         : $OutputRoot"
Write-Host "inventory JSON : $inventoryPath"
Write-Host "====================================================="

if ($InventoryOnly) {
    Write-Host "InventoryOnly set; skipping profile runs."
    exit 0
}

if (-not $exeExists -or -not $clipExists -or -not $receiptExists) {
    throw "Profile runs require existing -Exe, -Clip, and -Receipt paths. Use -InventoryOnly for zero-install hardware triage."
}

$allRows = @()
$restoreNeeded = $false
try {
    if ($GpuPreferenceMode -eq "Current" -or $GpuPreferenceMode -eq "Compare") {
        $allRows += Run-ProfileMatrix -Label "current" -Executable $Exe -ClipPath $Clip -ReceiptPath $Receipt `
            -ResultDir (Join-Path $OutputRoot "current") -FrameCount $Frames -Scale $ScaleFactor
    }

    if ($GpuPreferenceMode -eq "HighPerformance" -or $GpuPreferenceMode -eq "Compare") {
        Write-Host ""
        Write-Host "Setting per-app Windows GPU preference to High performance for this exe."
        Set-GpuPreferenceValue -Executable $Exe -Value "GpuPreference=2;"
        $restoreNeeded = -not $KeepGpuPreference
        $allRows += Run-ProfileMatrix -Label "highperf" -Executable $Exe -ClipPath $Clip -ReceiptPath $Receipt `
            -ResultDir (Join-Path $OutputRoot "highperf") -FrameCount $Frames -Scale $ScaleFactor
    }
} finally {
    if ($restoreNeeded) {
        Restore-GpuPreferenceValue -Executable $Exe -OriginalValue $originalPref
        Write-Host "Restored original per-app GPU preference."
    }
}

$summary = [pscustomobject]@{
    inventory = $inventory
    final_gpu_preference = Get-GpuPreferenceValue -Executable $Exe
    runs = $allRows
}
$summaryPath = Join-Path $OutputRoot "summary.json"
$summary | ConvertTo-Json -Depth 10 | Set-Content -Encoding ASCII $summaryPath

Write-Host ""
Write-Host "=================== PROFILE SUMMARY ==================="
# round 7 (both keys MAJOR -- "console projection drops host_load_provisional"): the JSON row
# already carries host_load_provisional (round 6), but this Select-Object -- the ONLY place a
# human actually reads these fps numbers at the console -- dropped it entirely, so a reader
# watching the terminal never saw the flag sitting right next to fps in the JSON. Simply
# appending it at the end (where fps already was) is not enough: Format-Table -AutoSize computes
# column widths against the console's reported buffer width (120 here) and SILENTLY drops
# trailing columns that do not fit -- verified live that a 12-column table with fps and
# host_load_provisional both left at the end drops BOTH of them with no warning at all, on a
# perfectly ordinary 120-column console. fps and host_load_provisional are moved right after
# run/renderer_verdict -- the two columns this round's fix exists to protect -- so they are never
# among the trailing columns AutoSize discards; -Wrap does not change this behaviour (verified),
# only column ORDER does.
$allRows |
    Select-Object label, run, fps, host_load_provisional, renderer_verdict, gpu_proc_active, gpu_deb_active, llrawproc_ms, processing_ms, debayer_ms, render_ms, cadence_ms |
    Format-Table -AutoSize
Write-Host "======================================================="
Write-Host "summary JSON: $summaryPath"

$renderers = @($allRows | Where-Object { $_.renderer } | Select-Object -ExpandProperty renderer -Unique)
if ($renderers.Count -gt 0) {
    Write-Host ""
    Write-Host "Renderers observed:"
    foreach ($renderer in $renderers) {
        Write-Host ("  {0} [{1}]" -f $renderer, (Get-RendererVerdict -Renderer $renderer))
    }
}

if ($allRows.renderer_verdict -contains "software-gl") {
    Write-Host ""
    Write-Warning "Software GL was observed. That is not a valid hardware-GPU benchmark."
}
if (($allRows.renderer_verdict -contains "intel-hardware-gl") -and ($adapters.name -match "NVIDIA|GeForce|RTX|GTX|AMD|Radeon")) {
    Write-Host ""
    Write-Warning "A discrete GPU appears to exist, but at least one run used Intel GL. Use the highperf rows as the dGPU-selection check."
}

exit 0
