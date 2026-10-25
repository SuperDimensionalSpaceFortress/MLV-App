# Measure-FleetQualityLift.ps1 -- how often the Chief of Staff (CoS / Dot) finds a demonstrated defect that a project's own
# factory did not catch first, per project and fleet-wide. Owner 2026-10-25: implement the quality-lift measurement fleet wide.
# Project-neutral: every project-specific value comes from the bus data itself, the bus clone (-Bus), the output folder
# (-OutDir) and the review-sources file (-ReviewSources). Nothing here names a project, a host or a board path. See
# README.md beside this file.
#
# READ-ONLY on GitHub. On the bus it only reads, except the one `git pull --ff-only` before measuring, which -NoFetch skips.
# It reads:
#   - the bus history of cos-feedback/<project>/pr-*.md (one CoS pass per commit and file; the section header
#     "# <owner>/<repo> #<n> / <40-hex head>" names the repo, PR and head; a first "## Blockers" line B<n> = blocked)
#   - GitHub commit statuses on each blocked head (every page, every context, reported per context)
#   - for a project that declares native reviews: the PR's reviews (on that head's commit_id) and review comments
#     (written on that head: original_commit_id), every page, folded per author
#   - PR bodies created in the last 24 h, for an "Adversary pass" line (shift-left adoption; bodies only, never comments)
# It writes ONLY under -OutDir:
#   single project (-Project x): <OutDir>\quality-lift.md|.json, history.jsonl, corpus.jsonl, status-cache.json
#   all projects (default):      <OutDir>\<project>\... per project, plus <OutDir>\fleet.md and fleet.json
# Review evidence is DECLARED, never inferred. A status context is not a review because it said 'success': CI says that too.
# -ReviewSources names, per project, which evidence is the factory's review: status contexts (wildcards, e.g. review/*)
# and/or native PR reviews (optionally only by named authors). A native review counts as an approval only when its state is
# APPROVED; every other review state and every review comment is review evidence, never an approval.
# Definitions (lower is better on every rate). A "round" is one head CoS reviewed:
#   blockedRate        share of CoS-reviewed heads (one pass per repo, PR and head; a head re-reviewed clean-then-blocked counts
#                      at its first BLOCKING pass) whose first Blockers line starts B<n>
#   firstRoundBlocked  PRs whose first CoS-reviewed head (ordered by its first pass in the window) was blocked; clean-then-
#                      blocked on that head counts as blocked. Each window is cut from the raw passes before heads are folded.
#   noPriorKey         blocked heads whose evidence is known and that carry no DECLARED review evidence from before the CoS
#                      pass (CoS read it first). 'unknown' for a project that declares no review evidence.
#   trueMiss           blocked heads with a DECLARED approval (a declared status context's 'success', or an APPROVED native
#                      review) from before the CoS pass (the factory approved a defect). 'unknown' when nothing is declared.
#   statusBeforeCos    the raw status proxy, every context alike: noPriorStatus (no status of any context before CoS) and
#                      successBeforeCos (some context said 'success' before CoS). It proves neither a review nor its absence.
#   statusUnknown      blocked heads with no evidence (-NoStatuses with no cache entry, or a failed fetch); they count in
#                      neither noPriorKey nor trueMiss nor the proxy
#   contextsBeforeCos  per status context, and nativeBeforeCos per native reviewer, what existed before CoS on blocked heads,
#                      each marked review=true when it is declared review evidence (kept apart: a status may use any text)
#   roundsPerPr        distinct heads CoS reviewed per PR
# Names are literal: a project folder is matched as text, never as a pattern. Folders starting with _ or . are bus plumbing
# and are named, not measured; a project folder named fleet.md or fleet.json is a named error in fleet mode.
# Windows: 'since' is [-Since, now]; 'last-24h' is [now - 24 h, now], whatever -Since says. Later timestamps are ignored.
# Inputs:
#   -Bus        the fleet doctrine bus clone; default $env:FACTORY_BUS_CLONE. Required one way or the other.
#   -OutDir     where the outputs go. Required.
#   -Since      ISO time (an offset or Z); default 7 days ago. Normalised to UTC before use.
#   -NoStatuses read nothing from GitHub (no statuses, no PR bodies): the status cache already in -OutDir is still used, heads
#               it lacks count as statusUnknown, and the adoption counts read n/a. -NoFetch skips the bus pull.
#   -LockWaitSec how long a run waits for another run on the same project folder (default 60); past it, a source error.
#   -ReviewSources a JSON file (default review-sources.json beside this script):
#               {"schema": "fleet/quality-lift/review-sources/v1",
#                "projects": {"<project>": {"statusContexts": ["review/*"], "nativeReviews": false, "reviewAuthors": []}}}
#               A project must declare at least one source. A project it does not name reads 'unknown' for noPriorKey and
#               trueMiss. A missing or malformed file is a usage error.
# Evidence: a cached entry proves what existed before a CoS pass only when it was fetched AFTER that pass (statuses are
# append-only with their creation time), and, for a project that declares native reviews, only when it holds them. An
# entry that does not is refetched, and reads as unknown when it cannot be.
# Exit 0 = measured; 2 = measured with source errors (named in the report: a bus read, the bus pull, a GitHub read, a
# truncated listing, a malformed cache entry, a held lock); 3 = bus unreadable (unset, missing, no cos-feedback/, or not a
# git repository); 4 = usage error (no -OutDir, an unparseable -Since, a missing or malformed -ReviewSources).
param(
    [string]$Bus = $env:FACTORY_BUS_CLONE,
    [string]$Project = "",
    [string]$Since = ([DateTime]::UtcNow.AddDays(-7).ToString('yyyy-MM-ddTHH:mm:ssZ')),
    [string]$OutDir = "",
    [string]$ReviewSources = (Join-Path $PSScriptRoot 'review-sources.json'),
    [switch]$NoStatuses,
    [switch]$NoFetch,
    [ValidateRange(0, 600)][int]$LockWaitSec = 60
)
$ErrorActionPreference = 'Stop'
$StatusPageMax = 20      # 100 rows per page; a listing of more than 2,000 is a named source error, never a silent cut
$PrListLimit = 200
# git prints raw UTF-8 paths under core.quotepath=false; read native output as UTF-8, not as the console code page.
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

