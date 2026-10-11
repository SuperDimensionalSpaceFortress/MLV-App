# Prune-MlvGpuPreferences.ps1 -- OWNER-RUN. No agent or lane ever executes this script (never-authorized NA-8).
#
# WHY IT EXISTS. Until GPU-PREF-REGISTRY-STOP-AND-PRUNE-1 the venue job scripts wrote one per-app Windows
# GPU-preference value ("GpuPreference=2;") into
#   HKCU\Software\Microsoft\DirectX\UserGpuPreferences
# for every staged exe path, and every build staged to a new path, so the key grew by one value per run (about
# 710 by 2026-10-11) and nothing pruned it. The writers are gone; this script removes what they left behind.
#
# WHAT IT REMOVES. A value whose NAME is a path under an MLV root AND whose file no longer exists. The roots are
# the agentRoot / scratchRoot of every venue in tools\profiling\dual-venue\venues.json, plus C:\mlvtmp,
# C:\dell-bench and C:\temp (always, whatever venues.json says). Nothing else: not the values for apps you
# installed, not a value for any exe outside those roots, not a value whose path still exists (unless you pass
# -IncludeExisting, below), not a value under a root that is not mounted right now. See MlvGpuPreferencePrune.psm1
# for the exact rules (and the reason every kept value stays).
#
# -IncludeExisting. Without it, a value whose exe is still on disk is kept. With it, a value whose exe still exists is
# ALSO removed when that exe is a staged MLVApp-*.exe build under one of the roots above. It never reaches an exe
# outside the roots, and it needs no -Apply to be previewed: the dry run lists exactly what it would remove.
#
# HOW TO RUN (in YOUR OWN PowerShell, on the venue whose key you want to prune):
#   1. Dry run (the default; changes nothing, writes only a JSON receipt):
#        pwsh -NoProfile -File tools\profiling\venue\Prune-MlvGpuPreferences.ps1 [-IncludeExisting]
#   2. Read the listing and the receipt. When it is right:
#        pwsh -NoProfile -File tools\profiling\venue\Prune-MlvGpuPreferences.ps1 [-IncludeExisting] -Apply
#
# The receipt (JSON, default %LOCALAPPDATA%\MlvGpuPreferencePrune\) lists every value judged, the data of every value
# removed (so any one can be recreated), and the counts before and after.
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$IncludeExisting,
    [string]$VenuesJson = '',
    [string]$ReceiptPath = ''
)
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MlvGpuPreferencePrune.psm1') -Force

$subKeyPath = 'Software\Microsoft\DirectX\UserGpuPreferences'
if (-not $VenuesJson) { $VenuesJson = Join-Path $PSScriptRoot '..\dual-venue\venues.json' }
if (-not (Test-Path -LiteralPath $VenuesJson -PathType Leaf)) { throw "venues.json not found: $VenuesJson" }
if (-not $ReceiptPath) {
    $dir = Join-Path $env:LOCALAPPDATA 'MlvGpuPreferencePrune'
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $ReceiptPath = Join-Path $dir ("prune-{0}-{1}.json" -f $(if ($Apply) { 'apply' } else { 'dryrun' }), $stamp)
}

function Get-GpuPrefValueNames {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($subKeyPath, $false)
    if ($null -eq $key) { return @() }
    try { return @($key.GetValueNames() | Where-Object { $_ -ne '' }) } finally { $key.Close() }
}

$roots = @(Get-MlvGpuPreferenceRoots -VenuesJsonPath $VenuesJson)
$namesBefore = @(Get-GpuPrefValueNames)
$judged = @(Select-MlvStaleGpuPreferenceValue -Names $namesBefore -Roots $roots `
        -FileExists { param($p) Test-Path -LiteralPath $p } `
        -RootExists { param($r) Test-Path -LiteralPath $r -PathType Container } `
        -IncludeExisting:$IncludeExisting)
$candidates = @($judged | Where-Object { $_.Verdict -eq 'prune' })

Write-Host ("MLV GPU-preference prune -- mode: {0}" -f $(if ($Apply) { 'APPLY' } else { 'DRY RUN (nothing is changed)' }))
Write-Host ("key            : HKCU\{0}" -f $subKeyPath)
Write-Host ("include existing MLVApp-*.exe builds : {0}" -f $(if ($IncludeExisting) { 'yes' } else { 'no' }))
Write-Host ("roots          : {0}" -f ($roots -join '; '))
Write-Host ("values before  : {0}" -f $namesBefore.Count)
foreach ($group in @($judged | Group-Object Verdict | Sort-Object Name)) {
    Write-Host ("  {0,-24} {1}" -f $group.Name, $group.Count)
}
Write-Host ("to remove      : {0}" -f $candidates.Count)
foreach ($c in $candidates) { Write-Host ("  {0}{1}" -f $c.Name, $(if ($c.ExistingBuild) { '   [exe still exists: -IncludeExisting]' } else { '' })) }

$removed = New-Object System.Collections.Generic.List[object]
$failed = New-Object System.Collections.Generic.List[object]
if ($Apply -and $candidates.Count -gt 0) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($subKeyPath, $true)
    if ($null -eq $key) { throw "cannot open HKCU\$subKeyPath for writing" }
    try {
        foreach ($c in $candidates) {
            $data = $key.GetValue($c.Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            try {
                $key.DeleteValue($c.Name, $true)
                $removed.Add([pscustomobject]@{ name = $c.Name; data = $data })
            }
            catch {
                $failed.Add([pscustomobject]@{ name = $c.Name; error = $_.Exception.Message })
            }
        }
    }
    finally { $key.Close() }
}

$namesAfter = @(Get-GpuPrefValueNames)
Write-Host ("removed        : {0}" -f $removed.Count)
if ($failed.Count -gt 0) { Write-Host ("FAILED         : {0}" -f $failed.Count) }
Write-Host ("values after   : {0}" -f $namesAfter.Count)

$receipt = [ordered]@{
    schema = 'mlv-app/gpu-preference-prune-receipt/v1'
    capturedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    mode = $(if ($Apply) { 'apply' } else { 'dry-run' })
    includeExisting = [bool]$IncludeExisting
    host = $env:COMPUTERNAME
    user = $env:USERNAME
    key = ('HKCU\' + $subKeyPath)
    venuesJson = (Resolve-Path -LiteralPath $VenuesJson).Path
    roots = @($roots)
    countBefore = $namesBefore.Count
    countAfter = $namesAfter.Count
    verdictCounts = [ordered]@{}
    toRemove = @($candidates | ForEach-Object { $_.Name })
    removed = @($removed)
    failed = @($failed)
}
foreach ($group in @($judged | Group-Object Verdict | Sort-Object Name)) { $receipt.verdictCounts[$group.Name] = $group.Count }
$receiptDir = Split-Path -Parent $ReceiptPath
if ($receiptDir -and -not (Test-Path -LiteralPath $receiptDir)) { [void][System.IO.Directory]::CreateDirectory($receiptDir) }
[System.IO.File]::WriteAllText($ReceiptPath, ($receipt | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("receipt        : {0}" -f $ReceiptPath)
if (-not $Apply -and $candidates.Count -gt 0) {
    Write-Host 'Dry run only. To remove exactly the values listed above, re-run with -Apply.'
}
if ($failed.Count -gt 0) { exit 1 }
