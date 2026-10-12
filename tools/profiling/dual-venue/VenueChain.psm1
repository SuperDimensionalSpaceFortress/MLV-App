# VenueChain.psm1 -- the venue claim and the quiet-rate table behind Invoke-VenueChain.ps1 (VENUE-CHAIN-RUNNER-1).
#
# ONE CLAIM PER VENUE. A claim is one JSON file, <ClaimDir>\<venue>.claim.json (default <main checkout>\.claude-state\dual-venue\claims), naming the
# owner pid, that process's creation time, the run dir, a purpose, an expiry and a nonce. It is TAKEN only by creating the file with CreateNew (never
# read-then-write), so two takers can never both hold it. A claim is STALE when its pid is dead (dead-pid), when the pid is alive but its process was
# started at another time (pid-reused), when its expiry has passed (expired), or when it has been unparseable for longer than a writer needs
# (unreadable). A stale claim may be broken. Breaking and releasing both open the file exclusively, re-read it and delete it only when it still holds
# the bytes that were judged (release: the owner's nonce), so a breaker can never delete a claim that another chain took a moment earlier.
#
# TICKET QUEUE (VENUE-CLAIM-FIFO-1). The claim alone is a polling race: every waiter retries CreateNew on its own 30 s phase, so the waiter whose poll lands
# first after a release wins, and a chain that releases and re-asks within seconds beats every sleeping waiter (q02 of PLAYBACK-CPU-PACE-GAP-1 r5 lost 5
# handoffs in a row, 60 min). A ticket-aware waiter therefore takes a TICKET first: one file, <ClaimDir>\<venue>.tickets\<utc ticks, 19 digits>-p<pid>-<start
# ticks>[-n].ticket.json, taken by CreateNew (so names sort in arrival order and two takers can never share one). Only the OLDEST LIVE ticket may try the
# claim (Request-VenueClaim -UseTicket); every other ticket holder gets state QUEUED. A ticket is STALE when its owner pid is dead (dead-pid), was reused
# (pid-reused), has not refreshed its heartbeat for staleAfterSec (heartbeat; default 3 x ClaimPollSec, widened by the owner while it sits in a quiet probe),
# or has been unparseable for more than a writer needs (unreadable); the next waiter removes it (Request-VenueClaim reports it in .staleTickets) and the line
# moves up. A chain that releases its claim on GATE_BUSY keeps its ticket (it does not go to the back of the line); Remove-VenueTicket ends it.
# BACK-COMPAT: the claim file, its name and its CreateNew semantics are unchanged, and Request-VenueClaim called WITHOUT -UseTicket / -Ticket is the old
# one-shot attempt. Ticketing orders ONLY ticket holders: a legacy (ticketless) chain still takes any free claim (it can win a race against the line), and a
# ticket holder never waits on a legacy waiter (nothing ticketless is ever in the line), so the two kinds cannot deadlock; they only share the claim.
#
# A claim is held ONLY around a leg: a chain that is waiting for quiet holds nothing, and nothing but a LIVE claim blocks another chain (no process
# scan, no lane-prompt scan: the 2026-10-09 mutual wait came from two waiters each treating the other's WAITING as venue use). Hand-written chains
# adopt it the same way:
#   Import-Module tools\profiling\dual-venue\VenueChain.psm1
#   $c = Request-VenueClaim -Venue bachelor -RunDir $rd -Purpose 'leg film'
#   if ($c.granted) { try { <run the leg> } finally { Release-VenueClaim -Venue bachelor -Nonce $c.claim.nonce | Out-Null } }
#
# QUIET RATE. ConvertFrom-VenueGateLog reads the PROBE lines Wait-VenueQuiet.ps1 appends to a gate log (v1 shape:
# `PROBE venue-quiet-probe-<yyyyMMddTHHmmssfff>Z <venue> samples=<a/b/c> mean=<m> state=QUIET|BUSY|UNKNOWN ...`); the hour is the UTC hour of the job id's
# stamp and a job id seen in two logs is counted once. Get-VenueQuietRateTable groups them by UTC hour of day (rate = QUIET / measured probes; UNKNOWN is
# counted apart and is never a probe that measured anything). Select-VenueQuietWindow decides whether a chain should wait for a later hour.
# Nothing here reads a venue share, submits a job or touches a process.

$script:ClaimSchema = 'mlv-app/venue-claim/v1'
$script:UnreadableGraceSec = 30
$script:PidStartToleranceSec = 2

function Get-VenueMainRoot {
    # The MAIN checkout (where the gitignored .claude-state lives), resolved from the git common dir: a worktree has no .claude-state of its own.
    param([string]$From = $PSScriptRoot)
    $common = (& git -C $From rev-parse --path-format=absolute --git-common-dir 2>$null)
    if ($LASTEXITCODE -eq 0 -and $common) { return (Split-Path -Parent ([string]$common)) }
    (Resolve-Path (Join-Path $From '..\..\..')).Path
}

