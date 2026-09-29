# playback-hfr-settings-job.ps1 -- GENERATOR (runs locally / in a lane; nothing here runs on
# Bachelor). Emits a self-contained <jobId>.job.ps1 for the Bachelor agent inbox that records,
# sets or restores the saved QSettings values the PLAYBACK-HFR-CONFORM-DEFAULT-1 live legs
# depend on, and records per-core CPU load beside them (the hub's quiet-window ruling wants the
# per-core load on record). Registry only: it never opens, names or resolves a media file.
#
#   -Mode record          snapshot fpsOverride, frameRate, audioOutput, dragFrameMode and the
#                         Playback\Conform* / AutoTargetFps values (present/absent, kind, value)
#                         to the agent root on FIRST run only, print them, and print per-core load.
#   -Mode setFpsOverride  save fpsOverride=true, frameRate=25 (a saved export override, as the
#                         conform card's control leg requires); refuses without a snapshot.
#   -Mode restore         put every recorded value back (absent values are removed), print the
#                         result and delete the snapshot.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\playback-hfr-settings-job.ps1 `
#       -Mode record -OutFile <path>\<jobId>.job.ps1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('record', 'setFpsOverride', 'restore')]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$OutFile,

    [ValidatePattern('^[A-Za-z]:\\[A-Za-z0-9 _.~\\-]+$')]
    [string]$AgentRoot = 'C:\mlvtmp\mlv-agent'
)

$ErrorActionPreference = 'Stop'

$template = @'
$ErrorActionPreference = 'Stop'
$Mode = '__MODE__'
$root = 'HKCU:\Software\magiclantern.MLVApp\MLVApp'
$play = "$root\Playback"
$stateFile = Join-Path '__AGENT_ROOT__' 'hfr-settings-orig.json'
$items = @(
    @{ path = $root; name = 'fpsOverride' }, @{ path = $root; name = 'frameRate' },
    @{ path = $root; name = 'audioOutput' }, @{ path = $root; name = 'dragFrameMode' },
    @{ path = $play; name = 'ConformEnabled' }, @{ path = $play; name = 'ConformTargetFps' },
    @{ path = $play; name = 'ConformThresholdFps' }, @{ path = $play; name = 'AutoTargetFps' }
)
function Read-Item($it) {
    $out = [ordered]@{ path = $it.path; name = $it.name; present = $false; value = $null; kind = $null }
    try {
        $k = Get-Item -LiteralPath $it.path -ErrorAction Stop
        if ($k.GetValueNames() -contains $it.name) {
            $out.present = $true
            $out.value = [string]$k.GetValue($it.name)
            $out.kind = [string]$k.GetValueKind($it.name)
        }
    } catch { }
    [pscustomobject]$out
}
function Show($label, $rows) {
    foreach ($r in $rows) {
        Write-Output ("{0} {1}\{2} present={3} kind={4} value={5}" -f $label, ($r.path -replace '^HKCU:\\Software\\', ''), $r.name, $r.present, $r.kind, $r.value)
    }
}
$rows = @($items | ForEach-Object { Read-Item $_ })
if ($Mode -eq 'record') {
    if (-not (Test-Path -LiteralPath $stateFile)) {
        $rows | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $stateFile -Encoding UTF8
        Write-Output "STATE_FILE_WRITTEN $stateFile"
    } else { Write-Output "STATE_FILE_EXISTS $stateFile (original snapshot kept)" }
    Show 'SAVED' $rows
    $c = (Get-Counter '\Processor(*)\% Processor Time' -SampleInterval 3 -MaxSamples 3).CounterSamples
    $per = @{}
    foreach ($g in ($c | Where-Object { $_.InstanceName -ne '_total' } | Group-Object InstanceName)) {
        $per[$g.Name] = [math]::Round((($g.Group | Measure-Object CookedValue -Average).Average), 1)
    }
    $tot = ($c | Where-Object { $_.InstanceName -eq '_total' } | Measure-Object CookedValue -Average).Average
    $ordered = ($per.Keys | Sort-Object { [int]$_ } | ForEach-Object { "cpu$_=$($per[$_])" }) -join ' '
    Write-Output "PERCORE $ordered TOTAL=$([math]::Round($tot, 1))"
} elseif ($Mode -eq 'setFpsOverride') {
    if (-not (Test-Path -LiteralPath $stateFile)) { throw 'no original snapshot: run -Mode record first' }
    if (-not (Test-Path -LiteralPath $root)) { New-Item -Path $root -Force | Out-Null }
    Set-ItemProperty -LiteralPath $root -Name 'fpsOverride' -Value 'true' -Type String
    Set-ItemProperty -LiteralPath $root -Name 'frameRate' -Value '25' -Type String
    Show 'NOW' @($items | ForEach-Object { Read-Item $_ })
} elseif ($Mode -eq 'restore') {
    if (-not (Test-Path -LiteralPath $stateFile)) { throw 'no original snapshot to restore' }
    $orig = @(Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json)
    foreach ($o in $orig) {
        if ($o.present) {
            $type = if ($o.kind -eq 'DWord') { 'DWord' } else { 'String' }
            $val = if ($type -eq 'DWord') { [int]$o.value } else { [string]$o.value }
            if (-not (Test-Path -LiteralPath $o.path)) { New-Item -Path $o.path -Force | Out-Null }
            Set-ItemProperty -LiteralPath $o.path -Name $o.name -Value $val -Type $type
        } elseif (Test-Path -LiteralPath $o.path) {
            Remove-ItemProperty -LiteralPath $o.path -Name $o.name -ErrorAction SilentlyContinue
        }
    }
    Show 'RESTORED' @($items | ForEach-Object { Read-Item $_ })
    Remove-Item -LiteralPath $stateFile -Force
    Write-Output 'STATE_FILE_REMOVED'
}
'@

$text = $template.Replace('__MODE__', $Mode).Replace('__AGENT_ROOT__', $AgentRoot.Replace("'", "''"))
$outDir = Split-Path -Parent $OutFile
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
[IO.File]::WriteAllText($OutFile, $text, [Text.UTF8Encoding]::new($false))
[pscustomobject]@{ jobFile = $OutFile; mode = $Mode }
