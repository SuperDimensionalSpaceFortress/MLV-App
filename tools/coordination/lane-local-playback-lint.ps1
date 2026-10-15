# lane-local-playback-lint.ps1 -- HUB-VM-NO-LOCAL-PLAYBACK-1 (owner objection 2026-10-11). Dot-source it; it defines three functions and runs nothing.
#
#   Get-HubHostConfig                       the tracked host list (tools/profiling/dual-venue/hub-hosts.json)
#   Test-HubHost [-ComputerName <name>]     $true when this host (COMPUTERNAME, else the runtime host name) is a hub host and the CI-fixture override is NOT set
#   Get-LocalPlaybackPromptFindings -Text   one string per sentence that tells a lane to run MLVApp playback locally (wrap the call in @())
#
# Why: the 2026-10-11 incident. A producer prompt said to run the exactness arms "where a GUI desktop session and the clip exist"; the lane found
# a copy of the clip on the hub VM and launched MLVApp.exe gui-smoke-playback there. The hub VM is a guest on Ultra-Magnus, so the window appeared
# on the owner's machine and the run contaminated the Ultra-Magnus venue legs. The app refuses that entry on a hub host (platform/qt/HubHostPlaybackGuard.h);
# this lint stops the INSTRUCTION earlier: Invoke-Lane.ps1 refuses a lane on a hub host whose prompt asks for local playback, and
# .claude-state\hub-autopilot\dispatch-perf-lane.ps1 refuses such a producer prompt before any lane is created.
#
# The binding "NO LOCAL PLAYBACK" section the dispatch scripts append is excluded from the scan (it names the forbidden flags on purpose), and a
# clause that is itself a prohibition (never / do not / must not / forbidden / refuse ...) is not an instruction. A clause that routes the work
# through a venue (Bachelor, Ultra-Magnus, the venue chain) is not a local launch -- in exactly the shapes enumerated below.
#
# Scope (HUB-VM-LINT-CLAUSE-SCOPE-1): a negation or a venue word exempts only the CLAUSE it sits in, never a later clause of the same sentence
# ("Do not run the venue chain, run playback locally" is refused; "Run playback on this host after the venue job finishes" is refused). Within
# a clause the venue qualification is read before the local-playback match, so "run playback locally on Bachelor through the venue chain" passes.
# A clause with no launch verb of its own ("... locally and on this host") carries the negation of the clause before it, never its venue words.
#
# Fail closed (HUB-VM-LINT-CLAUSE-SCOPE-1 r3). A clause that holds a local-playback marker (locally, on this host / the hub, here, a direct
# launch) is REFUSED unless it is a prohibition (its own never / do not / ..., or the prohibition it is coordinated with) or it matches one
# of the ENUMERATED affirmative shapes below. A venue word or a contrast word somewhere in the clause is NOT an exemption: r1b and r2 each
# read polarity from a word list and each list missed wordings master refuses ("locally since Bachelor is busy", "without audio locally",
# "not only on Bachelor but also locally" ...). Extend a SHAPE, in review, with the catalogue row that asks for it -- never a word list:
#   E1 venue-routed marker (rule 2, `locally` only): the marker is immediately followed by a routing preposition (on / via / through /
#      using) and a venue word inside the same clause -- "Run playback locally on Bachelor through the venue chain." Row: the probe of the
#      Sol review of PR #401 (CLAUSE_MUST_PASS in tools/repo_hygiene/test_hub_vm_no_local_playback.py).
#   E2 negated marker (rules 2, 3, 3b, 4): the marker opens a contrast complement, i.e. it is the first thing after without / not / , not /
#      , never / nor / instead of / rather than / skip / bypass -- "Run playback on Bachelor rather than locally.", "... through the venue
#      chain, not on this host." Rows: those two CLAUSE_MUST_PASS entries. A complement that merely CONTAINS the marker later ("without
#      audio locally") is not negated.
#   E3 venue-routed clause (rules 3, 3b, 4): a venue word exempts the marker's clause only inside a routing phrase (on / via / through /
#      using / at, an optional article, then the venue word) -- "The venue job calls <launcher> -Backend cpu on Bachelor." Row: that green
#      prompt in PromptLintTests.test_green_prompts_that_must_pass. "since Bachelor is busy" is not a routing phrase.
# Locus and defeaters (r4). E1 and E3 read a venue word only as the LOCUS of execution: the routing preposition's object is the venue itself
# and what follows it is the end of the clause, punctuation or a function word. "using Bachelor's clip", "on the Bachelor clip" and
# "through the venue chain output" name a venue as an attribute, so they route nothing. A local-host marker (on this host / the hub,
# a bare "here" that is not "from here") in the same clause, or in the verbless clauses coordinated after it, DEFEATS E1, E2 and E3: a
# clause that names both a venue and the local host is refused ("locally on Bachelor, on this host"). Every shape has defeater rows in
# DEFEATER_AXIS (tools/repo_hygiene/test_hub_vm_no_local_playback.py); a new shape is admitted only with its defeater rows.
# Closed lists (r5). What may follow the venue name is a LOCUS-PRESERVING word only (another routing preposition or a coordinator, or the
# phrase "from here"): never a purpose / instrument / source complement (to, for, as, that, which, with, so, only, from ...), which makes
# the venue a tool, a source or a producer ("using Bachelor to copy the clip to this host", "using the venue chain that produced the clip").
# A comma continues the clause and what follows it must again be the end or such a word. A defeater is ANY reference to the local machine
# ("<this|the|my|your|our|that|local> <hub|host|vm|machine|box|...>" in any context, localhost, virtual-ten, a bare here). Every word
# list lives in Get-LocalPlaybackLintWordLists, and ListMemberDefeaterTests generates one defeater row PER MEMBER of each, so a word added
# to a list is tested by construction (ruling candidate mlv-safety-lint-exemption-defeater-corpus-r1 with the CoS amendment).
# Fail closed (r6). r5 still had to guess which later clause mattered (one that "refers back to the playback"), and each guess was a new
# hole. Now ANY later clause of the same sentence that names the local host defeats the earlier venue exemption, whatever its verb or its
# object is: "locally on Bachelor, then play the clip here", "...; run it on this host", "... and on the hub-VM". A semicolon is a clause
# boundary like a comma (it used to end the sentence, so the follow-up was never read together with the venue clause). The host
# vocabulary is tolerant: a determiner, up to two modifier words, a noun that may carry -vm (this same host, our own box, the hub-VM),
# a bare preposition + noun (on hub), spelling variants (hub-VM = hub vm = hubvm), names (localhost, 127.0.0.1, ::1) and a later bare
# `locally`. Accepted over-refusal: a harmless later clause that merely names the local host ("..., then build the app here") refuses --
# a safety lint reads the sentence, not the intent; a negated clause ("..., not on this host") is skipped, not a stop.
# Locus-only host reference on a direct launch line (r7). Rule 4 has no `locally`, so a host noun there is either WHERE the work runs
# ("... on Bachelor and on my machine", "... on Bachelor using this host's GPU") or a destination ("... on Bachelor and posts the result
# to the hub"). Only the first defeats the venue exemption: a locus preposition (HostLocusPrepositions: on / at / in / using) + determiner
# + up to two words + host noun (or the bare noun), or a host name (localhost, virtual-ten, 127.0.0.1, hub-vm in every spelling). to /
# into / from + a host noun is a destination and does not. A purpose or destination complement after the venue name itself ("via
# Bachelor to this host") is not a locus (r5 closed follow list), so that line is refused as before -- a stated, fail-closed difference.
# Sentence-terminal E1 (r8; CoS fix option (i), the 12:27Z ACK wording). Six rounds of host-reference detection each left the next family
# open (a later clause hidden by a negation tail, a host noun nobody listed, a follow-up sentence), because a detector can never list
# every way to name the local machine. E1 therefore no longer hunts for host references: `locally <prep> <venue>` exempts ONLY when it
# is the END of what the text says about running. Everything after the venue phrase -- the rest of the sentence and every later sentence
# of the same instruction block (a block ends at a heading) -- must be (1) more venue routing, (2) a pure locus word ("from here"),
# (3) a negation that governs a host reference ("not here", "never on this host", "do not run it here") or (4) a handling clause (a
# handling verb and its object: "then read the log", "write the summary to the run dir"; no launch verb, no locus preposition, no host
# reference). Anything else voids E1: a launch verb whatever its object or any qualifier ("..., then run it here with no overlay"), a
# verbless locus ("... and on this rig"), a follow-up sentence ("... Then play it here."), a clause with an unlisted verb. The word lists
# only ADMIT (TailHandlingVerbs, TailPureLocus, TailNegations ...); a word nobody listed is refused, so the lists fail closed. Accepted
# over-refusals, listed in the PR body: any later clause that is not one of the four shapes, e.g. "..., then archive the log.",
# "... This produces the receipts.", "..., then read the log in the run dir." (a locus preposition in a handling clause), and a venue
# phrase followed by a comma and an unlisted word (the r5 closed follow list). A strong negation that comes AFTER the marker in its clause
# ("..., don't wait, then run it here") no longer exempts the clause: a prohibition governs the marker only when it precedes it or is
# inherited from an earlier clause. Rules 3, 3b and 4 are unchanged by r8.
# Strictly terminal E1 (r10; hub ruling HUB-TICK 20261015T1650Z). r8's shape (4), the handling clause, and its "more venue routing"
# with any lead each decided a tail by ENUMERATING verbs, and every round left the next phrase family open: an adverb moved a launch
# verb out of the pattern ("then read the log to quietly replay it"), a destination preposition after a launch lead read as routing
# ("then play the clip from the venue chain"). r10 stops enumerating. The tail (the rest of the sentence and every later sentence of
# the instruction block; a block ends at a heading) must match a CLOSED, VERBLESS allow-grammar, read by a whitelist tokenizer
# (Test-VenueTerminalTail). A fragment (between , ; : . ! ?, a spaced dash, and / or / then) is exactly one of:
#   (A) venue routing: a locus preposition (TailLocusPrepositions: on via through using at), an optional article, a venue name, chained
#       ("through the venue chain", "on Bachelor via the chain runner"), optionally led or followed by pure locus words;
#   (B) one pure locus word or phrase (TailPureLocus: "from here", "afterwards" ...);
#   (C) a verbless host negation: TailNegations (not never nor) + a host target ("not here", "never on this host", "not locally").
# Any other token -- a verb, an unlisted noun or adverb, to / into / from / for, a quote or a bracket -- voids E1 and the text REFUSES.
# Accepted over-refusal (ruling 20261015T1650Z, listed in the PR body): a harmless follow-up in the same block ("..., then post the
# result to Bachelor.", "...; then run the report on Bachelor.", ". Read the log.") now refuses; put it in its own section.
# Rules 3, 3b and 4 keep master's weak-negation exemption (not / no / without in the marker's clause, narrowed from the whole sentence to
# the clause), so they refuse at least what master refuses. Anything that is not E1 / E2 / E3 / a prohibition keeps master's verdict.
# A contrast word still CUTS the sentence (the head clause is judged without its complement) and the complement ends at its next comma; the
# clause after that comma inherits from the HEAD clause, so a leading "Never run playback ..." still reaches "..., locally or on this host".
#
# ASCII only (PowerShell 5.1 reads a BOM-less file as the ANSI code page). No Set-StrictMode here: a dot-sourced file would leak it into the caller.

