# cos-open-ask-scan.ps1 - COS-OPEN-ASK-SCAN-1: make the CoS response duty mechanical.
# TRACKED HOME (HUB-TOOLS-TRACKED-1): this is the tracked, profile-driven copy; the live .claude-state\fleet-runs\hub-tools path becomes a forwarder to it at cutover. Unbound profile-backed parameters take hub-profile.json.
#   pwsh -NoProfile -File cos-open-ask-scan.ps1 [-BusRoot <clone>] [-Ref origin/master] [-Repo owner/name] [-OutFile <md>] [-FixtureDir <dir>] [-RepoAlias <former repo name>] [-PrStateFile <json>]
# COS-SCAN-LATEST-ASK-1 r1 (three defects seen 2026-10-16; every false NOASK / false ANSWERED lets a PR with an open CoS ask be enqueued):
#   1. the NEWEST ask counts, not the first: ask_utc is the newest of the bus header time, a stamped bus section that holds an ACK request (heading with a UTC stamp, e.g. `## Iverson ... (2026-10-16T15:49Z ...)`)
#      and an owner-account PR comment that holds one; an ACK answers it only when it is newer (strictly newer than a section / comment ask). An owner comment that itself starts with ACK: / DISTINGUISH: is never an ask.
#   2. an ACK request in the bus text OR in an owner-account PR comment newer than the bus header is an ask whatever the header verdict says (OPEN PRs; CLOSED / MERGED keep the -ExplicitOnly rule).
#      A green-header file with no ask text is therefore probed (`gh pr view`) when its PR is OPEN; the PR states come from ONE `gh pr list --state all` call (a failed call = UNKNOWN pr=- reason=pr-state-list-failed, exit 20).
#   3. bus headers naming the repo's former name (-RepoAlias, taken from the profile's repoAliases) are scanned normally; a truly foreign repo header stays UNKNOWN + exit 20.
#   UNANSWERED and ANSWERED rows end with ` ask_src=<header|bus-commit|section|comment|bus-commit-latest>` (where the ask time came from).
# COS-SCAN-UNDATED-ASK-1 r1b (sol SG-08): a follow-up ask appended to a bus file under an UNSTAMPED heading has no time of its own, so an older ACK looked like an answer to it. The commit time of the NEWEST bus commit that
#   changed the file (`git log -1 --format=%cI <ref> -- <file>`) is now one more ask-time candidate (ask_src=bus-commit-latest): it applies when the file's text holds an ACK request (same HasAskText rule as the file, -ExplicitOnly on
#   CLOSED / MERGED) and no stamped section already dates an ask at or after that commit; like a section / comment ask it is answered only by an ACK strictly newer than it. A commit time that cannot be read is
#   UNKNOWN pr=<N> reason=latest-commit-time-unreadable (never ANSWERED). Cost: any CoS edit to a PR file that carries an ask text re-opens the ask until a newer ACK (the intended fail-closed direction).
#   Fixture seam: <N>.latest-committed.txt (absent = not exercised; present but not a time = unreadable).
# Duty (owner ruling 2026-10-15 07:11:50Z): every Chief-of-Staff review ask is answered on the PR thread within 24 h, starting
# `ACK: <plan or ETA>` or `DISTINGUISH: <reason>`, before any merge or enqueue of that PR.
#
# Input : <FeedbackPath>/pr-<N>.md on the bus ref (read with `git show` / `git ls-tree`: no checkout, no fetch, no write to the bus).
#         A header block of `key: value` lines (schema, project, repo, pr, head, reviewed_utc, event, verdict), then markdown.
# ASK   : verdict is not merge-when-ci-green, OR any text carries an ACK request: `ACK required` (bold or not), `ACK: <plan` /
#         `starting with ACK:`, or any line holding both ACK and DISTINGUISH (case-insensitive, whole words).
#         Known false-positive direction (deliberate): a line such as "no ACK required" counts as an ask; a spurious ACK is cheap, a missed ask is not.
#         MERGE-GATE-TIP-LEGS-1 r2: on a CLOSED / MERGED PR the verdict alone is NOT an ask and the loose 'ACK and DISTINGUISH on one line' rule does not apply; only an explicit ACK
#         request counts (HasAskText -ExplicitOnly), else the row is `NOASK pr=<N> verdict=<v> state=<CLOSED|MERGED>`. A header with no reviewed_utc takes the file's bus commit time
#         (git log -1 --format=%cI on the bus ref) as ask_utc and the UNANSWERED row ends with ` ask_src=bus-commit`; fixture seam: <N>.committed.txt.
# ANSWER: an issue comment on the PR created at or after reviewed_utc whose body starts (after whitespace) with `ACK:` or `DISTINGUISH:` (case-sensitive) AND is authored by the OWNER
#         account (-OwnerLogin, default layibabalola; the hub posts under it). MERGE-GATE-TIP-LEGS-1 r3 (MG-R2-05): an answer from any other account is ignored. CLOSED / MERGED PRs carrying an ask are scanned exactly like OPEN ones (MERGE-GATE-TIP-LEGS-1; they used to be
#         SKIP, which hid the post-merge asks on #448 #451 #454): their own row says UNANSWERED or ANSWERED, never SKIP.
# Output: one line per file on stdout and in OutFile (self-dating markdown):
#   SCAN bus_ref=<ref> bus_commit=<sha8> committed=<utc> at=<utc> files=<n>
#   UNANSWERED pr=<N> head=<sha8> ask_utc=<reviewed_utc> deadline=<+24h> verdict=<v> [state=<CLOSED|MERGED>]
#   ANSWERED pr=<N> by=<comment id> <utc> author=<login> kind=<ACK|DISTINGUISH> [state=<CLOSED|MERGED>]
#     (the trailing state= field is its own field and is written only when the PR is not OPEN; no state= means OPEN)
#   NOASK pr=<N> verdict=<v>
#   UNKNOWN pr=<N|-> reason=<why> [header_pr=<n>]     (r3: header_pr=<n> is written on EVERY UNKNOWN row whose header names a PR, parse failures included)
#   COS_OPEN_ASKS unanswered=<n> answered=<n> skipped=<n> unknown=<n>      (skipped is kept for format compatibility and is always 0 now)
# Exit : 0 = no UNANSWERED and no UNKNOWN; 10 = at least one UNANSWERED (and nothing UNKNOWN);
#        20 = anything UNKNOWN (bus ref missing, no feedback files, gh failure, unparsable header). UNKNOWN wins over 10 because a blind
#        scan must never read as a clean one; the UNANSWERED lines are still printed and the hub still answers each of them.
# -FixtureDir test seam: pr-*.md are read from the folder; PR comments come from <N>.comments.json (array; gh `pr view` or REST shape)
#   and PR state from <N>.state.txt (OPEN|CLOSED|MERGED) instead of gh. Only PRs that carry an ask need those two files.
# Every external call is bounded by a timeout and fails to UNKNOWN. ASCII only, PowerShell 7.
param(
    [string]$BusRoot,
    [string]$Ref,
    [string]$Repo,
    [string]$OwnerLogin,
    [string]$OutFile,
    [string]$FixtureDir = '',
    [string]$FeedbackPath,
    # COS-SCAN-LATEST-ASK-1 r1: repo names a bus header may carry for THIS repo (the repo's pre-transfer name, listed in the profile's repoAliases; GitHub redirects it, PR numbers are unchanged). Passing -RepoAlias REPLACES the list.
    [string[]]$RepoAlias,
    # test seam: a JSON array of { number, state } read instead of `gh pr list --state all` (the scan asks GitHub for every PR's state once)
    [string]$PrStateFile = '',
    # COS-SCAN-EDITED-COMMENT-ASK-1 (sol SG-10) test seam for the live path: <N>.view.json stands for `gh pr view --json state`,
    # <N>.api.json for `gh api repos/<repo>/issues/<N>/comments --paginate --slurp`; a file whose text is FAIL is a failed call.
    [string]$GhDataDir = '',
    [int]$GhTimeoutSec = 60,
    # sol gate key r16 SG-17 test seam: the gh executable the live path runs (a fake .cmd in the tests); default gh.
    [string]$GhExe = 'gh',
    [int]$GitTimeoutSec = 60,
    # HUB-TOOLS-TRACKED-1: the profile that backs BusRoot, Ref, Repo, OwnerLogin, OutFile, FeedbackPath and RepoAlias when they are not passed.
    [string]$ProfilePath = (Join-Path $PSScriptRoot 'hub-profile.json')
)
# HUB-TOOLS-TRACKED-1: resolve the profile-backed parameters. Bound vs unbound is decided by $PSBoundParameters, never by emptiness: a BOUND -OutFile '' stays '' (no file; merge-enqueue.ps1's call shape).
# The profile is loaded only when at least one of them is unbound. A profile that cannot be used is a blind scan: UNKNOWN pr=-, no file, exit 20.
$profileBacked = @('BusRoot', 'Ref', 'Repo', 'OwnerLogin', 'OutFile', 'FeedbackPath', 'RepoAlias')
if (@($profileBacked | Where-Object { -not $PSBoundParameters.ContainsKey($_) }).Count -gt 0) {
    $profileFail = ''
    try {
        Import-Module (Join-Path $PSScriptRoot 'HubProfile.psm1') -Force -ErrorAction Stop
        $hubProfile = Get-HubProfile -Path $ProfilePath
    } catch {
        $profileFail = if ($_.Exception.GetType().Name -eq 'HubProfileMissingKeyException') { "hub-profile-missing-key key=$($_.Exception.Key)" } else { "hub-profile-unreadable path=$ProfilePath" }
    }
    if ($profileFail) {
        Write-Output "UNKNOWN pr=- reason=$profileFail"
        Write-Output 'COS_OPEN_ASKS unanswered=0 answered=0 skipped=0 unknown=1'
        exit 20
    }
    if (-not $PSBoundParameters.ContainsKey('BusRoot')) { $BusRoot = [string]$hubProfile.busRoot }
    if (-not $PSBoundParameters.ContainsKey('Ref')) { $Ref = [string]$hubProfile.busRef }
    if (-not $PSBoundParameters.ContainsKey('Repo')) { $Repo = [string]$hubProfile.repoSlug }
    if (-not $PSBoundParameters.ContainsKey('OwnerLogin')) { $OwnerLogin = [string]$hubProfile.ownerLogin }
    if (-not $PSBoundParameters.ContainsKey('OutFile')) { $OutFile = Join-Path ([string]$hubProfile.boardRoot) ([string]$hubProfile.cosOpenAsksOutFile) }
    if (-not $PSBoundParameters.ContainsKey('FeedbackPath')) { $FeedbackPath = [string]$hubProfile.cosFeedbackPath }
    if (-not $PSBoundParameters.ContainsKey('RepoAlias')) { $RepoAlias = [string[]]@($hubProfile.repoAliases) }
}
$ErrorActionPreference = 'Continue'
$inv = [Globalization.CultureInfo]::InvariantCulture