function Get-VenueClaimPath {
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '')
    if ($Venue -cnotmatch '^[a-z0-9][a-z0-9-]{0,62}$') { throw "VENUE_CLAIM_USAGE '$Venue' is not a venue name" }
    $dir = $(if ($ClaimDir) { $ClaimDir } else { Join-Path (Get-VenueMainRoot) '.claude-state\dual-venue\claims' })
    Join-Path $dir "$Venue.claim.json"
}

function ConvertTo-VenueUtc($Value) {
    # ConvertFrom-Json turns ISO strings into DateTime on PowerShell 7, so accept a DateTime, a DateTimeOffset or a string. $null when unparseable.
    if ($null -eq $Value) { return $null }
    if ($Value -is [DateTime]) { if ($Value.Kind -eq [DateTimeKind]::Unspecified) { return [DateTime]::SpecifyKind($Value, [DateTimeKind]::Utc) }; return $Value.ToUniversalTime() }
    if ($Value -is [DateTimeOffset]) { return $Value.UtcDateTime }
    $o = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$o)) { return $o.UtcDateTime }
    $null
}

function Get-VenueProcessStartUtc([int]$ProcessId) {
    # $null when there is no such process; [DateTime]::MinValue when it exists but its start time cannot be read.
    $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    try { return $p.StartTime.ToUniversalTime() } catch { return [DateTime]::MinValue }
}

function Get-VenueClaimStaleReason {
    <#
    .SYNOPSIS
    Why a parsed claim is stale ('dead-pid', 'pid-reused', 'expired', 'unreadable'), or $null when it is live.
    #>
    param([Parameter(Mandatory)]$Claim, [DateTime]$NowUtc = [DateTime]::UtcNow)
    $pidValue = 0
    if (-not [int]::TryParse([string]$Claim.ownerPid, [ref]$pidValue) -or $pidValue -le 0) { return 'unreadable' }
    $created = ConvertTo-VenueUtc $Claim.ownerCreatedUtc
    $expires = ConvertTo-VenueUtc $Claim.expiresUtc
    if ($null -eq $created -or $null -eq $expires) { return 'unreadable' }
    $started = Get-VenueProcessStartUtc $pidValue
    if ($null -eq $started) { return 'dead-pid' }
    # A start time that cannot be read cannot prove a reuse: the claim stays live until its expiry.
    if ($started -ne [DateTime]::MinValue -and [Math]::Abs(($started - $created).TotalSeconds) -gt $script:PidStartToleranceSec) { return 'pid-reused' }
    if ($NowUtc -ge $expires) { return 'expired' }
    $null
}

function Read-VenueClaimFile([string]$Path) {
    # One read with FileShare.Read: a writer (CreateNew, FileShare.None) or a breaker (FileShare.Delete) holding the file makes it BUSY, never half-read.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return [pscustomobject]@{ exists = $false; busy = $false; text = $null; lastWriteUtc = $null } }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        try {
            $sr = [IO.StreamReader]::new($fs, [Text.UTF8Encoding]::new($false), $false, 4096, $true)
            $text = $sr.ReadToEnd(); $sr.Dispose()
        } finally { $fs.Dispose() }
        return [pscustomobject]@{ exists = $true; busy = $false; text = $text; lastWriteUtc = [IO.File]::GetLastWriteTimeUtc($Path) }
    } catch {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return [pscustomobject]@{ exists = $false; busy = $false; text = $null; lastWriteUtc = $null } }
        return [pscustomobject]@{ exists = $true; busy = $true; text = $null; lastWriteUtc = $null }
    }
}

function Get-VenueClaim {
    <#
    .SYNOPSIS
    The venue's claim and its state: FREE (no claim), LIVE (held), STALE (may be broken; .reason says why) or BUSY (being written or broken right now).
    #>
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', [DateTime]$NowUtc = [DateTime]::UtcNow)
    $path = Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir
    $f = Read-VenueClaimFile $path
    $out = [ordered]@{ venue = $Venue; path = $path; state = 'FREE'; reason = $null; claim = $null; text = $f.text }
    if (-not $f.exists) { return [pscustomobject]$out }
    if ($f.busy) { $out.state = 'BUSY'; return [pscustomobject]$out }
    $claim = $null
    try { $claim = $f.text | ConvertFrom-Json -ErrorAction Stop } catch { $claim = $null }
    if ($null -eq $claim -or [string]$claim.schema -cne $script:ClaimSchema -or [string]$claim.venue -cne $Venue) {
        # unparseable: a writer gets a grace period before the file counts as abandoned
        if ($null -ne $f.lastWriteUtc -and ($NowUtc - $f.lastWriteUtc).TotalSeconds -lt $script:UnreadableGraceSec) { $out.state = 'LIVE'; $out.reason = 'being-written' }
        else { $out.state = 'STALE'; $out.reason = 'unreadable' }
        $out.claim = $claim
        return [pscustomobject]$out
    }
    $out.claim = $claim
    $why = Get-VenueClaimStaleReason -Claim $claim -NowUtc $NowUtc
    if ($why) { $out.state = 'STALE'; $out.reason = $why } else { $out.state = 'LIVE' }
    [pscustomobject]$out
}