# ISO text or DateTime -> UTC DateTime. A bare [DateTime] cast of ISO text yields LOCAL time, and ConvertFrom-Json already
# returns DateTime objects; both silently break before/after comparisons (measured 2026-10-25).
function ConvertTo-QlUtc([object]$s) {
    if ($s -is [DateTime]) { if ($s.Kind -eq [DateTimeKind]::Utc) { return $s } else { return $s.ToUniversalTime() } }
    ([DateTimeOffset]::Parse([string]$s, [Globalization.CultureInfo]::InvariantCulture)).UtcDateTime
}

function Get-QlKey($p) { "$($p.repo)@$($p.head)" }

# Every CoS pass (one per commit and file) since $From, unfolded. --since-as-filter, not --since: --since stops the walk at
# the first older commit, so a commit behind one with a skewed (older) date would be missed. The project name is literal
# everywhere (a git :(literal) pathspec, an escaped wildcard), so 'demo[1]' is not a pattern that matches 'demo1'. seq is the
# commit order (topo order, oldest = 1): it breaks ties between passes committed in the same second.
function Get-QlPasses([string]$Proj, [string]$From) {
    $out = @()
    $log = @(git -C $Bus log --topo-order --since-as-filter=$From --format="%H|%cI" -- ":(literal)cos-feedback/$Proj")
    if ($LASTEXITCODE -ne 0) { throw "git log of cos-feedback/$Proj failed (exit $LASTEXITCODE)" }
    $like = "cos-feedback/$([WildcardPattern]::Escape($Proj))/pr-*.md"
    for ($i = 0; $i -lt $log.Count; $i++) {
        $h, $t = $log[$i].Split('|')
        $seq = $log.Count - $i
        # --diff-filter=d: a commit that deletes a pr file has no blob to read at that commit. quotepath off: a non-ASCII
        # path is printed raw, so it matches the filter below and reaches the blob read unquoted.
        $names = @(git -C $Bus -c core.quotepath=false show --name-only --diff-filter=d --format= $h)
        if ($LASTEXITCODE -ne 0) { throw "listing the files of $($h.Substring(0,12)) failed (exit $LASTEXITCODE)" }
        foreach ($f in ($names | Where-Object { $_ -like $like })) {
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
                utc = (ConvertTo-QlUtc $t); seq = $seq; repo = $m.Groups[1].Value; pr = [int]$m.Groups[2].Value
                head = $m.Groups[3].Value; blocked = ($blk -match '^B\d'); blocker = $(if ($blk.Length -gt 160) { $blk.Substring(0, 160) } else { $blk })
            }
        }
    }
    $out
}

# One record per repo, PR and head, from passes ALREADY cut to a window: the first blocking pass if any, else the first pass.
# firstSeen/firstSeq (the head's first pass in the window) order the heads of a PR; utc (the blocking pass) is the CoS time
# that statuses are compared against. Passes in the same second keep their commit order (seq).
function Group-QlHeads([object[]]$Raw) {
    @(@($Raw) | Where-Object { $null -ne $_ } | Sort-Object utc, seq | Group-Object { "$($_.repo)|$($_.pr)|$($_.head)" } | ForEach-Object {
        $bl = @($_.Group | Where-Object blocked)
        $rep = $(if ($bl.Count) { $bl[0] } else { $_.Group[0] })
        [pscustomobject]@{ utc = $rep.utc; firstSeen = $_.Group[0].utc; firstSeq = $_.Group[0].seq; repo = $rep.repo; pr = $rep.pr; head = $rep.head; blocked = $rep.blocked; blocker = $rep.blocker }
    })
}