function Exec([string]$Exe, [string[]]$ExeArgs, [int]$TimeoutSec) {
    try {
        $psi = [Diagnostics.ProcessStartInfo]::new($Exe)
        foreach ($a in $ExeArgs) { $psi.ArgumentList.Add($a) }
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
        $psi.StandardOutputEncoding = [Text.Encoding]::UTF8; $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
        $p = [Diagnostics.Process]::Start($psi)
        $out = $p.StandardOutput.ReadToEndAsync(); $err = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($TimeoutSec * 1000)) {
            try { $p.Kill($true) } catch {}
            return [pscustomobject]@{ ok = $false; out = ''; err = "TIMEOUT ${TimeoutSec}s" }
        }
        $p.WaitForExit()
        return [pscustomobject]@{ ok = ($p.ExitCode -eq 0); out = $out.Result; err = $err.Result }
    } catch { return [pscustomobject]@{ ok = $false; out = ''; err = "EXEC_ERROR $($_.Exception.Message)" } }
}
function BusGit([string[]]$GitArgs) { Exec 'git' (@('-C', $BusRoot) + $GitArgs) $GitTimeoutSec }

function ToUtc($v) {
    # Returns a DateTimeOffset in UTC or $null. ConvertFrom-Json may already have turned an ISO string into a DateTime.
    if ($null -eq $v) { return $null }
    try {
        if ($v -is [datetimeoffset]) { return $v.ToUniversalTime() }
        if ($v -is [datetime]) { return [datetimeoffset]::new($v.ToUniversalTime(), [timespan]::Zero) }
        $s = ([string]$v).Trim().Trim('"').Trim("'")
        if ($s -eq '') { return $null }
        $styles = [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
        return [datetimeoffset]::Parse($s, $inv, $styles)
    } catch { return $null }
}
function Fmt($d) { if ($null -eq $d) { 'UNKNOWN' } else { $d.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $inv) } }

function HeaderPrField($H) {
    # ' header_pr=<n>' (digits only, the first number in the header's pr value) or '' when the header is absent or names no PR.
    if ($null -ne $H -and $H.ContainsKey('pr') -and ([string]$H['pr']) -match '(\d+)') { return " header_pr=$($Matches[1])" }
    return ''
}

