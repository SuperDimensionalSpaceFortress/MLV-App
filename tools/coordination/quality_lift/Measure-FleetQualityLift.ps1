# Measure-FleetQualityLift.ps1 -- how often the Chief of Staff (CoS / Dot) finds a demonstrated defect that a project's own
# factory did not catch first, per project and fleet-wide. Owner 2026-10-25: implement the quality-lift measurement fleet wide.
# Project-neutral: every project-specific value comes from the bus data itself, the bus clone (-Bus) and the output folder
# (-OutDir). Nothing here names a project, a host or a board path. See README.md beside this file.
#
# READ-ONLY on the bus and on GitHub. It reads:
#   - the bus history of cos-feedback/<project>/pr-*.md (one CoS pass per commit and file; the section header
#     "# <owner>/<repo> #<n> / <40-hex head>" names the repo, PR and head; a first "## Blockers" line B<n> = blocked)
#   - GitHub commit statuses on each blocked head (every context, reported per context)
#   - PR bodies created in the last 24 h, for an "Adversary pass" line (shift-left adoption)
# It writes ONLY under -OutDir:
#   single project (-Project x): <OutDir>\quality-lift.md|.json, history.jsonl, corpus.jsonl, status-cache.json
#   all projects (default):      <OutDir>\<project>\... per project, plus <OutDir>\fleet.md and fleet.json
# Definitions (lower is better on every rate):
#   blockedRate        share of CoS passes (first pass per head; a head re-reviewed clean-then-blocked counts at its first
#                      BLOCKING pass) whose first Blockers line starts B<n>
#   firstRoundBlocked  PRs whose first CoS pass was blocked
#   noPriorKey         blocked heads with NO commit status created before the CoS pass (CoS read it first)
#   trueMiss           blocked heads where some status was 'success' BEFORE the CoS pass (the factory approved a defect)
#   roundsPerPr        distinct heads CoS reviewed per PR
# Inputs:
#   -Bus        the fleet doctrine bus clone; default $env:FACTORY_BUS_CLONE. Required one way or the other.
#   -OutDir     where the outputs go. Required.
#   -Since      ISO time (an offset or Z); default 7 days ago. Normalised to UTC before use.
#   -NoStatuses read nothing from GitHub (no statuses, no PR bodies): the status cache already in -OutDir is still used and
#               the adoption counts read n/a. -NoFetch skips the bus `git pull --ff-only`.
# Exit 0 = measured; 2 = measured with source errors (named in the report); 3 = bus unreadable (unset, missing, no
# cos-feedback/, or not a git repository); 4 = usage error (no -OutDir, an unparseable -Since).
param(
    [string]$Bus = $env:FACTORY_BUS_CLONE,
    [string]$Project = "",
    [string]$Since = ([DateTime]::UtcNow.AddDays(-7).ToString('yyyy-MM-ddTHH:mm:ssZ')),
    [string]$OutDir = "",
    [switch]$NoStatuses,
    [switch]$NoFetch
)
$ErrorActionPreference = 'Stop'

# ISO text or DateTime -> UTC DateTime. A bare [DateTime] cast of ISO text yields LOCAL time, and ConvertFrom-Json already
# returns DateTime objects; both silently break before/after comparisons (measured 2026-10-25).
function ConvertTo-QlUtc([object]$s) {
    if ($s -is [DateTime]) { if ($s.Kind -eq [DateTimeKind]::Utc) { return $s } else { return $s.ToUniversalTime() } }
    ([DateTimeOffset]::Parse([string]$s, [Globalization.CultureInfo]::InvariantCulture)).UtcDateTime
}

