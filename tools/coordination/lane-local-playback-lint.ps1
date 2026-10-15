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
# ("<this|the|my|your|our|that|local> <hub|host|vm|machine|box|...>" in any context, localhost, virtual-ten, a bare here), and it reaches on
# into a later clause that has a launch verb when that clause refers back to the playback ("then run it here"). Every word list lives in
# Get-LocalPlaybackLintWordLists, and ListMemberDefeaterTests generates one defeater row PER MEMBER of each, so a word added to a list is
# tested by construction (ruling candidate mlv-safety-lint-exemption-defeater-corpus-r1 with the CoS amendment).
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
    foreach ($m in [regex]::Matches($Sentence, $Separator)) { $cuts.Add([pscustomobject]@{ Index = $m.Index; Length = $m.Length; Contrast = $false; Disjunct = ($m.Value.Trim() -ieq 'or') }) }
    if ($Contrast) {
        foreach ($m in [regex]::Matches($Sentence, $Contrast)) { $cuts.Add([pscustomobject]@{ Index = $m.Index; Length = $m.Length; Contrast = $true; Disjunct = $false }) }
    }
    $ordered = @($cuts | Sort-Object -Property @{ Expression = 'Index'; Descending = $false }, @{ Expression = 'Length'; Descending = $true })
    $spans = New-Object System.Collections.Generic.List[object]
    $pos = 0
    $pendingContrast = $false
    $pendingDisjunct = $false
    foreach ($cut in $ordered) {
        if ($cut.Index -lt $pos) { continue }
        $spans.Add([pscustomobject]@{ Start = $pos; End = $cut.Index; Contrast = $pendingContrast; NoInherit = $false; Disjunct = $pendingDisjunct })
        $pos = $cut.Index + $cut.Length
        $pendingContrast = [bool]$cut.Contrast
        $pendingDisjunct = [bool]$cut.Disjunct
    }
    $spans.Add([pscustomobject]@{ Start = $pos; End = $Sentence.Length; Contrast = $pendingContrast; NoInherit = $false; Disjunct = $pendingDisjunct })
    # A contrast complement ends at its first comma; what follows is a fresh, non-inheriting clause.
    $flat = New-Object System.Collections.Generic.List[object]
    foreach ($sp in $spans) {
        if ($sp.Contrast -and $sp.End -gt $sp.Start) {
            $comma = $Sentence.IndexOf(',', $sp.Start, $sp.End - $sp.Start)
            if ($comma -ge 0) {
                $flat.Add([pscustomobject]@{ Start = $sp.Start; End = $comma; Contrast = $true; NoInherit = $false; Disjunct = $sp.Disjunct })
                $flat.Add([pscustomobject]@{ Start = $comma + 1; End = $sp.End; Contrast = $false; NoInherit = $true; Disjunct = $false })
                continue
            }
        }
        $flat.Add($sp)
    }
    $clauses = New-Object System.Collections.Generic.List[object]
    $prev = $null
    $head = $null
    foreach ($sp in $flat) {
        $start = [int]$sp.Start; $end = [int]$sp.End
        if ($end -le $start) { continue }
        $text = $Sentence.Substring($start, $end - $start)
        if (-not $text.Trim()) { continue }
        $hasVerb = [bool]($text -match $LaunchVerb)
        $strong = [bool]($text -match $StrongNeg)
        $weak = [bool]($text -match $WeakNeg)
        # A complement is judged on its own words: being the object of "without" negates nothing here (only the E2 shape exempts a marker).
        $from = if ($sp.NoInherit) { $head } else { $prev }
        if ((-not $hasVerb -or $sp.Disjunct) -and $from) { $strong = $strong -or $from.Strong; $weak = $weak -or $from.Weak }
        if ($sp.Contrast) { $head = $prev }
        $c = [pscustomobject]@{ Start = $start; End = $end; Text = $text; HasVerb = $hasVerb; Strong = $strong; Weak = $weak; Contrast = [bool]$sp.Contrast; Venue = (-not $sp.Contrast) -and [bool]($text -match $VenueQualified) }
        $clauses.Add($c)
        $prev = $c
    }
    if ($clauses.Count -eq 0) {
        $clauses.Add([pscustomobject]@{ Start = 0; End = $Sentence.Length; Text = $Sentence; HasVerb = [bool]($Sentence -match $LaunchVerb); Strong = [bool]($Sentence -match $StrongNeg); Weak = [bool]($Sentence -match $WeakNeg); Contrast = $false; Venue = [bool]($Sentence -match $VenueQualified) })
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
# $venueLocus in Get-LocalPlaybackPromptFindings), all inside the marker's own clause.
function Test-VenueRoutedLocally {
    param([string]$Sentence, [object]$Clause, [int]$Index, [string]$VenueLocus, [string]$Prepositions)
    if ($Index -lt $Clause.Start -or $Index -ge $Clause.End) { return $false }
    $tail = $Sentence.Substring($Index, $Clause.End - $Index)
    return [bool]($tail -match "(?i)^locally\s+(?:${Prepositions})\s+(?:(?:the|a|an)\s+)?${VenueLocus}")
}

# Defeater (r4, widened r5): a local-host reference in the clause, or in the clauses coordinated right after it, that is not itself negated or
# the first thing of a contrast complement. A venue qualification never exempts a clause that also names the local host. $HostPatterns are
# the reference patterns (the marker, a determiner + host noun in ANY context, a host name, a bare "here" that is not "from here").
# The scan runs on through a later clause that has a launch verb only when that clause refers back to the playback ("then run it here"):
# a verbless clause ("... and the hub copy") is scanned as before, any other verb clause ends the scan.
function Test-LocalHostInScope {
    param([string]$Sentence, [object[]]$Clauses, [object]$Clause, [string[]]$HostPatterns, [string]$BackReference, [string]$PlaybackWord)
    $started = $false
    foreach ($k in $Clauses) {
        if (-not $started) {
            if ([object]::ReferenceEquals($k, $Clause)) { $started = $true } else { continue }
        }
        elseif ($k.Contrast -or $k.Strong -or $k.Weak) { break }
        elseif ($k.HasVerb -and -not (($k.Text -match $BackReference) -or ($k.Text -match $PlaybackWord))) { break }
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
    param([string]$Sentence, [object[]]$Clauses, [object]$Clause, [string[]]$HostPatterns, [string]$BackReference, [string]$PlaybackWord)
    if (-not $Clause.Venue) { return $false }
    return -not (Test-LocalHostInScope -Sentence $Sentence -Clauses $Clauses -Clause $Clause -HostPatterns $HostPatterns -BackReference $BackReference -PlaybackWord $PlaybackWord)
}

# The CLOSED word lists the exemptions depend on (r5). Every pattern in Get-LocalPlaybackPromptFindings is built from these lists, and
# tools/repo_hygiene/test_hub_vm_no_local_playback.py (ListMemberDefeaterTests) reads them back and generates one defeater row PER MEMBER,
# so a word added here is tested by construction. A word joins VenueFollowLocus only with a reason in LOCUS_FOLLOW_REASONS there.
#   VenueWords                  what names a venue.
#   RoutingPrepositionsLocally  the preposition that must follow `locally` for E1.   RoutingPrepositionsVenue  the prepositions of an E3 routing phrase.
#   VenueFollowLocus            what may follow the venue name and still leave it the PLACE the work runs: another routing preposition or a
#                               coordinator. Never a purpose / instrument / source complement (to for as that which with so only from ...).
#   VenueFollowPhrases          the same, as a phrase: "from here" says where a venue job is started from, not where it runs.
#   HostDeterminers / HostNouns / HostNames   a reference to the local machine, in ANY context: "<determiner> <noun>" or a name.
#   BackReferences              a follow-up clause that names the local host and refers back to the playback defeats the earlier exemption.
#   ContrastWords               the words that open a contrast complement (E2).
function Get-LocalPlaybackLintWordLists {
    return [ordered]@{
        VenueWords                 = @('venue', 'venues', 'bachelor', 'ultra-magnus', 'um-run', 'Invoke-VenueChain', 'Invoke-VenueLeg', 'Invoke-VenueChain.ps1', 'Invoke-VenueLeg.ps1', 'chain runner')
        RoutingPrepositionsLocally = @('on', 'via', 'through', 'using')
        RoutingPrepositionsVenue   = @('on', 'via', 'through', 'using', 'at')
        VenueFollowLocus           = @('on', 'via', 'through', 'using', 'at', 'and', 'or', 'but', 'then')
        VenueFollowPhrases         = @('from here')
        HostDeterminers            = @('this', 'the', 'my', 'your', 'our', 'that', 'local')
        HostNouns                  = @('hub vm', 'hub host', 'hub', 'host', 'vm', 'machine', 'box', 'workstation', 'computer', 'pc', 'desktop', 'laptop')
        HostNames                  = @('localhost', 'virtual-ten')
        BackReferences             = @('it', 'them', 'that', 'those', 'the same', 'again')
        ContrastWords              = @('instead of', 'rather than', 'without', 'nor', 'skip', 'skips', 'skipping', 'bypass', 'bypasses', 'bypassing')
    }
}

# A regex alternation of literal words, longest first, a space in a word matching any run of white space.
function ConvertTo-LintAlternation {
    param([string[]]$Words)
    $sorted = @($Words | Sort-Object -Property @{ Expression = { $_.Length }; Descending = $true })
    return (($sorted | ForEach-Object { [regex]::Escape($_).Replace('\ ', '\s+') }) -join '|')
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
    $separator = "(?i),\s*(?:(?:then|and|just|also|please|so)\s+)?(?=(?:${verbs}|use|using|do)${e})|${b}(and then|then|and|or|but|after|before|once|when|whenever|while|until|because|otherwise|unless|except|although|though|whereas)${e}|\s-{1,2}\s"
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
    $hostDeterminer = ConvertTo-LintAlternation -Words @($lists.HostDeterminers)
    $hostNoun = ConvertTo-LintAlternation -Words @($lists.HostNouns)
    $hostName = ConvertTo-LintAlternation -Words @($lists.HostNames)
    $hostRef = "(?i)${b}(?:${hostDeterminer})\s+(?:${hostNoun})(?![\w\-])|${b}(?:${hostName})(?![\w\-])"
    $backReference = "(?i)${b}(?:" + (ConvertTo-LintAlternation -Words @($lists.BackReferences)) + ")${e}"
    $hostDefeaters = @($hostMarker, $hostRef, $hereDefeater)
    # Rule 4 (a direct launch line) carries NO local marker: the venue phrase is the only evidence of where it runs, and a host noun there is
    # usually a destination ("... -Backend cpu on Bachelor and posts the result to the hub"), which master and r4 pass. It keeps the r4
    # defeaters (the on-this-host marker and a bare here); every `locally` and host-marker rule uses the wide set above.
    $hostDefeatersLaunch = @($hostMarker, $hereDefeater)
    $direct = @(
        "(?i)${b}MLVApp(Release)?\.exe\s+(?:(?!/\s)\S+\s+){0,12}--(gui-smoke-playback|profile-playback)",
        "(?i)(pwsh|powershell)(\.exe)?${e}[^\n]{0,80}-File\s+\S*$scripts",
        "(?i)(^|[\s``&(])(&\s*)?\.{0,2}[\\/]?\S*$scripts\s+-\w+")
    $findings = New-Object System.Collections.Generic.List[string]
    # A "sentence" is a line, cut again at '. ' so one bullet carrying several instructions is judged piece by piece.
    foreach ($line in ($scan -split "\r?\n")) {
        foreach ($sentence in ($line -split '(?<=[.;!?])\s+')) {
            $s = $sentence.Trim()
            if ($s.Length -lt 8) { continue }
            $clauses = @(Get-LintClauses -Sentence $s -Separator $separator -Contrast $contrast -LaunchVerb $launchVerb -StrongNeg $strongNeg -WeakNeg $weakNeg -VenueQualified $venueQualified)
            $isPlayback = ($s -match $playbackWord) -and ($s -match $launchVerb)
            $hit = $null
            # (1) the incident phrase: "where a GUI desktop session and the clip exist" and its relatives. A phrase that opens with a quote
            # mark is a QUOTATION of the old wording (an incident report), not an instruction; the clause holding "where" must not be a prohibition.
            $m1 = [regex]::Match($s, $incident)
            if ($m1.Success -and -not (Get-ClauseAt -Clauses $clauses -Index $m1.Index).Strong) { $hit = 'where-a-gui-session-exists' }
            # (2) "locally" attached to a playback / smoke / MLVApp / arm instruction. FAIL CLOSED: its own clause must be a prohibition, or
            # match E1 (locally + routing preposition + venue word) or E2 (the marker opens a contrast complement). A venue word elsewhere
            # in the clause ("locally since Bachelor is busy") exempts nothing.
            if (-not $hit -and $isPlayback) {
                foreach ($m in [regex]::Matches($s, $localMarker)) {
                    $c = Get-ClauseAt -Clauses $clauses -Index $m.Index
                    if ($c.Strong) { continue }
                    if ((Test-ContrastLead -Sentence $s -Clause $c -Index $m.Index) -and
                        -not (Test-LocalHostInScope -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeaters -BackReference $backReference -PlaybackWord $playbackWord)) { continue }
                    if ((Test-VenueRoutedLocally -Sentence $s -Clause $c -Index $m.Index -VenueLocus $venueLocus -Prepositions $routingLocally) -and
                        -not (Test-LocalHostInScope -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeaters -BackReference $backReference -PlaybackWord $playbackWord)) { continue }
                    $hit = 'run-playback-locally'; break
                }
            }
            # (3) "on this host / the hub (VM)" attached to a playback launch; a negation or a venue word in that clause exempts it (master's
            # exemption, clause-scoped), as does E2
            if (-not $hit -and $isPlayback) {
                foreach ($m in [regex]::Matches($s, $hostMarker)) {
                    $c = Get-ClauseAt -Clauses $clauses -Index $m.Index
                    if (-not ($c.Strong -or $c.Weak -or (Test-VenueExempts -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeaters -BackReference $backReference -PlaybackWord $playbackWord) -or (Test-ContrastLead -Sentence $s -Clause $c -Index $m.Index))) { $hit = 'run-playback-on-this-host'; break }
                }
            }
            # (3b) a bare "here" is weak evidence, so the launch verb and the playback word must sit in the same clause as the word
            if (-not $hit) {
                foreach ($m in [regex]::Matches($s, $hereMarker)) {
                    $c = Get-ClauseAt -Clauses $clauses -Index $m.Index
                    if ($c.HasVerb -and ($c.Text -match $playbackWord) -and -not ($c.Strong -or $c.Weak -or (Test-VenueExempts -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeaters -BackReference $backReference -PlaybackWord $playbackWord) -or (Test-ContrastLead -Sentence $s -Clause $c -Index $m.Index))) { $hit = 'run-playback-here'; break }
                }
            }
            # (4) a direct command line: the app with a playback mode, or a tracked launcher called with a parameter
            if (-not $hit) {
                foreach ($pattern in $direct) {
                    $m4 = [regex]::Match($s, $pattern)
                    if (-not $m4.Success) { continue }
                    $c = Get-ClauseAt -Clauses $clauses -Index $m4.Index
                    if (-not ($c.Strong -or $c.Weak -or (Test-VenueExempts -Sentence $s -Clauses $clauses -Clause $c -HostPatterns $hostDefeatersLaunch -BackReference $backReference -PlaybackWord $playbackWord) -or (Test-ContrastLead -Sentence $s -Clause $c -Index $m4.Index))) { $hit = 'direct-playback-launch'; break }
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
