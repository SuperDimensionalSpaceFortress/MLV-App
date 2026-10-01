<#
.SYNOPSIS
    Measure the auto-WB solve as a DISTRIBUTION across builds. Never quotes a single draw.

.DESCRIPTION
    WHY A DISTRIBUTION AND NOT A VALUE.
    MLVAPP_LOOK_ASSIST_WB_TRACE emits one WB_TRACE_WORKER line PER ANALYSIS, and a single run can
    emit several with DIFFERENT values - measured 2026-09-03, one run produced both 7330/-33 and
    7430/-34. So 'the last line' is an arbitrary pick, and a bisect built on single draws is a
    bisect built on noise. Every number this board published from that probe before this script
    existed was a draw.

    THE WIDTH IS THE SIGNAL, NOT AN ERROR BAR TO AVERAGE AWAY.
    Three runs each, same footage, same settings:
        a6bf25f9 (isolate thumbnail + WB analysis) -> -34,-33 / -34,-33 / -34,-33   spread  1
        aa0cab24 (wire isolated WB analysis)       -> -32,7 / 19,13 / -49,-28       spread 68
    The defect introduced at aa0cab24 is VARIANCE, not an offset. Averaging it away destroys the
    finding; reporting min/max/distinct preserves it.

    A clamp can hide this downstream: qBound(-35, tint, 18) at MainWindow.cpp:15567 emits a
    rock-steady -35 for any raw value below the floor, so post-clamp reporting can show a stable
    number over a wildly unstable solve. Always measure PRE-clamp.

    EVIDENCE IS VALID ONLY WHEN THE LAUNCHER SAYS SO (PLAYBACK-CLIP-LENGTH-ENFORCE-4).
    Every sample is the product of an evidence Play started through the TRACKED launcher
    capture-reference-frame.ps1, which exits 0 only when the app's own receipt proves its engine consumed >= 20 s of
    source footage (exit 43 = INVALID, 4 = the app failed). This script reads that exit code for EVERY run:
    a run that exited non-zero, or that wrote no capture.err.txt, is an INVALID run -- its traces never enter the
    distribution, it is counted in invalidRuns, and the script exits 43. A build that is missing is a failure too. The
    published JSON carries verdict OK / INVALID so nothing downstream can mistake a partial set for a measurement.
    (The four built-in historical binaries all predate the receipt fields, so today every run of them is INVALID by
    design: they are a bisect list, not evidence, until rebuilt with the receipt.)

    The footage is the caller's: pass -ClipPath (real footage of >= 20 s). This script names none.

.NOTES
    ASCII-only by project convention. Run ON the GPU bench. Uses the app's own capture path;
    never a desktop grab - the bench is a machine its owner uses interactively.
#>
param(
    [Parameter(Mandatory = $true)][string]$ClipPath,
    [string]$Root = 'C:\mlvtmp\mlv-agent',
    # The tracked launcher in THIS checkout -- never a staged copy that can drift from the oracle.
    [string]$CaptureScript = (Join-Path $PSScriptRoot 'capture-reference-frame.ps1'),
    [int]$Runs = 3
)
# VALID MEASUREMENT of raw_wb_tint: a DISTRIBUTION, not a draw.
# Established 2026-09-03: the WB probe fires once per analysis and a single run emitted BOTH
# 7330/-33 and 7430/-34. Taking "the last line" is an arbitrary pick, so every earlier bisect
# number was a draw. This collects EVERY WB_TRACE_WORKER line across N VALID runs per build and reports
# n / distinct / median / min / max. Bachelor is also thermally unstable, so aggregation is
# required on this venue regardless (bachelor-cannot-decide-playback-ab-20260830: 3 legs minimum).
$ErrorActionPreference='Continue'
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$base=Join-Path $Root ("outbox\wbdist-$stamp")
$env:MLVAPP_LOOK_ASSIST_WB_TRACE='1'