function Get-QlPasses([string]$Proj) {
    $out = @()
    $log = @(git -C $Bus log --since=$Since --format="%H|%cI" -- "cos-feedback/$Proj")
    if ($LASTEXITCODE -ne 0) { throw "git log of cos-feedback/$Proj failed (exit $LASTEXITCODE)" }
    foreach ($c in $log) {
        $h, $t = $c.Split('|')
        $files = @(git -C $Bus show --name-only --format= $h | Where-Object { $_ -like "cos-feedback/$Proj/pr-*.md" })
        foreach ($f in $files) {
            $txt = (git -C $Bus show "${h}:$f" 2>$null) -join "`n"
            $ms = [regex]::Matches($txt, '(?m)^# ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) #(\d+) / ([0-9a-f]{40})')
            if ($ms.Count -eq 0) { continue }
            $m = $ms[$ms.Count - 1]
            $sec = $txt.Substring($m.Index)
            $i = $sec.IndexOf('## Blockers')
            $blk = ''
            if ($i -ge 0) { $blk = ((($sec.Substring($i + 11) -split "`n") | Where-Object { $_.Trim() } | Select-Object -First 1) + '').Trim() }
            $out += [pscustomobject]@{
                utc = (ConvertTo-QlUtc $t); repo = $m.Groups[1].Value; pr = [int]$m.Groups[2].Value
                head = $m.Groups[3].Value; blocked = ($blk -match '^B\d'); blocker = $(if ($blk.Length -gt 160) { $blk.Substring(0, 160) } else { $blk })
            }
        }
    }
    @($out | Sort-Object utc | Group-Object { "$($_.repo)|$($_.pr)|$($_.head)" } | ForEach-Object {
        $bl = @($_.Group | Where-Object blocked)
        if ($bl.Count) { $bl[0] } else { $_.Group[0] }
    })
}

function Get-QlWindow([object[]]$P, [string]$Name, [hashtable]$Cache) {
    $P = @($P)
    $blocked = @($P | Where-Object blocked)
    $firstPerPr = @($P | Group-Object { "$($_.repo)#$($_.pr)" } | ForEach-Object { $_.Group | Sort-Object utc | Select-Object -First 1 })
    $noKey = 0; $trueMiss = 0; $keyStats = [ordered]@{}
    foreach ($b in $blocked) {
        $rows = @(); if ($Cache[$b.head]) { $rows = @($Cache[$b.head].rows) }
        $before = @($rows | Where-Object { (ConvertTo-QlUtc $_.firstUtc) -lt $b.utc })
        if ($before.Count -eq 0) { $noKey++ }
        if (@($before | Where-Object { $_.state -eq 'success' }).Count) { $trueMiss++ }
        foreach ($r in $before) {
            if (-not $keyStats.Contains($r.context)) { $keyStats[$r.context] = [ordered]@{ successBeforeCos = 0; failureBeforeCos = 0 } }
            if ($r.state -eq 'success') { $keyStats[$r.context].successBeforeCos++ } elseif ($r.state -in 'failure', 'error') { $keyStats[$r.context].failureBeforeCos++ }
        }
    }
    $rounds = @($P | Group-Object { "$($_.repo)#$($_.pr)" } | ForEach-Object { $_.Count })
    [ordered]@{
        window = $Name; passes = $P.Count; blocked = $blocked.Count
        blockedRate = $(if ($P.Count) { [math]::Round($blocked.Count / $P.Count, 3) } else { $null })
        prs = $firstPerPr.Count; firstRoundBlocked = @($firstPerPr | Where-Object blocked).Count
        firstRoundBlockedRate = $(if ($firstPerPr.Count) { [math]::Round(@($firstPerPr | Where-Object blocked).Count / $firstPerPr.Count, 3) } else { $null })
        noPriorKey = $noKey; trueMiss = $trueMiss
        roundsPerPr = $(if ($rounds.Count) { [math]::Round(($rounds | Measure-Object -Average).Average, 2) } else { $null })
        roundsPerPrMax = $(if ($rounds.Count) { ($rounds | Measure-Object -Maximum).Maximum } else { $null })
        keysBeforeCos = $keyStats
    }
}

