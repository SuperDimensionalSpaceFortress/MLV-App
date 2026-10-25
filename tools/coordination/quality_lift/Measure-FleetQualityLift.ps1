# Measure-FleetQualityLift.ps1 -- how often the Chief of Staff (CoS / Dot) finds a demonstrated defect that a project's own
# factory did not catch first, per project and fleet-wide. Owner 2026-10-25: implement the quality-lift measurement fleet wide.
# Project-neutral: every project-specific value comes from the bus data itself, the bus clone (-Bus) and the output folder
# (-OutDir). Nothing here names a project, a host or a board path. See README.md beside this file.
#
# READ-ONLY on GitHub. On the bus it only reads, except the one `git pull --ff-only` before measuring, which -NoFetch skips.
# It reads:
#   - the bus history of cos-feedback/<project>/pr-*.md (one CoS pass per commit and file; the section header
#     "# <owner>/<repo> #<n> / <40-hex head>" names the repo, PR and head; a first "## Blockers" line B<n> = blocked)
#   - GitHub commit statuses on each blocked head (every page, every context, reported per context)
#   - PR bodies created in the last 24 h, for an "Adversary pass" line (shift-left adoption; bodies only, never comments)
# It writes ONLY under -OutDir:
#   single project (-Project x): <OutDir>\quality-lift.md|.json, history.jsonl, corpus.jsonl, status-cache.json
#   all projects (default):      <OutDir>\<project>\... per project, plus <OutDir>\fleet.md and fleet.json
# Definitions (lower is better on every rate). A "round" is one head CoS reviewed:
#   blockedRate        share of CoS-reviewed heads (one pass per repo, PR and head; a head re-reviewed clean-then-blocked counts
#                      at its first BLOCKING pass) whose first Blockers line starts B<n>
#   firstRoundBlocked  PRs whose first CoS-reviewed head (ordered by its first pass in the window) was blocked; clean-then-
#                      blocked on that head counts as blocked. Each window is cut from the raw passes before heads are folded.
#   noPriorKey         blocked heads whose statuses are known and none was created before the CoS pass (CoS read it first)
#   trueMiss           blocked heads where some context reported 'success' BEFORE the CoS pass (the factory approved a defect)
#   statusUnknown      blocked heads with no status evidence (-NoStatuses with no cache entry, or a failed fetch); they count
#                      in neither noPriorKey nor trueMiss
#   roundsPerPr        distinct heads CoS reviewed per PR
# Windows: 'since' is [-Since, now]; 'last-24h' is [now - 24 h, now], whatever -Since says. Later timestamps are ignored.
# Inputs:
#   -Bus        the fleet doctrine bus clone; default $env:FACTORY_BUS_CLONE. Required one way or the other.
#   -OutDir     where the outputs go. Required.
#   -Since      ISO time (an offset or Z); default 7 days ago. Normalised to UTC before use.
#   -NoStatuses read nothing from GitHub (no statuses, no PR bodies): the status cache already in -OutDir is still used, heads
#               it lacks count as statusUnknown, and the adoption counts read n/a. -NoFetch skips the bus pull.
#   -LockWaitSec how long a run waits for another run on the same project folder (default 60); past it, a source error.
# Status evidence: a cached status list proves what existed before a CoS pass only when it was fetched AFTER that pass
# (statuses are append-only with their creation time). An entry fetched earlier is refetched, and reads as unknown when it
# cannot be.
# Exit 0 = measured; 2 = measured with source errors (named in the report: a bus read, the bus pull, a GitHub read, a
# truncated listing, a malformed cache entry, a held lock); 3 = bus unreadable (unset, missing, no cos-feedback/, or not a
# git repository); 4 = usage error (no -OutDir, an unparseable -Since).
param(
    [string]$Bus = $env:FACTORY_BUS_CLONE,
    [string]$Project = "",
    [string]$Since = ([DateTime]::UtcNow.AddDays(-7).ToString('yyyy-MM-ddTHH:mm:ssZ')),
    [string]$OutDir = "",
    [switch]$NoStatuses,
    [switch]$NoFetch,
    [ValidateRange(0, 600)][int]$LockWaitSec = 60
)
$ErrorActionPreference = 'Stop'
$StatusPageMax = 20      # 100 statuses per page; a head with more than 2,000 is a named source error, never a silent cut
$PrListLimit = 200