function Get-HubHostConfig {
    [CmdletBinding()]
    param([string]$Path = (Join-Path $PSScriptRoot '..\profiling\dual-venue\hub-hosts.json'))
    if (-not (Test-Path -LiteralPath $Path)) { throw "hub-hosts.json not found: $Path" }
    $cfg = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($cfg.schema -ne 'mlv-app/hub-hosts/v1') { throw "hub-hosts.json: unexpected schema '$($cfg.schema)'" }
    if (@($cfg.hubHosts).Count -lt 1) { throw 'hub-hosts.json: hubHosts is empty' }
    return $cfg
}

function Test-HubHost {
    [CmdletBinding()]
    # COMPUTERNAME is Windows-only; pwsh on Linux/macOS leaves it unset, so fall back to the runtime's own host name there.
    param([string]$ComputerName = $(if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { [Environment]::MachineName }), [string]$HostsFile)
    $cfg = if ($HostsFile) { Get-HubHostConfig -Path $HostsFile } else { Get-HubHostConfig }
    $overrideNow = [Environment]::GetEnvironmentVariable([string]$cfg.ciFixtureOverrideEnv)
    # Exact and untrimmed, the same compare as the app header and the launcher guards (-cne): a padded value is not the override.
    if ($overrideNow -ceq [string]$cfg.ciFixtureOverrideValue) { return $false }
    $name = ([string]$ComputerName).Trim()
    if (-not $name) { return $false }
    foreach ($h in @($cfg.hubHosts)) { if ($name -ieq [string]$h) { return $true } }
    return $false
}

