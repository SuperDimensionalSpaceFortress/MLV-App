Set-StrictMode -Version Latest
<#
VenueEvidence.psm1 -- pure file logic behind Get-VenueEvidence.ps1 (DUAL-VENUE-RECONCILE-1, component C3).

Reads dual-venue receipts (schema mlv-app/dual-venue-receipt/v1) and reports them PER VENUE. It never touches a
venue, starts a job or writes a file. The refusals it enforces (see DESIGN.md principles):
  P2 no merged verdict  - a pair is COMPLETE only when every expected venue's LATEST receipt at the same subject
                          digest says PASS or FAIL; the delta is DIAGNOSTIC and exists only for a COMPLETE pair.
  P3 role is data       - acceptance reads only receipts whose role is `acceptance` for that card; a recorded role
                          that disagrees with venues.json is refused (ROLE_MISMATCH).
  P4 typed terminals    - only a receipt that itself says PASS/FAIL carries signal; a malformed receipt is reported
                          and never counted.
  P6 venue truth        - a PASS/FAIL receipt must have declared == detected == venue.name.
  Acceptance never rests on a role the receipt declares for itself: -AcceptanceFor needs a PRESENT, valid venue
  table that lists the card; without one the answer is NO_ACCEPTANCE_EVIDENCE (VENUE_TABLE_ABSENT |
  CARD_NOT_IN_VENUE_TABLE). Report mode may still show recorded roles, marked UNVERIFIED.
  Owner rule 2026-09-30 (long clips): a PASS/FAIL receipt is playback evidence only with metrics.clipSeconds >= 20
  and metrics.wrapped == 0. wrapped=1 -> INVALID_LOOPED, clipSeconds < 20 -> INVALID_CLIP_TOO_SHORT (both refused,
  never counted); fields absent -> UNVERIFIED_CLIP_LENGTH (excluded from acceptance).
  DUAL-VENUE-RECONCILE-2 -- ONE CLASS: an older or less valid result is never presented as current. STRUCTURE:
    * Get-VeNewestAttempts is the ONLY selection. It runs once, over EVERY receipt of the root (never a filtered subset),
      and returns the NEWEST ATTEMPT per leg (card|legId|backend|lookFlavor|venue) and its VALIDATED STATE
      (Get-VeValidatedState, the one definition: PASS/FAIL only when valid, clip >= 20 s, wrapped = 0, role
      authoritative; everything else is a typed non-signal state).
    * A receipt file that fails validation is a MALFORMED ATTEMPT (New-VeBarrier). With a readable identity and finish
      time it is an attempt of its own leg (state MALFORMED_NEWER_RECEIPT); with an unreadable one it withholds the whole card.
    * Acceptance mode AND report mode consume only that output (latest = newest attempt; a pair is COMPLETE only if every
      expected venue's newest attempt is a validated PASS/FAIL at one digest; the delta exists only for COMPLETE).
    * Filters never change which attempt is newest. A -SubjectDigest/-BuildManifestSha256 filter only asks whether the
      leg's newest attempt is at that subject/build; if not the leg WITHHOLDS (NEWER_ATTEMPT_OUTSIDE_FILTER), no fallback.
  Anything newest that is not a clean PASS/FAIL WITHHOLDS and the card is non-green (NEWEST_ATTEMPT_WITHHELD).
  A blank argument is an error, never "no filter" or "report mode".
  A receipt for a terminal reached before a run (VENUE_UNHEALTHY, ...) may carry metrics null; that is well-formed.
ASCII only (cp1252-safe). Requires PowerShell 7+ (ConvertFrom-Json -AsHashtable).
#>

$script:ReceiptSchema = 'mlv-app/dual-venue-receipt/v1'
$script:ReportSchema = 'mlv-app/dual-venue-evidence-report/v1'
$script:KnownVenues = @('bachelor', 'ultra-magnus')
$script:KnownRoles = @('acceptance', 'supplementary')
$script:KnownOutcomes = @('PASS', 'FAIL', 'VENUE_UNHEALTHY', 'VENUE_NOT_QUIESCENT', 'VENUE_HOST_MISMATCH',
    'DEVICE_UNAVAILABLE', 'UNRESOLVED', 'RETRACTED')
$script:SignalOutcomes = @('PASS', 'FAIL')
$script:RefusalStates = @('DUPLICATE_RECEIPT_ID', 'ROLE_MISMATCH', 'VENUE_DETECTION_MISMATCH', 'INVALID_LOOPED', 'INVALID_CLIP_TOO_SHORT')
$script:KnownBackends = @('cuda', 'gl', 'cpu')
$script:MaxReceiptBytes = 1MB
$script:MinClipSeconds = 20
$script:MaxClockSkewMinutes = 15

# ---------------------------------------------------------------- small helpers

function Get-VeValue {
    param($Dict, [string]$Key)
    if ($Dict -isnot [System.Collections.IDictionary]) { return $null }
    foreach ($k in $Dict.Keys) { if ([string]$k -ceq $Key) { return $Dict[$k] } }
    return $null
}

function Find-VeKey {
    # Case-insensitive key lookup for the venue table (card names are operator-typed).
    param($Dict, [string]$Key)
    if ($Dict -isnot [System.Collections.IDictionary]) { return $null }
    foreach ($k in $Dict.Keys) { if ([string]$k -ieq $Key) { return [string]$k } }
    return $null
}

function Test-VeString { param($Value) return (($Value -is [string]) -and -not [string]::IsNullOrWhiteSpace($Value)) }

function ConvertTo-VeInstant {
    # Strict ISO-8601 with an explicit zone. A zoneless stamp is refused: "latest" must be a real instant.
    param($Value)
    if ($Value -is [datetime]) {
        if ($Value.Kind -eq [System.DateTimeKind]::Unspecified) { return $null }
        return [System.DateTimeOffset]::new($Value.ToUniversalTime())
    }
    if ($Value -isnot [string]) { return $null }
    if ($Value -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?(Z|[+-]\d{2}:\d{2})$') { return $null }
    [System.DateTimeOffset]$out = [System.DateTimeOffset]::MinValue
    if ([System.DateTimeOffset]::TryParse($Value, [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::None, [ref]$out)) { return $out }
    return $null
}

function Format-VeInstant {
    param([System.DateTimeOffset]$Instant)
    return $Instant.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", [System.Globalization.CultureInfo]::InvariantCulture)
}

function ConvertFrom-VeJsonText {
    param([string]$Text)
    $args2 = @{ AsHashtable = $true; ErrorAction = 'Stop' }
    # PowerShell 7.5+ can keep date-looking strings as strings; older builds convert them, which
    # ConvertTo-VeInstant tolerates.
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { $args2['DateKind'] = 'String' }
    return ($Text | ConvertFrom-Json @args2)
}

function Resolve-VeDefaultReceiptsRoot {
    # .claude-state is git-ignored, so it lives only in the MAIN checkout, never in a worktree: resolve it
    # through the git common dir. MLV_DUAL_VENUE_RECEIPTS_ROOT overrides (tests, other hosts).
    $envRoot = $env:MLV_DUAL_VENUE_RECEIPTS_ROOT
    if (-not [string]::IsNullOrWhiteSpace($envRoot)) { return $envRoot }
    $repo = $null
    try {
        $common = (& git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
        if ($LASTEXITCODE -eq 0 -and $common) {
            $common = ([string]($common | Select-Object -First 1)).Trim()
            if ((Split-Path -Leaf $common) -eq '.git') { $repo = Split-Path -Parent $common }
        }
    } catch { $repo = $null }
    if (-not $repo) { $repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path }
    return (Join-Path $repo '.claude-state\dual-venue\receipts')
}

# ---------------------------------------------------------------- venue table

function Read-VenueTable {
    <#
    Returns @{ present; path; defaultRole; venues[]; roles } . A missing file is `present = $false` (roles are then
    unverified, and the report says so). A PRESENT but unreadable or invalid table throws: a broken role table must
    never degrade silently into "no role checks".
    #>
    param([Parameter(Mandatory)][string]$Path)
    $result = [ordered]@{ present = $false; path = $Path; defaultRole = 'supplementary'; venues = @(); roles = @{} }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $result }
    try {
        $t = ConvertFrom-VeJsonText -Text (Get-Content -LiteralPath $Path -Raw -Encoding UTF8)
    } catch { throw "venue table '$Path' is not valid JSON: $($_.Exception.Message)" }
    if ($t -isnot [System.Collections.IDictionary]) { throw "venue table '$Path' must be a JSON object" }
    $result.present = $true
    $dr = Get-VeValue $t 'defaultRole'
    if ($null -ne $dr) {
        if ($dr -cnotin $script:KnownRoles) { throw "venue table '$Path': defaultRole '$dr' is not acceptance|supplementary" }
        $result.defaultRole = [string]$dr
    }
    $venues = Get-VeValue $t 'venues'
    if ($null -ne $venues) {
        if ($venues -isnot [System.Collections.IDictionary]) { throw "venue table '$Path': venues must be an object" }
        foreach ($v in $venues.Keys) {
            if ([string]$v -notin $script:KnownVenues) { throw "venue table '$Path': unknown venue '$v'" }
        }
        $result.venues = @($venues.Keys | ForEach-Object { [string]$_ } | Sort-Object)
    }
    $roles = Get-VeValue $t 'roles'
    if ($null -ne $roles) {
        if ($roles -isnot [System.Collections.IDictionary]) { throw "venue table '$Path': roles must be an object" }
        $seenCards = @{}
        foreach ($card in $roles.Keys) {
            # Card names match case-insensitively, so two spellings of one card would make "last wins" decide a role.
            $cardLower = ([string]$card).ToLowerInvariant()
            if ($seenCards.ContainsKey($cardLower)) { throw "venue table '$Path': roles names card '$card' twice (card names are case-insensitive)" }
            $seenCards[$cardLower] = $true
            $perVenue = $roles[$card]
            if ($perVenue -isnot [System.Collections.IDictionary]) { throw "venue table '$Path': roles['$card'] must be an object" }
            $clean = @{}
            foreach ($v in $perVenue.Keys) {
                if ([string]$v -notin $script:KnownVenues) { throw "venue table '$Path': roles['$card'] names unknown venue '$v'" }
                if ($perVenue[$v] -cnotin $script:KnownRoles) { throw "venue table '$Path': roles['$card']['$v'] = '$($perVenue[$v])' is not acceptance|supplementary" }
                $clean[[string]$v] = [string]$perVenue[$v]
            }
            $result.roles[[string]$card] = $clean
        }
    }
    return $result
}

function Get-VeExpectedRole {
    param($Table, [string]$Card, [string]$Venue)
    $ck = Find-VeKey $Table.roles $Card
    if ($null -ne $ck -and $Table.roles[$ck].ContainsKey($Venue)) { return $Table.roles[$ck][$Venue] }
    return $Table.defaultRole
}

function Get-VeExplicitRole {
    # Acceptance reads ONLY a role the table names for this exact (card, venue). Unlike Get-VeExpectedRole it never
    # falls back to defaultRole: a venue the card's entry does not list is not an acceptance venue.
    param($Table, [string]$Card, [string]$Venue)
    $ck = Find-VeKey $Table.roles $Card
    if ($null -ne $ck -and $Table.roles[$ck].ContainsKey($Venue)) { return $Table.roles[$ck][$Venue] }
    return $null
}

function Get-VeExpectedVenues {
    # "every venue in the card's roles"; a card the table does not list expects every venue the table knows.
    param($Table, [string]$Card)
    if ($Table.present) {
        $ck = Find-VeKey $Table.roles $Card
        if ($null -ne $ck) { return @($Table.roles[$ck].Keys | Sort-Object) }
        if ($Table.venues.Count -gt 0) { return @($Table.venues) }
    }
    return @($script:KnownVenues)
}

# ---------------------------------------------------------------- receipt validation

function ConvertTo-VeRecord {
    <#
    Validates one parsed receipt against mlv-app/dual-venue-receipt/v1 and its place in the receipts layout
    <card>\<legId>\<venue>\<receiptId>.json. Returns @{ reasons = string[]; record = hashtable|$null }.
    Only fields the reconciler relies on are required; extra fields pass through untouched.
    #>
    param($Receipt, [string[]]$RelParts)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($Receipt -isnot [System.Collections.IDictionary]) {
        return @{ reasons = @('top-level JSON value is not an object'); record = $null }
    }
    $schema = Get-VeValue $Receipt 'schema'
    if ($schema -cne $script:ReceiptSchema) { $reasons.Add("schema is not '$($script:ReceiptSchema)'") }
    $receiptId = Get-VeValue $Receipt 'receiptId'
    if (-not (Test-VeString $receiptId) -or $receiptId -notmatch '^[A-Za-z0-9._-]+$') { $reasons.Add('receiptId missing or not a path-safe token') }
    $card = Get-VeValue $Receipt 'card'
    if (-not (Test-VeString $card)) { $reasons.Add('card missing') }
    $legId = Get-VeValue $Receipt 'legId'
    if (-not (Test-VeString $legId)) { $reasons.Add('legId missing') }

    $subject = Get-VeValue $Receipt 'subject'
    $digest = $null
    $backend = $null
    $lookFlavor = $null
    if ($subject -isnot [System.Collections.IDictionary]) {
        $reasons.Add('subject missing or not an object')
    } else {
        $digest = Get-VeValue $subject 'digest'
        if (-not ($digest -is [string]) -or $digest -notmatch '^[0-9a-fA-F]{64}$') { $reasons.Add('subject.digest is not a sha256 hex string') }
        else { $digest = $digest.ToLowerInvariant() }
        foreach ($f in 'buildManifestSha256', 'legSpecSha256', 'clipId', 'clipContentSha256') {
            if (-not (Test-VeString (Get-VeValue $subject $f))) { $reasons.Add("subject.$f missing") }
        }
        # lookFlavor distinguishes legs (it is part of the leg key), so a present value that is not a string is
        # malformed, never silently "no flavor".
        $lf = Get-VeValue $subject 'lookFlavor'
        if ($lf -is [string]) { $lookFlavor = $lf }
        elseif ($null -ne $lf) { $reasons.Add('subject.lookFlavor is not a string') }
        $b = Get-VeValue $subject 'backend'
        if ($null -ne $b) {
            if ($b -cnotin $script:KnownBackends) { $reasons.Add("subject.backend '$b' is not cuda|gl|cpu") } else { $backend = [string]$b }
        }
    }

    $venue = Get-VeValue $Receipt 'venue'
    $venueName = $null; $venueRole = $null; $declared = $null; $detected = $null
    if ($venue -isnot [System.Collections.IDictionary]) {
        $reasons.Add('venue missing or not an object')
    } else {
        $venueName = Get-VeValue $venue 'name'
        if ($venueName -cnotin $script:KnownVenues) { $reasons.Add('venue.name is not bachelor|ultra-magnus'); $venueName = $null }
        $venueRole = Get-VeValue $venue 'role'
        if ($venueRole -cnotin $script:KnownRoles) { $reasons.Add('venue.role is not acceptance|supplementary'); $venueRole = $null }
        $declared = Get-VeValue $venue 'declared'
        $detected = Get-VeValue $venue 'detected'
    }

    $outcome = Get-VeValue $Receipt 'outcome'
    if ($outcome -cnotin $script:KnownOutcomes) { $reasons.Add('outcome is not one of the typed terminals'); $outcome = $null }

    $started = ConvertTo-VeInstant (Get-VeValue $Receipt 'startedUtc')
    $finished = ConvertTo-VeInstant (Get-VeValue $Receipt 'finishedUtc')
    if ($null -eq $started) { $reasons.Add('startedUtc is not an ISO-8601 instant with a zone') }
    if ($null -eq $finished) { $reasons.Add('finishedUtc is not an ISO-8601 instant with a zone') }
    if ($null -ne $started -and $null -ne $finished -and $finished -lt $started) { $reasons.Add('finishedUtc is before startedUtc') }
    # "Newest" is decided by finishedUtc, so a finish time in the future would outrank every real later attempt for ever.
    if ($null -ne $finished -and $finished -gt [System.DateTimeOffset]::UtcNow.AddMinutes($script:MaxClockSkewMinutes)) {
        $reasons.Add("finishedUtc is more than $($script:MaxClockSkewMinutes) minutes in the future")
    }

    # PASS/FAIL carry measurements, so metrics must be an object for them. A terminal reached BEFORE a run (VENUE_UNHEALTHY,
    # VENUE_HOST_MISMATCH, DEVICE_UNAVAILABLE, UNRESOLVED, RETRACTED, ...) has none, and the runner writes null: that is a
    # well-formed receipt, not a malformed one. Anything else that is not an object (a number, a string) is malformed.
    $metrics = Get-VeValue $Receipt 'metrics'
    if ($metrics -isnot [System.Collections.IDictionary] -and ($null -ne $metrics -or $outcome -cin $script:SignalOutcomes)) {
        $reasons.Add('metrics missing or not an object')
    }

    # Place in the append-only layout: the path is part of the claim, so a receipt filed under another
    # venue/leg/card folder is refused rather than trusted.
    if ($RelParts.Count -ne 4) {
        $reasons.Add('receipt is not filed at <card>\<legId>\<venue>\<receiptId>.json')
    } else {
        if ((Test-VeString $card) -and $card -ine $RelParts[0]) { $reasons.Add('card does not match its folder') }
        if ((Test-VeString $legId) -and $legId -ine $RelParts[1]) { $reasons.Add('legId does not match its folder') }
        if ($null -ne $venueName -and $venueName -ine $RelParts[2]) { $reasons.Add('venue.name does not match its folder') }
        $stem = [System.IO.Path]::GetFileNameWithoutExtension($RelParts[3])
        if ((Test-VeString $receiptId) -and $receiptId -ine $stem) { $reasons.Add('receiptId does not match its file name') }
    }

    if ($reasons.Count -gt 0) { return @{ reasons = $reasons.ToArray(); record = $null } }

    $evidence = Get-VeValue $Receipt 'evidence'
    $clip = @{ state = 'NOT_APPLICABLE'; clipSeconds = $null; wrapped = $null }
    if ($outcome -in $script:SignalOutcomes) { $clip = Get-VeClipLength $metrics }
    # state starts as the receipt's own outcome; Get-VeValidatedState (the one definition) sets what it amounts to as an
    # attempt once duplicate ids and the venue table are known.
    $record = @{
        isBarrier     = $false
        state         = [string]$outcome
        refusal       = $null
        receiptId     = [string]$receiptId
        card          = [string]$card
        legId         = [string]$legId
        backend       = $backend
        digest        = $digest
        buildManifest = [string](Get-VeValue $subject 'buildManifestSha256')
        legSpec       = [string](Get-VeValue $subject 'legSpecSha256')
        clipId        = [string](Get-VeValue $subject 'clipId')
        lookFlavor    = $lookFlavor
        clipState     = $clip.state
        clipSeconds   = $clip.clipSeconds
        wrapped       = $clip.wrapped
        venue         = [string]$venueName
        role          = [string]$venueRole
        declared      = $declared
        detected      = $detected
        outcome       = [string]$outcome
        outcomeDetail = (Get-VeValue $Receipt 'outcomeDetail')
        started       = $started
        finished      = $finished
        metrics       = $metrics
        umRunOutcome  = (Get-VeValue $evidence 'umRunOutcome')
        path          = ''
    }
    return @{ reasons = @(); record = $record }
}

function Get-VeReceiptFiles {
    # EVERY *.json under the root, hard cap on count. A receipt is only valid at the four-level layout
    # <root>\<card>\<leg>\<venue>\<id>.json, but a file anywhere else is still SEEN (and reported), never invisible:
    # a newer result misfiled one level off must not let an older PASS stand. rel = the path segments below the root.
    param([string]$Root, [int]$Max)
    $files = [System.Collections.Generic.List[object]]::new()
    $rootFull = (Resolve-Path -LiteralPath $Root).ProviderPath
    foreach ($f in (Get-ChildItem -LiteralPath $Root -Recurse -File -Force -Filter '*.json' | Sort-Object FullName)) {
        $relPath = [System.IO.Path]::GetRelativePath($rootFull, $f.FullName)
        $files.Add(@{ file = $f; rel = @($relPath -split '[\\/]') })
        if ($files.Count -gt $Max) { throw "receipts root holds more than -MaxReceipts ($Max) receipts; raise the bound deliberately" }
    }
    return , $files.ToArray()
}

function Get-VeFlavorKey {
    # lookFlavor as it takes part in a leg key: trimmed, lower-case; absent or blank is the flavorless leg ('').
    param($Flavor)
    if ($Flavor -is [string]) { return $Flavor.Trim().ToLowerInvariant() }
    return ''
}

function Get-VeLegKey {
    # THE ONE place a leg key is built. A LEG is (card, legId, backend, lookFlavor, venue): every subject field that
    # distinguishes legs is in here (the digest folds backend and lookFlavor in, so the key must not be coarser than it).
    # Venue '*' gives the venue-less key report mode groups a pair under.
    param([string]$Card, [string]$LegId, $Backend, $LookFlavor, [string]$Venue)
    return (@($Card.ToLowerInvariant(), $LegId.ToLowerInvariant(), [string]$Backend, (Get-VeFlavorKey $LookFlavor), $Venue.ToLowerInvariant()) -join '|')
}

function New-VeBarrier {
    <#
    A receipt file that could not be turned into a record (not JSON, not an object, oversized, failed validation, or
    misfiled) still TOOK PLACE: it is a MALFORMED ATTEMPT. What it could belong to is read from the folder it sits in AND
    from what it claims about itself.
      keyed   = every part of its identity (card, leg, venue, backend, lookFlavor) is readable and unambiguous AND it has a
                real finishedUtc not before its startedUtc. A keyed barrier is an attempt of ITS OWN leg: it can be that
                leg's newest attempt and then withholds that leg (and only that leg).
      unkeyed = anything else (unreadable part, folder and content disagree, no usable finish time). It cannot be placed
                in a leg, so it withholds the whole card it could belong to. A wildcard ($null list) matches anything.
    #>
    param([string[]]$Rel, $Parsed, [string[]]$Reasons)
    $n = $Rel.Count
    $cards = [System.Collections.Generic.List[string]]::new()
    $legs = [System.Collections.Generic.List[string]]::new()
    $venues = [System.Collections.Generic.List[string]]::new()
    $unclean = $false
    $backendKnown = $false; $backend = $null
    $flavorKnown = $false; $flavor = $null
    $finished = $null
    if ($n -ge 2) { $cards.Add($Rel[0]) }
    if ($n -ge 3) { $legs.Add($Rel[1]) }
    if ($n -ge 4) { if ($Rel[2] -in $script:KnownVenues) { $venues.Add($Rel[2].ToLowerInvariant()) } else { $unclean = $true } }
    $isDict = ($Parsed -is [System.Collections.IDictionary])
    if ($isDict) {
        $c = Get-VeValue $Parsed 'card'
        if ($null -ne $c) { if (Test-VeString $c) { $cards.Add([string]$c) } else { $unclean = $true } }
        $l = Get-VeValue $Parsed 'legId'
        if ($null -ne $l) { if (Test-VeString $l) { $legs.Add([string]$l) } else { $unclean = $true } }
        $venue = Get-VeValue $Parsed 'venue'
        if ($venue -is [System.Collections.IDictionary]) {
            $vn = Get-VeValue $venue 'name'
            if ($null -ne $vn) { if ($vn -is [string] -and $vn -cin $script:KnownVenues) { $venues.Add([string]$vn) } else { $unclean = $true } }
        } elseif ($null -ne $venue) { $unclean = $true }
        $subject = Get-VeValue $Parsed 'subject'
        if ($subject -is [System.Collections.IDictionary]) {
            $b = Get-VeValue $subject 'backend'
            if ($null -eq $b) { $backendKnown = $true }
            elseif ($b -is [string] -and $b -cin $script:KnownBackends) { $backendKnown = $true; $backend = [string]$b }
            else { $unclean = $true }
            $lf = Get-VeValue $subject 'lookFlavor'
            if ($null -eq $lf) { $flavorKnown = $true }
            elseif ($lf -is [string]) { $flavorKnown = $true; $flavor = $lf }
            else { $unclean = $true }
        } else { $unclean = $true }
        $finished = ConvertTo-VeInstant (Get-VeValue $Parsed 'finishedUtc')
        $started = ConvertTo-VeInstant (Get-VeValue $Parsed 'startedUtc')
        # A finish time before the start time is not an instant anyone can order by.
        if ($null -ne $finished -and $null -ne $started -and $finished -lt $started) { $finished = $null }
    }
    $distinct = { param($list) @($list | ForEach-Object { $_.ToLowerInvariant() } | Select-Object -Unique) }
    $keyed = $isDict -and -not $unclean -and $backendKnown -and $flavorKnown -and ($null -ne $finished) -and
        (@(& $distinct $cards).Count -eq 1) -and (@(& $distinct $legs).Count -eq 1) -and (@(& $distinct $venues).Count -eq 1)
    $identity = $null
    if ($keyed) { $identity = @{ card = $cards[0]; legId = $legs[0]; backend = $backend; lookFlavor = $flavor; venue = $venues[0] } }
    return @{
        path     = ($Rel -join '\')
        reasons  = @($Reasons)
        cards    = $(if ($cards.Count -gt 0) { @($cards.ToArray()) } else { $null })
        legs     = $(if ($legs.Count -gt 0) { @($legs.ToArray()) } else { $null })
        venues   = $(if ($venues.Count -gt 0) { @($venues.ToArray()) } else { $null })
        finished = $finished
        keyed    = $keyed
        identity = $identity
    }
}

function Test-VeCardWideApplies {
    # Does an UNKEYED barrier withhold this card? A wildcard ($null) matches anything; otherwise the barrier must name
    # the card, one of the venues in question and (when the answer is scoped to one leg) that leg, all case-insensitively.
    param($Barrier, [string]$Card, [string[]]$Venues, [string]$LegId)
    if ($null -ne $Barrier.cards -and @($Barrier.cards | Where-Object { $_ -ieq $Card }).Count -eq 0) { return $false }
    if ($null -ne $Barrier.venues -and @($Barrier.venues | Where-Object { $_ -in $Venues }).Count -eq 0) { return $false }
    if ($LegId -and $null -ne $Barrier.legs -and @($Barrier.legs | Where-Object { $_ -ieq $LegId }).Count -eq 0) { return $false }
    return $true
}

function Get-VeNewestAttempts {
    <#
    THE canonical selection. Both acceptance mode and report mode consume ONLY this function's output; no other code
    decides what is newest, what is current or whether a pair is complete.
    Input is EVERY receipt of the receipts root, never a filtered subset: $Participants = every parsed receipt (valid,
    refused, withheld), each already carrying its VALIDATED STATE (Get-VeValidatedState), and $Barriers = every file that
    could not be parsed (New-VeBarrier). Output:
      legs     one per leg key (card|legId|backend|lookFlavor|venue): every attempt in order (ascending finishedUtc; exact
               tie -> non-signal, then FAIL, then PASS, then id), `newest` (the last one) and `state` = the newest attempt's
               state. A keyed barrier is an attempt of its own leg with state MALFORMED_NEWER_RECEIPT.
      cardWide the unkeyed barriers: they belong to no leg, so each withholds the whole card it could belong to.
    Filters (-SubjectDigest, -BuildManifestSha256, -LegId, -Card) never change which attempt is newest; they only ask
    whether the newest attempt is the one wanted.
    #>
    param($Participants, $Barriers)
    $byKey = @{}
    foreach ($r in $Participants) {
        $key = Get-VeLegKey $r.card $r.legId $r.backend $r.lookFlavor $r.venue
        if (-not $byKey.ContainsKey($key)) { $byKey[$key] = [System.Collections.Generic.List[object]]::new() }
        $byKey[$key].Add($r)
    }
    $cardWide = [System.Collections.Generic.List[object]]::new()
    foreach ($b in $Barriers) {
        if (-not $b.keyed) { $cardWide.Add($b); continue }
        $i = $b.identity
        $attempt = @{ isBarrier = $true; state = 'MALFORMED_NEWER_RECEIPT'; refusal = $null; receiptId = [string]$b.path; path = [string]$b.path
            finished = $b.finished; card = $i.card; legId = $i.legId; backend = $i.backend; lookFlavor = $i.lookFlavor; venue = $i.venue
            digest = $null; buildManifest = $null; outcome = $null; reasons = @($b.reasons) }
        $key = Get-VeLegKey $i.card $i.legId $i.backend $i.lookFlavor $i.venue
        if (-not $byKey.ContainsKey($key)) { $byKey[$key] = [System.Collections.Generic.List[object]]::new() }
        $byKey[$key].Add($attempt)
    }
    $legs = [System.Collections.Generic.List[object]]::new()
    foreach ($key in ($byKey.Keys | Sort-Object { $_ })) {
        $sorted = Sort-VeReceipts $byKey[$key]
        $newest = $sorted[$sorted.Count - 1]
        # the newest PARSED attempt at each subject digest (for listing superseded digests; never evidence)
        $byDigest = @{}
        foreach ($a in $sorted) { if (-not $a.isBarrier) { $byDigest[$a.digest] = $a } }
        $legs.Add(@{
                newestByDigest = $byDigest
                key      = $key
                groupKey = (Get-VeLegKey $newest.card $newest.legId $newest.backend $newest.lookFlavor '*')
                card     = $newest.card; legId = $newest.legId; backend = $newest.backend; lookFlavor = $newest.lookFlavor; venue = $newest.venue
                attempts = $sorted
                newest   = $newest
                state    = $newest.state
                isBarrier = [bool]$newest.isBarrier
                signal   = ((-not $newest.isBarrier) -and ($newest.state -cin $script:SignalOutcomes))
            })
    }
    return @{ legs = $legs.ToArray(); cardWide = $cardWide.ToArray() }
}

function Get-VeValidatedState {
    <#
    THE one definition of what a parsed receipt amounts to as an ATTEMPT. Only PASS and FAIL carry signal, and only when
    the receipt is valid: its id is unique, its recorded role is the table's, its host was the venue it names, and its clip
    is proven long (>= 20 s) and not looped. Everything else is a typed non-signal state: a refusal (DUPLICATE_RECEIPT_ID,
    ROLE_MISMATCH, VENUE_DETECTION_MISMATCH, INVALID_LOOPED, INVALID_CLIP_TOO_SHORT), UNVERIFIED_CLIP_LENGTH, or the
    receipt's own non-signal outcome (UNRESOLVED, RETRACTED, VENUE_UNHEALTHY, ...).
    #>
    param($Record, $Table, [int]$IdCount)
    if ($IdCount -gt 1) { return 'DUPLICATE_RECEIPT_ID' }
    if ($Table.present -and $Record.role -cne (Get-VeExpectedRole -Table $Table -Card $Record.card -Venue $Record.venue)) { return 'ROLE_MISMATCH' }
    if ($Record.outcome -cin $script:SignalOutcomes) {
        if ($Record.declared -isnot [string] -or $Record.detected -isnot [string] -or $Record.declared -ine $Record.venue -or $Record.detected -ine $Record.venue) {
            return 'VENUE_DETECTION_MISMATCH'
        }
        if ($Record.clipState -ceq 'LOOPED') { return 'INVALID_LOOPED' }
        if ($Record.clipState -ceq 'SHORT') { return 'INVALID_CLIP_TOO_SHORT' }
        if ($Record.clipState -ceq 'UNVERIFIED') { return 'UNVERIFIED_CLIP_LENGTH' }
    }
    return [string]$Record.outcome
}

# ---------------------------------------------------------------- comparison helpers

function Sort-VeReceipts {
    # Ascending by finishedUtc instant. An exact tie is broken fail-closed, never by a random id: a non-signal
    # receipt (retraction, unresolved, ...) outranks FAIL, FAIL outranks PASS, then receiptId (ordinal). The LAST
    # element is "latest".
    param($Records)
    $list = [System.Collections.Generic.List[object]]::new()
    foreach ($r in $Records) { $list.Add($r) }
    $list.Sort([System.Comparison[object]] {
            param($a, $b)
            $c = $a.finished.UtcTicks.CompareTo($b.finished.UtcTicks)
            if ($c -ne 0) { return $c }
            $c = (Get-VeTieRank $a.state).CompareTo((Get-VeTieRank $b.state))
            if ($c -ne 0) { return $c }
            return [string]::CompareOrdinal($a.receiptId, $b.receiptId)
        })
    return , $list.ToArray()
}

function Test-VeNumber {
    param($Value)
    return (($Value -is [int]) -or ($Value -is [long]) -or ($Value -is [double]) -or ($Value -is [decimal]) -or ($Value -is [single]))
}

function Get-VeTieRank {
    # $State is a receipt's state (outcome, UNVERIFIED_CLIP_LENGTH or a refusal reason): anything that is not a plain
    # PASS/FAIL outranks both, so an exact tie never resolves toward a pass.
    param([string]$State)
    if ($State -ceq 'PASS') { return 0 }
    if ($State -ceq 'FAIL') { return 1 }
    return 2
}

function Get-VeClipLength {
    <#
    Owner rule 2026-09-30. Reads metrics.clipSeconds (or the app summary's clip_seconds) and metrics.wrapped.
    State: LOOPED (wrapped == 1) | SHORT (clipSeconds < 20) | UNVERIFIED (a field absent, or not a number / 0|1)
    | OK. A known wrap or a known short clip is INVALID even when the other field is missing.
    #>
    param($Metrics)
    $secs = $null
    $wrap = $null
    if ($Metrics -is [System.Collections.IDictionary]) {
        # Both spellings present: the STRICTER reading decides (the smaller length); one of them unreadable -> unverified.
        # Never "the first one found", which would let the lenient spelling win.
        $rawSecs = $null
        $secsList = [System.Collections.Generic.List[object]]::new()
        foreach ($n in 'clipSeconds', 'clip_seconds') {
            $v = Get-VeValue $Metrics $n
            if ($null -ne $v) { $secsList.Add($v) }
        }
        if ($secsList.Count -gt 0) {
            $bad = @($secsList | Where-Object { -not (Test-VeNumber $_) -or [double]::IsNaN([double]$_) })
            if ($bad.Count -eq 0) { $rawSecs = ($secsList | ForEach-Object { [double]$_ } | Measure-Object -Minimum).Minimum }
        }
        $rawWrap = Get-VeValue $Metrics 'wrapped'
        if ((Test-VeNumber $rawSecs) -and -not [double]::IsNaN([double]$rawSecs)) { $secs = [double]$rawSecs }
        if ($rawWrap -is [bool]) { $wrap = [int]$rawWrap }
        elseif ((Test-VeNumber $rawWrap) -and (([double]$rawWrap -eq 0) -or ([double]$rawWrap -eq 1))) { $wrap = [int][double]$rawWrap }
    }
    $state = 'OK'
    if ($wrap -eq 1) { $state = 'LOOPED' }
    elseif ($null -ne $secs -and $secs -lt $script:MinClipSeconds) { $state = 'SHORT' }
    elseif ($null -eq $secs -or $null -eq $wrap) { $state = 'UNVERIFIED' }
    return @{ state = $state; clipSeconds = $secs; wrapped = $wrap }
}

function ConvertTo-VeReceiptView {
    param($Record, [bool]$RoleVerified, [bool]$Full, $History = $null)
    $clipLength = $(if ($Record.clipState -ceq 'UNVERIFIED') { 'UNVERIFIED_CLIP_LENGTH' } else { $Record.clipState })
    $view = [ordered]@{
        receiptId     = $Record.receiptId
        role          = $Record.role
        roleVerified  = $RoleVerified
        clipLength    = $clipLength
        outcome       = $Record.outcome
        refusal       = $Record.refusal
        outcomeDetail = $Record.outcomeDetail
        startedUtc    = (Format-VeInstant $Record.started)
        finishedUtc   = (Format-VeInstant $Record.finished)
        umRunOutcome  = $Record.umRunOutcome
        path          = $Record.path
        metrics       = $Record.metrics
    }
    if ($Full) {
        $view = [ordered]@{
            receiptId     = $Record.receiptId
            card          = $Record.card
            legId         = $Record.legId
            backend       = $Record.backend
            subjectDigest = $Record.digest
            buildManifestSha256 = $Record.buildManifest
            legSpecSha256 = $Record.legSpec
            clipId        = $Record.clipId
            lookFlavor    = $Record.lookFlavor
            venue         = $Record.venue
            role          = $Record.role
            roleVerified  = $RoleVerified
            clipLength    = $clipLength
            clipSeconds   = $Record.clipSeconds
            wrapped       = $Record.wrapped
            outcome       = $Record.outcome
            refusal       = $Record.refusal
            outcomeDetail = $Record.outcomeDetail
            startedUtc    = (Format-VeInstant $Record.started)
            finishedUtc   = (Format-VeInstant $Record.finished)
            umRunOutcome  = $Record.umRunOutcome
            history       = $History
            path          = $Record.path
            metrics       = $Record.metrics
        }
    }
    return $view
}

function Get-VeDelta {
    # DIAGNOSTIC only. Built solely for a COMPLETE pair; metrics are copied side by side, never recomputed.
    param($Venues, $LatestByVenue)
    $keys = [System.Collections.Generic.SortedSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($v in $Venues) { foreach ($k in $LatestByVenue[$v].metrics.Keys) { [void]$keys.Add([string]$k) } }
    $metrics = [ordered]@{}
    foreach ($k in $keys) {
        $values = [ordered]@{}
        $missing = @()
        foreach ($v in $Venues) {
            $m = $LatestByVenue[$v].metrics
            $hit = $null
            foreach ($mk in $m.Keys) { if ([string]$mk -ceq $k) { $hit = $mk; break } }
            if ($null -eq $hit) { $values[$v] = $null; $missing += $v } else { $values[$v] = $m[$hit] }
        }
        $distinct = @($Venues | ForEach-Object { ConvertTo-Json -InputObject $values[$_] -Compress -Depth 20 } | Select-Object -Unique)
        $differs = ($missing.Count -gt 0) -or ($distinct.Count -gt 1)
        $diff = $null
        if ($Venues.Count -eq 2 -and (Test-VeNumber $values[$Venues[0]]) -and (Test-VeNumber $values[$Venues[1]])) {
            $diff = $values[$Venues[1]] - $values[$Venues[0]]
        }
        $metrics[$k] = [ordered]@{ values = $values; differs = $differs; diff = $diff; missingOn = @($missing) }
    }
    return [ordered]@{
        label   = 'DIAGNOSTIC'
        note    = 'Side-by-side measurements at one subject digest. Not a verdict: each receipt is judged only against its own venue.'
        venues  = @($Venues)
        diffSign = if ($Venues.Count -eq 2) { "$($Venues[1]) minus $($Venues[0])" } else { $null }
        metrics = $metrics
    }
}

# ---------------------------------------------------------------- the report

function Get-VenueEvidenceReport {
    <#
    Scans $ReceiptsRoot, validates every receipt, then returns the report as an ordered dictionary (schema
    mlv-app/dual-venue-evidence-report/v1). With -AcceptanceFor <card> the result is acceptance-only: it carries
    receipts whose role is `acceptance` for that card and nothing from any supplementary venue.
    BOTH modes read the NEWEST ATTEMPT per leg from Get-VeNewestAttempts, computed once over every receipt.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$ReceiptsRoot,
        [Parameter(Mandatory)][string]$VenueTablePath,
        [string]$Card,
        [string]$LegId,
        [string]$AcceptanceFor,
        [string]$SubjectDigest,
        [string]$BuildManifestSha256,
        [int]$MaxReceipts = 20000
    )
    # A parameter that is BOUND but empty/blank is refused: an unset caller variable must never silently switch modes
    # (report instead of acceptance) or drop a filter (an unbound-looking query is broader than the one asked for).
    foreach ($n in 'Card', 'LegId', 'AcceptanceFor', 'SubjectDigest', 'BuildManifestSha256') {
        if ($PSBoundParameters.ContainsKey($n) -and [string]::IsNullOrWhiteSpace([string]$PSBoundParameters[$n])) {
            throw "-$n was supplied but is empty or blank; omit the parameter instead (an empty value never means 'no filter' or 'report mode')"
        }
    }
    if ($Card -and $AcceptanceFor -and $Card -ine $AcceptanceFor) { throw "-Card '$Card' conflicts with -AcceptanceFor '$AcceptanceFor'; acceptance is asked for one card" }
    foreach ($f in @(@('SubjectDigest', $SubjectDigest), @('BuildManifestSha256', $BuildManifestSha256))) {
        if (-not $f[1]) { continue }
        if (-not $AcceptanceFor) { throw "-$($f[0]) applies to -AcceptanceFor only" }
        if ($f[1] -notmatch '^[0-9a-fA-F]{64}$') { throw "-$($f[0]) must be a 64-hex sha256 string" }
    }
    $table = Read-VenueTable -Path $VenueTablePath
    $rootExists = Test-Path -LiteralPath $ReceiptsRoot -PathType Container
    $scanned = 0
    $malformed = [System.Collections.Generic.List[object]]::new()
    $refused = [System.Collections.Generic.List[object]]::new()
    $structural = [System.Collections.Generic.List[object]]::new()
    $barriers = [System.Collections.Generic.List[object]]::new()

    if ($rootExists) {
        foreach ($entry in (Get-VeReceiptFiles -Root $ReceiptsRoot -Max $MaxReceipts)) {
            $scanned++
            $rel = ($entry.rel -join '\')
            $placed = ($entry.rel.Count -eq 4)
            if ($entry.file.Length -gt $script:MaxReceiptBytes) {
                $why = @("file is larger than $($script:MaxReceiptBytes) bytes; not read")
                $malformed.Add([ordered]@{ path = $rel; reasons = $why })
                # Unread, so unorderable: inside the layout it may be the newest attempt of its leg.
                if ($placed) { $barriers.Add((New-VeBarrier -Rel $entry.rel -Parsed $null -Reasons $why)) }
                continue
            }
            $parsed = $null
            try {
                $parsed = ConvertFrom-VeJsonText -Text (Get-Content -LiteralPath $entry.file.FullName -Raw -Encoding UTF8)
            } catch {
                $why = @('not valid JSON')
                $malformed.Add([ordered]@{ path = $rel; reasons = $why })
                if ($placed) { $barriers.Add((New-VeBarrier -Rel $entry.rel -Parsed $null -Reasons $why)) }
                continue
            }
            $checked = ConvertTo-VeRecord -Receipt $parsed -RelParts $entry.rel
            if ($checked.reasons.Count -gt 0) {
                $malformed.Add([ordered]@{ path = $rel; reasons = @($checked.reasons) })
                # A file that sits in the layout, or that calls itself a receipt anywhere, took place: it is a barrier.
                # (any schema version: a v2 file misfiled one level off is still somebody's newer attempt)
                $claimsReceipt = $false
                if ($parsed -is [System.Collections.IDictionary]) {
                    $sch = Get-VeValue $parsed 'schema'
                    $claimsReceipt = (($sch -is [string]) -and ($sch -clike 'mlv-app/dual-venue-receipt*')) -or
                        ($null -ne (Get-VeValue $parsed 'receiptId') -and $null -ne (Get-VeValue $parsed 'outcome'))
                }
                if ($placed -or $claimsReceipt) { $barriers.Add((New-VeBarrier -Rel $entry.rel -Parsed $parsed -Reasons @($checked.reasons))) }
                continue
            }
            $checked.record.path = $rel
            $structural.Add($checked.record)
        }
    }

    # Duplicate receiptIds: the ledger is append-only and ids are unique, so two files claiming one id cannot
    # both be trusted and we cannot tell which came first.
    $byId = @{}
    foreach ($r in $structural) {
        $id = $r.receiptId.ToLowerInvariant()
        if (-not $byId.ContainsKey($id)) { $byId[$id] = 0 }
        $byId[$id]++
    }
    # EVERY parsed receipt is a participant, refused or not; its state is the one Get-VeValidatedState defines.
    $valid = 0
    foreach ($r in $structural) {
        $r.state = Get-VeValidatedState -Record $r -Table $table -IdCount $byId[$r.receiptId.ToLowerInvariant()]
        if ($r.state -cin $script:RefusalStates) {
            $r.refusal = $r.state
            $refused.Add([ordered]@{ path = $r.path; receiptId = $r.receiptId; card = $r.card; legId = $r.legId; venue = $r.venue; reason = $r.state })
        } else {
            $valid++
        }
    }
    # THE ONE selection, over every receipt of the root (no card, leg, subject or build filter applied yet).
    $newest = Get-VeNewestAttempts -Participants $structural -Barriers $barriers

    $report = [ordered]@{
        schema             = $script:ReportSchema
        mode               = $(if ($AcceptanceFor) { 'acceptance' } else { 'report' })
        generatedUtc       = (Format-VeInstant ([System.DateTimeOffset]::UtcNow))
        receiptsRoot       = $ReceiptsRoot
        receiptsRootExists = $rootExists
        venueTable         = [ordered]@{ path = $table.path; present = $table.present }
        counts             = [ordered]@{ scanned = $scanned; valid = $valid; malformed = $malformed.Count; refused = $refused.Count }
    }

    if ($AcceptanceFor) {
        # Acceptance needs an AUTHORITATIVE role: a present, valid venue table that lists this card. A receipt's own
        # venue.role is a label, never the authority, so without the table there is nothing to read.
        $blocked = $null
        $acceptVenues = @()
        if (-not $table.present) { $blocked = 'VENUE_TABLE_ABSENT' }
        elseif ($null -eq (Find-VeKey $table.roles $AcceptanceFor)) { $blocked = 'CARD_NOT_IN_VENUE_TABLE' }
        else {
            # Only a venue the table EXPLICITLY names acceptance for this card; defaultRole never makes one.
            foreach ($v in $script:KnownVenues) {
                if ((Get-VeExplicitRole -Table $table -Card $AcceptanceFor -Venue $v) -ceq 'acceptance') { $acceptVenues += $v }
            }
            if ($acceptVenues.Count -eq 0) { $blocked = 'NO_ACCEPTANCE_VENUE_FOR_CARD' }
        }

        # A leg is acceptance evidence only if its NEWEST attempt (whatever its digest or build) is a clean PASS/FAIL and,
        # when a subject or build is pinned, is AT that subject/build. A filter never picks among attempts: a newest
        # attempt outside it withholds the leg (NEWER_ATTEMPT_OUTSIDE_FILTER). There is no fallback to an older receipt.
        $filtered = [bool]($SubjectDigest -or $BuildManifestSha256)
        $receipts = [System.Collections.Generic.List[object]]::new()
        $withheld = [System.Collections.Generic.List[object]]::new()
        $olderDigests = [System.Collections.Generic.List[object]]::new()
        $failCount = 0
        if (-not $blocked) {
            foreach ($leg in $newest.legs) {
                if ($leg.card -ine $AcceptanceFor) { continue }
                if ($leg.venue -cnotin $acceptVenues) { continue }
                if ($LegId -and $leg.legId -ine $LegId) { continue }
                $top = $leg.newest
                $superseded = @($leg.attempts | Where-Object { -not $_.isBarrier -and $_.receiptId -cne $top.receiptId -and $_.state -cin $script:SignalOutcomes } | ForEach-Object { $_.receiptId })
                $entry = [ordered]@{ legId = $leg.legId; backend = $leg.backend; lookFlavor = $leg.lookFlavor; subjectDigest = $top.digest; venue = $leg.venue
                    receiptId = $(if ($leg.isBarrier) { $null } else { $top.receiptId }); reason = $null }
                if ($leg.isBarrier) {
                    $entry.reason = 'MALFORMED_NEWER_RECEIPT'
                    $entry['blockedBy'] = @($top.path)
                    $entry['supersededSignal'] = $superseded
                    $withheld.Add($entry)
                    continue
                }
                # Older subject digests of this leg: listed, never evidence.
                foreach ($d in @($leg.newestByDigest.Keys | Where-Object { $_ -cne $top.digest } | Sort-Object { $_ })) {
                    $latestOfDigest = $leg.newestByDigest[$d]
                    $olderDigests.Add([ordered]@{ legId = $latestOfDigest.legId; backend = $latestOfDigest.backend; lookFlavor = $latestOfDigest.lookFlavor
                            venue = $latestOfDigest.venue; subjectDigest = $d; latestOutcome = $latestOfDigest.state; receiptId = $latestOfDigest.receiptId })
                }
                $inFilter = (-not $SubjectDigest -or $top.digest -ceq $SubjectDigest.ToLowerInvariant()) -and
                    (-not $BuildManifestSha256 -or $top.buildManifest -ieq $BuildManifestSha256)
                if (-not $inFilter) {
                    $entry.reason = 'NEWER_ATTEMPT_OUTSIDE_FILTER'
                    $entry['newestState'] = $top.state
                    $entry['newestBuildManifestSha256'] = $top.buildManifest
                    $entry['newestFinishedUtc'] = (Format-VeInstant $top.finished)
                    $entry['supersededSignal'] = @($superseded + @($(if ($top.state -cin $script:SignalOutcomes) { $top.receiptId })))
                    $withheld.Add($entry)
                } elseif ($leg.signal) {
                    if ($top.outcome -ceq 'FAIL') { $failCount++ }
                    $atDigest = @($leg.attempts | Where-Object { -not $_.isBarrier -and $_.digest -ceq $top.digest })
                    $hist = [ordered]@{}
                    foreach ($o in ($atDigest | ForEach-Object { $_.state } | Sort-Object -Unique)) { $hist[$o] = @($atDigest | Where-Object { $_.state -ceq $o }).Count }
                    $receipts.Add((ConvertTo-VeReceiptView -Record $top -RoleVerified $true -Full $true -History ([ordered]@{ total = $atDigest.Count; byOutcome = $hist })))
                } else {
                    $entry.reason = $top.state
                    $entry['supersededSignal'] = $superseded
                    $withheld.Add($entry)
                }
            }
            # A receipt file that cannot be placed in any leg withholds the whole card it could belong to.
            foreach ($b in $newest.cardWide) {
                if (-not (Test-VeCardWideApplies -Barrier $b -Card $AcceptanceFor -Venues $acceptVenues -LegId $LegId)) { continue }
                $withheld.Add([ordered]@{ legId = $(if ($b.legs) { @($b.legs)[0] } else { '*' }); backend = $null; lookFlavor = $null; subjectDigest = $null
                        venue = $(if ($b.venues) { @($b.venues)[0] } else { '*' }); receiptId = $null; scope = 'CARD'
                        reason = $(if ($null -eq $b.finished) { 'UNORDERABLE_MALFORMED_RECEIPT' } else { 'UNKEYABLE_MALFORMED_RECEIPT' })
                        blockedBy = @($b.path); supersededSignal = @() })
            }
        }

        # ACCEPTANCE_EVIDENCE means every attempted acceptance leg's newest receipt is a clean PASS/FAIL: one withheld
        # leg makes the whole card non-green (the healthy legs stay listed under receipts, but are not an acceptance).
        $reason = $blocked
        if (-not $reason -and $withheld.Count -gt 0) { $reason = 'NEWEST_ATTEMPT_WITHHELD' }
        if (-not $reason -and $receipts.Count -eq 0) { $reason = 'NO_ACCEPTANCE_RECEIPTS' }
        $report['acceptance'] = [ordered]@{
            card                = $AcceptanceFor
            status              = $(if ($receipts.Count -gt 0 -and $withheld.Count -eq 0) { 'ACCEPTANCE_EVIDENCE' } else { 'NO_ACCEPTANCE_EVIDENCE' })
            reason              = $reason
            failCount           = $failCount
            digestSelection     = $(if ($filtered) { 'FILTERED' } else { 'NEWEST_PER_LEG' })
            digestSelectionNote = $(if ($filtered) { 'Filtered by -SubjectDigest and/or -BuildManifestSha256: the filter never chooses among attempts. Each leg answers only if its NEWEST attempt (whatever its subject or build) is at the requested subject/build; a leg whose newest attempt is elsewhere is withheld (NEWER_ATTEMPT_OUTSIDE_FILTER), never answered from an older receipt.' }
                else { 'No -SubjectDigest/-BuildManifestSha256 filter: only the NEWEST attempt per leg (card, leg, backend, lookFlavor, venue) decides, at whatever digest; older digests are listed under olderDigests and are never evidence. Bind to the candidate build with -BuildManifestSha256.' })
            filters             = [ordered]@{ legId = $(if ($LegId) { $LegId } else { $null }); subjectDigest = $(if ($SubjectDigest) { $SubjectDigest.ToLowerInvariant() } else { $null })
                buildManifestSha256 = $(if ($BuildManifestSha256) { $BuildManifestSha256.ToLowerInvariant() } else { $null }) }
            acceptanceVenues    = @($acceptVenues)
            receipts            = @($receipts.ToArray())
            withheld            = @($withheld.ToArray())
            olderDigests        = @($olderDigests.ToArray())
        }
        $report['malformed'] = @($malformed.ToArray())
        $report['refused'] = @($refused.ToArray() | Where-Object { $_.card -ieq $AcceptanceFor })
        return $report
    }

    # ---- report mode: one pair per (card, leg, backend, lookFlavor, newest subject digest). Each venue row is that
    # venue's NEWEST attempt from Get-VeNewestAttempts; a venue whose newest attempt is a malformed file, or sits at another
    # digest, is shown as exactly that, never as an older receipt. A pair is COMPLETE only when every expected venue's
    # newest attempt is a validated PASS/FAIL at this digest.
    $pairs = @{}
    foreach ($leg in $newest.legs) {
        if ($Card -and $leg.card -ine $Card) { continue }
        if ($LegId -and $leg.legId -ine $LegId) { continue }
        if (-not $pairs.ContainsKey($leg.groupKey)) { $pairs[$leg.groupKey] = [System.Collections.Generic.List[object]]::new() }
        $pairs[$leg.groupKey].Add($leg)
    }
    $groups = [System.Collections.Generic.List[object]]::new()
    $notes = [System.Collections.Generic.List[string]]::new()
    foreach ($gk in ($pairs.Keys | Sort-Object { $_ })) {
        $legsOfPair = $pairs[$gk]
        $first = $legsOfPair[0]
        $expected = @(Get-VeExpectedVenues -Table $table -Card $first.card)
        $observed = @($legsOfPair | ForEach-Object { $_.venue } | Select-Object -Unique)
        $allVenues = @($expected + @($observed | Where-Object { $_ -notin $expected }))
        $legByVenue = @{}
        foreach ($l in $legsOfPair) { $legByVenue[$l.venue] = $l }
        $digests = @($legsOfPair | Where-Object { -not $_.isBarrier } | ForEach-Object { $_.newest.digest } | Sort-Object -Unique)
        if ($digests.Count -eq 0) { $digests = @($null) }
        $cardWideHere = @($newest.cardWide | Where-Object { Test-VeCardWideApplies -Barrier $_ -Card $first.card -Venues $script:KnownVenues -LegId $LegId })

        foreach ($d in $digests) {
            $venueRows = [ordered]@{}
            $latestByVenue = @{}
            $reasons = [System.Collections.Generic.List[object]]::new()
            foreach ($v in $allVenues) {
                $leg = $(if ($legByVenue.ContainsKey($v)) { $legByVenue[$v] } else { $null })
                if ($null -eq $leg) {
                    $venueRows[$v] = [ordered]@{ expected = ($v -in $expected); status = 'missing'; latest = $null; history = [ordered]@{ total = 0; byOutcome = [ordered]@{} } }
                    if ($v -in $expected) { $reasons.Add([ordered]@{ venue = $v; reason = 'missing' }) }
                    continue
                }
                $mine = @($leg.attempts | Where-Object { -not $_.isBarrier })
                $hist = [ordered]@{}
                foreach ($o in ($mine | ForEach-Object { $_.outcome } | Sort-Object -Unique)) { $hist[$o] = @($mine | Where-Object { $_.outcome -eq $o }).Count }
                $row = [ordered]@{ expected = ($v -in $expected); status = $leg.state; latest = $null; history = [ordered]@{ total = $mine.Count; byOutcome = $hist } }
                if ($leg.isBarrier) {
                    $row['blockedBy'] = @($leg.newest.path)
                } elseif ($leg.newest.digest -cne $d) {
                    $row.status = 'NEWEST_ATTEMPT_AT_OTHER_DIGEST'
                    $row['newestSubjectDigest'] = $leg.newest.digest
                } else {
                    $row.latest = ConvertTo-VeReceiptView -Record $leg.newest -RoleVerified $table.present -Full $false
                    $latestByVenue[$v] = $leg.newest
                }
                $venueRows[$v] = $row
                if ($v -in $expected -and -not ($leg.signal -and $row.status -ceq $leg.state)) { $reasons.Add([ordered]@{ venue = $v; reason = $row.status }) }
            }
            foreach ($b in $cardWideHere) {
                $reasons.Add([ordered]@{ venue = '*'; reason = $(if ($null -eq $b.finished) { 'UNORDERABLE_MALFORMED_RECEIPT' } else { 'UNKEYABLE_MALFORMED_RECEIPT' }); blockedBy = @($b.path) })
            }
            $complete = ($reasons.Count -eq 0)
            $g = [ordered]@{
                card            = $first.card
                legId           = $first.legId
                backend         = $first.backend
                lookFlavor      = $first.lookFlavor
                subjectDigest   = $d
                expectedVenues  = @($expected)
                venues          = $venueRows
                pair            = [ordered]@{ state = $(if ($complete) { 'COMPLETE' } else { 'INCOMPLETE' }); reasons = @($reasons.ToArray()) }
            }
            if ($complete) { $g['delta'] = Get-VeDelta -Venues $expected -LatestByVenue $latestByVenue }
            $groups.Add($g)
        }
        if ($digests.Count -gt 1) {
            $legName = "$($first.card)/$($first.legId)" + $(if ($first.backend) { " [$($first.backend)]" } else { '' }) + $(if ($first.lookFlavor) { " look=$($first.lookFlavor)" } else { '' })
            $short = ($digests | ForEach-Object { $_.Substring(0, 12) }) -join ', '
            $notes.Add("$legName has $($digests.Count) subject digests ($short): receipts from different subject digests are never compared.")
        }
    }
    $report['groups'] = @($groups.ToArray())
    $report['digestNotes'] = @($notes.ToArray())
    $report['malformed'] = @($malformed.ToArray())
    $report['refused'] = @($refused.ToArray())
    return $report
}

# ---------------------------------------------------------------- human output

function Format-VenueEvidenceText {
    param($Report)
    $out = [System.Collections.Generic.List[string]]::new()
    $c = $Report.counts
    $out.Add("DUAL-VENUE EVIDENCE ($($Report.schema))")
    $out.Add("receipts root : $($Report.receiptsRoot)" + $(if (-not $Report.receiptsRootExists) { '  (does not exist: zero receipts)' } else { '' }))
    $absent = $(if ($Report.mode -eq 'acceptance') { '  (ABSENT: acceptance refused, recorded roles are never trusted)' } else { '  (ABSENT: recorded roles are UNVERIFIED)' })
    $out.Add("venue table   : $($Report.venueTable.path)" + $(if ($Report.venueTable.present) { '' } else { $absent }))
    $out.Add("receipts      : $($c.scanned) scanned, $($c.valid) counted, $($c.malformed) MALFORMED, $($c.refused) refused")
    $out.Add('')

    if ($Report.mode -eq 'acceptance') {
        $a = $Report.acceptance
        $out.Add("ACCEPTANCE for $($a.card): $($a.status)" + $(if ($a.reason) { " (reason: $($a.reason))" } else { '' }))
        $out.Add("  subject digest selection: $($a.digestSelection) -- $($a.digestSelectionNote)")
        if ($a.filters.legId) { $out.Add("  SCOPE: leg '$($a.filters.legId)' only -- other legs of the card are not part of this answer") }
        foreach ($r in $a.receipts) {
            $short = $r.subjectDigest.Substring(0, 12)
            $flavor = $(if ($r.lookFlavor) { " look=$($r.lookFlavor)" } else { '' })
            $hist = ($r.history.byOutcome.Keys | ForEach-Object { "$_`:$($r.history.byOutcome[$_])" }) -join ','
            $out.Add("  $($r.outcome)  $($r.venue) ($($r.role); role verified against venue table)  leg=$($r.legId)$(if ($r.backend) { " backend=$($r.backend)" })$flavor  clip=$($r.clipId) ($($r.clipSeconds) s, wrapped=$($r.wrapped))  build=$($r.buildManifestSha256.Substring(0, [Math]::Min(12, $r.buildManifestSha256.Length)))  leg-spec=$($r.legSpecSha256.Substring(0, [Math]::Min(12, $r.legSpecSha256.Length)))  digest=$short  finished=$($r.finishedUtc)  receipts=$($r.history.total) [$hist]  receipt=$($r.receiptId)")
        }
        foreach ($w in $a.withheld) {
            $why = switch -CaseSensitive ($w.reason) {
                'UNVERIFIED_CLIP_LENGTH' { 'UNVERIFIED_CLIP_LENGTH: the receipt carries no clipSeconds/wrapped proof of a long, non-looping clip' }
                'MALFORMED_NEWER_RECEIPT' { "MALFORMED_NEWER_RECEIPT: the newest attempt of this leg is a receipt file that failed validation ($($w.blockedBy -join '; '))" }
                'UNKEYABLE_MALFORMED_RECEIPT' { "UNKEYABLE_MALFORMED_RECEIPT: a receipt file whose leg cannot be read took place; it withholds the whole card ($($w.blockedBy -join '; '))" }
                'UNORDERABLE_MALFORMED_RECEIPT' { "UNORDERABLE_MALFORMED_RECEIPT: a receipt file with no usable finish time took place; it withholds the whole card ($($w.blockedBy -join '; '))" }
                'NEWER_ATTEMPT_OUTSIDE_FILTER' { "NEWER_ATTEMPT_OUTSIDE_FILTER: the newest attempt of this leg ($($w.newestState), finished $($w.newestFinishedUtc), build $(if ($w.newestBuildManifestSha256) { $w.newestBuildManifestSha256.Substring(0, [Math]::Min(12, $w.newestBuildManifestSha256.Length)) } else { '-' })) is not at the requested subject/build" }
                default { $(if ($w.reason -cin @('PASS', 'FAIL')) { "latest receipt is $($w.reason)" } elseif ($w.reason -cmatch '^(INVALID_|ROLE_MISMATCH|VENUE_DETECTION_MISMATCH|DUPLICATE_RECEIPT_ID)') { "the newest attempt was refused ($($w.reason)); it is still the newest attempt" } else { "latest receipt is $($w.reason), which carries no signal" }) }
            }
            $sup = $(if ($w.supersededSignal -and @($w.supersededSignal).Count -gt 0) { "; does NOT fall back to older receipt(s) $(@($w.supersededSignal) -join ',')" } else { '' })
            $dig = $(if ($w.subjectDigest) { $w.subjectDigest.Substring(0, 12) } else { '-' })
            $flv = $(if ($w.lookFlavor) { " look=$($w.lookFlavor)" } else { '' })
            $out.Add("  WITHHELD  $($w.venue) leg=$($w.legId)$flv digest=$dig`: $why$sup")
        }
        foreach ($o in $a.olderDigests) {
            $flv = $(if ($o.lookFlavor) { " look=$($o.lookFlavor)" } else { '' })
            $out.Add("  OLDER DIGEST (not evidence)  $($o.venue) leg=$($o.legId)$flv digest=$($o.subjectDigest.Substring(0, 12)) latest=$($o.latestOutcome)")
        }
        if ($a.failCount -gt 0) { $out.Add("  NOTE: $($a.failCount) acceptance receipt(s) say FAIL (exit code 3)") }
    } else {
        foreach ($g in $Report.groups) {
            $be = $(if ($g.backend) { " [$($g.backend)]" } else { '' }) + $(if ($g.lookFlavor) { " look=$($g.lookFlavor)" } else { '' })
            $out.Add("$($g.card) / $($g.legId)$be  digest $(if ($g.subjectDigest) { $g.subjectDigest.Substring(0, 12) } else { '-' })")
            if ($g.pair.state -eq 'COMPLETE') {
                $out.Add('  pair: COMPLETE (every expected venue''s NEWEST attempt is a validated PASS/FAIL at this digest; no merged verdict)')
            } else {
                $why = ($g.pair.reasons | ForEach-Object { "$($_.venue): $($_.reason)" }) -join '; '
                $out.Add("  pair: INCOMPLETE ($why)")
            }
            $out.Add(('  {0,-14} {1,-14} {2,-20} {3,-25} {4}' -f 'venue', 'role', 'latest outcome', 'finished (UTC)', 'receipts'))
            foreach ($v in $g.venues.Keys) {
                $row = $g.venues[$v]
                if ($null -eq $row.latest) {
                    # missing, a malformed newest attempt, or a newest attempt at another digest: never an older receipt
                    $out.Add(('  {0,-14} {1,-14} {2,-20} {3,-25} {4}' -f $v, '-', $row.status, '-', $row.history.total))
                } else {
                    $roleText = $row.latest.role + $(if ($row.latest.roleVerified) { '' } else { ' (UNVERIFIED)' })
                    $clipNote = $(if ($row.latest.clipLength -ceq 'UNVERIFIED_CLIP_LENGTH') { '  UNVERIFIED_CLIP_LENGTH' } else { '' })
                    $out.Add(('  {0,-14} {1,-14} {2,-20} {3,-25} {4}{5}' -f $v, $roleText, $row.status, $row.latest.finishedUtc, $row.history.total, $clipNote))
                }
            }
            if ($g.Contains('delta')) {
                $d = $g.delta
                $out.Add("  DIAGNOSTIC delta (not a verdict; $($d.diffSign))")
                foreach ($k in $d.metrics.Keys) {
                    $m = $d.metrics[$k]
                    $vals = ($m.values.Keys | ForEach-Object { "$_=$($m.values[$_])" }) -join '  '
                    $mark = $(if ($m.differs) { 'differs' } else { 'same' })
                    $dv = $(if ($null -ne $m.diff) { "  diff=$($m.diff)" } else { '' })
                    $out.Add("    $k : $vals  [$mark]$dv")
                }
            }
            $out.Add('')
        }
        if ($Report.groups.Count -eq 0) { $out.Add('no receipts'); $out.Add('') }
        foreach ($n in $Report.digestNotes) { $out.Add("NOTE: $n") }
    }
    foreach ($m in $Report.malformed) { $out.Add("MALFORMED $($m.path): $($m.reasons -join '; ')  (ignored, never counted)") }
    foreach ($r in $Report.refused) { $out.Add("REFUSED $($r.reason) $($r.path)  (not counted)") }
    return , $out.ToArray()
}

Export-ModuleMember -Function Get-VenueEvidenceReport, Format-VenueEvidenceText, Resolve-VeDefaultReceiptsRoot