# Every repository the project's CoS files at the bus HEAD name, so adoption is read even when no pass is in the window.
# --null prints each path verbatim before a NUL, so the pr-*.md filter runs on the literal project folder.
function Get-QlRepos([string]$Proj) {
    $lines = @(git -C $Bus grep --null -I -E '^# [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+ #[0-9]+ / [0-9a-f]{40}' HEAD -- ":(literal)cos-feedback/$Proj/" 2>$null)
    if ($LASTEXITCODE -gt 1) { throw "git grep of cos-feedback/$Proj failed (exit $LASTEXITCODE)" }
    $like = "HEAD:cos-feedback/$([WildcardPattern]::Escape($Proj))/pr-*.md"
    @($lines | ForEach-Object {
        $path, $text = $_.Split([char]0, 2)
        if ($path -like $like -and $text -match '^# ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) #') { $Matches[1] }
    })
}

# The -ReviewSources file -> @{ project = declaration }. Throws on anything malformed: a guessed declaration would turn into
# exactly the review provenance this file exists to keep honest.
function Read-QlReviewSources([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "not found: '$Path'" }
    $j = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($null -eq $j -or $null -eq $j.projects -or $j.projects -isnot [Management.Automation.PSCustomObject]) { throw "no 'projects' object" }
    $map = @{}
    foreach ($p in $j.projects.PSObject.Properties) {
        $v = $p.Value
        if ($v -isnot [Management.Automation.PSCustomObject]) { throw "$($p.Name): not an object" }
        foreach ($list in 'statusContexts', 'reviewAuthors') {
            $x = $v.$list
            if ($null -ne $x -and ($x -isnot [array] -or @($x | Where-Object { $_ -isnot [string] -or -not $_ }).Count)) { throw "$($p.Name): $list must be an array of non-empty strings" }
        }
        foreach ($pat in @($v.statusContexts)) {
            # A wildcard is parsed lazily, on its first match: parse it here, or a bad one would end the run mid-measurement.
            try { $null = [WildcardPattern]::new($pat).IsMatch('') } catch { throw "$($p.Name): statusContexts '$pat' is not a valid wildcard" }
        }
        if ($null -ne $v.nativeReviews -and $v.nativeReviews -isnot [bool]) { throw "$($p.Name): nativeReviews must be true or false" }
        $d = [pscustomobject]@{ statusContexts = @($v.statusContexts | Where-Object { $_ }); nativeReviews = ($v.nativeReviews -eq $true); reviewAuthors = @($v.reviewAuthors | Where-Object { $_ }) }
        if (-not $d.statusContexts.Count -and -not $d.nativeReviews) { throw "$($p.Name): declares no review evidence (statusContexts or nativeReviews)" }
        $map[$p.Name] = $d
    }
    $map
}

function Format-QlSource($Decl) {
    if (-not $Decl) { return 'undeclared' }
    $parts = @()
    if ($Decl.statusContexts.Count) { $parts += "statuses $($Decl.statusContexts -join ', ')" }
    if ($Decl.nativeReviews) { $parts += "native reviews$(if ($Decl.reviewAuthors.Count) { " by $($Decl.reviewAuthors -join ', ')" })" }
    $parts -join '; '
}

# A cache entry is evidence only when it has a parseable fetched time and a rows array whose every row names a context with
# parseable times; a native row (source review or review-comment) also names its author, a review comment never carries a
# success, and an entry that holds native reviews lists the PR numbers it read them from. Anything else is dropped (the head
# reads as unknown) and named as a source error.
function Test-QlCacheEntry($e) {
    try {
        if ($null -eq $e -or $null -eq $e.fetched) { return $false }
        $null = ConvertTo-QlUtc $e.fetched
        if ($e.rows -isnot [array]) { return $false }
        if ($e.native -eq $true -and ($e.prs -isnot [array] -or @($e.prs | Where-Object { $_ -isnot [long] -and $_ -isnot [int] }).Count)) { return $false }
        foreach ($r in $e.rows) {
            if ($null -eq $r -or $r -is [string] -or $r.context -isnot [string] -or -not $r.context) { return $false }
            if ($null -ne $r.source -and $r.source -notin 'status', 'review', 'review-comment') { return $false }
            if ($r.source -in 'review', 'review-comment' -and ($r.login -isnot [string] -or -not $r.login)) { return $false }
            if ($r.source -eq 'review-comment' -and $null -ne $r.successUtc -and "$($r.successUtc)" -ne '') { return $false }
            $null = ConvertTo-QlUtc $r.firstUtc
            foreach ($v in @($r.successUtc, $r.failureUtc)) { if ($null -ne $v -and "$v" -ne '') { $null = ConvertTo-QlUtc $v } }
        }
        $true
    } catch { $false }
}

# Every row of one GitHub listing, all pages.
function Get-QlGhPages([string]$Path) {
    $all = @()
    for ($page = 1; ; $page++) {
        # Plain -F arguments only: where gh resolves through a .cmd shim, '&', '|' and '>' in an argument are shell syntax.
        $raw = gh api -X GET $Path -F per_page=100 -F page=$page 2>$null
        if ($LASTEXITCODE -ne 0) { throw "gh api exit $LASTEXITCODE" }
        $rows = @((($raw | Out-String) | ConvertFrom-Json) | Where-Object { $null -ne $_ })
        if ($rows.Count -and $page -gt $StatusPageMax) { throw "more than $($StatusPageMax * 100) rows at $Path; not measured" }
        $all += $rows
        if ($rows.Count -lt 100) { break }
    }
    $all
}

