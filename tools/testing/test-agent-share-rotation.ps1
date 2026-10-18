# Tests tools\profiling\agent-share\Invoke-AgentShareRotation.ps1, Get-AgentShareRecent.ps1 and AgentShare.psm1 against
# temp mock agent roots. Exit 0 = pass, 1 = fail.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$rot = Join-Path $repo 'tools\profiling\agent-share\Invoke-AgentShareRotation.ps1'
$recent = Join-Path $repo 'tools\profiling\agent-share\Get-AgentShareRecent.ps1'
Import-Module (Join-Path $repo 'tools\profiling\agent-share\AgentShare.psm1') -Force
$fails = 0
function Check([bool]$ok, [string]$what) { if ($ok) { "PASS $what" } else { "FAIL $what"; $script:fails++ } }

$now = [datetime]::new(2026, 10, 17, 12, 0, 0, [DateTimeKind]::Utc)
$script:root = $null
function New-Root {
    $script:root = Join-Path ([IO.Path]::GetTempPath()) ("agent-share-rot-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    foreach ($d in 'inbox', 'running', 'processed', 'outbox') { New-Item -ItemType Directory -Force -Path (Join-Path $script:root $d) | Out-Null }
}
function New-Entry([string]$rel, [int]$ageDays, [switch]$Dir) {
    $p = Join-Path $script:root $rel
    if ($Dir) { New-Item -ItemType Directory -Force -Path $p | Out-Null; Set-Content (Join-Path $p 'x.txt') 'x' }
    else { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null; Set-Content $p 'x' }
    $t = $now.AddDays(-$ageDays)
    if ($Dir) { [IO.Directory]::SetLastWriteTimeUtc($p, $t) } else { [IO.File]::SetLastWriteTimeUtc($p, $t) }
}
function Has([string]$rel) { Test-Path -LiteralPath (Join-Path $script:root $rel) }
function Invoke-Rot([string[]]$extra) { pwsh -NoProfile -File $rot -AgentShare $script:root -OlderThanDays 30 -NowUtc $now.ToString('o') @extra | Out-String | ConvertFrom-Json }
function Remove-Root { if ($script:root) { Remove-Item -Recurse -Force -LiteralPath $script:root -ErrorAction SilentlyContinue } }

# --- job-key rules (B1/B3 basis)
$keys = @{
    'J.job.ps1' = 'J'; 'J.meta.json' = 'J'; 'J.started.completed.json' = 'J'; 'J.started.requeue-no-job.json' = 'J'
    'J.started.json' = 'J'; 'J.started.json.tmp' = 'J'; 'J.started.json.0123456789abcdef0123456789abcdef.tmp' = 'J'
    'J.started.json.0123456789abcdef0123456789abcdef.tmp.stale-638000000000000000' = 'J'
    'J.result.json' = 'J'; 'J.result.tmp' = 'J'; 'J.result.0123456789abcdef0123456789abcdef.tmp' = 'J'
    'J.artifacts' = 'J'; 'J.artifacts.unproven-1' = 'J'; 'J.cancel' = 'J'
    'J.other.result.json' = 'J.other'; 'J.other.started.json.tmp' = 'J.other'; 'wbdet-20260903-141643' = 'wbdet-20260903-141643'
}
foreach ($n in $keys.Keys) { Check ((Get-AgentShareJobKey -Name $n) -ceq $keys[$n]) "job key '$n' -> '$($keys[$n])' (got '$(Get-AgentShareJobKey -Name $n)')" }

try {
    # --- base scenario
    New-Root
    New-Entry 'processed\old-a.job.ps1' 60; New-Entry 'processed\old-a.meta.json' 60; New-Entry 'processed\old-a.started.completed.json' 59
    New-Entry 'outbox\old-a.result.json' 59; New-Entry 'outbox\old-a.artifacts' 59 -Dir
    New-Entry 'processed\mixed-b.job.ps1' 60; New-Entry 'processed\mixed-b.started.completed.json' 2
    New-Entry 'outbox\young-c.result.json' 1
    New-Entry 'outbox\live-d.result.json' 90; New-Entry 'running\live-d.started.json' 0
    # B1: the ONLY running entry is an unresolved tmp marker (both producer shapes)
    New-Entry 'processed\tmp-f.job.ps1' 90; New-Entry 'outbox\tmp-f.result.json' 90; New-Entry 'running\tmp-f.started.json.0123456789abcdef0123456789abcdef.tmp' 0
    New-Entry 'outbox\tmp-g.result.json' 90; New-Entry 'running\tmp-g.started.json.tmp' 0
    # B2: old processed half + young outbox half must not split
    New-Entry 'processed\split-h.job.ps1' 60; New-Entry 'processed\split-h.started.completed.json' 60; New-Entry 'outbox\split-h.result.json' 3
    # B2: collision in ONE directory keeps BOTH halves live
    New-Entry 'processed\coll-e.job.ps1' 45; New-Entry 'outbox\coll-e.result.json' 45; New-Entry 'archive\outbox\2026-09\coll-e.result.json' 45
    $collBefore = [IO.File]::ReadAllText((Join-Path $root 'archive\outbox\2026-09\coll-e.result.json'))

    $dry = Invoke-Rot @()
    Check ($dry.mode -eq 'dry-run') 'default mode is dry-run'
    Check (Has 'processed\old-a.job.ps1') 'dry-run moves nothing'
    Check (-not (Has 'archive\rotation-manifests')) 'dry-run writes no manifest'
    Check ($dry.stats.eligibleJobs -eq 1) "only old-a is eligible (got $($dry.stats.eligibleJobs): $($dry.stats.samples -join '; '))"
    Check ($dry.stats.eligibleEntries.processed -eq 3 -and $dry.stats.eligibleEntries.outbox -eq 2) 'eligible entries 3 processed + 2 outbox'
    Check ($dry.stats.keptLive -eq 3) "live-d, tmp-f, tmp-g kept live (got $($dry.stats.keptLive))"
    Check ($dry.stats.skippedCollision -eq 1) 'coll-e skipped as a whole job'

    $app = Invoke-Rot @('-Apply')
    Check ($LASTEXITCODE -eq 0 -and -not $app.stoppedOn) 'apply exit 0'
    Check ((Has 'archive\processed\2026-08\old-a.job.ps1') -and (Has 'archive\processed\2026-08\old-a.started.completed.json')) 'old job processed half moved'
    Check ((Has 'archive\outbox\2026-08\old-a.result.json') -and (Has 'archive\outbox\2026-08\old-a.artifacts\x.txt')) 'old job outbox half moved, same month'
    Check ((Has 'processed\mixed-b.job.ps1') -and (Has 'outbox\young-c.result.json') -and (Has 'outbox\live-d.result.json')) 'mixed, young, live kept'
    Check ((Has 'processed\tmp-f.job.ps1') -and (Has 'outbox\tmp-f.result.json') -and (Has 'outbox\tmp-g.result.json')) 'B1: tmp-marker-only jobs kept'
    Check ((Has 'processed\split-h.job.ps1') -and (Has 'outbox\split-h.result.json')) 'B2: old processed + young outbox not split'
    Check ((Has 'processed\coll-e.job.ps1') -and (Has 'outbox\coll-e.result.json')) 'B2: one-directory collision keeps both halves'
    Check ([IO.File]::ReadAllText((Join-Path $root 'archive\outbox\2026-09\coll-e.result.json')) -eq $collBefore) 'collision dest not overwritten'
    $rows = @(Get-ChildItem (Join-Path $root 'archive\rotation-manifests') -Filter *.jsonl | ForEach-Object { Get-Content $_.FullName } | ForEach-Object { $_ | ConvertFrom-Json })
    Check (@($rows | Where-Object phase -eq 'intent').Count -eq 5 -and @($rows | Where-Object { $_.phase -eq 'done' -and $_.ok }).Count -eq 5) "manifest: 5 intent + 5 ok done rows (got $($rows.Count))"
    Check ($rows[0].phase -eq 'intent' -and $rows[1].phase -eq 'done' -and $rows[0].name -eq $rows[1].name) 'intent row precedes its done row'

    # --- B3: exact JobId lookup with overlapping dotted ids; JSON arrays
    New-Entry 'outbox\J.result.json' 1; New-Entry 'outbox\J.other.result.json' 1; New-Entry 'outbox\Jx.result.json' 1; New-Entry 'outbox\J.artifacts' 1 -Dir
    $hit = @(pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -JobId J -AsJson | Out-String | ConvertFrom-Json)
    Check ($hit.Count -eq 2 -and -not ($hit.name -contains 'J.other.result.json') -and -not ($hit.name -contains 'Jx.result.json')) "B3: -JobId J returns J only (got $($hit.name -join ','))"
    $hit2 = @(pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -JobId J.other -AsJson | Out-String | ConvertFrom-Json)
    Check ($hit2.Count -eq 1 -and $hit2[0].name -eq 'J.other.result.json') 'B3: -JobId J.other returns its own entry'
    $one = pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -JobId J.other -AsJson | Out-String
    Check ($one.TrimStart().StartsWith('[')) 'one-row JSON is an array'
    $none = pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -JobId nosuch -AsJson | Out-String
    Check ($none.Trim() -eq '[]') "empty JSON is [] (got '$($none.Trim())')"
    $top = @(pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -First 1 -AsJson | Out-String | ConvertFrom-Json)
    Check ($top.Count -eq 1) 'recent -First 1 returns one row'
    $arch = @(pwsh -NoProfile -File $recent -AgentShare $root -Directory outbox -JobId old-a -IncludeArchive -AsJson | Out-String | ConvertFrom-Json)
    Check ($arch.Count -eq 2) "-IncludeArchive finds the archived job (got $($arch.Count))"
    $null = pwsh -NoProfile -File $recent -AgentShare (Join-Path $root 'nope') -Directory outbox -JobId J -AsJson 2>&1
    Check ($LASTEXITCODE -ne 0) 'unreachable root is an error, not an empty answer'
    Remove-Root

    # --- failed move: the run stops, names the partial job, and the manifest accounts for it
    New-Root
    New-Entry 'processed\p-1.job.ps1' 60; New-Entry 'outbox\p-1.result.json' 60
    New-Entry 'processed\q-2.job.ps1' 50
    $hook = { param($m) if ($m.dir -eq 'outbox' -and $m.entry.Name -eq 'p-1.result.json') { throw 'simulated rename failure' } }
    $out = & $rot -AgentShare $root -OlderThanDays 30 -NowUtc $now -Apply -TestHookBeforeMove $hook | Out-String | ConvertFrom-Json
    Check ($LASTEXITCODE -eq 3) "failed move exits 3 (got $LASTEXITCODE)"
    Check ($out.stoppedOn.job -eq 'p-1' -and $out.stoppedOn.step -eq 'move' -and @($out.stoppedOn.movedMembers).Count -eq 1) 'stoppedOn names the partial job and its moved member'
    Check ((Has 'processed\q-2.job.ps1')) 'run stopped: the next job was not touched'
    $rows = @(Get-ChildItem (Join-Path $root 'archive\rotation-manifests') -Filter *.jsonl | ForEach-Object { Get-Content $_.FullName } | ForEach-Object { $_ | ConvertFrom-Json })
    Check (@($rows | Where-Object { $_.phase -eq 'done' -and -not $_.ok }).Count -eq 1) 'manifest records the failed move'
    Remove-Root

    # --- -MaxGroups counts jobs, never splits one
    New-Root
    New-Entry 'processed\m-1.job.ps1' 70; New-Entry 'outbox\m-1.result.json' 70
    New-Entry 'processed\m-2.job.ps1' 60; New-Entry 'outbox\m-2.result.json' 60
    $cap = Invoke-Rot @('-Apply', '-MaxGroups', '1')
    Check ($cap.stats.eligibleJobs -eq 1 -and $cap.stats.keptCap -eq 1) 'cap: one job planned, one kept'
    Check ((Has 'archive\processed\2026-08\m-1.job.ps1') -and (Has 'archive\outbox\2026-08\m-1.result.json')) 'cap: oldest job moved whole'
    Check ((Has 'processed\m-2.job.ps1') -and (Has 'outbox\m-2.result.json')) 'cap: capped job kept whole'
} finally {
    Remove-Root
}
if ($fails) { "FAILED $fails"; exit 1 } else { 'ALL PASS'; exit 0 }