# Drop every "## NO LOCAL PLAYBACK ..." section (heading to the next heading of the same or a higher level, or the end of the text).
function Remove-NoLocalPlaybackSection {
    param([string]$Text)
    $out = New-Object System.Collections.Generic.List[string]
    $skipLevel = 0
    foreach ($line in ($Text -split "\r?\n")) {
        if ($line -match '^(#{1,6})\s+(.*)$') {
            $level = $Matches[1].Length
            if ($skipLevel -gt 0 -and $level -le $skipLevel) { $skipLevel = 0 }
            if ($skipLevel -eq 0 -and $Matches[2] -match '^NO LOCAL PLAYBACK\b') { $skipLevel = $level; continue }
        }
        if ($skipLevel -eq 0) { $out.Add($line) }
    }
    return ($out -join "`n")
}

# One sentence cut into clauses. A boundary is a connector that opens a new predicate: ", run ...", " and ", " then ", " after ", " but ", " - ".
# Each clause records the negation / venue words it holds itself; a clause with no launch verb of its own is a coordinated complement and
# inherits the negation (not the venue qualification) of the clause before it. A contrast word ("without", ", not", "instead of" ...) cuts
# too: the complement after it is Contrast = never Venue, and it runs only to its next comma. The clause after that comma (NoInherit) does
# not inherit from the complement; it inherits from the HEAD clause, the one the contrast word cut. "or" cuts too, and the clause after it
# inherits the negation of the clause before it even when it has a verb ("NO GUI or playback run ..." is one prohibition).
function Get-LintClauses {
    param([string]$Sentence, [string]$Separator, [string]$Contrast, [string]$LaunchVerb, [string]$StrongNeg, [string]$WeakNeg, [string]$VenueQualified)
    $cuts = New-Object System.Collections.Generic.List[object]
    foreach ($m in [regex]::Matches($Sentence, $Separator)) { $cuts.Add([pscustomobject]@{ Index = $m.Index; Length = $m.Length; Contrast = $false; Disjunct = ($m.Value.Trim() -ieq 'or'); Hard = ($m.Value -ceq ';') }) }
    if ($Contrast) {
        foreach ($m in [regex]::Matches($Sentence, $Contrast)) { $cuts.Add([pscustomobject]@{ Index = $m.Index; Length = $m.Length; Contrast = $true; Disjunct = $false; Hard = $false }) }
    }
    $ordered = @($cuts | Sort-Object -Property @{ Expression = 'Index'; Descending = $false }, @{ Expression = 'Length'; Descending = $true })
    $spans = New-Object System.Collections.Generic.List[object]
    $pos = 0
    $pendingContrast = $false
    $pendingDisjunct = $false
    $pendingHard = $false
    foreach ($cut in $ordered) {
        if ($cut.Index -lt $pos) { continue }
        $spans.Add([pscustomobject]@{ Start = $pos; End = $cut.Index; Contrast = $pendingContrast; NoInherit = $false; Disjunct = $pendingDisjunct; Hard = $pendingHard })
        $pos = $cut.Index + $cut.Length
        $pendingContrast = [bool]$cut.Contrast
        $pendingDisjunct = [bool]$cut.Disjunct
        $pendingHard = [bool]$cut.Hard
    }
    $spans.Add([pscustomobject]@{ Start = $pos; End = $Sentence.Length; Contrast = $pendingContrast; NoInherit = $false; Disjunct = $pendingDisjunct; Hard = $pendingHard })
    # A contrast complement ends at its first comma; what follows is a fresh, non-inheriting clause.
    $flat = New-Object System.Collections.Generic.List[object]
    foreach ($sp in $spans) {
        if ($sp.Contrast -and $sp.End -gt $sp.Start) {
            $comma = $Sentence.IndexOf(',', $sp.Start, $sp.End - $sp.Start)
            if ($comma -ge 0) {
                $flat.Add([pscustomobject]@{ Start = $sp.Start; End = $comma; Contrast = $true; NoInherit = $false; Disjunct = $sp.Disjunct; Hard = $sp.Hard })
                $flat.Add([pscustomobject]@{ Start = $comma + 1; End = $sp.End; Contrast = $false; NoInherit = $true; Disjunct = $false; Hard = $false })
                continue
            }
        }
        $flat.Add($sp)
    }
    $clauses = New-Object System.Collections.Generic.List[object]
    $prev = $null
    $head = $null
    $group = 0
    $hardPending = $false
    foreach ($sp in $flat) {
        $start = [int]$sp.Start; $end = [int]$sp.End
        if ($sp.Hard) { $hardPending = $true }
        if ($end -le $start) { continue }
        $text = $Sentence.Substring($start, $end - $start)
        if (-not $text.Trim()) { continue }
        # A semicolon ends the instruction (it was a sentence boundary before r6): nothing is inherited across it and the playback test is
        # per group. It does NOT end the defeater scan, which reads on into every later clause of the sentence (Test-LocalHostInScope).
        if ($hardPending) { $prev = $null; $head = $null; $group++; $hardPending = $false }
        $hasVerb = [bool]($text -match $LaunchVerb)
        $strong = [bool]($text -match $StrongNeg)
        $weak = [bool]($text -match $WeakNeg)
        # A complement is judged on its own words: being the object of "without" negates nothing here (only the E2 shape exempts a marker).
        $from = if ($sp.NoInherit) { $head } else { $prev }
        if ((-not $hasVerb -or $sp.Disjunct) -and $from) { $strong = $strong -or $from.Strong; $weak = $weak -or $from.Weak }
        if ($sp.Contrast) { $head = $prev }
        $c = [pscustomobject]@{ Start = $start; End = $end; Group = $group; Text = $text; HasVerb = $hasVerb; Strong = $strong; Weak = $weak; Contrast = [bool]$sp.Contrast; Venue = (-not $sp.Contrast) -and [bool]($text -match $VenueQualified) }
        $clauses.Add($c)
        $prev = $c
    }
    if ($clauses.Count -eq 0) {
        $clauses.Add([pscustomobject]@{ Start = 0; End = $Sentence.Length; Group = 0; Text = $Sentence; HasVerb = [bool]($Sentence -match $LaunchVerb); Strong = [bool]($Sentence -match $StrongNeg); Weak = [bool]($Sentence -match $WeakNeg); Contrast = $false; Venue = [bool]($Sentence -match $VenueQualified) })
    }
    return $clauses.ToArray()
}