# Rows of {when, ord, state} folded per context: the first row of any state, the first $Ok one, the first $Bad one, and the
# latest state. "Before CoS" is decided from those times, never from the latest state. ord is the row's place in GitHub's
# listing, oldest = lowest, so rows created in the same second keep their order.
function Group-QlRows([object[]]$Rows, [string]$Context, [string]$Source, [string]$Login, [string[]]$Ok, [string[]]$Bad) {
    $g = @($Rows | Sort-Object when, ord)
    $okRows = @($g | Where-Object { $_.state -in $Ok })
    $badRows = @($g | Where-Object { $_.state -in $Bad })
    $row = [ordered]@{
        context = $Context; source = $Source; firstUtc = $g[0].when.ToString('o'); state = $g[-1].state
        successUtc = $(if ($okRows.Count) { $okRows[0].when.ToString('o') } else { $null })
        failureUtc = $(if ($badRows.Count) { $badRows[0].when.ToString('o') } else { $null })
    }
    if ($Login) { $row.login = $Login }
    [pscustomobject]$row
}

# GitHub lists a commit's statuses newest first, so the listing index counts down.
function Get-QlStatusRows([string]$Repo, [string]$Head) {
    $list = @(Get-QlGhPages "repos/$Repo/commits/$Head/statuses")
    $tagged = @(for ($i = 0; $i -lt $list.Count; $i++) {
        [pscustomobject]@{ context = "$($list[$i].context)"; when = (ConvertTo-QlUtc $list[$i].created_at); ord = -$i; state = "$($list[$i].state)" }
    })
    @($tagged | Group-Object context | ForEach-Object { Group-QlRows $_.Group $_.Name 'status' '' @('success') @('failure', 'error') })
}

# Native reviews and review comments that bear on one head, from each PR that head was blocked on, folded per author:
# context pull-request-review/<login> (APPROVED = success, CHANGES_REQUESTED = failure; a review still pending has no
# submitted_at and is skipped) and pull-request-review-comment/<login> (never a success). A review counts for the commit
# it was submitted on, a review comment for the commit it was written on. Both listings are oldest first.
function Get-QlNativeRows([string]$Repo, [int[]]$Prs, [string]$Head) {
    $seen = @(); $ord = 0
    foreach ($n in @($Prs | Sort-Object -Unique)) {
        foreach ($r in @(Get-QlGhPages "repos/$Repo/pulls/$n/reviews")) {
            $ord++
            if ("$($r.commit_id)" -ne $Head -or $null -eq $r.submitted_at -or "$($r.submitted_at)" -eq '') { continue }
            $seen += [pscustomobject]@{ source = 'review'; login = "$($r.user.login)"; when = (ConvertTo-QlUtc $r.submitted_at); ord = $ord; state = "$($r.state)" }
        }
        foreach ($c in @(Get-QlGhPages "repos/$Repo/pulls/$n/comments")) {
            $ord++
            $on = $(if ($c.original_commit_id) { "$($c.original_commit_id)" } else { "$($c.commit_id)" })
            if ($on -ne $Head -or $null -eq $c.created_at -or "$($c.created_at)" -eq '') { continue }
            $seen += [pscustomobject]@{ source = 'review-comment'; login = "$($c.user.login)"; when = (ConvertTo-QlUtc $c.created_at); ord = $ord; state = 'COMMENTED' }
        }
    }
    @($seen | Where-Object { $_.login } | Group-Object source, login | ForEach-Object {
        $f = $_.Group[0]
        Group-QlRows $_.Group "pull-request-$($f.source)/$($f.login)" $f.source $f.login @('APPROVED') @('CHANGES_REQUESTED')
    })
}

function Test-QlBefore([object]$When, [DateTime]$Cos) { ($null -ne $When) -and ("$When" -ne '') -and ((ConvertTo-QlUtc $When) -lt $Cos) }

function Test-QlStatusRow($r) { $null -eq $r.source -or $r.source -eq 'status' }

# Is this row evidence the project DECLARED as its review? Nothing is, for a project with no declaration.
function Test-QlReviewRow($r, $Decl) {
    if (-not $Decl) { return $false }
    if (Test-QlStatusRow $r) { foreach ($pat in $Decl.statusContexts) { if ($r.context -like $pat) { return $true } }; return $false }
    $Decl.nativeReviews -and (-not $Decl.reviewAuthors.Count -or $r.login -in $Decl.reviewAuthors)
}

# An approval before CoS: a 'success' time before the pass on a row that can carry one (never a review comment).
function Test-QlApprovedBefore($r, [DateTime]$Cos) { $r.source -ne 'review-comment' -and (Test-QlBefore $r.successUtc $Cos) }