function Remove-VenueClaimIfUnchanged {
    # Opens the claim exclusively (only FileShare.Delete, so no one can read, write or take it meanwhile), re-reads it and deletes it only when it still
    # holds -ExpectedText (or, with -ExpectedNonce, that nonce). Returns REMOVED | CHANGED | ABSENT | BUSY.
    param([Parameter(Mandatory)][string]$Path, [string]$ExpectedText = $null, [string]$ExpectedNonce = '')
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 'ABSENT' }
        $fs = $null
        try { $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Delete) } catch { Start-Sleep -Milliseconds (50 * $attempt); continue }
        try {
            $sr = [IO.StreamReader]::new($fs, [Text.UTF8Encoding]::new($false), $false, 4096, $true)
            $text = $sr.ReadToEnd(); $sr.Dispose()
            $same = $false
            if ($ExpectedNonce) {
                $j = $null; try { $j = $text | ConvertFrom-Json -ErrorAction Stop } catch { $j = $null }
                $same = ($null -ne $j -and [string]$j.nonce -ceq $ExpectedNonce)
            } else { $same = ($text -ceq $ExpectedText) }
            if (-not $same) { return 'CHANGED' }
            [IO.File]::Delete($Path)   # allowed while our handle is open because it shares Delete; the name is gone once the handle closes
            return 'REMOVED'
        } finally { $fs.Dispose() }
    }
    'BUSY'
}

function Invoke-VenueClaimAttempt {
    # The one-shot, ticketless attempt: the pre-VENUE-CLAIM-FIFO-1 Request-VenueClaim, unchanged.
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$RunDir, [string]$Purpose = '', [int]$TtlSec = 10800,
        [string]$ClaimDir = '', [int]$OwnerPid = $PID)
    $path = Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    $ownerStart = Get-VenueProcessStartUtc $OwnerPid
    if ($null -eq $ownerStart -or $ownerStart -eq [DateTime]::MinValue) { throw "VENUE_CLAIM_USAGE the owner pid $OwnerPid has no readable start time: a claim must be checkable for pid reuse" }
    $broken = [System.Collections.Generic.List[object]]::new()
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        $now = [DateTime]::UtcNow
        $claim = [ordered]@{
            schema = $script:ClaimSchema; venue = $Venue; ownerPid = $OwnerPid; ownerCreatedUtc = $ownerStart.ToString('o'); host = $env:COMPUTERNAME
            runDir = $RunDir; purpose = $Purpose; claimedUtc = $now.ToString('o'); expiresUtc = $now.AddSeconds($TtlSec).ToString('o'); nonce = [guid]::NewGuid().ToString('N')
        }
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($claim | ConvertTo-Json -Compress) + "`n")
        $created = $false
        try {
            $fs = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)   # the only way a claim is taken
            try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }
            $created = $true
        } catch { $created = $false }
        if ($created) { return [pscustomobject]@{ granted = $true; state = 'TAKEN'; claim = [pscustomobject]$claim; path = $path; broken = @($broken) } }
        $seen = Get-VenueClaim -Venue $Venue -ClaimDir $ClaimDir
        switch ($seen.state) {
            'FREE' { continue }   # it went away between the two calls: try again
            'LIVE' { return [pscustomobject]@{ granted = $false; state = 'LIVE'; claim = $seen.claim; path = $path; broken = @($broken) } }
            'BUSY' { Start-Sleep -Milliseconds 100; continue }
            'STALE' {
                $r = Remove-VenueClaimIfUnchanged -Path $path -ExpectedText $seen.text
                if ($r -ceq 'REMOVED') { $broken.Add([pscustomobject]@{ reason = $seen.reason; claim = $seen.claim }) }
                continue
            }
        }
    }
    [pscustomobject]@{ granted = $false; state = 'BUSY'; claim = $null; path = $path; broken = @($broken) }
}

$script:TicketSchema = 'mlv-app/venue-ticket/v1'
$script:TicketNameRx = [regex]'^(\d{19})-p(\d+)-(\d+)\.ticket\.json$'

function Resolve-VenueNow($Value) { if ($null -eq $Value) { [DateTime]::UtcNow } else { ConvertTo-VenueUtc $Value } }