# The clause a match starting at $Index sits in (the last clause that starts at or before it).
function Get-ClauseAt {
    param([object[]]$Clauses, [int]$Index)
    $found = $Clauses[0]
    foreach ($c in $Clauses) { if ($c.Start -le $Index) { $found = $c } }
    return $found
}

# E2: the marker at $Index is the first thing of a contrast complement ("rather than locally", ", not on this host").
function Test-ContrastLead {
    param([string]$Sentence, [object]$Clause, [int]$Index)
    if (-not $Clause.Contrast -or $Index -lt $Clause.Start -or $Index -gt $Clause.End) { return $false }
    return [string]::IsNullOrWhiteSpace($Sentence.Substring($Clause.Start, $Index - $Clause.Start))
}

# E1: "locally" immediately followed by a routing preposition and a venue LOCUS (a venue word that is where the work runs, see
# $venueLocus in Get-LocalPlaybackPromptFindings), all inside the marker's own clause. Returns the index in the sentence where that
# phrase ENDS (what follows it is the tail Test-VenueTerminalTail reads), or -1 when the marker is not venue-routed.
function Get-VenueRoutedLocallyEnd {
    param([string]$Sentence, [object]$Clause, [int]$Index, [string]$VenueLocus, [string]$Prepositions)
    if ($Index -lt $Clause.Start -or $Index -ge $Clause.End) { return -1 }
    $tail = $Sentence.Substring($Index, $Clause.End - $Index)
    $m = [regex]::Match($tail, "(?i)^locally\s+(?:${Prepositions})\s+(?:(?:the|a|an)\s+)?${VenueLocus}")
    if (-not $m.Success) { return -1 }
    return $Index + $m.Length
}

# r10 -- strictly terminal E1 (hub ruling HUB-TICK 20261015T1650Z). One tail PIECE (the rest of the marker's sentence, or one later
# sentence of its block) is in the closed allow-grammar ($Grammar, built in Get-LocalPlaybackPromptFindings: fragments (A) venue routing,
# (B) a pure locus word, (C) a verbless host negation, separated by punctuation, a spaced dash or and / or / then). A whitelist, not a
# blacklist: the first token the grammar does not name fails the match. A piece that another piece of the block follows must end in
# punctuation, so a fragment never runs on across a line break (each piece is read on its own, which is never wider than reading them joined).
function Test-TailPiece {
    param([string]$Text, [bool]$Followed, [regex]$Grammar)
    if (-not $Grammar.IsMatch($Text)) { return $false }
    return (-not $Followed) -or ($Text.TrimEnd() -match '[.!?;:,]\z')
}

# The venue exemption holds only when the venue routing is the END of what the text says about running: the rest of the sentence
# ($Remainder) and every later item of the same block must be in the grammar. Later items are read through an index, never copied:
# $State.NextDirty[j] is the first item at or after j, in j's block, that is NOT in the grammar (-1: none). It is built once, lazily, in
# one backward pass, so a large heading-free prompt costs O(n) (r9 kept a copy of the rest of the block per item: O(n^2)).
function Test-VenueTerminalTail {
    param([string]$Remainder, [object]$Items, [int]$Index, [hashtable]$State)
    $n = $Items.Count
    if ($null -eq $State.NextDirty) {
        $nd = New-Object 'int[]' ($n + 1)
        $nd[$n] = -1
        for ($j = $n - 1; $j -ge 0; $j--) {
            $followedJ = ($j + 1 -lt $n) -and ($Items[$j + 1].Block -eq $Items[$j].Block)
            if (-not (Test-TailPiece -Text $Items[$j].Text -Followed $followedJ -Grammar $State.Grammar)) { $nd[$j] = $j }
            elseif ($followedJ) { $nd[$j] = $nd[$j + 1] }
            else { $nd[$j] = -1 }
        }
        $State.NextDirty = $nd
    }
    $followed = ($Index + 1 -lt $n) -and ($Items[$Index + 1].Block -eq $Items[$Index].Block)
    if (-not (Test-TailPiece -Text $Remainder -Followed $followed -Grammar $State.Grammar)) { return $false }
    if ($followed -and $State.NextDirty[$Index + 1] -ne -1) { return $false }
    return $true
}

# Defeater (r4, widened r5, fail closed r6): a local-host reference in the clause, or in ANY later clause of the same sentence (across a
# comma, a semicolon, a dash, then / and / but / so ...), that is not itself negated or the first thing of a contrast complement. A venue
# qualification never exempts a sentence that also names the local host later, whatever that clause's verb or object is: "locally on
# Bachelor, then run the clip here" and "locally on Bachelor; run the build here" both refuse (the second is an accepted over-refusal --
# a safety lint reads the sentence, not the intent). $HostPatterns are the reference patterns (the marker, a determiner + host noun in
# ANY context, a host name, a bare "here" that is not "from here", a later bare "locally"). A later clause that is a prohibition or a
# contrast complement is skipped, not a stop: what follows it is still scanned.
function Test-LocalHostInScope {
    param([string]$Sentence, [object[]]$Clauses, [object]$Clause, [string[]]$HostPatterns)
    $started = $false
    foreach ($k in $Clauses) {
        if (-not $started) {
            if ([object]::ReferenceEquals($k, $Clause)) { $started = $true } else { continue }
        }
        elseif ($k.Contrast -or $k.Strong -or $k.Weak) { continue }
        foreach ($pattern in $HostPatterns) {
            foreach ($m in [regex]::Matches($k.Text, $pattern)) {
                if (-not (Test-ContrastLead -Sentence $Sentence -Clause $k -Index ($k.Start + $m.Index))) { return $true }
            }
        }
    }
    return $false
}

# A clause is venue-qualified (E3) only when no local-host reference defeats it.
function Test-VenueExempts {
    param([string]$Sentence, [object[]]$Clauses, [object]$Clause, [string[]]$HostPatterns)
    if (-not $Clause.Venue) { return $false }
    return -not (Test-LocalHostInScope -Sentence $Sentence -Clauses $Clauses -Clause $Clause -HostPatterns $HostPatterns)
}

