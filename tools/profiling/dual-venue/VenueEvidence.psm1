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
ASCII only (cp1252-safe). Requires PowerShell 7+ (ConvertFrom-Json -AsHashtable).
#>

$script:ReceiptSchema = 'mlv-app/dual-venue-receipt/v1'
$script:ReportSchema = 'mlv-app/dual-venue-evidence-report/v1'
$script:KnownVenues = @('bachelor', 'ultra-magnus')
$script:KnownRoles = @('acceptance', 'supplementary')
$script:KnownOutcomes = @('PASS', 'FAIL', 'VENUE_UNHEALTHY', 'VENUE_NOT_QUIESCENT', 'VENUE_HOST_MISMATCH',
    'DEVICE_UNAVAILABLE', 'UNRESOLVED', 'RETRACTED')
$script:SignalOutcomes = @('PASS', 'FAIL')
$script:KnownBackends = @('cuda', 'gl', 'cpu')
$script:MaxReceiptBytes = 1MB
$script:MinClipSeconds = 20

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
        if ($dr -notin $script:KnownRoles) { throw "venue table '$Path': defaultRole '$dr' is not acceptance|supplementary" }
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
        foreach ($card in $roles.Keys) {
            $perVenue = $roles[$card]
            if ($perVenue -isnot [System.Collections.IDictionary]) { throw "venue table '$Path': roles['$card'] must be an object" }
            $clean = @{}
            foreach ($v in $perVenue.Keys) {
                if ([string]$v -notin $script:KnownVenues) { throw "venue table '$Path': roles['$card'] names unknown venue '$v'" }
                if ($perVenue[$v] -notin $script:KnownRoles) { throw "venue table '$Path': roles['$card']['$v'] = '$($perVenue[$v])' is not acceptance|supplementary" }
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
        $lf = Get-VeValue $subject 'lookFlavor'
        if ($lf -is [string]) { $lookFlavor = $lf }
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

    $metrics = Get-VeValue $Receipt 'metrics'
    if ($metrics -isnot [System.Collections.IDictionary]) { $reasons.Add('metrics missing or not an object') }

    # Place in the append-only layout: the path is part of the claim, so a receipt filed under another
    # venue/leg/card folder is refused rather than trusted.
    if ($RelParts.Count -eq 4) {
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
    $record = @{
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
    # Exactly the four-level layout <root>\<card>\<leg>\<venue>\*.json -- no recursive scan, hard cap on count.
    param([string]$Root, [int]$Max)
    $files = [System.Collections.Generic.List[object]]::new()
    foreach ($cardDir in (Get-ChildItem -LiteralPath $Root -Directory | Sort-Object Name)) {
        foreach ($legDir in (Get-ChildItem -LiteralPath $cardDir.FullName -Directory | Sort-Object Name)) {
            foreach ($venueDir in (Get-ChildItem -LiteralPath $legDir.FullName -Directory | Sort-Object Name)) {
                foreach ($f in (Get-ChildItem -LiteralPath $venueDir.FullName -File -Filter '*.json' | Sort-Object Name)) {
                    $files.Add(@{ file = $f; rel = @($cardDir.Name, $legDir.Name, $venueDir.Name, $f.Name) })
                    if ($files.Count -gt $Max) { throw "receipts root holds more than -MaxReceipts ($Max) receipts; raise the bound deliberately" }
                }
            }
        }
    }
    return , $files.ToArray()
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
            $c = (Get-VeTieRank $a.outcome).CompareTo((Get-VeTieRank $b.outcome))
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
    param([string]$Outcome)
    if ($Outcome -ceq 'PASS') { return 0 }
    if ($Outcome -ceq 'FAIL') { return 1 }
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
        $rawSecs = $null
        foreach ($n in 'clipSeconds', 'clip_seconds') {
            $v = Get-VeValue $Metrics $n
            if ($null -ne $v) { $rawSecs = $v; break }
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

    if ($rootExists) {
        foreach ($entry in (Get-VeReceiptFiles -Root $ReceiptsRoot -Max $MaxReceipts)) {
            $scanned++
            $rel = ($entry.rel -join '\')
            if ($entry.file.Length -gt $script:MaxReceiptBytes) {
                $malformed.Add([ordered]@{ path = $rel; reasons = @("file is larger than $($script:MaxReceiptBytes) bytes; not read") })
                continue
            }
            try {
                $parsed = ConvertFrom-VeJsonText -Text (Get-Content -LiteralPath $entry.file.FullName -Raw -Encoding UTF8)
            } catch {
                $malformed.Add([ordered]@{ path = $rel; reasons = @('not valid JSON') })
                continue
            }
            $checked = ConvertTo-VeRecord -Receipt $parsed -RelParts $entry.rel
            if ($checked.reasons.Count -gt 0) {
                $malformed.Add([ordered]@{ path = $rel; reasons = @($checked.reasons) })
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
    $valid = [System.Collections.Generic.List[object]]::new()
    foreach ($r in $structural) {
        $reason = $null
        if ($byId[$r.receiptId.ToLowerInvariant()] -gt 1) {
            $reason = 'DUPLICATE_RECEIPT_ID'
        } elseif ($table.present -and $r.role -cne (Get-VeExpectedRole -Table $table -Card $r.card -Venue $r.venue)) {
            $reason = 'ROLE_MISMATCH'
        } elseif ($r.outcome -in $script:SignalOutcomes -and
            ($r.declared -isnot [string] -or $r.detected -isnot [string] -or $r.declared -ine $r.venue -or $r.detected -ine $r.venue)) {
            $reason = 'VENUE_DETECTION_MISMATCH'
        } elseif ($r.clipState -ceq 'LOOPED') {
            $reason = 'INVALID_LOOPED'
        } elseif ($r.clipState -ceq 'SHORT') {
            $reason = 'INVALID_CLIP_TOO_SHORT'
        }
        if ($reason) {
            $refused.Add([ordered]@{ path = $r.path; receiptId = $r.receiptId; card = $r.card; legId = $r.legId; venue = $r.venue; reason = $reason })
        } else {
            $valid.Add($r)
        }
    }

    $report = [ordered]@{
        schema             = $script:ReportSchema
        mode               = $(if ($AcceptanceFor) { 'acceptance' } else { 'report' })
        generatedUtc       = (Format-VeInstant ([System.DateTimeOffset]::UtcNow))
        receiptsRoot       = $ReceiptsRoot
        receiptsRootExists = $rootExists
        venueTable         = [ordered]@{ path = $table.path; present = $table.present }
        counts             = [ordered]@{ scanned = $scanned; valid = $valid.Count; malformed = $malformed.Count; refused = $refused.Count }
    }

    if ($AcceptanceFor) {
        # Acceptance needs an AUTHORITATIVE role: a present, valid venue table that lists this card. A receipt's own
        # venue.role is a label, never the authority, so without the table there is nothing to read.
        $blocked = $null
        if (-not $table.present) { $blocked = 'VENUE_TABLE_ABSENT' }
        elseif ($null -eq (Find-VeKey $table.roles $AcceptanceFor)) { $blocked = 'CARD_NOT_IN_VENUE_TABLE' }

        $latest = @{}
        if (-not $blocked) {
            foreach ($r in $valid) {
                if ($r.card -ine $AcceptanceFor) { continue }
                if ((Get-VeExpectedRole -Table $table -Card $r.card -Venue $r.venue) -cne 'acceptance' -or $r.role -cne 'acceptance') { continue }
                if ($LegId -and $r.legId -ine $LegId) { continue }
                if ($SubjectDigest -and $r.digest -ne $SubjectDigest.ToLowerInvariant()) { continue }
                if ($BuildManifestSha256 -and $r.buildManifest -ine $BuildManifestSha256) { continue }
                $key = "$($r.legId)|$($r.backend)|$($r.venue)|$($r.digest)"
                if (-not $latest.ContainsKey($key)) { $latest[$key] = [System.Collections.Generic.List[object]]::new() }
                $latest[$key].Add($r)
            }
        }
        # One entry per (leg, backend, venue, digest): its latest receipt plus how many receipts of each outcome exist.
        $entriesByLeg = @{}
        foreach ($key in ($latest.Keys | Sort-Object)) {
            $sorted = Sort-VeReceipts $latest[$key]
            $top = $sorted[$sorted.Count - 1]
            $hist = [ordered]@{}
            foreach ($o in ($latest[$key] | ForEach-Object { $_.outcome } | Sort-Object -Unique)) {
                $hist[$o] = @($latest[$key] | Where-Object { $_.outcome -eq $o }).Count
            }
            $legKey = "$($top.legId)|$($top.backend)|$($top.venue)"
            if (-not $entriesByLeg.ContainsKey($legKey)) { $entriesByLeg[$legKey] = [System.Collections.Generic.List[object]]::new() }
            $entriesByLeg[$legKey].Add(@{ top = $top; history = [ordered]@{ total = $latest[$key].Count; byOutcome = $hist } })
        }
        $filtered = [bool]($SubjectDigest -or $BuildManifestSha256)
        $receipts = [System.Collections.Generic.List[object]]::new()
        $withheld = [System.Collections.Generic.List[object]]::new()
        $olderDigests = [System.Collections.Generic.List[object]]::new()
        $failCount = 0
        foreach ($legKey in ($entriesByLeg.Keys | Sort-Object)) {
            $entries = $entriesByLeg[$legKey]
            $selected = @($entries.ToArray())
            if (-not $filtered -and $entries.Count -gt 1) {
                # No subject filter: only the NEWEST digest of a leg can speak for the candidate; older digests are
                # listed but are never evidence.
                $order = Sort-VeReceipts @($entries | ForEach-Object { $_.top })
                $newestId = $order[$order.Count - 1].receiptId
                $selected = @($entries | Where-Object { $_.top.receiptId -ceq $newestId })
                foreach ($e in ($entries | Where-Object { $_.top.receiptId -cne $newestId })) {
                    $olderDigests.Add([ordered]@{ legId = $e.top.legId; backend = $e.top.backend; venue = $e.top.venue
                            subjectDigest = $e.top.digest; latestOutcome = $e.top.outcome; receiptId = $e.top.receiptId })
                }
            }
            foreach ($e in $selected) {
                $top = $e.top
                if ($top.outcome -notin $script:SignalOutcomes) {
                    $withheld.Add([ordered]@{ legId = $top.legId; backend = $top.backend; subjectDigest = $top.digest; venue = $top.venue
                            receiptId = $top.receiptId; reason = $top.outcome })
                } elseif ($top.clipState -cne 'OK') {
                    $withheld.Add([ordered]@{ legId = $top.legId; backend = $top.backend; subjectDigest = $top.digest; venue = $top.venue
                            receiptId = $top.receiptId; reason = 'UNVERIFIED_CLIP_LENGTH' })
                } else {
                    if ($top.outcome -ceq 'FAIL') { $failCount++ }
                    $receipts.Add((ConvertTo-VeReceiptView -Record $top -RoleVerified $true -Full $true -History $e.history))
                }
            }
        }
        $reason = $blocked
        if (-not $reason -and $receipts.Count -eq 0) { $reason = 'NO_ACCEPTANCE_RECEIPTS' }
        $report['acceptance'] = [ordered]@{
            card                = $AcceptanceFor
            status              = $(if ($receipts.Count -gt 0) { 'ACCEPTANCE_EVIDENCE' } else { 'NO_ACCEPTANCE_EVIDENCE' })
            reason              = $reason
            failCount           = $failCount
            digestSelection     = $(if ($filtered) { 'FILTERED' } else { 'NEWEST_PER_LEG' })
            digestSelectionNote = $(if ($filtered) { 'Filtered by -SubjectDigest and/or -BuildManifestSha256: every matching digest is reported.' }
                else { 'No -SubjectDigest/-BuildManifestSha256 filter: only the NEWEST subject digest per leg/backend/venue is reported; older digests are listed under olderDigests and are never evidence. Bind to the candidate build with -BuildManifestSha256.' })
            filters             = [ordered]@{ subjectDigest = $(if ($SubjectDigest) { $SubjectDigest.ToLowerInvariant() } else { $null })
                buildManifestSha256 = $(if ($BuildManifestSha256) { $BuildManifestSha256.ToLowerInvariant() } else { $null }) }
            receipts            = @($receipts.ToArray())
            withheld            = @($withheld.ToArray())
            olderDigests        = @($olderDigests.ToArray())
        }
        $report['malformed'] = @($malformed.ToArray())
        $report['refused'] = @($refused.ToArray() | Where-Object { $_.card -ieq $AcceptanceFor })
        return $report
    }

    # ---- per-(card, leg, backend, digest) groups
    $groupMap = @{}
    foreach ($r in $valid) {
        if ($Card -and $r.card -ine $Card) { continue }
        if ($LegId -and $r.legId -ine $LegId) { continue }
        $key = "$($r.card)|$($r.legId)|$($r.backend)|$($r.digest)"
        if (-not $groupMap.ContainsKey($key)) { $groupMap[$key] = [System.Collections.Generic.List[object]]::new() }
        $groupMap[$key].Add($r)
    }
    $groups = [System.Collections.Generic.List[object]]::new()
    $digestsPerLeg = @{}
    foreach ($key in ($groupMap.Keys | Sort-Object { $_ })) {
        $members = $groupMap[$key]
        $first = $members[0]
        $expected = @(Get-VeExpectedVenues -Table $table -Card $first.card)
        $observed = @($members | ForEach-Object { $_.venue } | Select-Object -Unique)
        $allVenues = @($expected + @($observed | Where-Object { $_ -notin $expected }))

        $venueRows = [ordered]@{}
        $latestByVenue = @{}
        $reasons = [System.Collections.Generic.List[object]]::new()
        foreach ($v in $allVenues) {
            $mine = @($members | Where-Object { $_.venue -eq $v })
            $hist = [ordered]@{}
            foreach ($o in ($mine | ForEach-Object { $_.outcome } | Sort-Object -Unique)) { $hist[$o] = @($mine | Where-Object { $_.outcome -eq $o }).Count }
            if ($mine.Count -eq 0) {
                $venueRows[$v] = [ordered]@{ expected = ($v -in $expected); status = 'missing'; latest = $null; history = [ordered]@{ total = 0; byOutcome = $hist } }
            } else {
                $sorted = Sort-VeReceipts $mine
                $top = $sorted[$sorted.Count - 1]
                $latestByVenue[$v] = $top
                $venueRows[$v] = [ordered]@{
                    expected = ($v -in $expected)
                    status   = $top.outcome
                    latest   = (ConvertTo-VeReceiptView -Record $top -RoleVerified $table.present -Full $false)
                    history  = [ordered]@{ total = $mine.Count; byOutcome = $hist }
                }
            }
            if ($v -in $expected) {
                if (-not $latestByVenue.ContainsKey($v)) { $reasons.Add([ordered]@{ venue = $v; reason = 'missing' }) }
                elseif ($latestByVenue[$v].outcome -notin $script:SignalOutcomes) { $reasons.Add([ordered]@{ venue = $v; reason = $latestByVenue[$v].outcome }) }
            }
        }
        $complete = ($reasons.Count -eq 0)
        $g = [ordered]@{
            card            = $first.card
            legId           = $first.legId
            backend         = $first.backend
            subjectDigest   = $first.digest
            expectedVenues  = @($expected)
            venues          = $venueRows
            pair            = [ordered]@{ state = $(if ($complete) { 'COMPLETE' } else { 'INCOMPLETE' }); reasons = @($reasons.ToArray()) }
        }
        if ($complete) { $g['delta'] = Get-VeDelta -Venues $expected -LatestByVenue $latestByVenue }
        $groups.Add($g)

        $legKey = "$($first.card)/$($first.legId)" + $(if ($first.backend) { " [$($first.backend)]" } else { '' })
        if (-not $digestsPerLeg.ContainsKey($legKey)) { $digestsPerLeg[$legKey] = [System.Collections.Generic.List[string]]::new() }
        $digestsPerLeg[$legKey].Add($first.digest)
    }
    $notes = [System.Collections.Generic.List[string]]::new()
    foreach ($legKey in ($digestsPerLeg.Keys | Sort-Object)) {
        $ds = @($digestsPerLeg[$legKey] | Select-Object -Unique)
        if ($ds.Count -gt 1) {
            $short = ($ds | ForEach-Object { $_.Substring(0, 12) }) -join ', '
            $notes.Add("$legKey has $($ds.Count) subject digests ($short): receipts from different subject digests are never compared.")
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
        foreach ($r in $a.receipts) {
            $short = $r.subjectDigest.Substring(0, 12)
            $flavor = $(if ($r.lookFlavor) { " look=$($r.lookFlavor)" } else { '' })
            $hist = ($r.history.byOutcome.Keys | ForEach-Object { "$_`:$($r.history.byOutcome[$_])" }) -join ','
            $out.Add("  $($r.outcome)  $($r.venue) ($($r.role); role verified against venue table)  leg=$($r.legId)$(if ($r.backend) { " backend=$($r.backend)" })$flavor  clip=$($r.clipId) ($($r.clipSeconds) s, wrapped=$($r.wrapped))  build=$($r.buildManifestSha256.Substring(0, [Math]::Min(12, $r.buildManifestSha256.Length)))  leg-spec=$($r.legSpecSha256.Substring(0, [Math]::Min(12, $r.legSpecSha256.Length)))  digest=$short  finished=$($r.finishedUtc)  receipts=$($r.history.total) [$hist]  receipt=$($r.receiptId)")
        }
        foreach ($w in $a.withheld) {
            $why = $(if ($w.reason -ceq 'UNVERIFIED_CLIP_LENGTH') { 'UNVERIFIED_CLIP_LENGTH: the receipt carries no clipSeconds/wrapped proof of a long, non-looping clip' } else { "latest receipt is $($w.reason), which carries no signal" })
            $out.Add("  WITHHELD  $($w.venue) leg=$($w.legId) digest=$($w.subjectDigest.Substring(0, 12)): $why")
        }
        foreach ($o in $a.olderDigests) {
            $out.Add("  OLDER DIGEST (not evidence)  $($o.venue) leg=$($o.legId) digest=$($o.subjectDigest.Substring(0, 12)) latest=$($o.latestOutcome)")
        }
        if ($a.failCount -gt 0) { $out.Add("  NOTE: $($a.failCount) acceptance receipt(s) say FAIL (exit code 3)") }
    } else {
        foreach ($g in $Report.groups) {
            $be = $(if ($g.backend) { " [$($g.backend)]" } else { '' })
            $out.Add("$($g.card) / $($g.legId)$be  digest $($g.subjectDigest.Substring(0, 12))")
            if ($g.pair.state -eq 'COMPLETE') {
                $out.Add('  pair: COMPLETE (every expected venue has a PASS/FAIL receipt at this digest; no merged verdict)')
            } else {
                $why = ($g.pair.reasons | ForEach-Object { "$($_.venue): $($_.reason)" }) -join '; '
                $out.Add("  pair: INCOMPLETE ($why)")
            }
            $out.Add(('  {0,-14} {1,-14} {2,-20} {3,-25} {4}' -f 'venue', 'role', 'latest outcome', 'finished (UTC)', 'receipts'))
            foreach ($v in $g.venues.Keys) {
                $row = $g.venues[$v]
                if ($null -eq $row.latest) {
                    $out.Add(('  {0,-14} {1,-14} {2,-20} {3,-25} {4}' -f $v, '-', 'missing', '-', 0))
                } else {
                    $roleText = $row.latest.role + $(if ($row.latest.roleVerified) { '' } else { ' (UNVERIFIED)' })
                    $clipNote = $(if ($row.latest.clipLength -ceq 'UNVERIFIED_CLIP_LENGTH') { '  UNVERIFIED_CLIP_LENGTH' } else { '' })
                    $out.Add(('  {0,-14} {1,-14} {2,-20} {3,-25} {4}{5}' -f $v, $roleText, $row.latest.outcome, $row.latest.finishedUtc, $row.history.total, $clipNote))
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