function ParseFeedback([string]$Text, $FallbackWhen = $null) {
    # Header = leading run of `key: value` lines (optionally inside a `---` fence). Returns @{ ok; err; h; body; when; src }.
    # MERGE-GATE-TIP-LEGS-1 r2: a header with NO reviewed_utc uses $FallbackWhen (the file's bus commit time) when the caller has one; src='bus-commit' then. A PRESENT but unparsable value stays an error.
    $lines = $Text -split "\r?\n"
    $i = 0
    while ($i -lt $lines.Count -and $lines[$i].Trim() -eq '') { $i++ }
    $fenced = $false
    if ($i -lt $lines.Count -and $lines[$i].Trim() -eq '---') { $fenced = $true; $i++ }
    $h = @{}
    while ($i -lt $lines.Count) {
        $m = [regex]::Match($lines[$i], '^\s*([A-Za-z][A-Za-z0-9_\-]*)\s*:\s*(.*?)\s*$')
        if (-not $m.Success) { break }
        $k = $m.Groups[1].Value.ToLowerInvariant()
        if (-not $h.ContainsKey($k)) { $h[$k] = $m.Groups[2].Value.Trim().Trim('"').Trim("'") }
        $i++
    }
    $body = if ($i -lt $lines.Count) { ($lines[$i..($lines.Count - 1)] -join "`n") } else { '' }
    if ($h.Count -eq 0) { return @{ ok = $false; err = 'no-header' } }
    $noWhen = (-not $h.ContainsKey('reviewed_utc')) -or [string]::IsNullOrWhiteSpace($h['reviewed_utc'])
    foreach ($req in @('reviewed_utc', 'verdict')) {
        if (-not $h.ContainsKey($req) -or [string]::IsNullOrWhiteSpace($h[$req])) {
            if ($req -eq 'reviewed_utc' -and $null -ne $FallbackWhen) { continue }
            return @{ ok = $false; err = "header-missing-$req"; h = $h }
        }
    }
    if ($noWhen) { return @{ ok = $true; err = ''; h = $h; body = $body; when = $FallbackWhen; src = 'bus-commit' } }
    $when = ToUtc $h['reviewed_utc']
    if ($null -eq $when) { return @{ ok = $false; err = "header-unparsable-reviewed_utc"; h = $h } }
    return @{ ok = $true; err = ''; h = $h; body = $body; when = $when; src = 'header' }
}

# The ask test. Default = the three patterns below (deliberately wide: a spurious ACK is cheap, a missed ask is not).
# -ExplicitOnly = the first two only (an explicit ACK request); used for CLOSED / MERGED PRs, where a line that merely mentions ACK and DISTINGUISH together is prose about the duty, not an ask.
# merge-gate-tip-legs.ps1 carries a copy of this function (Test-MergeGateAskText); Test-MergeGateTipLegs.ps1 row W9 fails if the two drift apart.
function HasAskText([string]$Text, [switch]$ExplicitOnly) {
    foreach ($line in ($Text -split "\r?\n")) {
        if ($line -match '(?i)\bACK\s+(required|requested|request)\b') { return $true }
        if ($line -match '(?i)\bACK\s*:\s*(<\s*[A-Za-z]|plan)' -or $line -match '(?i)starting\s+with\s+\W{0,4}ACK\b') { return $true }   # 'ACK: <plan or ETA>' and any other 'ACK: <placeholder>' (pr-409: 'Reply ACK: <owner-decision link ...>')
        if (-not $ExplicitOnly -and $line -match '(?i)\bACK\b' -and $line -match '(?i)\bDISTINGUISH\b') { return $true }
    }
    return $false
}

# MERGE-GATE-TIP-LEGS-1 r2: when a bus file has no reviewed_utc, its bus commit time stands in as the ask time. $null = unknown (the row stays UNKNOWN).
function Get-FeedbackCommitTime($Entry) {
    if ($FixtureDir) {
        $cf = Join-Path $FixtureDir "$($Entry.pr).committed.txt"
        if (Test-Path -LiteralPath $cf) { return (ToUtc ([IO.File]::ReadAllText($cf))) }
        return $null
    }
    if (-not $Entry.ContainsKey('path') -or -not $Entry.path) { return $null }
    $lg = BusGit @('log', '-1', '--format=%cI', $Ref, '--', $Entry.path)
    if (-not $lg.ok -or -not $lg.out.Trim()) { return $null }
    return (ToUtc $lg.out.Trim())
}

# COS-SCAN-UNDATED-ASK-1 r1b: the commit time of the NEWEST bus commit that changed this PR's file. Returns @{ state = 'ok' | 'skip' | 'unreadable'; when }.
# skip = fixture seam not exercised (no <N>.latest-committed.txt); unreadable = the git call failed / printed nothing / the seam holds no time (the caller turns that into UNKNOWN).
function Get-FeedbackLatestCommitTime($Entry) {
    if ($FixtureDir) {
        $lf = Join-Path $FixtureDir "$($Entry.pr).latest-committed.txt"
        if (-not (Test-Path -LiteralPath $lf)) { return @{ state = 'skip'; when = $null } }
        $t = ToUtc ([IO.File]::ReadAllText($lf))
        if ($null -eq $t) { return @{ state = 'unreadable'; when = $null } }
        return @{ state = 'ok'; when = $t }
    }
    if (-not $Entry.ContainsKey('path') -or -not $Entry.path) { return @{ state = 'unreadable'; when = $null } }
    $lg = BusGit @('log', '-1', '--format=%cI', $Ref, '--', $Entry.path)
    if (-not $lg.ok -or -not $lg.out.Trim()) { return @{ state = 'unreadable'; when = $null } }
    $t = ToUtc $lg.out.Trim()
    if ($null -eq $t) { return @{ state = 'unreadable'; when = $null } }
    return @{ state = 'ok'; when = $t }
}