function Get-VenueTicketDir {
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '')
    Join-Path (Split-Path -Parent (Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir)) "$Venue.tickets"
}

function ConvertTo-VenueTicketBytes($Fields) { [Text.UTF8Encoding]::new($false).GetBytes(($Fields | ConvertTo-Json -Compress) + "`n") }

function New-VenueTicket {
    <#
    .SYNOPSIS
    Take a place in the venue's line: one ticket file, created with CreateNew, named by the UTC ticks it was taken at (a clash moves to the next tick).
    .OUTPUTS
    [pscustomobject]@{ name; path; nonce; venue; runDir; ownerPid; createdUtc }
    #>
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$RunDir, [string]$Purpose = '', [ValidateRange(1, 86400)][int]$StaleAfterSec = 90,
        [string]$ClaimDir = '', [int]$OwnerPid = $PID, $NowUtc = $null)
    $dir = Get-VenueTicketDir -Venue $Venue -ClaimDir $ClaimDir
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $ownerStart = Get-VenueProcessStartUtc $OwnerPid
    if ($null -eq $ownerStart -or $ownerStart -eq [DateTime]::MinValue) { throw "VENUE_CLAIM_USAGE the owner pid $OwnerPid has no readable start time: a ticket must be checkable for pid reuse" }
    $now = Resolve-VenueNow $NowUtc
    for ($n = 0; $n -lt 500; $n++) {
        $name = '{0:D19}-p{1}-{2}.ticket.json' -f ($now.Ticks + $n), $OwnerPid, $ownerStart.Ticks
        $path = Join-Path $dir $name
        $fields = [ordered]@{
            schema = $script:TicketSchema; venue = $Venue; ownerPid = $OwnerPid; ownerCreatedUtc = $ownerStart.ToString('o'); host = $env:COMPUTERNAME
            runDir = $RunDir; purpose = $Purpose; createdUtc = $now.ToString('o'); heartbeatUtc = $now.ToString('o'); staleAfterSec = $StaleAfterSec
            phase = 'waiting'; nonce = [guid]::NewGuid().ToString('N')
        }
        $bytes = ConvertTo-VenueTicketBytes $fields
        try {
            $fs = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)   # the only way a ticket is taken
            try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }
        } catch { continue }
        return [pscustomobject]@{ name = $name; path = $path; nonce = $fields.nonce; venue = $Venue; runDir = $RunDir; ownerPid = $OwnerPid; createdUtc = $now.ToString('o') }
    }
    throw "VENUE_CLAIM_USAGE could not create a ticket in '$dir'"
}

function Get-VenueTicketStaleReason {
    <#
    .SYNOPSIS
    Why a parsed ticket is stale ('dead-pid', 'pid-reused', 'heartbeat', 'unreadable'), or $null when it is live.
    #>
    param([Parameter(Mandatory)]$Ticket, $NowUtc = $null)
    $now = Resolve-VenueNow $NowUtc
    $pidValue = 0
    if (-not [int]::TryParse([string]$Ticket.ownerPid, [ref]$pidValue) -or $pidValue -le 0) { return 'unreadable' }
    $created = ConvertTo-VenueUtc $Ticket.ownerCreatedUtc
    $beat = ConvertTo-VenueUtc $Ticket.heartbeatUtc
    $after = 0.0
    if ($null -eq $created -or $null -eq $beat -or -not [double]::TryParse([string]$Ticket.staleAfterSec, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$after)) { return 'unreadable' }
    $started = Get-VenueProcessStartUtc $pidValue
    if ($null -eq $started) { return 'dead-pid' }
    if ($started -ne [DateTime]::MinValue -and [Math]::Abs(($started - $created).TotalSeconds) -gt $script:PidStartToleranceSec) { return 'pid-reused' }
    if (($now - $beat).TotalSeconds -gt $after) { return 'heartbeat' }
    $null
}