# The CLOSED word lists the exemptions depend on (r5). Every pattern in Get-LocalPlaybackPromptFindings is built from these lists, and
# tools/repo_hygiene/test_hub_vm_no_local_playback.py (ListMemberDefeaterTests) reads them back and generates one defeater row PER MEMBER,
# so a word added here is tested by construction. A word joins VenueFollowLocus only with a reason in LOCUS_FOLLOW_REASONS there.
#   VenueWords                  what names a venue.
#   RoutingPrepositionsLocally  the preposition that must follow `locally` for E1.   RoutingPrepositionsVenue  the prepositions of an E3 routing phrase.
#   VenueFollowLocus            what may follow the venue name and still leave it the PLACE the work runs: another routing preposition or a
#                               coordinator. Never a purpose / instrument / source complement (to for as that which with so only from ...).
#   VenueFollowPhrases          the same, as a phrase: "from here" says where a venue job is started from, not where it runs.
#   HostDeterminers / HostNouns / HostNames   a reference to the local machine, in ANY context: "<determiner> [up to two modifier words]
#                               <noun>" (this same host, the local box, our own machine), a bare "<preposition> <noun>" (on hub), or a name.
#                               A space in a noun also matches a hyphen or nothing (hub vm = hub-VM = HUBVM); a noun may be followed by -vm.
#   HostPrepositions            the prepositions that make a bare noun a reference ("on hub", "to host"; no determiner needed).
#   ContrastWords               the words that open a contrast complement (E2).
# (r5's BackReferences list is gone: a later clause no longer has to refer back to the playback to defeat the exemption -- see Test-LocalHostInScope.)
function Get-LocalPlaybackLintWordLists {
    return [ordered]@{
        VenueWords                 = @('venue', 'venues', 'bachelor', 'ultra-magnus', 'um-run', 'Invoke-VenueChain', 'Invoke-VenueLeg', 'Invoke-VenueChain.ps1', 'Invoke-VenueLeg.ps1', 'chain runner')
        RoutingPrepositionsLocally = @('on', 'via', 'through', 'using')
        RoutingPrepositionsVenue   = @('on', 'via', 'through', 'using', 'at')
        VenueFollowLocus           = @('on', 'via', 'through', 'using', 'at', 'and', 'or', 'but', 'then')
        VenueFollowPhrases         = @('from here')
        HostDeterminers            = @('this', 'the', 'my', 'your', 'our', 'that', 'local', 'current', 'same', 'own', 'its', 'their')
        HostNouns                  = @('hub vm', 'hub host', 'hub', 'host', 'vm', 'virtual machine', 'machine', 'box', 'workstation', 'computer', 'pc', 'desktop', 'laptop', 'system', 'device', 'server', 'node', 'guest')
        HostPrepositions           = @('on', 'onto', 'at', 'in', 'into', 'to', 'from', 'inside', 'within', 'via', 'using')
        HostLocusPrepositions      = @('on', 'at', 'in', 'using')
        HostNames                  = @('localhost', 'virtual-ten', 'hub vm', '127.0.0.1', '::1', 'loopback')
        ContrastWords             = @('instead of', 'rather than', 'without', 'nor', 'skip', 'skips', 'skipping', 'bypass', 'bypasses', 'bypassing')
        # r10 (strictly terminal E1, the closed allow-grammar of Test-VenueTerminalTail). The tokenizer admits ONLY these words (plus
        # VenueWords and, inside a host negation, HostDeterminers / HostNouns / HostNames / here / locally); any other token voids E1.
        # There is no verb list and no destination list: to / into / from / for are simply not in the grammar ("from here" is one locus phrase).
        TailLocusPrepositions      = @('on', 'via', 'through', 'using', 'at')
        TailArticles               = @('the', 'a', 'an')
        TailJoiners                = @('and', 'or', 'then')
        TailNegations              = @('not', 'never', 'nor')
        TailNegationPrepositions   = @('on', 'at')
        TailPureLocus              = @('from here', 'afterwards', 'afterward', 'later', 'finally', 'next', 'too', 'as well')
    }
}

# A regex alternation of literal words, longest first, a space in a word matching any run of white space.
function ConvertTo-LintAlternation {
    param([string[]]$Words, [switch]$Loose)
    $sorted = @($Words | Sort-Object -Property @{ Expression = { $_.Length }; Descending = $true })
    # -Loose (host vocabulary): a space also matches a hyphen or nothing, so "hub vm" is hub-VM, HUB-VM, hub VM and hubvm.
    $gap = if ($Loose) { '[\s\-]*' } else { '\s+' }
    return (($sorted | ForEach-Object { [regex]::Escape($_).Replace('\ ', $gap) }) -join '|')
}