function Measure-QlProject([string]$Proj, [string]$Dir) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $now = [DateTime]::UtcNow
    $errors = @()
    $passes = @()
    try { $passes = @(Get-QlPasses $Proj) } catch { $errors += "bus ${Proj}: $($_.Exception.Message)" }

    $cacheFile = Join-Path $Dir 'status-cache.json'
    $cache = @{}
    if (Test-Path $cacheFile) { try { (Get-Content $cacheFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cache[$_.Name] = $_.Value } } catch { $cache = @{} } }
    if (-not $NoStatuses) {
        foreach ($p in ($passes | Where-Object blocked)) {
            $e = $cache[$p.head]
            if ($e -and ((($now - (ConvertTo-QlUtc $e.fetched)).TotalHours -lt 1) -or (($now - $p.utc).TotalHours -gt 6))) { continue }
            try {
                $st = gh api "repos/$($p.repo)/commits/$($p.head)/statuses?per_page=100" 2>$null | ConvertFrom-Json
                if ($LASTEXITCODE -ne 0) { throw "gh api exit $LASTEXITCODE" }
                $rows = @($st | Group-Object context | ForEach-Object {
                    $first = $_.Group | Sort-Object created_at | Select-Object -First 1
                    $last = $_.Group | Sort-Object created_at | Select-Object -Last 1
                    [pscustomobject]@{ context = $_.Name; firstUtc = (ConvertTo-QlUtc $first.created_at).ToString('o'); state = $last.state }
                })
                $cache[$p.head] = [pscustomobject]@{ fetched = $now.ToString('o'); rows = $rows }
            } catch { $errors += "statuses $($p.repo)@$($p.head.Substring(0,8)): $($_.Exception.Message)" }
        }
        ($cache | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $cacheFile -Encoding utf8
    }

    $all = Get-QlWindow $passes 'since' $cache
    $d1 = Get-QlWindow @($passes | Where-Object { $_.utc -ge $now.AddHours(-24) }) 'last-24h' $cache

    $adopt = [ordered]@{ prs = $null; withAdversaryLine = $null }
    $repos = @($passes | ForEach-Object repo | Sort-Object -Unique)
    if ($repos.Count -and -not $NoStatuses) {
        $n = 0; $w = 0
        foreach ($rp in $repos) {
            try {
                # No --search: where gh resolves through a .cmd shim, '>' in "created:>=" becomes a redirect.
                $list = gh pr list --repo $rp --state all --limit 80 --json number,body,createdAt 2>$null
                if ($LASTEXITCODE -ne 0) { throw "gh pr list exit $LASTEXITCODE" }
                $prs = @($list | ConvertFrom-Json | Where-Object { (ConvertTo-QlUtc $_.createdAt) -ge $now.AddHours(-24) })
                $n += $prs.Count; $w += @($prs | Where-Object { $_.body -match '(?im)^\W*Adversary pass' }).Count
            } catch { $errors += "pr list ${rp}: $($_.Exception.Message)" }
        }
        $adopt.prs = $n; $adopt.withAdversaryLine = $w
    }

    $corpusFile = Join-Path $Dir 'corpus.jsonl'
    $known = @{}
    if (Test-Path $corpusFile) { Get-Content $corpusFile | ForEach-Object { try { $known[($_ | ConvertFrom-Json).head] = 1 } catch { } } }
    $newCorpus = 0
    foreach ($b in ($passes | Where-Object blocked | Sort-Object utc)) {
        if ($known.ContainsKey($b.head)) { continue }
        $prior = @(); if ($cache[$b.head]) { $prior = @($cache[$b.head].rows | Where-Object { $_.state -eq 'success' -and (ConvertTo-QlUtc $_.firstUtc) -lt $b.utc } | ForEach-Object context) }
        ([ordered]@{ addedUtc = $now.ToString('o'); cosUtc = $b.utc.ToString('o'); repo = $b.repo; pr = $b.pr; head = $b.head; blocker = $b.blocker; successBeforeCos = $prior } | ConvertTo-Json -Compress) | Add-Content -LiteralPath $corpusFile -Encoding utf8
        $newCorpus++
    }

    $res = [ordered]@{ schema = 'fleet/quality-lift/v1'; project = $Proj; utc = $now.ToString('o'); since = $Since; windows = @($all, $d1); shiftLeft = $adopt; corpusNew = $newCorpus; errors = $errors }
    ($res | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $Dir 'quality-lift.json') -Encoding utf8
    ([ordered]@{ utc = $now.ToString('o'); project = $Proj; blockedRate24h = $d1.blockedRate; firstRound24h = $d1.firstRoundBlockedRate; noPriorKey24h = $d1.noPriorKey; trueMiss24h = $d1.trueMiss; rounds24h = $d1.roundsPerPr; adversaryLine = $adopt.withAdversaryLine; prs24h = $adopt.prs } | ConvertTo-Json -Compress) | Add-Content -LiteralPath (Join-Path $Dir 'history.jsonl') -Encoding utf8

    $pct = { param($x) if ($null -eq $x) { 'n/a' } else { "$([math]::Round(100 * [double]$x))%" } }
    $md = @()
    $md += "QUALITY-LIFT $Proj $($now.ToString('yyyy-MM-ddTHH:mm'))Z: CoS blocker rate 24h $(& $pct $d1.blockedRate) (since $($Since.Substring(0,10)) $(& $pct $all.blockedRate)); first-round blocked 24h $($d1.firstRoundBlocked)/$($d1.prs); CoS read first 24h $($d1.noPriorKey)/$($d1.blocked); true misses 24h $($d1.trueMiss); adversary-pass PRs 24h $($adopt.withAdversaryLine)/$($adopt.prs)$(if ($errors) { "; ERRORS $($errors.Count)" })"
    $md += ''
    $md += "| window | CoS passes | blocked | first-round blocked (PRs) | CoS read first | true misses | CoS rounds per PR |"
    $md += "|---|---|---|---|---|---|---|"
    foreach ($w in @($all, $d1)) { $md += "| $($w.window) | $($w.passes) | $($w.blocked) ($(& $pct $w.blockedRate)) | $($w.firstRoundBlocked)/$($w.prs) | $($w.noPriorKey) of $($w.blocked) | $($w.trueMiss) | $($w.roundsPerPr) (max $($w.roundsPerPrMax)) |" }
    $md += ''
    $md += "Statuses that existed before CoS on blocked heads:"
    foreach ($k in $all.keysBeforeCos.Keys) { $v = $all.keysBeforeCos[$k]; $md += "- $k : success $($v.successBeforeCos), failure $($v.failureBeforeCos)" }
    if ($errors) { $md += ''; $md += 'Errors:'; $errors | Select-Object -First 10 | ForEach-Object { $md += "- $_" } }
    $md | Set-Content -LiteralPath (Join-Path $Dir 'quality-lift.md') -Encoding utf8
    [pscustomobject]@{ project = $Proj; headline = $md[0]; all = $all; d1 = $d1; adopt = $adopt; errors = $errors.Count }
}