function Get-VenueTickets {
    <#
    .SYNOPSIS
    The venue's line, oldest ticket first. One row per ticket: name, path, state (LIVE | STALE | BUSY = being rewritten, counts as live), reason, text, ticket (parsed),
    position (1-based among the non-stale tickets; 0 for a stale one), ageSec and heartbeatAgeSec ($null when unreadable).
    #>
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', $NowUtc = $null)
    $now = Resolve-VenueNow $NowUtc
    $dir = Get-VenueTicketDir -Venue $Venue -ClaimDir $ClaimDir
    $rows = [System.Collections.Generic.List[object]]::new()
    if (Test-Path -LiteralPath $dir -PathType Container) {
        foreach ($file in @(Get-ChildItem -LiteralPath $dir -Filter '*.ticket.json' -File -ErrorAction SilentlyContinue)) {
            $m = $script:TicketNameRx.Match($file.Name)
            if (-not $m.Success) { continue }
            $f = Read-VenueClaimFile $file.FullName
            if (-not $f.exists) { continue }
            $row = [ordered]@{ name = $file.Name; path = $file.FullName; key = [int64]$m.Groups[1].Value; state = 'LIVE'; reason = $null; text = $f.text; ticket = $null; position = 0; ageSec = $null; heartbeatAgeSec = $null }
            if ($f.busy) { $row.state = 'BUSY' }
            else {
                $t = $null
                try { $t = $f.text | ConvertFrom-Json -ErrorAction Stop } catch { $t = $null }
                if ($null -eq $t -or [string]$t.schema -cne $script:TicketSchema -or [string]$t.venue -cne $Venue) {
                    if ($null -ne $f.lastWriteUtc -and ($now - $f.lastWriteUtc).TotalSeconds -lt $script:UnreadableGraceSec) { $row.reason = 'being-written' }
                    else { $row.state = 'STALE'; $row.reason = 'unreadable' }
                } else {
                    $row.ticket = $t
                    $why = Get-VenueTicketStaleReason -Ticket $t -NowUtc $now
                    if ($why) { $row.state = 'STALE'; $row.reason = $why }
                    $c = ConvertTo-VenueUtc $t.createdUtc; $b = ConvertTo-VenueUtc $t.heartbeatUtc
                    if ($null -ne $c) { $row.ageSec = [int][Math]::Max(0, ($now - $c).TotalSeconds) }
                    if ($null -ne $b) { $row.heartbeatAgeSec = [int][Math]::Max(0, ($now - $b).TotalSeconds) }
                }
            }
            $rows.Add([pscustomobject]$row)
        }
    }
    $sorted = @($rows | Sort-Object -Property key)
    $pos = 0
    foreach ($r in $sorted) { if ($r.state -cne 'STALE') { $pos++; $r.position = $pos } }
    $sorted
}

function Update-VenueTicket {
    <#
    .SYNOPSIS
    Heartbeat: rewrite the ticket's heartbeat (and, when given, its phase and staleAfterSec). Returns OK | LOST (the ticket is gone or is not ours) | BUSY (a reader held it; try again).
    #>
    param([Parameter(Mandatory)]$Ticket, [string]$Phase = '', [int]$StaleAfterSec = 0, $NowUtc = $null)
    $now = Resolve-VenueNow $NowUtc
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }
        $fs = $null
        try { $fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Read) } catch { Start-Sleep -Milliseconds (40 * $attempt); continue }
        try {
            $sr = [IO.StreamReader]::new($fs, [Text.UTF8Encoding]::new($false), $false, 4096, $true)
            $text = $sr.ReadToEnd(); $sr.Dispose()
            $j = $null; try { $j = $text | ConvertFrom-Json -ErrorAction Stop } catch { $j = $null }
            if ($null -eq $j -or [string]$j.nonce -cne [string]$Ticket.nonce) { return 'LOST' }
            $fields = [ordered]@{
                schema = [string]$j.schema; venue = [string]$j.venue; ownerPid = [int]$j.ownerPid; ownerCreatedUtc = (ConvertTo-VenueUtc $j.ownerCreatedUtc).ToString('o'); host = [string]$j.host
                runDir = [string]$j.runDir; purpose = [string]$j.purpose; createdUtc = (ConvertTo-VenueUtc $j.createdUtc).ToString('o'); heartbeatUtc = $now.ToString('o')
                staleAfterSec = $(if ($StaleAfterSec -gt 0) { $StaleAfterSec } else { [int]$j.staleAfterSec }); phase = $(if ($Phase) { $Phase } else { [string]$j.phase }); nonce = [string]$j.nonce
            }
            $bytes = ConvertTo-VenueTicketBytes $fields
            $fs.SetLength(0); $fs.Position = 0; $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true)
            return 'OK'
        } finally { $fs.Dispose() }
    }
    'BUSY'
}

function Remove-VenueTicket {
    <#
    .SYNOPSIS
    End the caller's place in the line (only when the file still carries the ticket's nonce). Returns REMOVED | CHANGED | ABSENT | BUSY.
    #>
    param([Parameter(Mandatory)]$Ticket)
    Remove-VenueClaimIfUnchanged -Path $Ticket.path -ExpectedNonce ([string]$Ticket.nonce)
}