function Get-LocalPlaybackPromptFindings {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $scan = Remove-NoLocalPlaybackSection -Text $Text
    # A verb or product word is bounded by non-word, non-hyphen characters, so "run-release-gui-smoke" is not the verb "run" and
    # "MLVAPP_C1_VECTORS_DIR" is not the app.
    $b = '(?<![\w\-])'; $e = '(?![\w\-])'
    $strongNeg = "(?i)${b}(never|do not|don't|must not|mustn't|may not|no local|not locally|forbidden|forbids|refuse[sd]?|refusal|prohibit\w*|instead of running|rather than running|without running)${e}"
    $weakNeg = "(?i)${b}(not|no|without)${e}"
    $lists = Get-LocalPlaybackLintWordLists
    $venueWords = ConvertTo-LintAlternation -Words @($lists.VenueWords)
    # A venue LOCUS (r4, narrowed r5): the routing preposition's object is the venue itself, so what follows the venue name can only be the
    # end of the clause (or of the sentence), or a LOCUS-PRESERVING word -- another routing preposition or a coordinator, never a purpose,
    # instrument or source complement ("to fetch", "for the clip", "as the clip source", "that produced it", "with this host's copy",
    # "so the hub has it", "from this host" all make the venue a tool, a source or a producer). A possessive ("Bachelor's clip") or a noun
    # ("the Bachelor clip", "the venue chain output") makes the venue an attribute of something else. Punctuation continues the clause: what
    # follows it must again be the end or a locus-preserving word ("on Bachelor, which holds the clip" is not a locus). The sets are
    # closed and fail closed: an unknown word after the venue name is not a locus.
    $venueName = "(?:${venueWords})(?:\s+(?:chain|chains|runner|job|jobs|leg|legs|lane))*"
    $venueFollow = (ConvertTo-LintAlternation -Words @($lists.VenueFollowLocus)) + '|' + (ConvertTo-LintAlternation -Words @($lists.VenueFollowPhrases))
    $venueEnd = "(?=\s*(?:\z|[.!?](?:\s|\z)|[,;:)\]`"]\s*(?:\z|[.!?](?:\s|\z)|(?:${venueFollow})(?![\w\-'])))|\s+(?:${venueFollow})(?![\w\-']))"
    $venueLocus = "${venueName}${venueEnd}"
    $routingLocally = ConvertTo-LintAlternation -Words @($lists.RoutingPrepositionsLocally)
    $routingVenue = ConvertTo-LintAlternation -Words @($lists.RoutingPrepositionsVenue)
    # E3: a venue word counts only inside a routing phrase ("on Bachelor", "through the venue chain", "via the chain runner") whose object is
    # a venue locus; "since Bachelor is busy", "to avoid the venue chain", "with no venue job" and "using Bachelor's clip" name a venue
    # without routing anything to it.
    $venueQualified = "(?i)${b}(?:${routingVenue})\s+(?:(?:the|a|an)\s+)?${venueLocus}"
    $verbs = 'run|runs|running|launch|launches|launching|launched|start|starts|starting|execute|executes|executing|invoke|invokes|invoking|drive|drives|driving|kick off|play|plays|playing|open|opens|opening'
    $launchVerb = "(?i)${b}(${verbs})${e}"
    $playbackWord = "(?i)(${b}playback${e}|gui-smoke|profile-playback|${b}MLVApp(Release)?(\.exe)?${e}|smoke run|${b}exactness${e}|${b}arms?${e}|${b}GUI${e})"
    $scripts = '(run-release-gui-smoke|start-release-cuda-playback|run-release-playback-profile|validate-visible-playback|capture-reference-frame)\.ps1'
    # Clause boundaries: a comma that opens a new imperative, a coordinating / subordinating connector, or a spaced dash.
    $separator = "(?i),\s*(?:(?:then|and|just|also|please|so)\s+)?(?=(?:${verbs}|use|using|do)${e})|${b}(and then|then|and|or|but|after|before|once|when|whenever|while|until|because|otherwise|unless|except|although|though|whereas)${e}|\s-{1,2}\s|;(?=\s|\z)"
    # Contrast words: the complement after one is negated and never routes. A bare "not" counts unless an auxiliary holds it ("do not", "is not").
    $auxiliary = 'do|does|did|must|may|can|could|should|would|will|shall|is|are|was|were|has|have|had|need|to|if|whether'
    $contrastWords = ConvertTo-LintAlternation -Words @($lists.ContrastWords)
    $contrast = "(?i),\s*(?:not|never)${e}|(?:,\s*)?${b}(?:${contrastWords})${e}|(?<!${b}(?:${auxiliary})\s+)${b}not${e}"
    $incident = '(?i)(?<![\w\-"''`])where\s+an?\s+(gui|desktop|display)(\s+desktop)?\s+session' + $e + '[^.\n]{0,100}' + $b + 'exists?' + $e
    $localMarker = "(?i)${b}locally${e}"
    $hostMarker = "(?i)${b}on\s+(this|the)\s+(hub|host|vm|machine|box)${e}|${b}on\s+virtual-ten${e}"
    $hereMarker = "(?i)${b}here(?![\w\-'])"
    # "from here" says where a venue job is started from, not where it runs; only the defeater skips it (rule 3b still reads it).
    $hereDefeater = "(?i)(?<!${b}from\s+)${b}here(?![\w\-'])"
    # r5: ANY reference to the local machine defeats a venue exemption, not only "on this host": "<determiner> <noun>" in any context
    # (to this host, from the hub, my machine, this host's copy, the hub copy) or a host name (localhost, virtual-ten). It is a DEFEATER only;
    # the rule-3 trigger stays $hostMarker, so a launcher line that merely mentions "the hub" is not newly refused.
    # r6: broad and tolerant. A determiner, up to two modifier words (this same host, the local box, our own machine; a venue word is not a
    # modifier, so "the Bachelor machine" stays a venue), then a noun that may carry a -vm suffix; a bare "<preposition> <noun>" (on hub);
    # or a name. A hyphen after the noun does not end the reference (hub-VM, HUB-VM-side).
    $hostDeterminer = ConvertTo-LintAlternation -Words @($lists.HostDeterminers)
    $hostNoun = ConvertTo-LintAlternation -Words @($lists.HostNouns) -Loose
    $hostName = ConvertTo-LintAlternation -Words @($lists.HostNames) -Loose
    $hostNounEnd = '(?:[\s\-]?vm)?(?!\w)'
    $hostModifier = "(?:(?!(?:${venueWords})${e})[\w'\-]+\s+){0,2}"
    $hostPreposition = ConvertTo-LintAlternation -Words @($lists.HostPrepositions)
    $hostRef = "(?i)${b}(?:${hostDeterminer})\s+${hostModifier}(?:${hostNoun})${hostNounEnd}|${b}(?:${hostPreposition})\s+(?:${hostNoun})${hostNounEnd}|${b}(?:${hostName})(?!\w)"
    # A bare `locally` that is not itself routed to a venue ("then run it locally"). The marker's own E1 phrase is excluded, so the scan
    # starting at it does not defeat itself.
    $localDefeater = "(?i)${b}locally${e}(?!\s+(?:${routingLocally})\s+(?:(?:the|a|an)\s+)?${venueLocus})"
    $hostDefeaters = @($hostMarker, $hostRef, $hereDefeater, $localDefeater)
    # Rule 4 (a direct launch line) carries NO local marker: the venue phrase is the only evidence of where it runs, and a host noun there is
    # often a DESTINATION ("... -Backend cpu on Bachelor and posts the result to the hub"), which master and r4 pass. r7 reads the host
    # reference there as a LOCUS only: a host noun introduced by a locus preposition (on / at / in / using + determiner + up to two words +
    # noun, or the bare noun) or a host name says where the work RUNS and defeats the exemption ("... on Bachelor and on my machine",
    # "... on Bachelor using this host's GPU", "... on Bachelor and on localhost"); to / into / from + a host noun is a destination and
    # does not. a595464f narrowed this rule to the r4 defeaters (on-this-host marker + bare here) and gave back the refusals r5 had.
    $hostLocusPreposition = ConvertTo-LintAlternation -Words @($lists.HostLocusPrepositions)
    # A host NAME that is also a host noun ("hub vm") is a destination after to / into / from + a determiner ("copies the clip to the hub
    # VM"); every other name (localhost, virtual-ten, an address) defeats wherever it stands, and "hub vm" with no determiner still does.
    $destinationPreposition = ConvertTo-LintAlternation -Words @('to', 'into', 'from')
    $nounNames = @($lists.HostNames | Where-Object { @($lists.HostNouns) -contains $_ })
    $plainNames = @($lists.HostNames | Where-Object { @($lists.HostNouns) -notcontains $_ })
    # An empty word list would build `(?:)`, which matches everywhere; each name alternative exists only when its list is not empty.
    $hostLocusRef = "(?i)${b}(?:${hostLocusPreposition})\s+(?:(?:${hostDeterminer})\s+${hostModifier})?(?:${hostNoun})${hostNounEnd}"
    if ($plainNames.Count -gt 0) {
        $hostLocusRef += "|${b}(?:$(ConvertTo-LintAlternation -Words $plainNames -Loose))(?!\w)"
    }
    if ($nounNames.Count -gt 0) {
        $hostLocusRef += "|(?<!${b}(?:${destinationPreposition})\s+(?:${hostDeterminer})\s+)${b}(?:$(ConvertTo-LintAlternation -Words $nounNames -Loose))(?!\w)"
    }
    $hostDefeatersLaunch = @($hostMarker, $hereDefeater, $hostLocusRef)
    # r10: the closed allow-grammar of the strictly terminal E1 (Test-VenueTerminalTail), a whitelist tokenizer as ONE anchored regex.
    # Every token is a word from a list followed by a token end ($tb); each fragment and each separator is atomic, so the match runs left
    # to right without backtracking into what it has read and stops at the first token the grammar does not name. Two fragments need a
    # separator between them ("not here on Bachelor" is not two fragments). A venue fragment is led and followed by pure locus words only.
    $tb = "(?![\w\-'])"
    $tailPrep = ConvertTo-LintAlternation -Words @($lists.TailLocusPrepositions)
    $tailArticle = ConvertTo-LintAlternation -Words @($lists.TailArticles)
    $tailJoiner = ConvertTo-LintAlternation -Words @($lists.TailJoiners)
    $tailNegation = ConvertTo-LintAlternation -Words @($lists.TailNegations)
    $tailNegPrep = ConvertTo-LintAlternation -Words @($lists.TailNegationPrepositions)
    $tailPureLocus = ConvertTo-LintAlternation -Words @($lists.TailPureLocus)
    $tailVenue = "(?:${venueWords})${tb}(?:\s+(?:chain|chains|runner|job|jobs|leg|legs|lane)${tb})*"
    $tailRoute = "(?:${tailPrep})${tb}\s+(?:(?:${tailArticle})${tb}\s+)?${tailVenue}"
    $tailLocus = "(?:${tailPureLocus})${tb}"
    $tailFragA = "(?:${tailLocus}\s+)*${tailRoute}(?:\s+${tailRoute})*(?:\s+${tailLocus})*"
    $tailHostDet = "(?:${hostDeterminer})${tb}(?:\s+(?:${hostDeterminer}|${tailArticle})${tb}){0,2}\s+(?:${hostNoun})${hostNounEnd}"
    $tailFragC = "(?:${tailNegation})${tb}\s+(?:here${tb}|locally${tb}|(?:(?:${tailNegPrep})${tb}\s+)?(?:${hostName})(?!\w)|(?:${tailNegPrep})${tb}\s+(?:${hostNoun})${hostNounEnd}|(?:(?:${tailNegPrep})${tb}\s+)?${tailHostDet})"
    $tailSep = "(?:[.!?;:,]+(?=\s|\z)|(?<=\s|^)-{1,2}(?=\s)|(?:${tailJoiner})${tb})"
    $tailFrag = "(?:${tailFragA}|${tailFragC}|${tailLocus})"
    $tailState = @{
        Grammar   = [regex]::new("(?i)^\s*(?:(?>${tailSep})\s*)*(?:(?>${tailFrag})(?:\s*(?>${tailSep}))+\s*)*(?:(?>${tailFrag})\s*)?\z")
        NextDirty = $null
    }
    $direct = @(
        "(?i)${b}MLVApp(Release)?\.exe\s+(?:(?!/\s)\S+\s+){0,12}--(gui-smoke-playback|profile-playback)",
        "(?i)(pwsh|powershell)(\.exe)?${e}[^\n]{0,80}-File\s+\S*$scripts",
        "(?i)(^|[\s``&(])(&\s*)?\.{0,2}[\\/]?\S*$scripts\s+-\w+")
    $findings = New-Object System.Collections.Generic.List[string]
    # A "sentence" is a line, cut again at '. ' so one bullet carrying several instructions is judged piece by piece. A semicolon is NOT a
    # sentence boundary since r6: it is a hard clause cut (no inheritance, per-group playback test) that the defeater scan reads across.
    # r8: every sentence knows the sentences that FOLLOW it in the same instruction block, because the sentence-terminal E1 reads them:
    # "Run playback locally on Bachelor. Then play it here." A block ends ONLY at a heading: a blank line, a line break or the next list
    # item is not a boundary ("...on Bachelor.\n\nThen play it here." is the same instruction with a paragraph break), so a follow-up
    # cannot hide behind formatting.
    $items = New-Object System.Collections.Generic.List[object]
    $blockId = 0
    foreach ($line in ($scan -split "\r?\n")) {
        if ($line -match '^\s*#{1,6}\s') { $blockId++ }
        foreach ($piece in ($line -split '(?<=[.!?])\s+')) {
            if ($piece.Trim()) { $items.Add([pscustomobject]@{ Text = $piece.Trim(); Block = $blockId }) }
        }
    }
    # r10: no per-item copy of the rest of the block (r9 kept one per item: O(n^2) in time and retained memory on a large heading-free
    # prompt). The sentence-terminal E1 reads later items through their index (Test-VenueTerminalTail, $tailState.NextDirty).
    # A sentence that holds none of the rule triggers (the incident phrase, `locally`, an on-this-host marker, a bare `here`, a direct
    # launch pattern) cannot be refused by any rule below, so it skips the clause analysis: same verdict, linear cost on long prompts.
    $triggers = @([regex]::new($incident), [regex]::new($localMarker), [regex]::new($hostMarker), [regex]::new($hereMarker)) + @($direct | ForEach-Object { [regex]::new($_) })
    for ($itemIndex = 0; $itemIndex -lt $items.Count; $itemIndex++) {
        foreach ($sentence in @($items[$itemIndex].Text)) {
            $s = $sentence.Trim()
            if ($s.Length -lt 8) { continue }
            $triggered = $false
            foreach ($rx in $triggers) { if ($rx.IsMatch($s)) { $triggered = $true; break } }
            if (-not $triggered) { continue }
            $clauses = @(Get-LintClauses -Sentence $s -Separator $separator -Contrast $contrast -LaunchVerb $launchVerb -StrongNeg $strongNeg -WeakNeg $weakNeg -VenueQualified $venueQualified)
            # Playback test per semicolon group: "Build MLVApp; run the tests on this host" is two instructions, as it was when ';' split sentences.
            $groupFirst = @{}
            $groupLast = @{}
            foreach ($c in $clauses) {
                $gid = [int]$c.Group
                if (-not $groupFirst.ContainsKey($gid) -or $c.Start -lt $groupFirst[$gid]) { $groupFirst[$gid] = [int]$c.Start }
                if (-not $groupLast.ContainsKey($gid) -or $c.End -gt $groupLast[$gid]) { $groupLast[$gid] = [int]$c.End }
            }
            $groupPlayback = @{}
            foreach ($gid in @($groupFirst.Keys)) {
                $gt = $s.Substring($groupFirst[$gid], $groupLast[$gid] - $groupFirst[$gid])
                $groupPlayback[$gid] = ($gt -match $playbackWord) -and ($gt -match $launchVerb)
            }
            $hit = $null
            # (1) the incident phrase: "where a GUI desktop session and the clip exist" and its relatives. A phrase that opens with a quote
            # mark is a QUOTATION of the old wording (an incident report), not an instruction; the clause holding "where" must not be a prohibition.
            $m1 = [regex]::Match($s, $incident)
            if ($m1.Success -and -not (Get-ClauseAt -Clauses $clauses -Index $m1.Index).Strong) { $hit = 'where-a-gui-session-exists' }
            # (2) "locally" attached to a playback / smoke / MLVApp / arm instruction. FAIL CLOSED: its own clause must be a prohibition, or
            # match E1 (locally + routing preposition + venue word) or E2 (the marker opens a contrast complement). A venue word elsewhere
            # in the clause ("locally since Bachelor is busy") exempts nothing.
            if (-not $hit) {
                foreach ($m in [regex]::Matches($s, $localMarker)) {
                    $c = Get-ClauseAt -Clauses $clauses -Index $m.Index
                    if (-not $groupPlayback[[int]$c.Group]) { continue }
                    # A prohibition exempts the marker only when it GOVERNS it: inherited from an earlier clause, or its own negation word sits
                    # before the marker. A negation that comes AFTER the marker in the same clause ("..., don't wait, then run it here") is
                    # a qualifier of something else and hides nothing (r8; it used to exempt the whole clause).
                    if ($c.Strong) {
                        if (-not [regex]::IsMatch($c.Text, $strongNeg)) { continue }
                        if ([regex]::IsMatch($s.Substring($c.Start, [Math]::Max(0, $m.Index - $c.Start)), $strongNeg)) { continue }
                    }
                    if ((Test-ContrastLead -Sentence $s -Clause $c -Index $m.Index) -and
                        -not (Test-LocalHostInScope -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeaters)) { continue }
                    # E1 is strictly terminal (r10): the rest of the sentence and of the block must be in the closed, verbless allow-grammar
                    # (Test-VenueTerminalTail). A host reference BEFORE the marker in its own clause is still a veto (the r4-r7 scan, limited
                    # to what precedes the venue phrase: what follows it is the grammar's to judge).
                    $e1End = Get-VenueRoutedLocallyEnd -Sentence $s -Clause $c -Index $m.Index -VenueLocus $venueLocus -Prepositions $routingLocally
                    if ($e1End -ge 0) {
                        $prefix = $s.Substring($c.Start, [Math]::Max(0, $m.Index - $c.Start))
                        $hostBefore = $false
                        foreach ($pattern in $hostDefeaters) { if ([regex]::IsMatch($prefix, $pattern)) { $hostBefore = $true; break } }
                        if (-not $hostBefore -and (Test-VenueTerminalTail -Remainder $s.Substring($e1End) -Items $items -Index $itemIndex -State $tailState)) { continue }
                    }
                    $hit = 'run-playback-locally'; break
                }
            }
            # (3) "on this host / the hub (VM)" attached to a playback launch; a negation or a venue word in that clause exempts it (master's
            # exemption, clause-scoped), as does E2
            if (-not $hit) {
                foreach ($m in [regex]::Matches($s, $hostMarker)) {
                    $c = Get-ClauseAt -Clauses $clauses -Index $m.Index
                    if (-not $groupPlayback[[int]$c.Group]) { continue }
                    if (-not ($c.Strong -or $c.Weak -or (Test-VenueExempts -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeaters) -or (Test-ContrastLead -Sentence $s -Clause $c -Index $m.Index))) { $hit = 'run-playback-on-this-host'; break }
                }
            }
            # (3b) a bare "here" is weak evidence, so the launch verb and the playback word must sit in the same clause as the word
            if (-not $hit) {
                foreach ($m in [regex]::Matches($s, $hereMarker)) {
                    $c = Get-ClauseAt -Clauses $clauses -Index $m.Index
                    if ($c.HasVerb -and ($c.Text -match $playbackWord) -and -not ($c.Strong -or $c.Weak -or (Test-VenueExempts -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeaters) -or (Test-ContrastLead -Sentence $s -Clause $c -Index $m.Index))) { $hit = 'run-playback-here'; break }
                }
            }
            # (4) a direct command line: the app with a playback mode, or a tracked launcher called with a parameter
            if (-not $hit) {
                foreach ($pattern in $direct) {
                    $m4 = [regex]::Match($s, $pattern)
                    if (-not $m4.Success) { continue }
                    $c = Get-ClauseAt -Clauses $clauses -Index $m4.Index
                    if (-not ($c.Strong -or $c.Weak -or (Test-VenueExempts -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeatersLaunch) -or (Test-ContrastLead -Sentence $s -Clause $c -Index $m4.Index))) { $hit = 'direct-playback-launch'; break }
                }
            }
            if ($hit) {
                $shown = if ($s.Length -gt 220) { $s.Substring(0, 220) + '...' } else { $s }
                $findings.Add("${hit}: $shown")
            }
        }
    }
    return $findings.ToArray()
}