if (-not $Bus) { Write-Output "BUS_UNREADABLE (unset: pass -Bus or set FACTORY_BUS_CLONE)"; exit 3 }
if (-not (Test-Path -LiteralPath (Join-Path $Bus 'cos-feedback'))) { Write-Output "BUS_UNREADABLE $Bus"; exit 3 }
git -C $Bus rev-parse --verify -q HEAD 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Output "BUS_UNREADABLE $Bus (not a git repository with a commit)"; exit 3 }
if (-not $OutDir) { Write-Output "USAGE -OutDir is required (the folder the reports are written to)"; exit 4 }
try { $Since = (ConvertTo-QlUtc $Since).ToString('yyyy-MM-ddTHH:mm:ssZ') } catch { Write-Output "USAGE -Since '$Since' is not an ISO time"; exit 4 }
if (-not $NoFetch) { try { git -C $Bus pull --ff-only -q 2>&1 | Out-Null } catch { } }
if ($Project) {
    $r = Measure-QlProject $Project $OutDir
    $r.headline
    if ($r.errors) { exit 2 } else { exit 0 }
}
$projects = @(Get-ChildItem (Join-Path $Bus 'cos-feedback') -Directory | Where-Object { $_.Name -notmatch '^[_.]' } | ForEach-Object Name)
$results = @(foreach ($p in $projects) { Measure-QlProject $p (Join-Path $OutDir $p) })
$pct = { param($x) if ($null -eq $x) { 'n/a' } else { "$([math]::Round(100 * [double]$x))%" } }
$fl = @()
$fl += "# Fleet quality lift ($([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm'))Z, since $($Since.Substring(0,10)))"
$fl += ''
$fl += "| project | CoS passes | blocked | first-round blocked | CoS read first | true misses | rounds/PR | adversary-pass PRs 24h | source errors |"
$fl += "|---|---|---|---|---|---|---|---|---|"
$tp = 0; $tb = 0; $tn = 0; $tm = 0
foreach ($r in ($results | Where-Object { $_.all.passes -gt 0 } | Sort-Object { - $_.all.passes })) {
    $fl += "| $($r.project) | $($r.all.passes) | $($r.all.blocked) ($(& $pct $r.all.blockedRate)) | $($r.all.firstRoundBlocked)/$($r.all.prs) | $($r.all.noPriorKey) of $($r.all.blocked) | $($r.all.trueMiss) | $($r.all.roundsPerPr) | $($r.adopt.withAdversaryLine)/$($r.adopt.prs) | $($r.errors) |"
    $tp += $r.all.passes; $tb += $r.all.blocked; $tn += $r.all.noPriorKey; $tm += $r.all.trueMiss
}
$fl += "| **fleet** | $tp | $tb ($(if ($tp) { [math]::Round(100 * $tb / $tp) } else { 'n/a' })%) | | $tn of $tb | $tm | | | |"
$fl | Set-Content -LiteralPath (Join-Path $OutDir 'fleet.md') -Encoding utf8
($results | Select-Object project, all, d1, adopt, errors | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $OutDir 'fleet.json') -Encoding utf8
$fl
if (@($results | Where-Object { $_.errors }).Count) { exit 2 } else { exit 0 }