function Request-VenueClaim {
    <#
    .SYNOPSIS
    Try ONCE to take the venue's claim (it never waits; the caller decides whether to poll). Stale claims found on the way are broken (each once).
    With -UseTicket (or a -Ticket from an earlier call) the caller first takes / refreshes its place in the line, removes stale tickets, and only the OLDEST LIVE
    ticket may try the claim; any other gets state QUEUED. Without either it is the old ticketless attempt.
    .OUTPUTS
    [pscustomobject]@{ granted; state (TAKEN | LIVE | BUSY | QUEUED); claim (ours when granted, the holder's when LIVE or QUEUED); path; broken (one {reason; claim} per stale claim broken);
    ticket (the caller's, keep it and pass it back; $null when ticketless); queue (the live tickets, oldest first); position (the caller's place, 0 when ticketless); staleTickets (one {reason; name; ticket} per stale ticket removed) }
    -NowUtc is the clock for tickets only (a test seam); the claim's own expiry always uses the real clock.
    #>
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$RunDir, [string]$Purpose = '', [ValidateRange(60, 86400)][int]$TtlSec = 10800,
        [string]$ClaimDir = '', [int]$OwnerPid = $PID, [switch]$UseTicket, $Ticket = $null, [ValidateRange(1, 86400)][int]$TicketStaleAfterSec = 90, $NowUtc = $null)
    $attemptArgs = @{ Venue = $Venue; RunDir = $RunDir; Purpose = $Purpose; TtlSec = $TtlSec; ClaimDir = $ClaimDir; OwnerPid = $OwnerPid }
    if (-not $UseTicket -and $null -eq $Ticket) {
        return (Invoke-VenueClaimAttempt @attemptArgs | Add-Member -NotePropertyName ticket -NotePropertyValue $null -PassThru | Add-Member -NotePropertyName queue -NotePropertyValue @() -PassThru |
            Add-Member -NotePropertyName position -NotePropertyValue 0 -PassThru | Add-Member -NotePropertyName staleTickets -NotePropertyValue @() -PassThru)
    }
    $now = Resolve-VenueNow $NowUtc
    $t = $Ticket
    if ($null -ne $t -and (Update-VenueTicket -Ticket $t -Phase 'waiting' -StaleAfterSec $TicketStaleAfterSec -NowUtc $now) -ceq 'LOST') { $t = $null }   # taken out from under us: back of the line
    if ($null -eq $t) { $t = New-VenueTicket -Venue $Venue -RunDir $RunDir -Purpose $Purpose -StaleAfterSec $TicketStaleAfterSec -ClaimDir $ClaimDir -OwnerPid $OwnerPid -NowUtc $now }
    $removed = [System.Collections.Generic.List[object]]::new()
    $line = Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now
    $stale = @($line | Where-Object { $_.state -ceq 'STALE' })
    foreach ($s in $stale) {
        if ((Remove-VenueClaimIfUnchanged -Path $s.path -ExpectedText $s.text) -ceq 'REMOVED') { $removed.Add([pscustomobject]@{ reason = $s.reason; name = $s.name; ticket = $s.ticket }) }
    }
    if ($stale.Count -gt 0) { $line = Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now }
    $live = @($line | Where-Object { $_.state -cne 'STALE' })
    $mine = @($live | Where-Object { $_.name -ceq $t.name })
    $position = $(if ($mine.Count -gt 0) { [int]$mine[0].position } else { 0 })
    if ($position -ne 1) {
        $seen = Get-VenueClaim -Venue $Venue -ClaimDir $ClaimDir
        return [pscustomobject]@{ granted = $false; state = 'QUEUED'; claim = $seen.claim; path = $seen.path; broken = @(); ticket = $t; queue = $live; position = $position; staleTickets = @($removed) }
    }
    $r = Invoke-VenueClaimAttempt @attemptArgs
    $r | Add-Member -NotePropertyName ticket -NotePropertyValue $t -PassThru | Add-Member -NotePropertyName queue -NotePropertyValue $live -PassThru |
        Add-Member -NotePropertyName position -NotePropertyValue $position -PassThru | Add-Member -NotePropertyName staleTickets -NotePropertyValue @($removed) -PassThru
}

function Get-VenueClaimStatus {
    <#
    .SYNOPSIS
    The venue's claim and its whole line in one object: venue, claim (Get-VenueClaim), tickets (Get-VenueTickets, stale ones included, oldest first).
    #>
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', $NowUtc = $null)
    $now = Resolve-VenueNow $NowUtc
    [pscustomobject]@{ venue = $Venue; claim = (Get-VenueClaim -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now); tickets = (Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now); nowUtc = $now.ToString('o') }
}