# The cache entry that proves the evidence of one pass, or $null: it must have been fetched at or after the pass, and, when
# the project declares native reviews, hold the reviews of that pass's PR.
function Get-QlEvidence([hashtable]$Cache, $p, [bool]$NeedNative) {
    $e = $Cache[(Get-QlKey $p)]
    if ($e -and (ConvertTo-QlUtc $e.fetched) -ge $p.utc -and (-not $NeedNative -or ($e.native -eq $true -and $p.pr -in @($e.prs)))) { $e } else { $null }
}

function Get-QlWindow([object[]]$P, [string]$Name, [hashtable]$Cache, $Decl) {
    # An empty window can arrive as $null, and @($null) is one element: drop nulls so it counts zero passes.
    $P = @($P | Where-Object { $null -ne $_ })
    $blocked = @($P | Where-Object blocked)
    $firstPerPr = @($P | Group-Object { "$($_.repo)#$($_.pr)" } | ForEach-Object { $_.Group | Sort-Object firstSeen, firstSeq | Select-Object -First 1 })
    # Status contexts and native review rows are counted apart: a status may carry any context text, a native one's included.
    $noKey = 0; $trueMiss = 0; $unknown = 0; $noStatus = 0; $okStatus = 0; $ctxStats = [ordered]@{}; $natStats = [ordered]@{}
    foreach ($b in $blocked) {
        $e = Get-QlEvidence $Cache $b ([bool]($Decl -and $Decl.nativeReviews))
        if (-not $e) { $unknown++; continue }
        $rows = @($e.rows | Where-Object { $null -ne $_ })
        $before = @($rows | Where-Object { Test-QlBefore $_.firstUtc $b.utc })
        $review = @($before | Where-Object { Test-QlReviewRow $_ $Decl })
        if ($review.Count -eq 0) { $noKey++ }
        if (@($review | Where-Object { Test-QlApprovedBefore $_ $b.utc }).Count) { $trueMiss++ }
        $statusBefore = @($before | Where-Object { Test-QlStatusRow $_ })
        if ($statusBefore.Count -eq 0) { $noStatus++ }
        if (@($statusBefore | Where-Object { Test-QlBefore $_.successUtc $b.utc }).Count) { $okStatus++ }
        foreach ($r in $before) {
            $stats = $(if (Test-QlStatusRow $r) { $ctxStats } else { $natStats })
            if (-not $stats.Contains($r.context)) { $stats[$r.context] = [ordered]@{ review = (Test-QlReviewRow $r $Decl); successBeforeCos = 0; failureBeforeCos = 0 } }
            if (Test-QlApprovedBefore $r $b.utc) { $stats[$r.context].successBeforeCos++ }
            if (Test-QlBefore $r.failureUtc $b.utc) { $stats[$r.context].failureBeforeCos++ }
        }
    }
    $rounds = @($P | Group-Object { "$($_.repo)#$($_.pr)" } | ForEach-Object { $_.Count })
    [ordered]@{
        window = $Name; passes = $P.Count; blocked = $blocked.Count
        blockedRate = $(if ($P.Count) { [math]::Round($blocked.Count / $P.Count, 3) } else { $null })
        prs = $firstPerPr.Count; firstRoundBlocked = @($firstPerPr | Where-Object blocked).Count
        firstRoundBlockedRate = $(if ($firstPerPr.Count) { [math]::Round(@($firstPerPr | Where-Object blocked).Count / $firstPerPr.Count, 3) } else { $null })
        reviewSource = (Format-QlSource $Decl)
        noPriorKey = $(if ($Decl) { $noKey } else { 'unknown' }); trueMiss = $(if ($Decl) { $trueMiss } else { 'unknown' }); statusUnknown = $unknown
        statusBeforeCos = [ordered]@{ noPriorStatus = $noStatus; successBeforeCos = $okStatus }
        roundsPerPr = $(if ($rounds.Count) { [math]::Round(($rounds | Measure-Object -Average).Average, 2) } else { $null })
        roundsPerPrMax = $(if ($rounds.Count) { ($rounds | Measure-Object -Maximum).Maximum } else { $null })
        contextsBeforeCos = $ctxStats; nativeBeforeCos = $natStats
    }
}

# Appends one JSON line. A torn last line (no final newline, from an interrupted run) is left as it is, and the new row
# starts on a line of its own instead of being glued onto it.
function Add-QlJsonLine([string]$Path, [string]$Line) {
    if ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -gt 0) {
        $fs = [IO.File]::OpenRead($Path)
        try { $null = $fs.Seek(-1, [IO.SeekOrigin]::End); $last = $fs.ReadByte() } finally { $fs.Dispose() }
        if ($last -ne 10) { $Line = "`n" + $Line }
    }
    Add-Content -LiteralPath $Path -Value $Line -Encoding utf8
}

function Format-QlOf($n, $w) { "$n of $($w.blocked)$(if ($w.statusUnknown) { " ($($w.statusUnknown) unknown)" })" }
function Format-QlFirst($w) { if ($w.noPriorKey -is [string]) { 'unknown' } else { Format-QlOf $w.noPriorKey $w } }
function Format-QlProxy($w) { Format-QlOf $w.statusBeforeCos.noPriorStatus $w }