# COS-SCAN-LATEST-ASK-1 r1: a later ask inside the same bus file. A heading carrying a UTC stamp (`## Iverson single-model verification (2026-10-16T15:49Z, ...)`) opens a section that runs to the next
# heading of the same or a higher level (sub-headings belong to it); when that section's text holds an ACK request (HasAskText, same -ExplicitOnly rule as the file) the stamp is an ask time.
function Get-StampedAskTimes([string]$Body, [bool]$ExplicitOnly) {
    $times = [Collections.Generic.List[object]]::new()
    $lines = $Body -split "\r?\n"
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $hm = [regex]::Match($lines[$i], '^(#{1,6})\s+(.*)$')
        if (-not $hm.Success) { continue }
        $sm = [regex]::Match($hm.Groups[2].Value, '\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)Z\b')
        if (-not $sm.Success) { continue }
        $when = ToUtc ($sm.Groups[1].Value + 'Z')
        if ($null -eq $when) { continue }
        $level = $hm.Groups[1].Value.Length
        $j = $i + 1
        while ($j -lt $lines.Count) {
            $nm = [regex]::Match($lines[$j], '^(#{1,6})\s')
            if ($nm.Success -and $nm.Groups[1].Value.Length -le $level) { break }
            $j++
        }
        if (HasAskText (($lines[$i..($j - 1)]) -join "`n") -ExplicitOnly:$ExplicitOnly) { $times.Add($when) }
    }
    return , $times
}

function NormalizeComments($Raw) {
    $list = [Collections.Generic.List[object]]::new()
    foreach ($c in @($Raw)) {
        if ($null -eq $c) { continue }
        $id = ''
        if ($c.PSObject.Properties['id'] -and $c.id) { $id = [string]$c.id }
        elseif ($c.PSObject.Properties['databaseId'] -and $c.databaseId) { $id = [string]$c.databaseId }
        elseif ($c.PSObject.Properties['url'] -and ([string]$c.url) -match 'issuecomment-(\d+)') { $id = $Matches[1] }
        $author = ''
        if ($c.PSObject.Properties['user'] -and $c.user -and $c.user.PSObject.Properties['login']) { $author = [string]$c.user.login }
        elseif ($c.PSObject.Properties['author'] -and $c.author) {
            if ($c.author -is [string]) { $author = $c.author } elseif ($c.author.PSObject.Properties['login']) { $author = [string]$c.author.login }
        }
        $created = $null
        if ($c.PSObject.Properties['created_at']) { $created = ToUtc $c.created_at }
        elseif ($c.PSObject.Properties['createdAt']) { $created = ToUtc $c.createdAt }
        # COS-SCAN-EDITED-COMMENT-ASK-1 r1b (sol SG-10): `updated` = updated_at / updatedAt (REST carries it; `gh pr view` never did), falling back to created. The newest of created / updated dates an ASK
        # (an owner comment edited to carry a new ACK request); an ANSWER keeps `created` (strict), so editing an old ACK never answers a newer ask. A row with no created time, or an updated value that is
        # present but not a time, is `bad`: the caller turns any bad row into UNKNOWN reason=comments-unparsable (it used to be dropped silently, which read as ANSWERED).
        $updated = $created; $bad = ($null -eq $created)
        $rawUpdated = $null
        if ($c.PSObject.Properties['updated_at']) { $rawUpdated = $c.updated_at } elseif ($c.PSObject.Properties['updatedAt']) { $rawUpdated = $c.updatedAt }
        if ($null -ne $rawUpdated -and ([string]$rawUpdated).Trim() -ne '') {
            $u = ToUtc $rawUpdated
            if ($null -eq $u) { $bad = $true } elseif ($null -eq $created -or $u -gt $created) { $updated = $u }
        }
        $body = ''
        if ($c.PSObject.Properties['body'] -and $null -ne $c.body) { $body = [string]$c.body }
        $list.Add([pscustomobject]@{ id = $id; author = $author; created = $created; updated = $updated; bad = $bad; body = $body })
    }
    return , $list
}
# gh api --paginate --slurp answers an array of PAGES, each an array of comments: [ [ {..} ], [ {..} ] ]. Returns @{ ok; rows } with the pages flattened; anything that is not an array of arrays of
# objects (an error object such as {"message":"Not Found"}, a bare string, a null row) is not ok. An empty list of pages is a PR with no comments (ok, no rows).
# COS-SCAN-REVIEW-SURFACE-ASK-1 (sol SG-11): PR reviews carry submitted_at (no created_at); a PENDING review has none and is
# invisible to everyone but its author, so it is skipped. Review comments (inline and thread replies) already carry created_at /
# updated_at. Rows are then normalised like issue comments (a row with no usable time is `bad`).
function Normalize-ReviewRows($Raw) {
    $keep = [Collections.Generic.List[object]]::new()
    foreach ($r in @($Raw)) {
        if ($null -eq $r) { continue }
        if ($r.PSObject.Properties['state'] -and ([string]$r.state) -eq 'PENDING') { continue }
        if (-not $r.PSObject.Properties['created_at'] -and $r.PSObject.Properties['submitted_at']) {
            $r | Add-Member -NotePropertyName created_at -NotePropertyValue $r.submitted_at -Force
        }
        $keep.Add($r)
    }
    return (NormalizeComments @($keep))
}
function Flatten-CommentPages($Parsed) {
    $rows = [Collections.Generic.List[object]]::new()
    if ($null -eq $Parsed) { return @{ ok = $false; rows = $rows } }
    $pages = @($Parsed)
    foreach ($pg in $pages) {
        if ($null -eq $pg) { return @{ ok = $false; rows = $rows } }
        if ($pg -is [System.Management.Automation.PSCustomObject] -or $pg -is [string] -or $pg -is [ValueType]) {
            # a page that is a single object (pwsh unrolls one-element pages) is a comment row only when it looks like one
            if ($pg -is [System.Management.Automation.PSCustomObject] -and $pg.PSObject.Properties['body'] -and $pg.PSObject.Properties['user']) { $rows.Add($pg); continue }
            return @{ ok = $false; rows = $rows }
        }
        foreach ($row in @($pg)) {
            if ($row -isnot [System.Management.Automation.PSCustomObject]) { return @{ ok = $false; rows = $rows } }
            $rows.Add($row)
        }
    }
    return @{ ok = $true; rows = $rows }
}

# ---------------------------------------------------------------- collect the feedback files
$now = [datetimeoffset]::UtcNow
$out = [Collections.Generic.List[string]]::new()
$counts = @{ unanswered = 0; answered = 0; skipped = 0; unknown = 0 }
$files = [Collections.Generic.List[object]]::new()   # @{ pr; text } or @{ pr; err }
$busCommit = 'n/a'; $busCommitted = 'n/a'; $refLabel = $Ref