function Format-VenueClaimStatus {
    # One CLAIM_STATUS line, one TICKET_QUEUE line, then one TICKET / TICKET_STALE line per ticket with its owner run dir, pid and age.
    param([Parameter(Mandatory)]$Status)
    $c = $Status.claim
    $held = ''
    if ($c.claim) {
        $claimed = ConvertTo-VenueUtc $c.claim.claimedUtc
        $age = $(if ($null -ne $claimed) { [int][Math]::Max(0, ((ConvertTo-VenueUtc $Status.nowUtc) - $claimed).TotalSeconds) } else { 'UNKNOWN' })
        $held = " holderPid=$($c.claim.ownerPid) runDir=$($c.claim.runDir) purpose=$($c.claim.purpose) ageSec=$age expiresUtc=$((ConvertTo-VenueUtc $c.claim.expiresUtc).ToString('o'))"
    }
    "CLAIM_STATUS venue=$($Status.venue) state=$($c.state)$(if ($c.reason) { " reason=$($c.reason)" })$held"
    $live = @($Status.tickets | Where-Object { $_.state -cne 'STALE' })
    "TICKET_QUEUE venue=$($Status.venue) live=$($live.Count) stale=$(@($Status.tickets).Count - $live.Count)"
    foreach ($r in $Status.tickets) {
        $t = $r.ticket
        $who = $(if ($t) { "pid=$($t.ownerPid) runDir=$($t.runDir) purpose=$($t.purpose) ageSec=$($r.ageSec) heartbeatAgeSec=$($r.heartbeatAgeSec) phase=$($t.phase) staleAfterSec=$($t.staleAfterSec)" } else { "state=$($r.state) (unparsed)" })
        if ($r.state -ceq 'STALE') { "TICKET_STALE reason=$($r.reason) $who name=$($r.name)" } else { "TICKET position=$($r.position) $who name=$($r.name)" }
    }
}

function Format-VenueTicketQueue {
    # The line on one short string for a log line: "#1 pid=.. age=..s run=..; #2 ...".
    param([AllowEmptyCollection()][object[]]$Queue = @())
    if (@($Queue).Count -eq 0) { return 'empty' }
    (@($Queue | ForEach-Object { "#$($_.position) pid=$($_.ticket.ownerPid) age=$($_.ageSec)s run=$($_.ticket.runDir)" })) -join '; '
}

function Release-VenueClaim {
    <#
    .SYNOPSIS
    Release the venue's claim if it is still the one with -Nonce. Returns RELEASED | NOT_OURS | ABSENT | BUSY.
    #>
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$Nonce, [string]$ClaimDir = '')
    $path = Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir
    switch (Remove-VenueClaimIfUnchanged -Path $path -ExpectedNonce $Nonce) {
        'REMOVED' { 'RELEASED' }
        'CHANGED' { 'NOT_OURS' }
        default { $_ }
    }
}