# One run per project folder at a time: the corpus check-then-append and the cache rewrite are not safe to interleave. A run
# that cannot take the lock within -LockWaitSec writes nothing and reports a source error.
function Measure-QlProject([string]$Proj, [string]$Dir) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $lockPath = Join-Path $Dir '.quality-lift.lock'
    $lock = $null
    $waited = [Diagnostics.Stopwatch]::StartNew()   # monotonic: a wall-clock step cannot stretch or cut the wait
    while ($true) {
        try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None'); break }
        catch { if ($waited.Elapsed.TotalSeconds -ge $LockWaitSec) { break }; Start-Sleep -Milliseconds 250 }
    }
    if (-not $lock) {
        $empty = Get-QlWindow @() 'since' @{} $script:ReviewDecl[$Proj]
        return [pscustomobject]@{ project = $Proj; headline = "QUALITY-LIFT $Proj NOT MEASURED: another run holds the lock $lockPath"; all = $empty; d1 = $empty; adopt = [ordered]@{ prs = $null; withAdversaryLine = $null }; errors = 1 }
    }
    try { Measure-QlProjectLocked $Proj $Dir } finally { $lock.Dispose() }
}

function Measure-QlProjectLocked([string]$Proj, [string]$Dir) {
    $now = [DateTime]::UtcNow
    $d1Start = $now.AddHours(-24)
    $errors = @($script:SharedErrors)
    $decl = $script:ReviewDecl[$Proj]
    $needNative = [bool]($decl -and $decl.nativeReviews)
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
        # Native reviews are read from every PR the head was blocked on, and an entry that lacks one of them is refetched.
        $latest = @{}; $prsOf = @{}
        foreach ($p in ($raw | Where-Object blocked)) {
            $k = Get-QlKey $p
            if (-not $latest.ContainsKey($k) -or $p.utc -gt $latest[$k].utc) { $latest[$k] = $p }
            $prsOf[$k] = @(@($prsOf[$k]) + $p.pr | Where-Object { $null -ne $_ } | Sort-Object -Unique)
        }
        foreach ($k in @($latest.Keys)) {
            $p = $latest[$k]
            try {
                $e = Get-QlEvidence $cache $p $needNative
                if ($e -and (-not $needNative -or @($prsOf[$k] | Where-Object { $_ -notin @($e.prs) }).Count -eq 0)) { continue }
                $rows = @(Get-QlStatusRows $p.repo $p.head)
                if ($needNative) { $rows += @(Get-QlNativeRows $p.repo $prsOf[$k] $p.head) }
                $prsRead = @(if ($needNative) { $prsOf[$k] })   # an array even for one PR: a cached native entry must list them
                $cache[$k] = [pscustomobject]@{ fetched = $now.ToString('o'); native = $needNative; prs = $prsRead; rows = $rows }
            } catch { $errors += "statuses or reviews $($p.repo)@$($p.head.Substring(0,8)): $($_.Exception.Message)" }
        }
        ($cache | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $cacheFile -Encoding utf8
    }

    $all = Get-QlWindow $passes 'since' $cache $decl
    $d1 = Get-QlWindow $passes24 'last-24h' $cache $decl

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

    # The corpus keeps one row per blocked repo@head whose evidence is KNOWN; an unknown head is written on a later run.
    # successBeforeCos is raw (every status context that said success); reviewApprovedBeforeCos (declared status contexts)
    # and nativeApprovedBeforeCos (declared native reviewers) are the declared approvals only. A line that does not parse
    # (a torn append) is kept as it is and named.
    $corpusFile = Join-Path $Dir 'corpus.jsonl'
    $known = @{}; $badLines = 0
    if (Test-Path -LiteralPath $corpusFile) { Get-Content -LiteralPath $corpusFile | Where-Object { $_.Trim() } | ForEach-Object { try { $known[(Get-QlKey ($_ | ConvertFrom-Json))] = 1 } catch { $badLines++ } } }
    if ($badLines) { $errors += "corpus.jsonl has $badLines line(s) that do not parse; kept as they are, their heads are not counted as recorded" }
    $newCorpus = 0
    foreach ($b in ($heads | Where-Object blocked | Sort-Object utc)) {
        $k = Get-QlKey $b
        $e = Get-QlEvidence $cache $b $needNative
        if ($known.ContainsKey($k) -or -not $e) { continue }
        $okRows = @($e.rows | Where-Object { $null -ne $_ -and (Test-QlApprovedBefore $_ $b.utc) })
        $prior = @($okRows | Where-Object { Test-QlStatusRow $_ } | ForEach-Object context)
        if ($decl) {
            $approved = @($okRows | Where-Object { (Test-QlStatusRow $_) -and (Test-QlReviewRow $_ $decl) } | ForEach-Object context)
            $nativeApproved = @($okRows | Where-Object { -not (Test-QlStatusRow $_) -and (Test-QlReviewRow $_ $decl) } | ForEach-Object context)
        } else { $approved = 'unknown'; $nativeApproved = 'unknown' }
        Add-QlJsonLine $corpusFile ([ordered]@{ addedUtc = $now.ToString('o'); cosUtc = $b.utc.ToString('o'); repo = $b.repo; pr = $b.pr; head = $b.head; blocker = $b.blocker; successBeforeCos = $prior; reviewApprovedBeforeCos = $approved; nativeApprovedBeforeCos = $nativeApproved; reviewSource = (Format-QlSource $decl) } | ConvertTo-Json -Compress)
        $known[$k] = 1
        $newCorpus++
    }

    $res = [ordered]@{ schema = 'fleet/quality-lift/v1'; project = $Proj; utc = $now.ToString('o'); since = $Since; windows = @($all, $d1); shiftLeft = $adopt; corpusNew = $newCorpus; errors = $errors }
    ($res | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $Dir 'quality-lift.json') -Encoding utf8
    Add-QlJsonLine (Join-Path $Dir 'history.jsonl') ([ordered]@{ utc = $now.ToString('o'); project = $Proj; blockedRate24h = $d1.blockedRate; firstRound24h = $d1.firstRoundBlockedRate; noPriorKey24h = $d1.noPriorKey; trueMiss24h = $d1.trueMiss; statusUnknown24h = $d1.statusUnknown; noPriorStatus24h = $d1.statusBeforeCos.noPriorStatus; reviewSource = $d1.reviewSource; rounds24h = $d1.roundsPerPr; adversaryLine = $adopt.withAdversaryLine; prs24h = $adopt.prs } | ConvertTo-Json -Compress)

    $pct = { param($x) if ($null -eq $x) { 'n/a' } else { "$([math]::Round(100 * [double]$x))%" } }
    $md = @()
    $md += "QUALITY-LIFT $Proj $($now.ToString('yyyy-MM-ddTHH:mm'))Z: CoS blocker rate 24h $(& $pct $d1.blockedRate) (since $($Since.Substring(0,10)) $(& $pct $all.blockedRate)); first-round blocked 24h $($d1.firstRoundBlocked)/$($d1.prs); CoS read first 24h $(Format-QlFirst $d1); true misses 24h $($d1.trueMiss); review evidence: $($d1.reviewSource); no status before CoS 24h (proxy) $(Format-QlProxy $d1); adversary-pass PRs 24h $($adopt.withAdversaryLine)/$($adopt.prs)$(if ($errors) { "; ERRORS $($errors.Count)" })"
    $md += ''
    $md += "| window | CoS passes | blocked | first-round blocked (PRs) | CoS read first | true misses | no status before CoS (proxy) | CoS rounds per PR |"
    $md += "|---|---|---|---|---|---|---|---|"
    foreach ($w in @($all, $d1)) { $md += "| $($w.window) | $($w.passes) | $($w.blocked) ($(& $pct $w.blockedRate)) | $($w.firstRoundBlocked)/$($w.prs) | $(Format-QlFirst $w) | $($w.trueMiss) | $(Format-QlProxy $w) | $($w.roundsPerPr) (max $($w.roundsPerPrMax)) |" }
    $md += ''
    $md += "CoS read first and true misses count only declared review evidence ($($all.reviewSource)); 'unknown' when none is declared."
    $md += ''
    $md += "Contexts that existed before CoS on blocked heads (review = declared review evidence):"
    foreach ($k in $all.contextsBeforeCos.Keys) { $v = $all.contextsBeforeCos[$k]; $md += "- status $k$(if ($v.review) { ' [review]' }) : success $($v.successBeforeCos), failure $($v.failureBeforeCos)" }
    foreach ($k in $all.nativeBeforeCos.Keys) { $v = $all.nativeBeforeCos[$k]; $md += "- native $k$(if ($v.review) { ' [review]' }) : approved $($v.successBeforeCos), changes requested $($v.failureBeforeCos)" }
    if ($errors) { $md += ''; $md += 'Errors:'; $errors | Select-Object -First 10 | ForEach-Object { $md += "- $_" } }
    $md | Set-Content -LiteralPath (Join-Path $Dir 'quality-lift.md') -Encoding utf8
    [pscustomobject]@{ project = $Proj; headline = $md[0]; all = $all; d1 = $d1; adopt = $adopt; errors = $errors.Count }
}