if ($FixtureDir) {
    $refLabel = "fixture:$FixtureDir"
    if (-not (Test-Path -LiteralPath $FixtureDir -PathType Container)) {
        $out.Add('UNKNOWN pr=- reason=fixture-dir-missing'); $counts.unknown++
    } else {
        foreach ($f in (Get-ChildItem -LiteralPath $FixtureDir -Filter 'pr-*.md' -File)) {
            if ($f.Name -match '^pr-(\d+)\.md$') {
                $files.Add(@{ pr = [int]$Matches[1]; text = [IO.File]::ReadAllText($f.FullName, [Text.Encoding]::UTF8) })
            }
        }
    }
} else {
    $rv = BusGit @('rev-parse', '--verify', '--quiet', "$Ref^{commit}")
    if (-not $rv.ok -or -not $rv.out.Trim()) {
        $out.Add("UNKNOWN pr=- reason=bus-ref-missing ref=$Ref bus=$BusRoot $($rv.err.Trim())"); $counts.unknown++
    } else {
        $busCommit = $rv.out.Trim().Substring(0, 8)
        $cd = BusGit @('log', '-1', '--format=%cI', $Ref)
        if ($cd.ok) { $busCommitted = Fmt (ToUtc $cd.out.Trim()) }
        # DG-GIT-PATHLIST: quotepath off so a non-ASCII feedback path is printed raw and reaches 'git show' unquoted.
        $ls = BusGit @('-c', 'core.quotepath=false', 'ls-tree', '-r', '--name-only', $Ref, '--', "$FeedbackPath/")
        if (-not $ls.ok) {
            $out.Add("UNKNOWN pr=- reason=ls-tree-failed $($ls.err.Trim())"); $counts.unknown++
        } else {
            foreach ($path in ($ls.out -split "\r?\n")) {
                if ($path -match '/pr-(\d+)\.md$') {
                    $n = [int]$Matches[1]
                    $sh = BusGit @('show', "${Ref}:$path")
                    if ($sh.ok) { $files.Add(@{ pr = $n; text = $sh.out; path = $path }) }
                    else { $files.Add(@{ pr = $n; err = "git-show-failed $($sh.err.Trim())" }) }
                }
            }
        }
    }
}
if ($out.Count -eq 0 -and $files.Count -eq 0) {
    # A valid ref/dir with no feedback files is suspicious, not clean: unknown is never none.
    $out.Add("UNKNOWN pr=- reason=no-feedback-files path=$FeedbackPath"); $counts.unknown++
}

# ---------------------------------------------------------------- PR states (one call; r1)
# A green-header file with no ask text is only probed for a comment ask when its PR is OPEN, and a CLOSED / MERGED file with only a verdict-level ask needs no per-PR call: both
# decisions come from this map. A map that cannot be read is a blind scan (UNKNOWN pr=-, exit 20); the per-PR path then still runs for every file that carries an ask.
$stateMap = $null
if (-not $FixtureDir -and $files.Count -gt 0) {
    $listRaw = ''; $listErr = ''
    if ($PrStateFile) {
        try { $listRaw = [IO.File]::ReadAllText($PrStateFile, [Text.Encoding]::UTF8) } catch { $listErr = "state-file-unreadable $($_.Exception.Message)" }
    } else {
        $gl = Exec $GhExe @('pr', 'list', '--repo', $Repo, '--state', 'all', '--limit', '5000', '--json', 'number,state') $GhTimeoutSec
        if ($gl.ok) { $listRaw = $gl.out } else { $listErr = "gh-failed $($gl.err.Trim() -replace '\s+', ' ')" }
    }
    if (-not $listErr) {
        try {
            $map = @{}
            foreach ($it in @($listRaw | ConvertFrom-Json)) {
                if ($null -ne $it -and $null -ne $it.number -and $it.state) { $map[[int]$it.number] = ([string]$it.state).Trim().ToUpperInvariant() }
            }
            $stateMap = $map
        } catch { $listErr = 'json-unparsable' }
    }
    if ($listErr) { $out.Add("UNKNOWN pr=- reason=pr-state-list-failed $listErr"); $counts.unknown++ }
}