function ConvertFrom-VenueGateLog {
    <#
    .SYNOPSIS
    The venue's quiet probes from one or more gate logs: one record per job id (a job id seen twice counts once), hour = the UTC hour of the job id's stamp.
    #>
    param([AllowEmptyCollection()][string[]]$Path = @(), [Parameter(Mandatory)][string]$Venue)
    $rx = [regex]'^PROBE (venue-quiet-probe-(\d{8}T\d{6})\d{3}Z) (\S+) samples=\S+ mean=(\S+) state=(QUIET|BUSY|UNKNOWN)\b'
    $seen = @{}
    $records = [System.Collections.Generic.List[object]]::new()
    foreach ($p in $Path) {
        if ([string]::IsNullOrWhiteSpace($p) -or -not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
        foreach ($line in [IO.File]::ReadAllLines($p)) {
            $m = $rx.Match($line)
            if (-not $m.Success -or $m.Groups[3].Value -cne $Venue) { continue }
            $id = $m.Groups[1].Value
            if ($seen.ContainsKey($id)) { continue }
            $seen[$id] = $true
            $utc = [DateTime]::ParseExact($m.Groups[2].Value, 'yyyyMMddTHHmmss', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal)
            $records.Add([pscustomobject]@{ jobId = $id; venue = $Venue; utc = $utc; hour = $utc.Hour; date = $utc.ToString('yyyy-MM-dd'); state = $m.Groups[5].Value; mean = $m.Groups[4].Value; log = $p })
        }
    }
    , $records.ToArray()
}

function Get-VenueQuietRateTable {
    <#
    .SYNOPSIS
    One row per UTC hour of day that has probes: hourUtc, probes (measured: QUIET + BUSY), quiet, unknown, ratePercent (QUIET / probes, 0.1; $null with no
    measured probe) and days (distinct dates the hour was seen on).
    #>
    param([AllowEmptyCollection()][object[]]$Records = @())
    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($g in @($Records | Group-Object { [int]$_.hour } | Sort-Object { [int]$_.Name })) {
        $quiet = @($g.Group | Where-Object { $_.state -ceq 'QUIET' }).Count
        $busy = @($g.Group | Where-Object { $_.state -ceq 'BUSY' }).Count
        $unknown = @($g.Group | Where-Object { $_.state -ceq 'UNKNOWN' }).Count
        $probes = $quiet + $busy
        $rows.Add([pscustomobject]@{
            hourUtc = [int]$g.Name; probes = $probes; quiet = $quiet; unknown = $unknown
            ratePercent = $(if ($probes -gt 0) { [Math]::Round(100.0 * $quiet / $probes, 1) } else { $null })
            days = @($g.Group | ForEach-Object { $_.date } | Sort-Object -Unique).Count
        })
    }
    , $rows.ToArray()
}

function Format-VenueQuietRateTable {
    param([AllowEmptyCollection()][object[]]$Table = @(), [string]$Venue = '')
    $p = ($Table | Measure-Object -Property probes -Sum).Sum; $q = ($Table | Measure-Object -Property quiet -Sum).Sum; $u = ($Table | Measure-Object -Property unknown -Sum).Sum
    "QUIET_RATE_TABLE venue=$Venue hours=$(@($Table).Count) probes=$([int]$p) quiet=$([int]$q) unknown=$([int]$u)"
    foreach ($r in $Table) {
        "QUIET_RATE hour={0:00}Z probes={1} quiet={2} unknown={3} rate={4} days={5}" -f $r.hourUtc, $r.probes, $r.quiet, $r.unknown, $(if ($null -eq $r.ratePercent) { 'n/a' } else { '{0:0.0}%' -f $r.ratePercent }), $r.days
    }
}

function Select-VenueQuietWindow {
    <#
    .SYNOPSIS
    Whether a chain should run now or wait for a later UTC hour. WAIT only when the current hour has at least -MinProbes measured probes at a rate under
    -PreferRatePercent AND a later hour within -HorizonHours (searched in order, nearest first) has at least -MinProbes at a rate at or above it, and that
    hour starts before -DeadlineUtc. Otherwise RUN_NOW, with the reason.
    .OUTPUTS
    [pscustomobject]@{ action (RUN_NOW | WAIT); reason; currentHour; currentRate; currentProbes; targetHour; targetUtc; targetRate; waitSec }
    #>
    param([AllowEmptyCollection()][object[]]$Table = @(), [DateTime]$NowUtc = [DateTime]::UtcNow, [double]$PreferRatePercent = 30.0,
        [ValidateRange(1, 23)][int]$HorizonHours = 6, [ValidateRange(1, 1000)][int]$MinProbes = 3, $DeadlineUtc = $null)
    $now = $NowUtc.ToUniversalTime()
    $byHour = @{}; foreach ($r in $Table) { $byHour[[int]$r.hourUtc] = $r }
    $cur = $byHour[$now.Hour]
    $out = [ordered]@{ action = 'RUN_NOW'; reason = ''; currentHour = $now.Hour; currentRate = $(if ($cur) { $cur.ratePercent } else { $null }); currentProbes = $(if ($cur) { $cur.probes } else { 0 })
        targetHour = $null; targetUtc = $null; targetRate = $null; waitSec = 0 }
    if ($null -eq $cur -or $cur.probes -lt $MinProbes) { $out.reason = 'current-hour-unmeasured'; return [pscustomobject]$out }
    if ($cur.ratePercent -ge $PreferRatePercent) { $out.reason = 'current-rate-ok'; return [pscustomobject]$out }
    $top = [DateTime]::new($now.Year, $now.Month, $now.Day, $now.Hour, 0, 0, [DateTimeKind]::Utc)
    for ($h = 1; $h -le $HorizonHours; $h++) {
        $start = $top.AddHours($h)
        $r = $byHour[$start.Hour]
        if ($null -eq $r -or $r.probes -lt $MinProbes -or $r.ratePercent -lt $PreferRatePercent) { continue }
        if ($null -ne $DeadlineUtc -and $start -ge ([DateTime]$DeadlineUtc).ToUniversalTime()) { $out.reason = 'window-after-deadline'; $out.targetHour = $start.Hour; $out.targetUtc = $start.ToString('o'); $out.targetRate = $r.ratePercent; return [pscustomobject]$out }
        $out.action = 'WAIT'; $out.reason = 'later-window-better'; $out.targetHour = $start.Hour; $out.targetUtc = $start.ToString('o'); $out.targetRate = $r.ratePercent
        $out.waitSec = [int][Math]::Ceiling(($start - $now).TotalSeconds)
        return [pscustomobject]$out
    }
    $out.reason = 'no-window-in-horizon'
    [pscustomobject]$out
}

Export-ModuleMember -Function Get-VenueMainRoot, Get-VenueClaimPath, Get-VenueClaim, Get-VenueClaimStaleReason, Request-VenueClaim, Release-VenueClaim,
    Get-VenueTicketDir, New-VenueTicket, Get-VenueTickets, Get-VenueTicketStaleReason, Update-VenueTicket, Remove-VenueTicket, Get-VenueClaimStatus, Format-VenueClaimStatus, Format-VenueTicketQueue,
    ConvertFrom-VenueGateLog, Get-VenueQuietRateTable, Format-VenueQuietRateTable, Select-VenueQuietWindow