# ISO text or DateTime -> UTC DateTime. A bare [DateTime] cast of ISO text yields LOCAL time, and ConvertFrom-Json already
# returns DateTime objects; both silently break before/after comparisons (measured 2026-10-25).
function ConvertTo-QlUtc([object]$s) {
    if ($s -is [DateTime]) { if ($s.Kind -eq [DateTimeKind]::Utc) { return $s } else { return $s.ToUniversalTime() } }
    ([DateTimeOffset]::Parse([string]$s, [Globalization.CultureInfo]::InvariantCulture)).UtcDateTime
}

function Get-QlKey($p) { "$($p.repo)@$($p.head)" }

# Every CoS pass (one per commit and file) since $From, unfolded. --since-as-filter, not --since: --since stops the walk at
# the first older commit, so a commit behind one with a skewed (older) date would be missed.
function Get-QlPasses([string]$Proj, [string]$From) {
    $out = @()
    $log = @(git -C $Bus log --since-as-filter=$From --format="%H|%cI" -- "cos-feedback/$Proj")
    if ($LASTEXITCODE -ne 0) { throw "git log of cos-feedback/$Proj failed (exit $LASTEXITCODE)" }
    foreach ($c in $log) {
        $h, $t = $c.Split('|')
        # --diff-filter=d: a commit that deletes a pr file has no blob to read at that commit.
        $names = @(git -C $Bus show --name-only --diff-filter=d --format= $h)
        if ($LASTEXITCODE -ne 0) { throw "git show --name-only $($h.Substring(0,12)) failed (exit $LASTEXITCODE)" }
        foreach ($f in ($names | Where-Object { $_ -like "cos-feedback/$Proj/pr-*.md" })) {
            $txt = (git -C $Bus show "${h}:$f" 2>$null) -join "`n"
            if ($LASTEXITCODE -ne 0) { throw "git show $($h.Substring(0,12)):$f failed (exit $LASTEXITCODE)" }
            $ms = [regex]::Matches($txt, '(?m)^# ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) #(\d+) / ([0-9a-f]{40})')
            if ($ms.Count -eq 0) { continue }
            $m = $ms[$ms.Count - 1]
            $sec = $txt.Substring($m.Index)
            # The heading line, not the phrase: a verdict that mentions "## Blockers" mid-line is not the section.
            $hm = [regex]::Match($sec, '(?m)^##[ \t]+Blockers[ \t]*$')
            $blk = ''
            if ($hm.Success) { $blk = ((($sec.Substring($hm.Index + $hm.Length) -split "`n") | Where-Object { $_.Trim() } | Select-Object -First 1) + '').Trim() }
            $out += [pscustomobject]@{
                utc = (ConvertTo-QlUtc $t); repo = $m.Groups[1].Value; pr = [int]$m.Groups[2].Value
                head = $m.Groups[3].Value; blocked = ($blk -match '^B\d'); blocker = $(if ($blk.Length -gt 160) { $blk.Substring(0, 160) } else { $blk })
            }
        }
    }
    $out
}

# One record per repo, PR and head, from passes ALREADY cut to a window: the first blocking pass if any, else the first pass.
# firstSeen (the head's first pass in the window) orders the heads of a PR; utc (the blocking pass) is the CoS time that
# statuses are compared against.
function Group-QlHeads([object[]]$Raw) {
    @(@($Raw) | Where-Object { $null -ne $_ } | Sort-Object utc | Group-Object { "$($_.repo)|$($_.pr)|$($_.head)" } | ForEach-Object {
        $bl = @($_.Group | Where-Object blocked)
        $rep = $(if ($bl.Count) { $bl[0] } else { $_.Group[0] })
        [pscustomobject]@{ utc = $rep.utc; firstSeen = $_.Group[0].utc; repo = $rep.repo; pr = $rep.pr; head = $rep.head; blocked = $rep.blocked; blocker = $rep.blocker }
    })
}

