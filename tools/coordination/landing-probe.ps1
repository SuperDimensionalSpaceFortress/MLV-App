# landing-probe.ps1 - ONE definition of "did this card land", dot-sourced by every tool that asks.
#
# WHY THIS IS A SHARED FILE AND NOT A COPY IN EACH CALLER.
# Two tools ask this question: Invoke-Workstream.ps1 (to avoid dispatching a lane on finished
# work) and queue-derive.ps1 (to refuse a typed queue state that disagrees with raw). They must
# not answer it differently. The vocabulary below has ALREADY been widened twice from measured
# misses; a second copy would have been widened once and gone stale, which is this board's most
# frequently paid failure.
#
# WHAT COUNTS, AND WHY THE ASYMMETRY IS LOAD-BEARING.
#   TITLE, behind a landing verb  -> STRONG. "...(lands SIDECAR-FIX-1)".
#   BODY, behind a landing verb   -> STRONG. "Closes OWN-2 and delivers GATE-RESIDUALS-1(b)".
#   BARE MENTION, either place    -> NEAR-MISS. Reported with the PR cited, and NOT treated as a
#                                    landing. A false positive SKIPS genuinely open work, which is
#                                    strictly worse than re-deriving finished work.
#
# THE TITLE IS VERB-GATED TOO, AND THAT WAS A CORRECTION. The title match was originally
# unconditional on the theory that "a maintainer wrote the card id into the merge subject" implies
# a landing. MEASURED 2026-09-05, that theory is false: PR #41 is titled "coordination: reject a
# --timestamp that is not a real instant (ADDRESSES STAMP-APPENDER-1)", and its body says in as
# many words -- "this PR says addresses, not lands: a false landing signal would mark the card done
# and skip remaining work." The author deliberately signalled NOT-LANDED and the probe overrode
# them, skipping an open card. Gating the title on the same verb list costs NOTHING: across all 51
# merged PRs and all 117 queue ids there are exactly three title matches, and the two real landings
# (#23, #38) both read "lands". Widening a match is a decision; this narrows one, which is the safe
# direction of the asymmetry above.
#
# FAIL-OPEN BY CONSTRUCTION. If gh is missing, unauthenticated, offline or slow, this returns
# status 'cannot-determine: <reason>' with NO cards matched. An unreadable signal must never
# silently shrink the board - that is how a queue goes quiet and looks healthy.

# NO Set-StrictMode HERE, DELIBERATELY. This file is DOT-SOURCED, so anything it sets lands in the
# CALLER's scope, not its own. An earlier draft set `-Version Latest` and instantly broke
# queue-derive.ps1, which reads optional queue fields (`$item.priority`) that are legitimately
# absent on many cards -- under Latest that is a terminating error, so a shared helper silently
# changed the semantics of an unrelated script that merely consulted it. A library imposes no
# strictness on its callers; Invoke-Workstream.ps1 sets its own, before dot-sourcing this.

. (Join-Path $PSScriptRoot 'repo-slug.ps1')

# The landing vocabulary. ONE definition; both the title and body matchers consume it, so they can
# never drift apart either. Tolerates markdown emphasis around the id, and the optional 'card' /
# 'queue item' connector that real PR bodies use ("Closes queue item OWN-1-PRECEDENCE", PR #52).
$script:LandingVerbs = 'lands|closes|fixes|resolves|delivers'

function Get-LandingRegex {
    <#
    .SYNOPSIS
        The verb-gated landing regex for one card id, used against a PR title OR body.
    #>
    param([Parameter(Mandatory)][string]$CardId)
    return '(?i)(?:' + $script:LandingVerbs + ')\s+(?:(?:card|queue\s+item)\s+)?[*_`]*' +
           [regex]::Escape($CardId) + '[*_`]*(?![A-Za-z0-9-])'
}

function Get-MentionRegex {
    <#
    .SYNOPSIS
        A bare mention of the id, with no landing verb. Word-boundary on the trailing side so
        C2-SUBMIT-2 never matches C2-SUBMIT-22.
    #>
    param([Parameter(Mandatory)][string]$CardId)
    return '(?i)(?<![A-Za-z0-9-])[*_`]*' + [regex]::Escape($CardId) + '[*_`]*(?![A-Za-z0-9-])'
}

function Get-CardLandingEvidence {
    <#
    .SYNOPSIS
        Ask GitHub which of $CardId landed in a merged PR. Never reads queue.json, never writes.

    .OUTPUTS
        [pscustomobject] with:
          status    - 'ok: <n> merged PR(s) scanned, <m> matched' or 'cannot-determine: <reason>'
          landed    - hashtable id -> @{ number; title; how = 'title'|'body' }
          nearMiss  - string[] of 'ID(PR #n)', reported and NOT acted on
          scanned   - count of merged PRs examined (0 when cannot-determine)
    #>
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$CardId,
        [string]$Repo = (Get-MlvRepoSlug),
        [int]$Limit = 100
    )

    $landed = @{}
    $nearMiss = @()

    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $gh) {
        return [pscustomobject]@{
            status = 'cannot-determine: gh not on PATH'; landed = $landed; nearMiss = $nearMiss; scanned = 0
        }
    }

    try {
        $raw = & gh pr list --repo $Repo --state merged --limit $Limit --json number,title,body 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) {
            return [pscustomobject]@{
                status = "cannot-determine: gh exited $LASTEXITCODE"; landed = $landed; nearMiss = $nearMiss; scanned = 0
            }
        }
        $prs = @($raw | ConvertFrom-Json)
    } catch {
        return [pscustomobject]@{
            status = "cannot-determine: $($_.Exception.Message)"; landed = $landed; nearMiss = $nearMiss; scanned = 0
        }
    }

    foreach ($id in $CardId) {
        if (-not $id) { continue }
        $rxLanding = Get-LandingRegex $id
        $rxMention = Get-MentionRegex $id

        $hit = @($prs | Where-Object { $_.title -and ($_.title -match $rxLanding) }) | Select-Object -First 1
        $how = 'title'
        if (-not $hit) {
            $hit = @($prs | Where-Object { $_.body -and ($_.body -match $rxLanding) }) | Select-Object -First 1
            if ($hit) { $how = 'body' }
        }
        if ($hit) {
            $landed[$id] = @{ number = $hit.number; title = $hit.title; how = $how }
            continue
        }
        # NEAR-MISS. A merged PR naming this card WITHOUT a landing verb is either prose ('unlike
        # OWN-GITHUB-1', correctly ignored) or a landing phrased in a verb this probe does not
        # know. Both were silent before; the second cost a dispatch. REPORT and cite the PR - do
        # NOT act on it. COLLECTED, never printed per card: the first draft of this emitted one
        # line per near-miss and produced TEN on a single run, and a diagnostic that fires every
        # run is one nobody reads, which is the failure it was meant to prevent.
        $near = @($prs | Where-Object {
            ($_.title -and ($_.title -match $rxMention)) -or ($_.body -and ($_.body -match $rxMention))
        }) | Select-Object -First 1
        if ($near) { $nearMiss += "$id(PR #$($near.number))" }
    }

    return [pscustomobject]@{
        status   = "ok: $($prs.Count) merged PR(s) scanned, $($landed.Count) card(s) matched by a landing verb in the title or body"
        landed   = $landed
        nearMiss = $nearMiss
        scanned  = $prs.Count
    }
}