# ---------------------------------------------------------------- classify
foreach ($e in ($files | Sort-Object { $_.pr })) {
    $n = $e.pr
    if ($e.ContainsKey('err')) { $out.Add("UNKNOWN pr=$n reason=$($e.err)"); $counts.unknown++; continue }
    $p = ParseFeedback $e.text
    if (-not $p.ok -and $p.err -eq 'header-missing-reviewed_utc') {
        # MERGE-GATE-TIP-LEGS-1 r2: no reviewed_utc -> the file's bus commit time is the ask time (flagged ask_src=bus-commit on the row); no commit time -> UNKNOWN as before.
        $fbWhen = Get-FeedbackCommitTime $e
        if ($null -ne $fbWhen) { $p = ParseFeedback $e.text $fbWhen }
    }
    # MERGE-GATE-TIP-LEGS-1 r3 (sol MG-R2-04, fable MG-R2-05): the header's pr is read BEFORE any other parse check, and EVERY UNKNOWN row below carries header_pr=<n> when the header names a PR,
    # so a parse failure (missing verdict, unparsable reviewed_utc, repo mismatch) in a file misnamed for another PR cannot hide the PR the header really names. The gate treats a row naming
    # PR N anywhere (pr=N / header_pr=N) as blind for PR N. A file that could not be read at all (git-show-failed) or has no header at all has nothing to carry.
    $hp = HeaderPrField $p.h
    if (-not $p.ok) { $out.Add("UNKNOWN pr=$n reason=$($p.err)$hp"); $counts.unknown++; continue }
    $askSrcField = if ($p.src -eq 'bus-commit') { ' ask_src=bus-commit' } else { '' }
    $h = $p.h
    if ($h.ContainsKey('pr') -and $h['pr'] -match '(\d+)' -and [int]$Matches[1] -ne $n) {
        $out.Add("UNKNOWN pr=$n reason=header-pr-mismatch$hp"); $counts.unknown++; continue
    }
    # r1: the repo's former name (-RepoAlias) is the same repo, so its rows scan normally; a header naming any other repo stays UNKNOWN.
    $repoKnown = @($Repo) + @($RepoAlias | Where-Object { $_ })
    if ($h.ContainsKey('repo') -and $h['repo'].Contains('/') -and -not ($repoKnown | Where-Object { $_.ToLowerInvariant() -eq $h['repo'].ToLowerInvariant() })) {
        $out.Add("UNKNOWN pr=$n reason=header-repo-mismatch header_repo=$($h['repo']) scan_repo=$Repo$hp"); $counts.unknown++; continue
    }
    $verdict = $h['verdict'].Trim().ToLowerInvariant()
    # r1: an ask is (a) a non-green header verdict, (b) an ACK request in the bus text, or (c) below, an ACK request in a newer owner-account PR comment (seen only on an OPEN PR when (a) and (b) are absent).
    $askCand = ($verdict -ne 'merge-when-ci-green') -or (HasAskText $e.text)
    $explicit = HasAskText $e.text -ExplicitOnly

    $headFull = if ($h.ContainsKey('head')) { $h['head'] } else { '' }
    $head8 = if ($headFull.Length -ge 8) { $headFull.Substring(0, 8) } elseif ($headFull) { $headFull } else { 'unknown' }

    # PR state (+ comments). fixture: <N>.state.txt and <N>.comments.json; live: the state map first, `gh pr view` only when the comments are needed.
    $state = ''; $comments = $null; $stateHint = ''; $haveData = $false
    if ($FixtureDir) {
        $sf = Join-Path $FixtureDir "$n.state.txt"; $cf = Join-Path $FixtureDir "$n.comments.json"
        if ((Test-Path -LiteralPath $sf) -and (Test-Path -LiteralPath $cf)) {
            $state = ([IO.File]::ReadAllText($sf)).Trim().ToUpperInvariant()
            try { $comments = NormalizeComments (@([IO.File]::ReadAllText($cf, [Text.Encoding]::UTF8) | ConvertFrom-Json)) }
            catch { $out.Add("UNKNOWN pr=$n reason=fixture-comments-unparsable"); $counts.unknown++; continue }
            $haveData = $true; $stateHint = $state
        } elseif ($askCand) {
            $out.Add("UNKNOWN pr=$n reason=fixture-state-or-comments-missing"); $counts.unknown++; continue
        }
    } elseif ($null -ne $stateMap -and $stateMap.ContainsKey($n)) { $stateHint = $stateMap[$n] }
    # nothing to read: no ask text and the PR is not (known to be) OPEN; or only a verdict-level ask on a PR known to be CLOSED / MERGED (the verdict alone is never an ask there)
    # sol SG-15: the state list is a hint, not proof. CLOSED / MERGED rows now say so (state=...), so the gate can refuse a PR it is merging that the
    # list called closed; a PR the list did not name at all is read live (state, comments, reviews) instead of being trusted as NOASK.
    if (-not $askCand -and $stateHint -in @('CLOSED', 'MERGED')) { $out.Add("NOASK pr=$n verdict=$verdict state=$stateHint"); continue }
    if (-not $askCand -and $stateHint -ne 'OPEN' -and ($FixtureDir -or $stateHint -ne '')) { $out.Add("NOASK pr=$n verdict=$verdict"); continue }
    if ($askCand -and $stateHint -in @('CLOSED', 'MERGED') -and -not $explicit) { $out.Add("NOASK pr=$n verdict=$verdict state=$stateHint"); continue }
    # COS-SCAN-EDITED-COMMENT-ASK-1 (sol SG-10): the live read no longer takes comments from `gh pr view --json state,comments` (no edit time).
    # The PR state still comes from gh pr view; the comments come from the REST API, which carries updated_at. Kept for reference:
    #   $g = Exec 'gh' @('pr', 'view', "$n", '--repo', $Repo, '--json', 'state,comments') $GhTimeoutSec ; $comments = NormalizeComments @($j.comments)
    if (-not $haveData) {
        if ($GhDataDir) {
            $vf = Join-Path $GhDataDir "$n.view.json"; $af = Join-Path $GhDataDir "$n.api.json"
            $vt = if (Test-Path -LiteralPath $vf) { [IO.File]::ReadAllText($vf, [Text.Encoding]::UTF8) } else { 'FAIL' }
            $at = if (Test-Path -LiteralPath $af) { [IO.File]::ReadAllText($af, [Text.Encoding]::UTF8) } else { 'FAIL' }
            $g = [pscustomobject]@{ ok = ($vt.Trim() -ne 'FAIL'); out = $vt; err = 'seam view FAIL' }
            $ga = [pscustomobject]@{ ok = ($at.Trim() -ne 'FAIL'); out = $at; err = 'seam api FAIL' }
        } else {
            $g = Exec $GhExe @('pr', 'view', "$n", '--repo', $Repo, '--json', 'state') $GhTimeoutSec
            $ga = if ($g.ok) { Exec $GhExe @('api', "repos/$Repo/issues/$n/comments", '--paginate', '--slurp') $GhTimeoutSec } else { $g }
        }
        if (-not $g.ok) { $out.Add("UNKNOWN pr=$n reason=gh-failed $(([string]$g.err).Trim() -replace '\s+', ' ')"); $counts.unknown++; continue }
        if (-not $ga.ok) { $out.Add("UNKNOWN pr=$n reason=gh-failed comments $(([string]$ga.err).Trim() -replace '\s+', ' ')"); $counts.unknown++; continue }
        try { $state = ([string](($g.out | ConvertFrom-Json).state)).Trim().ToUpperInvariant() }
        catch { $out.Add("UNKNOWN pr=$n reason=gh-json-unparsable"); $counts.unknown++; continue }
        $parsed = $null
        try { $parsed = ConvertFrom-Json -InputObject $ga.out -NoEnumerate } catch { $out.Add("UNKNOWN pr=$n reason=gh-json-unparsable"); $counts.unknown++; continue }
        $fl = Flatten-CommentPages $parsed
        if (-not $fl.ok) { $out.Add("UNKNOWN pr=$n reason=comments-unparsable$hp"); $counts.unknown++; continue }
        $comments = NormalizeComments @($fl.rows)
    }
    # COS-SCAN-REVIEW-SURFACE-ASK-1 (sol SG-11): owner asks can sit in a review body, an inline review comment or a review-thread reply.
    # Fixture / GhDataDir seam: <N>.reviews.json (missing = none; text FAIL = a failed read). Live: two REST reads, any failure is UNKNOWN.
    $reviews = @()
    $revText = $null; $revFail = ''
    if ($FixtureDir) {
        $rvf = Join-Path $FixtureDir "$n.reviews.json"; if (Test-Path -LiteralPath $rvf) { $revText = @([IO.File]::ReadAllText($rvf, [Text.Encoding]::UTF8)) }
    } elseif ($GhDataDir) {
        $rvf = Join-Path $GhDataDir "$n.reviews.json"; if (Test-Path -LiteralPath $rvf) { $revText = @([IO.File]::ReadAllText($rvf, [Text.Encoding]::UTF8)) }
    } else {
        # sol SG-12: REST review objects carry no edit time, so reviews come from GraphQL (lastEditedAt). One page of 100; a longer list is UNKNOWN.
        #   was: $gr = Exec 'gh' @('api', "repos/$Repo/pulls/$n/reviews", '--paginate', '--slurp') $GhTimeoutSec
        $own, $nm = $Repo -split '/', 2
        $q = 'query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviews(first:100){pageInfo{hasNextPage} nodes{databaseId author{login} state submittedAt lastEditedAt body}}}}}'
        $gr = Exec $GhExe @('api', 'graphql', '-f', "query=$q", '-F', "o=$own", '-F', "r=$nm", '-F', "n=$n") $GhTimeoutSec
        $gc = if ($gr.ok) { Exec $GhExe @('api', "repos/$Repo/pulls/$n/comments", '--paginate', '--slurp') $GhTimeoutSec } else { $gr }
        if (-not $gr.ok -or -not $gc.ok) { $revFail = (([string]$gr.err + ' ' + [string]$gc.err).Trim() -replace '\s+', ' ') } else { $revText = @($gr.out, $gc.out) }
        # sol gate key r16 SG-17: a failed read is decided by .ok, never by its diagnostic text. A read that exits non-zero with empty stderr left $revFail
        # empty, so the reviews were treated as read (none) and an owner review / inline ask became NOASK.
        if ((-not $gr.ok -or -not $gc.ok) -and -not $revFail) { $revFail = 'no-diagnostic (non-zero exit, empty stderr)' }
    }
    if ($revFail) { $out.Add("UNKNOWN pr=$n reason=gh-failed reviews $revFail"); $counts.unknown++; continue }
    $revBad = $false
    foreach ($t in @($revText)) {
        if ($null -eq $t) { continue }
        if (([string]$t).Trim() -eq 'FAIL') { $revBad = $true; break }
        $pv = $null
        try { $pv = ConvertFrom-Json -InputObject $t -NoEnumerate } catch { $revBad = $true; break }
        $rows = @()
        if ($pv -is [array] -and @($pv | Where-Object { $_ -is [array] }).Count -gt 0) { $fr = Flatten-CommentPages $pv; if (-not $fr.ok) { $revBad = $true; break }; $rows = @($fr.rows) }
        elseif ($pv -is [array]) { $rows = @($pv) }
        elseif ($pv.PSObject.Properties['data']) {
            # sol SG-12: the GraphQL reviews answer. hasNextPage (a truncated list) or a missing reviews node is unparsable, never ANSWERED.
            $rvNode = $null
            try { $rvNode = $pv.data.repository.pullRequest.reviews } catch { $rvNode = $null }
            if ($null -eq $rvNode -or $null -eq $rvNode.pageInfo -or [bool]$rvNode.pageInfo.hasNextPage) { $revBad = $true; break }
            foreach ($nd in @($rvNode.nodes)) {
                if ($null -eq $nd) { continue }
                $login = if ($nd.author) { [string]$nd.author.login } else { '' }
                $edited = if ($nd.lastEditedAt) { $nd.lastEditedAt } else { $nd.submittedAt }
                $row = [pscustomobject]@{ id = $nd.databaseId; user = [pscustomobject]@{ login = $login }; state = [string]$nd.state; body = [string]$nd.body }
                if ($nd.submittedAt) { $row | Add-Member -NotePropertyName submitted_at -NotePropertyValue $nd.submittedAt }
                if ($edited) { $row | Add-Member -NotePropertyName updated_at -NotePropertyValue $edited }
                $rows += $row
            }
        }
        else { $revBad = $true; break }
        $reviews += @(Normalize-ReviewRows $rows)
    }
    if ($revBad) { $out.Add("UNKNOWN pr=$n reason=comments-unparsable$hp"); $counts.unknown++; continue }
    if (@($reviews | Where-Object { $_.bad }).Count -gt 0) { $out.Add("UNKNOWN pr=$n reason=comments-unparsable$hp"); $counts.unknown++; continue }
    # COS-SCAN-EDITED-COMMENT-ASK-1 (sol SG-10): a comment row with no created time or an unparsable updated time is UNKNOWN (it used to be dropped silently).
    if ($null -ne $comments -and @($comments | Where-Object { $_.bad }).Count -gt 0) { $out.Add("UNKNOWN pr=$n reason=comments-unparsable$hp"); $counts.unknown++; continue }
    # MERGE-GATE-TIP-LEGS-1: a CLOSED / MERGED PR carrying an ask is scanned like an OPEN one (post-merge asks on #448 #451 #454 were invisible when this row was SKIP).
    # The state is its own trailing field, written only when the PR is not OPEN (absent = OPEN), so the OPEN row shape is unchanged for existing readers.
    if ($state -notin @('OPEN', 'CLOSED', 'MERGED')) { $out.Add("UNKNOWN pr=$n reason=pr-state-unrecognised state=$state"); $counts.unknown++; continue }
    $stateField = if ($state -ne 'OPEN') { " state=$state" } else { '' }
    # MERGE-GATE-TIP-LEGS-1 r2: on a CLOSED / MERGED PR the verdict alone is never an ask (a merged PR's 'merged' / 'merge-ready' / 'info' verdict made 11 false UNANSWERED rows at 14:28Z);
    # only an explicit ACK request in the text is (HasAskText -ExplicitOnly: 'ACK required', 'ACK: <plan', 'starting with ACK'; the loose 'ACK and DISTINGUISH on one line' rule is OPEN-only).
    # sol gate key r15 SG-16: the two early returns below skipped the owner comment / review asks on a CLOSED / MERGED PR and the first dropped state=,
    # so a PR read as CLOSED and reopened later passed the gate with an unanswered owner ask. Kept as comments; now the bus file is an ask on a non-OPEN PR only
    # through an explicit request (as before), and the flow continues to the owner comment / review reads, whose NOASK row carries state= too.
    #   if (-not $askCand -and $state -ne 'OPEN') { $out.Add("NOASK pr=$n verdict=$verdict"); continue }   (old)
    #   if ($state -ne 'OPEN' -and -not $explicit) { $out.Add("NOASK pr=$n verdict=$verdict$stateField"); continue }   (old)
    if ($state -ne 'OPEN') { $askCand = [bool]$explicit }

    # COS-SCAN-LATEST-ASK-1 r1: the ask time is the NEWEST ask, not the first. Candidates, newest wins: the bus header time (ask_src=header, or bus-commit when the header has no reviewed_utc), a stamped
    # section of the bus file that holds an ACK request (section), an owner-account PR comment newer than that which holds one (comment). On a CLOSED / MERGED PR only explicit requests count in all three.
    $explicitOnly = ($state -ne 'OPEN')
    $askUtc = $p.when; $askSrc = if ($p.src -eq 'bus-commit') { 'bus-commit' } else { 'header' }
    $stamps = Get-StampedAskTimes $p.body $explicitOnly
    foreach ($st in $stamps) { if ($st -gt $askUtc) { $askUtc = $st; $askSrc = 'section' } }
    $ownComments = @($comments | Where-Object { $null -ne $_.created -and ([string]$_.author).Trim() -ieq $OwnerLogin } | Sort-Object { $_.created })
    foreach ($c in $ownComments) {
        # a comment that itself starts with ACK: / DISTINGUISH: is an answer, never an ask, even when it quotes 'ACK required'
        # SG-13: with no ask in the bus file ($askCand false) the green header time is no floor, so the first owner ask found counts at its own time.
        $cFloorOff = (-not $askCand -and $askSrc -notin @('comment', 'review'))
        if (($cFloorOff -or $c.updated -gt $askUtc) -and $c.body -notmatch '^\s*(ACK|DISTINGUISH):' -and (HasAskText $c.body -ExplicitOnly:$explicitOnly)) { $askUtc = $c.updated; $askSrc = 'comment' }   # SG-10: dated by the newest edit (was created)
    }
    # COS-SCAN-REVIEW-SURFACE-ASK-1 (sol SG-11): an owner review body / inline review comment / thread reply holding an ACK request is an
    # ask dated by its newest edit. Reviews never answer (answers are issue comments only, strict), so `ACK:` in a review is not an answer.
    foreach ($rv in @($reviews | Where-Object { $null -ne $_.created -and ([string]$_.author).Trim() -ieq $OwnerLogin })) {
        $rFloorOff = (-not $askCand -and $askSrc -notin @('comment', 'review'))   # SG-13: the green header time is no floor
        if (($rFloorOff -or $rv.updated -gt $askUtc) -and (HasAskText $rv.body -ExplicitOnly:$explicitOnly)) { $askUtc = $rv.updated; $askSrc = 'review' }
    }
    # COS-SCAN-UNDATED-ASK-1 r1b (sol SG-08): one more candidate, the newest bus commit that changed this file, for a follow-up ask under an UNSTAMPED heading. Only a file whose text holds an ACK request is read
    # (a header-time commit of the same file has no ask to date), only when the header did not already fall back to the commit time, and not when a stamped ask section already dates the file at or after that commit.
    # A commit time that cannot be read is UNKNOWN: an unread file never reads as ANSWERED.
    if ($p.src -ne 'bus-commit' -and (HasAskText $e.text -ExplicitOnly:$explicitOnly)) {
        $lc = Get-FeedbackLatestCommitTime $e
        if ($lc.state -eq 'unreadable') { $out.Add("UNKNOWN pr=$n reason=latest-commit-time-unreadable$hp"); $counts.unknown++; continue }
        if ($lc.state -eq 'ok' -and $lc.when -gt $askUtc -and @($stamps | Where-Object { $_ -ge $lc.when }).Count -eq 0) { $askUtc = $lc.when; $askSrc = 'bus-commit-latest' }
    }
    # green header, no ask text: the row stays NOASK unless an owner comment turned out to be the ask
    if (-not $askCand -and $askSrc -notin @('comment', 'review')) { $out.Add("NOASK pr=$n verdict=$verdict$stateField"); continue }   # SG-11: a review ask counts too; SG-16: state= kept on a CLOSED / MERGED PR
    # sol gate key r8 SG-09 (2026-10-16T22:15Z): EVERY ask source is now strict. An ACK in the same second as the ask cannot be shown
    # to have read it (a new ask committed within that second kept the old ANSWERED row), so an ACK must be strictly newer than the ask.
    $strictOld = ($askSrc -ne 'header' -and $askSrc -ne 'bus-commit')
    $strict = $true   # was $strictOld: a section / comment / newest-commit ask is answered only by something strictly NEWER; the header time kept the original 'ator after'
    $hit = $null; $kind = ''
    # MERGE-GATE-TIP-LEGS-1 r3 (sol MG-R2-05, fable MG-R2-06): only the OWNER account (the hub posts under it) can answer; an outside commenter's 'ACK:' discharges nothing.
    foreach ($c in $ownComments) {
        if (-not (($strict -and $c.created -gt $askUtc) -or (-not $strict -and $c.created -ge $askUtc))) { continue }
        $mm = [regex]::Match($c.body, '^\s*(ACK|DISTINGUISH):')
        if ($mm.Success) { $hit = $c; $kind = $mm.Groups[1].Value; break }
    }
    if ($hit) {
        $out.Add("ANSWERED pr=$n by=$($hit.id) $(Fmt $hit.created) author=$($hit.author) kind=$kind$stateField ask_src=$askSrc"); $counts.answered++
    } else {
        $out.Add("UNANSWERED pr=$n head=$head8 ask_utc=$(Fmt $askUtc) deadline=$(Fmt $askUtc.AddHours(24)) verdict=$verdict$stateField ask_src=$askSrc"); $counts.unanswered++
    }
}