# Every repository the project's CoS files at the bus HEAD name, so adoption is read even when no pass is in the window.
function Get-QlRepos([string]$Proj) {
    $lines = @(git -C $Bus grep -h -I -E '^# [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+ #[0-9]+ / [0-9a-f]{40}' HEAD -- "cos-feedback/$Proj/pr-*.md" 2>$null)
    if ($LASTEXITCODE -gt 1) { throw "git grep of cos-feedback/$Proj failed (exit $LASTEXITCODE)" }
    @($lines | ForEach-Object { if ($_ -match '^# ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) #') { $Matches[1] } })
}

# A cache entry is evidence only when it has a parseable fetched time and a rows array whose every row names a context with
# parseable times. Anything else is dropped (the head reads as unknown) and named as a source error.
function Test-QlCacheEntry($e) {
    try {
        if ($null -eq $e -or $null -eq $e.fetched) { return $false }
        $null = ConvertTo-QlUtc $e.fetched
        if ($e.rows -isnot [array]) { return $false }
        foreach ($r in $e.rows) {
            if ($null -eq $r -or $r -is [string] -or $r.context -isnot [string] -or -not $r.context) { return $false }
            $null = ConvertTo-QlUtc $r.firstUtc
            foreach ($v in @($r.successUtc, $r.failureUtc)) { if ($null -ne $v -and "$v" -ne '') { $null = ConvertTo-QlUtc $v } }
        }
        $true
    } catch { $false }
}

# Every status of one head, all pages, folded per context: the first status of any state, the first 'success', the first
# 'failure'/'error', and the latest state. "Before CoS" is decided from those times, never from the latest state.
function Get-QlStatusRows([string]$Repo, [string]$Head) {
    $all = @()
    for ($page = 1; ; $page++) {
        # Plain -F arguments only: where gh resolves through a .cmd shim, '&', '|' and '>' in an argument are shell syntax.
        $raw = gh api -X GET "repos/$Repo/commits/$Head/statuses" -F per_page=100 -F page=$page 2>$null
        if ($LASTEXITCODE -ne 0) { throw "gh api exit $LASTEXITCODE" }
        $rows = @((($raw | Out-String) | ConvertFrom-Json) | Where-Object { $null -ne $_ })
        if ($rows.Count -and $page -gt $StatusPageMax) { throw "more than $($StatusPageMax * 100) statuses; not measured" }
        $all += $rows
        if ($rows.Count -lt 100) { break }
    }
    @($all | Group-Object context | ForEach-Object {
        $g = @($_.Group | Sort-Object { ConvertTo-QlUtc $_.created_at })
        $ok = @($g | Where-Object { $_.state -eq 'success' })
        $bad = @($g | Where-Object { $_.state -in 'failure', 'error' })
        [pscustomobject]@{
            context = $_.Name; firstUtc = (ConvertTo-QlUtc $g[0].created_at).ToString('o'); state = $g[-1].state
            successUtc = $(if ($ok.Count) { (ConvertTo-QlUtc $ok[0].created_at).ToString('o') } else { $null })
            failureUtc = $(if ($bad.Count) { (ConvertTo-QlUtc $bad[0].created_at).ToString('o') } else { $null })
        }
    })
}

function Test-QlBefore([object]$When, [DateTime]$Cos) { ($null -ne $When) -and ("$When" -ne '') -and ((ConvertTo-QlUtc $When) -lt $Cos) }

# The cache entry that proves the statuses of one pass, or $null: it must have been fetched at or after the pass.
function Get-QlEvidence([hashtable]$Cache, $p) {
    $e = $Cache[(Get-QlKey $p)]
    if ($e -and (ConvertTo-QlUtc $e.fetched) -ge $p.utc) { $e } else { $null }
}

