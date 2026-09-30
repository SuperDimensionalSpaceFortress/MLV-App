# attr3-trace-fetch-job.ps1 -- GENERATOR (runs locally). Emits a tiny job that prints a pre-launch
# trace file written by the ATTR3 owner-clip jobs (BACHELOR-OWNER-CLIP-STAGE-STALL-1). A job the
# agent kills at its cap returns no stdout, but its trace file (<AgentRoot>\logs\<JobId>.trace.txt,
# one timestamped line per pre-launch step, appended and flushed as it happens) survives on the
# host; this fetches it. With no -TraceJobId it lists the ten newest trace files (name, bytes,
# last write) and prints the tail of the newest one.
#
#   pwsh -NoProfile -File tools\profiling\bachelor\attr3-trace-fetch-job.ps1 -OutDir <dir> [-TraceJobId <id>] [-Tail 200]
# then submit the emitted job with tools\profiling\um-run.ps1 -AgentShare '\\bachelor\mlv-agent'.
# Trace lines are path-free by construction (part indexes, sizes, rates, step names).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutDir,
    [ValidatePattern('^[A-Za-z0-9._-]{1,120}\z')][string]$TraceJobId = '',
    [ValidateRange(1, 5000)][int]$Tail = 200,
    [string]$AgentRoot = 'C:\mlvtmp\mlv-agent'
)

$ErrorActionPreference = 'Stop'
if ($AgentRoot -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$') {
    throw 'ATTR3_TRACEFETCH_AGENTROOT_INVALID -AgentRoot contains characters outside the allowlist'
}
$jobId = "attr3-trace-fetch-$([guid]::NewGuid().ToString('N').Substring(0, 10))"
$body = @"
`$ErrorActionPreference = 'Stop'
`$Logs = Join-Path '$AgentRoot' 'logs'
`$Wanted = '$TraceJobId'
`$Tail = $Tail
`$files = @(Get-ChildItem -LiteralPath `$Logs -Filter '*.trace.txt' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)
Write-Output "TRACE_FILES=`$(`$files.Count)"
foreach (`$f in (`$files | Select-Object -First 10)) { Write-Output ("TRACE_LISTING {0} bytes={1} lastWriteUtc={2:o}" -f `$f.Name, `$f.Length, `$f.LastWriteTimeUtc) }
`$pick = if (`$Wanted) { `$files | Where-Object { `$_.Name -eq (`$Wanted + '.trace.txt') -or `$_.BaseName -eq `$Wanted } | Select-Object -First 1 } else { `$files | Select-Object -First 1 }
if (`$null -eq `$pick) { Write-Output 'RESULT=TRACE_NOT_FOUND'; exit 1 }
Write-Output "TRACE_BEGIN `$(`$pick.Name)"
Get-Content -LiteralPath `$pick.FullName -Tail `$Tail | ForEach-Object { Write-Output `$_ }
Write-Output 'TRACE_END'
Write-Output 'RESULT=TRACE_FETCHED'
exit 0
"@
if (-not (Test-Path -LiteralPath $OutDir)) { [void](New-Item -ItemType Directory -Path $OutDir -Force) }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$jobPath = Join-Path $OutDir "$jobId.job.ps1"
[IO.File]::WriteAllText($jobPath, $body, [Text.UTF8Encoding]::new($false))
Write-Output "RESULT=TRACE_FETCH_JOB_EMITTED JOB=$jobId"