$points=@(
 @{tag='765ed4a3d066-dirty'; label='Jun-09 GOOD (memo)';           dir='june-765ed4a3d066-dirty'},
 @{tag='d14139e8225e-dirty'; label='Jun-23 keep-LA-bright-diso';   dir='june-d14139e8225e-dirty'},
 @{tag='ba9dec3f3427';       label='Jun-27 tint-floor bandaid';    dir='june-ba9dec3f3427'},
 @{tag='5a30efddd186';       label='Sep-02 modern #25';            dir='mlvapp-5a30efddd186'}
)
function Stat($v){
  if(-not $v -or $v.Count -eq 0){ return $null }
  $s=@($v | Sort-Object); $n=$s.Count
  [ordered]@{ n=$n; distinct=@($v|Select-Object -Unique).Count; median=$s[[int][math]::Floor($n/2)]; min=$s[0]; max=$s[$n-1]
              values=((@($v|Select-Object -Unique|Sort-Object)) -join ',') }
}
$res=[ordered]@{schema='mlv-app/wbtint-distribution/v2'; host=$env:COMPUTERNAME; runsPerPoint=$Runs; verdict='OK'; points=@()}
$invalidTotal=0
$errorTotal=0
foreach($p in $points){
  $exe=Join-Path $Root ("staging\" + $p.dir + "\MLVApp-" + $p.tag + ".exe")
  $row=[ordered]@{tag=$p.tag; label=$p.label}
  if(-not (Test-Path -LiteralPath $exe)){
    $row.err='exe missing: '+$exe; $row.validRuns=0; $row.invalidRuns=0; $row.tint=$null; $row.temp=$null
    $errorTotal++; $res.points+=$row; continue }
  $tints=@(); $temps=@(); $valid=0; $invalid=0; $invalidReasons=@()
  for($i=1;$i -le $Runs;$i++){
    $o=Join-Path $base ("{0}-r{1}" -f $p.tag,$i)
    & pwsh -NoProfile -File $CaptureScript -Exe $exe -Clip $ClipPath -OutDir $o -Commit $p.tag -Seconds 45 -PresentedFrames 0 2>&1 | Out-Null
    $captureExit=$LASTEXITCODE
    $ef=Join-Path $o 'capture.err.txt'
    # The launcher's exit code is the verdict: 0 = the app's receipt proved >= 20 s of source footage; anything else
    # (43 INVALID, 4 app failure, ...) is not a measurement, whatever capture.err.txt happens to hold.
    if($captureExit -ne 0 -or -not (Test-Path -LiteralPath $ef)){
      $invalid++
      $invalidReasons += $(if($captureExit -ne 0){ "r$i exit $captureExit" } else { "r$i no capture.err.txt" })
      continue
    }
    $valid++
    foreach($l in (Get-Content -LiteralPath $ef)){
      if($l -match 'WB_TRACE_WORKER.*raw_wb_temp=(-?[0-9]+) raw_wb_tint=(-?[0-9]+)'){
        $temps+=[int]$Matches[1]; $tints+=[int]$Matches[2] }
    }
  }
  $row.validRuns=$valid
  $row.invalidRuns=$invalid
  if($invalid -gt 0){ $row.invalidReasons=$invalidReasons }
  $row.tint=Stat $tints
  $row.temp=Stat $temps
  $invalidTotal+=$invalid
  $res.points+=$row
}
if($invalidTotal -gt 0 -or $errorTotal -gt 0){ $res.verdict='INVALID' }
Write-Output ($res | ConvertTo-Json -Depth 8 -Compress)
if($invalidTotal -gt 0){ [Console]::Error.WriteLine("measure-wb-solve-distribution: $invalidTotal capture(s) were INVALID (not playback evidence); their traces were excluded. exit 43."); exit 43 }
if($errorTotal -gt 0){ [Console]::Error.WriteLine("measure-wb-solve-distribution: $errorTotal build(s) missing; the set is incomplete. exit 43."); exit 43 }
exit 0