function Get-QlWindow([object[]]$P, [string]$Name, [hashtable]$Cache) {
    # An empty window can arrive as $null, and @($null) is one element: drop nulls so it counts zero passes.
    $P = @($P | Where-Object { $null -ne $_ })
    $blocked = @($P | Where-Object blocked)
    $firstPerPr = @($P | Group-Object { "$($_.repo)#$($_.pr)" } | ForEach-Object { $_.Group | Sort-Object firstSeen | Select-Object -First 1 })
    $noKey = 0; $trueMiss = 0; $unknown = 0; $keyStats = [ordered]@{}
    foreach ($b in $blocked) {
        $e = Get-QlEvidence $Cache $b
        if (-not $e) { $unknown++; continue }
        $rows = @($e.rows | Where-Object { $null -ne $_ })
        $before = @($rows | Where-Object { Test-QlBefore $_.firstUtc $b.utc })
        if ($before.Count -eq 0) { $noKey++ }
        if (@($rows | Where-Object { Test-QlBefore $_.successUtc $b.utc }).Count) { $trueMiss++ }
        foreach ($r in $before) {
            if (-not $keyStats.Contains($r.context)) { $keyStats[$r.context] = [ordered]@{ successBeforeCos = 0; failureBeforeCos = 0 } }
            if (Test-QlBefore $r.successUtc $b.utc) { $keyStats[$r.context].successBeforeCos++ }
            if (Test-QlBefore $r.failureUtc $b.utc) { $keyStats[$r.context].failureBeforeCos++ }
        }
    }
    $rounds = @($P | Group-Object { "$($_.repo)#$($_.pr)" } | ForEach-Object { $_.Count })
    [ordered]@{
        window = $Name; passes = $P.Count; blocked = $blocked.Count
        blockedRate = $(if ($P.Count) { [math]::Round($blocked.Count / $P.Count, 3) } else { $null })
        prs = $firstPerPr.Count; firstRoundBlocked = @($firstPerPr | Where-Object blocked).Count
        firstRoundBlockedRate = $(if ($firstPerPr.Count) { [math]::Round(@($firstPerPr | Where-Object blocked).Count / $firstPerPr.Count, 3) } else { $null })
        noPriorKey = $noKey; trueMiss = $trueMiss; statusUnknown = $unknown
        roundsPerPr = $(if ($rounds.Count) { [math]::Round(($rounds | Measure-Object -Average).Average, 2) } else { $null })
        roundsPerPrMax = $(if ($rounds.Count) { ($rounds | Measure-Object -Maximum).Maximum } else { $null })
        keysBeforeCos = $keyStats
    }
}

function Format-QlFirst($w) { "$($w.noPriorKey) of $($w.blocked)$(if ($w.statusUnknown) { " ($($w.statusUnknown) unknown)" })" }

# One run per project folder at a time: the corpus check-then-append and the cache rewrite are not safe to interleave. A run
# that cannot take the lock within -LockWaitSec writes nothing and reports a source error.
function Measure-QlProject([string]$Proj, [string]$Dir) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $lockPath = Join-Path $Dir '.quality-lift.lock'
    $lock = $null
    $deadline = [DateTime]::UtcNow.AddSeconds($LockWaitSec)
    while ($true) {
        try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None'); break }
        catch { if ([DateTime]::UtcNow -ge $deadline) { break }; Start-Sleep -Milliseconds 250 }
    }
    if (-not $lock) {
        $empty = Get-QlWindow @() 'since' @{}
        return [pscustomobject]@{ project = $Proj; headline = "QUALITY-LIFT $Proj NOT MEASURED: another run holds the lock $lockPath"; all = $empty; d1 = $empty; adopt = [ordered]@{ prs = $null; withAdversaryLine = $null }; errors = 1 }
    }
    try { Measure-QlProjectLocked $Proj $Dir } finally { $lock.Dispose() }
}