if (-not $Bus) { Write-Output "BUS_UNREADABLE (unset: pass -Bus or set FACTORY_BUS_CLONE)"; exit 3 }
if (-not (Test-Path -LiteralPath (Join-Path $Bus 'cos-feedback'))) { Write-Output "BUS_UNREADABLE $Bus"; exit 3 }
git -C $Bus rev-parse --verify -q HEAD 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Output "BUS_UNREADABLE $Bus (not a git repository with a commit)"; exit 3 }
if (-not $OutDir) { Write-Output "USAGE -OutDir is required (the folder the reports are written to)"; exit 4 }
try { $SinceUtc = ConvertTo-QlUtc $Since; $Since = $SinceUtc.ToString('o') } catch { Write-Output "USAGE -Since '$Since' is not an ISO time"; exit 4 }
try { $ReviewDecl = Read-QlReviewSources $ReviewSources } catch { Write-Output "USAGE -ReviewSources is missing or malformed: $($_.Exception.Message)"; exit 4 }
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
$touched = @(git -C $Bus -c core.quotepath=false log --since-as-filter=$($fromUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')) --name-only --format= -- cos-feedback/)
if ($LASTEXITCODE -ne 0) { $SharedErrors += "git log of cos-feedback/ failed (exit $LASTEXITCODE); projects listed from the checkout only" }
# Folders starting with _ or . are bus plumbing (such as _bus, _example), not projects: they are named in the report, never
# measured. A project named like a fleet report would overwrite it (or be overwritten), so it is a named error instead.
$folders = @(@(Get-ChildItem -LiteralPath (Join-Path $Bus 'cos-feedback') -Directory | ForEach-Object Name) +
    @($touched | ForEach-Object { if ($_ -match '^cos-feedback/([^/]+)/') { $Matches[1] } }) | Where-Object { $_ } | Sort-Object -Unique)
$skipped = @($folders | Where-Object { $_ -match '^[_.]' })
$FleetErrors = @($folders | Where-Object { $_ -in 'fleet.md', 'fleet.json' } | ForEach-Object { "project folder '$_' has the name of a fleet report; not measured (rename the folder or measure it with -Project into its own -OutDir)" })
$projects = @($folders | Where-Object { $_ -notmatch '^[_.]' -and $_ -notin 'fleet.md', 'fleet.json' })
$results = @(foreach ($p in $projects) { Measure-QlProject $p (Join-Path $OutDir $p) })
$pct = { param($x) if ($null -eq $x) { 'n/a' } else { "$([math]::Round(100 * [double]$x))%" } }
$fl = @()
$fl += "# Fleet quality lift ($([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm'))Z, since $($Since.Substring(0,10)))"
$fl += ''
$fl += "| project | CoS passes | blocked | first-round blocked | review evidence | CoS read first | true misses | no status before CoS (proxy) | rounds/PR | adversary-pass PRs 24h | source errors |"
$fl += "|---|---|---|---|---|---|---|---|---|---|---|"
# The fleet's CoS-first and true-miss counts sum ONLY projects that declare review evidence; the proxy sums every project.
$tp = 0; $tb = 0; $tu = 0; $np = 0; $nd = 0; $dn = 0; $dm = 0; $db = 0; $du = 0; $sn = 0
# A project with no passes is listed when it has errors (a held lock, an unreadable bus object), so its diagnostic shows.
foreach ($r in ($results | Where-Object { $_.all.passes -gt 0 -or $_.errors } | Sort-Object { - $_.all.passes }, project)) {
    $fl += "| $($r.project) | $($r.all.passes) | $($r.all.blocked) ($(& $pct $r.all.blockedRate)) | $($r.all.firstRoundBlocked)/$($r.all.prs) | $($r.all.reviewSource) | $(Format-QlFirst $r.all) | $($r.all.trueMiss) | $(Format-QlProxy $r.all) | $($r.all.roundsPerPr) | $($r.adopt.withAdversaryLine)/$($r.adopt.prs) | $($r.errors) |"
    $np++; $tp += $r.all.passes; $tb += $r.all.blocked; $tu += $r.all.statusUnknown; $sn += $r.all.statusBeforeCos.noPriorStatus
    if ($r.all.noPriorKey -isnot [string]) { $nd++; $dn += $r.all.noPriorKey; $dm += $r.all.trueMiss; $db += $r.all.blocked; $du += $r.all.statusUnknown }
}
$fleetFirst = $(if ($nd) { Format-QlOf $dn ([pscustomobject]@{ blocked = $db; statusUnknown = $du }) } else { 'unknown' })
$fleetMiss = $(if ($nd) { $dm } else { 'unknown' })
$fl += "| **fleet** | $tp | $tb ($(if ($tp) { [math]::Round(100 * $tb / $tp) } else { 'n/a' })%) | | $nd of $np projects | $fleetFirst | $fleetMiss | $(Format-QlOf $sn ([pscustomobject]@{ blocked = $tb; statusUnknown = $tu })) | | | |"
if ($skipped) { $fl += ''; $fl += "Not measured (bus folders starting with _ or .): $($skipped -join ', ')" }
$listErrors = @($FleetErrors) + @(if (-not $results.Count) { $SharedErrors })
if ($listErrors) { $fl += ''; $fl += 'Errors:'; $listErrors | ForEach-Object { $fl += "- $_" } }
$fl | Set-Content -LiteralPath (Join-Path $OutDir 'fleet.md') -Encoding utf8
(ConvertTo-Json -InputObject @($results | Select-Object project, all, d1, adopt, errors) -Depth 8) | Set-Content -LiteralPath (Join-Path $OutDir 'fleet.json') -Encoding utf8
$fl
if (@($results | Where-Object { $_.errors }).Count -or $SharedErrors -or $FleetErrors) { exit 2 } else { exit 0 }
