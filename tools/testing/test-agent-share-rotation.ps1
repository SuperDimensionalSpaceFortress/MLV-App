# Tests tools\profiling\agent-share\Invoke-AgentShareRotation.ps1 and Get-AgentShareRecent.ps1 against a temp
# mock agent root. Exit 0 = pass, 1 = fail.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$rot = Join-Path $repo 'tools\profiling\agent-share\Invoke-AgentShareRotation.ps1'
$recent = Join-Path $repo 'tools\profiling\agent-share\Get-AgentShareRecent.ps1'
$root = Join-Path ([IO.Path]::GetTempPath()) ("agent-share-rot-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$fails = 0
function Check([bool]$ok, [string]$what) { if ($ok) { "PASS $what" } else { "FAIL $what"; $script:fails++ } }

$now = [datetime]::new(2026, 10, 17, 12, 0, 0, [DateTimeKind]::Utc)
function New-Entry([string]$rel, [int]$ageDays, [switch]$Dir) {
    $p = Join-Path $root $rel
    if ($Dir) { New-Item -ItemType Directory -Force -Path $p | Out-Null; Set-Content (Join-Path $p 'x.txt') 'x' }
    else { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null; Set-Content $p 'x' }
    $t = $now.AddDays(-$ageDays)
    if ($Dir) { [IO.Directory]::SetLastWriteTimeUtc($p, $t) } else { [IO.File]::SetLastWriteTimeUtc($p, $t) }
}
try {
    foreach ($d in 'inbox', 'running', 'processed', 'outbox') { New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null }
    # old job: all members old -> moves together
    New-Entry 'processed\old-a.job.ps1' 60; New-Entry 'processed\old-a.meta.json' 60; New-Entry 'processed\old-a.started.completed.json' 59
    New-Entry 'outbox\old-a.result.json' 59; New-Entry 'outbox\old-a.artifacts' 59 -Dir
    # mixed job: one member young -> stays whole
    New-Entry 'processed\mixed-b.job.ps1' 60; New-Entry 'processed\mixed-b.started.completed.json' 2
    # young job
    New-Entry 'outbox\young-c.result.json' 1
    # old but live in running\ -> skipped
    New-Entry 'outbox\live-d.result.json' 90; New-Entry 'running\live-d.started.json' 0
    # collision: archive already holds the name -> skipped, not overwritten
    New-Entry 'outbox\coll-e.result.json' 45; New-Entry 'archive\outbox\2026-09\coll-e.result.json' 45
    $collBefore = [IO.File]::ReadAllText((Join-Path $root 'archive\outbox\2026-09\coll-e.result.json'))

    $dry = pwsh -NoProfile -File $rot -AgentShare $root -OlderThanDays 30 -NowUtc $now.ToString('o') | Out-String | ConvertFrom-Json
    Check ($dry.mode -eq 'dry-run') 'default mode is dry-run'
    Check (Test-Path (Join-Path $root 'processed\old-a.job.ps1')) 'dry-run moves nothing'
    Check ($dry.directories.processed.eligibleEntries -eq 3) "dry-run processed eligibleEntries=3 (got $($dry.directories.processed.eligibleEntries))"
    Check ($dry.directories.outbox.eligibleEntries -eq 2) "dry-run outbox eligibleEntries=2 (got $($dry.directories.outbox.eligibleEntries))"
    Check ($dry.directories.outbox.keptLive -eq 1) 'live job counted keptLive'
    Check ($dry.directories.outbox.skippedCollision -eq 1) 'collision counted'
    Check (-not (Test-Path (Join-Path $root 'archive\rotation-manifests'))) 'dry-run writes no manifest'

    $app = pwsh -NoProfile -File $rot -AgentShare $root -OlderThanDays 30 -NowUtc $now.ToString('o') -Apply | Out-String | ConvertFrom-Json
    Check ($LASTEXITCODE -eq 0) 'apply exit 0'
    Check (Test-Path (Join-Path $root 'archive\processed\2026-08\old-a.job.ps1')) 'old job moved to month of newest member'
    Check (Test-Path (Join-Path $root 'archive\processed\2026-08\old-a.started.completed.json')) 'whole group moved'
    Check (Test-Path (Join-Path $root 'archive\outbox\2026-08\old-a.artifacts\x.txt')) 'artifacts dir moved intact'
    Check (Test-Path (Join-Path $root 'processed\mixed-b.job.ps1')) 'mixed group not split'
    Check (Test-Path (Join-Path $root 'outbox\young-c.result.json')) 'young kept'
    Check (Test-Path (Join-Path $root 'outbox\live-d.result.json')) 'live kept'
    Check (Test-Path (Join-Path $root 'outbox\coll-e.result.json')) 'collision source kept'
    Check ([IO.File]::ReadAllText((Join-Path $root 'archive\outbox\2026-09\coll-e.result.json')) -eq $collBefore) 'collision dest not overwritten'
    $man = @(Get-ChildItem (Join-Path $root 'archive\rotation-manifests') -Filter *.jsonl)
    $rows = @(Get-Content $man[0].FullName | ForEach-Object { $_ | ConvertFrom-Json })
    Check ($man.Count -eq 1 -and $rows.Count -eq 5 -and @($rows | Where-Object { -not $_.ok }).Count -eq 0) "manifest has 5 ok rows (got $($rows.Count))"

    $top = pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -First 1 -AsJson | Out-String | ConvertFrom-Json
    Check (@($top).Count -eq 1 -and @($top)[0].name -eq 'young-c.result.json') 'recent -First 1 returns newest only'
    $hit = pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -JobId old-a -IncludeArchive -AsJson | Out-String | ConvertFrom-Json
    Check (@($hit).Count -eq 2) "recent -JobId -IncludeArchive finds archived job (got $(@($hit).Count))"
} finally {
    Remove-Item -Recurse -Force -LiteralPath $root -ErrorAction SilentlyContinue
}
if ($fails) { "FAILED $fails"; exit 1 } else { 'ALL PASS'; exit 0 }