function Measure-QlProjectLocked([string]$Proj, [string]$Dir) {
    $now = [DateTime]::UtcNow
    $d1Start = $now.AddHours(-24)
    $errors = @($script:SharedErrors)
    $raw = @()
    $from = $(if ($script:SinceUtc -lt $d1Start) { $script:SinceUtc } else { $d1Start })
    try { $raw = @(Get-QlPasses $Proj $from.ToString('yyyy-MM-ddTHH:mm:ssZ')) } catch { $errors += "bus ${Proj}: $($_.Exception.Message)" }
    # Cut to the window FIRST, then fold per head: a blocking pass outside the window must not hide a clean pass inside it.
    $raw = @($raw | Where-Object { $_.utc -le $now })
    $passes = @(Group-QlHeads @($raw | Where-Object { $_.utc -ge $script:SinceUtc }))
    $passes24 = @(Group-QlHeads @($raw | Where-Object { $_.utc -ge $d1Start }))
    $heads = @(Group-QlHeads $raw)

    $cacheFile = Join-Path $Dir 'status-cache.json'
    $cache = @{}
    if (Test-Path -LiteralPath $cacheFile) {
        try {
            (Get-Content -LiteralPath $cacheFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object {
                if (Test-QlCacheEntry $_.Value) { $cache[$_.Name] = $_.Value } else { $errors += "status-cache entry $($_.Name) is malformed; dropped (the head reads as unknown until refetched)" }
            }
        } catch { $cache = @{}; $errors += "status-cache.json does not parse; ignored" }
    }
    if (-not $NoStatuses) {
        # Refetch a head unless its entry was fetched after its LATEST blocking pass: each window may compare against a
        # different blocking pass of the same head, and only an entry fetched after a pass proves that pass's statuses.
        $latest = @{}
        foreach ($p in ($raw | Where-Object blocked)) { $k = Get-QlKey $p; if (-not $latest.ContainsKey($k) -or $p.utc -gt $latest[$k].utc) { $latest[$k] = $p } }
        foreach ($k in @($latest.Keys)) {
            $p = $latest[$k]
            try {
                if (Get-QlEvidence $cache $p) { continue }
                $cache[$k] = [pscustomobject]@{ fetched = $now.ToString('o'); rows = @(Get-QlStatusRows $p.repo $p.head) }
            } catch { $errors += "statuses $($p.repo)@$($p.head.Substring(0,8)): $($_.Exception.Message)" }
        }
        ($cache | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $cacheFile -Encoding utf8
    }

    $all = Get-QlWindow $passes 'since' $cache
    $d1 = Get-QlWindow $passes24 'last-24h' $cache

    $adopt = [ordered]@{ prs = $null; withAdversaryLine = $null }
    $repos = @($heads | ForEach-Object repo)
    if (-not $NoStatuses) { try { $repos += @(Get-QlRepos $Proj) } catch { $errors += "repos ${Proj}: $($_.Exception.Message)" } }
    $repos = @($repos | Sort-Object -Unique)
    if ($repos.Count -and -not $NoStatuses) {
        $n = 0; $w = 0
        foreach ($rp in $repos) {
            try {
                # No --search: where gh resolves through a .cmd shim, '>' in "created:>=" becomes a redirect.
                # One past the limit, so exactly $PrListLimit recent PRs still reads as complete.
                $list = gh pr list --repo $rp --state all --limit ($PrListLimit + 1) --json number,body,createdAt 2>$null
                if ($LASTEXITCODE -ne 0) { throw "gh pr list exit $LASTEXITCODE" }
                $listed = @((($list | Out-String) | ConvertFrom-Json) | Where-Object { $null -ne $_ })
                if ($listed.Count -gt $PrListLimit -and @($listed | Where-Object { (ConvertTo-QlUtc $_.createdAt) -lt $d1Start }).Count -eq 0) {
                    throw "more than $PrListLimit PRs were created in the last 24 h; the listing is truncated"
                }
                $prs = @($listed | Where-Object { $c = ConvertTo-QlUtc $_.createdAt; $c -ge $d1Start -and $c -le $now })
                $n += $prs.Count; $w += @($prs | Where-Object { $_.body -match '(?im)^\W*Adversary pass' }).Count
            } catch { $errors += "pr list ${rp}: $($_.Exception.Message)" }
        }
        $adopt.prs = $n; $adopt.withAdversaryLine = $w
    }

    # The corpus keeps one row per blocked repo@head whose statuses are KNOWN; an unknown head is written on a later run.
    $corpusFile = Join-Path $Dir 'corpus.jsonl'
    $known = @{}
    if (Test-Path -LiteralPath $corpusFile) { Get-Content -LiteralPath $corpusFile | ForEach-Object { try { $known[(Get-QlKey ($_ | ConvertFrom-Json))] = 1 } catch { } } }
    $newCorpus = 0
    foreach ($b in ($heads | Where-Object blocked | Sort-Object utc)) {
        $k = Get-QlKey $b
        $e = Get-QlEvidence $cache $b
        if ($known.ContainsKey($k) -or -not $e) { continue }
        $prior = @($e.rows | Where-Object { $null -ne $_ -and (Test-QlBefore $_.successUtc $b.utc) } | ForEach-Object context)
        ([ordered]@{ addedUtc = $now.ToString('o'); cosUtc = $b.utc.ToString('o'); repo = $b.repo; pr = $b.pr; head = $b.head; blocker = $b.blocker; successBeforeCos = $prior } | ConvertTo-Json -Compress) | Add-Content -LiteralPath $corpusFile -Encoding utf8
        $known[$k] = 1
        $newCorpus++
    }

    $res = [ordered]@{ schema = 'fleet/quality-lift/v1'; project = $Proj; utc = $now.ToString('o'); since = $Since; windows = @($all, $d1); shiftLeft = $adopt; corpusNew = $newCorpus; errors = $errors }
    ($res | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $Dir 'quality-lift.json') -Encoding utf8
    ([ordered]@{ utc = $now.ToString('o'); project = $Proj; blockedRate24h = $d1.blockedRate; firstRound24h = $d1.firstRoundBlockedRate; noPriorKey24h = $d1.noPriorKey; trueMiss24h = $d1.trueMiss; statusUnknown24h = $d1.statusUnknown; rounds24h = $d1.roundsPerPr; adversaryLine = $adopt.withAdversaryLine; prs24h = $adopt.prs } | ConvertTo-Json -Compress) | Add-Content -LiteralPath (Join-Path $Dir 'history.jsonl') -Encoding utf8

    $pct = { param($x) if ($null -eq $x) { 'n/a' } else { "$([math]::Round(100 * [double]$x))%" } }
    $md = @()
    $md += "QUALITY-LIFT $Proj $($now.ToString('yyyy-MM-ddTHH:mm'))Z: CoS blocker rate 24h $(& $pct $d1.blockedRate) (since $($Since.Substring(0,10)) $(& $pct $all.blockedRate)); first-round blocked 24h $($d1.firstRoundBlocked)/$($d1.prs); CoS read first 24h $(Format-QlFirst $d1); true misses 24h $($d1.trueMiss); adversary-pass PRs 24h $($adopt.withAdversaryLine)/$($adopt.prs)$(if ($errors) { "; ERRORS $($errors.Count)" })"
    $md += ''
    $md += "| window | CoS passes | blocked | first-round blocked (PRs) | CoS read first | true misses | CoS rounds per PR |"
    $md += "|---|---|---|---|---|---|---|"
    foreach ($w in @($all, $d1)) { $md += "| $($w.window) | $($w.passes) | $($w.blocked) ($(& $pct $w.blockedRate)) | $($w.firstRoundBlocked)/$($w.prs) | $(Format-QlFirst $w) | $($w.trueMiss) | $($w.roundsPerPr) (max $($w.roundsPerPrMax)) |" }
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
try { $SinceUtc = ConvertTo-QlUtc $Since; $Since = $SinceUtc.ToString('yyyy-MM-ddTHH:mm:ssZ') } catch { Write-Output "USAGE -Since '$Since' is not an ISO time"; exit 4 }
$SharedErrors = @()
if (-not $NoFetch) {
    git -C $Bus pull --ff-only -q 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { $SharedErrors += "bus pull --ff-only failed (exit $LASTEXITCODE); measured the clone as it is (pass -NoFetch to skip the pull)" }
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if ($Project) {
    $r = Measure-QlProject $Project $OutDir
    $r.headline
    if ($r.errors) { exit 2 } else { exit 0 }
}
# Projects: every folder under cos-feedback/ now, plus every folder a commit in the window touched (a project whose files
# were since deleted still had CoS passes in the window).
$fromUtc = $(if ($SinceUtc -lt [DateTime]::UtcNow.AddHours(-24)) { $SinceUtc } else { [DateTime]::UtcNow.AddHours(-24) })
$touched = @(git -C $Bus log --since-as-filter=$($fromUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')) --name-only --format= -- cos-feedback/)
if ($LASTEXITCODE -ne 0) { $SharedErrors += "git log of cos-feedback/ failed (exit $LASTEXITCODE); projects listed from the checkout only" }
$projects = @(@(Get-ChildItem -LiteralPath (Join-Path $Bus 'cos-feedback') -Directory | ForEach-Object Name) +
    @($touched | ForEach-Object { if ($_ -match '^cos-feedback/([^/]+)/') { $Matches[1] } }) |
    Where-Object { $_ -and $_ -notmatch '^[_.]' } | Sort-Object -Unique)
$results = @(foreach ($p in $projects) { Measure-QlProject $p (Join-Path $OutDir $p) })
$pct = { param($x) if ($null -eq $x) { 'n/a' } else { "$([math]::Round(100 * [double]$x))%" } }
$fl = @()
$fl += "# Fleet quality lift ($([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm'))Z, since $($Since.Substring(0,10)))"
$fl += ''
$fl += "| project | CoS passes | blocked | first-round blocked | CoS read first | true misses | rounds/PR | adversary-pass PRs 24h | source errors |"
$fl += "|---|---|---|---|---|---|---|---|---|"
$tp = 0; $tb = 0; $tn = 0; $tm = 0; $tu = 0
foreach ($r in ($results | Where-Object { $_.all.passes -gt 0 } | Sort-Object { - $_.all.passes })) {
    $fl += "| $($r.project) | $($r.all.passes) | $($r.all.blocked) ($(& $pct $r.all.blockedRate)) | $($r.all.firstRoundBlocked)/$($r.all.prs) | $(Format-QlFirst $r.all) | $($r.all.trueMiss) | $($r.all.roundsPerPr) | $($r.adopt.withAdversaryLine)/$($r.adopt.prs) | $($r.errors) |"
    $tp += $r.all.passes; $tb += $r.all.blocked; $tn += $r.all.noPriorKey; $tm += $r.all.trueMiss; $tu += $r.all.statusUnknown
}
$fl += "| **fleet** | $tp | $tb ($(if ($tp) { [math]::Round(100 * $tb / $tp) } else { 'n/a' })%) | | $(Format-QlFirst ([pscustomobject]@{ noPriorKey = $tn; blocked = $tb; statusUnknown = $tu })) | $tm | | | |"
if ($SharedErrors -and -not $results.Count) { $fl += ''; $fl += 'Errors:'; $SharedErrors | ForEach-Object { $fl += "- $_" } }
$fl | Set-Content -LiteralPath (Join-Path $OutDir 'fleet.md') -Encoding utf8
(ConvertTo-Json -InputObject @($results | Select-Object project, all, d1, adopt, errors) -Depth 8) | Set-Content -LiteralPath (Join-Path $OutDir 'fleet.json') -Encoding utf8
$fl
if (@($results | Where-Object { $_.errors }).Count -or $SharedErrors) { exit 2 } else { exit 0 }