# ---------------------------------------------------------------- emit
$scan = "SCAN bus_ref=$refLabel bus_commit=$busCommit committed=$busCommitted at=$(Fmt $now) files=$($files.Count)"
$final = "COS_OPEN_ASKS unanswered=$($counts.unanswered) answered=$($counts.answered) skipped=$($counts.skipped) unknown=$($counts.unknown)"
$exit = if ($counts.unknown -gt 0) { 20 } elseif ($counts.unanswered -gt 0) { 10 } else { 0 }
$all = @($scan) + @($out) + @($final)
$all | ForEach-Object { Write-Output $_ }

if ($OutFile) {
    try {
        $dir = Split-Path -Parent $OutFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $md = [Collections.Generic.List[string]]::new()
        $md.Add('# CoS open asks (cos-open-ask-scan.ps1)')
        $md.Add('')
        $md.Add("generated_utc: $(Fmt $now)")
        $md.Add('stale_after: 30 minutes from generated_utc; an older file is UNKNOWN, not "unchanged"')
        $md.Add("bus_ref: $refLabel (commit $busCommit, committed $busCommitted)")
        $md.Add("repo: $Repo")
        $md.Add("exit: $exit (0 none unanswered, 10 at least one UNANSWERED, 20 UNKNOWN: scan blind)")
        $md.Add('')
        $md.Add('```text')
        foreach ($l in $all) { $md.Add($l) }
        $md.Add('```')
        # LF endings: line-anchored matches ('^key: v$') by the hub and by the tests must work on this view.
        [IO.File]::WriteAllText($OutFile, (($md -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
    } catch { Write-Output "UNKNOWN pr=- reason=outfile-write-failed $($_.Exception.Message)"; if ($exit -eq 0) { $exit = 20 } }
}
exit $exit
